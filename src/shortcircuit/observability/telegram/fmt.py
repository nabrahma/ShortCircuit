"""Formatting shared by every Telegram message. Pure functions, no I/O."""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from shortcircuit.execution import directions

IST = ZoneInfo("Asia/Kolkata")
DASH = "—"


def he(text: Any) -> str:
    """Escape for Telegram HTML. Every value that is not ours goes through this."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _finite(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def price(x: Any) -> str:
    v = _finite(x)
    return f"₹{v:,.2f}" if v is not None and v > 0 else DASH


def money(x: Any, signed: bool = False) -> str:
    v = _finite(x)
    if v is None:
        return DASH
    if signed:
        return f"+₹{v:,.2f}" if v >= 0 else f"−₹{abs(v):,.2f}"
    return f"₹{v:,.0f}"


def value(x: Any) -> str:
    """A strategy-supplied value, readable and escaped."""
    if isinstance(x, bool):
        return "yes" if x else "no"
    if isinstance(x, float):
        return DASH if not math.isfinite(x) else f"{x:,.4f}".rstrip("0").rstrip(".")
    if x is None:
        return DASH
    return he(x)


def now_ist() -> datetime:
    return datetime.now(IST)


def clock(dt: Optional[datetime] = None) -> str:
    return (dt or now_ist()).strftime("%H:%M:%S")


def side_label(side: Any) -> str:
    d = directions.normalise(side)
    return {directions.SHORT: "🔴 SHORT", directions.LONG: "🟢 LONG"}.get(d, DASH)


def trigger_sign(side: Any) -> str:
    """'&gt;' for a long (enters on a break above), '&lt;' for a short."""
    return "&gt;" if directions.is_long(side) else "&lt;"


def mode_label() -> str:
    """The direction filter: which sides may open positions."""
    return {
        directions.LONG: "🟢 LONG only (BUY)",
        directions.SHORT: "🔴 SHORT only (SELL)",
    }.get(directions.mode(), "🟢🔴 BOTH (each strategy its own side)")
