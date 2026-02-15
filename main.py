import time
import logging
import threading
import sys
import config
# Force UTF-8 for Console Output (Windows Fix)
# We use 'replace' to ensure that if a character cannot be encoded, it doesn't crash the bot.
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
from position_reconciliation import PositionReconciliation

# Logging Setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(config.LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ],
    force=True
)
logger = logging.getLogger(__name__)


def scalper_position_monitor(scalper_manager, trade_manager, fyers, bot, stop_event):
    """
    Phase 41.2: Background thread that polls LTP every 2s and manages
    the scalper position (breakeven, trailing, TP scale-out, stop).
    """
    while not stop_event.is_set():
        try:
            pos = scalper_manager.active_position
            if pos is None:
                time.sleep(2)
                continue

            # Fetch LTP
            resp = fyers.quotes(data={"symbols": pos.symbol})
            if 'd' not in resp or not resp['d']:
                time.sleep(2)
                continue

            # Phase 42: Broker position check before acting
            broker_pos = trade_manager._get_broker_position(pos.symbol)
            if broker_pos and broker_pos['net_qty'] >= 0:
                logger.warning(f"[SCALPER] Position flat/long for {pos.symbol} — skip exit")
                scalper_manager.close_position()
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
                # Full close — either stopped out or TP3 home run
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
    logger.info("--- [BOT] Starting ShortCircuit (Fyers Edition) ---")

    # 1. Authentication
    fyers_conn = FyersConnect()
    try:
        fyers = fyers_conn.authenticate()
    except Exception as e:
        logger.critical(f"Auth Failed: {e}")
        return

    # 2. Module Initialization
    scanner = FyersScanner(fyers)
    analyzer = FyersAnalyzer(fyers)
    trade_manager = TradeManager(fyers)
    bot = ShortCircuitBot(trade_manager)

    # Phase 41.1: Multi-Edge Detector + Performance Tracker (lazy init)
    multi_edge = None
    tracker = None
    if config.MULTI_EDGE_ENABLED:
        from multi_edge_detector import MultiEdgeDetector
        multi_edge = MultiEdgeDetector(config.ENABLED_DETECTORS)
        logger.info("[INIT] Multi-Edge Detection System ENABLED")
    if getattr(config, 'ENABLE_DETECTOR_TRACKING', False):
        from detector_performance_tracker import DetectorPerformanceTracker
        tracker = DetectorPerformanceTracker(
            getattr(config, 'DETECTOR_LOG_PATH', 'logs/detector_performance.csv')
        )
        logger.info("[INIT] Detector Performance Tracker ENABLED")

    # Phase 41.2: Scalper Position Manager (lazy init)
    scalper_manager = None
    scalper_stop_event = threading.Event()
    if getattr(config, 'USE_SCALPER_RISK_MANAGEMENT', False):
        from scalper_position_manager import ScalperPositionManager
        scalper_manager = ScalperPositionManager(trade_manager)
        logger.info("[INIT] ✓ Scalper Risk Management ENABLED (Phase 41.2)")

        # Start position monitoring thread
        scalper_thread = threading.Thread(
            target=scalper_position_monitor,
            args=(scalper_manager, trade_manager, fyers, bot, scalper_stop_event),
            daemon=True
        )
        scalper_thread.start()
        logger.info("[INIT] Scalper position monitor thread started")
    else:
        logger.info("[INIT] Legacy Risk Management (Phase 41.1)")

    # Phase 42: Position Reconciliation at startup
    reconciler = PositionReconciliation(fyers, trade_manager, bot)
    orphaned = reconciler.reconcile_positions()
    if orphaned:
        logger.critical(f"[INIT] ⚠️ Found {len(orphaned)} orphaned position(s) — check Telegram alerts")

    # 3. Start Telegram Thread
    t_bot = threading.Thread(target=bot.start_polling)
    t_bot.daemon = True
    t_bot.start()

    logger.info("[OK] System Initialized. Loop Starting...")
    
    # 3.1 Send Motivation
    try:
        bot.send_startup_message()
    except:
        pass

    # 4. Main Loop
    SCAN_INTERVAL = 60  # seconds
    import datetime
    last_reconciliation = datetime.datetime.now()
    while True:
        try:
            logger.info("[SCAN] Scanning Market...")
            start_time = time.time()

            # Phase 42: Periodic position reconciliation (every 30 min)
            recon_interval = getattr(config, 'POSITION_RECONCILIATION_INTERVAL', 1800)
            if (datetime.datetime.now() - last_reconciliation).total_seconds() > recon_interval:
                reconciler.reconcile_positions()
                last_reconciliation = datetime.datetime.now()
            
            # CHECK TIME FOR AUTO-EXIT
            # config.SQUARE_OFF_TIME format "HH:MM" e.g. "15:10"
            now = datetime.datetime.now()
            current_time = now.strftime("%H:%M")
            if current_time >= config.SQUARE_OFF_TIME:
                logger.warning(f"[TIME] Market Close Time ({current_time}). Initiating Square-off.")
                scalper_stop_event.set()  # Stop scalper monitor thread
                msg = trade_manager.close_all_positions()
                
                # Notify Telegram
                try:
                    bot.bot.send_message(bot.chat_id, f"[STOP] **MARKET CLOSED**\n\n{msg}\n\nSystem Shutdown. See you tomorrow!", parse_mode="Markdown")
                except:
                    pass
                    
                break # EXIT MAIN LOOP
            
            # A. Scan
            candidates = scanner.scan_market()
            
            if not candidates:
                logger.info("No volatility found. Sleeping...")
            
            # B. Analyze & Process
            for cand in candidates:
                symbol = cand['symbol']
                ltp = cand['ltp']
                oi = cand.get('oi', 0)
                
                signal = None
                history_df = cand.get('history_df')

                if config.MULTI_EDGE_ENABLED and multi_edge is not None:
                    # --- Phase 41.1 Path: Multi-Edge Detection ---
                    edge_candidate = {
                        'symbol': symbol,
                        'ltp': ltp,
                        'history_df': history_df,
                        'depth': None,      # Fetched inside analyzer if needed
                        'day_high': cand.get('day_high', 0),
                        'day_low': cand.get('day_low', 0),
                        'open': cand.get('open', 0),
                        'tick_size': cand.get('tick_size', 0.05),
                        'vwap': 0,          # Calculated by enrichment
                    }
                    edge_payload = multi_edge.scan_all_edges(edge_candidate)
                    if edge_payload:
                        signal = analyzer.check_setup_with_edges(
                            symbol, ltp, oi, history_df, edge_payload
                        )
                else:
                    # --- Phase 40 Path: Pattern-only (existing logic) ---
                    signal = analyzer.check_setup(symbol, ltp, oi, history_df)
                
                if signal:
                    # C. Validation Phase (Phase 37)
                    logger.info(f"[SIGNAL] {symbol} Candidate Found. Sending to Validation Gate.")
                    
                    # 1. Alert User (Pending)
                    if signal.get('edges_detected'):
                        bot.send_multi_edge_alert(signal)
                    else:
                        bot.send_validation_alert(signal) 
                    
                    # 2. Add to Gate (Starts Monitor Thread)
                    bot.focus_engine.add_pending_signal(signal)

                    # 3. Phase 41.1: Track detector performance
                    if tracker and signal.get('edges_detected'):
                        signal_id = f"{symbol}_{int(time.time())}"
                        signal['signal_id'] = signal_id  # Attach for later tracking
                        detector_names = [e['trigger'] for e in signal.get('edges_detected', [])]
                        tracker.log_signal_generated(signal_id, detector_names, symbol)

                    # 4. Phase 41.2: If scalper is enabled and trade executes,
                    #    start scalper position management.
                    #    Note: The actual trade execution happens inside
                    #    focus_engine.check_pending_signals() when validation fires.
                    #    The scalper manager monitors via its own thread.
                    if scalper_manager and signal.get('setup_high'):
                        # Store scalper-relevant fields on the signal for
                        # use by the validation gate's execution callback.
                        signal['_scalper_manager'] = scalper_manager

            elapsed = time.time() - start_time
            sleep_time = max(0, SCAN_INTERVAL - elapsed)
            logger.info(f"Cycle finished in {elapsed:.2f}s. Sleeping {sleep_time:.2f}s")
            time.sleep(sleep_time)

        except KeyboardInterrupt:
            logger.info("Manually Stopped.")
            scalper_stop_event.set()
            break
        except Exception as e:
            logger.error(f"Loop Error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()

