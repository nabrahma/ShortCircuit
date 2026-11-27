"""
Which side a trade is on, and which sides may trade right now.

Every strategy declares its direction (sdk.Direction) and the movers it wants
(sdk.Universe), and every signal carries its direction to the order path, so a
short and a long can both be active. Nothing here decides a side; it reads one.
The live engine and the backtest both use it, so they cannot disagree about
what a direction means.

`config.TRADE_DIRECTION` is a filter, switchable from Telegram:
  'BOTH'   every active strategy trades its own direction;
  'SHORT'  only short strategies may open positions;
  'LONG'   only long strategies may.
Anything else reads as 'BOTH', so an unexpected value never blocks a side
silently — and never trades one that no strategy asked for.
"""
from __future__ import annotations

from typing import Iterable, Mapping, Optional, Tuple

from shortcircuit import config

SHORT = "SHORT"
LONG = "LONG"
BOTH = "BOTH"


def normalise(direction) -> Optional[str]:
    """'SHORT' or 'LONG' from a Direction, a string or an order side; None if neither."""
    value = getattr(direction, "value", direction)
    text = str(value or "").strip().upper()
    if text in (SHORT, "SELL"):
        return SHORT
    if text in (LONG, "BUY"):
        return LONG
    return None


def mode() -> str:
    """The direction filter in force: 'BOTH', 'SHORT' or 'LONG'."""
    m = str(getattr(config, "TRADE_DIRECTION", BOTH) or BOTH).strip().upper()
    return m if m in (SHORT, LONG) else BOTH


def allowed(direction) -> bool:
    """May a strategy trading `direction` open a position now?"""
    d = normalise(direction)
    return d is not None and mode() in (BOTH, d)


def of_signal(signal: Mapping) -> str:
    """
    The side a signal or position trades. Signals carry 'direction'; positions
    carry 'side'. A record with neither predates per-strategy directions, when
    every trade was a short unless the engine was switched to LONG.
    """
    for key in ("direction", "side"):
        d = normalise(signal.get(key))
        if d:
            return d
    return LONG if mode() == LONG else SHORT


def is_long(direction) -> bool:
    return normalise(direction) == LONG


def entry_side(direction) -> str:
    """The order side that opens a position."""
    return "BUY" if is_long(direction) else "SELL"


def exit_side(direction) -> str:
    """The order side that closes one, and the side of its protective stop."""
    return "SELL" if is_long(direction) else "BUY"


def bands_of(strategy) -> Tuple[bool, bool]:
    """
    (gainers, losers): the movers a strategy is offered, from its `universe`.
    AUTO, the default, is the side a mean-reversion setup lives on: gainers for
    a short, losers for a long.
    """
    u = str(getattr(getattr(strategy, "universe", None), "value", "AUTO") or "AUTO").upper()
    if u == "GAINERS":
        return True, False
    if u == "LOSERS":
        return False, True
    if u == "MOVERS":
        return True, True
    long_ = is_long(getattr(strategy, "direction", None))
    return (not long_), long_


def scan_bands(strategies: Iterable) -> Tuple[bool, bool]:
    """
    (gainers, losers): which halves of the movers universe the scanner watches,
    the union of what the strategies allowed to trade are offered.

    With no strategy allowed (none active, or the filter excludes them all) it
    is gainers alone, which is what the engine always scanned: positions are
    still managed and the scan log stays comparable. Losers are scanned only
    when a strategy wants them, because each is a history fetch and fetches
    share the rate limit with the order path.
    """
    gainers = losers = False
    for s in strategies:
        if not allowed(getattr(s, "direction", None)):
            continue
        g, lo = bands_of(s)
        gainers, losers = gainers or g, losers or lo
    if not (gainers or losers):
        return True, False
    return gainers, losers


def offered(strategy, gain_pct: float) -> bool:
    """
    Is a candidate up (or down) `gain_pct` on the day one this strategy is
    offered? The sign decides; the scanner has already applied the band limits.
    """
    gainers, losers = bands_of(strategy)
    return (gainers and gain_pct >= 0) or (losers and gain_pct < 0)


def in_band(gain_pct: float, gainers: bool, losers: bool) -> bool:
    """Does a day change of `gain_pct` (%) fall in a band being scanned?"""
    if gainers and config.SCANNER_GAIN_MIN_PCT <= gain_pct <= config.SCANNER_GAIN_MAX_PCT:
        return True
    lo = float(getattr(config, "SCANNER_LOSS_MIN_PCT", config.SCANNER_GAIN_MIN_PCT))
    hi = float(getattr(config, "SCANNER_LOSS_MAX_PCT", config.SCANNER_GAIN_MAX_PCT))
    return losers and -hi <= gain_pct <= -lo


__all__ = ["BOTH", "LONG", "SHORT", "allowed", "bands_of", "entry_side", "exit_side",
           "in_band", "is_long", "mode", "normalise", "of_signal", "offered", "scan_bands"]
