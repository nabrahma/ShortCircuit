# -*- coding: utf-8 -*-
import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import time
from typing import Any, Callable, Optional

import config
import pytz

from analyzer import FyersAnalyzer
from capital_manager import CapitalManager
from database import DatabaseManager
from eod_analyzer import EODAnalyzer
from eod_scheduler import eod_scheduler
from eod_watchdog import eod_watchdog
from focus_engine import FocusEngine
from fyers_broker_interface import FyersBrokerInterface
from fyers_connect import FyersConnect
from market_session import MarketSession
from market_utils import is_market_hours
from reconciliation import ReconciliationEngine
from scanner import FyersScanner
from startup_recovery import StartupRecovery
from telegram_bot import ShortCircuitBot
from trade_manager import TradeManager
from dashboard_bridge import get_dashboard_bridge
from tools.dashboard_logger import DashboardLoggerHandler

IST = pytz.timezone("Asia/Kolkata")
logger = logging.getLogger("shortcircuit.supervisor")
_supervised_sleep = asyncio.sleep


def _configure_logging() -> None:
    if sys.platform.startswith("win"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    log_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    
    # Ensure log directory exists
    log_dir = os.path.dirname(config.LOG_FILE)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        
    file_handler = logging.handlers.RotatingFileHandler(
        config.LOG_FILE,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(log_formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(log_formatter)

    logging.basicConfig(
        level=logging.INFO,
        handlers=[file_handler, console_handler],
        force=True,
    )
    
    # Phase 75: High-frequency Dashboard Log Stream
    try:
        dash_handler = DashboardLoggerHandler()
        logging.getLogger().addHandler(dash_handler)
    except Exception:
        pass


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, shutdown_event: asyncio.Event):
    def _handler(signum: Optional[int] = None, frame: Optional[Any] = None):
        logger.warning("[SUPERVISOR] Shutdown signal received: %s", signum)
        shutdown_event.set()

    # Unix path
    try:
        loop.add_signal_handler(signal.SIGINT, _handler)
        loop.add_signal_handler(signal.SIGTERM, _handler)
        return
    except (NotImplementedError, RuntimeError):
        pass

    # Windows fallback
    signal.signal(signal.SIGINT, _handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handler)


async def _supervised(
    name: str,
    coro_factory: Callable[[], asyncio.Future],
    shutdown_event: asyncio.Event,
    max_retries: int = 3,
    retry_window_secs: float = 60.0,
    on_before_restart: Optional[Callable[[], asyncio.Future]] = None,
):
    """
    Restart-on-crash wrapper with crash-loop cutoff.
    """
    crash_times = deque(maxlen=max_retries)
    while not shutdown_event.is_set():
        try:
            await coro_factory()
            return  # clean exit must not restart
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            now = time.monotonic()
            crash_times.append(now)
            if (
                len(crash_times) == max_retries
                and (crash_times[-1] - crash_times[0]) < retry_window_secs
            ):
                logger.critical(
                    "[SUPERVISOR] %s crash-looping (%s failures in %ss). Aborting.",
                    name,
                    max_retries,
                    retry_window_secs,
                )
                raise

            if name == "main_trading_loop" and is_market_hours():
                logger.warning(
                    "[SUPERVISOR] Trading loop crashed during market hours; "
                    "running recovery scan before restart."
                )
                if on_before_restart is not None:
                    try:
                        await on_before_restart()
                    except Exception as restart_exc:
                        logger.error(
                            "[SUPERVISOR] Recovery scan before restart failed: %s",
                            restart_exc,
                        )
                await _supervised_sleep(5)

            logger.error("[SUPERVISOR] %s crashed: %r. Restarting...", name, exc)
            await _supervised_sleep(2)


@dataclass
class RuntimeContext:
    fyers_client: Any
    access_token: str
    capital_manager: CapitalManager
    trade_manager: TradeManager
    focus_engine: FocusEngine
    bot: ShortCircuitBot
    market_session: MarketSession
    scanner: FyersScanner
    analyzer: FyersAnalyzer
    db_manager: DatabaseManager
    broker: FyersBrokerInterface
    order_manager: Any  # OrderManager — constructed after broker
    reconciliation_engine: ReconciliationEngine
    startup_recovery: StartupRecovery


async def _initialize_runtime() -> RuntimeContext:
    logger.info("[INIT] Authenticating with Fyers...")
    fyers_conn = FyersConnect(config)
    fyers_client = fyers_conn.fyers
    access_token = fyers_conn.access_token
    if not fyers_client or not access_token:
        raise RuntimeError("Fyers authentication failed.")

    capital_manager = CapitalManager(
        leverage=getattr(config, "INTRADAY_LEVERAGE", 5.0),
    )

    trade_manager = TradeManager(fyers_client, capital_manager)
    focus_engine = FocusEngine(trade_manager)

    config_settings = {k: v for k, v in vars(config).items() if not k.startswith("__")}
    bot = ShortCircuitBot(config_settings, None, capital_manager, focus_engine)
    trade_manager.bot = bot
    focus_engine.telegram_bot = bot

    market_session = MarketSession(fyers_client, bot)
    logger.info("[INIT] Evaluating market session...")
    morning_context = await market_session.initialize_session()
    mh = morning_context["high"] if morning_context else None
    ml = morning_context["low"] if morning_context else None

    db_manager = DatabaseManager()
    await db_manager.initialize()

    # PRD-008: Enable periodic gate result flush — set DSN once after DB pool is ready
    from gate_result_logger import get_gate_result_logger
    from database import DB_CONFIG as _db_cfg
    import os as _os
    _db_dsn = (
        f"postgresql://{_os.getenv('DB_USER', _db_cfg['user'])}"
        f":{_os.getenv('DB_PASS', _os.getenv('DB_PASSWORD', _db_cfg['password']))}"
        f"@{_os.getenv('DB_HOST', _db_cfg['host'])}"
        f":{_db_cfg['port']}/{_db_cfg['database']}"
    )
    get_gate_result_logger().set_dsn(_db_dsn)

    broker = FyersBrokerInterface(
        access_token=access_token,
        client_id=os.getenv("FYERS_CLIENT_ID"),
        db_manager=db_manager,
        emergency_logger=None,
    )
    await broker.initialize()
    # PRD-3: Wire Telegram bot to broker for WS cache alerts
    broker.set_telegram(bot)

    # ── Phase 89.9: Dynamic 5% Target Initialization ──────────────────
    # Perform initial sync to capture morning ledger balance
    await capital_manager.sync(broker)
    
    analyzer = FyersAnalyzer(fyers_client, broker=broker, morning_high=mh, morning_low=ml)
    
    # Calculate 5% mission target automatically
    if getattr(config, 'DAILY_TARGET_INR', 0) == -1:
        initial_bal = capital_manager.initial_margin
        target_inr = initial_bal * 0.05
        analyzer.signal_manager.daily_target_inr = target_inr
        logger.info(f"🎯 [DYNAMIC TARGET] Morning Ledger: ₹{initial_bal:.2f} | 5% Goal: ₹{target_inr:.2f}")
    
    bot.signal_manager = analyzer.signal_manager
    bot.market_session = market_session


    # ── PRD-007: Phase 44.7 + Startup Gate ───────────────────────────
    # 1. Create scanner with broker reference
    scanner = FyersScanner(fyers_client, broker=broker)

    # 2. Fetch NSE symbol universe synchronously (blocks executor, not event loop)
    scanner_symbols = await asyncio.get_event_loop().run_in_executor(
        None, scanner._fetch_nse_symbols_sync
    )
    if scanner_symbols:
        # 3. Seed WS cache with REST snapshot to prevent false-missing inflation on late starts
        seeded = await asyncio.to_thread(broker.seed_from_rest, scanner_symbols)
        logger.info(
            "[STARTUP] WS cache REST seed: %s/%s symbols",
            seeded,
            len(scanner_symbols),
        )
        if seeded < int(len(scanner_symbols) * 0.90):
            logger.warning(
                "[STARTUP] REST seed coverage low: %s/%s symbols. Quotes API may be throttled.",
                seeded,
                len(scanner_symbols),
            )

        # 3. Subscribe to WS — sets state=PRIMING, starts health monitor thread
        broker.subscribe_scanner_universe(scanner_symbols)
        logger.info(f"[Phase 44.7] WS subscription dispatched for {len(scanner_symbols)} symbols")
    else:
        logger.warning("[Phase 44.7] Failed to load NSE symbols for WS cache priming")

    # 4. Startup Gate: block until READY (ticks arrive) or timeout
    CACHE_READY_TIMEOUT = getattr(config, 'WS_CACHE_READY_TIMEOUT_SEC', 45)
    logger.info(f"[STARTUP GATE] Waiting up to {CACHE_READY_TIMEOUT}s for WS cache readiness...")
    cache_ready = await asyncio.to_thread(broker.wait_for_cache_ready, float(CACHE_READY_TIMEOUT))

    if cache_ready:
        snap = broker.cache_health_snapshot()
        logger.info(
            f"[STARTUP GATE] Cache READY: {snap['fresh']}/{snap['total']} symbols fresh. Proceeding to scan."
        )
    else:
        # BUG-02 sub-fix 2c: attempt one full reconnect before accepting REST fallback
        snap = broker.cache_health_snapshot()
        logger.critical(
            f"[STARTUP GATE] Cache NOT ready after {CACHE_READY_TIMEOUT}s. "
            f"Fresh: {snap['fresh']}/{snap['total']}. Attempting full WS reconnect..."
        )
        try:
            await asyncio.to_thread(broker._do_full_ws_reconnect)
            logger.info("[STARTUP GATE] Full reconnect done — waiting for cache (attempt 2)...")
            cache_ready = await asyncio.to_thread(broker.wait_for_cache_ready, float(CACHE_READY_TIMEOUT))
        except Exception as reconnect_err:
            logger.error(f"[STARTUP GATE] Reconnect failed: {reconnect_err}")
            cache_ready = False

        if cache_ready:
            snap = broker.cache_health_snapshot()
            logger.info(
                f"[STARTUP GATE] ✅ Cache READY after reconnect: {snap['fresh']}/{snap['total']} symbols fresh."
            )
        else:
            snap = broker.cache_health_snapshot()
            logger.critical(
                f"[STARTUP GATE] Cache STILL not ready after reconnect. "
                f"Fresh: {snap['fresh']}/{snap['total']}. "
                f"Proceeding with REST fallback. INVESTIGATE WS CONNECTION."
            )
        try:
            await bot.send_alert(
                f"🚨 WS CACHE STARTUP FAILURE\n"
                f"Cache not ready after {CACHE_READY_TIMEOUT}s\n"
                f"Fresh: {snap['fresh']}/{snap['total']}\n"
                f"Scanning on REST — signals degraded."
            )
        except Exception as _e:
            logger.warning(f"[STARTUP GATE] Could not send Telegram alert: {_e}")

    # ── P0 FIX: Construct OrderManager with live broker ──────────────
    from order_manager import OrderManager
    order_manager = OrderManager(
        broker=broker,
        telegram_bot=bot,
        db=db_manager,
        capital_manager=capital_manager,
        trade_manager=trade_manager,
    )

    # Inject into FocusEngine (was None → caused NSESGL-EQ execution miss)
    focus_engine.order_manager = order_manager
    # PRD-008 Bug 2 fix: inject analyzer so focus_engine can call record_signal() at order placement
    focus_engine.analyzer = analyzer

    # Phase 52: Wire event loop for sync thread async dispatch
    focus_engine._event_loop = asyncio.get_event_loop()
    logger.info(f"[FOCUS] Event loop set: {focus_engine._event_loop is not None}")
    logger.info("[INIT] ✅ OrderManager constructed and injected into FocusEngine.")

    # Also wire into bot for /positions, /pnl, order alerts
    bot.order_manager = order_manager
    # ────────────────────────────────────────────────────────────────────

    # ────────────────────────────────────────────────────────────────────
    # Reconciliation Engine construction
    # ────────────────────────────────────────────────────────────────────
    reconciliation_engine = ReconciliationEngine(
        broker=broker,
        db_manager=db_manager,
        telegram_bot=bot,
        capital_manager=capital_manager,
        order_manager=order_manager,
    )

    await order_manager.startup_reconciliation()

    # Phase 44.6: Startup Recovery (now adopts orphans)
    startup_recovery = StartupRecovery(
        fyers_client=fyers_client,
        order_manager=order_manager,
        capital_manager=capital_manager,
        telegram=bot,
    )
    await startup_recovery.scan_orphaned_trades()

    return RuntimeContext(
        fyers_client=fyers_client,
        access_token=access_token,
        capital_manager=capital_manager,
        trade_manager=trade_manager,
        focus_engine=focus_engine,
        bot=bot,
        market_session=market_session,
        scanner=scanner,
        analyzer=analyzer,
        db_manager=db_manager,
        broker=broker,
        order_manager=order_manager,
        reconciliation_engine=reconciliation_engine,
        startup_recovery=startup_recovery,
    )


async def _trading_loop(shutdown_event: asyncio.Event, ctx: RuntimeContext):
    import config
    if getattr(ctx.bot, '_auto_on_queued', False):
        ctx.bot._auto_mode = True
        ctx.bot._auto_on_queued = False
        logger.info("[AUTO] Queued Auto ON activated — market ready")
        # FIX #5: Flush stale pending signals from pre-market
        if hasattr(ctx, 'focus_engine') and ctx.focus_engine:
            ctx.focus_engine.flush_stale_pending_signals(max_age_minutes=20)
            logger.info("[SESSION] Stale pending signals flushed at session open")
        await ctx.bot.send_message(
            "✅ *Auto Mode activated* — market is open, scanning live",
            parse_mode="Markdown",
        )

    await ctx.bot._send_morning_briefing(
        ws_cache=ctx.broker,
        market_ctx=ctx.analyzer.market_context if ctx.analyzer else None,
        startup_validation_passed=True,
    )

    logger.info("[TRADING] Trading loop started.")
    scan_interval = 60
    consecutive_errors = 0
    max_errors = 5
    loop_count = 0

    while not shutdown_event.is_set():
        try:
            if hasattr(ctx.capital_manager, "_last_sync") and ctx.capital_manager._last_sync:
                from datetime import datetime, timezone, UTC
                now_utc = datetime.now(UTC)
                last_sync = ctx.capital_manager._last_sync
                # Ensure both are aware (handle legacy naive timestamps)
                if last_sync.tzinfo is None:
                    last_sync = last_sync.replace(tzinfo=UTC)
                if (now_utc - last_sync).total_seconds() > 300:
                    try:
                        await asyncio.wait_for(ctx.capital_manager.sync(ctx.broker), timeout=30.0)
                    except asyncio.TimeoutError:
                        logger.error("[RESILIENCE] Capital sync timed out after 30s. Skipping.")
                    except Exception as e:
                        logger.error(f"[RESILIENCE] Capital sync failed: {e}")
            if not ctx.market_session.should_trade_now():
                current_state = ctx.market_session.get_current_state()
                if current_state in ("POST_MARKET", "EOD_WINDOW"):
                    logger.info("[TRADING] Session state %s, exiting trading loop.", current_state)
                    return
                await asyncio.sleep(60)
                continue

            if not config.TRADING_ENABLED:
                # Phase 89: Log once, not every 30s during warmup
                if not getattr(ctx, '_trading_disabled_logged', False):
                    logger.info("[TRADING] Trading disabled (warmup); waiting for 09:30 transition.")
                    ctx._trading_disabled_logged = True
                await asyncio.sleep(5)  # Check every 5s so we catch 9:30 transition fast
                continue
            else:
                ctx._trading_disabled_logged = False  # Reset for future pauses

            start_ts = time.monotonic()
            loop_count += 1
            logger.info(f"[HEARTBEAT] Loop #{loop_count} active | Scanning market...")
            try:
                candidates = await asyncio.wait_for(
                    asyncio.to_thread(ctx.scanner.scan_market),
                    timeout=45.0
                )
            except asyncio.TimeoutError:
                logger.error("[RESILIENCE] Scanning timed out after 45s. Skipping iteration.")
                candidates = []
            except Exception as e:
                logger.error(f"[RESILIENCE] Scanning error: {e}")
                candidates = []

            # PRD-008: Pull scan_id and data_tier from scanner for gate audit correlation
            _scan_id   = getattr(ctx.scanner, '_scan_counter', 0)
            _data_tier = getattr(ctx.scanner, '_last_data_tier', 'UNKNOWN')

            ctx.bot._scan_metadata = {
                "last_scan_time": datetime.now(),
                "candidate_count": len(candidates) if candidates else 0,
            }
            
            # Phase 72: Jarvis Heartbeat
            from dashboard_bridge import get_dashboard_bridge
            get_dashboard_bridge().broadcast("HEARTBEAT", {
                "pnl": ctx.trade_manager.daily_pnl if hasattr(ctx.trade_manager, 'daily_pnl') else 0.0,
                "status": "SCANNING"
            })

            # Phase 89.6: Parallelized Analysis
            async def run_analysis(cand):
                import config  # Phase 91: Thread-safe local import
                signal = await asyncio.to_thread(
                    ctx.analyzer.check_setup,
                    cand["symbol"],
                    cand["ltp"],
                    cand.get("oi", 0),
                    cand.get("history_df"),
                    cand.get("history_df_15m"),
                    _scan_id,
                    _data_tier,
                )
                return signal

            analysis_tasks = [run_analysis(cand) for cand in (candidates or [])]
            analysis_results = await asyncio.gather(*analysis_tasks) if analysis_tasks else []

            for signal in (res for res in analysis_results if res):
                if shutdown_event.is_set():
                    return
                symbol = signal["symbol"]
                if signal.get("cooldown_blocked"): # Added back
                    try:
                        unlock_at = (
                            ctx.analyzer.signal_manager.last_signal_time[symbol]
                            + timedelta(minutes=ctx.analyzer.signal_manager.cooldown_minutes)
                        )
                        ctx.bot.focus_engine.queue_cooldown_signal(signal, unlock_at)
                        logger.info(
                            "PENDING %s queued for cooldown unlock at %s",
                            symbol,
                            unlock_at.strftime("%H:%M"),
                        )
                    except Exception as exc:
                        logger.error("Failed to queue cooldown signal: %s", exc)
                    continue

                pending_signal = signal
                editable_enabled = False
                try:
                    editable_enabled = ctx.bot.is_editable_signal_flow_enabled()
                except Exception:
                    editable_enabled = False

                if editable_enabled and hasattr(ctx.bot, "queue_signal_discovery"):
                    pending_signal = dict(signal)
                    correlation_id = await ctx.bot.queue_signal_discovery(pending_signal)
                    pending_signal["correlation_id"] = correlation_id
                else:
                    if signal.get("edges_detected"):
                        ctx.bot.send_multi_edge_alert(signal)
                    else:
                        ctx.bot.send_validation_alert(signal)

                ctx.bot.focus_engine.add_pending_signal(pending_signal)

            elapsed = time.monotonic() - start_ts
            await asyncio.sleep(max(0, scan_interval - elapsed))
            consecutive_errors = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            consecutive_errors += 1
            logger.exception(
                "[TRADING] Loop error: %s (attempt %s/%s)",
                exc,
                consecutive_errors,
                max_errors,
            )
            if consecutive_errors >= max_errors:
                raise RuntimeError("Trading loop circuit breaker tripped.") from exc
            await asyncio.sleep(10)


def _update_terminal_log() -> None:
    try:
        logger.info("[CLEANUP] Updating terminal log.")
        subprocess.run([sys.executable, "dump_terminal_log.py"], check=False)
        
        # Phase 70: Auto-generate the noise-filtered markdown report
        import datetime as _dt
        import os
        today_str = _dt.date.today().strftime('%Y-%m-%d')
        log_path = f"logs/{today_str}_session.log"
        if os.path.exists(log_path):
            logger.info("[CLEANUP] Generating human-readable Markdown Session Report...")
            subprocess.run([sys.executable, "tools/analyze_session_log.py", log_path], check=False)
            logger.info(f"✅ Session Analysis Saved: reports/session_analysis_{today_str}.md")
            
    except Exception as exc:
        logger.error("[CLEANUP] Failed to update terminal logs / reports: %s", exc)


async def _cleanup_runtime(ctx: Optional[RuntimeContext]):
    """Ordered shutdown with hard timeouts. Total max: ~25s."""
    if ctx is None:
        return

    logger.info("[SHUTDOWN] Beginning cleanup sequence.")

    # ✅ ADD: Stop FocusEngine first — prevents post-shutdown signals
    if ctx.focus_engine:
        try:
            ctx.focus_engine.stop("PROCESS_SHUTDOWN")
            logger.info("[CLEANUP] FocusEngine stopped.")
        except Exception as e:
            logger.error(f"[CLEANUP] FocusEngine stop failed: {e}")

    # 1. RecEngine — 10s max
    try:
        await asyncio.wait_for(ctx.reconciliation_engine.stop(), timeout=10.0)
    except asyncio.TimeoutError:
        logger.warning("[CLEANUP] RecEngine stop timed out after 10s. Forcing.")
    except Exception as exc:
        logger.error("[CLEANUP] Reconciliation stop failed: %s", exc)

    # 2. Telegram — 5s max
    try:
        await asyncio.wait_for(ctx.bot.stop(), timeout=5.0)
    except asyncio.TimeoutError:
        logger.warning("[CLEANUP] Telegram stop timed out after 5s. Forcing.")
    except Exception as exc:
        logger.error("[CLEANUP] Telegram stop failed: %s", exc)

    # 3. DB Pool — 5s max
    try:
        await asyncio.wait_for(ctx.db_manager.close(), timeout=5.0)
    except asyncio.TimeoutError:
        logger.warning("[CLEANUP] DB close timed out after 5s. Forcing.")
    except Exception as exc:
        logger.error("[CLEANUP] DB close failed: %s", exc)

    # 4. Broker — 5s max
    try:
        await asyncio.wait_for(ctx.broker.disconnect(), timeout=5.0)
    except asyncio.TimeoutError:
        logger.warning("[CLEANUP] Broker disconnect timed out after 5s. Forcing.")
    except Exception as exc:
        logger.error("[CLEANUP] Broker disconnect failed: %s", exc)

    logger.info("[SUPERVISOR] ✅ Cleanup complete.")


def _validate_dependencies(ctx: RuntimeContext) -> None:
    """Hard-fail if any critical dependency is None. Runs BEFORE trading loop."""
    checks = {
        "BrokerInterface": ctx.broker,
        "OrderManager": ctx.order_manager,
        "CapitalManager": ctx.capital_manager,
        "FocusEngine": ctx.focus_engine,
        "FocusEngine.order_manager": ctx.focus_engine.order_manager,
        "TelegramBot": ctx.bot,
    }
    failed = [name for name, obj in checks.items() if obj is None]
    if failed:
        msg = f"[STARTUP FAIL] Critical dependencies not initialized: {failed}"
        logger.critical(msg)
        try:
            ctx.bot.send_alert(f"🚨 STARTUP FAIL: {failed} are None. Bot cannot trade.")
        except Exception:
            pass
        raise RuntimeError(msg)
    logger.info("[STARTUP] ✅ All dependency checks passed. Safe to trade.")


async def _run_startup_validation(ctx: RuntimeContext) -> None:
    """
    Pre-trade validation gate. Candle failure = HALT. WS low = WARN only.
    Called once before trading loop starts.
    """
    logger.info("[STARTUP VALIDATION] Running pre-trade checks...")
    import datetime as _dt

    # 1. Candle API smoke test — HARD HALT on failure
    today = _dt.date.today()
    five_back = today - _dt.timedelta(days=5)
    test_data = {
        "symbol": "NSE:NIFTY50-INDEX",
        "resolution": "1",
        "date_format": "1",
        "range_from": five_back.strftime("%Y-%m-%d"),
        "range_to": today.strftime("%Y-%m-%d"),
        "cont_flag": "1",
    }
    try:
        response = await asyncio.to_thread(ctx.fyers_client.history, data=test_data)
        candle_count = len(response.get("candles", []))
        if candle_count > 0:
            logger.info(f"[STARTUP VALIDATION] ✅ Candle API: {candle_count} bars for NIFTY50")
        else:
            logger.critical(
                f"[STARTUP VALIDATION] ❌ HALT: Candle API returned 0 bars | "
                f"status={response.get('s')} | msg={response.get('message', '')} | params={test_data}"
            )
            raise SystemExit(1)
    except SystemExit:
        raise
    except Exception as e:
        logger.critical(f"[STARTUP VALIDATION] ❌ HALT: Candle API exception: {e}")
        raise SystemExit(1)

    # 2. WS tick count — SOFT WARN (REST fallback is acceptable)
    snap = ctx.broker.cache_health_snapshot()
    fresh = snap.get("fresh", 0)
    total = snap.get("total", 0)
    if fresh >= 100:
        logger.info(f"[STARTUP VALIDATION] ✅ WS Cache: {fresh}/{total} symbols live")
    else:
        logger.warning(
            f"[STARTUP VALIDATION] ⚠️ WS only {fresh}/{total} fresh — using REST fallback, continuing"
        )

    # 3. DB pool alive
    if ctx.db_manager is None:
        logger.critical("[STARTUP VALIDATION] ❌ HALT: DB pool not initialized")
        raise SystemExit(1)
    logger.info("[STARTUP VALIDATION] ✅ DB pool alive")

    logger.info("[STARTUP VALIDATION] ✅ All checks passed — safe to trade")


async def main() -> int:
    import config
    _configure_logging()

    # Phase 72: AEGIS HUD (V1)
    if getattr(config, 'P72_DASHBOARD_ENABLED', False):
        try:
            from dashboard_server import start_dashboard_server
            import threading
            # Running FastAPI in a daemon thread to keep it fully non-blocking
            dashboard_thread = threading.Thread(
                target=start_dashboard_server,
                kwargs={'port': config.P72_DASHBOARD_PORT},
                daemon=True
            )
            dashboard_thread.start()
            logger.info(f"Phase 72: AEGIS HUD V1 deployed at http://127.0.0.1:{config.P72_DASHBOARD_PORT}")
        except Exception as _e:
            logger.error(f"Failed to start AEGIS HUD: {_e}")

    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    
    # Phase 75: Neural Link initialization
    get_dashboard_bridge().set_loop(loop)
    
    _install_signal_handlers(loop, shutdown_event)

    exit_code = 0
    ctx: Optional[RuntimeContext] = None
    bot_start_time = datetime.now(IST)

    try:
        ctx = await _initialize_runtime()
        _validate_dependencies(ctx)
        await _run_startup_validation(ctx)

        async def _notify(message: str):
            try:
                await ctx.bot.send_message(message)
            except Exception as exc:
                logger.error("[NOTIFY] Failed: %s", exc)

        async def _trigger_squareoff():
            # ✅ ADD: Stop the validation monitor BEFORE squaring off
            if ctx.focus_engine:
                ctx.focus_engine.stop("EOD_SQUAREOFF")
                logger.info("[EOD] FocusEngine validation monitor stopped before square-off.")

            try:
                # Add timeout to prevent hanging on broker connection issues
                message = await asyncio.wait_for(
                    asyncio.to_thread(ctx.trade_manager.close_all_positions),
                    timeout=60
                )
                await _notify(f"[EOD] Square-off result:\n{message}")
            except asyncio.TimeoutError:
                logger.error("[EOD] Square-off timed out after 60s!")
                await _notify("⚠️ EOD Square-off TIMED OUT. Some positions might stay open!")
            except Exception as exc:
                logger.error("[EOD] Square-off failed: %s", exc)
                await _notify(f"❌ EOD Square-off FAILED: {exc}")

        async def _run_analysis():
            try:
                # Timeout covers both Simulation (Ghost Trading) and report generation
                analyzer = EODAnalyzer(fyers_client=ctx.broker.rest_client, db=ctx.db_manager)
                report = await asyncio.wait_for(analyzer.run_daily_analysis(), timeout=90)
                await _notify(f"EOD Analysis Complete.\n\n{str(report)[:3000]}")
            except asyncio.TimeoutError:
                logger.error("[EOD] Analysis timed out after 90s!")
                await _notify("⚠️ EOD Analysis (Ghost Trading) TIMED OUT.")
            except Exception as exc:
                logger.error("[EOD] Analysis failed: %s", exc)
                await _notify(f"❌ EOD Analysis FAILED: {exc}")

            # PRD-008: Gate result EOD flush
            try:
                from gate_result_logger import get_gate_result_logger
                grl = get_gate_result_logger()
                summary_path = grl.write_eod_summary()
                flushed = await grl.flush_to_db(ctx.db_manager)
                logger.info(f"[PRD-008] EOD flush: {flushed} gate records written to DB. Summary: {summary_path}")
                await _notify(
                    f"📋 Gate Audit Trail: {flushed} records → DB\n"
                    f"Rejection summary: {summary_path}"
                )
            except Exception as _e:
                logger.error(f"[PRD-008] EOD flush failed: {_e}")

        async def _get_open_positions():
            return await ctx.broker.get_all_positions()

        async def _restart_recovery():
            await ctx.startup_recovery.scan_orphaned_trades()

        _update_terminal_log()

        # PRD-008: periodic terminal log update
        _update_terminal_log()

        async with asyncio.TaskGroup() as tg:
            tg.create_task(
                _supervised(
                    "main_trading_loop",
                    lambda: _trading_loop(shutdown_event, ctx),
                    shutdown_event,
                    on_before_restart=_restart_recovery,
                ),
                name="main_trading_loop",
            )
            tg.create_task(
                _supervised(
                    "telegram_bot",
                    lambda: ctx.bot.run(shutdown_event),
                    shutdown_event,
                    max_retries=5,
                ),
                name="telegram_bot",
            )
            tg.create_task(
                _supervised(
                    "reconciliation",
                    lambda: ctx.reconciliation_engine.run(shutdown_event),
                    shutdown_event,
                    max_retries=999,
                    retry_window_secs=3600,
                ),
                name="reconciliation",
            )
            tg.create_task(
                eod_scheduler(
                    shutdown_event=shutdown_event,
                    trigger_eod_squareoff=_trigger_squareoff,
                    run_eod_analysis=_run_analysis,
                    notify=_notify,
                    get_open_positions=_get_open_positions,
                    bot_start_time=bot_start_time,
                ),
                name="eod_scheduler",
            )
            # Bug 2A: Standalone EOD watchdog — independent failsafe
            tg.create_task(
                eod_watchdog(shutdown_event),
                name="eod_watchdog",
            )
    except* Exception as eg:
        logger.critical("[SUPERVISOR] TaskGroup failed: %s", eg)
        for i, exc in enumerate(eg.exceptions):
            logger.critical(
                "[SUPERVISOR] Sub-exception [%d/%d]: %s: %s",
                i + 1, len(eg.exceptions),
                type(exc).__name__, exc,
                exc_info=exc,
            )
        exit_code = 1
    finally:
        shutdown_event.set()
        await _cleanup_runtime(ctx)
        _update_terminal_log()  # keep last, captures full session shutdown
        logger.info("[SUPERVISOR] Cleanup complete.")

        # ✅ HARD EXIT FALLBACK — kills any hanging non-daemon threads
        # Give Python 10 seconds to exit naturally first (FastAPI/GhostAudit need time)
        import threading
        def _force_exit():
            import time
            time.sleep(10)
            logger.warning("[SUPERVISOR] Process did not exit naturally after 10s. "
                           "Forcing os._exit(0).")
            os._exit(0)

        t = threading.Thread(target=_force_exit, daemon=True)
        t.start()
        # Return normally — if Python exits cleanly, the daemon thread dies with it
        # If Python hangs, the daemon thread force-kills after 3s
        return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
