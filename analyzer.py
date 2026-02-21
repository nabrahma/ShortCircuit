import pandas as pd
import logging
import datetime
import csv
import os
import config
from typing import Optional, Dict, Any, Tuple
from collections import deque

from market_context import MarketContext
from signal_manager import get_signal_manager
from htf_confluence import HTFConfluence
from god_mode_logic import GodModeAnalyst
from tape_reader import TapeReader
from market_profile import ProfileAnalyzer
from ml_logger import get_ml_logger

logger = logging.getLogger(__name__)

SIGNAL_LOG_FILE = "logs/signals.csv"

def log_signal(symbol: str, ltp: float, pattern: str, stop_loss: float,
               meta: str = "", setup_high: float = 0.0,
               tick_size: float = 0.05, atr: float = 0.0):
    """
    Persists signal details to a CSV file for EOD analysis.
    Phase 41.2: Extended with setup_high, tick_size, atr for simulation.
    """
    os.makedirs(os.path.dirname(SIGNAL_LOG_FILE), exist_ok=True)
    file_exists = os.path.exists(SIGNAL_LOG_FILE)
    
    with open(SIGNAL_LOG_FILE, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "symbol", "ltp", "pattern",
                             "stop_loss", "meta", "setup_high", "tick_size", "atr"])
        
        writer.writerow([
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            symbol,
            ltp,
            pattern,
            stop_loss,
            meta,
            setup_high,
            tick_size,
            atr
        ])

class FyersAnalyzer:
    """
    Core analysis engine for the ShortCircuit strategy.
    Orchestrates technical analysis, pattern recognition, and risk checks.
    """
    
    def __init__(self, fyers, morning_high=None, morning_low=None):
        self.fyers = fyers
        self.market_context = MarketContext(fyers, morning_high, morning_low)
        self.signal_manager = get_signal_manager()
        self.htf_confluence = HTFConfluence(fyers)
        self.gm_analyst = GodModeAnalyst()
        self.tape_reader = TapeReader()
        self.profile_analyzer = ProfileAnalyzer()
        self.oi_history = {} # Symbol -> deque of (timestamp, oi, price) use deque(maxlen=20)

    def get_history(self, symbol: str, interval: str = "1") -> Optional[pd.DataFrame]:
        """
        Fetch intraday historical data for a symbol.
        """
        today = datetime.date.today().strftime("%Y-%m-%d")
        data = {
            "symbol": symbol,
            "resolution": interval,
            "date_format": "1",
            "range_from": today,
            "range_to": today,
            "cont_flag": "1"
        }

        try:
            response = self.fyers.history(data=data)
            if "candles" in response and response["candles"]:
                cols = ["epoch", "open", "high", "low", "close", "volume"]
                df = pd.DataFrame(response["candles"], columns=cols)
                df['datetime'] = pd.to_datetime(df['epoch'], unit='s').dt.tz_localize('UTC').dt.tz_convert('Asia/Kolkata')
                return df
            else:
                logger.warning(f"No history data for {symbol}")
                return None
        except Exception as e:
            logger.error(f"Error fetching history for {symbol}: {e}")
            return None

    def check_setup(self, symbol: str, ltp: float, oi: float = 0, pre_fetched_df: Optional[pd.DataFrame] = None) -> Optional[Dict[str, Any]]:
        """
        Validates a trading candidate using the God Mode strategy.
        Optimized to use pre-fetched History DF and Shared Depth Data.
        """

        
        # 1. Pre-analysis Filters
        if not self._check_filters(symbol):

            return None
            
        # 2. Data Fetching
        if pre_fetched_df is not None:
             df = pre_fetched_df
             # Ensure we're working with a copy to avoid side effects
             df = df.copy()
        else:
             df = self.get_history(symbol)
             
        if df is None or df.empty:

            return None
            
        # Hard guard (Change 3): if df has fewer than RVOL_MIN_CANDLES rows, RVOL is unreliable.
        # This fixes a confirmed false-positive bug where avg_vol=0 -> rvol=0, falsely passing as exhaustion vacuum.
        if config.RVOL_VALIDITY_GATE_ENABLED and len(df) < config.RVOL_MIN_CANDLES:
            logger.warning(f"SKIP {symbol} — RVOL_VALIDITY_GATE: Only {len(df)} candles — need {config.RVOL_MIN_CANDLES}")
            return None
            
        # Define prev_df (used for structural analysis components that need history context)
        # Note: df includes the current candle 'i'. prev_df is history up to 'i-1'.
        prev_df = df.iloc[:-1]

        # 3. Technical Calculations & Context
        self._enrich_dataframe(df)
        
        day_high = df['high'].max()
        open_price = df.iloc[0]['open']
        gain_pct = ((ltp - open_price) / open_price) * 100
        
        # 4. Hard Constraints (The "Ethos" Check)
        # LOGIC FIX (Phase 38): Pass open_price to valid Max Day Gain
        ok, msg = self.gm_analyst.check_constraints(ltp, day_high, gain_pct, open_price)
        
        if not ok:
            return None
        


        # 5. Circuit Guard (Requires Depth)
        # We fetch Depth ONCE here and share it with Circuit Guard and Tape Reader
        depth_data = None
        try:
            full_depth = self.fyers.depth(data={"symbol": symbol, "ohlcv_flag":"1"})
            # Validate response
            if 'd' in full_depth and symbol in full_depth['d']:
                depth_data = full_depth['d'][symbol]
        except:
            pass
            
        if self._check_circuit_guard(symbol, ltp, depth_data):
            return None

        # 6. Momentum Safeguard (Train Filter)
        slope, _ = self.gm_analyst.calculate_vwap_slope(df.iloc[-30:])
        if self._is_momentum_too_strong(df, slope, symbol):
             return None

        # 7. Pattern Recognition
        struct, z_score = self.gm_analyst.detect_structure_advanced(df)
        
        # Tape Analysis (Stall Detection only, Absorption disabled)
        is_stalled, _ = self.tape_reader.detect_stall(df)
        
        if len(df) > 1:
            prev_candle = df.iloc[-2]
            is_sniper_zone = self._check_sniper_zone(df)
        else:
            prev_candle = df.iloc[0] # Fallback
            is_sniper_zone = False
        
        valid_signal = False
        pattern_desc = ""
        
        # Check for Price Breakdown
        current_ltp = df.iloc[-1]['close']
        setup_low = prev_candle['low']
        breakdown = current_ltp < setup_low

        if breakdown:
            vwap_sd = self.gm_analyst.calculate_vwap_bands(prev_df)
            is_extended = vwap_sd > 2.0
            
            # Structural Patterns
            if struct in ["SHOOTING_STAR", "BEARISH_ENGULFING", "EVENING_STAR", "MOMENTUM_BREAKDOWN", "VOLUME_TRAP"]:
                valid_signal = True
                pattern_desc = struct
            
            # Sniper Doji
            elif struct == "ABSORPTION_DOJI" and is_sniper_zone:
                valid_signal = True
                pattern_desc = struct
            # Tape Stall (Drift) - Requires Extension
            elif is_stalled and is_extended:
                valid_signal = True
                pattern_desc = "TAPESTALL (Drift)"

            if valid_signal:
                # SPECIAL CASE: Momentum/Trap patterns don't need "Pro Confluence" if Volume is already high
                # But let's check basic confluence anyway
                valid_signal, pro_conf_msgs = self._check_pro_confluence(
                    symbol, df, prev_df, slope, is_extended, vwap_sd, pattern_desc, depth_data, ltp, oi
                )
                if pro_conf_msgs:
                    pattern_desc += f" + {', '.join(pro_conf_msgs)}"

        if valid_signal:
            return self._finalize_signal(symbol, ltp, df, pattern_desc, slope, "")
            
        return None

    def _check_filters(self, symbol: str) -> bool:
        """Runs pre-analysis checks: Market Regime."""
        # 1. Market Regime
        allow_short, reason = self.market_context.should_allow_short()
        if not allow_short:
            logger.info(f"BLOCKED by Market Regime: {symbol} - {reason}")
            return False
        
        # Time filter REMOVED per user request (stocks may hover, then move later)
        return True

    def _enrich_dataframe(self, df: pd.DataFrame):
        """Calculates VWAP and other indicators in-place."""
        v = df['volume'].values
        tp = (df['high'] + df['low'] + df['close']) / 3
        df['vwap'] = (tp * v).cumsum() / v.cumsum()

    def _check_circuit_guard(self, symbol: str, ltp: float, quote: dict = None) -> bool:
        """
        Safety Check: Blocks trade if price is too close to Upper Circuit.
        Uses cached quote/depth data if available.
        """
        try:
            if not quote:
                 # Fallback to API call if not provided (should be avoided)
                 depth_data = self.fyers.depth(data={"symbol": symbol, "ohlcv_flag":"1"})
                 if 'd' in depth_data:
                     quote = depth_data['d'].get(symbol, {})
            
            if quote:
                uc = quote.get('upper_ckt', 0)
                lc = quote.get('lower_ckt', 0)
                
                if uc > 0:
                    buffer_price = uc * 0.985
                    if ltp >= buffer_price:
                        logger.warning(f"🛑 CIRCUIT GUARD: {symbol} @ {ltp} (Too close to UC {uc})")
                        return True
                        
                # Also Block if at Lower Circuit (Already dead)
                if lc > 0 and ltp <= lc * 1.005:
                     return True
                     
        except Exception as e:
            logger.error(f"Circuit Check Error: {e}")
            
        return False

    def _is_momentum_too_strong(self, df: pd.DataFrame, slope: float, symbol: str) -> bool:
        """Checks if momentum is too strong to short."""
        try:
            recent_vols = df['volume'].iloc[-20:-1]
            avg_v = recent_vols.mean()
            curr_v = df['volume'].iloc[-1]
            rvol_now = curr_v / avg_v if avg_v > 0 else 0
            
            if rvol_now > 5.0 and slope > 40:
                logger.warning(f"🛑 TRAIN FILTER: {symbol} blocked (RVOL {rvol_now:.1f}, Slope {slope:.1f})")
                return True
        except Exception:
            pass
        return False

    def _check_sniper_zone(self, df: pd.DataFrame) -> bool:
        """Checks if price is at the top of the micro-range."""
        last_5 = df.iloc[-6:-1]
        micro_high = last_5['high'].max()
        micro_low = last_5['low'].min()
        denom = (micro_high - micro_low) if (micro_high - micro_low) > 0 else 0.001
        prev_close = df.iloc[-2]['close']
        range_pos = (prev_close - micro_low) / denom
        return range_pos > 0.70

    def _check_pro_confluence(self, symbol, df, prev_df, slope, is_extended, vwap_sd, pattern_desc, depth_data=None, ltp=0, oi=0) -> Tuple[bool, list]:
        """Verifies secondary confirmation signals."""
        pro_conf = []
        
        # Profile Rejection
        is_bearish_profile, _ = self.profile_analyzer.check_profile_rejection(df, df.iloc[-1]['close'])
        if is_bearish_profile: pro_conf.append("Profile Rejection")
        
        # Tape Wall (DOM)
        try:
            # Use cached depth data if available
            if depth_data:
                _, d_msg = self.tape_reader.analyze_depth(depth_data)
                if "Wall" in d_msg:
                    pro_conf.append(f"Dom: {d_msg}")
            else:
                 # Fallback (Should be rare)
                 dp = self.fyers.depth(data={"symbol": symbol, "ohlcv_flag":"1"})
                 if 'd' in dp:
                     _, d_msg = self.tape_reader.analyze_depth(dp['d'][symbol])
                     if "Wall" in d_msg:
                        pro_conf.append(f"Dom: {d_msg}")
        except Exception:
            pass

        # Technicals
        if slope < 5: pro_conf.append("VWAP Flat")
        if self.gm_analyst.check_rsi_divergence(prev_df): pro_conf.append("RSI Div [DOWN]")
        if is_extended: pro_conf.append(f"VWAP +{vwap_sd:.1f}SD [EXT]")

        # Fibonacci
        fibs = self.gm_analyst.calculate_fib_levels(prev_df)
        if fibs:
            setup_high = df.iloc[-2]['high']
            for name, level in fibs.items():
                if name == 'trend': continue
                if abs(setup_high - level) <= (level * 0.001):
                    if fibs.get('trend') == 'DOWN' and df.iloc[-2]['close'] < level:
                        pro_conf.append(f"{name} Reject [FIB]")
                        break
        
        # RVOL & Vacuum
        try:
            if len(df) > 20:
                avg_vol = df['volume'].iloc[-20:-2].mean()
                setup_vol = df.iloc[-2]['volume']
                rvol = setup_vol / avg_vol if avg_vol > 0 else 0
                
                if rvol > 2.0:
                    pro_conf.append(f"RVOL {rvol:.1f}x [VOL]")
                elif rvol < 0.5 and is_extended:
                    pro_conf.append(f"Vacuum/Exhaustion [EXHT]") # Phase 27
        except Exception:
            pass
            
        # Phase 27: Institutional Checks
        # 1. OI Divergence (Fakeout)
        self._track_oi(symbol, ltp, oi)
        is_fakeout, oi_msg = self._check_oi_divergence(symbol, ltp)
        if is_fakeout: pro_conf.append(oi_msg)
        
        # 2. dPOC Divergence (Value Migration)
        is_dpoc_div, dpoc_msg = self._check_dpoc_divergence(symbol, ltp, df)
        if is_dpoc_div: pro_conf.append(dpoc_msg)
        
        # ===== ORDERFLOW PRINCIPLES =====
        try:
            # #9: Round Number
            is_round, round_msg = self.tape_reader.check_round_number(ltp)
            if is_round: pro_conf.append(f"Round: {round_msg}")
            
            # #3: Large Wick (potential fill)
            is_wick, wick_msg = self.tape_reader.detect_large_wick(df)
            if is_wick: pro_conf.append(f"Wick: {wick_msg}")
            
            # #2: Bad High (good for shorts)
            is_bad_high, bh_msg = self.tape_reader.detect_bad_high(df, depth_data)
            if is_bad_high: pro_conf.append(f"[OF] {bh_msg}")
            
            # #1: Bad Low (AVOID shorting)
            is_bad_low, bl_msg = self.tape_reader.detect_bad_low(df, depth_data)
            if is_bad_low:
                logger.info(f"BLOCKED by Orderflow: {symbol} - {bl_msg}")
                return False, []  # Block the trade!
            
            # #5: Trapped Positions
            is_trapped, trap_msg = self.tape_reader.detect_trapped_positions(df)
            if is_trapped: pro_conf.append(f"[OF] {trap_msg}")
            
            # #10: Aggression without Progress
            is_absorb, absorb_msg = self.tape_reader.detect_aggression_no_progress(df)
            if is_absorb: pro_conf.append(f"[OF] {absorb_msg}")
        except Exception as e:
            logger.warning(f"Orderflow check error: {e}")

        # Validation Logic logic
        if not is_extended and "TAPE" not in pattern_desc:
             # Basic patterns must be extended or have confluence
             if not pro_conf:
                 logger.info(f"Refused {symbol}: Valid Structure but No Pro Confirmation.")
                 return False, []
        
        return True, pro_conf

    def _track_oi(self, symbol, ltp, oi):
        """Phase 27: Stores recent OI data."""
        if oi == 0: return
        
        if symbol not in self.oi_history:
            self.oi_history[symbol] = deque(maxlen=20) # Store last 20 scans (~20 mins)
            
        self.oi_history[symbol].append({
            'time': datetime.datetime.now(),
            'price': ltp,
            'oi': oi
        })

    def _check_oi_divergence(self, symbol, ltp):
        """
        Phase 27: Checks for Short Covering (Fakeout).
        Prices UP + OI DOWN = Fakeout.
        """
        if symbol not in self.oi_history or len(self.oi_history[symbol]) < 5:
            return False, ""
            
        history = list(self.oi_history[symbol])
        current = history[-1]
        past = history[0] # ~15-20 mins ago (depending on scan interval)
        
        price_change = current['price'] - past['price']
        oi_change = current['oi'] - past['oi']
        
        # Logic: Price Risng (Extension) but OI Dropping
        if price_change > 0 and oi_change < 0:
            drop_pct = (abs(oi_change) / past['oi']) * 100
            if drop_pct > 0.5: # Significant drop
                return True, f"OI Fakeout (OI -{drop_pct:.1f}%) 📉"
                
        return False, ""

    def _check_dpoc_divergence(self, symbol, ltp, df):
        """
        Phase 27: Checks if Developing POC is stuck low while Price is high.
        """
        try:
            dpoc = self.profile_analyzer.get_developing_poc(df)
            if dpoc == 0: return False, ""
            
            # If Price is significantly above dPOC (e.g. > 1%) 
            # and we are at Day Highs, but dPOC hasn't migrated.
            # This is hard to prove without historical dPOC, but if Price >> dPOC it implies thin value.
            
            dist_pct = (ltp - dpoc) / dpoc * 100
            if dist_pct > 1.0: # 1% Extension from Value
                return True, f"Value Div (LTP > POC+{dist_pct:.1f}%)"
                
        except:
            pass
        return False, ""

    # ------------------------------------------------------------------
    # Phase 41: Multi-Edge Analyzer Entry Point
    # ------------------------------------------------------------------
    def check_setup_with_edges(
        self, symbol: str, ltp: float, oi: float,
        pre_fetched_df, edge_payload: dict
    ):
        """
        Called when MULTI_EDGE_ENABLED is True.
        Runs Gates 1-7 and 9-12 (skips Gate 8 pattern detection since
        edges are already identified by MultiEdgeDetector).
        """
        # Gate 1-2: Pre-analysis filters (Signal Manager + Market Regime)
        if not self._check_filters(symbol):
            return None

        # Gate 3: Data
        if pre_fetched_df is not None:
            df = pre_fetched_df.copy()
        else:
            df = self.get_history(symbol)
        if df is None or df.empty:
            return None

        # Hard guard (Change 3): if df has fewer than RVOL_MIN_CANDLES rows, RVOL is unreliable.
        # This fixes a confirmed false-positive bug where avg_vol=0 -> rvol=0, falsely passing as exhaustion vacuum.
        if config.RVOL_VALIDITY_GATE_ENABLED and len(df) < config.RVOL_MIN_CANDLES:
            logger.warning(f"SKIP {symbol} — RVOL_VALIDITY_GATE: Only {len(df)} candles — need {config.RVOL_MIN_CANDLES}")
            return None

        prev_df = df.iloc[:-1]

        # Gate 4: Context enrichment
        self._enrich_dataframe(df)
        day_high = df['high'].max()
        open_price = df.iloc[0]['open']
        gain_pct = ((ltp - open_price) / open_price) * 100

        # Gate 5: Hard constraints
        ok, msg = self.gm_analyst.check_constraints(ltp, day_high, gain_pct, open_price)
        if not ok:
            return None

        # Gate 6: Circuit Guard
        depth_data = None
        try:
            full_depth = self.fyers.depth(data={"symbol": symbol, "ohlcv_flag": "1"})
            if 'd' in full_depth and symbol in full_depth['d']:
                depth_data = full_depth['d'][symbol]
        except Exception:
            pass

        if self._check_circuit_guard(symbol, ltp, depth_data):
            return None

        # Gate 7: Momentum Safeguard
        slope, _ = self.gm_analyst.calculate_vwap_slope(df.iloc[-30:])
        if self._is_momentum_too_strong(df, slope, symbol):
            return None

        # SKIP Gate 8 — edges already detected by MultiEdgeDetector

        # Gate 9: Breakdown confirmation (edge-specific entry trigger)
        current_ltp = df.iloc[-1]['close']
        if current_ltp >= edge_payload['entry_trigger']:
            return None  # Not yet broken down

        # Gate 10: Pro Confluence (same logic)
        vwap_sd = self.gm_analyst.calculate_vwap_bands(prev_df)
        is_extended = vwap_sd > 2.0
        edge_desc = " + ".join(e['trigger'] for e in edge_payload['edges'])

        valid_signal, pro_conf_msgs = self._check_pro_confluence(
            symbol, df, prev_df, slope, is_extended, vwap_sd,
            edge_desc, depth_data, ltp, oi
        )
        if pro_conf_msgs:
            edge_desc += f" + {', '.join(pro_conf_msgs)}"

        if not valid_signal:
            return None

        # Gate 11-12: HTF + Finalize (adds ML logging, SL, etc.)
        base_signal = self._finalize_signal(symbol, ltp, df, edge_desc, slope, "")
        if base_signal is None:
            return None

        # Merge edge metadata into signal for Telegram + logging
        base_signal['edges_detected'] = [e['trigger'] for e in edge_payload['edges']]
        base_signal['confidence'] = edge_payload['confidence']
        base_signal['edge_count'] = edge_payload['edge_count']
        base_signal['primary_edge'] = edge_payload['primary_trigger']

        # Override SL if multi-edge recommends a tighter one
        if edge_payload.get('recommended_sl') and edge_payload['recommended_sl'] < base_signal['stop_loss']:
            base_signal['stop_loss'] = edge_payload['recommended_sl']

        return base_signal

    def _finalize_signal(self, symbol, ltp, df, pattern_desc, slope, wall_msg):
        """Final HTF checks and logging."""
        # HTF Confluence Check
        htf_ok, htf_msg = self.htf_confluence.check_trend_exhaustion(symbol)
        if not htf_ok:
            logger.info(f"BLOCKED by HTF Confluence: {symbol} - {htf_msg}")
            return None

        # Key Level Check
        at_level, level_name, level_price = self.htf_confluence.is_at_key_level(symbol, ltp, tolerance_pct=1.0)
        level_msg = f"At {level_name} ({level_price:.2f})" if at_level else ""
        if level_msg:
            logger.info(f"   Key Level: {level_msg}")

        # Calculate Stop Loss (ATR)
        atr = self.gm_analyst.calculate_atr(df)
        buffer = max(atr * 0.5, 0.25)
        setup_high = df.iloc[-2]['high']
        sl_price = setup_high + buffer

        # Logging
        logger.info(f"[OK] GOD MODE SIGNAL: {symbol} | {pattern_desc}")
        logger.info(f"   HTF: {htf_msg}")
        
        meta_str = f"Slope:{slope:.1f}, {wall_msg}, ATR:{atr:.2f}, {htf_msg}, {level_msg}"
        log_signal(symbol, ltp, pattern_desc, sl_price, meta_str,
                   setup_high=setup_high, tick_size=0.05, atr=atr)
        
        # ===== ML DATA LOGGING =====
        try:
            ml_logger = get_ml_logger()
            prev_candle = df.iloc[-2]
            
            # Calculate candle metrics
            body = abs(prev_candle['close'] - prev_candle['open'])
            total_range = prev_candle['high'] - prev_candle['low']
            upper_wick = prev_candle['high'] - max(prev_candle['open'], prev_candle['close'])
            lower_wick = min(prev_candle['open'], prev_candle['close']) - prev_candle['low']
            
            # VWAP
            vwap = df['vwap'].iloc[-1] if 'vwap' in df.columns else ltp
            vwap_dist = ((ltp - vwap) / vwap) * 100 if vwap > 0 else 0
            
            # Volume
            vol_avg = df['volume'].iloc[-20:].mean() if len(df) > 20 else df['volume'].mean()
            rvol = prev_candle['volume'] / vol_avg if vol_avg > 0 else 1
            
            # Features dict
            features = {
                "prev_close": df.iloc[0]['open'],  # Approximation
                "day_high": df['high'].max(),
                "day_low": df['low'].min(),
                "gain_pct": ((ltp - df.iloc[0]['open']) / df.iloc[0]['open']) * 100,
                
                "vwap": vwap,
                "vwap_distance_pct": vwap_dist,
                "vwap_sd": self.gm_analyst.calculate_vwap_bands(df.iloc[:-1]),
                "vwap_slope": slope,
                
                "volume_current": prev_candle['volume'],
                "volume_avg_20": vol_avg,
                "rvol": rvol,
                
                "pattern": pattern_desc.split(" + ")[0],  # Base pattern only
                "candle_body_pct": (body / total_range * 100) if total_range > 0 else 0,
                "upper_wick_pct": (upper_wick / total_range * 100) if total_range > 0 else 0,
                "lower_wick_pct": (lower_wick / total_range * 100) if total_range > 0 else 0,
                
                "num_confirmations": pattern_desc.count(",") + 1 if "+" in pattern_desc else 0,
                "confirmations": pattern_desc.split(" + ")[1:] if " + " in pattern_desc else [],
                
                "nifty_trend": self.market_context.get_trend_label() if hasattr(self.market_context, 'get_trend_label') else "UNKNOWN",
            }
            
            obs_id = ml_logger.log_observation(symbol, ltp, features)
            logger.info(f"   [ML] Logged observation: {obs_id}")
        except Exception as e:
            logger.warning(f"   [ML] Logging error: {e}")
        
        # Record & Return
        # Record & Return
        signal_data = {
            'symbol': symbol,
            'ltp': ltp,
            'pattern': pattern_desc,
            'stop_loss': sl_price, 
            'day_high': df['high'].max(),
            'signal_low': df.iloc[-2]['low'], # CRITICAL: Validation Level
            'setup_high': setup_high,         # Phase 41.2: For scalper SL calc
            'tick_size': 0.05,                # Phase 41.2: Default NSE tick
            'atr': atr,                       # Phase 41.2: For legacy simulation
            'meta': meta_str
        }

        can_signal, reason = self.signal_manager.can_signal(symbol)
        if not can_signal:
            if "Cooldown" in reason:
                logger.info(f"   [PENDING] {symbol} blocked by cooldown.")
                signal_data['cooldown_blocked'] = True
                signal_data['cooldown_reason'] = reason
                return signal_data
            else:
                logger.info(f"   [BLOCKED] by Signal Manager: {symbol} - {reason}")
                return None
        
        self.signal_manager.record_signal(symbol, ltp, sl_price, pattern_desc)
        remaining = self.signal_manager.get_remaining_signals()
        logger.info(f"   Signals remaining today: {remaining}")
        
        return signal_data
