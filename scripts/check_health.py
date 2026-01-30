import sys
import os

# Add parent directory to path to allow importing modules from root
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

print("Checking imports...")
try:
    import analyzer
    import main
    import market_context
    import signal_manager
    import htf_confluence
    print("✅ Core modules imported successfully.")
except ImportError as e:
    print(f"❌ Import failed: {e}")
    sys.exit(1)
except Exception as e:
    print(f"❌ Error: {e}")
    sys.exit(1)

print("Checking config...")
if not os.path.exists("../access_token.txt"):
    print("⚠️  access_token.txt not found in root (expected if new session)")
else:
    print("✅ access_token.txt found.")

print("\nSystem Health: GOOD")
