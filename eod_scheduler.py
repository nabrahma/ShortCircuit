# -*- coding: utf-8 -*-
import asyncio
import logging
from datetime import datetime, time as dt_time
from typing import Awaitable, Callable

import pytz

IST = pytz.timezone("Asia/Kolkata")
EOD_TIME = dt_time(15, 10, 0)
EOD_ANALYSIS_TIME = dt_time(15, 32, 0)

logger = logging.getLogger("shortcircuit.eod_scheduler")


def _get_now() -> datetime:
    return datetime.now(IST)


async def eod_scheduler(
    shutdown_event: asyncio.Event,
    trigger_eod_squareoff: Callable[[], Awaitable[None]],
    run_eod_analysis: Callable[[], Awaitable[None]],
    notify: Callable[[str], Awaitable[None]],
    get_open_positions: Callable[[], Awaitable[list]],
    bot_start_time: datetime,
    _now_fn: Callable[[], datetime] = _get_now,
) -> None:
    """
    Independent EOD task, decoupled from the trading scanner lifecycle.
    """
    eod_done_today = False
    analysis_done_today = False
    last_date = None

    while not shutdown_event.is_set():
        now = _now_fn()
        today = now.date()

        if last_date != today:
            eod_done_today = False
            analysis_done_today = False
            last_date = today

        if not eod_done_today and now.time() >= EOD_TIME:
            # Guard against late starts: only fire if we had open positions
            # or process started before 15:10 IST.
            should_fire = False
            try:
                open_positions = await get_open_positions()
                should_fire = bool(open_positions) or bot_start_time.time() < EOD_TIME
            except Exception as exc:
                logger.error("[EOD_SCHEDULER] Failed to fetch open positions: %s", exc)
                should_fire = bot_start_time.time() < EOD_TIME

            if should_fire:
                logger.info("[EOD_SCHEDULER] 15:10 reached; triggering forced square-off.")
                try:
                    await trigger_eod_squareoff()
                    await notify("EOD Square-off complete.")
                except Exception as exc:
                    logger.error("[EOD_SCHEDULER] Square-off failed: %s", exc)
                    await notify(f"EOD Square-off FAILED: {exc}")
            else:
                logger.info(
                    "[EOD_SCHEDULER] No positions + late start; skipping square-off."
                )

            # Always latch, even when skipped, to prevent double fire.
            eod_done_today = True

        if not analysis_done_today and now.time() >= EOD_ANALYSIS_TIME:
            logger.info("[EOD_SCHEDULER] 15:32 reached; triggering EOD analysis.")
            try:
                await run_eod_analysis()
            except Exception as exc:
                logger.error("[EOD_SCHEDULER] Analysis failed: %s", exc)
                await notify(f"EOD Analysis FAILED: {exc}")
            analysis_done_today = True

            # ── Bug 2A FIX: Fire graceful shutdown after EOD work is done ──
            logger.info("[EOD_SCHEDULER] All EOD tasks complete. Firing shutdown.")
            await notify("✅ EOD complete. Shutting down bot.")
            shutdown_event.set()
            return  # Exit scheduler — shutdown_event will stop all other tasks

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=15)
        except asyncio.TimeoutError:
            continue
