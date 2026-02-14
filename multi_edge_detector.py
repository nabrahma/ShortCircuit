"""
Multi-Edge Detection System — Phase 41.1
Detects 5 institutional edges in parallel (reduced from 8 in Phase 41),
scores confidence via weighted confluence, and packages results for
the analyzer pipeline.

Removed in 41.1: OI Divergence Proxy, TPO Poor High, Momentum Exhaustion.
Simplified in 41.1: Failed Auction (60→30 candle minimum).
Added in 41.1: Weighted confluence scoring via config.EDGE_WEIGHTS.

Feature-flag controlled: config.MULTI_EDGE_ENABLED (default False).
Each detector is individually toggleable via config.ENABLED_DETECTORS.
"""
import logging
import numpy as np
import config
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)


class MultiEdgeDetector:
    """
    Runs up to 8 parallel edge detectors on a single candidate,
    scores overall confidence, and returns a structured payload.
    """

    def __init__(self, enabled_detectors: dict):
        self.enabled_detectors = enabled_detectors

    # ------------------------------------------------------------------
    # PUBLIC API
    # ------------------------------------------------------------------
    def scan_all_edges(self, candidate: dict) -> Optional[Dict[str, Any]]:
        """
        Main entry. Returns None if no qualifying edges found,
        otherwise an edge payload dict consumed by the analyzer.

        Args
        ----
        candidate : dict
            Must contain keys: symbol, ltp, history_df, depth (may be None),
            day_high, day_low, open, tick_size, vwap.

        Returns
        -------
        None | dict  (see _score_and_package)
        """
        edges: List[dict] = []

        detectors = [
            ("PATTERN",              self._detect_pattern),
            ("TRAPPED_POSITION",     self._detect_trapped_longs),
            ("ABSORPTION",           self._detect_absorption),
            ("BAD_HIGH",             self._detect_bad_high),
            ("FAILED_AUCTION",       self._detect_failed_auction),
            # Phase 41.1: Removed — see commented-out implementations below
            # ("OI_DIVERGENCE_PROXY",  self._detect_oi_divergence_proxy),
            # ("TPO_POOR_HIGH",        self._detect_poor_high),
            # ("MOMENTUM_EXHAUSTION",  self._detect_momentum_exhaustion),
        ]

        for name, fn in detectors:
            if not self.enabled_detectors.get(name, False):
                continue
            try:
                result = fn(candidate)
                if result is not None:
                    edges.append(result)
            except Exception as e:
                logger.warning(f"[MULTI-EDGE] Detector {name} error: {e}")

        if not edges:
            return None

        return self._score_and_package(edges, candidate)

    # ------------------------------------------------------------------
    # CONFIDENCE SCORING
    # ------------------------------------------------------------------
    def _score_and_package(self, edges: list, candidate: dict) -> Optional[dict]:
        """
        Phase 41.1 — Weighted confluence scoring.

        Uses config.EDGE_WEIGHTS to compute a total score.
        Thresholds (from config):
            ≥ CONFIDENCE_THRESHOLD_EXTREME  → EXTREME
            ≥ CONFIDENCE_THRESHOLD_HIGH     → HIGH
            ≥ CONFIDENCE_THRESHOLD_MEDIUM + 2 edges → HIGH  (confluence bonus)
            Single MEDIUM edge → rejected
        """
        weights = getattr(config, 'EDGE_WEIGHTS', {})
        total_score = sum(weights.get(e['trigger'], 1.0) for e in edges)
        edge_count = len(edges)

        thresh_extreme = getattr(config, 'CONFIDENCE_THRESHOLD_EXTREME', 5.0)
        thresh_high = getattr(config, 'CONFIDENCE_THRESHOLD_HIGH', 3.0)
        thresh_medium = getattr(config, 'CONFIDENCE_THRESHOLD_MEDIUM', 2.0)

        if total_score >= thresh_extreme:
            overall = "EXTREME"
        elif total_score >= thresh_high:
            overall = "HIGH"
        elif total_score >= thresh_medium and edge_count >= 2:
            overall = "HIGH"  # confluence bonus
        elif total_score >= 1.5:
            # Check if any edge is individually HIGH/EXTREME
            if any(e['confidence'] in ('HIGH', 'EXTREME') for e in edges):
                overall = "HIGH"
            else:
                return None  # all MEDIUM edges, reject
        else:
            return None  # score too low

        # Entry trigger = lowest of all edge-specific triggers
        entry_triggers = [
            e.get("entry_level", candidate["ltp"]) for e in edges
        ]
        entry_trigger = min(entry_triggers)

        return {
            "edges": edges,
            "confidence": overall,
            "edge_count": edge_count,
            "weighted_score": total_score,
            "primary_trigger": edges[0]["trigger"],
            "entry_trigger": entry_trigger,
            "recommended_sl": self._calculate_unified_sl(edges, candidate),
        }

    @staticmethod
    def _calculate_unified_sl(edges: list, candidate: dict) -> float:
        """
        Unified SL = max(individual SL suggestions, day_high + buffer).
        Falls back to day_high + 0.3 % if no edge provides an SL.
        """
        sl_candidates = []
        for e in edges:
            if "sl_level" in e.get("metrics", {}):
                sl_candidates.append(e["metrics"]["sl_level"])

        day_high = candidate.get("day_high", candidate["ltp"])
        default_sl = day_high * 1.003  # 0.3 % above day high

        if sl_candidates:
            return max(max(sl_candidates), default_sl)
        return default_sl

    # ==================================================================
    # DETECTOR 1 — PATTERN ENGINE  (wraps existing GodMode logic)
    # ==================================================================
    def _detect_pattern(self, c: dict) -> Optional[dict]:
        """
        Inline reproduction of GodModeAnalyst.detect_structure_advanced()
        wrapped in the standard detector interface.
        """
        df = c["history_df"]
        if df is None or df.empty or len(df) < 3:
            return None

        c1 = df.iloc[-3]
        c2 = df.iloc[-2]
        c3 = df.iloc[-1]

        def stats(row):
            body = abs(row["close"] - row["open"])
            d = 1 if row["close"] > row["open"] else -1
            uw = row["high"] - max(row["open"], row["close"])
            tr = row["high"] - row["low"]
            if tr == 0:
                tr = 0.05
            return body, d, uw, tr

        b1, d1, uw1, r1 = stats(c1)
        b2, d2, uw2, r2 = stats(c2)
        b3, d3, uw3, r3 = stats(c3)

        recent_vol = df["volume"].iloc[-20:-1]
        avg_vol = recent_vol.mean()
        std_vol = recent_vol.std()
        cur_vol = c3["volume"]
        z = (cur_vol - avg_vol) / std_vol if std_vol > 0 else 0

        pattern = None
        confidence = "HIGH"

        # -- Bearish Engulfing --
        if d2 == 1 and d3 == -1 and b3 > b2 and c3["close"] < c2["open"] and z > 0:
            pattern = "BEARISH_ENGULFING"

        # -- Evening Star --
        if pattern is None and d2 == 1 and b2 < (r2 * 0.3) and d3 == -1:
            if c3["close"] < (c1["open"] + c1["close"]) / 2:
                pattern = "EVENING_STAR"

        # -- Shooting Star --
        if pattern is None and uw3 > (2 * b3) and z > 1.5:
            pattern = "SHOOTING_STAR"

        # -- Absorption Doji --
        if pattern is None and z > 2.0 and b3 < (c3["close"] * 0.0005):
            pattern = "ABSORPTION_DOJI"
            confidence = "EXTREME"

        # -- Momentum Breakdown --
        if pattern is None:
            avg_body = df["high"].iloc[-20:-1].sub(df["low"].iloc[-20:-1]).abs().mean()
            if avg_body == 0:
                avg_body = 0.1
            is_big_red = d3 == -1 and b3 > (1.2 * avg_body)
            is_high_vol = (
                z > 2.0
                or (b3 > 1.5 * avg_body and z > 1.2)
                or (b3 > 3.0 * avg_body)
            )
            closes_low = (c3["close"] - c3["low"]) < (r3 * 0.35)
            if is_big_red and is_high_vol and closes_low:
                pattern = "MOMENTUM_BREAKDOWN"

        # -- Volume Trap --
        if pattern is None:
            prev_vol = c2["volume"]
            prev_z = (prev_vol - avg_vol) / std_vol if std_vol > 0 else 0
            if d2 == 1 and prev_z > 1.5 and d3 == -1 and c3["close"] < c2["low"]:
                pattern = "VOLUME_TRAP"

        if pattern is None:
            return None

        return {
            "trigger": f"PATTERN_{pattern}",
            "confidence": confidence,
            "entry_level": c3["low"],
            "reasoning": f"{pattern} on {c['symbol']} (Vol Z={z:.1f})",
            "metrics": {"volume_zscore": z, "pattern_type": pattern},
        }

    # ==================================================================
    # DETECTOR 2 — TRAPPED POSITION
    # ==================================================================
    def _detect_trapped_longs(self, c: dict) -> Optional[dict]:
        df = c["history_df"]
        depth = c.get("depth")
        day_high = c["day_high"]
        day_low = c.get("day_low", df["low"].min())
        day_range = day_high - day_low
        if day_range <= 0:
            return None

        if len(df) < 35:
            return None

        vol_mean = df["volume"].iloc[-30:-5].mean()
        vol_std = df["volume"].iloc[-30:-5].std()
        if vol_std == 0:
            return None

        ltp = c["ltp"]

        for i in range(-5, -1):
            candle = df.iloc[i]
            dist_from_high = day_high - candle["high"]
            if dist_from_high > (day_range * 0.1):
                continue

            z = (candle["volume"] - vol_mean) / vol_std
            if z <= 1.5:
                continue

            trap_low = candle["low"]
            if ltp >= trap_low:
                continue

            # Orderflow confirmation (optional)
            confidence = "MEDIUM"
            ratio_str = "N/A"
            if depth:
                bid = depth.get("totalbuyqty", depth.get("totalbidqty", 0))
                ask = depth.get("totalsellqty", depth.get("totalaskqty", 0))
                if bid > 0 and ask > 0:
                    ratio = bid / ask
                    ratio_str = f"{ratio:.2f}"
                    if ratio < 0.7:
                        confidence = "HIGH"
                    elif ratio < 0.9:
                        confidence = "MEDIUM"

            return {
                "trigger": "TRAPPED_LONGS",
                "confidence": confidence,
                "entry_level": trap_low,
                "reasoning": (
                    f"Heavy volume ({z:.1f}σ) at {candle['high']:.2f}, "
                    f"broken below {trap_low:.2f}. Bid/Ask: {ratio_str}"
                ),
                "metrics": {
                    "trap_high": candle["high"],
                    "trap_low": trap_low,
                    "volume_zscore": z,
                    "sl_level": candle["high"] * 1.002,
                },
            }
        return None

    # ==================================================================
    # DETECTOR 3 — ABSORPTION
    # ==================================================================
    def _detect_absorption(self, c: dict) -> Optional[dict]:
        df = c["history_df"]
        if len(df) < 30:
            return None

        current = df.iloc[-1]
        day_high = c["day_high"]

        if current["high"] < day_high * 0.998:
            return None

        body = abs(current["close"] - current["open"])
        body_pct = (body / current["close"]) * 100 if current["close"] > 0 else 999

        vol_mean = df["volume"].iloc[-30:-1].mean()
        vol_std = df["volume"].iloc[-30:-1].std()
        if vol_std == 0:
            return None
        z = (current["volume"] - vol_mean) / vol_std

        if z <= 2.0 or body_pct >= 0.15:
            return None

        uw = current["high"] - max(current["open"], current["close"])
        tr = current["high"] - current["low"]
        wick_ratio = (uw / tr) if tr > 0 else 0

        if z > 3.0 and wick_ratio > 0.5:
            confidence = "EXTREME"
        elif z > 2.5:
            confidence = "HIGH"
        else:
            confidence = "MEDIUM"

        return {
            "trigger": "ABSORPTION",
            "confidence": confidence,
            "entry_level": current["low"],
            "reasoning": (
                f"Absorption at day high: Vol {z:.1f}σ, "
                f"body {body_pct:.2f}%, wick {wick_ratio*100:.0f}%"
            ),
            "metrics": {
                "volume_zscore": z,
                "body_pct": body_pct,
                "wick_ratio": wick_ratio,
                "sl_level": day_high * 1.003,
            },
        }

    # ==================================================================
    # DETECTOR 4 — BAD HIGH
    # ==================================================================
    def _detect_bad_high(self, c: dict) -> Optional[dict]:
        df = c["history_df"]
        depth = c.get("depth")
        if not depth:
            return None

        current = df.iloc[-1]
        day_high = c["day_high"]

        if current["high"] < day_high * 0.999:
            return None

        bid = depth.get("totalbuyqty", depth.get("totalbidqty", 0))
        ask = depth.get("totalsellqty", depth.get("totalaskqty", 0))
        if bid == 0:
            return None

        sell_buy = ask / bid
        if sell_buy <= 2.5:
            return None

        uw = current["high"] - max(current["open"], current["close"])
        tr = current["high"] - current["low"]
        wick_pct = (uw / tr * 100) if tr > 0 else 0

        if wick_pct <= 40:
            return None

        confidence = "EXTREME" if sell_buy > 4.0 else "HIGH"
        return {
            "trigger": "BAD_HIGH",
            "confidence": confidence,
            "entry_level": current["low"],
            "reasoning": (
                f"Day high {day_high:.2f} with sell walls "
                f"({sell_buy:.1f}x) and {wick_pct:.0f}% rejection wick"
            ),
            "metrics": {
                "sell_buy_ratio": sell_buy,
                "wick_pct": wick_pct,
                "total_bid": bid,
                "total_ask": ask,
                "sl_level": day_high * 1.003,
            },
        }

    # ==================================================================
    # DETECTOR 5 — FAILED AUCTION  (Simplified in Phase 41.1)
    # ==================================================================
    def _detect_failed_auction(self, c: dict) -> Optional[dict]:
        """
        Simplified: Recent range breakout that failed.
        Works from 9:45 AM onwards (30-candle minimum, down from 60).
        """
        df = c["history_df"]
        if len(df) < 30:
            return None

        # Define recent range from 10-30 candles ago
        range_df = df.iloc[-30:-10]
        range_high = range_df["high"].max()
        range_low = range_df["low"].min()

        # Was there a breakout attempt in last 10 candles?
        recent = df.tail(10)
        breakout_occurred = any(c_row.high > range_high for c_row in recent.itertuples())
        if not breakout_occurred:
            return None

        # Check if price has failed back below range high
        ltp = c["ltp"]
        if ltp >= range_high * 0.998:
            return None  # Still above — not a failure

        # Count how many candles held above (acceptance quality)
        held_above = sum(1 for c_row in recent.itertuples() if c_row.close > range_high)
        confidence = "HIGH" if held_above >= 3 else "MEDIUM"

        return {
            "trigger": "FAILED_AUCTION",
            "confidence": confidence,
            "entry_level": range_high * 0.998,
            "reasoning": (
                f"Broke above {range_high:.2f}, held {held_above} candles, "
                f"now failed back to {ltp:.2f}"
            ),
            "metrics": {
                "range_high": range_high,
                "range_low": range_low,
                "held_candles": held_above,
                "sl_level": range_high * 1.005,
            },
        }

    # ==================================================================
    # DETECTOR 6 — OI DIVERGENCE PROXY  (COMMENTED OUT — Phase 41.1)
    # Rationale: Always MEDIUM confidence, fires on lunch-hour chop,
    # news reactions, HFT activity.  Without real OI data this is noise.
    # ==================================================================
    # def _detect_oi_divergence_proxy(self, c: dict) -> Optional[dict]:
    #     df = c["history_df"]
    #     if len(df) < 25:
    #         return None
    #     recent = df.tail(5)
    #     p_start = recent.iloc[0]["close"]
    #     p_end = recent.iloc[-1]["close"]
    #     momentum = ((p_end - p_start) / p_start) * 100
    #     recent_vol = recent["volume"].sum()
    #     base_vol = df["volume"].iloc[-25:-5].sum()
    #     if base_vol == 0:
    #         return None
    #     surge = recent_vol / (base_vol / 4)
    #     if surge <= 3.0 or momentum >= 0.5:
    #         return None
    #     return {
    #         "trigger": "OI_DIVERGENCE_PROXY",
    #         "confidence": "MEDIUM",
    #         "entry_level": recent.iloc[-1]["low"],
    #         "reasoning": (
    #             f"Volume surge {surge:.1f}x but momentum only "
    #             f"{momentum:.2f}% = weak rally, potential exhaustion"
    #         ),
    #         "metrics": {"volume_surge": surge, "momentum_pct": momentum},
    #     }

    # ==================================================================
    # DETECTOR 7 — TPO POOR HIGH  (COMMENTED OUT — Phase 41.1)
    # Rationale: O(n×50) nested loop = 50-100ms/candidate overhead.
    # 50-bucket granularity on wide price ranges = 95% information loss.
    # Bad High detector already covers this scenario with orderflow.
    # ==================================================================
    # def _detect_poor_high(self, c: dict) -> Optional[dict]:
    #     df = c["history_df"]
    #     ltp = c["ltp"]
    #     day_high = c["day_high"]
    #     if ltp < day_high * 0.995:
    #         return None
    #     profile = self._tpo_profile(df)
    #     if not profile:
    #         return None
    #     closest = min(profile.keys(), key=lambda x: abs(x - ltp))
    #     tpo_here = profile[closest]
    #     avg_tpo = sum(profile.values()) / len(profile)
    #     if avg_tpo == 0 or tpo_here >= avg_tpo * 0.4:
    #         return None
    #     return {
    #         "trigger": "TPO_POOR_HIGH",
    #         "confidence": "MEDIUM",
    #         "entry_level": closest * 0.999,
    #         "reasoning": (
    #             f"Price at {ltp:.2f} has thin TPO acceptance "
    #             f"({tpo_here} vs avg {avg_tpo:.0f}) = vulnerable level"
    #         ),
    #         "metrics": {
    #             "tpo_count": tpo_here,
    #             "avg_tpo": avg_tpo,
    #             "tpo_ratio": tpo_here / avg_tpo,
    #         },
    #     }
    #
    # @staticmethod
    # def _tpo_profile(df) -> dict:
    #     price_min = df["low"].min()
    #     price_max = df["high"].max()
    #     rng = price_max - price_min
    #     if rng == 0:
    #         return {}
    #     num = 50
    #     tick = rng / num
    #     counts: Dict[float, int] = {}
    #     for row in df.itertuples():
    #         lo = int((row.low - price_min) / tick)
    #         hi = int((row.high - price_min) / tick)
    #         for b in range(lo, min(hi + 1, num)):
    #             level = round(price_min + b * tick, 2)
    #             counts[level] = counts.get(level, 0) + 1
    #     return counts

    # ==================================================================
    # DETECTOR 8 — MOMENTUM EXHAUSTION  (COMMENTED OUT — Phase 41.1)
    # Rationale: Logically redundant with Gate 7 (Train Filter).
    # Gate 7 blocks aggressive momentum; this detects exhausted momentum.
    # Checking the SAME phenomenon from opposite angles creates confusion.
    # Existing patterns (Shooting Star, Momentum Breakdown, Evening Star)
    # already capture exhaustion setups.
    # ==================================================================
    # def _detect_momentum_exhaustion(self, c: dict) -> Optional[dict]:
    #     df = c["history_df"]
    #     ltp = c["ltp"]
    #     vwap = c.get("vwap", 0)
    #     if vwap == 0 or len(df) < 20:
    #         return None
    #     std = df["close"].std()
    #     if std == 0:
    #         return None
    #     vwap_sd = (ltp - vwap) / std
    #     consec_green = 0
    #     for row in df.iloc[::-1].itertuples():
    #         if row.close > row.open:
    #             consec_green += 1
    #         else:
    #             break
    #     if vwap_sd <= 2.5 or consec_green < 7:
    #         return None
    #     recent_vol = df["volume"].tail(5).mean()
    #     prior_vol = df["volume"].iloc[-20:-5].mean()
    #     vol_declining = prior_vol > 0 and recent_vol < prior_vol * 0.7
    #     round_numbers = [50, 100, 150, 200, 250, 500, 1000, 1500, 2000, 2500, 5000]
    #     near_round = any(
    #         abs(ltp - rn) / ltp < 0.005 for rn in round_numbers if ltp > 0
    #     )
    #     if vol_declining and near_round:
    #         confidence = "HIGH"
    #     elif vol_declining or near_round:
    #         confidence = "MEDIUM"
    #     else:
    #         confidence = "MEDIUM"
    #     return {
    #         "trigger": "MOMENTUM_EXHAUSTION",
    #         "confidence": confidence,
    #         "entry_level": df.iloc[-1]["low"],
    #         "reasoning": (
    #             f"Extended {vwap_sd:.1f}σ above VWAP after "
    #             f"{consec_green} green candles. Vol declining: {vol_declining}"
    #         ),
    #         "metrics": {
    #             "vwap_extension_sd": vwap_sd,
    #             "consecutive_green": consec_green,
    #             "volume_declining": vol_declining,
    #             "near_round_number": near_round,
    #         },
    #     }
