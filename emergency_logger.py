
import logging
import asyncio
import sys
import os
import aiofiles
from datetime import datetime
from queue import SimpleQueue
from logging.handlers import RotatingFileHandler

# Configuration
LOG_DIR = "logs/emergency"
os.makedirs(LOG_DIR, exist_ok=True)
EMERGENCY_LOG_FILE = os.path.join(LOG_DIR, "CRITICAL_FAILURE.log")

class EmergencyLogger:
    """
    Phase 42.1: Zero-Fail Async Logger.
    
    Responsibilities:
    1. Capture critical errors without blocking main loop.
    2. Fallback to stderr if disk write fails.
    3. Async queue-based implementation.
    """
    
    _instance = None
    
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(EmergencyLogger, cls).__new__(cls)
            cls._instance.queue = SimpleQueue()
            cls._instance.running = False
            # Defer setup to __init__ or explicit start to avoid side effects in __new__
            # But original code called _setup_sync_logger here. 
            # Let's keep it but ensure it's robust.
            cls._instance._setup_sync_logger()
        return cls._instance

    def _setup_sync_logger(self):
        """
        Setup standard python logger as backup.
        """
        self.logger = logging.getLogger("EMERGENCY")
        self.logger.setLevel(logging.CRITICAL)
        
        # Formatter
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        
        # File Handler
        fh = RotatingFileHandler(EMERGENCY_LOG_FILE, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
        fh.setFormatter(formatter)
        self.logger.addHandler(fh)
        
        # Console Handler (Stderr)
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(formatter)
        self.logger.addHandler(ch)

    async def start(self):
        """Start the async logging consumer."""
        if self.running: return
        self.running = True
        asyncio.create_task(self._process_queue())
        print("✅ EmergencyLogger Started.")

    async def stop(self):
        """Stop the logger."""
        self.running = False
        # Process remaining? 
        while not self.queue.empty():
            await asyncio.sleep(0.1)

    async def _process_queue(self):
        """
        Consumer loop.
        """
        while self.running:
            if not self.queue.empty():
                level, msg = self.queue.get()
                try:
                    # Generic async write to separate file to never block
                    async with aiofiles.open(EMERGENCY_LOG_FILE, mode='a', encoding='utf-8') as f:
                        timestamp = datetime.now().isoformat()
                        await f.write(f"{timestamp} [{level}] {msg}\n")
                except Exception as e:
                    # FALLBACK: Use Sync Logger (Blocking but safe)
                    self.logger.critical(f"Async Write Failed! Original Msg: {msg} | Error: {e}")
            else:
                await asyncio.sleep(0.1)

    def log(self, msg: str, level: str = "CRITICAL"):
        """
        Non-blocking log call. Puts message in queue.
        """
        self.queue.put((level, msg))
        
        # Also print to stderr immediately for visibility if CRITICAL
        if level == "CRITICAL":
            print(f"🔥 [CRITICAL] {msg}", file=sys.stderr)

    # Convenience methods
    def info(self, msg): self.log(msg, "INFO")
    def error(self, msg): self.log(msg, "ERROR")
    def critical(self, msg): self.log(msg, "CRITICAL")
