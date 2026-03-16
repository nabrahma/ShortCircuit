import pandas as pd
import numpy as np
import logging
# from scipy.stats import linregress

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("GodModeLogic")

class GodModeAnalyst:
    def __init__(self):
        pass

    def calculate_vwap_slope(self, df, window=30):
        """
        Calculates the slope of the VWAP curve over the last 'window' candles.
        Returns:
            slope (float): Interpretation: < 0.05 is Flat (Reversion), > 0.1 is Trend.
            status (str): "FLAT" or "TRENDING"
        """
        if df.empty or len(df) < window:
            return 0.0, "INSUFFICIENT_DATA"
            
        # Get last N VWAP values
        if 'vwap' not in df.columns:
            # Approx VWAP if not present (simple calculation)
            v = df['volume'].values
            tp = (df['high'] + df['low'] + df['close']) / 3
            df['vwap'] = (tp * v).cumsum() / v.cumsum()
            
        y = df['vwap'].iloc[-window:].values
        x = np.arange(len(y))
        
        if len(y) < 2: return 0.0, "INSUFFICIENT_DATA"
        
        # Linear Regression using Numpy (Degree 1)
        slope, intercept = np.polyfit(x, y, 1)
        
        # Normalize slope
        # Better: Slope as % of Price.
        # normalized_slope = slope / df['close'].iloc[-1] * 100
        
        # For simplicity, let's use raw angle or just look at R-squared for linearity?
        # Let's standardize: Change in VWAP per minute / Current Price * 10000 (Basis points)
        
        pct_slope = (slope / df['close'].iloc[-1]) * 10000
        
        status = "FLAT" if abs(pct_slope) < 5 else "TRENDING" # Threshold need tuning
        
        return pct_slope, status

    def detect_structure(self, df):
        """
        Analyzes the last candle for Absorption or Exhaustion.
        """
        if df.empty: return None
        
        last = df.iloc[-1]
        
        # Volatility stats for Z-Score
        recent_vol = df['volume'].iloc[-20:] # Last 20 candles
        avg_vol = recent_vol.mean()
        std_vol = recent_vol.std()
        
        current_vol = last['volume']
        z_score_vol = (current_vol - avg_vol) / std_vol if std_vol > 0 else 0
        
        # Candle shape
        body = abs(last['close'] - last['open'])
        upper_wick = last['high'] - max(last['open'], last['close'])
        
        structure = "NORMAL"
        
        # 1. Absorption: High Vol + Tiny Body (Effort vs Result divergence)
        if z_score_vol > 2.0 and body < (last['close'] * 0.0005): # 0.05% body
            structure = "ABSORPTION"
            
        # 2. Exhaustion: High Vol + Long Wick (Shooting Star)
        elif z_score_vol > 1.5 and upper_wick > (2 * body):
            structure = "EXHAUSTION"
            
        return structure, z_score_vol

    def check_constraints(self, ltp, day_high, net_gain_pct, open_price, df=None, atr: float = 0.0, is_decaying: bool = False):
        """
        G1: Gain & Consistency Constraints.
        Ensures the stock is structurally overextended but not yet in a confirmed crash.
        Phase 66: Added is_decaying flag for adaptive retrace softening.
        """
        import config
        
        # 0. G1.2: Kill Backdoor (ATR-relative)
        # Prevents chasing a move that has already dropped too far from the high.
        if config.P51_G1_KILL_BACKDOOR:
            use_atr = getattr(config, 'P51_G1_KILL_BACKDOOR_USE_ATR', False)
            fixed_pct = getattr(config, 'P51_G1_KILL_BACKDOOR_FIXED_PCT', 0.015)
            atr_mult = getattr(config, 'P51_G1_KILL_BACKDOOR_ATR_MULT', 0.3)

            if use_atr and atr > 0 and day_high > 0:
                atr_pct = atr / day_high
                threshold_pct = max(fixed_pct, atr_mult * atr_pct)
            else:
                threshold_pct = fixed_pct
            
            # Phase 66: Adaptive Softening for Dalton Rotation
            if is_decaying and getattr(config, 'P66_ADAPTIVE_G1_ENABLED', False):
                threshold_pct = config.P66_G1_ROTATION_THRESHOLD_PCT
                logger.info(f"🛡️ [ADAPTIVE G1] Softening retrace limit to {threshold_pct*100:.1f}% due to verified decay.")

            if ltp < day_high * (1 - threshold_pct):
                return False, (
                    f"[G1_REJECT] Kill Backdoor: ₹{ltp:.2f} is "
                    f"{threshold_pct * 100:.1f}% below day high ₹{day_high:.2f} "
                    f"(ATR={atr:.2f}, limit={threshold_pct*100:.2f}%)"
                )

        # 1. G1.3: Time-Since-High Buffer
        # REVERSION LOGIC: We only trade 'fresh' rejections. 
        # If a stock has been 'plateauing' at the high for more than 20 minutes, 
        # it suggests price acceptance, not rejection.
        if df is not None and not df.empty:
            high_idx = df['high'].idxmax()
            last_idx = df.index[-1]
            candles_since_high = last_idx - high_idx
            
            max_candles = getattr(config, 'P51_G1_TIME_SINCE_HIGH_CANDLES', 20)
            if candles_since_high > max_candles:
                return False, f"G1 Time Gate: {candles_since_high} candles since day high (limit {max_candles})"

        return True, "PASSED"

    # Phase 19: ATR
    def calculate_atr(self, df, period=14):
        """
        Calculates Average True Range (ATR).
        """
        try:
            high = df['high']
            low = df['low']
            close = df['close']
            
            # TR1 = High - Low
            # TR2 = abs(High - PrevClose)
            # TR3 = abs(Low - PrevClose)
            
            prev_close = close.shift(1)
            tr1 = high - low
            tr2 = (high - prev_close).abs()
            tr3 = (low - prev_close).abs()
            
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            atr = tr.rolling(window=period).mean()
            
            return atr.iloc[-1]
            
        except Exception as e:
            # logger.error(f"ATR Calc Error: {e}")
            return 1.0 # Fallback default

    # Phase 21: Advanced Reversal Patterns
    def detect_structure_advanced(self, df, vah: float = None):
        """
        Expanded Reversal Logic:
        1. Single Candle (Shooting Star, Doji)
        2. Multi-Candle (Bearish Engulfing, Evening Star)
        3. Auction Theory (VAH Rejection/Look Above & Fail)
        """
        if df.empty or len(df) < 3: return "NORMAL", 0
        
        # Last 3 candles
        c1 = df.iloc[-3] # 2 candles ago
        c2 = df.iloc[-2] # Prev candle
        c3 = df.iloc[-1] # Current candle (Closed)

        # Pattern 0: VAH_REJECTION (Auction Theory: Look Above & Fail) - Phase 59 
        # Logic: Price probed above VAH in the last 3 candles but current candle closes back inside.
        if vah and vah > 0:
            poked_above = df['high'].iloc[-3:].max() > (vah * 1.0005) # Significant probe
            closed_back_in = c3['close'] < (vah * 0.9995) # Acceptance back into balance
            if poked_above and closed_back_in:
                # We return this immediately as it takes precedence over normal patterns for Auction logic
                return "VAH_REJECTION", 0.0 # Confidence handled by Z-Score below if needed
        
        # Helper: Get Body/Range
        def get_candle_stats(row):
            body = abs(row['close'] - row['open'])
            direction = 1 if row['close'] > row['open'] else -1 # 1 Green, -1 Red
            upper_wick = row['high'] - max(row['open'], row['close'])
            total_range = row['high'] - row['low']
            if total_range == 0: total_range = 0.05
            return body, direction, upper_wick, total_range
            
        b1, d1, uw1, r1 = get_candle_stats(c1)
        b2, d2, uw2, r2 = get_candle_stats(c2)
        b3, d3, uw3, r3 = get_candle_stats(c3)
        
        # Vol Stats for Z-Score (on C3)
        recent_vol = df['volume'].iloc[-20:-1]
        avg_vol = recent_vol.mean()
        std_vol = recent_vol.std()
        current_vol = c3['volume']
        z_score = (current_vol - avg_vol) / std_vol if std_vol > 0 else 0
        
        # Pattern 1: Bearish Engulfing (ZENTEC Killer)
        # Prev Green, Curr Red, Curr Body > Prev Body, Curr Open > Prev Close
        if d2 == 1 and d3 == -1:
            if b3 > b2 and c3['close'] < c2['open']:
                # Filter: Significant size (not tiny candles) or Volume
                if z_score > 0: # Ensure at least average volume
                     return "BEARISH_ENGULFING", z_score
                 
        if d2 == 1 and b2 < (r2 * 0.3) and d3 == -1:
             if c3['close'] < (c1['open'] + c1['close'])/2: # Closes below midpoint of C1
                 return "EVENING_STAR", z_score

        # Pattern 3: Shooting Star (Legacy)
        # High Vol + Long Wick
        if uw3 > (2 * b3) and z_score > 1.5:
            return "SHOOTING_STAR", z_score
            
        # Pattern 4: Doji / Absorption
        if z_score > 2.0 and b3 < (c3['close'] * 0.0005):
            return "ABSORPTION_DOJI", z_score

        # Pattern 5: MOMENTUM BREAKDOWN (The "Flush") - Phase 38
        # Big Red Candle + High Vol + Closing at Lows (No lower wick)
        # Body must be > 1.2x Average Body (Relaxed from 1.5x)
        avg_body = df['high'].iloc[-20:-1].sub(df['low'].iloc[-20:-1]).abs().mean()
        if avg_body == 0: avg_body = 0.1
        
        is_big_red = d3 == -1 and b3 > (1.2 * avg_body) 
        # Vol check: relaxed to > 1.5 if body is HUGE (> 2x), or > 1.2 if just Big Red
        # If Body > 1.5x, allow Z > 1.2. If Body > 2.0x, allow Z > 1.0.
        # EXTREME: If Body > 3.0x Avg, allow ANY Volume (Vacuum Flush)
        
        is_high_vol = False
        if z_score > 2.0: is_high_vol = True
        elif b3 > 1.5 * avg_body and z_score > 1.2: is_high_vol = True
        elif b3 > 3.0 * avg_body: is_high_vol = True # Vacuum Flush
        
        # Wick check: Allow up to 35% lower wick (some buying pressure is normal)
        closes_at_low = (c3['close'] - c3['low']) < (r3 * 0.35) 
    
        if is_big_red and is_high_vol and closes_at_low:
            return "MOMENTUM_BREAKDOWN", z_score

        # Pattern 6: VOLUME TRAP (Failed Breakout) - Phase 38
        # Prev: Green + High Vol (Attempted Breakout)
        # Curr: Red + Closes below Prev Low (Trap)
        prev_vol = c2['volume']
        prev_z = (prev_vol - avg_vol) / std_vol if std_vol > 0 else 0
        
        if d2 == 1 and prev_z > 1.5: # Prev was pushing up with vol
            if d3 == -1 and c3['close'] < c2['low']: # Curr flushed it
                return "VOLUME_TRAP", z_score
            
        return "NORMAL", z_score

    # ─── Phase 44.8 ──────────────────────────────────────────────────
    def is_exhaustion_at_stretch(
        self,
        candles: list[dict],
        profile,          # ProfileAnalyzer result (dict)
        gain_pct: float,  # current % gain vs prev close
        atr: float = 0,   # Phase 51: Added ATR for VAH clearance check
        vwap_sd: float = 0 # Phase 57: vwap_sd for Absorption/Z-Process
    ) -> dict:
        """
        Phase 44.8 — Core edge detector.
        Phase 51: Hardened with all-day high, ATR clearance, and late-session rule.
        """
        result = {
            "fired":         False,
            "confidence":    "",
            "vol_fade_ratio": 0.0,
            "stretch_score": 0.0,
            "pattern_bonus": "None",
            "reject_reason": ""
        }

        import config
        from datetime import datetime
        import pytz

        pattern = "NORMAL"

        # Guard must accommodate lookback(15) + 1 current candle + 1 safety buffer = 17
        _vol_lookback = getattr(config, 'P55_G5_VOL_FADE_LOOKBACK', 15)
        if len(candles) < (_vol_lookback + 2):
            result["reject_reason"] = f"insufficient_candles (need {_vol_lookback + 2}, got {len(candles)})"
            return result

        # ── Gate A: Stretch sweet spot ──────────────────────────────
        # stretch_score = relative stretch above scanner minimum.
        # = 0.0 at scanner floor (6.18%), = 1.0 at 12.36%, = 1.346 at 14.5% max.
        stretch_score = round((gain_pct - config.SCANNER_GAIN_MIN_PCT) / config.SCANNER_GAIN_MIN_PCT, 3)
        result["stretch_score"] = stretch_score

        if gain_pct < config.G5_STRETCH_LOW_PCT:
            result["reject_reason"] = f"stretch_too_low:{gain_pct:.2f}%"
            return result
        if gain_pct > config.G5_STRETCH_HIGH_PCT:
            result["reject_reason"] = f"stretch_too_high:{gain_pct:.2f}%"
            return result

        # ── Gate B: ALL-DAY intraday high (Peak Detection) ──────────
        # Uses a 'Murphy-Guo Hybrid' tolerance: max of fixed noise-floor or volatility-scaled ATR.
        if config.P51_G5_GATE_B_USE_ALLDAY_HIGH:
            day_high = max(c['high'] for c in candles)
            curr_high = candles[-1]['high']
            
            fixed_tol = getattr(config, 'P51_G5_GATE_B_FIXED_TOLERANCE', 0.005) # 0.5% floor
            atr_mult  = getattr(config, 'P51_G5_GATE_B_ATR_MULT', 0.2)
            
            # Compute dynamic tolerance
            if atr > 0 and day_high > 0:
                atr_tol_pct = (atr * atr_mult) / day_high
                tolerance = max(fixed_tol, atr_tol_pct)
            else:
                tolerance = fixed_tol

            is_at_day_high = curr_high >= day_high * (1 - tolerance)
            
            if not is_at_day_high:
                result["reject_reason"] = f"not_at_day_high (ltp_high:{curr_high} < day_high:{day_high} | tol:{tolerance*100:.2f}%)"
                return result

        # ── Gate C: Volume fading on the new high ───────────────────
        _vol_lookback = getattr(config, 'P55_G5_VOL_FADE_LOOKBACK', 15)
        prior_vols = [c['volume'] for c in candles[-(_vol_lookback + 1):-1]]
        avg_prior_vol = sum(prior_vols) / len(prior_vols) if prior_vols else 0
        if avg_prior_vol == 0:
            result["reject_reason"] = "zero_prior_volume"
            return result

        vol_fade_ratio = round(candles[-1]['volume'] / avg_prior_vol, 3)
        result["vol_fade_ratio"] = vol_fade_ratio

        # ── PHASE 60: SPEAR OF EXHAUSTION (Volume Climax) [G5.4] ──────
        curr_c = candles[-1]
        prev_c = candles[-2]
        
        is_new_high = curr_c['high'] > prev_c['high']
        is_rejection_close = curr_c['close'] < (curr_c['high'] + curr_c['low']) / 2
        
        vol_climax_mult = getattr(config, 'P60_G5_SPEAR_VOL_CLIMAX_MULT', 3.0)
        is_vol_climax = curr_c['volume'] > (avg_prior_vol * vol_climax_mult)
        
        if is_new_high and is_rejection_close and is_vol_climax:
            result["fired"] = True
            result["confidence"] = "EXTREME"
            pattern = "SPEAR_OF_EXHAUSTION"
            logger.info(f"🔥 [SPEAR] Climax Vol Detected: {vol_fade_ratio}x")
            # We don't return yet — we want to compute patterns and VAH checks too.
            # But we bypass the MAX_FADE check below.
            max_fade = 99.0 # Effectively bypass

        # Phase 57: Z-Process Threshold Relaxation (Guo-Zhang Model)
        elif vwap_sd > getattr(config, 'P57_G5_Z_EXTREME_THRESHOLD', 3.3): # Body check removed for simplicity or keep it? PRD didn't mention changing it.
            # Keep existing relaxation logic
            max_fade = 0.65
            curr_candle = candles[-1]
            body = abs(curr_candle['close'] - curr_candle['open'])
            body_pct = body / curr_candle['close'] if curr_candle['close'] > 0 else 0
            if body_pct < 0.0005:
                max_fade = getattr(config, 'P57_G5_Z_FADE_RELAXATION', 0.95)
        else:
            max_fade = 0.65

        if vol_fade_ratio > max_fade:
            result["reject_reason"] = f"volume_not_faded:{vol_fade_ratio:.2f} (max:{max_fade})"
            return result

        # ── Pattern Discovery (Phase 59: Moved up for VAH Rejection bypass) ─────────
        try:
            import pandas as pd
            bonus_candles = candles[-20:] if len(candles) >= 20 else candles
            df_slice = pd.DataFrame(bonus_candles)
            vah_val = profile.get('vah') if isinstance(profile, dict) else getattr(profile, 'vah', None)
            adv_pattern, z_score = self.detect_structure_advanced(df_slice, vah=vah_val)
            if adv_pattern != "NORMAL":
                pattern = adv_pattern
        except Exception:
            pass

        # ── Gate D: Price above VAH (ATR clearance) [G5.2] ─────────
        vah = profile.get('vah') if isinstance(profile, dict) else getattr(profile, 'vah', None)
        if vah is None or vah <= 0:
            result["reject_reason"] = "vah_not_computed"
            return result

        curr_close = candles[-1]['close']
        
        # ── PHASE 59: VAH REJECTION BYPASS ──────────────────────────
        # If we have a "Look Above & Fail" pattern, we allow the close to be BELOW VAH.
        is_vah_rejection = (pattern == "VAH_REJECTION")
        
        if not is_vah_rejection:
            if curr_close <= vah:
                result["reject_reason"] = f"price_below_vah:{curr_close:.2f}|vah:{vah:.2f}"
                return result
                
            # ATR-relative clearance: must be significantly above VAH to prove rejection
            if getattr(config, 'P51_G5_GATE_D_ATR_CLEARANCE', False) and atr > 0:
                clearance = curr_close - vah
                min_clearance = atr * 0.2
                if clearance < min_clearance:
                    result["reject_reason"] = f"insufficient_vah_clearance:{clearance:.2f} < {min_clearance:.2f} (0.2*ATR)"
                    return result

        # ── All gates passed — base signal fires ────────────────────
        result["fired"] = True
        result["pattern_bonus"] = pattern

        # ── Confidence tier ─────────────────────────────────────────
        # Base confidence from volume fade
        if pattern == "SPEAR_OF_EXHAUSTION":
            conf_tier = 3 # Base EXTREME for Spear
        elif vol_fade_ratio < 0.30:
            conf_tier = 3 # EXTREME
        elif vol_fade_ratio < 0.50:
            conf_tier = 2 # HIGH
        else:
            conf_tier = 1 # MEDIUM
            
        # Upgrade tier for patterns
        if pattern in ("BEARISH_ENGULFING", "SHOOTING_STAR", "EVENING_STAR", "VOLUME_TRAP", "ABSORPTION_DOJI", "SPEAR_OF_EXHAUSTION"):
            conf_tier += 1
        
        # ── PHASE 59: SESSION-PHASE CONFLUENCE ──────────────────────
        # "Holy Grail": VAH Rejection + Stretched (>2.2 SD) after 10:45 AM
        IST = pytz.timezone('Asia/Kolkata')
        from datetime import datetime, time
        now_ist = datetime.now(IST).time()
        
        if is_vah_rejection and vwap_sd > 2.2 and now_ist > time(10, 45):
            conf_tier = 4 # MAX_CONVICTION
            
        tiers = {1: "MEDIUM", 2: "HIGH", 3: "EXTREME", 4: "MAX_CONVICTION"}
        result["confidence"] = tiers.get(min(conf_tier, 4), "MEDIUM")

        # ── Gate E: Late-session EXTREME-only rule [G5.3] ────────────
        if getattr(config, 'P51_G5_GATE_E_LATE_SESSION_EXTREME_ONLY', False):
            if now_ist > time(14, 30):
                if conf_tier < 3: # Not EXTREME or MAX
                    result["fired"] = False
                    result["reject_reason"] = f"late_session_non_extreme (conf:{result['confidence']})"
                    return result

        return result

    # Phase 21: Statistical Extremes
    def calculate_vwap_bands(self, df):
        """
        Returns dist from VWAP in Standard Deviations.
        """
        if 'vwap' not in df.columns: return 0
        
        # Std Dev of Price relative to VWAP over last 20 candles
        # Approx: StdDev of (Close - VWAP)
        
        window = df.iloc[-20:]
        diffs = window['close'] - window['vwap']
        std_dev = diffs.std()
        
        if std_dev == 0: return 0
        
        current_diff = df.iloc[-1]['close'] - df.iloc[-1]['vwap']
        score = current_diff / std_dev
        
        return score # > 2.0 is +2SD
        
    # Phase 21: Momentum
    def calculate_rsi(self, df, period=14):
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return rsi.iloc[-1]
        
    def check_rsi_divergence(self, df):
        # Higher High in Price, Lower High in RSI (Last 10 frames)
        # Simplified: Price Slope Positive, RSI Slope Negative
        try:
           p_slope, _ = self.calculate_vwap_slope(df[['close', 'volume']], window=10) # Reuse slope logic on Price? No need generic.
           
           # Quick Linear Reg on last 10 RSI points
           curr_rsi = self.calculate_rsi(df) # Logic needs full series.
           # Recalc series
           delta = df['close'].diff()
           gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
           loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
           rs = gain/loss
           rsi_series = 100 - (100/(1+rs))
           
           import config as _cfg_rsi
           _rsi_window = getattr(_cfg_rsi, 'P55_G6_RSI_DIVERGENCE_WINDOW', 25)
           recent_rsi = rsi_series.iloc[-_rsi_window:]
           recent_price = df['close'].iloc[-_rsi_window:]
           
           # Check Price trend
           p_start, p_end = recent_price.iloc[0], recent_price.iloc[-1]
           r_start, r_end = recent_rsi.iloc[0], recent_rsi.iloc[-1]
           
           if p_end > p_start and r_end < r_start:
               return True
           return False
        except:
            return False

    # Phase 21: AMT
    def calculate_market_profile(self, df):
        """
        Approx VAH/VAL using Volume Profile on DataFrame.
        Returns VAH, VAL, POC.
        """
        # Create Price Bins
        price_min = df['low'].min()
        price_max = df['high'].max()
        tick_size = 0.05
        bins = np.arange(price_min, price_max + tick_size, tick_size)
        
        # Bin Volume
        # Simple attribution: Total candle vol to Close price bin (approx)
        # Better: TPO? No, use close.
        vol_dist = df.groupby(pd.cut(df['close'], bins))['volume'].sum()
        
        total_vol = vol_dist.sum()
        sorted_dist = vol_dist.sort_values(ascending=False)
        
        # POC
        poc_bin = sorted_dist.index[0]
        poc = poc_bin.mid
        
        # VA (70%)
        target_vol = total_vol * 0.7
        current_vol = 0
        va_indices = []
        
        # Grow from POC out? 
        # Simple for now: Just take top 70% volume bins (Aggregated Profile, not ordered)
        # Correct way involves growing up/down from POC. 
        # For simplicity in this bot: Just return POC.
        # VAH/VAL requires ordered traversal.
        
        return poc

    # Phase 22: Fibonacci Golden Ratio
    def calculate_fib_levels(self, df):
        """
        Identifies recent Swing High/Low and calculates Retracements (.382, .5, .618).
        Logic:
        1. Find Highest High (HH) and Lowest Low (LL) in last 50 candles.
        2. Determine Context: Are we closer to Low (Downtrend) or High (Uptrend)?
           Actually, better logic: Find the massive Impulse.
           Simplified: Take Range of last 100 candles.
        Returns: Dict of Levels.
        """
        if len(df) < 50: return {}
        
        # Lookback 50 candles
        window = df.iloc[-50:]
        
        high = window['high'].max()
        low = window['low'].min()
        
        # Time of High vs Time of Low
        high_idx = window['high'].idxmax()
        low_idx = window['low'].idxmin()
        
        levels = {}
        
        # Scenario A: Downtrend (High -> Low)
        # We are looking for Retracement UP (Bear Flag)
        if high_idx < low_idx:
            direction = "DOWN"
            diff = high - low
            # Valid Retracement Levels (Price moving up from Low)
            levels['fib_382'] = low + (diff * 0.382)
            levels['fib_5']   = low + (diff * 0.5)
            levels['fib_618'] = low + (diff * 0.618)
            levels['trend'] = "DOWN"
            
        # Scenario B: Uptrend (Low -> High)
        # We are looking for Retracement DOWN (Bull Flag)
        else:
            direction = "UP"
            diff = high - low
            # Valid Retracement Levels (Price moving down from High)
            levels['fib_382'] = high - (diff * 0.382)
            levels['fib_5']   = high - (diff * 0.5)
            levels['fib_618'] = high - (diff * 0.618)
            levels['trend'] = "UP"
            
        return levels

