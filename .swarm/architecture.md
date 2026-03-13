# ShortCircuit — Architecture

## How to Read This File
This is the authoritative map of what every module owns.
If you are about to edit a file, check here first.
If a module is not listed here, ask the manager before assuming.
Do not assume the list below is complete — read the actual directory.

# ShortCircuit — Architecture Reference
**Version:** Phase 61 — Scanner & Gain Standardisation
 | **Last Updated:** 2026-03-13

---

## SECTION 1 — System Overview

ShortCircuit is a fully automated, event-driven algorithmic trading bot for NSE (Indian National Stock Exchange) equities, operating intraday-only (all positions closed by 15:10 IST). It implements a short-selling momentum reversal strategy, detecting and trading against institutional exhaustion at intraday highs. The strategy core is the **GOD MODE signal**: a multi-factor gate that requires simultaneous confirmation of exhaustion at stretch (9–18% intraday gain + new high + vol_fade < 0.65 + above VAH), RVOL spike (≥3×), volume-profile deviation (LTP vs. POC divergence), and **[Phase 61]** Math-First HTF Confluence (Z-Score stretch/stall/acceleration physics). Bearish patterns are bonus confidence scorers. A separate 12-gate validation framework monitors price in real-time and only fires execution when a **1-minute candle close** breaks the entry trigger.

The system infrastructure is: Python 3.10+ asyncio, Fyers API v3 (REST for quote batches and order submission; WebSocket for real-time tick data and order fill events), PostgreSQL + asyncpg for trade journaling, and python-telegram-bot (PTB) v20+ for the operator interface. The operator has no web UI — all signals, trade alerts, live P&L, commands (`/auto on`, `/status`, `/positions`), and EOD summaries flow exclusively through Telegram.

The concurrency model is a **single asyncio event loop** with a `TaskGroup` launching four concurrent tasks: `trading_loop`, `telegram_bot`, `reconciliation`, and `eod_scheduler`, plus the new `eod_watchdog` (Bug 2A fix). All tasks share a single `asyncio.Event` called `shutdown_event`. When any component sets this event, every `while not shutdown_event.is_set()` loop exits cleanly. The maximum signal limit per day is **5**, enforced by `SignalManager` with a 45-minute per-symbol cooldown. A consecutive-loss pause (3 losses) also halts new signals for the rest of the session. **[Phase 44.9.2 PRD-5]** SL tick-size rounding (`_round_sl_to_tick`) and capital release safety nets prevent capital lockouts from Fyers tick-size rejections. **[Phase 44.9.3 PRD-6]** Full manual trade lifecycle coverage: `adopt_orphan()` in `ReconciliationEngine` detects manual entries within 6 seconds, places a tick-rounded emergency SL, acquires the capital slot, and logs to DB — preventing infinite re-adoption loops. Manual exits detected via phantom handler which calls `_finalize_closed_position()` and sets `_db_dirty=True`.

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
**Notes:** `AUTO_MODE` is hardcoded `False` in `config.py` — cannot be enabled by env var; only `/auto on` Telegram command enables trading. Startup injects broker into scanner and subscribes to the scanner universe via WebSocket. Startup sequence includes: (a) `broker.seed_from_rest(scanner_symbols)` before WS subscribe — seeds cache so no symbols start as `Missing`; (b) startup validation gate — candle API HARD HALT, DB HARD HALT, WS SOFT WARN; (c) at trading loop start: auto queue resolution (`_auto_on_queued` → `auto_mode=True`) then `_send_morning_briefing()`. **[Phase 44.8.9]** `broker.set_telegram(telegram_bot)` is wired immediately after both broker (`await broker.initialize()`) and bot (`ShortCircuitBot(...)`) are constructed, enabling Telegram alerts from the broker health monitor daemon thread.

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
**Notes:** `AUTO_MODE` is overridden to `False` regardless of env var (explicit safety measure). `TRADING_ENABLED` is dynamically updated by `MarketSession` via `set_trading_enabled()`. **[Phase 51+52]** Added `P51_` flags for gate hardening and `P52_` flags for partial exits and safety cleanup.

---

### fyers_broker_interface.py
**Role:** Unified broker interface — manages Fyers REST API client and both WebSocket connections (Data WS for ticks, Order WS for fill events); exposes `place_order`, `get_positions`, tick subscriptions, and callback registration.
**Key Classes:** `OrderUpdate`, `PositionUpdate`, `TickData`, `FyersBrokerInterface`, `CacheEntry` (dataclass), `CacheEntrySource` (enum: `WS_TICK`, `REST_SEED`)
**Key Functions:** `get_quote_cache_snapshot`, `subscribe_scanner_universe`, `seed_from_rest`, `set_telegram`, `is_cache_severely_degraded`, `increment_degraded_scan_count`
**Imports from project:** None (imports Fyers SDK: `fyers_apiv3`, `fyers_apiv3.FyersWebsocket.data_ws`, `fyers_apiv3.FyersWebsocket.order_ws`)
**Called by:** `main.py`, `order_manager.py`, `reconciliation.py`, `trade_manager.py` (via fyers client), `focus_engine.py` (via FyersConnect)
**Calls into:** Fyers SDK (external), `database.py`
**State it owns:** `position_cache: dict[str, PositionUpdate]`, `_quote_cache: dict`, `_ws_subscribed_symbols_set: set`, `order_callbacks: list`, `tick_callbacks: list`, `data_ws`, `order_ws`, connection state flags, rate-limit tracking dict
**Error handling:** `_on_data_ws_error` / `_on_order_ws_error` log errors; WebSocket reconnect handled by Fyers SDK automatically; DNS errors (errno 11001) logged and SDK retries; `place_order` wraps REST call in try/except returning `None` on failure
**Notes:** Both WebSocket connections run in background daemon threads (blocking SDK calls); asyncio callbacks are scheduled via `asyncio.run_coroutine_threadsafe()` using `self._loop` captured via `asyncio.get_running_loop()` during `initialize()` — never `get_event_loop()` from a thread (which is unsafe in Python 3.12). WS import has a fallback if `setuptools==79.0.1` is not installed. Maintains a real-time `_quote_cache` from Data WS ticks via a formal UNINITIALIZED→PRIMING→READY state machine (Phase 44.9 — see SECTION 16). Health monitor daemon thread runs every 30s. **[Phase 44.8.9]** DEGRADED status is now determined solely by `fresh_pct` (WS-ticked symbols / total) — NOT `known_pct`. This fixes the trap where REST-seeded data kept `known_pct >= 90%` healthy even when no real WS ticks existed. `_severe_degraded_since` timestamp tracks continuous degradation; 30-second gate must expire before auto-recovery triggers. Max 3 reprime attempts per degraded episode; on 3rd failure transitions to UNRECOVERABLE and sends Telegram alert. Telegram alerts sent on: DEGRADED entry, RECOVERED, and UNRECOVERABLE. `set_telegram(bot)` wired from `main.py` after broker and bot are both initialized. `is_cache_severely_degraded()` returns bool — polled by `scanner.py` for DEGRADED MODE banner. `increment_degraded_scan_count()` called by scanner each cycle during degraded state. Tier freshness TTL sourced from `config.WS_TICK_FRESHNESS_TTL_SECONDS`. Tier selector: `WS_CACHE` when fresh ≥ threshold; `HYBRID` when `known_pct ≥ 90%`; `REST_EMERGENCY` only when truly unknown. 3 consecutive re-prime failures trigger a **nuclear full reconnect**: Data WS torn down completely, 5-second sleep, full re-subscribe + re-seed cycle. Guarded by `_reprime_failure_count` counter, reset to 0 on any successful re-prime. **[Phase 53]** WS Cache Data Erasure bug fixed by merging delta ticks with cached fields to preserve critical layout.

---

### order_manager.py
**Role:** Async order lifecycle manager — entry, SL placement, WebSocket fill detection, safe exit, capital allocation/release, and DB journaling. Primary execution path for all live trades.
**Key Classes:** `OrderManager`
**Key Functions:** `_round_sl_to_tick(price, side, tick=0.05)`, `close_partial_position()`, `modify_sl_qty()`, `safe_exit()`
**Imports from project:** `fyers_broker_interface.FyersBrokerInterface`
**Called by:** `focus_engine.py`, `telegram_bot.py`, `main.py`, `reconciliation.py`
**Calls into:** `fyers_broker_interface.py`, `database.py`, `capital_manager.py`, `telegram_bot.py`
**State it owns:** `active_positions`, `position_locks`, `exit_in_progress`, `hard_stops`, `_exec_cooldowns`
**Error handling:** Multi-stage try/except; SL placement isolation; capital release safety net.
**Notes:** **[Phase 51+52]** Added ATR-based SL calculation: `max(ATR * 0.5, 3 * tick)`. Implemented `close_partial_position()` for 40/40/20 TP strategy with G13 isolation. Added `modify_sl_qty()` for broker SL-M sync. Patched `safe_exit()` for cancel-first execution.

---

### focus_engine.py
**Role:** Signal validation gate and position monitor — maintains `pending_signals` dict, monitors price vs. trigger in a background thread, fires `order_manager.enter_position` on validation, and runs the `focus_loop` for active position management.
**Key Classes:** `FocusEngine`
**Key Functions:** `start_focus()`, `stop_focus()`, `focus_loop()`, `check_pending_signals()`
**Imports from project:** `fyers_connect.FyersConnect`, `config`, `order_manager.OrderManager`, `discretionary_engine.DiscretionaryEngine`
**Called by:** `main.py`, `telegram_bot.py`
**Calls into:** `order_manager.py`, `fyers_connect.py`, `discretionary_engine.py`, `telegram_bot.py`
**State it owns:** `pending_signals`, `cooldown_signals`, `active_trade`, `_event_loop`
**Error handling:** [P0 FIX] `order_manager` injection check. `CLOSED_EXTERNALLY` safety bypass for G13.
**Notes:** **[Phase 52]** Implemented Partial Exit Engine (40/40/20 strategy). Added cancel-first exit logic and `cleanup_orders()` in `stop_focus()`. Fixed async dispatch to `monitor_hard_stop_status` via `run_coroutine_threadsafe`.

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
**Notes:** Gain filter: 6–18%. Volume filter: >100k. LTP filter: `config.SCANNER_MIN_LTP` (default **₹50** — penny stocks below ₹50 are rejected at pre-filter before any API call, eliminating Fyers basket-rule rejections e.g. ESSARSHPNG ₹27, STEELXIND ₹8). `SCANNER_PARALLEL_WORKERS=3`. Symbol list cached — re-fetched from NSE master synchronously on startup via requests. WS cache vastly reduces REST API calls during scans. Candle history fetch uses `date_format="1"` with YYYY-MM-DD range strings and 5-day lookback. Tier freshness TTL sourced from `config.WS_TICK_FRESHNESS_TTL_SECONDS`. Tier selector: `WS_CACHE` when fresh ≥ threshold; `HYBRID` when `known_pct ≥ 90%`; `REST_EMERGENCY` only when truly unknown. **[Phase 44.8.9]** DEGRADED MODE banner: when `broker.is_cache_severely_degraded()` is `True`, `scan_market()` logs a `⚠️ DEGRADED MODE — cache severely degraded` warning banner every 10 scan cycles.

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
**Role:** HFT reconciliation engine — zero-cost when flat (pure WebSocket cache check), cache-driven when live. Detects orphaned, phantom, and mismatched positions between DB and broker. Handles full manual trade lifecycle: entry detection (orphan adoption), exit detection (phantom cleanup), and capital sync for both.
**Key Classes:** `ReconciliationEngine`
**Key Functions:** `adopt_orphan(broker_pos: dict)` — adopts a manually-entered broker position: places tick-rounded emergency SL, registers in `order_manager.active_positions`, logs to DB, sets `_db_dirty=True`, acquires capital slot (or emits TWO POSITIONS OPEN critical alert if slot occupied)
**Imports from project:** `database.DatabaseManager`, `fyers_broker_interface.FyersBrokerInterface`
**Called by:** `main.py` (`reconciliation_engine.run(shutdown_event)` in TaskGroup; `_cleanup_runtime` calls `stop()`)
**Calls into:** `fyers_broker_interface.py`, `database.py`, `telegram_bot.py` (alerts), `order_manager._finalize_closed_position()` (phantom handler), `capital_manager.acquire_slot()` / `release_slot()` (orphan adoption and phantom cleanup)
**State it owns:** `_db_positions: dict`, `_db_dirty: bool`, `_has_open_positions: bool`, `_shutdown_event: asyncio.Event`, `running: bool`, `capital: CapitalManager` (injected), `order_manager: OrderManager` (injected)
**Error handling:** [Bug 2B FIX] — `stop()` now logs structured messages; `run()` uses `_interruptible_sleep()` instead of bare `asyncio.sleep()`; `_cleanup_runtime()` wraps `stop()` in `asyncio.wait_for(..., timeout=10.0)`; DB query already has 1.5s timeout. `adopt_orphan()` has idempotency guard at top (symbol-in-active_positions check) AND in `_handle_divergence()` ORPHANS loop — two concurrent reconcile cycles cannot double-adopt the same position.
**Notes:** Market hours interval: 6s. Off-hours with positions: 30s. Fully flat off-hours: 300s. Dirty flag set by `TradeManager.mark_dirty()` on trade open/close. **[Phase 44.6]** `capital_manager` and `order_manager` injected at construction. **[PRD-5 / Phase 44.9.2]** Phantom handler (`_handle_divergence` PHANTOMS block) rewritten: now calls `order_manager._finalize_closed_position(reason='MANUAL_CLOSE_DETECTED')` instead of manual dict cleanup — ensures `db.log_trade_exit()` is called so DB position state changes OPEN→CLOSED. Capital release condition changed from `capital.active_symbol == sym` to `not capital.is_slot_free` (single-position bot: any phantom = slot must be free; old symbol-match could silently skip release). `_db_dirty = True` set after every phantom processed — forces fresh DB read next cycle, terminating the infinite phantom detection loop that would otherwise fire every 6s. Telegram alert updated: "GHOST POSITION CLEARED" → "MANUAL CLOSE DETECTED" with explicit PnL-not-tracked warning. **[PRD-6 / Phase 44.9.3]** `adopt_orphan()` fully rewritten. Three gaps closed: (1) GAP-1 critical: `adopt_orphan()` now calls `db.log_trade_entry()` and sets `_db_dirty=True` after adoption — without this, every 6-second cycle re-detected the same orphan and placed ~600 SL orders/hour; (2) GAP-2: idempotency guards added in both `_handle_divergence()` ORPHANS loop and inside `adopt_orphan()` itself — protects against concurrent reconcile cycles; (3) GAP-3: when capital slot is occupied (bot trade open) and manual trade detected simultaneously, previously logged WARNING only — now emits CRITICAL Telegram alert "TWO POSITIONS OPEN" with both symbol names and recommended action. `adopt_orphan()` SL price uses same tick-rounding logic as `_round_sl_to_tick()` (math.ceil for SHORT, math.floor for LONG, tick=0.05). `import math` at top-level imports.

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
**Role:** Live-synced capital tracker — source of truth is Fyers GET /funds (available_margin), never a hardcoded base_capital. Enforces single-position architecture via slot acquisition/release. 5× intraday leverage applied to real margin from Fyers.
**Key Classes:** `CapitalManager`
**Key Functions:** `sync(broker)` — pulls actual available_margin from Fyers /funds API and updates `_real_margin`; `compute_qty(symbol, ltp)` — computes maximum qty for full margin utilization with 2% safety buffer, returns `(qty, cost, margin_required)`; `acquire_slot(symbol)` — locks capital slot after confirmed fill, raises RuntimeError if slot already occupied; `release_slot(broker=None)` — releases slot after SL/TP/manual exit, calls `sync(broker)` outside lock to refresh margin for next trade
**Imports from project:** None
**Called by:** `order_manager.py` (`compute_qty`, `acquire_slot`, `release_slot`, `is_slot_free`, `get_slot_status`), `reconciliation.py` (`acquire_slot`, `release_slot`, `is_slot_free`, `active_symbol`), `telegram_bot.py` (`get_slot_status` for /status display), `main.py` (construction + `sync()` at startup)
**Calls into:** `fyers_broker_interface.py` (via `broker.get_funds()` in `sync()`), nothing else
**State it owns:** `_real_margin: float` (always from Fyers, never hardcoded), `_position_active: bool`, `_active_symbol: Optional[str]`, `_last_sync: Optional[datetime]`, `leverage: float` (default 5.0), `_lock: asyncio.Lock`
**Error handling:** `sync()` keeps last value on Fyers API failure (does not crash); `acquire_slot()` raises `RuntimeError` if slot occupied — caller must check `is_slot_free` first; `release_slot()` is idempotent (sets already-False to False, harmless). `_parse_fyers_funds()` handles 3 Fyers response shapes: `fund_limit` list (v3 standard), `equity` dict, and flat dict.
**Notes:** **[Phase 44.6 full rewrite — replaces hardcoded base_capital architecture]** `_real_margin` is live from Fyers /funds `id=2 "Available Balance"` entry. `buying_power = _real_margin * leverage`. `is_slot_free` property: `not _position_active`. `active_symbol` property: `_active_symbol`. `compute_qty()` applies 2% safety buffer (`safety_cap = _real_margin * 0.98`) and walks down qty until `margin_req <= safety_cap`. `acquire_slot()` protected by `asyncio.Lock` — thread-safe for concurrent coroutines. `release_slot()` calls `sync(broker)` OUTSIDE the lock to avoid deadlock while getting fresh margin for next trade. `sync()` schedule: session start, after every confirmed fill, after every position close (SL/TP/manual), every 5 minutes in health monitor heartbeat. `get_slot_status()` returns rich dict used by Telegram /status command. Legacy compatibility: `get_status()`, `can_afford()`, `allocate()` (DEPRECATED, logs warning), `release()` (DEPRECATED, logs warning) kept for backward compat — do not use in new code.

---

### signal_manager.py
**Role:** Daily signal gate — enforces max 5 signals/day, 45-min per-symbol cooldown, and 3-consecutive-loss auto-pause. Global singleton via `get_signal_manager()`.
**Key Classes:** `SignalManager`
**Key Functions:** `get_signal_manager`, `record_outcome()`
**Imports from project:** None
**Called by:** `analyzer.py`, `telegram_bot.py`, `order_manager.py` (G13)
**Calls into:** Nothing
**State it owns:** `signals_today`, `last_signal_time`, `consecutive_losses`, `is_paused`, `stats`
**Error handling:** Thread-safe via `threading.Lock()`; auto-resets on date change.
**Notes:** **[Phase 51]** G13 Trade Outcome Recording: `record_outcome()` updates win rate and consecutive losses. Daily symbol blacklist and 3-signal cap enforced.

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
**Role:** Macro context engine — determines NIFTY market regime (TREND_UP/TREND_DOWN/RANGE) and manages session-level blacklists. Exposes `morning_range_valid` flag and circuit-touched sets.
**Key Classes:** `MarketContext`
**Key Functions:** `should_allow_short()`, `is_circuit_touched()`
**Imports from project:** `symbols.NIFTY_50`, `config`
**Called by:** `analyzer.py`
**Calls into:** Fyers REST (NIFTY intraday data)
**State it owns:** `morning_high/low`, `daily_circuit_touched_set`, `morning_range_valid`
**Notes:** **[Phase 51]** G3: Session-permanent circuit hitter blacklist. G7: Time gate logic (pre-10AM, lunch-hour, and post-15:10 blocks).

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

### gate_result_logger.py
**Role:** Gate audit trail logger — buffers `GateResult` objects in-memory and flushes them in batches to the `gate_results` PostgreSQL table. Provides a JSON-Lines fallback on any DB failure so no audit record is ever lost.
**Key Classes:** `GateResultLogger`
**Key Functions:** `get_gate_result_logger` (singleton accessor), `_flush_batch`, `_sanitize_row`, `_flush_to_json_fallback`
**Imports from project:** `database.DatabaseManager`
**Called by:** `analyzer.py` (records G1–G9 outcomes per candidate), `focus_engine.py` (records G10–G12 outcomes on validation), `main.py` (calls `get_gate_result_logger().set_dsn(dsn)` at startup)
**Calls into:** `database.py` (async `executemany`), `aiofiles` (async fallback write)
**State it owns:** `_buffer: list[GateResult]`, `_dsn: str`, `_fallback_path: Path`
**Error handling:** `_flush_batch()` calls `_sanitize_row()` on every row before insert, then wraps `executemany` in `try/except`; on any DB exception falls through to `_flush_to_json_fallback()` — guarantees zero silent data loss.
**Notes:** **[Phase 44.8.9]** `_COLUMN_SPEC` dict defines the expected Python type for each of the 36 SQL parameters. `_sanitize_row()` coerces all values to their expected types before `executemany` (e.g. casts `g9_value` and `g11_value` to `str` since those columns are now `VARCHAR`). `_flush_to_json_fallback()` appends records to `logs/gate_fallback_YYYYMMDD.jsonl` using `aiofiles` for non-blocking I/O. Singleton via `get_gate_result_logger()`. `set_dsn()` called from `main.py` after DB pool is initialized.

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
**Role:** Higher-Time-Frame confluence checks — 15-minute structure confirmation and VWAP extensions.
**Key Classes:** `HTFAnalyzer`
**Key Functions:** `check_htf_structure()`, `check_vwap_extension()`
**Imports from project:** None
**Called by:** `analyzer.py` (G9)
**Calls into:** Fyers REST (15m candle history)
**Notes:** **[Phase 51]** G9 rebuilt: removed bullish candle requirement; added 1.5SD VWAP extension + volume fade checks for HTF confirmation.

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
- `v44_8_2_gate_results.sql` — Phase 44.9 schema: creates the `gate_results` table for full 12-gate audit trail.
- `v44_8_3_gate_results_g9_type_fix.sql` — **[Phase 44.8.9]** Alters `g9_value` and `g11_value` from `NUMERIC` to `VARCHAR(100)`. Run this migration before deploying Phase 44.8.9 code.
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
- `gate_fallback_YYYYMMDD.jsonl` — **[Phase 44.8.9 NEW]** JSON-Lines fallback written by `GateResultLogger._flush_to_json_fallback()` when DB `executemany` fails. One JSON record per line. Recovered via `tools/eod_reimport.py`.
- `rejections_YYYYMMDD.log` — Per-symbol gate rejection summary written at EOD by `GateResultLogger`.
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
- `eod_reimport.py` — **[Phase 44.8.9 NEW]** EOD recovery utility. Reads `logs/gate_fallback_YYYYMMDD.jsonl` (written during DB downtime) and re-imports records into `gate_results`. Run manually after market close: `python tools/eod_reimport.py`
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
| 16.8 | **[Phase 44.8.9]** `broker.set_telegram(telegram_bot)` — wires Telegram into broker health monitor for thread-safe DEGRADED/RECOVERED/UNRECOVERABLE alerts from daemon thread | — | — | — |
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
| 1.3 | Pre-filter per symbol: gain% 6–18%, volume >100k, LTP ≥ `config.SCANNER_MIN_LTP` (₹50), tick size valid, OI >0 |
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
**[Updated: Phase 44.6 + PRD-5]**

| Step | Action |
|------|--------|
| 4.1 | Auto mode gate: block if `telegram.is_auto_mode() == False` |
| 4.2 | LTP fetch: from signal dict or `broker.get_ltp(symbol)` fallback; abort if LTP=0 (5-min cooldown) |
| 4.3 | Sizing: `capital_manager.compute_qty(symbol, ltp)` → `(qty, cost, margin_req)` using real Fyers margin × 5× leverage with 2% safety buffer. Abort + alert if qty=0 |
| 4.4 | `broker.place_order(symbol, side, qty, MARKET)` — REST POST entry order, returns `entry_id` |
| 4.5 | Telegram: ENTRY ORDER PLACED alert with qty, cost, order_id |
| 4.6 | `broker.wait_for_fill(entry_id, timeout=15.0)` — WebSocket fill detection, 15s timeout |
| 4.7 | If fill timeout: attempt cancel → if cancel says "not a pending order" → REST verify fill (`_verify_fill_via_rest`) → if confirmed filled, use REST fill price. If genuinely unfilled: 20-min cooldown, return None |
| 4.8 | Fill confirmed: `capital_manager.acquire_slot(symbol)` — capital slot LOCKED (single-position gate now enforced) |
| 4.9 | SL price: `raw_sl = ltp * (1 ± sl_pct)` → `_round_sl_to_tick(raw_sl, side)` → tick-rounded to 0.05 boundary (ceil for SHORT, floor for LONG). Prevents Fyers code=-50 |
| 4.10 | SL placement in its own `try/except`: `broker.place_order(SL_MARKET, trigger=stop_price)`. On exception OR None return: emergency_exit + release_slot + 15-min cooldown + SL-specific Telegram alert. Return None. (SL errors never fall through to outer except) |
| 4.11 | `active_positions[symbol]` registered with full state dict: qty, side, entry_id, sl_id, status=OPEN, entry_price, stop_loss |
| 4.12 | `db_manager.log_trade_entry(data)` — atomic write to `orders` + `positions` tables |
| 4.13 | Outer `except` safety net: if exception fires after fill (e.g. DB error), `capital.release_slot()` called if `not is_slot_free` — ensures capital is never permanently locked by unexpected exceptions |

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
| G1 | SCANNER_QUALITY | analyzer.py | 45-candle minimum, 9% gain floor, quality checks | grl.record(REJECTED) |
| G2 | RVOL_VALIDITY | analyzer.py | ≥20 min since market open | grl.record(REJECTED) |
| G3 | CIRCUIT_GUARD | analyzer.py | Session-permanent circuit hitter blacklist | grl.record(REJECTED) |
| G4 | MOMENTUM | analyzer.py | VWAP slope FLAT (<0.05), no parabolic spikes | grl.record(REJECTED) |
| G5 | EXHAUSTION | analyzer.py | 9–14.5% gain, VAH relative ATR, MEDIUM pattern | grl.record(REJECTED) |
| G6 | PRO_CONFLUENCE| analyzer.py | Tiered scoring ≥2 (DPOC/OI/Tape), no auto-pass | grl.record(REJECTED) |
| G7 | TIME_GATE | analyzer.py | Pre-10AM, Lunch (12-1PM), Post-15:10 blocks | grl.record(REJECTED) |
| G8 | SIGNAL_LIMIT | analyzer.py | Max 5/day, 45-min cooldown, win-rate pause | grl.record(REJECTED) |
| G9 | MATH_FIRST_HTF| analyzer.py | Z-Score Stretch (>3.0) / Momentum Stall / Accel Reject | grl.record(REJECTED) |
| G10 | EXEC_PRECISION| focus_engine.py | Soft Gate: 0.4% spread CAUTIOUS logic, cleanup done | grl.record(REJECTED) |
| G11 | TIMEOUT | focus_engine.py | Fixed 15-min Expire, Late-session block removed | grl.record(REJECTED) |
| G12 | CANDLE_CLOSE | focus_engine.py | 1-min Candle Close < Trigger, 0.2% buffer invalidate | grl.record(REJECTED) |
| G13 | OUTCOME_LOG | signal_manager.py| Trade Outcome Recording (Win/Loss Tracking) | grl.record(LOGGED) |

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
| `INITIAL` | Entry fill confirmed | `max(ATR * 0.5, 3 * tick)` buffer above high | `order_manager.py` | `SL set @ {price}` |
| `BREAKEVEN` | TP1 Hit (40% qty) | Moved to Entry Price | `focus_engine.py` | `TP1 Hit: SL → BREAKEVEV` |
| `TP1_LEVEL` | TP2 Hit (40% qty) | Moved to TP1 Price | `focus_engine.py` | `TP2 Hit: SL → TP1` |
| `TRAILING` | TP3 Runner (20% qty) | Trails by ATR * 0.5 | `focus_engine.py` | `TRAILING SL → {price}` |
| `HARD_STOP` | SL-M order fills | Position force-closed | `order_manager.py` | `Hard stop filled` |

> **Note:** `USE_SCALPER_RISK_MANAGEMENT=False` — the scalper SL state machine is feature-flagged off. Active production SL uses `TradeManager.update_stop_loss()`.

---

### TP Structure / Exit Paths

| Exit Path | Trigger | Exit Size | Config Key | File / Method | Transport |
|-----------|---------|-----------|-----------|---------------|-----------|
| TP1 | +1.5x ATR | 40% of position | `TP1_ATR_MULT` | `order_manager.close_partial_position()` | REST |
| TP2 | +2.5x ATR | 40% more (80% total) | `TP2_ATR_MULT` | `order_manager.close_partial_position()` | REST |
| TP3 | +3.5x ATR | 20% remaining (Runner) | `TP3_ATR_MULT` | `order_manager.safe_exit()` | REST |
| EOD_SQUAREOFF | 15:10 IST (hard wall) | All open positions | `SQUARE_OFF_TIME` | `trade_manager.close_all_positions()` | REST |
| CAUTIOUS | Spread > 0.4% | 50% Size, 30% tight TP | `P51_G10_SPREAD_MAX_PCT` | `order_manager.enter_position()` | REST |
| EMERGENCY | `/emergency` | All positions | — | `trade_manager.emergency_exit()` | REST |

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

**[Phase 44.8.9 — migration `v44_8_3_gate_results_g9_type_fix.sql`]** `g9_value` and `g11_value` columns were `NUMERIC` and have been altered to `VARCHAR(100)`. The HTF confluence gate (G9) and cooldown-spacing gate (G11) return string rejection reasons (e.g. `"NO_HTF_LOWER_HIGH"`), not numbers. The old NUMERIC type caused `decimal.ConversionSyntax` errors that silently dropped 2,300+ gate records per session.

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
| `SCANNER_MIN_LTP` | float | `50.0` | `scanner.py` | Minimum LTP (₹) for symbol to pass Phase 1 pre-filter. Blocks all penny stocks from reaching order stage. |
| `RVOL_MIN_CANDLES` | int | `20` | `analyzer.py` | Min minutes since open for valid RVOL |
| `RVOL_VALIDITY_GATE_ENABLED` | bool | `True` | `analyzer.py` | Feature flag — set False to disable instantly |
| `USE_SCALPER_RISK_MANAGEMENT` | bool | `False` | `trade_manager.py` | Scalper SL system master switch |
| `SCALPER_STOP_TICK_BUFFER` | int | `12` | `scalper_risk_calculator.py` | Ticks above setup high for initial SL |
| `SCALPER_STOP_HUNT_BUFFER_ENABLED` | bool | `True` | `scalper_risk_calculator.py` | Enable 0.3% buffer above setup high for SL placement |
| SCALPER_STOP_HUNT_BUFFER_PCT | float | 

## Last Updated
07 Mar 2026 — initial architecture map created
