
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from order_manager import OrderManager

# Mock Fyers and DB
class MockFyers:
    def __init__(self):
        self.cancel_called = False
        self.exit_called = False
        self.place_order_log = []
        self.cancel_order_log = []

    def cancel_order(self, data):
        self.cancel_called = True
        self.cancel_order_log.append(data)
        return {'s': 'ok'}

    def place_order(self, data):
        self.place_order_log.append(data)
        if data.get('type') == 2: # Market Exit
            if not self.cancel_called:
                 raise Exception("RACE CONDITION: Exit placed before Cancel!")
            self.exit_called = True
        return {'s': 'ok', 'id': 'ORDER_123'}

    def positions(self):
        return {'s': 'ok', 'netPositions': []}
        
    def orderbook(self):
        return {'s': 'ok', 'orderBook': []}

class MockDB:
    async def log_trade_exit(self, symbol, data):
        pass

@pytest.mark.asyncio
async def test_safe_exit_race_condition():
    """
    Verify safe_exit cancels SL *before* placing exit order.
    """
    fyers = MockFyers()
    # Mock run_in_executor to just call the function directly for testing logic flow
    # or keep the real one? Real one uses threads.
    # To keep test deterministic, we might want to mock _run_blocking or allow it.
    # Since MockFyers is fast, real thread pool is fine.
    
    om = OrderManager(fyers, None, MockDB())
    
    # Setup Active Position
    symbol = "NSE:TEST-EQ"
    om.active_positions[symbol] = {
        'symbol': symbol,
        'qty': 100,
        'side': 'SHORT',
        'sl_id': 'SL_ORDER_999',
        'status': 'OPEN'
    }
    
    # Execute Safe Exit
    await om.safe_exit(symbol, "TEST_EXIT")
    
    # Assertions
    assert fyers.cancel_called == True, "SL Order should be cancelled"
    assert fyers.exit_called == True, "Exit Order should be placed"
    assert len(fyers.cancel_order_log) == 1
    assert fyers.cancel_order_log[0]['id'] == 'SL_ORDER_999'
    
    # Verify Order: Cancel happened before Exit (Logic in MockFyers raises exception otherwise)
    print("✅ Race Condition Test Passed: SL Cancelled before Exit.")

@pytest.mark.asyncio
async def test_safe_exit_concurrency():
    """
    Verify double-exit prevention using locks.
    """
    fyers = MockFyers()
    om = OrderManager(fyers, None, MockDB())
    symbol = "NSE:TEST-EQ"
    om.active_positions[symbol] = {
        'symbol': symbol,
        'qty': 100,
        'side': 'SHORT',
        'sl_id': 'SL_ORDER_999',
        'status': 'OPEN'
    }
    
    # Run 2 exits concurrently
    task1 = asyncio.create_task(om.safe_exit(symbol, "EXIT_1"))
    task2 = asyncio.create_task(om.safe_exit(symbol, "EXIT_2"))
    
    await asyncio.gather(task1, task2)
    
    # Only 1 exit order should be placed
    market_orders = [d for d in fyers.place_order_log if d.get('type') == 2]
    assert len(market_orders) == 1, f"Should place exactly 1 exit order, placed {len(market_orders)}"
    print("✅ Concurrency Test Passed: Double Exit Prevented.")
