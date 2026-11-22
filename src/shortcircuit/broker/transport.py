"""
The single path from this process to the Fyers REST API.

Every request the Fyers SDK makes goes through `MeteredRequests`, which

  1. bounds it with a timeout (the SDK passes none),
  2. charges it to the account-wide rate limiter at a priority derived from the
     endpoint, and
  3. reports broker throttling (HTTP 429 / code -429) back to the limiter.

Why metering lives here and not at call sites
---------------------------------------------
It used to be the caller's job to take a token before each SDK call. About
twenty call sites never did. The worst was the hard-stop monitor: dispatched on
every 5 Hz focus tick, it fetched the full orderbook each time — ~280 requests a
minute against a 200/min broker limit. On 2026-09-23 and 2026-10-13 the account
was locked out within a minute of an entry, and the lockout also failed the
position reads that verify an exit, so exits were abandoned. A limiter that only
sees the traffic callers remember to report is not a limiter.

Metering at the transport makes "every request is counted" true by construction.
The invariant is pinned by tests/unit/test_transport.py: no module outside this
one may call the limiter, and no module may reach the broker API through a bare
`requests` call.

fyers-apiv3 3.1.7 calls the `requests` module functions directly
(`requests.post(URL, ...)`) and holds no Session, so the seam is the module
object it imported: `install()` replaces `fyersModel.requests` with the proxy.
Only the SDK's view of `requests` changes; the global module is untouched.
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Any, Callable, TypeVar
from urllib.parse import urlsplit

from shortcircuit.broker.rest_limiter import Priority, RateLimiter, rest_limiter

logger = logging.getLogger(__name__)

__all__ = [
    "ASYNC_CALL_TIMEOUT",
    "ASYNC_RETRIED_TIMEOUT",
    "BrokerBudgetExhausted",
    "DEFAULT_HTTP_TIMEOUT",
    "HTTP_MAX_RETRIES",
    "HTTP_READ_TIMEOUT",
    "MeteredRequests",
    "THROTTLE_COOLDOWN_S",
    "classify",
    "install",
    "metered_requests",
    "prepay",
    "prepay_async",
]

T = TypeVar("T")


# Timeouts
#
# (connect, read) seconds. The SDK issues requests with NO timeout, so a stalled
# socket blocks its caller forever — on 2026-07-29 that cost 41 scans. Kept tight
# because these budgets stack: with one retry an 8s read is a ~20s worst case on
# a path reconciliation polls every 6 seconds.
DEFAULT_HTTP_TIMEOUT: tuple[float, float] = (3.05, 8.0)

# Every asyncio wait_for around a REST call MUST outlast the HTTP read timeout,
# or it abandons a request still in flight and the caller cannot tell whether it
# succeeded. On 2026-08-07 a 5s wait_for over a 12s socket read lost the day's
# only entry. Derived from the transport so raising one raises the others.
HTTP_MAX_RETRIES = 1                                   # GET/HEAD only; POSTs never retry
HTTP_READ_TIMEOUT = DEFAULT_HTTP_TIMEOUT[1]            # 8.0s
ASYNC_CALL_TIMEOUT = HTTP_READ_TIMEOUT + 4.0           # 12.0s — single attempt (POST)
ASYNC_RETRIED_TIMEOUT = HTTP_READ_TIMEOUT * (HTTP_MAX_RETRIES + 1) + 4.0   # 20.0s

# How long NORMAL traffic stands down after the broker answers 429. Fyers says
# "retry after few mins"; 30s per rejection, extended by each further one, keeps
# the scanner from prolonging the lockout without idling it for minutes on a
# single stray rejection.
THROTTLE_COOLDOWN_S = 30.0


# Priority classification
#
# HIGH: anything that changes or reveals the state of orders and positions —
# placing and cancelling stops, and the reads that verify an exit. These draw on
# the limiter's reserved headroom and are never paused by a throttle cooldown.
# NORMAL: market data, funds, profile — everything a scan can wait for.
_HIGH_PRIORITY_PREFIXES: tuple[str, ...] = (
    "/orders",            # /orders (book), /orders/sync (place, modify, cancel)
    "/positions",         # read, exit, convert
    "/tradebook",
    "/multi-order",       # basket orders
    "/multileg",
    "/gtt",
    "/validate-authcode",  # login: rare, and nothing works until it completes
)

_API_PREFIX = "/api/v3"


def classify(method: str, url: str) -> Priority:
    """Rate-limit priority for a request, from its endpoint path."""
    path = urlsplit(url).path or "/"
    if path.startswith(_API_PREFIX):
        path = path[len(_API_PREFIX):] or "/"
    for prefix in _HIGH_PRIORITY_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return Priority.HIGH
    return Priority.NORMAL


# Throttle detection

_THROTTLE_BODY = re.compile(rb'"code"\s*:\s*-?429\b')


def _is_throttled(response: Any) -> bool:
    """True if the broker rejected this request for exceeding its rate limit."""
    if getattr(response, "status_code", None) == 429:
        return True
    # Fyers also reports throttling in the body with HTTP 200 on some endpoints.
    # The code field sits at the head of the document; never scan a history payload.
    try:
        head = response.content[:256]
    except Exception:
        return False
    return bool(head) and _THROTTLE_BODY.search(head) is not None


class BrokerBudgetExhausted(RuntimeError):
    """The daily request quota is spent; the request was not sent."""


# Prepaid tokens
#
# Metering inside the request means the wait for a token happens on whichever
# thread sends it. Where that thread runs under a deadline — an order placement
# inside a 12s wait_for, a history fetch behind an 8s future — the wait would eat
# the deadline and turn "the limiter shaped us" into "the request timed out".
# `prepay` takes the token on the calling thread, before the deadline starts, and
# hands the worker a one-request credit. Forgetting to prepay is safe: the
# request is simply metered where it is sent.

_credit = threading.local()


def _take_credit() -> bool:
    if getattr(_credit, "count", 0) > 0:
        _credit.count -= 1
        return True
    return False


def _prepaid(fn: Callable[..., T]) -> Callable[..., T]:
    def run(*args: Any, **kwargs: Any) -> T:
        _credit.count = 1
        try:
            return fn(*args, **kwargs)
        finally:
            _credit.count = 0      # an unspent credit never leaks to the next task

    run.__name__ = getattr(fn, "__name__", "prepaid")
    return run


def prepay(
    fn: Callable[..., T],
    priority: Priority = Priority.NORMAL,
    limiter: RateLimiter = rest_limiter,
) -> Callable[..., T]:
    """
    Take a token now, on this thread; return `fn` wrapped to spend it later.

    The wrapped callable covers exactly one request. Raises BrokerBudgetExhausted
    if the daily quota is spent.
    """
    if not limiter.acquire(priority=priority):
        raise BrokerBudgetExhausted("daily REST quota exhausted")
    return _prepaid(fn)


async def prepay_async(
    fn: Callable[..., T],
    priority: Priority = Priority.NORMAL,
    limiter: RateLimiter = rest_limiter,
) -> Callable[..., T]:
    """`prepay` for async callers: the wait runs off the event loop."""
    if not await limiter.acquire_async(priority=priority):
        raise BrokerBudgetExhausted("daily REST quota exhausted")
    return _prepaid(fn)


# The proxy

class MeteredRequests:
    """
    Stand-in for the `requests` module that meters and bounds every call.

    An explicit `timeout=` passed by a caller always wins. Attributes other than
    the HTTP verbs (exceptions, `codes`, `Session`, ...) pass straight through, so
    code that does `requests.HTTPError` against the proxy keeps working.
    """

    _VERBS = frozenset({"get", "post", "put", "delete", "patch", "head", "options"})

    def __init__(
        self,
        inner: Any,
        limiter: RateLimiter = rest_limiter,
        timeout: tuple[float, float] = DEFAULT_HTTP_TIMEOUT,
        cooldown_s: float = THROTTLE_COOLDOWN_S,
    ) -> None:
        self._inner = inner
        self._limiter = limiter
        self._timeout = timeout
        self._cooldown_s = cooldown_s

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr
        if name in self._VERBS:
            return self._wrap(attr, lambda args, kwargs: (name.upper(), _url_of(args, kwargs, 0)))
        if name == "request":
            return self._wrap(
                attr,
                lambda args, kwargs: (
                    str(args[0] if args else kwargs.get("method", "GET")).upper(),
                    _url_of(args, kwargs, 1),
                ),
            )
        return attr

    def _wrap(
        self,
        fn: Callable[..., Any],
        describe: Callable[[tuple, dict], tuple[str, str]],
    ) -> Callable[..., Any]:
        def metered(*args: Any, **kwargs: Any) -> Any:
            method, url = describe(args, kwargs)
            priority = classify(method, url)
            if not _take_credit() and not self._limiter.acquire(priority=priority):
                # Raised inside the SDK's own try-block, which converts it to its
                # standard {'s': 'error', 'code': -99} response.
                raise BrokerBudgetExhausted(f"daily REST quota exhausted; {method} {url} not sent")
            kwargs.setdefault("timeout", self._timeout)
            response = fn(*args, **kwargs)
            if _is_throttled(response):
                self._limiter.note_throttled(self._cooldown_s)
                logger.debug("[HTTP] 429 on %s %s (priority=%s)", method, url, priority.name)
            return response

        metered.__name__ = getattr(fn, "__name__", "metered")
        return metered


def _url_of(args: tuple, kwargs: dict, position: int) -> str:
    if len(args) > position:
        return str(args[position])
    return str(kwargs.get("url", ""))


# Installation

def _requests_module() -> Any:
    import requests

    return requests


# For the few broker calls the SDK does not wrap (the margin calculator). Using
# this rather than `requests` keeps them inside the same budget.
metered_requests = MeteredRequests(_requests_module())


def install(timeout: tuple[float, float] | None = None) -> bool:
    """
    Route every fyers-apiv3 REST call through `MeteredRequests`. Idempotent.

    Returns True when the SDK is metered. This is a hard requirement, not a
    nicety: an unbounded call inside the position monitor or the EOD square-off
    blocks a path that must not block.
    """
    try:
        from fyers_apiv3 import fyersModel
    except Exception as exc:  # pragma: no cover - SDK missing is a deploy error
        logger.error("[HTTP] fyers-apiv3 not importable: %s", exc)
        return False

    current = getattr(fyersModel, "requests", None)
    if isinstance(current, MeteredRequests):
        return True
    if current is None:
        logger.error("[HTTP] fyersModel has no `requests` attribute — cannot meter calls.")
        return False

    effective = timeout or DEFAULT_HTTP_TIMEOUT
    fyersModel.requests = MeteredRequests(current, timeout=effective)
    logger.info(
        "[HTTP] Fyers REST calls metered and bounded at connect=%.2fs read=%.1fs.",
        float(effective[0]), float(effective[1]),
    )
    return True
