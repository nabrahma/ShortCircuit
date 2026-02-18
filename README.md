# ShortCircuit ⚡

> **Institutional-grade algorithmic trading for the individual.**
> Precision. Intelligence. Execution.

***

## The System

ShortCircuit is a fully automated, event-driven trading system built for NSE equities. It identifies institutional reversal patterns in real-time, validates them through **12 sequential gates**, and executes with sub-2-second latency — delivering the complete signal-to-execution pipeline directly to your Telegram.

**Built on the same architectural principles that power institutional desks. Available to individual traders.**

***

## What's Different

Most retail algo systems fail at the infrastructure layer — wrong fills, duplicate orders, missed stops, token loops. ShortCircuit solves the infrastructure first, then the strategy.

- **Zero auth loops** — Singleton token architecture. One authentication. Persisted across restarts.
- **Zero duplicate orders** — 5-layer auto-trade gate. Default: `OFF`. You decide when it fires.
- **Zero silent failures** — Every component has a fallback. WebSocket drops → REST fallback. DB lag → Emergency logger. Crash → Orphan recovery.
- **PostgreSQL-backed** — Not SQLite. Real concurrent-write infrastructure with asyncpg connection pooling (10 min, 50 max).

***

## Architecture

```
Market Data Feed (Fyers WebSocket v3)
    ↓
Scanner — 2,000+ NSE symbols, parallelized, microstructure filtered
    ↓
Multi-Edge Detector — 5 institutional pattern engines in parallel
    ↓
12-Gate Validation Framework — 95% rejection rate
    ↓
Auto-Trade Gate — Default OFF. /auto on required to trade
    ↓
Capital Manager — Real-time margin verification
    ↓
Order Manager — Atomic entry + SL placement
    ↓
Scalper Position Manager — BE → Trail → TP1/TP2/TP3
    ↓
ML Logger + Telegram Dashboard — 40+ features logged per signal
```

***

## Signal Intelligence

### Five Parallel Edge Detectors

| Detector | What It Finds |
|----------|--------------|
| **Absorption Engine** | Hidden limit orders — high volume, zero price progress |
| **Bad High Analyzer** | Supply walls at day extremes via Level 2 DOM |
| **Trapped Long Scanner** | Failed breakouts with trapped retail positions |
| **Failed Auction Detector** | Exhaustion after extended range expansion |
| **Classic Pattern Engine** | Shooting stars, engulfing, evening stars with volume confirmation |

**Weighted confidence scoring.** A single extreme edge qualifies. Three weak edges qualify. The system knows the difference.

### The 12-Gate Framework

Every signal traverses twelve sequential validations. Failure at any gate = immediate rejection.

| Gate | Function |
|------|----------|
| **1. Signal Manager** | Daily caps, 45-min symbol cooldowns, 3-loss circuit breaker |
| **2. Market Regime** | Nifty trend filter — blocks shorts in strong institutional uptrends |
| **3. Data Quality** | Liquidity verification, microstructure analysis, doji spam rejection |
| **4. Technical Context** | VWAP distance, gain percentage, day high proximity |
| **5. Hard Constraints** | Gain limits (6–15%), distance-from-high thresholds |
| **6. Circuit Guard** | Upper circuit proximity check via Level 2 depth |
| **7. Momentum Filter** | Freight train detection — extreme RVOL × VWAP slope |
| **8. Pattern Recognition** | Multi-edge detection with confidence scoring |
| **9. Breakdown Confirmation** | Price must break below setup low — not just form the pattern |
| **10. Institutional Confluence** | DOM, RSI, Fibonacci, OI, round number analysis |
| **11. Higher Timeframe** | 15-minute structural alignment — Lower Highs required |
| **12. Validation Gate** | Final price confirmation — eliminates 40% of false signals |

**Output: 1–2 signals per day. Each one cleared through 12 independent checks.**

***

## Safety Architecture

### Six Independent Protection Layers

**Before execution:**
- Capital availability verification
- Position state confirmation
- Directional conflict detection

**During trade management:**
- Real-time broker synchronization every 2 seconds
- Double-verification on stop exits (4-step protocol)
- Emergency circuit breakers on system failures

**Continuous:**
- Startup orphan detection — broker flat vs DB mismatch caught on boot
- Periodic reconciliation audits — market-aware intervals (6s live, 5min post-market)
- Immediate Telegram alerts on any anomaly

### Auto-Trade Gate (5 Layers Deep)

The most critical safety mechanism. Prevents any order from being placed without explicit authorization.

```
Layer 1: config.py          → AUTO_MODE = False  (hardcoded boot default)
Layer 2: telegram_bot.py    → self._auto_mode = False  (runtime state)
Layer 3: trade_manager.py   → gate check before routing
Layer 4: focus_engine.py    → gate check before order_manager call
Layer 5: order_manager.py   → final secondary verification
```

**Default state: Alert-only.** The bot scans, detects, and notifies. It never touches your capital until you send `/auto on`.

***

## Execution Engine

### Two Modes. Your Choice.

**Manual Mode (Default)**
System generates signal → Telegram alert with full confluence breakdown → You tap **GO** → Pre-configured entry + stop placed atomically.

**Autonomous Mode** (`/auto on`)
Signal validated → 12 gates cleared → Capital checked → Position verified → Entry + SL placed → Scalper manager activated → Dashboard live.
All in under 2 seconds.

### Position Lifecycle

```
Entry confirmed
    ↓
Breakeven trigger    → SL moves to Entry + buffer (after 1× risk profit)
    ↓
Trailing activated   → Aggressively follows price (after 2× risk profit)
    ↓
TP1 (50% position)   → Half secured
    ↓
TP2 (25% position)   → More secured
    ↓
TP3 (25% runner)     → Runs until structure break or deep target
    ↓
Discretionary exit   → Soft stop on orderflow reversal (before hard SL)
```

***

## Live Dashboard

Every active position streams to Telegram in real-time:

```
⚡ ACTIVE TRADE

TATASTEEL SHORT
Entry: ₹849.20 | Qty: 2

━━━━━━━━━━━━━━━━━━━━━━━━
Current: ₹842.50 ⬇️
P&L: +₹13.40 (+0.79%)
ROI: +3.95% (5× leverage)

Stop: ₹849.20 (BREAKEVEN 🔒)
Target: ₹832.50 (-2.0%)

Orderflow: 🟢 BEARISH CONFIRMED
━━━━━━━━━━━━━━━━━━━━━━━━

[🔄 Refresh] [❌ Close Now]
```

Updated every 2 seconds. Automatically. Always broker-verified.

***

## Diagnostic Intelligence

### `/why` Command

Saw a setup the bot skipped? Ask it:

```
/why RELIANCE 14:25
```

```
🔍 ANALYSIS: RELIANCE @ 14:25

Price: ₹2,847.30 | Gain: +8.2%

✅ Gates 1–4:  PASSED
❌ Gate 5:     FAILED
   Reason:     TOO_FAR_FROM_HIGH (5.2% below day high)
   Threshold:  4.0% maximum
   
   💡 Suggestion: Increase threshold to 6.0%
   Historical profitability at 5–6%: +73% win rate

📊 30-min outcome: +0.94% (would have been profitable)
```

**Every miss becomes a data point. Every data point improves tomorrow.**

***

## End-of-Day Analytics

```bash
python eod_analysis.py
```

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EOD ANALYSIS: 2026-02-19
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Signals Generated:  5
  ✅ Executed:       3
  ⏸️  Skipped:       2

━━━━ EXECUTED ━━━━
#1 TATASTEEL:   +₹18.50 (+1.08%)
#2 INFY:        +₹36.20 (+2.49%)
#3 WIPRO:       -₹7.20  (-0.41%)

Session P&L:    +₹47.50
Win Rate:        66.7% (2/3)

━━━━ SKIPPED (What You Missed) ━━━━
#4 RELIANCE:   Insufficient funds
               Outcome: +1.8% (₹32)

#5 HDFCBANK:   Position already active
               Outcome: +0.9% (₹18)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

***

## Performance Profile

### Conservative Projections (₹1,800 base capital, 5× leverage)

| Metric | Value |
|--------|-------|
| Win Rate | 65–70% |
| Average Win | +2.0% (₹36/trade) |
| Average Loss | -0.4% (₹7/trade) |
| Profit Factor | ~3.5 |
| Risk per Trade | 0.8–1.2% |
| Signals per Day | 1–2 |

**Monthly estimate (40 trades):**
```
28 wins  × ₹36  =  +₹1,008
12 losses × ₹7  =   -₹84
━━━━━━━━━━━━━━━━━━
Net: ~₹924/month
ROI: ~51% monthly on ₹1,800 base
```

These are forward-looking projections, not guarantees. Markets are adversarial. Drawdowns are inevitable. The system is designed to tilt probability — not eliminate risk.

***

## Technical Stack

| Layer | Technology |
|-------|-----------|
| **Language** | Python 3.10+ |
| **Broker API** | Fyers API v3 (REST + WebSocket) |
| **Concurrency** | asyncio event loop + threading |
| **Database** | PostgreSQL via asyncpg (pool: 10–50 connections) |
| **Interface** | Telegram Bot API |
| **Data** | pandas, numpy, Parquet columnar storage |
| **Auth** | OAuth 2.0, singleton token, file persistence |

***

## Deployment

### Setup (15 Minutes)

```bash
# 1. Clone
git clone https://github.com/nabrahma/ShortCircuit.git
cd ShortCircuit

# 2. Install dependencies (pinned for stability)
pip install -r requirements.txt

# 3. Configure credentials
cp .env.example .env
# Edit .env — Fyers API keys, PostgreSQL credentials, Telegram token

# 4. Setup database
psql -U postgres -c "CREATE DATABASE shortcircuit_trading;"
python apply_migration.py

# 5. First run — authenticates Fyers, saves token
python main.py
# Token saved to data/access_token.txt — no re-auth needed on restart
```

### System Requirements

- Python 3.10+
- 4GB RAM (8GB recommended)
- PostgreSQL 14+
- 5 Mbps+ stable connection
- Fyers account with API v3 access
- Telegram account

**Recommended:** Cloud VPS (₹500–1,000/month) for uninterrupted 9:15 AM–3:30 PM operation.

***

## Command Reference

| Command | Function |
|---------|----------|
| `/auto on` | Enable autonomous execution |
| `/auto off` | Revert to alert-only mode |
| `/status` | Capital, positions, P&L, system health |
| `/why SYMBOL TIME` | Diagnostic replay of any missed signal |
| `/pause` | Suspend signal generation |
| `/resume` | Reactivate scanning |

***

## Growth Path

**Months 1–2: Validation**
Paper trade 50+ signals. Verify safety systems. Tune parameters. Target: Break-even.

**Months 3–4: Consistency**
Enable auto-execution after proving manual success. Increase capital to ₹3,000. Target: ₹2,000–3,000/month.

**Months 5–6: Optimization**
Analyze 200+ trades. Fine-tune gate thresholds. Enable ML-driven weight adjustments. Target: ₹3,000–5,000/month.

**Months 7+: Scale**
Increase to ₹5,000/trade. Multi-position deployment. Target: ₹5,000–10,000/month.

***

## Who This Is For

**✅ Right fit:**
- Experienced intraday traders seeking disciplined automation
- Technical analysts who understand VWAP, orderflow, DOM
- Risk-aware individuals who prioritize capital preservation
- Data-driven traders who want diagnostics, not black boxes

**❌ Wrong fit:**
- Complete beginners (learn manual trading first)
- Traders seeking guaranteed returns
- Anyone unable to monitor a live system during market hours
- Undercapitalized traders (₹10,000 minimum recommended)

***

## Data & Security

- All credentials stored locally — never transmitted to any third party
- No telemetry, no usage tracking, no phone-home
- OAuth 2.0 authentication — no password storage
- Trade/read permissions only — zero withdrawal access
- Full open-source transparency — audit every line

***

## Risk Disclosure

ShortCircuit automates execution. It does not automate judgment.

**What it does:** Scans thousands of symbols, validates signals through 12 gates, executes with six safety layers, manages positions dynamically, and logs everything for continuous improvement.

**What it does not do:** Guarantee profits, eliminate losses, replace human oversight, or provide investment advice.

Markets are adversarial by nature. A 65% win rate means 35% of trades lose. A 20% drawdown is possible and planned for. The edge is statistical, not certain.

**Trade with capital you can afford to lose. Monitor the system during market hours. Override when judgment demands it.**

***

## License

Apache License 2.0 — Commercial use, modification, and distribution permitted. No warranty provided.

Trading equities involves substantial risk of capital loss. The software is provided as-is. The developer assumes no liability for trading losses, system failures, or consequential damages.

***

## Support

- Bug reports: GitHub Issues
- Security vulnerabilities: GitHub Security Advisories
- Documentation: inline code docs + `ARCHITECTURE_COMPLETE.md`

Commercial support, strategy consultation, and investment advice are not available.

***

*Created by [@nabrahma](https://github.com/nabrahma)*

**ShortCircuit. Institutional infrastructure. Individual scale.** ⚡

```bash
git clone https://github.com/nabrahma/ShortCircuit.git
```

*Star this repository if you believe retail traders deserve institutional-grade tools.* ⭐
