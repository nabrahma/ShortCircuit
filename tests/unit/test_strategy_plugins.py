"""
The strategy plug-in system: contract, discovery, and the engine pipeline.

Everything here runs against throwaway strategies written in the test, so the
suite says nothing about — and needs nothing from — the private strategies of
any particular deployment.
"""
from __future__ import annotations

import datetime as dt
import subprocess
import textwrap
from pathlib import Path

import pandas as pd
import pytest

from shortcircuit import config
from shortcircuit.execution import analyzer as an_mod
from shortcircuit.strategy import registry
from shortcircuit.strategy.examples.vwap_stretch_fade import VwapStretchFade
from shortcircuit.strategy.sdk import (
    Direction, ExitPlan, ExitProfile, RsiCover, Signal, Strategy, Universe,
)

REPO = Path(__file__).resolve().parents[2]


# ── the contract ─────────────────────────────────────────────────────────────

def test_rsi_cover_is_for_hold_to_close_exits_only():
    with pytest.raises(ValueError):
        ExitPlan(profile=ExitProfile.DEFAULT, rsi_cover=RsiCover())


def test_a_hold_to_close_exit_has_no_target_to_scale():
    with pytest.raises(ValueError):
        ExitPlan(profile=ExitProfile.EOD_HOLD, tp_atr_mult=1.0)


def test_exit_params_serialise():
    plan = ExitPlan(profile=ExitProfile.EOD_HOLD, rsi_cover=RsiCover(threshold=35.0))
    assert plan.params() == {"rsi_cover": {"threshold": 35.0, "period": 14,
                                           "skip_minutes": 5, "min_candles": 30}}
    assert ExitPlan().params() == {"rsi_cover": None}


def test_strategy_is_abstract():
    class Incomplete(Strategy):
        name = "incomplete"

    with pytest.raises(TypeError):
        Incomplete()


# ── discovery ────────────────────────────────────────────────────────────────

PLUGIN = '''
from shortcircuit.strategy.sdk import Direction, Signal, Strategy

class {cls}(Strategy):
    name = "{name}"
    direction = Direction.{direction}

    def evaluate(self, ctx):
        return None
'''


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv(registry.ENV_ACTIVE, raising=False)
    monkeypatch.delenv(registry.ENV_PATH, raising=False)


def test_a_folder_of_modules_is_a_plugin_directory(tmp_path, clean_env):
    _write(tmp_path / "alpha.py", PLUGIN.format(cls="Alpha", name="alpha", direction="SHORT"))
    _write(tmp_path / "_private.py", "raise RuntimeError('never imported')")
    _write(tmp_path / "test_alpha.py", "raise RuntimeError('never imported')")
    catalog = registry.discover([tmp_path])
    assert "alpha" in catalog.strategies
    assert catalog.errors == []


def test_a_package_declares_what_is_active(tmp_path, clean_env):
    pkg = tmp_path / "deploy_pkg_a"
    _write(pkg / "__init__.py", "ACTIVE = ('beta',)\n")
    _write(pkg / "beta" / "__init__.py", PLUGIN.format(cls="Beta", name="beta", direction="SHORT"))
    _write(pkg / "tests" / "test_x.py", "raise RuntimeError('tests are never imported')")
    strategies = registry.load(directories=[pkg])
    assert [s.name for s in strategies] == ["beta"]


def test_the_environment_overrides_the_declared_set(tmp_path, clean_env, monkeypatch):
    pkg = tmp_path / "deploy_pkg_b"
    _write(pkg / "__init__.py", "ACTIVE = ('gamma',)\n")
    _write(pkg / "gamma.py", PLUGIN.format(cls="Gamma", name="gamma", direction="SHORT"))
    monkeypatch.setenv(registry.ENV_ACTIVE, "example_vwap_stretch_fade, gamma")
    strategies = registry.load(directories=[pkg])
    assert [s.name for s in strategies] == ["example_vwap_stretch_fade", "gamma"]


def test_the_strategy_path_environment_variable_is_honoured(tmp_path, clean_env, monkeypatch):
    _write(tmp_path / "delta.py", PLUGIN.format(cls="Delta", name="delta", direction="SHORT"))
    monkeypatch.setenv(registry.ENV_PATH, str(tmp_path))
    assert registry.plugin_dirs() == [tmp_path.resolve()]
    assert "delta" in registry.discover().strategies


def test_nothing_active_is_allowed_and_loud(tmp_path, clean_env, caplog):
    assert registry.load(directories=[tmp_path]) == []
    assert "no strategy is active" in caplog.text


def test_an_unknown_name_fails_fast_and_says_what_exists(tmp_path, clean_env):
    with pytest.raises(registry.StrategyConfigError, match="example_vwap_stretch_fade"):
        registry.load(["nope"], directories=[tmp_path])


def test_an_import_error_is_reported_with_the_missing_name(tmp_path, clean_env):
    _write(tmp_path / "broken.py", "import does_not_exist_anywhere\n")
    with pytest.raises(registry.StrategyConfigError, match="does_not_exist_anywhere"):
        registry.load(["broken"], directories=[tmp_path])


def test_a_broken_plugin_does_not_block_the_others(tmp_path, clean_env):
    _write(tmp_path / "broken.py", "raise RuntimeError('boom')\n")
    _write(tmp_path / "fine.py", PLUGIN.format(cls="Fine", name="fine", direction="SHORT"))
    assert [s.name for s in registry.load(["fine"], directories=[tmp_path])] == ["fine"]


def test_two_strategies_cannot_share_a_name(tmp_path, clean_env):
    _write(tmp_path / "one.py", PLUGIN.format(cls="One", name="same", direction="SHORT"))
    _write(tmp_path / "two.py", PLUGIN.format(cls="Two", name="same", direction="SHORT"))
    with pytest.raises(registry.StrategyConfigError, match="two strategies are named 'same'"):
        registry.discover([tmp_path])


def test_listing_a_strategy_twice_is_an_error(tmp_path, clean_env):
    with pytest.raises(registry.StrategyConfigError, match="listed twice"):
        registry.load(["example_vwap_stretch_fade"] * 2, directories=[tmp_path])


def test_a_long_strategy_loads_alongside_a_short_one(tmp_path, clean_env):
    """The engine runs both sides; each strategy keeps its own."""
    _write(tmp_path / "longy.py", PLUGIN.format(cls="Longy", name="longy", direction="LONG"))
    _write(tmp_path / "shorty.py", PLUGIN.format(cls="Shorty", name="shorty", direction="SHORT"))
    loaded = registry.load(["longy", "shorty"], directories=[tmp_path])
    assert [(s.name, s.direction) for s in loaded] == [("longy", Direction.LONG), ("shorty", Direction.SHORT)]


def test_a_universe_that_is_not_a_universe_is_refused(tmp_path, clean_env):
    _write(tmp_path / "odd.py", PLUGIN.format(cls="Odd", name="odd", direction="LONG")
           + "\nOdd.universe = 'LOSERS'\n")
    with pytest.raises(registry.StrategyConfigError, match="universe"):
        registry.load(["odd"], directories=[tmp_path])


# ── the pipeline ─────────────────────────────────────────────────────────────

class _GRL:
    def __init__(self):
        self.records = []

    def record(self, gr):
        self.records.append(gr)


class _ML:
    def __init__(self):
        self.logged = []

    def log_observation(self, symbol, ltp, features):
        self.logged.append(features)
        return f"obs{len(self.logged)}"

    def update_fields(self, obs_id, **fields):
        return True


class _Ctx:
    def __init__(self, *a, **k):
        pass

    def is_safe_trade_window(self):
        return True, "OK"

    def get_volume_z_score(self, df):
        return 0.0

    def mark_circuit_touched(self, s):
        pass

    def is_circuit_hitter(self, s):
        return False

    def mark_lower_circuit_touched(self, s):
        pass

    def is_lower_circuit_hitter(self, s):
        return False

    def get_trend_label(self):
        return "UNKNOWN"


class _HTF:
    calls = 0

    def __init__(self, *a, **k):
        pass

    directions: list = []

    def check_trend_exhaustion(self, symbol, df_15m=None, vwap_sd=0.0, direction="SHORT"):
        _HTF.calls += 1
        _HTF.directions.append(direction)
        return True, "ok"


class _SM:
    def can_signal(self, symbol, confidence=""):
        return True, ""


class _Fyers:
    def depth(self, data=None):
        return {"d": {}}


def _frame(n=40, step=0.6):
    """A steady rally with wide bars, so ATR is well above the 0.25 buffer floor."""
    t0 = dt.datetime(2026, 9, 21, 10, 30)
    closes = [100.0 + i * step for i in range(n)]
    return pd.DataFrame({
        "datetime": pd.to_datetime([t0 + dt.timedelta(minutes=i) for i in range(n)]),
        "open": closes, "high": [c + 1.5 for c in closes], "low": [c - 1.5 for c in closes],
        "close": closes, "volume": [10_000] * n,
    })


@pytest.fixture
def engine(monkeypatch):
    grl, ml = _GRL(), _ML()
    monkeypatch.setattr(an_mod, "get_gate_result_logger", lambda: grl)
    monkeypatch.setattr(an_mod, "get_ml_logger", lambda: ml)
    monkeypatch.setattr(an_mod, "log_signal", lambda *a, **k: None)
    monkeypatch.setattr(an_mod, "MarketContext", _Ctx)
    monkeypatch.setattr(an_mod, "HTFConfluence", _HTF)
    monkeypatch.setattr(an_mod, "get_signal_manager", lambda: _SM())
    monkeypatch.setattr(config, "TRADE_DIRECTION", "SHORT")
    _HTF.calls = 0
    _HTF.directions = []

    def run(*strategies, df=None):
        a = an_mod.FyersAnalyzer(_Fyers(), strategies=list(strategies))
        df = _frame() if df is None else df
        out = a.check_setup("NSE:X-EQ", float(df.iloc[-1]["close"]), pre_fetched_df=df)
        return out, grl.records[-1], ml.logged
    return run


def _strategy(name, result=None, *, raises=None, direction=Direction.SHORT, htf=True):
    seen = []

    class S(Strategy):
        requires_htf_confirmation = htf

        def evaluate(self, ctx):
            seen.append(ctx)
            if raises:
                raise raises
            return result(ctx) if callable(result) else result

    S.name, S.direction = name, direction
    s = S()
    s.seen = seen
    return s


def _short(ctx, **over):
    base = dict(pattern="TEST", stop_reference=ctx.day_high)
    base.update(over)
    return Signal(**base)


def test_the_first_strategy_with_a_signal_wins(engine):
    first = _strategy("first", None)
    second = _strategy("second", _short)
    third = _strategy("third", _short)
    out, gr, _ = engine(first, second, third)
    assert out["strategy"] == "second"
    assert len(first.seen) == 1 and len(third.seen) == 0, "later strategies are not asked"
    assert gr.verdict == "ANALYZER_PASS"


def test_no_signal_is_a_g5_rejection_naming_the_strategies(engine):
    out, gr, _ = engine(_strategy("a"), _strategy("b"))
    assert out is None
    assert gr.first_fail_gate == "G5_STRATEGY" and "a, b" in gr.rejection_reason


def test_no_strategies_rejects_every_candidate(engine):
    out, gr, _ = engine()
    assert out is None and gr.rejection_reason == "No strategy active"


def test_a_raising_strategy_fails_closed_and_the_next_one_still_runs(engine):
    out, _, _ = engine(_strategy("boom", raises=RuntimeError("bug")), _strategy("ok", _short))
    assert out["strategy"] == "ok"


@pytest.mark.parametrize("bad", [
    dict(stop_reference=0.0),
    dict(stop_reference=float("nan")),
    dict(stop_reference=True),
    dict(trigger_price=-1.0),
    dict(trigger_price=10_000.0),          # above the stop: not a short
])
def test_an_unusable_signal_is_ignored(engine, bad):
    out, gr, _ = engine(_strategy("bad", lambda c: _short(c, **bad)))
    assert out is None and gr.first_fail_gate == "G5_STRATEGY"


def test_a_strategy_of_the_other_direction_is_never_asked(engine, monkeypatch):
    """TRADE_DIRECTION can be flipped from Telegram; a short setup must never be
    executed as a buy."""
    monkeypatch.setattr(config, "TRADE_DIRECTION", "LONG")
    s = _strategy("shorty", _short)
    out, _, _ = engine(s)
    assert out is None and s.seen == []


# ── long strategies ──────────────────────────────────────────────────────────

def _long(ctx, **over):
    base = dict(pattern="TEST", stop_reference=ctx.day_low)
    base.update(over)
    return Signal(**base)


def _falling():
    """The rally's mirror: a steady slide, so the candidate is a loser."""
    return _frame(step=-0.6)


@pytest.fixture
def both(monkeypatch):
    monkeypatch.setattr(config, "TRADE_DIRECTION", "BOTH")


def test_a_long_signal_trades_its_own_side(engine, both):
    df = _falling()
    out, _, ml = engine(_strategy("longy", _long, direction=Direction.LONG), df=df)
    assert out["direction"] == out["side"] == "LONG"
    # The gate buys the break above the previous bar's high and is abandoned
    # below the setup low; the stop sits under that low by the ATR buffer.
    assert out["signal_high"] == df.iloc[-2]["high"]
    assert out["signal_low"] == out["setup_low"] == df["low"].min()
    assert out["stop_loss"] == pytest.approx(df["low"].min() - max(out["atr"] * 0.5, 0.25))
    assert "setup_high" not in out
    assert ml[-1]["direction"] == "LONG"


def test_a_long_named_trigger_replaces_the_previous_high(engine, both):
    df = _falling()
    trig = float(df["low"].min()) + 5.0
    out, _, _ = engine(_strategy("l", lambda c: _long(c, trigger_price=trig),
                                 direction=Direction.LONG), df=df)
    assert out["signal_high"] == out["trigger_price"] == pytest.approx(trig)


def test_a_long_trigger_must_sit_above_its_setup_low(engine, both):
    df = _falling()
    trig = float(df["low"].min()) - 1.0
    out, gr, _ = engine(_strategy("l", lambda c: _long(c, trigger_price=trig),
                                  direction=Direction.LONG), df=df)
    assert out is None and gr.first_fail_gate == "G5_STRATEGY"


def test_a_long_percentage_stop_sits_below_the_price(engine, both):
    df = _falling()
    plan = ExitPlan(stop_pct=2.0)
    out, _, _ = engine(_strategy("l", lambda c: _long(c, exit=plan), direction=Direction.LONG), df=df)
    assert out["stop_loss"] == pytest.approx(float(df.iloc[-1]["close"]) * 0.98)


@pytest.mark.parametrize("mode, longs_asked, shorts_asked", [
    ("BOTH", True, True), ("LONG", True, False), ("SHORT", False, True),
])
def test_the_direction_filter_decides_which_side_is_asked(engine, monkeypatch, mode,
                                                          longs_asked, shorts_asked):
    monkeypatch.setattr(config, "TRADE_DIRECTION", mode)
    movers = Universe.MOVERS
    lng, sht = _strategy("l", None, direction=Direction.LONG), _strategy("s", None)
    type(lng).universe = type(sht).universe = movers
    engine(lng, sht)
    assert bool(lng.seen) is longs_asked and bool(sht.seen) is shorts_asked


def test_each_strategy_is_offered_its_own_movers(engine, both):
    """AUTO: a short is offered gainers, a long losers."""
    lng, sht = _strategy("l", None, direction=Direction.LONG), _strategy("s", None)
    engine(lng, sht)                                  # a rally: a gainer
    assert lng.seen == [] and len(sht.seen) == 1
    engine(lng, sht, df=_falling())                   # a slide: a loser
    assert len(lng.seen) == 1 and len(sht.seen) == 1


def test_a_movers_strategy_is_offered_both_sides(engine, both):
    m = _strategy("m", None, direction=Direction.LONG)
    type(m).universe = Universe.MOVERS
    engine(m)
    engine(m, df=_falling())
    assert len(m.seen) == 2


def test_the_scanners_band_decides_not_the_change_from_the_open(engine, both, monkeypatch):
    """With no previous close the analyzer's gain is from the open, so a stock
    that gapped up and faded reads as falling. The scanner's own day change says
    it is a gainer, and that decides who is offered it."""
    sht = _strategy("s", None)
    a = an_mod.FyersAnalyzer(_Fyers(), strategies=[sht])
    df = _falling()
    a.check_setup("NSE:X-EQ", float(df.iloc[-1]["close"]), pre_fetched_df=df, day_change_pct=9.5)
    assert len(sht.seen) == 1


def test_g9_is_asked_about_the_strategys_own_side(engine, both):
    engine(_strategy("l", _long, direction=Direction.LONG), df=_falling())
    engine(_strategy("s", _short))
    assert _HTF.directions == ["LONG", "SHORT"]


def test_the_circuit_band_rides_on_the_signal(engine, both):
    out, _, _ = engine(_strategy("s", _short))
    assert out["upper_circuit"] == 0.0 and out["lower_circuit"] == 0.0


def test_g9_runs_only_for_strategies_that_ask_for_it(engine):
    engine(_strategy("wants", _short, htf=True))
    assert _HTF.calls == 1
    out, gr, _ = engine(_strategy("skips", _short, htf=False))
    assert _HTF.calls == 1 and gr.g9_pass is True and "not required" in gr.g9_value


def test_the_stop_is_the_reference_plus_the_atr_buffer(engine):
    out, _, _ = engine(_strategy("s", lambda c: _short(c, stop_reference=150.0)))
    assert out["atr"] > 0.5, "this fixture must exercise the ATR branch, not the floor"
    assert out["stop_loss"] == pytest.approx(150.0 + max(out["atr"] * 0.5, 0.25))
    assert out["setup_high"] == out["signal_high"] == 150.0


def test_a_named_trigger_replaces_the_default_entry_level(engine):
    out, _, _ = engine(_strategy("s", lambda c: _short(c, trigger_price=120.0)))
    assert out["signal_low"] == out["trigger_price"] == 120.0


def test_without_a_trigger_the_entry_is_the_previous_bars_low(engine):
    df = _frame()
    out, _, _ = engine(_strategy("s", _short), df=df)
    assert out["signal_low"] == df.iloc[-2]["low"]
    assert "trigger_price" not in out


def test_the_exit_plan_reaches_the_signal_dict(engine):
    hold = lambda c: _short(c, exit=ExitPlan(profile=ExitProfile.EOD_HOLD, rsi_cover=RsiCover()))
    out, _, ml = engine(_strategy("h", hold))
    assert out["exit_profile"] == "EOD_HOLD"
    assert out["exit_params"]["rsi_cover"]["threshold"] == 40.0
    assert "tp_atr_mult_override" not in out
    assert ml[0]["tp_price"] is None and ml[0]["strategy"] == "h"

    scaled = lambda c: _short(c, exit=ExitPlan(tp_atr_mult=0.5))
    out, _, _ = engine(_strategy("d", scaled))
    assert out["tp_atr_mult_override"] == 0.5
    assert "exit_profile" not in out and "exit_params" not in out


def test_extra_levels_ride_on_the_signal_but_never_overwrite_engine_fields(engine):
    out, _, ml = engine(_strategy("x", lambda c: _short(
        c, extra={"box_high": 1.0, "stop_loss": -1.0}, ml_features={"box_high": 1.0})))
    assert out["box_high"] == 1.0
    assert out["stop_loss"] > 0, "a strategy cannot overwrite the engine's stop"


def test_the_shipped_example_is_a_valid_strategy(engine):
    catalog = registry.discover([])
    assert catalog.strategies["example_vwap_stretch_fade"] is VwapStretchFade
    out, gr, _ = engine(VwapStretchFade())
    assert out is None or out["strategy"] == "example_vwap_stretch_fade"
    assert gr.verdict in ("ANALYZER_PASS", "REJECTED")


# ── privacy ──────────────────────────────────────────────────────────────────

def _git_ignored(path: str) -> bool:
    try:
        r = subprocess.run(["git", "check-ignore", "-q", path], cwd=REPO,
                           capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("git unavailable")
    if r.returncode == 128:
        pytest.skip("not a git working tree")
    return r.returncode == 0


@pytest.mark.parametrize("path", [
    "strategies/__init__.py", "strategies/my_edge/__init__.py",
    "strategies/my_edge/params.py", "strategies/tests/test_my_edge.py",
])
def test_private_strategies_never_reach_the_repository(path):
    assert _git_ignored(path), f"{path} is not git-ignored"


def test_the_strategies_readme_is_public():
    assert not _git_ignored("strategies/README.md")


# ── a fresh clone can write, test and run a plug-in ──────────────────────────
#
# Each of these failed on a clean checkout once; none could fail on a machine
# whose private strategies/conftest.py papered over them.

def _top_level_functions(path: Path) -> set[str]:
    import ast
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}


def test_the_safety_guards_cover_plugin_tests():
    # A conftest governs only its own directory. Under tests/ the guards would
    # leave strategies/tests/ free to reach a live broker with real credentials.
    guards = {"no_network", "no_real_credentials"}
    assert guards <= _top_level_functions(REPO / "conftest.py")
    assert not guards & _top_level_functions(REPO / "tests" / "conftest.py"), \
        "defined twice: the copies would drift"


def test_plugin_tests_can_import_the_engine_and_their_strategy():
    import tomllib
    ini = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    pythonpath = ini["tool"]["pytest"]["ini_options"]["pythonpath"]
    assert "src" in pythonpath, "`import shortcircuit` fails in strategies/tests/"
    assert "." in pythonpath, "`from strategies.my_fade import MyFade` fails"


def test_the_runtime_image_can_import_the_package():
    dockerfile = (REPO / "deploy" / "Dockerfile").read_text(encoding="utf-8")
    shared, _, runtime = dockerfile.partition("FROM base AS runtime")
    assert '"-m", "shortcircuit"' in runtime
    assert "PYTHONPATH=/app/src" in shared, \
        "nothing installs the package, so ENTRYPOINT and HEALTHCHECK cannot import it"
