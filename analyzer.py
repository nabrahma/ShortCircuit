import pandas as pd
import logging
import datetime
import csv
import os
import config
from dashboard_bridge import get_dashboard_bridge
from typing import Optional, Dict, Any, Tuple
from collections import deque

from market_context import MarketContext
from signal_manager import get_signal_manager
from htf_confluence import HTFConfluence
from god_mode_logic import GodModeAnalyst
from tape_reader import TapeReader
from market_profile import ProfileAnalyzer
from ml_logger import get_ml_logger
from gate_result_logger import GateResult, get_gate_result_logger

logger = logging.getLogger(__name__)

SIGNAL_LOG_FILE = "logs/signals.csv"

def log_signal(symbol: str, ltp: float, pattern: str, stop_loss: float,
               meta: str = "", setup_high: float = 0.0,
               tick_size: float = 0.05, atr: float = 0.0,
               stretch_score: float = 0.0, vol_fade_ratio: float = 0.0,
               confidence: str = "", pattern_bonus: str = "None",
               oi_direction: str = "unknown"):
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
                             "stop_loss", "meta", "setup_high", "tick_size", "atr",
                             "stretch_score", "vol_fade_ratio", "confidence",
                             "pattern_bonus", "oi_direction"])
        
        writer.writerow([
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            symbol,
            ltp,
            pattern,
            stop_loss,
            meta,
            setup_high,
            tick_size,
            atr,
            stretch_score,
            vol_fade_ratio,
            confidence,
            pattern_bonus,
            oi_direction
        ])

class FyersAnalyzer:
    """
    Core analysis engine for the ShortCircuit strategy.
    Orchestrates technical analysis, pattern recognition, and risk checks.
    """
    
    def __init__(self, fyers, broker=None, morning_high=None, morning_low=None):
        self.fyers = fyers
        self.broker = broker
        self.market_context = MarketContext(fyers, morning_high, morning_low)
        self.signal_manager = get_signal_manager()
        self.htf_confluence = HTFConfluence(fyers)
        self.gm_analyst = GodModeAnalyst()
        self.tape_reader = TapeReader()
        self.profile_analyzer = ProfileAnalyzer()
        self.oi_history = {} # Symbol -> deque of (timestamp, oi, price) use deque(maxlen=20)
        self.slope_decay_tracker = {} # Phase 66: Symbol -> first_decay_time (datetime)

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

    def check_setup(self, symbol: str, ltp: float, oi: float = 0, pre_fetched_df: Optional[pd.DataFrame] = None,
                    df_15m: Optional[pd.DataFrame] = None, # Phase 51
                    scan_id: int = 0, data_tier: str = "UNKNOWN") -> Optional[Dict[str, Any]]:
        """
        Validates a trading candidate using the God Mode strategy.
        Optimized to use pre-fetched History DF and Shared Depth Data.
        """
        grl = get_gate_result_logger()
        gr = GateResult(symbol=symbol, scan_id=scan_id, data_tier=data_tier)
        signal_meta = {}

        # ── G1+G2: Data Fetching & Enrichment (Phase 65 Order) ───────
        if pre_fetched_df is not None:
             df = pre_fetched_df
             df = df.copy()
        else:
             df = self.get_history(symbol)
             
        if df is None or df.empty:
            gr.verdict = "DATA_ERROR"
            gr.rejection_reason = "No history data available"
            grl.record(gr)
            return None
            
        # Hard guard: if df has fewer than RVOL_MIN_CANDLES rows, RVOL is unreliable.
        if config.RVOL_VALIDITY_GATE_ENABLED and len(df) < config.RVOL_MIN_CANDLES:
            gr.g2_pass = False
            gr.g2_value = float(len(df))
            gr.verdict = "REJECTED"
            gr.first_fail_gate = "G2_RVOL_VALIDITY"
            gr.rejection_reason = f"Only {len(df)} candles — need {config.RVOL_MIN_CANDLES} for valid RVOL"
            logger.warning(f"SKIP {symbol} — RVOL_VALIDITY_GATE: Only {len(df)} candles — need {config.RVOL_MIN_CANDLES}")
            grl.record(gr)
            return None
        gr.g2_pass = True
        # Enrichment & Technicals
        self._enrich_dataframe(df)
        prev_df = df.iloc[:-1]

        # Phase 66: Tech Pre-calc (Moved up for Adaptive G1)
        atr = self.gm_analyst.calculate_atr(df)
        vwap_sd = self.gm_analyst.calculate_vwap_bands(prev_df)
        is_extended = vwap_sd > 2.0
        slope_now, _  = self.gm_analyst.calculate_vwap_slope(df.iloc[-30:])
        slope_prev, _ = self.gm_analyst.calculate_vwap_slope(df.iloc[-31:-1])

        # Phase 66: Momentum Decay Tracker (Stateful)
        is_decaying = False
        if getattr(config, 'P66_ADAPTIVE_G1_ENABLED', False):
            # Condition: Momentum is slowing AND price is extended
            if slope_now < slope_prev and vwap_sd > config.P66_G4_DECAY_SD_THRESHOLD:
                if symbol not in self.slope_decay_tracker:
                    self.slope_decay_tracker[symbol] = datetime.datetime.now()
                
                # Check confirmation window (120s)
                first_decay = self.slope_decay_tracker[symbol]
                duration = (datetime.datetime.now() - first_decay).total_seconds()
                if duration >= config.P66_G4_DECAY_CONFIRMATION_SEC:
                    is_decaying = True
            else:
                # Reset if momentum accelerates or extension drops
                self.slope_decay_tracker.pop(symbol, None)

        # Standardize Gain Calculation
        pc = 0
        try:
            if self.broker:
                snapshot = self.broker.get_quote_cache_snapshot()
                if symbol in snapshot:
                    entry = snapshot[symbol]
                    pc = entry.get('pc', 0)
                    if pc == 0 and entry.get('ch_oc', 0) != 0:
                        ltp_val = entry.get('ltp', ltp)
                        ch_oc = entry.get('ch_oc')
                        pc = ltp_val / (1 + (ch_oc / 100))
        except Exception:
            pass

        day_high = df['high'].max()
        open_price = df.iloc[0]['open']
        baseline = pc if pc > 0 else open_price
        gain_pct = ((ltp - baseline) / baseline) * 100
        
        # Phase 65: AMT Pre-checks (Profile & Volume Z)
        profile = None
        profile_rejection = False
        vol_z = 0.0
        try:
            profile = self.profile_analyzer.calculate_market_profile(df, mode='VOLUME')
            if profile:
                profile_rejection, _ = self.profile_analyzer.check_profile_rejection(df, ltp)
            vol_z = self.market_context.get_volume_z_score(df)
        except Exception as e:
            logger.warning(f"Profile/VolZ pre-calc error for {symbol}: {e}")

        # V1: Broadcast Symbol Focus to HUD
        get_dashboard_bridge().broadcast("SYMBOL_UPDATE", {
            "symbol": symbol,
            "ltp": ltp,
            "gain_pct": gain_pct if 'gain_pct' in locals() else 0.0,
            "rvol": rvol if 'rvol' in locals() else 0.0,
            "slope": slope_now if 'slope_now' in locals() else 0.0,
            "nifty_trend": getattr(reason, 'split')(':')[-1].strip() if 'reason' in locals() else "Unknown"
        })

        # ── G7: Market Regime & Time Gate (Phase 65 Signature) ────────
        allowed, reason = self.market_context.evaluate_g7(
            vwap_sd=vwap_sd, 
            profile_rejection=profile_rejection, 
            volume_z=vol_z
        )
            
        gr.g7_pass = allowed
        gr.g7_value = reason
        get_dashboard_bridge().broadcast("GATE_UPDATE", {"gate": "G7", "status": "PASS" if allowed else "FAIL"})
        if not allowed:
            gr.verdict = "REJECTED"
            gr.first_fail_gate = "G7_REGIME"
            gr.rejection_reason = reason
            grl.record(gr)
            return None

        # ── G1: Constraints & Kill Backdoor (Phase 66 Adaptive) ──────
        min_gain = config.SCANNER_GAIN_MIN_PCT
        amt_failing_auction = False
        if getattr(config, 'P65_AMT_ENABLED', False):
            if profile_rejection and gain_pct >= config.P65_G1_NET_GAIN_THRESHOLD:
                min_gain = config.P65_G1_NET_GAIN_THRESHOLD
                amt_failing_auction = True
        
        ok, msg = self.gm_analyst.check_constraints(
            ltp, day_high, gain_pct, open_price, 
            df=df, atr=atr, is_decaying=is_decaying
        )
        
        # Override gain check for AMT
        if gain_pct < min_gain:
            ok = False
            msg = f"Insufficient Gain: {gain_pct:.1f}% (need {min_gain}%)"
        elif gain_pct < config.SCANNER_GAIN_MIN_PCT and not amt_failing_auction:
             ok = False
             msg = f"Low Gain {gain_pct:.1f}% requires AMT Profile Rejection"

        gr.g1_pass = ok
        gr.g1_value = round(gain_pct, 2)
        get_dashboard_bridge().broadcast("GATE_UPDATE", {"gate": "G1", "status": "PASS" if ok else "FAIL"})
        if not ok:
            gr.verdict = "REJECTED"
            gr.first_fail_gate = "G1_GAIN_CONSTRAINTS"
            gr.rejection_reason = msg
            grl.record(gr)
            return None

        # ── G3: Circuit Guard & Blacklist ───────────────────────────
        depth_data = None
        try:
            full_depth = self.fyers.depth(data={"symbol": symbol, "ohlcv_flag":"1"})
            if 'd' in full_depth and symbol in full_depth['d']:
                depth_data = full_depth['d'][symbol]
                uc = depth_data.get('upper_ckt', 0)
                if uc > 0 and ltp >= uc * 0.999:
                    self.market_context.mark_circuit_touched(symbol)
        except:
            pass
            
        is_blacklisted = self.market_context.is_circuit_hitter(symbol)
        circuit_blocked = is_blacklisted or self._check_circuit_guard(symbol, ltp, depth_data)
        
        gr.g3_pass = not circuit_blocked
        gr.g3_value = "BLACKLISTED" if is_blacklisted else round(ltp, 2)
        get_dashboard_bridge().broadcast("GATE_UPDATE", {"gate": "G3", "status": "PASS" if not circuit_blocked else "FAIL"})
        if circuit_blocked:
            gr.verdict = "REJECTED"
            gr.first_fail_gate = "G3_CIRCUIT_GUARD"
            gr.rejection_reason = "Circuit Hitter (Session Blacklist)" if is_blacklisted else f"LTP {ltp} too close to upper circuit"
            grl.record(gr)
            return None

        # ── G4: Momentum safeguard ──────────────────────────────────
        slope_now, _  = self.gm_analyst.calculate_vwap_slope(df.iloc[-30:])
        slope_prev, _ = self.gm_analyst.calculate_vwap_slope(df.iloc[-31:-1])
        
        momentum_blocked = self._is_momentum_too_strong(df, slope_now, slope_prev, vwap_sd, symbol, gain_pct)
        gr.g4_pass = not momentum_blocked
        gr.g4_value = round(slope_now, 3)
        get_dashboard_bridge().broadcast("GATE_UPDATE", {"gate": "G4", "status": "PASS" if not momentum_blocked else "FAIL"})
        if momentum_blocked:
            gr.verdict = "REJECTED"
            gr.first_fail_gate = "G4_MOMENTUM"
            gr.rejection_reason = f"Momentum too strong (slope={slope_now:.2f})"
            grl.record(gr)
            return None

        # ── G5: Gate 5 — Exhaustion at Stretch ──────────────────────
        if profile is None:
            gr.g5_pass = False
            gr.verdict = "REJECTED"
            gr.first_fail_gate = "G5_PROFILE_UNAVAILABLE"
            gr.rejection_reason = "Market profile computation failed — VAH unverifiable"
            grl.record(gr)
            return None

        exhaustion = self.gm_analyst.is_exhaustion_at_stretch(
            candles=df.to_dict('records'),
            profile=profile,
            gain_pct=gain_pct,
            atr=atr,
            vwap_sd=vwap_sd
        )
        gr.g5_pass = exhaustion["fired"]
        gr.g5_value = round(exhaustion.get("stretch_score", 0), 3)
        get_dashboard_bridge().broadcast("GATE_UPDATE", {"gate": "G5", "status": "PASS" if exhaustion["fired"] else "FAIL"})

        if not exhaustion["fired"]:
            gr.verdict = "REJECTED"
            gr.first_fail_gate = "G5_EXHAUSTION"
            gr.rejection_reason = exhaustion.get('reject_reason', 'Exhaustion conditions not met')
            grl.record(gr)
            return None

        signal_meta.update({
            "stretch_score":  exhaustion["stretch_score"],
            "vol_fade_ratio": exhaustion["vol_fade_ratio"],
            "confidence":     exhaustion["confidence"],
            "pattern_bonus":  exhaustion["pattern_bonus"],
        })

        pattern_desc = exhaustion['pattern_bonus'] if exhaustion['pattern_bonus'] != "None" else "EXHAUSTION_FADE"

        # ── G6: Tiered Scoring ──────────────────────────────────────
        valid_signal, pro_conf_msgs = self._check_pro_confluence(
            symbol, df, prev_df, slope_now, is_extended, vwap_sd,
            pattern_desc, depth_data, ltp, oi, signal_meta
        )
        
        if config.PHASE_51_ENABLED:
            confluence_score = len(pro_conf_msgs)
            if confluence_score < 2:
                valid_signal = False
                gr.rejection_reason = f"Low Confluence Score: {confluence_score} (need 2+)"
            
        gr.g6_pass = valid_signal
        gr.g6_value = f"+{len(pro_conf_msgs)}conf"
        get_dashboard_bridge().broadcast("GATE_UPDATE", {"gate": "G6", "status": "PASS" if valid_signal else "FAIL"})
        if pro_conf_msgs:
            pattern_desc += f" + {', '.join(pro_conf_msgs)}"

        if not valid_signal:
            gr.verdict = "REJECTED"
            gr.first_fail_gate = "G6_TIERED_SCORING"
            grl.record(gr)
            return None

        # ── G8: Signal Manager gate ───────────────────────────────
        sm = self.signal_manager
        can_signal, sm_reason = sm.can_signal(symbol) if hasattr(sm, 'can_signal') else (True, "")
        gr.g8_pass = can_signal
        gr.g8_value = None
        get_dashboard_bridge().broadcast("GATE_UPDATE", {"gate": "G8", "status": "PASS" if can_signal else "FAIL"})
        if not can_signal:
            gr.verdict = "REJECTED"
            gr.first_fail_gate = "G8_SIGNAL_MANAGER"
            gr.rejection_reason = sm_reason
            grl.record(gr)
            return None

        # ── G9: HTF Confluence ────────────────────────────────────
        import concurrent.futures
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _htf_exec:
                _htf_future = _htf_exec.submit(self.htf_confluence.check_trend_exhaustion, symbol, df_15m=df_15m, vwap_sd=vwap_sd)
                htf_ok, htf_msg = _htf_future.result(timeout=1.5)
        except Exception as e:
            htf_ok, htf_msg = True, f"HTF_BYPASS:{e}"

        gr.g9_pass  = htf_ok
        gr.g9_value = htf_msg
        get_dashboard_bridge().broadcast("GATE_UPDATE", {"gate": "G9", "status": "PASS" if htf_ok else "FAIL"})
        if not htf_ok:
            gr.verdict = "REJECTED"
            gr.first_fail_gate = "G9_HTF_CONFLUENCE"
            gr.rejection_reason = f"HTF blocked: {htf_msg}"
            grl.record(gr)
            return None

        # ── G13: Risk & Reward (Phase 65 Dynamic Scaling) ───────────
        if getattr(config, 'P65_AMT_ENABLED', False) and gain_pct < 9.0:
            signal_meta['tp1_atr_mult_override'] = 1.0
            logger.info(f"[G13] Dynamic Risk Scaling applied for {symbol} (Gain: {gain_pct:.1f}%) -> TP1: 1.0x ATR")

        # Phase 66: Snapshot Reference High (Peak of Day)
        # Ensure SL and Signal High are derived from the absolute top, even if rotating.
        signal_meta['snapshot_high'] = day_high
        get_dashboard_bridge().broadcast("GATE_UPDATE", {"gate": "G13", "status": "PASS"})

        gr.verdict = "ANALYZER_PASS"
        get_dashboard_bridge().broadcast("SYSTEM_ALERT", {"msg": f"🎯 Signal Confirmed: {symbol}"})
        finalized = self._finalize_signal(symbol, ltp, df, pattern_desc, slope_now, "", signal_meta)
        if finalized:
            finalized['_gate_result'] = gr
        return finalized

    def _check_filters(self, symbol: str) -> bool:
        """Runs pre-analysis checks: Market Regime. (Legacy — delegates to detailed version)"""
        allowed, _ = self._check_filters_detailed(symbol)
        return allowed

    def _check_filters_detailed(self, symbol: str):
        """Returns (allowed: bool, reason: str) for G7 gate recording."""
        allow_short, reason = self.market_context.should_allow_short()
        if not allow_short:
            logger.info(f"BLOCKED by Market Regime: {symbol} - {reason}")
            return False, reason
        return True, "ok"


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

    def _is_momentum_too_strong(self, df: pd.DataFrame, slope_now: float, slope_prev: float, vwap_sd: float, symbol: str, gain_pct: float = 0.0) -> bool:
        """Checks if momentum is too strong to short."""
        try:
            recent_vols = df['volume'].iloc[-20:-1]
            avg_v  = recent_vols.mean()
            curr_v = df['volume'].iloc[-1]
            rvol_now = curr_v / avg_v if avg_v > 0 else 0

            # Phase 51: Using config thresholds
            rvol_thresh = config.P51_G4_RVOL_THRESHOLD if config.PHASE_51_ENABLED else 5.0
            slope_thresh = config.P51_G4_SLOPE_MIN if config.PHASE_51_ENABLED else 3.0
            
            if rvol_now > rvol_thresh:
                logger.warning(f"  MOMENTUM BLOCK {symbol} RVOL {rvol_now:.1f}x (> {rvol_thresh}x threshold)")
                return True
            
            if slope_now > slope_thresh:
                # Phase 57: Slope Decay Check (Murphy Divergence)
                # If momentum is slowing down (slope_now < slope_prev) and price is extended, allow.
                if getattr(config, 'P57_G4_SLOPE_DECAY_ENABLED', False):
                    div_thresh = getattr(config, 'P57_G4_DIVERGENCE_SD', 1.5)
                    
                    # Phase 60: Structural Extension Fallback (Gain > 10%)
                    is_structurally_extended = gain_pct > getattr(config, 'P60_G4_STRUCTURAL_FALLBACK_GAIN', 10.0)
                    
                    if slope_now < slope_prev and (vwap_sd > div_thresh or is_structurally_extended):
                        logger.info(f"✅ [MOMENTUM DECAY] {symbol} allowed via Structural Fallback (Gain={gain_pct:.1f}%)")
                        return False

                logger.warning(f"  MOMENTUM BLOCK {symbol} Slope {slope_now:.1f} (> {slope_thresh} threshold)")
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

    def _check_pro_confluence(self, symbol, df, prev_df, slope, is_extended, vwap_sd, pattern_desc, depth_data=None, ltp=0, oi=0, signal_meta=None) -> Tuple[bool, list]:
        """Verifies secondary confirmation signals."""
        if signal_meta is None: signal_meta = {}
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
        
        # ── PHASE 44.8: Futures OI enrichment ───────────────────────────
        # Fetch if available. Never blocks signal. Logs direction only.
        oi_direction = "unknown"
        try:
            from symbols import get_front_month_futures
            fut_sym = get_front_month_futures(symbol)
            if fut_sym:
                fut_resp = self.fyers.quotes({"symbols": fut_sym})
                if (fut_resp
                        and fut_resp.get('d')
                        and fut_resp['d'][0].get('s') != 'error'
                        and fut_resp['d'][0].get('v')):
                    oi_chg = fut_resp['d'][0]['v'].get('ch', 0)
                    if oi_chg < 0:
                        oi_direction = "falling"   # short covering = confirms exhaustion
                        logger.debug(f"[Gate6-OI] {symbol} OI falling ✅ short-covering rally")
                    elif oi_chg > 0:
                        oi_direction = "rising"    # new longs = flag, not block
                        logger.debug(f"[Gate6-OI] {symbol} OI rising ⚠️ new longs entering")
                    else:
                        oi_direction = "flat"
                else:
                    logger.debug(f"[Gate6-OI] {symbol} no futures contract — skipped")
        except Exception as e:
            logger.debug(f"[Gate6-OI] {symbol} OI fetch error (non-fatal): {e}")

        signal_meta["oi_direction"] = oi_direction
        
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
            dist_pct = (ltp - dpoc) / dpoc * 100
            if dist_pct > 1.0: 
                return True, "Value Div"
        except:
            pass
        return False, ""

    def check_setup_with_edges(
        self, symbol: str, ltp: float, oi: float,
        pre_fetched_df, edge_payload: dict,
        scan_id: int = 0, data_tier: str = "UNKNOWN"
    ):
        """
        Called when MULTI_EDGE_ENABLED is True.
        Runs Gates G1-G9 (same as check_setup) then validates the edge-specific
        entry trigger. All exit paths are recorded in the gate audit trail.
        """
        grl = get_gate_result_logger()
        gr = GateResult(symbol=symbol, scan_id=scan_id, data_tier=data_tier)
        signal_meta = {}

        # ── G1+G2: Data Fetching & Enrichment (Phase 65) ──────────────
        if pre_fetched_df is not None:
            df = pre_fetched_df.copy()
        else:
            df = self.get_history(symbol)

        if df is None or df.empty:
            gr.verdict = "DATA_ERROR"
            gr.rejection_reason = "No history data available"
            grl.record(gr)
            return None

        if config.RVOL_VALIDITY_GATE_ENABLED and len(df) < config.RVOL_MIN_CANDLES:
            gr.g2_pass  = False
            gr.g2_value = float(len(df))
            gr.verdict  = "REJECTED"
            gr.first_fail_gate  = "G2_RVOL_VALIDITY"
            gr.rejection_reason = f"Only {len(df)} candles — need {config.RVOL_MIN_CANDLES}"
            logger.warning(f"SKIP {symbol} — RVOL_VALIDITY_GATE: Only {len(df)} candles")
            grl.record(gr)
            return None
        gr.g2_pass  = True
        gr.g2_value = float(len(df))

        # Enrichment & Technicals
        self._enrich_dataframe(df)
        prev_df = df.iloc[:-1]

        # Standardize Gain Calculation
        pc = 0
        try:
            snapshot = self.fyers.get_quote_cache_snapshot()
            if symbol in snapshot:
                pc = snapshot[symbol].get('pc', 0)
        except Exception:
            pass

        day_high   = df['high'].max()
        open_price = df.iloc[0]['open']
        baseline = pc if pc > 0 else open_price
        gain_pct = ((ltp - baseline) / baseline) * 100

        atr = self.gm_analyst.calculate_atr(df)
        vwap_sd = self.gm_analyst.calculate_vwap_bands(prev_df)
        is_extended = vwap_sd > 2.0

        # Phase 65: AMT Pre-checks
        profile = None
        profile_rejection = False
        vol_z = 0.0
        try:
            profile = self.profile_analyzer.calculate_market_profile(df, mode='VOLUME')
            if profile:
                profile_rejection, _ = self.profile_analyzer.check_profile_rejection(df, ltp)
                # Phase 75: Broadcast Volume Profile to UI for AMT Charting
                get_dashboard_bridge().broadcast("AMT_UPDATE", {
                    "symbol": symbol,
                    "vah": profile.get('vah'),
                    "poc": profile.get('poc'),
                    "val": profile.get('val'),
                    "counts": profile.get('counts', []).tolist() if hasattr(profile.get('counts'), 'tolist') else [],
                    "bins": profile.get('bins', []).tolist() if hasattr(profile.get('bins'), 'tolist') else []
                })
            vol_z = self.market_context.get_volume_z_score(df)
        except Exception as e:
            logger.warning(f"Profile/VolZ pre-calc error for edge {symbol}: {e}")

        # ── G7: Market Regime & Time Gate (Phase 65) ──────────────────
        allowed, regime_reason = self.market_context.evaluate_g7(
            vwap_sd=vwap_sd,
            profile_rejection=profile_rejection,
            volume_z=vol_z
        )
        gr.g7_pass  = allowed
        gr.g7_value = regime_reason
        get_dashboard_bridge().broadcast("GATE_UPDATE", {"symbol": symbol, "gate": "G7", "status": "PASS" if allowed else "FAIL", "value": regime_reason})
        if not allowed:
            gr.verdict = "REJECTED"
            gr.first_fail_gate = "G7_REGIME"
            gr.rejection_reason = regime_reason
            grl.record(gr)
            return None

        # ── G1: Gain constraints (Phase 65 Soft Threshold) ───────────
        min_gain = config.SCANNER_GAIN_MIN_PCT
        amt_failing_auction = False
        if getattr(config, 'P65_AMT_ENABLED', False):
            if profile_rejection and gain_pct >= config.P65_G1_NET_GAIN_THRESHOLD:
                min_gain = config.P65_G1_NET_GAIN_THRESHOLD
                amt_failing_auction = True

        ok, msg = self.gm_analyst.check_constraints(ltp, day_high, gain_pct, open_price, df=df, atr=atr)
        
        if gain_pct < min_gain:
            ok = False
            msg = f"Insufficient Gain: {gain_pct:.1f}% (need {min_gain}%)"
        elif gain_pct < config.SCANNER_GAIN_MIN_PCT and not amt_failing_auction:
             ok = False
             msg = f"Low Gain {gain_pct:.1f}% requires AMT Profile Rejection"

        gr.g1_pass  = ok
        gr.g1_value = round(gain_pct, 2)
        get_dashboard_bridge().broadcast("GATE_UPDATE", {"symbol": symbol, "gate": "G1", "status": "PASS" if ok else "FAIL", "value": f"{gain_pct:.1f}%"})
        if not ok:
            gr.verdict = "REJECTED"
            gr.first_fail_gate  = "G1_GAIN_CONSTRAINTS"
            gr.rejection_reason = msg
            grl.record(gr)
            return None

        # ── G3: Circuit Guard ──────────────────────────────────────────
        depth_data = None
        try:
            full_depth = self.fyers.depth(data={"symbol": symbol, "ohlcv_flag": "1"})
            if 'd' in full_depth and symbol in full_depth['d']:
                depth_data = full_depth['d'][symbol]
        except Exception:
            pass

        circuit_blocked = self._check_circuit_guard(symbol, ltp, depth_data)
        gr.g3_pass  = not circuit_blocked
        gr.g3_value = round(ltp, 2)
        get_dashboard_bridge().broadcast("GATE_UPDATE", {"symbol": symbol, "gate": "G3", "status": "PASS" if not circuit_blocked else "FAIL"})
        if circuit_blocked:
            gr.verdict = "REJECTED"
            gr.first_fail_gate  = "G3_CIRCUIT_GUARD"
            gr.rejection_reason = f"LTP {ltp} too close to upper circuit"
            grl.record(gr)
            return None

        # ── G4: Momentum Safeguard ──────────────────────────────────
        slope_now, _  = self.gm_analyst.calculate_vwap_slope(df.iloc[-30:])
        slope_prev, _ = self.gm_analyst.calculate_vwap_slope(df.iloc[-31:-1])

        momentum_blocked = self._is_momentum_too_strong(df, slope_now, slope_prev, vwap_sd, symbol, gain_pct)
        gr.g4_pass  = not momentum_blocked
        gr.g4_value = round(slope_now, 3)
        get_dashboard_bridge().broadcast("GATE_UPDATE", {"symbol": symbol, "gate": "G4", "status": "PASS" if not momentum_blocked else "FAIL", "value": f"{slope_now:.1f}"})
        if momentum_blocked:
            gr.verdict = "REJECTED"
            gr.first_fail_gate  = "G4_MOMENTUM"
            gr.rejection_reason = f"Momentum too strong (slope={slope_now:.2f})"
            grl.record(gr)
            return None

        # ── Edge-specific entry trigger check ─────────────────────────
        current_ltp = df.iloc[-1]['close']
        is_g5_pass = current_ltp < edge_payload['entry_trigger']
        get_dashboard_bridge().broadcast("GATE_UPDATE", {"symbol": symbol, "gate": "G5", "status": "PASS" if is_g5_pass else "FAIL", "value": f"@{current_ltp}"})
        if not is_g5_pass:
            gr.g5_pass  = False
            gr.g5_value = round(current_ltp - edge_payload['entry_trigger'], 4)
            gr.verdict  = "REJECTED"
            gr.first_fail_gate  = "G5_EDGE_TRIGGER_NOT_BROKEN"
            gr.rejection_reason = f"LTP {current_ltp} >= entry_trigger {edge_payload['entry_trigger']}"
            grl.record(gr)
            return None
        gr.g5_pass = True

        # ── G6: Pro Confluence ─────────────────────────────────────────
        edge_desc   = " + ".join(e['trigger'] for e in edge_payload['edges'])
        valid_signal, pro_conf_msgs = self._check_pro_confluence(
            symbol, df, prev_df, slope_now, is_extended, vwap_sd,
            edge_desc, depth_data, ltp, oi, signal_meta
        )
        gr.g6_pass  = valid_signal
        gr.g6_value = f"+{len(pro_conf_msgs)}conf"
        if pro_conf_msgs:
            edge_desc += f" + {', '.join(pro_conf_msgs)}"

        if not valid_signal:
            gr.verdict = "REJECTED"
            gr.first_fail_gate  = "G6_PRO_CONFLUENCE"
            gr.rejection_reason = "No pro confluence for multi-edge candidate"
            grl.record(gr)
            return None

        # ── G9: HTF Confluence ─────────────────────────────────────────
        import concurrent.futures
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _htf_exec:
                _htf_future = _htf_exec.submit(self.htf_confluence.check_trend_exhaustion, symbol, vwap_sd=vwap_sd)
                htf_ok, htf_msg = _htf_future.result(timeout=1.5)
        except Exception as e:
            htf_ok, htf_msg = True, f"HTF_BYPASS:{e}"

        gr.g9_pass  = htf_ok
        gr.g9_value = htf_msg
        get_dashboard_bridge().broadcast("GATE_UPDATE", {"symbol": symbol, "gate": "G9", "status": "PASS" if htf_ok else "FAIL", "value": htf_msg})
        if not htf_ok:
            gr.verdict = "REJECTED"
            gr.first_fail_gate  = "G9_HTF_CONFLUENCE"
            gr.rejection_reason = f"HTF blocked: {htf_msg}"
            grl.record(gr)
            return None

        # ── G13: Risk & Reward (Phase 65 Dynamic Scaling) ───────────
        if getattr(config, 'P65_AMT_ENABLED', False) and gain_pct < 9.0:
            signal_meta['tp1_atr_mult_override'] = 1.0

        # ── Finalize ───────────────────────────────────────────────────
        gr.verdict = "ANALYZER_PASS"
        base_signal = self._finalize_signal(symbol, ltp, df, edge_desc, slope_now, "", signal_meta)
        if base_signal is None:
            gr.verdict = "DATA_ERROR"
            gr.rejection_reason = "_finalize_signal returned None unexpectedly"
            grl.record(gr)
            return None

        # Merge edge metadata
        base_signal['edges_detected'] = [e['trigger'] for e in edge_payload['edges']]
        base_signal['confidence']     = edge_payload['confidence']
        base_signal['edge_count']     = edge_payload['edge_count']
        base_signal['primary_edge']   = edge_payload['primary_trigger']
        base_signal['_gate_result']   = gr

        if edge_payload.get('recommended_sl') and edge_payload['recommended_sl'] < base_signal['stop_loss']:
            base_signal['stop_loss'] = edge_payload['recommended_sl']

        get_dashboard_bridge().broadcast("CANDIDATE_PULSE", {"symbol": symbol, "status": "CONFIRMED"})
        # G12: Pattern Quality
        get_dashboard_bridge().broadcast("GATE_UPDATE", {"symbol": symbol, "gate": "G12", "status": "PASS", "value": edge_payload['confidence']})

        return base_signal
    def _finalize_signal(self, symbol, ltp, df, pattern_desc, slope, wall_msg, signal_meta: dict = None):
        """Calculates SL, builds signal dict, logs to signal log and ML. Pure — no gate checks."""
        if signal_meta is None: signal_meta = {}
        level_msg = "" # Legacy hook

        # Calculate Stop Loss (ATR)
        atr = self.gm_analyst.calculate_atr(df)
        buffer = max(atr * 0.5, 0.25)
        
        # Phase 66: Use Absolute High Snapshot for SL and Signal High
        # This prevents the SL from moving down while the price is rotating.
        peak_high = signal_meta.get('snapshot_high', df.iloc[-2]['high'])
        setup_high = peak_high
        sl_price = setup_high + buffer

        # Logging
        logger.info(f"[OK] GOD MODE SIGNAL: {symbol} | {pattern_desc}")
        logger.info(f"   HTF: checked upstream (G9)")
        
        meta_str = f"Slope:{slope:.1f}, {wall_msg}, ATR:{atr:.2f}, {level_msg}"
        log_signal(symbol, ltp, pattern_desc, sl_price, meta_str,
                   setup_high=setup_high, tick_size=0.05, atr=atr,
                   stretch_score=signal_meta.get('stretch_score', 0.0),
                   vol_fade_ratio=signal_meta.get('vol_fade_ratio', 0.0),
                   confidence=signal_meta.get('confidence', ''),
                   pattern_bonus=signal_meta.get('pattern_bonus', 'None'),
                   oi_direction=signal_meta.get('oi_direction', 'unknown'))
        
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
                "atr": atr,
                "sl_price": sl_price,
                "tp1_price": tp1,
                "tp2_price": tp2,
                "tp3_price": tp3,
            }
            
            obs_id = ml_logger.log_observation(symbol, ltp, features)
            logger.info(f"   [ML] Logged observation: {obs_id}")
        except Exception as e:
            logger.warning(f"   [ML] Logging error: {e}")
        
        # Record & Return
        signal_data = {
            'symbol': symbol,
            'ltp': ltp,
            'pattern': pattern_desc,
            'stop_loss': sl_price, 
            'day_high': df['high'].max(),
            'signal_low': df.iloc[-2]['low'], # CRITICAL: Validation Level
            'setup_high': setup_high,         # Phase 41.2: For scalper SL calc
            'signal_high': setup_high,        # Phase 51: Consistent naming
            'tick_size': 0.05,                # Phase 41.2: Default NSE tick
            'atr': atr,                       # Phase 41.2: For legacy simulation
            'meta': meta_str,
            'obs_id': obs_id if 'obs_id' in locals() else None  # Phase 71: ML Link
        }
        
        # Phase 44.8 — new signal quality fields
        signal_data["stretch_score"]  = signal_meta.get("stretch_score",  0.0)
        signal_data["vol_fade_ratio"] = signal_meta.get("vol_fade_ratio", 0.0)
        signal_data["confidence"]     = signal_meta.get("confidence",     "")
        signal_data["pattern_bonus"]  = signal_meta.get("pattern_bonus",  "None")
        signal_data["oi_direction"]   = signal_meta.get("oi_direction",   "unknown")
        
        # Phase 65: Dynamic Risk Scaling Override
        if 'tp1_atr_mult_override' in signal_meta:
            signal_data['tp1_atr_mult_override'] = signal_meta['tp1_atr_mult_override']
            
        return signal_data
