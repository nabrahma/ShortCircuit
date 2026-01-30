import requests
from bs4 import BeautifulSoup
import yfinance as yf
from datetime import datetime, timedelta
import config
import logging
import pandas as pd
import os

logger = logging.getLogger(__name__)

class MarketScanner:
    def __init__(self, kite):
        self.kite = kite
        self.blacklist = self._load_blacklist()

    def _load_blacklist(self):
        if hasattr(self, 'blacklist') and self.blacklist:
            return self.blacklist
        
        # Load From File
        bl = []
        if os.path.exists("blacklist.txt"):
            with open("blacklist.txt", "r") as f:
               bl = [l.strip() for l in f if l.strip()]
        return bl

    def get_sim_universe(self):
        """
        Returns a hardcoded list of liquid NSE stocks for Simulation.
        (Nifty 100 + High IV Stocks)
        """
        return [
            "ABB", "ACC", "ADANIENSOL", "ADANIENT", "ADANIGREEN", "ADANIPORTS", "ADANIPOWER", "ALKEM", "AMBUJACEM", "APOLLOHOSP",
            "APOLLOTYRE", "ASHOKLEY", "ASIANPAINT", "ASTRAL", "ATGL", "AUBANK", "AUROPHARMA", "AXISBANK", "BAJAJ-AUTO", "BAJAJFINSV",
            "BAJAJHLDNG", "BAJFINANCE", "BALKRISIND", "BANDHANBNK", "BANKBARODA", "BANKINDIA", "BEL", "BERGEPAINT", "BHARATFORG", "BHARTIARTL",
            "BHEL", "BIOCON", "BOSCHLTD", "BPCL", "BRITANNIA", "CANBK", "CHOLAFIN", "CIPLA", "COALINDIA", "COFORGE",
            "COLPAL", "CONCOR", "CUMMINSIND", "DABUR", "DELHIVERY", "DIVISLAB", "DLF", "DMART", "DRREDDY", "EICHERMOT",
            "ESCORTS", "GAIL", "GLAND", "GODREJCP", "GODREJPROP", "GRASIM", "HAL", "HAVELLS", "HCLTECH", "HDFCAMC",
            "HDFCBANK", "HDFCLIFE", "HEROMOTOCO", "HINDALCO", "HINDPETRO", "HINDUNILVR", "ICICIBANK", "ICICIGI", "ICICIPRULI", "IDEA",
            "IDFCFIRSTB", "IEX", "IGL", "INDHOTEL", "INDIANB", "INDIGO", "INDUSINDBK", "INDUSTOWER", "INFY", "IOC",
            "IRCTC", "ITC", "JINDALSTEL", "JIOFIN", "JSWENERGY", "JSWSTEEL", "JUBLFOOD", "KOTAKBANK", "LALPATHLAB", "LICI",
            "LODHA", "LT", "LTIM", "LTTS", "LUPIN", "M&M", "M&MFIN", "MARICO", "MARUTI", "MCDOWELL-N",
            "MCX", "METROPOLIS", "MFSL", "MGL", "MOTHERSON", "MPHASIS", "MRF", "MUTHOOTFIN", "NAUKRI", "NESTLEIND",
            "NMDC", "NTPC", "OBEROIRLTY", "OFSS", "ONGC", "PAGEIND", "PAYTM", "PEL", "PERSISTENT", "PETRONET",
            "PFC", "PIDILITIND", "PIIND", "PNB", "POLYCAB", "POONAWALLA", "POWERGRID", "PRESTIGE", "PVRINOX", "REC",
            "RECLTD", "RELIANCE", "SAIL", "SBICARD", "SBILIFE", "SBIN", "SHREECEM", "SHRIRAMFIN", "SIEMENS", "SOLARINDS",
            "SONACOMS", "SRF", "SUNPHARMA", "SUNTV", "SUPREMEIND", "SYNGENE", "TATACHEM", "TATACOMM", "TATACONSUM", "TATAELXSI",
            "TATAMOTORS", "TATAPOWER", "TATASTEEL", "TCS", "TECHM", "TIINDIA", "TITAN", "TORNTPHARM", "TRENT", "TVSMOTOR",
            "UBL", "ULTRACEMCO", "UNIONBANK", "UPL", "VBL", "VEDL", "VOLTAS", "WIPRO", "YESBANK", "ZOMATO", "ZYDUSLIFE"
        ]

    def scan(self):
        """
        Orchestrates the scanning process.
        """
        # SIMULATION MODE
        if config.SIMULATION_MODE:
            logger.info("Scanning via yfinance (Simulating Top Gainers)...")
            universe = self.get_sim_universe()
            candidates = []
            
            # Batch fetch via yfinance
            chunk_size = 50
            for i in range(0, len(universe), chunk_size):
                chunk = universe[i:i+chunk_size]
                tickers = [f"{s}.NS" for s in chunk]
                
                try:
                    # '1d' period gives today's candle (High/Low/Close) even if closed.
                    data = yf.download(tickers, period="2d", group_by='ticker', progress=False)
                    
                    for sym in chunk:
                        try:
                            df = data[f"{sym}.NS"]
                            if df.empty or len(df) < 2: continue
                            
                            prev_close = df['Close'].iloc[-2]
                            ltp = df['Close'].iloc[-1]
                            
                            pct_change = ((ltp - prev_close) / prev_close) * 100
                            
                            # Log significant movers for debug
                            if pct_change > 5.0:
                                logger.info(f"Sim Scanner: {sym} is up {pct_change:.2f}%")

                            # Strategy Filter: > 5% Pump (Widened for Post-Market Test)
                            if pct_change >= 5.0:
                                logger.info(f"Candidate Found: {sym} (+{pct_change:.2f}%)")
                                candidates.append(sym)
                                
                        except Exception:
                            continue
                except Exception as e:
                    logger.error(f"YF Scan Error: {e}")
            
            return candidates

        # REAL MODE (Kite)
        full_universe = self.get_universe()
        
        # Optimization: Limit to top 500 by some metric? 
        # Since we can't get volume without quoting, we have to batch quote.
        # 3000 symbols / 400 chunk = 8 calls. Feasible.
        
        candidates = []
        chunk_size = 400
        
        logger.info(f"Scanning {len(full_universe)} stocks in chunks...")
        
        for i in range(0, len(full_universe), chunk_size):
            chunk = full_universe[i:i+chunk_size]
            exchange_tokens = [f"NSE:{s}" for s in chunk]
            
            try:
                quotes = self.kite.quote(exchange_tokens)
                
                for token, quote in quotes.items():
                    symbol = token.replace("NSE:", "")
                    
                    # Missing data check
                    if 'ohlc' not in quote or 'last_price' not in quote:
                        continue
                        
                    ohlc = quote['ohlc']
                    ltp = quote['last_price']
                    volume = quote.get('volume', 0)
                    
                    # Filter 3: Penny Stock Check (> 50)
                    if ltp < 50:
                        continue
                        
                    # Filter 4: Liquidity Check (Turnover > 5Cr)
                    turnover = ltp * volume
                    if turnover < 50000000: # 5 Cr
                        continue
                        
                    # Filter 5: Pump Condition (+10% to +18%)
                    prev_close = ohlc['close']
                    day_change_pct = ((ltp - prev_close) / prev_close) * 100
                    
                    if 10.0 <= day_change_pct <= 18.0:
                        # Filter 6: Trap Check (Below UC)
                        # We need Upper Circuit. Kite Quote usually has 'upper_circuit_limit'.
                        uc_limit = quote.get('upper_circuit_limit', 0)
                        if uc_limit > 0:
                            dist_to_uc = (uc_limit - ltp) / uc_limit
                            if dist_to_uc < 0.015: # Too close to UC (< 1.5%)
                                continue
                        
                        logger.info(f"Found Candidate: {symbol} (+{day_change_pct:.2f}%)")
                        candidates.append(symbol)
                        
            except Exception as e:
                logger.error(f"Error scanning chunk {i}: {e}")
                
        # Final Step: News Guard
        safe_candidates = []
        for sym in candidates:
            if self.check_earnings(sym):
                safe_candidates.append(sym)
            else:
                logger.warning(f"Discarding {sym} due to Earnings/News Risk.")
                
        return safe_candidates

    def check_earnings(self, symbol):
        """
        Uses yfinance to check if earnings are Today or Yesterday.
        Returns True if SAFE, False if RISK.
        """
        try:
            # yfinance expects 'RELIANCE.NS'
            ticker = yf.Ticker(f"{symbol}.NS")
            
            # fetch earnings dates
            # earnings_dates is a dataframe index by date
            dates = ticker.earnings_dates
            if dates is None or dates.empty:
                # If data missing, we assume safe or risky?
                # Midcaps often missing. We'll warn and accept to avoid over-filtering.
                return True
                
            # Get latest/upcoming
            # We want to check if any date matches Today or Yesterday.
            
            today = pd.Timestamp(datetime.now().date())
            yesterday = today - timedelta(days=1)
            
            # Check if index (dates) contains today or yesterday
            # Index is usually timezone aware. Localize logic needed?
            # Let's verify format. usually datetime64[ns].
            
            dates.index = dates.index.tz_localize(None) # Remove tz
            
            has_earnings_today = today in dates.index
            has_earnings_yesterday = yesterday in dates.index
            
            if has_earnings_today or has_earnings_yesterday:
                return False
                
            return True
        except Exception as e:
            logger.warning(f"Earnings check failed for {symbol}: {e}")
            return True # Fail open to avoid blocking trading on API error? Or fail closed?
            # Fail open for now.
