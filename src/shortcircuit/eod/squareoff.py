"""
When each position must be flat, per symbol.

Fyers squares off intraday (MIS) positions itself: at 15:05 for stocks in the
closing auction session (CAS, which is the F&O stock list, since 3 Aug 2026) and
at 15:20 for every other equity. The engine's own square-off at 15:10 is ahead
of the second and behind the first, so an F&O position left to it would be
closed by the broker first, with an auto square-off charge and no say over the
fill — and no stop-loss order is carried into the auction.

A cutoff moves earlier only for a symbol POSITIVELY known to be an F&O stock.
Anything uncertain — the list could not be loaded, or the symbol is not on it —
keeps the engine's 15:10. Moving early is only worth its cost when the broker's
square-off is certain.

The F&O list is Fyers' own public symbol master. Membership changes at most
monthly and the process restarts daily, so it is loaded once, at startup.
"""
from __future__ import annotations

import csv
import io
import logging
import threading
from datetime import datetime, time
from typing import Callable, Optional

import pytz

from shortcircuit import config

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

ENGINE_SQUAREOFF = time(15, 10)

FNO_MASTER_URL = "https://public.fyers.in/sym_details/NSE_FO.csv"
_INSTRUMENT_TYPE_COL = 2
_UNDERLYING_COL = 13
_STOCK_FUTURE = "13"          # Fyers instrument type; 11 is an index future


def cas_squareoff() -> time:
    hour, minute = getattr(config, "CAS_SQUAREOFF_HHMM", (15, 2))
    return time(int(hour), int(minute))


def parse_fno_master(text: str) -> frozenset[str]:
    """Cash symbols of every stock with a listed future, e.g. NSE:RELIANCE-EQ."""
    symbols = set()
    for row in csv.reader(io.StringIO(text)):
        if len(row) > _UNDERLYING_COL and row[_INSTRUMENT_TYPE_COL].strip() == _STOCK_FUTURE:
            underlying = row[_UNDERLYING_COL].strip()
            if underlying:
                symbols.add(f"NSE:{underlying}-EQ")
    return frozenset(symbols)


def _download_master() -> str:
    # Public static file on Fyers' CDN, not the rate-limited trading API.
    import requests
    response = requests.get(FNO_MASTER_URL, timeout=(5, 30))
    response.raise_for_status()
    return response.text


class FnoRegistry:
    """The F&O stock list, or None while it is unknown."""

    # Fewer than this many names means a truncated or reshaped file, not a list.
    MIN_PLAUSIBLE = 100

    def __init__(self) -> None:
        self._symbols: Optional[frozenset[str]] = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._symbols is not None

    def refresh(self, fetch: Callable[[], str] = _download_master) -> bool:
        try:
            symbols = parse_fno_master(fetch())
        except Exception as exc:
            logger.warning(
                "[SQUAREOFF] F&O list unavailable (%s: %s) — every position keeps "
                "the %s square-off.", type(exc).__name__, exc, ENGINE_SQUAREOFF.strftime("%H:%M"),
            )
            return False
        if len(symbols) < self.MIN_PLAUSIBLE:
            logger.warning(
                "[SQUAREOFF] F&O list has only %d names — not trusted; every position "
                "keeps the %s square-off.", len(symbols), ENGINE_SQUAREOFF.strftime("%H:%M"),
            )
            return False
        with self._lock:
            self._symbols = symbols
        logger.info(
            "[SQUAREOFF] %d F&O stocks loaded: those square off at %s, everything "
            "else at %s.", len(symbols), cas_squareoff().strftime("%H:%M"),
            ENGINE_SQUAREOFF.strftime("%H:%M"),
        )
        return True

    def is_fno(self, symbol: str) -> Optional[bool]:
        """True or False when known; None when the list has not loaded."""
        with self._lock:
            symbols = self._symbols
        if symbols is None:
            return None
        return symbol in symbols


registry = FnoRegistry()


def squareoff_time(symbol: str, fno: Optional[FnoRegistry] = None) -> time:
    """The time by which `symbol` must be flat."""
    if (fno or registry).is_fno(symbol) is True:
        return min(cas_squareoff(), ENGINE_SQUAREOFF)
    return ENGINE_SQUAREOFF


def past_squareoff(symbol: str, now: Optional[datetime] = None,
                   fno: Optional[FnoRegistry] = None) -> bool:
    now = now or datetime.now(IST)
    if now.tzinfo is not None:
        now = now.astimezone(IST)
    return now.time() >= squareoff_time(symbol, fno)
