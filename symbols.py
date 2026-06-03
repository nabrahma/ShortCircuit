"""
Central symbol registry for Fyers API
All symbols MUST use Fyers exact format
"""

# ===== INDICES =====
NIFTY_50 = 'NSE:NIFTY50-INDEX'
BANK_NIFTY = 'NSE:NIFTYBANK-INDEX'
FIN_NIFTY = 'NSE:FINNIFTY-INDEX'
MIDCAP_NIFTY = 'NSE:NIFTYMID50-INDEX'

# ===== REFERENCE INDEX =====
# Default index for market regime detection
DEFAULT_INDEX = NIFTY_50

# ===== SYMBOL VALIDATION =====
def validate_symbol(symbol: str) -> bool:
    """
    Validate symbol format for Fyers API
    
    Args:
        symbol: Symbol string to validate
        
    Returns:
        True if valid Fyers symbol format
        
    Examples:
        >>> validate_symbol('NSE:SBIN-EQ')
        True
        >>> validate_symbol('SBIN')
        False
    """
    if not symbol:
        return False
    
    # Must contain exchange:symbol-type format
    parts = symbol.split(':')
    if len(parts) != 2:
        return False
    
    exchange, instrument = parts
    
    # Must have hyphen for instrument type
    # Actually Fyers format is EXCHANGE:SYMBOL-SERIES for Equities e.g. NSE:SBIN-EQ
    # For Indices: NSE:NIFTY50-INDEX
    # So hyphen check is good for most, but let's be lenient if needed.
    # The PRD says "Must have hyphen for instrument type"
    if '-' not in instrument:
        return False
    
    return True



# ─── Phase 44.8 ────────────────────────────────────────────────
import calendar
from datetime import datetime, timedelta

def _last_thursday(year: int, month: int) -> datetime:
    """Return the last Thursday of the given month."""
    last_day = calendar.monthrange(year, month)[1]
    d = datetime(year, month, last_day)
    while d.weekday() != 3:  # 3 = Thursday
        d -= timedelta(days=1)
    return d

