"""
Single source of truth for on-disk locations.

Every runtime path is resolved relative to the REPOSITORY ROOT, discovered by
walking up from this file until a marker is found. Two failure modes motivate
this:

1. **`__file__`-relative paths break when a module moves.** `fyers_connect.py`
   resolved the broker token as `Path(__file__).parent / "data" / ...`. That is
   correct only while the module sits in the repository root — relocating it into
   a package would silently point the token at a directory inside the package,
   and the bot would find no cached token and try to re-authenticate
   interactively on every start. The line would not change, so a diff review
   would not catch it.

2. **CWD-relative paths break when the process is not launched from the root.**
   `"logs/bot.log"` works from the repository root and writes somewhere
   unexpected from anywhere else — including from a systemd unit or a container
   with a different `WORKDIR`.

Anchoring on a discovered root makes both classes of bug impossible.
"""
from __future__ import annotations

import os
from pathlib import Path

# Files that only ever exist at the repository root.
_ROOT_MARKERS = ("pyproject.toml", "requirements.txt", ".git")


def _discover_root() -> Path:
    """Walk upward from this file until a repository marker is found."""
    here = Path(__file__).resolve()
    for candidate in (here.parent, *here.parents):
        if any((candidate / marker).exists() for marker in _ROOT_MARKERS):
            return candidate
    # No marker found (installed as a package, say). Fall back to the process's
    # working directory rather than guessing a location inside site-packages.
    return Path.cwd()


# Overridable so a container or test can relocate state without code changes.
PROJECT_ROOT: Path = Path(os.getenv("SHORTCIRCUIT_ROOT", "")).resolve() \
    if os.getenv("SHORTCIRCUIT_ROOT") else _discover_root()

LOGS_DIR:      Path = PROJECT_ROOT / "logs"
DATA_DIR:      Path = PROJECT_ROOT / "data"
REPORTS_DIR:   Path = PROJECT_ROOT / "reports"
MIGRATIONS_DIR: Path = PROJECT_ROOT / "migrations"

ML_DIR:        Path = DATA_DIR / "ml"
TOKEN_FILE:    Path = DATA_DIR / "access_token.txt"

FYERS_REST_LOG_DIR:     Path = LOGS_DIR / "fyers_rest"
FYERS_ORDER_WS_LOG_DIR: Path = LOGS_DIR / "fyers_order_ws"
FYERS_DATA_WS_LOG_DIR:  Path = LOGS_DIR / "fyers_data_ws"


def ensure_runtime_dirs() -> None:
    """Create the directories the runtime writes into. Safe to call repeatedly."""
    for directory in (
        LOGS_DIR, DATA_DIR, REPORTS_DIR, ML_DIR,
        FYERS_REST_LOG_DIR, FYERS_ORDER_WS_LOG_DIR, FYERS_DATA_WS_LOG_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)
