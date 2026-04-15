import asyncio
import logging
import json
from datetime import datetime
from typing import Dict, Any, List, Optional

logger = logging.getLogger("DashboardBridge")

class DashboardBridge:
    """
    Singleton bridge between the trading bot and the FastAPI dashboard server.
    Allows for fire-and-forget broadcasting of state changes.
    """
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(DashboardBridge, cls).__new__(cls)
            # Increased size to handle high-frequency log streams
            cls._instance.queue = asyncio.Queue(maxsize=500)
            cls._instance.is_running = False
            cls._instance.active_connections = []
            cls._instance._loop = None
        return cls._instance

    def set_loop(self, loop):
        """Must be called by main.py at startup."""
        self._loop = loop

    def _safe_put_nowait(self, payload: Dict[str, Any]):
        """Helper to safely put into queue inside the event loop."""
        try:
            self.queue.put_nowait(payload)
        except (asyncio.QueueFull, Exception):
            # Drop message if queue is full or other loop-level error occurs
            pass

    def broadcast(self, message_type: str, data: Dict[str, Any]):
        """Non-blocking broadcast to all dashboard clients."""
        payload = {
            "type": message_type,
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "payload": data
        }
        
        if not self._loop:
            return

        try:
            if self._loop.is_running():
                # Scheduled via helper to catch exceptions inside the loop context
                self._loop.call_soon_threadsafe(self._safe_put_nowait, payload)
        except Exception:
            # Catch errors in call_soon_threadsafe call itself
            pass

    def broadcast_log(self, message: str, level: str = "INFO"):
        """Convenience method for log streaming."""
        self.broadcast("LOG_STREAM", {"msg": message, "level": level})

    def broadcast_candidate_pulse(self, symbol: str, metrics: Dict[str, Any]):
        """High-frequency candidate telemetry."""
        data = {"symbol": symbol, **metrics}
        self.broadcast("CANDIDATE_PULSE", data)

    async def get_next_message(self) -> Any:
        """Dashboard server calls this to wait for new messages to broadcast via WebSockets."""
        return await self.queue.get()

def get_dashboard_bridge() -> DashboardBridge:
    return DashboardBridge()
