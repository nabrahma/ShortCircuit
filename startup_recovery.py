"""
Phase 44.6: StartupRecovery — now adopts orphans, places emergency SL.
Previously: logged only. Now: acts.
"""
import logging
import asyncio
from datetime import datetime

logger = logging.getLogger(__name__)


class StartupRecovery:

    def __init__(self, fyers_client, order_manager=None, capital_manager=None, telegram=None):
        self.fyers          = fyers_client
        self.order_manager  = order_manager    # NEW
        self.capital        = capital_manager  # NEW
        self.telegram       = telegram         # NEW
        logger.info("[RECOVERY] StartupRecovery initialized (Phase 44.6 — adoption enabled).")

    def scan_orphaned_trades(self):
        """
        Synchronous startup scan.
        Calls async adoption via asyncio.get_event_loop().run_until_complete
        only if an orphan is found.
        """
        try:
            positions = self.fyers.positions()
            if positions.get('s') != 'ok':
                logger.error(f"Recovery scan failed: {positions}")
                return

            net_positions = positions.get('netPositions', [])
            open_positions = [p for p in net_positions if p['netQty'] != 0]

            if not open_positions:
                logger.info("✅ [RECOVERY] No orphaned positions found (Broker is Flat).")
                return

            logger.critical(
                f"⚠️ [RECOVERY] Found {len(open_positions)} OPEN POSITION(S) at startup!"
            )
            for p in open_positions:
                sym     = p['symbol']
                qty     = p['netQty']
                side    = 'SHORT' if qty < 0 else 'LONG'
                avg     = p.get('avgPrice', 0.0)
                logger.critical(f"   - {sym}: qty={qty} ({side}) avgPrice=₹{avg:.2f}")

                # Phase 44.6: Attempt adoption instead of just logging
                if self.order_manager and self.capital:
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            # Inside async context — schedule as task
                            asyncio.create_task(
                                self._adopt_orphan_async(sym, qty, side, avg)
                            )
                        else:
                            loop.run_until_complete(
                                self._adopt_orphan_async(sym, qty, side, avg)
                            )
                    except Exception as e:
                        logger.critical(f"[RECOVERY] Adoption failed for {sym}: {e}")
                else:
                    # Fallback: alert only (old behaviour if managers not injected)
                    logger.critical(
                        f"[RECOVERY] order_manager/capital not injected — "
                        f"cannot adopt {sym}. Alert only."
                    )
                    if self.telegram:
                        asyncio.create_task(self.telegram.send_alert(
                            f"⚠️ **ORPHAN AT STARTUP**: `{sym}` qty={qty}\n"
                            f"Cannot auto-adopt — managers not wired."
                        ))

        except Exception as e:
            logger.error(f"Recovery scan failed: {e}")

    async def _adopt_orphan_async(
        self, symbol: str, net_qty: int, side: str, avg_price: float
    ):
        """Async adoption logic — places emergency SL, registers position, locks capital."""
        qty      = abs(net_qty)
        sl_pct   = 0.01
        sl_price = round(avg_price * (1 + sl_pct), 2) if side == 'SHORT' \
                   else round(avg_price * (1 - sl_pct), 2)
        sl_side  = 'BUY' if side == 'SHORT' else 'SELL'

        # Step 1: Place emergency SL
        sl_id = None
        try:
            sl_id = await self.order_manager.broker.place_order(
                symbol=symbol,
                side=sl_side,
                qty=qty,
                order_type='SL_MARKET',
                trigger_price=sl_price,
            )
            logger.critical(
                f"[RECOVERY] Emergency SL placed | {symbol} sl_id={sl_id} "
                f"@ ₹{sl_price:.2f}"
            )
        except Exception as e:
            logger.critical(
                f"[RECOVERY] Emergency SL FAILED for {symbol}: {e} | NAKED POSITION"
            )
            if self.telegram:
                await self.telegram.send_alert(
                    f"🚨 *STARTUP ORPHAN — SL FAILED*\n\n"
                    f"`{symbol}` qty={qty}\nSL error: `{e}`\n"
                    f"⚠️ Manual close required NOW"
                )

        # Step 2: Register in order_manager
        self.order_manager.active_positions[symbol] = {
            'symbol':      symbol,
            'qty':         qty,
            'side':        side,
            'entry_id':    'STARTUP_ORPHAN',
            'sl_id':       sl_id,
            'status':      'OPEN',
            'entry_time':  datetime.utcnow(),
            'entry_price': avg_price,
            'stop_loss':   sl_price if sl_id else 0.0,
            'source':      'STARTUP_ORPHAN_ADOPTED',
        }
        if sl_id:
            self.order_manager.hard_stops[symbol] = sl_id

        # Step 3: Lock capital slot
        try:
            if self.capital.is_slot_free:
                await self.capital.acquire_slot(symbol)
            else:
                logger.warning(
                    f"[RECOVERY] Capital slot occupied by {self.capital.active_symbol} "
                    f"— cannot lock for orphan {symbol}"
                )
        except Exception as e:
            logger.error(f"[RECOVERY] Capital acquire_slot failed: {e}")

        # Step 4: Alert
        if self.telegram:
            await self.telegram.send_alert(
                f"⚠️ *STARTUP ORPHAN ADOPTED*\n\n"
                f"Symbol:   `{symbol}`\n"
                f"Side:     {side}  Qty: {qty}\n"
                f"AvgPrice: ₹{avg_price:.2f}\n"
                f"EmergSL:  {'₹' + str(sl_price) if sl_id else '❌ FAILED'}\n\n"
                f"Bot is now managing this position."
            )
        logger.critical(
            f"[RECOVERY] ✅ Orphan adopted: {symbol} {side} ×{qty} "
            f"sl_id={sl_id} sl=₹{sl_price:.2f}"
        )
