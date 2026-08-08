"""
Package entry point: `python -m shortcircuit`.

The supervisor lives in shortcircuit.runtime.supervisor; this module exists so
the package is runnable without knowing that.
"""
import asyncio
import sys

from shortcircuit.runtime.supervisor import main

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
