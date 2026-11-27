"""
The Telegram command registry.

A command is one `Command` record: its name, a one-line summary, the group it
is listed under and its handler. `/help` and Telegram's command menu are built
from the registry, so adding a command is one registration, with nothing else
to keep in step.

Built-in commands live in this package, one module per group. A deployment can
add its own without touching the engine: list module paths in
`config.TELEGRAM_COMMAND_MODULES`, and give each module a
`register(registry)` function that adds `Command`s. Such a module may live in
the private `strategies/` folder, e.g. `strategies.my_commands`. Every command,
built-in or added, passes the same authorization gate before its handler runs.
A module that fails to import is logged and skipped; it cannot stop the bot.

A handler is `async def handler(bot, update, context)`: `bot` is the running
`ShortCircuitBot`, `update` and `context` are python-telegram-bot's.
"""
from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Iterator, Optional

logger = logging.getLogger(__name__)

Handler = Callable[[Any, Any, Any], Awaitable[None]]
CallbackHandler = Callable[[Any, Any, list], Awaitable[None]]

GROUP_ORDER = ("Trading", "Strategies", "Monitoring", "System")
BUILTIN_MODULES = (
    "shortcircuit.observability.telegram.commands.trading",
    "shortcircuit.observability.telegram.commands.strategies",
    "shortcircuit.observability.telegram.commands.monitoring",
    "shortcircuit.observability.telegram.commands.system",
)


@dataclass(frozen=True)
class Command:
    name: str
    summary: str
    handler: Handler
    usage: str = ""
    group: str = "Other"
    menu: bool = False  # listed in Telegram's "/" menu

    def __post_init__(self) -> None:
        if not self.name or not self.name.isascii() or not self.name.replace("_", "").isalnum() \
                or self.name != self.name.lower() or len(self.name) > 32:
            raise ValueError(f"invalid command name {self.name!r}: lowercase letters, digits, _")
        if not self.summary:
            raise ValueError(f"command /{self.name} needs a summary")


class CommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}
        self._callbacks: dict[str, CallbackHandler] = {}

    def add(self, command: Command) -> None:
        if command.name in self._commands:
            raise ValueError(f"/{command.name} is already registered")
        self._commands[command.name] = command

    def add_callback(self, prefix: str, handler: CallbackHandler) -> None:
        """Inline-button presses whose data starts '<prefix>:'."""
        if prefix in self._callbacks:
            raise ValueError(f"callback prefix {prefix!r} is already registered")
        self._callbacks[prefix] = handler

    def get(self, name: str) -> Optional[Command]:
        return self._commands.get(name)

    def callback(self, prefix: str) -> Optional[CallbackHandler]:
        return self._callbacks.get(prefix)

    def __iter__(self) -> Iterator[Command]:
        return iter(self._commands.values())

    def __len__(self) -> int:
        return len(self._commands)

    def __contains__(self, name: object) -> bool:
        return name in self._commands

    def menu(self) -> list[tuple[str, str]]:
        return [(c.name, c.summary[:256]) for c in self if c.menu]

    def help_html(self) -> str:
        groups: dict[str, list[Command]] = {}
        for c in self:
            groups.setdefault(c.group, []).append(c)
        order = [g for g in GROUP_ORDER if g in groups] + sorted(g for g in groups if g not in GROUP_ORDER)
        out = ["⚡ <b>ShortCircuit commands</b>"]
        for g in order:
            out.append(f"\n<b>{_he(g)}</b>")
            for c in groups[g]:
                usage = f" {_he(c.usage)}" if c.usage else ""
                out.append(f"/{c.name}{usage} · {_he(c.summary)}")
        return "\n".join(out)

    def load(self, module_path: str) -> Optional[str]:
        """Import a command module and call its register(); the error text on failure."""
        try:
            module = importlib.import_module(module_path)
            register = getattr(module, "register", None)
            if not callable(register):
                raise AttributeError("module has no register(registry) function")
            register(self)
            return None
        except Exception as exc:
            logger.error("[TELEGRAM] command module %s not loaded: %s: %s",
                         module_path, type(exc).__name__, exc)
            return f"{type(exc).__name__}: {exc}"


def _he(text: Any) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build(extra_modules: Iterable[str] = ()) -> tuple[CommandRegistry, dict[str, str]]:
    """The built-in commands plus any deployment modules; (registry, load errors)."""
    registry = CommandRegistry()
    errors: dict[str, str] = {}
    for path in (*BUILTIN_MODULES, *extra_modules):
        err = registry.load(path)
        if err:
            if path in BUILTIN_MODULES:
                raise RuntimeError(f"built-in Telegram commands failed to load ({path}): {err}")
            errors[path] = err
    return registry, errors
