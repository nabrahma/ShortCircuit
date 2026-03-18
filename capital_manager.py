"""
Phase 44.6: Capital Management Module — Live Fyers Sync Edition

Key changes from Phase 42.1:
- Removed hardcoded base_capital. Real margin fetched from Fyers GET /funds.
- compute_qty() for full margin utilization (replaces floor(available/ltp)).
- acquire_slot() / release_slot() for single-position enforcement.
- Soft observation mode: slot check is SEPARATE from execution block.
  Callers can observe capital status without being hard-blocked.
- Backward-compatible get_status() / can_afford() kept for legacy callers.
"""

import asyncio
import logging
from datetime import datetime
from math import floor
from typing import Optional

logger = logging.getLogger(__name__)


class CapitalManager:
    """
    Live-synced capital tracker.
    Source of truth: Fyers GET /funds → available_margin.
    NOT the hardcoded base_capital from config.
    """

    def __init__(self, leverage: float = 5.0):
        self.leverage = leverage
        self._real_margin: float = 0.0        # always from Fyers, never hardcoded
        self._last_sync: Optional[datetime] = None
        self._position_active: bool = False
        self._active_symbol: Optional[str] = None
        self._lock = asyncio.Lock()

        logger.info(
            f"💰 Capital Manager initialized | leverage={leverage}x | "
            f"real_margin=PENDING (call sync() before trading)"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Properties
    # ─────────────────────────────────────────────────────────────────────────

    @property
    def buying_power(self) -> float:
        return self._real_margin * self.leverage

    @property
    def is_slot_free(self) -> bool:
        return not self._position_active

    @property
    def active_symbol(self) -> Optional[str]:
        return self._active_symbol

    # ─────────────────────────────────────────────────────────────────────────
    # Fyers Sync
    # ─────────────────────────────────────────────────────────────────────────

    async def sync(self, broker) -> float:
        """
        Pull actual available_margin from Fyers.

        Call schedule:
          - Session start (before first scan)
          - After every confirmed fill
          - After every position close (SL/TP/manual)
          - Every 5 minutes in health monitor heartbeat

        Returns: real_margin (float)
        """
        async with self._lock:
            try:
                funds = await broker.get_funds()
                margin = self._parse_fyers_funds(funds)
                self._real_margin = margin
                self._last_sync = datetime.utcnow()

                logger.info(
                    f"💰 CAPITAL SYNC | real_margin=₹{self._real_margin:.2f} | "
                    f"buying_power=₹{self.buying_power:.2f} | "
                    f"slot={'OCCUPIED → ' + (self._active_symbol or '?') if self._position_active else 'FREE'} | "
                    f"synced_at={self._last_sync.strftime('%H:%M:%S')}"
                )
                return self._real_margin

            except Exception as e:
                logger.error(
                    f"💰 CAPITAL SYNC FAILED: {e} | "
                    f"keeping last value ₹{self._real_margin:.2f}"
                )
                return self._real_margin

    def _parse_fyers_funds(self, funds: dict) -> float:
        """
        Parse Fyers /funds response — handles multiple API response shapes.

        Fyers v3 /funds returns:
          { "s": "ok", "fund_limit": [
              {"id": 1, "title": "Total Balance",     "equityAmount": 1800.00},
              {"id": 2, "title": "Available Balance", "equityAmount": 1700.00},
              ...
          ]}

        We want id=2 "Available Balance".
        """
        if not isinstance(funds, dict) or funds.get('s') != 'ok':
            raise ValueError(f"Fyers funds response invalid: {funds}")

        # Pattern 1: fund_limit list (Fyers v3 standard)
        best_val = 0.0
        for item in funds.get('fund_limit', []):
            # id=10 is "Available Balance" in Fyers v3. id=1 is "Total Balance".
            # id=2 is "Utilized Amount" (which was causing the bot to size based on used margin!)
            val = float(item.get('equityAmount', 0) or 0)
            title = str(item.get('title', '')).lower()
            
            if item.get('id') == 10 or 'available' in title:
                if val > 100: # Threshold for "normal" account balance
                    return val
                best_val = max(best_val, val)
            
            # Fallback to id=1 (cash) if id=10 is missing
            if item.get('id') == 1 or 'total' in title:
                best_val = max(best_val, val)

        if best_val > 0:
            return best_val

        # Pattern 2: equity dict (some Fyers SDK wrappers)
        eq = funds.get('equity', {})
        if isinstance(eq, dict):
            for key in ('available_margin', 'availableMargin', 'available', 'cash_balance'):
                if key in eq:
                    return float(eq[key] or 0)

        # Pattern 3: flat dict
        for key in ('available_margin', 'availableMargin', 'available_balance', 'cashBalance'):
            if key in funds:
                return float(funds[key] or 0)

        logger.warning(f"Abnormal funds structure detected: {json.dumps(funds)}")
        raise ValueError(f"Cannot parse available margin from Fyers funds.")

    # ─────────────────────────────────────────────────────────────────────────
    # Sizing
    # ─────────────────────────────────────────────────────────────────────────

    def compute_qty(self, symbol: str, ltp: float) -> tuple:
        """
        Compute maximum qty for FULL margin utilization.

        Uses real Fyers margin (not virtual/hardcoded).
        Applies 2% safety buffer to avoid Fyers code -50.

        Returns: (qty: int, cost: float, margin_required: float)

        Example on ₹1,700 account:
          LTFOODS @ ₹407 → buying_power=₹8,500
          raw=20.88 → qty=20 → cost=₹8,140 → margin_req=₹1,628
          utilization = 1628/1700 = 95.8%
        """
        if ltp <= 0 or self._real_margin <= 0:
            return 0, 0.0, 0.0

        safety_cap = self._real_margin * 0.98   # 2% safety buffer
        raw_qty = self.buying_power / ltp
        qty = int(floor(raw_qty))

        # Walk down until margin fits within safety cap
        while qty > 0:
            cost = qty * ltp
            margin_req = cost / self.leverage
            if margin_req <= safety_cap:
                utilization = (margin_req / self._real_margin) * 100
                logger.info(
                    f"💰 SIZING {symbol} | real_margin=₹{self._real_margin:.2f} "
                    f"buying_power=₹{self.buying_power:.2f} | ltp=₹{ltp:.2f} "
                    f"raw={raw_qty:.2f} → qty={qty} | cost=₹{cost:.2f} "
                    f"margin_req=₹{margin_req:.2f} | utilization={utilization:.1f}%"
                )
                return qty, cost, margin_req
            qty -= 1

        logger.warning(
            f"💰 SIZING {symbol} — ZERO QTY | "
            f"real_margin=₹{self._real_margin:.2f} ltp=₹{ltp:.2f} "
            f"(stock costs more than available margin)"
        )
        return 0, 0.0, 0.0

    # ─────────────────────────────────────────────────────────────────────────
    # Slot Management (Single-Position Architecture)
    # ─────────────────────────────────────────────────────────────────────────

    async def acquire_slot(self, symbol: str):
        """
        Lock capital slot after confirmed fill.
        Call AFTER broker confirms fill, BEFORE SL placement.
        """
        async with self._lock:
            if self._position_active:
                raise RuntimeError(
                    f"Slot occupied by {self._active_symbol} — "
                    f"cannot acquire for {symbol}"
                )
            self._position_active = True
            self._active_symbol = symbol
            logger.info(
                f"💰 CAPITAL SLOT ACQUIRED → {symbol} | "
                f"margin_committed=₹{self._real_margin:.2f} | "
                f"all new entries BLOCKED until position closes"
            )

    async def release_slot(self, broker=None):
        """
        Release capital slot after SL/TP/manual exit.
        Re-syncs Fyers margin if broker is provided.
        """
        async with self._lock:
            released = self._active_symbol
            self._position_active = False
            self._active_symbol = None
            logger.info(f"💰 CAPITAL SLOT RELEASED ← {released}")

        # Sync outside lock — avoids deadlock, gets fresh margin for next trade
        if broker:
            await self.sync(broker)

    def get_slot_status(self) -> dict:
        """Rich status dict — used in Telegram capital alerts."""
        return {
            'slot_free': self.is_slot_free,
            'active_symbol': self._active_symbol,
            'real_margin': self._real_margin,
            'buying_power': self.buying_power,
            'leverage': self.leverage,
            'last_sync': self._last_sync.strftime('%H:%M:%S') if self._last_sync else 'NEVER',
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Legacy Compatibility (keeps existing callers working)
    # ─────────────────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Legacy-compatible — used by order_manager for buying_power lookup."""
        return {
            'base_capital': self._real_margin,
            'leverage': self.leverage,
            'total_buying_power': self.buying_power,
            'available': self.buying_power if self.is_slot_free else 0.0,
            'in_use': self.buying_power if not self.is_slot_free else 0.0,
            'positions_count': 1 if self._position_active else 0,
            'active_symbol': self._active_symbol,
        }

    def can_afford(self, symbol: str, cost: float) -> dict:
        """Legacy-compatible — kept for backward compat. Use get_slot_status() for new code."""
        if not self.is_slot_free:
            return {
                'allowed': False,
                'reason': 'CAPITAL_LOCKED',
                'available': 0.0,
                'required': cost,
                'active_symbol': self._active_symbol,
            }
        if cost > self.buying_power:
            return {
                'allowed': False,
                'reason': 'INSUFFICIENT_FUNDS',
                'available': self.buying_power,
                'required': cost,
            }
        return {'allowed': True, 'reason': 'OK', 'available': self.buying_power, 'required': cost}

    def allocate(self, symbol: str, cost: float):
        """DEPRECATED — use acquire_slot(). Kept so old code doesn't crash."""
        logger.warning(f"⚠️ capital.allocate() called [DEPRECATED] for {symbol} — migrate to acquire_slot()")

    def release(self, symbol: str):
        """DEPRECATED — use release_slot(). Kept so old code doesn't crash."""
        logger.warning(f"⚠️ capital.release() called [DEPRECATED] for {symbol} — migrate to release_slot()")
