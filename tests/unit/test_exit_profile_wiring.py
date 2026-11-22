"""
The wiring that keeps an EOD_HOLD trade intact from signal to exit.

ExitProfile.EOD_HOLD means "run on the hard stop to the 15:10 square-off". Three
separate mechanisms in the engine will each cut such a trade short on their own —
the take-profit ladder, the 45-minute time stop, and the 1.5% soft stop, which
sits INSIDE a typical hard stop (1.63% median on the top_coil setups this profile
was built for) and would silently govern about half of those exits. Each is
pinned below, because any one of them turns a hold-to-close strategy into a
different strategy without failing anything else.

Which strategy asks for EOD_HOLD, and with what parameters, is the strategy's
business (see strategy/sdk.py); these tests pin only the engine's side.
"""
from __future__ import annotations

import pytest

import shortcircuit.config as cfg
from shortcircuit.execution import focus_engine as fe


class _Engine:
    """A FocusEngine reduced to start_focus and the state that method touches."""

    start_focus = fe.FocusEngine.start_focus

    def __init__(self, order_manager=None):
        self.order_manager = order_manager
        self.active_trade = None
        self.is_running = False
        self.thread = None
        self._consecutive_flat_reads = 1
        self._api_fail_streak = 1
        self._last_broker_pos_check = 0.0
        self._flat_check_not_before = 0.0

    # The real loop polls the broker at 5Hz. These tests only care about the trade
    # dict start_focus builds before it spawns the thread.
    def focus_loop(self):
        return None


def _pos(**over):
    """A filled short: entry 100, hard stop 101.63 — the measured median risk."""
    base = {
        'symbol': 'NSE:T-EQ',
        'entry_price': 100.0,
        'stop_loss': 101.63,
        'qty': 20,
        'tick_size': 0.05,
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _short_side(monkeypatch):
    monkeypatch.setattr(cfg, "TRADE_DIRECTION", "SHORT", raising=False)
    monkeypatch.setattr(cfg, "TP_MODE", "SCALE", raising=False)


# The soft stop must not pre-empt the measured hard stop

def test_eod_hold_pushes_the_soft_stop_behind_the_hard_stop():
    eng = _Engine()
    eng.start_focus('NSE:T-EQ', _pos(exit_profile='EOD_HOLD'))
    t = eng.active_trade
    assert t['soft_sl'] > t['sl'], (
        "a soft stop inside the hard stop exits the trade early and turns "
        "EOD_HOLD back into the variant that lost 0.21% per trade"
    )


def test_default_trades_keep_their_soft_stop():
    """The change must be confined to EOD_HOLD — the primary strategy's 1.5%
    soft stop is load-bearing for it and is not being revisited here."""
    eng = _Engine()
    eng.start_focus('NSE:T-EQ', _pos())
    assert eng.active_trade['soft_sl'] == pytest.approx(101.5)


def test_soft_stop_is_untouched_when_there_is_no_hard_stop():
    eng = _Engine()
    eng.start_focus('NSE:T-EQ', _pos(exit_profile='EOD_HOLD', stop_loss=0.0))
    assert eng.active_trade['soft_sl'] == pytest.approx(101.5)


# No take-profit ladder

def test_eod_hold_runs_without_a_take_profit():
    eng = _Engine(order_manager=None)
    eng.start_focus('NSE:T-EQ', _pos(exit_profile='EOD_HOLD'))
    t = eng.active_trade
    assert t['tp_mode'] == 'OFF'
    assert t['tp_1'] is None and t['tp'] is None


def test_eod_hold_never_asks_for_take_profit_levels():
    """compute_take_profits is the source of the 1%-of-entry target the whole
    book has been exiting at. An EOD_HOLD trade must not even consult it."""
    class _OM:
        def __init__(self):
            self.calls = 0

        def compute_take_profits(self, entry, pos):
            self.calls += 1
            return {'tp': entry * 0.99}

    om = _OM()
    eng = _Engine(order_manager=om)
    eng.start_focus('NSE:T-EQ', _pos(exit_profile='EOD_HOLD'))
    assert om.calls == 0

    eng.start_focus('NSE:T-EQ', _pos())       # a default trade still does
    assert om.calls == 1


# No 45-minute time stop

def test_time_stop_skips_eod_hold_positions():
    """The edge is in the trades that take longer than 45 minutes to work: the
    thesis is distribution into the close, not a fading 45-minute impulse."""
    src = fe.__file__
    with open(src, encoding="utf-8") as fh:
        body = fh.read()
    assert "not _eod_hold and _max_hold > 0" in body, (
        "the time stop no longer checks the exit profile — EOD_HOLD positions "
        "will be closed at MAX_HOLD_TIME_MINUTES"
    )


def test_position_state_carries_the_exit_profile():
    """focus_engine's time stop reads the profile off active_positions, not off
    the signal — the signal is long gone by the time the time stop runs."""
    from shortcircuit.execution import order_manager as om

    with open(om.__file__, encoding="utf-8") as fh:
        body = fh.read()
    assert "'exit_profile': _exit_profile," in body
    assert "'exit_params': signal.get('exit_params')," in body, (
        "the strategy's exit parameters (its RSI cover) must ride on the position; "
        "focus_engine reads them from there, not from global config"
    )
    assert "None if _eod_hold else self.compute_take_profits(ltp, signal)" in body


def test_start_focus_carries_the_exit_params_into_the_trade():
    params = {'rsi_cover': {'threshold': 40.0, 'period': 14, 'skip_minutes': 5, 'min_candles': 30}}
    eng = _Engine()
    eng.start_focus('NSE:T-EQ', _pos(exit_profile='EOD_HOLD', exit_params=params))
    assert eng.active_trade['exit_params'] == params
    assert fe.rsi_cover_of(eng.active_trade).threshold == 40.0
