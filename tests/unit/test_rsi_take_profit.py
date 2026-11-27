"""
The engine's RSI relief-rally cover for EOD_HOLD shorts (ExitPlan.rsi_cover).
Longs run the mirror of it; see test_long_short.py.

A strategy opts in by putting an RsiCover on its exit plan; the parameters ride
on the position (`exit_params`) and on its ML row, so the live loop and the
ghost audit run the strategy's own rule rather than a global config switch.
The rule and its defaults come from top_coil's operator rule:

Rule (30 Sep 2026): once an EOD_HOLD short is in profit, cover when the 1-minute
RSI(14) closes below 40, because a relief rally follows. It does follow — in 82% of
147 historical cases price bounced at least 0.5% within 30 minutes. Measured over
190 trades the average return is unchanged (+0.009% vs +0.058%, t=-0.27) while the
per-trade swing falls from 2.76% to 1.14% and the worst drawdown from -39% to -17%.

What is pinned here is that the live check runs the rule that was measured:
completed bars only, in profit only, never in the first 5 minutes (the breakdown
that triggers the entry drags RSI under 40 by itself — FERMENTA read 39.6 on its
entry bar), and a decided exit is retried every 10s, never re-fired at 5Hz.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pandas as pd
import pytest

import json
from dataclasses import asdict

from shortcircuit.eod.eod_analyzer import EODAnalyzer, IST
from shortcircuit.execution import focus_engine as fe
from shortcircuit.strategy.features import compute_rsi_wilder
from shortcircuit.strategy.sdk import RsiCover

COVER = {"rsi_cover": asdict(RsiCover())}     # threshold 40, period 14, skip 5, warm-up 30

NOW = 1_790_000_000 // 60 * 60 + 30          # 30s into a minute


# ── the RSI itself ───────────────────────────────────────────────────────────

def test_rsi_extremes_and_insufficient_data():
    assert compute_rsi_wilder([10 + 0.1 * i for i in range(40)]) == pytest.approx(100.0)
    assert compute_rsi_wilder([10 - 0.1 * i for i in range(40)]) == pytest.approx(0.0)
    assert compute_rsi_wilder([1.0, 2.0]) != compute_rsi_wilder([1.0, 2.0])   # NaN


def test_rsi_matches_wilder_smoothing():
    """Wilder smoothing (alpha = 1/14), as broker charts draw it — not a simple
    moving average, which reads noticeably different on the same closes."""
    closes = [100, 101, 100.5, 102, 101, 103, 102.5, 101, 100, 99, 99.5, 98, 97.5,
              98.2, 97, 96.5, 97.1, 96, 95.5, 96.2]
    s = pd.Series(closes, dtype=float).diff()
    up = s.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
    dn = (-s.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
    assert compute_rsi_wilder(closes) == pytest.approx(100 - 100 / (1 + up / dn))


# ── the live check ───────────────────────────────────────────────────────────

@dataclass
class _C:
    epoch: int
    close: float


class _Broker:
    def __init__(self, candles):
        self.candles = candles

    def get_local_candles(self, symbol, n=100):
        return self.candles[-n:]


class _OM:
    def __init__(self, broker):
        self.broker = broker


class _Engine:
    _rsi_take_profit_due = fe.FocusEngine._rsi_take_profit_due

    def __init__(self, candles):
        self.order_manager = _OM(_Broker(candles))


def _candles(n_warm=40, after=None, forming=None):
    """n_warm completed bars rising gently, then `after` closes from the entry bar
    on, then optionally a forming bar. The last completed bar ends at NOW's minute."""
    after = after or []
    closes = [100 + 0.05 * i for i in range(n_warm)] + list(after)
    first = NOW // 60 * 60 - 60 * len(closes)
    out = [_C(first + 60 * k, c) for k, c in enumerate(closes)]
    if forming is not None:
        out.append(_C(NOW // 60 * 60, forming))
    return out, first + 60 * n_warm            # candles, entry-bar epoch


def _trade(entry_epoch, entry=102.0, **over):
    t = {'exit_profile': 'EOD_HOLD', 'exit_params': COVER, 'direction': 'SHORT',
         'entry': entry, 'entry_minute': entry_epoch, 'remaining_qty': 3}
    t.update(over)
    return t


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    monkeypatch.setattr(fe.time, "time", lambda: NOW)


FALLING = [101.5, 101.0, 100.6, 100.2, 99.8, 99.4, 99.0]    # entry bar + 6 more


def test_fires_on_a_completed_bar_in_profit_after_the_skip():
    c, e = _candles(after=FALLING)
    rsi = _Engine(c)._rsi_take_profit_due("NSE:X-EQ", _trade(e))
    assert rsi is not None and rsi < 40


def test_not_inside_the_first_five_minutes():
    c, e = _candles(after=FALLING[:4])          # entry bar + 3
    assert _Engine(c)._rsi_take_profit_due("NSE:X-EQ", _trade(e)) is None


def test_not_when_the_trade_is_not_in_profit():
    c, e = _candles(after=FALLING)
    assert _Engine(c)._rsi_take_profit_due("NSE:X-EQ", _trade(e, entry=98.0)) is None


def test_the_forming_bar_is_ignored():
    """A forming bar's RSI swings on every tick; only closes count."""
    c, e = _candles(after=[102.0] * 7, forming=90.0)
    assert _Engine(c)._rsi_take_profit_due("NSE:X-EQ", _trade(e, entry=103.0)) is None


def test_each_bar_is_judged_once():
    c, e = _candles(after=FALLING)
    eng, t = _Engine(c), _trade(e)
    assert eng._rsi_take_profit_due("NSE:X-EQ", t) is not None
    assert eng._rsi_take_profit_due("NSE:X-EQ", t) is None


def test_too_few_candles_leaves_the_position_alone():
    """After a mid-session restart the aggregator has little history; an unwarmed
    RSI is not a signal, so the stop and 15:10 remain the only exits."""
    c, e = _candles(n_warm=15, after=FALLING)
    assert _Engine(c)._rsi_take_profit_due("NSE:X-EQ", _trade(e)) is None


@pytest.mark.parametrize("over", [{'exit_profile': 'DEFAULT'}, {'direction': 'LONG'}])
def test_only_eod_hold_and_only_in_profit(over):
    """A DEFAULT position never takes it. A long on these falling closes is not
    in profit, so it is not sold either; the mirrored rule for longs (sell when
    RSI rises above 100 - threshold) is pinned in test_long_short.py."""
    c, e = _candles(after=FALLING)
    assert _Engine(c)._rsi_take_profit_due("NSE:X-EQ", _trade(e, **over)) is None


@pytest.mark.parametrize("params", [None, {}, {"rsi_cover": None}])
def test_a_position_without_an_rsi_cover_never_takes_it(params):
    """Opt-in per strategy: no cover on the exit plan, no RSI exit."""
    c, e = _candles(after=FALLING)
    assert _Engine(c)._rsi_take_profit_due("NSE:X-EQ", _trade(e, exit_params=params)) is None


def test_the_strategys_own_threshold_is_used():
    c, e = _candles(after=FALLING)
    strict = {"rsi_cover": asdict(RsiCover(threshold=5.0))}
    assert _Engine(c)._rsi_take_profit_due("NSE:X-EQ", _trade(e, exit_params=strict)) is None


def test_a_malformed_cover_disables_the_rule_rather_than_raising():
    c, e = _candles(after=FALLING)
    bad = {"rsi_cover": {"threshold": 40.0, "unknown_field": 1}}
    assert _Engine(c)._rsi_take_profit_due("NSE:X-EQ", _trade(e, exit_params=bad)) is None


def test_a_decided_exit_is_retried_every_ten_seconds_not_redecided(monkeypatch):
    c, e = _candles(after=[102.0] * 7)          # RSI nowhere near 40 now
    eng = _Engine(c)
    t = _trade(e, _rsi_reading=35.0, _rsi_retry_at=NOW + 10)
    assert eng._rsi_take_profit_due("NSE:X-EQ", t) is None           # waiting
    monkeypatch.setattr(fe.time, "time", lambda: NOW + 10)
    assert eng._rsi_take_profit_due("NSE:X-EQ", t) == 35.0            # retry the decision


def test_the_loop_hook_respects_manual_override_and_comes_before_the_tp_engine():
    src = fe.__file__
    body = open(src, encoding="utf-8").read()
    hook = body.index("_rsi_hit = None if manual_override else self._rsi_take_profit_due(symbol, t)")
    assert hook < body.index("# Take-profit engine — see config.TP_MODE")
    assert 'safe_exit(symbol, "RSI_TP")' in body


# ── the ghost audit grades it the same way ───────────────────────────────────

ARMED = dt.datetime(2026, 9, 29, 10, 0, tzinfo=IST)


def _session(after):
    """40 rising warm-up bars ending at ARMED, then `after` bars."""
    start = ARMED - dt.timedelta(minutes=40)
    closes = [100 + 0.05 * i for i in range(40)] + list(after)
    rows = [(max(c, c) + 0.1, c - 0.1, c) for c in closes]
    return pd.DataFrame({
        "dt": [start + dt.timedelta(minutes=i) for i in range(len(closes))],
        "high": [r[0] for r in rows], "low": [r[1] for r in rows], "close": [r[2] for r in rows],
    })


def _obs(exit_params=json.dumps(COVER)):
    """As the ML logger stores it: exit_params is JSON text."""
    return {"exit_profile": "EOD_HOLD", "exit_params": exit_params, "trigger_price": 101.8,
            "coil_high": 102.1, "sl_price": 103.5, "armed_at": ARMED.isoformat()}


def test_ghost_audit_applies_the_rsi_exit():
    sess = _session([101.9, 101.5] + FALLING + [99.0] * 20)
    after = sess[sess["dt"] > ARMED]
    r = EODAnalyzer()._simulate_eod_hold(_obs(), after, session=sess)
    assert r["exit_reason"] == "RSI_TP" and r["outcome"] == "WIN"


def test_ghost_audit_reads_an_in_memory_dict_too():
    sess = _session([101.9, 101.5] + FALLING + [99.0] * 20)
    after = sess[sess["dt"] > ARMED]
    r = EODAnalyzer()._simulate_eod_hold(_obs(exit_params=COVER), after, session=sess)
    assert r["exit_reason"] == "RSI_TP"


@pytest.mark.parametrize("params", [None, float("nan"), "not json", json.dumps({})])
def test_ghost_audit_without_a_cover_holds_to_the_close(params):
    sess = _session([101.9, 101.5] + FALLING + [99.0] * 20)
    after = sess[sess["dt"] > ARMED]
    r = EODAnalyzer()._simulate_eod_hold(_obs(exit_params=params), after, session=sess)
    assert r["exit_reason"] == "EOD_SQUAREOFF"
