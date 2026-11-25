"""
The STAGED_RSI exit profile, the fixed-percentage stop, and the per-symbol
square-off schedule.

STAGED_RSI: a short books part of its position when RSI cools to one level,
moving the stop on the rest to cost, then trails the stop on what is left once
RSI reaches a second. What is pinned:

  * RSI is read on completed bars only, once per bar, never from a bar that
    closed before the entry, and only acted on in profit;
  * a scale-out that does not fill changes nothing and is retried later, not
    at 5Hz; one that fills always shrinks the stop to the remaining quantity;
  * a trailing stop only ever tightens, and a trail level price has already
    crossed closes the position rather than placing a stop marketable on arrival.

Square-off: Fyers squares off MIS in closing-auction (F&O) stocks at 15:05,
before the engine's 15:10. A position moves to the earlier cutoff only when the
symbol is positively on Fyers' F&O list; anything uncertain keeps 15:10.
"""
from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import asdict, dataclass
from types import SimpleNamespace

import pytest

from shortcircuit.eod import squareoff
from shortcircuit.eod.eod_scheduler import eod_scheduler
from shortcircuit.execution import focus_engine as fe
from shortcircuit.execution.order_manager import OrderManager
from shortcircuit.strategy.sdk import ExitPlan, ExitProfile, RsiCover, StagedRsiExit

IST = squareoff.IST


# ── the contract ─────────────────────────────────────────────────────────────

def test_staged_profile_requires_its_parameters_and_nothing_else():
    with pytest.raises(ValueError):
        ExitPlan(profile=ExitProfile.STAGED_RSI)
    with pytest.raises(ValueError):
        ExitPlan(staged_rsi=StagedRsiExit())
    with pytest.raises(ValueError):
        ExitPlan(profile=ExitProfile.STAGED_RSI, staged_rsi=StagedRsiExit(), tp_atr_mult=1.0)
    with pytest.raises(ValueError):
        ExitPlan(profile=ExitProfile.STAGED_RSI, staged_rsi=StagedRsiExit(),
                 rsi_cover=RsiCover())


@pytest.mark.parametrize("bad", [
    dict(trail_rsi=60.0),                 # trail must sit below the scale-out level
    dict(scale_out_fraction=1.0),
    dict(scale_out_fraction=0.0),
    dict(bar_minutes=7),                  # does not tile the session
    dict(min_bars=10, period=14),         # no RSI from fewer than period + 1 closes
])
def test_staged_parameters_are_validated(bad):
    with pytest.raises(ValueError):
        StagedRsiExit(**bad)


@pytest.mark.parametrize("pct", [0, -1, 10.5, True])
def test_stop_pct_must_be_a_plausible_percentage(pct):
    with pytest.raises(ValueError):
        ExitPlan(stop_pct=pct)


def test_params_carry_the_new_fields_and_leave_existing_ones_alone():
    assert ExitPlan(profile=ExitProfile.EOD_HOLD, rsi_cover=RsiCover()).params() == {
        "rsi_cover": asdict(RsiCover())
    }
    p = ExitPlan(profile=ExitProfile.STAGED_RSI, staged_rsi=StagedRsiExit(), stop_pct=5).params()
    assert p["staged_rsi"] == asdict(StagedRsiExit())
    assert p["stop_pct"] == 5.0
    assert fe.staged_rsi_of({"exit_params": p}) == StagedRsiExit()


# ── the fixed-percentage stop ────────────────────────────────────────────────

def _om():
    return SimpleNamespace(_round_sl_to_tick=OrderManager._round_sl_to_tick)


def test_a_percentage_stop_is_measured_from_the_fill(monkeypatch):
    monkeypatch.setattr(fe.config, "TRADE_DIRECTION", "SHORT")
    stop = OrderManager.compute_stop_loss(
        _om(), 200.0, {"stop_pct": 5.0, "tick_size": 0.05, "signal_high": 250.0, "atr": 3.0},
    )
    assert stop == 210.0, "5% above the fill, regardless of the setup level"


def test_a_percentage_stop_rounds_away_from_the_fill(monkeypatch):
    monkeypatch.setattr(fe.config, "TRADE_DIRECTION", "SHORT")
    stop = OrderManager.compute_stop_loss(_om(), 123.47, {"stop_pct": 5.0, "tick_size": 0.1})
    assert stop == pytest.approx(129.7) and stop >= 123.47 * 1.05


def test_without_a_percentage_the_stop_is_unchanged(monkeypatch):
    monkeypatch.setattr(fe.config, "TRADE_DIRECTION", "SHORT")
    sig = {"tick_size": 0.05, "signal_high": 105.0, "atr": 1.0}
    assert OrderManager.compute_stop_loss(_om(), 100.0, sig) == 105.5


# ── per-symbol square-off ────────────────────────────────────────────────────

def _row(kind, underlying):
    cols = ["0"] * 21
    cols[2], cols[13] = kind, underlying
    return ",".join(cols)


def _master(n=150):
    rows = [_row("13", f"STOCK{i}") for i in range(n)]
    rows += [_row("15", "STOCK0"), _row("11", "NIFTY"), _row("13", "M&M")]
    return "\n".join(rows)


def test_the_fno_list_is_stock_futures_only():
    symbols = squareoff.parse_fno_master(_master())
    assert "NSE:STOCK0-EQ" in symbols and "NSE:M&M-EQ" in symbols
    assert "NSE:NIFTY-EQ" not in symbols, "index futures are not stocks"


def test_only_a_listed_fno_stock_squares_off_early():
    reg = squareoff.FnoRegistry()
    assert reg.refresh(lambda: _master())
    assert squareoff.squareoff_time("NSE:STOCK3-EQ", reg) == dt.time(15, 2)
    assert squareoff.squareoff_time("NSE:SMALLCAP-EQ", reg) == dt.time(15, 10)


@pytest.mark.parametrize("fetch", [
    lambda: (_ for _ in ()).throw(OSError("offline")),
    lambda: _master(n=5),                 # truncated or reshaped file
])
def test_an_unknown_list_keeps_every_position_on_1510(fetch):
    reg = squareoff.FnoRegistry()
    assert reg.refresh(fetch) is False
    assert reg.is_fno("NSE:STOCK3-EQ") is None
    assert squareoff.squareoff_time("NSE:STOCK3-EQ", reg) == dt.time(15, 10)


def test_past_squareoff_reads_the_clock_in_ist():
    reg = squareoff.FnoRegistry()
    reg.refresh(lambda: _master())
    at = lambda h, m: IST.localize(dt.datetime(2026, 11, 25, h, m))  # noqa: E731
    assert not squareoff.past_squareoff("NSE:STOCK3-EQ", at(15, 1), reg)
    assert squareoff.past_squareoff("NSE:STOCK3-EQ", at(15, 2), reg)
    assert not squareoff.past_squareoff("NSE:OTHER-EQ", at(15, 9), reg)
    # 09:32 UTC is 15:02 IST: a UTC container clock must not delay the close.
    utc = dt.datetime(2026, 11, 25, 9, 32, tzinfo=dt.timezone.utc)
    assert squareoff.past_squareoff("NSE:STOCK3-EQ", utc, reg)


def _run_scheduler(clock, positions, cutoff_for):
    fired, read = [], []
    shutdown = asyncio.Event()

    async def trigger():
        fired.append(read[-1].time())
        shutdown.set()
        return True

    async def notify(_):
        pass

    async def get_positions():
        return positions

    def now():
        t = clock[0]
        read.append(t)
        clock[0] = t + dt.timedelta(minutes=1)
        if t.time() > dt.time(15, 31):
            shutdown.set()
        return t

    async def main():
        import shortcircuit.eod.eod_scheduler as sched

        async def no_wait(aw, timeout):
            aw.close()
            raise asyncio.TimeoutError
        orig = sched.asyncio.wait_for
        sched.asyncio.wait_for = no_wait
        try:
            await eod_scheduler(shutdown, trigger, lambda: asyncio.sleep(0), notify,
                                get_positions, IST.localize(dt.datetime(2026, 11, 25, 9, 0)),
                                _now_fn=now, _cutoff_for=cutoff_for)
        finally:
            sched.asyncio.wait_for = orig

    asyncio.run(main())
    return fired


def test_scheduler_closes_an_fno_position_at_its_cutoff():
    clock = [IST.localize(dt.datetime(2026, 11, 25, 14, 58))]
    cutoff = lambda s: dt.time(15, 2) if s == "NSE:FNO-EQ" else dt.time(15, 10)  # noqa: E731
    fired = _run_scheduler(clock, [{"symbol": "NSE:FNO-EQ", "netQty": -10}], cutoff)
    assert fired == [dt.time(15, 2)]


def test_scheduler_keeps_1510_for_everything_else():
    clock = [IST.localize(dt.datetime(2026, 11, 25, 14, 58))]
    cutoff = lambda s: dt.time(15, 10)  # noqa: E731
    fired = _run_scheduler(clock, [{"symbol": "NSE:SMALL-EQ", "netQty": -10}], cutoff)
    assert fired == [dt.time(15, 10)]


def test_scheduler_ignores_a_flat_fno_row():
    clock = [IST.localize(dt.datetime(2026, 11, 25, 14, 58))]
    cutoff = lambda s: dt.time(15, 2)  # noqa: E731
    fired = _run_scheduler(clock, [{"symbol": "NSE:FNO-EQ", "netQty": 0}], cutoff)
    assert fired == [dt.time(15, 10)]


# ── bars ─────────────────────────────────────────────────────────────────────

@dataclass
class _C:
    epoch: int
    high: float
    close: float


OPEN = int(IST.localize(dt.datetime(2026, 11, 25, 9, 15)).timestamp())


def _candles(closes, highs=None, start=OPEN):
    highs = highs or [c + 0.2 for c in closes]
    return [_C(start + 60 * i, h, c) for i, (c, h) in enumerate(zip(closes, highs, strict=True))]


def test_bars_align_to_the_open_and_exclude_the_forming_one():
    candles = _candles([100 + i for i in range(12)])           # 09:15 .. 09:26
    bars = fe.completed_bars(candles, 5, now_epoch=OPEN + 12 * 60)
    assert [b.epoch for b in bars] == [OPEN, OPEN + 300], "09:25 bar is still forming"
    assert bars[0].close == 104 and bars[0].high == pytest.approx(104.2)


# ── the staged exits ─────────────────────────────────────────────────────────

class _OM:
    def __init__(self, candles, *, partial_ok=True, move_ok=True, verify_qty=None):
        self.broker = SimpleNamespace(get_local_candles=lambda s, n=400: candles)
        self.partial_ok, self.move_ok, self.verify_qty = partial_ok, move_ok, verify_qty
        self.partials, self.moves, self.exits = [], [], []

    async def partial_exit(self, symbol, qty, reason):
        self.partials.append((qty, reason))
        return self.partial_ok

    async def move_hard_stop(self, symbol, price, new_qty=None):
        self.moves.append((price, new_qty))
        return self.move_ok

    async def verify_stop_qty(self, symbol):
        return self.verify_qty

    async def safe_exit(self, symbol, reason):
        self.exits.append(reason)
        return True


class _Engine:
    _staged_rsi_step = fe.FocusEngine._staged_rsi_step
    _staged_scale_out = fe.FocusEngine._staged_scale_out
    _staged_trail = fe.FocusEngine._staged_trail
    _resize_stop_after_partial = fe.FocusEngine._resize_stop_after_partial
    _retire_or_retry = fe.FocusEngine._retire_or_retry

    def __init__(self, om):
        self.order_manager = om
        self._event_loop = object()
        self.alerts, self.stopped = [], []
        self._exit_retry_at, self._exit_retry_count = {}, {}

    def _dispatch_alert(self, msg):
        self.alerts.append(msg)

    def stop_focus(self, reason="STOPPED"):
        self.stopped.append(reason)


@pytest.fixture(autouse=True)
def _inline(monkeypatch):
    def run_now(coro, loop):
        class _F:
            def result(self, timeout=None):
                return asyncio.new_event_loop().run_until_complete(coro)
        return _F()
    monkeypatch.setattr(fe.asyncio, "run_coroutine_threadsafe", run_now)
    monkeypatch.setattr(fe.time, "sleep", lambda *_: None)


CFG = StagedRsiExit(bar_minutes=1, trail_bar_minutes=1, min_bars=15)

# A rise, then a pullback that cools RSI(14) to ~46: under 50, above 20.
COOLING = [100 + 0.5 * i for i in range(20)] + [109.5 - 0.8 * i for i in range(8)]


def _trade(entry=100.0, qty=10, sl=105.0, entry_ts=OPEN):
    return {
        "symbol": "NSE:X-EQ", "entry": entry, "sl": sl, "remaining_qty": qty,
        "direction": "SHORT", "exit_profile": "STAGED_RSI", "tick_size": 0.05,
        "exit_params": ExitPlan(profile=ExitProfile.STAGED_RSI, staged_rsi=CFG).params(),
        "entry_ts": entry_ts, "be_moved": False,
        "staged": {"scaled_out": False, "trailing": False, "rsi_epoch": None, "trail_epoch": None},
    }


def _at(minutes):
    return OPEN + 60 * minutes + 30


def _falling(n=30, top=110.0, step=0.4):
    # A rise to warm the RSI, then a steady fall: RSI ends deeply oversold.
    up = [top - 8 + i * 0.4 for i in range(20)]
    return up + [top - i * step for i in range(n)]


def test_scale_out_books_the_fraction_and_moves_the_stop_to_cost(monkeypatch):
    closes = list(COOLING)
    candles = _candles(closes)
    monkeypatch.setattr(fe.time, "time", lambda: _at(len(closes)))
    om = _OM(candles, verify_qty=5)
    eng, t = _Engine(om), _trade(entry=108.0, sl=113.4, entry_ts=OPEN + 60 * 20)

    rsi = fe.F.compute_rsi_wilder(closes, 14)
    assert 20 < rsi <= 50, "fixture must cool RSI to between the two levels"
    assert eng._staged_rsi_step("NSE:X-EQ", t, ltp=104.0) is None
    assert om.partials == [(5, "RSI_SCALE_OUT")]
    assert om.moves == [(108.0, 5)], "stop on the rest goes to cost, resized to 5"
    assert t["remaining_qty"] == 5 and t["be_moved"] is True and t["sl"] == 108.0
    assert t["staged"]["scaled_out"] is True


def test_no_scale_out_while_the_trade_is_not_in_profit(monkeypatch):
    closes = list(COOLING)
    monkeypatch.setattr(fe.time, "time", lambda: _at(len(closes)))
    om = _OM(_candles(closes))
    eng, t = _Engine(om), _trade(entry=100.0, sl=105.0, entry_ts=OPEN + 60 * 20)
    eng._staged_rsi_step("NSE:X-EQ", t, ltp=108.5)
    assert om.partials == [] and t["staged"]["scaled_out"] is False


def test_a_bar_that_closed_before_entry_is_not_read(monkeypatch):
    closes = list(COOLING)
    monkeypatch.setattr(fe.time, "time", lambda: _at(len(closes)))
    om = _OM(_candles(closes))
    eng = _Engine(om)
    t = _trade(entry=108.0, entry_ts=_at(len(closes)))          # entered after the last close
    eng._staged_rsi_step("NSE:X-EQ", t, ltp=104.0)
    assert om.partials == []


def test_a_scale_out_that_does_not_fill_changes_nothing_and_waits(monkeypatch):
    closes = list(COOLING)
    clock = [_at(len(closes))]
    monkeypatch.setattr(fe.time, "time", lambda: clock[0])
    om = _OM(_candles(closes), partial_ok=False)
    eng, t = _Engine(om), _trade(entry=108.0, sl=113.4, entry_ts=OPEN + 60 * 20)

    eng._staged_rsi_step("NSE:X-EQ", t, ltp=104.0)
    assert om.moves == [] and t["remaining_qty"] == 10 and t["staged"]["scaled_out"] is False
    eng._staged_rsi_step("NSE:X-EQ", t, ltp=104.0)
    assert len(om.partials) == 1, "not re-fired inside the 10s back-off"
    clock[0] += 11
    eng._staged_rsi_step("NSE:X-EQ", t, ltp=104.0)
    assert len(om.partials) == 2


def test_trailing_starts_at_the_second_level_and_only_tightens(monkeypatch):
    closes = _falling()
    clock = [_at(len(closes))]
    monkeypatch.setattr(fe.time, "time", lambda: clock[0])
    om = _OM(_candles(closes), verify_qty=5)
    eng, t = _Engine(om), _trade(entry=112.0, sl=117.6, entry_ts=OPEN + 60 * 20)
    assert fe.F.compute_rsi_wilder(closes, 14) <= 20

    eng._staged_rsi_step("NSE:X-EQ", t, ltp=closes[-1])
    assert t["staged"]["scaled_out"] and t["staged"]["trailing"]
    last_high = closes[-1] + 0.2
    expected = OrderManager._round_sl_to_tick(last_high * 1.001, "SELL", 0.05)
    assert om.moves[-1] == (expected, 5), "trailed above the last completed bar's high"
    assert t["sl"] == expected

    # Next bar prints a higher high: the trail must not loosen.
    candles = _candles(closes + [closes[-1] + 0.3])
    om.broker = SimpleNamespace(get_local_candles=lambda s, n=400: candles)
    clock[0] += 60
    moves_before = len(om.moves)
    eng._staged_rsi_step("NSE:X-EQ", t, ltp=closes[-1])
    assert len(om.moves) == moves_before


def test_a_crossed_trail_level_closes_instead_of_placing_a_marketable_stop(monkeypatch):
    closes = _falling()
    monkeypatch.setattr(fe.time, "time", lambda: _at(len(closes)))
    om = _OM(_candles(closes), verify_qty=5)
    eng, t = _Engine(om), _trade(entry=112.0, sl=117.6, entry_ts=OPEN + 60 * 20)
    t["staged"].update(scaled_out=True, trailing=True)
    t["remaining_qty"] = 5

    assert eng._staged_rsi_step("NSE:X-EQ", t, ltp=closes[-1] + 1.0) == "done"
    assert om.exits == ["TRAIL_STOP"] and om.moves == []


def test_a_rejected_trail_keeps_the_previous_stop(monkeypatch):
    closes = _falling()
    monkeypatch.setattr(fe.time, "time", lambda: _at(len(closes)))
    om = _OM(_candles(closes), move_ok=False)
    eng, t = _Engine(om), _trade(entry=112.0, sl=112.0, entry_ts=OPEN + 60 * 20)
    t["staged"].update(scaled_out=True, trailing=True)
    t["remaining_qty"] = 5

    eng._staged_rsi_step("NSE:X-EQ", t, ltp=closes[-1])
    assert t["sl"] == 112.0 and t["staged"]["trail_epoch"] is None


def test_too_few_bars_leaves_the_position_on_its_stop(monkeypatch):
    closes = [100 - 0.5 * i for i in range(10)]
    monkeypatch.setattr(fe.time, "time", lambda: _at(len(closes)))
    om = _OM(_candles(closes))
    eng, t = _Engine(om), _trade(entry=101.0, entry_ts=OPEN)
    assert eng._staged_rsi_step("NSE:X-EQ", t, ltp=95.0) is None
    assert om.partials == [] and om.moves == []


def test_other_profiles_are_untouched(monkeypatch):
    om = _OM(_candles(_falling()))
    eng = _Engine(om)
    t = _trade()
    t["exit_profile"] = "EOD_HOLD"
    assert eng._staged_rsi_step("NSE:X-EQ", t, ltp=90.0) is None
    assert om.partials == [] and om.moves == []
