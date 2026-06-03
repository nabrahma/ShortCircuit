# ShortCircuit — Architecture Reference

**Version:** BackToVWAPShort Doctrine | **Last Updated:** 2026-06-03

---

## SECTION 1 — System Overview

ShortCircuit is a fully automated, event-driven algorithmic trading bot for NSE (Indian National Stock Exchange) equities, operating intraday-only (all positions closed by 15:10 IST). It implements a single production strategy — **BackToVWAPShort** — a failed-auction mean-reversion system that shorts overextended stocks when the upside auction fails and structure breaks for reversion toward VWAP.

The product thesis:

> The edge is not in predicting every move. The edge is in waiting until momentum is extended, structurally rejected, liquidity is fading, and execution can be verified.

The system infrastructure is: Python 3.10+ asyncio, Fyers API v3 (REST for quote batches and order submission; WebSocket for real-time tick data and order fill events), PostgreSQL + asyncpg for trade journaling, and python-telegram-bot (PTB) v20+ for the operator interface. The operator has no web UI — all signals, trade alerts, live P&L, commands (`/auto on`, `/status`, `/mode buy|sell`), and EOD summaries flow exclusively through Telegram.

The concurrency model is a **single asyncio event loop** with a `TaskGroup` launching four concurrent tasks: `trading_loop`, `telegram_bot`, `reconciliation`, and `eod_scheduler`, plus `eod_watchdog`. All tasks share a single `asyncio.Event` called `shutdown_event`. When any component sets this event, every `while not shutdown_event.is_set()` loop exits cleanly.

---

## SECTION 2 — Brain vs Muscle Architecture

ShortCircuit is split into two physically isolated layers:

### The Brain (`strategy/`)

All trading intelligence is sealed inside the `strategy/` directory. The Brain knows nothing about brokers, websockets, or Telegram. It only knows math, risk, and logic.

| File | Responsibility |
|---|---|
| `strategy/back_to_vwap.py` | Single unified strategy: 6 hard gates, confidence tagging |
| `strategy/features.py` | Stateless math library: VWAP SD, swing-based RSI divergence, volume fade, ATR, pattern detection |
| `strategy/market_profile.py` | Dalton Value Area algorithm: VAH, VAL, POC computation |
| `strategy/market_context.py` | Nifty regime detection, morning range, circuit blacklist, volume z-score |
| `strategy/htf_confluence.py` | Higher-timeframe momentum physics: stall vs acceleration on 15-minute bars |

### The Muscle (Root Directory)

The rest of the repository fetches data, asks the Brain for decisions, and pulls the physical triggers.

| File | Responsibility |
|---|---|
| `main.py` | Supervisor: initializes all components, runs asyncio TaskGroup, orchestrates shutdown |
| `analyzer.py` | Orchestrator: data fetch → enrich → pre-filter → `strategy.evaluate()` → finalize signal |
| `scanner.py` | NSE-EQ universe scanner, quote cache, candle prefetcher |
| `focus_engine.py` | Pending signal validation (trigger break) and active position monitoring |
| `order_manager.py` | Entry orders, stop placement, exit orders, fill verification |
| `trade_manager.py` | Auto-trade state, position safety guards, broker position queries |
| `capital_manager.py` | Live broker funds sync, sizing, capital slot control |
| `fyers_broker_interface.py` | Data/order WebSocket and complete Fyers API abstraction |
| `fyers_connect.py` | Authentication and token management |
| `reconciliation.py` | Broker/DB/internal-state truth reconciliation |
| `telegram_bot.py` | Operator command and alert interface |
| `database.py` | PostgreSQL access layer |
| `signal_manager.py` | Daily signal caps, per-symbol cooldowns, consecutive loss tracking |
| `gate_result_logger.py` | Gate audit trail, daily rejection summary, EOD analytics |
| `ml_logger.py` | Parquet/CSV ML observation logger |
| `eod_analyzer.py` | EOD analytics, missed-signal audit, ML outcome labeling |
| `eod_scheduler.py` | Scheduled EOD square-off and report generation |
| `eod_watchdog.py` | Safety watchdog ensuring EOD fires even if scheduler fails |
| `market_session.py` | Market hours and session state management |
| `market_utils.py` | Utility: `is_market_hours()` helper |
| `startup_recovery.py` | Crash recovery: restores active positions from DB on restart |
| `symbols.py` | Symbol constants and validation (NIFTY_50, BANKNIFTY) |

---

## SECTION 3 — Strategy: BackToVWAPShort

### Doctrine

Every live short must satisfy one sentence:

> "Shorted because the stock was materially stretched above VWAP and value, the upside auction failed, exhaustion was confirmed, and the trigger low broke for reversion toward VWAP."

### 6 Hard Gates (ALL must pass)

| Gate | Name | Implementation | Threshold |
|---|---|---|---|
| C1 | VWAP Stretch | `vwap_sd >= STRATEGY_VWAP_SD_FLOOR` | **4.5 SD** minimum |
| C2 | Value Location | Price above VAH, or profile rejection confirmed | VAH from Dalton algorithm |
| C3 | Failed Auction | Profile rejection OR VAH Look-Above-And-Fail | No narrowing-highs proxy |
| C4 | Divergence | Swing-based RSI divergence OR price lower-high | Real swing comparison |
| C5 | Volume Fade | `vol_fade_ratio <= 0.65` | Absolute, no relaxation |
| C6 | Momentum Decay | `slope_fast < slope_slow * 0.85` | Genuine velocity decay |

### Forbidden Behaviors

- No gate can be bypassed because another feature looks strong.
- No volume climax ("Spear of Exhaustion") can override the volume-fade gate.
- No adaptive relaxation of thresholds based on decay state.
- No trivial flat-slope OR clause in momentum decay.
- Confidence scoring is informational only — never influences gate decisions.

### Confidence Tiers (Post-Gate, Informational Only)

| Tier | Criteria |
|---|---|
| EXTREME | VWAP SD ≥ 6.0 AND 5+ confluences |
| HIGH | VWAP SD ≥ 5.0 OR 4+ confluences |
| MEDIUM | All 6 gates passed (default) |

---

## SECTION 4 — Data Plane

### Quote Cache Architecture

The scanner universe is seeded and maintained through a WebSocket-first quote cache with freshness tracking.

```text
UNINITIALIZED → PRIMING → READY → DEGRADED → REPRIME/RECOVER
```

- REST seed gives cold-start coverage but does not count as true freshness.
- WebSocket tick freshness determines readiness.
- Startup waits for cache readiness before scanning.
- Degraded cache can reprime. REST fallback is available during data stress.

### History Fetching Priority

```text
1. Local candle aggregator (1-minute, from WebSocket ticks)
2. REST fallback (Fyers history API)
```

The local candle engine (`P82_LOCAL_CANDLES_ENABLED`) builds 1-minute bars from raw WebSocket ticks, avoiding REST rate limits during the main scanning loop.

---

## SECTION 5 — Signal Flow

```text
Scanner (finds gainers)
  → Analyzer (data fetch, enrichment, pre-filters)
    → BackToVWAPShort.evaluate() (6 hard gates)
      → G9: HTF Confluence (fail-closed)
        → G8: Signal Manager (cooldown, daily cap)
          → Signal finalized (SL calc, CSV/ML log)
            → Focus Engine: Pending validation
              → G12: Trigger break (candle low)
                → Order Manager: Entry + Hard Stop
                  → Active position monitoring
                    → Exit: TP/SL/EOD
```

### Pre-Filters (Before Strategy)

| Filter | Location | Purpose |
|---|---|---|
| G2 | `analyzer.py` | Minimum candle count for valid RVOL |
| G7 | `market_context.py` | Market regime (time gates, Nifty trend) — **fail-closed** |

### Post-Strategy Filters

| Filter | Location | Purpose |
|---|---|---|
| G9 | `htf_confluence.py` | 15-minute momentum stall vs acceleration — **fail-closed** |
| G8 | `signal_manager.py` | Per-symbol cooldown, daily signal cap, loss pause |

---

## SECTION 6 — Fail-Closed Policy

All context and data-availability checks are **fail-closed**. If the system cannot prove the setup, it does not trade.

| Module | Missing Data Behavior |
|---|---|
| `htf_confluence.py` | No 15m data → `BLOCK: HTF Data Unavailable` |
| `htf_confluence.py` | Calculation error → `BLOCK: Calculation Error` |
| `market_context.py` | No Nifty index data → `BLOCK: No index data available` |
| `market_context.py` | Morning range unavailable → `BLOCK: Morning range unavailable` |
| `analyzer.py` | HTF timeout → `BLOCK: Timeout` |
| `back_to_vwap.py` | Profile unavailable → `REJECT: Market profile unavailable` |

---

## SECTION 7 — Execution Plane

### Entry Lifecycle

```text
Signal finalized by Analyzer
  → Focus Engine: add_pending_signal()
  → Pending validation loop (1-minute bars)
  → Trigger: candle low breaks signal_low
  → Invalidation: price > signal_high * 1.002
  → Timeout: 15 minutes (G11)
  → Capital slot check (CapitalManager)
  → OrderManager.enter_position()
  → WebSocket fill confirmation (15s timeout, REST fallback)
  → Hard stop placement (SL-M above exhaustion high)
  → Active monitoring begins
```

### Exit Lifecycle

```text
TP Hit:
  → Cancel protective stop
  → Exit order (market)
  → Fill confirmation
  → DB close + ML outcome label
  → Capital release

SL Hit:
  → WebSocket fill event
  → Broker position sync
  → Internal state close
  → Capital release

EOD:
  → Square off all open risk at 15:10
  → Flush gate results
  → ML labeling
  → EOD report
```

### Position Model

The system defaults to **one open position**. This is a risk boundary, not a limitation. Capital, state, reconciliation, and Telegram control are all simpler and more robust when the live system protects one thesis at a time.

---

## SECTION 8 — Capital Model

ShortCircuit sizes from live broker funds, not a hardcoded number.

```text
AUTO_MODE = False on boot (enable via Telegram only)
INTRADAY_LEVERAGE = 5.0
Single active capital slot
```

Capital manager responsibilities:
- Read Fyers funds
- Parse multiple response shapes
- Derive real margin
- Apply intraday leverage assumptions
- Reserve one active slot
- Prevent concurrent entries
- Release only after confirmed close
- Resync after fills and exits

---

## SECTION 9 — Reconciliation

Live trading systems fail when internal state and broker state diverge. ShortCircuit treats reconciliation as a core runtime service.

### Detection

- Orphaned broker positions (broker has position, internal state doesn't)
- Phantom internal positions (internal state has position, broker doesn't)
- Quantity mismatches
- Manual closes
- Broker-side fills missed by the internal loop

### Recovery

- Adopt orphan positions with emergency SL protection
- Release capital for phantoms
- Update DB state
- Mark DB cache dirty
- Alert operator via Telegram
- Grace period to avoid false orphan alerts during settlement lag

---

## SECTION 10 — Operator Surface (Telegram)

Telegram is the only operator interface. It handles:

- Startup/status alerts
- Signal discovery notifications
- Validation updates
- Auto-mode control (`/auto on`, `/auto off`)
- Direction switching (`/mode buy`, `/mode sell`)
- Position and P&L snapshots (`/status`)
- Broker health alerts
- Trade notifications (entry, SL, TP, exit)
- Risk alerts (circuit, spread, loss pause)
- EOD reports

Auto trading is deliberately off on boot. The operator must explicitly enable it via Telegram.

---

## SECTION 11 — Persistence and Auditability

### Storage Layers

| Layer | Purpose |
|---|---|
| PostgreSQL | Orders, positions, reconciliation events, gate results |
| CSV signal log | Human-readable setup review (`logs/signals.csv`) |
| Daily session logs | Rotating file logs (`logs/bot.log`) |
| Gate result logger | Daily rejection summary, per-gate pass rates |
| ML parquet | Observation logging with outcome labeling |
| EOD reports | Daily markdown report (`reports/`) |

### ML Feedback Loop

Observations logged at signal time, outcomes labeled at EOD:
- Symbol, date, time, gain%, VWAP distance, VWAP SD, slope, volume, RVOL
- Pattern metadata, profile/confluence flags
- Direction, ATR, stop/target prices
- Outcome, exit price, MFE, MAE, PnL%, hold time

Data written daily to `data/ml/observations_YYYY-MM-DD.parquet`.

---

## SECTION 12 — Configuration

All strategy parameters are centralized in `config.py`.

### Critical Strategy Parameters

| Key | Value | Purpose |
|---|---|---|
| `STRATEGY_VWAP_SD_FLOOR` | 4.5 | Minimum VWAP stretch for any signal |
| `STRATEGY_VWAP_SD_HIGH` | 5.0 | HIGH confidence tier |
| `STRATEGY_VWAP_SD_EXTREME` | 6.0 | EXTREME confidence tier |
| `STRATEGY_VOL_FADE_MAX_RATIO` | 0.65 | Volume fade threshold (absolute) |
| `STRATEGY_MOMENTUM_DECAY_RATIO` | 0.85 | Fast slope must be < slow × this |
| `STRATEGY_REQUIRE_FAILED_AUCTION` | True | Hard gate: require auction failure |

### Safety Parameters

| Key | Value | Purpose |
|---|---|---|
| `AUTO_MODE` | False | Always off on boot |
| `MAX_SESSION_LOSS_INR` | 500 | Max cumulative daily loss before halt |
| `INTRADAY_LEVERAGE` | 5.0 | Fixed leverage |
| `SQUARE_OFF_TIME` | 15:10 | Forced EOD square-off |
| `VALIDATION_TIMEOUT_MINUTES` | 15 | Pending signal expiry |

### Scanner Parameters

| Key | Value | Purpose |
|---|---|---|
| `SCANNER_GAIN_MIN_PCT` | 7.5 | Minimum intraday gain to scan |
| `SCANNER_GAIN_MAX_PCT` | 18.0 | Upper-circuit runner protection |
| `SCANNER_MIN_VOLUME` | 333,333 | Liquidity floor |
| `SCANNER_MIN_LTP` | 40.0 | Sub-₹40 manipulation filter |

---

## SECTION 13 — Project Layout

```text
ShortCircuit/
├── main.py                      Supervisor and task orchestration
├── config.py                    All parameters (strategy, risk, infra)
├── scanner.py                   NSE-EQ scanner and candle prefetcher
├── analyzer.py                  Orchestrator: data → Brain → signal
├── focus_engine.py              Pending validation + active monitoring
├── order_manager.py             Entry, stops, exits, fill verification
├── trade_manager.py             Auto-trade state, position safety
├── capital_manager.py           Funds sync, sizing, capital slot
├── fyers_broker_interface.py    WebSocket + REST broker abstraction
├── fyers_connect.py             Authentication and token management
├── telegram_bot.py              Operator command and alert interface
├── database.py                  PostgreSQL access layer
├── signal_manager.py            Daily caps, cooldowns, loss tracking
├── gate_result_logger.py        Gate audit trail, rejection summary
├── ml_logger.py                 Parquet/CSV ML observation logger
├── reconciliation.py            Broker/DB/internal-state reconciliation
├── eod_analyzer.py              EOD analytics, ML labeling
├── eod_scheduler.py             Scheduled EOD square-off
├── eod_watchdog.py              Safety watchdog for EOD
├── market_session.py            Market hours and session state
├── market_utils.py              is_market_hours() helper
├── startup_recovery.py          Crash recovery from DB
├── symbols.py                   Symbol constants and validation
│
├── strategy/                    THE BRAIN: All logic and math
│   ├── back_to_vwap.py          Single unified strategy (6 gates)
│   ├── features.py              VWAP, RSI, volume, ATR, patterns
│   ├── market_profile.py        Dalton value areas (VAH/VAL/POC)
│   ├── market_context.py        Nifty regime, morning range
│   └── htf_confluence.py        15-minute momentum physics
│
├── logs/                        Session logs, signal CSV
├── data/ml/                     ML parquet observations
├── reports/                     EOD markdown reports
├── migrations/                  PostgreSQL schema migrations
└── README.md                    Project overview
```

---

## SECTION 14 — One-Line Positioning

ShortCircuit is a real-time, gate-driven, WebSocket-first NSE execution engine that converts intraday momentum exhaustion into auditable, capital-aware, broker-verified trades — using exactly one production strategy: BackToVWAPShort.
