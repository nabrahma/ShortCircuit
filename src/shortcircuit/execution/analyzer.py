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

import concurrent.futures
import csv
import datetime
from datetime import time as dtime
import logging
import os
from typing import Optional, Dict, Any, Tuple

import pandas as pd

from shortcircuit import config
from shortcircuit.strategy import features as F
from shortcircuit.observability.gate_result_logger import GateResult, get_gate_result_logger
from shortcircuit.strategy.htf_confluence import HTFConfluence
from shortcircuit.strategy.market_context import MarketContext
from shortcircuit.strategy.market_profile import ProfileAnalyzer
from shortcircuit.observability.ml_logger import get_ml_logger
from shortcircuit.execution.signal_manager import get_signal_manager
from shortcircuit.strategy.back_to_vwap import BackToVWAPShort

logger = logging.getLogger(__name__)

SIGNAL_LOG_FILE = "logs/signals.csv"

# Shared pool for the G9 higher-timeframe check. Sized for the scanner's parallel
# candidate analysis; daemon threads so it can never hold up interpreter shutdown.
_HTF_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="htf-confluence"
)


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



# 09:15 to 15:30 IST is 375 one-minute bars. Request the full session so the
# cumulative VWAP in features.enrich_dataframe() is anchored to the open rather
# than to wherever a truncated frame happens to begin.
SESSION_OPEN_IST = dtime(9, 15)
SESSION_BARS_1M = 400          # 375 + slack; the aggregator's deque holds 500


def frame_reaches_session_open(df: pd.DataFrame) -> bool:
    """
    True when the frame's first bar is at or before the session open, so a
    cumulative VWAP over it is the session VWAP.

    One minute of slack: the 09:15 bar can be stamped 09:15:00 or arrive as the
    first tick a few seconds later.
    """
    if df is None or df.empty or 'datetime' not in df.columns:
        return False
    try:
        first = df['datetime'].iloc[0]
        return first.time() <= dtime(9, 16)
    except Exception:
        return False


class FyersAnalyzer:
    """
    Thin orchestrator: data fetch → enrich → pre-filter → strategy.evaluate() → finalize.
    """

    def __init__(self, fyers, broker=None, morning_high=None, morning_low=None):
        self.fyers = fyers
        self.broker = broker
        self.market_context = MarketContext(fyers, morning_high, morning_low, broker=broker)
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
        #
        # BUG-2026-08-12 (C1 was measuring the wrong thing):
        # features.enrich_dataframe() computes a CUMULATIVE vwap over whatever
        # frame it is handed:
        #     df['vwap'] = (tp * v).cumsum() / v.cumsum()
        # That is only the session VWAP if the frame starts at the session open.
        # This asked for 100 bars, so the anchor slid forward every minute and
        # C1 measured stretch against a ~100-minute rolling VWAP instead.
        #
        # NSE:ORISSAMINE-EQ on 12 Aug is the worked example. At 11:56 IST the
        # anchor sat at ~10:16 — the day's peak — so price read as 1.52 SD BELOW
        # VWAP while a session-anchored VWAP had it above. Every one of that
        # day's 295 WS_CACHE scans used this path. The REST fallback below
        # fetches range_from=today and was always correct, so the definition of
        # "VWAP" silently depended on which data tier answered.
        if interval == "1" and getattr(config, 'P82_LOCAL_CANDLES_ENABLED', False) and self.broker:
            local_candles = self.broker.get_local_candles(symbol, n=SESSION_BARS_1M)

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

                # Asking for enough bars is necessary but not sufficient: the
                # aggregator only holds candles observed since the process
                # started. After a mid-session restart the buffer begins at
                # restart time, and its cumulative VWAP would be anchored there.
                # Fall through to REST in that case — it returns the whole
                # session, which is the only anchor C1 is defined against.
                if frame_reaches_session_open(df):
                    return df
                logger.debug(
                    "[HISTORY] %s local buffer starts %s, past the open — "
                    "using REST so VWAP stays session-anchored",
                    symbol, df['datetime'].iloc[0].strftime('%H:%M'),
                )

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
        decay_sd = 2.0
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

        allowed, reason = self.market_context.is_safe_trade_window()
        gr.g7_pass = allowed
        gr.g7_value = reason
        if not allowed:
            gr.verdict = "REJECTED"
            gr.first_fail_gate = "G7_REGIME"
            gr.rejection_reason = reason
            grl.record(gr)
            return None

        # ── Pre-fetch Depth for Strategy ─────────────────────────────
        upper_circuit = 0.0
        lower_circuit = 0.0
        spread_pct = 0.0
        is_circuit_hitter = False
        try:
            full_depth = self.fyers.depth(data={"symbol": symbol, "ohlcv_flag": "1"})
            if 'd' in full_depth and symbol in full_depth['d']:
                depth_data = full_depth['d'][symbol]
                upper_circuit = depth_data.get('upper_ckt', 0)
                lower_circuit = depth_data.get('lower_ckt', 0)
                
                if upper_circuit > 0 and ltp >= upper_circuit * 0.999:
                    self.market_context.mark_circuit_touched(symbol)
                
                # Spread
                ask = depth_data['ask'][0]['price'] if depth_data.get('ask') else ltp
                bid = depth_data['bid'][0]['price'] if depth_data.get('bid') else ltp
                if ltp > 0:
                    spread_pct = (ask - bid) / ltp
                    
        except Exception:
            pass

        is_circuit_hitter = self.market_context.is_circuit_hitter(symbol)

        # ── STRATEGY EVALUATION (Replaces G1-G6) ────────────────────
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
            upper_circuit=upper_circuit,
            lower_circuit=lower_circuit,
            spread_pct=spread_pct,
            is_circuit_hitter=is_circuit_hitter,
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
        # Runs on a shared, long-lived pool. The previous version built a new
        # ThreadPoolExecutor per symbol per scan and used it as a context manager,
        # so __exit__ called shutdown(wait=True) — meaning a timed-out G9 check
        # still blocked here until the worker finished, defeating the timeout it
        # was written to enforce. It also churned thread creation across every
        # candidate on every 60s cycle.
        try:
            _htf_future = _HTF_EXECUTOR.submit(
                self.htf_confluence.check_trend_exhaustion,
                symbol, df_15m=df_15m, vwap_sd=vwap_sd,
            )
            htf_ok, htf_msg = _htf_future.result(timeout=1.5)
        except concurrent.futures.TimeoutError:
            # Abandon the worker rather than waiting on it; G9 is fail-closed.
            htf_ok, htf_msg = False, "G9 BLOCK: HTF check timed out (1.5s)"
        except Exception as e:
            htf_ok, htf_msg = False, f"G9 BLOCK: {e}"

        gr.g9_pass = htf_ok
        gr.g9_value = htf_msg

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

        # ── Reward Risk ───────────────────────────
        if gain_pct < 9.0:
            signal_meta['tp_atr_mult_override'] = 0.5
        else:
            signal_meta['tp_atr_mult_override'] = 1.0

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

        # Calculate VWAP for both ML logging and TP targeting
        vwap = df['vwap'].iloc[-1] if 'vwap' in df.columns else ltp

        # ML Data Logging
        obs_id = None
        try:
            ml_logger = get_ml_logger()
            prev_candle = df.iloc[-2]

            body = abs(prev_candle['close'] - prev_candle['open'])
            total_range = prev_candle['high'] - prev_candle['low']
            upper_wick = prev_candle['high'] - max(prev_candle['open'], prev_candle['close'])
            lower_wick = min(prev_candle['open'], prev_candle['close']) - prev_candle['low']

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
                "stretch_score": signal_meta.get('stretch_score', 0.0),
                "vol_fade_ratio": signal_meta.get('vol_fade_ratio', 0.0),
                "confidence": signal_meta.get('confidence', 'MEDIUM'),
                "pattern_bonus": signal_meta.get('pattern_bonus', 'None'),
                "num_confirmations": pattern_desc.count(",") + 1 if "+" in pattern_desc else 0,
                "confirmations": pattern_desc.split(" + ")[1:] if " + " in pattern_desc else [],
                "nifty_trend": (
                    self.market_context.get_trend_label()
                    if hasattr(self.market_context, 'get_trend_label') else "UNKNOWN"
                ),
                "atr": atr,
                "sl_price": sl_price,
                "tp_price": vwap, # Update TP price in ML logs to reflect VWAP target
                "direction": getattr(config, "TRADE_DIRECTION", "SHORT"),
                "leverage": getattr(config, 'INTRADAY_LEVERAGE', 5.0),
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
            'vwap': vwap,
            'meta': meta_str,
            'obs_id': obs_id,
            'leverage': getattr(config, 'INTRADAY_LEVERAGE', 5.0),
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
