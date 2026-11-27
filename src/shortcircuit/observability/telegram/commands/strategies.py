"""What is plugged in: the active strategies and what each publishes about itself."""
from __future__ import annotations

from shortcircuit.observability.telegram import cards, fmt
from shortcircuit.observability.telegram.commands import Command

GROUP = "Strategies"


async def strategies(bot, update, context):
    await bot.reply(update, cards.strategies_list(bot.book))


async def strategy(bot, update, context):
    args = context.args or []
    if not args:
        await bot.reply(update, "Usage: /strategy &lt;name&gt;\n\n" + cards.strategies_list(bot.book))
        return
    found = bot.book.find(args[0])
    if found is None:
        names = ", ".join(f"<code>{fmt.he(n)}</code>" for n in bot.book.names) or "none"
        await bot.reply(update, f"No active strategy named <code>{fmt.he(args[0])}</code>. Active: {names}")
        return
    await bot.reply(update, cards.strategy_detail(bot.book, found))


def register(registry) -> None:
    registry.add(Command("strategies", "Active strategies, their state and today's counts",
                         strategies, group=GROUP, menu=True))
    registry.add(Command("strategy", "One strategy's settings", strategy,
                         usage="<name>", group=GROUP))
