# 🤖 AI Crypto Futures Trading Bot

A fully automated, AI-powered crypto futures trading system for small accounts ($10–$50).
Built with FastAPI, OpenAI GPT-4o, Binance Futures API, and n8n automation.

---

## ⚠️ DISCLAIMER

Trading crypto futures involves significant risk of loss. This system is provided for
educational and research purposes. Always start on **testnet** and never risk more than
you can afford to lose. Past performance does not guarantee future results.

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        n8n Workflow                         │
│  [Cron 4h] → [Scan] → [Analyze] → [AI] → [Risk] → [Trade] │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTP
┌──────────────────────────▼──────────────────────────────────┐
│                   FastAPI Backend                           │
│  ┌──────────┐  ┌──────────┐  ┌────────────┐  ┌─────────┐  │
│  │ Scanner  │  │ Analyzer │  │ Risk Engine│  │Executor │  │
│  └──────────┘  └──────────┘  └────────────┘  └─────────┘  │
│  ┌──────────┐  ┌──────────┐  ┌────────────┐               │
│  │OrderBook │  │AI Engine │  │Safety Layer│               │
│  └──────────┘  └──────────┘  └────────────┘               │
└────────────┬───────────────────────────┬────────────────────┘
             │                           │
    ┌────────▼────────┐        ┌─────────▼────────┐
    │  Binance Futures│        │    OpenAI GPT-4o  │
    │  USDT Perps     │        │    Decision Engine│
    └─────────────────┘        └──────────────────┘
```

---

## 📁 Project Structure

```
crypto_bot/
├── app/
│   ├── main.py               # FastAPI entry point
│   ├── config.py             # Settings + env vars
│   ├── modules/
│   │   ├── scanner.py        # Market scanning + ranking
│   │   ├── analyzer.py       # Technical indicators
│   │   ├── orderbook.py      # L2 order book analysis
│   │   ├── ai_engine.py      # OpenAI decision engine
│   │   ├── risk_engine.py    # Dynamic risk + safety filters
│   │   ├── executor.py       # Binance trade execution
│   │   └── telegram.py       # Telegram notifications
│   ├── routers/
│   │   ├── scanner.py        # GET /api/v1/scan
│   │   ├── analyzer.py       # POST /api/v1/analyze
│   │   ├── executor.py       # POST /api/v1/execute
│   │   └── status.py         # GET /api/v1/status
│   └── utils/
│       ├── logger.py         # Rotating file + console logger
│       └── state.py          # Trade state persistence
├── n8n_workflow.json          # Import this into n8n
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

---

## 🚀 Quick Start

### Option A: Docker (Recommended)

```bash
# 1. Clone and enter directory
git clone <your-repo>
cd crypto_bot

# 2. Set up environment
cp .env.example .env
nano .env   # Fill in your API keys

# 3. Start everything
docker-compose up -d

# 4. Open n8n
# Visit http://localhost:5678
# Login: admin / changeme
# Import n8n_workflow.json via Workflows → Import
```

### Option B: Local Development

```bash
# 1. Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate     # Windows

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
nano .env

# 4. Run the server
uvicorn app.main:app --reload --port 8000

# 5. Test endpoints
curl http://localhost:8000/health
curl http://localhost:8000/api/v1/scan
curl http://localhost:8000/api/v1/status
```

---

## ⚙️ Configuration

Edit `.env`:

| Variable | Description | Default |
|---|---|---|
| `BINANCE_API_KEY` | Binance Futures API key | required |
| `BINANCE_SECRET_KEY` | Binance Futures secret | required |
| `BINANCE_TESTNET` | Use testnet (paper trade) | `true` |
| `OPENAI_API_KEY` | OpenAI API key | required |
| `OPENAI_MODEL` | GPT model to use | `gpt-4o` |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token | optional |
| `TELEGRAM_CHAT_ID` | Telegram chat ID | optional |
| `ACCOUNT_BALANCE` | Starting USDT balance | `20.0` |
| `MIN_CONFIDENCE` | Minimum AI confidence to trade | `70` |

---

## 📊 Dynamic Risk Tiers

| AI Confidence | Leverage | Capital Risk |
|---|---|---|
| < 70 | NO TRADE | — |
| 70–79 | 5x | 5% |
| 80–89 | 10x | 10% |
| 90–94 | 12x | 15% |
| 95–100 | 15x | 20% |

---

## 🔒 Scanner Filters

| Filter | Rule |
|---|---|
| Coins excluded | BTC, ETH, BNB |
| Price range | $0.001 – $50 |
| 24h volume | > $5,000,000 USDT |
| Price change | > 3% (absolute) |
| Bid-ask spread | < 0.2% |

---

## 🔒 Safety Checks (Pre-Trade)

All must pass or the trade is skipped:
1. No existing open position
2. ATR% < 8% (no extreme volatility spikes)
3. Spread still < 0.3%
4. Not the same coin as last trade
5. Volume not dropped below 50% of threshold

---

## 🔌 API Endpoints

### `GET /api/v1/scan`
Scans market and returns top 3 ranked coins.

### `POST /api/v1/analyze`
Full technical + AI analysis for a single coin.

**Body:**
```json
{
  "symbol": "SOLUSDT",
  "price_change_pct": 5.2,
  "volume_24h": 120000000,
  "score": 78.4,
  "spread_pct": 0.04,
  "bid": 145.20,
  "ask": 145.26
}
```

### `POST /api/v1/execute`
Apply risk engine + safety filters + execute trade.

### `GET /api/v1/status`
Returns bot stats (trades, win rate, PnL).

---

## 📲 n8n Setup Guide

1. Open n8n at `http://localhost:5678`
2. Go to **Workflows** → **Import from file**
3. Select `n8n_workflow.json`
4. Add Telegram credentials:
   - Settings → Credentials → New → Telegram
   - Paste your bot token
5. Set n8n variables:
   - Settings → Variables → `TELEGRAM_CHAT_ID`
6. Activate the workflow
7. The bot will run every 4 hours automatically

---

## 📈 Coin Scoring Formula

```
score = (volatility_pct * 0.4) + (normalized_volume * 0.3) + (trend_strength * 0.3)
```

- **Volatility**: 24h price change % (capped at 30%)
- **Normalized Volume**: Log-scaled to 0–100
- **Trend Strength**: % of up-candles in last 24 hourly candles

---

## 🛡️ Production Safety Checklist

- [ ] Start on **Binance Testnet** (`BINANCE_TESTNET=true`)
- [ ] Run for 2–4 weeks on paper before going live
- [ ] Review AI decisions in Telegram before trusting them
- [ ] Set `ACCOUNT_BALANCE` accurately
- [ ] Never use funds you cannot afford to lose
- [ ] Monitor logs daily: `docker logs crypto-trading-bot -f`

---

## 📋 Logs

- Location: `logs/trading_bot.log`
- Rotates at 10 MB, keeps 5 backups
- Trade state persisted in `logs/trade_state.json`

---

## 🔧 Extending the System

### Add a new indicator
Edit `app/modules/analyzer.py` → add method → include in `to_dict()`

### Change scan interval
Edit the `scheduleTrigger` node in n8n (or modify cron settings)

### Add more safety checks
Edit `app/modules/risk_engine.py` → `SafetyFilter.check()`

### Switch to a different exchange
Replace `app/modules/executor.py` with your exchange's API client
