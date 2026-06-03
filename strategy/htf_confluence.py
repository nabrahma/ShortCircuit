"""
Higher Timeframe Confluence Module
Math-First Architecture (Phase 61.1)
Based on Leung & Li: "Optimal Mean Reversion" and Momentum Physics.
"""
import logging
import datetime
import pandas as pd
import config

logger = logging.getLogger(__name__)

class HTFConfluence:
    """
    Analyzes higher timeframe charts to confirm trade direction using Math-First logic.
    """
    
    def __init__(self, fyers):
        self.fyers = fyers
    
    def _get_htf_history(self, symbol, interval="15"):
        """
        Fetch higher timeframe data.
        """
        today = datetime.date.today().strftime("%Y-%m-%d")
        
        now = _time.time()
        if not hasattr(self, '_last_range_fetch_time'):
            self._last_range_fetch_time = 0.0
        if now - self._last_range_fetch_time < 600:  # Phase 91: Increased TTL to 600s to prevent 429 rate limits
            return None
        self._last_range_fetch_time = now
        
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
            if response.get('s') == 'ok' and response.get('candles'):
                candles = response['candles']
                df = pd.DataFrame(candles, columns=['t', 'o', 'h', 'l', 'c', 'v'])
                # Phase 91: Guard against malformed responses
                if 'c' not in df.columns or len(df) < 3:
                    logger.warning(f"G9: HTF data malformed for {symbol} — skipping")
                    return None
                df['t'] = pd.to_datetime(df['t'], unit='s')
                return df
        except Exception as e:
            logger.error(f"HTF data fetch failed for {symbol}: {e}")
        
        return None
    
    def check_trend_exhaustion(self, symbol, df_15m=None, vwap_sd: float = 0.0):
        """
        Math-First G9 (Phase 61.1):
        1. Alpha Strike (Bypass): If stretch is extreme, pass immediately.
        2. Acceleration Guard: Reject if momentum is accelerating up.
        3. Stall Check: Pass if momentum has slowed down at highs.
        
        Returns:
            tuple: (allowed, message)
        """
        # ── Step 1: Alpha Strike (Leung & Li Bypass) ──────────
        if vwap_sd > config.P61_G9_BYPASS_SD_THRESHOLD:
            return True, f"G9 PASS: Alpha Strike (Stretch={vwap_sd:.1f}SD)"

        # ── Step 2: Data Fetching ───────────────────────────
        df = df_15m if df_15m is not None else self._get_htf_history(symbol, interval="15")
        
        if df is None or len(df) < 3:
            return False, "G9 BLOCK: HTF Data Unavailable"

        # ── Step 3: Momentum Physics (Velocity/Acceleration) ─────
        try:
            # Phase 98.1: Handle both column naming conventions
            # Analyzer DataFrames use 'close', HTF self-fetched uses 'c'
            close_col = 'c' if 'c' in df.columns else 'close'
            if close_col not in df.columns:
                return True, "G9 SKIP: No close column in DataFrame"

            curr_c = df[close_col].iloc[-1]
            prev_c = df[close_col].iloc[-2]
            pprev_c = df[close_col].iloc[-3]
            
            if prev_c == 0 or pprev_c == 0:
                return True, "G9 SKIP: Zero price in candle data"

            # move_pct in last 15 mins vs previous 15 mins
            curr_move = ((curr_c - prev_c) / prev_c) * 100
            prev_move = ((prev_c - pprev_c) / pprev_c) * 100
            
            # Acceleration: Are we going up faster now?
            acceleration = curr_move - prev_move
            
            # ACCEL REJECT: Rocket Ship Guard
            if curr_move > config.P61_G9_ACCEL_REJECT_THRESHOLD:
                 return False, f"G9 BLOCK: Momentum Accel (+{curr_move:.2f}%)"

            # STALL PASS: Rubber Band logic
            if curr_move < config.P61_G9_STALL_PASS_THRESHOLD:
                 return True, f"G9 PASS: Momentum Stall (Move {curr_move:.2f}% < {config.P61_G9_STALL_PASS_THRESHOLD}%)"

        except Exception as e:
            logger.debug(f"G9 Math Logic Error (non-fatal): {e}")
            return False, f"G9 BLOCK: Calculation Error ({e})"

        return False, f"G9 BLOCK: Sustained Trend (Move {curr_move:.2f}%)"
    
