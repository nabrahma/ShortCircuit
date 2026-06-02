"""
features.py — Pure stateless feature extraction.

All functions are pure: data in, value out. No broker calls, no side effects.
Extracted from god_mode_logic.py and analyzer.py during the BackToVWAPShort collapse.
"""

import numpy as np
import pandas as pd
from typing import Tuple, Optional


# ─────────────────────────────────────────────────────────────────────────────
# VWAP
# ─────────────────────────────────────────────────────────────────────────────

def compute_vwap_sd(df: pd.DataFrame, window: int = 20) -> float:
    """
    Returns distance from VWAP in standard deviations.
    Uses the last *window* candles to compute σ of (close - VWAP).
    """
    if 'vwap' not in df.columns or len(df) < window:
        return 0.0

    recent = df.iloc[-window:]
    diffs = recent['close'] - recent['vwap']
    std_dev = diffs.std()

    if std_dev == 0:
        return 0.0

    current_diff = df.iloc[-1]['close'] - df.iloc[-1]['vwap']
    return current_diff / std_dev


def compute_vwap_slope(df: pd.DataFrame, window: int = 30) -> Tuple[float, str]:
    """
    Slope of the VWAP curve over the last *window* candles.
    Returns (slope_bps_per_min, status).
    status: "FLAT" if |slope| < 5 bps/min, "TRENDING" otherwise.
    """
    if df.empty or len(df) < window:
        return 0.0, "INSUFFICIENT_DATA"

    if 'vwap' not in df.columns:
        v = df['volume'].values
        tp = (df['high'] + df['low'] + df['close']) / 3
        vwap = (tp * v).cumsum() / v.cumsum()
    else:
        vwap = df['vwap']

    y = vwap.iloc[-window:].values
    x = np.arange(len(y))

    if len(y) < 2:
        return 0.0, "INSUFFICIENT_DATA"

    slope, _ = np.polyfit(x, y, 1)
    pct_slope = (slope / df['close'].iloc[-1]) * 10000

    status = "FLAT" if abs(pct_slope) < 5 else "TRENDING"
    return pct_slope, status


def enrich_dataframe(df: pd.DataFrame) -> None:
    """Calculates VWAP in-place on the dataframe."""
    v = df['volume'].values
    tp = (df['high'] + df['low'] + df['close']) / 3
    df['vwap'] = (tp * v).cumsum() / v.cumsum()


# ─────────────────────────────────────────────────────────────────────────────
# RSI
# ─────────────────────────────────────────────────────────────────────────────

def compute_rsi(df: pd.DataFrame, period: int = 14) -> float:
    """Standard RSI calculation. Returns NaN if insufficient data."""
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()

    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi.iloc[-1]


def compute_rsi_divergence(df: pd.DataFrame, window: int = 25) -> bool:
    """
    Returns True if price is making higher highs but RSI is making lower highs
    over the last *window* candles (bearish divergence).
    """
    try:
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi_series = 100 - (100 / (1 + rs))

        recent_rsi = rsi_series.iloc[-window:]
        recent_price = df['close'].iloc[-window:]

        p_start, p_end = recent_price.iloc[0], recent_price.iloc[-1]
        r_start, r_end = recent_rsi.iloc[0], recent_rsi.iloc[-1]

        return bool(p_end > p_start and r_end < r_start)
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# ATR
# ─────────────────────────────────────────────────────────────────────────────

def compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Average True Range. Returns 1.0 as fallback on error."""
    try:
        high = df['high']
        low = df['low']
        close = df['close']

        prev_close = close.shift(1)
        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()

        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(window=period).mean()
        return atr.iloc[-1]
    except Exception:
        return 1.0


# ─────────────────────────────────────────────────────────────────────────────
# Volume
# ─────────────────────────────────────────────────────────────────────────────

def compute_volume_fade_ratio(candles: list, lookback: int = 15) -> float:
    """
    Ratio of current volume (avg of last 2 candles) to prior average.
    < 0.65 = fading (exhaustion). > 1.0 = acceleration.
    """
    if len(candles) < (lookback + 2):
        return 1.0

    prior_vols = [c['volume'] for c in candles[-(lookback + 1):-1]]
    avg_prior = sum(prior_vols) / len(prior_vols) if prior_vols else 0
    if avg_prior == 0:
        return 1.0

    current_avg = sum(c['volume'] for c in candles[-2:]) / 2
    return round(current_avg / avg_prior, 3)


def compute_rvol(df: pd.DataFrame) -> float:
    """
    Relative volume: current candle volume vs 20-candle average.
    Uses 2-minute average for the "current" to smooth spikes.
    """
    if len(df) < 22:
        return 1.0
    avg_vol = df['volume'].iloc[-20:-2].mean()
    setup_vol = df['volume'].iloc[-3:-1].mean()
    return setup_vol / avg_vol if avg_vol > 0 else 1.0


# ─────────────────────────────────────────────────────────────────────────────
# Stretch & Gain
# ─────────────────────────────────────────────────────────────────────────────

def compute_stretch_score(gain_pct: float, scanner_min: float) -> float:
    """
    Relative stretch above scanner minimum.
    0.0 at scanner floor, 1.0 at 2× scanner floor.
    """
    if scanner_min == 0:
        return 0.0
    return round((gain_pct - scanner_min) / scanner_min, 3)


# ─────────────────────────────────────────────────────────────────────────────
# Pattern Detection
# ─────────────────────────────────────────────────────────────────────────────

def detect_pattern(df: pd.DataFrame, vah: float = None) -> Tuple[str, float]:
    """
    Multi-candle reversal pattern detection.
    Returns (pattern_name, volume_z_score).
    pattern_name is one of: VAH_REJECTION, BEARISH_ENGULFING, EVENING_STAR,
    SHOOTING_STAR, ABSORPTION_DOJI, MOMENTUM_BREAKDOWN, VOLUME_TRAP, NORMAL.
    """
    if df.empty or len(df) < 3:
        return "NORMAL", 0.0

    c1 = df.iloc[-3]  # 2 candles ago
    c2 = df.iloc[-2]  # prev candle
    c3 = df.iloc[-1]  # current candle

    # Pattern 0: VAH_REJECTION (Look Above & Fail)
    if vah and vah > 0:
        poked_above = df['high'].iloc[-3:].max() > (vah * 1.0005)
        closed_back_in = c3['close'] < (vah * 0.9995)
        if poked_above and closed_back_in:
            return "VAH_REJECTION", 0.0

    def _stats(row):
        body = abs(row['close'] - row['open'])
        direction = 1 if row['close'] > row['open'] else -1
        upper_wick = row['high'] - max(row['open'], row['close'])
        total_range = row['high'] - row['low']
        if total_range == 0:
            total_range = 0.05
        return body, direction, upper_wick, total_range

    b1, d1, uw1, r1 = _stats(c1)
    b2, d2, uw2, r2 = _stats(c2)
    b3, d3, uw3, r3 = _stats(c3)

    # Vol Z-Score
    recent_vol = df['volume'].iloc[-20:-1]
    avg_vol = recent_vol.mean()
    std_vol = recent_vol.std()
    current_vol = c3['volume']
    z_score = (current_vol - avg_vol) / std_vol if std_vol > 0 else 0

    # Pattern 1: Bearish Engulfing
    if d2 == 1 and d3 == -1 and b3 > b2 and c3['close'] < c2['open'] and z_score > 0:
        return "BEARISH_ENGULFING", z_score

    # Pattern 2: Evening Star
    if d2 == 1 and b2 < (r2 * 0.3) and d3 == -1:
        if c3['close'] < (c1['open'] + c1['close']) / 2:
            return "EVENING_STAR", z_score

    # Pattern 3: Shooting Star
    if uw3 > (2 * b3) and z_score > 1.5:
        return "SHOOTING_STAR", z_score

    # Pattern 4: Absorption Doji
    if z_score > 2.0 and b3 < (c3['close'] * 0.0005):
        return "ABSORPTION_DOJI", z_score

    # Pattern 5: Momentum Breakdown
    avg_body = df['high'].iloc[-20:-1].sub(df['low'].iloc[-20:-1]).abs().mean()
    if avg_body == 0:
        avg_body = 0.1

    is_big_red = d3 == -1 and b3 > (1.2 * avg_body)
    is_high_vol = (
        z_score > 2.0
        or (b3 > 1.5 * avg_body and z_score > 1.2)
        or (b3 > 3.0 * avg_body)
    )
    closes_at_low = (c3['close'] - c3['low']) < (r3 * 0.35)

    if is_big_red and is_high_vol and closes_at_low:
        return "MOMENTUM_BREAKDOWN", z_score

    # Pattern 6: Volume Trap
    prev_vol = c2['volume']
    prev_z = (prev_vol - avg_vol) / std_vol if std_vol > 0 else 0

    if d2 == 1 and prev_z > 1.5 and d3 == -1 and c3['close'] < c2['low']:
        return "VOLUME_TRAP", z_score

    return "NORMAL", z_score


# ─────────────────────────────────────────────────────────────────────────────
# Structure Checks
# ─────────────────────────────────────────────────────────────────────────────

def is_narrowing_highs(df: pd.DataFrame, n: int = 3) -> bool:
    """
    Returns True if the last *n* completed candles each have a lower high.
    Murphy: "staircase down" preceding institutional selling.
    """
    if len(df) < (n + 1):
        return False
    highs = [df['high'].iloc[-(i + 2)] for i in range(n)]
    # highs[0] = most recent completed, highs[-1] = oldest
    return all(highs[i] < highs[i + 1] for i in range(len(highs) - 1))


def is_at_day_high(
    candles: list, atr: float = 0, tolerance: float = 0.005, atr_mult: float = 0.2
) -> bool:
    """
    Returns True if the recent high (max of last 3 candles) is within tolerance
    of the all-day high.
    """
    if len(candles) < 3:
        return False

    day_high = max(c['high'] for c in candles)
    curr_high = max(c['high'] for c in candles[-3:])

    if atr > 0 and day_high > 0:
        atr_tol_pct = (atr * atr_mult) / day_high
        tol = max(tolerance, atr_tol_pct)
    else:
        tol = tolerance

    return curr_high >= day_high * (1 - tol)


def check_time_since_high(df: pd.DataFrame, max_candles: int = 25) -> bool:
    """
    Returns True if the day high occurred within the last *max_candles*.
    Prevents trading stale highs (price acceptance, not rejection).
    """
    if df.empty:
        return False
    high_idx = df['high'].idxmax()
    last_idx = df.index[-1]
    candles_since = last_idx - high_idx
    return candles_since <= max_candles


def check_kill_backdoor(
    ltp: float, day_high: float, atr: float = 0,
    fixed_pct: float = 0.015, atr_mult: float = 0.3
) -> Tuple[bool, str]:
    """
    Returns (is_blocked, reason).
    Blocks entry if price has already dropped too far from the high.
    """
    if atr > 0 and day_high > 0:
        atr_pct = atr / day_high
        threshold = max(fixed_pct, atr_mult * atr_pct)
    else:
        threshold = fixed_pct

    if ltp < day_high * (1 - threshold):
        return True, (
            f"Kill Backdoor: ₹{ltp:.2f} is {threshold * 100:.1f}% "
            f"below day high ₹{day_high:.2f}"
        )
    return False, "PASSED"


def check_unspecified_move_audit(
    df: pd.DataFrame,
    surge_mult: float = 3.0,
    fade_ratio: float = 0.6,
) -> Tuple[bool, str]:
    """
    Unspecified Move Audit (RVOL Surge-and-Fade).
    Returns (passed, message).
    Rejects steady-trend accumulations — requires a volume spike followed by fade.
    """
    if len(df) < 18:
        return True, "INSUFFICIENT_DATA"

    try:
        baseline_vol = df['volume'].iloc[-18:-3].mean()
        if baseline_vol == 0:
            return True, "ZERO_BASELINE"

        recent_vols = df['volume'].iloc[-3:]
        max_recent = recent_vols.max()
        current_vol = recent_vols.iloc[-1]

        surge_hit = max_recent > (baseline_vol * surge_mult)
        if not surge_hit:
            return False, (
                f"Steady Trend: Peak RVOL {max_recent / baseline_vol:.1f}x "
                f"< {surge_mult}x required"
            )

        fade_hit = current_vol < (max_recent * fade_ratio)
        if not fade_hit:
            return False, (
                f"No Fade: Vol {current_vol:.0f} > "
                f"{fade_ratio * 100:.0f}% of spike {max_recent:.0f}"
            )

        return True, "PASS"
    except Exception as e:
        return True, f"ERROR:{e}"
