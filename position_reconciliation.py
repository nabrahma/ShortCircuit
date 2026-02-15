"""
Phase 42: Position Reconciliation Module

Verifies bot's internal state matches broker's actual positions.
Critical for detecting orphaned positions from bugs.

Usage:
    reconciler = PositionReconciliation(fyers, trade_manager, bot)
    reconciler.reconcile_positions()
"""

import os
import logging
from datetime import datetime

import config

logger = logging.getLogger(__name__)

ORPHANED_LOG = getattr(config, 'ORPHANED_POSITION_LOG_PATH', 'logs/orphaned_positions.log')


class PositionReconciliation:
    """
    Verifies bot's internal state matches broker's actual positions.
    Critical for detecting orphaned positions from bugs.
    """

    def __init__(self, fyers, trade_manager, bot=None):
        self.fyers = fyers
        self.trade_manager = trade_manager
        self.bot = bot  # ShortCircuitBot (optional, for alerts)

    def reconcile_positions(self) -> list:
        """
        Check for positions that exist on broker but bot doesn't know about.

        Returns:
            list of orphaned position dicts (empty if clean)
        """
        logger.info("[RECONCILE] Running position reconciliation...")

        try:
            response = self.fyers.positions()
        except Exception as e:
            logger.error(f"[RECONCILE] Could not fetch positions: {e}")
            return []

        if response.get('s') != 'ok' and 'netPositions' not in response:
            logger.error(f"[RECONCILE] Bad response: {response}")
            return []

        broker_positions = response.get('netPositions', [])

        # Filter for non-zero positions
        open_positions = [p for p in broker_positions if p.get('netQty', 0) != 0]

        if not open_positions:
            logger.info("[RECONCILE] ✅ No open positions on broker — clean state")
            return []

        orphaned = []

        for pos in open_positions:
            symbol = pos['symbol']
            net_qty = pos['netQty']
            side = 'LONG' if net_qty > 0 else 'SHORT'

            # Check if FocusEngine is managing this position
            focus_engine = getattr(self.bot, 'focus_engine', None) if self.bot else None
            is_managed = False

            if focus_engine and focus_engine.active_trade:
                if focus_engine.active_trade.get('symbol') == symbol:
                    is_managed = True

            if is_managed:
                logger.info(f"[RECONCILE] ✅ {symbol}: Bot is managing (qty: {net_qty}, {side})")
            else:
                # ORPHANED POSITION DETECTED
                logger.critical(f"🚨 [RECONCILE] ORPHANED POSITION: {symbol}")
                logger.critical(f"    Qty: {net_qty} ({side})")
                logger.critical(f"    Bot is NOT managing this position")

                orphaned.append({
                    'symbol': symbol,
                    'net_qty': net_qty,
                    'side': side,
                    'avg_price': pos.get('avgPrice', 0),
                    'pnl': pos.get('pl', 0),
                })

                # Send emergency alert
                if self.bot and hasattr(self.bot, 'send_emergency_alert'):
                    self.bot.send_emergency_alert(
                        f"🚨 ORPHANED POSITION DETECTED\n\n"
                        f"Symbol: {symbol}\n"
                        f"Quantity: {net_qty}\n"
                        f"Side: {side}\n"
                        f"Bot State: NOT MANAGING\n\n"
                        f"Possible causes:\n"
                        f"- Bot crashed after order placement\n"
                        f"- Duplicate order bug\n"
                        f"- Manual trade via Fyers app\n\n"
                        f"Action: Close position manually or restart bot"
                    )

                # Log to file
                self._log_orphaned(symbol, net_qty, side)

        if orphaned:
            logger.critical(f"[RECONCILE] Found {len(orphaned)} orphaned position(s)!")
        else:
            logger.info("[RECONCILE] ✅ All open positions are managed by bot")

        return orphaned

    def _log_orphaned(self, symbol: str, net_qty: int, side: str):
        """Append orphaned position to log file."""
        try:
            os.makedirs(os.path.dirname(ORPHANED_LOG), exist_ok=True)
            with open(ORPHANED_LOG, 'a') as f:
                f.write(f"{datetime.now()} | {symbol} | {net_qty} | {side} | NOT_MANAGED\n")
        except Exception as e:
            logger.error(f"[RECONCILE] Failed to write orphaned log: {e}")
