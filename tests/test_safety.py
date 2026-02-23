import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from order_manager import OrderManager


class MockBroker:
    def __init__(self):
        self.sequence = []
        self.cancelled = []
        self.orders = []
        self._positions = []
        self._orderbook = {"s": "ok", "orderBook": []}
        self.rest_client = SimpleNamespace(orderbook=self._sync_orderbook)

    def _sync_orderbook(self):
        return self._orderbook

    async def cancel_order(self, order_id):
        self.sequence.append(("cancel", order_id))
        self.cancelled.append(order_id)
        return True

    async def place_order(self, symbol, side, qty, order_type):
        self.sequence.append(("place", symbol, side, qty, order_type))
        self.orders.append(
            {
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "order_type": order_type,
            }
        )
        return f"ORD_{len(self.orders)}"

    async def wait_for_fill(self, order_id, timeout=30.0):
        await asyncio.sleep(0)
        self.sequence.append(("fill", order_id))
        return True

    async def get_position(self, symbol):
        return {"symbol": symbol, "qty": -100}

    async def get_all_positions(self):
        return list(self._positions)


@pytest.mark.asyncio
async def test_phantom_fill_prevention():
    """
    Verify SL cancel runs before market exit in safe_exit().
    """
    broker = MockBroker()
    bot = AsyncMock()
    om = OrderManager(broker, bot)

    symbol = "NSE:SBIN-EQ"
    om.active_positions[symbol] = {
        "symbol": symbol,
        "qty": 100,
        "side": "SHORT",
        "status": "OPEN",
        "sl_id": "SL_ORDER_999",
    }

    ok = await om.safe_exit(symbol, "SOFT_STOP")
    assert ok is True
    assert "SL_ORDER_999" in broker.cancelled

    cancel_idx = next(i for i, e in enumerate(broker.sequence) if e[0] == "cancel")
    place_idx = next(i for i, e in enumerate(broker.sequence) if e[0] == "place")
    assert cancel_idx < place_idx


@pytest.mark.asyncio
async def test_hard_stop_detection():
    """
    Verify hard-stop monitor closes local state when SL is filled at broker.
    """
    broker = MockBroker()
    om = OrderManager(broker, None)

    symbol = "NSE:RELIANCE-EQ"
    om.active_positions[symbol] = {
        "symbol": symbol,
        "status": "OPEN",
        "sl_id": "SL_ORDER_555",
    }

    broker._orderbook = {
        "s": "ok",
        "orderBook": [{"id": "SL_ORDER_555", "status": 2}],
    }

    closed = await om.monitor_hard_stop_status(symbol)
    assert closed is True
    assert symbol not in om.active_positions


@pytest.mark.asyncio
async def test_race_condition():
    """
    Verify concurrent exits place only one market exit.
    """
    broker = MockBroker()
    om = OrderManager(broker, None)

    symbol = "NSE:TATASTEEL-EQ"
    om.active_positions[symbol] = {
        "symbol": symbol,
        "qty": 100,
        "side": "SHORT",
        "status": "OPEN",
        "sl_id": "SL_ORDER_777",
    }

    await asyncio.gather(
        om.safe_exit(symbol, "REASON_1"),
        om.safe_exit(symbol, "REASON_2"),
    )

    market_buys = [
        o for o in broker.orders
        if o["order_type"] == "MARKET" and o["side"] == "BUY"
    ]
    assert len(market_buys) == 1


@pytest.mark.asyncio
async def test_startup_reconciliation():
    """
    Verify startup reconciliation cancels stale pending orders.
    """
    broker = MockBroker()
    om = OrderManager(broker, None)

    broker._orderbook = {
        "s": "ok",
        "orderBook": [
            {"id": "STALE_SL", "status": 6, "symbol": "NSE:INFY-EQ"},
        ],
    }

    await om.startup_reconciliation()
    assert "STALE_SL" in broker.cancelled
