import pandas as pd
import logging
from fyers_connect import FyersConnect
import time

logger = logging.getLogger(__name__)

class FyersScanner:
    def __init__(self, fyers, broker=None):
        self.fyers = fyers
        self.broker = broker
        self.symbols = {} # Cache for symbols
        # NOTE: Do NOT use TYPO_PATCHES to suppress symbols.
        # Zero-volume symbols are handled by quality_reject_counts blacklist.
        # Both NSE:AKASH-EQ and NSE:AAKASH-EQ are separate listed entities.
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

    def _fetch_nse_symbols_sync(self):
        """
        Synchronous version of fetch_nse_symbols for Phase 44.7 startup.
        Uses requests to avoid asyncio loop locking in run_in_executor.
        """
        import requests
        try:
            url = "https://public.fyers.in/sym_details/NSE_CM.csv"
            response = requests.get(url, timeout=10)
            
            candidates = []
            if response.status_code == 200:
                lines = response.text.splitlines()
                for line in lines:
                    cols = line.split(',')
                    if len(cols) > 9:
                        sym = cols[9].strip()
                        if not sym.endswith('-EQ') and len(cols) > 13:
                            sym = cols[13].strip()
                        if sym.startswith('NSE:') and sym.endswith('-EQ'):
                            candidates.append(sym)
            
            logger.info(f"Loaded {len(candidates)} NSE EQ symbols synchronously.")
            return candidates
        except Exception as e:
            logger.error(f"Error fetching NSE symbols sync: {e}")
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
            import config
            now_dt = datetime.datetime.fromtimestamp(to_date)
            is_early_morning = now_dt.hour < 10
            
            # RVOL validity gate — replaces is_early_morning heuristic
            # RVOL calculation requires minimum 20 candles (iloc[-20:-2])
            # Market opens 9:15 AM IST → earliest valid signal: 9:35 AM
            _mins_open = config.minutes_since_market_open()
            _rvol_valid = _mins_open >= config.RVOL_MIN_CANDLES  # 20 minutes from open
            
            if config.RVOL_VALIDITY_GATE_ENABLED:
                if not _rvol_valid:
                    logger.warning(f"SKIP {symbol} — RVOL_VALIDITY_GATE: {_mins_open:.1f} min since open — need {config.RVOL_MIN_CANDLES} min for valid RVOL. Skip.")
                    self.quality_reject_counts[symbol] = self.quality_reject_counts.get(symbol, 0) + 1
                    return False, None
                # Candle count: keep as absolute floor for API data integrity only
                min_candles = 10  # No longer varies by time — cliff-edge removed
            else:
                # Rollback to pre-PRD heuristic behavior
                min_candles = 5 if is_early_morning else 10
            
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
        
        # ── PRD-007: Tiered Data Provider ─────────────────────────
        import time as _time
        scan_start_ms = _time.monotonic() * 1000

        # Increment scan counter (module-level for log correlation)
        if not hasattr(self, '_scan_counter'):
            self._scan_counter = 0
        self._scan_counter += 1
        scan_id = self._scan_counter

        data_tier = "REST_EMERGENCY"   # Will be overridden below

        if self.broker and hasattr(self.broker, 'is_cache_ready') and self.broker.is_cache_ready():
            # ── Tier 1: Full WS Cache ────────────────────────────────
            snapshot = self.broker.get_quote_cache_snapshot()
            fresh = {}
            stale_symbols = []

            for symbol in symbol_list:
                quote = snapshot.get(symbol)
                if quote is None:
                    stale_symbols.append(symbol)
                    continue
                age_s = _time.time() - quote.get('ts', 0)
                if age_s > 60:
                    stale_symbols.append(symbol)
                else:
                    fresh[symbol] = quote

            fresh_pct = len(fresh) / max(len(symbol_list), 1)

            if fresh_pct >= 0.85:
                # Pure Tier 1
                data_tier = "WS_CACHE"
                all_quotes = fresh

            elif fresh_pct >= 0.50:
                # Tier 2: HYBRID — supplement only stale/missing symbols via REST
                data_tier = "HYBRID"
                logger.warning(
                    f"[WS Cache] Tier 2 HYBRID: {len(stale_symbols)} symbols stale/missing "
                    f"(WS fresh: {len(fresh)}/{len(symbol_list)}). Supplementing via REST."
                )
                all_quotes = dict(fresh)
                # REST supplement for stale symbols only
                batch_size = 50
                for i in range(0, len(stale_symbols), batch_size):
                    batch = stale_symbols[i:i + batch_size]
                    try:
                        data = {"symbols": ",".join(batch)}
                        response = self.fyers.quotes(data=data)
                        if "d" in response:
                            for stock in response["d"]:
                                quote_data = stock.get('v')
                                if not isinstance(quote_data, dict):
                                    continue
                                sym = stock.get('n')
                                ltp = quote_data.get('lp', 0)
                                volume = quote_data.get('volume', 0)
                                chp = quote_data.get('chp', 0)
                                if sym:
                                    all_quotes[sym] = {
                                        'ltp': ltp, 'volume': volume,
                                        'ch_oc': chp, 'oi': quote_data.get('oi', 0),
                                        'ts': _time.time(),
                                    }
                    except Exception as e:
                        logger.error(f"[Tier 2] REST supplement batch error: {e}")

            else:
                # Tier 3: REST EMERGENCY — cache too degraded
                data_tier = "REST_EMERGENCY"
                snap = self.broker.cache_health_snapshot()
                logger.critical(
                    f"[WS Cache] TIER 3 REST EMERGENCY: WS cache only {fresh_pct:.1%} fresh "
                    f"({snap.get('fresh')}/{snap.get('total')}). Full REST fallback. INVESTIGATE IMMEDIATELY."
                )
                if hasattr(self, '_bot_alert_fn') and self._bot_alert_fn:
                    try:
                        self._bot_alert_fn(
                            f"⚠️ WS CACHE FAILURE\nFresh: {snap.get('fresh')}/{snap.get('total')} ({fresh_pct:.1%})\n"
                            "Falling back to full REST. Signals degraded."
                        )
                    except Exception:
                        pass
                all_quotes = {}  # Will REST-fill below

            # Build pre_candidates from all_quotes (Tier 1 and 2)
            if data_tier in ("WS_CACHE", "HYBRID"):
                for symbol, quote in all_quotes.items():
                    ltp    = quote.get('ltp', 0)
                    volume = quote.get('volume', 0)
                    gain   = abs(quote.get('ch_oc', 0))
                    oi     = quote.get('oi', 0)

                    if gain >= 6.0 and gain <= 18.0 and volume >= 100_000 and ltp >= 5:
                        if self.quality_reject_counts.get(symbol, 0) >= 3:
                            logger.debug(f"BLACKLIST {symbol} — Quality rejected 3x today, skipping.")
                            continue
                        tick_size = self.symbols.get(symbol, 0.05)
                        pre_candidates.append({
                            'symbol': symbol, 'ltp': ltp,
                            'volume': volume, 'change': gain,
                            'tick_size': tick_size, 'oi': oi,
                        })

            # Elapsed for tier 1/2
            if data_tier in ("WS_CACHE", "HYBRID"):
                tier_ms = int((_time.monotonic() * 1000) - scan_start_ms)
                logger.info(
                    f"SCAN #{scan_id} | Tier: {data_tier} | "
                    f"Cache: {len(fresh)}/{len(symbol_list)} fresh | "
                    f"Scan_ms: {tier_ms} | Pre-candidates: {len(pre_candidates)}"
                )

        if data_tier == "REST_EMERGENCY" or not (self.broker and hasattr(self.broker, 'is_cache_ready')):
            # ── Tier 3 / No-broker fallback: original REST batch path ────
            if self.broker:
                # Already logged CRITICAL above; just do the REST scan
                pass
            else:
                logger.warning("[WS Cache] No broker configured — using REST batch scan")
            data_tier = "REST_EMERGENCY"

            batch_size = 50
            total_symbols = len(symbol_list)
            logger.info(f"Scanning {total_symbols} symbols in batches via REST...")

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

                        if 6.0 <= change_p <= 18.0 and volume > 100000 and ltp > 5:

                            if self.quality_reject_counts.get(symbol, 0) >= 3:
                                logger.debug(f"BLACKLIST {symbol} — Quality rejected 3x today, skipping history fetch.")
                                continue

                            tick_size = self.symbols.get(symbol, 0.05)
                            oi = quote_data.get('oi', 0)
                            pre_candidates.append({
                                'symbol': symbol, 'ltp': ltp,
                                'volume': volume, 'change': change_p,
                                'tick_size': tick_size, 'oi': oi,
                            })
                except Exception as e:
                    logger.error(f"Batch Error: {e}")

            tier_ms = int((_time.monotonic() * 1000) - scan_start_ms)
            logger.info(
                f"SCAN #{scan_id} | Tier: REST_EMERGENCY | Cache: FAILED | "
                f"Scan_ms: {tier_ms} | Pre-candidates: {len(pre_candidates)}"
            )

        # PRD-008: Store tier for main.py gate audit trail correlation
        self._last_data_tier = data_tier

        if not pre_candidates:
            logger.info("No pre-candidates passed filter.")
            return []

        # ── PHASE 44.4: ETF CLUSTER DEDUPLICATION (Section 7) ──────
        # Silver ETFs (and future: GOLD, NIFTY) often fire simultaneously.
        # Keep highest-volume member per cluster, suppress duplicates.
        if getattr(config, 'ETF_CLUSTER_DEDUP_ENABLED', False):
            cluster_keywords = getattr(config, 'ETF_CLUSTER_KEYWORDS', [])
            for keyword in cluster_keywords:
                keyword_upper = keyword.upper()
                cluster = [c for c in pre_candidates if keyword_upper in c['symbol'].upper()]
                if len(cluster) > 1:
                    # Sort by volume descending, keep the top one
                    cluster.sort(key=lambda x: x['volume'], reverse=True)
                    keeper = cluster[0]
                    suppressed = cluster[1:]
                    suppressed_syms = [c['symbol'] for c in suppressed]
                    
                    # Remove suppressed from pre_candidates
                    pre_candidates = [c for c in pre_candidates if c not in suppressed]
                    
                    logger.info(
                        f"[DEDUP] {keyword} cluster: kept {keeper['symbol']} "
                        f"(vol={keeper['volume']:,}), suppressed {len(suppressed)}: "
                        f"{', '.join(suppressed_syms)}"
                    )

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
