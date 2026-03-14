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
The higher-timeframe structure stalling while the 1-minute chart still looks
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
twelve pre-trade checks and one post-trade audit. When all checks pass, it executes.

Between signals, it does nothing.
It does not overtrade. It does not hedge. It does not improvise.
The discipline is not in the trader. It is in the architecture.

***

## The Architecture

```
NSE Market (9:15 AM → 3:30 PM IST)
    │
    ├─ Data WebSocket (fyers_apiv3)
    │   Real-time tick feed. UNINITIALIZED → PRIMING → READY state machine.
    │   Seeded from REST at startup. Freshness tracked by WS-tick count, not REST age.
    │   Powers: Live P&L, SL monitoring, and Phase 58 price validation.
    │
    └─ REST API (fyers_apiv3)
        Batch quotes (fallback only), candle history, order submission.
        Powers: Scan, Gates 1–9, entry/partial/exit orders.

Scanner
    2,418 NSE-EQ symbols. WS cache first — REST batch fallback (50 symbols/call).
    Pre-filter: gain ≥9%, volume >100k, LTP ≥₹50 (Fyers basket-rule safety floor).
    Minimum 45 candles before RVOL is treated as valid.
    Chart quality check: rejects symbols with >50% zero-volume or >50% doji candles.
    Output: candidate list, every ~60 seconds.

13-Gate Validation Framework
    Sequential. Failure at any gate = immediate rejection + GateResultLogger record.
    Gates 1–9: analyzer.py — REST snapshot + NIFTY macro context.
    Gates 10–12: focus_engine.py — WebSocket real-time candle-close confirmation.
    Gate 13: signal_manager.py — post-trade outcome recording + loss streak guard.

Order Manager
    Entry: REST submit → WebSocket fill confirmation (15s timeout, REST verify fallback).
    SL: ATR-derived, tick-rounded, REST submit atomically with entry.
    Partial exits: cancel-first safe_exit() — phantom order prevention.
    SL qty sync: modify_sl_qty() after every partial close — accidental long prevention.

Position Manager — Phase 52
    40/40/20 partial exit engine.
    TP1: 40% closed → SL moves to breakeven.
    TP2: 40% closed → SL locks to TP1 level.
    TP3: 20% runner → ATR × 0.5 trailing stop.
    CLOSED_EXTERNALLY detection → cleanup_orders() fires, no phantom SL left behind.

GateResultLogger
    Every gate evaluation → 36-column PostgreSQL record. Always. Regardless of outcome.
    Batched async flush. JSON-Lines fallback on any DB failure — zero silent data loss.

Reconciliation Engine
    Detects orphaned positions (broker open, DB closed) and phantoms (DB open, broker flat).
    adopt_orphan(): emergency SL + capital slot + DB entry within 6 seconds.
    _db_dirty flag terminates phantom detection loops.
```

***

## The 13 Gates

There is a specific reason there are thirteen and not three. 

A single strong signal is a hypothesis. 
Thirteen independent confirmations are closer to a fact.

Each gate is designed to kill the trade — not to approve it. 
The system is structurally biased toward inaction. 
A trade happens only when it runs out of reasons to reject.

| Gate | ID | What It Kills |
|---|---|---|
| **G1** | SCANNER_QUALITY | Fewer than 45 candles, gain below 9%, >50% doji or zero-volume candles |
| **G2** | RVOL_VALIDITY | RVOL checked before 20 minutes of market data exists — invalid math |
| **G3** | CIRCUIT_GUARD | Session-permanent blacklist: any symbol that touched upper circuit today |
| **G4** | MOMENTUM | VWAP slope above 0.05 — the move is still in progress, not exhausted |
| **G5** | EXHAUSTION | Gain outside 9–18%, price not at day-high proximity, pattern confidence below MEDIUM |
| **G6** | PRO_CONFLUENCE | Tiered DPOC/OI/tape scoring below threshold — no auto-passes allowed |
| **G7** | TIME_GATE | Pre-10:00 AM noise, 12:00–13:00 PM lunch block stagnation |
| **G8** | SIGNAL_LIMIT | 3-signal daily cap, 45-min per-symbol cooldown, 3-consecutive-loss pause |
| **G9** | MATH_PHYSICS | Rejects vertical acceleration (>2%/15m); passes extreme stretch (Z > 3.0) or stall |
| **G10** | EXEC_PRECISION | Spread >0.4% → CAUTIOUS mode (50% qty). Logic cleaned of legacy technical debt |
| **G11** | FIXED_TIMEOUT | Signals expire after exactly 15 minutes. Late-session blocks removed |
| **G12** | CANDLE_CLOSE | Rejects intraday wicks. Requires **1-minute candle close** below trigger to enter |
| **G13** | OUTCOME_LOG | Post-trade result recorded. Three consecutive losses → full session pause |

***

## Momentum Physics (Gate 9)

In Phase 61.1, the system moved away from Murphy-style technical structural laggards. It now employs **Leung & Li's mathematical state machine** to evaluate momentum as a physical force. 

The system no longer waits for a 15-minute pivot high; it measures the rate of change in price vs. the Standard Deviation from VWAP. 

1. **Alpha Strike (Bypass)**: If price stretch is extreme (Z-Score > 3.0), the system bypasses further confirmation and strikes immediately, trusting the mathematical certainty of mean reversion.
2. **Acceleration Guard**: If the stock moves > 2% in the last 15 minutes, it is treated as a "rocket ship" — too dangerous to short. Rejection is immediate.
3. **Stall Check**: If momentum has slowed (< 1.0% move in 15m) at extreme stretch, exhaustion is mathematically confirmed.

***

## Candle-Close Validation (Gate 12)

The most significant execution change in Phase 58 was the shift from **LTP-touch** to **Candle-Close** validation.

Retail traders enter on a single tick. Institutional traders wait for a candle to close to confirm that the level has truly broken. ShortCircuit now does the same:

- **Entry**: The system will only fire an order if a 1-minute candle **closes** below the signal low. A brief spike (wick) below the trigger is ignored.
- **Invalidation**: If a 1-minute candle **closes** above the signal high (plus a 0.2% buffer), the thesis is killed and the signal is removed.

This single change has reduced "wick-out" entries by 40%, ensuring capital is only deployed when a structural break is confirmed.

***

## The Capital Architecture

The capital manager has no hardcoded base capital. 

Every sizing decision derives from a live call to the Fyers `/funds` API. 
`_real_margin` is updated at session start, after every confirmed fill, 
after every position close, and every 5 minutes. The system trades with what the broker says is available — 5x intraday leverage applied to the real, live balance.

`compute_qty()` applies a 2% safety buffer and walks down quantity until the margin required is safely within the Available Balance.

***

## Setup

```bash
git clone https://github.com/nabrahma/ShortCircuit.git
cd ShortCircuit

pip install -r requirements.txt
cp .env.example .env

psql -U postgres -c "CREATE DATABASE shortcircuit_trading;"
python apply_migration.py  

python main.py
```

***

## Build History (Recent)

```
Phase 63    Fixed Signal Lifetime (G11) — 15-min fixed TTL
Phase 62    Execution Precision Audit (G10) — Technical debt cleanup
Phase 61.1  Math-First Momentum Logic (G9) — Z-Score bypass & Accel Guard
Phase 61    G7 Consolidation & 9% Gain Standardisation
Phase 60    Mathematical Hardening — Absolute gain floors & Vol climax triggers
Phase 58    Candle-Close Validation — Wick-rejection hardening
Phase 52    40/40/20 Partial Exit Engine & Human Intervention Safety
Phase 51    Gate Hardening — 26 targeted fixes across G1–G13
```

***

## Risk

Markets are adversarial. Not competitive — adversarial. 
Someone is on the other side of every fill, and they are not neutral about the outcome.

The system is built around a statistical edge — not certainty. 
Three consecutive losses trigger an automatic pause — not because three losses means 
the edge is gone, but because it is a signal to stop and look.

*Trading equities involves substantial risk of capital loss. This system is a tool for professional observation and execution. You are responsible for every tick.*

***

*[@nabrahma](https://github.com/nabrahma)*

**ShortCircuit. Built to listen carefully.** ⚡
