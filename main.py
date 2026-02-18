import time
import logging
import threading
import sys
import os
import config
# Force UTF-8 for Console Output (Windows Fix)
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception as e:
        print(f"Warning: Could not force UTF-8: {e}")

from fyers_connect import FyersConnect
from scanner import FyersScanner
from analyzer import FyersAnalyzer
from trade_manager import TradeManager
from telegram_bot import ShortCircuitBot

# Logging Setup
import logging.handlers
log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

file_handler = logging.handlers.RotatingFileHandler(
    config.LOG_FILE, 
    maxBytes=10*1024*1024, 
    backupCount=5, 
    encoding='utf-8'
)
file_handler.setFormatter(log_formatter)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(log_formatter)

logging.basicConfig(
    level=logging.INFO,
    handlers=[file_handler, console_handler],
    force=True
)
logger = logging.getLogger(__name__)

def scalper_position_monitor(scalper_manager, trade_manager, fyers, bot, stop_event):
    """
    Phase 41.2: Background thread that polls LTP every 2s.
    """
    while not stop_event.is_set():
        try:
            pos = scalper_manager.active_position
            if pos is None:
                time.sleep(2)
                continue

            # Fetch LTP
            # We should ideally use Broker Interface here too, but for legacy compat keep direct fyers
            # or update ScalperManager to use Broker later.
            resp = fyers.quotes(data={"symbols": pos.symbol})
            if 'd' not in resp or not resp['d']:
                time.sleep(2)
                continue

            current_ltp = resp['d'][0]['v']['lp']
            action = scalper_manager.update_position(current_ltp)

            if action['action'] == 'UPDATE_STOP':
                trade_manager.update_stop_loss(pos.symbol, action['new_stop'])

            elif action['action'] == 'CLOSE_PARTIAL':
                trade_manager.close_partial_position(
                    pos.symbol, action['quantity'], action['reason']
                )
                try:
                    msg = (f"[SCALPER] {action['reason']}: Closed {action['quantity']} "
                           f"shares @ ₹{action['price']:.2f}")
                    bot.bot.send_message(bot.chat_id, msg)
                except Exception:
                    pass

            elif action['action'] in ('STOP_HIT', 'CLOSE_ALL'):
                trade_manager.close_partial_position(
                    pos.symbol, action['quantity'], action['reason']
                )
                try:
                    emoji = "🏠" if action['reason'] == 'TP3_HOME_RUN' else "🛑"
                    msg = (f"{emoji} [SCALPER] {action['reason']}: "
                           f"Closed ALL @ ₹{action['price']:.2f}")
                    bot.bot.send_message(bot.chat_id, msg)
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"Scalper monitor error: {e}")

        time.sleep(2)


def main():
    logger.info("--- [BOT] Starting ShortCircuit (WebSocket Phase 42.2) ---")

    # ===================================================================
    # STEP 1: FYERS CONNECTION - HAPPENS EXACTLY ONCE
    # ===================================================================
    logger.info("🔐 Authenticating with Fyers...")
    # This is the ONLY call to FyersConnect() in the entire codebase.
    # The singleton pattern ensures subsequent imports return this same instance.
    fyers_conn = FyersConnect(config)
    fyers_client = fyers_conn.fyers          # fyersModel.FyersModel instance
    access_token = fyers_conn.access_token   # Raw token string
    
    if not fyers_client or not access_token:
         logger.critical("❌ Fyers Authentication Failed. Exiting.")
         return

    logger.info("✅ Fyers Connected Successfully")

    # ===================================================================
    # STEP 2: WARN-UP & RECOVERY
    # ===================================================================
    from capital_manager import CapitalManager
    # CapitalManager uses fixed capital from config, not Fyers funds
    capital_manager = CapitalManager(
        base_capital=getattr(config, 'CAPITAL_PER_TRADE', 1800.0),
        leverage=getattr(config, 'INTRADAY_LEVERAGE', 5.0)
    )

    from startup_recovery import StartupRecovery
    # Pass object, don't re-authenticate
    recovery = StartupRecovery(fyers_client)  
    recovery.scan_orphaned_trades()

    logger.info("daily_bias: NEUTRAL (No bias calculated yet)")

    # 2. Telegram Bot (Sync)
    # TradeManager (Legacy wrapper) need fyers, AND capital_manager
    trade_manager = TradeManager(fyers_client, capital_manager)
    bot = ShortCircuitBot(trade_manager)

    # 3. Async Bridge & Infrastructure (WebSocket)
    from async_utils import AsyncExecutor, SyncWrapper

    logger.info("🌉 Initializing Async Bridge...")
    
    # Adapter for Async OrderManager to call Sync Bot
    class AsyncBotAdapter:
        def __init__(self, bot):
            self.bot = bot
        async def send_alert(self, msg):
            # We use the AsyncExecutor loop which is running this to offload to thread?
            # No, OrderManager runs in AsyncExecutor loop. 
            # We need to run sync code in executor.
            import asyncio
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: self.bot.send_alert(msg))
        @property
        def journal(self): return self.bot.journal # Journal access used by OrderManager?

        def is_auto_mode(self):
            """Pass-through to real bot's auto_mode check."""
            return self.bot.is_auto_mode()
        
    # Construct Config for AsyncExecutor
    # We populate it with values from config.py and env vars
    async_config = {
        'database': {}, # Uses env vars internally in DatabaseManager
        'fyers': {
            'client_id': os.getenv('FYERS_CLIENT_ID'), # fyers_connect uses FYERS_APP_ID
            'access_token': access_token, # Pass the token we just generated/loaded
        },
        'risk': {
            'base_capital': 1800.0,    # ← NUMBER not object
            'leverage': 5.0,           # ← NUMBER not object
            'max_positions': 2,
            'max_daily_loss': 500.0
        },
        'logging': {
            'level': 'INFO',
            'path': 'logs/'
        },
        'telegram_bot_instance': AsyncBotAdapter(bot), # Pass the WRAPPED adapter
        'capital_manager_instance': capital_manager   # Inject existing instance (Fix Double Init)
    }

    async_exec = AsyncExecutor()
    try:
        async_exec.start(async_config)
    except Exception as e:
        logger.critical(f"Async Bridge Start Failed: {e}")
        return

    # Retrieve Initialized Components
    raw_order_manager = async_exec.order_manager
    db_manager = async_exec.db
    reconciler = async_exec.reconciliation
    
    # Create Sync Wrappers
    order_manager = SyncWrapper(raw_order_manager, async_exec)

    # 4. Run Startup Recovery
    # Updated to accept fyers_client as per Phase 42.2.2 refactor
    # recovery = StartupRecovery(fyers_client) <--- REMOVED DUPLICATE
    # Note: scan_orphaned_trades is sync now, but if we need async execution we can wrap it
    # For now, running valid sync method
    recovery.scan_orphaned_trades()
    
    # ──────────────────────────────────────────────────────

    # 5. Dependency Injection for Sync Modules
    
    # Inject Async Order Manager (Wrapper) into TradeManager? 
    # TradeManager logic manages its own state currently (legacy).
    # Ideally, we should update TradeManager to use order_manager for execution.
    # For Phase 42.2, we focus on preserving layout.
    
    # FIX: Update Raw Order Manager's Telegram Adapter if not passed in config
    # We passed 'telegram_bot_instance' in config, so AsyncExecutor should have handled it.
    
    # ── PHASE 41.3.1: MARKET SESSION AWARENESS ──
    from market_session import MarketSession
    market_session = MarketSession(fyers_client, bot)
    
    logger.info("🕐 Analyzing market session state...")
    morning_context = market_session.initialize_session()
    
    mh = morning_context['high'] if morning_context else None
    ml = morning_context['low'] if morning_context else None
    
    # Init components that depend on context
    scanner = FyersScanner(fyers_client)
    analyzer = FyersAnalyzer(fyers_client, morning_high=mh, morning_low=ml)
    
    # ── PHASE 41.3: INTELLIGENT EXITS ──
    if getattr(config, 'ENABLE_DISCRETIONARY_EXITS', True):
        from discretionary_engine import DiscretionaryEngine
        discretionary_engine = DiscretionaryEngine(fyers_client, order_manager)
        
        bot.focus_engine.order_manager = order_manager
        bot.focus_engine.discretionary_engine = discretionary_engine
        logger.info("[INIT] ✓ OrderManager & DiscretionaryEngine ENABLED")

    # Phase 41.1: Multi-Edge Detector
    multi_edge = None
    tracker = None
    if config.MULTI_EDGE_ENABLED:
        from multi_edge_detector import MultiEdgeDetector
        multi_edge = MultiEdgeDetector(config.ENABLED_DETECTORS)
    if getattr(config, 'ENABLE_DETECTOR_TRACKING', False):
        from detector_performance_tracker import DetectorPerformanceTracker
        tracker = DetectorPerformanceTracker(getattr(config, 'DETECTOR_LOG_PATH', 'logs/detector_performance.csv'))

    # Phase 41.2: Scalper Position Manager
    scalper_manager = None
    scalper_stop_event = threading.Event()
    if getattr(config, 'USE_SCALPER_RISK_MANAGEMENT', False):
        from scalper_position_manager import ScalperPositionManager
        scalper_manager = ScalperPositionManager(trade_manager)
        
        scalper_thread = threading.Thread(
            target=scalper_position_monitor,
            args=(scalper_manager, trade_manager, fyers, bot, scalper_stop_event),
            daemon=True
        )
        scalper_thread.start()
        logger.info("[INIT] ✓ Scalper Risk Management ENABLED")

    # 6. Start Telegram Thread
    t_bot = threading.Thread(target=bot.start_polling)
    t_bot.daemon = True
    t_bot.start()

    logger.info("[OK] System Initialized. Loop Starting...")
    
    try:
        if market_session.get_current_state() in ['EARLY_MARKET', 'MID_MARKET']:
            bot.send_startup_message()
    except:
        pass

    # 7. Main Loop
    SCAN_INTERVAL = 60
    import datetime
    last_reconciliation = datetime.datetime.now()
    
    while True:
        try:
            if not market_session.should_trade_now():
                if market_session.get_current_state() == 'POST_MARKET':
                    logger.info("🌙 Market Closed. Stopping Loop.")
                    break
                time.sleep(60)
                continue

            if not config.TRADING_ENABLED:
                logger.info("⏸️ Trading Disabled (Warmup).")
                time.sleep(30)
                continue

            logger.info("[SCAN] Scanning Market...")
            start_time = time.time()

            # Periodic Reconciliation
            # Note: Async ReconciliationEngine runs continuously in background now!
            # We might keep this legacy one if it does different checks, but 
            # for now let's rely on the background one.
            # But the logic below called `reconciler.reconcile_positions()` which was synchronous legacy.
            # We have replaced `reconciler` variable with the NEW Async Engine instance.
            # The new instance has `reconcile()` which is async.
            # Calling `reconciler.reconcile()` here would crash if called synchronously on async object.
            # Plus, it's running in background loop.
            # So REMOVE explicit call here.
            
            # CHECK TIME FOR AUTO-EXIT
            now = datetime.datetime.now()
            current_time = now.strftime("%H:%M")
            if current_time >= config.SQUARE_OFF_TIME:
                logger.warning(f"[TIME] Market Close ({current_time}). Square-off.")
                scalper_stop_event.set()
                msg = trade_manager.close_all_positions()
                try:
                    bot.bot.send_message(bot.chat_id, f"[STOP] **MARKET CLOSED**\n\n{msg}")
                except:
                    pass
                break
            
            # A. Scan
            candidates = scanner.scan_market()
            
            if not candidates:
                logger.info("No candidates. Sleeping...")
            
            # B. Analyze
            for cand in candidates:
                symbol = cand['symbol']
                signal = None
                
                if config.MULTI_EDGE_ENABLED and multi_edge:
                    edge_payload = multi_edge.scan_all_edges({
                        'symbol': symbol, 'ltp': cand['ltp'], 'history_df': cand.get('history_df'),
                        'day_high': cand.get('day_high'), 'day_low': cand.get('day_low'),
                        'open': cand.get('open'), 'tick_size': 0.05
                    })
                    if edge_payload:
                        signal = analyzer.check_setup_with_edges(symbol, cand['ltp'], cand.get('oi',0), cand.get('history_df'), edge_payload)
                else:
                    signal = analyzer.check_setup(symbol, cand['ltp'], cand.get('oi',0), cand.get('history_df'))
                
                if signal:
                    logger.info(f"[SIGNAL] {symbol} Found.")
                    
                    if signal.get('edges_detected'):
                        bot.send_multi_edge_alert(signal)
                    else:
                        bot.send_validation_alert(signal)
                    
                    bot.focus_engine.add_pending_signal(signal)

                    if tracker and signal.get('edges_detected'):
                        signal_id = f"{symbol}_{int(time.time())}"
                        signal['signal_id'] = signal_id
                        tracker.log_signal_generated(signal_id, [e['trigger'] for e in signal.get('edges_detected', [])], symbol)

                    if scalper_manager and signal.get('setup_high'):
                        signal['_scalper_manager'] = scalper_manager

            elapsed = time.time() - start_time
            sleep_time = max(0, SCAN_INTERVAL - elapsed)
            time.sleep(sleep_time)

        except KeyboardInterrupt:
            logger.info("Manually Stopped.")
            scalper_stop_event.set()
            break
        except Exception as e:
            logger.error(f"Loop Error: {e}")
            time.sleep(10)

    # EOD Analysis
    logger.info("🏁 Session Ended. EOD Analysis...")
    try:
        from eod_analyzer import EODAnalyzer
        if hasattr(bot, 'journal') and hasattr(bot.journal, 'db'):
             analyzer = EODAnalyzer(fyers, bot.journal.db)
             report = analyzer.run_daily_analysis()
             if report and getattr(config, 'EOD_CONFIG', {}).get('auto_send_telegram', True):
                 bot.bot.send_message(bot.chat_id, report, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"❌ EOD Analysis Failed: {e}")

if __name__ == "__main__":
    main()
