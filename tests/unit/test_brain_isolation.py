"""
Structural test: the Brain must not depend on the Muscle.

The README claims trading intelligence is sealed inside `strategy/`, knowing
nothing about brokers, sockets or Telegram. That is an architectural claim, and
architectural claims decay silently — one convenient import during a late-night
debugging session and it is quietly false.

This converts the claim into a verified fact by parsing the AST of every module
under `strategy/` — and of every strategy plug-in under `strategies/`, when that
private directory is present — and inspecting what it imports.

An earlier version compared only the top-level package of each import. Under the
src/ layout every internal import's top level is `shortcircuit`, so it could not
fail: `from shortcircuit.execution.order_manager import ...` passed. Every
component of an internal import path is checked now.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
STRATEGY_DIR = REPO / "src" / "shortcircuit" / "strategy"
PLUGIN_DIR = REPO / "strategies"

# Modules in the runtime layer. `strategy/` importing any of these would mean the
# Brain has reached into the Muscle.
RUNTIME_MODULES = {
    "fyers_broker_interface", "fyers_connect", "order_manager", "focus_engine",
    "trade_manager", "capital_manager", "telegram_bot", "database",
    "reconciliation", "scanner", "analyzer", "main", "ml_logger",
    "gate_result_logger", "signal_manager", "market_session",
    "startup_recovery", "eod_analyzer", "eod_scheduler", "eod_watchdog",
    "transport", "rest_limiter", "supervisor",
    # Whole runtime subpackages.
    "broker", "execution", "runtime", "state", "observability", "eod", "capital",
}

# Third-party packages that would imply I/O from inside the Brain.
FORBIDDEN_THIRD_PARTY = {
    "fyers_apiv3", "telegram", "asyncpg", "psycopg2", "requests",
    "aiohttp", "httpx", "websockets",
}

# `config` and `symbols` are deliberately permitted: the Brain reads engine
# thresholds and validates index symbols. REST pacing used to need
# `rest_limiter` here; the metered transport made that unnecessary.
ALLOWED_RUNTIME_IMPORTS = {"config", "symbols"}


def _strategy_modules() -> list[Path]:
    modules = [p for p in STRATEGY_DIR.rglob("*.py") if "__pycache__" not in p.parts]
    if PLUGIN_DIR.is_dir():
        modules += [
            p for p in PLUGIN_DIR.rglob("*.py")
            if "__pycache__" not in p.parts and "tests" not in p.parts
            and p.name != "conftest.py"
        ]
    return sorted(p for p in modules if p.name != "__init__.py")


def _imported_roots(path: Path) -> set[str]:
    """Top-level package name of every import in the file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()

    def add(dotted: str) -> None:
        parts = dotted.split(".")
        roots.add(parts[0])
        if parts[0] == "shortcircuit":
            roots.update(parts[1:])   # every component of an internal path

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:            # relative import, stays inside the package
                continue
            if node.module:
                add(node.module)
                if node.module == "shortcircuit":   # from shortcircuit import config
                    roots.update(alias.name for alias in node.names)
    return roots


def test_strategy_directory_exists():
    assert STRATEGY_DIR.is_dir(), f"strategy/ not found at {STRATEGY_DIR}"


def test_strategy_has_modules():
    assert _strategy_modules(), "no strategy modules found — the test would pass vacuously"


@pytest.mark.parametrize("module", _strategy_modules(), ids=lambda p: p.name)
def test_strategy_module_does_not_import_the_runtime_layer(module: Path):
    offenders = (_imported_roots(module) & RUNTIME_MODULES) - ALLOWED_RUNTIME_IMPORTS
    assert not offenders, (
        f"{module.name} imports runtime module(s) {sorted(offenders)}. "
        f"The Brain must not depend on the Muscle."
    )


@pytest.mark.parametrize("module", _strategy_modules(), ids=lambda p: p.name)
def test_strategy_module_does_not_import_io_libraries(module: Path):
    """
    market_context and htf_confluence receive an injected `fyers` client, which
    is the correct pattern — they never import the SDK themselves.
    """
    offenders = _imported_roots(module) & FORBIDDEN_THIRD_PARTY
    assert not offenders, (
        f"{module.name} imports I/O library/libraries {sorted(offenders)}. "
        f"The Brain should receive clients by injection, never import them."
    )


def test_no_strategy_module_opens_a_file():
    """Reading or writing files from inside the Brain would make it stateful."""
    offenders = []
    for module in _strategy_modules():
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "open":
                    offenders.append(module.name)
    assert not offenders, f"strategy modules performing file I/O: {sorted(set(offenders))}"


def test_the_allowlist_is_documented_not_accidental():
    """
    Guards the guard: if someone widens ALLOWED_RUNTIME_IMPORTS to make a failure
    go away, this forces the list to stay small and deliberate.
    """
    assert ALLOWED_RUNTIME_IMPORTS == {"config", "symbols"}, (
        "The Brain's permitted runtime imports changed. This is an architectural "
        "decision — update docs/DECISIONS.md before widening it."
    )
