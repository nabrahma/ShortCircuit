"""
What Telegram knows about the active strategies: who they are, whether they are
paused, and how each one's day is going. Nothing here is specific to any one
strategy; it reads the plug-in contract (`strategy/sdk.py`) and the signals the
engine hands it.
"""
from __future__ import annotations

import inspect
import logging
import threading
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Mapping, Optional, Sequence

from shortcircuit.execution import strategy_switch
from shortcircuit.observability.telegram import fmt
from shortcircuit.strategy.sdk import Strategy

logger = logging.getLogger(__name__)

# Validation outcomes the focus engine reports, by the day counter they feed.
OUTCOME_EVENT = {"VALIDATED": "triggered", "TIMEOUT": "expired", "REJECTED": "invalidated"}
UNNAMED = "(unnamed)"


@dataclass
class DayStats:
    armed: int = 0
    triggered: int = 0
    expired: int = 0
    invalidated: int = 0

    def line(self) -> str:
        return (f"armed {self.armed} · triggered {self.triggered} · "
                f"expired {self.expired} · invalidated {self.invalidated}")


def blurb(strategy: Strategy) -> str:
    """The strategy's own one-liner, else the first line of its docstring."""
    text = str(getattr(strategy, "description", "") or "").strip()
    if not text:
        doc = type(strategy).__dict__.get("__doc__") or ""
        text = inspect.cleandoc(doc).split("\n\n", 1)[0].replace("\n", " ").strip() if doc else ""
    return text


class StrategyBook:
    def __init__(self, strategies: Iterable[Strategy] = ()) -> None:
        self._lock = threading.Lock()
        self._strategies: list[Strategy] = list(strategies)
        self._stats: dict[str, DayStats] = {}
        self._day: Optional[date] = None

    # Membership

    def attach(self, strategies: Iterable[Strategy]) -> None:
        with self._lock:
            self._strategies = list(strategies)

    @property
    def strategies(self) -> Sequence[Strategy]:
        with self._lock:
            return tuple(self._strategies)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.strategies)

    def find(self, name: str) -> Optional[Strategy]:
        wanted = (name or "").strip().lower()
        return next((s for s in self.strategies if s.name.lower() == wanted), None)

    def resolve(self, args: Sequence[str]) -> tuple[list[str], list[str]]:
        """Names from command arguments ('all' for every active one): (known, unknown)."""
        if not args:
            return [], []
        if len(args) == 1 and args[0].lower() == "all":
            return list(self.names), []
        known: list[str] = []
        unknown: list[str] = []
        for a in args:
            s = self.find(a)
            (known if s else unknown).append(s.name if s else a)
        return known, unknown

    # Today's counters

    def _today(self) -> dict[str, DayStats]:
        today = fmt.now_ist().date()
        if self._day != today:
            self._day, self._stats = today, {}
        return self._stats

    def record(self, signal: Mapping[str, Any], event: str) -> None:
        name = str(signal.get("strategy") or UNNAMED)
        with self._lock:
            stats = self._today().setdefault(name, DayStats())
            if hasattr(stats, event):
                setattr(stats, event, getattr(stats, event) + 1)

    def record_outcome(self, signal: Mapping[str, Any], outcome: str) -> None:
        event = OUTCOME_EVENT.get(str(outcome).upper())
        if event:
            self.record(signal, event)

    def stats(self, name: str) -> DayStats:
        with self._lock:
            return DayStats(**vars(self._today().get(name, DayStats())))

    def totals(self) -> DayStats:
        with self._lock:
            out = DayStats()
            for s in self._today().values():
                for k in vars(out):
                    setattr(out, k, getattr(out, k) + getattr(s, k))
            return out

    # Description

    @staticmethod
    def is_paused(name: str) -> bool:
        return strategy_switch.is_paused(name)

    def details(self, strategy: Strategy) -> list[tuple[str, str]]:
        """The strategy's own describe() output, guarded: it is plug-in code."""
        try:
            raw = strategy.describe() or {}
            return [(fmt.he(k), fmt.value(v)) for k, v in dict(raw).items()]
        except Exception as exc:
            logger.warning("[TELEGRAM] %s.describe() raised: %s", strategy.name, exc)
            return [("describe()", fmt.he(f"raised {type(exc).__name__}: {exc}"))]
