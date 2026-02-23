import asyncio

import pytest

from order_manager import OrderManager


class MockFyers:
    def __init__(self):
        self.cancel_called = False
        self.exit_called = False
        self.place_order_log = []
        self.cancel_order_log = []

    async def cancel_order(self, order_id):
        self.cancel_called = True
        self.cancel_order_log.append(order_id)
        return True

    async def place_order(self, symbol, side, qty, order_type):
        call = {
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "order_type": order_type,
        }
        self.place_order_log.append(call)

        if order_type == "MARKET" and side == "BUY":
            if not self.cancel_called:
                raise RuntimeError("RACE_CONDITION: Exit placed before cancel")
            self.exit_called = True
        return "ORDER_123"

    async def wait_for_fill(self, order_id, timeout=30.0):
        return True

    async def get_position(self, symbol):
        return {"symbol": symbol, "qty": -100}


class MockDB:
    async def log_trade_exit(self, symbol, data):
        return None


@pytest.mark.asyncio
async def test_safe_exit_race_condition():
    """
    Verify safe_exit cancels SL before placing exit order.
    """
    fyers = MockFyers()
    om = OrderManager(fyers, None, MockDB())

    symbol = "NSE:TEST-EQ"
    om.active_positions[symbol] = {
        "symbol": symbol,
        "qty": 100,
        "side": "SHORT",
        "sl_id": "SL_ORDER_999",
        "status": "OPEN",
    }

    ok = await om.safe_exit(symbol, "TEST_EXIT")
    assert ok is True
    assert fyers.cancel_called is True
    assert fyers.exit_called is True
    assert fyers.cancel_order_log == ["SL_ORDER_999"]


@pytest.mark.asyncio
async def test_safe_exit_concurrency():
    """
    Verify double-exit prevention using per-symbol lock + exit_in_progress state.
    """
    fyers = MockFyers()
    om = OrderManager(fyers, None, MockDB())
    symbol = "NSE:TEST-EQ"
    om.active_positions[symbol] = {
        "symbol": symbol,
        "qty": 100,
        "side": "SHORT",
        "sl_id": "SL_ORDER_999",
        "status": "OPEN",
    }

    task1 = asyncio.create_task(om.safe_exit(symbol, "EXIT_1"))
    task2 = asyncio.create_task(om.safe_exit(symbol, "EXIT_2"))
    await asyncio.gather(task1, task2)

    market_buys = [
        d for d in fyers.place_order_log
        if d.get("order_type") == "MARKET" and d.get("side") == "BUY"
    ]
    assert len(market_buys) == 1
