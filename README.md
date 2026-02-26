# ShortCircuit ⚡

> *The market is not a place. It is a conversation between participants
> with asymmetric information, asymmetric conviction, and asymmetric speed.
> This system is an attempt to listen more carefully than the other side.*

---

## Origin

This project did not start with code. It started with a conversation.

A friend — a sharper market observer than most — had a simple thesis:
*pumped stocks fail. Every time. The question is only when.*

He was right. The microstructure of a stock up 10% intraday on retail FOMO
is consistent, readable, and tradeable. The absorption at the top. The trapped longs.
The 15-minute structure breaking down quietly while the 1-minute chart still looks
like momentum.

I borrowed that thesis entirely. What I added was the attempt to formalize it —
to ask: what does this look like, precisely, in data? What are the exact conditions
that separate the setups that work from the ones that look identical but don't?
And can those conditions be verified automatically, without hesitation, every time?

ShortCircuit is the answer to those questions. The core idea belongs to him.
The implementation is mine. The debt is acknowledged.

---

## Why This Exists

Every market participant believes they have an edge.

Most are wrong — not because their ideas are bad, but because the gap between
*having an insight* and *acting on it correctly, every time, without hesitation,
without error* is wider than any strategy can bridge manually.

Human judgment degrades under pressure. Execution hesitates at exactly the wrong
moment. Stops get moved. Rules get broken at 2:47 PM when a position is down and
the brain starts negotiating with itself.

ShortCircuit is not an attempt to replace judgment.
It is an attempt to protect it — by removing the execution layer from human control
entirely, and trusting only what can be verified, logged, and audited.

---

## What It Is

A fully automated, event-driven intraday trading system for NSE equities.

It watches the market continuously. It identifies a specific microstructure event —
institutional exhaustion at intraday highs. It validates that observation through
twelve independent checks. When all twelve pass, it executes.

Between signals, it does nothing.
It does not overtrade. It does not hedge. It does not improvise.

The discipline is in the architecture.

---

## The Philosophy of Infrastructure First

Before any strategy logic was written, three problems were solved completely:

**Authentication:**
A trading system that re-authenticates mid-session is not a trading system.
It is a liability. ShortCircuit uses a singleton OAuth token — one authentication
per deploy, persisted to disk, validated on startup with a lightweight profile call.
The broker session does not expire during trading hours. Ever.

**State:**
A single `asyncio` event loop. A single `asyncio.Event` (`shutdown_event`) shared
by every concurrent task. There is no ambiguity about what the system is doing
at any point in time. There is no thread racing on position state. There is no
"was that order filled?" — the Order WebSocket answers that question, and it
answers it exactly once, immediately, without polling.

**Failure:**
Systems fail. The question is not whether failure happens — it is whether failure
is recoverable. Every component has a documented failure mode. Every failure mode
has a handler. Crashes are followed by orphan recovery. WebSocket drops are handled
by SDK reconnect. DB lag triggers the emergency logger. A hung cleanup task times
out in 10 seconds and force-exits. Nothing fails silently.

---

## The Architecture

```
NSE Market (9:15 AM → 3:30 PM IST)
    │
    ├─ Data WebSocket (fyers_apiv3)
    │   Real-time tick feed. All subscribed symbols.
    │   Powers: Gate 12 validation, live P&L, position SL monitoring.
    │
    └─ REST API (fyers_apiv3)
        Batch quotes, candle history, order submission.
        Powers: Scan, Gates 1–11, entry/exit orders.

Scanner
    2,418 NSE-EQ symbols. Batch-fetched via REST in groups of 50.
    Pre-filter: gain 6–18%, volume > 100k, LTP > ₹5.
    Parallel quality check: asyncio workers, candle history fetch.
    Output: candidate list, every ~60 seconds.

12-Gate Validation Framework
    Sequential. Failure at any gate = immediate rejection.
    Gates 1–11: REST snapshot data.
    Gate 12: WebSocket tick — real-time LTP break of entry trigger.
    Rejection rate: ~95% of candidates.

Order Manager
    Entry: REST submit → WebSocket fill confirmation.
    SL: REST submit, atomically with entry.
    Position: registered in-memory and in PostgreSQL.
    Exits: REST submit → WebSocket fill confirmation.

Position Manager
    SL state machine: INITIAL → BREAKEVEN → TRAILING → TIGHTENING.
    Exits: TP1 (50%) / TP2 (25%) / TP3 (25% runner) / EOD 15:10 / SOFT_STOP / EMERGENCY.
    DiscretionaryEngine monitors orderflow for soft-stop signals.

ML Logger
    Every signal → structured Parquet observation. Always. Regardless of outcome.
    40+ features. UUID4 observation ID. Daily file.
    Not used in signal scoring today. Training data for tomorrow.

Telegram
    The only interface. No web UI. No dashboard server.
    Every signal. Every fill. Every alert. Every exit. Real-time.
```

---

## The 12 Gates

There is a specific reason there are twelve and not three.

A single strong signal is a hypothesis. Twelve independent confirmations
are closer to a fact. Each gate is designed to kill the trade — not to approve it.
The system is structurally biased toward inaction. A trade happens only
when the system runs out of reasons to reject it.

| Gate | What It Kills |
|---|---|
| **1. Signal Manager** | Signals that exceed the daily cap (5), breach the 45-min cooldown, or follow 3 consecutive losses |
| **2. Market Regime** | Shorts during confirmed institutional uptrends — NIFTY first-hour range analysis |
| **3. Data Quality** | Illiquid symbols, corrupt microstructure, doji-spam patterns |
| **4. Technical Context** | Setups too far from VWAP, insufficient gain, wrong position in day range |
| **5. Hard Constraints** | Gain outside 6–15% band — too little momentum, too much risk |
| **6. Circuit Guard** | Upper-circuit proximity via Level 2 depth — cannot short into a circuit |
| **7. Momentum Filter** | Freight-train detection — extreme RVOL × VWAP slope means the move is not over |
| **8. Pattern Recognition** | Five parallel edge detectors — weighted confidence scoring, not majority vote |
| **9. Breakdown Confirmation** | The pattern must have already broken the setup low — anticipation is not a trade |
| **10. Institutional Confluence** | DOM, RSI, Fibonacci, OI, round-number analysis — the full picture |
| **11. Higher Timeframe** | 15-minute Lower High structure — no macro context, no trade |
| **12. Validation Gate** | Real-time LTP break of entry trigger via Data WebSocket — the final proof |

Gate 12 alone eliminates ~40% of signals that cleared Gates 1–11.

---

## The Microstructure Event

The system hunts one specific setup.

A stock has moved 7–15% intraday on elevated volume. Retail momentum buyers
are extended. Market makers have been absorbing the buying at the high.
The last retail breakout buyers are now trapped above their entry.

The tell is not the candle pattern. The candle pattern is the last confirmation.
The tell is the combination of: order flow evidence (absorption, trapped longs),
market profile deviation (LTP far from POC), higher-timeframe structure failure
(15m Lower High), and volume character (high volume, zero price progress).

When all five edge detectors confirm simultaneously — the setup is not a prediction.
It is an observation of something that has already happened.

The trade executes on the WebSocket tick that confirms entry trigger breach.
Not a prediction. Not an anticipation. A confirmation.

### Five Parallel Edge Detectors

| Detector | What It Finds |
|---|---|
| **Absorption Engine** | High volume, zero price progress — hidden limit supply |
| **Bad High Analyzer** | Level 2 DOM supply wall at day extreme |
| **Trapped Long Scanner** | Failed breakout — retail buyers trapped above entry |
| **Failed Auction Detector** | Range expansion exhaustion — price rejected by time, not force |
| **Classic Pattern Engine** | Bearish Engulfing, Shooting Star, Evening Star — volume-confirmed |

Confidence tiers: `EXTREME ≥ 5.0` | `HIGH ≥ 3.0` | `MEDIUM ≥ 2.0`

A single MEDIUM edge without confluence is rejected.

---

## Two WebSockets. Separate Concerns.

```
Data WebSocket
    ─ Tick feed for subscribed symbols
    ─ Gate 12: LTP monitored in real-time for entry trigger breach
    ─ Position monitor: SL/TP levels checked on every tick
    ─ Dashboard: 2s P&L refresh, broker-verified LTP

Order WebSocket
    ─ Fill events: PENDING → TRADED status change
    ─ Entry fill confirmation, SL hit detection, exit confirmation
    ─ Capital released only after TRADED event — never on REST response alone
```

Both run as background daemon threads (blocking Fyers SDK calls).
Callbacks are bridged to the asyncio event loop via `call_soon_threadsafe`.
No polling. No `asyncio.sleep` refresh loops on order state.
Events arrive. Handlers fire. State updates. Done.

### REST vs WebSocket — Where Each Is Used

| Operation | Transport | Reason |
|---|---|---|
| Scanner: 2,418 symbol quote batch | REST | No bulk WebSocket subscription available |
| Gates 1–11 signal analysis | REST (cached snapshot) | Point-in-time batch analysis |
| Gate 12 price monitoring | **WebSocket** | Real-time tick — no polling lag acceptable |
| Dashboard P&L refresh (2s) | **WebSocket** | Zero-latency broker-verified LTP |
| Order submission (entry / exit) | REST | Fyers requires REST for new orders |
| Fill confirmation | **WebSocket** | Fastest fill notification — no REST poll |
| EOD reconciliation | REST | Point-in-time position verification |

---

## The Single Asyncio Event Loop

One process. One event loop. Five concurrent tasks.

```python
async with asyncio.TaskGroup() as tg:
    tg.create_task(_trading_loop(ctx, shutdown_event))
    tg.create_task(bot.run(shutdown_event))
    tg.create_task(reconciliation_engine.run(shutdown_event))
    tg.create_task(eod_scheduler(...))
    tg.create_task(eod_watchdog(shutdown_event))
```

All five share `shutdown_event`. Setting it from any component
exits every loop cleanly on its next iteration. No `os._exit`. No `sys.exit(1)`
in the middle of a cleanup sequence. Structured. Deterministic.

`_supervised()` wraps each task — crashes are retried up to the configured limit.

`_validate_dependencies()` runs before the TaskGroup starts.
If any critical dependency is `None`, the process hard-crashes before the
first scan. No degraded state. No silent no-ops.

```python
def _validate_dependencies(ctx: RuntimeContext) -> None:
    if ctx.order_manager is None:
        raise RuntimeError("FATAL: OrderManager not initialized")
    if ctx.broker is None:
        raise RuntimeError("FATAL: Broker not initialized")
    # ... all critical deps verified
```

---

## The Auto-Trade Gate (5 Layers)

No order is placed without clearing all five. In sequence.

```
Layer 1 — config.py          AUTO_MODE = False   [hardcoded boot default]
Layer 2 — telegram_bot.py    self._auto_mode = False   [runtime state]
Layer 3 — trade_manager.py   gate check before routing
Layer 4 — focus_engine.py    gate check before order_manager call
Layer 5 — order_manager.py   final verification before broker REST call
```

Default state: alert-only. The engine scans, detects, and notifies.
It never touches capital until you send `/auto on` from your registered Telegram.

---

## Reconciliation

The reconciliation engine runs continuously in the background.

It compares DB state to broker state. It detects orphaned positions
(broker open, DB closed), phantom positions (DB open, broker flat),
and price mismatches. It alerts immediately on any discrepancy.

It is aware of its own cost:

```
Market hours, open positions   → every 6 seconds
Off-hours, open positions      → every 30 seconds
Off-hours, fully flat          → every 300 seconds
```

When flat off-hours, a single cache check (`_has_open_positions = False`)
short-circuits the entire reconciliation cycle. Zero DB queries. Zero broker calls.
The system is not busy when there is nothing to do.

---

## EOD Shutdown (Two Independent Layers)

EOD is the highest-risk operational moment.
A stuck scan loop, a hung DB query, a WebSocket reconnect thread —
any of these can prevent a clean shutdown.

Two independent mechanisms. Neither depends on the other.

```
15:10 IST  — TradeManager.close_all_positions()  [hard square-off]
15:32 IST  — eod_scheduler: analysis fires → shutdown_event.set()
15:32 IST  — eod_watchdog: independently fires shutdown_event.set()
15:40 IST  — eod_watchdog: os.kill(os.getpid(), SIGTERM)  [hard kill]
```

`eod_watchdog` checks every 30 seconds. It is isolated in its own task.
No scan loop, DB hang, or WebSocket thread can prevent it from running.

Maximum graceful shutdown window: **25 seconds.**

```
bot.stop()                    → 5s timeout
reconciliation_engine.stop()  → 10s timeout
db_pool.close()               → 5s timeout
broker.shutdown()             → 5s timeout
```

---

## The SL State Machine

A stop loss is not a number. It is a state.

```
INITIAL      → placed at entry. Fixed distance. REST order submitted atomically.
     ↓
BREAKEVEN    → SL moved to entry + buffer.
               Trigger: 1× risk profit reached.
               Reason: eliminate the possibility of a winning trade becoming a loss.
     ↓
TRAILING     → SL follows price.
               0.20% trail at 2× risk. 0.15% at 3×. 0.10% near target.
               Reason: let winners run. Tighten as conviction grows.
     ↓
TIGHTENING   → SL tightens aggressively near TP.
               Reason: prevent giveback on the final move into target.
```

### Exit Hierarchy

Six exit types. Priority order is fixed.

```
1. EMERGENCY      → Immediate full close. Circuit breaker. No confirmation.
2. HARD_SL        → WebSocket price breach. Broker SL order triggered.
3. SOFT_STOP      → DiscretionaryEngine: orderflow reversal detected.
4. TP1 (50%)      → +1.5% from entry. Half position closed. REST order.
5. TP2 (25%)      → +2.5% from entry. Quarter position closed. REST order.
6. TP3 (25%)      → +3.5% from entry. Runner. Structure break closes it.
7. EOD_SQUAREOFF  → 15:10 IST. Hard close. No exceptions.
```

---

## The Live Dashboard

```
⚡ ACTIVE TRADE — SHORT

NSE:TATASTEEL-EQ
Entry: ₹849.20  |  Qty: 2  |  Margin: ₹1,698

━━━━━━━━━━━━━━━━━━━━━━━━
LTP:    ₹842.50  ⬇️
P&L:    +₹13.40  (+0.79%)
ROI:    +3.95%  (5× leverage)

SL:     ₹849.20  [BREAKEVEN 🔒]
TP1:    ₹836.60  (-1.5%) — 50% exit
TP2:    ₹828.20  (-2.5%) — 25% exit
TP3:    ₹819.80  (-3.5%) — runner

OF:     🔴 BEARISH CONFIRMED — Trapped longs detected
HTF:    ✅ 15m Lower High: 849.80 → 847.40
━━━━━━━━━━━━━━━━━━━━━━━━

[🔄 Refresh]  [❌ Close Now]
```

Broker-verified every 2 seconds. Not estimated. Not cached.

---

## What Gets Logged

Everything.

```
logs/bot.log                       — rotating, 10MB × 5, every system event
logs/signals.csv                   — every signal, executed and rejected, gate results
logs/diagnostic_analysis.csv       — every /why query, gate-by-gate breakdown
logs/emergency_alerts.log          — critical failure events only
data/ml/data{YYYY-MM-DD}.parquet   — 40+ features per signal observation
data/trade_journal.csv             — human-readable trade record
```

The signal log exists because the most useful data is the data
about what the system rejected — and why. Every rejected signal is queryable.
Every threshold that caused a rejection is visible.

The ML log exists because today's rejected signals are tomorrow's training labels.
The system is not yet smart enough to learn from its own history.
It is building the dataset that will eventually allow it to.

---

## The `/why` Command

```
/why RELIANCE 14:25
```

The system reruns the full 12-gate analysis on historical data for that symbol
at that timestamp and returns a gate-by-gate pass/fail breakdown.

Every missed signal has a reason. Every reason has a threshold.
Every threshold is adjustable. The system does not hide its logic.

Every run is appended to `logs/diagnostic_analysis.csv`. Cumulative.
The record of every time the system said no — and whether it was right.

---

## The Database

PostgreSQL 14+. asyncpg. Pool: 10 minimum, 50 maximum connections.

Three tables:

```
positions           — every trade: entry, exit, size, P&L, status
orders              — every order: submission time, fill price, status, broker ID
reconciliation_log  — every reconciliation cycle: mismatches, timestamps
```

Migration: `migrations/v42_1_0_postgresql.sql`

`log_trade_entry()` wraps the `positions` and `orders` inserts in a single
atomic transaction. Either both succeed or neither does.
There is no state where a position exists without a corresponding order record.

---

## Technical Stack

| Layer | Technology |
|---|---|
| Language | Python 3.10+ |
| Concurrency | `asyncio.TaskGroup` + `threading` (WebSocket daemon threads) |
| Broker | Fyers API v3 — REST + dual WebSocket |
| Database | PostgreSQL 14+ via asyncpg (pool: 10–50) |
| Interface | python-telegram-bot v20+ (PTB) |
| Data | pandas, numpy, Apache Parquet |
| Auth | OAuth 2.0, singleton token, file persistence |
| Logging | `RotatingFileHandler` — 10MB × 5 backups |

---

## Setup

```bash
git clone https://github.com/nabrahma/ShortCircuit.git
cd ShortCircuit

pip install -r requirements.txt

cp .env.example .env
# FYERS_CLIENT_ID, FYERS_SECRET, FYERS_REDIRECT_URI
# DB_HOST, DB_NAME, DB_USER, DB_PASS
# TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

psql -U postgres -c "CREATE DATABASE shortcircuit_trading;"
python apply_migration.py

python main.py
# Token saved to data/access_token.txt after first OAuth flow.
# No re-auth needed on subsequent restarts.
```

Before enabling auto-trade, verify the startup log contains all three lines:

```
[INIT]     ✅ OrderManager constructed and injected into FocusEngine.
[STARTUP]  ✅ All dependency checks passed. Safe to trade.
[WATCHDOG] ✅ EOD watchdog started. Monitoring for 15:32 IST.
```

If any line is missing — do not trade. Diagnose first.

---

## Commands

| Command | What It Does |
|---|---|
| `/auto on` | Enable autonomous execution |
| `/auto off` | Revert to alert-only mode |
| `/status` | Capital state, open positions, session P&L, system health |
| `/positions` | All open positions with live broker-verified P&L |
| `/pnl` | Session P&L summary |
| `/why SYMBOL TIME` | Full gate-by-gate replay of any signal or miss |
| `/pause` | Suspend signal generation |
| `/resume` | Resume scanning |

---

## Who Should Use This

People who understand that an algo does not make you a better trader.
It makes you a *more consistent* trader — which is only valuable if the
underlying judgment is already sound.

The system enforces discipline mechanically. But the parameters it enforces —
the gain thresholds, the RVOL requirements, the HTF structure rules —
those were written by a human. If that human's understanding of markets
is wrong, the system will execute that wrongness with perfect consistency.

Read the code before running it with real capital.
Understand every gate before trusting any of them.
Monitor the system during market hours.

This is a tool. The responsibility for how it is used remains entirely
with the person who runs it.

---

## Risk

Markets are adversarial. The system is designed around a statistical edge —
not certainty. Losses are expected. Drawdowns are modeled for.
Three consecutive losses trigger an automatic pause.

The edge, if it exists, is in the microstructure event described above —
a real phenomenon in liquid markets, detectable with the right instrumentation.
Whether that edge persists, and for how long, is an empirical question
that only a live trading record can answer.

This system is built to generate that record cleanly.

---

## Security

- All credentials in `.env` — never transmitted, never logged
- No telemetry, no external data collection, no phone-home
- OAuth 2.0 — no broker password stored anywhere
- Trade and read permissions only — withdrawal access is not possible
- Fully open source — every gate, every order path, every handler is auditable

---

## License

Apache 2.0. Use, modify, distribute. No warranty.

Trading equities involves substantial risk of capital loss.
The software is provided as-is. No liability for trading losses,
system failures, or consequential damages.

---

*[@nabrahma](https://github.com/nabrahma)*

**ShortCircuit. Built to listen carefully.** ⚡

```bash
git clone https://github.com/nabrahma/ShortCircuit.git
```
