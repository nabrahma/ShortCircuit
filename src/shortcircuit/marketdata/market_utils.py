from datetime import datetime, time as dt_time

import pytz

IST = pytz.timezone("Asia/Kolkata")


def is_market_hours() -> bool:
    """
    Return True when current IST time is within regular NSE cash hours.
    """
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    current = now.time()
    return dt_time(9, 15) <= current <= dt_time(15, 30)
