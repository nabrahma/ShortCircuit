"""
Long and short strategies, side by side.

Every strategy declares its direction and every signal carries it to the order
path, so the engine can run a short and a long at once. config.TRADE_DIRECTION
only filters ('BOTH', 'SHORT', 'LONG'). What is pinned here:

  * the rules of direction (execution/directions.py): the filter, the order
    sides, and which movers each strategy is offered;
  * G9 reads a long as the mirror of a short;
  * the order path buys a LONG signal and rests a SELL stop under it, whatever
    the global filter says, and still sells a SHORT one;
  * the validation gate buys a break ABOVE a long's trigger and abandons it on a
    drop below its setup low; a position keeps its own side once open;
  * the RSI exits and the trail are mirrored for a long;
  * the scanner watches losers only when a strategy wants them;
  * the backtest is direction-symmetric: a long on a price path reflected
    through 2 x the previous close trades exactly the reflection of the short.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time
from dataclasses import asdict, dataclass
from types import SimpleNamespace

import pandas as pd
import pytest

from shortcircuit import config
from shortcircuit.backtest.replay import Costs, infer_lower_circuit, replay_day
from shortcircuit.execution import directions
from shortcircuit.execution import focus_engine as fe
from shortcircuit.execution.order_manager import OrderManager
from shortcircuit.strategy.htf_confluence import HTFConfluence
from shortcircuit.strategy.sdk import (
    Direction, ExitPlan, ExitProfile, RsiCover, Signal, StagedRsiExit, Strategy, Universe,
)


@pytest.fixture
def mode(monkeypatch):
    def set_(value):
        monkeypatch.setattr(config, "TRADE_DIRECTION", value)
    set_("BOTH")
    return set_


def _strat(direction, universe=Universe.AUTO):
    return SimpleNamespace(direction=direction, universe=universe)


# ── the rules of direction ───────────────────────────────────────────────────

@pytest.mark.parametrize("value, expected", [
    ("BOTH", "BOTH"), ("short", "SHORT"), ("LONG", "LONG"), ("", "BOTH"), ("SIDEWAYS", "BOTH"),
])
def test_an_unknown_filter_reads_as_both(mode, value, expected):
    mode(value)
    assert directions.mode() == expected


@pytest.mark.parametrize("value, short_ok, long_ok", [
    ("BOTH", True, True), ("SHORT", True, False), ("LONG", False, True),
])
def test_the_filter_allows_its_sides(mode, value, short_ok, long_ok):
    mode(value)
    assert directions.allowed(Direction.SHORT) is short_ok
    assert directions.allowed("LONG") is long_ok
    assert directions.allowed(None) is False


def test_order_sides():
    assert (directions.entry_side("LONG"), directions.exit_side("LONG")) == ("BUY", "SELL")
    assert (directions.entry_side("SHORT"), directions.exit_side("SHORT")) == ("SELL", "BUY")


def test_a_record_keeps_its_own_side_whatever_the_filter(mode):
    mode("SHORT")
    assert directions.of_signal({"direction": "LONG"}) == "LONG"
    assert directions.of_signal({"side": "LONG"}) == "LONG"
    mode("LONG")
    assert directions.of_signal({"side": "SHORT"}) == "SHORT"


def test_a_record_with_no_side_is_a_short_unless_the_engine_is_long_only(mode):
    """Positions from before per-strategy directions carry no side."""
    assert directions.of_signal({}) == "SHORT"
    mode("LONG")
    assert directions.of_signal({}) == "LONG"


@pytest.mark.parametrize("strategy, expected", [
    (_strat(Direction.SHORT), (True, False)),
    (_strat(Direction.LONG), (False, True)),
    (_strat(Direction.LONG, Universe.GAINERS), (True, False)),
    (_strat(Direction.SHORT, Universe.LOSERS), (False, True)),
    (_strat(Direction.SHORT, Universe.MOVERS), (True, True)),
])
def test_bands_follow_the_universe(strategy, expected):
    assert directions.bands_of(strategy) == expected


def test_the_scanner_watches_the_union_of_the_allowed_strategies(mode):
    s, lng = _strat(Direction.SHORT), _strat(Direction.LONG)
    assert directions.scan_bands([s]) == (True, False)
    assert directions.scan_bands([lng]) == (False, True)
    assert directions.scan_bands([s, lng]) == (True, True)
    mode("SHORT")
    assert directions.scan_bands([s, lng]) == (True, False)
    mode("LONG")
    assert directions.scan_bands([s, lng]) == (False, True)


def test_with_no_strategy_allowed_the_scan_is_the_old_gainers_scan(mode):
    assert directions.scan_bands([]) == (True, False)
    mode("SHORT")
    assert directions.scan_bands([_strat(Direction.LONG)]) == (True, False)


def test_bands(monkeypatch):
    for k, v in (("SCANNER_GAIN_MIN_PCT", 7.5), ("SCANNER_GAIN_MAX_PCT", 18.0),
                 ("SCANNER_LOSS_MIN_PCT", 7.5), ("SCANNER_LOSS_MAX_PCT", 18.0)):
        monkeypatch.setattr(config, k, v)
    assert directions.in_band(10, True, False) and not directions.in_band(-10, True, False)
    assert directions.in_band(-10, False, True) and not directions.in_band(10, False, True)
    assert not directions.in_band(-5, True, True) and not directions.in_band(-20, True, True)


def test_offered_by_sign():
    assert directions.offered(_strat(Direction.LONG), -9.0)
    assert not directions.offered(_strat(Direction.LONG), 9.0)
    assert directions.offered(_strat(Direction.SHORT), 9.0)
    assert directions.offered(_strat(Direction.LONG, Universe.MOVERS), 9.0)


# ── G9, mirrored ─────────────────────────────────────────────────────────────

def _htf(closes):
    return pd.DataFrame({"close": closes})


@pytest.mark.parametrize("closes, short_ok, long_ok", [
    ([100, 101, 103], False, True),      # accelerating up: block a short, not a long
    ([100, 99, 97], True, False),        # accelerating down: block a long, not a short
])
def test_g9_blocks_a_move_running_against_the_fade(monkeypatch, closes, short_ok, long_ok):
    monkeypatch.setattr(config, "P61_G9_BYPASS_SD_THRESHOLD", 4.0)
    monkeypatch.setattr(config, "P61_G9_ACCEL_REJECT_THRESHOLD", 1.0)
    monkeypatch.setattr(config, "P61_G9_STALL_PASS_THRESHOLD", 0.3)
    g = HTFConfluence(None)
    assert g.check_trend_exhaustion("X", df_15m=_htf(closes), vwap_sd=0.0)[0] is short_ok
    assert g.check_trend_exhaustion("X", df_15m=_htf(closes), vwap_sd=0.0, direction="LONG")[0] is long_ok


def test_g9_bypass_is_stretch_below_vwap_for_a_long(monkeypatch):
    monkeypatch.setattr(config, "P61_G9_BYPASS_SD_THRESHOLD", 4.0)
    g = HTFConfluence(None)
    ok, msg = g.check_trend_exhaustion("X", df_15m=None, vwap_sd=-4.5, direction="LONG")
    assert ok and "Alpha Strike" in msg
    ok, _ = g.check_trend_exhaustion("X", df_15m=None, vwap_sd=-4.5)
    assert not ok, "a short is not bypassed by a stretch below VWAP (and has no data here)"


# ── the order path ───────────────────────────────────────────────────────────

def _om():
    return SimpleNamespace(_round_sl_to_tick=OrderManager._round_sl_to_tick)


@pytest.mark.parametrize("filt", ["BOTH", "SHORT", "LONG"])
def test_a_long_signals_stop_is_below_the_fill_whatever_the_filter(mode, filt):
    mode(filt)
    sig = {"direction": "LONG", "tick_size": 0.05, "signal_low": 95.0, "atr": 1.0}
    assert OrderManager.compute_stop_loss(_om(), 100.0, sig) == 94.5
    assert OrderManager.compute_stop_loss(_om(), 100.0, {**sig, "stop_pct": 2.0}) == 98.0
    short = {"direction": "SHORT", "tick_size": 0.05, "signal_high": 105.0, "atr": 1.0}
    assert OrderManager.compute_stop_loss(_om(), 100.0, short) == 105.5


def test_a_long_targets_above_entry_without_a_vwap(mode):
    mode("SHORT")
    om = SimpleNamespace()
    assert OrderManager.compute_take_profits(om, 100.0, {"direction": "LONG"})["tp"] == 101.0
    assert OrderManager.compute_take_profits(om, 100.0, {"direction": "SHORT"})["tp"] == 99.0


class _Broker:
    def __init__(self, fill):
        self.fill, self.orders = fill, []
        self._leverage_cache = {}

    async def place_order(self, symbol, side, qty, order_type, price=None, trigger_price=None):
        self.orders.append({"side": side, "qty": qty, "type": order_type,
                            "price": price, "trigger": trigger_price})
        return f"id{len(self.orders)}"

    async def wait_for_fill(self, order_id, timeout=15.0):
        return True

    async def get_order_avg_price(self, order_id):
        return self.fill

    async def get_ltp(self, symbol):
        return self.fill

    async def cancel_order(self, order_id):
        return True


@pytest.fixture
def live(monkeypatch):
    from shortcircuit.eod import squareoff
    from shortcircuit.execution import order_manager as om_mod
    monkeypatch.setattr(om_mod.run_mode, "is_data_only", lambda: False)
    monkeypatch.setattr(squareoff, "past_squareoff", lambda *a, **k: False)


@pytest.mark.parametrize("direction, entry_side, stop_side", [
    ("LONG", "BUY", "SELL"), ("SHORT", "SELL", "BUY"),
])
def test_entry_trades_the_signals_side_and_rests_the_stop_opposite(mode, live, direction,
                                                                   entry_side, stop_side):
    mode("SHORT" if direction == "LONG" else "LONG")     # the filter must not flip it
    broker = _Broker(fill=100.0)
    om = OrderManager(broker=broker, telegram_bot=None)
    sig = {"symbol": "NSE:X-EQ", "strategy": "s", "direction": direction, "ltp": 100.0,
           "signal_low": 98.0 if direction == "LONG" else 99.8,
           "signal_high": 100.2 if direction == "LONG" else 102.0,
           "atr": 1.0, "tick_size": 0.05}
    pos = asyncio.run(om.enter_position(sig))
    assert pos is not None and pos["side"] == direction
    entry, stop = broker.orders
    assert entry["side"] == entry_side and entry["type"] == "MARKET"
    assert stop["side"] == stop_side and stop["type"] == "SL_LIMIT"
    if direction == "LONG":
        assert stop["trigger"] == 97.5 < 100.0, "the stop rests under a long"
        assert stop["price"] < stop["trigger"], "a SELL stop-limit's limit sits under its trigger"
    else:
        assert stop["trigger"] == 102.5 > 100.0
        assert stop["price"] > stop["trigger"]


def test_data_only_names_the_signals_side(monkeypatch, caplog):
    from shortcircuit.execution import order_manager as om_mod
    monkeypatch.setattr(om_mod.run_mode, "is_data_only", lambda: True)
    om = OrderManager(broker=SimpleNamespace(), telegram_bot=None)
    sig = {"symbol": "NSE:X-EQ", "ltp": 100.0, "strategy": "l", "direction": "LONG",
           "signal_high": 100.5}
    with caplog.at_level("WARNING"):
        assert asyncio.run(om.enter_position(sig)) is None
    assert "would LONG NSE:X-EQ" in caplog.text and "trigger=100.5" in caplog.text


# ── the validation gate and the open position ────────────────────────────────

class _FixedDT(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        base = dt.datetime(2026, 11, 27, 11, 0)
        return tz.localize(base) if tz is not None else base


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.setattr(fe, "datetime", SimpleNamespace(
        datetime=_FixedDT, timedelta=dt.timedelta, timezone=dt.timezone))
    eng = fe.FocusEngine.__new__(fe.FocusEngine)
    eng.pending_signals, eng.monitoring_active, eng.telegram_bot = {}, True, None
    eng.fyers = SimpleNamespace(depth=lambda data=None: {})
    quotes = {}
    eng.order_manager = SimpleNamespace(broker=SimpleNamespace(get_quote_cache_snapshot=lambda: quotes))

    def tick(sym, ltp):
        quotes[sym] = {"ltp": ltp}
        asyncio.run(eng.check_pending_signals(None))
    return eng, tick


LONG_SIG = {"symbol": "NSE:L-EQ", "direction": "LONG", "signal_high": 100.0,
            "signal_low": 95.0, "tick_size": 0.05}


def test_a_long_arms_above_its_trigger_and_is_abandoned_under_its_low(gate):
    eng, tick = gate
    eng.add_pending_signal(dict(LONG_SIG))
    p = eng.pending_signals["NSE:L-EQ"]
    assert p["trigger"] == 100.0 and p["invalidate"] == pytest.approx(95.0 * 0.998)
    tick("NSE:L-EQ", 99.0)                      # between: still waiting
    assert "NSE:L-EQ" in eng.pending_signals
    tick("NSE:L-EQ", 100.1)                     # broke above: validated (auto off, so alert-only)
    assert "NSE:L-EQ" not in eng.pending_signals
    assert p["data"]["adjusted_entry"] == pytest.approx(100.05), "one tick through the trigger"


def test_a_long_falling_through_its_low_is_invalidated(gate):
    eng, tick = gate
    eng.add_pending_signal(dict(LONG_SIG))
    p = eng.pending_signals["NSE:L-EQ"]
    tick("NSE:L-EQ", 94.0)
    assert "NSE:L-EQ" not in eng.pending_signals and "adjusted_entry" not in p["data"]


def test_a_short_still_arms_below_its_trigger(gate):
    eng, tick = gate
    eng.add_pending_signal({"symbol": "NSE:S-EQ", "signal_low": 100.0, "signal_high": 104.0,
                            "tick_size": 0.05})
    p = eng.pending_signals["NSE:S-EQ"]
    assert p["direction"] == "SHORT" and p["invalidate"] == pytest.approx(104.0 * 1.002)
    tick("NSE:S-EQ", 100.5)
    assert "NSE:S-EQ" in eng.pending_signals
    tick("NSE:S-EQ", 99.9)
    assert p["data"]["adjusted_entry"] == pytest.approx(99.95)


def test_a_long_without_a_trigger_is_not_queued(gate):
    eng, _ = gate
    eng.add_pending_signal({"symbol": "NSE:L-EQ", "direction": "LONG", "signal_low": 95.0})
    assert eng.pending_signals == {}


def test_an_open_long_keeps_its_side_under_a_short_filter(mode, monkeypatch):
    mode("SHORT")
    monkeypatch.setattr(config, "TP_MODE", "SCALE")
    monkeypatch.setattr(fe.threading, "Thread",
                        lambda *a, **k: SimpleNamespace(start=lambda: None))
    eng = fe.FocusEngine.__new__(fe.FocusEngine)
    eng.order_manager = None
    eng.start_focus("NSE:L-EQ", {"entry_price": 100.0, "stop_loss": 97.5, "qty": 10,
                                 "side": "LONG", "tick_size": 0.05})
    t = eng.active_trade
    assert t["direction"] == "LONG"
    assert t["soft_sl"] < 100.0, "the soft stop sits under a long"
    assert t["entry"] < t["tp_1"] < t["tp"], "targets lie above a long's entry"


# ── the RSI exits, mirrored ──────────────────────────────────────────────────

@dataclass
class _C:
    epoch: int
    close: float
    high: float = 0.0
    low: float = 0.0


NOW = 1_790_000_000 // 60 * 60 + 30


class _CandleOM:
    def __init__(self, candles):
        self.broker = SimpleNamespace(get_local_candles=lambda s, n=400: candles[-n:])


def _rsi_engine(candles):
    e = SimpleNamespace(order_manager=_CandleOM(candles))
    e._rsi_take_profit_due = fe.FocusEngine._rsi_take_profit_due.__get__(e)
    return e


def _series(warm, after):
    closes = list(warm) + list(after)
    first = NOW // 60 * 60 - 60 * len(closes)
    return [_C(first + 60 * k, c) for k, c in enumerate(closes)], first + 60 * len(warm)


def _cover_trade(entry_epoch, entry, direction):
    return {"exit_profile": "EOD_HOLD", "exit_params": {"rsi_cover": asdict(RsiCover())},
            "direction": direction, "entry": entry, "entry_minute": entry_epoch, "remaining_qty": 3}


def test_a_long_is_sold_when_rsi_rises_past_the_mirror_level(monkeypatch):
    monkeypatch.setattr(fe.time, "time", lambda: NOW)
    warm = [100 - 0.05 * i for i in range(40)]                 # a gentle slide
    rising = [98.5, 99.0, 99.4, 99.8, 100.2, 100.6, 101.0]     # entry bar + 6, in profit
    c, e = _series(warm, rising)
    rsi = _rsi_engine(c)._rsi_take_profit_due("NSE:L-EQ", _cover_trade(e, 98.0, "LONG"))
    assert rsi is not None and rsi > 60


def test_a_long_not_in_profit_is_not_sold(monkeypatch):
    monkeypatch.setattr(fe.time, "time", lambda: NOW)
    warm = [100 - 0.05 * i for i in range(40)]
    rising = [98.5, 99.0, 99.4, 99.8, 100.2, 100.6, 101.0]
    c, e = _series(warm, rising)
    assert _rsi_engine(c)._rsi_take_profit_due("NSE:L-EQ", _cover_trade(e, 102.0, "LONG")) is None


class _StagedOM:
    def __init__(self, candles):
        self.broker = SimpleNamespace(get_local_candles=lambda s, n=400: candles)
        self.moves, self.exits = [], []

    async def move_hard_stop(self, symbol, price, new_qty=None):
        self.moves.append((price, new_qty))
        return True

    async def safe_exit(self, symbol, reason):
        self.exits.append(reason)
        return True


def _staged_engine(om):
    e = SimpleNamespace(order_manager=om, _event_loop=object(), alerts=[], stopped=[],
                        _exit_retry_at={}, _exit_retry_count={})
    e._staged_trail = fe.FocusEngine._staged_trail.__get__(e)
    e._retire_or_retry = fe.FocusEngine._retire_or_retry.__get__(e)
    e._dispatch_alert = e.alerts.append
    e.stop_focus = lambda reason="STOPPED": e.stopped.append(reason)
    return e


@pytest.fixture
def inline(monkeypatch):
    def run_now(coro, loop):
        return SimpleNamespace(result=lambda timeout=None: asyncio.new_event_loop().run_until_complete(coro))
    monkeypatch.setattr(fe.asyncio, "run_coroutine_threadsafe", run_now)
    monkeypatch.setattr(fe.time, "sleep", lambda *_: None)


def _bars_ending_now(lows, highs, now):
    first = int(now) // 60 * 60 - 60 * len(lows)
    return [_C(first + 60 * k, (lo + hi) / 2, hi, lo) for k, (lo, hi) in enumerate(zip(lows, highs, strict=True))]


def _long_staged_trade(sl):
    cfg = StagedRsiExit(bar_minutes=1, trail_bar_minutes=1, min_bars=15, trail_buffer_pct=0.1)
    return cfg, {"symbol": "NSE:L-EQ", "entry": 100.0, "sl": sl, "remaining_qty": 5,
                 "direction": "LONG", "exit_profile": "STAGED_RSI", "tick_size": 0.05,
                 "staged": {"scaled_out": True, "trailing": True, "rsi_epoch": None,
                            "trail_epoch": None}}


def test_a_long_trail_follows_the_low_and_only_rises(inline):
    now = time.time()
    candles = _bars_ending_now([101.0, 102.0, 103.0], [101.5, 102.5, 103.5], now)
    om = _StagedOM(candles)
    eng = _staged_engine(om)
    cfg, t = _long_staged_trade(sl=100.0)
    assert eng._staged_trail("NSE:L-EQ", t, cfg, candles, 104.0, now) is None
    new = OrderManager._round_sl_to_tick(103.0 * 0.999, "BUY", 0.05)
    assert om.moves == [(new, 5)] and t["sl"] == new and new < 103.0
    t["staged"]["trail_epoch"] = None
    t["sl"] = 103.5                                  # already tighter than the next level
    assert eng._staged_trail("NSE:L-EQ", t, cfg, candles, 104.0, now) is None
    assert len(om.moves) == 1, "a trail never loosens"


def test_a_long_already_under_its_trail_level_is_closed(inline):
    now = time.time()
    candles = _bars_ending_now([101.0, 102.0, 103.0], [101.5, 102.5, 103.5], now)
    om = _StagedOM(candles)
    eng = _staged_engine(om)
    cfg, t = _long_staged_trade(sl=100.0)
    assert eng._staged_trail("NSE:L-EQ", t, cfg, candles, 102.5, now) == "done"
    assert om.exits == ["TRAIL_STOP"] and om.moves == []


def test_bars_carry_their_low():
    open_ = int(pd.Timestamp("2026-11-27 09:15", tz="Asia/Kolkata").timestamp())
    candles = [_C(open_ + 60 * i, 100.0, 101.0 + i, 99.0 - i) for i in range(6)]
    bars = fe.completed_bars(candles, 5, now_epoch=open_ + 360)
    assert bars[0].low == 95.0 and bars[0].high == 105.0


# ── the scanner ──────────────────────────────────────────────────────────────

def _scan(strategies, quotes, monkeypatch):
    from shortcircuit.marketdata import scanner as sc
    now = time.time()
    snap = {s: {"ltp": 100.0, "volume": 5_000_000, "ch_oc": chg, "oi": 0, "ts": now, "source": "ws"}
            for s, chg in quotes.items()}
    broker = SimpleNamespace(
        is_cache_severely_degraded=lambda: False, _degraded_scan_count=0,
        is_cache_ready=lambda: True, get_quote_cache_snapshot=lambda: snap,
        cache_health_snapshot=lambda: {"total": len(snap), "fresh": len(snap), "stale": 0, "seeded": 0},
    )
    s = sc.FyersScanner(None, broker=broker, strategies=strategies)
    s.symbols = {sym: 0.05 for sym in quotes}
    monkeypatch.setattr(s, "check_chart_quality", lambda sym: (True, None, None))
    monkeypatch.setattr(config, "ETF_CLUSTER_DEDUP_ENABLED", False, raising=False)
    return [c["symbol"] for c in s.scan_market()]


QUOTES = {"NSE:UP-EQ": 10.0, "NSE:BIGUP-EQ": 15.0, "NSE:DOWN-EQ": -9.0, "NSE:FLAT-EQ": 1.0}


def test_a_short_only_deployment_scans_gainers_alone(mode, monkeypatch):
    assert _scan([_strat(Direction.SHORT)], QUOTES, monkeypatch) == ["NSE:BIGUP-EQ", "NSE:UP-EQ"]


def test_a_long_strategy_brings_the_losers_in(mode, monkeypatch):
    got = _scan([_strat(Direction.SHORT), _strat(Direction.LONG)], QUOTES, monkeypatch)
    assert got == ["NSE:BIGUP-EQ", "NSE:UP-EQ", "NSE:DOWN-EQ"], "biggest movers first"
    assert _scan([_strat(Direction.LONG)], QUOTES, monkeypatch) == ["NSE:DOWN-EQ"]


# ── the backtest is direction-symmetric ──────────────────────────────────────

IST = "Asia/Kolkata"
PREV = 100.0
K = 2 * PREV          # reflecting through 2 x previous close keeps a +g% day a -g% day


def _short_day():
    """Up ~10% into a squeeze to 10:25, then a fade with a bounce: every exit has work."""
    t0 = pd.Timestamp("2026-11-24 09:15", tz=IST)
    closes = []
    for i in range(375):
        if i < 70:
            c = 108 + 6 * i / 70 + (0.3 if i % 3 == 0 else -0.2)
        elif i < 120:
            c = 114 - 4 * (i - 70) / 50 + (0.25 if i % 4 == 0 else -0.15)
        elif i < 160:
            c = 110 + 1.5 * (i - 120) / 40
        else:
            c = 111.5 - 2 * (i - 160) / 215 + (0.2 if i % 5 == 0 else -0.1)
        closes.append(round(c / 0.05) * 0.05)
    opens = [closes[max(0, i - 1)] for i in range(375)]
    dts = [t0 + pd.Timedelta(minutes=i) for i in range(375)]
    return pd.DataFrame({
        "epoch": [int(d.timestamp()) for d in dts], "datetime": dts, "open": opens,
        "high": [round(max(o, c) + 0.15, 2) for o, c in zip(opens, closes, strict=True)],
        "low": [round(min(o, c) - 0.15, 2) for o, c in zip(opens, closes, strict=True)],
        "close": closes, "volume": [200_000] * 375,
    })


def _reflect(day):
    out = day.copy()
    out["open"], out["close"] = K - day["open"], K - day["close"]
    out["high"], out["low"] = K - day["low"], K - day["high"]
    return out


def _once(direction, plan, at="10:30"):
    class Once(Strategy):
        name = f"once_{direction.value.lower()}"
        requires_htf_confirmation = False

        def evaluate(self, ctx):
            if ctx.now.strftime("%H:%M") != at:
                return None
            ref = ctx.day_low if direction is Direction.LONG else ctx.day_high
            return Signal(pattern="T", stop_reference=ref, exit=plan)

    Once.direction = direction
    return Once()


PLANS = {
    "tp_ladder": ExitPlan(),
    "eod_hold_rsi_cover": ExitPlan(profile=ExitProfile.EOD_HOLD, rsi_cover=RsiCover()),
    "staged_rsi": ExitPlan(profile=ExitProfile.STAGED_RSI,
                           staged_rsi=StagedRsiExit(bar_minutes=1, trail_bar_minutes=1, min_bars=15)),
    # No stop to cost, and a trail that engages: scale-out, then the trailing stop.
    # The trail's buffer is a % of each side's own bar extreme, so it mirrors in
    # %, not in rupees (as stop_pct does); 0 keeps the reflection exact.
    "staged_rsi_trail": ExitPlan(profile=ExitProfile.STAGED_RSI, staged_rsi=StagedRsiExit(
        bar_minutes=1, trail_bar_minutes=1, min_bars=15, stop_to_cost=False,
        scale_out_rsi=45.0, trail_rsi=40.0, trail_buffer_pct=0.0)),
}
# What each plan must actually exercise on this day, so the symmetry is not vacuous.
EXITS = {
    "tp_ladder": ["TP1_PARTIAL", "TP_HIT"],
    "eod_hold_rsi_cover": ["RSI_TP"],
    "staged_rsi": ["RSI_SCALE_OUT", "COST_STOP"],
    "staged_rsi_trail": ["RSI_SCALE_OUT", "TRAIL_STOP"],
}


@pytest.mark.parametrize("name", sorted(PLANS))
def test_a_long_on_the_reflected_day_is_the_reflection_of_the_short(mode, monkeypatch, name):
    monkeypatch.setattr(config, "TP_MODE", "SCALE")
    monkeypatch.setattr(config, "P52_BREAKEVEN_AFTER_TP1", True)
    plan = PLANS[name]
    short = replay_day("NSE:X-EQ", _short_day(), PREV, [_once(Direction.SHORT, plan)], Costs(0.0, 0.0))
    long_ = replay_day("NSE:X-EQ", _reflect(_short_day()), K - PREV, [_once(Direction.LONG, plan)],
                       Costs(0.0, 0.0))
    assert len(short.trades) == len(long_.trades) == 1
    s, lg = short.trades[0], long_.trades[0]
    assert (s.direction, lg.direction) == ("SHORT", "LONG")
    assert s.exit_model == lg.exit_model == name.removesuffix("_trail")
    assert [r for _, _, r, _ in s.legs] == EXITS[name]
    assert lg.entry == pytest.approx(K - s.entry) and lg.stop == pytest.approx(K - s.stop)
    assert [(f, r, w) for f, _, r, w in lg.legs] == [(f, r, w) for f, _, r, w in s.legs]
    assert [K - p for _, p, _, _ in s.legs] == pytest.approx([p for _, p, _, _ in lg.legs])
    assert [K - p for _, p in s.stop_path] == pytest.approx([p for _, p in lg.stop_path])
    # Same price moves in the trade's favour, so the same sign of P&L.
    assert (s.gross_pct > 0) == (lg.gross_pct > 0)


def test_a_long_makes_money_when_price_rises_after_entry(mode):
    day = _reflect(_short_day())
    res = replay_day("NSE:X-EQ", day, K - PREV, [_once(Direction.LONG, ExitPlan(profile=ExitProfile.EOD_HOLD))],
                     Costs(0.0, 0.0))
    t = res.trades[0]
    last = float(day["close"].iloc[-1])
    assert t.exit_reason in ("EOD_SQUAREOFF", "SL_HIT")
    exit_px = t.legs[-1][1]
    assert t.gross_pct == pytest.approx((exit_px - t.entry) / t.entry * 100)
    assert t.stop < t.entry and last > 0


def test_the_long_filter_holds_a_short_back_in_the_backtest(mode):
    mode("LONG")
    res = replay_day("NSE:X-EQ", _short_day(), PREV, [_once(Direction.SHORT, ExitPlan())], Costs(0.0, 0.0))
    assert res.trades == [] and res.signals == 0


def test_lower_circuit_inference_mirrors_the_upper():
    assert infer_lower_circuit(100.0, 90.0, False) == 90.0
    assert infer_lower_circuit(100.0, 85.0, False) == 80.0
    assert infer_lower_circuit(100.0, 95.0, False) == 0.0
    assert infer_lower_circuit(100.0, 90.0, True) == 0.0
