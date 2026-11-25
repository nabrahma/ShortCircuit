# coding: utf-8
import asyncio
import logging
from datetime import datetime, time as dt_time
from typing import Awaitable, Callable

import pytz

from shortcircuit.eod import squareoff

IST = pytz.timezone("Asia/Kolkata")
EOD_TIME = squareoff.ENGINE_SQUAREOFF
EOD_ANALYSIS_TIME = dt_time(15, 32, 0)

logger = logging.getLogger("shortcircuit.eod_scheduler")


def _get_now() -> datetime:
    return datetime.now(IST)


def _open_symbols(positions) -> list:
    out = []
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        try:
            qty = int(p.get('netQty', p.get('qty', 0)) or 0)
        except (TypeError, ValueError):
            qty = 0
        if qty != 0 and p.get('symbol'):
            out.append(p['symbol'])
    return out


async def eod_scheduler(
    shutdown_event: asyncio.Event,
    trigger_eod_squareoff: Callable[[], Awaitable[None]],
    run_eod_analysis: Callable[[], Awaitable[None]],
    notify: Callable[[str], Awaitable[None]],
    get_open_positions: Callable[[], Awaitable[list]],
    bot_start_time: datetime,
    _now_fn: Callable[[], datetime] = _get_now,
    _cutoff_for: Callable[[str], dt_time] = squareoff.squareoff_time,
) -> None:
    """
    Independent EOD task, decoupled from the trading scanner lifecycle.

    The square-off fires at 15:10, or earlier — at the F&O cutoff — when an open
    position is in a stock the broker will square off before 15:10 itself.
    """
    eod_done_today = False
    analysis_done_today = False
    last_date = None
    early_cutoff = squareoff.cas_squareoff()

    while not shutdown_event.is_set():
        now = _now_fn()
        today = now.date()

        if last_date != today:
            eod_done_today = False
            analysis_done_today = False
            last_date = today

        # Between the F&O cutoff and 15:10, fire only for a position the broker
        # would otherwise close first. The position read is the WS cache in the
        # common case, so polling it every 15s here costs nothing.
        early = False
        if not eod_done_today and early_cutoff <= now.time() < EOD_TIME:
            try:
                symbols = _open_symbols(await get_open_positions())
                early = any(_cutoff_for(s) <= now.time() for s in symbols)
                if early:
                    logger.info(
                        "[EOD_SCHEDULER] %s squares off at the broker before 15:10 — "
                        "closing now.", ", ".join(s for s in symbols if _cutoff_for(s) <= now.time()),
                    )
            except Exception as exc:
                logger.error("[EOD_SCHEDULER] Early-cutoff position check failed: %s", exc)

        if not eod_done_today and (early or now.time() >= EOD_TIME):
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
                logger.info(
                    "[EOD_SCHEDULER] %s reached; triggering forced square-off.",
                    now.strftime("%H:%M") if early else EOD_TIME.strftime("%H:%M"),
                )
                try:
                    # Only announce success when the caller could actually PROVE the
                    # account is flat. Previously this always reported "complete",
                    # even one second after a timeout warning.
                    ok = await trigger_eod_squareoff()
                    if ok is False:
                        logger.error(
                            "[EOD_SCHEDULER] Square-off did not verify flat — "
                            "operator has been alerted."
                        )
                    else:
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

            # Fire graceful shutdown after EOD work is done
            logger.info("[EOD_SCHEDULER] All EOD tasks complete. Firing shutdown.")
            await notify("✅ EOD complete. Shutting down bot.")
            shutdown_event.set()
            return  # Exit scheduler — shutdown_event will stop all other tasks

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=15)
        except asyncio.TimeoutError:
            continue
