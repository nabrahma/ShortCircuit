"""The bot itself: greeting, help (built from the registry), and shutdown."""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from shortcircuit.observability.telegram import fmt
from shortcircuit.observability.telegram.commands import Command

logger = logging.getLogger(__name__)

GROUP = "System"
STOP = "system_stop"


async def start(bot, update, context):
    bp = getattr(bot.capital_manager, "buying_power", 0.0) if bot.capital_manager else 0.0
    await bot.reply(update, (
        "⚡ <b>ShortCircuit</b> is running.\n\n"
        f"Run: {bot.run_text()} · Auto: {bot.auto_text()}\n"
        f"Strategies: {bot.strategies_text()}\n"
        f"Buying power: {fmt.money(bp)}\n\n"
        "/help lists every command."
    ))


async def help_(bot, update, context):
    await bot.reply(update, bot.registry.help_html())


async def stop(bot, update, context):
    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton("🛑 YES, STOP BOT", callback_data=f"{STOP}:confirm"),
        InlineKeyboardButton("❌ CANCEL", callback_data=f"{STOP}:cancel"),
    ]])
    await bot.reply(update, (
        "⚠️ <b>Stop ShortCircuit?</b>\n\n"
        "The process shuts down in order. Open positions keep their broker-side stops "
        "and are not closed by this; the broker squares off intraday positions itself.\n\n"
        "<b>Are you sure?</b>"
    ), reply_markup=buttons)


async def stop_pressed(bot, query, parts):
    if len(parts) > 1 and parts[1] == "confirm":
        logger.critical("🛑 [SHUTDOWN] Bot termination CONFIRMED via Telegram /stop")
        await query.edit_message_text("🛑 <b>Shutting down ShortCircuit…</b>\n\n"
                                      "<i>This may take a few seconds.</i>", parse_mode="HTML")
        bot.request_shutdown()
    else:
        await query.edit_message_text("✅ <b>Shutdown cancelled.</b> The bot keeps running.",
                                      parse_mode="HTML")


def register(registry) -> None:
    registry.add(Command("start", "What is running", start, group=GROUP))
    registry.add(Command("help", "Every command", help_, group=GROUP, menu=True))
    registry.add(Command("stop", "Shut the bot down (asks to confirm)", stop, group=GROUP, menu=True))
    registry.add_callback(STOP, stop_pressed)
