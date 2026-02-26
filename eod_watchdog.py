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


async def eod_watchdog(shutdown_event: asyncio.Event) -> None:
    """
    Standalone failsafe. Fires graceful shutdown at 15:32 IST,
    SIGTERM at 15:40 IST if still alive.
    """
    logger.info("[EOD-WATCHDOG] Started. Monitoring for %02d:%02d IST.", *EOD_SOFT_SHUTDOWN)
    soft_fired = False

    while True:
        await asyncio.sleep(30)
        now = datetime.now(IST)
        h, m = now.hour, now.minute

        # Graceful shutdown at 15:32
        if not soft_fired and (
            h > EOD_SOFT_SHUTDOWN[0]
            or (h == EOD_SOFT_SHUTDOWN[0] and m >= EOD_SOFT_SHUTDOWN[1])
        ):
            logger.info("[EOD-WATCHDOG] ⏰ %02d:%02d IST reached. Firing graceful shutdown.", *EOD_SOFT_SHUTDOWN)
            shutdown_event.set()
            soft_fired = True

        # Hard kill at 15:40 — process still alive means cleanup is stuck
        if h > EOD_HARD_KILL[0] or (h == EOD_HARD_KILL[0] and m >= EOD_HARD_KILL[1]):
            if soft_fired:
                logger.critical(
                    "[EOD-WATCHDOG] ☠️ %02d:%02d IST reached. Process still alive. SIGTERM.",
                    *EOD_HARD_KILL,
                )
                os.kill(os.getpid(), signal.SIGTERM)
                return  # should never reach here
