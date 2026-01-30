import unittest
import pandas as pd
import sys
import os

# Add parent dir to path to import modules
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from strategy_brain import StrategyBrain

class TestStrategyLogic(unittest.TestCase):
    def setUp(self):
        self.sb = StrategyBrain()

    def test_liquidity_sweep_signal(self):
        # Create a mock dataframe
        # Scenario: 
        # Candle 1: High 100
        # Candle 2 (Current): High 101 (Sweep), Close 99 (Fail)
        # Prev Swing High logic requires looking back.
        # Logic: 15 candles window?
        
        data = {
            'open': [100] * 20,
            'high': [100] * 20,
            'low': [90] * 20,
            'close': [95] * 20,
            'volume': [1000] * 20
        }
        df = pd.DataFrame(data)
        
        # Set a Swing High at index -5
        df.loc[15, 'high'] = 105.0 # Prev Swing High
        
        # Current Candle (Last one)
        # Sweep: High > 105, Close < 105
        df.loc[19, 'high'] = 106.0
        df.loc[19, 'close'] = 104.0
        
        # Needs VWAP to pass '2.5% extension' check
        # VWAP must be far
        # If Close is 104, VWAP should be < 101.5 approx.
        # Let's set VWAP column
        df['VWAP'] = 100.0 # 4% away

        # Market Depth Mock
        # Sell > Buy * 1.5
        depth = {
            'buy': [{'quantity': 100}],
            'sell': [{'quantity': 200}] # 200 > 150
        }
        
        signal, sl = self.sb.check_signals('TEST', df, depth)
        self.assertTrue(signal, "Should detect liquidity sweep")
        self.assertEqual(sl, 106.1, "SL should be High + 0.1")

    def test_no_signal_if_close_above_high(self):
        # Case where it breaks high and closes ABOVE (Breakout, not sweep)
        data = {
            'open': [100] * 20,
            'high': [100] * 20,
            'low': [90] * 20,
            'close': [95] * 20,
            'VWAP': [90] * 20
        }
        df = pd.DataFrame(data)
        df.loc[15, 'high'] = 105.0 # Swing
        
        df.loc[19, 'high'] = 106.0
        df.loc[19, 'close'] = 106.0 # Closed above
        
        depth = {'buy': [{'quantity': 100}], 'sell': [{'quantity': 200}]}
        
        signal, _ = self.sb.check_signals('TEST', df, depth)
        self.assertFalse(signal, "Should fail if close > prev_high")

if __name__ == '__main__':
    unittest.main()
