
import asyncio
import threading
import logging
import time
from concurrent.futures import Future

logger = logging.getLogger(__name__)

class AsyncExecutor:
    """
    Phase 42.2: Async-Sync Bridge & Component Orchestrator.
    
    Responsibilities:
    1. Manage dedicated background thread and asyncio loop.
    2. Initialize and hold references to Async Components:
       - FyersBrokerInterface (WebSocket)
       - DatabaseManager
       - OrderManager
       - ReconciliationEngine
       - EmergencyLogger
    3. Allow Sync Main Thread to 'run' coroutines on this loop.
    """
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(AsyncExecutor, cls).__new__(cls)
            cls._instance.loop = None
            cls._instance.thread = None
            cls._instance.running = False
            
            # Components
            cls._instance.broker = None
            cls._instance.db = None
            cls._instance.order_manager = None
            cls._instance.reconciliation = None
            cls._instance.emergency_logger = None
            
        return cls._instance
        
    def start(self, config: dict):
        """Starts a dedicated thread for the asyncio loop and initializes components."""
        if self.running:
            logger.warning("AsyncExecutor already running")
            return
            
        self.running = True
        self.ready_event = threading.Event()
        
        self.thread = threading.Thread(target=self._run_loop, args=(config,), daemon=True, name="AsyncExecutorThread")
        self.thread.start()
        
        logger.info("⏳ [ASYNC BRIDGE] Waiting for initialization...")
        # Wait for initialization to complete (or timeout)
        if not self.ready_event.wait(timeout=30):
             logger.critical("🔥 [ASYNC BRIDGE] Initialization Timed Out!")
             raise TimeoutError("Async Component Initialization Failed")
             
        logger.info("✅ [ASYNC BRIDGE] Event Loop & Components Ready.")
        
    def _run_loop(self, config: dict):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        
        # Initialize Components
        try:
             self.loop.run_until_complete(self._initialize_components(config))
             self.ready_event.set()
        except Exception as e:
             logger.critical(f"🔥 [ASYNC BRIDGE] Init Failed: {e}")
             # We should probably set ready event to unblock main thread, 
             # but main thread checks components? 
             # For now, let it timeout in main thread if this fails.
             return

        self.loop.run_forever()
        
    async def _initialize_components(self, config: dict):
        """
        Initialize all async components in strict order with Fail-Fast logic.
        """
        from database import DatabaseManager
        from fyers_broker_interface import FyersBrokerInterface
        from order_manager import OrderManager
        from reconciliation import ReconciliationEngine
        from emergency_logger import EmergencyLogger
        from capital_manager import CapitalManager
        
        try:
            # 1. Emergency Logger (First Priority)
            logging_cfg = config.get('logging', {})
            self.emergency_logger = EmergencyLogger(logging_cfg)
            if hasattr(self.emergency_logger, 'start') and asyncio.iscoroutinefunction(self.emergency_logger.start):
                await self.emergency_logger.start()

            # 2. Database (Crucial Infrastructure)
            self.db = DatabaseManager()
            await self.db.initialize()

            # 3. Broker Interface (WebSocket Connectivity)
            fyers_cfg = config.get('fyers', {})
            # access_token already in config - passed from main.py
            self.broker = FyersBrokerInterface(
                access_token=fyers_cfg.get('access_token'),
                client_id=fyers_cfg.get('client_id'),
                db_manager=self.db,
                emergency_logger=self.emergency_logger
            )
            await self.broker.initialize()
            
            # 4. Capital Manager
            if 'capital_manager_instance' in config:
                self.capital_manager = config['capital_manager_instance']
                # logger.info(f"💰 Capital Manager injected from main") 
            else:
                risk_cfg = config.get('risk', {})
                self.capital_manager = CapitalManager(
                    base_capital=risk_cfg.get('base_capital', 1800.0), # float
                    leverage=risk_cfg.get('leverage', 5.0)             # float
                )
            
            # 5. Alert System (Injected)
            self.telegram = config.get('telegram_bot_instance')

            # 6. Order Manager
            self.order_manager = OrderManager(
                broker=self.broker,
                telegram_bot=self.telegram,
                db=self.db,
                capital_manager=self.capital_manager
            )
            
            # 7. Reconciliation Engine
            self.reconciliation = ReconciliationEngine(
                broker=self.broker,
                db_manager=self.db,
                telegram_bot=self.telegram
            )

            asyncio.create_task(self.reconciliation.start())
            
            logger.info("✅ [ASYNC INIT] All Components Initialized & Ready.")

        except Exception as e:
            # FAIL FAST
            msg = f"Critical Initialization Failure: {e}"
            logger.critical(msg)
            if self.emergency_logger:
                 self.emergency_logger.critical(msg)
            raise e # Bubble up to main thread timeout or error handler

    def run(self, coro):
        """Run blocking."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()
        
    def run_bg(self, coro):
        """Run fire-and-forget."""
        asyncio.run_coroutine_threadsafe(coro, self.loop)
        
    def get_loop(self):
        return self.loop

class SyncWrapper:
    """
    Wraps an async object and executes its methods synchronously 
    via the AsyncExecutor.
    """
    def __init__(self, target, executor: AsyncExecutor):
        self._target = target
        self._executor = executor
        
    def __getattr__(self, name):
        attr = getattr(self._target, name)
        if callable(attr):
             # check if it's a method
             if asyncio.iscoroutinefunction(attr) or asyncio.iscoroutine(attr):
                def wrapped(*args, **kwargs):
                    return self._executor.run(attr(*args, **kwargs))
                return wrapped
             return attr
        return attr
