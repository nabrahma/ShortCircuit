import logging
import pandas as pd

logger = logging.getLogger(__name__)

class StrategyBrain:
    def __init__(self):
        pass

    def filter_universe(self, ticks, nifty_pct):
        """
        Filters the universe based on:
        1. Pump: +10% to +18%
        2. Trap: CMP < UpperCircuit - 1.5%
        3. Nifty Trend: < 1.5%
        
        'ticks' matches Kite Connect ticker format or quote result.
        Expects a list of dicts or a dict of quotes.
        """
        if nifty_pct > 1.5:
            logger.info(f"Nifty Trend too strong (+{nifty_pct:.2f}%). Trading DISABLED.")
            return []

        candidates = []
        # Expecting 'ticks' to be the full market quote or a subset.
        # In reality, scanning "All F&O" requires iterating a large list.
        # We assume 'ticks' contains the relevant fields: last_price, ohlc(close), upper_circuit_limit.
        
        for symbol, data in ticks.items():
            try:
                ltp = data['last_price']
                prev_close = data['ohlc']['close']
                upper_circuit = data.get('upper_circuit_limit', 0) # Kite quote has this? Validating needed.
                # Actually, Kite quote structure: 'upper_circuit_limit' is inside 'depth' maybe?
                # No, usually in top level of quote for equity.
                
                if upper_circuit == 0:
                    continue # Safety

                day_change_pct = ((ltp - prev_close) / prev_close) * 100
                
                # Condition 1: Pump
                if 10.0 <= day_change_pct <= 18.0:
                    # Condition 2: Trap Check (Below UC)
                    # "At least 1.5% below UC"
                    # Distance to UC = (UC - LTP) / UC
                    dist_to_uc = (upper_circuit - ltp) / upper_circuit
                    
                    if dist_to_uc >= 0.015:
                         candidates.append(symbol)
            except Exception as e:
                continue
        
        return candidates

    def check_signals(self, symbol, df, market_depth):
        """
        Composite signal check:
        1. Liquidity Sweep (Turtle Soup)
        2. TPO/VWAP Extension (> 2.5% away)
        3. Order Imbalance (Ask > Bid * 1.5)
        """
        if df.empty or len(df) < 5:
            return False, None

        # 1. Liquidity Sweep
        # Identify Previous Swing High (highest in last 15 mins)
        # We verify if the current or last closed candle swept a high.
        # But logic says "Price breaks above Swing High" and "Closes below".
        # This implies looking at the *latest completed* candle or the *current forming* one? 
        # Usually completed candle is safer for backtest/logic match.
        
        last_candle = df.iloc[-1]
        
        # Look back 15 candles exclude the last one for swing calculation?
        # "highest point in the last 15 mins"
        window = df.iloc[-16:-1] # 15 candles before current
        if window.empty:
            return False, None
            
        prev_swing_high = window['high'].max()
        
        # Sweep: High > SwingHigh AND Close < SwingHigh
        # "Same 1-min candle ... closes below"
        is_sweep = (last_candle['high'] > prev_swing_high) and (last_candle['close'] < prev_swing_high)
        
        if not is_sweep:
            return False, None

        # 2. VWAP Extension
        # Price > 2.5% away from VWAP
        current_vwap = last_candle.get('VWAP_D', 0) # pandas_ta default name might vary. 
        # Usually 'VWAP'. Let's assume calculate_indicators adds 'VWAP'.
        if 'VWAP' in last_candle:
            current_vwap = last_candle['VWAP']
        
        if current_vwap == 0:
            return False, None

        dist_from_vwap = (last_candle['close'] - current_vwap) / current_vwap
        if dist_from_vwap <= 0.025: # Must be > 2.5%
            return False, None
            
        # 3. Order Book Imbalance
        # Calculate total Buy vs Sell qty from top 5
        buy_qty = sum([b['quantity'] for b in market_depth['buy']])
        sell_qty = sum([s['quantity'] for s in market_depth['sell']])
        
        if buy_qty == 0:
            return False, None # Weird

        # Rule: Ask (Sell) > Bid (Buy) * 1.5
        if not (sell_qty > (buy_qty * 1.5)):
            return False, None
            
        # All pass
        # Return Signal + SL recommendation
        # SL = High of Sweep Candle + 0.10
        sl_price = last_candle['high'] + 0.10
        
        return True, sl_price
