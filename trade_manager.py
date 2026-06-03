import logging
import asyncio
from datetime import datetime


import config
from capital_manager import CapitalManager


logger = logging.getLogger(__name__)


class TradeManager:
    def __init__(self, fyers, capital_manager):
        self.fyers = fyers
        # Legacy Auto-Trade State (Now managed by TelegramBot)
        self.auto_trade_enabled = False  # Default to False always

        # Phase 42: Position Safety — Track active SL orders
        self.active_sl_orders = {}   # {symbol: order_id}

        # Phase 42.1: Capital Management (Injected)
        self.capital_manager = capital_manager

        # Reference to Telegram bot (set externally after init)
        self.bot = None
        
        # Phase 42.3.4: Reconciliation Engine (Injected)
        self.reconciliation_engine = None

        # Phase 44.6: Scalper Position Manager (Injected)
        self.scalper_manager = None




    # ==================================================================
    # PHASE 42: POSITION SAFETY — CRITICAL GUARDS
    # ==================================================================

    def _get_broker_position(self, symbol: str) -> dict:
        """
        Query broker for ACTUAL current position.

        Returns:
            dict with 'net_qty', 'symbol', 'raw' or None on error
        """
        try:
            positions = self.fyers.positions()

            if positions.get('s') != 'ok' and 'netPositions' not in positions:
                logger.error(f"[SAFETY] Could not fetch positions: {positions}")
                return None

            for pos in positions.get('netPositions', []):
                if pos['symbol'] == symbol:
                    return {
                        'net_qty': pos['netQty'],
                        'symbol': symbol,
                        'raw': pos
                    }

            # Symbol not in positions = FLAT
            return {'net_qty': 0, 'symbol': symbol, 'raw': None}

        except Exception as e:
            logger.error(f"[SAFETY] Broker position query failed: {e}")
            return None





    def cleanup_active_orders(self, symbol: str):
        """
        Cancels all pending orders for a symbol. 
        Called after TP/SL hit or manual exit.
        """
        logger.info(f"🧹 [SAFETY] Cleaning up orphaned orders for {symbol}")
        try:
            orders = self.fyers.orderbook()
            if "orderBook" not in orders:
                return
            
            for order in orders["orderBook"]:
                if order["symbol"] == symbol and order["status"] in [6]: # 6=Pending
                    logger.warning(f"❌ [SAFETY] Cancelling orphaned order: {order['id']} ({order['type']})")
                    self.fyers.cancel_order(data={"id": order["id"]})
        except Exception as e:
            logger.error(f"❌ [SAFETY] Order cleanup failed for {symbol}: {e}")

    def close_all_positions(self):
        """
        Closes all open intraday positions.
        Used for EOD Auto-Square Off.
        Phase 42.1: Releases capital for each closed position.
        """
        logger.warning("[ALERT] INITIATING AUTO-SQUARE OFF...")
        try:
            positions_response = self.fyers.positions()
            if 'netPositions' not in positions_response:
                logger.info("No positions to close.")

            # Cancel all pending orders first
            try:
                orders = self.fyers.orderbook()
                if 'orderBook' in orders:
                    cleaned = 0
                    for o in orders['orderBook']:
                        if o['status'] in [6]:  # Pending
                            self.fyers.cancel_order(data={"id": o['id']})
                            cleaned += 1
                    logger.info(f"EOD Cleanup: Cancelled {cleaned} pending orders.")
            except Exception as e:
                logger.error(f"EOD Order Cleanup Failed: {e}")

            if 'netPositions' not in positions_response:
                return "Checked Orders. No open positions."

            closed_count = 0
            for pos in positions_response['netPositions']:
                net_qty = pos['netQty']
                symbol = pos['symbol']

                if net_qty != 0:
                    exit_side = -1 if net_qty > 0 else 1
                    exit_qty = abs(net_qty)

                    data = {
                        "symbol": symbol,
                        "qty": exit_qty,
                        "type": 2,
                        "side": exit_side,
                        "productType": pos["productType"],
                        "limitPrice": 0,
                        "stopPrice": 0,
                        "validity": "DAY",
                        "disclosedQty": 0,
                        "offlineOrder": False
                    }

                    logger.info(f"[EOD] Squaring off {symbol}: Qty {exit_qty} Side {exit_side}")
                    res = self.fyers.place_order(data=data)
                    
                    # Phase 80: Standardize log for session analyzer
                    avg_price = pos.get('avgPrice', 0)
                    exit_price = pos.get('lp', ltp if 'ltp' in locals() else 0)
                    pnl_estimate = 0.0
                    if avg_price > 0 and exit_price > 0:
                        if net_qty < 0: # SHORT
                            pnl_estimate = (avg_price - exit_price) * abs(net_qty)
                        elif net_qty > 0: # LONG
                            pnl_estimate = (exit_price - avg_price) * abs(net_qty)
                    
                    logger.info(f"[EXIT] {symbol} reason=EOD_SQUAREOFF exit=₹{exit_price:.2f} pnl=₹{pnl_estimate:.2f}")
                    logger.info(f"Square-off Response: {res}")
                    
                    # Phase 51 [G13]: Record outcome
                    try:
                        avg_price = pos.get('avgPrice', 0)
                        exit_price = pos.get('lp', 0) # Use last price as estimate for PnL
                        pnl_estimate = 0.0
                        if avg_price > 0 and exit_price > 0:
                            if net_qty < 0: # SHORT
                                pnl_estimate = (avg_price - exit_price) * abs(net_qty)
                            elif net_qty > 0: # LONG
                                pnl_estimate = (exit_price - avg_price) * abs(net_qty)
                        self.record_trade_outcome(symbol, pnl_estimate)
                    except Exception as e:
                        logger.error(f"G13 outcome recording failed in square-off: {e}")
                    
                    closed_count += 1

                    # Phase 42: Clean up SL tracking
                    self._cleanup_sl_tracking(symbol)

                    # Phase 42.1: Release capital (Handled by main loop in Phase 97)
                    pass
                    
                    # Phase 42.3.4: Mark Dirty
                    if self.reconciliation_engine: self.reconciliation_engine.mark_dirty()

            return f"Squaring Off Complete. Closed {closed_count} positions."

        except Exception as e:
            logger.error(f"Auto-Square Off Failed: {e}")
            return f"Square Off Error: {e}"

    def record_trade_outcome(self, symbol: str, pnl: float):
        """
        Phase 69 [G13]: Record trade outcome in SignalManager.
        Updates daily PnL tracking and global stats.
        """
        from signal_manager import get_signal_manager
        sm = get_signal_manager()
        sm.record_outcome(symbol, pnl)
        logger.info(f"Phase 69 Outcome recorded for {symbol}: ₹{pnl:.2f}")

    # ==================================================================
    # SAFETY UTILITIES
    # ==================================================================

    def _cleanup_sl_tracking(self, symbol: str):
        """Remove SL tracking after position closed."""
        if symbol in self.active_sl_orders:
            del self.active_sl_orders[symbol]
            logger.info(f"[SAFETY] SL tracking cleaned up for {symbol}")
