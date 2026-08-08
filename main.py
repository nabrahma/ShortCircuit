#!/usr/bin/env python3
"""
Entry point shim: `python main.py` continues to work after the restructure.

The runtime moved to src/shortcircuit/runtime/supervisor.py. This file is kept
deliberately — the bot is launched by hand and by any existing service
definition as `python main.py`, and silently changing that command is exactly
the kind of breakage a repackaging exercise is supposed to avoid.

Equivalent, once the package is installed:  python -m shortcircuit
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from shortcircuit.runtime.supervisor import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
