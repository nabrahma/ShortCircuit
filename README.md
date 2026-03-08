# ShortCircuit ⚡

> *The market is not a place. It is a conversation between participants
> with asymmetric information, asymmetric conviction, and asymmetric speed.
> This system is an attempt to listen more carefully than the other side.*

***

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

***

## Why This Exists

Every market participant believes they have an edge.

Most are wrong — not because their ideas are bad, but because the gap between
*having an insight* and *acting on it correctly, every time, without hesitation,
without error* is wider than any strategy can bridge manually.

Human judgment degrades under pressure. Execution hesitates at exactly the wrong
moment. Stops get moved. Rules get broken at 2:47 PM when a position is down and
the brain starts negotiating with itself.

The negotiation is the problem. Not the market.

The mind under financial stress does not think in probabilities. It anchors to
entry price as if the market knows or cares where you bought. It holds losers
because closing them makes the loss *real*. It cuts winners because the relief
of booking profit overrides the logic of letting the setup play out.

These are not character flaws. They are the predictable outputs of a nervous system
that was not designed for adversarial probabilistic environments with real stakes.

ShortCircuit is not an attempt to replace judgment.
It is an attempt to protect it — by removing the execution layer from human control
entirely, and trusting only what can be verified, logged, and audited.

The rules are written when the mind is calm.
They execute when it is not.
That gap is the entire value of the system.

***

## What It Is

A fully automated, event-driven intraday short-selling system for NSE equities.

It watches the market continuously. It identifies one specific microstructure event —
institutional exhaustion at intraday highs. It validates that observation through
thirteen independent sequential checks. When all thirteen pass, it executes.

Between signals, it does nothing.
It does not overtrade. It does not hedge. It does not improvise.
The discipline is not in the trader. It is in the architecture.

***

## The Philosophy of Infrastructure First

Before any strategy logic was written, three problems were solved completely.
Not partially. Not "good enough for now." Completely.

**Authentication:**
A trading system that re-authenticates mid-session is not a trading system.
It is a liability with a Telegram interface. ShortCircuit uses a singleton OAuth token —
one authentication per deploy, persisted to disk, validated on startup via a
lightweight profile call. The broker session does not expire during trading hours. Ever.

**State:**
Every task shares one `asyncio.Event` — `shutdown_event`.
When any component sets it, every `while not shutdown_event.is_set()` loop exits
cleanly on its next iteration. There is no ambiguity about system state at any moment.
There is no thread racing on position state. There is no "was that order filled?" —
the Order WebSocket answers that question exactly once, immediately, without polling.
Certainty is not a luxury in live trading. It is a prerequisite.

**Failure:**
Every component has a documented failure mode.
Every failure mode has a handler.
Nothing fails silently.
Silence in a live system is always the most dangerous state.

***

## The Architecture

```
NSE Market (9:15 AM → 3:30 PM IST)
    │
    ├─ Data WebSocket (fyers_apiv3)
    │   Real-time tick feed. UNINITIALIZED → PRIMING → READY state machine.
    │   Seeded from REST at startup. Freshness tracked by WS-tick count, not REST age.
    │   Powers: Gate 12 two-tick validation, live P&L, SL monitoring.
    │
    └─ REST API (fyers_apiv3)
        Batch quotes (fallback only), candle history, order submission.
        Powers: Scan, Gates 1–12, entry/partial/exit orders.

Scanner
    2,418 NSE-EQ symbols. WS cache first — REST batch fallback (50 symbols/call).
    Pre-filter: gain ≥9%, volume >100k, LTP ≥₹50 (Fyers basket-rule safety floor).
    Minimum 45 candles before RVOL is treated as valid.
    Chart quality check: rejects symbols with >50% zero-volume or >50% doji candles.
    Output: candidate list, every ~60 seconds.

13-Gate Validation Framework
    Sequential. Failure at any gate = immediate rejection + GateResultLogger record.
    Gates 1–9: analyzer.py — REST snapshot + NIFTY macro context.
    Gates 10–12: focus_engine.py — WebSocket real-time price confirmation.
    Gate 13: signal_manager.py — post-trade outcome recording + loss streak guard.

Order Manager
    Entry: REST submit → WebSocket fill confirmation (15s timeout, REST verify fallback).
    SL: ATR-derived, tick-rounded, REST submit atomically with entry.
    Partial exits: cancel-first safe_exit() — phantom order prevention.
    SL qty sync: modify_sl_qty() after every partial close — stale SL kills.

Position Manager — Phase 52
    40/40/20 partial exit engine.
    TP1: 40% closed → SL moves to breakeven.
    TP2: 40% closed → SL locks to TP1 level.
    TP3: 20% runner → ATR × 0.5 trailing stop.
    CLOSED_EXTERNALLY detection → cleanup_orders() fires, no phantom SL left behind.
    sfp_watch_loop() monitors for Sweep-and-Flip pattern 10 minutes post-exit.

Capital Manager
    Source of truth: Fyers /funds API. Never a hardcoded base capital.
    Parses 3 Fyers response shapes. 2% safety buffer. Single-position slot lock.
    release_slot() calls sync() outside asyncio.Lock — deadlock prevention.

GateResultLogger
    Every gate evaluation → 36-column PostgreSQL record. Always. Regardless of outcome.
    Batched async flush. JSON-Lines fallback on any DB failure — zero silent data loss.
    Recoverable via tools/eod_reimport.py after market close.

Reconciliation Engine
    Detects orphaned positions (broker open, DB closed) and phantoms (DB open, broker flat).
    adopt_orphan(): emergency SL + capital slot + DB entry within 6 seconds.
    Idempotency guards at two levels — 600 SL orders/hour otherwise.
    _db_dirty flag terminates phantom detection loops — without it, same phantom fires every 6s forever.

Telegram
    The only interface. No web UI. No dashboard server. No REST endpoint.
    Every signal. Every fill. Every partial. Every alert. Every exit. Real-time.
```

***

## The 13 Gates

There is a specific reason there are thirteen and not three.

A single strong signal is a hypothesis.
Thirteen independent confirmations are closer to a fact.

Each gate is designed to kill the trade — not to approve it.
The system is structurally biased toward inaction.
A trade happens only when it runs out of reasons to reject.

This inverts the default psychology of a trader who looks for reasons to enter.
The system looks for reasons to stay out.
Whatever survives that search is worth acting on.

The gates have grown over time. Not because the original design was wrong,
but because the market kept finding the gaps. Each new gate is scar tissue
from a lesson that cost something to learn. They are not additions. They are corrections.

| Gate | ID | What It Kills |
|---|---|---|
| **G1** | SCANNER_QUALITY | Fewer than 45 candles, gain below 9%, >50% doji or zero-volume candles |
| **G2** | RVOL_VALIDITY | RVOL checked before 20 minutes of market data exists — invalid math |
| **G3** | CIRCUIT_GUARD | Session-permanent blacklist: any symbol that touched upper circuit today |
| **G4** | MOMENTUM | VWAP slope above 0.05 — the move is still in progress, not exhausted |
| **G5** | EXHAUSTION | Gain outside 9–14.5%, price not above VAH, pattern confidence below MEDIUM |
| **G6** | PRO_CONFLUENCE | Tiered DPOC/OI/tape scoring below threshold — no auto-passes at any tier |
| **G7** | TIME_GATE | Pre-10:00 AM, 12:00–13:00 PM lunch block, post-14:45 PM — market noise windows |
| **G8** | SIGNAL_LIMIT | 3-signal daily cap, 45-min per-symbol cooldown, 3-consecutive-loss pause |
| **G9** | HTF_STRUCTURE | No 15m Lower High confirmed with 1.5SD VWAP extension and volume fade |
| **G10** | ENTRY_CONFIRM | Two consecutive ticks below trigger required. Spread >0.4% → CAUTIOUS mode |
| **G11** | TIMEOUT | Dynamic expiry (3–15 min scaled to remaining session time) + above-high invalidation |
| **G12** | PRICE_STABILITY | Recovery above `signal_high × 1.002` after initial breach — signal invalidated |
| **G13** | OUTCOME_LOG | Post-trade result recorded. Three consecutive losses → full session pause |

Every rejection at every gate produces a `GateResult` record with the exact failing value,
the threshold it failed against, and the timestamp. The system cannot be accused of opacity.

Gate 9 used to live inside `_finalize_signal()` as a silent `return None` with no audit trail.
It was promoted to G9 with full recording specifically because invisible rejections
are indistinguishable from bugs. Every rejection is now visible, queryable, and reviewable.

Gate 13 does not block entries. It closes the feedback loop after they close.
It is the system watching itself.

***

## The Microstructure Event

The system hunts one specific setup.

A stock has moved 9–14.5% intraday on elevated volume. Retail momentum buyers
are extended. Market makers have been absorbing the buying at the high.
The last retail breakout buyers are now trapped above their entry.

The tell is not the candle pattern. The candle pattern is the last confirmation —
the visible shadow of something that already happened at the order book level.

The actual tell is the simultaneous presence of:
order flow evidence (absorption at the high, trapped long structure),
market profile deviation (LTP beyond 1.5SD above VWAP, POC far below current price),
higher-timeframe structure failure (15m Lower High with volume fade),
and volume character (extreme RVOL, zero net price progress — the definition of hidden supply).

When these confirm simultaneously, the setup is not a prediction.
It is an observation of something that has already happened.
The trade does not anticipate the move. It confirms it has begun.

Entry fires on the **second** consecutive WebSocket tick below the trigger.
Not the first. One tick is a probe. The second is direction.

If price recovers above `signal_high × 1.002` at any point before the second tick —
the signal is invalidated and removed. The buffer exists because markets test and retrace.
A 0.2% recovery is noise. Anything beyond it means the selling pressure was not real.

### Five Parallel Edge Detectors

| Detector | What It Finds |
|---|---|
| **Absorption Engine** | High volume, zero price progress — hidden limit supply absorbing every buy at the high |
| **Bad High Analyzer** | Level 2 DOM supply wall sitting at the day extreme — the ceiling is visible in the depth |
| **Trapped Long Scanner** | Failed breakout above prior high — retail buyers now underwater at a structural level |
| **Failed Auction Detector** | Range expansion exhaustion — price was rejected by *time*, not by a single visible order |
| **Classic Pattern Engine** | Bearish Engulfing, Shooting Star, Evening Star — volume-confirmed only, no pattern-only signals |

Confidence tiers: `EXTREME ≥ 5.0` | `HIGH ≥ 3.0` | `MEDIUM ≥ 2.0`

A MEDIUM signal without cross-confluence is rejected at G6.
A HIGH signal without HTF confirmation is rejected at G9.
EXTREME confidence does not bypass either.
Confidence is a weight, not a passport.

***

## The WS Cache — Freshness vs Presence

This distinction took multiple debugging sessions to discover and one architectural change to fix.

The original health monitor tracked `known_pct` — the fraction of symbols with *any* cached data,
whether from a REST seed or an actual WebSocket tick. This produced a system that reported
`HEALTHY` while running on hours-old REST snapshots, because seeded symbols counted as known.

The system scanned. It found candidates. It analyzed them.
It was analyzing the market from data that had not been live-ticked in 20 minutes.

The fix: DEGRADED status is now determined entirely by `fresh_pct` —
the fraction of symbols that have received an actual WebSocket tick within the TTL window.
REST-seeded data does not count toward freshness.

```
UNINITIALIZED   → broker constructed, no connection yet
PRIMING         → subscribe_scanner_universe() called, WS connecting
READY           → ≥85% of symbols have a valid WS tick within TTL

Startup blocks for up to 45 seconds waiting for READY.
If the 45s expires: CRITICAL alert + Telegram notification + REST fallback mode.

During live operation:
  fresh_pct drops → DEGRADED flag set (after 30s continuous degradation)
  Max 3 re-prime attempts per degraded episode
  3rd failure triggers nuclear reconnect: full WS teardown, 5s sleep, full re-subscribe + re-seed
  3rd failure also triggers UNRECOVERABLE Telegram alert
```

`seed_from_rest()` runs before `subscribe_scanner_universe()` at startup.
This prevents "Missing:" inflation on cold starts where symbols take time to receive their first tick.
REST-seeded entries are tagged `REST_SEED` and excluded from freshness calculations.
They exist only to give the system something to read while WS catches up.

***

## The Execution Bug — On Silence as the Worst Failure Mode

In January 2026, live trading began.

For two months, the system scanned correctly. Signals were detected correctly.
Gate analysis passed. Candidates were forwarded to the execution path.

Zero trades were placed.

The cause was a missing `await`.

```python
# Wrong — runs for two months
pos = self.order_manager.enter_position(signal_data)

# Correct
pos = await self.order_manager.enter_position(signal_data)
```

`enter_position` is `async def`. Without `await`, Python returns a coroutine object —
not a result, not an error, not a warning. A coroutine object is truthy.
The `if pos:` block entered. `pos.get()` crashed. The exception was caught.
The system logged the error, moved on, and kept scanning.

Every cycle. For two months.

The lesson is not about async/await syntax.
The lesson is about what a system does when it fails.
This system failed silently and cleanly, which made the failure nearly invisible.

Every critical path now validates its own output.
`_validate_dependencies()` hard-crashes the process before the first scan if any
component is `None`. The startup validation gate makes a live candle API call —
if it returns nothing, the process exits before any trading begins.

Silence is no longer acceptable as a failure mode.

***

## The Capital Architecture

The capital manager has no hardcoded base capital.

Every sizing decision derives from a live call to the Fyers `/funds` API.
`_real_margin` is updated at session start, after every confirmed fill,
after every position close, and every 5 minutes in the background health monitor.
The system trades with what the broker says is available — nothing more, nothing less.

Three Fyers `/funds` response shapes exist across API versions.
All three are parsed:

```python
_parse_fyers_funds():
    → fund_limit list (v3 standard) — indexed by id=2 "Available Balance"
    → equity dict — direct key lookup
    → flat dict — top-level key lookup
```

On any Fyers API failure, `sync()` retains the last known value and logs a warning.
It does not crash. It does not zero out capital. It continues with the last confirmed figure.

`compute_qty()` applies a 2% safety buffer (`safety_cap = _real_margin × 0.98`)
and walks down quantity until `margin_required ≤ safety_cap`.
This buffer absorbs intraday margin fluctuations without ever touching the hard limit.

`release_slot()` calls `sync(broker)` *outside* the `asyncio.Lock`.
This is deliberate. Calling a potentially-blocking broker sync *inside* a lock
would deadlock any concurrent coroutine waiting to check slot state.
The lock protects state mutation. The sync does not need to be inside it.

***

## The Signal Slot Problem

Before Phase 51, a signal burned its daily slot at detection.

A signal detected at 10:30 AM that timed out at 10:45 AM (never validated, never traded)
still consumed one of the day's five slots. After four such timeouts, the system
went silent for the rest of the session with trades still possible in the afternoon.

The fix: `signal_manager.record_signal()` is called only at `enter_position()` success —
not at detection, not at validation, not at pending queue entry.

```
Detection           → signal logged to CSV, ML observation recorded
Validation (G12)    → two-tick confirmation
enter_position()    → fill confirmed via WebSocket
record_signal()     → slot burned here, and only here
```

A signal that times out is a signal that did not trade.
It should not cost a slot. It does not.

***

## The Orphan Problem

You open a position manually on the Fyers app during lunch.
The bot's reconciliation engine runs its 6-second cycle and sees a broker position
with no corresponding DB entry.

Before Phase 44.9.3, the engine logged a warning and moved on.
On the next cycle — 6 seconds later — it detected the same position again.
And placed another SL order. And another. And another.

At 10 cycles per minute: approximately 600 SL orders per hour.

The fix is `adopt_orphan()`:

```
detect orphan → idempotency check (symbol already in active_positions?) → skip if yes
              → place tick-rounded emergency SL via REST
              → acquire capital slot
              → log_trade_entry() to DB atomically
              → _db_dirty = True   ← forces fresh DB read next cycle
              → if capital slot occupied: CRITICAL alert "TWO POSITIONS OPEN"
```

The idempotency guard exists at two levels: inside `adopt_orphan()` itself,
and in the `_handle_divergence()` ORPHANS loop that calls it.
Two concurrent reconciliation cycles cannot double-adopt the same position.

`_db_dirty = True` is the mechanism that terminates the detection loop.
Without it, the adopted position remains invisible to the next DB read,
the orphan is re-detected, and the cycle repeats.

The same dirty flag applies to phantom cleanup:
after a manually-closed position is processed, `_db_dirty = True` is set —
forcing a fresh DB read on the next cycle so the cleaned phantom does not re-appear.

***

## The Partial Exit Problem

At TP1, 40% of the position is closed. A new REST call modifies the active SL-M order
to reflect the remaining quantity.

If this step is skipped, the SL-M order retains the original full quantity.
When price hits the stop, the broker tries to sell more shares than currently held.
For a short position, selling more than held is a *buy*. The system would accidentally
open a long position in the opposite direction.

This is not a theoretical edge case. It is a predictable consequence of partial exits
without SL quantity synchronization. `modify_sl_qty()` fires after every partial close.
No exceptions.

`safe_exit()` uses cancel-first logic:

```
1. Send cancel request for existing SL-M order
2. Wait for cancel confirmation
3. Submit new exit order at current market
```

Submitting an exit without cancelling first risks two fills against the same position —
one from the SL-M trigger and one from the manual exit. Cancel-first eliminates this.
The cancelled order may already be filled. `safe_exit()` handles that case too:
if the broker responds that the order is not cancellable (already executed),
the fill is verified via REST before any new order is submitted.

***

## The Gate Audit Trail

Every gate evaluation produces a row in the `gate_results` table.
Every row. Whether the signal fired, timed out, was rejected at G1, or was suppressed
because auto mode was off. 36 columns per row.

```sql
verdict IN ('SIGNAL_FIRED', 'REJECTED', 'DATA_ERROR', 'SUPPRESSED')
first_fail_gate: 'G5_EXHAUSTION', 'G9_HTF_STRUCTURE', 'G12_TIMEOUT', ...
rejection_reason: exact human-readable threshold description
data_tier: 'WS_CACHE' | 'HYBRID' | 'REST_EMERGENCY'
```

Phase 44.9 discovered that `g9_value` and `g11_value` were typed as `NUMERIC` in PostgreSQL,
but both HTF structure gate (G9) and the timeout gate (G11) return string rejection reasons
(`"NO_HTF_LOWER_HIGH"`, `"TIMEOUT_EXPIRED"`). Every batch insert failed silently with
`decimal.ConversionSyntax`. 2,300+ gate records per session were disappearing into `/dev/null`.

Migration `v44_8_3_gate_results_g9_type_fix.sql` alters both columns to `VARCHAR(100)`.
The `_sanitize_row()` function in `GateResultLogger` now coerces every value
to its expected type before `executemany`. No type mismatch can silently drop a record.

On any DB write failure, `GateResultLogger` falls through to `_flush_to_json_fallback()`:

```python
# Non-blocking fallback via aiofiles
# Appends to logs/gate_fallback_YYYYMMDD.jsonl
# Recoverable via tools/eod_reimport.py after market close
```

There is no state in which a gate evaluation produces no record.
The fallback is not a degraded mode. It is the second line of an unbreakable audit trail.

***

## Two WebSockets. Separate Concerns.

```
Data WebSocket
    ─ Tick feed for all subscribed symbols
    ─ Gate 12: two consecutive ticks below trigger = execution
    ─ Gate 12 invalidation: recovery above signal_high × 1.002 cancels the signal
    ─ Position monitor: SL/TP levels checked on every tick
    ─ Dashboard: 2s P&L refresh, broker-verified LTP

Order WebSocket
    ─ Fill events: PENDING → TRADED status change
    ─ Entry fill, SL-M hit, partial exit fill confirmation
    ─ Capital slot released only after TRADED event — never on REST response alone
```

Both run as daemon threads using the blocking Fyers SDK.
Callbacks bridge to the `asyncio` event loop via `asyncio.run_coroutine_threadsafe()`
using `self._loop` captured during `initialize()` via `asyncio.get_running_loop()` —
never `get_event_loop()` from a thread, which is unsafe in Python 3.12.

***

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

`_supervised()` wraps each task — crashes retry up to `max_retries` within a rolling window,
then hard-fail with a critical Telegram alert. `_validate_dependencies()` hard-crashes
before the TaskGroup starts if any critical dependency is `None`.
No degraded state. No partial systems that look alive but cannot execute.

***

## The 40/40/20 Exit Engine

Partial exits are not a compromise. They are a statement about uncertainty.

You do not know that TP2 will hit when TP1 does. No one does.
Taking 40% at TP1 is not discipline failure. It is probability applied honestly.
The remaining 60% now rides with a breakeven stop — after TP1,
the worst possible outcome is zero loss. That changes the psychology of holding entirely.
You are no longer asking the market for permission to feel good.

```
Entry: Short 100 shares

TP1 (ATR × 1.5) hit:
    → Close 40 shares via REST
    → modify_sl_qty(60) — broker SL-M updated immediately
    → SL moves to entry price: zero remaining risk
    → 60 shares remain on a free ride

TP2 (ATR × 2.5) hit:
    → Close 40 shares via REST
    → modify_sl_qty(20) — broker SL-M updated immediately
    → SL locks to TP1 level: profit guaranteed on runner
    → 20 shares remain: trailing begins

TP3 zone (ATR × 3.5+):
    → 20-share runner trails at ATR × 0.5 distance
    → Closes on structure break, not on a fixed price
    → The market decides when it is done
```

### Human Intervention Safety

You can override the system. The system accepts that.
What it does not do is leave a mess when you do.

```
You close the position manually on the Fyers app:
    → CLOSED_EXTERNALLY detected within 2 ticks
    → stop_focus() halts the position monitor
    → cleanup_orders() cancels ALL pending SL/TP orders immediately
    → _finalize_closed_position() updates DB: OPEN → CLOSED
    → Capital slot released
    → Gate 13 does not record the outcome — human action ≠ system performance data

You open a position manually (orphan):
    → ReconciliationEngine detects within 6 seconds
    → adopt_orphan() places emergency SL, acquires slot, writes DB
    → _db_dirty = True prevents re-detection on next cycle
    → If a system trade is already open: CRITICAL Telegram alert "TWO POSITIONS OPEN"
```

***

## Reconciliation

The reconciliation engine runs continuously.
It compares DB state to broker state — detecting orphans, phantoms, and quantity mismatches.

It is aware of its own cost:

```
Market hours, open positions   → every 6 seconds
Off-hours, open positions      → every 30 seconds
Off-hours, fully flat          → every 300 seconds
```

When flat off-hours, `_has_open_positions = False` short-circuits the entire cycle.
Zero DB queries. Zero broker REST calls. Zero WS checks.
The system is genuinely idle when there is nothing to protect.
Idle compute is not discipline. It is noise.

***

## EOD Shutdown

EOD is the highest-risk operational moment — not because of the market,
but because of the software. A stuck scan loop, a hung DB query,
or a WebSocket reconnect in backoff can prevent shutdown and leave
open positions in after-hours where they cannot be managed.

Two independent mechanisms. Neither depends on the other:

```
15:10 IST  → TradeManager.close_all_positions()  [hard square-off]
15:32 IST  → eod_scheduler fires → shutdown_event.set()
15:32 IST  → eod_watchdog independently fires → shutdown_event.set()
15:40 IST  → eod_watchdog → os.kill(os.getpid(), SIGTERM)  [cannot be trapped]
```

The watchdog checks every 30 seconds from within its own isolated task.
No scan loop, no DB hang, no WS thread can block it.

Maximum graceful shutdown window: **25 seconds.**

```
reconciliation_engine.stop()  → 10s timeout
bot.stop()                    → 5s timeout
db_pool.close()               → 5s timeout
broker.disconnect()           → 5s timeout
```

***

## The SL State Machine

A stop loss is not a number. It is a state. And state has a history.

```
INITIAL    → max(ATR × 0.5, 3 × tick_size) above signal_high.
             Placed atomically with entry. Immovable until TP1.
             Defines the maximum loss before the trade breathes.
     ↓
BREAKEVEN  → SL moves to entry after TP1 hit.
             The trade has paid for itself.
             A winning trade that becomes a loss is not acceptable. Ever.
     ↓
TP1_LEVEL  → SL locks to TP1 price after TP2 hit.
             Profit on the runner is now guaranteed regardless of outcome.
     ↓
TRAILING   → SL trails at ATR × 0.5 distance on the runner.
             Only tightens. Never widens.
             The market decides when the move is over.
```

### Exit Hierarchy

Seven exit types. Priority order is fixed and never negotiated.

```
1. EMERGENCY       → Full close. No confirmation. No hesitation.
2. HARD_SL         → Broker SL-M triggered. Order WS fill confirmation.
3. SOFT_STOP       → DiscretionaryEngine: orderflow reversal detected pre-SL.
4. TP1 (40%)       → ATR × 1.5. Partial close. modify_sl_qty(). SL → breakeven.
5. TP2 (40%)       → ATR × 2.5. Partial close. modify_sl_qty(). SL → TP1 level.
6. TP3 TRAIL (20%) → ATR × 3.5 zone. Runner trails. Structure break closes it.
7. EOD_SQUAREOFF   → 15:10 IST. Hard close. No exceptions.
```

***

## The Live Dashboard

```
⚡ ACTIVE TRADE — SHORT

NSE:TATASTEEL-EQ
Entry: ₹849.20  |  Qty: 10  |  Margin: ₹1,699

━━━━━━━━━━━━━━━━━━━━━━━━
LTP:    ₹842.50  ⬇️
P&L:    +₹67.00  (+0.79%)

SL:     ₹849.20  [BREAKEVEN 🔒]
TP1:    ₹836.54  (ATR×1.5) — 40% [HIT ✅]
TP2:    ₹827.55  (ATR×2.5) — 40%
TP3:    ₹818.56  (ATR×3.5) — 20% runner, trail active

OF:     🔴 BEARISH CONFIRMED — Trapped longs detected
HTF:    ✅ 15m Lower High: 849.80 → 847.40
━━━━━━━━━━━━━━━━━━━━━━━━

[🔄 Refresh]  [❌ Close Now]
```

Broker-verified via Data WebSocket every 2 seconds.
Not estimated. Not interpolated. Not from the last REST snapshot.

***

## What Gets Logged

Everything. Without exception.

```
logs/YYYY-MM-DD_session.log         — daily session snapshot, written at shutdown
logs/signals.csv                    — every signal: executed, rejected, timed out, suppressed
logs/rejections_YYYYMMDD.log        — gate-level rejection reasons with exact threshold values
logs/diagnostic_analysis.csv        — every /why query, gate-by-gate breakdown
logs/emergency_alerts.log           — critical failure events only
logs/gate_fallback_YYYYMMDD.jsonl   — DB-failure recovery buffer (aiofiles, non-blocking)
data/ml/YYYY-MM-DD.parquet          — 40+ features per signal, UUID4 observation ID
data/trade_journal.csv              — human-readable trade record
```

The gate fallback log exists because DB failures during market hours are not recoverable
in real time. The JSON-Lines file accumulates all gate records that could not be written to PostgreSQL.
After market close, `tools/eod_reimport.py` replays them into `gate_results`.
No audit record is ever permanently lost.

The rejection log is the most useful file in the system.
Every missed signal has a reason. Every reason has a threshold.
Every threshold is a choice that can be revisited with evidence.

The ML parquet exists because today's rejected signals are tomorrow's training labels.
The system is building the dataset that will eventually allow it to learn from its own history.
Every observation recorded today is a vote in a future decision the system cannot yet make.

***

## The `/why` Command

```
/why RELIANCE 14:25
```

The system reruns the full 13-gate analysis on historical data for that symbol at that timestamp
and returns a gate-by-gate pass/fail breakdown to Telegram, including the exact value
that caused each rejection and the threshold it failed against.

The system cannot be accused of opacity.
Every missed signal has a reason. Every reason is visible.
Every threshold is adjustable with evidence.
Every run is appended to `logs/diagnostic_analysis.csv`. Cumulative.

***

## The Database

PostgreSQL 14+. asyncpg. Pool: 10 minimum, 50 maximum connections.

Four tables:

```
positions           — every trade: entry, exit, size, P&L, status, partial timestamps
orders              — every order: submission, fill, status, broker ID, qty modifications
reconciliation_log  — every reconciliation cycle: divergences, timestamps, resolution actions
gate_results        — every gate evaluation: 36 columns, verdict, first_fail_gate, data_tier
```

`log_trade_entry()` wraps `positions` and `orders` in a single atomic transaction.
Either both succeed or neither does.
There is no state where a position exists without a corresponding order record.
The database is the source of truth. Not memory. Not the broker. The database.

***

## Build History

The system is not finished. It has never been finished.
Every phase is a response to something the previous version got wrong —
in live conditions, under real pressure, with real consequences.
This is not a version log. It is a record of what the market corrected.

```
Phase 37    Validation Gate — Gate 12 WebSocket tick confirmation
Phase 41    Multi-edge detection, intelligent exits, session management
Phase 42    Position safety, capital management, reconciliation engine
Phase 43    Cooldown queue, cooldown unlock mechanism
Phase 44    Async order manager, WS fill detection, capital live-sync
            The await fix — singular cause of 2 months of zero execution
            WS cache state machine — fresh_pct replaces known_pct for health
            Gate result audit trail — 36-column GateResultLogger
            Orphan adoption rewrite — idempotency, DB write, dirty flag
            Phantom cleanup rewrite — _finalize_closed_position(), capital fix
            Signal slot fix — record_signal() moved to fill confirmation
            Stale signal flush — pre-open signals dropped at 9:45 boundary
Phase 51    Gate hardening — 26 targeted fixes across G1–G13
            9% scanner floor | 45-candle RVOL minimum
            Session-permanent circuit blacklist
            Time gate (pre-10 AM, lunch, post-14:45)
            Tiered confluence scoring — no auto-passes
            HTF rebuilt with 1.5SD VWAP + volume fade
            Two-tick entry confirmation + invalidation buffer
            ATR-based SL replacing fixed percentages
            g9_value / g11_value VARCHAR migration — 2,300+ records recovered
Phase 52    40/40/20 partial exit engine
            cancel-first safe_exit() — phantom order prevention
            modify_sl_qty() after every partial close — accidental long prevention
            CLOSED_EXTERNALLY detection with cleanup_orders()
            G13 call-site isolation — exactly 3 locations in codebase
```

***

## Technical Stack

| Layer | Technology |
|---|---|
| Language | Python 3.10+ |
| Concurrency | `asyncio.TaskGroup` + `threading` (WS daemon threads) |
| Broker | Fyers API v3 — REST + dual WebSocket |
| Database | PostgreSQL 14+ via asyncpg (pool: 10–50) |
| Interface | python-telegram-bot v20+ (PTB) |
| Data | pandas, numpy, Apache Parquet, aiofiles |
| Auth | OAuth 2.0, singleton token, file persistence |
| Logging | `RotatingFileHandler` 10MB × 5 + dated daily session snapshot |

***

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
python apply_migration.py  # runs v42, v44_8_2, v44_8_3 in sequence

python main.py
# OAuth flow on first run. Token saved to data/access_token.txt.
# No re-auth needed on subsequent restarts.
```

Before enabling auto-trade, verify startup log contains all three lines:

```
[INIT]     ✅ OrderManager constructed and injected into FocusEngine.
[STARTUP]  ✅ All dependency checks passed. Safe to trade.
[WATCHDOG] ✅ EOD watchdog started. Monitoring for 15:32 IST.
```

If any line is missing — the system failed a dependency check before the first scan.
The failure reason is in the log immediately above. Fix it before trading.
The system is designed to fail loudly. Silence is not readiness.

***

## Commands

| Command | What It Does |
|---|---|
| `/auto on` | Enable autonomous execution (queued if sent pre-9:45 AM) |
| `/auto off` | Revert to alert-only mode |
| `/status` | Capital state (live Fyers margin), open positions, session P&L, WS health |
| `/positions` | All open positions with broker-verified P&L |
| `/pnl` | Session P&L summary |
| `/why SYMBOL TIME` | Full 13-gate replay — exact failing values and thresholds |
| `/pause` | Suspend signal generation |
| `/resume` | Resume scanning |

***

## Who Should Use This

People who understand that an algo does not make you a better trader.
It makes you a *more consistent* trader — which is only valuable if the
underlying judgment is already sound.

The system enforces discipline mechanically. But the parameters it enforces —
the gain thresholds, the RVOL minimums, the ATR multiples, the HTF structure rules —
those were written by a human. If that human's understanding of markets is wrong,
the system will execute that wrongness with perfect consistency.

There is something quietly clarifying about that.
You cannot blame the rules for following your own instructions.
The system is a mirror. What it shows you is what you believed
when you were thinking clearly, run at scale, without hesitation.

Read the code before running it with real capital.
Understand every gate before trusting any of them.
Monitor the system during market hours — not to intervene, but to learn.

This is a tool. The responsibility for how it is used
remains entirely with the person who built it.

***

## Risk

Markets are adversarial. Not competitive — adversarial.
Someone is on the other side of every fill, and they are not neutral about the outcome.

The system is built around a statistical edge — not certainty. Never certainty.
Losses are expected. Drawdowns are modeled for.
Three consecutive losses trigger an automatic pause — not because three losses means
the edge is gone, but because three losses in sequence is a signal to stop and look,
not to keep firing.

The edge, if it exists, is in the microstructure event described above —
a real, repeatable phenomenon in liquid markets, detectable with the right instrumentation
and the right filters applied in the right sequence.

Whether that edge persists, degrades, or disappears entirely is an empirical question.
The only honest answer is a live trading record built over months, not a backtest.

This system is built to generate that record cleanly.
That is the only honest claim it makes.

***

## Security

- All credentials in `.env` — never transmitted, never logged, never hardcoded
- No telemetry, no external data collection, no callbacks home
- OAuth 2.0 — no broker password stored anywhere in the system
- Trade and read permissions only — withdrawal access is structurally impossible
- Fully open source — every gate, every order path, every failure handler is auditable

***

## License

Apache 2.0. Use, modify, distribute. No warranty.

Trading equities involves substantial risk of capital loss.
The software is provided as-is. No liability for trading losses,
system failures, or consequential damages of any kind.

You are responsible for understanding what you run.

***

*[@nabrahma](https://github.com/nabrahma)*

**ShortCircuit. Built to listen carefully.** ⚡

```bash
git clone https://github.com/nabrahma/ShortCircuit.git
```
