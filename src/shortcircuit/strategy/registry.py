"""
Strategy discovery and loading.

Where plug-ins come from, in order:

  1. Built-in examples shipped with the engine (`shortcircuit.strategy.examples`).
  2. Every directory in SHORTCIRCUIT_STRATEGY_PATH (os.pathsep-separated), or,
     when that is unset, `strategies/` at the repository root.

A plug-in directory is either a Python package (it has an `__init__.py`, and
each submodule or subpackage is scanned) or a plain folder of `.py` files.
Every concrete `Strategy` subclass found is registered under its `name`.

Which strategies run, in priority order (the first to return a Signal for a
candidate wins):

  1. SHORTCIRCUIT_STRATEGIES, comma-separated, if set;
  2. otherwise the `ACTIVE` tuple of a plug-in package's `__init__.py`;
  3. otherwise none — the engine scans and manages positions but opens nothing,
     and says so loudly at startup.

Loading fails fast. An active name that does not resolve, two plug-ins sharing a
name, or a strategy the engine cannot execute is a configuration error raised
before the first scan, never a silent "no signals today".
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import logging
import os
import pkgutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Iterable, Optional, Sequence

from shortcircuit.strategy.sdk import Direction, Strategy

logger = logging.getLogger(__name__)

__all__ = ["Catalog", "StrategyConfigError", "active_names", "discover", "load", "plugin_dirs"]

ENV_PATH = "SHORTCIRCUIT_STRATEGY_PATH"
ENV_ACTIVE = "SHORTCIRCUIT_STRATEGIES"

_BUILTIN_PACKAGE = "shortcircuit.strategy.examples"
_SUPPORTED_DIRECTIONS = frozenset({Direction.SHORT})


class StrategyConfigError(RuntimeError):
    """The configured strategies cannot be loaded as asked."""


@dataclass
class Catalog:
    """Everything discovery found."""

    strategies: dict[str, type[Strategy]] = field(default_factory=dict)
    origins: dict[str, str] = field(default_factory=dict)       # name -> module
    declared_active: tuple[str, ...] = ()                       # from plug-in ACTIVE
    errors: list[str] = field(default_factory=list)             # import failures

    def add(self, cls: type[Strategy], origin: str) -> None:
        name = getattr(cls, "name", None)
        if not isinstance(name, str) or not name:
            self.errors.append(f"{origin}: {cls.__name__} has no `name`")
            return
        if name in self.strategies and self.strategies[name] is not cls:
            raise StrategyConfigError(
                f"two strategies are named {name!r}: {self.origins[name]} and {origin}"
            )
        self.strategies[name] = cls
        self.origins[name] = origin


# Discovery

def plugin_dirs() -> list[Path]:
    """Directories searched for plug-ins, in order."""
    raw = os.getenv(ENV_PATH, "").strip()
    if raw:
        return [Path(p).expanduser().resolve() for p in raw.split(os.pathsep) if p.strip()]
    from shortcircuit.paths import STRATEGIES_DIR

    return [STRATEGIES_DIR]


def _strategies_in(module: ModuleType) -> Iterable[type[Strategy]]:
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if (issubclass(obj, Strategy) and obj is not Strategy
                and not inspect.isabstract(obj) and obj.__module__ == module.__name__):
            yield obj


def _skipped(leaf: str) -> bool:
    """Private modules, tests and pytest plumbing are never scanned for strategies."""
    return leaf.startswith(("_", "test")) or leaf == "conftest"


def _walk_package(package: ModuleType, catalog: Catalog) -> None:
    """
    Import every non-skipped submodule and subpackage, depth first.

    Not pkgutil.walk_packages: that imports each subpackage in order to recurse
    into it, so a `tests` package would be imported before it could be skipped.
    """
    for info in pkgutil.iter_modules(package.__path__, prefix=package.__name__ + "."):
        if _skipped(info.name.rsplit(".", 1)[-1]):
            continue
        try:
            module = importlib.import_module(info.name)
        except Exception as exc:
            catalog.errors.append(f"{info.name}: {type(exc).__name__}: {exc}")
            logger.error("[STRATEGY] could not import %s: %s", info.name, exc)
            continue
        for cls in _strategies_in(module):
            catalog.add(cls, info.name)
        if info.ispkg:
            _walk_package(module, catalog)


def _load_package_dir(directory: Path, catalog: Catalog) -> None:
    parent = str(directory.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    try:
        package = importlib.import_module(directory.name)
    except Exception as exc:
        catalog.errors.append(f"{directory}: {type(exc).__name__}: {exc}")
        logger.error("[STRATEGY] could not import plug-in package %s: %s", directory, exc)
        return
    if Path(getattr(package, "__file__", "") or "").resolve().parent != directory:
        catalog.errors.append(
            f"{directory}: the name {directory.name!r} is already taken by "
            f"{getattr(package, '__file__', package)!r}; rename the directory"
        )
        return
    for cls in _strategies_in(package):
        catalog.add(cls, package.__name__)
    active = getattr(package, "ACTIVE", None)
    if active:
        catalog.declared_active = tuple(str(n) for n in active)
    _walk_package(package, catalog)


def _load_file_dir(directory: Path, catalog: Catalog) -> None:
    # Namespaces the module so two plug-in directories may each hold a `fade.py`.
    # An identifier, not a security boundary.
    digest = hashlib.sha1(str(directory).encode("utf-8"), usedforsecurity=False).hexdigest()[:10]
    for path in sorted(directory.glob("*.py")):
        if path.stem.startswith(("_", "test")) or path.stem == "conftest":
            continue
        module_name = f"shortcircuit_plugin_{digest}_{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception as exc:
            sys.modules.pop(module_name, None)
            catalog.errors.append(f"{path}: {type(exc).__name__}: {exc}")
            logger.error("[STRATEGY] could not import %s: %s", path, exc)
            continue
        for cls in _strategies_in(module):
            catalog.add(cls, str(path))


def discover(directories: Optional[Sequence[Path]] = None) -> Catalog:
    """Find every available strategy. Never raises on a broken plug-in module."""
    catalog = Catalog()

    builtin = importlib.import_module(_BUILTIN_PACKAGE)
    _walk_package(builtin, catalog)

    for directory in (plugin_dirs() if directories is None else directories):
        directory = Path(directory)
        if not directory.is_dir():
            continue
        if (directory / "__init__.py").is_file():
            _load_package_dir(directory, catalog)
        else:
            _load_file_dir(directory, catalog)
    return catalog


# Selection and loading

def active_names(catalog: Catalog) -> tuple[str, ...]:
    """The configured strategy names, in priority order."""
    raw = os.getenv(ENV_ACTIVE)
    if raw is not None:
        return tuple(n.strip() for n in raw.split(",") if n.strip())
    return catalog.declared_active


def _validate(cls: type[Strategy]) -> None:
    direction = getattr(cls, "direction", None)
    if not isinstance(direction, Direction):
        raise StrategyConfigError(f"strategy {cls.name!r}: `direction` must be a Direction")
    if direction not in _SUPPORTED_DIRECTIONS:
        raise StrategyConfigError(
            f"strategy {cls.name!r} trades {direction.value}; the execution engine "
            f"supports {sorted(d.value for d in _SUPPORTED_DIRECTIONS)} only"
        )


def load(
    names: Optional[Sequence[str]] = None,
    directories: Optional[Sequence[Path]] = None,
) -> list[Strategy]:
    """
    Instantiate the requested strategies, in order.

    `names` defaults to the configured active set (see module docstring).
    Raises StrategyConfigError on any name that cannot be honoured.
    """
    catalog = discover(directories)
    requested = tuple(names) if names is not None else active_names(catalog)

    seen: set[str] = set()
    strategies: list[Strategy] = []
    for name in requested:
        if name in seen:
            raise StrategyConfigError(f"strategy {name!r} is listed twice")
        seen.add(name)
        cls = catalog.strategies.get(name)
        if cls is None:
            detail = f"; import errors: {catalog.errors}" if catalog.errors else ""
            raise StrategyConfigError(
                f"no strategy named {name!r}. Available: "
                f"{sorted(catalog.strategies) or 'none'}{detail}"
            )
        _validate(cls)
        strategies.append(cls())

    if strategies:
        logger.info(
            "[STRATEGY] active, in priority order: %s",
            ", ".join(f"{s.name} ({catalog.origins[s.name]})" for s in strategies),
        )
    else:
        logger.warning(
            "[STRATEGY] no strategy is active — the engine will scan and manage open "
            "positions but will not open any. Set %s or ACTIVE in %s.",
            ENV_ACTIVE, " / ".join(str(d / "__init__.py") for d in plugin_dirs()),
        )
    return strategies
