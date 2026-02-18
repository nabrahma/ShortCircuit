
import pytest
import asyncio
import threading
from async_utils import AsyncExecutor, SyncWrapper

class AsyncMockComponent:
    def __init__(self, loop):
        self.loop = loop
        self.value = 0
        
    async def increment(self, amount):
        # Ensure we are on the correct loop
        if asyncio.get_running_loop() != self.loop:
            raise Exception("Wrong Loop!")
        await asyncio.sleep(0.1)
        self.value += amount
        return self.value

def test_async_bridge_execution():
    """
    Verify SyncWrapper -> AsyncExecutor -> AsyncComponent flow.
    """
    print(f"Main Thread: {threading.current_thread().name}")
    
    # 1. Init Executor
    executor = AsyncExecutor()
    loop = executor.get_loop()
    
    # 2. Init Async Component on the loop
    # In real app, we pass loop to component
    # We must instantiate it such that it knows the loop
    # Here we just pass it. 
    # But if __init__ did asyncio.get_event_loop(), we'd need to run it on executor.
    
    async_comp = AsyncMockComponent(loop)
    
    # 3. Create Wrapper
    wrapper = SyncWrapper(async_comp, executor)
    
    # 4. Call Synchronously
    result = wrapper.increment(10)
    print(f"Result 1: {result}")
    assert result == 10
    
    result = wrapper.increment(5)
    print(f"Result 2: {result}")
    assert result == 15
    
    # 5. Check side effects
    assert async_comp.value == 15
    
    print("✅ Async Bridge Test Passed.")

if __name__ == "__main__":
    test_async_bridge_execution()
