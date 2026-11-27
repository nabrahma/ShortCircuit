"""
The Telegram console: a command registry the deployment can extend, one
authorization gate for every command, and cards that describe any strategy from
the plug-in contract alone. No network: updates and the broker are stand-ins.
"""
from __future__ import annotations

import asyncio
import textwrap
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from shortcircuit import config
from shortcircuit.eod import squareoff
from shortcircuit.execution import strategy_switch
from shortcircuit.observability.telegram import ShortCircuitBot, cards, exits, fmt
from shortcircuit.observability.telegram.book import StrategyBook, blurb
from shortcircuit.observability.telegram.commands import Command, CommandRegistry
from shortcircuit.strategy.sdk import Direction, Strategy, Universe

CHAT = "4242"


class _Msg:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, parse_mode=None, reply_markup=None):
        self.sent.append((text, parse_mode, reply_markup))
        return SimpleNamespace(message_id=len(self.sent))


def _update(chat=CHAT):
    return SimpleNamespace(effective_chat=SimpleNamespace(id=int(chat)), effective_message=_Msg())


def _bot(**cfg):
    return ShortCircuitBot({"TELEGRAM_CHAT_ID": CHAT, "TELEGRAM_BOT_TOKEN": "x", **cfg}, None, None)


async def _call(bot, name, *args, chat=CHAT, update=None):
    upd = update or _update(chat)
    await bot._guarded(bot.registry.get(name))(upd, SimpleNamespace(args=list(args)))
    return upd


def _texts(upd):
    return [t for t, _, _ in upd.effective_message.sent]


def _say(bot, name, *args, chat=CHAT):
    return "\n".join(_texts(asyncio.run(_call(bot, name, *args, chat=chat))))


class Fade(Strategy):
    """Shorts stretched tops.

    A second paragraph the list does not show."""

    name = "fade"
    direction = Direction.SHORT

    def evaluate(self, ctx):
        return None

    def describe(self):
        return {"Stop": "7%", "Odd <tag>": 1.5}


class Bounce(Strategy):
    name = "bounce"
    direction = Direction.LONG
    universe = Universe.LOSERS
    description = "Buys capitulation on red days."

    def evaluate(self, ctx):
        return None

    def describe(self):
        raise RuntimeError("boom")


@pytest.fixture(autouse=True)
def _clean():
    strategy_switch.reset()
    yield
    strategy_switch.reset()


@pytest.fixture
def at(monkeypatch):
    def set_time(h, m, day=27):
        monkeypatch.setattr(fmt, "now_ist", lambda: datetime(2026, 11, day, h, m, tzinfo=fmt.IST))
    return set_time


def _short_signal(**over):
    base = {"symbol": "NSE:ABC-EQ", "strategy": "fade", "direction": "SHORT", "ltp": 101.0,
            "signal_low": 100.5, "signal_high": 104.0, "stop_loss": 104.6, "pattern": "TOP_FADE",
            "confidence": "HIGH"}
    base.update(over)
    return base


# ── the registry ─────────────────────────────────────────────────────────────

def test_every_builtin_command_is_in_help_with_a_summary():
    bot = _bot()
    help_text = bot.registry.help_html()
    for c in bot.registry:
        assert c.summary and f"/{c.name}" in help_text
    for name in ("auto", "mode", "pause", "resume", "strategies", "strategy",
                 "status", "positions", "signals", "health", "help", "stop"):
        assert name in bot.registry


def test_the_menu_holds_only_menu_commands_within_telegram_limits():
    bot = _bot()
    menu = dict(bot.registry.menu())
    assert {"auto", "status", "strategies", "help"} <= set(menu)
    assert "resume" not in menu
    assert all(len(n) <= 32 and 0 < len(d) <= 256 for n, d in menu.items())


def test_a_command_name_cannot_be_registered_twice():
    reg = CommandRegistry()
    reg.add(Command("ping", "x", lambda *a: None))
    with pytest.raises(ValueError, match="already registered"):
        reg.add(Command("ping", "y", lambda *a: None))


@pytest.mark.parametrize("bad", ["Bad", "with-dash", "", "x" * 33, "spa ce"])
def test_command_names_follow_telegram_rules(bad):
    with pytest.raises(ValueError):
        Command(bad, "summary", lambda *a: None)


def test_a_deployment_adds_its_own_commands_without_touching_the_engine(tmp_path, monkeypatch):
    (tmp_path / "my_cmds.py").write_text(textwrap.dedent("""
        from shortcircuit.observability.telegram.commands import Command

        async def ping(bot, update, context):
            await bot.reply(update, "pong " + " ".join(context.args))

        def register(registry):
            registry.add(Command("ping", "Answer pong", ping, group="Mine", menu=True))
    """), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    bot = _bot(TELEGRAM_COMMAND_MODULES=("my_cmds",))
    assert "ping" in bot.registry and not bot.command_errors
    assert "/ping" in bot.registry.help_html() and "<b>Mine</b>" in bot.registry.help_html()
    assert _say(bot, "ping", "a", "b") == "pong a b"


def test_a_broken_command_module_is_reported_and_the_rest_still_load(tmp_path, monkeypatch):
    (tmp_path / "broken_cmds.py").write_text("import does_not_exist\n", encoding="utf-8")
    (tmp_path / "shadow_cmds.py").write_text(textwrap.dedent("""
        from shortcircuit.observability.telegram.commands import Command
        def register(registry):
            registry.add(Command("status", "hijack", lambda *a: None))
    """), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    bot = _bot(TELEGRAM_COMMAND_MODULES=("broken_cmds", "shadow_cmds", "no_such_module"))
    assert set(bot.command_errors) == {"broken_cmds", "shadow_cmds", "no_such_module"}
    assert bot.registry.get("status").summary != "hijack", "a plug-in cannot replace a built-in"


# ── the gate ─────────────────────────────────────────────────────────────────

def test_a_stranger_gets_no_answer_and_changes_nothing():
    bot = _bot(AUTO_MODE=True)
    assert _say(bot, "auto", "off", chat="999") == ""
    assert bot.is_auto_mode() is True


def test_every_command_is_registered_behind_the_gate():
    bot = _bot()
    added = []
    bot.app = SimpleNamespace(add_handler=added.append)
    bot._register_handlers()
    names = [h.commands for h in added if hasattr(h, "commands")]
    assert sorted(n for s in names for n in s) == sorted(c.name for c in bot.registry)


def test_a_failing_command_answers_with_the_error():
    bot = _bot()

    async def explode(bot, update, context):
        raise ZeroDivisionError("division by zero")

    bot.registry.add(Command("explode", "x", explode))
    out = _say(bot, "explode")
    assert "/explode failed" in out and "ZeroDivisionError" in out


# ── trading controls ─────────────────────────────────────────────────────────

def test_pause_and_resume_one_strategy_or_all():
    bot = _bot()
    bot.attach_strategies([Fade(), Bounce()])
    assert "Paused" in _say(bot, "pause", "FADE")
    assert strategy_switch.paused() == {"fade"}
    assert "Already paused" in _say(bot, "pause", "fade")
    assert "Not an active strategy: nope" in _say(bot, "pause", "nope")
    assert "Resumed" in _say(bot, "resume", "all")
    assert strategy_switch.paused() == set()


def test_pause_without_a_name_lists_what_is_active():
    bot = _bot()
    bot.attach_strategies([Fade(), Bounce()])
    out = _say(bot, "pause")
    assert "Usage" in out and "fade" in out and "bounce" in out
    assert strategy_switch.paused() == set()


def test_mode_switches_the_direction_filter(monkeypatch):
    monkeypatch.setattr(config, "TRADE_DIRECTION", "BOTH")
    bot = _bot()
    assert "LONG only" in _say(bot, "mode", "long")
    assert config.TRADE_DIRECTION == "LONG"
    assert "Usage" in _say(bot, "mode", "sideways")
    assert config.TRADE_DIRECTION == "LONG"


def test_auto_off_then_on_after_the_open(at):
    at(10, 0)
    bot = _bot(AUTO_MODE=True)
    assert "AUTO TRADE OFF" in _say(bot, "auto", "off")
    assert bot.is_auto_mode() is False
    assert "Auto mode ON" in _say(bot, "auto", "on")
    assert bot.is_auto_mode() is True


def test_auto_on_before_the_open_is_queued_for_0930(at):
    at(9, 10)
    bot = _bot(AUTO_MODE=False)
    assert "queued" in _say(bot, "auto", "on")
    assert bot.is_auto_mode() is False
    at(9, 31)
    assert bot.is_auto_mode() is True


# ── strategies ───────────────────────────────────────────────────────────────

def test_strategies_lists_every_plugin_with_its_side_state_and_blurb():
    bot = _bot()
    bot.attach_strategies([Fade(), Bounce()])
    strategy_switch.pause(["bounce"])
    out = _say(bot, "strategies")
    assert "2 active, 1 paused" in out
    assert "fade" in out and "SHORT" in out and "Shorts stretched tops." in out
    assert "A second paragraph" not in out
    assert "bounce" in out and "LONG" in out and "LOSERS" in out and "Buys capitulation" in out
    assert "⏸️ paused" in out


def test_a_strategy_publishes_its_settings_escaped():
    bot = _bot()
    bot.attach_strategies([Fade(), Bounce()])
    out = _say(bot, "strategy", "fade")
    assert "Stop: 7%" in out and "Odd &lt;tag&gt;: 1.5" in out


def test_a_raising_describe_is_shown_not_fatal():
    bot = _bot()
    bot.attach_strategies([Bounce()])
    assert "raised RuntimeError: boom" in _say(bot, "strategy", "bounce")


def test_no_active_strategy_is_said_plainly():
    assert "No strategy is active" in _say(_bot(), "strategies")


def test_blurb_falls_back_to_the_docstring_and_never_to_the_base_class():
    class Bare(Strategy):
        name = "bare"

        def evaluate(self, ctx):
            return None

    assert blurb(Fade()) == "Shorts stretched tops."
    assert blurb(Bare()) == ""


# ── cards ────────────────────────────────────────────────────────────────────

def test_a_short_card_names_its_strategy_trigger_exit_and_square_off():
    text = cards.signal_armed(_short_signal(display={"Range width": "1.8%", "Note": "<b>x</b>"}),
                              auto=True, data_only=False, margin="₹800 (qty 30)")
    assert "<code>fade</code>" in text and "🔴 SHORT" in text
    assert "trigger &lt; ₹100.50" in text and "stop ₹104.60" in text
    assert "square-off 15:10" in text
    assert "Range width: 1.8%" in text and "Note: &lt;b&gt;x&lt;/b&gt;" in text
    assert "auto, to execution" in text and "₹800 (qty 30)" in text


def test_a_long_card_triggers_above_its_high():
    sig = {"symbol": "NSE:XYZ-EQ", "strategy": "bounce", "direction": "LONG", "ltp": 100.0,
           "signal_high": 101.25, "signal_low": 97.0, "stop_loss": 96.5}
    text = cards.signal_armed(sig, auto=False, data_only=False)
    assert "🟢 LONG" in text and "trigger &gt; ₹101.25" in text and "alert only" in text


def test_a_named_trigger_wins_over_the_bar_level():
    text = cards.signal_armed(_short_signal(trigger_price=99.95), auto=True, data_only=False)
    assert "trigger &lt; ₹99.95" in text


def test_an_fno_stock_card_shows_the_brokers_earlier_square_off():
    squareoff.registry.adopt(frozenset({"NSE:ABC-EQ"}))
    try:
        text = cards.signal_armed(_short_signal(), auto=True, data_only=False)
    finally:
        squareoff.registry.adopt(None)
    assert f"square-off {squareoff.cas_squareoff().strftime('%H:%M')}" in text


def test_display_lines_are_capped():
    sig = _short_signal(display={f"k{i}": i for i in range(12)})
    lines = cards.display_lines(sig)
    assert len(lines) == cards.MAX_DISPLAY_LINES + 1 and "4 more" in lines[-1]


def test_data_only_mode_says_no_order_is_placed():
    text = cards.signal_armed(_short_signal(), auto=True, data_only=True)
    assert "data-only mode, no order" in text


@pytest.mark.parametrize("outcome,head,why", [
    ("VALIDATED", "TRIGGERED", "the trigger broke"),
    ("TIMEOUT", "EXPIRED", f"no trigger within {cards.PENDING_MINUTES} min"),
    ("REJECTED", "INVALIDATED", "price ran through the setup level first"),
])
def test_outcome_cards(outcome, head, why):
    reason = {"VALIDATED": "GATE12_TRIGGER_BROKEN", "TIMEOUT": "G11_DYNAMIC_TIMEOUT",
              "REJECTED": "G12_INVALIDATED_BUFFER"}[outcome]
    text = cards.signal_outcome(_short_signal(), outcome, {"reason": reason, "ltp": 100.4},
                                auto=True, data_only=False)
    assert head in text and why in text and "<code>fade</code>" in text
    assert ("Entry" in text) is (outcome == "VALIDATED")


# ── exits, in words ──────────────────────────────────────────────────────────

def test_the_default_exit_follows_the_engine_config(monkeypatch):
    monkeypatch.setattr(config, "TP_MODE", "SCALE")
    monkeypatch.setattr(config, "P52_BREAKEVEN_AFTER_TP1", True)
    monkeypatch.setattr(config, "MAX_HOLD_TIME_MINUTES", 45)
    assert exits.describe({"direction": "SHORT"}) == (
        "half at the midpoint, rest to VWAP, stop to breakeven after the half; 45-min time stop")
    monkeypatch.setattr(config, "TP_MODE", "OFF")
    monkeypatch.setattr(config, "MAX_HOLD_TIME_MINUTES", 0)
    assert exits.describe({}) == "no take-profit"


def test_a_hold_to_close_long_mirrors_its_rsi_cover():
    rec = {"direction": "LONG", "exit_profile": "EOD_HOLD", "exit_params": {"rsi_cover": {"threshold": 40}}}
    assert exits.describe(rec) == "hold on the hard stop to the square-off; early exit at RSI 60 once in profit"


def test_staged_rsi_and_a_fixed_stop():
    rec = {"direction": "SHORT", "exit_profile": "STAGED_RSI", "stop_pct": 7.0,
           "exit_params": {"staged_rsi": {"scale_out_rsi": 50, "scale_out_fraction": 0.5,
                                          "stop_to_cost": True, "trail_rsi": 20}}}
    assert exits.describe(rec) == "stop 7% from the fill; 50% off at RSI 50, stop to cost; trail from RSI 20"


# ── what the bot counts ──────────────────────────────────────────────────────

def test_counts_per_strategy_reset_each_day(at):
    at(10, 0)
    book = StrategyBook([Fade()])
    book.record({"strategy": "fade"}, "armed")
    book.record_outcome({"strategy": "fade"}, "VALIDATED")
    book.record_outcome({"strategy": "fade"}, "TIMEOUT")
    s = book.stats("fade")
    assert (s.armed, s.triggered, s.expired, s.invalidated) == (1, 1, 1, 0)
    assert book.totals().armed == 1
    at(9, 20, day=28)
    assert book.stats("fade").armed == 0


def test_signal_cards_count_their_setups_even_before_telegram_is_up():
    bot = _bot()

    async def go():
        cid = await bot.queue_signal_discovery(_short_signal())
        await bot.queue_signal_validation_update(cid, _short_signal(), "REJECTED", {})
        return cid

    assert asyncio.run(go())
    s = bot.book.stats("fade")
    assert (s.armed, s.invalidated) == (1, 1)


def test_the_edited_card_flow_is_on_unless_configured_off():
    assert _bot().is_editable_signal_flow_enabled() is True
    assert _bot(EDITABLE_SIGNAL_FLOW_ENABLED=False).is_editable_signal_flow_enabled() is False


def test_the_broker_health_alert_hook_exists():
    # FyersBrokerInterface schedules bot.send_util_alert(...) on its loop; without
    # it every degraded-feed alert died in a swallowed AttributeError.
    assert asyncio.iscoroutinefunction(ShortCircuitBot.send_util_alert)


# ── read-only views ──────────────────────────────────────────────────────────

def test_positions_show_strategy_side_unrealised_and_exit():
    bot = _bot()
    bot.order_manager = SimpleNamespace(
        active_positions={"NSE:ABC-EQ": {"side": "SELL", "qty": 10, "entry_price": 100.0,
                                         "stop_loss": 107.0, "strategy": "fade",
                                         "exit_profile": "EOD_HOLD", "exit_params": {}}},
        broker=SimpleNamespace(get_quote_cache_snapshot=lambda: {"NSE:ABC-EQ": {"ltp": 98.0}}),
    )
    out = _say(bot, "positions")
    assert "NSE:ABC-EQ" in out and "🔴 SHORT ×10" in out and "<code>fade</code>" in out
    assert "+₹20.00" in out and "hold on the hard stop" in out


def test_no_positions_is_said_plainly():
    assert "No open positions" in _say(_bot(), "positions")


def test_signals_lists_armed_setups_and_todays_counts():
    bot = _bot()
    bot.attach_strategies([Bounce()])
    bot.focus_engine = SimpleNamespace(pending_signals={
        "NSE:B-EQ": {"data": {"strategy": "bounce", "direction": "LONG"}, "trigger": 50.5,
                     "invalidate": 48.0, "timestamp": time.time() - 120, "direction": "LONG"}})
    out = _say(bot, "signals")
    assert "Armed setups</b> · 1" in out and "trigger &gt; ₹50.50" in out and "armed 2 min ago" in out
    assert "<code>bounce</code>: armed 0" in out


def test_status_renders_before_anything_is_wired():
    bot = _bot()
    bot.attach_strategies([Fade()])
    out = _say(bot, "status")
    assert "ShortCircuit Status" in out and "fade (SHORT)" in out and "Run:" in out


def test_stop_asks_first_and_only_the_confirm_button_shuts_down():
    bot = _bot()

    async def go():
        bot._shutdown_event = asyncio.Event()
        upd = await _call(bot, "stop")
        assert upd.effective_message.sent[0][2] is not None, "needs confirmation buttons"
        edits = []

        async def edit(text, parse_mode=None):
            edits.append(text)

        async def answer():
            return None

        def press(data):
            return SimpleNamespace(callback_query=SimpleNamespace(
                data=data, answer=answer, edit_message_text=edit,
                from_user=SimpleNamespace(id=int(CHAT)), message=None))

        await bot._handle_callback(press("system_stop:cancel"), None)
        assert not bot._shutdown_event.is_set()
        await bot._handle_callback(press("system_stop:confirm"), None)
        return bot._shutdown_event.is_set(), edits

    stopped, edits = asyncio.run(go())
    assert stopped and "cancelled" in edits[0] and "Shutting down" in edits[1]


def test_the_morning_brief_lists_the_strategies():
    text = cards.morning_brief(
        date_text="Friday", time_text="09:30 IST", quote="q", range_lines=["High 1"], data_ok=True,
        order_ok=False, cache_text="WS cache 1/1", candle_ok=True, auto_text="ON", run_text="LIVE",
        strategy_names=["fade (SHORT)", "bounce (LONG)"], squareoff_text="F&O stocks 15:02",
    )
    assert "fade (SHORT), bounce (LONG)" in text and "F&amp;O stocks 15:02" in text
    assert "WS order ❌" in text
