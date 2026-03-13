# ShortCircuit — Build Progress

System is live-capable. Tests: 85 passed, 1 intentional skip.
AUTO_MODE = False. No active build in progress.
Last updated: 13 Mar 2026

## How to Use This File
Before starting any task, read this to understand what the
system can and cannot do right now. After finishing a task,
append a summary at the TOP of the Completed Phases section.

## Completed Phases (Most Recent First)

### Phase 63 — G11 Refactor & Simplification (Time Gate)
Date: 13 Mar 2026
- **Fixed Signal Lifetime**: Replaced dynamic late-session timeout with a fixed 15-minute window for all signals.
- **Documentation Cleanup**: Purged redundant invalidation paths and obsolete logic comments in `focus_engine.py`.
- **Verification**: Verified with full regression suite. Total: 85 passed.

### Phase 62 — G10 Audit & Cleanup (Execution Precision)
Date: 13 Mar 2026
- **Spread Guard (G10.1)**: Refactored and optimized spread validation logic in `focus_engine.py`.
- **Code Hygiene**: Purged legacy comments and dead code blocks regarding the obsolete "Two-tick confirmation" logic.
- **Verification**: Verified with full regression suite. Total: 85 passed.

### Phase 61.1 — Math-First G9 (Momentum Physics)
Date: 13 Mar 2026
- **Architecture Shift**: Replaced Murphy-style structural laggards (15m pivots/5m counts) with Leung & Li's mathematical state machine.
- **Alpha Strike Bypass**: Extreme extensions (VWAP SD > 3.0) now bypass HTF confirmation for immediate strike.
- **Physics Filters**: Implemented Acceleration Guard (rejects vertical squeezes > 2% move/15m) and Stall Check (allows trades when move slows < 1% move/15m).
- **Execution Efficiency**: Reduced API latency by eliminating redundant 5-minute candle history fetches in HTF module.
- **Verification**: Verified with `tests/test_g9_math_logic.py` and full suite. Total: 85 passed (81 baseline + 4 new math tests).

### Phase 61 — G7 Cleanup & Consolidation
Date: 13 Mar 2026
- **G7 Architecture:** Consolidated fragmented methods (`get_market_regime`, `is_favorable_time_for_shorts`, `should_allow_short`) into a single high-performance `evaluate_g7()`.
- **Nifty Caching:** Implemented 5-minute TTL cache for index data. Significantly reduces API overhead during multi-symbol parallel analysis.
- **Dead Code Purge:** Removed incomplete override logic, redundant `_calculate_morning_range` helper, and commented-out legacy blocks.
- **Scanner Precision:** Removed `abs()` from gain filter to strictly follow the "massive gainers only" strategy (Gain >= 9%).
- **Bug Fixes:** Resolved data gap for Previous Close in Hybrid mode and fixed `test_gap_v2_1` mocking anomalies.
- **Verification:** Verified with full 81-test suite. All tests PASSED.

Date: 12 Mar 2026
- **G4 Structural Fallback:** Implemented absolute gain floor (10%) to waive Z-score requirements during momentum decay. Prevents "Normalization Bias" where VWAP catching up would block valid entries on structurally overextended stocks.
- **Spear of Exhaustion (G5.4):** Implemented high-conviction liquidity climax trigger. Detects stop-run highs followed by rejection closes on ≥3x volume climax.
- **Confidence Upgrade:** Spear of Exhaustion patterns now trigger **MAX_CONVICTION** confidence when confluencing with base exhaustion.
- **Verification:** Verified with `tests/test_phase_60_hardening.py`. Total system tests: 89 passed, 1 skipped.

### Phase 59 — Auction Rejection & Volume Profile
Date: 12 Mar 2026
- **Volume Profile Upgrade:** Upgraded `market_profile.py` to use Volume-Weighted bins (`mode='VOLUME'`). System now calculates **vPoC**, **vVAH**, and **vVAL** reflecting institutional fair value.
- **"Look Above & Fail" Setup:** Implemented `VAH_REJECTION` pattern in `god_mode_logic.py`. Detects price probes above VAH followed by acceptance back into the value area.
- **G5 Refactor:** Modified Exhaustion Gate to allow trades where the close is below VAH, provided an auction rejection is detected.
- **"Holy Grail" Confluence:** Added a new **MAX_CONVICTION** confidence tier for setups hitting VAH Rejection + VWAP-SD stretch (>2.2) after 10:45 AM.
- **Verification:** Verified with `tests/test_phase_59_vah_logic.py`. 3/3 passed.
Status: 86 passed (83 baseline + 3 new).

### Phase 58 — G12 Candle-Close Validation
Date: 12 Mar 2026
- **Validation Refactor:** G12 now uses 1-minute candle closes instead of LTP touches for entries and invalidations.
- **G10 Two-Tick Removal:** Completely removed the "two-tick" confirmation logic as it is superseded by candle-close/LTP-immediate logic.
- **Noise Reduction:** Prevents "wick-outs" where temporary price spikes above the signal high would kill valid signals.
- **Murphy Closing Filter:** Implemented logic to wait for candle boundary confirmation (IST-aligned).
Status: 83/83 passed.

### Phase 57 — Mean Reversion Optimization & Lunch Block Removal
Date: 12 Mar 2026
- **G4 Slope Decay (Guo-Zhang Model):** Implemented Murphy Divergence check. Allows shorting even if momentum is above threshold, provided it is slowing down (slope_now < slope_prev) and price is extremely extended (>1.5 SD).
- **G5 Absorption (Z-Process):** Relaxed volume fade requirement (max 0.95 vs 0.65) for candidates at extreme extensions (>3.3 SD) showing narrow-body "Absorption Dojis".
- **Lunch Block Removal:** Disabled the G7 time-gate block for 12:00 PM – 1:00 PM IST per user request.
- **Bug Fix:** Corrected `datetime.time` import/call in `is_exhaustion_at_stretch`.
Status: 83/83 tests passed.

### Phase 56 — Schema Expansion for Audit Trail
Date: 11 Mar 2026
Widened PostgreSQL columns in `gate_results` to prevent `StringDataRightTruncationError`.
Affected columns: `g6_value`, `g7_value`, `g11_value`, `verdict`, `first_fail_gate`, `data_tier`.
Created migration: `v56_schema_expansion.sql`.
Result: Database flushes safely even with long log strings.

### Phase 55 — Gate Precision Hardening (6 Anomalies)
Date: 10 Mar 2026
G5 Gate B day-high tolerance 0.05% → 0.3% (config-driven).
G5 Gate C volume fade lookback 5 → 15 candles (captures climax).
G6 RSI divergence window 10 → 25 candles (noise filter).
G9 raw candle Lower High → proper pivot swing high detection.
G11 unreachable above-high elif removed (dead code).
G7 get_time_filter() timezone-naive → IST-explicit.
Result: 81/81 tests green.

### Phase 54 — G1 Kill Backdoor & G4 VWAP Slope Recalibration
Date: 10 Mar 2026
G1 Kill Backdoor replaced hardcoded 0.5% with ATR-relative formula: max(1.0%, 0.3 × ATR%).
Dead 2.5%/3.5% distance block removed (was unreachable).
G4 VWAP slope threshold recalibrated from 0.5 bp/min → 3.0 bp/min.
G2.3 gap-up filter removed from scanner (both WS and REST paths).
ATR calculation moved before G1 in both check_setup() paths.
Result: 81/81 tests green.

### Phase 53 — WS Cache Data Erasure Bug Fix
Date: 09 Mar 2026
Fixed WebSocket cache data erasure by merging incoming delta ticks with existing cached fields. Preserved critical legacy fields like prev_close and calculated ch_oc dynamically when missing in delta ticks.
Result: 81/81 tests green.

### Log Analysis — 2026-03-07
Date: 07 Mar 2026
Checked session log for 2026-03-07. No entries found (likely weekend). 
Appended findings to `.swarm/log-findings.md` and returned NOMINAL status.

### Phase 52 — Partial Exit Engine + Human Intervention Safety
Date: 07 Mar 2026
40/40/20 TP split. close_partial_position() with G13 isolation.
modify_sl_qty() for broker SL sync. cancel-first safe_exit().
TP1→breakeven. TP2→TP1 lock. TP3 ATR trail.
Async dispatch corrected. G13 exactly 3 call sites.
Result: 81/81 tests green.

### Phase 51 — Gate Hardening G1–G13
Date: 07 Mar 2026
26 targeted fixes. 45-candle RVOL minimum, 9% scanner floor,
time gate, session-permanent circuit blacklist, G5 all-day high check,
G6 tiered scoring, G9 HTF rebuilt, two-tick entry confirmation,
dynamic timeouts, ATR-based SL/TP.
Result: 81/81 tests green.

### Phase 44 — Async Order Manager + WebSocket Fill Detection
[No summary]

### Phase 43 — Cooldown Signals
[No summary]

### Phase 42 — Position Safety + Capital Management
[No summary]

### Phase 41 — Multi-Edge Detection + Intelligent Exits + Session Management
[No summary]

### Phase 37 — Validation Gate
[No summary]

## Test Baseline
81 passed, 1 intentional skip (test_candle.py — live Fyers required).
This is the floor. No commit goes below this.
