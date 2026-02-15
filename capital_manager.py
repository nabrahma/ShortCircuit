"""
Phase 42.1: Capital Management Module

Tracks available capital with 5× intraday leverage.
Prevents orders when funds are insufficient.

Usage:
    cm = CapitalManager(base_capital=1800.0, leverage=5.0)
    check = cm.can_afford('NSE:RELIANCE-EQ', 2800.0)
    if check['allowed']:
        # place order...
        cm.allocate('NSE:RELIANCE-EQ', 2800.0)
    # on exit:
    cm.release('NSE:RELIANCE-EQ')
"""

import logging

logger = logging.getLogger(__name__)


class CapitalManager:
    """
    Simple capital tracker with 5× leverage.
    Prevents orders when funds insufficient.
    """

    def __init__(self, base_capital: float = 1800.0, leverage: float = 5.0):
        """
        Args:
            base_capital: Your actual capital (₹1,800)
            leverage: Fixed at 5.0 (NSE intraday)
        """
        self.base_capital = base_capital
        self.leverage = leverage
        self.total_buying_power = base_capital * leverage  # ₹9,000
        self.available = self.total_buying_power

        # Track active positions: {symbol: capital_allocated}
        self.positions = {}

        logger.info(f"💰 Capital Manager initialized:")
        logger.info(f"   Base: ₹{base_capital:.2f}")
        logger.info(f"   Leverage: {leverage}×")
        logger.info(f"   Buying Power: ₹{self.total_buying_power:.2f}")

    def can_afford(self, symbol: str, cost: float) -> dict:
        """
        Check if we can afford a trade.

        Args:
            symbol: NSE:SYMBOL-EQ
            cost: Required capital (LTP × qty)

        Returns:
            {
                'allowed': bool,
                'reason': str,
                'available': float,
                'required': float
            }
        """
        # Check 1: Already holding this symbol?
        if symbol in self.positions:
            return {
                'allowed': False,
                'reason': 'ALREADY_HOLDING',
                'available': self.available,
                'required': cost
            }

        # Check 2: Sufficient capital?
        if cost > self.available:
            return {
                'allowed': False,
                'reason': 'INSUFFICIENT_FUNDS',
                'available': self.available,
                'required': cost
            }

        # All good
        return {
            'allowed': True,
            'reason': 'OK',
            'available': self.available,
            'required': cost
        }

    def allocate(self, symbol: str, cost: float):
        """
        Reserve capital for a position.
        Call AFTER order is successfully placed.
        """
        if symbol in self.positions:
            logger.warning(f"⚠️ Already allocated capital for {symbol}, skipping")
            return

        self.positions[symbol] = cost
        self.available -= cost

        logger.info(f"✅ Capital allocated: {symbol}")
        logger.info(f"   Used: ₹{cost:.2f}")
        logger.info(f"   Available: ₹{self.available:.2f} / ₹{self.total_buying_power:.2f}")

    def release(self, symbol: str):
        """
        Free up capital when position closes.
        Call AFTER exit order is filled.
        """
        if symbol not in self.positions:
            logger.warning(f"⚠️ No capital allocated for {symbol}, skipping release")
            return

        cost = self.positions[symbol]
        self.available += cost
        del self.positions[symbol]

        logger.info(f"✅ Capital released: {symbol}")
        logger.info(f"   Freed: ₹{cost:.2f}")
        logger.info(f"   Available: ₹{self.available:.2f} / ₹{self.total_buying_power:.2f}")

    def get_status(self) -> dict:
        """Get current status for logging/Telegram."""
        return {
            'base_capital': self.base_capital,
            'leverage': self.leverage,
            'total_buying_power': self.total_buying_power,
            'available': self.available,
            'in_use': self.total_buying_power - self.available,
            'positions_count': len(self.positions),
            'positions': dict(self.positions)
        }
