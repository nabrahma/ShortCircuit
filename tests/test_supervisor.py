import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from main import _supervised


@pytest.mark.asyncio
async def test_clean_exit_no_restart():
    call_count = 0

    async def _once():
        nonlocal call_count
        call_count += 1
        return

    shutdown = asyncio.Event()
    await _supervised("test_loop", _once, shutdown)
    assert call_count == 1


@pytest.mark.asyncio
async def test_backoff_delay_uses_patchable_sleep():
    call_count = 0
    shutdown = asyncio.Event()

    async def _factory():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("boom")
        shutdown.set()
        return

    with patch("main._supervised_sleep", new=AsyncMock()) as mocked_sleep:
        await _supervised("test_loop", _factory, shutdown)
        mocked_sleep.assert_awaited_once_with(2)
