import pytest
import time
from fyers_broker_interface import MinuteCandleAggregator, TickData

def test_candle_formation_and_transition():
    agg = MinuteCandleAggregator(max_candles=5)
    symbol = "NSE:SBIN-EQ"
    
    # Base timestamp (Start of a minute)
    t0 = 1710000000.0  # Just a fixed timestamp
    
    # 1. Start a candle at T+0s (First tick sets baseline, volume 0)
    agg.update(TickData({'symbol': symbol, 'ltp': 100, 'volume': 1000}), timestamp=t0)
    
    candles = agg.get_candles(symbol)
    assert len(candles) == 1
    assert candles[0].open == 100
    assert candles[0].epoch == 1710000000
    assert candles[0].volume == 0
    
    # 2. Update at T+30s (Same minute)
    # Volume delta: 1500 - 1000 = 500
    agg.update(TickData({'symbol': symbol, 'ltp': 110, 'volume': 1500}), timestamp=t0 + 30)
    c = agg.get_candles(symbol)[0]
    assert c.high == 110
    assert c.close == 110
    assert c.volume == 500
    assert len(agg.get_candles(symbol)) == 1

    # 3. Transition to next minute at T+65s (New minute resets baseline)
    agg.update(TickData({'symbol': symbol, 'ltp': 105, 'volume': 2000}), timestamp=t0 + 65)
    
    candles = agg.get_candles(symbol)
    assert len(candles) == 2
    
    # Old candle (finalized) volume should be the final delta of that minute
    assert candles[1].epoch == 1710000060 # second candle is the current one
    assert candles[0].close == 110
    assert candles[0].volume == 500
    
    # New candle (current) starts at 0 for the first tick
    assert candles[1].open == 105
    assert candles[1].volume == 0

def test_max_candles_limit():
    agg = MinuteCandleAggregator(max_candles=2)
    symbol = "NSE:SBIN-EQ"
    t0 = 1710000000.0
    
    # Four minutes of data
    agg.update(TickData({'symbol': symbol, 'ltp': 10}, ), timestamp=t0)      # Min 0
    agg.update(TickData({'symbol': symbol, 'ltp': 20}, ), timestamp=t0 + 60) # Min 1
    agg.update(TickData({'symbol': symbol, 'ltp': 30}, ), timestamp=t0 + 120)# Min 2
    agg.update(TickData({'symbol': symbol, 'ltp': 40}, ), timestamp=t0 + 180)# Min 3
    
    # get_candles(n=100) should only return max_candles (2) in history + current (1) = 3 total
    # But wait, max_candles is for history only in my implementation.
    # Current is separate.
    candles = agg.get_candles(symbol, n=10)
    assert len(candles) == 3 # Hist (2) + Current (1)
    assert candles[0].ltp == 20 if hasattr(candles[0], 'ltp') else candles[0].close == 20
