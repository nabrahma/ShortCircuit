
import sys
import os
import logging

# Add parent dir
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from analyzer import FyersAnalyzer

logging.basicConfig(level=logging.INFO)

class MockFyers:
    def depth(self, data):
        symbol = data.get("symbol")
        # Scenario 1: Safe Stock (UC 100, LTP 90)
        if symbol == "SAFE":
            return {
                "d": {
                    "SAFE": {
                        "upper_ckt": 100.0,
                        "lower_ckt": 80.0
                    }
                }
            }
        # Scenario 2: Trap Stock (UC 100, LTP 99.0)
        elif symbol == "TRAP":
            return {
                "d": {
                    "TRAP": {
                        "upper_ckt": 100.0,
                        "lower_ckt": 80.0
                    }
                }
            }
        return {}

def test_circuit_guard():
    print("--- Testing Circuit Guard ---")
    fyers = MockFyers()
    analyzer = FyersAnalyzer(fyers)
    
    # 1. Safe Scenario
    # LTP 90, UC 100 -> Gap is 10%. Safe.
    is_blocked = analyzer._check_circuit_guard("SAFE", 90.0)
    print(f"SAFE (90/100): Blocked={is_blocked} [Expected: False]")
    assert is_blocked == False
    
    # 2. Trap Scenario
    # LTP 99, UC 100 -> Gap is 1%. Trap. (Buffer is 1.5%)
    is_blocked = analyzer._check_circuit_guard("TRAP", 99.0)
    print(f"TRAP (99/100): Blocked={is_blocked} [Expected: True]")
    assert is_blocked == True
    
    print("\n✅ Circuit Guard Logic Verified")

if __name__ == "__main__":
    test_circuit_guard()
