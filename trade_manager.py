import logging
import config

logger = logging.getLogger(__name__)

class TradeManager:
    def __init__(self, fyers):
        self.fyers = fyers
        self.auto_trade_enabled = config.AUTO_TRADE

    def set_auto_trade(self, enabled: bool):
        self.auto_trade_enabled = enabled
        logger.info(f"Auto-Trade set to: {self.auto_trade_enabled}")
        return self.auto_trade_enabled

    def tick_round(self, price, tick=0.05):
        """Rounds price to nearest tick size."""
        return round(round(price / tick) * tick, 2)

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

        tick_size = signal.get('tick_size', 0.05) # Get Dynamic Tick
        
        # Calculate Qty
        qty = int(config.CAPITAL / ltp)
        if qty < 1:
            qty = 1
            
        logger.info(f"Processing Trade for {symbol}. Qty: {qty}. Tick: {tick_size}")
        
        if self.auto_trade_enabled:
            # PLACE ENTRY ORDER
            try:
                # 1. Main Sell Order
                entry_data = {
                    "symbol": symbol,
                    "qty": qty,
                    "type": 2, # Market Order
                    "side": -1, # Sell
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
                    # Use Dynamic Tick Size
                    sl_trigger = self.tick_round(float(sl), tick_size)
                    sl_limit_price = sl_trigger * 1.005 # 0.5% buffer
                    sl_limit = self.tick_round(sl_limit_price, tick_size)
                    
                    sl_data = {
                        "symbol": symbol,
                        "qty": qty,
                        "type": 4, # SL-Limit
                        "side": 1, # Buy (Cover)
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
                                logger.info(f"SL Order SUCCESS (Attempt {attempt}): {resp_sl}")
                                sl_placed = True
                                break # Exit Loop
                            else:
                                logger.warning(f"SL Attempt {attempt} Failed: {resp_sl}")
                        except Exception as e:
                            logger.warning(f"SL Attempt {attempt} Exception: {e}")
                    
                    # FINAL CHECK
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

    def emergency_exit(self, symbol, qty):
        """
        Closes a position immediately via Market Order.
        Used when SL placement fails to prevent naked positions.
        """
        try:
            data = {
                "symbol": symbol,
                "qty": qty,
                "type": 2, # Market
                "side": 1, # Buy/Cover
                "productType": "INTRADAY",
                "limitPrice": 0,
                "stopPrice": 0,
                "validity": "DAY",
                "disclosedQty": 0,
                "offlineOrder": False
            }
            self.fyers.place_order(data=data)
            logger.info(f"[OK] Emergency Exit Placed for {symbol}")
        except Exception as e:
            logger.critical(f"[CRIT] EMERGENCY EXIT FAILED for {symbol}: {e}")

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
                        if o['status'] in [6]: # Pending
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
                    # If net_qty > 0 (Long), we need to Sell (-1)
                    # If net_qty < 0 (Short), we need to Buy (1)
                    exit_side = -1 if net_qty > 0 else 1
                    exit_qty = abs(net_qty)
                    
                    data = {
                        "symbol": symbol,
                        "qty": exit_qty,
                        "type": 2, # Market
                        "side": exit_side,
                        "productType": pos["productType"], # 'INTRADAY' or 'CNC'
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
                    
            return f"Squaring Off Complete. Closed {closed_count} positions."
            
        except Exception as e:
            logger.error(f"Auto-Square Off Failed: {e}")
            return f"Square Off Error: {e}"
