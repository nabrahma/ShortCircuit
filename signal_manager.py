"""
Signal Manager Module
Handles daily signal caps, per-symbol cooldowns, and consecutive loss tracking.
Based on Volman's principle: "Quality over quantity - 80% of profits from 20% of trades."
"""
import logging
from datetime import datetime, timedelta
from collections import defaultdict

logger = logging.getLogger(__name__)

class SignalManager:
    """
    Manages signal flow to prevent overtrading and enforce quality filters.
    """
    
    def __init__(self, max_signals_per_day=5, cooldown_minutes=45):
        self.max_signals_per_day = max_signals_per_day
        self.cooldown_minutes = cooldown_minutes
        
        # Daily tracking
        self.signals_today = []
        self.current_date = None
        
        # Per-symbol tracking
        self.last_signal_time = {}  # symbol -> datetime
        
        # Loss tracking for auto-pause
        self.consecutive_losses = 0
        self.max_consecutive_losses = 3
        self.is_paused = False
        
        # Stats
        self.stats = defaultdict(int)
    
    def _reset_if_new_day(self):
        """Reset counters at the start of a new trading day."""
        today = datetime.now().date()
        
        if self.current_date != today:
            logger.info(f"New trading day: {today}. Resetting signal manager.")
            self.signals_today = []
            self.last_signal_time = {}
            self.consecutive_losses = 0
            self.is_paused = False
            self.current_date = today
            self.stats = defaultdict(int)
    
    def can_signal(self, symbol):
        """
        Check if we can send a signal for this symbol.
        
        Args:
            symbol: The stock symbol (e.g., "NSE:HINDCOPPER-EQ")
            
        Returns:
            tuple: (allowed, reason)
        """
        self._reset_if_new_day()
        now = datetime.now()
        
        # Check 1: Daily limit
        if len(self.signals_today) >= self.max_signals_per_day:
            self.stats['blocked_daily_limit'] += 1
            return False, f"Daily limit reached ({self.max_signals_per_day} signals)"
        
        # Check 2: Paused due to consecutive losses
        if self.is_paused:
            self.stats['blocked_paused'] += 1
            return False, f"Trading paused after {self.max_consecutive_losses} consecutive losses"
        
        # Check 3: Per-symbol cooldown
        if symbol in self.last_signal_time:
            time_diff = (now - self.last_signal_time[symbol]).total_seconds() / 60
            if time_diff < self.cooldown_minutes:
                remaining = self.cooldown_minutes - time_diff
                self.stats['blocked_cooldown'] += 1
                return False, f"Cooldown: {symbol} signaled {time_diff:.0f}m ago (wait {remaining:.0f}m)"
        
        return True, "OK"
    
    def record_signal(self, symbol, entry_price, stop_loss, pattern):
        """
        Record a new signal being sent.
        
        Args:
            symbol: Stock symbol
            entry_price: Entry price
            stop_loss: Stop loss price
            pattern: Pattern name
        """
        self._reset_if_new_day()
        now = datetime.now()
        
        signal_record = {
            'symbol': symbol,
            'time': now,
            'entry': entry_price,
            'stop': stop_loss,
            'pattern': pattern,
            'status': 'OPEN'  # Will be updated when we track outcome
        }
        
        self.signals_today.append(signal_record)
        self.last_signal_time[symbol] = now
        self.stats['signals_sent'] += 1
        
        logger.info(f"Signal recorded: {symbol} @ {entry_price} (#{len(self.signals_today)} today)")
    
    def record_outcome(self, symbol, is_win):
        """
        Record the outcome of a trade for consecutive loss tracking.
        
        Args:
            symbol: Stock symbol
            is_win: True if trade was profitable
        """
        if is_win:
            self.consecutive_losses = 0
            self.stats['wins'] += 1
            logger.info(f"WIN recorded for {symbol}. Consecutive losses reset.")
        else:
            self.consecutive_losses += 1
            self.stats['losses'] += 1
            logger.warning(f"LOSS recorded for {symbol}. Consecutive losses: {self.consecutive_losses}")
            
            if self.consecutive_losses >= self.max_consecutive_losses:
                self.is_paused = True
                logger.critical(f"TRADING PAUSED: {self.consecutive_losses} consecutive losses")
    
    def get_remaining_signals(self):
        """Get how many signals are left for today."""
        self._reset_if_new_day()
        return max(0, self.max_signals_per_day - len(self.signals_today))
    
    def get_status(self):
        """Get current status for dashboard/logging."""
        self._reset_if_new_day()
        return {
            'date': self.current_date,
            'signals_sent': len(self.signals_today),
            'signals_remaining': self.get_remaining_signals(),
            'consecutive_losses': self.consecutive_losses,
            'is_paused': self.is_paused,
            'symbols_on_cooldown': list(self.last_signal_time.keys()),
            'stats': dict(self.stats)
        }
    
    def get_signals_summary(self):
        """Get summary of today's signals for EOD analysis."""
        self._reset_if_new_day()
        return self.signals_today.copy()


# Global singleton instance
_signal_manager = None

def get_signal_manager():
    """Get the global signal manager instance."""
    global _signal_manager
    if _signal_manager is None:
        _signal_manager = SignalManager(
            max_signals_per_day=5,
            cooldown_minutes=45
        )
    return _signal_manager
