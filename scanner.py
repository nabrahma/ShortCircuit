import pandas as pd
import logging
from fyers_connect import FyersConnect
import time

logger = logging.getLogger(__name__)

class FyersScanner:
    def __init__(self, fyers):
        self.fyers = fyers
        self.symbols = [] # Cache for symbols

    def fetch_nse_symbols(self):
        """
        Downloads NSE Equity Master list and filters for EQ series.
        """
        try:
            # NSE Equity URL provided by Fyers or standard source
            # For robustness, we will try to read a local CSV first or download
            # Using a public list URL for now or if Fyers offers a symbol master API
            # Fyers typically provides a CSV. 
            
            # Fyers CSV URL
            url = "https://public.fyers.in/sym_details/NSE_CM.csv"
            
            # Columns (Official Fyers V3 Spec):
            # 0:Exch, 1:SymbolDesc, 2:SymbolDetails, 3:LotSize, 4:MinTick, 5:ISIN, 6:TradingSession, 7:LastUpdate, 8:Expiry, 9:Symbol, 10:Price, 11:ExchangeToken, 12:TickSize, 13:SymbolRoot
            # Actually, standard layout varies. We will robustly find the '-EQ' symbol and 'MinTick' (Col 4 or 12).
            
            df = pd.read_csv(url, header=None)
            
            candidates = {} # Map Symbol -> TickSize
            
            # Index 9 is usually the Symbol (NSE:SBIN-EQ). Index 4 is MinTick (0.05).
            # Let's verify by iterating.
            
            for index, row in df.iterrows():
                # Finding the Symbol Column (usually col 9 or 13)
                symbol = str(row.get(9, "")) # Try Col 9 first
                if not symbol.endswith("-EQ"):
                    symbol = str(row.get(13, "")) # Try Col 13
                
                if symbol.startswith("NSE:") and symbol.endswith("-EQ"):
                    # Finding Tick Size (Col 4 or 12 or 2)
                    try:
                        tick = float(row.get(4, 0.05)) # Col 4 is often MinTick
                        if tick == 0: tick = 0.05
                    except:
                        tick = 0.05
                        
                    candidates[symbol] = tick

            logger.info(f"Loaded {len(candidates)} Equity Symbols with Tick Sizes.")
            return candidates # Returns Dict {Symbol: Tick}

        except Exception as e:
            logger.error(f"Error fetching NSE symbols: {e}")
            return []

    def check_chart_quality(self, symbol):
        """
        Microstructure Filter: Rejects 'gappy' or 'illiquid' charts.
        Logic: Checks last 60 mins (1min candles).
        - Rejects if > 30% candles have 0 volume.
        - Rejects if > 40% candles are 'Flat' (Open == Close) (Dead movement).
        """
        try:
            # Get 1min history
            import datetime
            to_date = int(time.time())
            from_date = to_date - (60 * 60) # Last 1 Hour
            
            data = {
                "symbol": symbol,
                "resolution": "1",
                "date_format": "0",
                "range_from": str(from_date),
                "range_to": str(to_date),
                "cont_flag": "1"
            }
            
            # Use history API (needs access to history method, assumed available in fyers instance or via direct call)
            # FyersConnect usually has .history(data=data)
            # If not available on self.fyers, we might need a workaround. Assuming it exists.
            response = self.fyers.history(data=data)
            
            # Check Time: If < 10:00 AM, we won't have 30 candles (Market opens 09:15)
            import datetime
            now_dt = datetime.datetime.fromtimestamp(to_date)
            is_early_morning = now_dt.hour < 10
            
            min_candles = 5 if is_early_morning else 30
            
            if 'candles' in response and len(response['candles']) >= min_candles:
                candles = response['candles'] # [[ts, o, h, l, c, v], ...]
                
                total = len(candles)
                zero_vol = 0
                flat_candles = 0 
                
                for c in candles:
                    o, h, l, c_price, v = c[1], c[2], c[3], c[4], c[5]
                    
                    if v == 0:
                        zero_vol += 1
                        
                    if o == c_price and h == l: # Completely dead candle
                        flat_candles += 1
                        
                bad_candle_ratio = (zero_vol + flat_candles) / total
                
                # Threshold: If > 30% are dead/empty, it's garbage.
                if bad_candle_ratio > 0.3:
                    logger.warning(f"⚠️ Quality Reject: {symbol} | Bad Candles: {int(bad_candle_ratio*100)}% (Flat/Zero)")
                    return False
                    
                return True
                
            return False # Not enough data = Reject
            
        except Exception as e:
            logger.error(f"Quality Check Error {symbol}: {e}")
            return False # Fail safe

    def scan_market(self):
        """
        Main Scan Logic.
        1. Get Symbols
        2. Batch Request Quotes (50 at a time)
        3. Filter (Gain 5-20%, Vol > 100k, LTP > 50)
        """
        if not self.symbols:
            self.symbols = self.fetch_nse_symbols()
            if not self.symbols:
                return []

        # Batching (Symbols is now a Dict)
        symbol_list = list(self.symbols.keys()) # EXTRACT KEYS
        filtered_candidates = []
        batch_size = 50
        total_symbols = len(symbol_list)
        
        logger.info(f"Scanning {total_symbols} symbols in batches...")

        for i in range(0, total_symbols, batch_size):
            batch = symbol_list[i:i+batch_size]
            # print(f"Scanning Batch {i}/{total_symbols}...") # Reduced Noise
            symbols_str = ",".join(batch)
            
            try:
                data = {"symbols": symbols_str}
                response = self.fyers.quotes(data=data)
                
                if "d" not in response:
                    continue
                    
                for stock in response["d"]:
                    quote_data = stock.get('v')
                    if not isinstance(quote_data, dict):
                        continue
                        
                    symbol = stock.get('n')
                    ltp = quote_data.get('lp') 
                    volume = quote_data.get('volume')
                    change_p = quote_data.get('chp')
                    
                    if ltp is None or volume is None or change_p is None:
                        continue
                        
                    # 1. Gain: 5% to 20% | 2. Volume > 100k
                    if change_p >= 5.0 and volume > 100000:
                            if ltp > 5:
                                if self.check_chart_quality(symbol):
                                    tick_size = self.symbols.get(symbol, 0.05) # GET TICK
                                    
                                    logger.info(f"🔥 CANDIDATE: {symbol} | Gain: {change_p}% | Tick: {tick_size}")
                                    filtered_candidates.append({
                                        'symbol': symbol,
                                        'ltp': ltp,
                                        'volume': volume,
                                        'change': change_p,
                                        'tick_size': tick_size # PASS TICK SIZE
                                    })
                                else:
                                    logger.info(f"🗑️ Skipped {symbol} (Poor Microstructure)")
            except Exception as e:
                logger.error(f"Batch Error: {e}")
                
        # Sort by Change % Descending
        filtered_candidates.sort(key=lambda x: x['change'], reverse=True)
        top_gainers = filtered_candidates[:20]
        
        logger.info(f"Scan Complete. Found {len(filtered_candidates)} candidates.")
        
        # Notify Telegram
        # Notify Telegram (DISABLED per user request to reduce noise)
        # try:
        #     import telebot
        #     import config
            
        #     if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        #         bot = telebot.TeleBot(config.TELEGRAM_BOT_TOKEN)
                
        #         msg = "🚀 *Market Status: Top Gainers (EOD)* 🚀\n\n"
        #         for stock in top_gainers:
        #             symbol_clean = stock['symbol'].replace('NSE:', '').replace('-EQ', '')
        #             msg += f"• *{symbol_clean}*: {stock['change']}% (Vol: {stock['volume'] :,})\n"
                
        #         if not top_gainers:
        #             msg += "No significant gainers found."
                    
        #         # bot.send_message(config.TELEGRAM_CHAT_ID, msg, parse_mode="Markdown")
        #         # print("✅ Telegram Notification Sent!")
        #     else:
        #         print("⚠️ Telegram credentials missing in config.")
                
        # except Exception as e:
        #     print(f"❌ Telegram Error: {e}")
            
        return top_gainers

if __name__ == "__main__":
    # Test Scanner
    try:
        fyers_obj = FyersConnect().authenticate()
        scanner = FyersScanner(fyers_obj)
        candidates = scanner.scan_market()
        print("Candidates:", candidates)
    except Exception as e:
        print(e)
