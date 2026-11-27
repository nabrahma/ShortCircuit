"""
How a signal or a position will exit, in words, read from the exit plan it
carries. The engine decides the exits; this only describes what it will do, so a
card never claims an exit the strategy did not ask for.
"""
from __future__ import annotations

from typing import Any, Mapping

from shortcircuit import config
from shortcircuit.eod import squareoff
from shortcircuit.execution import directions


def _num(x: Any) -> str:
    return f"{float(x):g}"


def _mirror(level: Any, is_long: bool) -> str:
    """RSI levels are written for a short; a long reads them as 100 - level."""
    return _num(100 - float(level)) if is_long else _num(level)


def describe(record: Mapping[str, Any]) -> str:
    """One line: the take-profit or hold rule, any fixed stop, the time stop."""
    profile = str(record.get("exit_profile") or "DEFAULT").upper()
    params = record.get("exit_params") or {}
    is_long = directions.is_long(directions.of_signal(record))
    parts: list[str] = []

    stop_pct = record.get("stop_pct") or params.get("stop_pct")
    if stop_pct:
        parts.append(f"stop {_num(stop_pct)}% from the fill")

    if profile == "EOD_HOLD":
        parts.append("hold on the hard stop to the square-off")
        cover = params.get("rsi_cover")
        if cover:
            parts.append(f"early exit at RSI {_mirror(cover.get('threshold', 40), is_long)} once in profit")
    elif profile == "STAGED_RSI":
        s = params.get("staged_rsi") or {}
        frac = float(s.get("scale_out_fraction", 0.5)) * 100
        step = f"{frac:.0f}% off at RSI {_mirror(s.get('scale_out_rsi', 50), is_long)}"
        if s.get("stop_to_cost", True):
            step += ", stop to cost"
        parts.append(step)
        parts.append(f"trail from RSI {_mirror(s.get('trail_rsi', 20), is_long)}")
    else:
        tp = str(getattr(config, "TP_MODE", "SCALE")).upper()
        if tp == "SCALE":
            text = "half at the midpoint, rest to VWAP"
            if getattr(config, "P52_BREAKEVEN_AFTER_TP1", False):
                text += ", stop to breakeven after the half"
        elif tp == "SINGLE":
            text = "all at the midpoint"
        else:
            text = "no take-profit"
        parts.append(text)
        hold = getattr(config, "MAX_HOLD_TIME_MINUTES", 0)
        if hold:
            parts.append(f"{hold}-min time stop")
    return "; ".join(parts)


def squareoff_at(symbol: str) -> str:
    """When this symbol is flattened today: 15:02 for F&O stocks, else 15:10."""
    try:
        return squareoff.squareoff_time(symbol).strftime("%H:%M")
    except Exception:
        return squareoff.ENGINE_SQUAREOFF.strftime("%H:%M")
