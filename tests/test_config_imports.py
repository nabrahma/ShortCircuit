import sys
import os
import unittest

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

class TestConfigImports(unittest.TestCase):
    def test_market_context_import(self):
        try:
            import market_context
            print("✅ market_context imported successfully")
        except ImportError as e:
            self.fail(f"market_context import failed: {e}")
            
    def test_focus_engine_import(self):
        try:
            import focus_engine
            print("✅ focus_engine imported successfully")
        except ImportError as e:
            self.fail(f"focus_engine import failed: {e}")

    def test_order_manager_import(self):
        try:
            import order_manager
            print("✅ order_manager imported successfully")
        except ImportError as e:
            self.fail(f"order_manager import failed: {e}")

    def test_discretionary_engine_import(self):
        try:
            import discretionary_engine
            print("✅ discretionary_engine imported successfully")
        except ImportError as e:
            self.fail(f"discretionary_engine import failed: {e}")

if __name__ == '__main__':
    unittest.main()
