# ShortCircuit — Architecture Reference
**Version:** Post-Phase 44.9 + PRD v2.2 | **Last Updated:** 2026-03-03

---

## SECTION 1 — System Overview

ShortCircuit is a fully automated, event-driven algorithmic trading bot for NSE (Indian National Stock Exchange) equities, operating intraday-only (all positions closed by 15:10 IST). It implements a short-selling momentum reversal strategy, detecting and trading against institutional exhaustion at intraday highs. The strategy core is the **GOD MODE signal**: a multi-factor gate that requires simultaneous confirmation of exhaustion at stretch (9–14.5% intraday gain + new high + vol_fade < 0.65 + above VAH), RVOL spike (≥3×), volume-profile deviation (LTP vs. POC divergence), order-flow evidence (Trapped Longs, absorption), and Higher-Time-Frame structure (15m Lower High). Bearish patterns are bonus confidence scorers. A separate 12-gate validation framework monitors price in real-time and only fires execution when LTP breaks the entry trigger.

The system infrastructure is: Python 3.10+ asyncio, Fyers API v3 (REST for quote batches and order submission; WebSocket for real-time tick data and order fill events), PostgreSQL + asyncpg for trade journaling, and python-telegram-bot (PTB) v20+ for the operator interface. The operator has no web UI — all signals, trade alerts, live P&L, commands (`/auto on`, `/status`, `/positions`), and EOD summaries flow exclusively through Telegram.

The concurrency model is a **single asyncio event loop** with a `TaskGroup` launching four concurrent tasks: `trading_loop`, `telegram_bot`, `reconciliation`, and `eod_scheduler`, plus the new `eod_watchdog` (Bug 2A fix). All tasks share a single `asyncio.Event` called `shutdown_event`. When any component sets this event, every `while not shutdown_event.is_set()` loop exits cleanly. The maximum signal limit per day is **5**, enforced by `SignalManager` with a 45-minute per-symbol cooldown. A consecutive-loss pause (3 losses) also halts new signals for the rest of the session.

---

## SECTION 2 — Root Directory Files

### main.py
**Role:** Supervisor and entry point — initialises all components, assembles `RuntimeContext`, runs the `asyncio.TaskGroup`, and orchestrates clean shutdown.
**Key Classes:** `RuntimeContext` (dataclass)
**Key Functions:** `_configure_logging`, `_install_signal_handlers`, `_supervised`, `_initialize_runtime`, `_cleanup_runtime`, `_validate_dependencies`, `_trading_loop`, `_update_terminal_log`, `main`
**Imports from project:** `analyzer.FyersAnalyzer`, `capital_manager.CapitalManager`, `database.DatabaseManager`, `eod_analyzer.EODAnalyzer`, `eod_scheduler.eod_scheduler`, `eod_watchdog.eod_watchdog`, `focus_engine.FocusEngine`, `fyers_broker_interface.FyersBrokerInterface`, `fyers_connect.FyersConnect`, `market_session.MarketSession`, `market_utils.is_market_hours`, `reconciliation.ReconciliationEngine`, `scanner.FyersScanner`, `startup_recovery.StartupRecovery`, `telegram_bot.ShortCircuitBot`, `trade_manager.TradeManager`, `order_manager.OrderManager`
**Called by:** Nothing (entry point — `python main.py`)
**Calls into:** All of the above
**State it owns:** `shutdown_event: asyncio.Event`, `RuntimeContext` dataclass instance, `IST` timezone
**Error handling:** `except* Exception` on `TaskGroup` failures; `_supervised()` wrapper retries crashed tasks up to `max_retries`; `_validate_dependencies()` raises `RuntimeError` + Telegram alert if any dep is `None`; `finally` always calls `_cleanup_runtime()` and `_update_terminal_log()`
**Notes:** `AUTO_MODE` is hardcoded `False` in `config.py` — cannot be enabled by env var; only `/auto on` Telegram command enables trading. Startup injects broker into scanner and subscribes to the scanner universe via WebSocket. Startup sequence includes: (a) `broker.seed_from_rest(scanner_symbols)` before WS subscribe — seeds cache so no symbols start as `Missing`; (b) startup validation gate — candle API HARD HALT, DB HARD HALT, WS SOFT WARN; (c) at trading loop start: auto queue resolution (`_auto_on_queued` → `auto_mode=True`) then `_send_morning_briefing()`.

---

### config.py
**Role:** Central configuration module — loads env vars via `python-dotenv`, defines all trading parameters, feature flags, and one helper function.
**Key Classes:** None
**Key Functions:** `minutes_since_market_open`, `set_trading_enabled`
**Imports from project:** None (no local imports)
**Called by:** Nearly every module (`import config` or `from config import ...`)
**Calls into:** `os`, `dotenv`, `datetime`, `pytz`
**State it owns:** All module-level constants (`CAPITAL_PER_TRADE=1800`, `INTRADAY_LEVERAGE=5.0`, `AUTO_MODE=False`, `MAX_SIGNALS_PER_DAY` via `SignalManager` default, `VALIDATION_TIMEOUT_MINUTES=15`, `SQUARE_OFF_TIME=15:10`, `EDITABLE_SIGNAL_FLOW_ENABLED=False`, `ETF_CLUSTER_DEDUP_ENABLED=True`, etc.)
**Error handling:** None — config errors surface as `AttributeError` at import time
**Notes:** `AUTO_MODE` is overridden to `False` regardless of env var (explicit safety measure). `TRADING_ENABLED` is dynamically updated by `MarketSession` via `set_trading_enabled()`.

---

### fyers_broker_interface.py
**Role:** Unified broker interface — manages Fyers REST API client and both WebSocket connections (Data WS for ticks, Order WS for fill events); exposes `place_order`, `get_positions`, tick subscriptions, and callback registration.
**Key Classes:** `OrderUpdate`, `PositionUpdate`, `TickData`, `FyersBrokerInterface`, `CacheEntry` (dataclass), `CacheEntrySource` (enum: `WS_TICK`, `REST_SEED`)
**Key Functions:** `get_quote_cache_snapshot`, `subscribe_scanner_universe`, `seed_from_rest`
**Imports from project:** None (imports Fyers SDK: `fyers_apiv3`, `fyers_apiv3.FyersWebsocket.data_ws`, `fyers_apiv3.FyersWebsocket.order_ws`)
**Called by:** `main.py`, `order_manager.py`, `reconciliation.py`, `trade_manager.py` (via fyers client), `focus_engine.py` (via FyersConnect)
**Calls into:** Fyers SDK (external), `database.py`
**State it owns:** `position_cache: dict[str, PositionUpdate]`, `_quote_cache: dict`, `_ws_subscribed_symbols_set: set`, `order_callbacks: list`, `tick_callbacks: list`, `data_ws`, `order_ws`, connection state flags, rate-limit tracking dict
**Error handling:** `_on_data_ws_error` / `_on_order_ws_error` log errors; WebSocket reconnect handled by Fyers SDK automatically; DNS errors (errno 11001) logged and SDK retries; `place_order` wraps REST call in try/except returning `None` on failure
**Notes:** Both WebSocket connections run in background daemon threads (blocking SDK calls); asyncio callbacks are scheduled via `asyncio.get_event_loop().call_soon_threadsafe`. WS import has a fallback if `setuptools==79.0.1` is not installed. Maintains a real-time `_quote_cache` from Data WS ticks via a formal UNINITIALIZED→PRIMING→READY state machine (Phase 44.9 — see SECTION 16). Health monitor daemon thread runs every 30s and triggers automatic re-prime if cache freshness drops below 50%. Cache entries now use `CacheEntry` dataclass with `source` field (`WS_TICK` or `REST_SEED`). `seed_from_rest(symbols)` seeds all symbols from REST snapshot at startup so none are counted as `Missing` before first WS tick. Health snapshot includes `fresh`, `stale`, `seeded`, `missing` fields. Scan tier uses `known_pct` (fresh + stale + seeded) for `WS_CACHE`/`HYBRID`/`REST_EMERGENCY` decisions. 3 consecutive re-prime failures trigger a **nuclear full reconnect**: Data WS torn down completely, 5-second sleep, full re-subscribe + re-seed cycle. Guarded by `_reprime_failure_count` counter, reset to 0 on any successful re-prime.

---

### order_manager.py
**Role:** Async order lifecycle manager — entry, SL placement, WebSocket fill detection, safe exit, capital allocation/release, and DB journaling. Primary execution path for all live trades.
**Key Classes:** `OrderManager`
**Key Functions:** None (all methods on `OrderManager`)
**Imports from project:** `fyers_broker_interface.FyersBrokerInterface`
**Called by:** `focus_engine.py` (`check_pending_signals` → `order_manager.enter_position`), `telegram_bot.py` (manual exit commands), `main.py` (construction + injection)
**Calls into:** `fyers_broker_interface.py`, `database.py`, `capital_manager.py`, `telegram_bot.py` (alerts)
**State it owns:** `positions: dict[str, dict]` (open positions keyed by symbol), `pending_orders: dict[str, str]` (order_id→symbol), per-symbol `asyncio.Lock` objects
**Error handling:** `enter_position` wraps REST call; on `code -50` or rejection, logs `ERROR` + sends Telegram failure alert with full payload; capital is **not** deducted until fill confirmed; `safe_exit` has WebSocket race condition protection via per-symbol locks
**Notes:** `FYERS_ORDER_STATUS_TRADED = 2` constant used to detect fill from WS event. [P0 FIX] — previously never instantiated; `focus_engine.order_manager` was `None`.

---

### focus_engine.py
**Role:** Signal validation gate and position monitor — maintains `pending_signals` dict, monitors price vs. trigger in a background thread, fires `order_manager.enter_position` on validation, and runs the `focus_loop` for active position management.
**Key Classes:** `FocusEngine`
**Key Functions:** None (all methods)
**Imports from project:** `fyers_connect.FyersConnect`, `config`, `order_manager.OrderManager`, `discretionary_engine.DiscretionaryEngine`
**Called by:** `main.py` (construction, injection, `_trading_loop` calls `check_pending_signals`)
**Calls into:** `order_manager.py`, `fyers_connect.py`, `discretionary_engine.py`, `telegram_bot.py` (via `self.telegram_bot`)
**State it owns:** `pending_signals: dict`, `cooldown_signals: dict`, `active_trade: dict`, `monitor_thread: threading.Thread`, `is_running: bool`
**Error handling:** [P0 FIX] — `check_pending_signals` raises `RuntimeError` if `self.order_manager is None` (was previously a silent no-op). `attempt_recovery()` called in `__init__` to adopt orphaned broker positions.

```
Validation Gate Architecture (Updated 2026-03-04):

SIGNAL LIFECYCLE:
add_pending_signal(signal_data)
  → stores: trigger, invalidation, timestamp, queued_at (NEW), correlation_id
  → calls start_pending_monitor() if not already running

start_pending_monitor()
  → PRIMARY: asyncio.create_task(monitor_pending_loop())
    stored in self._monitor_task for later cancellation
  → FALLBACK (no event loop): threading.Thread(_monitor_pending_loop_sync, loop)
    loop passed explicitly — required for Python 3.12 compatibility

monitor_pending_loop() [async]
  → EOD GUARD: if hour==15 and minute>=10 → calls self.stop("EOD_TIME_BOUNDARY")
  → flushes cooldown signals via await asyncio.to_thread(flush_pending_signals)
  → calls await check_pending_signals() every 2 seconds

check_pending_signals() [async]
  → EOD GUARD: if hour==15 and minute>=10 → calls self.stop(), returns
  → fetches LTP via await asyncio.to_thread(self.fyers.quotes, ...)
  → THREE outcomes per symbol:
    A. LTP < trigger → VALIDATED → execute (with slot guard)
    B. LTP > invalidation → INVALIDATED → remove
    C. age > VALIDATION_TIMEOUT_MINUTES → TIMEOUT → remove
  → Uses continue (NOT return) after each symbol — prevents signal starvation
    when 2+ stocks are in pending_signals simultaneously

STALE SIGNAL FLUSH (NEW):
flush_stale_pending_signals(max_age_minutes=20)
  → Called at 9:45 session boundary from main.py trading_loop
  → Drops any signal older than 20 minutes (queued during pre-open)
  → Prevents stale pre-market trigger prices from executing at open

FocusEngine.stop(reason) [NEW METHOD]:
  → Sets monitoring_active = False
  → Clears pending_signals and cooldown_signals
  → Cancels self._monitor_task if running
  → Called from:
    - monitor_pending_loop EOD guard (15:10)
    - check_pending_signals EOD guard (15:10)
    - trigger_squareoff() in main.py (15:10)
    - cleanup_runtime() in main.py (shutdown)
```

**Notes:** Validation monitor runs as `asyncio.create_task(monitor_pending_loop())` — PRIMARY path. `threading.Thread` fallback (`_monitor_pending_loop_sync`) used only when no event loop is available. Task stored in `self._monitor_task` for cancellation via `stop()`. `sfp_watch_loop` monitors for a Sweep-and-Flip pattern 10 minutes after exit.

---

### trade_manager.py
**Role:** Legacy order execution engine (pre-OrderManager). Handles EOD square-off, partial closes, SL modification, and signal CSV logging. Still used for `close_all_positions()` at EOD.
**Key Classes:** `TradeManager`
**Key Functions:** None (all methods)
**Imports from project:** `config`, `capital_manager.CapitalManager`
**Called by:** `main.py` (construction), `focus_engine.py` (passed as `trade_manager` param — legacy path no longer used for new orders), `eod_scheduler.py` (square-off at 15:10)
**Calls into:** Fyers REST client directly (`self.fyers`), `capital_manager.py`
**State it owns:** `auto_trade_enabled: bool`, `positions: dict`, SL tracking state
**Error handling:** Position verification before every exit (`_verify_position_safe`); REST call errors caught and logged
**Notes:** New signal execution routes through `OrderManager`, not `TradeManager`. `TradeManager` is retained for EOD square-off (`close_all_positions`) and SL order management.

---

### telegram_bot.py
**Role:** Full Telegram operator interface — PTB Application, all command handlers (`/auto`, `/status`, `/positions`, `/pnl`, `/why`, `/skip`, `/go`), signal/trade alert formatters, live 2-second dashboard loop, and the global PTB error handler.
**Key Classes:** `ShortCircuitBot`
**Key Functions:** None (all methods on `ShortCircuitBot`)
**Imports from project:** `config`, `capital_manager.CapitalManager`, `focus_engine.FocusEngine`, `order_manager.OrderManager` (via injection)
**Called by:** `main.py` (construction, `bot.run(shutdown_event)`)
**Calls into:** `focus_engine.py`, `order_manager.py`, `capital_manager.py`, `signal_manager.py`, `diagnostic_analyzer.py`
**State it owns:** `_auto_mode: bool` (default `False`), `_auto_on_queued: bool` (default `False`), `_morning_brief_sent: bool` (default `False`), `app: Application` (PTB), `order_manager` (injected post-construction), dashboard task handle
**Error handling:** Global `_error_handler` registered via `app.add_error_handler()`. [Bug 3 FIX] — Transient `getaddrinfo`/`NetworkError` errors now return a single WARNING log instead of full traceback + Telegram alert. Other errors log full traceback + send alert.
**Notes:** `AUTO_MODE` must be `False` on startup — enforced by `config.py`. Dashboard refresh loop (`_dashboard_refresh_loop`) polls `focus_engine.get_position_snapshot()` every 2 seconds. `send_signal_alert` displays confidence, vol_fade, pattern bonus, and Futures OI emoji. `/auto_on` before 09:45 IST queues activation (`_auto_on_queued=True`) and replies with minutes remaining — does not activate immediately. Queue resolves at trading loop start in `main.py`. `_send_morning_briefing()` fires once per session at trading loop start (guarded by `_morning_brief_sent`); includes NIFTY morning range, WS cache stats, candle/DB status, and Auto Mode state.

---

### scanner.py
**Role:** Market scanner — fetches NSE symbol universe, attempts to use broker WebSocket quote cache first (<60s old) for scanning ~2418 EQ symbols, falling back to batch REST quotes (50 at a time) if needed. Filters by gain%/volume/price, then parallel-fetches candle history for quality checking.
**Key Classes:** `FyersScanner`
**Key Functions:** `_fetch_nse_symbols_sync`, `scan_market`
**Imports from project:** `fyers_connect.FyersConnect`
**Called by:** `main.py` (`_trading_loop` calls `scanner.scan_market()`)
**Calls into:** Fyers REST API (via `fyers_connect`), NSE symbol master endpoint
**State it owns:** `symbols: list` (cached symbol universe)
**Error handling:** `check_chart_quality` passes on API lag (does not reject liquid stocks due to transient empty data); individual quote fetch failures caught per-symbol
**Notes:** Gain filter: 6–18%. Volume filter: >100k. LTP filter: config.SCANNER_MIN_LTP (default 50). `SCANNER_PARALLEL_WORKERS=3`. Symbol list cached — re-fetched from NSE master synchronously on startup via requests. WS cache vastly reduces REST API calls during scans. Candle history fetch uses `date_format="1"` with YYYY-MM-DD range strings and 5-day lookback. Tier freshness TTL sourced from `config.WS_TICK_FRESHNESS_TTL_SECONDS`. Tier selector: `WS_CACHE` when fresh ≥ threshold; `HYBRID` when `known_pct ≥ 90%`; `REST_EMERGENCY` only when truly unknown.

```
Bug Fixed (2026-03-04):
- scanner.py:check_chart_quality had a dangling reference to `to_date`
- Variable was deleted during a prior refactor (BUG-03 candle fix)
  but the reference at line 132 was left behind
- NameError caused every quality check to fail with:
  ERROR - Quality Check Error SYMBOL name to_date is not defined
- Function was fail-open: returned (True, None) on exception
- All stocks passed quality check with history_df = None
- Downstream analysis was running on corrupted/missing data

Fix Applied:
- scanner.py L132: datetime.datetime.fromtimestamp(to_date)
  replaced with _dt.datetime.now()
- Duplicate bare `import datetime` removed (was shadowing _dt alias)

Current behaviour:
- Quality check runs cleanly
- Illiquid stocks correctly rejected:
  WARNING - [SKIP] Quality Reject: NSE:CALSOFT-EQ | Zero Volume: 72%
- Stocks with valid microstructure promoted as CANDIDATE with real dataframes
```
---

### analyzer.py
**Role:** Core signal analysis engine — runs the full 12-gate God Mode strategy check on each scanner candidate, calls pattern detection, RVOL check, VWAP slope, market profile, order flow, HTF confirmation, and logs ML observations.
**Key Classes:** `FyersAnalyzer`
**Key Functions:** `log_signal`
**Imports from project:** `market_context.MarketContext`, `signal_manager.get_signal_manager`, `god_mode_logic.GodModeAnalyst`, `tape_reader.TapeReader`, `market_profile.ProfileAnalyzer`, `ml_logger.get_ml_logger`, `config`
**Called by:** `main.py` (`_trading_loop` calls `analyzer.check_setup()` per candidate)
**Calls into:** `market_context.py`, `signal_manager.py`, `god_mode_logic.py`, `tape_reader.py`, `market_profile.py`, `ml_logger.py`, Fyers REST API
**State it owns:** `signal_manager` singleton reference, RVOL validity gate state
**Error handling:** Per-gate failures return `False` (signal discarded silently); signal CSV write wrapped in try/except
**Notes:** `check_setup_with_edges()` used when `MULTI_EDGE_ENABLED=True` (currently `False`). `RVOL_VALIDITY_GATE_ENABLED=True` requires ≥20 minutes of market data before RVOL checks are valid. Gate 5 now fires at stretch high using exhaustion-at-stretch logic (no longer requiring breakdown), leaving Gate 10 as the sole breakdown confirmation. 5 new `signal_meta` fields added. OI enrichment happens in `_check_pro_confluence`.

---

### reconciliation.py
**Role:** HFT reconciliation engine — zero-cost when flat (pure WebSocket cache check), cache-driven when live. Detects orphaned, phantom, and mismatched positions between DB and broker.
**Key Classes:** `ReconciliationEngine`
**Key Functions:** None (all methods)
**Imports from project:** `database.DatabaseManager`, `fyers_broker_interface.FyersBrokerInterface`
**Called by:** `main.py` (`reconciliation_engine.run(shutdown_event)` in TaskGroup; `_cleanup_runtime` calls `stop()`)
**Calls into:** `fyers_broker_interface.py`, `database.py`, `telegram_bot.py` (alerts)
**State it owns:** `_db_positions: dict`, `_db_dirty: bool`, `_has_open_positions: bool`, `_shutdown_event: asyncio.Event`, `running: bool`
**Error handling:** [Bug 2B FIX] — `stop()` now logs structured messages; `run()` uses `_interruptible_sleep()` instead of bare `asyncio.sleep()`; `_cleanup_runtime()` wraps `stop()` in `asyncio.wait_for(..., timeout=10.0)`; DB query already has 1.5s timeout
**Notes:** Market hours interval: 6s. Off-hours with positions: 30s. Fully flat off-hours: 300s. Dirty flag set by `TradeManager.mark_dirty()` on trade open/close.

---

### eod_scheduler.py
**Role:** EOD task scheduler — triggers EOD square-off at 15:10 IST and EOD analysis at 15:32 IST, then fires `shutdown_event.set()` and exits.
**Key Classes:** None
**Key Functions:** `eod_scheduler` (async coroutine), `_get_now`
**Imports from project:** None (pure stdlib + pytz)
**Called by:** `main.py` (registered in TaskGroup as `"eod_scheduler"`)
**Calls into:** Callback functions passed in: `trigger_eod_squareoff`, `run_eod_analysis`, `notify`, `get_open_positions`
**State it owns:** `eod_done_today: bool`, `analysis_done_today: bool`, `last_date: date`
**Error handling:** Square-off and analysis failures caught and notified to Telegram; loop uses `asyncio.wait_for(shutdown_event.wait(), timeout=15)` for interruptible sleep
**Notes:** [Bug 2A FIX] — After analysis fires at 15:32, now calls `shutdown_event.set()` and returns. Previously kept looping indefinitely after EOD work completed. Uses IST-aware `datetime.now(IST)` correctly throughout.

---

### eod_watchdog.py
**Role:** Standalone EOD failsafe — independent of all other tasks; fires graceful shutdown at 15:32 IST, SIGTERM at 15:40 IST if process is still alive.
**Key Classes:** None
**Key Functions:** `eod_watchdog` (async coroutine)
**Imports from project:** None (pure stdlib + pytz)
**Called by:** `main.py` (registered in TaskGroup as `"eod_watchdog"`) [NEW — Bug 2A FIX]
**Calls into:** `asyncio.sleep`, `os.kill`, `signal.SIGTERM`
**State it owns:** `soft_fired: bool` (module-local to coroutine)
**Error handling:** No exceptions expected — pure time check + signal send
**Notes:** Checks every 30 seconds. Cannot be blocked by scanning loops, DB hangs, or WS stalls. Soft `shutdown_event.set()` at 15:32. Hard `os._exit(0)` at 15:40 — bypasses all Python cleanup, cannot be trapped by signal handlers. `EOD_SOFT_SHUTDOWN=(15,32)`, `EOD_HARD_KILL=(15,40)`.

---

### capital_manager.py
**Role:** Capital tracker with 5× intraday leverage — tracks buying power, prevents orders when insufficient, allocates/releases per position.
**Key Classes:** `CapitalManager`
**Key Functions:** None (all methods)
**Imports from project:** None
**Called by:** `order_manager.py`, `trade_manager.py`, `telegram_bot.py` (status display), `main.py` (construction)
**Calls into:** Nothing
**State it owns:** `base_capital=₹1800`, `leverage=5.0`, `total_buying_power=₹9000`, `available: float`, `positions: dict[str, float]`
**Error handling:** Double-allocation guard (logs warning if symbol already allocated); double-release guard
**Notes:** `get_status()` returns `{'available': float, 'total_buying_power': float, ...}`. `available` represents buying power (₹9000 minus allocated), not base capital.

---

### signal_manager.py
**Role:** Daily signal gate — enforces max 5 signals/day, 45-min per-symbol cooldown, and 3-consecutive-loss auto-pause. Global singleton via `get_signal_manager()`.
**Key Classes:** `SignalManager`
**Key Functions:** `get_signal_manager`
**Imports from project:** None
**Called by:** `analyzer.py` (`get_signal_manager()` singleton), `telegram_bot.py` (status display)
**Calls into:** Nothing
**State it owns:** `signals_today: list`, `last_signal_time: dict`, `consecutive_losses: int`, `is_paused: bool`, `stats: defaultdict`
**Error handling:** `_reset_if_new_day()` called at start of every public method — auto-resets on date change

```
Slot Burn Behaviour (Updated 2026-03-04):

PREVIOUS (BROKEN) FLOW:
1. Signal validated
2. record_signal() called → slot burned
3. enter_position() called (was missing await → crashed)
4. Slot burned on EVERY crash → signals_today filled with phantom entries
5. can_signal() never consulted → slot count irrelevant
6. Same stock re-validated 3,500+ times per session

CURRENT (FIXED) FLOW:
1. can_signal(symbol) called BEFORE any execution attempt
   → if slots exhausted: signal dropped, pending_signals cleared, continue
2. await enter_position() called
3. Only if pos is a valid dict with order_id:
   → record_signal() called → slot burned
   → remaining slots logged
4. If pos is None or invalid:
   → WARNING logged
   → Telegram alert sent
   → Slot NOT burned

Thread Safety:
- threading.Lock() added to SignalManager.__init__
- All reads (can_signal) and writes (record_signal) wrapped with self._lock
- Prevents race condition when two signals fire concurrently
```

**Notes:** Singleton — one instance per process. Uses naive `datetime.now()` for date comparison (sufficient for daily reset purposes).

---

### market_session.py
**Role:** Market session state machine — determines if bot started pre/during/post market, handles startup gating, fetches morning NIFTY range, and calls `set_trading_enabled()`.
**Key Classes:** `MarketSession`
**Key Functions:** None (all methods)
**Imports from project:** `config.MARKET_SESSION_CONFIG`, `config.set_trading_enabled`, `symbols.NIFTY_50`
**Called by:** `main.py` (construction + `initialize_session()` at startup), `telegram_bot.py` (injected as `bot.market_session`)
**Calls into:** Fyers REST (morning range fetch), `telegram_bot.py` (startup alerts)
**State it owns:** Session state enum (`PREMARKET`, `EARLY_MARKET`, `MID_MARKET`, `EOD_WINDOW`, `POSTMARKET`), `morning_high/low`
**Error handling:** `morning_range_fallback_pct=0.5%` used if morning range API call fails
**Notes:** `should_trade_now()` is the gatekeeper for the main trading loop.

---

### database.py
**Role:** Async PostgreSQL manager (asyncpg) — singleton connection pool, async CRUD methods, trade entry/exit logging, and a synchronous `query()` method for offline scripts.
**Key Classes:** `DatabaseManager`
**Key Functions:** None (all methods)
**Imports from project:** None (uses asyncpg + optional psycopg2)
**Called by:** `order_manager.py`, `reconciliation.py`, `main.py`
**Calls into:** asyncpg (PostgreSQL), psycopg2 (sync fallback for `query()`)
**State it owns:** `_pool: asyncpg.Pool` (class-level singleton), `DB_CONFIG` dict
**Error handling:** Pool operations wrapped in try/except in calling code; `query()` uses psycopg2 with separate blocking connection per call
**Notes:** Database: `shortcircuit_trading`. Default credentials in code (should come from env). `log_trade_entry()` uses atomic transaction across `positions` and `orders` tables.

---

### ml_logger.py
**Role:** ML data logger — records signal observations to daily Parquet files for future model training. Logs features at signal time; outcomes updated at EOD or trade close.
**Key Classes:** `MLDataLogger`
**Key Functions:** `get_ml_logger`
**Imports from project:** None
**Called by:** `analyzer.py` (`get_ml_logger().log_observation(...)`)
**Calls into:** `pandas`, `pathlib`
**State it owns:** Daily DataFrame in memory, parquet file at `data/ml/data{YYYY-MM-DD}.parquet`
**Error handling:** Atomic writes (temp file + rename to prevent corruption on crash)
**Notes:** Schema version `1.0.0`. Observation ID is UUID4. Features include: pattern, RVOL, VWAP bands, OF flags (`is_trapped`, `is_absorption`, `is_bad_high`), Nifty trend, bid-ask spread, confirmations list. ML predictions are **not** used in signal scoring — logging only (future training dataset).

---

### god_mode_logic.py
**Role:** Core technical analysis primitives — VWAP slope, structure detection (absorption/exhaustion), ATR, advanced pattern detection (Shooting Star, Bearish Engulfing, Evening Star, Doji), Market Profile (VAH/VAL/POC), Fibonacci levels.
**Key Classes:** `GodModeAnalyst`
**Key Functions:** `is_exhaustion_at_stretch`
**Imports from project:** None (pandas, numpy only)
**Called by:** `analyzer.py` (`GodModeAnalyst` instance), `multi_edge_detector.py` (inline reproduction of pattern detection)
**Calls into:** `pandas`, `numpy`
**State it owns:** None (stateless)
**Error handling:** None explicit — division-by-zero guards in VWAP calculation
**Notes:** `scipy.stats.linregress` import is commented out. VWAP slope < 0.05 = FLAT (reversion setup), > 0.1 = TRENDING. `is_exhaustion_at_stretch` computes the primary Phase 44.8 trading edge.

---

### market_context.py
**Role:** Macro context engine — determines NIFTY market regime (TREND_UP/TREND_DOWN/RANGE) using first-hour range, filters out unfavorable shorting conditions. Fetches 9:15–9:45 IST morning range via REST (IST-aware epoch with `ZoneInfo("Asia/Kolkata")`). Exposes `morning_range_valid` flag to guard all range-dependent logic.
**Key Classes:** `MarketContext`
**Key Functions:** `_fetch_morning_range_from_rest`, `_refresh_morning_range_if_needed` (5-min throttled), `morning_high` (property), `morning_low` (property)
**Imports from project:** `symbols.NIFTY_50`, `symbols.validate_symbol`, `config`
**Called by:** `analyzer.py` (`_check_filters` calls `market_context.should_allow_short()`)
**Calls into:** Fyers REST (NIFTY intraday data)
**State it owns:** `morning_high/low`, `morning_range_valid: bool`, `_last_range_fetch_time: float`, cached regime
**Error handling:** Regime defaults to allowing trade if API call fails
**Notes:** `should_allow_short()` has override patterns (EVENING_STAR, BEARISH_ENGULFING, SHOOTING_STAR) that bypass regime filter. If `morning_range_valid=False`, regime logic returns safe `"RANGE"` instead of using a 0/0 anchor. `_refresh_morning_range_if_needed()` throttled to max once per 5 minutes. MID_MARKET cold start (bot started after 9:45) fetches range immediately via REST — never reads from empty WS cache.

---

### multi_edge_detector.py
**Role:** Phase 41.1 multi-edge detection system — runs 5 parallel institutional edge detectors (Pattern, Trapped Longs, Absorption, Bad High, Failed Auction) with weighted confidence scoring.
**Key Classes:** `MultiEdgeDetector`
**Key Functions:** None (all methods)
**Imports from project:** `config`
**Called by:** `analyzer.py` (`check_setup_with_edges()` — only when `MULTI_EDGE_ENABLED=True`, currently `False`)
**Calls into:** `config` (edge weights, thresholds, detector toggles)
**State it owns:** `enabled_detectors: dict`
**Error handling:** Each detector returns `None` on failure (non-fatal)
**Notes:** `MULTI_EDGE_ENABLED=False` — system is inactive. Confidence thresholds: EXTREME≥5.0, HIGH≥3.0, MEDIUM≥2.0. Single MEDIUM edge rejected without confluence.

---

### eod_analyzer.py
**Role:** EOD session analysis — queries DB for today's closed trades, formats P&L summary, and sends to Telegram. Separate from `eod_analysis.py` (offline simulation script).
**Key Classes:** `EODAnalyzer`
**Key Functions:** None (all methods)
**Imports from project:** `database.DatabaseManager`, `fyers_connect.FyersConnect`
**Called by:** `main.py` (`_run_analysis()` callback → `eod_scheduler` at 15:32)
**Calls into:** `database.py`, `telegram_bot.py` (via notify callback)
**State it owns:** None (stateless)
**Error handling:** DB query failures caught, fallback to empty report
**Notes:** Uses `db.get_today_trades()` which queries the `positions` table for CLOSED trades. Different from `eod_analysis.py` which is a standalone CLI simulation script.

---

### eod_analysis.py
**Role:** Offline EOD simulation CLI script — loads `logs/signals.csv`, fetches post-signal price history, and compares legacy vs. scalper risk system side-by-side.
**Key Classes:** `EODAnalyzer` (different from `eod_analyzer.py`)
**Key Functions:** None
**Imports from project:** `trade_simulator.TradeSimulator`, `fyers_connect.FyersConnect`
**Called by:** `eod_scheduler.py` (via `run_eod_analysis` callback — uses `eod_analyzer.EODAnalyzer`, not this file)
**Calls into:** `trade_simulator.py`, Fyers REST
**State it owns:** None (stateless CLI tool)
**Error handling:** `FYERS_NO_INTERACTIVE=true` set to prevent re-auth prompt in scheduled runs
**Notes:** Run as `python eod_analysis.py [YYYY-MM-DD]`. Output written to `logs/eod_summary.csv` and `md/terminal_log.md`.

---

### fyers_connect.py
**Role:** Singleton Fyers authentication manager — loads/validates saved token from `data/access_token.txt`, runs OAuth flow if expired, builds `fyersModel.FyersModel` REST client.
**Key Classes:** `FyersConnect`
**Key Functions:** None (singleton via `__new__`)
**Imports from project:** `config`
**Called by:** `main.py` (primary construction), `focus_engine.py` (`FyersConnect().authenticate()` — uses singleton), `scanner.py`, `analyzer.py`
**Calls into:** `fyers_apiv3.fyersModel`, `os`, `webbrowser` (auth flow only)
**State it owns:** `_instance` (class-level singleton), `_access_token: str`, `_fyers: FyersModel`
**Error handling:** Token validation via lightweight `get_profile()` call; raises `ConnectionError` if no valid token and `FYERS_NO_INTERACTIVE` is set
**Notes:** Token stored at `data/access_token.txt`. Also checks `FYERS_ACCESS_TOKEN` env var as override. REST client built with `is_async=False`.

---

### market_utils.py
**Role:** Minimal utility — single `is_market_hours()` helper function.
**Key Classes:** None
**Key Functions:** `is_market_hours`
**Imports from project:** None
**Called by:** `main.py`
**Calls into:** `datetime`, `pytz`
**State it owns:** None
**Error handling:** None
**Notes:** 375 bytes. Thin wrapper.

---

### symbols.py
**Role:** NSE symbol constants, symbol validation, and front-month futures resolution.
**Key Classes:** None
**Key Functions:** `validate_symbol`, `_last_thursday`, `get_front_month_futures`
**Imports from project:** None
**Called by:** `market_context.py`, `market_session.py`, `analyzer.py`
**Calls into:** Nothing
**State it owns:** `NIFTY_50: str` constant
**Error handling:** `get_front_month_futures` returns `None` on failure
**Notes:** Static data + dynamic derivatives processing. Provides auto-rolling futures symbol generation.

---

### startup_recovery.py
**Role:** Orphaned trade scanner at startup — checks broker for open positions not tracked locally.
**Key Classes:** `StartupRecovery`
**Key Functions:** None (all methods)
**Imports from project:** None
**Called by:** `main.py` (`startup_recovery.scan_orphaned_trades()` at init; `_restart_recovery` callback in `_supervised`)
**Calls into:** Fyers REST (positions endpoint)
**State it owns:** `fyers` client reference
**Error handling:** Errors caught and logged; non-fatal
**Notes:** 1443 bytes. Runs synchronously at startup before TaskGroup.

---

### async_utils.py
**Role:** Asyncio utility helpers.
**Key Classes:** None
**Key Functions:** (inspect file for specifics)
**Imports from project:** None
**Called by:** <!-- AUDIT NOTE: unclear — verify consumers -->
**Calls into:** `asyncio`
**State it owns:** None
**Notes:** 1728 bytes.

---

### emergency_logger.py
**Role:** Emergency alert logger — writes critical failure events to `logs/emergency_alerts.log` and `logs/orphaned_positions.log`.
**Key Classes:** <!-- AUDIT NOTE: unclear — verify class name -->
**Key Functions:** (emergency log write)
**Imports from project:** None
**Called by:** `fyers_broker_interface.py` (passed as `emergency_logger` param)
**Calls into:** `logging`, file I/O
**State it owns:** Log file path
**Notes:** 3653 bytes.

---

### diagnostic_analyzer.py
**Role:** `/why` command engine — reruns the full 12-gate analysis on a symbol and returns a gate-by-gate pass/fail breakdown to Telegram.
**Key Classes:** <!-- AUDIT NOTE: verify class name -->
**Key Functions:** (diagnostic run)
**Imports from project:** `config`, Fyers client
**Called by:** `telegram_bot.py` (`/why` command handler)
**Calls into:** `analyzer.py` gates, Fyers REST
**State it owns:** None (stateless per call)
**Notes:** 35657 bytes. Largest file after `telegram_bot.py`. Writes to `logs/diagnostic_analysis.csv`.

---

### htf_confluence.py
**Role:** Higher-Time-Frame confluence checks — 15-minute Lower High structure confirmation.
**Key Classes:** <!-- AUDIT NOTE: verify -->
**Key Functions:** (HTF check)
**Imports from project:** None
**Called by:** `analyzer.py` (`_finalize_signal`)
**Calls into:** Fyers REST (15m candle history)
**Notes:** 7789 bytes.

---

### market_profile.py
**Role:** Market Profile / Value Area calculation — VAH, VAL, POC from 1-minute OHLCV data.
**Key Classes:** `ProfileAnalyzer`
**Key Functions:** (profile calculation)
**Imports from project:** None
**Called by:** `analyzer.py`
**Calls into:** `pandas`, `numpy`
**Notes:** 7446 bytes.

---

### tape_reader.py
**Role:** Order flow / tape reading — detects trapped longs, absorption signals, bad highs from tick + depth data.
**Key Classes:** `TapeReader`
**Key Functions:** (tape analysis)
**Imports from project:** None
**Called by:** `analyzer.py`
**Calls into:** `pandas`, `numpy`
**Notes:** 11446 bytes.

---

### scalper_position_manager.py
**Role:** Phase 41.2 scalper position management — SL state machine (INITIAL→BREAKEVEN→TRAILING→TIGHTENING), TP scale-out logic.
**Key Classes:** (verify class name)
**Key Functions:** (SL update, partial exit triggers)
**Imports from project:** `config`
**Called by:** `trade_manager.py` or `focus_engine.py` focus loop
**Notes:** 10598 bytes. `USE_SCALPER_RISK_MANAGEMENT=False` — feature-flagged off by default.

---

### scalper_risk_calculator.py
**Role:** Risk sizing calculations for scalper system — tick-based SL distance, ATR-based sizing.
**Key Classes:** (verify)
**Key Functions:** (sizing calc)
**Imports from project:** `config`
**Called by:** `scalper_position_manager.py`
**Notes:** 4014 bytes.

---

### discretionary_engine.py
**Role:** Phase 41.3 intelligent exit engine — evaluates market regime + order flow for soft-stop / target extension decisions.
**Key Classes:** `DiscretionaryEngine`
**Key Functions:** (exit evaluation)
**Imports from project:** `config`, `market_context.MarketContext`
**Called by:** `focus_engine.py` (injected as `discretionary_engine`)
**Notes:** 6819 bytes.

---

### discretionary_signals.py
**Role:** Discretionary exit signal catalogue — definitions of soft-stop, hard-stop, target-extension triggers.
**Key Classes:** (verify)
**Key Functions:** (signal definitions)
**Imports from project:** `config`
**Called by:** `discretionary_engine.py`
**Notes:** 9505 bytes.

---

### journal_manager.py
**Role:** Trade journal writer — appends trade records to `data/trade_journal.csv` for human review.
**Key Classes:** (verify)
**Key Functions:** (journal append)
**Imports from project:** None
**Called by:** `trade_manager.py` or `order_manager.py`
**Notes:** 5928 bytes.

---

### position_reconciliation.py
**Role:** Lightweight position cross-check utility (separate from `ReconciliationEngine`).
**Key Classes:** (verify)
**Key Functions:** (position check)
**Imports from project:** None
**Called by:** <!-- AUDIT NOTE: unclear — verify -->
**Notes:** 4657 bytes.

---

### detector_performance_tracker.py
**Role:** Phase 41.1 detector analytics — logs per-detector hit rate and P&L correlation to `logs/detector_performance.csv`.
**Key Classes:** (verify)
**Key Functions:** (track + log)
**Imports from project:** `config`
**Called by:** `analyzer.py` (when `ENABLE_DETECTOR_TRACKING=True`)
**Notes:** 6930 bytes.

---

### trade_simulator.py
**Role:** Offline trade simulation engine — replays signals on historical candles to compute simulated P&L for EOD analysis.
**Key Classes:** `TradeSimulator`
**Key Functions:** (simulate)
**Imports from project:** `config`
**Called by:** `eod_analysis.py`
**Notes:** 10461 bytes.

---

### apply_migration.py
**Role:** One-time migration runner — applies `migrations/v42_1_0_postgresql.sql` to the database.
**Key Classes:** None
**Key Functions:** (main migration run)
**Imports from project:** `database.DatabaseManager`
**Called by:** Run manually once (`python apply_migration.py`)
**Notes:** 1434 bytes.

---

### dump_terminal_log.py
**Role:** Utility — extracts session log lines from `logs/bot.log` and writes to `md/terminal_log.md`.
**Key Classes:** None
**Key Functions:** (log extract)
**Imports from project:** None
**Called by:** `eod_analysis.py` (`_generate_session_log`), manual run
**Notes:** 1636 bytes.

---

### eod_why.py
**Role:** EOD diagnostic utility — post-session analysis of why specific signals passed or failed gates.
**Key Classes:** (verify)
**Key Functions:** (why analysis)
**Imports from project:** `config`, `fyers_connect.FyersConnect`
**Called by:** Manual CLI run
**Notes:** 5126 bytes.

---

## SECTION 3 — Subdirectory Map

### /tests/
**Purpose:** Pytest test suite — unit and integration tests for all major components.
**Files:**
- `conftest.py` — Shared fixtures
- `test_phase44_ux.py` — 25 Phase 44.4 UX tests (passes in CI)
- `test_websocket_integration.py` — WS integration tests including `test_order_manager_backward_compatible`
- `test_eod_scheduler.py` — EOD scheduler tests
- `test_eod_flow.py`, `test_eod_flow_integration.py` — EOD execution flow tests (Note: datetime casting fix; suite 79/79)
- `test_eod_fixes.py`, `test_eod_analyzer_contract.py` — EOD analyzer contract tests
- `test_market_session.py` — MarketSession state machine tests
- `test_safety.py` — Position safety / circuit breaker tests
- `test_supervisor.py` — Supervisor/TaskGroup tests
- `test_safe_exit_race.py` — OrderManager race condition tests
- `test_database_query_contract.py`, `test_db_pool.py` — DB contract tests
- `test_config_imports.py` — Config smoke test
- `test_symbols.py` — Symbol validation tests
- `test_async_integration.py` — Async integration smoke tests
- `test_fix.py` — Regression tests
- `test_phase44_5_editable_flow.py` — Phase 44.5 editable flow tests
- `test_telegram_shutdown_contract.py` — Telegram shutdown contract
- `test_gap_v2_1.py` — PRD v2.1 gap closure tests: `test_morning_range_mid_market_start`, `test_cache_seed_reduces_missing_count`
- `test_candle.py` — Live candle API integration test (requires Fyers auth — **skipped in CI** via `pytestmark = pytest.mark.skip`)
**Consumed by:** `pytest` (run via `pytest -q`). Current suite: **81 passed, 1 skipped** (`test_candle.py` requires live auth). All non-auth tests run without flags.

---

### /migrations/
**Purpose:** PostgreSQL schema migration SQL scripts.
**Files:**
- `v42_1_0_postgresql.sql` — Phase 42.1 schema: creates `orders`, `positions`, `reconciliation_log` tables with indexes and `update_updated_at` trigger.
**Consumed by:** `apply_migration.py` (manual run)

---

### /data/
**Purpose:** Runtime data storage — access token, SQLite fallback DB, trade journal, ML observations.
**Files:**
- `access_token.txt` — Fyers OAuth access token (written by `fyers_connect.py`)
- `short_circuit.db` — SQLite DB (legacy — primary store is PostgreSQL)
- `trade_journal.csv` — Human-readable trade journal
- `.gitkeep` — Ensures folder committed to repo
- `/data/ml/` — Daily ML observation parquet files (`data{YYYY-MM-DD}.parquet`, `training_data.parquet`)
**Consumers:** `fyers_connect.py` (read/write token), `ml_logger.py` (write parquet), `journal_manager.py` (write CSV)

---

### /logs/
**Purpose:** Runtime log files — rotating bot log, signal CSV, diagnostic CSV, EOD simulation CSV, emergency alerts.
**Files (generated at runtime — not committed):**
- `bot.log` — Primary rotating log (10MB × 5 backups, `RotatingFileHandler`)
- `signals.csv` — All signal events (executed + skipped). Added Phase 44.8 columns (stretch_score, vol_fade_ratio, confidence, pattern_bonus, oi_direction)
- `detector_performance.csv` — Per-detector hit rate tracking
- `eod_simulation.csv` — EOD simulation results
- `emergency_alerts.log` — Critical failure events
- `orphaned_positions.log` — Orphaned position discoveries
- `diagnostic_analysis.csv` — `/why` command runs
- `/logs/fyers_rest/` — Fyers SDK REST call logs
**Consumers:** `main.py` (`RotatingFileHandler`), `analyzer.py` (signals.csv), `detector_performance_tracker.py`, `eod_analysis.py`, `emergency_logger.py`, `diagnostic_analyzer.py`

---

### /md/
**Purpose:** Markdown output files — terminal log extracted by `dump_terminal_log.py`.
**Files:**
- `terminal_log.md` — Last session log summary (written by `eod_analysis.py` / `dump_terminal_log.py`)
**Consumers:** `eod_analysis.py` writes; human reads

---

### /tools/
**Purpose:** Auth utilities — standalone scripts for manual token management.
**Files:**
- `get_auth_url.py` — Prints Fyers OAuth URL for manual browser auth
- `set_token.py` — Writes access token to `data/access_token.txt`
**Consumed by:** Operator (manual run only)

---

### /verification/
**Purpose:** Verification scripts (contents not inventoried — 0 Python files found in listing).
**Consumers:** Developer use

---

## SECTION 4 — Full Dependency Graph (Module Level)

```
main.py
  ├── config.py
  ├── fyers_connect.py
  │     └── fyers_apiv3 (external SDK)
  ├── fyers_broker_interface.py
  │     ├── fyers_apiv3 + fyers_apiv3.FyersWebsocket (external)
  │     └── database.py
  ├── order_manager.py
  │     ├── fyers_broker_interface.py
  │     ├── database.py
  │     ├── capital_manager.py
  │     └── telegram_bot.py (alerts)
  ├── capital_manager.py
  ├── trade_manager.py
  │     ├── config.py
  │     └── capital_manager.py
  ├── focus_engine.py
  │     ├── fyers_connect.py
  │     ├── config.py
  │     ├── order_manager.py
  │     └── discretionary_engine.py
  │           ├── config.py
  │           ├── market_context.py
  │           └── discretionary_signals.py
  ├── telegram_bot.py
  │     ├── config.py
  │     ├── capital_manager.py
  │     ├── focus_engine.py
  │     ├── signal_manager.py
  │     └── diagnostic_analyzer.py
  ├── scanner.py
  │     └── fyers_connect.py
  ├── analyzer.py
  │     ├── config.py
  │     ├── market_context.py
  │     │     ├── symbols.py
  │     │     └── config.py
  │     ├── signal_manager.py
  │     ├── god_mode_logic.py
  │     ├── tape_reader.py
  │     ├── market_profile.py
  │     ├── ml_logger.py
  │     └── multi_edge_detector.py (when MULTI_EDGE_ENABLED=True)
  │           └── config.py
  ├── market_session.py
  │     ├── config.py
  │     └── symbols.py
  ├── reconciliation.py
  │     ├── fyers_broker_interface.py
  │     └── database.py
  ├── eod_scheduler.py          ← no project imports (callbacks injected)
  ├── eod_watchdog.py           ← no project imports [NEW — Bug 2A]
  ├── eod_analyzer.py
  │     ├── database.py
  │     └── fyers_connect.py
  ├── database.py               ← asyncpg (external)
  ├── startup_recovery.py
  └── market_utils.py
```

---

## SECTION 5 — RuntimeContext Dataclass

Defined at `main.py:138`. Assembled in `_initialize_runtime()`. Passed into every TaskGroup task.

| Field | Type | Initialized at | Used by |
|---|---|---|---|
| `fyers_client` | `fyersModel.FyersModel` | `FyersConnect(config).fyers` | `scanner`, `analyzer`, `market_session` |
| `access_token` | `str` | `FyersConnect(config).access_token` | `fyers_broker_interface` constructor |
| `capital_manager` | `CapitalManager` | `CapitalManager(base=1800, lev=5.0)` | `order_manager`, `trade_manager`, `telegram_bot` |
| `trade_manager` | `TradeManager` | `TradeManager(fyers, capital_manager)` | `focus_engine`, EOD square-off |
| `focus_engine` | `FocusEngine` | `FocusEngine(trade_manager)` | `_trading_loop`, `telegram_bot` |
| `bot` | `ShortCircuitBot` | `ShortCircuitBot(config, None, capital_manager, focus_engine)` | All alert/command paths |
| `market_session` | `MarketSession` | `MarketSession(fyers_client, bot)` | `_trading_loop` gating |
| `scanner` | `FyersScanner` | `FyersScanner(fyers_client)` | `_trading_loop` |
| `analyzer` | `FyersAnalyzer` | `FyersAnalyzer(fyers_client, mh, ml)` | `_trading_loop` |
| `db_manager` | `DatabaseManager` | `DatabaseManager()` + `await initialize()` | `order_manager`, `reconciliation` |
| `broker` | `FyersBrokerInterface` | `FyersBrokerInterface(token, client_id, db, None)` + `await initialize()` | `order_manager`, `reconciliation` |
| `order_manager` | `OrderManager` | `OrderManager(broker, bot, db, capital_manager)` **[P0 FIX]** | `focus_engine`, `telegram_bot` |
| `reconciliation_engine` | `ReconciliationEngine` | `ReconciliationEngine(broker, db, bot)` | TaskGroup, `_cleanup_runtime` |
| `startup_recovery` | `StartupRecovery` | `StartupRecovery(fyers_client)` | `_initialize_runtime` + restart callback |

---

## SECTION 6 — Startup Sequence (Exact Order)

| # | Step | REST/WS | Blocking? | On Failure |
|---|------|---------|-----------|------------|
| 1 | `_configure_logging()` — RotatingFileHandler + console | — | Yes | sys.exit |
| 2 | `asyncio.Event` `shutdown_event` created | — | — | — |
| 3 | Signal handlers for `SIGINT`/`SIGTERM` → `shutdown_event.set()` | — | — | — |
| 4 | `FyersConnect(config)` — loads token from `data/access_token.txt`; validates via REST `get_profile()` | REST | Yes | `RuntimeError` |
| 5 | `CapitalManager(base=1800, leverage=5.0)` constructed | — | — | — |
| 6 | `StartupRecovery(fyers_client).scan_orphaned_trades()` — REST positions endpoint | REST | Yes | Logged, non-fatal |
| 7 | `TradeManager(fyers_client, capital_manager)` constructed | — | — | — |
| 8 | `FocusEngine(trade_manager)` — **`order_manager=None` at this point** | — | — | — |
| 9 | `ShortCircuitBot(config, None, capital_manager, focus_engine)` constructed; PTB Application built | — | — | — |
| 10 | `trade_manager.bot = bot`, `focus_engine.telegram_bot = bot` wired | — | — | — |
| 11 | `MarketSession(fyers_client, bot).initialize_session()` — determines session state, fetches NIFTY 9:15–9:30 range | REST | Yes | Fallback ±0.5% range |
| 12 | `FyersScanner(fyers_client)` constructed | — | — | — |
| 13 | `FyersAnalyzer(fyers_client, morning_high, morning_low)` constructed | — | — | — |
| 14 | `DatabaseManager()` + `await db_manager.initialize()` — asyncpg pool created | DB TCP | Yes | `RuntimeError` |
| 15 | `FyersBrokerInterface(token, client_id, db, None)` constructed | — | — | — |
| 16 | `await broker.initialize()` — Data WS + Order WS launched in daemon threads | WebSocket | ~2s | `RuntimeError` if WS unavailable |
| 16.4 | `broker.seed_from_rest(scanner_symbols)` — seeds REST snapshot prices for all symbols into WS cache as `REST_SEED` entries; prevents `Missing:` inflation on cold/late start | REST | ~2–5s | Warning log if <90% seeded; non-fatal |
| 16.5 | `broker.subscribe_scanner_universe()` → cache state → PRIMING; health monitor thread starts | WebSocket | — | — |
| 16.6 | **[PRD-007]** `await asyncio.to_thread(broker.wait_for_cache_ready, 45.0)` — **BLOCKS** until ≥ 85% symbols have tick or 45s timeout | WebSocket | up to 45s | CRITICAL log + Telegram alert + REST fallback |
| 16.7 | **[PRD-008]** `get_gate_result_logger().set_dsn(db_dsn)` — enables periodic 100-record flush to `gate_results` table | — | — | — |
| 17 | **[P0 FIX]** `OrderManager(broker, bot, db, capital_manager)` constructed | — | — | — |
| 18 | **[P0 FIX]** `focus_engine.order_manager = order_manager` injected | — | — | — |
| 19 | **[P0 FIX]** `bot.order_manager = order_manager` injected | — | — | — |
| 20 | `ReconciliationEngine(broker, db, bot)` constructed | — | — | — |
| 21 | `RuntimeContext` assembled | — | — | — |
| 22 | **[P0 FIX]** `_validate_dependencies(ctx)` — hard crash + Telegram alert if any critical dep is `None` | — | — | `RuntimeError` |
| 22.1 | **[PRD v2.0]** Startup Validation Gate — `_run_startup_validation()`: candle API smoke test (1-min NIFTY candle) → HARD HALT on failure; DB pool ping → HARD HALT on failure; WS cache readiness → SOFT WARN only | REST + DB | Yes | Candle/DB failure: `SystemExit(1)` + Telegram CRITICAL alert; WS failure: WARNING + continues |
| 22.2 | **[PRD v2.2]** Auto queue check — if `_auto_on_queued=True`, activate `auto_mode=True` immediately; then `_send_morning_briefing()` fires once with full system status | — | — | — |
| 23 | `asyncio.TaskGroup` started with 5 async tasks (see below) | — | Async | TaskGroup propagates exception |

**TaskGroup tasks:**

| Task name | Coroutine | Description |
|-----------|-----------|-------------|
| `trading_loop` | `_trading_loop(ctx, shutdown_event)` | Main scan → analyze → signal → execute loop |
| `telegram_bot` | `ctx.bot.run(shutdown_event)` | PTB polling loop |
| `reconciliation` | `ctx.reconciliation_engine.run(shutdown_event)` | Periodic position reconciliation |
| `eod_scheduler` | `eod_scheduler(shutdown_event, ...)` | 15:10 square-off + 15:32 analysis |
| `eod_watchdog` | `eod_watchdog(shutdown_event)` | **[Bug 2A NEW]** Failsafe shutdown at 15:32/15:40 |

---

## SECTION 7 — Complete Signal-to-Trade Execution Flow

### Phase 44.8 — Async Execution Fix (2026-03-04)

```
Root Cause:
- enter_position() in order_manager.py is defined as async def
- Call site in focus_engine.py:check_pending_signals was missing await
- Python silently returned a coroutine object instead of executing the function
- Coroutine is truthy → entered if pos: block → crashed on pos.get()
- Result: Zero trades placed from January 2026 to March 2026

Fix Applied:
- focus_engine.py L305: pos = await self.order_manager.enter_position(...)
- check_pending_signals converted from def → async def
- monitor_pending_loop converted from def → async def with await asyncio.sleep()
- start_pending_monitor() now uses asyncio.create_task() as primary launch
- Sync fallback _monitor_pending_loop_sync retained for no-event-loop edge cases
  with explicit loop passed as argument (Python 3.12 compatibility)
- self.fyers.quotes() inside check_pending_signals wrapped in
  await asyncio.to_thread() to prevent event loop blocking

Impact: This was the singular reason for 2 months of zero trade execution.
```
### PHASE 1 — MARKET SCANNING
**File:** `scanner.py` — `FyersScanner.scan_market()`
**Transport:** REST only — Fyers quotes endpoint (batches of 50 symbols)
**Interval:** ~60 seconds (main trading loop sleep)

| Step | Action |
|------|--------|
| 1.1 | `fetch_nse_symbols()` — downloads NSE EQ master CSV, filters for EQ series (~2418 symbols) |
| 1.2 | Check WS quote cache (<60s old); fall back to Batch REST requests (50 symbols/call) if empty/stale |
| 1.3 | Pre-filter per symbol: gain% 6–18%, volume >100k, LTP >₹5, tick size valid, OI >0 |
| 1.4 | Parallel `fetch_quality()` for all candidates (up to `SCANNER_PARALLEL_WORKERS=3` threads) |
| 1.5 | `check_chart_quality()` — last 60 min 1-min candles: reject if >50% zero-volume or >50% doji candles |
| 1.6 | Return filtered candidate list to `_trading_loop` |

### PHASE 2 — SIGNAL DETECTION (GOD MODE)
**File:** `analyzer.py` — `FyersAnalyzer.check_setup()`
**Transport:** REST (1-min candle history fetched in Phase 1); WebSocket tick cache for LTP

| Step | Action |
|------|--------|
| 2.1 | `_check_filters()` → `MarketContext.should_allow_short()` — regime check (TREND_UP blocks) |
| 2.2 | RVOL validity gate: skip if <20 min since 9:15 open AND `RVOL_VALIDITY_GATE_ENABLED=True` |
| 2.3 | `GodModeAnalyst.is_exhaustion_at_stretch()` — evaluates exhaustion at high (no breakdown required). Old patterns now just bonus scorers |
| 2.4 | `GodModeAnalyst.calculate_vwap_slope()` — must be FLAT (slope <0.05) |
| 2.5 | `GodModeAnalyst.calculate_vwap_bands()` — LTP must be ≥1 SD above VWAP |
| 2.6 | `_check_circuit_guard()` — blocked if price within upper circuit proximity |
| 2.7 | `_is_momentum_too_strong()` — blocked if slope >0.1 |
| 2.8 | `_check_sniper_zone()` — price must be at micro-range top |
| 2.9 | `_check_pro_confluence()` — DPOC divergence, OI divergence, tape signals (TapeReader + ProfileAnalyzer) |
| 2.10 | `htf_confluence.py` — 15m Lower High structure via REST 15m candles |
| 2.11 | `signal_manager.can_signal(symbol)` — daily limit (5) + 45-min cooldown + pause gate |
| 2.12 | All pass → **[PRD-008]** `GateResult` object finalized with `verdict=ANALYZER_PASS`, forwarded to focus_engine via `finalized['_gate_result'] = gr` |
| 2.13 | **[PRD-009 fix]** `signal_manager.record_signal()` is NOT called here — slot burned only at `enter_position()` success in `focus_engine.py` |
| 2.14 | `ml_logger.log_observation()` (returns UUID obs_id), `log_signal()` to CSV |
| 2.15 | Log line: `"GOD MODE SIGNAL {symbol}"` |
| 2.16 | Telegram: signal discovery alert (pattern, entry, SL, signals remaining) |
| 2.17 | `focus_engine.add_pending_signal(signal_data)` → enters Phase 3 validation |

### PHASE 3 — GATE 12 PRICE VALIDATION
**File:** `focus_engine.py` — `add_pending_signal()` + `check_pending_signals()`
**Transport:** Data WebSocket (real-time tick — NOT REST polling)

| Step | Action |
|------|--------|
| 3.1 | Signal added to `focus_engine.pending_signals` dict keyed by symbol |
| 3.2 | `start_pending_monitor()` — spawns `monitor_thread` (daemon thread, 2s loop) if not running |
| 3.3 | `monitor_pending_loop()` calls `check_pending_signals(trade_manager)` every 2 seconds |
| 3.4 | Each check: compare LTP (broker tick cache) vs `entry_trigger` price |
| 3.5 | If LTP breaks below trigger within `VALIDATION_TIMEOUT_MINUTES=15`: **VALIDATED** |
| 3.6 | Telegram: `queue_signal_validation_update(VALIDATED)` |
| 3.7 | If 15-min timeout fires first: signal removed, Telegram `TIMEOUT` alert |

### PHASE 4 — ORDER EXECUTION
**File:** `order_manager.py` — `OrderManager.enter_position(signal)`
**Transport:** REST for submission; WebSocket for fill confirmation

| Step | Action |
|------|--------|
| 4.1 | `capital_manager.can_afford(symbol, ltp * qty)` — buying power check |
| 4.2 | Quantity: `math.floor(capital_manager.available / ltp)` |
| 4.3 | `broker.place_order(...)` — REST POST: SELL, INTRADAY, MARKET type |
| 4.4 | `order_id` returned; added to `pending_orders` dict |
| 4.5 | SL-M order placed immediately via second REST call |
| 4.6 | `capital_manager.allocate(symbol, cost)` called (capital reserved) |
| 4.7 | `db_manager.log_trade_entry(data)` — atomic write to `orders` + `positions` tables |
| 4.8 | REST returns immediately — **do not block** |
| 4.9 | Order WebSocket fires `on_order_update()` when broker status = 2 (TRADED) |
| 4.10 | `order_manager` matches `order_id`, updates position with `avg_filled_price` |
| 4.11 | Telegram: execution confirmation + pre-execution payload logged |
| 4.12 | `focus_engine.start_focus(symbol, position_data)` — `active_trade` set |
| 4.13 | Telegram live dashboard starts (2s refresh loop via `_dashboard_refresh_loop`) |

### PHASE 5 — POSITION MANAGEMENT
**File:** `focus_engine.py` — `focus_loop()`
**Transport:** Data WebSocket (tick feed for LTP updates); REST for exit orders

| Step | Action |
|------|--------|
| 5.1 | `focus_loop()` runs in `monitor_thread`; polls LTP every 2 seconds |
| 5.2 | `order_manager.monitor_hard_stop_status(symbol)` checks if SL-M hit (broker orderbook REST poll) |
| 5.3 | If hard-stop fill detected → `_finalize_closed_position()`: release capital, update DB, clear `active_trade` |
| 5.4 | `discretionary_engine` evaluates soft-stop / target-extension signals each cycle |
| 5.5 | TP1/TP2/TP3 at 1.5%/2.5%/3.5%: `trade_manager.close_partial_position()` → REST sell order |
| 5.6 | Telegram dashboard refreshes every 2s with unrealized P&L via `get_position_snapshot()` |
| 5.7 | On full exit: `capital_manager.release(symbol)`, `db_manager.log_trade_exit()`, Telegram final P&L alert |
| 5.8 | `sfp_watch_loop()` runs 10 min post-exit watching for Sweep-and-Flip pattern |

---

## SECTION 8 — Gate Pipeline (G1–G12)

> **IMPORTANT:** Gate numbering was unified in Phase 44.9 (PRD-008). G1–G9 run in `analyzer.py`. G10–G12 run in `focus_engine.py`. Legacy docs described «Gate 1–12» with different semantics — the table below is the authoritative current definition.

| Gate | ID | Location | What It Checks | Fail Behaviour |
|------|----|----------|----------------|----------------|
| G1 | PROXIMITY_AND_GAIN | analyzer.py | check_setup() → gmanalyst.check_constraints() |
|    | Checks:                                                                              |
|    | 1. trend_gain >= 5.0% OR max_gain_pct (day_high vs open) >= 7.0%                   |
|    | 2. trend_gain <= 15.0% (circuit risk ceiling)                                        |
|    | 3. dist_from_high = (day_high - ltp) / day_high * 100 <= 2.5%                       |
|    |    (3.5% if max_gain_pct > 10.0%)                                                    |
|    | NOTE: Primary rejection cause is rule 3 (proximity), NOT rule 1/2 (gain range).     |
|    | Stocks failing G1 as "GAINCONSTRAINTS" are almost always too far from their high.    |
| RVOL Validity | G2 | `analyzer.py / check_setup()` | ≥20 min since market open when `RVOL_VALIDITY_GATE_ENABLED` | grl.record(REJECTED) |
| Circuit Guard | G3 | `analyzer.py / check_setup()` | Not near upper circuit | grl.record(REJECTED) |
| Momentum | G4 | `analyzer.py / check_setup()` | VWAP slope not too steep | grl.record(REJECTED) |
| Exhaustion at Stretch | G5 | `analyzer.py / check_setup()` | gain 9–14.5%, new high, vol_fade < 0.65, close > VAH | grl.record(REJECTED) |
| Pro Confluence | G6 | `analyzer.py / check_setup()` | DPOC + OI divergence + tape signals | grl.record(REJECTED) |
| Market Regime | G7 | `analyzer.py / check_setup()` | NIFTY regime allows short | grl.record(REJECTED) |
| Signal Manager | G8 | `analyzer.py / check_setup()` | Daily limit (5) + 45-min cooldown + loss pause | grl.record(REJECTED) |
| HTF Confluence | **G9** | `analyzer.py / check_setup()` | 15m Lower High structure (REST 15m candles) | grl.record(REJECTED) |
| Cooldown Spacing | G10 | `focus_engine.py` | Inter-signal minimum gap | grl.record(REJECTED) |
| Capital Availability | G11 | `focus_engine.py` | `capital_manager.can_afford()` | grl.record(REJECTED) |
| Pre-Entry Conviction | G12 | `focus_engine.py` | LTP broke trigger / invalidated / timeout | grl.record(REJECTED or SUPPRESSED or SIGNAL_FIRED) |

Gates execute in order. First failure terminates evaluation immediately and records to `GateResultLogger`.

**Note on G9 (HTF):** Prior to Phase 44.9, this check lived in `_finalize_signal()` and had zero audit trail (silent `return None`). It was promoted to G9 in `check_setup()` so all rejections produce a `GateResult` record. See SECTION 17 for full PRD-008 audit trail detail.

**Note on G12 terminal states:**
- `SIGNAL_FIRED` — LTP broke trigger, `enter_position()` succeeded, `record_signal()` called
- `REJECTED / G12_INVALIDATED_PRE_ENTRY` — LTP hit stop before trigger
- `REJECTED / G12_TIMEOUT` — trigger not broken within `VALIDATION_TIMEOUT_MINUTES`
- `SUPPRESSED` — auto mode OFF; operator alerted manually
- `DATA_ERROR` — `order_manager` was None at execution time (startup failure)

---

### SL State Machine

| State | Transition Trigger | SL Position | File | Log |
|-------|--------------------|-------------|------|-----|
| `INITIAL` | Entry fill confirmed | `setup_high + SCALPER_STOP_TICK_BUFFER (12 ticks)` | `scalper_risk_calculator.py` | `SL set @ {price}` |
| `BREAKEVEN` | Price moves `SCALPER_BREAKEVEN_TRIGGER_PCT=0.3%` in favor | Moved to entry price | `scalper_position_manager.py` | `BREAKEVEN: SL → {entry}` |
| `TRAILING` | Price continues in favor beyond breakeven | Trails by 0.2% (0.15% after TP1, 0.1% after TP2) | `scalper_position_manager.py` | `TRAILING SL → {price}` |
| `HARD_STOP` | SL-M order fills at broker | Position force-closed | `order_manager.monitor_hard_stop_status()` | `Hard stop triggered` |

> **Note:** `USE_SCALPER_RISK_MANAGEMENT=False` — the scalper SL state machine is feature-flagged off. Active production SL uses `TradeManager.update_stop_loss()`.

---

### TP Structure / Exit Paths

| Exit Path | Trigger | Exit Size | Config Key | File / Method | Transport |
|-----------|---------|-----------|-----------|---------------|-----------|
| TP1 | +1.5% from entry | 50% of position | `SCALPER_TP1_PCT=0.015` | `trade_manager.close_partial_position()` | REST |
| TP2 | +2.5% from entry | 25% more (75% total closed) | `SCALPER_TP2_PCT=0.025` | `trade_manager.close_partial_position()` | REST |
| TP3 | +3.5% from entry | All remaining | `SCALPER_TP3_PCT=0.035` | `trade_manager.close_partial_position()` | REST |
| EOD_SQUAREOFF | 15:10 IST (hard wall) | All open positions | `SQUARE_OFF_TIME=15:10` | `trade_manager.close_all_positions()` via `eod_scheduler` | REST |
| SOFT_STOP | Discretionary engine: regime reversal or OF deterioration (score ≥2) | Full exit | `DISCRETIONARY_CONFIG.soft_stop_pct=0.5%` | `discretionary_engine.py` → `order_manager.safe_exit()` | REST |
| EMERGENCY | Telegram `/emergency` command | All positions immediately | — | `telegram_bot.py` → `trade_manager.emergency_exit()` | REST |

Exit fill confirmation for all paths: **Order WebSocket** `on_order_update()` → `order_manager` processes fill, releases capital, updates DB.

---

## SECTION 9 — WebSocket vs REST Decision Matrix

| Operation | Transport | File | Method | Why |
|---|---|---|---|---|
| NSE symbol universe download | REST (NSE public CSV) | `scanner.py` | `fetch_nse_symbols()` | Static bulk list |
| Quote fetch (2418 symbols, 50/call) | WebSocket cache first → REST fallback | `scanner.py` | `scan_market()` batch loop | WS cache reduces REST calls drastically |
| 1-min candle history (chart quality) | REST | `scanner.py` | `check_chart_quality()` | Historical candles needed |
| 1-min candle history (analysis) | REST | `analyzer.py` | `FyersAnalyzer.get_history()` | Batch snapshot |
| 15-min candle history (HTF) | REST | `htf_confluence.py` | HTF check | Historical HTF data |
| NIFTY morning range (9:15–9:30) | REST | `market_session.py` | `_fetch_morning_range()` | One-time startup fetch |
| NIFTY regime check | REST | `market_context.py` | `_get_index_data()` | Intraday regime assessment |
| Gate 12 price monitoring | WebSocket (Data WS) | `fyers_broker_interface.py` → `focus_engine.py` | tick callback → `check_pending_signals()` | Real-time tick required |
| Live dashboard LTP (2s refresh) | WebSocket (Data WS) | `telegram_bot.py` | `_dashboard_refresh_loop` | Zero-latency P&L |
| Position LTP in focus loop | WebSocket (Data WS) | `focus_engine.py` | `focus_loop()` | Continuous monitoring |
| Order submission (entry + SL-M) | REST | `fyers_broker_interface.py` | `place_order()` | Fyers API requires REST for new orders |
| Partial exit (TP1/TP2/TP3) | REST | `trade_manager.close_partial_position()` | `place_order()` | Order submission always REST |
| Emergency exit | REST | `trade_manager.emergency_exit()` | `place_order()` | Same |
| EOD square-off | REST | `trade_manager.close_all_positions()` | `place_order()` | Forced close |
| Order fill confirmation | WebSocket (Order WS) | `fyers_broker_interface.py` → `order_manager.py` | `on_order_update()` | 10–50ms fill notification |
| Hard-stop SL fill detection | WebSocket (Order WS) + REST fallback | `order_manager.monitor_hard_stop_status()` | broker orderbook poll | SL-M fill detection |
| Token validation at startup | REST | `fyers_connect.py` | `_validate_token()` → `get_profile()` | Verify auth before trading |
| Reconciliation (position check) | WebSocket cache first → REST fallback | `reconciliation.py` | `_get_broker_positions_cached()` | Zero-cost when flat |
| EOD P&L report data | PostgreSQL (DB) | `eod_analyzer.py` | `db.get_today_trades()` | Post-close source-of-truth |
| Orphaned position scan at startup | REST | `startup_recovery.py` | `scan_orphaned_trades()` | Broker positions endpoint |

---

## SECTION 10 — Error Handling Map

### main.py
| Failure | Handler | Behaviour |
|---|---|---|
| Fyers auth fails | `RuntimeError` in `_initialize_runtime` | Bot exits; no TaskGroup started |
| Any critical dep is `None` | `_validate_dependencies()` raises `RuntimeError` [P0 FIX] | Hard crash + Telegram alert before any trading |
| TaskGroup task crashes | `_supervised()` wrapper — exponential retry up to `max_retries=5` | Restarts crashed task; propagates after limit |
| `except* Exception` on TaskGroup | Critical log + `exit_code = 1` | `finally` block always runs `_cleanup_runtime` |
| Cleanup step hangs | `asyncio.wait_for(step, timeout)` [Bug 2B FIX] | Hard timeout per step; logs WARNING; never hangs forever |

### fyers_broker_interface.py
| Failure | Handler | Behaviour |
|---|---|---|
| Data WS disconnect | Fyers SDK auto-reconnect | No custom logic — SDK handles internally |
| Order WS disconnect | Fyers SDK auto-reconnect | `_on_order_ws_connect` re-subscribes on reconnect |
| DNS error (`getaddrinfo`, errno 11001) | SDK retries; `telegram_bot._error_handler` suppresses [Bug 3 FIX] | Recovers in ~2s per session log |
| REST `place_order` fails (code -50 etc.) | try/except in `order_manager.enter_position()` | Logs ERROR with full payload; Telegram failure alert; capital not deducted |
| WS modules not importable (setuptools) | `_WS_AVAILABLE = False` at module load | Bot runs in REST-only fallback mode |

### order_manager.py
| Failure | Handler | Behaviour |
|---|---|---|
| Broker rejects entry order | try/except; error captured | Logs `code`, `message`; Telegram alert with payload; position not opened |
| `order_manager` is `None` in focus_engine | `raise RuntimeError` [P0 FIX] | Hard crash — replaces silent legacy fallback path |
| Capital insufficient | `can_afford()` returns False | Log + Telegram alert; no order; `available` unchanged |
| WS fill not received | `monitor_hard_stop_status()` polls broker orderbook | REST fallback fill detection |
| Concurrent exit race condition | Per-symbol `asyncio.Lock` | Second caller waits; duplicate exits prevented |

### focus_engine.py
| Failure | Handler | Behaviour |
|---|---|---|
| `order_manager is None` at execution | `raise RuntimeError` [P0 FIX] | Hard crash (was previously a silent no-op) |
| `attempt_recovery()` at startup | try/except; logged | Non-fatal; startup continues |
| Signal monitor thread crashes | Thread exception logged | Restarts on next `start_pending_monitor()` |
| Validation timeout | Signal removed from `pending_signals` | Telegram TIMEOUT alert |

### telegram_bot.py
| Failure | Handler | Behaviour |
|---|---|---|
| Transient DNS / NetworkError / TimedOut | [Bug 3 FIX] `_error_handler` early-returns with single WARNING log | No traceback flood; no Telegram alert; PTB auto-retries |
| All other PTB exceptions | Full traceback logged + Telegram operator alert | Bot continues running |
| `bot.stop()` hangs | `asyncio.wait_for(stop(), 5.0)` [Bug 2B FIX] | Hard 5s timeout; continues cleanup |

### reconciliation.py
| Failure | Handler | Behaviour |
|---|---|---|
| `run()` sleep uninterruptible (old bug) | [Bug 2B FIX] `_interruptible_sleep()` — wakes on `shutdown_event` | Exits loop immediately on shutdown |
| `stop()` hangs | `asyncio.wait_for(stop(), 10.0)` in `_cleanup_runtime` [Bug 2B FIX] | Hard 10s timeout; WARNING logged |
| DB query timeout | `asyncio.wait_for(query, 1.5)` | 1.5s timeout; reconciliation skipped for cycle |
| REST broker fetch timeout | `asyncio.wait_for(fetch, 2.0)` | 2s timeout; error logged |
| Divergence detected | `_handle_divergence()` | Telegram alert + DB insert into `reconciliation_log` |

### eod_watchdog.py
| Failure | Handler | Behaviour |
|---|---|---|
| `eod_scheduler` fails to trigger shutdown | [Bug 2A FIX] watchdog fires independently at 15:32 | `shutdown_event.set()` regardless of scheduler state |
| Process still alive at 15:40 IST | `os.kill(os.getpid(), signal.SIGTERM)` | Nuclear exit — no further cleanup possible |

### eod_scheduler.py
| Failure | Handler | Behaviour |
|---|---|---|
| Square-off fails at 15:10 | try/except; `notify()` Telegram | Logged; analysis phase still proceeds |
| Analysis fails at 15:32 | try/except; `notify()` Telegram | [Bug 2A FIX] `shutdown_event.set()` fires regardless in finally |

### signal_manager.py
| Failure | Handler | Behaviour |
|---|---|---|
| Daily limit reached | `(False, "Daily limit reached")` | Caller logs; no signal fired |
| 3 consecutive losses | `is_paused = True` | All signals blocked for rest of session |
| Per-symbol cooldown active | `(False, "Cooldown: ...")` | Caller logs; no signal fired |

### capital_manager.py
| Failure | Handler | Behaviour |
|---|---|---|
| Insufficient buying power | `{'allowed': False, 'reason': 'INSUFFICIENT_FUNDS'}` | Order blocked upstream in `order_manager` |
| Already holding symbol | `{'allowed': False, 'reason': 'ALREADY_HOLDING'}` | Prevents double-entry |
| Double allocation attempt | Warning log; skip | Idempotent allocation |

---

## SECTION 11 — Shutdown Sequence (Exact Order)

**Four possible shutdown triggers:**

| Trigger | Source | Time |
|---------|--------|------|
| (a) `eod_scheduler` completes analysis | `eod_scheduler.py` [Bug 2A FIX] | ~15:32–15:35 IST |
| (b) `eod_watchdog` soft shutdown | `eod_watchdog.py` [Bug 2A FIX NEW] | 15:32 IST exactly |
| (c) `eod_watchdog` hard os._exit(0) | `eod_watchdog.py` [Bug 2A FIX NEW] | 15:40 IST if still alive |
| (d) SIGINT/SIGTERM from OS | `_install_signal_handlers()` | Any time (e.g. Ctrl+C) |

```
[SHUTDOWN SEQUENCE — _cleanup_runtime()]

  Method: shutdown_event.set()
  Effect: All while/async-for loops exit on next iteration

  1. ctx.focus_engine.stop("PROCESS_SHUTDOWN") called explicitly at TOP of
     cleanup_runtime() — clears pending_signals, cooldown_signals, cancels
     monitor task. No new signals accepted from this point.

  2. TaskGroup tasks wind down:
     - trading_loop: exits on shutdown_event check
     - eod_scheduler: exits on next 15s sleep check
     - eod_watchdog: exits on next 30s sleep check
     - reconciliation: _interruptible_sleep() wakes immediately [Bug 2B FIX]
     - telegram_bot: shutdown_event propagated to PTB

  3. _cleanup_runtime() runs in finally block:

     await asyncio.wait_for(ctx.reconciliation_engine.stop(), 10.0)
     └─ Timeout: 10s  [Bug 2B FIX]
     └─ Log: "[REC-ENGINE] Stop called. Hard timeout: 10s."
     └─ On timeout: "RecEngine stop timed out. Forcing."

     await asyncio.wait_for(ctx.bot.stop(), 5.0)
     └─ Timeout: 5s   [Bug 2B FIX]
     └─ On timeout: "Telegram stop timed out. Forcing."

     await asyncio.wait_for(ctx.db_manager.close(), 5.0)
     └─ Timeout: 5s   [Bug 2B FIX]
     └─ On timeout: "DB close timed out. Forcing."

     await asyncio.wait_for(ctx.broker.disconnect(), 5.0)
     └─ Timeout: 5s   [Bug 2B FIX]
     └─ On timeout: "Broker disconnect timed out. Forcing."

  4. _update_terminal_log() — final log flush to md/terminal_log.md

  5. "[SUPERVISOR] ✅ Cleanup complete."

Maximum total shutdown time: 10 + 5 + 5 + 5 = 25 seconds
```

---

## SECTION 12 — Database Schema

**Database:** `shortcircuit_trading` (PostgreSQL)
**Migration file:** `migrations/v42_1_0_postgresql.sql`
**Extension:** `uuid-ossp` required

### Table: `orders` — Order State Machine Persistence

| Column | Type | Description |
|---|---|---|
| `order_id` | UUID PK | Internal ShortCircuit order ID |
| `exchange_order_id` | VARCHAR(50) UNIQUE | Fyers-assigned order ID |
| `symbol` | VARCHAR(20) NOT NULL | e.g. `NSE:NSESGL-EQ` |
| `side` | VARCHAR(4) CHECK(BUY, SELL) | Trade direction |
| `order_type` | VARCHAR(20) CHECK(MARKET, LIMIT, SL, SL-M) | Order type |
| `qty` | INTEGER NOT NULL >0 | Shares |
| `price` | DECIMAL(12,2) | Limit price (NULL for MARKET) |
| `trigger_price` | DECIMAL(12,2) | SL trigger price |
| `state` | VARCHAR(30) | PENDING / SUBMITTED / SUBMITTED_UNCONFIRMED / OPEN / PARTIAL_FILL / FILLED / REJECTED / CANCELLED / CANCEL_PENDING / MODIFY_PENDING / EXPIRED / DISCONNECTED |
| `created_at` | TIMESTAMPTZ DEFAULT NOW() | |
| `submitted_at` | TIMESTAMPTZ | When sent to broker |
| `filled_at` | TIMESTAMPTZ | When fill confirmed |
| `cancelled_at` | TIMESTAMPTZ | When cancelled |
| `updated_at` | TIMESTAMPTZ | Auto-updated by trigger |
| `filled_qty` | INTEGER DEFAULT 0 | Filled quantity |
| `avg_filled_price` | DECIMAL(12,2) | Average fill price |
| `commission` | DECIMAL(10,2) | Brokerage fees |
| `error_code` | VARCHAR(50) | Broker error code (e.g. `-50`) |
| `error_message` | TEXT | Broker error description |
| `signal_id` | VARCHAR(50) | Link to originating signal |
| `strategy_name` | VARCHAR(50) DEFAULT 'SHORT_CIRCUIT' | Strategy tag |
| `session_date` | DATE NOT NULL | Trading session date |
| `created_by` | VARCHAR(50) DEFAULT 'BOT' | Audit field |

**Indexes:** `(symbol, state)`, `(session_date)`, `(exchange_order_id)`
**Trigger:** `update_updated_at()` BEFORE UPDATE — auto-sets `updated_at = NOW()`

---

### Table: `positions` — Source of Truth for Open/Closed Positions

| Column | Type | Description |
|---|---|---|
| `position_id` | UUID PK | Internal position ID |
| `symbol` | VARCHAR(20) NOT NULL | NSE symbol |
| `qty` | INTEGER NOT NULL | Size (negative = short) |
| `entry_price` | DECIMAL(12,2) NOT NULL | Fill price |
| `current_price` | DECIMAL(12,2) | Last known LTP |
| `unrealized_pnl` | DECIMAL(12,2) | Mark-to-market P&L |
| `realized_pnl` | DECIMAL(12,2) DEFAULT 0 | Closed P&L |
| `state` | VARCHAR(20) CHECK(OPEN, CLOSED, ORPHANED, RECONCILED) | Lifecycle state |
| `entry_order_id` | UUID FK → orders | Entry order link |
| `exit_order_id` | UUID FK → orders | Exit order link |
| `sl_order_id` | VARCHAR(50) | Broker SL-M order ID |
| `opened_at` | TIMESTAMPTZ DEFAULT NOW() | |
| `closed_at` | TIMESTAMPTZ | |
| `last_reconciled_at` | TIMESTAMPTZ | Last reconciliation timestamp |
| `source` | VARCHAR(30) CHECK(SIGNAL, MANUAL, ORPHAN_RECOVERY, RECONCILIATION) | How position was created |
| `session_date` | DATE NOT NULL | Trading session date |
| `notes` | TEXT | Freeform notes |

**Indexes:** `(symbol, state)`, partial index on `state = 'OPEN'`

---

### Table: `reconciliation_log` — Audit Trail

| Column | Type | Description |
|---|---|---|
| `recon_id` | UUID PK | Log entry ID |
| `timestamp` | TIMESTAMPTZ DEFAULT NOW() | When ran |
| `internal_pos_count` | INTEGER NOT NULL | DB position count |
| `broker_pos_count` | INTEGER NOT NULL | Broker position count |
| `orphans_detected` | JSONB | Broker has, DB missing |
| `phantoms_detected` | JSONB | DB has, broker missing |
| `mismatches` | JSONB | Quantity discrepancies |
| `status` | VARCHAR(40) CHECK(CLEAN, DIVERGENCE_DETECTED, AUTO_RESOLVED, MANUAL_INTERVENTION_REQUIRED) | Result |
| `resolution_action` | TEXT | What was done to resolve |
| `check_duration_ms` | INTEGER | Duration of reconciliation |
| `session_date` | DATE NOT NULL | Trading session date |

**Indexes:** `(session_date)`, partial on `status != 'CLEAN'`

---

### Table: `gate_results` — Signal Gate Audit Trail (Phase 44.9 — migration `v44_8_2_gate_results.sql`)

Stores every gate evaluation: one row per candidate per scan.

| Column | Type | Description |
|--------|------|-------------|
| `id` | BIGSERIAL PK | Auto-increment |
| `session_date` | DATE | Trading date |
| `scan_id` | INTEGER | Monotonic scan counter (forwarded from scanner) |
| `evaluated_at` | TIMESTAMPTZ | Exact evaluation timestamp |
| `symbol` | VARCHAR(30) | NSE symbol (e.g. `NSE:SBIN-EQ`) |
| `nifty_regime` | VARCHAR(10) | BULLISH / BEARISH / RANGE at eval time |
| `nifty_level` | NUMERIC(10,2) | NIFTY spot at eval time |
| `g1_pass`–`g9_pass` | BOOLEAN | NULL=not evaluated, TRUE=pass, FALSE=fail |
| `g1_value`–`g9_value` | NUMERIC or VARCHAR | Gate-specific metric value |
| `g10_pass`–`g12_pass` | BOOLEAN | focus_engine gates (NULL if signal never queued) |
| `g10_value`–`g12_value` | NUMERIC | focus_engine gate metric values |
| `verdict` | VARCHAR(20) | `SIGNAL_FIRED` / `REJECTED` / `DATA_ERROR` / `SUPPRESSED` |
| `first_fail_gate` | VARCHAR(30) | e.g. `G5_EXHAUSTION`, `G12_TIMEOUT` |
| `rejection_reason` | TEXT | Human-readable reason string |
| `data_tier` | VARCHAR(20) | `WS_CACHE` / `HYBRID` / `REST_EMERGENCY` |
| `entry_price` | NUMERIC(10,2) | Set only when `verdict = SIGNAL_FIRED` |
| `qty` | INTEGER | Set only when `verdict = SIGNAL_FIRED` |

**Indexes:** `(session_date, symbol)`, `(session_date, verdict)`, `(session_date, first_fail_gate) WHERE first_fail_gate IS NOT NULL`

**Key diagnostic query:**
```sql
SELECT symbol, first_fail_gate, COUNT(*) AS n
FROM gate_results
WHERE session_date = CURRENT_DATE
GROUP BY symbol, first_fail_gate
ORDER BY symbol, n DESC;
```

---

## SECTION 13 — Configuration Reference

All keys from `config.py`. Loaded from `.env` via `python-dotenv` unless marked hardcoded.

| Key | Type | Default | Used By | Description |
|---|---|---|---|---|
| `FYERS_CLIENT_ID` | str | env | `fyers_connect.py` | Fyers API client ID |
| `FYERS_SECRET_ID` | str | env | `fyers_connect.py` | Fyers API secret |
| `FYERS_REDIRECT_URI` | str | hardcoded fallback | `fyers_connect.py` | OAuth redirect URI |
| `TELEGRAM_BOT_TOKEN` | str | env | `telegram_bot.py` | PTB bot token |
| `TELEGRAM_CHAT_ID` | str | env | `telegram_bot.py` | Authorized operator chat |
| `CAPITAL_PER_TRADE` | int | `1800` | `main.py` → `CapitalManager` | Base capital in INR |
| `CAPITAL` | int | `1800` | Backward-compat alias | Alias for `CAPITAL_PER_TRADE` |
| `RISK_PER_TRADE` | int | `200` | Optional | Max loss INR |
| `AUTO_MODE` | bool | `False` (hardcoded) | `telegram_bot.py` | **Always False on startup** |
| `AUTO_MODE_DEFAULT` | bool | `False` | Backup fallback | Never set True |
| `WS_TICK_FRESHNESS_TTL_SECONDS` | float | `180.0` | `fyers_broker_interface.py`, `scanner.py` | Max age (seconds) for a WS tick to be considered fresh |
| `LOG_FILE` | str | `logs/bot.log` | `main.py` | RotatingFileHandler path |
| `SQUARE_OFF_TIME` | str | `15:10` | `eod_scheduler.py` | EOD hard square-off HH:MM |
| `VALIDATION_TIMEOUT_MINUTES` | int | `15` | `focus_engine.py` | Gate 12 timeout |
| `MULTI_EDGE_ENABLED` | bool | `False` | `analyzer.py` | Multi-edge detection master switch |
| `CONFIDENCE_THRESHOLD` | str | `MEDIUM` | `multi_edge_detector.py` | Min confidence level |
| `ENABLED_DETECTORS` | dict | PATTERN+TRAPPED+ABSORPTION+BAD_HIGH+FAILED_AUCTION all True | `multi_edge_detector.py` | Individual toggle per detector |
| `EDGE_WEIGHTS` | dict | ABSORPTION=3.0, BAD_HIGH=2.0, etc. | `multi_edge_detector.py` | Weighted confluence scoring |
| `CONFIDENCE_THRESHOLD_EXTREME` | float | `5.0` | `multi_edge_detector.py` | EXTREME confidence threshold |
| `CONFIDENCE_THRESHOLD_HIGH` | float | `3.0` | `multi_edge_detector.py` | HIGH confidence threshold |
| `CONFIDENCE_THRESHOLD_MEDIUM` | float | `2.0` | `multi_edge_detector.py` | MEDIUM threshold |
| `LOG_MULTI_EDGE_DETAILS` | bool | `True` | `multi_edge_detector.py` | Log all detection attempts |
| `SCANNER_PARALLEL_WORKERS` | int | `3` | `scanner.py` | Max concurrent candle quality-check threads |
| `RVOL_MIN_CANDLES` | int | `20` | `analyzer.py` | Min minutes since open for valid RVOL |
| `RVOL_VALIDITY_GATE_ENABLED` | bool | `True` | `analyzer.py` | Feature flag — set False to disable instantly |
| `USE_SCALPER_RISK_MANAGEMENT` | bool | `False` | `trade_manager.py` | Scalper SL system master switch |
| `SCALPER_STOP_TICK_BUFFER` | int | `12` | `scalper_risk_calculator.py` | Ticks above setup high for initial SL |
| `SCALPER_STOP_HUNT_BUFFER_ENABLED` | bool | `True` | `scalper_risk_calculator.py` | Enable 0.3% buffer above setup high for SL placement |
| SCALPER_STOP_HUNT_BUFFER_PCT | float |  .003 | scalper_risk_calculator.py | 0.3% stop hunt protection |
| SCALPER_BREAKEVEN_TRIGGER_PCT | float |  .003 | scalper_position_manager.py | 0.3% profit triggers breakeven move |
| `SCALPER_TRAILING_DISTANCE_INITIAL` | float | `0.002` | `scalper_position_manager.py` | 0.2% trailing distance |
| `SCALPER_TRAILING_DISTANCE_AFTER_TP1` | float | `0.0015` | `scalper_position_manager.py` | 0.15% after TP1 |
| `SCALPER_TRAILING_DISTANCE_AFTER_TP2` | float | `0.001` | `scalper_position_manager.py` | 0.1% after TP2 |
| `SCALPER_TP1_PCT` | float | `0.015` | `scalper_position_manager.py` | TP1 at +1.5% |
| `SCALPER_TP2_PCT` | float | `0.025` | `scalper_position_manager.py` | TP2 at +2.5% |
| `SCALPER_TP3_PCT` | float | `0.035` | `scalper_position_manager.py` | TP3 at +3.5% |
| `ENABLE_EOD_SIMULATION` | bool | `True` | `eod_analysis.py` | Run EOD simulation |
| `INTRADAY_LEVERAGE` | float | `5.0` | `main.py` → `CapitalManager` | Fixed 5× NSE intraday |
| `ENABLE_POSITION_VERIFICATION` | bool | `True` | `trade_manager.py` | Safety check before orders — never False |
| `ENABLE_BROKER_POSITION_POLLING` | bool | `True` | `focus_engine.py` | Broker check in focus loop |
| `POSITION_RECONCILIATION_INTERVAL` | int | `1800` | `reconciliation.py` | Reconcile every 30 min off-hours |
| `EMERGENCY_ALERT_ENABLED` | bool | `True` | `emergency_logger.py` | Emergency log active |
| `ENABLE_DIAGNOSTIC_ANALYZER` | bool | `True` | `telegrambot.py` | Enable `/why` command |
| `ENABLE_DETECTOR_TRACKING` | bool | `True` | `detector_performance_tracker.py` | Log per-detector hit rate and P&L correlation |
| `DIAGNOSTIC_LOG_PATH` | str | `logs/diagnostic_analysis.csv` | `diagnostic_analyzer.py` | Gate failure tracking |
| `ENABLE_DISCRETIONARY_EXITS` | bool | `True` | `discretionary_engine.py` | Soft-stop / target extension |
| `ENABLE_HARD_STOPS` | bool | `True` | `trade_manager.py` | SL-M orders active |
| `ENABLE_MARKET_REGIME_OPTIMIZATION` | bool | `True` | `discretionary_engine.py` | Regime-based exit optimization |
| `TRADING_ENABLED` | bool | `False` | `market_session.py` | Dynamically flipped by `set_trading_enabled()` |
| `ETF_CLUSTER_DEDUP_ENABLED` | bool | `True` | `scanner.py` / `analyzer.py` | Blocks ETF cluster duplicates (e.g. SILVER ETFs) |
| `ETF_CLUSTER_KEYWORDS` | list | `["SILVER"]` | Same | Keywords triggering dedup |
| `EDITABLE_SIGNAL_FLOW_ENABLED` | bool | `False` | `telegram_bot.py` | Phase 44.5 editable message flow |
| `SIGNAL_LOG_PATH` | str | `logs/signals.csv` | `trademanager.py` | Signal execution log |
| `LOG_ALL_SIGNALS` | bool | `True` | `trademanager.py` | Log executed AND skipped signals |
| `SIMULATION_LOG_PATH` | str | `logs/eod_simulation.csv` | `eod_analysis.py` | EOD simulation output CSV |
| `DETECTOR_LOG_PATH` | str | `logs/detector_performance.csv` | `detector_performance_tracker.py` | Per-detector hit rate log |
| `EMERGENCY_LOG_PATH` | str | `logs/emergency_alerts.log` | `emergency_logger.py` | Critical failure events log |
| `ORPHANED_POSITION_LOG_PATH` | str | `logs/orphaned_positions.log` | `emergency_logger.py` | Orphaned position discoveries log |

### Config Dict Constants

These are structured dicts defined in `config.py`. Values here are the defaults — do not flatten into individual rows as the keys are consumed as a unit.

| Constant | Consumed by | Keys |
|---|---|---|
| `DISCRETIONARY_CONFIG` | `discretionary_engine.py` | `soft_stop_pct=0.005`, `hard_stop_pct=0.02`, `initial_target_pct=0.025`, `extended_target_pct=0.04`, `bearish_exit_threshold=2`, `momentum_extend_threshold=2` |
| `EOD_CONFIG` | `eod_analyzer.py`, `eod_analysis.py` | `audit_slippage_threshold=0.005`, `report_format="Markdown"`, `save_reports_locally=True`, `auto_send_telegram=True` |
| `MARKET_REGIME_CONFIG` | `discretionary_engine.py`, `market_context.py` | `strong_trend_threshold=0.01`, `moderate_trend_threshold=0.005`, `momentum_decay_minutes=10`, `override_patterns=["EVENING_STAR","BEARISH_ENGULFING","SHOOTING_STAR"]`, `divergence_threshold=-2.0` |
| `MARKET_SESSION_CONFIG` | `market_session.py` | `market_open="09:15"`, `safe_trade_start="09:45"`, `eod_cutoff="15:10"`, `market_close="15:30"`, `allow_postmarket_sleep=True`, `enable_warmup_scanning=True`, `require_morning_data=True`, `morning_range_fallback_pct=0.5`, `telegram_startup_alert=True`, `telegram_state_transitions=True` |

---

## SECTION 14 — ML Data Pipeline

### Overview
ML logging is **passive data collection only** — predictions are not used in signal scoring. Data accumulates as a labelled training dataset for future model development.

### Handler
`ml_logger.py` — `MLDataLogger` class; accessed via `get_ml_logger()` singleton

### Write Points
| When | Who | Call |
|------|-----|------|
| At signal detection | `analyzer.py` | `get_ml_logger().log_observation(symbol, ltp, features)` → returns `obs_id` (UUID4) |
| At trade close / EOD | `order_manager.py` or EOD script | `ml_logger.update_outcome(obs_id, outcome, exit_price, mfe, mae, hold_time_mins)` |

### Storage
| File | Description |
|------|-------------|
| `data/ml/data{YYYY-MM-DD}.parquet` | Daily observation file — one per session |
| `data/ml/data{YYYY-MM-DD}.csv` | CSV backup alongside each parquet |
| `data/ml/training_data.parquet` | Combined labelled dataset (via `export_for_training()`) |

### Observation ID Format
UUID4 string — e.g. `cbb2d7e6-0391-4a5a-bec8-...`
Referenced in logs as: `ML Logged observation {obs_id} for {symbol}`

### Atomic Write Mechanism
Writes to temp file first, then `os.rename()` — no corruption on crash.

### Feature Schema (`FEATURE_COLUMNS` in `ml_logger.py`)
| Feature | Type | Description |
|---------|------|-------------|
| `obs_id` | str | UUID4 observation ID |
| `schema_version` | str | `1.0.0` |
| `date` | str | YYYY-MM-DD |
| `time` | str | HH:MM:SS |
| `symbol` | str | NSE:SYMBOL-EQ |
| `ltp` | float | Price at signal time |
| `pattern` | str | Detected pattern name |
| `rvol` | float | Relative volume multiplier |
| `vwap_sd` | float | VWAP standard deviation bands |
| `bid_ask_spread` | float | Spread as % |
| `num_confirmations` | int | Count of pro_confluence confirmations |
| `confirmations` | str | JSON list of detailed confirmations |
| `is_round_number` | bool | Near psychological price level |
| `is_bad_high` | bool | Heavy sellers at high detected |
| `is_trapped` | bool | Trapped positions detected |
| `is_absorption` | bool | Aggression-no-progress pattern |
| `nifty_trend` | str | UP / DOWN / RANGE |
| `exit_price` | float | Actual or simulated exit (labelled at EOD) |
| `max_favorable` | float | Max favorable excursion (MFE) |
| `max_adverse` | float | Max adverse excursion (MAE) |
| `pnl_pct` | float | P&L as % of entry |
| `hold_time_mins` | int | Minutes position held |

### Prediction Usage
None — model inference is not wired into any signal gate. The pipeline is write-only during live trading.

---

## SECTION 15 — File Manifest

All files present in root as of 2026-02-26 audit (54 files, 10 subdirectories):

```
ShortCircuit/
├── main.py                          (21 KB)  Supervisor + entry point [Phase 44.9]
├── config.py                        (10 KB)  All configuration + env vars
├── fyers_broker_interface.py        (42 KB)  WS + REST broker interface [Phase 44.9 — WS cache state machine]
├── order_manager.py                 (25 KB)  Async order lifecycle  [P0 FIXED]
├── focus_engine.py                  (30 KB)  Validation gate + position monitor + G10-G12 audit [Phase 44.9]
├── trade_manager.py                 (29 KB)  Legacy execution engine + EOD square-off
├── telegram_bot.py                  (83 KB)  Full Telegram operator interface [Phase 44.8 + PRD v2.2 — auto queue + morning briefing]
├── scanner.py                       (14 KB)  Market scanner — tiered WS/REST data provider [Phase 44.9]
├── analyzer.py                      (27 KB)  G1–G9 GOD MODE signal engine + full gate audit [Phase 44.9]
├── gate_result_logger.py            ( 9 KB)  GateResult dataclass + singleton logger + periodic flush [Phase 44.9 — NEW]
├── capital_manager.py               ( 4 KB)  5× leverage capital tracker
├── signal_manager.py                 (6 KB)  Daily signal gate + cooldown + loss pause
├── market_session.py                (11 KB)  Market session state machine (premarket→postmarket)
├── market_context.py                (10 KB)  NIFTY regime detector (TREND_UP/DOWN/RANGE) [PRD v2.1 — REST morning range + morning_range_valid]
├── reconciliation.py                (12 KB)  HFT reconciliation engine  [Bug 2B FIXED]
├── database.py                      (10 KB)  PostgreSQL + asyncpg connection pool
├── fyers_connect.py                  (8 KB)  OAuth token management (singleton)
├── eod_scheduler.py                  (3 KB)  EOD task scheduler (15:10 SQ + 15:32 analysis)  [Bug 2A FIXED]
├── eod_watchdog.py                   (2 KB)  EOD failsafe watchdog  [Bug 2A — NEW FILE]
├── eod_analyzer.py                  (10 KB)  In-session EOD P&L report (DB-driven)
├── eod_analysis.py                  (16 KB)  Offline EOD simulation CLI script
├── eod_why.py                        (5 KB)  Post-session gate diagnostic CLI
├── god_mode_logic.py                (15 KB)  Technical analysis primitives [Phase 44.8]
├── multi_edge_detector.py           (23 KB)  Phase 41.1 multi-edge detection (flag-gated off)
├── market_profile.py                 (7 KB)  VAH/VAL/POC (Market Profile) calculator
├── tape_reader.py                   (11 KB)  Order flow + tape analysis (trapped longs, absorption)
├── htf_confluence.py                 (8 KB)  15m Higher-Time-Frame Lower High check
├── ml_logger.py                     (12 KB)  ML observation logger to daily parquet
├── discretionary_engine.py           (7 KB)  Intelligent exit engine (soft-stop / target extend)
├── discretionary_signals.py         (10 KB)  Exit signal catalogue
├── scalper_position_manager.py      (11 KB)  SL state machine (INITIAL→BREAKEVEN→TRAILING)  [flag-gated off]
├── scalper_risk_calculator.py        (4 KB)  Scalper risk sizing + tick-based SL
├── market_utils.py                   (1 KB)  is_market_hours() helper
├── symbols.py                        (2 KB)  NIFTY_50 constant + validate_symbol() [Phase 44.8]
├── emergency_logger.py               (4 KB)  Emergency alert file logger
├── startup_recovery.py               (1 KB)  Orphaned trade scanner at startup
├── detector_performance_tracker.py   (7 KB)  Per-detector hit rate + P&L tracking
├── trade_simulator.py               (10 KB)  Offline signal simulation engine
├── journal_manager.py                (6 KB)  Trade journal CSV writer
├── position_reconciliation.py        (5 KB)  Lightweight position cross-check utility
├── diagnostic_analyzer.py           (36 KB)  /why command — full gate breakdown
├── async_utils.py                    (2 KB)  Asyncio utility helpers
├── apply_migration.py                (1 KB)  PostgreSQL migration runner (manual)
├── dump_terminal_log.py              (2 KB)  Session log extractor → md/terminal_log.md
├── ARCHITECTURE.md                          This file
├── README.md                        (14 KB)  Project README
├── Strategy.md                      (18 KB)  Strategy documentation
├── Dockerfile                        (1 KB)  Container definition
├── requirements.txt                  (2 KB)  Python dependencies
├── pytest.ini                               Test runner configuration
├── .env.example                             Environment variable template
├── .gitignore
├── access_token.txt                         Root-level token copy (see also data/)
│
├── tests/                           (22 test files)
│   ├── conftest.py
│   ├── test_phase44_ux.py            26 tests — all passing  ✅
│   ├── test_websocket_integration.py
│   ├── test_eod_scheduler.py
│   ├── test_eod_flow.py
│   ├── test_eod_flow_integration.py
│   ├── test_eod_fixes.py
│   ├── test_eod_analyzer_contract.py
│   ├── test_market_session.py
│   ├── test_safety.py
│   ├── test_supervisor.py
│   ├── test_safe_exit_race.py
│   ├── test_database_query_contract.py
│   ├── test_db_pool.py
│   ├── test_config_imports.py
│   ├── test_symbols.py
│   ├── test_async_integration.py
│   ├── test_fix.py
│   ├── test_phase44_5_editable_flow.py
│   ├── test_telegram_shutdown_contract.py
│   ├── test_gap_v2_1.py              PRD v2.1 gap closure tests (81 passed suite) ✅
│   └── test_candle.py                Live candle API test (skipped in CI — requires auth)
│
├── migrations/
│   ├── v42_1_0_postgresql.sql        Schema: orders + positions + reconciliation_log
│   └── v44_8_2_gate_results.sql      Schema: gate_results table + 3 indexes [Phase 44.9 — NEW]
│
├── data/
│   ├── access_token.txt              Fyers OAuth token (gitignored)
│   ├── short_circuit.db              SQLite (legacy — primary is PostgreSQL)
│   ├── trade_journal.csv             Human-readable trade log
│   └── ml/
│       ├── data{YYYY-MM-DD}.parquet  Daily ML observation files
│       └── training_data.parquet     Combined labelled training dataset
│
├── logs/                             Runtime logs (gitignored)
│   ├── bot.log                       Primary rotating log (10 MB × 5 backups)
│   ├── signals.csv                   All signal events (now with 5 Phase 44.8 signal metadata columns)
│   ├── detector_performance.csv      Per-detector hit/miss tracking
│   ├── eod_simulation.csv            EOD simulation results
│   ├── emergency_alerts.log          Critical failure events
│   ├── orphaned_positions.log        Orphaned position discoveries
│   ├── diagnostic_analysis.csv       /why command gate failure log
│   ├── rejections_YYYYMMDD.log       EOD gate rejection summary [Phase 44.9 — NEW]
│   └── fyers_rest/                   Fyers SDK REST request logs
│
├── md/
│   └── terminal_log.md               Last session log summary (written by dump_terminal_log.py)
│
└── tools/
    ├── get_auth_url.py               Prints Fyers OAuth URL for manual auth
    └── set_token.py                  Writes access token to data/access_token.txt
```

---

*Document generated from live code audit — 2026-02-26 23:57 IST.
All P0/P1/P2/P3 bug fixes (Bug 2A EOD shutdown, Bug 2B RecEngine timeout, Bug 3 DNS resilience) reflected as current merged behaviour.
Phase 44.9 (PRD-007 WS Cache Reliability + PRD-008 Signal Rejection Audit Trail) additions reflected as of 2026-03-03.*

*String "Legacy TradeManager" intentionally absent — dead code path not documented.*

---

## SECTION 16 — WS Cache Architecture (Phase 44.9 — PRD-007)

### State Machine
The WS quote cache operates as a formal state machine:

```
UNINITIALIZED → PRIMING → READY
                    ↓ (if 2 consecutive CRITICAL health readings)
                CRITICAL → _trigger_reprime() → PRIMING
```

| State | Condition | Behaviour |
|-------|-----------|----------|
| `UNINITIALIZED` | Default on init | No subscription active |
| `PRIMING` | `subscribe_scanner_universe()` called | Ticks arriving; `_check_cache_readiness_internal()` evaluates each tick |
| `READY` | ≥ threshold% of symbols have fresh tick (< 60s old) | `_cache_ready_event.set()`; scanner uses Tier 1 |

Threshold: **85%** for first 30 min after open, **80%** thereafter (`_get_readiness_threshold()`).

### Readiness Gate
`wait_for_cache_ready(timeout_sec=45.0)` blocks the startup sequence until `READY` state is confirmed.
Called in `main.py` via `asyncio.to_thread()` after `subscribe_scanner_universe()`.
If timeout expires: CRITICAL log + Telegram alert; scanner falls back to REST for first scan.

### Tiered Data Provider (`scanner.py`)

| Tier | Condition | Action | Latency |
|------|-----------|--------|---------|
| **Tier 1 WS_CACHE** | ≥ 85% symbols fresh | Pure WS cache — no REST calls | ~100ms |
| **Tier 2 HYBRID** | 50–85% fresh | WS for fresh symbols; REST supplement for stale subset only | ~1,500ms |
| **Tier 3 REST_EMERGENCY** | < 50% fresh | Full REST + CRITICAL log + Telegram alert | ~6,000ms |

Tier 3 must never occur silently. Always logged as CRITICAL.

### Health Monitor
Background daemon thread `WSCacheHealthMonitor` in `FyersBrokerInterface`.
- Runs every 30 seconds
- Log format: `CACHE HEALTH | Fresh: X/Y (Z%) | Stale: N | Missing: N | Age P50/P95/P99 | State | Status`
- Status: `HEALTHY` (≥85%) | `DEGRADED` (50–85%) | `CRITICAL` (<50%)
- 2 consecutive CRITICAL readings → `_trigger_reprime()` (re-subscribes all symbols, resets to PRIMING)
- Zombie guard: thread restart checks both `_health_monitor_running` flag **and** `thread.is_alive()` (Phase 44.9 bug fix)

### Root Cause Fixed (Phase 44.9)
`scanner.py` had `if self.broker._quote_cache:` — empty dict is falsy in Python. WS subscribe ACK fires instantly; ticks arrive 500ms–2s later. Every startup scan hit the empty dict check, fell to REST, and stayed there the entire session.
**Fix:** `is_cache_ready()` checks `_cache_state == 'READY'`, not dict truthiness.

### Per-Scan Logging
Every scan emits:
```
SCAN #N | Tier: WS_CACHE | Cache: X/Y fresh | Scan_ms: Z | Pre-candidates: N
```

---

## SECTION 17 — Signal Gate Audit Trail (Phase 44.9 — PRD-008)

### Overview
Every candidate evaluation produces exactly one `GateResult` record stored in `GateResultLogger` (singleton, `gate_result_logger.py`). Records flow to PostgreSQL `gate_results` table via periodic flush (every 100 records) and EOD flush.

### GateResult Object
`GateResult` dataclass — fields: `symbol`, `scan_id`, `evaluated_at`, `nifty_regime`, `nifty_level`, `g1_pass`–`g12_pass`, `g1_value`–`g12_value`, `verdict`, `first_fail_gate`, `rejection_reason`, `data_tier`, `entry_price`, `qty`.

Populated incrementally: G1–G9 in `check_setup()`, G10–G12 in `focus_engine.py`.
Passed between analyzer and focus_engine via `finalized['_gate_result'] = gr`.

### Log Output Format
```
[SIGNAL]     SYMBOL | Scan#N | ALL GATES PASSED → ENTRY ₹X.XX
[REJECTED]   SYMBOL | Scan#N | FAILED at GN_GATE_NAME | Reason: ...
[DATA_ERROR] SYMBOL | Scan#N | <error description>
[SUPPRESSED] SYMBOL | Scan#N | <same gate/reason within 60s>
```

Suppression: same `(symbol, gate, reason_category)` within 60s is suppressed. Force-log every 300s regardless.

### record_signal() Placement — CRITICAL INVARIANT

> **⚠ WARNING: This must never regress.**
> `signal_manager.record_signal()` burns one daily signal slot (counted against the 5/day limit).
> It is called **ONLY** at the moment `enter_position()` succeeds in `focus_engine.py`.
> It must **never** be called before order placement confirmation.
>
> Historical bug (fixed Phase 44.9): it was incorrectly called in `_finalize_signal()` before
> `focus_engine` gate checks — signals that were rejected by G10–G12 still consumed a daily slot.

### Flush Architecture
| Path | Trigger | Mechanism |
|------|---------|----------|
| Periodic | Every 100 `record()` calls | Daemon thread `GateResultPeriodicFlush` → `asyncio.run(_flush_batch())` |
| EOD | `flush_to_db()` | Reuses `_flush_batch()` — catches any records periodic flush missed |

`_flushed_count` advances **after** successful `executemany` and **before** `conn.close()` — `close()` failure does not cause duplicate inserts on retry.

---

## SECTION 18 — Phase 44.9 Change Registry

### Files Changed

| File | Change Type | Summary |
|------|-------------|--------|
| `fyers_broker_interface.py` | MODIFIED | Full WS cache state machine (`UNINITIALIZED→PRIMING→READY`), health monitor daemon, tiered readiness, `import time` moved to module level, zombie thread guard |
| `scanner.py` | MODIFIED | Tier 1/2/3 data provider, per-scan logging (`SCAN #N | Tier: ... | Cache: X/Y`), `is_cache_ready()` replacing dict falsy check |
| `analyzer.py` | MODIFIED | G1–G9 gate recording with `GateResult`, `check_setup_with_edges()` fully instrumented, `_finalize_signal()` made pure (no gate logic), HTF promoted to G9 |
| `focus_engine.py` | MODIFIED | G10–G12 gate recording, `record_signal()` moved here from `_finalize_signal()`, `self.analyzer` injected for slot tracking |
| `main.py` | MODIFIED | Startup gate (`wait_for_cache_ready`), `scan_id` + `data_tier` forwarded to analyzer, EOD gate flush, `set_dsn()` call after DB init, `focus_engine.analyzer` injection |
| `gate_result_logger.py` | **NEW** | `GateResult` dataclass, `GateResultLogger` singleton, suppression logic, periodic flush (100-record trigger), EOD summary writer, `flush_to_db()` → `gate_results` table |
| `migrations/v44_8_2_gate_results.sql` | **NEW** | `gate_results` table + 3 indexes |

### Bugs Fixed in Phase 44.9

| # | Bug | Location | Root Cause | Fix |
|---|-----|----------|------------|-----|
| 1 | Empty dict falsy cache check | `scanner.py:201` | `if self.broker._quote_cache:` evaluates empty dict as False → entire session used REST | Replaced with `is_cache_ready()` state machine check |
| 2 | HTF gate invisible | `analyzer.py / _finalize_signal()` | HTF confluence blocked trades with `return None` — no log, no DB record | Promoted to G9 in `check_setup()`, `grl.record()` on every failure |
| 3 | Premature `record_signal()` | `analyzer.py / _finalize_signal()` | Daily slot burned before `focus_engine` confirmed order placement | Removed from `_finalize_signal()`, moved to `focus_engine.py` at `enter_position()` success |
| 4 | Signal Manager called twice | `analyzer.py` | `can_signal()` called at G8 and again inside `_finalize_signal()` | Removed second call; `_finalize_signal()` is now pure (SL calc + dict assembly only) |
| 5 | `check_setup_with_edges()` silent | `analyzer.py` | All return paths were silent `None` — zero gate records | Fully instrumented with `GateResult`, matches `check_setup()` gate-for-gate |
| 6 | Health monitor zombie flag | `fyers_broker_interface.py` | `_health_monitor_running` stayed `True` after thread death; re-prime skipped restarting thread | Added `thread.is_alive()` guard alongside boolean flag |
| 7 | `import time` inside tick handler | `fyers_broker_interface.py` | Module lookup on every tick (thousands/second during peak hours) | Moved to module level; removed 5 inline `import time` statements |
| 8 | `flush_batch` duplicate risk | `gate_result_logger.py` | `_flushed_count` incremented after `conn.close()` — `close()` failure caused retry to re-insert | `_flushed_count` now incremented before `close()`, which runs in `finally` with its own `try/except` |


---

## SECTION 19 — EOD Sequence (Updated 2026-03-04)

```
EOD Sequence (Updated 2026-03-04):

15:10 — trigger_squareoff() fires (via eod_scheduler):
  1. ctx.focus_engine.stop("EOD_SQUAREOFF")  ← NEW
     → pending_signals.clear()
     → cooldown_signals.clear()
     → monitor task cancelled
  2. trade_manager.close_all_positions()
  3. Telegram: "EOD Square-off result: Closed X positions"

15:10 — focus_loop() self-terminates:
  → if now.hour == 15 and now.minute >= 10: safe_exit + stop_focus("EOD")

15:10 — monitor_pending_loop EOD guard fires:
  → self.stop("EOD_TIME_BOUNDARY") → loop exits

15:10 — flush_pending_signals EOD guard fires (pre-existing):
  → cooldown_signals.clear()

15:32 — eod_watchdog fires (FIXED):
  → shutdown_event.set()
  → TaskGroup receives signal → tasks begin cancelling
  → Loop NOW breaks cleanly when shutdown confirmed (was: never broke)

15:32–15:57 — cleanup_runtime() runs (25s max):
  → FocusEngine.stop("PROCESS_SHUTDOWN")
  → ReconciliationEngine.stop() (10s max)
  → Telegram bot stop (5s max)
  → DB pool close (5s max)
  → Broker disconnect() — NOW REAL IMPLEMENTATION (was: pass)
    → health monitor thread stopped via _ws_cache_stop flag
    → data WebSocket closed via asyncio.to_thread(data_ws.close)
    → order WebSocket closed

15:33 — update_terminal_log() runs:
  → subprocess.run(dump_terminal_log.py) — blocking, completes fully
  → Log file written to disk BEFORE process exits

15:33 — _force_exit daemon thread starts 3s countdown:
  → If Python exits naturally → daemon thread dies harmlessly
  → If Python hangs (non-daemon ThreadPoolExecutor threads) →
    os._exit(0) fires at 15:33+3s

15:40 — eod_watchdog hard kill (LAST RESORT):
  → os._exit(0) — bypasses all Python cleanup
  → Process is guaranteed dead by 15:40 regardless of state

PREVIOUSLY BROKEN (before 2026-03-04):
- FocusEngine never stopped at EOD → signals fired after 15:10
- eod_watchdog while True never broke → TaskGroup never finished
- disconnect() was a pass statement → ThreadPoolExecutor threads kept process alive
- Process required manual Ctrl+C every session
- update_terminal_log never ran automatically
```

---

## SECTION 20 — Known Architecture Risks & Mitigations (as of 2026-03-04)

| Risk | Severity | Mitigation |
|---|---|---|
| Fyers SDK is synchronous | Medium | All SDK calls wrapped in asyncio.to_thread() |
| ThreadPoolExecutor threads are non-daemon | High | os._exit(0) fallback in finally + eod_watchdog hard kill |
| Signal slots in-memory only | Medium | threading.Lock() added; resets daily via _reset_if_new_day() |
| Single event loop for all async tasks | Medium | Sync fallback monitor thread for edge cases |
| No end-to-end broker integration test | High | Test verifies enter_position logic but mocks broker API |
| Capital base ₹1800 (buying power ₹9000) | High | Verify qty >= 1 on first live trade |
| Telegram token exposed in logs | Low | Rotate token periodically |

---

## SECTION 21 — Incident Log

### 2026-03-04 — P0 Incident: Zero Trades Since January 2026

Duration: ~45 trading days with zero trade execution
Root cause: Missing await on async enter_position() call
Detection: Manual log review showing 3,500+ coroutine crashes per session

Bugs Fixed:
#  | File                      | Description                                    | Severity
1  | focus_engine.py L305      | Missing await on enter_position()              | P0
2  | scanner.py L132           | Dangling to_date variable in quality check     | P0
3a | focus_engine.py           | No can_signal() guard before execution         | P0
3b | focus_engine.py           | Slot burned before order confirmation          | P0
3c | focus_engine.py           | Silent exception swallow, no Telegram alert    | P0
4  | signal_manager.py         | Missing threading.Lock() on list ops           | P1
5  | focus_engine.py + main.py | No stale signal flush at 9:45 boundary         | P1
R1 | focus_engine.py           | return instead of continue in signal loop      | P0
R2 | focus_engine.py           | get_event_loop() crash in Python 3.12 thread   | P0
R3 | focus_engine.py           | Blocking REST call in async monitor loop       | P1
B2 | focus_engine.py + main.py | Validation monitor fires after EOD 15:10       | P0
B3 | eod_watchdog.py + main.py | Process never terminates cleanly               | P1
   | fyers_broker_interface.py | disconnect() was a no-op pass statement        | P1

All fixes verified: py_compile clean, pytest 81 passed 1 skipped.
