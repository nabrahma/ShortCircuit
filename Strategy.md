# Phase 30: Operational Strategy Manual
> **Last Updated:** Feb 09, 2026 | **Style:** Classical Trend Following (Sniper)
> **Status:** BATTLE TESTED + ORDERFLOW UPGRADES

## 1. Core Philosophy: "The Sniper"
We trade high-conviction reversals at extended levels.
- **Goal:** Catch 1-2 major moves per day (Shorting extended stocks at Day Highs).
- **Edge:** Multi-gate filtering + Orderflow principles.
- **Safety:** Capital Preservation > Signals.

---

## 2. The Signal Funnel: 8 Gates to a Trade

Every signal must pass **8 sequential gates**. Failure at any gate = NO TRADE.

```
┌─────────────────────────────────────────────────────────────┐
│  SCANNER (main.py → scanner.py)                             │
├─────────────────────────────────────────────────────────────┤
│  GATE 1: Market Cap Filter                                  │
│  ├─ Scans NSE stocks via Fyers API                          │
│  ├─ Filters: Price > ₹50, Volume > 100K                     │
│  └─ Output: ~500 candidates                                 │
├─────────────────────────────────────────────────────────────┤
│  GATE 2: Momentum Screener                                  │
│  ├─ Requires: Gain 6% - 18% from Previous Close             │
│  ├─ Too Low (<6%): Not enough trend                         │
│  ├─ Too High (>18%): Circuit risk                           │
│  └─ Output: ~50-150 candidates                              │
├─────────────────────────────────────────────────────────────┤
│  GATE 3: Microstructure Quality                             │
│  ├─ Fetches 1-min candles                                   │
│  ├─ Counts: Zero-volume + Doji candles                      │
│  ├─ Threshold: If > 50% are "dead", SKIP                    │
│  ├─ On API delay: PASS (fail-open)                          │
│  └─ Output: ~30-100 candidates                              │
└─────────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────┐
│  ANALYZER (analyzer.py)                                     │
├─────────────────────────────────────────────────────────────┤
│  GATE 4: Pre-Filters                                        │
│  ├─ A. Signal Manager: Max 5 trades/day, 45-min cooldown    │
│  ├─ B. Market Regime: Nifty Trend filter                    │
│  │     └─ If Nifty is TRENDING UP → BLOCK all shorts        │
│  └─ Output: Pass/Fail                                       │
├─────────────────────────────────────────────────────────────┤
│  GATE 5: Technical Setup Validation                         │
│  ├─ Requires BREAKDOWN of setup candle low                  │
│  ├─ Pattern Detection:                                      │
│  │   ├─ SHOOTING_STAR: Long upper wick rejection            │
│  │   ├─ BEARISH_ENGULFING: Full body overlap                │
│  │   ├─ EVENING_STAR: 3-candle reversal                     │
│  │   ├─ ABSORPTION_DOJI: Small body at micro-range top      │
│  │   └─ TAPESTALL: Price stall at highs (drift)             │
│  ├─ Extension Check: Must be >2 SD from VWAP                │
│  └─ Output: Valid pattern or SKIP                           │
├─────────────────────────────────────────────────────────────┤
│  GATE 6: Pro Confluence (Confirmation Stack)                │
│  ├─ Profile Rejection (Market Profile POC/VAH)              │
│  ├─ DOM Wall (Sell/Buy ratio > 2.5x)                        │
│  ├─ VWAP Flat (Slope < 5)                                   │
│  ├─ RSI Divergence                                          │
│  ├─ Fibonacci Rejection (38.2%, 50%, 61.8%)                 │
│  ├─ RVOL Spike (> 2.0x average)                             │
│  ├─ Vacuum/Exhaustion (Low vol at extension)                │
│  ├─ OI Divergence (Price UP + OI DOWN = Fakeout)            │
│  └─ dPOC Divergence (Value not migrating)                   │
├─────────────────────────────────────────────────────────────┤
│  GATE 7: Orderflow Checks                                   │
│  ├─ Round Number: Near 100, 500, 1000... → +Conf            │
│  ├─ Large Wick: >60% wick = strong rejection → +Conf        │
│  ├─ Bad High: Heavy sellers at day high → +Conf             │
│  ├─ Trapped Positions: High vol at top + drop → +Conf       │
│  ├─ Absorption: High vol, no progress → +Conf               │
│  ├─ Bad Low: Heavy buyers at day low → **BLOCK TRADE**      │
│  └─ Output: Confirmation list OR blocked                    │
├─────────────────────────────────────────────────────────────┤
│  GATE 8: HTF Confluence (15-Minute Check)                   │
│  ├─ Fetches 15-min chart                                    │
│  ├─ Requires: Lower Highs OR Exhaustion on HTF              │
│  ├─ If 15m is bullish → BLOCK                               │
│  └─ Output: FINAL SIGNAL or SKIP                            │
└─────────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────┐
│  TRADE MANAGER (trade_manager.py)                           │
├─────────────────────────────────────────────────────────────┤
│  GATE 9: Circuit Guard (Anti-Trap)                          │
│  ├─ Reads real-time Upper Circuit limit                     │
│  ├─ If LTP > (UC * 0.985) → **BLOCK** (within 1.5% of lock) │
│  └─ Output: Safe to trade or blocked                        │
├─────────────────────────────────────────────────────────────┤
│  EXECUTION                                                  │
│  ├─ Calculate Entry, SL, Targets                            │
│  ├─ Dynamic Tick Rounding (0.05 or stock-specific)          │
│  ├─ Send to Telegram for Manual/Auto execution              │
│  ├─ If Auto: Place SELL order + SL-M order                  │
│  └─ If SL fails 3x → Emergency Market Close                 │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. Gate Summary Table

| Gate | Name | Module | Type | Failure Action |
|------|------|--------|------|----------------|
| 1 | Market Cap Filter | `scanner.py` | Inclusion | Skip |
| 2 | Momentum Screener | `scanner.py` | Range 6-18% | Skip |
| 3 | Microstructure Quality | `scanner.py` | <50% dead candles | Skip |
| 4 | Pre-Filters | `analyzer.py` | Signal Cap + Regime | Block |
| 5 | Technical Setup | `analyzer.py` | Pattern + Extension | Skip |
| 6 | Pro Confluence | `analyzer.py` | Confirmation stack | Refuse |
| 7 | Orderflow Checks | `tape_reader.py` | Orderflow | Block (Bad Low) |
| 8 | HTF Confluence | `htf_confluence.py` | 15m alignment | Block |
| 9 | Circuit Guard | `trade_manager.py` | UC proximity | Block |

---

## 4. Orderflow Principles Implemented

| Principle | Implemented | Effect |
|-----------|-------------|--------|
| Too much buying at low = bad low | ✅ `detect_bad_low()` | **BLOCKS trade** |
| Too much selling at high = bad high | ✅ `detect_bad_high()` | Adds confirmation |
| Large wicks get partially filled | ✅ `detect_large_wick()` | Adds confirmation |
| Trapped positions fuel reversals | ✅ `detect_trapped_positions()` | Adds confirmation |
| Round numbers attract liquidity | ✅ `check_round_number()` | Adds confirmation |
| Aggression without progression | ✅ `detect_aggression_no_progress()` | Adds confirmation |

---

## 5. Execution & Safety

### A. Entry Calculation
```
Entry = Setup Candle Low (Breakdown point)
Stop Loss = Setup Candle High + (ATR * 0.5)
Target 1 = Entry - (Risk * 1.5)  [Scale out 50%]
Target 2 = VWAP (Mean reversion target)
```

### B. Safety Features
1. **Dynamic Tick Rounding** - Uses stock-specific tick size
2. **3x Retry Logic** - If SL order fails, retry 3 times
3. **Emergency Exit** - If all retries fail, market-close immediately
4. **Focus Mode** - Live tracking with dynamic trailing stops

### C. Position Sizing
```
Risk Per Trade = 1% of Capital
Qty = Risk Amount / (Entry - Stop Loss)
Max Position = 10% of Capital
```

---

## 6. What Each File Does

| File | Role |
|------|------|
| `main.py` | Entry point, orchestrates scan-analyze loop |
| `scanner.py` | Gates 1-3: Finds momentum candidates |
| `analyzer.py` | Gates 4-8: Validates setups with confluence |
| `tape_reader.py` | Gate 7: Orderflow checks |
| `htf_confluence.py` | Gate 8: 15-minute structure check |
| `trade_manager.py` | Gate 9 + Execution: Places orders safely |
| `focus_engine.py` | Live tracking: Dynamic SL/TP management |
| `market_context.py` | Nifty trend detection for regime filter |
| `signal_manager.py` | Signal cap and cooldown tracking |
| `god_mode_logic.py` | VWAP, RSI, Fibonacci calculations |
| `market_profile.py` | POC, VAH, dPOC calculations |
| `telegram_bot.py` | Alerts and manual trade confirmation |
| `ml_logger.py` | ML data collection for training |

---

## 7. Quick Reference: Pattern Definitions

| Pattern | Description | Best Context |
|---------|-------------|--------------|
| SHOOTING_STAR | Long upper wick (>2x body), close near low | At day high, VAH |
| BEARISH_ENGULFING | Red candle fully covers previous green | After strong rally |
| EVENING_STAR | Green → Doji → Red sequence | At resistance |
| ABSORPTION_DOJI | Tiny body, high volume, at range top | At liquidity wall |
| TAPESTALL | Price flat at highs, momentum dies | After parabolic move |

---

## 8. Current Active Filters

```
✅ Market Regime (Nifty trend)
✅ Signal Cap (5/day max)
✅ 45-min Cooldown
✅ HTF Confirmation (15m)
✅ Circuit Guard (UC proximity)
✅ Orderflow Bad Low Block
✅ OI Divergence
✅ dPOC Divergence
❌ Time Filter (REMOVED - stocks can hover then move)
```

---

# Appendix: The Original Microstructure Thesis

[See original Strategy.md for detailed theory on Auction Market Theory, Order Flow, and DOM analysis...]
