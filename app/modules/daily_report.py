"""
V17 Daily Report System — Automated performance analytics at 00:00 UTC

Generates a full Telegram report including:
- Total signals, activated, TP/SL counts
- Win rate
- Best/worst symbol
- LONG vs SHORT breakdown
- Scalp vs Swing breakdown
- Average confidence and activation delay
- Signal diagnostics (rejection reasons)

Triggered by POST /daily-report (called by n8n cron at 00:00 UTC)
"""

import logging
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter
from sqlalchemy import text

from app.database import async_session
from app.modules.telegram import TelegramNotifier
from app.utils.serialization import clean_json_types

router = APIRouter()
logger = logging.getLogger(__name__)


async def _generate_daily_stats(date_str: str) -> dict:
    """Query DB for daily signal stats."""
    try:
        async with async_session() as session:
            # All signals for today
            result = await session.execute(
                text("""
                    SELECT
                        COUNT(*) AS total_signals,
                        COUNT(*) FILTER (WHERE status = 'ENTRY_HIT') AS activated,
                        COUNT(*) FILTER (WHERE result = 'TP') AS tp_hits,
                        COUNT(*) FILTER (WHERE result = 'SL') AS sl_hits,
                        COUNT(*) FILTER (WHERE status = 'CANCELLED') AS missed,
                        COUNT(*) FILTER (WHERE status = 'INVALIDATED') AS invalidated,
                        AVG(confidence) AS avg_confidence,
                        AVG(EXTRACT(EPOCH FROM (entry_hit_at - created_at))/60)
                            FILTER (WHERE entry_hit_at IS NOT NULL) AS avg_activation_min,
                        COUNT(*) FILTER (WHERE side = 'BUY') AS long_count,
                        COUNT(*) FILTER (WHERE side = 'SELL') AS short_count,
                        COUNT(*) FILTER (WHERE strategy_type LIKE 'scalp%') AS scalp_count,
                        COUNT(*) FILTER (WHERE strategy_type LIKE 'swing%') AS swing_count
                    FROM signals
                    WHERE DATE(created_at) = :date_str
                """),
                {"date_str": date_str},
            )
            row = result.fetchone()
            if not row:
                return {}

            stats = {
                "total_signals": int(row.total_signals or 0),
                "activated": int(row.activated or 0),
                "tp_hits": int(row.tp_hits or 0),
                "sl_hits": int(row.sl_hits or 0),
                "missed": int(row.missed or 0),
                "invalidated": int(row.invalidated or 0),
                "avg_confidence": round(float(row.avg_confidence or 0), 1),
                "avg_activation_min": round(float(row.avg_activation_min or 0), 1),
                "long_count": int(row.long_count or 0),
                "short_count": int(row.short_count or 0),
                "scalp_count": int(row.scalp_count or 0),
                "swing_count": int(row.swing_count or 0),
            }

            # Win rate
            decided = stats["tp_hits"] + stats["sl_hits"]
            stats["win_rate"] = round(
                stats["tp_hits"] / decided * 100 if decided > 0 else 0.0, 1
            )

            # Best/worst symbol by result
            sym_result = await session.execute(
                text("""
                    SELECT symbol, side, result, confidence
                    FROM signals
                    WHERE DATE(created_at) = :date_str
                      AND result IS NOT NULL
                    ORDER BY confidence DESC
                """),
                {"date_str": date_str},
            )
            sym_rows = sym_result.fetchall()

            tp_symbols = [r.symbol for r in sym_rows if r.result == "TP"]
            sl_symbols = [r.symbol for r in sym_rows if r.result == "SL"]

            # Most frequent TP symbol = best
            if tp_symbols:
                from collections import Counter
                stats["best_symbol"] = Counter(tp_symbols).most_common(1)[0][0]
            else:
                stats["best_symbol"] = "N/A"

            if sl_symbols:
                from collections import Counter
                stats["worst_symbol"] = Counter(sl_symbols).most_common(1)[0][0]
            else:
                stats["worst_symbol"] = "N/A"

            return stats

    except Exception as e:
        logger.error(f"[DailyReport] DB query failed: {e}")
        return {}


def _format_report(date_str: str, stats: dict) -> str:
    """Format the daily report as a Telegram message."""
    total = stats.get("total_signals", 0)
    activated = stats.get("activated", 0)
    tp = stats.get("tp_hits", 0)
    sl = stats.get("sl_hits", 0)
    missed = stats.get("missed", 0)
    invalidated = stats.get("invalidated", 0)
    win_rate = stats.get("win_rate", 0.0)
    avg_conf = stats.get("avg_confidence", 0.0)
    avg_act = stats.get("avg_activation_min", 0.0)
    long_c = stats.get("long_count", 0)
    short_c = stats.get("short_count", 0)
    scalp_c = stats.get("scalp_count", 0)
    swing_c = stats.get("swing_count", 0)
    best = stats.get("best_symbol", "N/A")
    worst = stats.get("worst_symbol", "N/A")

    activation_rate = round(activated / total * 100 if total > 0 else 0, 1)

    # Win rate emoji
    if win_rate >= 65:
        wr_emoji = "🟢"
    elif win_rate >= 50:
        wr_emoji = "🟡"
    else:
        wr_emoji = "🔴"

    lines = [
        f"📊 *Daily Signal Report — {date_str}*",
        "",
        f"📡 *Signals Generated:* `{total}`",
        f"⚡ *Activated:* `{activated}` ({activation_rate}%)",
        f"🎯 *TP Hit:* `{tp}`  |  ❌ *SL Hit:* `{sl}`",
        f"⏳ *Missed Entry:* `{missed}`  |  🔄 *Invalidated:* `{invalidated}`",
        "",
        f"{wr_emoji} *Win Rate:* `{win_rate}%`",
        f"💡 *Avg Confidence:* `{avg_conf}`",
        f"⏱ *Avg Activation Delay:* `{avg_act}m`",
        "",
        f"📈 *LONG:* `{long_c}`  |  📉 *SHORT:* `{short_c}`",
        f"⚡ *Scalp:* `{scalp_c}`  |  🌊 *Swing:* `{swing_c}`",
        "",
        f"🏆 *Best Symbol:* `{best}`",
        f"💸 *Worst Symbol:* `{worst}`",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "_Generated by V17 Signal Engine_",
    ]

    return "\n".join(lines)


@router.post("/daily-report")
async def generate_daily_report(date: str = None):
    """
    Generate and send daily Telegram report.
    Called by n8n cron at 00:00 UTC.
    Optionally pass ?date=YYYY-MM-DD for historical reports.
    """
    if not date:
        # Default: yesterday (report covers the completed day)
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        date = yesterday.strftime("%Y-%m-%d")

    logger.info(f"[DailyReport] Generating report for {date}...")

    stats = await _generate_daily_stats(date)

    if not stats:
        logger.warning(f"[DailyReport] No data for {date}")
        return clean_json_types({
            "status": "ok",
            "date": date,
            "message": "No signals found for this date",
            "stats": {},
        })

    message = _format_report(date, stats)

    # Send to Telegram
    sent = False
    try:
        telegram = TelegramNotifier()
        # Use send_simple if it exists, else fallback
        if hasattr(telegram, "send_simple"):
            await telegram.send_simple(message)
        else:
            await telegram.send_message(message)
        sent = True
        logger.info(f"[DailyReport] Sent for {date}: {stats.get('total_signals',0)} signals, {stats.get('win_rate',0)}% win rate")
    except Exception as e:
        logger.error(f"[DailyReport] Telegram send failed: {e}")

    return clean_json_types({
        "status": "ok",
        "date": date,
        "telegram_sent": sent,
        "stats": stats,
        "report_preview": message[:500],
    })


@router.get("/daily-report/preview")
async def preview_daily_report(date: str = None):
    """Preview today's stats without sending Telegram."""
    if not date:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    stats = await _generate_daily_stats(date)
    message = _format_report(date, stats) if stats else "No data"

    return clean_json_types({
        "status": "ok",
        "date": date,
        "stats": stats,
        "report": message,
    })
