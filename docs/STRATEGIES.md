# Writing a strategy

ShortCircuit is an execution engine. It owns everything that touches the broker
or the account: the scan universe, market data, risk gates, order placement,
stops, take-profit and square-off mechanics, reconciliation and logging. A
**strategy** owns one decision — *is there a setup on this symbol right now, and
how should it be traded?* — and answers it by returning a `Signal` or `None`.

The contract is [`src/shortcircuit/strategy/sdk.py`](../src/shortcircuit/strategy/sdk.py).
A complete working example is
[`strategy/examples/vwap_stretch_fade.py`](../src/shortcircuit/strategy/examples/vwap_stretch_fade.py).

---

## Quick start

1. Create `strategies/my_fade.py` at the repository root:

   ```python
   from shortcircuit.strategy.sdk import Direction, SetupContext, Signal, Strategy

   class MyFade(Strategy):
       name = "my_fade"
       direction = Direction.SHORT

       def evaluate(self, ctx: SetupContext) -> Signal | None:
           if ctx.gain_pct < 10 or ctx.vwap_sd < 3 or not ctx.is_decaying:
               return None
           return Signal(pattern="MY_FADE", stop_reference=ctx.day_high)
   ```

2. Turn it on, either for one run:

   ```bash
   SHORTCIRCUIT_STRATEGIES=my_fade python -m shortcircuit
   ```

   or permanently, in `strategies/__init__.py`:

   ```python
   ACTIVE = ("my_fade",)
   ```

   A long is the same contract with `direction = Direction.LONG`: its
   `stop_reference` is the setup low, and the engine buys the break above its
   trigger. See [Long and short](#long-and-short).

3. Start the bot. The startup log names every active strategy and where it was
   loaded from:

   ```
   [STRATEGY] active, in priority order: my_fade (shortcircuit_plugin_…_my_fade)
   ```

`strategies/` is **git-ignored apart from its README**. Nothing you put there
reaches the repository, so a fork can stay public while its strategies stay
private.

---

## Where plug-ins are found

| Source | Notes |
|---|---|
| `shortcircuit.strategy.examples` | Shipped with the engine. Never active unless named. |
| `strategies/` at the repo root | The default plug-in directory. |
| `SHORTCIRCUIT_STRATEGY_PATH` | `os.pathsep`-separated directories; replaces the default. |

A plug-in directory is either a **package** (it has `__init__.py`; every
submodule and subpackage is scanned) or a **plain folder of `.py` files**. Every
concrete `Strategy` subclass found is registered under its `name`. Modules whose
names start with `_` or `test`, and `conftest.py`, are never imported.

A package layout scales better once a strategy has parameters and tests:

```
strategies/
├── __init__.py          ACTIVE = ("my_fade",)
├── my_fade/
│   ├── __init__.py      the Strategy subclass
│   ├── detector.py      pattern logic
│   └── params.py        thresholds
└── tests/
    └── test_my_fade.py  collected by pytest automatically
```

## Which strategies run

In priority order:

1. `SHORTCIRCUIT_STRATEGIES` (comma-separated), if set;
2. otherwise `ACTIVE` in a plug-in package's `__init__.py`;
3. otherwise **none**: the engine scans and manages any open position but opens
   nothing, and logs a warning saying so.

For each candidate, the active strategies are asked **in order**, and the first
`Signal` wins. A later strategy only sees candidates every earlier one declined,
so adding a strategy can never loosen another's gates.

Loading fails fast, before the first scan, on an unknown name, a name listed
twice, two plug-ins sharing a name, or a strategy the engine cannot execute.
Import errors are included in the message.

---

## Long and short

Every strategy declares the side it trades, and every signal it returns trades
that side. A short and a long can be active at once, and the one capital slot is
shared between them (the first to fill takes it).

```python
from shortcircuit.strategy.sdk import Direction, Signal, Strategy, Universe

class Capitulation(Strategy):
    name = "capitulation"
    direction = Direction.LONG          # buys; the stop sits under stop_reference
    universe = Universe.LOSERS          # the default for a LONG anyway (AUTO)

    def evaluate(self, ctx):
        if ctx.gain_pct > -8 or ctx.vwap_sd > -3:
            return None
        return Signal(pattern="CAPITULATION", stop_reference=ctx.day_low)
```

**The engine mirrors everything for a long.**

| | SHORT | LONG |
|---|---|---|
| Entry order | SELL | BUY |
| `stop_reference` | the setup high | the setup low |
| Hard stop | above it, by `max(0.5 × ATR, 0.25)` | below it, by the same buffer |
| Default trigger | the previous bar's low | the previous bar's high |
| Validation gate | enters on a break **below** the trigger; abandoned if price first pushes 0.2% above `stop_reference` | enters on a break **above** the trigger; abandoned if price first drops 0.2% below `stop_reference` |
| Take-profit ladder | targets below entry, toward VWAP | targets above entry, toward VWAP |
| G9 HTF check | passes stretch above VWAP; blocks a move up that is accelerating | the mirror: stretch below VWAP, blocks an accelerating fall |
| RSI exits | levels as written | read as 100 − RSI, so one set of parameters means the same thing on both sides |
| Trailing stop | above each completed bar's high, only falling | below each completed bar's low, only rising |

`stop_pct` and the trail buffer are percentages of the long's own prices, so a
long mirrors a short in percent, not in rupees.

**Which movers a strategy is offered: `universe`.** The scanner watches the
union of what the active strategies ask for, and offers each candidate only to
the strategies whose universe includes it.

| `Universe` | Offered |
|---|---|
| `AUTO` (default) | gainers for a SHORT, losers for a LONG |
| `GAINERS` | up `SCANNER_GAIN_MIN_PCT`–`SCANNER_GAIN_MAX_PCT` on the day |
| `LOSERS` | down `SCANNER_LOSS_MIN_PCT`–`SCANNER_LOSS_MAX_PCT` |
| `MOVERS` | both |

Losers are only scanned while some active strategy wants them: each candidate
is a history fetch, and fetches share the broker's rate limit with the order
path. A deployment of short strategies scans exactly what it always has.

**The operator's filter: `config.TRADE_DIRECTION`.** `'BOTH'` (the default)
lets each strategy trade its own side; `'SHORT'` or `'LONG'` holds the other
side back. It is switchable from Telegram with `/mode both|long|short`. It only
filters: it never turns a short setup into a buy, and an open position keeps its
own side, stop and exits whatever it is switched to.

---

## What a strategy receives: `SetupContext`

Computed once per candidate by the engine, for every strategy.

| Field | Meaning |
|---|---|
| `symbol`, `ltp`, `now` | The candidate, its last price, evaluation time |
| `df` | Session 1-minute frame, enriched with `vwap` and indicator columns. **The last row is the bar still forming**; use `df.iloc[:-1]` for completed bars |
| `df_15m` | 15-minute frame, or `None` |
| `gain_pct` | % change vs previous close (open if unavailable); negative for a loser |
| `day_high`, `day_low`, `open_price` | Session high, low and open |
| `atr` | ATR of the 1-minute frame |
| `vwap_sd` | Stretch from session VWAP in standard deviations (completed bars) |
| `slope_fast`, `slope_slow` | 5- and 30-minute VWAP slopes |
| `is_decaying` | Fast slope under 90% of slow while stretched > 2 SD |
| `vol_z` | Volume z-score |
| `profile`, `profile_rejection` | Market profile (value area) and whether price is rejecting it |
| `upper_circuit`, `lower_circuit` | Circuit limits; `0.0` when unknown |
| `spread_pct` | `(ask − bid) / ltp`; `0.0` when unknown |
| `is_circuit_hitter` | Touched its upper circuit earlier this session |
| `is_lower_circuit_hitter` | Touched its lower circuit earlier this session (the long-side twin) |

The helpers in `shortcircuit.strategy.features` (RSI, VWAP SD, ATR, volume fade,
pattern detection) are available for anything the context does not carry.

## What a strategy returns: `Signal`

| Field | Default | Meaning |
|---|---|---|
| `pattern` | required | Label for logs, the signal CSV and the ML row |
| `stop_reference` | required | The setup level the hard stop is built from: the stop sits beyond it by the engine's buffer, `max(0.5 × ATR, 0.25)` — above it for a SHORT, below it for a LONG. The setup is also abandoned if price runs through it before entry |
| `confidence` | `"MEDIUM"` | Passed to the signal manager (G8) and logged |
| `trigger_price` | previous bar's low (SHORT) or high (LONG) | Entry level the validation gate watches at 5 Hz; must sit on the entry side of `stop_reference`: below it for a SHORT, above it for a LONG |
| `exit` | `ExitPlan()` | How the engine manages the position (below) |
| `rearm_window` | `None` | A setup re-qualifying inside this window keeps its first ML observation instead of logging a new one |
| `stretch_score`, `vol_fade_ratio`, `oi_direction` | `0.0`, `0.0`, `"unknown"` | Quality scores logged for analysis |
| `extra` | `{}` | Strategy-specific levels carried on the signal. Cannot overwrite engine fields |
| `ml_features` | `{}` | Strategy-specific features written to the ML observation |

A `Signal` the engine cannot execute (a non-positive or non-finite stop, a
trigger on the wrong side of it) is logged and treated as no setup.

### Exits: `ExitPlan`

The exit mechanics live in the engine; a strategy picks and parameterises one.

| Profile | Behaviour | Parameters |
|---|---|---|
| `ExitProfile.DEFAULT` | Take-profit ladder per `TP_MODE` toward VWAP, soft stop, `MAX_HOLD_TIME_MINUTES` time stop | `tp_atr_mult` scales the target |
| `ExitProfile.EOD_HOLD` | No ladder, no time stop: runs on the hard stop to the 15:10 square-off | `rsi_cover=RsiCover(...)` covers early on a relief-rally RSI dip once in profit |
| `ExitProfile.STAGED_RSI` | No ladder, no time stop. In profit, RSI cooling to one level books part of the position and moves the stop on the rest to cost; RSI reaching a second level trails the stop above each completed bar's high (below its low for a long) | `staged_rsi=StagedRsiExit(...)`: levels, fraction, RSI and trail timeframes |

Any profile can also set `stop_pct`: the hard stop then sits that far from the
actual fill (e.g. `ExitPlan(stop_pct=5.0)` is 5% above a short's fill, 5% below
a long's) instead of at `stop_reference` plus the ATR buffer. `stop_reference` still decides when
a pending setup is abandoned.

**Square-off is per stock.** Fyers squares off intraday positions in F&O stocks
(the closing-auction list) at 15:05, so those close at 15:02 and take no new
entries after it; everything else closes at 15:10. A stock moves to the earlier
cutoff only when it is on Fyers' published F&O list. If the list cannot be
loaded, every position keeps 15:10.

```python
Signal(
    pattern="RANGE_BREAK",
    stop_reference=range_high,
    trigger_price=range_low * 0.999,
    exit=ExitPlan(profile=ExitProfile.EOD_HOLD, rsi_cover=RsiCover(threshold=40)),
    rearm_window=timedelta(minutes=15),
)
```

The plan travels with the position, so the live exit loop and the end-of-day
ghost audit both apply the strategy's own parameters.

### Engine gates every signal still passes

| Gate | When | Strategy control |
|---|---|---|
| G2 candle count | before strategies | — |
| G7 trade window and regime | before strategies | — |
| G9 higher-timeframe confirmation | after a signal | `requires_htf_confirmation = False` opts out |
| G8 cooldown, daily target, loss pause | after a signal | — |
| Validation gate, sizing, stop placement, reconciliation | execution | — |

---

## Rules a strategy must keep

- **Pure.** No broker calls, no network, no files, no sleeping. The engine has
  already fetched what it needs, and calls `evaluate` on the scan path for every
  candidate. `tests/unit/test_brain_isolation.py` checks the imports of every
  module under `strategies/` and fails on a runtime or I/O import.
- **Stateless, or idempotent.** A setup re-qualifies on every bar it persists;
  use `rearm_window` rather than remembering what you already signalled.
- **Fast.** It runs once per candidate per scan.
- **One side per strategy.** A strategy trades its `direction` and nothing
  else. Strategies whose side the operator's `TRADE_DIRECTION` filter holds back
  are not asked, never inverted.

An exception from `evaluate` is logged and treated as no setup — one broken
strategy cannot take the process down or block the others.

---

## Testing a strategy

Put tests in `strategies/tests/`. `pytest` collects them alongside the engine's
suite, under the same guards: outbound network is blocked and real credentials
are hidden. Build a `SetupContext` directly and assert on the `Signal`:

```python
import datetime as dt
import pandas as pd
from shortcircuit.strategy.sdk import SetupContext
from strategies.my_fade import MyFade

def ctx(**over):
    df = pd.DataFrame({...})                      # a 1-minute frame
    base = dict(symbol="NSE:X-EQ", ltp=110.0, now=dt.datetime(2026, 9, 21, 11, 0),
                df=df, df_15m=None, gain_pct=12.0, day_high=111.0, open_price=100.0,
                atr=0.8, vwap_sd=3.5, slope_fast=0.5, slope_slow=1.0, is_decaying=True,
                vol_z=0.0, profile=None, profile_rejection=False, upper_circuit=0.0,
                lower_circuit=0.0, spread_pct=0.001, is_circuit_hitter=False)
    base.update(over)
    return SetupContext(**base)

def test_fires_on_a_stretched_decaying_mover():
    assert MyFade().evaluate(ctx()) is not None

def test_declines_below_the_gain_floor():
    assert MyFade().evaluate(ctx(gain_pct=8.0)) is None
```

To see the engine treat your signal end to end — G9, G8, the stop and the
signal dict execution receives — drive `FyersAnalyzer.check_setup` with your
strategy passed in `strategies=[...]`; `tests/unit/test_strategy_plugins.py`
shows the stubs it needs.

---

## Running in Docker

Strategies are mounted, never baked into an image:
`deploy/docker-compose.yml` mounts `../strategies` read-only at
`/app/strategies`, and `.dockerignore` keeps the directory out of builds. An
image pushed to a registry therefore carries no strategy.
