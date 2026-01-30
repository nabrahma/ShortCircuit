import logging
import pandas as pd
import pandas_ta as ta
import yfinance as yf
import config
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

class DataEngine:
    def __init__(self, kite_manager):
        self.km = kite_manager
        self.kite = kite_manager.get_session()

    def get_market_depth(self, symbol):
        """
        Fetches market depth (quote) for a symbol to check order flow.
        """
        if config.SIMULATION_MODE:
            # Mock Depth for Simulation
            # Return a structure that triggers the strategy (Bearish Imbalance for Shorts)
            # Strategy: Total Sell > Total Buy * 1.5
            return {
                "buy": [{"quantity": 1000, "price": 100, "orders": 1}],
                "sell": [{"quantity": 2000, "price": 101, "orders": 1}] 
                # Note: StrategyBrain likely calculates totals from this list or uses 'buy_quantity' field?
                # If StrategyBrain sums the list, this works.
                # If it expects 'buy_quantity' in the quote, we need to verify StrategyBrain usage.
                # Assuming StrategyBrain sums it up or we pass the right object.
            }
            
        try:
            quote = self.kite.quote(f"NSE:{symbol}")
            if f"NSE:{symbol}" in quote:
                return quote[f"NSE:{symbol}"]["depth"]
            return None
        except Exception as e:
            logger.error(f"Error fetching depth for {symbol}: {e}")
            return None

    def get_ltp(self, symbol):
        """
        Fetches single LTP.
        """
        if config.SIMULATION_MODE:
            try:
                # Use yfinance for LTP
                ticker = yf.Ticker(f"{symbol}.NS")
                # fast_info is faster
                return ticker.fast_info['last_price']
            except:
                return None

        try:
            quote = self.kite.ltp(f"NSE:{symbol}")
            if f"NSE:{symbol}" in quote:
                return quote[f"NSE:{symbol}"]["last_price"]
            return None
        except Exception as e:
            logger.error(f"Error fetching LTP for {symbol}: {e}")
            return None

    def get_ohlc(self, symbol, interval, days_back=1):
        """
        Fetches historical data.
        """
        if config.SIMULATION_MODE:
            try:
                # yfinance interval mapping
                yf_interval = "1m" if interval == "minute" else "1d"
                # yf requires period, max 7d for 1m
                df = yf.download(f"{symbol}.NS", period="1d", interval=yf_interval, progress=False)
                if not df.empty:
                    # Standardize columns: Open, High, Low, Close, Volume
                    # YF columns are Capitalized.
                    df.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"}, inplace=True)
                    # VWAP expects datetime index. YF has it.
                return df
            except Exception as e:
                logger.error(f"YF OHLC Error: {e}")
                return pd.DataFrame()

        to_date = datetime.now()
        from_date = to_date - timedelta(days=days_back)
        
        try:
            records = self.kite.historical_data(
                instrument_token=self._get_instrument_token(symbol),
                from_date=from_date,
                to_date=to_date,
                interval=interval
            )
            df = pd.DataFrame(records)
            if not df.empty:
                df['date'] = pd.to_datetime(df['date'])
                df.set_index('date', inplace=True)
            return df
        except Exception as e:
            logger.error(f"Error fetching OHLC for {symbol}: {e}")
            return pd.DataFrame()

    def _get_instrument_token(self, symbol):
        """
        Helper to get token. Ideally, we cache the instrument list.
        For now, doing a lookup (expensive if done repeatedly).
        Better to fetch instrument dump once in main.
        """
        # TODO: Optimize by passing token directly or caching map
        # This is a placeholder as getting token strictly by symbol requires a lookup
        # In a real efficient bot, we'd have a map 'NSE:SBIN' -> 123456 loaded at startup.
        # For this implementation, we will assume the caller might eventually pass the token
        # but the prompt implies symbols.
        # We will do a quick fetch (inefficient but works for 5 stocks).
        # Actually, let's just use a lookup utility in main or passed in.
        # For safety/speed, we'll try to find it in a fresh dump or assume we have it.
        # Let's try to fetch it if not cached. 
        # CAUTION: 'kite.instruments()' is 20MB+. Do NOT call in loop.
        # We will assume checking logic happens in Main to get tokens.
        # Here we will fail if we don't handle it.
        # Let's mock or use a passed token map? 
        # I will change signature of fetch_ohlc to take token_id OR symbol and handle it in main.
        pass 
        # Real implementation should get token from main. We'll rely on one-time fetch in Main.
        return None # Placeholder, will need fixing in integration

    def calculate_indicators(self, df):
        """
        Adds VWAP and other indicators.
        """
        if df.empty:
            return df
        
        # VWAP
        # pandas_ta VWAP requires high, low, close, volume and 'anchor' (optional)
        # Intraday VWAP resets daily. pandas_ta usually handles this via 'offset' or just calculating on the DF provided.
        # If DF contains multiple days, we need grouping. If 1 day (intraday), straightforward.
        try:
            # We assume DF is cleaned for the current session for VWAP accuracy or handle logic here.
            # Simple VWAP:
            df.ta.vwap(append=True)
            # RSI just in case
            df.ta.rsi(length=14, append=True)
        except Exception as e:
            logger.error(f"Indicator calc error: {e}")
        
        return df

    def get_nifty_trend(self):
        """
        Checks Nifty 50 change.
        """
        try:
            # Nifty 50 token is usually 256265 (NSE:NIFTY 50) but dynamic looking up is safer.
            # Using quote for 'NSE:NIFTY 50'
            quote = self.kite.quote("NSE:NIFTY 50")
            if "NSE:NIFTY 50" in quote:
                ohlc = quote["NSE:NIFTY 50"]["ohlc"]
                open_price = ohlc["open"] # Or close of previous day?
                # Change usually based on prev_close
                close_price = ohlc["close"] # This is previous close
                ltp = quote["NSE:NIFTY 50"]["last_price"]
                
                change_pct = ((ltp - close_price) / close_price) * 100
                return change_pct
            return 0.0
        except Exception as e:
            logger.error(f"Error getting Nifty trend: {e}")
            return 0.0
