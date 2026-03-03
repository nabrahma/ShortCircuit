import pytest
pytestmark = pytest.mark.skip(reason="Requires live Fyers auth — run manually only")
pytest.skip("Requires live Fyers auth — run manually only", allow_module_level=True)

"""
Isolated candle API test — run tonight before sleeping.
Confirms Fyers v3 history() returns data with corrected parameter types.
"""
import sys
import os
import datetime

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from fyers_connect import FyersConnect

print("=" * 60)
print("Candle API Parameter Test — PRD v2.0 BUG-03")
print("=" * 60)

# Authenticate (same pattern as main.py L158-159)
fyers_conn = FyersConnect(config)
fyers = fyers_conn.fyers

if not fyers:
    print("❌ Fyers authentication failed")
    sys.exit(1)

today = datetime.date.today()
five_back = today - datetime.timedelta(days=5)

test_cases = [
    ("NSE:NIFTY50-INDEX", "1"),
    ("NSE:RELIANCE-EQ", "1"),
    ("NSE:TCS-EQ", "15"),
]

all_pass = True
for symbol, resolution in test_cases:
    data = {
        "symbol": symbol,
        "resolution": resolution,
        "date_format": "1",
        "range_from": five_back.strftime("%Y-%m-%d"),
        "range_to": today.strftime("%Y-%m-%d"),
        "cont_flag": "1",
    }

    try:
        r = fyers.history(data=data)
        status = r.get("s", "unknown")
        candles = r.get("candles", [])
        count = len(candles)

        if status == "ok" and count > 0:
            last = candles[-1]
            print(f"  ✅ {symbol} ({resolution}m) → {count} bars | last={last}")
        else:
            print(f"  ❌ {symbol} ({resolution}m) → EMPTY | status={status} | msg={r.get('message', '')}")
            all_pass = False
    except Exception as e:
        print(f"  ❌ {symbol} ({resolution}m) → EXCEPTION: {e}")
        all_pass = False

print()
if all_pass:
    print("✅ ALL TESTS PASSED — candle API is working with corrected params")
    print("   Safe to deploy for tomorrow's session")
else:
    print("❌ SOME TESTS FAILED — DO NOT DEPLOY. Fix params first.")

sys.exit(0 if all_pass else 1)
