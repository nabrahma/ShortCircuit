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

When the setup appears, the system does not immediately trade. It promotes the symbol into a staged gate pipeline. The analyzer must approve structure, momentum, volume, profile, confluence, higher-timeframe context, and signal eligibility. The focus engine then waits for a real structural trigger: a 1-minute close through the entry level, with invalidation above the setup high.

Only after that does the order layer engage.

At runtime the bot coordinates:

- Fyers REST and WebSocket connectivity
- NSE-EQ scanner universe
- WebSocket-first quote cache with freshness tracking
- Market regime and climax-window logic
- Multi-gate analyzer
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

## Strategy Model

ShortCircuit is primarily a short-side mean-reversion and exhaustion engine.

The system is built around three market observations:

1. Strong intraday gain creates unstable positioning.
2. Momentum becomes tradable only when extension meets rejection.
3. A signal is not an entry until price confirms structural failure.

The bot is interested in stocks that have already moved aggressively, usually above VWAP and value, where late momentum participants are vulnerable. It then looks for evidence that the move is no longer being accepted.

Core setup ingredients:

- Intraday gain expansion
- VWAP standard-deviation stretch
- Value Area High or profile rejection
- VWAP flattening or momentum decay
- Volume fade after surge
- Failed continuation structure
- Optional order-flow confluence
- Higher-timeframe stall or fail-open guard
- Candle-close trigger through the signal low

The strategy is not "short everything that is up."

It is "short only when an up-move has become statistically stretched, structurally rejected, and execution-confirmed."

---

## Runtime Modes

### 09:30-10:00 IST: Climax Mode

The opening window is treated as a special regime.

During this period, normal market noise is dangerous. G7 blocks most candidates unless they qualify as a climax exception.

Current climax requirements:

```text
P65_AMT_ENABLED = True
P65_G7_CLIMAX_WINDOW_START = 09:30
P65_G7_SAFE_TRADE_START = 10:00
P65_G7_CLIMAX_SD_THRESHOLD = 2.5
P65_G7_VOLUME_Z_SCORE_THRESHOLD = 2.0
```

A candidate must show:

- enough VWAP stretch
- enough volume z-score
- profile rejection

Otherwise it is rejected at `G7_REGIME`.

### After 10:00 IST: Normal Mode

After the safe-trade start, G7 stops acting as an opening-volatility blocker. The bot shifts into the normal exhaustion pipeline:

- gain and rotation constraints
- circuit guard
- momentum safeguard
- exhaustion/profile gate
- confluence scoring
- higher-timeframe confluence
- signal manager eligibility
- validation trigger

This is where most clean VAH/Profile Rejection trades should come from.

### Version A: Momentum Decay Fast Path

Version A is a specialized decay path.

It is enabled by:

```text
P92_VERSION_A_ENABLED = True
```

It only activates when decay is detected:

```text
slope_5m < slope_30m * 0.90
vwap_sd > P66_G4_DECAY_SD_THRESHOLD
```

Version A can bypass the normal G5/G6 exhaustion composite because the system treats the combination of:

- strong gain
- VWAP stretch
- confirmed decay
- surge-and-fade volume audit

as the signal itself.

Normal Version A thresholds:

```text
P92_VA_MIN_GAIN = 10.0
P92_VA_MIN_SD = 2.5
P93_RVOL_SURGE_PEAK_MULT = 3.0
P93_RVOL_SURGE_FADE_RATIO = 0.6
```

Version A+ is stricter and is intended for post-target conditions:

```text
P92_VA_PLUS_MIN_GAIN = 13.0
P92_VA_PLUS_MIN_SD = 3.5
P92_VA_PLUS_REQUIRE_NARROWING_HIGHS = True
```

Version A signals are named explicitly:

```text
A_EXHAUSTION_SCALP
A+_EXHAUSTION_SCALP
```

If a signal is named `VAH_REJECTION + Profile Rejection`, it came from the normal profile/exhaustion path, not Version A.

---

## Gate Stack

ShortCircuit is designed to reject aggressively. A trade is not "found"; it survives.

| Gate | Name | Function |
|---|---|---|
| Scanner | Pre-filter | Gain, volume, LTP, tick size, candle quality, dirty chart rejection |
| G2 | RVOL/Data Validity | Rejects insufficient history or unreliable candle context |
| G7 | Market Regime | Opening climax filter, premarket block, EOD cutoff, Nifty regime |
| G1 | Gain/Rotation Constraints | Gain floor, normal threshold, retrace/kill-backdoor logic, adaptive softening |
| G3 | Circuit Guard | Blocks symbols near or touching circuit behavior |
| G4 | Momentum Safeguard | Blocks active extension unless slope inflection/decay is confirmed |
| Version A | Decay Fast Path | Optional bypass of G5/G6 when decay + stretch + audit satisfy fast-path requirements |
| G15 | Unspecified Move Audit | Surge-and-fade volume test used inside Version A/A+ |
| G5 | Exhaustion/Profile | Confirms VAH/profile rejection, stretch score, volume fade, exhaustion state |
| G6 | Confluence | Requires enough pro-confluence: value divergence, round level, wick rejection, order-flow evidence |
| G9 | HTF Confluence | 15m stall/acceleration guard with alpha-strike bypass and fail-open behavior |
| G8 | Signal Manager | Per-symbol cooldown, daily state, target/loss constraints |
| G13 | Risk Model | Stop/target metadata, trade payload finalization |
| G10 | Execution Precision | Spread guard and cautious execution mode |
| G11 | Timeout | Pending signal expiry, default 15 minutes |
| G12 | Validation | 1-minute candle close through trigger, invalidation above setup high |

The current production flow is deliberately layered:

```text
Scanner
  -> Analyzer gates
  -> Pending signal
  -> G12 candle-close validation
  -> Order entry
  -> Stop placement
  -> Active focus monitor
  -> Exit/finalization
  -> ML + DB + EOD feedback
```

---

## G9: Higher-Timeframe Physics

G9 is the higher-timeframe confluence layer.

It lives in `htf_confluence.py` and models whether the larger timeframe has stalled or is still accelerating.

Core logic:

```text
if vwap_sd > P61_G9_BYPASS_SD_THRESHOLD:
    PASS as Alpha Strike

else:
    read 15m candles
    curr_move = current 15m close move
    prev_move = previous 15m close move

    if curr_move > P61_G9_ACCEL_REJECT_THRESHOLD:
        BLOCK as momentum acceleration

    if curr_move < P61_G9_STALL_PASS_THRESHOLD:
        PASS as momentum stall

    otherwise:
        BLOCK as sustained trend
```

Current parameters:

```text
P61_G9_BYPASS_SD_THRESHOLD = 3.0
P61_G9_ACCEL_REJECT_THRESHOLD = 2.0
P61_G9_STALL_PASS_THRESHOLD = 1.0
```

Operationally, G9 is a confluence and protection layer, not the first signal generator.

Important implementation behavior:

- If stretch is extreme, G9 passes immediately.
- If 15m data is missing or insufficient, G9 passes fail-open.
- If the calculation errors, G9 passes fail-open.
- Version A normal mode bypasses G9.
- Version A+ can require G9.

This is intentional in the sense that the system should not drop a high-quality immediate setup purely because the HTF fetch path is degraded. It is also auditable, because the G9 value is logged into gate results.

---

## G15: Unspecified Move Audit

G15 is not a universal gate. It is a specialized audit inside Version A/A+.

Its job is to reject steady institutional trend continuation and admit only overreaction-style moves that show surge and fade.

The logic:

```text
baseline_vol = average volume of candles [-18:-3]
max_recent_vol = max volume of last 3 candles
current_vol = volume of latest candle

surge_hit = max_recent_vol > baseline_vol * 3.0
fade_hit = current_vol < max_recent_vol * 0.6

PASS only if surge_hit and fade_hit
```

This makes Version A more selective.

It prevents the decay fast path from shorting a stock that is still being accumulated in a clean, persistent, high-quality trend. The system wants "exhaustion after overreaction," not "fade a real trend because it is up."

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

The system separates "known" from "fresh."

A stale cached quote is not live market data. The bot treats that distinction as a first-class safety property.

---

## Execution Plane

The execution system is built around controlled state transitions.

Entry lifecycle:

```text
Signal detected
  -> Telegram alert / ML observation / gate audit
  -> Pending validation
  -> Candle close breaks trigger
  -> Capital slot check
  -> Entry order
  -> WebSocket fill confirmation or REST verification fallback
  -> Broker stop placement
  -> Active focus monitor
```

Exit lifecycle:

```text
TP hit
  -> cancel/coordinate protective stop
  -> exit order
  -> fill confirmation
  -> DB close
  -> ML outcome
  -> capital release

SL hit
  -> order WebSocket fill event
  -> broker position sync
  -> internal state close
  -> capital release
```

The system defaults to one open position.

This is not a limitation. It is a risk boundary. Capital, state, reconciliation, and Telegram control are all simpler and more robust when the live system protects one thesis at a time.

---

## Capital Model

ShortCircuit sizes from live broker funds, not a hardcoded number.

Capital manager responsibilities:

- read Fyers funds
- parse multiple response shapes
- derive real margin
- apply intraday leverage assumptions
- reserve one active slot
- prevent concurrent entries
- release only after confirmed close
- resync after fills and exits

Current runtime behavior:

```text
AUTO_MODE = False on boot
INTRADAY_LEVERAGE = 5.0
DAILY_TARGET_INR = -1  # dynamic target mode
single active capital slot
```

The bot can scan continuously, but entry is blocked while capital is occupied.

---

## Operator Surface

ShortCircuit exposes one operator surface.

### Telegram

Telegram is the command and observability surface.

It handles:

- startup/status alerts
- signal discovery
- validation updates
- auto-mode control
- mode switching
- position and P&L snapshots
- broker health alerts
- trade notifications
- risk alerts
- EOD reports

There is no separate web UI in the runtime path. The bot is intentionally Telegram-first so the live operator stack stays lean: fewer services, fewer sockets, fewer deployment dependencies, and one control plane for alerts and action.

Auto trading is deliberately off on boot. The operator must explicitly enable it from Telegram.

---

## Persistence And Auditability

ShortCircuit records the session as structured data.

Storage layers:

- PostgreSQL for orders, positions, reconciliation, gate results
- CSV signal log for human-readable setup review
- daily session logs
- daily rejection summary
- ML parquet observations
- EOD report markdown

Every candidate that reaches analyzer evaluation produces a gate result.

The daily rejection summary answers:

- how many scans ran
- how many symbols were evaluated
- which symbols appeared
- where each symbol failed most often
- how many signals fired
- which gates were systemically restrictive

This matters because the bot is not only an execution engine. It is a research instrument.

---

## ML Feedback Loop

ShortCircuit logs observations at signal time and labels outcomes later.

ML logger fields include:

- symbol/date/time
- gain percent
- VWAP distance and SD
- VWAP slope
- volume and RVOL
- pattern metadata
- profile/confluence flags
- direction
- ATR
- stop/target prices
- outcome
- exit price
- max favorable excursion
- max adverse excursion
- pnl percent
- hold time

Data is written daily:

```text
data/ml/observations_YYYY-MM-DD.parquet
data/ml/observations_YYYY-MM-DD.csv
```

The trainer is intended to use this archive to search parameter space for better thresholds. The goal is not to replace the strategy with a black box. The goal is to tune the deterministic gate stack using observed market response.

---

## Reconciliation And Recovery

Live trading systems fail when internal state and broker state diverge.

ShortCircuit treats reconciliation as a core runtime service.

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

## EOD Protocol

End of day is a controlled shutdown, not a process kill.

EOD responsibilities:

- square off open risk
- stop scanning
- flush gate results
- save reports
- audit missed signals
- label ML observations
- release resources
- notify operator

The session should end with a broker-flat state, clean DB state, and a complete audit trail.

---

## Project Layout

```text
main.py                    Runtime supervisor and task orchestration
config.py                  Strategy, risk, mode, and infrastructure parameters
scanner.py                 NSE-EQ scanner and candle prefetcher
analyzer.py                Gate pipeline and signal construction
god_mode_logic.py          Core exhaustion, VWAP, ATR, and constraint math
market_context.py          G7 market regime and opening-window logic
htf_confluence.py          G9 higher-timeframe confluence
focus_engine.py            Pending validation and active position monitoring
order_manager.py           Entry, stops, exits, fill verification
capital_manager.py         Funds sync, sizing, capital slot control
fyers_broker_interface.py  Data/order WebSocket and broker abstraction
telegram_bot.py            Operator command and alert interface
database.py                PostgreSQL access layer
gate_result_logger.py      Gate audit trail and EOD rejection summary
ml_logger.py               Parquet/CSV ML observation logger
reconciliation.py          Broker/DB/internal-state reconciliation
eod_analyzer.py            EOD analytics, missed-signal audit, ML labeling
tools/trainer.py           Parameter search over ML parquet observations
```

---

## Quick Start

Install dependencies:

```bash
python -m venv .venv
. .venv/bin/activate
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
