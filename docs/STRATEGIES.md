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

## What a strategy receives: `SetupContext`

Computed once per candidate by the engine, for every strategy.

| Field | Meaning |
|---|---|
| `symbol`, `ltp`, `now` | The candidate, its last price, evaluation time |
| `df` | Session 1-minute frame, enriched with `vwap` and indicator columns. **The last row is the bar still forming**; use `df.iloc[:-1]` for completed bars |
| `df_15m` | 15-minute frame, or `None` |
| `gain_pct` | % change vs previous close (open if unavailable) |
| `day_high`, `open_price` | Session high and open |
| `atr` | ATR of the 1-minute frame |
| `vwap_sd` | Stretch from session VWAP in standard deviations (completed bars) |
| `slope_fast`, `slope_slow` | 5- and 30-minute VWAP slopes |
| `is_decaying` | Fast slope under 90% of slow while stretched > 2 SD |
| `vol_z` | Volume z-score |
| `profile`, `profile_rejection` | Market profile (value area) and whether price is rejecting it |
| `upper_circuit`, `lower_circuit` | Circuit limits; `0.0` when unknown |
| `spread_pct` | `(ask − bid) / ltp`; `0.0` when unknown |
| `is_circuit_hitter` | Touched its upper circuit earlier this session |

The helpers in `shortcircuit.strategy.features` (RSI, VWAP SD, ATR, volume fade,
pattern detection) are available for anything the context does not carry.

## What a strategy returns: `Signal`

| Field | Default | Meaning |
|---|---|---|
| `pattern` | required | Label for logs, the signal CSV and the ML row |
| `stop_reference` | required | The hard stop sits here plus the engine's buffer, `max(0.5 × ATR, 0.25)`. The setup is also abandoned if price pushes above it before entry |
| `confidence` | `"MEDIUM"` | Passed to the signal manager (G8) and logged |
| `trigger_price` | previous bar's low | Entry level the validation gate watches at 5 Hz; must sit below `stop_reference` |
| `exit` | `ExitPlan()` | How the engine manages the position (below) |
| `rearm_window` | `None` | A setup re-qualifying inside this window keeps its first ML observation instead of logging a new one |
| `stretch_score`, `vol_fade_ratio`, `oi_direction` | `0.0`, `0.0`, `"unknown"` | Quality scores logged for analysis |
| `extra` | `{}` | Strategy-specific levels carried on the signal. Cannot overwrite engine fields |
| `ml_features` | `{}` | Strategy-specific features written to the ML observation |

A `Signal` the engine cannot execute (a non-positive or non-finite stop, a
trigger at or above the stop) is logged and treated as no setup.

### Exits: `ExitPlan`

The exit mechanics live in the engine; a strategy picks and parameterises one.

| Profile | Behaviour | Parameters |
|---|---|---|
| `ExitProfile.DEFAULT` | Take-profit ladder per `TP_MODE` toward VWAP, soft stop, `MAX_HOLD_TIME_MINUTES` time stop | `tp_atr_mult` scales the target |
| `ExitProfile.EOD_HOLD` | No ladder, no time stop: runs on the hard stop to the 15:10 square-off | `rsi_cover=RsiCover(...)` covers early on a relief-rally RSI dip once in profit |

```python
Signal(
    pattern="COIL_BREAK",
    stop_reference=coil_high,
    trigger_price=coil_low * 0.999,
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
- **SHORT only, for now.** The validation gate enters on a break below the
  trigger. A `Direction.LONG` strategy is refused at load time. Strategies whose
  direction does not match the engine's current `TRADE_DIRECTION` (switchable
  from Telegram) are skipped, never inverted.

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
