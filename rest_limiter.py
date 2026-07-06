# rest_limiter.py
"""
Global REST API Rate Limiter for Fyers API v3.

Fyers enforces a hard limit of 10 requests/second and 200 requests/minute
across ALL endpoints, shared by User ID. This module provides a single
process-wide token bucket that all modules (scanner, reconciliation,
market_context) must acquire before making any REST call.

Usage:
    from rest_limiter import rest_limiter
    rest_limiter.acquire()   # blocks until a token is available
    response = fyers.history(data=...)
"""

import threading
import time
import logging

logger = logging.getLogger(__name__)

class TokenBucketRateLimiter:
    """
    Thread-safe token bucket rate limiter.
    Default: 8 tokens/sec (leaves 2 req/sec headroom below Fyers' 10/sec limit).
    """

    def __init__(self, rate: float = 8.0, burst: int = 8):
        self._rate = rate        # tokens refilled per second
        self._burst = burst      # max tokens in bucket at once
        self._tokens = float(burst)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0):
        """Block until a token is available, then consume it."""
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                # Calculate wait time until next token is available
                wait = (tokens - self._tokens) / self._rate

            time.sleep(wait)

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        self._last_refill = now


# Singleton — import this everywhere
rest_limiter = TokenBucketRateLimiter(rate=8.0, burst=8)
