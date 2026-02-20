import pandas as pd
import logging
from fyers_connect import FyersConnect
import time

logger = logging.getLogger(__name__)

class FyersScanner:
    def __init__(self, fyers):
        self.fyers = fyers
        self.symbols = [] # Cache for symbols
        self.quality_reject_counts = {} # Phase 42.4: Track 0-volume rejects

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
                
                # Phase 42.4 Fix #6: Manual Patch for Fyers Master List Typos
                if symbol == "NSE:AKASH-EQ":
                    symbol = "NSE:AAKASH-EQ"
                
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
        - Rejects if > 50% candles have 0 volume (truly illiquid).
        - Rejects if > 50% candles are 'Doji' (body < 0.1% of price).
        - PASSES if insufficient data (don't reject liquid stocks due to API lag).
        """
        try:
            # Get 1min history - Last 30 minutes (not 60) to avoid stale consolidation
            import datetime
            to_date = int(time.time())
            from_date = to_date - (30 * 60)  # Last 30 Minutes (changed from 60)
            
            data = {
                "symbol": symbol,
                "resolution": "1",
                "date_format": "0",
                "range_from": str(from_date),
                "range_to": str(to_date),
               "cont_flag": "1"
            }
            
            response = self.fyers.history(data=data)
            
            # Check Time: If < 10:00 AM, we won't have many candles (Market opens 09:15)
            import datetime
            now_dt = datetime.datetime.fromtimestamp(to_date)
            is_early_morning = now_dt.hour < 10
            
            min_candles = 5 if is_early_morning else 10  # Reduced from 15 (30min has ~10-30 candles)
            
            if 'candles' in response and len(response['candles']) >= min_candles:
                candles = response['candles'] # [[ts, o, h, l, c, v], ...]
                
                total = len(candles)
                zero_vol = 0
                
                for c in candles:
                    v = c[5]  # volume
                    
                    if v == 0:
                        zero_vol += 1
                        
                zero_vol_ratio = zero_vol / total
                
                # Threshold: If > 50% have zero volume, it's illiquid/choppy
                if zero_vol_ratio > 0.5:
                    reject_pct = int(zero_vol_ratio*100)
                    logger.warning(f"[SKIP] Quality Reject: {symbol} | Zero Volume: {reject_pct}%")
                    self.quality_reject_counts[symbol] = self.quality_reject_counts.get(symbol, 0) + 1
                    return False, None
                    
                # Return Success AND the Dataframe (Reuse Strategy)
                cols = ["epoch", "open", "high", "low", "close", "volume"]
                df = pd.DataFrame(response["candles"], columns=cols)
                df['datetime'] = pd.to_datetime(df['epoch'], unit='s').dt.tz_localize('UTC').dt.tz_convert('Asia/Kolkata')
                return True, df
            
            # Fix #4: Hard block 0-candle data instead of allowing
            candle_count = len(response.get('candles', []))
            logger.warning(f"SKIP {symbol} — Insufficient candle data ({candle_count}). Blocking.")
            self.quality_reject_counts[symbol] = self.quality_reject_counts.get(symbol, 0) + 1
            return False, None
            
        except Exception as e:
            logger.error(f"Quality Check Error {symbol}: {e}")
            return True, None  # PASS on error (fail-open)

    def scan_market(self):
        """
        Main Scan Logic (Phase 41.1 — Parallel fetch).
        1. Get Symbols
        2. Batch Request Quotes (50 at a time)
        3. Filter (Gain 6-18%, Vol > 100k, LTP > 5)
        4. Parallel fetch history + quality check for all candidates
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import config

        if not self.symbols:
            self.symbols = self.fetch_nse_symbols()
            if not self.symbols:
                return []

        # Batching (Symbols is now a Dict)
        symbol_list = list(self.symbols.keys()) # EXTRACT KEYS
        pre_candidates = []  # Pass gain/volume/price filter, pending quality
        batch_size = 50
        total_symbols = len(symbol_list)
        
        logger.info(f"Scanning {total_symbols} symbols in batches...")

        # Phase A: Batch quote scan (serial — cheap API calls)
        for i in range(0, total_symbols, batch_size):
            batch = symbol_list[i:i+batch_size]
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
                        
                    # Filter: Gain 6-18%, Vol > 100k, LTP > 5
                    if 6.0 <= change_p <= 18.0 and volume > 100000 and ltp > 5:
                        
                        # Fix #5: Blacklist Check
                        if self.quality_reject_counts.get(symbol, 0) >= 3:
                            logger.debug(f"BLACKLIST {symbol} — Quality rejected 3x today, skipping history fetch.")
                            continue
                            
                        tick_size = self.symbols.get(symbol, 0.05)
                        oi = quote_data.get('oi', 0)
                        pre_candidates.append({
                            'symbol': symbol,
                            'ltp': ltp,
                            'volume': volume,
                            'change': change_p,
                            'tick_size': tick_size,
                            'oi': oi,
                        })
            except Exception as e:
                logger.error(f"Batch Error: {e}")

        if not pre_candidates:
            logger.info("No pre-candidates passed filter.")
            return []

        logger.info(f"Pre-filter: {len(pre_candidates)} candidates. Starting parallel quality check...")

        # Phase B: Parallel history + quality check
        filtered_candidates = []
        max_workers = getattr(config, 'SCANNER_PARALLEL_WORKERS', 10)

        def fetch_quality(candidate):
            """Fetch history + quality for a single candidate."""
            return self.check_chart_quality(candidate['symbol'])

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(fetch_quality, c): c for c in pre_candidates}

            for future in as_completed(futures):
                candidate = futures[future]
                try:
                    is_good, history_df = future.result(timeout=5)
                    if is_good:
                        candidate['history_df'] = history_df
                        logger.info(
                            f"[CANDIDATE] {candidate['symbol']} | "
                            f"Gain: {candidate['change']}% | "
                            f"Tick: {candidate['tick_size']} | OI: {candidate['oi']}"
                        )
                        filtered_candidates.append(candidate)
                    else:
                        logger.info(f"[SKIP] {candidate['symbol']} (Poor Microstructure)")
                except Exception as e:
                    logger.error(f"Quality check failed for {candidate['symbol']}: {e}")

        # Sort by Change % Descending
        filtered_candidates.sort(key=lambda x: x['change'], reverse=True)
        top_gainers = filtered_candidates[:20]
        
        logger.info(f"Scan Complete. Found {len(filtered_candidates)} candidates.")
        


            
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
