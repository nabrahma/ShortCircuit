"""
Phase 42.2: Diagnostic Analyzer (Missed Opportunity Tool)

Purpose: Re-run the 12-gate signal pipeline in "diagnostic mode".
         Unlike the normal analyzer that stops at the first failure,
         this runs ALL gates and reports detailed pass/fail reasons
         so the user can understand WHY a stock was not signaled.

Usage (CLI):   python eod_why.py RELIANCE 14:25
Usage (Telegram): /why RELIANCE 14:25
"""

import logging
import os
import csv
from datetime import datetime, timedelta

import pandas as pd

import config
from god_mode_logic import GodModeAnalyst
from market_context import MarketContext
from signal_manager import get_signal_manager
from htf_confluence import HTFConfluence
from tape_reader import TapeReader
from market_profile import ProfileAnalyzer

logger = logging.getLogger(__name__)


class DiagnosticAnalyzer:
    """
    Diagnostic version of the signal pipeline.
    Reports WHY a stock was rejected (or why it SHOULD have been signaled).
    """

    def __init__(self, fyers):
        self.fyers = fyers
        self.gm_analyst = GodModeAnalyst()
        self.market_context = MarketContext(fyers)
        self.signal_manager = get_signal_manager()
        self.htf_confluence = HTFConfluence(fyers)
        self.tape_reader = TapeReader()
        self.profile_analyzer = ProfileAnalyzer()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  MAIN ENTRY POINT
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def analyze_missed_opportunity(self, symbol: str, timestamp_str: str) -> dict:
        """
        Analyze why a stock was NOT signaled at a given time.

        Args:
            symbol: Stock symbol (e.g., 'RELIANCE' or 'NSE:RELIANCE-EQ')
            timestamp_str: Time to analyze (e.g., '14:25' or '2026-02-15 14:25:00')

        Returns:
            dict with gate results, first failure, and profitability check
        """
        logger.info(f"🔍 Diagnostic Analysis: {symbol} @ {timestamp_str}")

        # Parse timestamp
        if len(timestamp_str) <= 5:  # Format: '14:25'
            timestamp_str = f"{datetime.now().strftime('%Y-%m-%d')} {timestamp_str}:00"

        analysis_time = pd.to_datetime(timestamp_str)

        # Normalize symbol
        if ':' not in symbol:
            symbol = f"NSE:{symbol}-EQ"

        # Fetch historical data at that time
        data = self._fetch_historical_snapshot(symbol, analysis_time)

        if data is None:
            return {
                'error': 'Could not fetch historical data for this symbol/time.',
                'symbol': symbol,
                'timestamp': analysis_time
            }

        # Run ALL gates (diagnostic mode — never short-circuits)
        gate_results = []
        first_failure = None

        gates = [
            (1, lambda: self._check_gate1_signal_manager(symbol)),
            (2, lambda: self._check_gate2_market_regime()),
            (3, lambda: self._check_gate3_data_pipeline(data)),
            (4, lambda: self._check_gate4_technical_context(data)),
            (5, lambda: self._check_gate5_hard_constraints(data)),
            (6, lambda: self._check_gate6_circuit_guard(symbol, data)),
            (7, lambda: self._check_gate7_momentum_safeguard(data)),
            (8, lambda: self._check_gate8_pattern(data)),
            (9, lambda: self._check_gate9_breakdown(data)),
            (10, lambda: self._check_gate10_confluence(symbol, data)),
            (11, lambda: self._check_gate11_htf(symbol, data['ltp'])),
            (12, lambda: self._check_gate12_finalization(data)),
        ]

        for gate_num, check_fn in gates:
            try:
                result = check_fn()
            except Exception as e:
                result = {
                    'gate_num': gate_num,
                    'name': f'Gate {gate_num}',
                    'status': 'ERROR',
                    'reason': str(e)
                }
            gate_results.append(result)
            if result['status'] == 'FAILED' and first_failure is None:
                first_failure = gate_num

        if first_failure is None:
            first_failure = 13  # Passed all gates

        # Check profitability (what happened 30 min later?)
        profitability = self._check_profitability(symbol, analysis_time, data['ltp'])

        # Log diagnostic to CSV
        self._log_diagnostic(symbol, analysis_time, first_failure, gate_results, profitability)

        return {
            'symbol': symbol,
            'timestamp': analysis_time,
            'ltp_at_analysis': data['ltp'],
            'day_gain': data['change_pct'],
            'day_high': data['day_high'],
            'gates': gate_results,
            'first_failure_gate': first_failure,
            'passed_all_gates': first_failure == 13,
            'profitability': profitability
        }

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  DATA FETCHING
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _fetch_historical_snapshot(self, symbol: str, timestamp: datetime) -> dict:
        """
        Fetch data as it appeared at the given timestamp.
        Reconstructs: LTP, day high/low/open, gain %, volume, and 1-min candle DF.
        """
        market_open = timestamp.replace(hour=9, minute=15, second=0)

        data = {
            'symbol': symbol,
            'resolution': '1',
            'date_format': '1',
            'range_from': market_open.strftime('%Y-%m-%d'),
            'range_to': timestamp.strftime('%Y-%m-%d'),
            'cont_flag': '1'
        }

        try:
            response = self.fyers.history(data=data)

            if response.get('s') != 'ok' or not response.get('candles'):
                logger.error(f"Could not fetch historical data for {symbol}")
                return None

            df = pd.DataFrame(
                response['candles'],
                columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
            )
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')

            # Filter to only candles up to analysis time
            df = df[df['timestamp'] <= timestamp]

            if df.empty:
                return None

            last_candle = df.iloc[-1]
            first_candle = df.iloc[0]

            ltp = last_candle['close']
            day_high = df['high'].max()
            day_open = first_candle['open']

            # Enrich with VWAP
            df_copy = df.copy()
            if 'volume' in df_copy.columns and df_copy['volume'].sum() > 0:
                typical_price = (df_copy['high'] + df_copy['low'] + df_copy['close']) / 3
                cum_tp_vol = (typical_price * df_copy['volume']).cumsum()
                cum_vol = df_copy['volume'].cumsum()
                df_copy['vwap'] = cum_tp_vol / cum_vol.replace(0, 1)

            return {
                'ltp': ltp,
                'day_high': day_high,
                'day_low': df['low'].min(),
                'day_open': day_open,
                'prev_close': day_open,  # Approximation (not actual prev close)
                'change_pct': ((ltp - day_open) / day_open) * 100,
                'max_gain_pct': ((day_high - day_open) / day_open) * 100,
                'volume': df['volume'].sum(),
                'df': df_copy,
                'tick_size': 0.05
            }

        except Exception as e:
            logger.error(f"Exception fetching historical data: {e}")
            return None

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  GATE CHECKS (diagnostic mode — detailed output)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _check_gate1_signal_manager(self, symbol: str) -> dict:
        """Gate 1: Signal Manager (daily limit, cooldown, pause)."""
        can_trade, reason = self.signal_manager.can_signal(symbol)
        status_info = self.signal_manager.get_status()

        details = {
            'daily_signals': len(self.signal_manager.signals_today),
            'daily_limit': self.signal_manager.max_signals_per_day,
            'remaining': status_info['signals_remaining'],
            'is_paused': status_info['is_paused'],
            'consecutive_losses': status_info['consecutive_losses'],
        }

        suggestion = None
        if not can_trade:
            if 'Daily limit' in reason:
                suggestion = f"Daily signal limit ({details['daily_limit']}) reached. Increase MAX_SIGNALS_PER_DAY."
            elif 'Cooldown' in reason:
                suggestion = "Symbol is in cooldown period. Wait or reduce COOLDOWN_MINUTES."
            elif 'paused' in reason:
                suggestion = f"Trading paused after {details['consecutive_losses']} consecutive losses."

        return {
            'gate_num': 1,
            'name': 'Signal Manager',
            'status': 'PASSED' if can_trade else 'FAILED',
            'reason': reason,
            'details': details,
            'suggestion': suggestion
        }

    def _check_gate2_market_regime(self) -> dict:
        """Gate 2: Market Regime (Nifty trend check)."""
        allow_short, reason = self.market_context.should_allow_short()
        regime, regime_msg = self.market_context.get_market_regime()

        suggestion = None
        if not allow_short:
            suggestion = "Market is in TREND_UP mode — all shorts are blocked. Wait for regime to shift to RANGE or TREND_DOWN."

        return {
            'gate_num': 2,
            'name': 'Market Regime',
            'status': 'PASSED' if allow_short else 'FAILED',
            'reason': reason,
            'details': {
                'nifty_regime': regime,
                'regime_detail': regime_msg,
            },
            'suggestion': suggestion
        }

    def _check_gate3_data_pipeline(self, data: dict) -> dict:
        """Gate 3: Data Pipeline — is candle history available and sufficient?"""
        df = data.get('df')
        candle_count = len(df) if df is not None else 0
        has_enough = candle_count >= 10  # Need at least 10 candles for analysis

        return {
            'gate_num': 3,
            'name': 'Data Pipeline',
            'status': 'PASSED' if has_enough else 'FAILED',
            'reason': 'OK' if has_enough else f'Only {candle_count} candles available (need ≥10)',
            'details': {'candle_count': candle_count},
            'suggestion': 'Insufficient historical data. Stock may have been recently listed or had a trading halt.' if not has_enough else None
        }

    def _check_gate4_technical_context(self, data: dict) -> dict:
        """Gate 4: Technical Context — VWAP calculation."""
        df = data.get('df')
        has_vwap = df is not None and 'vwap' in df.columns
        vwap_value = df['vwap'].iloc[-1] if has_vwap else 0

        return {
            'gate_num': 4,
            'name': 'Technical Context (VWAP)',
            'status': 'PASSED' if has_vwap else 'FAILED',
            'reason': 'OK' if has_vwap else 'VWAP could not be calculated (no volume data)',
            'details': {
                'vwap': round(vwap_value, 2),
                'ltp': round(data['ltp'], 2),
                'ltp_vs_vwap': f"+{((data['ltp'] - vwap_value) / vwap_value * 100):.2f}%" if vwap_value > 0 else 'N/A'
            },
            'suggestion': None
        }

    def _check_gate5_hard_constraints(self, data: dict) -> dict:
        """Gate 5: Hard Constraints — min gain, max gain, distance from high."""
        ltp = data['ltp']
        day_high = data['day_high']
        open_price = data['day_open']
        gain_pct = data['change_pct']

        # Replicate exact GodModeAnalyst.check_constraints logic
        max_gain_pct = ((day_high - open_price) / open_price) * 100
        is_strong_trend = gain_pct >= 5.0
        was_strong_trend = max_gain_pct >= 7.0

        details = {
            'current_gain': f"{gain_pct:.2f}%",
            'max_day_gain': f"{max_gain_pct:.2f}%",
            'ltp': round(ltp, 2),
            'day_high': round(day_high, 2),
            'day_open': round(open_price, 2),
        }

        reason = None
        suggestion = None

        # Check 1: Trend strength
        if not is_strong_trend and not was_strong_trend:
            reason = 'WEAK_TREND'
            details['check_failed'] = 'Trend Strength'
            details['required'] = 'Current gain ≥5% OR max day gain ≥7%'
            suggestion = f"Stock gain {gain_pct:.1f}% and max gain {max_gain_pct:.1f}% — too weak. Need ≥5% current OR ≥7% max."
            return {
                'gate_num': 5, 'name': 'Hard Constraints', 'status': 'FAILED',
                'reason': reason, 'details': details, 'suggestion': suggestion
            }

        # Check 2: Circuit risk (too high gain)
        if gain_pct > 15.0:
            reason = 'CIRCUIT_RISK'
            details['check_failed'] = 'Max Gain Limit'
            details['limit'] = '15.0%'
            suggestion = f"Stock gain {gain_pct:.1f}% exceeds 15% — circuit risk. This is a safety guard."
            return {
                'gate_num': 5, 'name': 'Hard Constraints', 'status': 'FAILED',
                'reason': reason, 'details': details, 'suggestion': suggestion
            }

        # Check 3: Distance from high
        dist_from_high_pct = (day_high - ltp) / day_high * 100

        allowed_dist = 4.0
        if max_gain_pct > 10.0 or was_strong_trend:
            allowed_dist = 6.0

        details['distance_from_high'] = f"{dist_from_high_pct:.2f}%"
        details['max_allowed_distance'] = f"{allowed_dist:.1f}%"

        if dist_from_high_pct > allowed_dist:
            reason = 'TOO_FAR_FROM_HIGH'
            details['check_failed'] = 'Day High Proximity'
            suggestion = (
                f"Stock is {dist_from_high_pct:.2f}% below day high (limit: {allowed_dist:.1f}%). "
                f"Consider increasing allowed pullback distance if this is a common failure."
            )
            return {
                'gate_num': 5, 'name': 'Hard Constraints', 'status': 'FAILED',
                'reason': reason, 'details': details, 'suggestion': suggestion
            }

        # All constraints passed
        return {
            'gate_num': 5, 'name': 'Hard Constraints', 'status': 'PASSED',
            'reason': 'OK', 'details': details, 'suggestion': None
        }

    def _check_gate6_circuit_guard(self, symbol: str, data: dict) -> dict:
        """Gate 6: Circuit Guard — upper/lower circuit proximity."""
        ltp = data['ltp']

        try:
            depth_data = self.fyers.depth(data={"symbol": symbol, "ohlcv_flag": "1"})
            quote = {}
            if 'd' in depth_data and symbol in depth_data['d']:
                quote = depth_data['d'][symbol]

            uc = quote.get('upper_ckt', 0)
            lc = quote.get('lower_ckt', 0)

            details = {
                'upper_circuit': uc,
                'lower_circuit': lc,
                'ltp': round(ltp, 2),
            }

            if uc > 0:
                buffer_price = uc * 0.985
                details['uc_buffer'] = round(buffer_price, 2)
                if ltp >= buffer_price:
                    return {
                        'gate_num': 6, 'name': 'Circuit Guard', 'status': 'FAILED',
                        'reason': 'TOO_CLOSE_TO_UPPER_CIRCUIT',
                        'details': details,
                        'suggestion': f"LTP ₹{ltp:.2f} is within 1.5% of upper circuit ₹{uc:.2f}. Too risky to short."
                    }

            if lc > 0 and ltp <= lc * 1.005:
                return {
                    'gate_num': 6, 'name': 'Circuit Guard', 'status': 'FAILED',
                    'reason': 'AT_LOWER_CIRCUIT',
                    'details': details,
                    'suggestion': "Stock is at lower circuit — no entry possible."
                }

            return {
                'gate_num': 6, 'name': 'Circuit Guard', 'status': 'PASSED',
                'reason': 'OK', 'details': details, 'suggestion': None
            }

        except Exception as e:
            return {
                'gate_num': 6, 'name': 'Circuit Guard', 'status': 'PASSED',
                'reason': f'Could not fetch depth data ({e}), assuming OK',
                'details': {}, 'suggestion': None
            }

    def _check_gate7_momentum_safeguard(self, data: dict) -> dict:
        """Gate 7: Momentum Safeguard (train filter) — RVOL + VWAP slope."""
        df = data['df']

        if len(df) < 30:
            return {
                'gate_num': 7, 'name': 'Momentum Safeguard', 'status': 'PASSED',
                'reason': 'Insufficient data for momentum check (< 30 candles)',
                'details': {}, 'suggestion': None
            }

        slope, slope_status = self.gm_analyst.calculate_vwap_slope(df.iloc[-30:])

        # Calculate RVOL
        recent_vols = df['volume'].iloc[-20:-1]
        avg_v = recent_vols.mean()
        curr_v = df['volume'].iloc[-1]
        rvol = curr_v / avg_v if avg_v > 0 else 0

        details = {
            'vwap_slope': round(slope, 2),
            'slope_status': slope_status,
            'rvol': round(rvol, 2),
            'threshold': 'RVOL > 5.0 AND Slope > 40',
        }

        is_train = rvol > 5.0 and slope > 40

        if is_train:
            return {
                'gate_num': 7, 'name': 'Momentum Safeguard', 'status': 'FAILED',
                'reason': 'MOMENTUM_TOO_STRONG',
                'details': details,
                'suggestion': f"RVOL {rvol:.1f}x with slope {slope:.1f} — momentum train. Too dangerous to short."
            }

        return {
            'gate_num': 7, 'name': 'Momentum Safeguard', 'status': 'PASSED',
            'reason': 'OK', 'details': details, 'suggestion': None
        }

    def _check_gate8_pattern(self, data: dict) -> dict:
        """Gate 8: Pattern Recognition — checks for 6 reversal patterns."""
        df = data['df']

        if len(df) < 3:
            return {
                'gate_num': 8, 'name': 'Pattern Recognition', 'status': 'FAILED',
                'reason': 'INSUFFICIENT_DATA', 'details': {},
                'suggestion': 'Need at least 3 candles for pattern detection.'
            }

        struct, z_score = self.gm_analyst.detect_structure_advanced(df)

        # Also check tape stall
        is_stalled, stall_msg = self.tape_reader.detect_stall(df)

        # Check sniper zone
        is_sniper_zone = False
        if len(df) > 1:
            recent_highs = df['high'].iloc[-10:]
            ltp = df.iloc[-1]['close']
            micro_high = recent_highs.max()
            micro_low = df['low'].iloc[-10:].min()
            micro_range = micro_high - micro_low
            if micro_range > 0:
                position = (ltp - micro_low) / micro_range
                is_sniper_zone = position > 0.7

        valid_patterns = ["SHOOTING_STAR", "BEARISH_ENGULFING", "EVENING_STAR",
                          "MOMENTUM_BREAKDOWN", "VOLUME_TRAP"]

        details = {
            'structure_detected': struct,
            'z_score': round(z_score, 2) if z_score else 'N/A',
            'tape_stall': is_stalled,
            'sniper_zone': is_sniper_zone,
        }

        if struct in valid_patterns:
            return {
                'gate_num': 8, 'name': 'Pattern Recognition', 'status': 'PASSED',
                'reason': f'Pattern: {struct}', 'details': details, 'suggestion': None
            }
        elif struct == "ABSORPTION_DOJI" and is_sniper_zone:
            return {
                'gate_num': 8, 'name': 'Pattern Recognition', 'status': 'PASSED',
                'reason': f'Pattern: ABSORPTION_DOJI (in sniper zone)', 'details': details, 'suggestion': None
            }
        elif is_stalled:
            # Check extension for tape stall
            prev_df = df.iloc[:-1]
            vwap_sd = self.gm_analyst.calculate_vwap_bands(prev_df) if len(prev_df) > 5 else 0
            is_extended = vwap_sd > 2.0
            details['vwap_sd'] = round(vwap_sd, 2) if vwap_sd else 'N/A'
            details['is_extended'] = is_extended

            if is_extended:
                return {
                    'gate_num': 8, 'name': 'Pattern Recognition', 'status': 'PASSED',
                    'reason': 'Pattern: TAPESTALL (Drift) + Extended', 'details': details, 'suggestion': None
                }
            else:
                return {
                    'gate_num': 8, 'name': 'Pattern Recognition', 'status': 'FAILED',
                    'reason': 'TAPE_STALL_NOT_EXTENDED',
                    'details': details,
                    'suggestion': f'Tape stall detected but VWAP extension only {vwap_sd:.1f}SD (need >2.0SD).'
                }
        else:
            suggestion_parts = [f"Detected structure: '{struct}' — not a valid sell pattern."]
            suggestion_parts.append(f"Valid patterns: {', '.join(valid_patterns)}")
            if struct == "ABSORPTION_DOJI":
                suggestion_parts.append("ABSORPTION_DOJI detected but price not in sniper zone (top 30% of micro-range).")

            return {
                'gate_num': 8, 'name': 'Pattern Recognition', 'status': 'FAILED',
                'reason': 'NO_VALID_PATTERN',
                'details': details,
                'suggestion': ' '.join(suggestion_parts)
            }

    def _check_gate9_breakdown(self, data: dict) -> dict:
        """Gate 9: Breakdown Confirmation — LTP must be below setup candle low."""
        df = data['df']

        if len(df) < 2:
            return {
                'gate_num': 9, 'name': 'Breakdown Confirmation', 'status': 'FAILED',
                'reason': 'INSUFFICIENT_DATA', 'details': {}, 'suggestion': None
            }

        current_ltp = df.iloc[-1]['close']
        setup_low = df.iloc[-2]['low']
        breakdown = current_ltp < setup_low

        details = {
            'current_ltp': round(current_ltp, 2),
            'setup_candle_low': round(setup_low, 2),
            'breakdown': breakdown,
            'gap': round(setup_low - current_ltp, 2)
        }

        if breakdown:
            return {
                'gate_num': 9, 'name': 'Breakdown Confirmation', 'status': 'PASSED',
                'reason': f'LTP ₹{current_ltp:.2f} < Setup Low ₹{setup_low:.2f}',
                'details': details, 'suggestion': None
            }
        else:
            return {
                'gate_num': 9, 'name': 'Breakdown Confirmation', 'status': 'FAILED',
                'reason': 'NO_BREAKDOWN',
                'details': details,
                'suggestion': (
                    f"Price ₹{current_ltp:.2f} has NOT broken below setup candle low ₹{setup_low:.2f}. "
                    f"Need ₹{setup_low - current_ltp:.2f} more drop for breakdown confirmation."
                )
            }

    def _check_gate10_confluence(self, symbol: str, data: dict) -> dict:
        """Gate 10: Pro Confluence — secondary confirmation signals."""
        df = data['df']
        prev_df = df.iloc[:-1] if len(df) > 1 else df

        pro_conf = []

        # Profile Rejection
        try:
            is_bearish_profile, _ = self.profile_analyzer.check_profile_rejection(df, df.iloc[-1]['close'])
            if is_bearish_profile:
                pro_conf.append("Profile Rejection")
        except Exception:
            pass

        # VWAP Extension
        vwap_sd = 0
        is_extended = False
        try:
            vwap_sd = self.gm_analyst.calculate_vwap_bands(prev_df) if len(prev_df) > 5 else 0
            is_extended = vwap_sd > 2.0
            if is_extended:
                pro_conf.append(f"VWAP +{vwap_sd:.1f}SD [EXT]")
        except Exception:
            pass

        # VWAP Slope
        slope = 0
        try:
            if len(df) >= 30:
                slope, _ = self.gm_analyst.calculate_vwap_slope(df.iloc[-30:])
                if slope < 5:
                    pro_conf.append("VWAP Flat")
        except Exception:
            pass

        # RSI Divergence
        try:
            if self.gm_analyst.check_rsi_divergence(prev_df):
                pro_conf.append("RSI Div [DOWN]")
        except Exception:
            pass

        # RVOL & Vacuum
        try:
            if len(df) > 20:
                avg_vol = df['volume'].iloc[-20:-2].mean()
                setup_vol = df.iloc[-2]['volume']
                rvol = setup_vol / avg_vol if avg_vol > 0 else 0
                if rvol > 2.0:
                    pro_conf.append(f"RVOL {rvol:.1f}x [VOL]")
                elif rvol < 0.5 and is_extended:
                    pro_conf.append("Vacuum/Exhaustion [EXHT]")
        except Exception:
            pass

        # Fibonacci
        try:
            fibs = self.gm_analyst.calculate_fib_levels(prev_df)
            if fibs and len(df) > 2:
                setup_high = df.iloc[-2]['high']
                for name, level in fibs.items():
                    if name == 'trend':
                        continue
                    if abs(setup_high - level) <= (level * 0.001):
                        if fibs.get('trend') == 'DOWN' and df.iloc[-2]['close'] < level:
                            pro_conf.append(f"{name} Reject [FIB]")
                            break
        except Exception:
            pass

        details = {
            'confirmations_found': len(pro_conf),
            'confirmations': pro_conf,
            'vwap_sd': round(vwap_sd, 2),
            'is_extended': is_extended,
            'vwap_slope': round(slope, 2),
        }

        # Normal logic: Not extended + no tape → need confluence
        needs_confluence = not is_extended
        has_confluence = len(pro_conf) > 0

        if needs_confluence and not has_confluence:
            return {
                'gate_num': 10, 'name': 'Pro Confluence', 'status': 'FAILED',
                'reason': 'NO_CONFLUENCE_AND_NOT_EXTENDED',
                'details': details,
                'suggestion': (
                    f"No secondary confirmation found and VWAP SD is {vwap_sd:.1f} (need >2.0 for extension bypass). "
                    "Look for: Profile Rejection, RSI Divergence, RVOL >2x, Fib Rejection, or VWAP Flat slope."
                )
            }

        return {
            'gate_num': 10, 'name': 'Pro Confluence', 'status': 'PASSED',
            'reason': f"{'Extended' if is_extended else ''}{' + ' if is_extended and pro_conf else ''}{', '.join(pro_conf) if pro_conf else 'Extension OK'}".strip(),
            'details': details, 'suggestion': None
        }

    def _check_gate11_htf(self, symbol: str, ltp: float) -> dict:
        """Gate 11: HTF Confluence — higher timeframe trend check."""
        try:
            htf_ok, htf_msg = self.htf_confluence.check_trend_exhaustion(symbol)

            details = {'htf_message': htf_msg}

            # Key level check
            try:
                at_level, level_name, level_price = self.htf_confluence.is_at_key_level(
                    symbol, ltp, tolerance_pct=1.0
                )
                if at_level:
                    details['key_level'] = f"{level_name} (₹{level_price:.2f})"
            except Exception:
                pass

            if not htf_ok:
                return {
                    'gate_num': 11, 'name': 'HTF Confluence', 'status': 'FAILED',
                    'reason': htf_msg,
                    'details': details,
                    'suggestion': "Higher timeframe trend does not support a short entry at this level."
                }

            return {
                'gate_num': 11, 'name': 'HTF Confluence', 'status': 'PASSED',
                'reason': htf_msg, 'details': details, 'suggestion': None
            }

        except Exception as e:
            return {
                'gate_num': 11, 'name': 'HTF Confluence', 'status': 'PASSED',
                'reason': f'HTF check error ({e}), defaulting to PASS',
                'details': {}, 'suggestion': None
            }

    def _check_gate12_finalization(self, data: dict) -> dict:
        """Gate 12: Signal Finalization — ATR and SL calculation."""
        df = data['df']

        try:
            atr = self.gm_analyst.calculate_atr(df)
            buffer = max(atr * 0.5, 0.25)
            setup_high = df.iloc[-2]['high'] if len(df) > 1 else df.iloc[-1]['high']
            sl_price = setup_high + buffer

            return {
                'gate_num': 12, 'name': 'Signal Finalization', 'status': 'PASSED',
                'reason': 'OK',
                'details': {
                    'atr': round(atr, 2),
                    'sl_buffer': round(buffer, 2),
                    'setup_high': round(setup_high, 2),
                    'calculated_sl': round(sl_price, 2),
                },
                'suggestion': None
            }
        except Exception as e:
            return {
                'gate_num': 12, 'name': 'Signal Finalization', 'status': 'ERROR',
                'reason': str(e), 'details': {}, 'suggestion': None
            }

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  PROFITABILITY CHECK
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _check_profitability(self, symbol: str, entry_time: datetime, entry_price: float) -> dict:
        """Check what happened 30 minutes after the analysis time."""
        exit_time = entry_time + timedelta(minutes=30)

        # Clamp to market close
        market_close = entry_time.replace(hour=15, minute=30, second=0)
        if exit_time > market_close:
            exit_time = market_close

        data = {
            'symbol': symbol,
            'resolution': '1',
            'date_format': '1',
            'range_from': entry_time.strftime('%Y-%m-%d'),
            'range_to': exit_time.strftime('%Y-%m-%d'),
            'cont_flag': '1'
        }

        try:
            response = self.fyers.history(data=data)

            if response.get('s') != 'ok' or not response.get('candles'):
                return {'available': False}

            df = pd.DataFrame(
                response['candles'],
                columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
            )
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')

            # Filter to candles after entry time
            if isinstance(entry_time, pd.Timestamp):
                entry_naive = entry_time.tz_localize(None) if entry_time.tzinfo else entry_time
            else:
                entry_naive = entry_time
            df = df[df['timestamp'] >= entry_naive]

            if df.empty:
                return {'available': False}

            low_30min = df['low'].min()
            close_30min = df.iloc[-1]['close']

            # For shorts: profit = entry_price - low (we are selling first)
            max_profit_pts = entry_price - low_30min
            max_profit_pct = (max_profit_pts / entry_price) * 100

            exit_pts = entry_price - close_30min
            exit_pct = (exit_pts / entry_price) * 100

            return {
                'available': True,
                'entry_price': round(entry_price, 2),
                'low_30min': round(low_30min, 2),
                'close_30min': round(close_30min, 2),
                'max_profit_pct': round(max_profit_pct, 2),
                'exit_profit_pct': round(exit_pct, 2),
                'would_be_profitable': exit_pct > 0
            }

        except Exception as e:
            logger.error(f"Could not fetch profitability data: {e}")
            return {'available': False}

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  LOGGING
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _log_diagnostic(self, symbol, timestamp, first_failure, gates, profitability):
        """Log diagnostic result to CSV for pattern analysis."""
        log_path = getattr(config, 'DIAGNOSTIC_LOG_PATH', 'logs/diagnostic_analysis.csv')
        os.makedirs(os.path.dirname(log_path), exist_ok=True)

        file_exists = os.path.exists(log_path)

        try:
            with open(log_path, 'a', newline='') as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow([
                        'timestamp', 'analysis_time', 'symbol', 'first_failure_gate',
                        'failed_gate_name', 'failed_reason', 'would_profit',
                        'profit_pct', 'num_gates_passed'
                    ])

                failed_gate = next((g for g in gates if g['status'] == 'FAILED'), None)
                gates_passed = sum(1 for g in gates if g['status'] == 'PASSED')

                writer.writerow([
                    datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    timestamp.strftime('%Y-%m-%d %H:%M:%S') if hasattr(timestamp, 'strftime') else str(timestamp),
                    symbol,
                    first_failure,
                    failed_gate['name'] if failed_gate else 'NONE',
                    failed_gate['reason'] if failed_gate else 'ALL_PASSED',
                    profitability.get('would_be_profitable', 'N/A'),
                    profitability.get('exit_profit_pct', 'N/A'),
                    gates_passed
                ])
        except Exception as e:
            logger.warning(f"Could not log diagnostic: {e}")
