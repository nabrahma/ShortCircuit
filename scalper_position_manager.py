"""
Phase 41.2: Scalper Position Manager
Manages open positions with fast breakeven, immediate trailing, and scale-out TPs.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime

import config
from scalper_risk_calculator import ScalperRiskCalculator

logger = logging.getLogger(__name__)


@dataclass
class ScalperPosition:
    """Tracks state of an active scalper-managed position."""

    symbol: str
    entry_price: float
    entry_time: datetime
    initial_quantity: int
    remaining_quantity: int
    direction: str  # 'short'

    initial_stop: float
    current_stop: float

    tp1: float
    tp2: float
    tp3: float

    breakeven_threshold_points: float
    breakeven_triggered: bool = False

    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False

    trailing_distance_pct: float = field(default=None)

    realized_pnl_points: float = 0.0
    peak_profit_points: float = 0.0

    def __post_init__(self):
        if self.trailing_distance_pct is None:
            self.trailing_distance_pct = getattr(
                config, "SCALPER_TRAILING_DISTANCE_INITIAL", 0.002
            )


class ScalperPositionManager:
    """
    Manages position lifecycle: breakeven → trailing → TP1 → TP2 → TP3.
    Called every 2 seconds with current LTP, returns action dicts.
    """

    def __init__(self, trade_manager, logger_override=None):
        self.trade_manager = trade_manager
        self.log = logger_override or logger
        self.risk_calc = ScalperRiskCalculator()
        self.active_position = None

    def start_position(
        self,
        symbol: str,
        entry_price: float,
        quantity: int,
        setup_high: float,
        tick_size: float,
    ):
        """
        Initialize position management after trade execution.

        Args:
            symbol: NSE:SYMBOL-EQ format
            entry_price: Actual fill price
            quantity: Number of shares
            setup_high: High of the setup candle (for SL calc)
            tick_size: Instrument tick size
        """
        stop_info = self.risk_calc.calculate_initial_stop(setup_high, tick_size)
        tp_info = self.risk_calc.calculate_profit_targets(entry_price, "short")
        be_threshold = self.risk_calc.calculate_breakeven_threshold(entry_price)

        self.active_position = ScalperPosition(
            symbol=symbol,
            entry_price=entry_price,
            entry_time=datetime.now(),
            initial_quantity=quantity,
            remaining_quantity=quantity,
            direction="short",
            initial_stop=stop_info["recommended_stop"],
            current_stop=stop_info["recommended_stop"],
            tp1=tp_info["tp1"],
            tp2=tp_info["tp2"],
            tp3=tp_info["tp3"],
            breakeven_threshold_points=be_threshold,
        )

        self.log.info(
            f"[SCALPER] Position started: {symbol} @ ₹{entry_price:.2f}, "
            f"SL ₹{stop_info['recommended_stop']:.2f} "
            f"({stop_info['risk_pct']*100:.2f}% risk), "
            f"TPs: ₹{tp_info['tp1']:.2f} / ₹{tp_info['tp2']:.2f} / ₹{tp_info['tp3']:.2f}"
        )

    def update_position(self, current_ltp: float) -> dict:
        """
        Called every 2 seconds. Evaluates position and returns action.

        Returns:
            dict with 'action' key:
              - 'NO_POSITION': no active position
              - 'NONE': no action needed
              - 'UPDATE_STOP': stop moved (new_stop, reason)
              - 'CLOSE_PARTIAL': scale-out (quantity, reason, price)
              - 'STOP_HIT': stopped out (quantity, reason, price)
              - 'CLOSE_ALL': full close at TP3 (quantity, reason, price)
        """
        if self.active_position is None:
            return {"action": "NO_POSITION"}

        pos = self.active_position

        # Calculate profit (shorts: entry - current)
        profit_points = pos.entry_price - current_ltp
        profit_pct = profit_points / pos.entry_price if pos.entry_price > 0 else 0

        # Track peak profit
        if profit_points > pos.peak_profit_points:
            pos.peak_profit_points = profit_points

        # ── PHASE 6: Stop Loss Check (checked FIRST for safety) ──────────
        if current_ltp >= pos.current_stop:
            self.log.info(
                f"[SCALPER] STOP HIT {pos.symbol} @ ₹{current_ltp:.2f} "
                f"(SL was ₹{pos.current_stop:.2f})"
            )
            qty = pos.remaining_quantity
            self.active_position = None  # Position closed
            return {
                "action": "STOP_HIT",
                "quantity": qty,
                "reason": "STOP_LOSS",
                "price": current_ltp,
            }

        # ── PHASE 5: TP3 Check — Close all (before TP1/TP2 to catch gap-downs) ─
        if pos.tp2_hit and not pos.tp3_hit and current_ltp <= pos.tp3:
            self.log.info(f"[SCALPER] TP3 HOME RUN {pos.symbol} @ ₹{current_ltp:.2f}")
            pos.tp3_hit = True
            qty = pos.remaining_quantity
            pos.realized_pnl_points += qty * profit_points
            pos.remaining_quantity = 0
            self.active_position = None  # Position fully closed
            return {
                "action": "CLOSE_ALL",
                "quantity": qty,
                "reason": "TP3_HOME_RUN",
                "price": current_ltp,
            }

        # ── PHASE 4: TP2 Check — Close another 50% of original (75% total) ──
        if pos.tp1_hit and not pos.tp2_hit and current_ltp <= pos.tp2:
            self.log.info(f"[SCALPER] TP2 HIT {pos.symbol} @ ₹{current_ltp:.2f}")
            pos.tp2_hit = True
            # Close another 50% of original = 25% of original (since 50% already closed at TP1)
            close_qty = int(pos.initial_quantity * 0.25)
            if close_qty < 1:
                close_qty = 1
            if close_qty > pos.remaining_quantity:
                close_qty = pos.remaining_quantity

            pos.remaining_quantity -= close_qty
            pos.realized_pnl_points += close_qty * profit_points
            pos.trailing_distance_pct = getattr(
                config, "SCALPER_TRAILING_DISTANCE_AFTER_TP2", 0.001
            )

            # If nothing remains, close fully
            if pos.remaining_quantity <= 0:
                self.active_position = None
                return {
                    "action": "CLOSE_ALL",
                    "quantity": close_qty,
                    "reason": "TP2_FULL_CLOSE",
                    "price": current_ltp,
                }

            return {
                "action": "CLOSE_PARTIAL",
                "quantity": close_qty,
                "reason": "TP2_HIT",
                "price": current_ltp,
            }

        # ── PHASE 3: TP1 Check — Close 50% ──────────────────────────────
        if not pos.tp1_hit and current_ltp <= pos.tp1:
            self.log.info(f"[SCALPER] TP1 HIT {pos.symbol} @ ₹{current_ltp:.2f}")
            pos.tp1_hit = True
            close_qty = int(pos.remaining_quantity * 0.5)
            if close_qty < 1:
                close_qty = 1
            pos.remaining_quantity -= close_qty
            pos.realized_pnl_points += close_qty * profit_points
            pos.trailing_distance_pct = getattr(
                config, "SCALPER_TRAILING_DISTANCE_AFTER_TP1", 0.0015
            )

            # If nothing remains (qty was 1), close fully
            if pos.remaining_quantity <= 0:
                self.active_position = None
                return {
                    "action": "CLOSE_ALL",
                    "quantity": close_qty,
                    "reason": "TP1_FULL_CLOSE",
                    "price": current_ltp,
                }

            return {
                "action": "CLOSE_PARTIAL",
                "quantity": close_qty,
                "reason": "TP1_HIT",
                "price": current_ltp,
            }

        # ── PHASE 1: Breakeven Trigger ───────────────────────────────────
        if not pos.breakeven_triggered:
            if profit_points >= pos.breakeven_threshold_points:
                self.log.info(
                    f"[SCALPER] BREAKEVEN triggered {pos.symbol} "
                    f"at +{profit_pct*100:.2f}%"
                )
                pos.current_stop = pos.entry_price + 0.20  # Tiny buffer above entry
                pos.breakeven_triggered = True
                return {
                    "action": "UPDATE_STOP",
                    "new_stop": pos.current_stop,
                    "reason": "BREAKEVEN_HIT",
                }

        # ── PHASE 2: Aggressive Trailing (after breakeven) ───────────────
        if pos.breakeven_triggered:
            trailing_distance = pos.entry_price * pos.trailing_distance_pct
            # For shorts: proposed stop is ABOVE current price
            proposed_stop = current_ltp + trailing_distance

            # Only tighten (move stop DOWN for shorts), never loosen
            if proposed_stop < pos.current_stop:
                pos.current_stop = round(proposed_stop, 2)
                return {
                    "action": "UPDATE_STOP",
                    "new_stop": pos.current_stop,
                    "reason": "TRAILING_UPDATE",
                }

        # No action needed
        return {"action": "NONE"}

    def get_position_summary(self) -> dict:
        """Returns current position state for logging/display."""
        if self.active_position is None:
            return {"active": False}

        pos = self.active_position
        return {
            "active": True,
            "symbol": pos.symbol,
            "entry": pos.entry_price,
            "current_stop": pos.current_stop,
            "breakeven": pos.breakeven_triggered,
            "tp1_hit": pos.tp1_hit,
            "tp2_hit": pos.tp2_hit,
            "tp3_hit": pos.tp3_hit,
            "remaining_qty": pos.remaining_quantity,
            "realized_pnl": pos.realized_pnl_points,
            "peak_profit": pos.peak_profit_points,
        }
