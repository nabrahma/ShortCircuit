"""
strategy/back_to_vwap.py — BackToVWAPShort strategy.

Thesis: Short an overextended stock only after the upside auction fails,
with exhaustion confirmed, and mean reversion back toward VWAP likely.

6 conditions (ALL must pass):
1. VWAP stretch ≥ threshold SD
2. Price above Value Area High (VAH) or profile rejection (failed auction)
3. Failed auction / value-back-in behavior
4. RSI lower high OR price lower high (bearish divergence)
5. Volume fading vs prior expansion
6. Momentum decay (VWAP slope flattening / turning)

Entry trigger: break of trigger low (candle low).
Target: mean reversion toward VWAP.
"""

import logging
from typing import Optional, Dict, Any

import pandas as pd
import config as cfg
import features as F

logger = logging.getLogger(__name__)


class BackToVWAPShort:
    """
    Single strategy implementation for the ShortCircuit bot.

    Replaces:
    - analyzer.py check_setup() G5/G6 gate chain
    - god_mode_logic.is_exhaustion_at_stretch()
    - analyzer._check_pro_confluence()
    - Multi-edge detector (deleted)
    """

    # ──────────────────────────────────────────────────────────────────
    # PUBLIC API
    # ──────────────────────────────────────────────────────────────────

    def evaluate(
        self,
        symbol: str,
        ltp: float,
        df: pd.DataFrame,
        profile: Optional[dict],
        profile_rejection: bool,
        vwap_sd: float,
        atr: float,
        gain_pct: float,
        slope_fast: float,
        slope_slow: float,
        is_decaying: bool,
        upper_circuit: float = 0.0,
        lower_circuit: float = 0.0,
        spread_pct: float = 0.0,
        is_circuit_hitter: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        Evaluate whether all 6 BackToVWAPShort conditions hold.

        Returns a signal_meta dict on pass, None on reject.
        All gate audit info is logged via structured logging for
        correlation by GateResultLogger upstream.
        """
        candles = df.to_dict('records')

        # ── Pre-Filter: Gain, Circuit, and Spread ─────────────────────
        min_gain = getattr(cfg, 'SCANNER_GAIN_MIN_PCT', 7.5)
        if gain_pct < min_gain:
            logger.debug("  [C0] %s REJECT: Gain %.1f%% < %.1f%%", symbol, gain_pct, min_gain)
            return None

        if is_circuit_hitter:
            logger.debug("  [C0] %s REJECT: Blacklisted as Circuit Hitter", symbol)
            return None

        if upper_circuit > 0 and ltp >= upper_circuit * 0.985:
            logger.debug("  [C0] %s REJECT: Too close to Upper Circuit (%.2f / %.2f)", symbol, ltp, upper_circuit)
            return None

        if lower_circuit > 0 and ltp <= lower_circuit * 1.005:
            logger.debug("  [C0] %s REJECT: Too close to Lower Circuit (%.2f / %.2f)", symbol, ltp, lower_circuit)
            return None

        if spread_pct > 0.004:
            logger.debug("  [C0] %s REJECT: Spread %.4f > 0.004", symbol, spread_pct)
            return None

        # ── Condition 1: VWAP Stretch ────────────────────────────────
        sd_floor = getattr(cfg, 'STRATEGY_VWAP_SD_FLOOR', 2.5)
        if vwap_sd < sd_floor:
            logger.debug(
                "  [C1] %s REJECT: VWAP SD %.2f < floor %.1f",
                symbol, vwap_sd, sd_floor,
            )
            return None

        # ── Condition 2: Price above VAH / profile context ───────────
        if profile is None:
            logger.debug("  [C2] %s REJECT: Market profile unavailable", symbol)
            return None

        vah = (
            profile.get('vah')
            if isinstance(profile, dict)
            else getattr(profile, 'vah', None)
        )

        if vah is None or vah <= 0:
            logger.debug("  [C2] %s REJECT: VAH not computed", symbol)
            return None

        curr_close = df.iloc[-1]['close']
        # Allow price below VAH ONLY if we have a confirmed profile rejection
        # (Look Above & Fail / value-back-in).
        if curr_close <= vah and not profile_rejection:
            logger.debug(
                "  [C2] %s REJECT: price %.2f ≤ VAH %.2f (no profile rejection)",
                symbol, curr_close, vah,
            )
            return None

        # ── Condition 3: Failed auction behavior ─────────────────────
        require_auction = getattr(cfg, 'STRATEGY_REQUIRE_FAILED_AUCTION', True)
        has_auction_fail = self._check_auction_failure(
            df, candles, profile, vah, profile_rejection
        )

        if require_auction and not has_auction_fail:
            logger.debug("  [C3] %s REJECT: No failed auction behavior", symbol)
            return None

        # ── Condition 4: Divergence (RSI or price lower-high) ────────
        rsi_div = F.compute_rsi_divergence(
            df, window=getattr(cfg, 'STRATEGY_RSI_DIVERGENCE_WINDOW', 25)
        )
        price_lower_high = F.is_narrowing_highs(df, n=3)

        if not rsi_div and not price_lower_high:
            logger.debug(
                "  [C4] %s REJECT: No divergence (RSI div=%s, narrow highs=%s)",
                symbol, rsi_div, price_lower_high,
            )
            return None

        # ── Condition 5: Volume fading ───────────────────────────────
        lookback = getattr(cfg, 'STRATEGY_VOL_FADE_LOOKBACK', 15)
        max_ratio = getattr(cfg, 'STRATEGY_VOL_FADE_MAX_RATIO', 0.65)

        vol_fade = F.compute_volume_fade_ratio(candles, lookback=lookback)

        # Adaptive relaxation when momentum is already decaying
        if is_decaying:
            decay_relax = getattr(cfg, 'STRATEGY_VOL_FADE_DECAY_RELAX', 0.85)
            max_ratio = max(max_ratio, decay_relax)

        # Spear of Exhaustion bypass: volume climax overrides fade check
        spear_fired = self._check_spear_of_exhaustion(candles, lookback)

        if not spear_fired and vol_fade > max_ratio:
            logger.debug(
                "  [C5] %s REJECT: volume not fading (ratio %.3f > %.2f)",
                symbol, vol_fade, max_ratio,
            )
            return None

        # ── Condition 6: Momentum decay ──────────────────────────────
        slope_flat_thresh = getattr(cfg, 'STRATEGY_MOMENTUM_SLOPE_FLAT_THRESHOLD', 5.0)

        # Momentum is considered decaying if:
        # (a) Fast slope is falling below slow slope (inflection), OR
        # (b) Absolute slope is flat (< threshold)
        momentum_decaying = (
            is_decaying  # Already computed upstream via dual-window check
            or abs(slope_fast) < slope_flat_thresh
        )

        if not momentum_decaying:
            logger.debug(
                "  [C6] %s REJECT: momentum not decaying (fast=%.2f, slow=%.2f)",
                symbol, slope_fast, slope_slow,
            )
            return None

        # ── ALL 6 CONDITIONS PASSED ──────────────────────────────────
        # Compute confidence tier
        confidence = self._compute_confidence(
            vwap_sd, vol_fade, spear_fired, profile_rejection,
            rsi_div, price_lower_high, has_auction_fail,
        )

        # Pattern detection for enrichment
        vah_for_pattern = vah if isinstance(vah, (int, float)) else None
        pattern, vol_z = F.detect_pattern(df, vah=vah_for_pattern)
        if pattern == "NORMAL":
            pattern = "EXHAUSTION_FADE"

        stretch_score = F.compute_stretch_score(
            gain_pct, getattr(cfg, 'SCANNER_GAIN_MIN_PCT', 7.5)
        )

        logger.info(
            "✅ [STRATEGY] %s ALL 6 CONDITIONS MET | SD=%.2f conf=%s "
            "vol_fade=%.3f pattern=%s",
            symbol, vwap_sd, confidence, vol_fade, pattern,
        )

        return {
            'confidence': confidence,
            'pattern_bonus': pattern,
            'stretch_score': stretch_score,
            'vol_fade_ratio': vol_fade,
            'snapshot_high': df['high'].max(),
        }

    # ──────────────────────────────────────────────────────────────────
    # INTERNAL HELPERS
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _check_auction_failure(
        df: pd.DataFrame,
        candles: list,
        profile: dict,
        vah: float,
        profile_rejection: bool,
    ) -> bool:
        """
        Returns True if the stock shows failed-auction behavior:
        1. Profile rejection (value-back-in) from ProfileAnalyzer, OR
        2. VAH Rejection pattern (Look Above & Fail), OR
        3. At-day-high with narrowing highs (distribution at extreme).
        """
        if profile_rejection:
            return True

        # VAH Rejection: probed above VAH in last 3 candles, closed back inside
        if vah and vah > 0 and len(df) >= 3:
            poked_above = df['high'].iloc[-3:].max() > (vah * 1.0005)
            closed_back = df.iloc[-1]['close'] < (vah * 0.9995)
            if poked_above and closed_back:
                return True

        # Distribution at extreme: at day high but highs are narrowing
        if F.is_narrowing_highs(df, n=3):
            day_high = max(c['high'] for c in candles) if candles else 0
            if len(candles) >= 3:
                curr_high = max(c['high'] for c in candles[-3:])
                if day_high > 0 and curr_high >= day_high * 0.995:
                    return True

        return False

    @staticmethod
    def _check_spear_of_exhaustion(candles: list, lookback: int = 15) -> bool:
        """
        Spear of Exhaustion: volume climax at new high with rejection close.
        When detected, bypasses volume-fade requirement.
        """
        if len(candles) < (lookback + 2):
            return False

        prior_vols = [c['volume'] for c in candles[-(lookback + 1):-1]]
        avg_prior = sum(prior_vols) / len(prior_vols) if prior_vols else 0
        if avg_prior == 0:
            return False

        curr = candles[-1]
        prev = candles[-2]

        climax_mult = getattr(cfg, 'STRATEGY_SPEAR_VOL_CLIMAX_MULT', 3.0)
        is_new_high = curr['high'] > prev['high']
        is_rejection = curr['close'] < (curr['high'] + curr['low']) / 2
        is_climax = curr['volume'] > (avg_prior * climax_mult)

        return is_new_high and is_rejection and is_climax

    @staticmethod
    def _compute_confidence(
        vwap_sd: float,
        vol_fade: float,
        spear_fired: bool,
        profile_rejection: bool,
        rsi_div: bool,
        price_lower_high: bool,
        auction_fail: bool,
    ) -> str:
        """
        Tiered confidence scoring.
        EXTREME: SD ≥ 4.5 OR Spear OR 5+ confluences
        HIGH:    SD ≥ 3.3 OR 4+ confluences
        MEDIUM:  everything else (already passed all 6 gates)
        """
        sd_extreme = getattr(cfg, 'STRATEGY_VWAP_SD_EXTREME', 4.5)
        sd_high = getattr(cfg, 'STRATEGY_VWAP_SD_HIGH', 3.3)

        # Count confluences
        confluence = sum([
            profile_rejection,
            rsi_div,
            price_lower_high,
            auction_fail,
            vol_fade < 0.30,
            vwap_sd > sd_high,
        ])

        if spear_fired:
            return "EXTREME"
        if vwap_sd >= sd_extreme:
            return "EXTREME"
        if vwap_sd >= sd_high or confluence >= 4:
            return "HIGH"
        return "MEDIUM"
