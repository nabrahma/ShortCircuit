"""
The strategy plug-in contract.

ShortCircuit is an execution engine for Fyers. It owns everything that touches
the broker — the scan universe, market data, risk gates, order placement, stops,
take-profit and square-off mechanics, reconciliation, logging. A strategy owns
one decision: *is there a setup on this symbol right now, and how should it be
traded?* It answers by returning a `Signal`, or None.

Writing one::

    from shortcircuit.strategy.sdk import Direction, ExitPlan, SetupContext, Signal, Strategy

    class MyFade(Strategy):
        name = "my_fade"
        direction = Direction.SHORT

        def evaluate(self, ctx: SetupContext) -> Signal | None:
            if ctx.gain_pct < 10 or ctx.vwap_sd < 3:
                return None
            return Signal(pattern="MY_FADE", stop_reference=ctx.day_high)

A long strategy sets `direction = Direction.LONG`; its stop_reference is the
setup low and the engine buys the break above its trigger. Everything else is
the same contract.

Drop the module into the `strategies/` directory at the repository root (or any
directory listed in SHORTCIRCUIT_STRATEGY_PATH) and name it in
SHORTCIRCUIT_STRATEGIES or in `strategies/__init__.py`'s ACTIVE tuple. See
docs/STRATEGIES.md for the full guide.

Rules a strategy must keep
--------------------------
* Pure. `evaluate` reads the context and returns; no broker calls, no network,
  no files, no sleeping. The engine has already fetched everything it needs, and
  it calls `evaluate` on the scan thread for every candidate.
* Stateless across calls, or idempotent. A setup re-qualifies on every bar it
  persists; the engine de-duplicates via `Signal.rearm_window`.
* Fast. It runs once per candidate per scan.

The engine treats an exception from `evaluate` as "no setup" (fail closed) and
logs it; one broken strategy cannot take the process down.
"""
from __future__ import annotations

import datetime as _dt
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, ClassVar, Mapping, Optional

import pandas as pd

__all__ = [
    "Direction",
    "ExitPlan",
    "ExitProfile",
    "RsiCover",
    "SetupContext",
    "StagedRsiExit",
    "Signal",
    "Strategy",
    "Universe",
]


class Direction(str, Enum):
    """
    Which side a strategy trades. Every signal it returns trades that side.

    SHORT  enters on a break BELOW the trigger; the stop sits above
           `stop_reference` (the setup high); a push 0.2% above it first
           abandons the setup. Targets lie below entry.
    LONG   the mirror: enters on a break ABOVE the trigger; the stop sits below
           `stop_reference` (the setup low); a drop 0.2% below it first
           abandons the setup. Targets lie above entry.

    Both can be active at once. `config.TRADE_DIRECTION` ('BOTH', 'SHORT',
    'LONG') only filters which sides may open positions.
    """

    SHORT = "SHORT"
    LONG = "LONG"


class Universe(str, Enum):
    """
    Which of the scanner's day movers a strategy is offered.

    AUTO     gainers for a SHORT strategy, losers for a LONG one (the default)
    GAINERS  up config.SCANNER_GAIN_MIN_PCT..SCANNER_GAIN_MAX_PCT on the day
    LOSERS   down config.SCANNER_LOSS_MIN_PCT..SCANNER_LOSS_MAX_PCT
    MOVERS   both

    The scanner watches the union of what the active strategies ask for, and a
    candidate is offered only to the strategies whose universe includes it.
    """

    AUTO = "AUTO"
    GAINERS = "GAINERS"
    LOSERS = "LOSERS"
    MOVERS = "MOVERS"


class ExitProfile(str, Enum):
    """
    How the engine manages an open position. The mechanics live in the engine
    (execution/focus_engine.py); a strategy only chooses and parameterises one.

    DEFAULT   Take-profit ladder per config.TP_MODE toward the VWAP target,
              soft stop, and the MAX_HOLD_TIME_MINUTES time stop.
    EOD_HOLD  No take-profit ladder and no time stop: the position runs on its
              hard stop to the 15:10 square-off, optionally covered early by an
              RSI relief-rally rule (`ExitPlan.rsi_cover`).
    STAGED_RSI
              No take-profit ladder and no time stop. RSI decides the exits
              (`ExitPlan.staged_rsi`): a partial when it cools to one level, with
              the stop moved to cost, then a trailing stop on the remainder once
              it reaches a second. The hard stop and the square-off still apply.
    """

    DEFAULT = "DEFAULT"
    EOD_HOLD = "EOD_HOLD"
    STAGED_RSI = "STAGED_RSI"


@dataclass(frozen=True)
class RsiCover:
    """
    Cover an EOD_HOLD short early once it is in profit and the 1-minute RSI dips
    below `threshold` — the relief rally that tends to follow a breakdown.
    Evaluated on completed bars only, at least `skip_minutes` bars after entry,
    and never with fewer than `min_candles` of history.

    For a LONG the rule is mirrored: sell once in profit and RSI rises above
    100 - `threshold` (60 at the default), so one set of parameters means the
    same thing on either side.
    """

    threshold: float = 40.0
    period: int = 14
    skip_minutes: int = 5
    min_candles: int = 30


@dataclass(frozen=True)
class StagedRsiExit:
    """
    The exits of an ExitProfile.STAGED_RSI short, all read on COMPLETED bars of
    `bar_minutes` (aligned to the 09:15 open), RSI Wilder-smoothed over `period`.

    1. Scale-out: once the trade is in profit and RSI has cooled to
       `scale_out_rsi`, book `scale_out_fraction` of the position. With
       `stop_to_cost` the stop on the rest moves to the entry price.
    2. Trail: once RSI reaches `trail_rsi`, the stop on what is left follows the
       high of the previous completed `trail_bar_minutes` bar, plus
       `trail_buffer_pct`. It only ever tightens.

    No RSI is read from fewer than `min_bars` completed bars; until then the
    position runs on its hard stop and the square-off.

    For a LONG every level is mirrored: RSI is read as 100 - RSI (scale out once
    RSI has risen to 100 - `scale_out_rsi`, trail from 100 - `trail_rsi`), and
    the trail follows the LOW of the previous completed bar, less the buffer,
    only ever rising.
    """

    period: int = 14
    bar_minutes: int = 5
    min_bars: int = 15
    scale_out_rsi: float = 50.0
    scale_out_fraction: float = 0.5
    stop_to_cost: bool = True
    trail_rsi: float = 20.0
    trail_bar_minutes: int = 5
    trail_buffer_pct: float = 0.1

    def __post_init__(self) -> None:
        if self.period < 2 or self.min_bars < self.period + 1:
            raise ValueError("min_bars must be at least period + 1, and period at least 2")
        for name in ("bar_minutes", "trail_bar_minutes"):
            if getattr(self, name) not in (1, 2, 3, 5, 10, 15, 30):
                raise ValueError(f"{name} must divide the session into whole bars")
        if not 0.0 < self.scale_out_fraction < 1.0:
            raise ValueError("scale_out_fraction must be between 0 and 1")
        if not 0.0 < self.trail_rsi < self.scale_out_rsi < 100.0:
            raise ValueError("RSI levels must satisfy 0 < trail_rsi < scale_out_rsi < 100")
        if not 0.0 <= self.trail_buffer_pct < 5.0:
            raise ValueError("trail_buffer_pct must be in [0, 5)")


@dataclass(frozen=True)
class ExitPlan:
    """The exit a strategy asks the engine to run for its signal."""

    profile: ExitProfile = ExitProfile.DEFAULT
    # DEFAULT only: scales the ATR-based take-profit. None uses the engine default.
    tp_atr_mult: Optional[float] = None
    # EOD_HOLD only: early relief-rally cover. None disables it.
    rsi_cover: Optional[RsiCover] = None
    # STAGED_RSI only, and required there.
    staged_rsi: Optional[StagedRsiExit] = None
    # Hard stop this far from the actual fill, in percent. None places it at the
    # signal's stop_reference plus the engine's ATR buffer.
    stop_pct: Optional[float] = None

    def __post_init__(self) -> None:
        if self.profile is not ExitProfile.EOD_HOLD and self.rsi_cover is not None:
            raise ValueError("rsi_cover applies to ExitProfile.EOD_HOLD only")
        if self.profile is not ExitProfile.DEFAULT and self.tp_atr_mult is not None:
            raise ValueError(f"{self.profile.value} has no take-profit for tp_atr_mult to scale")
        if (self.profile is ExitProfile.STAGED_RSI) != (self.staged_rsi is not None):
            raise ValueError("staged_rsi is required by, and only valid for, ExitProfile.STAGED_RSI")
        # The order path refuses a stop more than 10% from the fill as implausible.
        if self.stop_pct is not None and not (
            isinstance(self.stop_pct, (int, float)) and not isinstance(self.stop_pct, bool)
            and 0.0 < self.stop_pct <= 10.0
        ):
            raise ValueError("stop_pct must be in (0, 10]")

    def params(self) -> dict[str, Any]:
        """Serialisable parameters carried with the position and the ML row."""
        out: dict[str, Any] = {"rsi_cover": asdict(self.rsi_cover) if self.rsi_cover else None}
        if self.staged_rsi is not None:
            out["staged_rsi"] = asdict(self.staged_rsi)
        if self.stop_pct is not None:
            out["stop_pct"] = float(self.stop_pct)
        return out


@dataclass(frozen=True)
class SetupContext:
    """
    Everything the engine knows about a candidate at evaluation time.

    `df` is the session's 1-minute frame, enriched with `vwap` and the engine's
    standard indicator columns. Its LAST row is the bar still forming; use
    `df.iloc[:-1]` for completed bars only.
    """

    symbol: str
    ltp: float
    now: _dt.datetime
    df: pd.DataFrame
    df_15m: Optional[pd.DataFrame]

    # Day statistics
    gain_pct: float          # vs previous close (open if unavailable)
    day_high: float
    open_price: float

    # Indicators, computed once by the engine for every strategy
    atr: float
    vwap_sd: float           # stretch from VWAP in standard deviations, completed bars
    slope_fast: float        # 5-minute VWAP slope
    slope_slow: float        # 30-minute VWAP slope
    is_decaying: bool        # fast slope fell below 90% of slow while stretched
    vol_z: float             # volume z-score

    # Market profile
    profile: Optional[Mapping[str, Any]]
    profile_rejection: bool

    # Order book and circuit state
    upper_circuit: float     # 0.0 when unknown
    lower_circuit: float     # 0.0 when unknown
    spread_pct: float        # (ask - bid) / ltp; 0.0 when unknown
    is_circuit_hitter: bool  # touched its upper circuit earlier this session

    # Added with LONG support. Defaulted, so a context built without them (a
    # plug-in's own tests) still works.
    day_low: float = 0.0
    is_lower_circuit_hitter: bool = False  # touched its lower circuit this session


@dataclass
class Signal:
    """
    A strategy's answer: there is a setup, and this is how to trade it.

    Prices are absolute, and their side follows the strategy's direction.
    SHORT: the hard stop sits at `stop_reference` plus the engine's ATR buffer,
    max(0.5 * ATR, 0.25); the engine enters when price breaks below
    `trigger_price` and abandons the setup if price instead pushes above
    `stop_reference` first. LONG: the stop sits at `stop_reference` minus the
    buffer; the engine enters when price breaks above `trigger_price` and
    abandons the setup if price drops below `stop_reference` first.
    """

    pattern: str
    stop_reference: float
    confidence: str = "MEDIUM"
    # Entry trigger the validation gate watches. None: the previous bar's low
    # for a SHORT, its high for a LONG. Must sit on the entry side of
    # stop_reference: below it for a SHORT, above it for a LONG.
    trigger_price: Optional[float] = None
    exit: ExitPlan = field(default_factory=ExitPlan)
    # A setup that re-qualifies within this window is the same setup with a
    # refreshed trigger: it keeps its first ML observation and is not re-logged.
    rearm_window: Optional[_dt.timedelta] = None
    # Quality scores the engine logs and the ML trainer reads.
    stretch_score: float = 0.0
    vol_fade_ratio: float = 0.0
    oi_direction: str = "unknown"
    # Strategy-specific levels carried on the signal dict handed to execution
    # (e.g. a range's high and low). Keys must not collide with engine fields.
    extra: dict[str, Any] = field(default_factory=dict)
    # Strategy-specific features written to the signal's ML observation.
    ml_features: dict[str, Any] = field(default_factory=dict)
    # Label -> value lines shown on the signal's Telegram card, in this order
    # (e.g. {"Range width": "1.8%", "Bars in range": 12}). Display only: the
    # engine never reads them.
    display: dict[str, Any] = field(default_factory=dict)


class Strategy(ABC):
    """Base class for strategy plug-ins. Subclass, set `name`, implement `evaluate`."""

    #: Unique, stable identifier. Appears in config, logs and ML rows.
    name: ClassVar[str]
    #: The side this strategy trades.
    direction: ClassVar[Direction] = Direction.SHORT
    #: Which of the scanner's movers it is offered (see Universe).
    universe: ClassVar[Universe] = Universe.AUTO
    #: Run the engine's higher-timeframe exhaustion check (G9) on this
    #: strategy's signals before they reach the order path.
    requires_htf_confirmation: ClassVar[bool] = True
    #: One line for the operator: what the strategy trades. Shown by Telegram's
    #: /strategies. Empty falls back to the first line of the class docstring.
    description: ClassVar[str] = ""

    @abstractmethod
    def evaluate(self, ctx: SetupContext) -> Optional[Signal]:
        """Return a Signal if there is a setup on `ctx.symbol`, else None."""

    def describe(self) -> Mapping[str, Any]:
        """
        Label -> value pairs for Telegram's /strategy <name>: thresholds, stop
        size, anything the operator should be able to check from the phone.

        Must be cheap and side-effect free, like `evaluate`. The default shows
        nothing beyond what the engine already knows about the strategy.
        """
        return {}

    def __repr__(self) -> str:
        return f"<Strategy {self.name} ({self.direction.value})>"
