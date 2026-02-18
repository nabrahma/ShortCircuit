
import logging

logger = logging.getLogger(__name__)

class StartupRecovery:
    """
    Boot safety checks.

    CHANGE: No longer instantiates FyersConnect.
    Receives authenticated fyers_client from main.py.
    """

    def __init__(self, fyers_client):
        # REMOVED: self.fyers = FyersConnect() 
        self.fyers = fyers_client  # Use the passed-in client directly
        logger.info("[RECOVERY] Scanning for orphaned trades...")

    def scan_orphaned_trades(self):
        """
        Scans for open positions at startup and alerts if they don't match DB.
        """
        try:
            positions = self.fyers.positions()
            if positions.get('s') != 'ok':
                logger.error(f"Recovery scan failed: {positions}")
                return

            net_positions = positions.get('netPositions', [])
            open_positions = [p for p in net_positions if p['netQty'] != 0]
            
            if open_positions:
                logger.critical(f"⚠️ [RECOVERY] Found {len(open_positions)} OPEN POSITIONS at startup!")
                for p in open_positions:
                    logger.critical(f"   - {p['symbol']}: {p['netQty']} Qty")
            else:
                logger.info("✅ [RECOVERY] No orphaned positions found (Broker is Flat).")
                
        except Exception as e:
            logger.error(f"Recovery scan failed: {e}")
