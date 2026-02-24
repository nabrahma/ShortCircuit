from datetime import datetime
from unittest.mock import AsyncMock

import asyncio
import pytest

from eod_scheduler import IST, eod_scheduler


@pytest.mark.asyncio
async def test_late_start_without_positions_skips_squareoff():
    shutdown = asyncio.Event()
    trigger_squareoff = AsyncMock()
    run_analysis = AsyncMock()
    notify = AsyncMock()

    async def get_open_positions():
        shutdown.set()
        return []

    bot_start_time = datetime(2026, 2, 24, 15, 20, 0, tzinfo=IST)
    fake_now = lambda: datetime(2026, 2, 24, 15, 20, 5, tzinfo=IST)

    await eod_scheduler(
        shutdown_event=shutdown,
        trigger_eod_squareoff=trigger_squareoff,
        run_eod_analysis=run_analysis,
        notify=notify,
        get_open_positions=get_open_positions,
        bot_start_time=bot_start_time,
        _now_fn=fake_now,
    )

    trigger_squareoff.assert_not_awaited()


@pytest.mark.asyncio
async def test_squareoff_triggers_with_open_positions():
    shutdown = asyncio.Event()
    trigger_squareoff = AsyncMock()
    run_analysis = AsyncMock()
    notify = AsyncMock()

    async def get_open_positions():
        shutdown.set()
        return [{"symbol": "NSE:TEST-EQ", "qty": -1}]

    bot_start_time = datetime(2026, 2, 24, 9, 0, 0, tzinfo=IST)
    fake_now = lambda: datetime(2026, 2, 24, 15, 10, 5, tzinfo=IST)

    await eod_scheduler(
        shutdown_event=shutdown,
        trigger_eod_squareoff=trigger_squareoff,
        run_eod_analysis=run_analysis,
        notify=notify,
        get_open_positions=get_open_positions,
        bot_start_time=bot_start_time,
        _now_fn=fake_now,
    )

    trigger_squareoff.assert_awaited_once()


@pytest.mark.asyncio
async def test_analysis_triggers_after_1532():
    shutdown = asyncio.Event()
    trigger_squareoff = AsyncMock()
    notify = AsyncMock()

    async def run_analysis():
        shutdown.set()

    async def get_open_positions():
        return []

    bot_start_time = datetime(2026, 2, 24, 15, 33, 0, tzinfo=IST)
    fake_now = lambda: datetime(2026, 2, 24, 15, 33, 5, tzinfo=IST)

    await eod_scheduler(
        shutdown_event=shutdown,
        trigger_eod_squareoff=trigger_squareoff,
        run_eod_analysis=run_analysis,
        notify=notify,
        get_open_positions=get_open_positions,
        bot_start_time=bot_start_time,
        _now_fn=fake_now,
    )

    trigger_squareoff.assert_not_awaited()
