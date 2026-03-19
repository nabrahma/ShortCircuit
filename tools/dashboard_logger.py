import logging
from dashboard_bridge import get_dashboard_bridge

class DashboardLoggerHandler(logging.Handler):
    """
    A custom logging handler that redirects all bot logs to the AEGIS HUD.
    """
    def __init__(self, bridge=None):
        super().__init__()
        self.bridge = bridge or get_dashboard_bridge()
        self.setFormatter(logging.Formatter('%(message)s'))

    def emit(self, record):
        try:
            msg = self.format(record)
            level = record.levelname
            # Filter out internal HUD noise to prevent feedback loops
            if "uvicorn" in msg.lower() or "DashboardBridge" in msg:
                return
            
            self.bridge.broadcast_log(msg, level)
        except Exception:
            self.handleError(record)
