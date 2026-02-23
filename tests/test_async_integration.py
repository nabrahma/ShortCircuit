import asyncio
import threading

from async_utils import AsyncExecutor, SyncWrapper


class AsyncMockComponent:
    def __init__(self, loop):
        self.loop = loop
        self.value = 0

    async def increment(self, amount):
        if asyncio.get_running_loop() != self.loop:
            raise RuntimeError("Wrong Loop")
        await asyncio.sleep(0.01)
        self.value += amount
        return self.value


def _start_test_loop(executor: AsyncExecutor):
    ready = threading.Event()
    loop = asyncio.new_event_loop()

    def _runner():
        asyncio.set_event_loop(loop)
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=_runner, daemon=True, name="AsyncTestLoop")
    thread.start()
    if not ready.wait(timeout=2):
        raise TimeoutError("Test loop did not start")

    executor.loop = loop
    return loop, thread


def test_async_bridge_execution():
    """
    Verify SyncWrapper -> AsyncExecutor.run -> async component flow.
    """
    executor = AsyncExecutor()
    loop, thread = _start_test_loop(executor)
    try:
        async_comp = AsyncMockComponent(loop)
        wrapper = SyncWrapper(async_comp, executor)

        result = wrapper.increment(10)
        assert result == 10

        result = wrapper.increment(5)
        assert result == 15

        assert async_comp.value == 15
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        executor.loop = None
