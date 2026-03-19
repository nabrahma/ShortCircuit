"""
Signal Manager Module
Handles daily signal caps, per-symbol cooldowns, and consecutive loss tracking.
Based on Volman's principle: "Quality over quantity - 80% of profits from 20% of trades."
"""
import logging
import threading
from datetime import datetime, timedelta
from collections import defaultdict

logger = logging.getLogger(__name__)

class SignalManager:
    """
    Manages signal flow to prevent overtrading and enforce quality filters.
    """
    
    def __init__(self, cooldown_minutes=45):
        self.cooldown_minutes = cooldown_minutes
        
        # Daily tracking
        self.signals_today = []
        self.current_date = None
        
        # Per-symbol tracking
        self.last_signal_time = {}  # symbol -> datetime
        
        # PnL tracking for auto-pause (Phase 69)
        import config
        self.daily_pnl = 0.0
        self.max_session_loss = getattr(config, 'MAX_SESSION_LOSS_INR', 500.0)
        self.is_paused = False
        
        # Stats
        self.stats = defaultdict(int)
        self._lock = threading.Lock()  # FIX #4: protect list/dict from concurrent thread access
        self._exec_cooldowns: dict = {}   # symbol → datetime (unblock_at)
    
    def _reset_if_new_day(self):
        """Reset counters at the start of a new trading day."""
        today = datetime.now().date()
        
        if self.current_date != today:
            logger.info(f"New trading day: {today}. Resetting signal manager.")
            self.signals_today = []
            self.last_signal_time = {}
            self.daily_pnl = 0.0
            self.is_paused = False
            self.current_date = today
            self.stats = defaultdict(int)
    
    def can_signal(self, symbol, is_execution=False):
        """
        Check if we can send a signal for this symbol.
        
        Args:
            symbol: The stock symbol (e.g., "NSE:HINDCOPPER-EQ")
            is_execution: If True, skips discovery-level cooldown check (Phase 73 Fix)
            
        Returns:
            tuple: (allowed, reason)
        """
        with self._lock:
            self._reset_if_new_day()
            now = datetime.now()
            
            # Check 0: Execution failure cooldown (Hard block on broker errors)
            cd = self._exec_cooldowns.get(symbol)
            if cd:
                now_ts = datetime.now()
                if now_ts < cd['blocked_until']:
                    remaining = int((cd['blocked_until'] - now_ts).total_seconds())
                    self.stats['blocked_exec_cooldown'] = self.stats.get('blocked_exec_cooldown', 0) + 1
                    return False, f"Exec cooldown: {symbol} blocked {remaining}s ({cd['reason']})"
                else:
                    del self._exec_cooldowns[symbol]  # expired
            

            # Check 2: Paused due to max session loss
            if self.is_paused:
                self.stats['blocked_paused'] += 1
                return False, f"Trading paused: Max session loss reached (₹{self.daily_pnl:.2f})"
            
            # Check 3: Per-symbol cooldown (Discovery only)
            if not is_execution and symbol in self.last_signal_time:
                unlock_at = self.last_signal_time[symbol]
                if now < unlock_at:
                    remaining = (unlock_at - now).total_seconds() / 60
                    self.stats['blocked_cooldown'] += 1
                    return False, f"Cooldown: {symbol} blocked for {remaining:.1f}m"
            
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
        import config
        with self._lock:
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
            
            # G8.1: Per-symbol cooldown (Standard)
            cooldown = self.cooldown_minutes
            if config.PHASE_51_ENABLED:
                cooldown = max(cooldown, config.P51_G8_COOLDOWN_ON_SIGNAL_ADD)
                
            self.last_signal_time[symbol] = now + timedelta(minutes=cooldown)
            self.stats['signals_sent'] += 1
            
            logger.info(f"Signal recorded: {symbol} @ {entry_price} (#{len(self.signals_today)} today). Cooldown set: {cooldown}m")

    def add_pending_signal(self, symbol: str):
        """
        Phase 51 [G8.3]: Set cooldown immediately when signal is added to FocusEngine.
        Prevents other scanners from picking up the same symbol.
        """
        import config
        with self._lock:
            self._reset_if_new_day()
            now = datetime.now()
            cooldown = config.P51_G8_COOLDOWN_ON_SIGNAL_ADD if config.PHASE_51_ENABLED else 30
            self.last_signal_time[symbol] = now + timedelta(minutes=cooldown)
            logger.info(f"G8.3 Cooldown set for {symbol}: {cooldown} minutes (Pending Signal added)")

    def record_execution_failure(
        self,
        symbol: str,
        cooldown_seconds: int = 900,
        reason: str = 'EXEC_FAILED'
    ):
        """
        Phase 44.6: Sets a hard re-entry block when enter_position() returns None.
        Different from record_signal() — this fires on FAILURE, not success.
        The scan can still discover the symbol; only execution is blocked.

        Cooldown guide:
          FILL_TIMEOUT  → 1200s (20 min)
          BROKER_ERROR  →  600s (10 min)
          ZERO_QTY      →  300s ( 5 min)
          Default       →  900s (15 min)
        """
        from datetime import timedelta
        with self._lock:
            unblock_at = datetime.now() + timedelta(seconds=cooldown_seconds)
            self._exec_cooldowns[symbol] = {
                'blocked_until': unblock_at,
                'reason':        reason,
                'set_at':        datetime.now(),
            }
            logger.warning(
                f"⏳ EXEC COOLDOWN SET | {symbol} | reason={reason} | "
                f"blocked {cooldown_seconds}s until {unblock_at.strftime('%H:%M:%S')}"
            )

    def is_exec_blocked(self, symbol: str) -> tuple:
        """
        Returns (blocked: bool, remaining_seconds: int, reason: str).
        Called by focus_engine before attempting entry.
        """
        with self._lock:
            cd = self._exec_cooldowns.get(symbol)
            if not cd:
                return False, 0, ''
            now = datetime.now()
            if now < cd['blocked_until']:
                remaining = int((cd['blocked_until'] - now).total_seconds())
                return True, remaining, cd['reason']
            # Expired — clean up
            del self._exec_cooldowns[symbol]
            return False, 0, ''
    
    def record_outcome(self, symbol: str, pnl: float):
        """
        Record the outcome of a trade for daily PnL risk limits.
        
        Args:
            symbol: Stock symbol
            pnl: Realized PnL from the trade
        """
        self.daily_pnl += pnl
        is_win = pnl > 0
        
        if is_win:
            self.stats['wins'] += 1
            logger.info(f"WIN recorded for {symbol} (₹{pnl:.2f}). Session PnL: ₹{self.daily_pnl:.2f}")
        else:
            self.stats['losses'] += 1
            logger.warning(f"LOSS recorded for {symbol} (₹{pnl:.2f}). Session PnL: ₹{self.daily_pnl:.2f}")
            
            if self.daily_pnl <= -self.max_session_loss:
                self.is_paused = True
                logger.critical(f"🚨 TRADING PAUSED: Max session loss limit breached. Session PnL: ₹{self.daily_pnl:.2f}")
    

    def get_status(self):
        """Get current status for dashboard/logging."""
        self._reset_if_new_day()
        return {
            'date': self.current_date,
            'signals_sent': len(self.signals_today),
            'signals_remaining': 'Unlimited',
            'daily_pnl': self.daily_pnl,
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
            cooldown_minutes=45
        )
    return _signal_manager
