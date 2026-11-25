"""
Run mode.

    SHORTCIRCUIT_MODE=live       (default) trades.
    SHORTCIRCUIT_MODE=data_only  logs in, streams and reads market data, scans,
                                 evaluates strategies and logs what it WOULD do —
                                 and can never place, modify or cancel an order.

Data-only is for testing and backtesting, including from a connection without a
static IP: Fyers enforces the static-IP rule on order requests only, so login,
history, quotes, depth and the websockets all work.

The guarantee is structural, not a convention. Every Fyers REST request passes
through broker/transport.py, and in data-only mode it refuses any request that
writes to orders or positions before it is sent. The checks elsewhere (entry,
adoption, square-off) only make the logs say what happened instead of showing a
broker error.
"""
from __future__ import annotations

import os
from enum import Enum

ENV = "SHORTCIRCUIT_MODE"


class Mode(str, Enum):
    LIVE = "live"
    DATA_ONLY = "data_only"


class TradingDisabled(RuntimeError):
    """An order write was attempted in data-only mode. It was not sent."""


def current() -> Mode:
    """The configured mode. An unrecognised value is an error, never a guess."""
    raw = os.getenv(ENV, "").strip().lower().replace("-", "_")
    if not raw:
        return Mode.LIVE
    try:
        return Mode(raw)
    except ValueError:
        raise ValueError(
            f"{ENV}={os.getenv(ENV)!r} is not one of: {', '.join(m.value for m in Mode)}"
        ) from None


def is_data_only() -> bool:
    return current() is Mode.DATA_ONLY
