"""
Tests for broker/transport.py — the single metered path to the Fyers REST API.

The failure this module exists to prevent: on 2026-09-23 and 2026-10-13 the bot
sent ~280 orderbook requests a minute (limit 200) from a 5 Hz hard-stop monitor
that never took a rate-limit token. Fyers locked the account out, and the
lockout failed the position reads that verify an exit, so exits were abandoned.

Two kinds of test live here:

  * Behaviour of the proxy, the prepaid credit and the throttle cooldown.
  * Structural invariants over the source tree, so the old failure mode — a call
    site that forgets to meter, or one that meters twice — cannot come back.
"""
from __future__ import annotations

import ast
import pathlib
from concurrent.futures import Future
from dataclasses import dataclass, field

import pytest

from shortcircuit.broker import transport
from shortcircuit.broker.rest_limiter import Priority, RateLimiter

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "shortcircuit"

API = "https://api-t1.fyers.in/api/v3"
DATA = "https://api-t1.fyers.in/data"


# Test doubles

@dataclass
class _Limiter:
    """Records every grant instead of enforcing windows."""

    grant: bool = True
    acquired: list[Priority] = field(default_factory=list)
    throttles: list[float] = field(default_factory=list)

    def acquire(self, priority=Priority.NORMAL, timeout=None) -> bool:
        self.acquired.append(Priority(priority))
        return self.grant

    async def acquire_async(self, priority=Priority.NORMAL, timeout=None) -> bool:
        return self.acquire(priority, timeout)

    def note_throttled(self, cooldown_s: float = 30.0) -> None:
        self.throttles.append(cooldown_s)


@dataclass
class _Response:
    status_code: int = 200
    content: bytes = b'{"s":"ok"}'


class _Requests:
    """Stand-in for the `requests` module."""

    codes = object()

    def __init__(self, response: _Response | None = None):
        self.response = response or _Response()
        self.calls: list[tuple[str, tuple, dict]] = []

    def _record(self, verb):
        def call(*args, **kwargs):
            self.calls.append((verb, args, kwargs))
            return self.response
        return call

    def __getattr__(self, name):
        if name in {"get", "post", "put", "delete", "patch", "head", "request"}:
            return self._record(name)
        raise AttributeError(name)


def _proxy(response: _Response | None = None, grant: bool = True):
    inner = _Requests(response)
    limiter = _Limiter(grant=grant)
    return transport.MeteredRequests(inner, limiter=limiter), inner, limiter


# Priority classification

@pytest.mark.parametrize("url", [
    f"{API}/orders/sync",          # place, modify, cancel
    f"{API}/orders",               # orderbook
    f"{API}/positions",
    f"{API}/tradebook",
    f"{API}/multi-order/sync",
    f"{API}/validate-authcode",
])
def test_order_and_position_endpoints_are_high_priority(url):
    assert transport.classify("GET", url) is Priority.HIGH


@pytest.mark.parametrize("url", [
    f"{DATA}/history",
    f"{DATA}/quotes",
    f"{DATA}/depth",
    f"{API}/funds",
    f"{API}/profile",
    f"{API}/multiorder/margin",    # not an order — a margin quote
])
def test_data_and_account_endpoints_are_normal_priority(url):
    assert transport.classify("GET", url) is Priority.NORMAL


def test_classification_does_not_match_on_a_prefix_of_a_word():
    """`/ordersfoo` is not `/orders`."""
    assert transport.classify("GET", f"{API}/ordersfoo") is Priority.NORMAL


# Metering

def test_every_request_is_charged_exactly_once_at_its_priority():
    proxy, inner, limiter = _proxy()
    proxy.post(f"{API}/orders/sync", data="{}")
    proxy.get(f"{DATA}/history")
    proxy.request("DELETE", f"{API}/orders/sync")
    assert limiter.acquired == [Priority.HIGH, Priority.NORMAL, Priority.HIGH]
    assert len(inner.calls) == 3


def test_url_passed_by_keyword_is_classified():
    proxy, _, limiter = _proxy()
    proxy.get(url=f"{API}/positions")
    assert limiter.acquired == [Priority.HIGH]


def test_a_default_timeout_is_applied_and_an_explicit_one_wins():
    proxy, inner, _ = _proxy()
    proxy.get(f"{DATA}/quotes")
    proxy.get(f"{DATA}/quotes", timeout=1.5)
    assert inner.calls[0][2]["timeout"] == transport.DEFAULT_HTTP_TIMEOUT
    assert inner.calls[1][2]["timeout"] == 1.5


def test_non_verb_attributes_pass_through_unmetered():
    proxy, inner, limiter = _proxy()
    assert proxy.codes is inner.codes
    assert limiter.acquired == []


def test_an_exhausted_daily_quota_refuses_without_sending():
    proxy, inner, _ = _proxy(grant=False)
    with pytest.raises(transport.BrokerBudgetExhausted):
        proxy.post(f"{API}/orders/sync")
    assert inner.calls == [], "a refused request must never reach the broker"


# Throttle feedback

def test_http_429_opens_a_cooldown():
    proxy, _, limiter = _proxy(_Response(status_code=429, content=b""))
    proxy.get(f"{API}/positions")
    assert limiter.throttles == [transport.THROTTLE_COOLDOWN_S]


def test_a_429_reported_in_the_body_opens_a_cooldown():
    body = b'{"code": -429, "message": "Request limit reached, retry after few mins", "s": "error"}'
    proxy, _, limiter = _proxy(_Response(status_code=200, content=body))
    proxy.get(f"{API}/funds")
    assert len(limiter.throttles) == 1


def test_an_ordinary_response_does_not_open_a_cooldown():
    proxy, _, limiter = _proxy(_Response(content=b'{"s":"ok","candles":[[1,429.0,2,3,4,5]]}'))
    proxy.get(f"{DATA}/history")
    assert limiter.throttles == [], "a price of 429 in a payload is not a throttle"


# Prepaid tokens

def test_a_prepaid_call_is_not_charged_a_second_time():
    proxy, _, limiter = _proxy()
    send = transport.prepay(lambda: proxy.post(f"{API}/orders/sync"), Priority.HIGH, limiter=limiter)
    assert limiter.acquired == [Priority.HIGH], "the token is taken at prepay time"
    send()
    assert limiter.acquired == [Priority.HIGH], "...and spent, not re-acquired, on send"


def test_a_prepaid_credit_covers_one_request_only():
    proxy, _, limiter = _proxy()

    def two_requests():
        proxy.get(f"{API}/orders")
        proxy.get(f"{API}/orders")

    transport.prepay(two_requests, Priority.HIGH, limiter=limiter)()
    assert limiter.acquired == [Priority.HIGH, Priority.HIGH]


def test_an_unspent_credit_does_not_leak_to_the_next_request():
    proxy, _, limiter = _proxy()
    transport.prepay(lambda: None, limiter=limiter)()      # spends nothing
    proxy.get(f"{DATA}/quotes")
    assert len(limiter.acquired) == 2, "the next request on this thread pays its own way"


def test_prepay_refuses_when_the_daily_quota_is_spent():
    with pytest.raises(transport.BrokerBudgetExhausted):
        transport.prepay(lambda: None, limiter=_Limiter(grant=False))


async def test_prepay_async_takes_the_token_before_returning():
    limiter = _Limiter()
    send = await transport.prepay_async(lambda: "sent", Priority.HIGH, limiter=limiter)
    assert limiter.acquired == [Priority.HIGH]
    assert send() == "sent"


# Installation

def test_install_meters_the_sdk_and_is_idempotent(monkeypatch):
    from fyers_apiv3 import fyersModel

    monkeypatch.setattr(fyersModel, "requests", _Requests())
    assert transport.install() is True
    first = fyersModel.requests
    assert isinstance(first, transport.MeteredRequests)
    assert transport.install() is True
    assert fyersModel.requests is first, "a second install must not wrap the proxy again"


# Throttle cooldown in the limiter

def _frozen(monkeypatch, start=1_000_000.0):
    state = {"now": start}
    monkeypatch.setattr("shortcircuit.broker.rest_limiter.time.time", lambda: state["now"])
    monkeypatch.setattr("shortcircuit.broker.rest_limiter.time.monotonic", lambda: state["now"])
    return state


def test_cooldown_pauses_normal_traffic_but_never_high(monkeypatch):
    clock = _frozen(monkeypatch)
    rl = RateLimiter(per_second=9, per_minute=190, per_day=1000)
    rl.note_throttled(30.0)
    assert rl.acquire(Priority.NORMAL, timeout=0) is False
    assert rl.acquire(Priority.HIGH, timeout=0) is True, "a stop must still be placeable"
    clock["now"] += 30.01
    assert rl.acquire(Priority.NORMAL, timeout=0) is True


def test_a_further_throttle_extends_rather_than_shortens_the_cooldown(monkeypatch):
    clock = _frozen(monkeypatch)
    rl = RateLimiter()
    rl.note_throttled(30.0)
    clock["now"] += 10
    rl.note_throttled(5.0)                        # would end sooner; must not
    assert rl.cooldown_remaining() == pytest.approx(20.0)
    assert rl.snapshot()["throttled_total"] == 2


# Hard-stop poll pacing (the root cause of the lockouts)

def _focus_engine(monkeypatch, start=100.0):
    from shortcircuit.execution import focus_engine as fe_mod

    clock = {"now": start}
    monkeypatch.setattr(fe_mod.time, "monotonic", lambda: clock["now"])
    engine = fe_mod.FocusEngine.__new__(fe_mod.FocusEngine)   # no broker, no threads
    return engine, clock, fe_mod.HARD_STOP_POLL_INTERVAL_S


def test_hard_stop_checks_are_paced(monkeypatch):
    engine, clock, interval = _focus_engine(monkeypatch)
    assert engine._hard_stop_poll_due() is True
    for _ in range(int(interval / 0.2) - 1):      # every 5 Hz tick inside the interval
        clock["now"] += 0.2
        assert engine._hard_stop_poll_due() is False
    clock["now"] += interval
    assert engine._hard_stop_poll_due() is True


def test_hard_stop_checks_never_overlap(monkeypatch):
    engine, clock, interval = _focus_engine(monkeypatch)
    engine._hard_stop_future = Future()           # still running
    clock["now"] += 10 * interval
    assert engine._hard_stop_poll_due() is False
    engine._hard_stop_future.set_result(False)
    assert engine._hard_stop_poll_due() is True


def test_hard_stop_polling_stays_well_inside_the_broker_minute_limit(monkeypatch):
    engine, clock, _ = _focus_engine(monkeypatch)
    dispatched = 0
    for _ in range(5 * 60):                       # one minute of 5 Hz ticks
        if engine._hard_stop_poll_due():
            dispatched += 1
        clock["now"] += 0.2
    assert dispatched <= 31, f"{dispatched} orderbook reads/min from one position"


# Structural invariants

_LIMITER_OWNERS = {
    SRC / "broker" / "rest_limiter.py",
    SRC / "broker" / "transport.py",
}

# Modules allowed to call `requests` directly. The scanner and the square-off
# schedule download public symbol masters from public.fyers.in, a static CDN
# that is not the rate-limited API.
_BARE_REQUESTS_ALLOWED = {
    SRC / "broker" / "transport.py",
    SRC / "marketdata" / "scanner.py",
    SRC / "eod" / "squareoff.py",
    SRC / "backtest" / "history.py",
}

_HTTP_VERBS = {"get", "post", "put", "delete", "patch", "head", "request"}


def _python_sources():
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _calls(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            yield node


def test_no_call_site_takes_limiter_tokens_itself():
    """
    The transport charges every request. A call site that also acquires a token
    charges that request twice and halves the real budget. Use transport.prepay
    when the wait has to happen outside a deadline.
    """
    offenders = []
    for path in _python_sources():
        if path in _LIMITER_OWNERS:
            continue
        for call in _calls(ast.parse(path.read_text(encoding="utf-8"))):
            if call.func.attr in {"acquire", "acquire_async"}:
                owner = call.func.value
                if isinstance(owner, ast.Name) and owner.id == "rest_limiter":
                    offenders.append(f"{path.relative_to(SRC)}:{call.lineno}")
    assert offenders == [], f"explicit limiter acquires outside the transport: {offenders}"


def test_nothing_reaches_the_network_through_bare_requests():
    """Everything that talks to the broker API must go through the metered proxy."""
    offenders = []
    for path in _python_sources():
        if path in _BARE_REQUESTS_ALLOWED:
            continue
        for call in _calls(ast.parse(path.read_text(encoding="utf-8"))):
            owner = call.func.value
            if (isinstance(owner, ast.Name) and owner.id == "requests"
                    and call.func.attr in _HTTP_VERBS):
                offenders.append(f"{path.relative_to(SRC)}:{call.lineno}")
    assert offenders == [], f"bare requests calls bypass the limiter: {offenders}"
