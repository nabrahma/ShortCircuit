"""
Data-only mode and the backtester.

Data-only (SHORTCIRCUIT_MODE=data_only) must be unable to place, modify or
cancel an order, by construction: the transport refuses every order write
before it is sent, whatever code path asks. Reads, login and the margin
calculator still work, which is what makes the mode useful for testing from a
connection without a static IP.

The backtester replays a day minute by minute through the live analyzer's
feature code and the live exits' primitives. Pinned here: the strategy sees
nothing from inside or after the minute it is evaluated in; the validation
gate's trigger, invalidation and expiry; and each exit path.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import types
from types import SimpleNamespace

import pandas as pd
import pytest
import requests

from shortcircuit.backtest import history as hist
from shortcircuit.backtest.replay import Costs, replay_day, summarise
from shortcircuit.broker import transport
from shortcircuit.broker.rest_limiter import Priority
from shortcircuit.runtime import mode as run_mode
from shortcircuit.strategy.sdk import (
    Direction, ExitPlan, ExitProfile, Signal, StagedRsiExit, Strategy,
)

API = "https://api-t1.fyers.in/api/v3"


@pytest.fixture
def data_only(monkeypatch):
    monkeypatch.setenv(run_mode.ENV, "data_only")


# ── the mode ─────────────────────────────────────────────────────────────────

def test_mode_defaults_to_live_and_rejects_a_typo(monkeypatch):
    monkeypatch.delenv(run_mode.ENV, raising=False)
    assert run_mode.current() is run_mode.Mode.LIVE
    monkeypatch.setenv(run_mode.ENV, "Data-Only")
    assert run_mode.is_data_only()
    monkeypatch.setenv(run_mode.ENV, "paper")
    with pytest.raises(ValueError):
        run_mode.current()


@pytest.mark.parametrize("method,path,write", [
    ("POST", "/orders/sync", True),           # place
    ("PATCH", "/orders/sync", True),          # modify
    ("DELETE", "/orders/sync", True),         # cancel
    ("POST", "/multi-order/sync", True),
    ("POST", "/multileg/orders/sync", True),
    ("POST", "/gtt/orders/sync", True),
    ("DELETE", "/positions", True),           # exit
    ("PUT", "/positions", True),              # convert
    ("GET", "/orders", False),                # orderbook read
    ("GET", "/positions", False),
    ("GET", "/tradebook", False),
    ("POST", "/multiorder/margin", False),    # margin calculator
    ("POST", "/validate-authcode", False),    # login
])
def test_order_writes_are_recognised(method, path, write):
    assert transport.is_order_write(method, API + path) is write


@pytest.mark.parametrize("data", [
    "https://api-t1.fyers.in/data/history", "https://api-t1.fyers.in/data/quotes",
    "https://api-t1.fyers.in/data/depth",
])
def test_market_data_is_never_an_order_write(data):
    assert not transport.is_order_write("GET", data)


class _Limiter:
    def __init__(self):
        self.acquired = []

    def acquire(self, priority=Priority.NORMAL):
        self.acquired.append(priority)
        return True

    def note_throttled(self, cooldown_s=30.0):
        pass


class _Inner:
    def __init__(self):
        self.sent = []

    def _send(self, method):
        def send(*args, **kwargs):
            self.sent.append((method, args[0] if args else kwargs.get("url")))
            return SimpleNamespace(status_code=200, content=b'{"s":"ok"}')
        return send

    def __getattr__(self, name):
        return self._send(name.upper())


def test_data_only_refuses_writes_before_sending_or_spending_a_token(data_only):
    inner, lim = _Inner(), _Limiter()
    proxy = transport.MeteredRequests(inner, limiter=lim)
    with pytest.raises(run_mode.TradingDisabled):
        proxy.post(API + "/orders/sync", json={})
    with pytest.raises(run_mode.TradingDisabled):
        proxy.request("DELETE", API + "/positions")
    assert inner.sent == [] and lim.acquired == []


def test_data_only_still_reads_and_logs_in(data_only):
    inner = _Inner()
    proxy = transport.MeteredRequests(inner, limiter=_Limiter())
    proxy.get(API + "/orders")
    proxy.get("https://api-t1.fyers.in/data/history")
    proxy.post(API + "/validate-authcode", json={})
    proxy.post(API + "/multiorder/margin", json={})
    assert len(inner.sent) == 4


def test_live_mode_sends_writes(monkeypatch):
    monkeypatch.delenv(run_mode.ENV, raising=False)
    inner = _Inner()
    transport.MeteredRequests(inner, limiter=_Limiter()).post(API + "/orders/sync", json={})
    assert inner.sent == [("POST", API + "/orders/sync")]


def test_the_sdks_own_place_order_never_leaves_the_process(data_only, monkeypatch, tmp_path):
    """End to end through fyers_apiv3: the SDK's order calls hit the guard."""
    import logging
    from fyers_apiv3 import fyersModel

    inner = _Inner()
    fake = types.SimpleNamespace(HTTPError=requests.HTTPError,
                                 **{v: inner._send(v.upper()) for v in ("get", "post", "patch", "delete", "put")})
    monkeypatch.setattr(fyersModel, "requests", transport.MeteredRequests(fake, limiter=_Limiter()))
    model = fyersModel.FyersModel(client_id="TEST-100", token="t", log_path=str(tmp_path))
    try:
        reply = model.place_order(data={"symbol": "NSE:X-EQ", "qty": 1, "side": -1, "type": 2})
        model.modify_order(data={"id": "1", "qty": 1})
        model.cancel_order(data={"id": "1"})
        model.exit_positions(data={"id": "NSE:X-EQ-INTRADAY"})
        model.orderbook()
    finally:
        for name in ("FyersAPI", "FyersAPIRequest"):
            for h in list(logging.getLogger(name).handlers):
                if str(tmp_path) in str(getattr(h, "baseFilename", "")):
                    h.close()
                    logging.getLogger(name).removeHandler(h)
    assert reply.get("s") != "ok"
    assert inner.sent == [("GET", "https://api-t1.fyers.in/api/v3/orders")], \
        "only the orderbook read may reach the network"


def test_entry_says_what_it_would_do_and_does_nothing(data_only, caplog):
    from shortcircuit.execution.order_manager import OrderManager
    fake = SimpleNamespace(_get_lock=lambda s: asyncio.Lock(), telegram=None)
    sig = {"symbol": "NSE:X-EQ", "ltp": 120.0, "strategy": "s", "trigger_price": 119.5, "stop_pct": 5.0}
    with caplog.at_level("WARNING"):
        assert asyncio.run(OrderManager.enter_position(fake, sig)) is None
    assert "would SHORT NSE:X-EQ" in caplog.text


def test_reconciliation_does_not_adopt_in_data_only(data_only):
    from shortcircuit.state.reconciliation import ReconciliationEngine
    fake = SimpleNamespace()                    # no broker: any use would raise
    assert asyncio.run(ReconciliationEngine.adopt_orphan(fake, {"symbol": "NSE:X-EQ", "qty": -5})) is None


# ── the backtester ───────────────────────────────────────────────────────────

IST = "Asia/Kolkata"


def _day(closes, *, vol=60_000, highs=None, lows=None, opens=None, start="09:15"):
    t0 = pd.Timestamp(f"2026-11-24 {start}", tz=IST)
    n = len(closes)
    opens = opens or [closes[max(0, i - 1)] for i in range(n)]
    highs = highs or [max(o, c) + 0.05 for o, c in zip(opens, closes, strict=True)]
    lows = lows or [min(o, c) - 0.05 for o, c in zip(opens, closes, strict=True)]
    dts = [t0 + pd.Timedelta(minutes=i) for i in range(n)]
    return pd.DataFrame({
        "epoch": [int(d.timestamp()) for d in dts], "datetime": dts, "open": opens,
        "high": highs, "low": lows, "close": closes, "volume": [vol] * n,
    })


class _Once(Strategy):
    """Signals once at `at`, with a fixed trigger and plan; records what it saw."""
    name = "once"
    direction = Direction.SHORT
    requires_htf_confirmation = False

    def __init__(self, at, trigger, stop_ref, plan=None):
        self.at, self.trigger, self.stop_ref = at, trigger, stop_ref
        self.plan = plan or ExitPlan(profile=ExitProfile.STAGED_RSI, stop_pct=5.0,
                                     staged_rsi=StagedRsiExit(bar_minutes=1, min_bars=15))
        self.seen = []

    def evaluate(self, ctx):
        self.seen.append(ctx)
        if ctx.now.strftime("%H:%M") != self.at:
            return None
        return Signal(pattern="T", stop_reference=self.stop_ref, trigger_price=self.trigger,
                      exit=self.plan)


PREV = 100.0

# A steady climb (RSI pinned near 100, so no scale-out), then a squeeze to +20%.
RUN_UP = [104 + 0.4 * i for i in range(20)] + [112.0, 112.5, 114.0, 116.0, 118.0, 120.0]
STOP_AT = round(-(-111.6 * 1.05 // 0.05) * 0.05, 2)       # 5% over the 111.6 fill, tick-ceiled


def _run(closes, strat, **kw):
    return replay_day("NSE:X-EQ", _day(closes, **kw), PREV, [strat], Costs(0.0, 0.0))


def test_the_strategy_never_sees_the_minute_it_is_judged_in():
    closes = [112 + 0.01 * i for i in range(60)]
    s = _Once(at="99:99", trigger=1, stop_ref=200)
    _run(closes, s)
    ctx = s.seen[0]
    day = _day(closes)
    i = int((day["datetime"] == pd.Timestamp(ctx.now).tz_localize(None).tz_localize(IST)).idxmax()) \
        if pd.Timestamp(ctx.now).tzinfo is None else int((day["datetime"] == ctx.now).idxmax())
    assert ctx.ltp == pytest.approx(day["open"].iloc[i]), "last price is the minute's open"
    forming = ctx.df.iloc[-1]
    assert forming["high"] == forming["low"] == forming["open"] and forming["volume"] == 0
    assert len(ctx.df) == i + 1


def test_no_signals_before_0930_or_outside_the_scanner_band():
    s = _Once(at="99:99", trigger=1, stop_ref=200)
    _run([112.0] * 40, s)
    assert min(c.now.time() for c in s.seen) >= dt.time(9, 30)
    s2 = _Once(at="99:99", trigger=1, stop_ref=200)
    _run([125.0] * 40, s2)                          # +25%: above the 18% ceiling
    assert s2.seen == []


def test_a_stop_is_hit_at_the_stop():
    closes = list(RUN_UP)
    s = _Once(at="09:35", trigger=112.0, stop_ref=113.0)
    res = _run(closes, s)
    (trade,) = res.trades
    assert trade.entry == pytest.approx(111.6), "opened through the trigger: filled at the open"
    assert trade.exit_reason == "SL_HIT"
    assert trade.legs[-1][1] == pytest.approx(STOP_AT)
    assert trade.gross_pct == pytest.approx((111.6 - STOP_AT) / 111.6 * 100)


def test_scale_out_then_the_rest_stops_at_cost():
    up = [104 + 0.4 * i for i in range(20)]                    # warms RSI near 100
    fall = [up[-1] - 1.0 * (i + 1) for i in range(6)]          # cools it to ~47
    back = [108.0, 110.0, 112.0, 113.0]
    s = _Once(at="09:35", trigger=111.0, stop_ref=112.5)
    res = _run(up + fall + back, s)
    (trade,) = res.trades
    reasons = [leg[2] for leg in trade.legs]
    assert reasons == ["RSI_SCALE_OUT", "COST_STOP"]
    assert trade.legs[0][0] == pytest.approx(0.5)
    assert trade.legs[1][1] == pytest.approx(trade.entry)


def test_an_unrelated_profile_runs_to_the_square_off():
    closes = [112.0] * 20 + [111.0] + [110.0] * 340
    plan = ExitPlan(profile=ExitProfile.EOD_HOLD)
    s = _Once(at="09:35", trigger=111.5, stop_ref=113.0, plan=plan)
    (trade,) = _run(closes, s).trades
    assert trade.exit_reason == "EOD_SQUAREOFF" and trade.exit_model == "stop_or_squareoff"
    assert trade.legs[-1][3].time() == dt.time(15, 10)


def test_the_gate_invalidates_above_the_setup_high_and_expires_after_15_minutes():
    flat = [112.0] * 20
    s = _Once(at="09:35", trigger=111.0, stop_ref=112.5)
    res = _run(flat + [113.0] * 10, s, highs=[112.1] * 20 + [113.2] * 10)
    assert res.signals == 1 and res.invalidated == 1 and res.trades == []
    s2 = _Once(at="09:35", trigger=100.0, stop_ref=150.0)
    res2 = _run(flat * 3, s2)
    assert res2.expired == 1 and res2.trades == []


def test_costs_come_off_every_trade():
    closes = list(RUN_UP)
    s = _Once(at="09:35", trigger=112.0, stop_ref=113.0)
    res = replay_day("NSE:X-EQ", _day(closes), PREV, [s], Costs(slippage_pct=0.1, cost_pct=0.11))
    (trade,) = res.trades
    assert trade.entry == pytest.approx(111.6 * 0.999)        # short fill moved down
    assert trade.net_pct(Costs(0.1, 0.11)) == pytest.approx(trade.gross_pct - 0.11)
    summary = summarise(res.trades, Costs(0.1, 0.11))
    assert summary["trades"] == 1 and summary["win_rate_pct"] == 0.0


# ── history and the universe ─────────────────────────────────────────────────

class _Client:
    def __init__(self, daily=None, minute=None):
        self.daily, self.minute, self.calls = daily or {}, minute or {}, []

    def history(self, data):
        self.calls.append((data["symbol"], data["resolution"]))
        src = self.daily if data["resolution"] == "D" else self.minute
        return {"s": "ok", "candles": src.get(data["symbol"], [])}


def _epoch(d, hh=9, mm=15):
    return int(pd.Timestamp(f"{d} {hh:02d}:{mm:02d}", tz=IST).timestamp())


def test_gainer_days_use_the_previous_close_and_the_scanner_floors(tmp_path):
    daily = {
        "NSE:A-EQ": [[_epoch("2026-11-20"), 100, 101, 99, 100, 5e5],
                     [_epoch("2026-11-23"), 101, 112, 100, 108, 9e5]],    # +12% high
        "NSE:B-EQ": [[_epoch("2026-11-20"), 30, 31, 29, 30, 5e5],
                     [_epoch("2026-11-23"), 31, 36, 30, 35, 9e5]],        # under ₹40
        "NSE:C-EQ": [[_epoch("2026-11-20"), 100, 101, 99, 100, 5e5],
                     [_epoch("2026-11-23"), 100, 104, 99, 103, 9e5]],     # only +4%
    }
    h = hist.History(_Client(daily=daily), cache_dir=tmp_path)
    days = hist.gainer_days(h, daily, dt.date(2026, 11, 23), dt.date(2026, 11, 23), 10.0, 40.0, 111111)
    assert days == [("NSE:A-EQ", dt.date(2026, 11, 23), 100.0)]


def test_history_is_cached_and_filtered_to_the_session(tmp_path):
    minute = {"NSE:A-EQ": [[_epoch("2026-11-23", 9, 15) - 86400, 1, 1, 1, 1, 1],   # day before
                           [_epoch("2026-11-23", 9, 15), 100, 101, 99, 100, 10],
                           [_epoch("2026-11-23", 15, 30), 100, 101, 99, 100, 10]]}
    client = _Client(minute=minute)
    h = hist.History(client, cache_dir=tmp_path)
    df = h.minute("NSE:A-EQ", dt.date(2026, 11, 23))
    assert len(df) == 1 and df["datetime"].iloc[0].strftime("%H:%M") == "09:15"
    h.minute("NSE:A-EQ", dt.date(2026, 11, 23))
    assert client.calls == [("NSE:A-EQ", "1")], "second read comes from the cache"


def test_the_equity_list_is_eq_series_only():
    text = "\n".join([
        "1,A,0,1,0.05,X,s,d,0,NSE:AAA-EQ,0",
        "2,B,0,1,0.05,X,s,d,0,NSE:BBB-BE,0",
        "3,C,0,1,0.05,X,s,d,0,NSE:NIFTY50-INDEX,0",
    ])
    assert hist.nse_equities(text) == ["NSE:AAA-EQ"]


def test_the_command_line_runs_end_to_end_without_the_network(tmp_path, monkeypatch):
    from shortcircuit.backtest import __main__ as cli
    from shortcircuit.eod import squareoff
    # setenv, not delenv: delenv of an unset variable records nothing to undo,
    # and run() sets it — the mode would leak into every later test.
    monkeypatch.setenv(run_mode.ENV, "live")
    monkeypatch.setattr(squareoff.registry, "refresh", lambda *a, **k: False)
    monkeypatch.setattr(hist, "CACHE_DIR", tmp_path)
    d = "2026-11-24"
    daily = {"NSE:A-EQ": [[_epoch("2026-11-21"), 100, 101, 99, 100, 5e5],
                          [_epoch(d), 101, 113, 100, 112, 9e5]]}
    bars = _day(list(RUN_UP))
    minute = {"NSE:A-EQ": bars[["epoch", "open", "high", "low", "close", "volume"]].values.tolist()}
    monkeypatch.setattr(cli, "History", lambda client: hist.History(client, cache_dir=tmp_path))
    strat = _Once(at="09:35", trigger=112.0, stop_ref=113.0)
    monkeypatch.setattr(cli.registry, "load", lambda names=None: [strat])
    args = cli.parse(["--from", d, "--to", d, "--symbols", "NSE:A-EQ", "--slippage", "0", "--cost", "0"])
    frame, summary = cli.run(args, _Client(daily=daily, minute=minute))
    assert summary["gainer_days"] == 1 and summary["trades"] == 1
    assert frame["exit_reason"].tolist() == ["SL_HIT"]
    assert run_mode.is_data_only(), "the backtest forces data-only mode"


# ── a strategy's own ML features ─────────────────────────────────────────────

def test_strategy_features_reach_the_ml_file_and_never_overwrite_engine_fields(tmp_path):
    import numpy as np
    from shortcircuit.observability.ml_logger import MLDataLogger

    log = MLDataLogger(data_dir=str(tmp_path), session_date="2026-11-24")
    log.log_observation("NSE:X-EQ", 120.0, {
        "strategy": "s", "gain_pct": 12.0,
        "climax_ratio": np.float64(14.5), "bars_since": np.int64(7),
        "levels": {"a": 1}, "ltp": 999.0,              # 'ltp' is an engine field
    })
    row = log._buffer[-1]
    assert row["climax_ratio"] == 14.5 and row["bars_since"] == 7
    assert isinstance(row["bars_since"], int), "numpy scalars are unwrapped"
    assert row["levels"] == '{"a": 1}'
    assert row["ltp"] == 120.0, "an engine field is never overwritten by a feature"
