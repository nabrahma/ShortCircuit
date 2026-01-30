from focus_engine import FocusEngine
from unittest.mock import MagicMock
import time
import logging

# Mute standard logs
logging.basicConfig(level=logging.ERROR)

def test_focus_logic():
    print("🧪 STARTING FOCUS ENGINE LOGIC TEST")
    
    # 1. Initialize Engine with Mocked Fyers/Bot
    engine = FocusEngine()
    engine.fyers = MagicMock() # Mock API
    engine.bot = MagicMock() # Mock Telegram
    
    # 2. Start Trade (Short)
    symbol = "TEST-EQ"
    entry = 100.0
    sl = 110.0
    engine.start_focus(symbol, entry, sl)
    
    trade = engine.active_trade
    print(f"✅ Trade Started: Entry {trade['entry']}, SL {trade['sl']}, TP1 {trade['tp1']}, TP2 {trade['tp2']}")
    
    # 3. Simulate Price Moves
    ticks = [
        (98.0, "Small Profit"),
        (92.0, "Near TP1"),
        (90.0, "Hit TP1 (Should BE)"),
        (85.0, "Between TP1-TP2"),
        (80.0, "Hit TP2 (Should Trail)"),
        (75.0, "Deep Profit (Tighten Trail)"),
        (78.0, "Pullback (Trail Holds)"),
        (95.0, "Reversal (Should Hit Trail)")
    ]
    
    for price, desc in ticks:
        print(f"\n--- Tick: {price} ({desc}) ---")
        engine.process_tick(price, 1000)
        
        t = engine.active_trade
        if t:
            print(f"   SL: {t['sl']:.2f} | BE: {t['sl_at_be']} | Trail: {t['trailing_active']}")
        else:
             print("   ⚠️ Trade Closed (Active Trade is None)")
             if price == 95.0: # Reversal step
                 print("🛑 STOP LOSS HIT Confirmed.")
             break
            
    # Verify Outcome
    expected_sl_hit = True
    if trade['status'] == 'SL HIT' and trade['sl'] < entry:
        print(f"\n✅ SUCCESS: Trade closed at Trail ({trade['sl']}) in profit.")
    else:
        print(f"\n❌ FAILURE: Trade outcome unexpected: {trade['status']} @ {trade['sl']}")

if __name__ == "__main__":
    test_focus_logic()
