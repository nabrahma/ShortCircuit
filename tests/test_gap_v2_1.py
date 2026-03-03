from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from fyers_broker_interface import FyersBrokerInterface
from market_context import MarketContext


IST = timezone(timedelta(hours=5, minutes=30))


def _epoch_ist(today, hour, minute):
    return int(datetime(today.year, today.month, today.day, hour, minute, tzinfo=IST).timestamp())


def test_morning_range_mid_market_start():
    today = datetime.now(IST).date()

    candles_1m = [
        [_epoch_ist(today, 9, 15), 100, 105, 99, 103, 1000],
        [_epoch_ist(today, 9, 30), 103, 112, 98, 110, 1400],
        [_epoch_ist(today, 9, 45), 110, 111, 100, 104, 1300],
        [_epoch_ist(today, 10, 0), 104, 106, 102, 105, 900],
    ]
    candles_5m = [
        [_epoch_ist(today, 10, 20), 104, 106, 103, 105, 5000],
        [_epoch_ist(today, 10, 25), 105, 107, 104, 106, 4800],
    ]

    class FakeFyers:
        def __init__(self):
            self.calls = []

        def history(self, data):
            self.calls.append(data)
            if data.get("resolution") == "1":
                return {"s": "ok", "candles": candles_1m}
            if data.get("resolution") == "5":
                return {"s": "ok", "candles": candles_5m}
            return {"s": "ok", "candles": []}

    fake = FakeFyers()
    mc = MarketContext(fake)

    regime, msg = mc.get_market_regime()

    assert regime in {"RANGE", "TREND_UP", "TREND_DOWN"}
    assert mc.morning_range_valid is True
    assert round(mc._morning_high, 2) == 112
    assert round(mc._morning_low, 2) == 98
    assert "unavailable" not in msg.lower()
    assert any(call.get("resolution") == "1" for call in fake.calls)


def test_cache_seed_reduces_missing_count():
    broker = FyersBrokerInterface(
        access_token="test_token",
        client_id="test_client",
        db_manager=MagicMock(),
        emergency_logger=MagicMock(),
    )

    symbols = [f"NSE:TEST{i:04d}-EQ" for i in range(120)]

    def _quotes_side_effect(data):
        batch_symbols = data["symbols"].split(",")
        return {
            "s": "ok",
            "d": [
                {
                    "n": sym,
                    "v": {"lp": 100.0, "volume": 1000, "chp": 1.0, "oi": 0},
                }
                for sym in batch_symbols
            ],
        }

    broker.rest_client = MagicMock()
    broker.rest_client.quotes.side_effect = _quotes_side_effect

    seeded = broker.seed_from_rest(symbols)
    snap = broker.cache_health_snapshot()

    assert seeded == len(symbols)
    assert snap["total"] == len(symbols)
    assert snap["seeded"] == len(symbols)
    assert snap["missing"] == 0
    assert snap["fresh"] == 0
