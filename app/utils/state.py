"""
In-memory trade state tracker with daily risk control.
Tracks last trades for duplicate protection and daily P&L limits.
"""

from dataclasses import dataclass, field
from typing import Optional, List
from datetime import datetime, date
import json
import os

STATE_FILE = "logs/trade_state.json"


@dataclass
class TradeState:
    has_open_trade: bool = False
    open_symbol: Optional[str] = None
    last_traded_symbols: List[str] = field(default_factory=list)
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0

    # Daily risk control
    daily_date: str = ""
    daily_trades: int = 0
    daily_pnl: float = 0.0
    daily_starting_balance: float = 0.0
    trading_paused: bool = False
    pause_reason: str = ""


class StateManager:

    def __init__(self):
        self.state = self._load()

    def _load(self) -> TradeState:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    data = json.load(f)
                return TradeState(**{k: v for k, v in data.items() if k in TradeState.__dataclass_fields__})
            except Exception:
                pass
        return TradeState()

    def save(self):
        os.makedirs("logs", exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(self.state.__dict__, f, indent=2)

    def _check_daily_reset(self, balance: float = 0.0):
        today = date.today().isoformat()
        if self.state.daily_date != today:
            self.state.daily_date = today
            self.state.daily_trades = 0
            self.state.daily_pnl = 0.0
            self.state.daily_starting_balance = balance if balance > 0 else self.state.daily_starting_balance
            self.state.trading_paused = False
            self.state.pause_reason = ""
            self.save()

    def check_daily_limits(self, current_balance: float) -> dict:
        """Check if daily trading limits are hit. Returns status dict."""
        self._check_daily_reset(current_balance)

        if self.state.trading_paused:
            return {"allowed": False, "reason": self.state.pause_reason}

        starting = self.state.daily_starting_balance
        if starting <= 0:
            self.state.daily_starting_balance = current_balance
            starting = current_balance
            self.save()

        # Check daily profit limit (>= 150%)
        if starting > 0:
            daily_pnl_pct = (self.state.daily_pnl / starting) * 100
            if daily_pnl_pct >= 150.0:
                self.state.trading_paused = True
                self.state.pause_reason = f"Daily profit limit hit: {daily_pnl_pct:.1f}% >= 150%"
                self.save()
                return {"allowed": False, "reason": self.state.pause_reason}

            # Check daily loss limit (<= -20%)
            if daily_pnl_pct <= -20.0:
                self.state.trading_paused = True
                self.state.pause_reason = f"Daily loss limit hit: {daily_pnl_pct:.1f}% <= -20%"
                self.save()
                return {"allowed": False, "reason": self.state.pause_reason}

        # Check max trades
        if self.state.daily_trades >= 25:
            self.state.trading_paused = True
            self.state.pause_reason = f"Daily trade limit hit: {self.state.daily_trades} >= 25"
            self.save()
            return {"allowed": False, "reason": self.state.pause_reason}

        return {"allowed": True, "reason": ""}

    def is_duplicate_trade(self, symbol: str) -> bool:
        """Check if symbol was in last 2 trades"""
        last_two = self.state.last_traded_symbols[-2:]
        return symbol in last_two

    def open_trade(self, symbol: str):
        self.state.has_open_trade = True
        self.state.open_symbol = symbol
        self.state.total_trades += 1
        self.state.daily_trades += 1
        self.state.last_traded_symbols.append(symbol)
        # Keep only last 10 symbols in memory
        if len(self.state.last_traded_symbols) > 10:
            self.state.last_traded_symbols = self.state.last_traded_symbols[-10:]
        self.save()

    def close_trade(self, pnl: float):
        self.state.has_open_trade = False
        self.state.open_symbol = None
        self.state.total_pnl += pnl
        self.state.daily_pnl += pnl
        if pnl > 0:
            self.state.winning_trades += 1
        else:
            self.state.losing_trades += 1
        self.save()

    def get_stats(self) -> dict:
        total = self.state.total_trades
        win_rate = (
            self.state.winning_trades / total * 100 if total > 0 else 0
        )
        starting = self.state.daily_starting_balance
        daily_pnl_pct = (self.state.daily_pnl / starting * 100) if starting > 0 else 0.0
        return {
            "total_trades": total,
            "winning_trades": self.state.winning_trades,
            "losing_trades": self.state.losing_trades,
            "win_rate_pct": round(win_rate, 1),
            "total_pnl": round(self.state.total_pnl, 4),
            "has_open_trade": self.state.has_open_trade,
            "open_symbol": self.state.open_symbol,
            "last_traded_symbols": self.state.last_traded_symbols[-2:],
            "daily_date": self.state.daily_date,
            "daily_trades": self.state.daily_trades,
            "daily_pnl": round(self.state.daily_pnl, 4),
            "daily_pnl_pct": round(daily_pnl_pct, 2),
            "trading_paused": self.state.trading_paused,
            "pause_reason": self.state.pause_reason,
        }


# Singleton
state_manager = StateManager()
