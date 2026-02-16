import logging
import config
from datetime import datetime
from discretionary_signals import DiscretionarySignals

logger = logging.getLogger(__name__)

class DiscretionaryEngine:
    """
    Phase 41.3: "Brain" for intelligent exit decisions.
    
    Evaluates:
    1. Soft Stop Breaches (Hold vs Exit)
    2. Profit Target Approaches (Take Profit vs Extend)
    """
    
    def __init__(self, fyers, order_manager):
        self.fyers = fyers
        self.order_manager = order_manager
        self.signals = DiscretionarySignals(fyers)
        self.conf = config.DISCRETIONARY_CONFIG

    def evaluate_soft_stop(self, symbol, position):
        """
        Called when price hits the "Soft Stop" level (e.g., 0.5% loss).
        Decides whether to HOLD (if signals imply reversal) or EXIT.
        
        Returns: 'HOLD' or 'EXIT'
        """
        try:
            # Get Context
            entry_price = position['entry_price']
            entry_time = position['entry_time']
            
            # Fetch Signals
            analysis = self.signals.evaluate_all_signals(symbol, entry_price)
            signals = analysis['signals']
            bullish = analysis['bullish_count']
            bearish = analysis['bearish_count']
            
            # Log Analysis
            log_msg = (
                f"\n⚠️ [SOFT STOP] Triggered for {symbol}\n"
                f"Signals:\n"
                f"  📊 Orderflow: {self._fmt(signals['orderflow'])}\n"
                f"  📈 Volume:     {self._fmt(signals['volume'])}\n"
                f"  🎯 Tests:      {self._fmt(signals['price_tests'])}\n"
                f"  💧 Liquidity:  {self._fmt(signals['liquidity'])}\n"
                f"  ⏱️  MTF:        {self._fmt(signals['mtf'])}\n"
                f"  ⚡ Velocity:   {self._fmt(signals['velocity'])}\n"
                f"Score: {bearish} Bearish vs {bullish} Bullish"
            )
            logger.info(log_msg)
            
            # DECISION LOGIC
            decision = 'HOLD'
            reason = "Mixed signals"
            
            # Case 1: Too many Bullish Signals (Reversal confirmed) -> EXIT
            if bullish >= self.conf['bullish_exit_threshold']:
                logger.info(f"🛑 [DECISION] EXIT {symbol} (Bullish Reversal Confirmed)")
                decision = 'EXIT'
                reason = "Bullish Reversal"
            
            # Case 2: Strong Bearish Signals (Fakeout) -> HOLD
            elif bearish >= self.conf['bearish_hold_threshold']:
                logger.info(f"✅ [DECISION] HOLD {symbol} (Bearish Continuation likely)")
                decision = 'HOLD'
                reason = "Bearish Continuation"
            
            # Case 3: Mixed Signals -> Time Filter
            else:
                mins_in_trade = (datetime.now() - entry_time).seconds / 60
                min_time = self.conf['min_time_before_exit_minutes']
                
                if mins_in_trade < min_time:
                    logger.info(f"⏱️ [DECISION] HOLD {symbol} (Only {mins_in_trade:.1f}m in trade)")
                    decision = 'HOLD'
                    reason = "Time Filter"
                else:
                    logger.info(f"🟡 [DECISION] EXIT {symbol} (Mixed signals + Time elapsed)")
                    decision = 'EXIT'
                    reason = "Time Expired"

            # Phase 41.3.2: Log Event to DB
            try:
                if self.order_manager and self.order_manager.telegram and hasattr(self.order_manager.telegram, 'journal'):
                    db = self.order_manager.telegram.journal.db
                    trade_id = position.get('trade_id')
                    
                    event_data = {
                        'trade_id': trade_id,
                        'date': datetime.now().strftime("%Y-%m-%d"),
                        'symbol': symbol,
                        'entry_price': entry_price,
                        'soft_stop_trigger_price': position.get('soft_sl', 0),
                        'soft_stop_decision': decision,
                        'exit_price': 0, # Not exited yet (or about to be)
                        'exit_reason': reason if decision == 'EXIT' else None,
                        'orderflow_signal': signals['orderflow'],
                        'volume_signal': signals['volume'],
                        'price_tests_signal': signals['price_tests'],
                        'liquidity_signal': signals['liquidity'],
                        'mtf_signal': signals['mtf'],
                        'velocity_signal': signals['velocity'],
                        'signal_score': bearish - bullish, # Net Score
                        'time_in_trade_minutes': int((datetime.now() - entry_time).seconds / 60),
                        'outcome': 'PENDING'
                    }
                    db.log_event('soft_stop_events', event_data)
            except Exception as e:
                logger.error(f"Failed to log soft stop event: {e}")

            return decision
                    
        except Exception as e:
            logger.error(f"Error in Soft Stop Eval: {e}")
            return 'EXIT' # Default safety

    def evaluate_profit_extension(self, symbol, position):
        """
        Called when nearing Initial Target.
        Decides whether to Take Profit or Extend for a runner.
        
        Returns: 'EXTEND', 'TAKE_PROFIT', 'HOLD'
        """
        try:
            analysis = self.signals.evaluate_all_signals(symbol, position['entry_price'])
            s = analysis['signals']
            
            # Momentum Score: Volume + Velocity + MTF (Key for extensions)
            momentum_score = s['volume'] + s['velocity'] + s['mtf']
            
            # Log
            logger.info(f"\n💰 [TARGET] Approaching for {symbol} | Momentum Score: {momentum_score}")
            
            # Strong Bearish Momentum -> EXTEND
            if momentum_score >= self.conf['momentum_extend_threshold']:
                logger.info(f"🚀 [DECISION] EXTEND TARGET (Strong Momentum)")
                return 'EXTEND'
            
            # Weak/Bullish Momentum -> TAKE PROFIT
            elif momentum_score <= 0:
                logger.info(f"💰 [DECISION] TAKE PROFIT (Momentum Fading)")
                return 'TAKE_PROFIT'
            
            return 'HOLD' # Wait
            
        except Exception as e:
            logger.error(f"Error in Profit Extension: {e}")
            return 'TAKE_PROFIT'

    def _fmt(self, val):
        if val == 1: return "🟢 Bearish"
        if val == -1: return "🔴 Bullish"
        return "⚪ Neutral"
