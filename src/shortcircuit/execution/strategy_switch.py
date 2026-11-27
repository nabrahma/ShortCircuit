"""
Which active strategies may open new setups right now.

Every strategy listed in the active set runs unless the operator pauses it from
Telegram (`/pause <name>`). A paused strategy is not offered candidates, so it
produces no new signals; positions it already holds and setups it already armed
are untouched and keep their own stops and exits. Pausing therefore only ever
reduces what the engine does: it cannot open, close or resize anything.

The state lives in memory and resets on restart, as the direction filter does.
To stop a strategy for good, remove it from the active set.
"""
from __future__ import annotations

import logging
import threading
from typing import FrozenSet, Iterable

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_paused: set[str] = set()


def pause(names: Iterable[str]) -> FrozenSet[str]:
    """Pause each name; return the ones that were not already paused."""
    with _lock:
        newly = {n for n in names if n not in _paused}
        _paused.update(newly)
    for name in sorted(newly):
        logger.critical("[STRATEGY] %s PAUSED: no new setups until /resume", name)
    return frozenset(newly)


def resume(names: Iterable[str]) -> FrozenSet[str]:
    """Resume each name; return the ones that were paused."""
    with _lock:
        was = {n for n in names if n in _paused}
        _paused.difference_update(was)
    for name in sorted(was):
        logger.critical("[STRATEGY] %s RESUMED", name)
    return frozenset(was)


def is_paused(name: str) -> bool:
    with _lock:
        return name in _paused


def paused() -> FrozenSet[str]:
    with _lock:
        return frozenset(_paused)


def reset() -> None:
    """Clear every pause. For tests and a fresh session."""
    with _lock:
        _paused.clear()
