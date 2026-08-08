# Strategy: BackToVWAPShort

What each filter measures, and why that measurement.

**Scope.** This describes the *mechanism* of each filter, not its tuning. Gate
thresholds live in `config.py` and are deliberately not reproduced here, and no
statistic characterising how the filter behaves or performs is published. See
[DISCLOSURE.md](DISCLOSURE.md).

The strategy is not the point of this repository. It is documented because the
engineering around it only makes sense once you know what it is protecting.

---

## The thesis

The system trades intraday momentum exhaustion on NSE equities. The premise is
structural rather than predictive:

1. **A strong intraday gain creates unstable positioning.** Participants who
   bought late are holding inventory acquired above the day's volume-weighted
   average, with progressively less room beneath them.
2. **Extension alone is not tradable.** Price can stay stretched for hours.
   Extension becomes actionable only when it coincides with *rejection* — the
   market declining to accept higher prices.
3. **Rejection alone is not an entry.** A stock can be rejected at value and
   grind sideways. The trade requires structural confirmation: price breaking
   the level that defined the setup.

Every gate below exists to test one of those three conditions.

---

## Where the setup is measured from

### VWAP as the reference price

VWAP — the volume-weighted average price since the open — is the anchor. It is
the price at which the average rupee traded today, which makes it the natural
reference for "expensive relative to what participants actually paid."

```
VWAP = Σ(typical_price × volume) / Σ(volume)     where typical = (H + L + C) / 3
```

Computed cumulatively from the session open, not on a rolling window: the
question is where price sits relative to the *whole day's* activity.

### Stretch, measured in standard deviations

Absolute distance from VWAP is not comparable across instruments — ₹5 means
something different on a ₹100 stock and a ₹3,000 stock. The system normalises by
the dispersion of the price-to-VWAP spread itself:

```
σ  = stdev(close − VWAP)  over the last 20 bars
SD = (close_now − VWAP_now) / σ
```

This asks *"how unusual is today's current stretch, relative to how stretched
this instrument has been all session?"* A stock that habitually oscillates ±2%
around VWAP needs a much larger absolute move to register than one that tracks it
tightly.

`STRATEGY_VWAP_SD_FLOOR` is the entry threshold. Above it, two further bands
(`_HIGH`, `_EXTREME`) tag confidence — **informational only, never influencing a
gate decision.**

Guard: when σ is zero (a completely flat series) the function returns 0.0 rather
than dividing by zero, so a dead instrument cannot produce an infinite stretch.

---

## Market profile: where "value" is

The system uses the **Dalton value area** to decide whether price is above or
below where the day's business was actually done.

Algorithm:

1. Segment the day's price range into 20 horizontal bins.
2. Sum traded volume in each bin.
3. The heaviest bin is the **Point of Control (POC)** — the price with the most
   agreement.
4. Expand outward from the POC, taking the heaviest remaining bin each step,
   until a configured share of total volume is enclosed.
5. The bounds of that region are the **Value Area High (VAH)** and **Value Area
   Low (VAL)**.

The invariant `VAH ≥ POC ≥ VAL` holds by construction and is asserted as a
property test over generated series.

**Why VAH matters.** Trading above VAH means trading above the range that
contained the bulk of the day's volume, a region the market has not agreed is
fair. Price there is either discovering a new range or is about to be rejected back
into the old one. The strategy is interested exclusively in the second case.

**Look Above and Fail.** The specific pattern sought: price probes above VAH
within the last three bars, then closes back inside it. That sequence traps
buyers who bought the breakout and is the cleanest available evidence of a failed
upside auction.

---

## The six hard gates

All must pass. No gate can be bypassed because another looks strong — a rule
that exists because an earlier revision allowed a volume climax to override the
volume-fade gate, which defeats the purpose of having the gate.

### C1 — VWAP stretch

`vwap_sd >= STRATEGY_VWAP_SD_FLOOR`

Is the stock materially extended above its own volume-weighted average? Rejects
anything not statistically unusual for that instrument today.

### C2 — Value location

Price above VAH, **or** a confirmed profile rejection.

Is it trading outside the day's agreed value? The disjunction matters: price
that has already fallen back inside value still qualifies *if* the rejection
itself was confirmed, because the failed auction has then already happened.

Fails closed when the profile cannot be computed — no profile, no trade.

### C3 — Failed auction

Two forms of evidence are accepted, and only two:

1. **Profile rejection** — value-back-in, from the profile analyser.
2. **VAH Look-Above-And-Fail** — probed above VAH in the last 3 bars, closed back
   below it.

Explicitly *not* accepted: narrowing highs near the day high. Lower highs are
consistent with mere consolidation and are not evidence that an auction failed.

### C4 — Divergence

Swing-based bearish RSI divergence, **or** a price lower-high.

Divergence is a relationship between *comparable swings*, not two arbitrary
endpoints. A swing high is a bar whose high exceeds both neighbours, with a
minimum three-bar separation to suppress noise. The gate compares the last two
such swings: price making a higher (or equal) high while RSI makes a lower high.

The endpoint-comparison approach used earlier produced divergence signals from
any two conveniently-spaced bars.

### C5 — Volume fade

`vol_fade_ratio <= STRATEGY_VOL_FADE_MAX_RATIO`

Is participation declining as price holds up? Computed as the mean volume of the
last two **completed** bars against a prior baseline window.

Two details that matter:

- **The forming bar is dropped.** A partially-formed bar has partial volume,
  which fakes a fade early in its life.
- **The windows are disjoint.** An earlier version double-counted a bar in both
  the current and prior windows, biasing every ratio toward 1.0.

Volume fading while price holds means the move is being maintained by fewer
participants — the definition of thinning demand.

### C6 — Momentum decay

Fast VWAP slope must fall genuinely behind slow slope:

```
if slope_slow > 0:   decaying = slope_fast < slope_slow × DECAY_RATIO
else:                decaying = slope_fast <= slope_slow
```

The branch is not cosmetic. `fast < slow × ratio` inverts its meaning when
`slow` is negative: the comparison then requires a steeper decline rather than
detecting deceleration. When the
premise (a real up-slope) is absent, the gate accepts continued roll-over only.

---

## Post-strategy gates

### G7 — Market regime

Time windows (nothing before 09:30, nothing after 15:10) and Nifty trend. Shorts
into a strong index uptrend are blocked. Fails closed when index data is
unavailable or stale: a false block costs a bounded number of missed trades, a
false pass during a hard trend does not have a bounded cost.

### G9 — Higher-timeframe confluence

15-minute momentum physics. Compares the last two 15-minute moves:

- **Acceleration guard** — if the current 15m move exceeds the reject threshold,
  block. Fading a still-accelerating move is the most expensive mistake available.
- **Stall pass** — if the current move is below the stall threshold, momentum has
  paused at the highs and reversion is plausible.
- **Alpha strike bypass** — above a configured stretch, G9 is skipped on the
  reasoning that sufficient extension dominates higher-timeframe context.

Fails closed on missing or malformed data.

### G8 — Signal manager

Per-symbol cooldown, session loss limit, and a daily target gate above which only
the highest confidence tier is accepted.

### G12 — Trigger

The entry itself. Price must break the signal low. Invalidation sits at
`signal_high × 1.002`; the pending signal expires after 15 minutes.

This is the "prove it" step: everything before it establishes that a setup
*exists*, and this establishes that the market has begun to act on it.

---

## Risk construction

**Stop.** ATR-based, placed above the setup high for a short:

```
buffer = max(ATR × SL_ATR_MULTIPLIER, tick_size × SL_MIN_TICK_BUFFER)
stop   = signal_high + buffer
```

Validated against the **actual fill**, not the signal price. If slippage carries
the fill past the structural level the stop is re-anchored, because a short's
stop below its entry is marketable on arrival ([D-006](DISCOVERIES.md)).

ATR returns `NaN` rather than a sentinel on insufficient data — the value feeds
stop sizing, and a silent default of 1.0 would produce a plausible-looking but
arbitrary stop.

**Exit.** The stop, or the 15:10 square-off. No take-profit
([ADR-009](DECISIONS.md)).

**Sizing.** From live broker margin at the leverage the broker actually grants
for that symbol, with a 2% safety buffer and a 5x→4x fallback on margin
rejection.

---

## Deliberately absent

- **No prediction.** Nothing forecasts direction or magnitude. Every gate
  measures a present condition.
- **No adaptive thresholds.** Thresholds do not relax based on recent outcomes.
  A gate that loosens after losses is a gate that fails when it matters most.
- **No confidence-weighted sizing.** Confidence tiers are recorded for later
  analysis and never influence position size or gate decisions.
- **No averaging down.** One entry, one stop.
