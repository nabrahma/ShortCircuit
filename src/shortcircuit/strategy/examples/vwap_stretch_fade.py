"""
example_vwap_stretch_fade — the smallest complete strategy.

Thesis: a stock far above its session VWAP whose short-term VWAP slope is rolling
over tends to mean-revert. Short the break of the previous bar's low; stop above
the day's high; take profit on the engine's default ladder toward VWAP.

ILLUSTRATION ONLY. The thresholds are round numbers, not fitted ones, and the
idea has not been measured. Use it to learn the contract, then replace it.

Enable it with:  SHORTCIRCUIT_STRATEGIES=example_vwap_stretch_fade
"""
from __future__ import annotations

from typing import Optional

from shortcircuit.strategy.sdk import Direction, ExitPlan, SetupContext, Signal, Strategy


class VwapStretchFade(Strategy):
    name = "example_vwap_stretch_fade"
    direction = Direction.SHORT
    requires_htf_confirmation = False

    MIN_GAIN_PCT = 8.0
    MIN_VWAP_SD = 3.0
    MAX_SPREAD = 0.004

    def evaluate(self, ctx: SetupContext) -> Optional[Signal]:
        # Risk pre-filters: never short into a limit-up move or a wide book.
        if ctx.is_circuit_hitter:
            return None
        if ctx.upper_circuit and ctx.ltp >= ctx.upper_circuit * 0.985:
            return None
        if ctx.spread_pct > self.MAX_SPREAD:
            return None

        # The pattern.
        if ctx.gain_pct < self.MIN_GAIN_PCT or ctx.vwap_sd < self.MIN_VWAP_SD:
            return None
        if not ctx.is_decaying:
            return None

        return Signal(
            pattern="VWAP_STRETCH_FADE",
            stop_reference=ctx.day_high,
            confidence="MEDIUM",
            stretch_score=ctx.vwap_sd,
            exit=ExitPlan(tp_atr_mult=1.0),
        )
