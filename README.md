# 🤖 V2 Multi-Account Crypto Futures Scalping Bot

A professional, automated crypto futures scalping system with multi-account support,
layered confluence signals, AI verification, dynamic risk management, and encrypted API key storage.

Built with FastAPI, PostgreSQL, OpenAI GPT-4o, Binance Futures API, and n8n workflow automation.

---

## ⚠️ DISCLAIMER

Trading crypto futures involves significant risk of loss. This system is provided for
educational and research purposes. Always start on **Binance Testnet** and never risk
more than you can afford to lose. Past performance does not guarantee future results.

---

## 🏗️ Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                        n8n Workflow (Every 5m)                       │
│  [Schedule] → [Scan] → [Batch Analyze] → [Filter] → [Execute Multi] │
└────────────────────────────────┬─────────────────────────────────────┘
                                 │ HTTP
┌────────────────────────────────▼─────────────────────────────────────┐
│                     FastAPI Backend (V2)                              │
│  ┌──────────┐  ┌──────────┐  ┌────────────┐  ┌───────────────────┐  │
│  │ Scanner  │  │ Analyzer │  │ AI Engine  │  │ Multi-Acc Executor│  │
│  └──────────┘  └──────────┘  │ (OpenAI +  │  └───────────────────┘  │
│  ┌──────────┐  ┌──────────┐  │ Technical) │  ┌───────────────────┐  │
│  │OrderBook │  │Risk Eng. │  └────────────┘  │ Accounts Manager  │  │
│  └──────────┘  └──────────┘                  └───────────────────┘  │
└──────┬──────────────┬──────────────┬──────────────────┬──────────────┘
       │              │              │                  │
┌──────▼──────┐ ┌─────▼──────┐ ┌────▼─────┐  ┌────────▼────────┐
│  Binance    │ │ PostgreSQL │ │ OpenAI   │  │   Telegram Bot  │
│  Futures    │ │  Database  │ │ GPT-4o   │  │   Notifications │
└─────────────┘ └────────────┘ └──────────┘  └─────────────────┘
```

---

## 📁 Project Structure

```
crypto_bot/
├── app/
│   ├── main.py                    # FastAPI entry point + DB lifecycle
│   ├── config.py                  # All settings from env vars
│   ├── database.py                # Async SQLAlchemy engine + sessions
│   ├── models/
│   │   ├── user.py                # User, Account, ApiConnection, Balance
│   │   ├── trading.py             # Signal, Trade, Position, TradeSkip
│   │   └── system.py              # Setting, Subscription, AuditLog
│   ├── modules/
│   │   ├── scanner.py             # Market scanning + volume ranking
│   │   ├── analyzer.py            # Technical indicators (VWAP, EMA, RSI, ATR)
│   │   ├── ai_engine.py           # Confluence + OpenAI verification
│   │   ├── risk_engine.py         # Balance-based dynamic risk
│   │   ├── executor.py            # Binance trade execution
│   │   ├── orderbook.py           # L2 order book analysis
│   │   ├── telegram.py            # Premium Telegram notifications
│   │   └── crypto_utils.py        # API key encryption/decryption
│   ├── routers/
│   │   ├── scanner.py             # GET  /api/v1/scan
│   │   ├── analyzer.py            # POST /api/v1/analyze, /analyze-batch
│   │   ├── executor.py            # POST /api/v1/execute, /execute-full, /execute-multi
│   │   ├── status.py              # GET  /api/v1/status
│   │   └── accounts.py            # CRUD /api/v1/accounts
│   └── utils/
│       ├── logger.py              # Rotating file + console logger
│       └── state.py               # In-memory trade state manager
├── migrations/
│   └── schema.sql                 # PostgreSQL schema (auto-runs on first start)
├── n8n_workflow_v2.json           # Import into n8n
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

---

## 🚀 Quick Start

### Step 1: Configure Environment

```bash
cp .env.example .env
```

Edit `.env` with your actual values:

```env
# Required
BINANCE_API_KEY=your_binance_api_key
BINANCE_SECRET_KEY=your_binance_secret_key
BINANCE_TESTNET=true

# Generate encryption key
# python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
ENCRYPTION_KEY=your_generated_fernet_key

# Optional (needed for AI verification)
OPENAI_API_KEY=your_openai_key

# Optional (for notifications)
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

### Step 2: Start Everything

```bash
docker-compose up -d --build
```

This starts:
- **PostgreSQL** (port 5432) — auto-creates tables from `schema.sql`
- **Trading Bot** (port 8000) — FastAPI backend
- **n8n** (port 5678) — Workflow automation

### Step 3: Verify

```bash
# Health check
curl http://localhost:8000/health

# Test scan
curl http://localhost:8000/api/v1/scan

# Check status
curl http://localhost:8000/api/v1/status
```

### Step 4: Add Trading Accounts

```bash
# Add your first account
curl -X POST http://localhost:8000/api/v1/accounts \
  -H "Content-Type: application/json" \
  -d '{
    "label": "Main Account",
    "api_key": "your_binance_api_key",
    "api_secret": "your_binance_secret_key"
  }'

# Test the connection
curl -X POST http://localhost:8000/api/v1/accounts/1/test

# Add more accounts for multi-account trading
curl -X POST http://localhost:8000/api/v1/accounts \
  -H "Content-Type: application/json" \
  -d '{
    "label": "Client Account 2",
    "api_key": "client_api_key",
    "api_secret": "client_api_secret"
  }'
```

### Step 5: Import n8n Workflow

1. Open n8n at `http://localhost:5678` (admin / changeme)
2. **Workflows** → **Import from file** → Select `n8n_workflow_v2.json`
3. Update Telegram credentials in the workflow
4. **Activate** the workflow
5. Bot runs every 5 minutes automatically

---

## 📊 Signal Logic — Layered Confluence

### Entry Conditions (ALL must align)

| # | LONG Condition | SHORT Condition |
|---|---|---|
| 1 | EMA 9 > EMA 21 | EMA 9 < EMA 21 |
| 2 | Price above VWAP | Price below VWAP |
| 3 | RSI 52-68 | RSI 32-48 |
| 4 | Volume spike (>1.5x avg) | Volume spike (>1.5x avg) |
| 5 | Spread < 0.15% | Spread < 0.15% |
| 6 | ATR% < max volatility | ATR% < max volatility |
| 7 | Bullish candle confirmed | Bearish candle confirmed |
| 8 | 15m HTF trend bullish | 15m HTF trend bearish |

**Minimum 5/8 conditions** required for a signal. Confidence scales with matching conditions.

### Avoid Trade If
- Sideways chop detected (EMAs converging)
- Spread too high (>0.15%)
- ATR% exceeds max volatility
- Weak volume
- Existing open position on same symbol

### AI Verification (Optional Layer 2)
- OpenAI receives indicator + orderbook summary
- Returns strict JSON: `{"action": "BUY", "confidence": 87, "reason": "..."}`
- If AI agrees with technical → confidence boosted
- If AI disagrees → confidence reduced
- If AI fails → falls back to technical rules only

---

## 💰 Risk Management Math

### Balance Risk Tiers

| Account Balance | Risk % per Trade |
|---|---|
| $20 – $100 | 8% |
| $101 – $300 | 6% |
| $301 – $1,000 | 4% |
| $1,000+ | 2% |

### Position Sizing Formula

```
safe_margin   = balance × risk_percent
position_size = safe_margin × leverage
quantity      = position_size / entry_price
```

**Example:**
```
Balance = $50
Risk    = 8% (tier: $20-$100)
Margin  = $4.00
Leverage = 5x (confidence 75)
Position = $20.00
```

### Leverage by Confidence

| Confidence | Leverage |
|---|---|
| < 70 | ❌ NO TRADE |
| 65 – 79 | 5x |
| 80 – 89 | 8x |
| 90 – 94 | 10x |
| 95+ | 12x |

### TP/SL by Confidence

| Confidence | Take Profit | Stop Loss |
|---|---|---|
| 65 – 79 (Low) | 5% | 2% |
| 80 – 89 (Med) | 10% | 5% |
| 90+ (High) | 15% | 6% |

> SL widens slightly for volatile coins (ATR-adjusted).

### Safe Margin Caps

| Balance | Max Single-Trade Margin |
|---|---|
| $20 | ~$2 |
| $50 | ~$4 |
| $100 | ~$7 |
| $300+ | ~$15 |

### Symbol Minimum Filter

Before every trade:
1. Fetch Binance symbol filters (minQty, stepSize, tickSize, minNotional)
2. If position < minNotional → try bumping to minimum
3. If bump would exceed safe margin → **SKIP** (account too small for this coin)

---

## 🔒 Security

### API Key Encryption
- All API keys encrypted with **AES-256 (Fernet)** before database storage
- Keys never stored in plaintext, never logged, never returned in API responses
- Display uses masked format: `abcd...wxyz`

### API Key Permissions
- Only **futures trading** permission required
- **Never** enable withdrawal permission
- Use IP whitelisting on Binance where possible

### Encryption Key
Generate with:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

---

## 📱 Telegram Notifications

### Trade Opened
```
✅ TRADE OPENED
Coin: SOLUSDT
Side: 🟢 LONG
Entry: $145.22
Leverage: 8x
Size: $15.00
TP: $146.40
SL: $144.60
Confidence: 87%
```

### Multi-Account Signal Summary
```
📊 SIGNAL SUMMARY
Coin: BTCUSDT
Side: 🟢 LONG
Confidence: 87%

✅ Executed: 12 accounts
⏭️ Skipped: 5 accounts

Skip Reasons:
  • 3 Low Balance
  • 1 Risk Limit
  • 1 Min Notional
```

---

## 🔌 API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | Service health check |
| GET | `/api/v1/scan` | Scan market for candidates |
| POST | `/api/v1/analyze` | Analyze single coin |
| POST | `/api/v1/analyze-batch` | Batch analyze multiple coins |
| POST | `/api/v1/execute` | Simple single-account trade |
| POST | `/api/v1/execute-full` | Full single-account with risk engine |
| POST | `/api/v1/execute-multi` | Multi-account execution |
| GET | `/api/v1/status` | Full dashboard status |
| POST | `/api/v1/accounts` | Create trading account |
| GET | `/api/v1/accounts` | List all accounts |
| GET | `/api/v1/accounts/{id}` | Get account details |
| PUT | `/api/v1/accounts/{id}` | Update account |
| DELETE | `/api/v1/accounts/{id}` | Deactivate account |
| POST | `/api/v1/accounts/{id}/test` | Test API connection |

---

## 🛡️ Trade Limits & Safety

| Control | Default |
|---|---|
| Hourly max trades | 10 |
| Daily max trades | 100 |
| Coin cooldown | 30 minutes |
| Max coin repeats/hour | 2 |
| Loss cooldown trigger | 3 consecutive losses |
| Loss cooldown duration | 15 minutes |
| Daily profit limit | 150% |
| Daily loss limit | -20% |
| Drawdown pause | -10% |
| Max volatility (ATR%) | 5% |

---

## 🧪 Testing on Binance Testnet

1. Set `BINANCE_TESTNET=true` in `.env`
2. Get testnet API keys from: https://testnet.binancefuture.com
3. Start the system: `docker-compose up -d --build`
4. Add testnet account via API
5. Monitor logs: `docker logs crypto-trading-bot -f`
6. Check Telegram for trade notifications
7. Run for 1-2 weeks before considering mainnet

---

## 📊 Database Tables

| Table | Purpose |
|---|---|
| `users` | User accounts |
| `accounts` | Trading accounts (1 user → many accounts) |
| `api_connections` | Encrypted Binance API keys |
| `balances` | Account balances |
| `signals` | Generated trading signals + AI logs |
| `trades` | Executed trades with P&L |
| `positions` | Open positions |
| `trade_skips` | Skipped trades with reasons |
| `settings` | Per-account risk overrides |
| `subscriptions` | Subscription plans (for future website) |
| `audit_logs` | Security audit trail |

---

## 🔧 Multi-Account Execution Flow

```
Signal Generated (BUY SOLUSDT, conf=87)
    │
    ├── Save signal to DB
    │
    ├── Load all active accounts
    │
    └── For EACH account (parallel):
        ├── Decrypt API keys
        ├── Fetch live balance from Binance
        ├── Calculate risk % (balance tier)
        ├── Calculate leverage (from confidence)
        ├── Calculate safe margin + position size
        ├── Fetch symbol filters (minQty, minNotional)
        ├── Validate: position >= minimum?
        │   ├── YES → Place trade + SL + TP → Save to DB
        │   └── NO  → Safe to bump? 
        │       ├── YES → Use minimum size → Place trade
        │       └── NO  → Skip (log reason to DB)
        └── Include in Telegram summary
```

---

## 📋 Logs

- Application logs: `logs/trading_bot.log` (10 MB × 5 rotations)
- Docker logs: `docker logs crypto-trading-bot -f`
- Database: All trades, signals, and skips persisted in PostgreSQL

---

## 🚀 Deployment

### VPS Requirements
- 2+ CPU cores
- 4 GB+ RAM
- 20 GB+ disk
- Docker + Docker Compose installed

### Production Checklist
- [ ] Generate unique `ENCRYPTION_KEY`
- [ ] Set `BINANCE_TESTNET=false` (only after thorough testing!)
- [ ] Change n8n password from `changeme`
- [ ] Set PostgreSQL password to something strong
- [ ] Enable firewall (only expose ports you need)
- [ ] Set up monitoring (healthcheck endpoint)
- [ ] Review Telegram notifications daily
- [ ] Never enable withdrawal API permissions
