"""Trading controls: the auto gate, the direction filter, per-strategy pause."""
from __future__ import annotations

import asyncio
import logging

from shortcircuit import config
from shortcircuit.execution import strategy_switch
from shortcircuit.observability.telegram import fmt
from shortcircuit.observability.telegram.commands import Command

logger = logging.getLogger(__name__)

GROUP = "Trading"


async def auto(bot, update, context):
    args = [a.lower() for a in (context.args or [])]
    if not args or args[0] not in ("on", "off"):
        state = "ON" if bot.is_auto_mode() else ("OFF (queued for 09:30)" if bot._auto_on_queued else "OFF")
        await bot.reply(update, f"Auto mode: <b>{state}</b>\n\nUsage: /auto on | /auto off")
        return
    if args[0] == "on":
        if bot.is_auto_mode():
            await bot.reply(update, "ℹ️ Auto mode is already <b>ON</b>.")
            return
        mins_left = bot.arm_auto()
        if mins_left is not None:
            await bot.reply(update, f"⏳ <b>Auto ON queued</b>: activates at 09:30 IST "
                                    f"({mins_left} min). No action needed.")
        else:
            await bot.reply(update, "✅ <b>Auto mode ON</b>: setups that trigger are traded.")
        return
    if not bot._auto_mode and not bot._auto_on_queued:
        await bot.reply(update, "ℹ️ Auto mode is already <b>OFF</b>.")
        return
    bot.disarm_auto()
    await asyncio.sleep(0)
    open_count = len(getattr(bot.order_manager, "active_positions", {}) or {}) if bot.order_manager else 0
    await bot.reply(update, (
        "🛑 <b>AUTO TRADE OFF</b>\n\n"
        f"{bot.capital_block()}"
        f"Positions: {open_count} open, still managed (stops and exits unchanged)\n"
        f"{bot.signal_block()}"
        "\nNew setups arrive as alerts only. /auto on to re-enable."
    ))


async def mode(bot, update, context):
    args = context.args or []
    if not args:
        await bot.reply(update, (
            f"Direction: <b>{fmt.mode_label()}</b>\n\n"
            "Each strategy trades its own side; this only filters.\n\n"
            "Usage: /mode both | long | short"
        ))
        return
    new = {"both": "BOTH", "all": "BOTH", "buy": "LONG", "long": "LONG",
           "sell": "SHORT", "short": "SHORT"}.get(args[0].lower().strip())
    if new is None:
        await bot.reply(update, "Usage: /mode both | long | short")
        return
    config.TRADE_DIRECTION = new
    logger.critical("[MODE] Trade direction filter switched to %s via Telegram", new)
    detail = {
        "BOTH": "Short strategies may sell and long strategies may buy.",
        "LONG": "Only long strategies may open positions (BUY). Short setups are held back.",
        "SHORT": "Only short strategies may open positions (SELL). Long setups are held back.",
    }[new]
    await bot.reply(update, f"<b>Direction: {fmt.mode_label()}</b>\n\n{detail}\n"
                            "Open positions are unaffected: each keeps its own side, stop and exits.")


async def _switch(bot, update, context, *, pausing: bool):
    verb = "pause" if pausing else "resume"
    if not bot.book.strategies:
        await bot.reply(update, "No strategy is active, so there is nothing to " + verb + ".")
        return
    known, unknown = bot.book.resolve(context.args or [])
    if not known and not unknown:
        names = ", ".join(f"<code>{fmt.he(n)}</code>" for n in bot.book.names)
        await bot.reply(update, f"Usage: /{verb} &lt;name&gt; … | /{verb} all\n\nActive: {names}")
        return
    if unknown:
        await bot.reply(update, "Not an active strategy: " + ", ".join(fmt.he(u) for u in unknown)
                        + "\n/strategies lists them.")
        if not known:
            return
    changed = strategy_switch.pause(known) if pausing else strategy_switch.resume(known)
    same = [n for n in known if n not in changed]
    lines = []
    if changed:
        lines.append(("⏸️ <b>Paused</b>: " if pausing else "▶️ <b>Resumed</b>: ")
                     + ", ".join(f"<code>{fmt.he(n)}</code>" for n in sorted(changed)))
    if same:
        lines.append(("Already paused: " if pausing else "Was not paused: ")
                     + ", ".join(f"<code>{fmt.he(n)}</code>" for n in same))
    if pausing and changed:
        lines.append("\nNo new setups from these until /resume. Open positions and setups "
                     "already armed keep their stops and exits (/positions, /signals). "
                     "A restart clears every pause.")
    await bot.reply(update, "\n".join(lines))


async def pause(bot, update, context):
    await _switch(bot, update, context, pausing=True)


async def resume(bot, update, context):
    await _switch(bot, update, context, pausing=False)


def register(registry) -> None:
    registry.add(Command("auto", "Arm auto-trading, or alerts only", auto,
                         usage="on|off", group=GROUP, menu=True))
    registry.add(Command("mode", "Which sides may open positions", mode,
                         usage="both|long|short", group=GROUP, menu=True))
    registry.add(Command("pause", "Stop a strategy taking new setups", pause,
                         usage="<name>|all", group=GROUP, menu=True))
    registry.add(Command("resume", "Let a paused strategy trade again", resume,
                         usage="<name>|all", group=GROUP))
