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
        "4,NIPPON INDIA ETF GOLD BEES,9,1,0.01,X,s,d,0,NSE:GOLDBEES-EQ,0",
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
    args = cli.parse(["--from", d, "--to", d, "--symbols", "NSE:A-EQ", "--slippage", "0", "--cost", "0", "--jobs", "1"])
    frame, summary, signals = cli.run(args, _Client(daily=daily, minute=minute))
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


# ── circuit inference, the signal record, the dashboard ──────────────────────

from shortcircuit.backtest.replay import infer_upper_circuit  # noqa: E402


@pytest.mark.parametrize("high,fno,uc", [
    (110.0, False, 110.0),      # stopped dead at +10%: banded at 10
    (113.0, False, 120.0),      # went past +10%: banded at 20
    (125.0, False, 0.0),        # past +20%: no band applies
    (105.0, False, 0.0),        # never near a band that matters
    (113.0, True, 0.0),         # F&O: no fixed band
    (113.0, None, 120.0),       # F&O list unknown: treated as banded
])
def test_the_upper_circuit_is_inferred_from_the_day(high, fno, uc):
    assert infer_upper_circuit(100.0, high, fno) == uc


def test_the_circuit_guard_sees_the_band_and_marks_a_hitter():
    closes = [104 + 0.4 * i for i in range(20)] + [113.0] * 20
    highs = [c + 0.05 for c in closes]
    highs[22] = 120.0                                   # touched the 20% circuit at 09:37
    s = _Once(at="99:99", trigger=1, stop_ref=200)
    replay_day("NSE:X-EQ", _day(closes, highs=highs), PREV, [s], Costs(0, 0), upper_circuit=120.0)
    by_time = {c.now.strftime("%H:%M"): c for c in s.seen}
    assert by_time["09:36"].upper_circuit == 120.0 and not by_time["09:36"].is_circuit_hitter
    assert by_time["09:38"].is_circuit_hitter, "marked once the session high reached the band"


def test_every_signal_is_recorded_with_its_outcome_and_the_stop_path():
    up = [104 + 0.4 * i for i in range(20)]
    fall = [up[-1] - 1.0 * (i + 1) for i in range(6)]
    s = _Once(at="09:35", trigger=111.0, stop_ref=112.5)
    res = _run(up + fall + [108.0, 110.0, 112.0, 113.0], s)
    assert [e["outcome"] for e in res.events] == ["ENTERED"]
    assert res.events[0]["time"] == "09:35" and res.events[0]["trigger"] == 111.0
    (trade,) = res.trades
    assert trade.trigger == 111.0
    assert [p for _, p in trade.stop_path] == [trade.stop, trade.entry], "initial stop, then to cost"
    row = trade.row(Costs(0, 0))
    import json
    assert [leg[2] for leg in json.loads(row["legs_json"])] == ["RSI_SCALE_OUT", "COST_STOP"]
    assert len(json.loads(row["stop_path_json"])) == 2

    s2 = _Once(at="09:35", trigger=100.0, stop_ref=150.0)
    assert [e["outcome"] for e in _run([112.0] * 60, s2).events] == ["EXPIRED"]


def test_the_command_line_writes_the_signals_and_the_dashboard(tmp_path, monkeypatch):
    import json
    from shortcircuit.backtest import __main__ as cli
    from shortcircuit.backtest import report
    from shortcircuit.eod import squareoff
    monkeypatch.setenv(run_mode.ENV, "live")
    monkeypatch.setattr(squareoff.registry, "refresh", lambda *a, **k: False)
    monkeypatch.setattr(hist, "CACHE_DIR", tmp_path / "cache")
    d = "2026-11-24"
    daily = {"NSE:A-EQ": [[_epoch("2026-11-21"), 100, 101, 99, 100, 5e5],
                          [_epoch(d), 101, 113, 100, 112, 9e5]]}
    bars = _day(list(RUN_UP))
    minute = {"NSE:A-EQ": bars[["epoch", "open", "high", "low", "close", "volume"]].values.tolist()}
    client = _Client(daily=daily, minute=minute)
    monkeypatch.setattr(cli, "History", lambda c: hist.History(c, cache_dir=tmp_path / "cache"))
    monkeypatch.setattr(cli, "connect", lambda: client)
    # A hostile feature string must not be able to end the page's data script.
    strat = _Once(at="09:35", trigger=112.0, stop_ref=113.0)
    real_eval = strat.evaluate

    def evaluate(ctx):
        sig = real_eval(ctx)
        if sig is not None:
            sig.ml_features = {"note": "</script><script>alert(1)</script>", "score": 1.5}
        return sig
    strat.evaluate = evaluate
    monkeypatch.setattr(cli.registry, "load", lambda names=None: [strat])
    out = tmp_path / "run.csv"
    assert cli.main(["--from", d, "--to", d, "--symbols", "NSE:A-EQ", "--slippage", "0",
                     "--cost", "0", "--jobs", "1", "--out", str(out)]) == 0

    signals = pd.read_csv(tmp_path / "run_signals.csv")
    assert signals["outcome"].tolist() == ["ENTERED"]
    page = (tmp_path / "run.html").read_text(encoding="utf-8")
    for url, sri in report.LIBS.values():
        assert f'src="{url}" integrity="{sri}"' in page, "libraries are pinned by hash"
    assert "{{" not in page, "every placeholder is filled"
    assert "</script><script>alert" not in page
    blob = page.split('<script id="data" type="application/json">', 1)[1].split("</script>", 1)[0]
    data = json.loads(blob)
    (t,) = data["trades"]
    assert t["reason"] == "SL_HIT" and t["features"]["note"].startswith("</script>")
    assert t["key"] in data["bars"], "the trade's minute bars are embedded for the replay"
    assert len(data["bars"][t["key"]]["t"]) == len(bars)
    assert t["entry_t"] == _epoch(d, 9, 35) + report.IST_OFFSET_S, "chart time reads as IST"


def test_the_dashboard_builds_for_a_run_with_no_trades(tmp_path):
    from shortcircuit.backtest import report
    csv = tmp_path / "empty.csv"
    csv.write_text("")
    page = (tmp_path / "empty.html")
    assert report.build(str(csv), cache_dir=tmp_path) == str(page)
    assert '"trades":[]' in page.read_text(encoding="utf-8")


def test_the_page_rsi_matches_the_engine_rsi():
    from shortcircuit.backtest import report
    from shortcircuit.strategy import features as F
    closes = pd.Series([100 + (i % 7) - 0.3 * i for i in range(60)], dtype=float)
    series = report._rsi(closes)
    assert series[:14] == [None] * 14
    for k in (20, 40, 59):
        assert series[k] == pytest.approx(F.compute_rsi_wilder(closes.iloc[: k + 1]), abs=0.01)


def test_parallel_replay_matches_the_serial_one(tmp_path, monkeypatch):
    """Worker processes load the strategy by name and get the parent's F&O list;
    the trades they find must be exactly the serial run's."""
    from shortcircuit.backtest import __main__ as cli
    from shortcircuit.eod import squareoff
    monkeypatch.setenv(run_mode.ENV, "live")
    monkeypatch.setattr(squareoff.registry, "refresh", lambda *a, **k: False)
    rng = __import__("random").Random(3)
    daily, minute, sessions = {}, {}, ["2026-11-23", "2026-11-24"]
    for k in range(3):
        sym = f"NSE:P{k}-EQ"
        daily[sym] = [[_epoch("2026-11-20"), 100, 101, 99, 100, 5e5]] + \
                     [[_epoch(d), 101, 116, 100, 100, 9e5] for d in sessions]
        rows = []
        for d in sessions:
            px = 103.0
            for i in range(375):
                o = px
                px = max(50.0, px + (0.06 if i < 90 else -0.03) + rng.uniform(-0.25, 0.25))
                rows.append([_epoch(d) + 60 * i, o, max(o, px) + 0.1, min(o, px) - 0.1, px,
                             900_000 if i == 88 else 40_000])
        minute[sym] = rows

    def run(jobs):
        monkeypatch.setattr(cli, "History", lambda c: hist.History(c, cache_dir=tmp_path / str(jobs)))
        args = cli.parse(["--from", sessions[0], "--to", sessions[-1], "--symbols", ",".join(daily),
                          "--strategies", "example_vwap_stretch_fade", "--jobs", str(jobs)])
        frame, summary, _ = cli.run(args, _Client(daily=daily, minute=minute))
        return frame, summary

    serial, s1 = run(1)
    parallel, s2 = run(2)
    assert s1["gainer_days"] == s2["gainer_days"] == 6
    assert s1["signals"] == s2["signals"] and s1["signals"] > 0, "the fixture must produce signals"
    pd.testing.assert_frame_equal(serial.reset_index(drop=True), parallel.reset_index(drop=True))


def test_minute_bars_are_fetched_per_100_day_window_and_cached_per_day(tmp_path):
    days = [dt.date(2026, 1, 5), dt.date(2026, 1, 6), dt.date(2026, 3, 30), dt.date(2026, 6, 1)]
    rows = [[_epoch(d.isoformat(), 10, 0), 100, 101, 99, 100, 10] for d in days]
    rows.append([_epoch("2026-01-07", 10, 0), 1, 1, 1, 1, 1])          # not a wanted day

    class Ranged(_Client):
        def history(self, data):
            self.calls.append((data["symbol"], data["range_from"], data["range_to"]))
            lo, hi = data["range_from"], data["range_to"]
            return {"s": "ok", "candles": [r for r in rows if lo <= pd.Timestamp(r[0], unit="s", tz="UTC")
                                           .tz_convert(IST).date().isoformat() <= hi]}

    client = Ranged()
    h = hist.History(client, cache_dir=tmp_path)
    assert hist.prefetch_minutes(h, [("NSE:A-EQ", d) for d in days]) == 2
    assert client.calls == [("NSE:A-EQ", "2026-01-05", "2026-03-30"), ("NSE:A-EQ", "2026-06-01", "2026-06-01")]
    for d in days:
        df = h.minute("NSE:A-EQ", d)
        assert len(df) == 1 and df["datetime"].iloc[0].date() == d
    assert not h._path("minute", "NSE:A-EQ", "2026-01-07").exists(), "only wanted days are written"
    assert hist.prefetch_minutes(h, [("NSE:A-EQ", d) for d in days]) == 0, "cached days cost nothing"



def test_a_throttled_history_request_is_retried_not_dropped(tmp_path):
    class Throttled(_Client):
        def __init__(self, fails):
            super().__init__(daily={"NSE:A-EQ": [[_epoch("2026-11-23"), 1, 2, 1, 2, 9]]})
            self.fails = fails

        def history(self, data):
            if self.fails:
                self.fails -= 1
                return {"s": "error", "code": -360, "message": "You've reached your API request limit."}
            return super().history(data)

    waits = []
    h = hist.History(Throttled(2), cache_dir=tmp_path, sleep=waits.append)
    assert len(h.daily("NSE:A-EQ", dt.date(2026, 11, 23), dt.date(2026, 11, 23))) == 1
    assert waits == list(hist.RETRY_WAITS_S[:2]) and h.failures == []

    h2 = hist.History(Throttled(99), cache_dir=tmp_path / "b", sleep=waits.append)
    assert hist.gainer_days(h2, ["NSE:A-EQ"], dt.date(2026, 11, 23), dt.date(2026, 11, 23),
                            10, 40, 1) == []
    assert len(h2.failures) == 1, "a request that never succeeds is counted, not lost"


def test_a_long_run_charts_the_recent_best_and_worst_trades_only():
    from shortcircuit.backtest import report
    n = 50
    trades = pd.DataFrame({"date": [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(n)],
                           "entry_time": ["10:00"] * n, "net_pct": [float(i % 17 - 8) for i in range(n)]})
    keep = report._charted(trades, recent=5, extremes=3)
    assert set(range(45, 50)) <= keep, "the most recent"
    assert trades["net_pct"].idxmax() in keep and trades["net_pct"].idxmin() in keep
    assert len(keep) <= 5 + 2 * 3
    assert report._charted(trades.head(8), recent=5, extremes=3) == set(range(8))
