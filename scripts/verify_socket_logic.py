import logging
import time
import sys
from unittest.mock import MagicMock

# --- MOCKING FYERS DEPENDENCY START ---
# Since fyers_apiv3 isn't installed in this test env, we mock it.
mock_fyers = MagicMock()
sys.modules["fyers_apiv3"] = mock_fyers
sys.modules["fyers_apiv3.FyersDataSocket"] = mock_fyers

# Mock the data_ws class specifically
mock_data_ws = MagicMock()
mock_fyers.data_ws = mock_data_ws
# --- MOCKING END ---

from socket_engine import SocketEngine

# Configure logging to see output
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("SocketTest")

# Mock the Fyers Socket class to avoid import errors if lib missing or connection needs
# But here we import SocketEngine which imports fyers_apiv3.
# We will assume fyers_apiv3 is installed or we mock the import if it fails.
# Since we are in the user's env, we assume standard libs.
# If fyers_apiv3 is missing, the previous step works but running this will fail.
# Let's hope it works or we mock it.

def test_hft_logic():
    print("--- TESTING HFT LOGIC ---")
    
    # Instantiate Engine (Token doesn't matter for logic test)
    engine = SocketEngine("MOCK_TOKEN")
    
    # 1. TEST WHALE DETECTION
    print("\n[1] Whale Detection Test")
    whale_msg = {
        'symbol': 'NSE:OMINFRAL-EQ',
        'ltp': 85.0,
        'last_traded_qty': 6000 # > 5000 threshold
    }
    engine.process_packet(whale_msg)
    
    if any("WHALE PRINT" in alert for alert in engine.alerts):
        print("✅ PASS: Whale Alert Triggered.")
    else:
        print("❌ FAIL: Whale Alert Missed.")
        
    # 2. TEST SPOOF DETECTION
    print("\n[2] Spoof Detection Test")
    
    # Step A: Initial Wall (Snapshot)
    # Total Sell Qty = 100,000, Price = 85.0
    msg_t0 = {
        'symbol': 'NSE:OMINFRAL-EQ',
        'ltp': 85.0,
        'total_sell_qty': 100000,
        'asks': [{'qty': 100000}] # Fallback
    }
    engine.process_packet(msg_t0)
    print("   Snapshot Taken (Qty 100k).")
    
    # Step B: Price Moves UP (Approaching)
    # Wall Vanishes (Drops to 10k)
    msg_t1 = {
        'symbol': 'NSE:OMINFRAL-EQ',
        'ltp': 85.10, # Price UP
        'total_sell_qty': 10000, # Drop 90%
        'asks': [{'qty': 10000}]
    }
    engine.process_packet(msg_t1)
    
    # Check Alerts
    spoof_alerts = [a for a in engine.alerts if "SPOOF DETECTED" in a]
    if spoof_alerts:
        print(f"✅ PASS: Spoof Alert Triggered: {spoof_alerts[-1]}")
    else:
        print("❌ FAIL: Spoof Alert Missed.")

if __name__ == "__main__":
    try:
        test_hft_logic()
    except ImportError:
        print("Skipping run: fyers_apiv3 not installed in this env.")
