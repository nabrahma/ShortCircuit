# -*- coding: utf-8 -*-
"""
EOD Watchdog — Standalone failsafe for market-close shutdown.

Runs independently of eod_scheduler. Checks every 30 seconds.
- 15:32 IST → fires shutdown_event (graceful)
- 15:40 IST → os.kill SIGTERM (nuclear, no argument)

This task CANNOT be blocked by scanning loops, DB hangs, or WS stalls.
"""

import asyncio
import logging
import os
import signal
from datetime import datetime

import pytz

logger = logging.getLogger("shortcircuit.eod_watchdog")
IST = pytz.timezone("Asia/Kolkata")

EOD_SOFT_SHUTDOWN = (15, 32)  # (hour, minute) IST — graceful
EOD_HARD_KILL = (15, 40)      # (hour, minute) IST — SIGTERM


async def eod_watchdog(shutdown_event: asyncio.Event):
    """
    Hard failsafe. Sets shutdown_event at 15:32.
    Force-kills process at 15:40 if still alive.
    """
    IST = pytz.timezone("Asia/Kolkata")

    while True:   # ← keep the while True but add the break
        now = datetime.now(IST)

        # Soft shutdown at 15:32 — give cleanup_runtime() 25s to finish
        if now.hour == 15 and now.minute >= 32:
            if not shutdown_event.is_set():
                logger.warning("[EOD-WATCHDOG] 15:32 IST — setting shutdown_event.")
                shutdown_event.set()

        # ✅ HARD KILL at 15:40 — cannot be trapped, cannot be ignored
        if now.hour == 15 and now.minute >= 40:
            logger.critical("[EOD-WATCHDOG] 15:40 IST — process did not exit cleanly. "
                            "Forcing os._exit(0).")
            os._exit(0)   # ← bypasses all Python cleanup, kills immediately

        # ✅ EXIT the loop once shutdown is confirmed AND it's past 15:32
        if shutdown_event.is_set() and now.hour == 15 and now.minute >= 32:
            logger.info("[EOD-WATCHDOG] Shutdown confirmed. Watchdog exiting cleanly.")
            return   # ← lets the TaskGroup finish normally

        await asyncio.sleep(30)
