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
            cls._instance.queue = asyncio.Queue(maxsize=100)
            cls._instance.is_running = False
            cls._instance.active_connections = []
        return cls._instance

    def broadcast(self, message_type: str, data: Dict[str, Any]):
        """
        Non-blocking broadcast. 
        Module usage: get_dashboard_bridge().broadcast("GATE_UPDATE", {"gate": "G5", "status": "PASS"})
        """
        payload = {
            "type": message_type,
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "payload": data
        }
        
        try:
            # We use a try-except because we might be calling this from a sync context 
            # or before the loop is ready.
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # If we're in an async loop, we can't block. 
                # If we're in a thread, we need to be careful.
                asyncio.run_coroutine_threadsafe(self.queue.put(payload), loop)
            else:
                # Fallback or initialization phase
                pass
        except Exception:
            # If the queue is full or loop isn't ready, we silently drop for V1 (Performance priority)
            pass

    async def get_next_message(self) -> Any:
        """Dashboard server calls this to wait for new messages to broadcast via WebSockets."""
        return await self.queue.get()

def get_dashboard_bridge() -> DashboardBridge:
    return DashboardBridge()
