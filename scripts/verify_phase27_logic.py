
import sys
import os
import pandas as pd
import datetime
import logging

# Add parent dir to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from analyzer import FyersAnalyzer
from market_profile import ProfileAnalyzer

# Mock Fyers
class MockFyers:
    def history(self, data): return {}
    def depth(self, data): return {}

logging.basicConfig(level=logging.INFO)

def test_oi_logic():
    print("--- Testing OI Divergence ---")
    fyers = MockFyers()
    analyzer = FyersAnalyzer(fyers)
    
    symbol = "NSE:TEST-EQ"
    
    # Simulate Price Up, OI Up (Healthy)
    analyzer._track_oi(symbol, 100, 1000)
    analyzer._track_oi(symbol, 101, 1100)
    analyzer._track_oi(symbol, 102, 1200)
    analyzer._track_oi(symbol, 103, 1300)
    analyzer._track_oi(symbol, 104, 1400) # Price +4%, OI +40%
    
    is_fake, msg = analyzer._check_oi_divergence(symbol, 104)
    print(f"Scenario 1 (Healthy): Fakeout={is_fake} | {msg}")
    assert is_fake == False
    
    # Simulate Price Up, OI DOWN (Fakeout)
    analyzer.oi_history[symbol].clear()
    analyzer._track_oi(symbol, 100, 5000)
    analyzer._track_oi(symbol, 101, 4900)
    analyzer._track_oi(symbol, 102, 4800)
    analyzer._track_oi(symbol, 103, 4500)
    analyzer._track_oi(symbol, 105, 4000) # Price +5%, OI -20%
    
    is_fake, msg = analyzer._check_oi_divergence(symbol, 105)
    print(f"Scenario 2 (Fakeout): Fakeout={is_fake} | {msg}")
    assert is_fake == True
    print("✅ OI Logic Verified")

def test_dpoc_logic():
    print("\n--- Testing dPOC Logic ---")
    profile = ProfileAnalyzer()
    
    # Create Data where POC is Low (100) but Price is High (105)
    # 80% volume at 100, 20% volume at 105
    data = []
    
    # Bulk at 100
    for _ in range(80):
        data.append({'close': 100.0, 'high': 100.1, 'low': 99.9})
        
    # Thin move to 105
    for _ in range(20):
        data.append({'close': 105.0, 'high': 105.1, 'low': 104.9})
        
    df = pd.DataFrame(data)
    
    poc = profile.get_developing_poc(df)
    print(f"Calculated POC: {poc}")
    
    # POC should be around 100
    assert 99.0 <= poc <= 101.0
    
    # Now check divergence logic in analyzer
    fyers = MockFyers()
    analyzer = FyersAnalyzer(fyers)
    
    # Test method directly
    is_div, msg = analyzer._check_dpoc_divergence("TEST", 105.0, df)
    print(f"Scenario 1 (Div): {is_div} | {msg}")
    
    # Should be True because Price (105) > POC (100) + 1%
    assert is_div == True
    print("✅ dPOC Logic Verified")

if __name__ == "__main__":
    try:
        test_oi_logic()
        test_dpoc_logic()
        print("\n🚀 ALL SYSTEMS GO: Phase 27 Logic Validated.")
    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
