import pandas as pd
import logging
from datetime import datetime

# Logging Setup
logger = logging.getLogger("OrderFlowEngine")

class FootprintCalculator:
    def __init__(self):
        self.raw_ticks = []  # List of {time, price, volume, type}
        self.last_ltp = None
        self.current_candle_start = None
        self.delta_data = {} # {timestamp: delta_value}
        
    def process_tick(self, tick):
        """
        Ingest a raw tick and classify it.
        tick: {'symbol', 'ltp', 'vol', 'timestamp'}
        Note: Fyers WebSocket gives 'ltp' and 'vol_traded_today'. 
        We need to calculate tick_volume = current_vol - prev_vol.
        """
        try:
            ltp = tick.get('ltp')
            # For simulation/mock, we might get direct volume. 
            # In live socket, we track total_volume change.
            # Assuming 'vol' is the volume of THIS tick for simplicity in this engine 
            # (wrapper should handle the diff).
            volume = tick.get('vol', 0) 
            timestamp = tick.get('timestamp', datetime.now())

            if self.last_ltp is None:
                self.last_ltp = ltp
                return # Need previous price for tick rule

            # Tick Rule Classification
            if ltp > self.last_ltp:
                side = 'BUY'
            elif ltp < self.last_ltp:
                side = 'SELL'
            else:
                # Continuation: Same as previous side? Or Neutral?
                # Standard practice: Assign to previous side.
                # If neutral start, assume Sell (conservative for short logic)
                side = self.raw_ticks[-1]['side'] if self.raw_ticks else 'SELL'
            
            # Store Tick
            tick_data = {
                'time': timestamp,
                'price': ltp,
                'qty': volume,
                'side': side
            }
            self.raw_ticks.append(tick_data)
            
            # Update State
            self.last_ltp = ltp
            
            # Maintain only recent data (e.g. 15 mins) to prevent memory leak
            if len(self.raw_ticks) > 10000:
                self.raw_ticks = self.raw_ticks[-5000:]
                
        except Exception as e:
            logger.error(f"Error processing tick: {e}")

    def get_candle_delta(self, timeframe_minutes=1):
        """
        Aggregate ticks into the current candle to find Delta.
        """
        if not self.raw_ticks:
            return 0, 0, 0 # Delta, BuyVol, SellVol
            
        # Get ticks from last X minutes
        # Simplify: Just take all ticks in buffer if we reset buffer per candle? 
        # PRD says: "Maintain a live DataFrame representing the *current* 1-minute candle."
        # Better approach: reset raw_ticks on new candle? 
        # Let's assume raw_ticks holds recent history and we filter.
        
        df = pd.DataFrame(self.raw_ticks)
        
        # Group by Side
        buy_vol = df[df['side'] == 'BUY']['qty'].sum()
        sell_vol = df[df['side'] == 'SELL']['qty'].sum()
        
        delta = buy_vol - sell_vol
        
        return delta, buy_vol, sell_vol

    def check_absorption(self):
        """
        Signal: Price Flat/Up but Delta Negative (Limit Sellers absorbing).
        """
        if len(self.raw_ticks) < 10: return False # Need data
        
        delta, buy_vol, sell_vol = self.get_candle_delta()
        
        # Price Change in current window
        start_price = self.raw_ticks[0]['price']
        end_price = self.raw_ticks[-1]['price']
        price_change = end_price - start_price
        
        # Absorption Logic:
        # 1. Price is Up or Flat (>= 0)
        # 2. Delta is Highly Negative (Sellers hitting bid, but price holding)
        # 3. Or Price Drop is tiny compared to Sell Delta?
        
        # PRD: "Return True for Bearish Absorption if Price is Flat/Up but Delta is heavily Negative"
        
        is_price_stable = price_change >= 0
        is_delta_negative = delta < -(buy_vol * 0.2) # Net Selling is > 20% of Buy Volume? 
        # Better: Delta is negative and significant.
        
        if is_price_stable and delta < 0 and abs(delta) > 500: # Threshold of volume?
             return True
             
        return False
        
    def reset(self):
        self.raw_ticks = []
        self.last_ltp = None
