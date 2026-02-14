"""
Phase 41.2: Scalper Risk Calculator
Pure calculation module — no API calls, no side effects.
Computes tight structure-based stops, profit targets, and breakeven thresholds.
"""

import config


class ScalperRiskCalculator:
    """
    Calculates scalper-optimized risk parameters.
    Replaces ATR-based swing logic with tick-based structure stops.
    """

    def calculate_initial_stop(
        self,
        setup_candle_high: float,
        tick_size: float = 0.05,
        hunt_buffer_enabled: bool = None,
    ) -> dict:
        """
        Calculate initial stop loss using structure + tick buffer.

        Args:
            setup_candle_high: Highest price of the setup candle
            tick_size: Instrument tick size (e.g. 0.05 for most NSE stocks)
            hunt_buffer_enabled: Add 0.3% buffer to avoid stop hunts.
                                 Defaults to config value if None.

        Returns:
            dict with base_stop, hunt_buffered_stop, risk_per_share,
            risk_pct, recommended_stop
        """
        if hunt_buffer_enabled is None:
            hunt_buffer_enabled = getattr(
                config, "SCALPER_STOP_HUNT_BUFFER_ENABLED", True
            )

        tick_buffer = getattr(config, "SCALPER_STOP_TICK_BUFFER", 12)
        hunt_pct = getattr(config, "SCALPER_STOP_HUNT_BUFFER_PCT", 0.003)

        # Base stop: setup high + N ticks
        base_buffer = tick_size * tick_buffer
        base_stop = round(setup_candle_high + base_buffer, 2)

        # Hunt-buffered stop: base + 0.3% of setup high
        hunt_buffer = round(setup_candle_high * hunt_pct, 2)
        hunt_buffered_stop = round(base_stop + hunt_buffer, 2)

        # Choose recommended stop
        recommended_stop = hunt_buffered_stop if hunt_buffer_enabled else base_stop

        # Estimate risk (assume entry ~0.3% below setup high)
        estimated_entry = round(setup_candle_high * 0.997, 2)
        risk_per_share = round(recommended_stop - estimated_entry, 2)
        risk_pct = round(risk_per_share / estimated_entry, 4) if estimated_entry > 0 else 0

        return {
            "base_stop": base_stop,
            "hunt_buffered_stop": hunt_buffered_stop,
            "risk_per_share": risk_per_share,
            "risk_pct": risk_pct,
            "recommended_stop": recommended_stop,
        }

    def calculate_profit_targets(
        self, entry_price: float, direction: str = "short"
    ) -> dict:
        """
        Calculate 3-tier profit targets.

        Args:
            entry_price: Actual fill price
            direction: 'short' or 'long'

        Returns:
            dict with tp1, tp2, tp3, tp1_pct, tp2_pct, tp3_pct
        """
        tp1_pct = getattr(config, "SCALPER_TP1_PCT", 0.015)
        tp2_pct = getattr(config, "SCALPER_TP2_PCT", 0.025)
        tp3_pct = getattr(config, "SCALPER_TP3_PCT", 0.035)

        if direction == "short":
            tp1 = round(entry_price * (1 - tp1_pct), 2)
            tp2 = round(entry_price * (1 - tp2_pct), 2)
            tp3 = round(entry_price * (1 - tp3_pct), 2)
        else:
            tp1 = round(entry_price * (1 + tp1_pct), 2)
            tp2 = round(entry_price * (1 + tp2_pct), 2)
            tp3 = round(entry_price * (1 + tp3_pct), 2)

        return {
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "tp1_pct": tp1_pct * 100,
            "tp2_pct": tp2_pct * 100,
            "tp3_pct": tp3_pct * 100,
        }

    def calculate_breakeven_threshold(self, entry_price: float) -> float:
        """
        Calculate profit points needed to trigger breakeven move.

        Args:
            entry_price: Actual fill price

        Returns:
            float — profit in points required for breakeven
        """
        trigger_pct = getattr(config, "SCALPER_BREAKEVEN_TRIGGER_PCT", 0.003)
        return round(entry_price * trigger_pct, 2)
