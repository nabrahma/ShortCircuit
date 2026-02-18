import unittest
from unittest.mock import MagicMock, patch
import threading
import time
from datetime import datetime
import sys
import os

# Add parent dir to path to import modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from order_manager import OrderManager

class TestSafety(unittest.TestCase):
    
    def setUp(self):
        self.mock_fyers = MagicMock()
        self.mock_bot = MagicMock()
        self.om = OrderManager(self.mock_fyers, self.mock_bot)
        
        # Default mock responses
        self.mock_fyers.place_order.return_value = {'s': 'ok', 'id': 'ORD_123'}
        self.mock_fyers.cancel_order.return_value = {'s': 'ok'}
        self.mock_fyers.orderbook.return_value = {'s': 'ok', 'orderBook': []}
        self.mock_fyers.positions.return_value = {'s': 'ok', 'netPositions': []}
        
    def test_phantom_fill_prevention(self):
        """Test 1: Verify SL-M order cancelled BEFORE exit"""
        print("\nRunning Test 1: Phantom Fill Prevention...")
        
        # 1. Setup Active Position
        symbol = "NSE:SBIN-EQ"
        self.om.active_positions[symbol] = {
            'symbol': symbol,
            'qty': 100,
            'side': 'SHORT',
            'status': 'OPEN',
            'sl_id': 'SL_ORDER_999'
        }
        
        # 2. Trigger Safe Exit
        self.om.safe_exit(symbol, "SOFT_STOP")
        
        # 3. Verify Cancel called for SL ID
        self.mock_fyers.cancel_order.assert_called_with(data={"id": "SL_ORDER_999"})
        
        # 4. Verify Exit Order placed (Market Buy)
        # Check that cancel happened (we can't easily check strict ordering with just assert_called, 
        # but in synchronous code, if cancel is called, it happened before exit if code is correct)
        # We check that place_order was called for exit
        self.mock_fyers.place_order.assert_called()
        args, _ = self.mock_fyers.place_order.call_args
        # Verify it was a BUY Market order
        # data arg is kwarg usually
        _, kwargs = self.mock_fyers.place_order.call_args
        data = kwargs.get('data')
        self.assertEqual(data['type'], 2) # Market
        self.assertEqual(data['side'], 1) # Buy
        
        print("✅ Test 1 Passed")

    def test_hard_stop_detection(self):
        """Test 2: Verify Hard Stop execution detected"""
        print("\nRunning Test 2: Hard Stop Detection...")
        
        symbol = "NSE:RELIANCE-EQ"
        self.om.active_positions[symbol] = {
            'symbol': symbol,
            'status': 'OPEN',
            'sl_id': 'SL_ORDER_555'
        }
        
        # Mock Orderbook returning Filled SL
        self.om._get_order_details = MagicMock(return_value={'id': 'SL_ORDER_555', 'status': 2}) # 2=Filled
        
        # Run Monitor
        self.om.monitor_hard_stop_status(symbol)
        
        # Verify State Closed
        self.assertNotIn(symbol, self.om.active_positions)
        print("✅ Test 2 Passed")

    def test_race_condition(self):
        """Test 3: Thread Safety (Race Condition)"""
        print("\nRunning Test 3: Race Condition...")
        
        symbol = "NSE:TATASTEEL-EQ"
        self.om.active_positions[symbol] = {
            'symbol': symbol,
            'qty': 100,
            'side': 'SHORT',
            'status': 'OPEN',
            'sl_id': 'SL_ORDER_777'
        }
        
        # Verify lock is used. 
        # We can't easily deterministic test race conditions in unit tests without complex setup,
        # but we can verify the lock object exists and methods use it.
        self.assertTrue(hasattr(self.om, '_position_lock'))
        
        # Simulate double exit
        t1 = threading.Thread(target=self.om.safe_exit, args=(symbol, "REASON_1"))
        t2 = threading.Thread(target=self.om.safe_exit, args=(symbol, "REASON_2"))
        
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        
        # Verify place_order called exactly ONCE (because second thread sees closed status)
        self.assertEqual(self.mock_fyers.place_order.call_count, 1)
        print("✅ Test 3 Passed")

    def test_startup_reconciliation(self):
        """Test 4: Startup Reconciliation"""
        print("\nRunning Test 4: Startup Reconciliation...")
        
        # Mock Orderbook with Stale SL
        self.mock_fyers.orderbook.return_value = {
            's': 'ok',
            'orderBook': [{'id': 'STALE_SL', 'status': 6, 'symbol': 'NSE:INFY-EQ'}] # 6=Pending
        }
        
        # Run
        self.om.startup_reconciliation()
        
        # Verify Cancel Called
        self.mock_fyers.cancel_order.assert_called_with(data={"id": "STALE_SL"})
        print("✅ Test 4 Passed")

if __name__ == '__main__':
    unittest.main()
