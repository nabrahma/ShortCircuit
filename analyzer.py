import pandas as pd
import logging
import datetime
import csv
import os
from typing import Optional, Dict, Any, Tuple

from market_context import MarketContext
from signal_manager import get_signal_manager
from htf_confluence import HTFConfluence
from god_mode_logic import GodModeAnalyst
from tape_reader import TapeReader
from market_profile import ProfileAnalyzer

logger = logging.getLogger(__name__)

SIGNAL_LOG_FILE = "logs/signals.csv"

def log_signal(symbol: str, ltp: float, pattern: str, stop_loss: float, meta: str = ""):
    """
    Persists signal details to a CSV file for EOD analysis.
    """
    os.makedirs(os.path.dirname(SIGNAL_LOG_FILE), exist_ok=True)
    file_exists = os.path.exists(SIGNAL_LOG_FILE)
    
    with open(SIGNAL_LOG_FILE, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "symbol", "ltp", "pattern", "stop_loss", "meta"])
        
        writer.writerow([
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            symbol,
            ltp,
            pattern,
            stop_loss,
            meta
        ])

class FyersAnalyzer:
    """
    Core analysis engine for the ShortCircuit strategy.
    Orchestrates technical analysis, pattern recognition, and risk checks.
    """
    
    def __init__(self, fyers):
        self.fyers = fyers
        self.market_context = MarketContext(fyers)
        self.signal_manager = get_signal_manager()
        self.htf_confluence = HTFConfluence(fyers)
        self.gm_analyst = GodModeAnalyst()
        self.tape_reader = TapeReader()
        self.profile_analyzer = ProfileAnalyzer()

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

    def check_setup(self, symbol: str, ltp: float) -> Optional[Dict[str, Any]]:
        """
        Validates a trading candidate using the God Mode strategy.
        
        Logic Flow:
        1. Pre-analysis Filters (Market Regime, Signal Caps, Time)
        2. Data Fetch & Technical Calculation
        3. Hard Constraint Check (Day High, Gain %)
        4. Pattern Recognition (Candle Structure + Tape)
        5. Confluence Checks (HTF, VWAP Extension, Key Levels)
        """
        # --- Pre-analysis Filters ---
        if not self._check_filters(symbol):
            return None

        # --- Data Fetching ---
        df = self.get_history(symbol)
        if df is None or df.empty:
            return None

        # --- Technical Calculations ---
        self._enrich_dataframe(df)
        
        day_high = df['high'].max()
        open_price = df.iloc[0]['open']
        gain_pct = ((ltp - open_price) / open_price) * 100
        
        # --- Hard Constraints ---
        ok, _ = self.gm_analyst.check_constraints(ltp, day_high, gain_pct)
        if not ok:
            return None
            
        slope, _ = self.gm_analyst.calculate_vwap_slope(df.iloc[-30:])

        # --- Momentum Safeguard (Train Filter) ---
        if self._is_momentum_too_strong(df, slope, symbol):
            return None

        # --- Pattern Recognition ---
        # Analyze the 'setup' candle (previous completed candle)
        prev_df = df.iloc[:-1]
        struct, _ = self.gm_analyst.detect_structure_advanced(prev_df)
        
        # Tape Analysis (Stall Detection only, Absorption disabled)
        is_stalled, _ = self.tape_reader.detect_stall(prev_df)

        # Sniper Zone Analysis
        prev_candle = df.iloc[-2]
        is_sniper_zone = self._check_sniper_zone(df)

        # --- Signal Validity Logic ---
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
            if struct in ["SHOOTING_STAR", "BEARISH_ENGULFING", "EVENING_STAR"]:
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
                valid_signal, pro_conf_msgs = self._check_pro_confluence(
                    symbol, df, prev_df, slope, is_extended, vwap_sd, pattern_desc
                )
                if pro_conf_msgs:
                    pattern_desc += f" + {', '.join(pro_conf_msgs)}"

        if valid_signal:
            return self._finalize_signal(symbol, ltp, df, pattern_desc, slope, "")
            
        return None

    def _check_filters(self, symbol: str) -> bool:
        """Runs pre-analysis checks: Signal Manager, Market Regime, Time."""
        # 1. Signal Manager
        can_signal, reason = self.signal_manager.can_signal(symbol)
        if not can_signal:
            logger.info(f"BLOCKED by Signal Manager: {symbol} - {reason}")
            return False
            
        # 2. Market Regime
        allow_short, reason = self.market_context.should_allow_short()
        if not allow_short:
            logger.info(f"BLOCKED by Market Regime: {symbol} - {reason}")
            return False
            
        # 3. Time of Day
        time_ok, reason = self.market_context.is_favorable_time_for_shorts()
        if not time_ok:
            logger.info(f"BLOCKED by Time Filter: {symbol} - {reason}")
            return False
            
        return True

    def _enrich_dataframe(self, df: pd.DataFrame):
        """Calculates VWAP and other indicators in-place."""
        v = df['volume'].values
        tp = (df['high'] + df['low'] + df['close']) / 3
        df['vwap'] = (tp * v).cumsum() / v.cumsum()

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

    def _check_pro_confluence(self, symbol, df, prev_df, slope, is_extended, vwap_sd, pattern_desc) -> Tuple[bool, list]:
        """Verifies secondary confirmation signals."""
        pro_conf = []
        
        # Profile Rejection
        is_bearish_profile, _ = self.profile_analyzer.check_profile_rejection(df, df.iloc[-1]['close'])
        if is_bearish_profile: pro_conf.append("Profile Rejection")
        
        # Tape Wall (DOM)
        try:
            depth_data = self.fyers.depth(data={"symbol": symbol, "ohlcv_flag":"1"})
            if 'd' in depth_data:
                _, d_msg = self.tape_reader.analyze_depth(depth_data['d'][symbol])
                if "Wall" in d_msg:
                    pro_conf.append(f"Dom: {d_msg}")
        except Exception:
            pass

        # Technicals
        if slope < 5: pro_conf.append("VWAP Flat")
        if self.gm_analyst.check_rsi_divergence(prev_df): pro_conf.append("RSI Div 📉")
        if is_extended: pro_conf.append(f"VWAP +{vwap_sd:.1f}SD 📏")

        # Fibonacci
        fibs = self.gm_analyst.calculate_fib_levels(prev_df)
        if fibs:
            setup_high = df.iloc[-2]['high']
            for name, level in fibs.items():
                if name == 'trend': continue
                if abs(setup_high - level) <= (level * 0.001):
                    if fibs.get('trend') == 'DOWN' and df.iloc[-2]['close'] < level:
                        pro_conf.append(f"{name} Reject 📐")
                        break
        
        # RVOL
        try:
            if len(df) > 20:
                avg_vol = df['volume'].iloc[-20:-2].mean()
                setup_vol = df.iloc[-2]['volume']
                rvol = setup_vol / avg_vol if avg_vol > 0 else 0
                if rvol > 2.0:
                    pro_conf.append(f"RVOL {rvol:.1f}x 🔊")
        except Exception:
            pass

        # Validation Logic logic
        if not is_extended and "TAPE" not in pattern_desc:
             # Basic patterns must be extended or have confluence
             if not pro_conf:
                 logger.info(f"Refused {symbol}: Valid Structure but No Pro Confirmation.")
                 return False, []
        
        return True, pro_conf

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
        sl_price = df.iloc[-2]['high'] + buffer

        # Logging
        logger.info(f"✅ GOD MODE SIGNAL: {symbol} | {pattern_desc}")
        logger.info(f"   HTF: {htf_msg}")
        
        meta_str = f"Slope:{slope:.1f}, {wall_msg}, ATR:{atr:.2f}, {htf_msg}, {level_msg}"
        log_signal(symbol, ltp, pattern_desc, sl_price, meta_str)
        
        # Record & Return
        self.signal_manager.record_signal(symbol, ltp, sl_price, pattern_desc)
        remaining = self.signal_manager.get_remaining_signals()
        logger.info(f"   Signals remaining today: {remaining}")
        
        return {
            'symbol': symbol,
            'ltp': ltp,
            'pattern': pattern_desc,
            'stop_loss': sl_price, 
            'day_high': df['high'].max(),
            'meta': meta_str
        }
