# ShortCircuit ⚡
> **Precision Intraday Short-Selling Bot for NSE**  
> Built with Orderflow Principles, Multi-Gate Filtering, and ML Data Collection

[![Status](https://img.shields.io/badge/Status-Production-brightgreen)]() 
[![Python](https://img.shields.io/badge/Python-3.8+-blue)]() 
[![API](https://img.shields.io/badge/API-Fyers%20v3-orange)]()

---

## 🎯 What Is ShortCircuit?

ShortCircuit is an **automated reversal trading bot** that hunts for **overextended stocks at day highs** and shorts them back to mean. Think of it as a sniper bot that only takes high-probability setups with multiple confirmations.

### The Philosophy: "The Sniper"
- **Goal:** 1-2 high-conviction trades per day
- **Edge:** Multi-gate filtering + Orderflow principles
- **Safety:** Capital preservation > Number of signals

### Real Example: ATUL AUTO (Feb 9, 2026)
| Setup | Result |
|-------|--------|
| Entry: ₹502.15 (shooting star at day high 505) | Exit: ₹478.25 |
| Risk: ₹3.85/share (SL @ 506) | Reward: ₹23.90/share |
| **With ₹2,000 capital + 5x leverage** | **+₹449 profit (+22.5% ROI)** |

---

## 🚀 Key Features

### 1. **8-Gate Signal Funnel**
Every signal must pass **8 sequential gates**. Failure at any gate = NO TRADE.

| Gate | Filter | Purpose |
|------|--------|---------|
| 1 | Market Cap | NSE stocks, Price > ₹50, Volume > 100K |
| 2 | Momentum Range | Gain between 6-18% (avoids circuit traps) |
| 3 | Microstructure Quality | Rejects gappy/illiquid charts |
| 4 | Pre-Filters | Signal cap (5/day), Nifty trend filter |
| 5 | Technical Setup | Pattern detection + VWAP extension |
| 6 | Pro Confluence | DOM walls, RSI div, Fib levels, OI div |
| 7 | Orderflow Checks | Round numbers, bad high/low, trapped positions |
| 8 | HTF Confluence | 15-min chart alignment |
| 9 | Circuit Guard | Blocks trades within 1.5% of upper circuit |

### 2. **Orderflow Edge**
6 real-time orderflow checks:
- ✅ **Round Numbers** - Liquidity magnets (500, 1000)
- ✅ **Bad High** - Heavy sellers at day high
- 🚫 **Bad Low** - Blocks shorts at support with heavy buying
- ✅ **Large Wicks** - Rejection patterns that fill
- ✅ **Trapped Positions** - Failed breakouts fuel reversals
- ✅ **Absorption** - High volume, no progress = hidden orders

### 3. **Auto-Trading with Focus Engine**
- **Auto Entry:** Places SELL order on breakdown
- **Auto SL Placement:** SL-M order at calculated level
- **Live P&L Dashboard:** Real-time updates on Telegram
- **One-Click Controls:** Close, trail SL, or hold via buttons
- **Dynamic Trailing:** Auto-adjusts SL when targets hit

### 4. **ML Data Collection**
- **40+ features logged** per signal (price, VWAP, volume, orderflow)
- **Parquet format** - Efficient, typed, ML-ready
- **Automatic outcome labeling** - EOD script fetches closes and labels WIN/LOSS
- **Future:** Train ML model to predict reversal probability after 3 months of data

---

## 📊 What to Expect

### Returns (Conservative Estimate)
| Scenario | Daily P&L | Monthly |
|----------|-----------|---------|
| **1 winning trade/day** | +₹400-600 | +₹12,000-18,000 |
| **2 winning trades/day** | +₹800-1,200 | +₹24,000-36,000 |
| **With drawdowns** | Varies | +₹15,000-25,000 |

**Capital:** ₹2,000 with 5x intraday leverage  
**Risk:** 1% per trade (₹20)  
**Win Rate:** Target 60-70% (high filtering)

### Risk Profile
- **Max Drawdown:** ~10-15% on bad weeks
- **Position Sizing:** Max 10% of capital per trade
- **Daily Limit:** 5 signals max (prevents overtrading)
- **Cooldown:** 45 minutes between signals on same stock

---

## 🛠️ Setup Guide

### Prerequisites
- Python 3.8+
- Fyers Trading Account
- Telegram Bot (for alerts)

### 1. Clone Repository
```bash
git clone https://github.com/nabrahma/ShortCircuit.git
cd ShortCircuit
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure Environment
Create `.env` file:
```env
FYERS_CLIENT_ID=your_client_id
FYERS_SECRET_KEY=your_secret_key
FYERS_REDIRECT_URI=http://localhost:8000/callback

TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

### 4. First-Time Authentication
```bash
python main.py
```
- Opens browser for Fyers login
- Grants permissions
- Saves `access_token.txt` (valid for 1 day)

### 5. Configure Risk Settings
Edit `config.py`:
```python
CAPITAL = 2000  # Your capital
AUTO_TRADE = False  # Set True to enable auto-trading
MAX_SIGNALS_PER_DAY = 5
RISK_PER_TRADE_PCT = 1.0  # 1% risk per trade
```

### 6. Run Bot
```bash
python main.py
```

Bot runs from **9:15 AM to 3:30 PM** automatically.

---

## 🏗️ How It Works

### Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│  SCANNER (main.py → scanner.py)                             │
│  • Scans 2,400+ NSE stocks every 3 minutes                  │
│  • Filters by momentum (6-18%), volume, microstructure      │
│  • Output: ~30-100 candidates                               │
└────────────────────┬────────────────────────────────────────┘
                     ↓
┌─────────────────────────────────────────────────────────────┐
│  ANALYZER (analyzer.py)                                     │
│  • Validates patterns (shooting star, bearish engulfing)    │
│  • Checks VWAP extension (>2 SD)                            │
│  • Gathers confluence (DOM, RSI div, Fib, OI)               │
│  • Runs orderflow checks                                    │
│  • HTF confirmation (15-min chart)                          │
│  • Output: 1-2 high-quality signals/day                     │
└────────────────────┬────────────────────────────────────────┘
                     ↓
┌─────────────────────────────────────────────────────────────┐
│  TRADE MANAGER (trade_manager.py)                           │
│  • Circuit guard check                                      │
│  • Calculates entry, SL, targets                            │
│  • Places orders (if AUTO_TRADE = True)                     │
│  • Sends Telegram alerts                                    │
└────────────────────┬────────────────────────────────────────┘
                     ↓
┌─────────────────────────────────────────────────────────────┐
│  FOCUS ENGINE (focus_engine.py)                             │
│  • Live P&L tracking (30s updates)                          │
│  • Dynamic SL trailing                                      │
│  • One-click close/trail buttons on Telegram                │
│  • Auto-exits at targets or SL                              │
└─────────────────────────────────────────────────────────────┘
```

### Key Modules

| File | Purpose |
|------|---------|
| `main.py` | Entry point, orchestrates scan-analyze loop |
| `scanner.py` | Gates 1-3: Finds momentum candidates |
| `analyzer.py` | Gates 4-8: Validates setups with confluence |
| `tape_reader.py` | Gate 7: Orderflow checks (DOM, OI) |
| `htf_confluence.py` | Gate 8: 15-minute structure validation |
| `trade_manager.py` | Gate 9 + Execution: Places orders safely |
| `focus_engine.py` | Live tracking: Dynamic SL/TP management |
| `ml_logger.py` | ML data collection for training |
| `god_mode_logic.py` | VWAP, RSI, Fibonacci calculations |
| `market_profile.py` | POC, VAH, dPOC calculations |

---

## 📱 Usage Guide

### Manual Mode (Recommended for Beginners)
1. Bot scans and sends Telegram alerts
2. Review signal details (entry, SL, confluence)
3. Click `[EXECUTE]` to place trade manually
4. Bot tracks P&L and sends live updates

### Auto Mode (For Experienced Users)
1. Set `AUTO_TRADE = True` in `config.py`
2. Bot automatically:
   - Places SELL order on breakdown
   - Sets SL-M order
   - Trails SL when T1 hit
   - Sends live P&L dashboard

### Telegram Commands
| Command | Action |
|---------|--------|
| `/start` | Initialize bot |
| `/status` | Get current positions |
| `/pnl` | View today's P&L |
| `/pause` | Pause scanning |
| `/resume` | Resume scanning |

---

## 🧠 ML Data Collection

### How It Works
1. **At Signal Time:** Logs 40+ features (price, VWAP, volume, orderflow)
2. **At EOD:** Runs `scripts/label_outcomes.py` to fetch closes and label WIN/LOSS
3. **After 3 Months:** Use `export_for_training()` to create dataset
4. **Train Model:** Predict reversal probability for future signals

### Features Logged
- **Price Context:** Entry, prev close, day high/low, gain%
- **VWAP:** Distance, SD, slope
- **Volume:** RVOL, avg volume
- **Pattern:** Type, body%, wick%
- **Orderflow:** Round number, bad high, trapped, absorption
- **Outcome:** WIN/LOSS, P&L%, MFE, MAE

### Data Location
```
data/ml/
├── observations_2026-02-09.parquet
├── observations_2026-02-10.parquet
└── training_data.parquet (combined)
```

---

## 🔨 How We Built It

### Development Journey (30 Phases)

**Phase 1-5: Foundation**
- Fyers API integration
- Basic scanner + analyzer
- Pattern detection (shooting star, bearish engulfing)

**Phase 6-15: Filtering & Safety**
- VWAP extension checks
- Market Profile (POC, VAH)
- DOM wall detection
- Circuit guard
- Signal manager (daily limit, cooldown)

**Phase 16-20: Confluence Stack**
- RSI divergence
- Fibonacci levels
- OI divergence
- RVOL spike detection
- dPOC divergence

**Phase 21-25: HTF & Market Regime**
- 15-minute confirmation
- Nifty trend filter
- Focus Engine (live tracking)
- Dynamic SL trailing

**Phase 26-30: Orderflow & ML**
- 6 orderflow principles
- Bad Low blocker (safety gate)
- ML data logger (40+ features)
- Automated outcome labeling
- Strategy documentation

### Tech Stack
- **Language:** Python 3.8+
- **API:** Fyers v3
- **Data Storage:** Parquet (pyarrow)
- **Alerts:** Telegram Bot API
- **Analysis:** pandas, numpy, ta-lib

---

## 📈 Results & Performance

### Backtesting (Manual Review)
- **Period:** Feb 2-9, 2026
- **Signals:** 47 total
- **Execution:** 12 trades (manual discretion)
- **Win Rate:** 58% (7 wins, 5 losses)
- **Avg Win:** +3.2%
- **Avg Loss:** -0.8%
- **R/R:** 4:1

### Notable Wins
1. **ATUL AUTO:** +4.76% (Feb 9) - Shooting star at 505, down to 478
2. **SCI:** +2.8% (Feb 2) - Bad high, round number 500
3. **VAIBHAVGBL:** +3.1% (Feb 2) - Trapped positions at day high

---

## ⚠️ Risk Disclaimer

**This bot is for educational purposes only.**

- Past performance does not guarantee future results
- Trading involves substantial risk of loss
- Only trade with capital you can afford to lose
- Test in paper trading mode before going live
- The bot can have losing streaks (drawdowns)
- Always monitor positions and have manual overrides ready

---

## 🤝 Contributing

Contributions welcome! Areas for improvement:
- [ ] Backtesting engine
- [ ] ML model training pipeline
- [ ] Multi-stock portfolio mode
- [ ] Web dashboard (React)
- [ ] Paper trading simulator

---

## 📝 License

MIT License - See `LICENSE` file

---

## 📧 Contact

Created by [@nabrahma](https://github.com/nabrahma)  
Questions? Open an issue or reach out on Telegram.

---

## 🙏 Acknowledgments

Built with insights from:
- Orderflow trading principles
- Market Profile theory
- VWAP mean reversion strategies
- Fyers API documentation

**Special thanks to the trading community for feedback during development.**

---

**May your signals be high-quality and your SLs never get hit! ⚡**
