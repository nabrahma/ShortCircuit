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
    "Signal",
    "Strategy",
]


class Direction(str, Enum):
    """
    Which side a strategy trades.

    The execution engine currently supports SHORT only: its validation gate
    enters on a break below the trigger and invalidates on a push above the
    setup high. The registry refuses to load a LONG strategy rather than let it
    trade through short-side mechanics. LONG is part of the contract so that
    plug-ins written for it need no change once the engine supports it.
    """

    SHORT = "SHORT"
    LONG = "LONG"


class ExitProfile(str, Enum):
    """
    How the engine manages an open position. The mechanics live in the engine
    (execution/focus_engine.py); a strategy only chooses and parameterises one.

    DEFAULT   Take-profit ladder per config.TP_MODE toward the VWAP target,
              soft stop, and the MAX_HOLD_TIME_MINUTES time stop.
    EOD_HOLD  No take-profit ladder and no time stop: the position runs on its
              hard stop to the 15:10 square-off, optionally covered early by an
              RSI relief-rally rule (`ExitPlan.rsi_cover`).
    """

    DEFAULT = "DEFAULT"
    EOD_HOLD = "EOD_HOLD"


@dataclass(frozen=True)
class RsiCover:
    """
    Cover an EOD_HOLD short early once it is in profit and the 1-minute RSI dips
    below `threshold` — the relief rally that tends to follow a breakdown.
    Evaluated on completed bars only, at least `skip_minutes` bars after entry,
    and never with fewer than `min_candles` of history.
    """

    threshold: float = 40.0
    period: int = 14
    skip_minutes: int = 5
    min_candles: int = 30


@dataclass(frozen=True)
class ExitPlan:
    """The exit a strategy asks the engine to run for its signal."""

    profile: ExitProfile = ExitProfile.DEFAULT
    # DEFAULT only: scales the ATR-based take-profit. None uses the engine default.
    tp_atr_mult: Optional[float] = None
    # EOD_HOLD only: early relief-rally cover. None disables it.
    rsi_cover: Optional[RsiCover] = None

    def __post_init__(self) -> None:
        if self.profile is ExitProfile.DEFAULT and self.rsi_cover is not None:
            raise ValueError("rsi_cover applies to ExitProfile.EOD_HOLD only")
        if self.profile is ExitProfile.EOD_HOLD and self.tp_atr_mult is not None:
            raise ValueError("EOD_HOLD has no take-profit for tp_atr_mult to scale")

    def params(self) -> dict[str, Any]:
        """Serialisable parameters carried with the position and the ML row."""
        return {"rsi_cover": asdict(self.rsi_cover) if self.rsi_cover else None}


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


@dataclass
class Signal:
    """
    A strategy's answer: there is a setup, and this is how to trade it.

    Prices are absolute. The hard stop sits at `stop_reference` plus the
    engine's ATR buffer, max(0.5 * ATR, 0.25). The engine enters when price
    breaks below `trigger_price`, and abandons the setup if price instead
    pushes above `stop_reference` first.
    """

    pattern: str
    stop_reference: float
    confidence: str = "MEDIUM"
    # Entry trigger the validation gate watches. None: the previous bar's low.
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
    # (e.g. coil_high / coil_low). Keys must not collide with engine fields.
    extra: dict[str, Any] = field(default_factory=dict)
    # Strategy-specific features written to the signal's ML observation.
    ml_features: dict[str, Any] = field(default_factory=dict)


class Strategy(ABC):
    """Base class for strategy plug-ins. Subclass, set `name`, implement `evaluate`."""

    #: Unique, stable identifier. Appears in config, logs and ML rows.
    name: ClassVar[str]
    #: The side this strategy trades.
    direction: ClassVar[Direction] = Direction.SHORT
    #: Run the engine's higher-timeframe exhaustion check (G9) on this
    #: strategy's signals before they reach the order path.
    requires_htf_confirmation: ClassVar[bool] = True

    @abstractmethod
    def evaluate(self, ctx: SetupContext) -> Optional[Signal]:
        """Return a Signal if there is a setup on `ctx.symbol`, else None."""

    def __repr__(self) -> str:
        return f"<Strategy {self.name} ({self.direction.value})>"
