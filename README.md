# ShortCircuit

**ShortCircuit is a live intraday NSE execution engine for detecting and trading momentum exhaustion.**

It is not a chart pattern script. It is not a notification bot with a broker wrapper bolted on. ShortCircuit is a full-stack trading system: real-time market data ingestion, microstructure filtering, sequential gate evaluation, capital-aware sizing, broker-side risk placement, reconciliation, audit logging, Telegram command/control, and parquet-based ML feedback loops.

The product thesis is simple:

> The edge is not in predicting every move.
> The edge is in waiting until momentum is extended, structurally rejected, liquidity is fading, and execution can be verified.

ShortCircuit turns that thesis into software.

---

## Executive Summary

ShortCircuit scans thousands of NSE equity symbols during the live session and hunts for a very specific event:

**an overextended intraday mover failing at or above value while momentum buyers are trapped and upward auction quality is degrading.**

When the setup appears, the system does not immediately trade. It promotes the symbol into a staged gate pipeline. The analyzer passes the data to the "Brain" (the `BackToVWAPShort` strategy) to approve structure, momentum, volume, profile, confluence, and higher-timeframe context. The focus engine then waits for a real structural trigger: a 1-minute close through the entry level, with invalidation above the setup high.

Only after that does the order layer engage.

At runtime the bot coordinates:

- Fyers REST and WebSocket connectivity
- NSE-EQ scanner universe
- WebSocket-first quote cache with freshness tracking
- Single-strategy exhaustion logic (`BackToVWAPShort`)
- Signal validation engine
- Single-position capital slot
- Broker-side stop placement
- Active trade monitor
- Telegram command/control
- PostgreSQL audit trail
- ML parquet logger
- EOD reporting and shutdown
- Broker/DB reconciliation

ShortCircuit is designed around one operating principle:

**if the system cannot prove the setup, prove the fill, prove the stop, and prove the state, it should not trade.**

---

## The "Brain" vs "Muscle" Architecture

ShortCircuit is split into two perfectly isolated components: the **Brain** and the **Muscle**.

### The Brain (`strategy/`)
All trading intelligence is physically sealed inside the `strategy/` folder. This is the brain. It knows nothing about brokers, websockets, or Telegram. It only knows math, risk, and logic.

- `strategy/back_to_vwap.py`: The single, unified execution logic.
- `strategy/features.py`: Stateless math library (VWAP, RSI, Volume Fade).
- `strategy/market_profile.py`: Dalton Value Area math (VAH/VAL).
- `strategy/market_context.py`: Nifty regime and broader trend evaluations.
- `strategy/htf_confluence.py`: High-timeframe risk gates.

### The Muscle (`Root Directory`)
The rest of the repository is the muscle, nervous system, and immune system. It fetches data, asks the Brain for decisions, and pulls the physical triggers.

- `analyzer.py`: The orchestrator that asks the Brain what to do.
- `order_manager.py` / `fyers_broker_interface.py`: The hands that execute trades on the Fyers API.
- `reconciliation.py`: The immune system verifying your local state matches the broker's real-time state.

---

## Strategy Model: BackToVWAPShort

ShortCircuit has collapsed its complex multi-edge history into a single, highly-focused strategy: **BackToVWAPShort**.

It is primarily a short-side mean-reversion and exhaustion engine (though it natively supports `/mode buy` via Telegram for inverse logic). The system is built around three market observations:

1. Strong intraday gain creates unstable positioning.
2. Momentum becomes tradable only when extension meets rejection.
3. A signal is not an entry until price confirms structural failure.

The bot is interested in stocks that have already moved aggressively, usually above VWAP and value, where late momentum participants are vulnerable. It then looks for evidence that the move is no longer being accepted.

Core setup ingredients inside the `BackToVWAPShort` evaluation:

- Intraday gain expansion
- VWAP standard-deviation stretch (≥ 2.5 SD)
- Value Area High or profile rejection
- VWAP flattening or momentum decay
- Volume fade after surge
- Failed continuation structure
- Higher-timeframe stall or fail-open guard
- Candle-close trigger through the signal low

The strategy is not "short everything that is up."

It is "short only when an up-move has become statistically stretched, structurally rejected, and execution-confirmed."

---

## Data Plane

ShortCircuit uses a WebSocket-first data architecture.

The scanner universe is seeded and maintained through a quote cache with freshness tracking.

Cache lifecycle:

```text
UNINITIALIZED -> PRIMING -> READY -> DEGRADED -> REPRIME/RECOVER
```

Design principles:

- REST seed gives cold-start coverage.
- REST seed does not count as true freshness.
- WebSocket tick freshness determines readiness.
- Startup waits for cache readiness before scanning.
- Degraded cache can reprime.
- REST fallback is available during data stress.

The system separates "known" from "fresh." A stale cached quote is not live market data. The bot treats that distinction as a first-class safety property.

---

## Execution Plane

The execution system is built around controlled state transitions.

Entry lifecycle:

```text
Signal detected by the Brain (strategy/)
  -> Telegram alert / ML observation / gate audit
  → Pending validation (Focus Engine)
  → Candle close breaks trigger
  → Capital slot check (Dynamic 4x/5x Leverage)
  → Entry order (Graceful Margin Fallback)
  → WebSocket fill confirmation or REST verification fallback
  -> Broker stop placement
  -> Active focus monitor
```

Exit lifecycle:

```text
TP1 Hit (Midpoint):
  → partial exit (50%)
  → coordinate protective stop to Break-Even (BE)

TP2 Hit (Final VWAP Target):
  → cancel/coordinate protective stop
  → exit order
  → fill confirmation
  → DB close
  → ML outcome (includes final leverage state)
  → capital release

SL hit
  → order WebSocket fill event
  → broker position sync
  → internal state close
  → capital release

Manual Override ("Driver's Seat")
  → bot detects user-side modifications to TP/SL via broker WebSocket
  → flags `manual_override = True`
  → instantly disables 45-min time-based exit timer
  → ceases automatic stop-loss adjustments (bot backs off)
```

The system defaults to one open position. This is not a limitation. It is a risk boundary. Capital, state, reconciliation, and Telegram control are all simpler and more robust when the live system protects one thesis at a time.

---

## Capital Model

ShortCircuit sizes from live broker funds, not a hardcoded number.

Capital manager responsibilities:

- read Fyers funds
- parse multiple response shapes
- derive real margin
- apply dynamic intraday leverage assumptions (auto-scales 5x down to 4x on margin rejection)
- reserve one active slot
- prevent concurrent entries
- release only after confirmed close
- resync after fills and exits

The bot can scan continuously, but entry is blocked while capital is occupied.

---

## Operator Surface

ShortCircuit exposes one operator surface.

### Telegram

Telegram is the command and observability surface. It handles:

- startup/status alerts
- signal discovery
- validation updates
- auto-mode control (`/auto on`, `/auto off`)
- mode switching (`/mode buy`, `/mode sell`)
- position and P&L snapshots (`/status`)
- broker health alerts
- trade notifications
- risk alerts
- EOD reports

There is no separate web UI in the runtime path. The bot is intentionally Telegram-first so the live operator stack stays lean: fewer services, fewer sockets, fewer deployment dependencies, and one control plane for alerts and action.

Auto trading is deliberately off on boot. The operator must explicitly enable it from Telegram.

---

## Persistence And Auditability

ShortCircuit records the session as structured data. Storage layers:

- PostgreSQL for orders, positions, reconciliation, gate results
- CSV signal log for human-readable setup review
- daily session logs
- daily rejection summary
- ML parquet observations
- EOD report markdown

Every candidate that reaches analyzer evaluation produces a gate result.

This matters because the bot is not only an execution engine. It is a research instrument.

---

## Reconciliation And Recovery

Live trading systems fail when internal state and broker state diverge. ShortCircuit treats reconciliation as a core runtime service.

It detects:

- orphaned broker positions
- phantom internal positions
- quantity mismatches
- manual closes
- broker-side fills missed by the internal loop

Recovery behavior:

- adopt orphan positions with emergency protection
- release capital for phantoms
- update DB state
- mark DB cache dirty
- alert operator
- avoid duplicate adoption

The reconciliation engine is what lets the bot survive manual intervention, WebSocket drops, partial broker outages, and delayed REST responses.

---

## Project Layout

```text
main.py                      Runtime supervisor and task orchestration
config.py                    Strategy, risk, mode, and infrastructure parameters
scanner.py                   NSE-EQ scanner and candle prefetcher
analyzer.py                  The Orchestrator: asks the Brain for logic, controls the Muscle
focus_engine.py              Pending validation and active position monitoring
order_manager.py             Entry, stops, exits, fill verification
capital_manager.py           Funds sync, sizing, capital slot control
fyers_broker_interface.py    Data/order WebSocket and broker abstraction
telegram_bot.py              Operator command and alert interface
database.py                  PostgreSQL access layer
gate_result_logger.py        Gate audit trail and EOD rejection summary
ml_logger.py                 Parquet/CSV ML observation logger
reconciliation.py            Broker/DB/internal-state reconciliation
eod_analyzer.py              EOD analytics, missed-signal audit, ML labeling

strategy/                    The Brain: All logic and math evaluation
├── back_to_vwap.py          Single unified execution strategy
├── features.py              VWAP, RSI, and micro-structure math
├── market_profile.py        Dalton value areas
├── market_context.py        Nifty broader context
└── htf_confluence.py        Higher timeframe protection gates
```

---

## Quick Start

Install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create `.env`:

```text
FYERS_CLIENT_ID=
FYERS_SECRET_ID=
FYERS_REDIRECT_URI=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
DB_HOST=localhost
DB_PORT=5432
DB_USER=
DB_PASSWORD=
DB_NAME=shortcircuit_trading
```

Run:

```bash
python main.py
```

The bot starts alert-only. Enable automated execution from Telegram when ready.

---

## Risk Statement

ShortCircuit is live trading infrastructure. It can place real orders, create real exposure, and lose real money.

It is engineered for discipline, observability, and fast recovery. It is not engineered to guarantee outcomes. Markets are adversarial, broker APIs fail, liquidity disappears, and no gate stack can eliminate risk.

Use it like a production system:
- monitor it
- audit it
- keep secrets out of logs
- verify broker state
- review every EOD report
- never assume a green process means a flat broker account

---

## One-Line Positioning

ShortCircuit is a real-time, gate-driven, WebSocket-first NSE execution engine that converts intraday momentum exhaustion into auditable, capital-aware, broker-verified trades.
