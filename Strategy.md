# ShortCircuit Strategy Manual ⚡

> **Last Updated:** Feb 28, 2026 | **Style:** Institutional Exhaustion Scalping
> **Status:** Production — Phase 44.8.1 | 12-Gate Pipeline | WS-First Scanner

***

## 1. The Core Idea — Why This Works

Every day on NSE, hundreds of small and mid-cap stocks explode upward. A result drops. An operator pushes a stock. Retail traders pile in chasing the move. FOMO buys from people who woke up and saw a stock up 9% on their watchlist. The stock rips from ₹100 to ₹112 in two hours.

Then it stops.

Not because sellers appeared. Because **buyers ran out.**

The last buyer bought at ₹112. There is nobody left to push it higher. Volume starts dying on every new tick up. The stock makes a new high at ₹112.50 — but only 40% of the volume that drove the previous high. Price is in no-man's land, far above where the day's actual trading happened (the Value Area). There is no structural support up here. It's floating.

This is the moment ShortCircuit was built to find and trade.

> *The edge is not predicting direction. The edge is recognising when a move has already exhausted itself — and the market hasn't priced that in yet.*

Mean reversion is not a theory. It is a mechanical reality. When a stock stretches too far from where the majority of today's volume traded, it will return. Not always. But often enough, fast enough, that a disciplined system trading it repeatedly will win.

***

## 2. The Hunt — What the Scanner Does

ShortCircuit monitors all **2418 NSE EQ symbols** simultaneously, every 60 seconds.

Since Phase 44.7, this happens at near-zero latency. The Fyers WebSocket streams live ticks for all 2418 symbols into an in-memory cache. When the scanner runs, it reads that cache — a sub-10ms operation — instead of making 50 separate REST API calls. REST is the fallback only if the cache is stale.

The pre-filter is brutal by design:

```
Gain today:   ≥ 7.5% from today's open
Volume:       > 100,000 shares traded
LTP:          > ₹50 (eliminates penny stocks)
```

Out of 2418 symbols, maybe 15-40 pass on a normal day. These are the candidates. Everything else is noise. The scanner hands these candidates to the analyzer — the real judge.

***

## 3. The Edge — What We're Actually Looking For

Before Phase 44.8, the system required a specific candle shape (Shooting Star, Bearish Engulfing etc.) to fire. This was wrong. A candle shape is a **visual symptom** of exhaustion. It is not the exhaustion itself.

The actual edge has four components. All four must be true simultaneously:

### Component 1 — The Sweet Spot (7.5–14.5% stretched)

Below 7.5%: the move hasn't stretched far enough. Mean reversion distance to VWAP is small. Risk/reward is poor.

Above 14.5%: the stock is approaching circuit territory. Liquidity dries up. Unpredictable.

Between 7.5–14.5% from today's open: the stock has moved enough that retail longs are underwater if it reverses, but not so much that circuits make execution dangerous. This is the zone.

### Component 2 — New High on Dying Volume

The stock must be making a **new intraday high** on the signal candle. Not bouncing, not consolidating — new high. This is important because it means buyers are still trying. But the critical tell is what's happening to volume while they try.

```
vol_fade_ratio = signal_candle_volume / avg_volume_of_prior_5_candles
```

If `vol_fade_ratio < 0.65` — volume has faded more than 35% on the new high — buyers are losing conviction. The rally is running on fumes. Less than 0.30 is extreme exhaustion. Less than 0.50 is high conviction. 0.50–0.65 is medium. All three fire.

This is the single most important number in the system. Your friend who trades this manually is looking at this exact thing — he just calls it "volume dying on the high" rather than computing a ratio.

### Component 3 — Price Above Value Area High (VAH)

The Market Profile tells you where the majority of today's volume actually traded — the Value Area. The Value Area High (VAH) is the upper boundary of accepted value.

Price above VAH = price is in **unaccepted territory**. Nobody has been trading here. There is no structural reason for price to stay here. Gravity pulls it back toward where value was established — the POC (Point of Control), which is typically 3-6% below current price by the time Gate 5 fires.

### Component 4 — Pattern Bonus (optional upgrade)

If the signal candle also happens to be a Shooting Star, Bearish Engulfing, or Evening Star — that's a bonus. It upgrades the confidence tier from MEDIUM → HIGH or HIGH → EXTREME. But it is **not required**. The exhaustion can exist without a textbook candle shape.

This is the philosophical shift of Phase 44.8: **we trade the physics, not the picture.**

***

## 4. The 12 Gates — The Full Validation Flow

Every candidate that passes the scanner enters the 12-gate pipeline. Think of it as 12 independent questions. Every single one must be answered correctly, or the trade doesn't happen.

### Gate 1 — Signal Manager
*"Are we allowed to trade right now?"*

Unlimited signals per day. 45-minute per-symbol cooldown. A cumulative session loss of ₹500 (Phase 69) triggers a full session pause. This gate exists entirely for capital preservation — not signal quality.

### Gate 2 — Market Regime
*"Is the macro environment hostile?"*

NIFTY is the tide. Shorting individual stocks when NIFTY is in TREND_UP mode (> 1.5% from high) is fighting the tide. Gate 2 fetches NIFTY intraday data, computes the morning range, and classifies the regime. TREND_UP = all shorts blocked. RANGE or TREND_DOWN = proceed.

Exception: if a setup is so strong (Evening Star, Bearish Engulfing confirmed) that the stock is clearly distributing against the market, the regime gate can be bypassed. The market does not move all 2418 stocks identically.

### Gate 3 — Data Quality
*"Do we have enough history to make a decision?"*

At least 45 candles required (15 during Climax Window). RVOL validity gate — ensures sufficient market activity before any RVOL reading is trusted. Fewer candles = unreliable averages = false signals.

### Gate 4 — Technical Context
*"Set up the calculations."*

VWAP is computed (cumulative `(TP × Volume) / Volume`). Day high, today's open, current gain% are established. This gate never fails — it's the setup for everything downstream.

### Gate 5 — Exhaustion at Stretch *(The Core Edge)*
*"Is this stock genuinely exhausted at the top?"*

This is the heart of the system. Four sub-checks — all must pass:

```
A. gain_pct between 7.5% and 14.5%          (the sweet spot)
B. New intraday high in last 10 candles     (still pushing up)
C. vol_fade_ratio < 0.65                    (buyers running out)
D. close > VAH                              (in unaccepted territory)
E. AMT Rejection Required (if gain < 9.0%)
```

Outputs: `fired=True/False`, `confidence=EXTREME/HIGH/MEDIUM`, `vol_fade_ratio`, `stretch_score`, `pattern_bonus`.

Gate 5 runs **at the top of the stretch** — not after the stock has already started breaking down. Finding exhaustion at the high and waiting for the first breakdown is the correct sequence. Phase 44.8.1 fixed a critical timing bug where Gate 5 was accidentally running after the first breakdown had already occurred on the candle, creating a double-breakdown requirement and late entries.

### Gate 6 — Circuit Guard
*"Are we about to walk into a trap?"*

Fetches Level 2 depth data. If LTP is within 1.5% of the Upper Circuit limit, the trade is blocked — circuit locks are instant and irreversible. Same for Lower Circuit. This gate has saved accounts.

Also: if the stock has a futures contract, Gate 6 fetches futures OI direction as enrichment data. Falling OI = short covering rally = stronger exhaustion signal. Rising OI = new longs entering = flag for caution. This never blocks the signal — it's information that shows up in the Telegram alert and `signals.csv` for analysis.

### Gate 7 — Momentum Safeguard (Train Filter)
*"Is this a freight train we're standing in front of?"*

```
RVOL > 5.0 AND VWAP slope > 3.0 → BLOCKED
```

If volume is 5× average AND momentum is steep AND still accelerating, the move is not exhausted — it's in full force. Shorting this is not mean reversion. It's getting run over. 

**Structural Fallback (Phase 60)**: If gain > 10% and slope starts slowing down (`Slope Now < Slope Prev`), it's marked as **Momentum Decay** and allowed to bypass this gate.

### Gate 8 — Pro Confluence
*"Do multiple independent indicators agree?"*

Nine confirmation checks run in parallel:

- **Profile Rejection** — POC is far below current price (value not migrating)
- **DOM Wall** — Sell side in Level 2 depth is 2.5× buy side
- **VWAP Flat** — Momentum is dying (slope < 5)
- **RSI Divergence** — Price making higher highs, RSI making lower highs
- **Fibonacci Rejection** — Price at 38.2%, 50%, or 61.8% retracement level
- **RVOL Spike** — Institutional volume signature (> 2.0×)
- **Vacuum/Exhaustion** — Low volume at extension (nobody buying up here)
- **OI Divergence** — Cash equity OI falling while price rises (fakeout)
- **dPOC Divergence** — Developing POC stuck low, price floating above it

Plus orderflow checks from the tape:
- **Round Number** — Price at ₹100, ₹200, ₹500 etc. (liquidity magnet)
- **Large Wick** — Previous candle's wick suggests partial fill zone
- **Bad High** — Heavy sell-side DOM at day high (supply wall)
- **Bad Low** — Heavy buy-side DOM at day low → **BLOCKS trade** (structural support exists, reversal may fail)
- **Trapped Positions** — Failed breakout with buyers stuck above entry
- **Aggression Without Progress** — Large buy orders not moving price (absorption)

Logic: if the stock is already > 2 standard deviations above VWAP (clearly extended), one confluence check is enough. If it's less extended, at least one confirmation is required. A setup with zero confluence gets refused.

### Gate 9 — HTF Confluence
*"Does the 15-minute chart agree?"*

The 1-minute chart shows the setup. The 15-minute chart shows the context. Gate 9 fetches 15m candle history and checks for trend exhaustion. 

**Alpha Strike Bypass (Phase 61)**: If a stock is extremely stretched (> 3.0 SD from VWAP), it bypasses G9 entirely. We assume the institutional climax is so powerful that mean reversion is imminent regardless of HTF trend.

### Gate 10 — WebSocket Price Trigger
*"Has the actual breakdown started?"*

This gate does not run in the analyzer. It runs in `focusengine.py`, watching a real-time WebSocket tick feed. After Gates 1-9 pass, the signal is registered with `signal_low = previous_candle_low`. The focus engine then monitors live LTP every 2 seconds. The moment `LTP < signal_low`, the signal is validated and execution begins. 15-minute timeout — if the breakdown doesn't happen in 15 minutes, the setup is cancelled.

### Gate 11 — Capital Check
*"Can we actually afford this trade?"*

₹1,800 base capital × 5× leverage = ₹9,000 buying power. Gate 11 checks if sufficient capital is available, verifies no duplicate position in this symbol, and calculates quantity. If capital is insufficient, the signal is logged as SKIPPED (not lost — it shows up in EOD analysis).

### Gate 12 — Order Execution
*"Execute with precision."*

SELL order placed at market. SL-M (Stop Loss Market) order placed at `setup_high + max(ATR × 0.5, ₹0.25)`. If SL order fails, 3 retries. If all retries fail, immediate emergency market close — never hold a position without a stop loss.

40+ ML features logged at signal time for future model training.

***

## 5. After Entry — How the Trade Is Managed

Once in a position, the focus engine runs a 2-second monitoring loop:

### Stop Loss
Structural: `setup_high + ATR buffer`. This is above where the setup candle topped — if price reclaims that level, the thesis is wrong. Exit.

### Take Profit Levels
```
TP1 = entry - (ATR × 1.5)   → Exit 40% of position
TP2 = entry - (ATR × 2.5)   → Exit 40% more  
TP3 = entry - (ATR × 3.5)   → Exit remainder (20%)
Ultimate target = VWAP (mean reversion complete)
```

Phase 44.9 will anchor TP2 to the prior session's nPoC (naked Point of Control) — a price level where nobody traded, which acts as a magnet for price to visit.

### Dynamic SL States
```
INITIAL    → Setup high + ATR buffer
BREAKEVEN  → Moved to entry price once 1× risk in profit
TRAILING   → Trails price by 0.2 ATR after TP1 hit
TIGHTENING → Tight trail as TP3 approaches
```

### Soft Stops
The Discretionary Engine evaluates market regime and orderflow continuously. If the macro environment reverses (NIFTY flips bullish mid-trade) or orderflow deteriorates (DOM shows heavy buying entering), a soft stop fires before the hard SL is hit.

***

## 6. The Confidence Tier System

Every signal now carries a confidence tier based on `vol_fade_ratio`:

| Tier | Vol Fade Ratio | What It Means |
|---|---|---|
| `EXTREME` | < 0.30 | Volume collapsed > 70% on the new high. Almost nobody buying. |
| `HIGH` | 0.30–0.50 | Volume faded 50–70%. Buyers clearly losing conviction. |
| `MEDIUM` | 0.50–0.65 | Volume faded 35–50%. Setup valid, less dramatic. |

Pattern bonus upgrades one tier. A MEDIUM setup with a Shooting Star becomes HIGH. A HIGH setup with a Bearish Engulfing becomes EXTREME.

The Telegram alert shows this tier immediately, before the operator presses GO or SKIP. EXTREME signals get priority when multiple signals are queued.

***

## 7. The Telegram Interface — The Operator's Cockpit

The operator never touches a web UI. Everything flows through one Telegram chat:

```
📉 SHORT SIGNAL — NSE:IDEACELLU-EQ
💰 LTP: ₹13.45 | SL: ₹14.12
📊 Edge: EXTREME | Vol Fade: 28%
🕯 Pattern Bonus: ShootingStar
📈 Futures OI: ➖ unknown
✅ Profile Rejection + Bad High + VWAP 2.8SD [EXT]
🎯 HTF: 15m Lower High confirmed

[GO] [SKIP]
```

Commands available:
- `/auto on` — enables fully automated execution (no GO required)
- `/status` — capital, positions, signals remaining today
- `/positions` — live P&L on open trades
- `/why SYMBOL HH:MM` — re-runs all 12 gates diagnostically, explains exactly what failed
- `/skip SYMBOL` — cancels a pending signal without executing

***

## 8. The Signals Log — Your Learning Engine

Every signal that fires — executed or skipped — is written to `logs/signals.csv`. After Phase 44.8, five new fields are captured per signal:

| Field | What It Tells You |
|---|---|
| `stretch_score` | How far into the 9-14.5% band the stock was |
| `vol_fade_ratio` | Exact volume collapse ratio on the signal candle |
| `confidence` | EXTREME / HIGH / MEDIUM |
| `pattern_bonus` | Which candle pattern was present, if any |
| `oi_direction` | Futures OI falling/rising/unknown at signal time |

After 2 weeks of live data, this file answers questions like:
- *"Do EXTREME confidence signals win more than HIGH?"*
- *"What vol_fade_ratio threshold produces the highest win rate?"*
- *"Does pattern_bonus actually improve outcomes or is it noise?"*
- *"When OI was falling, did win rate improve?"*

This is how you tune the system — not by guessing, but by reading what your own bot's data is telling you.

***

## 9. What This System Is Not

It is not a prediction engine. It does not know where the stock is going.

It is a **probability stacker**. Every gate is an independent filter that raises the probability that this specific setup, at this specific moment, in this specific market context, will mean-revert within the next 15-30 minutes.

No single gate is reliable alone. Gates 1-9 together are significantly reliable. Add live WebSocket confirmation (Gate 10), HTF agreement (Gate 9), and structural stop placement — and you have a system that is difficult to blow up and has a positive expected value when run with discipline over hundreds of trades.

The edge is real. Two years of backtesting. A live trader running the same logic manually and making consistent profit every month. Phase 44.8 aligned the bot's code to exactly what the manual trader does — find volume exhaustion at the stretch, wait for the first crack, enter.

The bot's advantage over the human: it watches all 2418 stocks simultaneously, executes in 500ms, has no emotion, never misses an entry because it was on the phone, and logs every decision for systematic improvement.
