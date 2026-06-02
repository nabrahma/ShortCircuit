"""
analyzer.py — Thin orchestrator for the ShortCircuit strategy.

Responsibilities:
1. Data fetching (REST / local candle cache)
2. Enrichment (VWAP, ATR, slopes)
3. Pre-filters (candle count, circuit guard, gain floor, momentum blocker)
4. Delegation to BackToVWAPShort strategy for signal evaluation
5. Signal finalization (SL calc, signal dict, CSV/ML logging)

All feature computation lives in features.py.
All strategy logic lives in strategy/back_to_vwap.py.
"""

import csv
import datetime
import logging
import os
from typing import Optional, Dict, Any, Tuple

import pandas as pd

import config
import features as F
from gate_result_logger import GateResult, get_gate_result_logger
from htf_confluence import HTFConfluence
from market_context import MarketContext
from market_profile import ProfileAnalyzer
from ml_logger import get_ml_logger
from signal_manager import get_signal_manager
from strategy.back_to_vwap import BackToVWAPShort

logger = logging.getLogger(__name__)

SIGNAL_LOG_FILE = "logs/signals.csv"


def log_signal(symbol: str, ltp: float, pattern: str, stop_loss: float,
               meta: str = "", setup_high: float = 0.0,
               tick_size: float = 0.05, atr: float = 0.0,
               stretch_score: float = 0.0, vol_fade_ratio: float = 0.0,
               confidence: str = "", pattern_bonus: str = "None",
               oi_direction: str = "unknown"):
    """Persists signal details to a CSV file for EOD analysis."""
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
            symbol, ltp, pattern, stop_loss, meta, setup_high, tick_size, atr,
            stretch_score, vol_fade_ratio, confidence, pattern_bonus, oi_direction
        ])


class FyersAnalyzer:
    """
    Thin orchestrator: data fetch → enrich → pre-filter → strategy.evaluate() → finalize.
    """

    def __init__(self, fyers, broker=None, morning_high=None, morning_low=None):
        self.fyers = fyers
        self.broker = broker
        self.market_context = MarketContext(fyers, morning_high, morning_low)
        self.signal_manager = get_signal_manager()
        self.htf_confluence = HTFConfluence(fyers)
        self.profile_analyzer = ProfileAnalyzer()
        self.strategy = BackToVWAPShort()

    # ──────────────────────────────────────────────────────────────────
    # DATA FETCHING
    # ──────────────────────────────────────────────────────────────────

    def get_history(self, symbol: str, interval: str = "1") -> Optional[pd.DataFrame]:
        """
        Fetch intraday historical data for a symbol.
        Prefers local candle aggregator (1-minute). Falls back to REST.
        """
        # 1. Try local aggregator first (1-minute only)
        if interval == "1" and getattr(config, 'P82_LOCAL_CANDLES_ENABLED', False) and self.broker:
            n_bars = max(100, getattr(config, 'RVOL_MIN_CANDLES', 15) + 5)
            local_candles = self.broker.get_local_candles(symbol, n=n_bars)

            min_required = getattr(config, 'RVOL_MIN_CANDLES', 15) + 3
            if local_candles and len(local_candles) >= min_required:
                data = []
                for c in local_candles:
                    data.append([c.epoch, c.open, c.high, c.low, c.close, c.volume])
                cols = ["epoch", "open", "high", "low", "close", "volume"]
                df = pd.DataFrame(data, columns=cols)
                df['datetime'] = pd.to_datetime(
                    df['epoch'], unit='s'
                ).dt.tz_localize('UTC').dt.tz_convert('Asia/Kolkata')
                return df

        # 2. Fallback to REST
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
                df['datetime'] = pd.to_datetime(
                    df['epoch'], unit='s'
                ).dt.tz_localize('UTC').dt.tz_convert('Asia/Kolkata')
                return df
            else:
                logger.warning(f"No history data for {symbol}")
                return None
        except Exception as e:
            logger.error(f"Error fetching history for {symbol}: {e}")
            return None

    # ──────────────────────────────────────────────────────────────────
    # MAIN ENTRY POINT
    # ──────────────────────────────────────────────────────────────────

    def check_setup(
        self,
        symbol: str,
        ltp: float,
        oi: float = 0,
        pre_fetched_df: Optional[pd.DataFrame] = None,
        df_15m: Optional[pd.DataFrame] = None,
        scan_id: int = 0,
        data_tier: str = "UNKNOWN",
    ) -> Optional[Dict[str, Any]]:
        """
        Public API — called by main.py trading loop.
        Signature intentionally unchanged from the original for backward compat.
        """
        grl = get_gate_result_logger()
        gr = GateResult(symbol=symbol, scan_id=scan_id, data_tier=data_tier)
        signal_meta = {}

        # ── Data Fetch ───────────────────────────────────────────────
        if pre_fetched_df is not None:
            df = pre_fetched_df.copy()
        else:
            df = self.get_history(symbol)

        if df is None or df.empty:
            gr.verdict = "DATA_ERROR"
            gr.rejection_reason = "No history data available"
            grl.record(gr)
            return None

        # ── G2: Candle count guard ───────────────────────────────────
        if config.RVOL_VALIDITY_GATE_ENABLED and len(df) < config.RVOL_MIN_CANDLES:
            gr.g2_pass = False
            gr.g2_value = float(len(df))
            gr.verdict = "REJECTED"
            gr.first_fail_gate = "G2_RVOL_VALIDITY"
            gr.rejection_reason = (
                f"Only {len(df)} candles — need {config.RVOL_MIN_CANDLES} for valid RVOL"
            )
            logger.warning(
                "SKIP %s — RVOL_VALIDITY_GATE: Only %s candles — need %s",
                symbol, len(df), config.RVOL_MIN_CANDLES,
            )
            grl.record(gr)
            return None
        gr.g2_pass = True

        # ── Enrichment ───────────────────────────────────────────────
        F.enrich_dataframe(df)
        prev_df = df.iloc[:-1]

        atr = F.compute_atr(df)
        vwap_sd = F.compute_vwap_sd(prev_df)
        slope_30m, _ = F.compute_vwap_slope(df.iloc[-30:], window=30)
        slope_5m, _ = F.compute_vwap_slope(df.iloc[-5:], window=5)

        # Decay trigger: fast slope drops below slow slope
        is_decaying = False
        if getattr(config, 'P66_ADAPTIVE_G1_ENABLED', False):
            decay_sd = getattr(config, 'P66_G4_DECAY_SD_THRESHOLD', 2.0)
            if slope_5m < (slope_30m * 0.90) and vwap_sd > decay_sd:
                is_decaying = True
                logger.info(
                    "⚡ [INFLECTION] %s Decaying: Fast(%.2f) < Slow(%.2f)",
                    symbol, slope_5m, slope_30m,
                )

        # ── Gain Calculation ─────────────────────────────────────────
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

        # ── Profile Pre-calc ─────────────────────────────────────────
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

        # ── G7: Market Regime & Time Gate ─────────────────────────────
        allowed, reason = self.market_context.evaluate_g7(
            vwap_sd=vwap_sd,
            profile_rejection=profile_rejection,
            volume_z=vol_z,
        )
        gr.g7_pass = allowed
        gr.g7_value = reason
        if not allowed:
            gr.verdict = "REJECTED"
            gr.first_fail_gate = "G7_REGIME"
            gr.rejection_reason = reason
            grl.record(gr)
            return None

        # ── G1: Gain Floor & Kill Backdoor ────────────────────────────
        min_gain = getattr(config, 'P65_G1_NORMAL_THRESHOLD', 9.0)
        amt_failing_auction = False
        if getattr(config, 'P65_AMT_ENABLED', False):
            if profile_rejection and gain_pct >= config.P65_G1_NET_GAIN_THRESHOLD:
                min_gain = config.P65_G1_NET_GAIN_THRESHOLD
                amt_failing_auction = True

        # Kill backdoor: price too far from high
        kill_blocked, kill_msg = F.check_kill_backdoor(
            ltp, day_high, atr=atr,
            fixed_pct=getattr(config, 'P51_G1_KILL_BACKDOOR_FIXED_PCT', 0.015),
            atr_mult=getattr(config, 'P51_G1_KILL_BACKDOOR_ATR_MULT', 0.3),
        )

        # Time-since-high
        time_ok = F.check_time_since_high(
            df, max_candles=getattr(config, 'P65_G1_TIME_SINCE_HIGH_CANDLES', 25)
        )

        g1_pass = True
        g1_msg = "PASSED"

        if gain_pct < min_gain:
            g1_pass = False
            g1_msg = (
                f"Insufficient Gain: {gain_pct:.1f}% "
                f"(need {min_gain}% {'with AMT' if amt_failing_auction else ''})"
            )
        elif getattr(config, 'P51_G1_KILL_BACKDOOR', True) and kill_blocked:
            g1_pass = False
            g1_msg = kill_msg
        elif not time_ok:
            g1_pass = False
            g1_msg = "G1 Time Gate: high too stale"

        gr.g1_pass = g1_pass
        gr.g1_value = round(gain_pct, 2)
        if not g1_pass:
            gr.verdict = "REJECTED"
            gr.first_fail_gate = "G1_GAIN_CONSTRAINTS"
            gr.rejection_reason = g1_msg
            grl.record(gr)
            return None

        # ── G3: Circuit Guard & Blacklist ─────────────────────────────
        depth_data = None
        try:
            full_depth = self.fyers.depth(data={"symbol": symbol, "ohlcv_flag": "1"})
            if 'd' in full_depth and symbol in full_depth['d']:
                depth_data = full_depth['d'][symbol]
                uc = depth_data.get('upper_ckt', 0)
                if uc > 0 and ltp >= uc * 0.999:
                    self.market_context.mark_circuit_touched(symbol)
        except Exception:
            pass

        is_blacklisted = self.market_context.is_circuit_hitter(symbol)
        circuit_blocked = is_blacklisted or self._check_circuit_guard(symbol, ltp, depth_data)

        gr.g3_pass = not circuit_blocked
        gr.g3_value = "BLACKLISTED" if is_blacklisted else round(ltp, 2)
        if circuit_blocked:
            gr.verdict = "REJECTED"
            gr.first_fail_gate = "G3_CIRCUIT_GUARD"
            gr.rejection_reason = (
                "Circuit Hitter (Session Blacklist)" if is_blacklisted
                else f"LTP {ltp} too close to upper circuit"
            )
            grl.record(gr)
            return None

        # ── G4: Momentum Guard ────────────────────────────────────────
        momentum_blocked = self._is_momentum_too_strong(
            df, slope_5m, slope_30m, vwap_sd, symbol, gain_pct
        )
        gr.g4_pass = not momentum_blocked
        gr.g4_value = round(slope_5m, 3)
        if momentum_blocked:
            gr.verdict = "REJECTED"
            gr.first_fail_gate = "G4_MOMENTUM"
            gr.rejection_reason = f"Momentum too strong (slope={slope_5m:.2f})"
            grl.record(gr)
            return None

        # ── STRATEGY EVALUATION (replaces G5 + G6) ────────────────────
        result = self.strategy.evaluate(
            symbol=symbol,
            ltp=ltp,
            df=df,
            profile=profile,
            profile_rejection=profile_rejection,
            vwap_sd=vwap_sd,
            atr=atr,
            gain_pct=gain_pct,
            slope_fast=slope_5m,
            slope_slow=slope_30m,
            is_decaying=is_decaying,
        )

        if result is None:
            gr.g5_pass = False
            gr.verdict = "REJECTED"
            gr.first_fail_gate = "G5_STRATEGY"
            gr.rejection_reason = "BackToVWAPShort conditions not met"
            grl.record(gr)
            return None

        gr.g5_pass = True
        gr.g5_value = round(result.get('stretch_score', 0), 3)
        gr.g6_pass = True
        gr.g6_value = f"+{result['confidence']}"

        signal_meta.update(result)
        pattern_desc = result.get('pattern_bonus', 'EXHAUSTION_FADE')

        # ── G9: HTF Confluence ────────────────────────────────────────
        import concurrent.futures
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _htf_exec:
                _htf_future = _htf_exec.submit(
                    self.htf_confluence.check_trend_exhaustion,
                    symbol, df_15m=df_15m, vwap_sd=vwap_sd,
                )
                htf_ok, htf_msg = _htf_future.result(timeout=1.5)
        except Exception as e:
            htf_ok, htf_msg = True, f"HTF_BYPASS:{e}"

        gr.g9_pass = htf_ok
        gr.g9_value = htf_msg

        # Promotion: Decay + HTF Stall → upgrade to EXTREME
        if htf_ok and is_decaying and signal_meta.get('confidence') == 'HIGH' and vwap_sd > 2.0:
            signal_meta['confidence'] = 'EXTREME'
            signal_meta['pattern_bonus'] = f"{pattern_desc} + PROMOTED"
            logger.info(
                "⭐ [PROMOTION] %s upgraded to EXTREME (Decay + G9 Stall confirmed)", symbol
            )

        if not htf_ok:
            gr.verdict = "REJECTED"
            gr.first_fail_gate = "G9_HTF_CONFLUENCE"
            gr.rejection_reason = f"HTF blocked: {htf_msg}"
            grl.record(gr)
            return None

        # ── G8: Signal Manager (cooldown + daily target) ──────────────
        sm = self.signal_manager
        confidence = signal_meta.get('confidence', '')
        can_signal, sm_reason = (
            sm.can_signal(symbol, confidence=confidence)
            if hasattr(sm, 'can_signal') else (True, "")
        )

        gr.g8_pass = can_signal
        gr.g8_value = None
        if not can_signal:
            gr.verdict = "REJECTED"
            gr.first_fail_gate = "G8_SIGNAL_MANAGER"
            gr.rejection_reason = sm_reason
            grl.record(gr)
            return None

        # ── G13: Risk & Reward (TP scaling) ───────────────────────────
        if gain_pct < 9.0:
            signal_meta['tp_atr_mult_override'] = getattr(
                config, 'P78_SINGLE_TP_ATR_MULT_LOW_GAIN', 0.5
            )
        else:
            signal_meta['tp_atr_mult_override'] = getattr(
                config, 'P78_SINGLE_TP_ATR_MULT_DEFAULT', 1.0
            )

        signal_meta['snapshot_high'] = day_high

        # ── Finalize ──────────────────────────────────────────────────
        gr.verdict = "ANALYZER_PASS"
        finalized = self._finalize_signal(
            symbol, ltp, df, pattern_desc, slope_5m, "", signal_meta
        )
        if finalized:
            finalized['_gate_result'] = gr
        else:
            gr.verdict = "DATA_ERROR"

        grl.record(gr)
        return finalized

    # ──────────────────────────────────────────────────────────────────
    # PRIVATE HELPERS (kept)
    # ──────────────────────────────────────────────────────────────────

    def _check_circuit_guard(self, symbol: str, ltp: float, quote: dict = None) -> bool:
        """Safety: blocks trade if price is too close to upper/lower circuit."""
        try:
            if not quote:
                depth_data = self.fyers.depth(data={"symbol": symbol, "ohlcv_flag": "1"})
                if 'd' in depth_data:
                    quote = depth_data['d'].get(symbol, {})

            if quote:
                uc = quote.get('upper_ckt', 0)
                lc = quote.get('lower_ckt', 0)

                if uc > 0:
                    buffer_price = uc * 0.985
                    if ltp >= buffer_price:
                        logger.warning(
                            "🛑 CIRCUIT GUARD: %s @ %s (Too close to UC %s)", symbol, ltp, uc
                        )
                        return True

                if lc > 0 and ltp <= lc * 1.005:
                    return True
        except Exception as e:
            logger.error(f"Circuit Check Error: {e}")

        return False

    def _is_momentum_too_strong(
        self, df: pd.DataFrame, slope_fast: float, slope_slow: float,
        vwap_sd: float, symbol: str, gain_pct: float = 0.0,
    ) -> bool:
        """Checks if momentum is too strong to short (dual-window)."""
        try:
            recent_vols = df['volume'].iloc[-20:-1]
            avg_v = recent_vols.mean()
            curr_v = df['volume'].iloc[-1]
            rvol_now = curr_v / avg_v if avg_v > 0 else 0

            rvol_thresh = config.P51_G4_RVOL_THRESHOLD if config.PHASE_51_ENABLED else 5.0
            slope_thresh = config.P51_G4_SLOPE_MIN if config.PHASE_51_ENABLED else 3.0

            if rvol_now > rvol_thresh:
                logger.info(
                    "📈 [CLIMAX] %s RVOL peak: %.1fx — tracking for fade", symbol, rvol_now
                )
                # Allow through — strategy checks volume fade downstream

            # Inflection check: fast slope dropping below slow
            if getattr(config, 'P57_G4_SLOPE_DECAY_ENABLED', False):
                div_thresh = getattr(config, 'P57_G4_DIVERGENCE_SD', 1.5)
                structural = gain_pct > getattr(config, 'P60_G4_STRUCTURAL_FALLBACK_GAIN', 10.0)

                if slope_fast < (slope_slow * 0.90) and (vwap_sd > div_thresh or structural):
                    logger.info(
                        "✅ [MOMENTUM DECAY] %s allowed via Inflection "
                        "(Fast:%.2f < Slow:%.2f)", symbol, slope_fast, slope_slow,
                    )
                    return False

                if slope_slow > slope_thresh:
                    logger.warning(
                        "  MOMENTUM BLOCK %s Slow Slope %.1f (> %.1f) — NOT INFLECTED",
                        symbol, slope_slow, slope_thresh,
                    )
                    return True

        except Exception:
            pass
        return False

    def _is_post_daily_target(self) -> bool:
        """Returns True if the daily P&L target has been reached."""
        try:
            daily_target = getattr(config, 'DAILY_TARGET_INR', 0)
            if daily_target == -1:
                daily_target = self.signal_manager.daily_target_inr
            return daily_target > 0 and self.signal_manager.daily_pnl >= daily_target
        except Exception:
            return False

    def _finalize_signal(
        self, symbol, ltp, df, pattern_desc, slope, wall_msg, signal_meta: dict = None,
    ):
        """Calculates SL, builds signal dict, logs to CSV and ML. Pure — no gate checks."""
        if signal_meta is None:
            signal_meta = {}

        # Calculate Stop Loss (ATR-based)
        atr = F.compute_atr(df)
        buffer = max(atr * 0.5, 0.25)

        # Use absolute high snapshot for SL
        peak_high = signal_meta.get('snapshot_high', df.iloc[-2]['high'])
        setup_high = peak_high
        sl_price = setup_high + buffer

        # Logging
        logger.info(f"[OK] SIGNAL: {symbol} | {pattern_desc}")

        meta_str = f"Slope:{slope:.1f}, ATR:{atr:.2f}"
        log_signal(
            symbol, ltp, pattern_desc, sl_price, meta_str,
            setup_high=setup_high,
            tick_size=signal_meta.get('tick_size', 0.05),
            atr=atr,
            stretch_score=signal_meta.get('stretch_score', 0.0),
            vol_fade_ratio=signal_meta.get('vol_fade_ratio', 0.0),
            confidence=signal_meta.get('confidence', ''),
            pattern_bonus=signal_meta.get('pattern_bonus', 'None'),
            oi_direction=signal_meta.get('oi_direction', 'unknown'),
        )

        # ML Data Logging
        obs_id = None
        try:
            ml_logger = get_ml_logger()
            prev_candle = df.iloc[-2]

            body = abs(prev_candle['close'] - prev_candle['open'])
            total_range = prev_candle['high'] - prev_candle['low']
            upper_wick = prev_candle['high'] - max(prev_candle['open'], prev_candle['close'])
            lower_wick = min(prev_candle['open'], prev_candle['close']) - prev_candle['low']

            vwap = df['vwap'].iloc[-1] if 'vwap' in df.columns else ltp
            vwap_dist = ((ltp - vwap) / vwap) * 100 if vwap > 0 else 0

            vol_avg = df['volume'].iloc[-20:].mean() if len(df) > 20 else df['volume'].mean()
            rvol = prev_candle['volume'] / vol_avg if vol_avg > 0 else 1

            features = {
                "prev_close": df.iloc[0]['open'],
                "day_high": df['high'].max(),
                "day_low": df['low'].min(),
                "gain_pct": ((ltp - df.iloc[0]['open']) / df.iloc[0]['open']) * 100,
                "vwap": vwap,
                "vwap_distance_pct": vwap_dist,
                "vwap_sd": F.compute_vwap_sd(df.iloc[:-1]),
                "vwap_slope": slope,
                "volume_current": prev_candle['volume'],
                "volume_avg_20": vol_avg,
                "rvol": rvol,
                "pattern": pattern_desc.split(" + ")[0],
                "candle_body_pct": (body / total_range * 100) if total_range > 0 else 0,
                "upper_wick_pct": (upper_wick / total_range * 100) if total_range > 0 else 0,
                "lower_wick_pct": (lower_wick / total_range * 100) if total_range > 0 else 0,
                "num_confirmations": pattern_desc.count(",") + 1 if "+" in pattern_desc else 0,
                "confirmations": pattern_desc.split(" + ")[1:] if " + " in pattern_desc else [],
                "nifty_trend": (
                    self.market_context.get_trend_label()
                    if hasattr(self.market_context, 'get_trend_label') else "UNKNOWN"
                ),
                "atr": atr,
                "sl_price": sl_price,
                "tp_price": ltp * 0.99,
                "direction": getattr(config, "TRADE_DIRECTION", "SHORT"),
            }

            obs_id = ml_logger.log_observation(symbol, ltp, features)
            logger.info(f"   [ML] Logged observation: {obs_id}")
        except Exception as e:
            logger.warning(f"   [ML] Logging error: {e}")

        # Build signal dict
        signal_data = {
            'symbol': symbol,
            'ltp': ltp,
            'pattern': pattern_desc,
            'stop_loss': sl_price,
            'day_high': df['high'].max(),
            'signal_low': df.iloc[-2]['low'],
            'setup_high': setup_high,
            'signal_high': setup_high,
            'tick_size': signal_meta.get('tick_size', 0.05),
            'atr': atr,
            'meta': meta_str,
            'obs_id': obs_id,
        }

        # Quality fields
        signal_data["stretch_score"] = signal_meta.get("stretch_score", 0.0)
        signal_data["vol_fade_ratio"] = signal_meta.get("vol_fade_ratio", 0.0)
        signal_data["confidence"] = signal_meta.get("confidence", "")
        signal_data["pattern_bonus"] = signal_meta.get("pattern_bonus", "None")
        signal_data["oi_direction"] = signal_meta.get("oi_direction", "unknown")

        # TP scaling override
        if 'tp_atr_mult_override' in signal_meta:
            signal_data['tp_atr_mult_override'] = signal_meta['tp_atr_mult_override']

        return signal_data
