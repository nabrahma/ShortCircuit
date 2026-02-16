import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

class DiscretionarySignals:
    """
    Phase 41.3: Calculates 6 market signals for intelligent exit decisions.
    
    Signals:
    1. Orderflow (Bid/Ask Imbalance)
    2. Volume Momentum (Acceleration)
    3. Price Tests (Rejection at levels)
    4. Liquidity (Spread Analysis)
    5. Multi-Timeframe Alignment
    6. Tick Velocity (Speed of movement)
    """
    
    def __init__(self, fyers):
        self.fyers = fyers
        self.signal_cache = {} # {symbol_signal: (value, timestamp)}
        self.cache_duration = 30 # seconds

    def _get_cached(self, key):
        if key in self.signal_cache:
            val, ts = self.signal_cache[key]
            if (datetime.now() - ts).seconds < self.cache_duration:
                return val
        return None

    def _set_cache(self, key, value):
        self.signal_cache[key] = (value, datetime.now())

    def evaluate_all_signals(self, symbol, entry_price):
        """
        Aggregates all 6 signals into a decision scorecard.
        """
        signals = {}
        
        signals['orderflow'] = self.calculate_orderflow(symbol)
        signals['volume'] = self.calculate_volume_momentum(symbol)
        signals['price_tests'] = self.detect_price_tests(symbol, entry_price)
        signals['liquidity'] = self.calculate_liquidity_score(symbol)
        signals['mtf'] = self.check_mtf_alignment(symbol)
        signals['velocity'] = self.calculate_tick_velocity(symbol)
        
        # Scoring
        bearish = sum(1 for v in signals.values() if v == 1)
        bullish = sum(1 for v in signals.values() if v == -1)
        score = sum(signals.values())
        
        return {
            'signals': signals,
            'bearish_count': bearish,
            'bullish_count': bullish,
            'score': score
        }

    def calculate_orderflow(self, symbol):
        """
        Signal 1: Bid/Ask Imbalance from Market Depth
        Returns: +1 (Bearish/Selling), -1 (Bullish/Buying), 0 (Neutral)
        """
        try:
            depth = self.fyers.depth(data={"symbol": symbol, "ohlcv_flag": 1})
            # Start validation of response structure could be tricky depending on API version
            if 'd' in depth and symbol in depth['d']:
                # V2/V3 structure often returns dict with symbol key
                bids = depth['d'][symbol]['bids']
                asks = depth['d'][symbol]['asks']
                
                total_bid_vol = sum(b['volume'] for b in bids[:5])
                total_ask_vol = sum(a['volume'] for a in asks[:5])
                
                if total_ask_vol > total_bid_vol * 1.5:
                    return 1 # Heavy Selling
                elif total_bid_vol > total_ask_vol * 1.5:
                    return -1 # Heavy Buying
            return 0
        except Exception as e:
            # logger.error(f"Orderflow calc failed: {e}")
            return 0

    def calculate_volume_momentum(self, symbol):
        """
        Signal 2: Volume Acceleration (Recent 3 candles vs Previous 3)
        Returns: +1 (Accelerating), -1 (Dying), 0 (Neutral)
        """
        try:
            # Fetch 30m of 5m candles
            data = {
                "symbol": symbol,
                "resolution": "5",
                "date_format": "1",
                "range_from": (datetime.now() - timedelta(minutes=45)).strftime('%Y-%m-%d'),
                "range_to": datetime.now().strftime('%Y-%m-%d'),
                "cont_flag": "1"
            }
            resp = self.fyers.history(data=data)
            
            if resp.get('s') != 'ok' or not resp.get('candles'): return 0
            
            candles = resp['candles'][-7:] # Get last few
            if len(candles) < 6: return 0
            
            v = [c[5] for c in candles] # Volume index 5
            
            recent = sum(v[-3:])
            prev = sum(v[-6:-3])
            
            if recent > prev * 1.3: return 1 # Accelerating
            if recent < prev * 0.7: return -1 # Dying
            return 0
        except Exception:
            return 0

    def detect_price_tests(self, symbol, entry_price):
        """
        Signal 3: Rejections at Entry Price (Stop Hunting)
        Returns: +1 (Bearish Rejection), -1 (Breakout), 0 (Neutral)
        """
        try:
            # 1m candles
            data = {
                "symbol": symbol,
                "resolution": "1",
                "date_format": "1",
                "range_from": (datetime.now() - timedelta(minutes=20)).strftime('%Y-%m-%d'),
                "range_to": datetime.now().strftime('%Y-%m-%d'),
                "cont_flag": "1"
            }
            resp = self.fyers.history(data=data)
            if not resp.get('candles'): return 0

            candles = resp['candles']
            rejections = 0
            current_close = candles[-1][4]
            
            for c in candles[-10:]:
                high = c[2]
                close = c[4]
                # Wick above entry but close below
                if high > entry_price * 1.002 and close < entry_price:
                    rejections += 1
            
            if rejections >= 2: return 1 # Bearish Rejection (Good for Short)
            if current_close > entry_price * 1.003: return -1 # Breakout (Bad for Short)
            return 0
        except Exception:
            return 0

    def calculate_liquidity_score(self, symbol):
        """
        Signal 4: Spread Analysis (Panic vs Absorption)
        Returns: +1 (Widening/Panic), -1 (Tight/Absorption), 0 (Normal)
        """
        try:
            # Requires depth
            # Simplified: Use cached avg spread if possible, or just current snapshot
            depth = self.fyers.depth(data={"symbol": symbol, "ohlcv_flag": 1})
            if 'd' in depth and symbol in depth['d']:
                bids = depth['d'][symbol]['bids']
                asks = depth['d'][symbol]['asks']
                
                spread = asks[0]['price'] - bids[0]['price']
                avg_spread = self._get_cached(f"{symbol}_spread")
                
                if not avg_spread:
                    self._set_cache(f"{symbol}_spread", spread)
                    return 0
                
                # Update cache (EWMA)
                new_avg = (avg_spread * 0.9) + (spread * 0.1)
                self._set_cache(f"{symbol}_spread", new_avg)
                
                if spread > new_avg * 1.5: return 1 # Panic/Illiquid
                if spread < new_avg * 0.7: return -1 # Absorption
            return 0
        except Exception:
            return 0

    def check_mtf_alignment(self, symbol):
        """
        Signal 5: 5m and 15m Trend Alignment
        Returns: +1 (Bearish Align), -1 (Bullish Align), 0 (Mixed)
        """
        try:
            # 5m
            d5 = {
                "symbol": symbol, "resolution": "5", "date_format": "1",
                "range_from": (datetime.now() - timedelta(minutes=30)).strftime('%Y-%m-%d'),
                "range_to": datetime.now().strftime('%Y-%m-%d'), "cont_flag": "1"
            }
            # 15m
            d15 = {
                "symbol": symbol, "resolution": "15", "date_format": "1",
                "range_from": (datetime.now() - timedelta(minutes=60)).strftime('%Y-%m-%d'),
                "range_to": datetime.now().strftime('%Y-%m-%d'), "cont_flag": "1"
            }
            
            r5 = self.fyers.history(data=d5)
            r15 = self.fyers.history(data=d15)
            
            bearish = 0
            if r5.get('candles'):
                c = r5['candles'][-1]
                if c[4] < c[1]: bearish += 1 # Red candle
            
            if r15.get('candles'):
                c = r15['candles'][-1]
                if c[4] < c[1]: bearish += 1
                
            if bearish == 2: return 1
            if bearish == 0: return -1
            return 0
        except Exception:
            return 0

    def calculate_tick_velocity(self, symbol):
        """
        Signal 6: Speed of movement (Fast Drop vs Fast Rip)
        Returns: +1 (Fast Drop), -1 (Fast Rip), 0 (Normal)
        """
        try:
            # Check last 3 1m candles
            data = {
                "symbol": symbol, "resolution": "1", "date_format": "1",
                "range_from": (datetime.now() - timedelta(minutes=5)).strftime('%Y-%m-%d'),
                "range_to": datetime.now().strftime('%Y-%m-%d'), "cont_flag": "1"
            }
            resp = self.fyers.history(data=data)
            if not resp.get('candles'): return 0
            
            candles = resp['candles'][-3:]
            drops = 0
            rips = 0
            
            for c in candles:
                body = abs(c[4] - c[1])
                # If body is large relative to wick? Or just direction?
                # Simple direction
                if c[4] < c[1]: drops += 1
                else: rips += 1
                
            if drops >= 2: return 1 # Momentum Down
            if rips >= 2: return -1 # Momentum Up
            return 0
        except Exception:
            return 0
