"""
The Telegram operator console.

    bot.py        transport, lifecycle, the auto-trade gate, signal cards
    commands/     the command registry and the built-in commands, one module per group
    cards.py      message bodies (pure builders)
    book.py       the active strategies as Telegram sees them, and today's counts
    exits.py      a signal's or position's exit plan, in words
    fmt.py        escaping and number formatting

Strategy-agnostic by construction: everything shown about a strategy comes from
the plug-in contract (`Strategy.name`, `direction`, `universe`, `description`,
`describe()`) and the signals it returns (`Signal.display`).
"""
from shortcircuit.observability.telegram.bot import ShortCircuitBot

__all__ = ["ShortCircuitBot"]
