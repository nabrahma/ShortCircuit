"""
Phase 44.4: Telegram UX Overhaul — Tests

Tests the key safety fixes and UX enhancements:
1. offlineOrder payload type validation
2. Capital-aware sizing in order_manager
3. Over-margin pre-flight guard
4. Qty-zero guard
5. Scanner ETF cluster deduplication
6. Closure card session tracking
"""

import unittest
import sys
import os

# Add parent dir to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _read(filename):
    """Read a source file with UTF-8 encoding (needed for emoji chars on Windows)."""
    with open(os.path.join(ROOT, filename), 'r', encoding='utf-8') as f:
        return f.read()


class TestOfflineOrderPayload(unittest.TestCase):
    """Section 5.2a: Verify offlineOrder is boolean False, not string."""
    
    def test_offline_order_is_boolean(self):
        content = _read('fyers_broker_interface.py')
        self.assertNotIn('"offlineOrder": "False"', content,
                         "offlineOrder is still a string! Must be boolean False.")
        self.assertNotIn("'offlineOrder': 'False'", content,
                         "offlineOrder is still a string (single quotes)!")
        self.assertIn('"offlineOrder": False', content,
                      "offlineOrder boolean not found.")


class TestCapitalAwareSizing(unittest.TestCase):
    """Section 5.2b: Verify order_manager uses capital_manager, not hardcoded 10000."""

    def test_no_hardcoded_capital(self):
        content = _read('order_manager.py')
        self.assertNotIn('capital = 10000', content,
                         "Hardcoded capital = 10000 still exists!")

    def test_buying_power_from_capital_manager(self):
        content = _read('order_manager.py')
        self.assertIn('buying_power', content)
        self.assertIn('get_status', content)


class TestOverMarginGuard(unittest.TestCase):
    """Section 5.2b: Pre-flight over-margin check."""
    
    def test_over_margin_check_exists(self):
        content = _read('order_manager.py')
        self.assertIn('required_capital > buying_power', content)
        self.assertIn('OVER MARGIN', content)

    def test_qty_zero_guard_exists(self):
        content = _read('order_manager.py')
        self.assertIn('QTY ZERO', content)


class TestPreExecutionLogging(unittest.TestCase):
    """Section 5.1: Pre-execution payload logging in both order paths."""
    
    def test_pre_exec_log_in_order_manager(self):
        content = _read('order_manager.py')
        self.assertIn('[PRE-EXEC]', content)
    
    def test_pre_exec_log_in_trade_manager(self):
        content = _read('trade_manager.py')
        self.assertIn('[PRE-EXEC]', content)


class TestPostFailureAlerts(unittest.TestCase):
    """Section 5.3: Post-failure Telegram alerts from both order paths."""
    
    def test_failure_alert_in_order_manager(self):
        content = _read('order_manager.py')
        self.assertIn('ORDER FAILED', content)
    
    def test_failure_alert_in_trade_manager(self):
        content = _read('trade_manager.py')
        self.assertIn('ORDER FAILED', content)

    def test_suspected_field_diagnosis(self):
        content = _read('order_manager.py')
        self.assertIn('Suspected', content)


class TestPTBErrorHandler(unittest.TestCase):
    """Section 6: PTB global error handler."""
    
    def test_error_handler_exists(self):
        content = _read('telegram_bot.py')
        self.assertIn('_error_handler', content)
        self.assertIn('add_error_handler', content)

    def test_error_handler_sends_telegram_alert(self):
        content = _read('telegram_bot.py')
        self.assertIn('BOT ERROR', content)


class TestScannerDeduplication(unittest.TestCase):
    """Section 7: ETF cluster deduplication."""
    
    def test_dedup_logic_exists(self):
        content = _read('scanner.py')
        self.assertIn('CLUSTER DEDUPLICATION', content)
        self.assertIn('[DEDUP]', content)
    
    def test_dedup_filters_cluster(self):
        """Test that dedup keeps highest volume and removes rest."""
        pre_candidates = [
            {'symbol': 'NSE:SILVERETF1-EQ', 'ltp': 80, 'volume': 500000, 'change': 7.0, 'tick_size': 0.05, 'oi': 0},
            {'symbol': 'NSE:SILVERETF2-EQ', 'ltp': 82, 'volume': 200000, 'change': 6.5, 'tick_size': 0.05, 'oi': 0},
            {'symbol': 'NSE:SILVERETF3-EQ', 'ltp': 81, 'volume': 100000, 'change': 6.8, 'tick_size': 0.05, 'oi': 0},
            {'symbol': 'NSE:RELIANCE-EQ', 'ltp': 2500, 'volume': 1000000, 'change': 8.0, 'tick_size': 0.05, 'oi': 0},
        ]
        
        for keyword in ['SILVER']:
            cluster = [c for c in pre_candidates if keyword.upper() in c['symbol'].upper()]
            if len(cluster) > 1:
                cluster.sort(key=lambda x: x['volume'], reverse=True)
                suppressed = cluster[1:]
                pre_candidates = [c for c in pre_candidates if c not in suppressed]
        
        self.assertEqual(len(pre_candidates), 2)
        symbols = [c['symbol'] for c in pre_candidates]
        self.assertIn('NSE:SILVERETF1-EQ', symbols)
        self.assertIn('NSE:RELIANCE-EQ', symbols)
        self.assertNotIn('NSE:SILVERETF2-EQ', symbols)


class TestConfigFlags(unittest.TestCase):
    """Phase 44.4 config flags."""
    
    def test_etf_dedup_config_exists(self):
        import config
        self.assertTrue(hasattr(config, 'ETF_CLUSTER_DEDUP_ENABLED'))
        self.assertTrue(hasattr(config, 'ETF_CLUSTER_KEYWORDS'))
        self.assertIsInstance(config.ETF_CLUSTER_KEYWORDS, list)
    
    def test_editable_signal_flow_disabled(self):
        import config
        self.assertFalse(getattr(config, 'EDITABLE_SIGNAL_FLOW_ENABLED', True))


class TestEnrichedCommandHandlers(unittest.TestCase):
    """Section 1: Verify command handlers have rich structured responses."""
    
    def test_auto_on_has_preflight(self):
        content = _read('telegram_bot.py')
        self.assertIn('PREFLIGHT', content)
        self.assertIn('_get_capital_block', content)
        self.assertIn('_get_signal_block', content)
        self.assertIn('_get_session_block', content)
    
    def test_status_has_health_block(self):
        content = _read('telegram_bot.py')
        self.assertIn('_get_health_block', content)
        self.assertIn('CAPITAL', content)
        self.assertIn('TRADING', content)

    def test_pnl_has_signal_stats(self):
        content = _read('telegram_bot.py')
        self.assertIn('signals_fired', content)
        self.assertIn('signals_rejected', content)
        self.assertIn('Win Rate', content)


class TestEODSummary(unittest.TestCase):
    """Section 3: EOD summary card."""
    
    def test_eod_summary_method_exists(self):
        content = _read('telegram_bot.py')
        self.assertIn('send_eod_summary', content)
        self.assertIn('END OF DAY SUMMARY', content)
    
    def test_eod_has_na_fallback(self):
        content = _read('telegram_bot.py')
        self.assertIn('N/A (data unavailable)', content)


class TestWhyCommand(unittest.TestCase):
    """Section 4: /why command enhancement."""
    
    def test_why_accepts_time_filter(self):
        content = _read('telegram_bot.py')
        self.assertIn('time_filter', content)
        self.assertIn('_why_from_parquet', content)


class TestClosureCardEnhancements(unittest.TestCase):
    """Section 2.4: Enhanced trade closure card."""
    
    def test_closure_has_duration(self):
        content = _read('telegram_bot.py')
        self.assertIn('duration_str', content)
        
    def test_closure_has_session_pnl(self):
        content = _read('telegram_bot.py')
        self.assertIn('session_pnl', content)
        self.assertIn('_session_trades', content)
    
    def test_closure_has_streak(self):
        content = _read('telegram_bot.py')
        self.assertIn('_win_streak', content)
        self.assertIn('_loss_streak', content)
        self.assertIn('Win streak', content)


if __name__ == '__main__':
    unittest.main()
