"""
Hold-to-close (EOD_HOLD) setups are recorded once, and graded the way the engine
trades them. Found on the top_coil strategy; enforced for every strategy that
asks for EOD_HOLD or a re-arm window.

Two faults, both visible in the 22-24 Sep reports:

* A coil re-qualifies on every bar it stays tight, and each re-qualification wrote
  a fresh ML row and signal-log line. One JAYKAY setup on 23 Sep became 43
  "missed trades", which is why that day's audit processed 43 and 24 Sep's 58.
* The ghost audit graded them on the VWAP-target path: entered at the price when
  the coil ARMED (a top-coil trade only exists once the trigger breaks), exited at
  a VWAP target EOD_HOLD never uses. Its win rates measured a different strategy.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from shortcircuit.eod.eod_analyzer import EODAnalyzer, IST
from shortcircuit.execution import analyzer as an_mod
from shortcircuit.execution.analyzer import FyersAnalyzer
from shortcircuit.strategy import features as F
from shortcircuit.strategy.sdk import (
    Direction, ExitPlan, ExitProfile, RsiCover, SetupContext, Signal, Strategy,
)


# ── grading ──────────────────────────────────────────────────────────────────

ARMED = dt.datetime(2026, 9, 23, 11, 49, tzinfo=IST)


def _bars(rows, start=ARMED + dt.timedelta(minutes=1)):
    """rows: (high, low, close) per minute from `start`."""
    return pd.DataFrame({
        "dt": [start + dt.timedelta(minutes=i) for i in range(len(rows))],
        "high": [r[0] for r in rows], "low": [r[1] for r in rows],
        "close": [r[2] for r in rows],
    })


def _obs(**over):
    base = {"exit_profile": "EOD_HOLD", "trigger_price": 216.06, "coil_high": 218.0,
            "sl_price": 219.71, "armed_at": ARMED.isoformat()}
    base.update(over)
    return base


@pytest.fixture
def eod():
    return EODAnalyzer()


def test_a_trigger_that_never_breaks_is_not_a_trade(eod):
    r = eod._simulate_eod_hold(_obs(), _bars([(217.5, 216.5, 217.0)] * 20))
    assert r["outcome"] == "NOT_TRIGGERED" and r["exit_reason"] == "EXPIRED"


def test_a_push_through_the_coil_high_cancels_before_entry(eod):
    rows = [(217.5, 216.5, 217.0), (218.6, 217.0, 218.4), (218.0, 215.0, 215.5)]
    r = eod._simulate_eod_hold(_obs(), _bars(rows))
    assert r["outcome"] == "NOT_TRIGGERED" and r["exit_reason"] == "INVALIDATED"


def test_the_trigger_must_break_inside_fifteen_minutes(eod):
    rows = [(217.5, 216.5, 217.0)] * 16 + [(216.5, 215.0, 215.2)]
    assert eod._simulate_eod_hold(_obs(), _bars(rows))["outcome"] == "NOT_TRIGGERED"


def test_entered_then_stopped(eod):
    rows = [(216.5, 216.0, 216.1), (219.8, 216.0, 219.5)]
    r = eod._simulate_eod_hold(_obs(), _bars(rows))
    assert r["exit_reason"] == "SL_HIT" and r["outcome"] == "LOSS"
    assert r["exit_price"] == pytest.approx(219.71)


def test_entered_and_held_to_the_square_off_not_to_a_target(eod):
    """JAYKAY on 23 Sep: entered 216.05, held, closed ~212.6. No VWAP target."""
    start = ARMED + dt.timedelta(minutes=1)
    minutes = int((dt.datetime(2026, 9, 23, 15, 20, tzinfo=IST) - start).total_seconds() // 60)
    rows = [(216.5, 216.0, 216.1)] + [(214.0, 212.0, 212.6)] * (minutes - 1)
    rows[-1] = (250.0, 200.0, 240.0)                 # after 15:10: must be ignored
    r = eod._simulate_eod_hold(_obs(), _bars(rows, start))
    assert r["exit_reason"] == "EOD_SQUAREOFF" and r["outcome"] == "WIN"
    assert r["exit_price"] == pytest.approx(212.6)


def test_rows_without_levels_are_skipped_not_guessed(eod):
    assert eod._simulate_eod_hold(_obs(trigger_price=None), _bars([(1, 1, 1)])) is None


def test_never_triggered_setups_stay_out_of_the_win_rate(eod):
    stats = {"total_pnl": 0.0, "win_rate": 0, "winners": 0, "losers": 0, "total_trades": 0}
    audit = {"status": "PASSED", "orphans": 0, "issues": []}
    ghosts = {"processed": 10, "wins": 1, "losses": 1, "tp_hits": 0, "eod_wins": 1,
              "not_triggered": 8}
    text = eod.generate_report("2026-09-23", stats, audit, {"total_decisions": 0}, ghosts)
    assert "Trigger never broke**: 8" in text
    assert "Win Rate**: 50.0%" in text


# ── one observation per setup ────────────────────────────────────────────────

class _ML:
    def __init__(self):
        self.logged, self.updated = [], []

    def log_observation(self, symbol, ltp, features):
        self.logged.append(features)
        return f"obs{len(self.logged)}"

    def update_fields(self, obs_id, **fields):
        self.updated.append((obs_id, fields))
        return True


class _Ctx:
    def get_trend_label(self):
        return "UNKNOWN"


def _frame():
    t0 = dt.datetime(2026, 9, 23, 10, 0)
    px = [210 + i * 0.1 for i in range(40)]
    df = pd.DataFrame({
        "datetime": pd.to_datetime([t0 + dt.timedelta(minutes=i) for i in range(40)]),
        "open": px, "high": [p + 0.2 for p in px], "low": [p - 0.2 for p in px],
        "close": px, "volume": [10_000] * 40,
    })
    F.enrich_dataframe(df)
    return df


class _Coil(Strategy):
    name = "coil_like"
    direction = Direction.SHORT

    def evaluate(self, ctx):          # not called: these tests finalize directly
        return None


class _Fade(Strategy):
    name = "fade_like"
    direction = Direction.SHORT

    def evaluate(self, ctx):
        return None


def _ctx(symbol, ltp, df):
    return SetupContext(
        symbol=symbol, ltp=ltp, now=dt.datetime(2026, 9, 23, 11, 49), df=df, df_15m=None,
        gain_pct=12.0, day_high=float(df["high"].max()), open_price=float(df.iloc[0]["open"]),
        atr=F.compute_atr(df), vwap_sd=F.compute_vwap_sd(df.iloc[:-1]), slope_fast=0.0,
        slope_slow=0.0, is_decaying=False, vol_z=0.0, profile=None, profile_rejection=False,
        upper_circuit=0.0, lower_circuit=0.0, spread_pct=0.0, is_circuit_hitter=False,
    )


def _hold_signal(trigger):
    return Signal(
        pattern="TOP_COIL_BREAKDOWN", stop_reference=218.0, trigger_price=trigger,
        exit=ExitPlan(profile=ExitProfile.EOD_HOLD, rsi_cover=RsiCover()),
        rearm_window=dt.timedelta(minutes=15),
        extra={"coil_high": 218.0}, ml_features={"coil_high": 218.0},
    )


@pytest.fixture
def analyzer(monkeypatch):
    ml = _ML()
    signals = []
    monkeypatch.setattr(an_mod, "get_ml_logger", lambda: ml)
    monkeypatch.setattr(an_mod, "log_signal", lambda *a, **k: signals.append(a[0]))
    a = FyersAnalyzer.__new__(FyersAnalyzer)
    a.market_context = _Ctx()
    a._episodes = {}
    a.ml, a.signals = ml, signals
    return a


def test_a_re_arm_reuses_the_setups_observation(analyzer):
    df = _frame()
    first = analyzer._finalize_signal(_Coil(), _hold_signal(213.59), _ctx("NSE:JAYKAY-EQ", 214.0, df))
    again = analyzer._finalize_signal(_Coil(), _hold_signal(216.06), _ctx("NSE:JAYKAY-EQ", 216.0, df))
    assert len(analyzer.ml.logged) == 1 and len(analyzer.signals) == 1
    assert again["obs_id"] == first["obs_id"], "the trade must label the setup's one row"
    # ...and the row carries the trigger the engine is actually watching now
    assert analyzer.ml.updated[-1][1]["trigger_price"] == 216.06


def test_a_setup_after_the_gates_window_is_a_new_observation(analyzer):
    df = _frame()
    analyzer._finalize_signal(_Coil(), _hold_signal(213.59), _ctx("NSE:JAYKAY-EQ", 214.0, df))
    key = ("coil_like", "NSE:JAYKAY-EQ")
    obs, seen = analyzer._episodes[key]
    analyzer._episodes[key] = (obs, seen - dt.timedelta(minutes=16))
    analyzer._finalize_signal(_Coil(), _hold_signal(214.68), _ctx("NSE:JAYKAY-EQ", 214.7, df))
    assert len(analyzer.ml.logged) == 2


def test_episodes_are_tracked_per_strategy(analyzer):
    """Two strategies arming on one symbol are two setups, not a re-arm."""

    class _Other(_Coil):
        name = "other_coil"

    df = _frame()
    analyzer._finalize_signal(_Coil(), _hold_signal(213.59), _ctx("NSE:X-EQ", 214.0, df))
    analyzer._finalize_signal(_Other(), _hold_signal(213.59), _ctx("NSE:X-EQ", 214.0, df))
    assert len(analyzer.ml.logged) == 2


def test_top_coil_rows_carry_their_levels_and_no_vwap_target(analyzer):
    analyzer._finalize_signal(_Coil(), _hold_signal(203.2), _ctx("NSE:KSCL-EQ", 206.0, _frame()))
    f = analyzer.ml.logged[0]
    assert f["exit_profile"] == "EOD_HOLD" and f["trigger_price"] == 203.2
    assert f["coil_high"] == 218.0 and f["armed_at"]
    assert f["tp_price"] is None
    assert f["exit_params"]["rsi_cover"]["threshold"] == 40.0, (
        "the ghost audit grades the RSI cover from this, not from global config"
    )
    assert f["strategy"] == "coil_like"


def test_other_signals_are_untouched(analyzer):
    """DEFAULT-exit signals without a re-arm window are logged every time and
    keep their VWAP target."""
    df = _frame()
    fade = Signal(pattern="SHOOTING_STAR", stop_reference=float(df["high"].max()))
    for _ in range(2):
        analyzer._finalize_signal(_Fade(), fade, _ctx("NSE:X-EQ", 214.0, df))
    assert len(analyzer.ml.logged) == 2
    assert analyzer.ml.logged[0]["tp_price"] is not None
    assert "exit_profile" not in analyzer.ml.logged[0]
