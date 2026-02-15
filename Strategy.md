# ShortCircuit Strategy Manual ⚡

> **Last Updated:** Feb 15, 2026 | **Style:** Institutional Reversal Scalping  
> **Status:** Production — 12-Gate Pipeline + Multi-Edge + Capital Management + Diagnostics

***

## 1. Core Philosophy: "The Sniper"

We trade high-conviction reversals at extended levels. Short stocks that have run too far, too fast — then catch the mean reversion back to VWAP.

- **Goal:** 1-2 surgical trades per day (shorting overextended stocks at day highs)
- **Edge:** 12-gate validation + 5 institutional pattern detectors + orderflow confirmation
- **Safety:** Capital preservation > signal count. Always.

***

## 2. The 12-Gate Signal Pipeline

Every signal must pass **12 sequential gates**. Failure at any gate = NO TRADE.

```
┌─────────────────────────────────────────────────────────────────┐
│  GATE 1: Signal Manager                                         │
│  ├─ Max 5 signals/day (prevents overtrading)                    │
│  ├─ 45-min per-symbol cooldown (prevents revenge trading)       │
│  └─ 3-loss pause (circuit breaker for bad sessions)             │
├─────────────────────────────────────────────────────────────────┤
│  GATE 2: Market Regime                                          │
│  ├─ Fetches Nifty 50 intraday data                              │
│  ├─ Calculates morning range (9:15–10:15)                       │
│  ├─ If Nifty TREND_UP → BLOCK all shorts                       │
│  └─ RANGE or TREND_DOWN → proceed                               │
├─────────────────────────────────────────────────────────────────┤
│  GATE 3: Data Pipeline                                          │
│  ├─ Fetches 1-min candle history via Fyers API                  │
│  ├─ Requires ≥10 candles for meaningful analysis                │
│  └─ Validates data quality (no gaps)                            │
├─────────────────────────────────────────────────────────────────┤
│  GATE 4: Technical Context                                      │
│  ├─ Enriches DataFrame with VWAP (cumulative TP × Vol / Vol)    │
│  ├─ Calculates day high, open, gain %                           │
│  └─ Prepares prev_df for structural analysis                    │
├─────────────────────────────────────────────────────────────────┤
│  GATE 5: Hard Constraints ("The Ethos Check")                   │
│  ├─ Trend Strength:                                             │
│  │   ├─ Current gain ≥ 5% OR max day gain ≥ 7%                 │
│  │   └─ Max gain > 15% → BLOCK (circuit risk)                  │
│  ├─ Day High Proximity:                                         │
│  │   ├─ Base: Within 4% of day high                             │
│  │   └─ Turbo: Within 6% if max gain > 10% (deep pullback OK)  │
│  └─ Purpose: Only trade stocks with strong trend + near highs   │
├─────────────────────────────────────────────────────────────────┤
│  GATE 6: Circuit Guard                                          │
│  ├─ Fetches Level 2 depth data (upper/lower circuits)           │
│  ├─ If LTP ≥ UC × 0.985 → BLOCK (within 1.5% of lock)         │
│  ├─ If LTP ≤ LC × 1.005 → BLOCK (at lower circuit)            │
│  └─ Shares depth data with Gate 10 (DOM analysis)               │
├─────────────────────────────────────────────────────────────────┤
│  GATE 7: Momentum Safeguard (Train Filter)                      │
│  ├─ Calculates VWAP slope over last 30 candles                  │
│  ├─ Calculates RVOL (current vol / 20-candle avg)               │
│  ├─ If RVOL > 5.0 AND slope > 40 → BLOCK                      │
│  └─ Purpose: Don't short a freight train                        │
├─────────────────────────────────────────────────────────────────┤
│  GATE 8: Pattern Recognition                                    │
│  ├─ Standard Path (check_setup):                                │
│  │   ├─ SHOOTING_STAR: Long upper wick (>2× body), bearish     │
│  │   ├─ BEARISH_ENGULFING: Red candle engulfs previous green    │
│  │   ├─ EVENING_STAR: Green → Doji → Red (3-candle reversal)   │
│  │   ├─ MOMENTUM_BREAKDOWN: Strong selling into support         │
│  │   ├─ VOLUME_TRAP: High volume without price progress         │
│  │   ├─ ABSORPTION_DOJI: Tiny body + high vol (sniper zone)    │
│  │   └─ TAPESTALL: Price stalls at highs (needs VWAP ext >2SD) │
│  ├─ Multi-Edge Path (check_setup_with_edges):                   │
│  │   ├─ 5 parallel detectors (Absorption, Bad High, Trapped,   │
│  │   │   Failed Auction, Classic Patterns)                      │
│  │   ├─ Weighted confidence scoring                             │
│  │   └─ Skips Gate 8, uses edge_payload directly                │
│  └─ Requires price BREAKDOWN below setup candle low             │
├─────────────────────────────────────────────────────────────────┤
│  GATE 9: Breakdown Confirmation                                 │
│  ├─ Current LTP must be BELOW previous candle's low             │
│  ├─ Not just a pattern — price must confirm the reversal        │
│  └─ Gap = setup_low - current_ltp (must be positive)            │
├─────────────────────────────────────────────────────────────────┤
│  GATE 10: Pro Confluence (9 Confirmation Checks)                │
│  ├─ Profile Rejection (POC/VAH via Market Profile)              │
│  ├─ DOM Wall (Sell/Buy ratio > 2.5× via Level 2 depth)         │
│  ├─ VWAP Flat (slope < 5 = momentum exhaustion)                │
│  ├─ RSI Divergence (price up, RSI down)                         │
│  ├─ Fibonacci Rejection (38.2%, 50%, 61.8% levels)              │
│  ├─ RVOL Spike (> 2.0× = institutional activity)               │
│  ├─ Vacuum/Exhaustion (low vol at extension)                    │
│  ├─ OI Divergence (price UP + OI DOWN = fakeout)                │
│  ├─ dPOC Divergence (value not migrating with price)            │
│  ├─ Orderflow Checks:                                           │
│  │   ├─ Round Number proximity (+Conf)                          │
│  │   ├─ Large Wick > 60% (+Conf)                               │
│  │   ├─ Bad High: heavy sellers at day high (+Conf)             │
│  │   ├─ Bad Low: heavy buyers at low → **BLOCKS trade**        │
│  │   ├─ Trapped Positions: failed breakout (+Conf)              │
│  │   └─ Aggression without Progress (+Conf)                     │
│  └─ Logic: If NOT extended (VWAP < 2SD), must have ≥1 confirm  │
├─────────────────────────────────────────────────────────────────┤
│  GATE 11: HTF Confluence (15-Minute Check)                      │
│  ├─ Checks 15-min chart for trend exhaustion                    │
│  ├─ Requires: Lower highs OR exhaustion on HTF                  │
│  ├─ If 15m is strongly bullish → BLOCK                         │
│  └─ Also checks key support/resistance levels (1% tolerance)    │
├─────────────────────────────────────────────────────────────────┤
│  GATE 12: Signal Finalization                                   │
│  ├─ Calculates ATR (14-period)                                  │
│  ├─ Stop Loss = setup_high + max(ATR × 0.5, 0.25)              │
│  ├─ Logs 40+ features for ML training                           │
│  ├─ Records signal in Signal Manager                            │
│  └─ Returns complete signal dict for execution                  │
└─────────────────────────────────────────────────────────────────┘
                             ↓
┌─────────────────────────────────────────────────────────────────┐
│  CAPITAL MANAGER (Phase 42.1)                                   │
│  ├─ Checks affordability (₹1,800 base × 5× leverage)           │
│  ├─ Verifies no duplicate positions                             │
│  ├─ If BLOCKED: logs signal as SKIPPED (not lost)               │
│  └─ If OK: proceeds to order execution                          │
├─────────────────────────────────────────────────────────────────┤
│  TRADE MANAGER                                                  │
│  ├─ Position verification (4-step safety check)                 │
│  ├─ SELL order + SL-M order (with 3× retry)                    │
│  ├─ If SL fails 3× → Emergency Market Close                    │
│  └─ Activates Focus Engine for live monitoring                  │
└─────────────────────────────────────────────────────────────────┘
```

***

## 3. Gate Summary Table

| Gate | Name | Module | Failure Action |
|------|------|--------|----------------|
| 1 | Signal Manager | `signal_manager.py` | Block (daily cap / cooldown / pause) |
| 2 | Market Regime | `market_context.py` | Block (Nifty TREND_UP) |
| 3 | Data Pipeline | `analyzer.py` | Skip (insufficient data) |
| 4 | Technical Context | `analyzer.py` | Skip (no VWAP) |
| 5 | Hard Constraints | `god_mode_logic.py` | Skip (weak trend / too far from high) |
| 6 | Circuit Guard | `analyzer.py` | Block (too close to UC/LC) |
| 7 | Momentum Safeguard | `analyzer.py` | Block (freight train) |
| 8 | Pattern Recognition | `god_mode_logic.py` | Skip (no valid pattern) |
| 9 | Breakdown Confirmation | `analyzer.py` | Skip (no price confirmation) |
| 10 | Pro Confluence | `analyzer.py` + `tape_reader.py` | Refuse (no confirmation + not extended) |
| 11 | HTF Confluence | `htf_confluence.py` | Block (15m bullish) |
| 12 | Signal Finalization | `analyzer.py` | Pass (ATR/SL calculation) |

***

## 4. Multi-Edge Detection System

When `MULTI_EDGE_ENABLED = True`, five pattern detectors run in parallel:

| Detector | What It Finds | Weight |
|----------|--------------|--------|
| **Absorption** | High volume, no price progress (hidden limit orders) | Varies |
| **Bad High** | Heavy sell DOM at day highs (supply wall) | Varies |
| **Trapped Positions** | Failed breakout with trapped longs | Varies |
| **Failed Auction** | Exhaustion after extended range expansion | Varies |
| **Classic Reversal** | Shooting Star, Engulfing, Evening Star | Varies |

**Confidence scoring:** Weighted sum of edge scores. Single strong edge OR multiple weak edges can qualify. Uses `check_setup_with_edges()` which skips Gate 8 (edges already identified) but runs Gates 1-7 and 9-12.

***

## 5. Capital Management (Phase 42.1)

```
Base Capital:    ₹1,800
Leverage:        5× (NSE intraday standard)
Buying Power:    ₹9,000
Per-Trade Risk:  0.8-1.2%
```

**Pre-order checks:**
1. `can_afford()` — Sufficient capital available?
2. Duplicate check — Already holding this symbol?
3. Quantity check — Can afford ≥1 share?

**All signals logged** — Executed AND skipped. End-of-day analysis shows missed P&L.

***

## 6. Execution & Safety

### Entry Calculation
```
Entry    = Setup Candle Low (breakdown point)
SL       = Setup Candle High + max(ATR × 0.5, 0.25)
Target 1 = Entry - (Risk × 1.5)  [scale out 50%]
Target 2 = VWAP (mean reversion target)
```

### Position Safety (6 Layers)
1. **Pre-order verification** — Capital + position state + direction check
2. **3× retry logic** — If SL order fails, retry 3 times
3. **Emergency exit** — If all retries fail, market-close immediately
4. **Broker sync** — Real-time position polling every 2 seconds
5. **Double-verification** — 4-step protocol on stop exits
6. **Orphan detection** — Startup reconciliation + 30-min audits

### Dynamic Trailing Stops
```
Phase 1: Initial SL (setup_high + ATR buffer)
Phase 2: Breakeven (after 1× risk profit — trade is risk-free)
Phase 3: Trailing (after 2× risk profit — locks in gains)
Phase 4: Tightening (as target approaches)
```

***

## 7. Diagnostic Analyzer (Phase 42.2)

When the bot misses a trade you expected it to catch:

```bash
# CLI
python eod_why.py RELIANCE 14:25

# Telegram
/why RELIANCE 14:25
```

**What it does:**
- Re-runs ALL 12 gates (diagnostic mode — never short-circuits)
- Reports detailed pass/fail per gate with raw values
- Provides actionable suggestions ("Increase allowed pullback to 6%")
- Checks what happened 30 min later (profitability simulation)
- Logs results to `logs/diagnostic_analysis.csv` for pattern analysis

***

## 8. Orderflow Principles

| Principle | Implementation | Effect |
|-----------|---------------|--------|
| Too much buying at low = bad low | `detect_bad_low()` | **BLOCKS trade** |
| Too much selling at high = bad high | `detect_bad_high()` | Adds confirmation |
| Large wicks get partially filled | `detect_large_wick()` | Adds confirmation |
| Trapped positions fuel reversals | `detect_trapped_positions()` | Adds confirmation |
| Round numbers attract liquidity | `check_round_number()` | Adds confirmation |
| Aggression without progression | `detect_aggression_no_progress()` | Adds confirmation |

***

## 9. Module Reference

| Module | Role |
|--------|------|
| `main.py` | Entry point, orchestrates scan → analyze → trade loop |
| `scanner.py` | Pre-filters: price > ₹50, volume > 100K, gain 6-18% |
| `analyzer.py` | Gates 1-12: Full signal validation pipeline |
| `god_mode_logic.py` | VWAP slope, constraints, ATR, patterns, Fib, RSI |
| `multi_edge_detector.py` | 5 parallel pattern detectors with confidence scoring |
| `tape_reader.py` | DOM analysis, stall detection, orderflow checks |
| `htf_confluence.py` | 15-minute chart structural alignment |
| `market_profile.py` | POC, VAH/VAL, dPOC calculations |
| `market_context.py` | Nifty regime detection (TREND_UP / RANGE / TREND_DOWN) |
| `signal_manager.py` | Daily caps (5), cooldowns (45 min), loss pause (3) |
| `trade_manager.py` | Order execution, position safety, SL management |
| `focus_engine.py` | Live trade monitoring, dynamic trailing, broker sync |
| `capital_manager.py` | Capital tracking, affordability checks, leverage |
| `telegram_bot.py` | Alerts, dashboard, /status, /why, /auto commands |
| `diagnostic_analyzer.py` | Missed opportunity analysis (12-gate diagnostic) |
| `eod_analysis.py` | End-of-day signal review + skipped signal analysis |
| `ml_logger.py` | 40+ feature extraction for ML training pipeline |

***

## 10. Pattern Quick Reference

| Pattern | Description | Best Context |
|---------|-------------|--------------|
| SHOOTING_STAR | Long upper wick (>2× body), close near low | At day high, VAH |
| BEARISH_ENGULFING | Red candle fully covers previous green | After strong rally |
| EVENING_STAR | Green → Doji → Red sequence | At resistance |
| MOMENTUM_BREAKDOWN | Strong selling pressure breaks support | Extended levels |
| VOLUME_TRAP | High volume with no price progress | At day high |
| ABSORPTION_DOJI | Tiny body, high volume, at range top | Sniper zone only |
| TAPESTALL | Price flat at highs, momentum dies | Requires VWAP > 2SD |

***

## 11. Active Filters

```
✅ Market Regime (Nifty trend)
✅ Signal Cap (5/day max)  
✅ 45-min Cooldown
✅ HTF Confluence (15m)
✅ Circuit Guard (UC proximity)
✅ Orderflow Bad Low Block
✅ OI Divergence
✅ dPOC Divergence
✅ Capital Management (pre-order check)
✅ Train Filter (RVOL × slope)
✅ Multi-Edge Detector (when enabled)
✅ Diagnostic Analyzer (/why command)
❌ Time Filter (REMOVED — stocks can hover then move)
```

***

## 12. Risk Parameters

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Max signals/day | 5 | Prevents overtrading |
| Cooldown | 45 min | Prevents revenge trading |
| Loss pause | 3 consecutive | Circuit breaker |
| Capital per trade | ₹1,800 | Fixed base amount |
| Leverage | 5× | NSE intraday standard |
| Max gain filter | 6-15% | Sweet spot for reversals |
| Max distance from high | 4-6% | Must be near day high |
| Initial SL | Setup high + ATR buffer | Structural stop |
| RVOL train threshold | > 5.0 | Don't short momentum trains |
| VWAP extension threshold | > 2.0 SD | Required for non-extended patterns |
