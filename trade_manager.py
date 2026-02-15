import time
import logging
import config

logger = logging.getLogger(__name__)


class TradeManager:
    def __init__(self, fyers):
        self.fyers = fyers
        self.auto_trade_enabled = config.AUTO_TRADE

        # Phase 42: Position Safety — Track active SL orders
        self.active_sl_orders = {}   # {symbol: order_id}

    def set_auto_trade(self, enabled: bool):
        self.auto_trade_enabled = enabled
        logger.info(f"Auto-Trade set to: {self.auto_trade_enabled}")
        return self.auto_trade_enabled

    def tick_round(self, price, tick=0.05):
        """Rounds price to nearest tick size."""
        return round(round(price / tick) * tick, 2)

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

    def _verify_position_safe(self, symbol: str, intended_action: str) -> bool:
        """
        CRITICAL safety check: Verify we're not about to flip direction.

        Args:
            symbol: NSE:SYMBOL-EQ
            intended_action: 'ENTER_SHORT', 'EXIT_SHORT'

        Returns:
            True if safe to proceed, False if dangerous
        """
        if not getattr(config, 'ENABLE_POSITION_VERIFICATION', True):
            return True  # Bypass if explicitly disabled (NOT recommended)

        broker_pos = self._get_broker_position(symbol)

        if broker_pos is None:
            logger.critical(f"🚨 [SAFETY] Cannot verify position for {symbol} — BLOCKING order (fail-safe)")
            return False

        net_qty = broker_pos['net_qty']

        if intended_action == 'ENTER_SHORT':
            if net_qty > 0:
                logger.critical(f"🚨 BLOCKED: Trying to SHORT {symbol} but already LONG {net_qty}!")
                return False
            if net_qty < 0:
                logger.warning(f"[SAFETY] Already SHORT {abs(net_qty)} of {symbol}, adding more")
            return True

        elif intended_action == 'EXIT_SHORT':
            if net_qty >= 0:
                logger.critical(
                    f"🚨 BLOCKED: Trying to BUY (exit short) {symbol} "
                    f"but position is {net_qty} (already flat or LONG)!"
                )
                return False
            return True  # We're short, safe to buy/cover

        elif intended_action == 'ENTER_LONG':
            if net_qty < 0:
                logger.critical(f"🚨 BLOCKED: Trying to LONG {symbol} but already SHORT {abs(net_qty)}!")
                return False
            return True

        elif intended_action == 'EXIT_LONG':
            if net_qty <= 0:
                logger.critical(f"🚨 BLOCKED: Trying to exit LONG {symbol} but position is {net_qty}!")
                return False
            return True

        logger.critical(f"🚨 [SAFETY] Unknown intended_action '{intended_action}' — BLOCKING")
        return False

    # ==================================================================
    # CORE TRADE EXECUTION
    # ==================================================================

    def execute_logic(self, signal):
        """
        Decides whether to execute instantly or return a manual alert prompt.
        """
        symbol = signal['symbol']
        ltp = signal['ltp']
        # CRITICAL FIX: Handle missing SL key to prevent loop crash
        sl = signal.get('stop_loss', 0.0)

        if sl == 0.0:
            logger.error(f"[CRITICAL] Missing Stop Loss for {symbol}. Aborting Trade.")
            return {
                "status": "ERROR",
                "msg": f"[FAIL] Malformed Signal: Missing Stop Loss for {symbol}"
            }

        tick_size = signal.get('tick_size', 0.05)  # Get Dynamic Tick

        # Calculate Qty
        qty = int(config.CAPITAL / ltp)
        if qty < 1:
            qty = 1

        logger.info(f"Processing Trade for {symbol}. Qty: {qty}. Tick: {tick_size}")

        if self.auto_trade_enabled:
            # ── PHASE 42: VERIFY BEFORE ENTRY ──────────────────────────
            if not self._verify_position_safe(symbol, 'ENTER_SHORT'):
                logger.critical(f"[SAFETY] Entry BLOCKED for {symbol} — position verification failed")
                return {
                    "status": "BLOCKED",
                    "msg": f"[BLOCKED] Position safety check failed for {symbol}"
                }

            # PLACE ENTRY ORDER
            try:
                # 1. Main Sell Order
                entry_data = {
                    "symbol": symbol,
                    "qty": qty,
                    "type": 2,  # Market Order
                    "side": -1,  # Sell
                    "productType": "INTRADAY",
                    "limitPrice": 0,
                    "stopPrice": 0,
                    "validity": "DAY",
                    "disclosedQty": 0,
                    "offlineOrder": False,
                }

                resp_entry = self.fyers.place_order(data=entry_data)

                if resp_entry.get("s") == "ok" and "id" in resp_entry:
                    entry_order_id = resp_entry["id"]
                    logger.info(f"Entry SUCCESS: {resp_entry}")

                    # 2. Place Safety Stop Loss Order (Buy SL-Limit)
                    sl_trigger = self.tick_round(float(sl), tick_size)
                    sl_limit_price = sl_trigger * 1.005  # 0.5% buffer
                    sl_limit = self.tick_round(sl_limit_price, tick_size)

                    sl_data = {
                        "symbol": symbol,
                        "qty": qty,
                        "type": 4,  # SL-Limit
                        "side": 1,  # Buy (Cover)
                        "productType": "INTRADAY",
                        "limitPrice": sl_limit,
                        "stopPrice": sl_trigger,
                        "validity": "DAY",
                        "disclosedQty": 0,
                        "offlineOrder": False,
                    }

                    sl_placed = False

                    # RETRY LOOP (3 Attempts)
                    for attempt in range(1, 4):
                        try:
                            resp_sl = self.fyers.place_order(data=sl_data)
                            if resp_sl.get("s") == "ok":
                                sl_order_id = resp_sl.get("id", "")
                                logger.info(f"SL Order SUCCESS (Attempt {attempt}): {resp_sl}")

                                # Phase 42: Track SL order ID
                                if sl_order_id:
                                    self.active_sl_orders[symbol] = sl_order_id

                                sl_placed = True
                                break
                            else:
                                logger.warning(f"SL Attempt {attempt} Failed: {resp_sl}")
                        except Exception as e:
                            logger.warning(f"SL Attempt {attempt} Exception: {e}")

                    # FINAL CHECK — SL failed
                    if not sl_placed:
                        logger.critical(f"[STOP] ALL 3 SL ATTEMPTS FAILED for {symbol}. Triggering EMERGENCY EXIT.")
                        self.emergency_exit(symbol, qty)
                        return {
                            "status": "ERROR",
                            "msg": f"[FAIL] SL Failed (x3). Emergency Exit Triggered for {symbol}."
                        }

                    return {
                        "status": "EXECUTED",
                        "order_id": entry_order_id,
                        "qty": qty,
                        "ltp": ltp,
                        "sl": sl,
                        "symbol": symbol,
                        "msg": f"[EXEC] Auto-Shorted {symbol} @ ~{ltp} with SL Order"
                    }

                else:
                    # ENTRY FAILED
                    logger.error(f"Entry FAILED: {resp_entry}")
                    return {
                        "status": "ERROR",
                        "msg": f"[FAIL] Entry Failed: {resp_entry.get('message', 'Unknown Error')}"
                    }

            except Exception as e:
                logger.error(f"Execution Exception: {e}")
                return {
                    "status": "ERROR",
                    "msg": f"[FAIL] Execution Exception: {e}"
                }

        else:
            # MANUAL MODE (Auto-Trade Disabled)
            return {
                "status": "MANUAL_WAIT",
                "symbol": symbol,
                "qty": qty,
                "value": int(qty * ltp),
                "ltp": ltp,
                "sl": sl,
                "pattern": signal.get('pattern', 'Unknown')
            }

    # ==================================================================
    # EXIT METHODS — ALL GUARDED
    # ==================================================================

    def emergency_exit(self, symbol, qty):
        """
        Closes a position immediately via Market Order.
        Phase 42: Now includes circuit breaker — verifies position before exit.
        """
        logger.critical(f"[EMERGENCY] Emergency exit triggered for {symbol}")

        # Phase 42: Wait 1s for broker state to settle, then verify
        time.sleep(1)

        broker_pos = self._get_broker_position(symbol)

        if broker_pos is None:
            logger.critical(f"[EMERGENCY] Cannot verify {symbol} — placing exit anyway (naked position risk)")
            # Fall through to exit — better to risk duplicate than leave naked position
        elif broker_pos['net_qty'] == 0:
            logger.info(f"[EMERGENCY] Position already flat for {symbol} — skipping exit")
            self._cleanup_sl_tracking(symbol)
            return
        elif broker_pos['net_qty'] > 0:
            logger.critical(f"🚨🚨 [EMERGENCY] {symbol} is LONG {broker_pos['net_qty']} — WRONG SIDE! Manual intervention needed!")
            self._cleanup_sl_tracking(symbol)
            return

        # Confirmed we're short — safe to exit
        actual_qty = abs(broker_pos['net_qty']) if broker_pos else qty

        try:
            data = {
                "symbol": symbol,
                "qty": actual_qty,
                "type": 2,  # Market
                "side": 1,  # Buy/Cover
                "productType": "INTRADAY",
                "limitPrice": 0,
                "stopPrice": 0,
                "validity": "DAY",
                "disclosedQty": 0,
                "offlineOrder": False
            }
            self.fyers.place_order(data=data)
            logger.info(f"[OK] Emergency Exit Placed for {symbol} (qty: {actual_qty})")
        except Exception as e:
            logger.critical(f"[CRIT] EMERGENCY EXIT FAILED for {symbol}: {e}")
        finally:
            self._cleanup_sl_tracking(symbol)

    def close_partial_position(self, symbol: str, quantity: int, reason: str) -> dict:
        """
        Phase 41.2: Close partial position for TP scale-out.
        Phase 42: Now guarded with position verification.
        """
        logger.info(f"[SCALPER] Closing {quantity} shares of {symbol} ({reason})")

        # ── PHASE 42: VERIFY BEFORE EXIT ────────────────────────────
        if not self._verify_position_safe(symbol, 'EXIT_SHORT'):
            logger.critical(f"[SAFETY] Partial close BLOCKED for {symbol}")
            return {"status": "BLOCKED", "reason": "POSITION_VERIFICATION_FAILED"}

        order_data = {
            "symbol": symbol,
            "qty": quantity,
            "type": 2,       # Market order
            "side": 1,       # Buy (cover short)
            "productType": "INTRADAY",
            "limitPrice": 0,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
        }

        try:
            response = self.fyers.place_order(data=order_data)

            if response.get("s") == "ok" and "id" in response:
                order_id = response["id"]
                logger.info(f"[SCALPER] Partial close order placed: {order_id}")
                return {
                    "status": "SUCCESS",
                    "order_id": order_id,
                    "filled_qty": quantity,
                }
            else:
                logger.error(f"[SCALPER] Partial close FAILED: {response}")
                return {"status": "FAILED", "error": str(response)}

        except Exception as e:
            logger.error(f"[SCALPER] Partial close exception: {e}")
            return {"status": "FAILED", "error": str(e)}

    def update_stop_loss(self, symbol: str, new_stop: float) -> dict:
        """
        Phase 41.2: Modify existing SL order to new price.
        """
        logger.info(f"[SCALPER] Updating SL for {symbol} to ₹{new_stop:.2f}")

        try:
            # Find pending SL order for this symbol
            orders = self.fyers.orderbook()
            if "orderBook" not in orders:
                logger.warning("[SCALPER] No orders found in orderbook")
                return {"status": "NO_ORDERS"}

            for order in orders["orderBook"]:
                if order["symbol"] == symbol and order["status"] == 6:  # Pending
                    tick_size = 0.05  # Default
                    sl_trigger = self.tick_round(float(new_stop), tick_size)
                    sl_limit = self.tick_round(sl_trigger * 1.005, tick_size)

                    modify_data = {
                        "id": order["id"],
                        "type": 4,  # SL-Limit
                        "limitPrice": sl_limit,
                        "stopPrice": sl_trigger,
                    }

                    resp = self.fyers.modify_order(data=modify_data)
                    if resp.get("s") == "ok":
                        logger.info(f"[SCALPER] SL updated to ₹{sl_trigger}")
                        return {"status": "SUCCESS", "new_stop": sl_trigger}
                    else:
                        logger.error(f"[SCALPER] SL modify failed: {resp}")
                        return {"status": "FAILED", "error": str(resp)}

            logger.warning(f"[SCALPER] No pending SL order found for {symbol}")
            return {"status": "NOT_FOUND"}

        except Exception as e:
            logger.error(f"[SCALPER] SL update exception: {e}")
            return {"status": "FAILED", "error": str(e)}

    def close_all_positions(self):
        """
        Closes all open intraday positions.
        Used for EOD Auto-Square Off.
        """
        logger.warning("[ALERT] INITIATING AUTO-SQUARE OFF...")
        try:
            # 1. Fetch Positions
            positions_response = self.fyers.positions()
            if 'netPositions' not in positions_response:
                logger.info("No positions to close.")

            # 0. CANCEL ALL PENDING ORDERS FIRST
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
                    # Determine Exit Side
                    exit_side = -1 if net_qty > 0 else 1
                    exit_qty = abs(net_qty)

                    data = {
                        "symbol": symbol,
                        "qty": exit_qty,
                        "type": 2,  # Market
                        "side": exit_side,
                        "productType": pos["productType"],
                        "limitPrice": 0,
                        "stopPrice": 0,
                        "validity": "DAY",
                        "disclosedQty": 0,
                        "offlineOrder": False
                    }

                    logger.info(f"Squaring off {symbol}: Qty {exit_qty} Side {exit_side}")
                    res = self.fyers.place_order(data=data)
                    logger.info(f"Square-off Response: {res}")
                    closed_count += 1

                    # Phase 42: Clean up SL tracking
                    self._cleanup_sl_tracking(symbol)

            return f"Squaring Off Complete. Closed {closed_count} positions."

        except Exception as e:
            logger.error(f"Auto-Square Off Failed: {e}")
            return f"Square Off Error: {e}"

    # ==================================================================
    # PHASE 42: SAFETY UTILITIES
    # ==================================================================

    def _cleanup_sl_tracking(self, symbol: str):
        """Remove SL tracking after position closed."""
        if symbol in self.active_sl_orders:
            del self.active_sl_orders[symbol]
            logger.info(f"[SAFETY] SL tracking cleaned up for {symbol}")
