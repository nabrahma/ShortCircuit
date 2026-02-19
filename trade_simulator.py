"""
Phase 41.2: Trade Simulator
Simulates legacy (ATR) vs scalper (structure) risk systems on historical price data.
Used for EOD "what-if" analysis without live trading.
"""

import logging

import pandas as pd
import sys
import os

# Add Parent Directory to Path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from scalper_risk_calculator import ScalperRiskCalculator

logger = logging.getLogger(__name__)


class TradeSimulator:
    """
    Simulates trade outcomes for both legacy and scalper risk systems.
    Processes 1-min candle DataFrames from signal time to EOD.
    """

    def __init__(self):
        self.risk_calc = ScalperRiskCalculator()

    def simulate_legacy_system(self, signal: dict, price_history_df: pd.DataFrame) -> dict:
        """
        Simulate Phase 41.1 (legacy) ATR-based system.

        Logic:
          - Stop = setup_high + (ATR * 0.5)
          - Breakeven at 1R profit
          - Trailing starts at 2R profit
          - Trail distance = 0.5R

        Args:
            signal: dict with entry_price, setup_high, atr, quantity
            price_history_df: 1-min candles from signal time to EOD

        Returns:
            dict with system, entry, exit, exit_reason, pnl_points,
            pnl_pct, pnl_cash, breakeven_hit, trailing_hit
        """
        entry_price = signal["entry_price"]
        setup_high = signal["setup_high"]
        atr = signal.get("atr", entry_price * 0.01)  # Fallback 1%
        quantity = signal.get("quantity", int(config.CAPITAL / entry_price))

        stop_loss = setup_high + (atr * 0.5)
        risk = stop_loss - entry_price  # Risk in points (positive for shorts)

        breakeven_activated = False
        trailing_activated = False
        trailing_stop = stop_loss
        exit_price = None
        exit_reason = None

        for _, candle in price_history_df.iterrows():
            current_high = candle["high"]
            current_low = candle["low"]

            # Check stop hit (for shorts: high breaches stop)
            if current_high >= trailing_stop:
                exit_price = trailing_stop
                exit_reason = "STOP_HIT"
                break

            profit_points = entry_price - current_low

            # Check breakeven (1R)
            if not breakeven_activated and profit_points >= risk:
                trailing_stop = entry_price + 0.20
                breakeven_activated = True

            # Check trailing activation (2R)
            if breakeven_activated and not trailing_activated:
                if profit_points >= risk * 2:
                    trailing_activated = True

            # Update trailing stop
            if trailing_activated:
                proposed_stop = current_low + (risk * 0.5)
                if proposed_stop < trailing_stop:
                    trailing_stop = proposed_stop

        # EOD close if not stopped
        if exit_price is None:
            exit_price = price_history_df.iloc[-1]["close"]
            exit_reason = "EOD_CLOSE"

        pnl_points = entry_price - exit_price
        pnl_pct = pnl_points / entry_price if entry_price > 0 else 0
        pnl_cash = pnl_points * quantity

        return {
            "system": "LEGACY",
            "entry": round(entry_price, 2),
            "exit": round(exit_price, 2),
            "exit_reason": exit_reason,
            "pnl_points": round(pnl_points, 2),
            "pnl_pct": round(pnl_pct, 4),
            "pnl_cash": round(pnl_cash, 2),
            "breakeven_hit": breakeven_activated,
            "trailing_hit": trailing_activated,
        }

    def simulate_scalper_system(self, signal: dict, price_history_df: pd.DataFrame) -> dict:
        """
        Simulate Phase 41.2 scalper system.

        Logic:
          - Stop = tick-based + hunt buffer
          - Breakeven at 0.3% profit
          - Immediate trailing after breakeven
          - Scale-out at 1.5% / 2.5% / 3.5% TPs

        Args:
            signal: dict with entry_price, setup_high, tick_size, quantity
            price_history_df: 1-min candles from signal time to EOD

        Returns:
            dict with system, entry, exit, exit_reason, pnl_points,
            pnl_pct, pnl_cash, breakeven_hit, tp1_hit, tp2_hit, tp3_hit
        """
        entry_price = signal["entry_price"]
        setup_high = signal["setup_high"]
        tick_size = signal.get("tick_size", 0.05)
        quantity = signal.get("quantity", int(config.CAPITAL / entry_price))

        stop_info = self.risk_calc.calculate_initial_stop(setup_high, tick_size)
        tp_info = self.risk_calc.calculate_profit_targets(entry_price, "short")
        breakeven_threshold = entry_price * getattr(
            config, "SCALPER_BREAKEVEN_TRIGGER_PCT", 0.003
        )

        trailing_stop = stop_info["recommended_stop"]
        trailing_distance_pct = getattr(config, "SCALPER_TRAILING_DISTANCE_INITIAL", 0.002)

        breakeven_activated = False
        tp1_hit = False
        tp2_hit = False
        tp3_hit = False
        remaining_qty = quantity
        total_pnl_cash = 0.0
        exit_price = None
        exit_reason = None

        for _, candle in price_history_df.iterrows():
            current_high = candle["high"]
            current_low = candle["low"]

            # Check stop hit
            if current_high >= trailing_stop:
                exit_price = trailing_stop
                exit_reason = "STOP_HIT"
                total_pnl_cash += remaining_qty * (entry_price - exit_price)
                remaining_qty = 0
                break

            profit_points = entry_price - current_low

            # Check breakeven (0.3%)
            if not breakeven_activated and profit_points >= breakeven_threshold:
                trailing_stop = entry_price + 0.20
                breakeven_activated = True

            # Trailing (immediate after breakeven)
            if breakeven_activated:
                proposed_stop = current_low + (entry_price * trailing_distance_pct)
                if proposed_stop < trailing_stop:
                    trailing_stop = proposed_stop

            # TP1 check (1.5% — close 50%)
            if not tp1_hit and current_low <= tp_info["tp1"]:
                tp1_hit = True
                close_qty = max(1, int(quantity * 0.5))
                if close_qty > remaining_qty:
                    close_qty = remaining_qty
                total_pnl_cash += close_qty * (entry_price - tp_info["tp1"])
                remaining_qty -= close_qty
                trailing_distance_pct = getattr(
                    config, "SCALPER_TRAILING_DISTANCE_AFTER_TP1", 0.0015
                )

            # TP2 check (2.5% — close another 25% of original)
            if tp1_hit and not tp2_hit and current_low <= tp_info["tp2"]:
                tp2_hit = True
                close_qty = max(1, int(quantity * 0.25))
                if close_qty > remaining_qty:
                    close_qty = remaining_qty
                total_pnl_cash += close_qty * (entry_price - tp_info["tp2"])
                remaining_qty -= close_qty
                trailing_distance_pct = getattr(
                    config, "SCALPER_TRAILING_DISTANCE_AFTER_TP2", 0.001
                )

            # TP3 check (3.5% — close all)
            if tp2_hit and not tp3_hit and current_low <= tp_info["tp3"]:
                tp3_hit = True
                total_pnl_cash += remaining_qty * (entry_price - tp_info["tp3"])
                remaining_qty = 0
                exit_reason = "TP3_HIT"
                exit_price = tp_info["tp3"]
                break

            # All shares closed by TPs
            if remaining_qty <= 0:
                exit_reason = "ALL_TPS_HIT"
                exit_price = tp_info["tp2"] if tp2_hit else tp_info["tp1"]
                break

        # EOD close for remaining shares
        if remaining_qty > 0:
            eod_close = price_history_df.iloc[-1]["close"]
            total_pnl_cash += remaining_qty * (entry_price - eod_close)
            remaining_qty = 0
            if exit_reason is None:
                exit_reason = "EOD_CLOSE"
                exit_price = eod_close

        total_pnl_pct = total_pnl_cash / (entry_price * quantity) if (entry_price * quantity) > 0 else 0

        # Determine display exit price
        if tp1_hit or tp2_hit or tp3_hit:
            display_exit = "PARTIAL_EXITS"
        else:
            display_exit = round(exit_price, 2) if exit_price else "N/A"

        return {
            "system": "SCALPER",
            "entry": round(entry_price, 2),
            "exit": display_exit,
            "exit_price_numeric": round(exit_price, 2) if exit_price else 0,
            "exit_reason": exit_reason or "UNKNOWN",
            "pnl_points": round(total_pnl_cash / quantity, 2) if quantity > 0 else 0,
            "pnl_pct": round(total_pnl_pct, 4),
            "pnl_cash": round(total_pnl_cash, 2),
            "breakeven_hit": breakeven_activated,
            "tp1_hit": tp1_hit,
            "tp2_hit": tp2_hit,
            "tp3_hit": tp3_hit,
        }

    def compare_systems(self, signal: dict, price_history_df: pd.DataFrame) -> dict:
        """
        Run both simulations and compare results.

        Returns:
            dict with legacy, scalper, delta_pnl_pct, delta_pnl_cash,
            better_system, improvement_pct
        """
        legacy = self.simulate_legacy_system(signal, price_history_df)
        scalper = self.simulate_scalper_system(signal, price_history_df)

        delta_pnl_pct = scalper["pnl_pct"] - legacy["pnl_pct"]
        delta_pnl_cash = scalper["pnl_cash"] - legacy["pnl_cash"]
        better = "SCALPER" if delta_pnl_pct > 0 else "LEGACY"

        improvement_pct = 0
        if abs(legacy["pnl_pct"]) > 0:
            improvement_pct = (delta_pnl_pct / abs(legacy["pnl_pct"])) * 100

        return {
            "legacy": legacy,
            "scalper": scalper,
            "delta_pnl_pct": round(delta_pnl_pct, 4),
            "delta_pnl_cash": round(delta_pnl_cash, 2),
            "better_system": better,
            "improvement_pct": round(improvement_pct, 1),
        }
