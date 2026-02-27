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

def format_stock_symbol(symbol: str) -> str:
    """
    Convert simple stock symbol to Fyers format
    
    Args:
        symbol: Stock symbol (e.g., 'SBIN', 'RELIANCE')
        
    Returns:
        Fyers formatted symbol (e.g., 'NSE:SBIN-EQ')
    """
    if validate_symbol(symbol):
        return symbol  # Already in correct format
    
    # Add NSE exchange and EQ type
    return f'NSE:{symbol}-EQ'


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

def get_front_month_futures(eq_symbol: str) -> str | None:
    """
    NSE:RELIANCE-EQ  →  NSE:RELIANCE25MARFUT
    NSE:IDEA-EQ      →  None  (will fail REST call gracefully)

    Auto-rolls within 3 calendar days of expiry.
    Returns None on malformed input — caller handles gracefully.
    """
    try:
        import pytz
        IST = pytz.timezone("Asia/Kolkata")
        now = datetime.now(IST)

        expiry = _last_thursday(now.year, now.month)
        days_to_expiry = (expiry.date() - now.date()).days

        # Within 3 days of expiry → roll to next month
        if days_to_expiry <= 3:
            if now.month == 12:
                year, month = now.year + 1, 1
            else:
                year, month = now.year, now.month + 1
            expiry = _last_thursday(year, month)

        month_code = expiry.strftime("%b").upper()   # MAR, APR, MAY
        year_code  = expiry.strftime("%y")           # 25, 26

        base = eq_symbol.replace("NSE:", "").replace("-EQ", "")
        if not base:
            return None
        return f"NSE:{base}{year_code}{month_code}FUT"

    except Exception:
        return None
