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
        base_capital=getattr(config, "CAPITAL_PER_TRADE", 1800.0),
        leverage=getattr(config, "INTRADAY_LEVERAGE", 5.0),
    )
    startup_recovery = StartupRecovery(fyers_client)
    startup_recovery.scan_orphaned_trades()

    trade_manager = TradeManager(fyers_client, capital_manager)
    focus_engine = FocusEngine(trade_manager)

    config_settings = {k: v for k, v in vars(config).items() if not k.startswith("__")}
    bot = ShortCircuitBot(config_settings, None, capital_manager, focus_engine)
    trade_manager.bot = bot
    focus_engine.telegram_bot = bot

    market_session = MarketSession(fyers_client, bot)
    logger.info("[INIT] Evaluating market session...")
    morning_context = market_session.initialize_session()
    mh = morning_context["high"] if morning_context else None
    ml = morning_context["low"] if morning_context else None

    scanner = FyersScanner(fyers_client)
    analyzer = FyersAnalyzer(fyers_client, morning_high=mh, morning_low=ml)
    bot.signal_manager = analyzer.signal_manager
    bot.market_session = market_session

    db_manager = DatabaseManager()
    await db_manager.initialize()

    broker = FyersBrokerInterface(
        access_token=access_token,
        client_id=os.getenv("FYERS_CLIENT_ID"),
        db_manager=db_manager,
        emergency_logger=None,
    )
    await broker.initialize()

    # ── P0 FIX: Construct OrderManager with live broker ──────────────
    from order_manager import OrderManager
    order_manager = OrderManager(
        broker=broker,
        telegram_bot=bot,
        db=db_manager,
        capital_manager=capital_manager,
    )

    # Inject into FocusEngine (was None → caused NSESGL-EQ execution miss)
    focus_engine.order_manager = order_manager
    logger.info("[INIT] ✅ OrderManager constructed and injected into FocusEngine.")

    # Also wire into bot for /positions, /pnl, order alerts
    bot.order_manager = order_manager
    # ────────────────────────────────────────────────────────────────────

    reconciliation_engine = ReconciliationEngine(
        broker=broker,
        db_manager=db_manager,
        telegram_bot=bot,
    )

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
    logger.info("[TRADING] Trading loop started.")
    scan_interval = 60
    consecutive_errors = 0
    max_errors = 5

    while not shutdown_event.is_set():
        try:
            if not ctx.market_session.should_trade_now():
                current_state = ctx.market_session.get_current_state()
                if current_state in ("POST_MARKET", "EOD_WINDOW"):
                    logger.info("[TRADING] Session state %s, exiting trading loop.", current_state)
                    return
                await asyncio.sleep(60)
                continue

            if not config.TRADING_ENABLED:
                logger.info("[TRADING] Trading disabled; waiting.")
                await asyncio.sleep(30)
                continue

            start_ts = time.monotonic()
            logger.info("[SCAN] Scanning market...")
            candidates = await asyncio.to_thread(ctx.scanner.scan_market)

            ctx.bot._scan_metadata = {
                "last_scan_time": datetime.now(),
                "candidate_count": len(candidates) if candidates else 0,
            }

            for cand in candidates or []:
                if shutdown_event.is_set():
                    return

                symbol = cand["symbol"]
                signal = await asyncio.to_thread(
                    ctx.analyzer.check_setup,
                    symbol,
                    cand["ltp"],
                    cand.get("oi", 0),
                    cand.get("history_df"),
                )
                if not signal:
                    continue

                if signal.get("cooldown_blocked"):
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
            logger.error(
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
    except Exception as exc:
        logger.error("[CLEANUP] Failed to update terminal log: %s", exc)


async def _cleanup_runtime(ctx: Optional[RuntimeContext]):
    """Ordered shutdown with hard timeouts. Total max: ~25s."""
    if ctx is None:
        return

    logger.info("[SHUTDOWN] Beginning cleanup sequence.")

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


async def main() -> int:
    _configure_logging()
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, shutdown_event)

    exit_code = 0
    ctx: Optional[RuntimeContext] = None
    bot_start_time = datetime.now(IST)

    try:
        ctx = await _initialize_runtime()
        _validate_dependencies(ctx)

        async def _notify(message: str):
            try:
                await ctx.bot.send_message(message)
            except Exception as exc:
                logger.error("[NOTIFY] Failed: %s", exc)

        async def _trigger_squareoff():
            message = await asyncio.to_thread(ctx.trade_manager.close_all_positions)
            await _notify(f"[EOD] Square-off result:\n{message}")

        async def _run_analysis():
            analyzer = EODAnalyzer(fyers_client=ctx.broker.rest_client, db=ctx.db_manager)
            report = await analyzer.run_daily_analysis()
            await _notify(f"EOD Analysis Complete.\n\n{str(report)[:3000]}")

        async def _get_open_positions():
            return await ctx.broker.get_all_positions()

        async def _restart_recovery():
            await asyncio.to_thread(ctx.startup_recovery.scan_orphaned_trades)

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
        exit_code = 1
    finally:
        shutdown_event.set()
        await _cleanup_runtime(ctx)
        _update_terminal_log()  # keep last, captures full session shutdown
        logger.info("[SUPERVISOR] Cleanup complete.")

    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
