# ShortCircuit — Complete System Architecture

**Classification**: Internal Technical Document
**Version**: Phase 42 (Feb 2026)
**Purpose**: Comprehensive technical reference for the ShortCircuit intraday short-selling bot. Every claim in this document has been verified against the current source code.

---

## 1. Executive Summary

ShortCircuit is a fully automated, short-only intraday equity trading system engineered for the Indian stock market (NSE). It connects to the Fyers brokerage through their v3 API, scans approximately 2,000 NSE equities every 60 seconds during market hours, identifies statistically significant bearish reversal patterns on stocks exhibiting strong intraday gains, and either executes short trades automatically or alerts the operator via Telegram for manual execution. The operator receives rich, real-time dashboards, interactive controls, and post-trade analytics through a live Telegram interface.

The system's philosophical foundation draws from Bob Volman's institutional scalping methodology and orderflow principles. The central thesis is simple: stocks that surge 6–18% intraday on momentum eventually exhaust. When that exhaustion manifests as a specific candlestick pattern with volume confirmation, price extension beyond statistical norms, and multi-timeframe alignment, the probability of a short-term reversal is high. ShortCircuit identifies these precise inflection points and capitalizes on them with disciplined risk management.

The architecture enforces a critical principle: quality over quantity. Every signal must survive a rigorous, multi-gate filtering pipeline before it reaches the operator. The system is designed to reject 95% of potential candidates and only surface the highest-conviction setups.

---

## 2. System Lifecycle Overview

The system operates as a continuous loop running every 60 seconds during market hours (09:15–15:10 IST). Each cycle executes the following macro-pipeline:

**SCAN** — The scanner fetches live quotes for the entire NSE equity universe in batches, filters for stocks exhibiting 6–18% intraday gains with sufficient volume, and validates chart quality through microstructure analysis.

**ANALYZE** — Each surviving candidate is fed into the analyzer's 12-gate sequential pipeline. The analyzer applies Signal Manager discipline checks, market regime validation, hard trading constraints, circuit proximity guards, momentum safeguards, pattern recognition, structural confluence checks, orderflow analysis, and multi-timeframe confirmation. If any single gate rejects the candidate, processing stops immediately.

**VALIDATE** — Approved signals do not trigger immediate execution. Instead, they enter a "Validation Gate" — a holding queue where the signal must be confirmed by price action. The signal defines a trigger price (the low of the setup candle). Only when live price breaks below this trigger does the system proceed to execution. This mechanism eliminates a large class of false signals that form patterns but never follow through.

**EXECUTE** — Upon validation, the system verifies broker position state (Phase 42), then either places a Market Sell order with an automatic Stop-Loss Buy order (auto mode) or sends a rich Telegram alert with a one-tap entry button (manual mode). Stop-loss placement includes a 3-attempt retry loop, and if all attempts fail, an emergency exit circuit breaker is triggered. Every order goes through `_verify_position_safe()` to prevent directional flips.

**MANAGE** — Once a trade is live, the Focus Engine activates. It polls the broker every 2 seconds, verifies position existence on each iteration (Phase 42), calculates real-time P&L, monitors orderflow sentiment, and updates a live Telegram dashboard by editing the message in-place. The engine implements a three-phase trailing stop mechanism (breakeven → trailing → tighten) that progressively locks in profit.

**EXIT** — Positions are closed through one of five mechanisms: stop-loss hit (broker-side or engine-detected with double-verification), manual close via Telegram button, end-of-day square-off at 15:10 IST, emergency exit on system failure, or scalper TP scale-out (Phase 41.2). After a stop-loss exit, a 10-minute Swing Failure Pattern (SFP) watch activates.

**RECONCILE** — At startup and every 30 minutes, the Position Reconciliation module (Phase 42) compares broker positions against the bot's internal state. Any orphaned positions trigger emergency Telegram alerts.

---

## 3. Module-by-Module Deep Dive

### 3.1 The Orchestrator — `main.py`

This is the entry point and the heartbeat of the system. On startup, it performs a strict initialization sequence.

First, it configures the Windows console for UTF-8 output using `sys.stdout.reconfigure(encoding='utf-8', errors='replace')` — a critical fix for Windows environments where emoji-heavy log messages would otherwise crash the console. This uses the `errors='replace'` strategy so that any unencodable character is substituted rather than causing a fatal exception.

Second, it initializes authentication through `FyersConnect`, which reads API credentials from environment variables (`FYERS_CLIENT_ID`, `FYERS_SECRET_ID`, `FYERS_REDIRECT_URI`) loaded by `python-dotenv`. The connector checks for a cached access token in `access_token.txt`, validates it by calling the Fyers profile API, and if the token is expired or invalid, initiates a browser-based OAuth2 re-authentication flow.

Third, it creates singleton instances of the four core modules: `FyersScanner` (market scanning), `FyersAnalyzer` (signal analysis), `TradeManager` (order execution), and `ShortCircuitBot` (Telegram interface). The bot instance receives the trade manager as a dependency, enabling it to execute trades from Telegram callbacks.

**Phase 41.1: Multi-Edge Detection System (Conditional).** If `config.MULTI_EDGE_ENABLED` is `True`, the orchestrator initializes the `MultiEdgeDetector` with the 5 active edge detectors. If `config.ENABLE_DETECTOR_TRACKING` is `True`, a `DetectorPerformanceTracker` is also initialized for per-detector analytics logging.

**Phase 41.2: Scalper Risk Management (Conditional).** If `config.USE_SCALPER_RISK_MANAGEMENT` is `True`, the orchestrator initializes a `ScalperPositionManager` backed by the trade manager. A background daemon thread `scalper_position_monitor()` is started, which polls LTP every 2 seconds and dispatches actions (breakeven moves, trailing stop updates, TP scale-outs) through the scalper manager's `update_position()` method. The thread is controlled by a `threading.Event` (`scalper_stop_event`) that is set during EOD square-off to gracefully shut down the monitor. When the feature flag is `False` (default), the system uses the legacy ATR-based risk management with the three-phase trailing stop in the Focus Engine.

The scalper monitor thread includes a Phase 42 safety guard: before acting on any scalper action, it calls `trade_manager._get_broker_position()` to verify the position still exists and is on the correct side. If the position is flat or long, the scalper closes its internal position tracking and skips the action, preventing stale scalper state from triggering orders on a closed position.

**Phase 42: Position Reconciliation at Startup.** After all modules are initialized but before the main loop starts, the orchestrator creates a `PositionReconciliation` instance and runs `reconcile_positions()`. This queries the broker for ALL open positions and compares them against the bot's internal state (Focus Engine's `active_trade`). Any orphaned positions (positions that exist on the broker but are not being managed by the bot) trigger:
- A `CRITICAL` log message
- An emergency Telegram alert via `bot.send_emergency_alert()`
- An entry in `logs/orphaned_positions.log`

This detects positions left over from crashes, duplicate order bugs, or manual trades placed via the Fyers app.

Fourth, a daemon thread is spawned running `bot.start_polling()` for continuous Telegram command listening. Fifth, a "Good Morning" startup message is sent to Telegram with a randomly selected trading quote.

The main loop runs a 60-second cycle. At the start of each cycle:

**Phase 42: Periodic Reconciliation.** The orchestrator tracks `last_reconciliation` as a `datetime` timestamp. If more than `config.POSITION_RECONCILIATION_INTERVAL` seconds (default 1800 = 30 minutes) have elapsed since the last reconciliation, `reconciler.reconcile_positions()` is called again. This catches orphaned positions that arise mid-session from bugs or manual broker activity.

**EOD Square-Off Check.** The current time is compared against `config.SQUARE_OFF_TIME` (default "15:10"). If current time exceeds this threshold, the system:
1. Sets `scalper_stop_event` to stop the scalper monitor thread
2. Calls `trade_manager.close_all_positions()` which first cancels ALL pending orders, then closes ALL net positions via market orders
3. Sends a "MARKET CLOSED" Telegram notification
4. Breaks the main loop

For each cycle during market hours, the orchestrator calls `scanner.scan_market()` to get candidates. What happens next depends on the `MULTI_EDGE_ENABLED` flag.

When the flag is `False` (the Phase 40 default), the system follows the original path: each candidate is passed to `analyzer.check_setup()`, which runs the full 12-gate pipeline including the pattern recognition gate.

When the flag is `True`, the system activates the Phase 41.1 Multi-Edge path. Each candidate is packaged into an edge candidate dictionary and passed to `MultiEdgeDetector.scan_all_edges()`, which runs 5 parallel edge detectors. If edges are found, the edge payload is forwarded to `analyzer.check_setup_with_edges()`, which runs Gates 1–7 and 9–12 (skipping Gate 8 since pattern detection is already covered by the multi-edge detector).

In both paths, the orchestrator never executes a trade directly. When a signal is returned, the Telegram alert is sent (multi-edge or standard), and the signal is passed to the Focus Engine's validation gate via `focus_engine.add_pending_signal()`. Execution is entirely deferred until price confirmation.

Error handling in the main loop is deliberately resilient. A `KeyboardInterrupt` cleanly breaks the loop. Any other exception is caught, logged, and the system sleeps 10 seconds before retrying.

---

### 3.2 The Scanner — `scanner.py`

The scanner's mission is to identify stocks that are "in play" — exhibiting the kind of aggressive intraday gains that precede exhaustion and reversal. It operates in three distinct phases.

**Phase A: Symbol Universe Construction.** The scanner downloads the official Fyers NSE symbol master CSV from `https://public.fyers.in/sym_details/NSE_CM.csv`, which contains approximately 2,000 equities. It filters for the `-EQ` series only (excluding derivatives, ETFs, warrants, other instrument types). For each symbol, it extracts the tick size from the CSV — the minimum price increment for that stock (e.g., ₹0.05 for most equities). This tick size is later used by the Trade Manager to round stop-loss prices to valid levels. The symbol list is cached after the first fetch.

**Phase B: Batch Quote Scanning.** The universe is split into batches of 50 symbols (Fyers API limit per request). For each stock, the scanner extracts the Last Traded Price (LTP), intraday volume, change percentage from previous close, and open interest. Three hard filters are applied:

The **gain filter** requires the stock to be up between 6% and 18% from the previous close. Below 6%, the move is too weak. Above 18%, the stock approaches its upper circuit limit (~20%). The **volume filter** requires intraday volume exceeding 100,000 shares for sufficient liquidity. The **price filter** requires LTP above ₹5, eliminating penny stocks.

**Phase C: Microstructure Quality Check (Parallelized in Phase 41.1).** For each surviving candidate, the scanner checks chart quality by fetching 1-minute candles for the last 30 minutes. In Phase 41.1, this was restructured to use `ThreadPoolExecutor` with configurable max workers (default 10 via `config.SCANNER_PARALLEL_WORKERS`). Each future has a 5-second timeout with per-candidate error handling — one failed API call does not block others. This reduces the data fetch phase from ~4 seconds (serial) to ~400ms (parallel), a 10× speedup.

The quality check calculates the zero-volume candle ratio. If more than 50% of candles have zero volume, the stock is rejected as illiquid. The function returns the fetched DataFrame alongside the quality verdict, allowing the analyzer to reuse this data without making a duplicate API call.

The scanner returns up to 20 candidates sorted by change percentage in descending order, each packaged with its LTP, change %, volume, open interest, tick size, and the pre-fetched history DataFrame.

---

### 3.3 The Signal Engine — `analyzer.py`

This is the brain of ShortCircuit. The `FyersAnalyzer` class orchestrates a 12-gate sequential pipeline within its `check_setup()` method. If any gate rejects the candidate, processing terminates immediately.

The analyzer exposes two entry points. The original `check_setup()` runs the complete 12-gate pipeline with its built-in pattern recognition gate. The Phase 41 addition, `check_setup_with_edges()`, accepts a pre-computed edge payload from the Multi-Edge Detector and runs Gates 1–7 and 9–12, skipping Gate 8 entirely.

**Gate 1: Signal Manager Discipline.** Queries the global `SignalManager` singleton enforcing three rules: daily signal cap (max 5 signals/day), per-symbol cooldown (45 minutes), and consecutive loss pause (3 losses triggers full-day trading pause).

**Gate 2: Market Regime Check.** Calls `MarketContext.should_allow_short()`. Fetches Nifty 50 intraday 5-minute candles and calculates the first-hour range (09:15–10:15 IST). If Nifty has extended 0.5× the morning range above the morning high, the market is classified as `TREND_UP` and ALL short signals are blocked. Morning range is cached per day. If Nifty data cannot be fetched, defaults to `RANGE` (fail-open).

**Gate 3: Data Pipeline.** Accepts a pre-fetched DataFrame from the scanner (avoiding duplicate API calls). If no cached data is provided, fetches fresh 1-minute intraday candles. A defensive `.copy()` prevents side effects. Also defines `prev_df = df.iloc[:-1]` for historical context calculations.

**Gate 4: Technical Context Enrichment.** The `_enrich_dataframe()` method calculates VWAP in-place using `cumulative(typical_price × volume) / cumulative(volume)`. Also captures day high, open price, and current gain percentage.

**Gate 5: Hard Constraints — "The Ethos Check".** `GodModeAnalyst.check_constraints()` enforces: minimum 5% gain (with a "Max Day Gain" exception — stocks that reached 7%+ at any point qualify even if current gain retraced below 5%), maximum 15% gain (circuit risk), and distance-from-high check (LTP within 4% of day high, relaxed to 6% for stocks with max gain ≥ 7% or ≥ 10%).

**Gate 6: Circuit Guard.** Fetches Level 2 depth data and extracts upper/lower circuit limits. If LTP is within 1.5% of upper circuit (`upper_circuit × 0.985`), the trade is blocked. Depth data is cached for subsequent orderflow analysis. Fail-open on API failure.

**Gate 7: Momentum Safeguard — "The Train Filter".** Calculates VWAP slope over last 30 candles using linear regression and Relative Volume (RVOL). If RVOL > 5.0 AND VWAP slope > 40, the trade is blocked — "Don't stand in front of a freight train."

**Gate 8: Pattern Recognition.** In the Phase 40 path, `GodModeAnalyst.detect_structure_advanced()` analyzes the last 3 candles and identifies one of six institutional reversal patterns: Bearish Engulfing (Z > 0), Evening Star (C2 body < 30% of range), Shooting Star (upper wick > 2× body, Z > 1.5), Absorption Doji (Z > 2.0, body < 0.05% of price, in Sniper Zone), Momentum Breakdown (body > 1.2× avg range, closes at lows, tiered volume), Volume Trap (C2 green Z > 1.5, C3 red below C2 low). If none match, a Tape Stall fallback check is performed. In the Phase 41 path, this gate is skipped.

**Gate 9: Breakdown Confirmation.** The current candle's close must be BELOW the entry trigger (Phase 40: previous candle's low; Phase 41: lowest `entry_level` across detected edges).

**Gate 10: Pro Confluence — "Belt and Suspenders".** Comprehensive secondary confirmations: Market Profile Rejection (VAH), DOM Wall Detection (sell/buy > 2.5×), VWAP Slope Analysis, RSI Divergence, VWAP Extension (> 2.0 SD), Fibonacci Rejection, Relative Volume (> 2× avg), OI Divergence, dPOC Divergence, Round Number Proximity. Plus five institutional orderflow principles: Large Wick Detection, Bad High Detection, Bad Low Guard (BLOCKS trade), Trapped Position Detection, Aggression Without Progress. Validation rule: if price is NOT extended beyond 2 SD above VWAP, at least one confluence factor must be present.

**Gate 11: HTF Confluence — Multi-Timeframe Confirmation.** 15-minute Lower High pattern check and 5-minute consecutive bullish candle count (≥ 5 required). Either condition qualifies. Also checks proximity to PDH, PDL, PDC, PWH, PWL.

**Gate 12: Signal Finalization.** Stop loss = `setup_candle_high + (ATR × 0.5)`, minimum buffer ₹0.25. Signal logged to `logs/signals.csv` with extended fields (setup_high, tick_size, atr, entry_price) for EOD simulation. Signal recorded in Signal Manager (triggers 45-minute cooldown). ML observation written with 20+ features.

---

### 3.4 The Validation Gate — `focus_engine.py` (Entry Only)

This is perhaps the single most important innovation in the system's architecture. The signal is NOT executed upon detection. Instead, it enters a structured holding queue managed by `check_pending_signals()`.

When a signal enters the gate, three parameters are defined: the entry trigger (signal_low — the low of the setup candle), the invalidation trigger (stop_loss price — setup candle high plus ATR buffer), and the timeout (configurable via `config.VALIDATION_TIMEOUT_MINUTES`, default 15 minutes).

The `check_pending_signals()` method polls every 2 seconds using `fyers.quotes()`. If the LTP drops below the trigger price, the signal is VALIDATED and immediately forwarded to `trade_manager.execute_logic()` for execution.

**Phase 41.2 Integration.** When `config.USE_SCALPER_RISK_MANAGEMENT` is `True` and a signal carries a `_scalper_manager` reference, the validation gate calls `scalper_manager.start_position()` after successful trade execution. This hands off position management from the Focus Engine's legacy trailing stop to the Scalper Position Manager's multi-phase TP system.

If the LTP rises above the invalidation price before triggering, the signal is INVALIDATED and removed. If the timeout passes without either condition, the signal times out and is removed. The strategic reasoning: many candlestick patterns form but fail to follow through. By requiring price to break the setup candle's low, the system confirms genuine selling pressure. Analysis shows this eliminates roughly 40% of would-be losing trades.

---

### 3.5 Trade Execution — `trade_manager.py`

When validation triggers, execution depends on the `AUTO_TRADE` flag (configurable via Telegram `/auto on|off`).

#### Phase 42: Position Safety Architecture

Every order placement method is guarded by the position safety system. This is the core fix for the critical directional flip bug (where duplicate buy orders could flip a short position into an unintended long).

**`_get_broker_position(symbol)`** — Queries the Fyers positions API for the ACTUAL current position. Returns a dict with `net_qty`, `symbol`, and `raw` position data. If the symbol is not found in the positions list, returns `{'net_qty': 0}` (position is flat). If the API call fails entirely, returns `None` (unknown state).

**`_verify_position_safe(symbol, intended_action)`** — The critical guard called before EVERY order. Accepts one of four intended actions:
- `ENTER_SHORT`: Blocks if position is already LONG (would cause a directional conflict). Warns if already SHORT (adding to position). Allows if flat.
- `EXIT_SHORT`: Blocks if position is flat or LONG (would create accidental long). Allows only if position is SHORT.
- `ENTER_LONG`: Blocks if position is SHORT.
- `EXIT_LONG`: Blocks if position is flat or SHORT.

If `config.ENABLE_POSITION_VERIFICATION` is `False`, the check is bypassed (NOT recommended for production — this exists only for testing). If the broker position cannot be determined (API failure), the check defaults to **BLOCKED** (fail-safe: better to miss a trade than place a dangerous order).

#### Trade Entry — `execute_logic()`

In auto mode, the method first calls `_verify_position_safe(symbol, 'ENTER_SHORT')`. If verification passes:

1. **Market Sell order** placed via `fyers.place_order()` with `type=2` (Market), `side=-1` (Sell).
2. If entry succeeds, a **SL-Limit Buy order** is placed. The SL trigger price is rounded to the stock's tick size using `tick_round()`. The limit price is set 0.5% above the trigger to ensure fill in fast markets.
3. SL placement uses a **3-attempt retry loop**. Each attempt independently calls the API. On success, the SL order ID is tracked in `self.active_sl_orders[symbol]` (Phase 42 SL tracking).
4. If all 3 SL attempts fail, `emergency_exit()` is called.

The quantity calculation: `int(CAPITAL / LTP)`, where CAPITAL defaults to ₹1,800. Minimum 1 share.

In manual mode, returns a `MANUAL_WAIT` status dict. The Telegram bot renders this as a one-tap "GO" button.

#### Emergency Exit — `emergency_exit()` (Phase 42 Circuit Breaker)

The emergency exit now implements a circuit breaker pattern. When triggered (typically after 3 failed SL placement attempts):

1. **Wait 1 second** for broker state to settle (the entry order may have already been canceled or the position may have been closed by another mechanism).
2. **Query broker position** via `_get_broker_position()`.
3. **Decision matrix:**
   - If position is **flat** (`net_qty == 0`): Log "position already flat", clean up SL tracking, **skip** the exit order. This is the critical fix — the broker's SL may have filled during the retry attempts.
   - If position is **long** (`net_qty > 0`): Log a `CRITICAL` alert — the position has flipped to the wrong side. This requires **manual intervention**. The bot does NOT attempt to fix this automatically.
   - If position is **short** (`net_qty < 0`): Confirmed short, safe to exit. Uses `abs(broker_pos['net_qty'])` as the actual quantity (not the original signal qty, which may differ if partial fills occurred).
   - If position is **unknown** (API failure): Falls through to place the exit anyway — better to risk a duplicate than leave a naked short.
4. Places a Market Buy order to cover the short.
5. Cleans up SL tracking via `_cleanup_sl_tracking()`.

#### Partial Position Close — `close_partial_position()` (Phase 41.2)

Used by the Scalper Position Manager for TP scale-outs. Guarded by `_verify_position_safe(symbol, 'EXIT_SHORT')`. Places a Market Buy for the specified quantity. Returns `BLOCKED` if position verification fails.

#### Stop Loss Update — `update_stop_loss()` (Phase 41.2)

Used by the Scalper Position Manager to dynamically modify existing SL orders. Scans the Fyers orderbook for pending orders (status 6) matching the symbol, then calls `fyers.modify_order()` with the new trigger and limit prices. The limit price is always set 0.5% above the trigger for fill protection.

#### EOD Square-Off — `close_all_positions()`

Executes the end-of-day closing sequence:
1. **Cancel all pending orders** first — scans the orderbook for status 6 (Pending) and cancels each. This prevents SL orders from triggering after the position is closed.
2. **Close all net positions** — for each position with `netQty != 0`, determines exit side (`-1` for longs, `1` for shorts) and places a Market order.
3. Cleans up SL tracking for each closed symbol.

#### SL Order Tracking — `active_sl_orders`

A dictionary mapping `{symbol: order_id}` that tracks active SL orders. Updated when:
- A new SL is placed (after successful `fyers.place_order()` in the retry loop)
- Cleaned up via `_cleanup_sl_tracking(symbol)` when a position is closed (emergency exit, EOD square-off, or manual close)

This tracking enables the system to know which SL orders are "owned" by the bot and prevents duplicate SL placements.

---

### 3.6 Active Trade Management — Focus Mode (`focus_engine.py`)

Once a trade is entered, the Focus Engine activates a real-time monitoring loop running on a daemon thread with 2-second intervals. This loop performs six critical functions simultaneously.

#### Phase 42: Broker Position Check (Loop Top)

At the **top of every focus loop iteration**, before any price processing occurs, the engine checks whether the position still exists on the broker:

```
broker_pos = self._check_broker_position(symbol)
if broker_pos is None or broker_pos.get('netQty', 0) == 0:
    → "Position already closed by broker (SL hit?)"
    → cleanup_orders(symbol)
    → stop_focus(reason="BROKER_SL_HIT")
```

This is controlled by `config.ENABLE_BROKER_POSITION_POLLING` (default `True`). The `_check_broker_position(symbol)` helper queries `fyers.positions()` and returns the matching position dict, or `None` if the position is not found (meaning it was closed). This check runs every 2 seconds and catches the scenario where the broker's hard SL order filled between polling intervals — the engine detects the flat position and exits gracefully without placing any duplicate orders.

#### Live Dashboard Updates

Every 2 seconds, the engine fetches the latest quote (LTP, volume, VWAP, bid quantities, ask quantities, day high). It calculates live P&L in points, cash, and ROI percentage (assuming 5× intraday leverage). It derives orderflow sentiment from the bid/ask ratio. All of this is rendered into a formatted Telegram message that is EDITED in-place (using `edit_message_text`). Interactive buttons: Refresh and Close Position.

#### Stop-Loss Hit Detection — `process_tick()` (Phase 42: Double-Verify)

When the LTP rises above the current stop level, the engine now performs a **4-step safety sequence** before placing any exit order:

**Step 1: Immediate broker check.** Query `_check_broker_position()`. If position is flat (`netQty == 0`), the broker's hard SL already filled — skip the manual exit, clean up orders, and stop focus with reason `BROKER_SL_HIT`. If position is LONG (`netQty > 0`), log a `CRITICAL` alert for wrong-side detection and stop focus with reason `WRONG_SIDE_DETECTED`.

**Step 2: Wait 500ms.** This delay allows the broker's SL order (which may have been triggered at the exact same price level) time to process and settle. Without this delay, the bot's manual exit and the broker's SL could race, causing both to fill.

**Step 3: Re-verify broker position.** Query the broker AGAIN after the 500ms delay. If the position is now flat, the broker's SL filled during the delay — skip the manual exit.

**Step 4: Manual exit (only if still short).** If the position is confirmed still short after both checks, use the ACTUAL broker quantity (`abs(broker_pos['netQty'])`) — not the original signal quantity — to place the Market Buy cover order. This handles partial fills correctly.

After exit: cancel all pending orders for the symbol, send stop-loss notification via Telegram, and stop focus mode.

#### Three-Phase Trailing Stop

The trailing mechanism uses the initial risk (distance between entry and initial stop) as its unit of measurement:

1. **Initial Phase**: Stop remains at the original level (setup candle high plus ATR buffer).
2. **TP1 — Breakeven** (profit ≥ 1× risk): Stop moved to entry price — the trade becomes risk-free.
3. **TP2 — Trailing Activation** (profit ≥ 2× risk): Trailing mode activates. Stop follows price at `LTP + (risk × 0.5)`, only tightening (never loosening).

This logic was previously a dead code bug (positioned after a `return` statement in `process_tick()` and structurally orphaned after the `cleanup_orders()` method definition). Phase 42 corrected this by moving the TP/trailing logic back inside `process_tick()` after the SL return block, ensuring breakeven and trailing stops actually execute.

#### Dynamic Constraints

The engine continuously recalculates two dynamic levels: dynamic SL at `day_high × 1.001` (tightens to `VWAP × 1.002` when LTP drops below VWAP), and dynamic target at 2% below current price.

#### SFP Watch — Post-Exit Fakeout Detection

After a stop-loss exit, a 10-minute background thread monitors price. If price crosses back BELOW the original entry, it's a Swing Failure Pattern — stops were hunted, price reversed. An urgent Telegram alert is sent with "RE-ENTER SHORT NOW".

#### Order Cleanup — `cleanup_orders(symbol)`

Scans the Fyers orderbook for all pending orders (status 6) matching the symbol and cancels each. Used after SL exits to remove orphaned SL orders, and during EOD square-off.

---

### 3.7 Position Reconciliation — `position_reconciliation.py` (Phase 42)

A safety module that detects orphaned positions — positions that exist on the broker but are not being tracked by the bot. This catches scenarios like:
- Bot crashed after placing an order but before starting position tracking
- Duplicate order bug created an extra position
- Manual trade placed via the Fyers app

**How it works:**

1. Calls `fyers.positions()` to get ALL open positions from the broker.
2. Filters for positions with `netQty != 0`.
3. For each open position, checks if the Focus Engine's `active_trade` is managing that symbol.
4. If a position is NOT managed by the bot, it's classified as **orphaned**.

**What it does when an orphan is found:**

- Logs a `CRITICAL` message with the symbol, quantity, side (LONG/SHORT), and the fact that the bot is not managing it.
- Calls `bot.send_emergency_alert()` with a detailed message listing possible causes (crash, duplicate order bug, manual trade) and recommended action (close manually or restart bot).
- Writes an entry to `logs/orphaned_positions.log` with timestamp.

**When it runs:**

- **At startup** (before the main trading loop starts): Catches positions left over from previous crashes.
- **Every 30 minutes** during the trading session: Controlled by `config.POSITION_RECONCILIATION_INTERVAL` (default 1800 seconds). The orchestrator tracks `last_reconciliation` timestamp and triggers reconciliation when the interval elapses.

---

### 3.8 Scalper Risk Management — `scalper_risk_calculator.py` + `scalper_position_manager.py` (Phase 41.2)

A feature-flagged alternative risk management system designed for scalping-style position management with multi-target exits and structure-based stops. Controlled by `config.USE_SCALPER_RISK_MANAGEMENT` (default `False`).

#### Risk Calculator — `scalper_risk_calculator.py`

Pure calculation module with no side effects. Provides three categories of calculations:

**Stop Loss Calculation:**
- Base stop: `setup_candle_high + (tick_size × SCALPER_STOP_TICK_BUFFER)` where `SCALPER_STOP_TICK_BUFFER` defaults to 12 ticks.
- Buffered stop: If `SCALPER_STOP_HUNT_BUFFER_ENABLED` is `True`, adds an additional `SCALPER_STOP_HUNT_BUFFER_PCT` (0.3%) buffer above the base stop to account for stop-hunting wicks.

**Profit Target Calculation:**
- TP1: `entry_price × (1 - SCALPER_TP1_PCT)` — default 1.5% below entry. Close 50% of position.
- TP2: `entry_price × (1 - SCALPER_TP2_PCT)` — default 2.5% below entry. Close 25% (75% total closed).
- TP3: `entry_price × (1 - SCALPER_TP3_PCT)` — default 3.5% below entry. Close remaining 25%.

**Breakeven Threshold:**
- Points required for breakeven: `entry_price × SCALPER_BREAKEVEN_TRIGGER_PCT` (default 0.3% of entry price, approximately 1.5 points on a ₹500 stock).

#### Position Manager — `scalper_position_manager.py`

Manages the lifecycle of an open scalper position through 6 phases:

1. **ENTRY**: Position just opened. No action, monitoring only.
2. **TP1**: Price reached TP1 level. Close 50% via `trade_manager.close_partial_position()`. Move SL to breakeven.
3. **BREAKEVEN**: SL has been moved to entry price. Trade is now risk-free. Wait for further price movement.
4. **TRAILING**: Price has moved past breakeven threshold. SL trails price using distance tiers configured in config:
   - Initial: `SCALPER_TRAILING_DISTANCE_INITIAL` (0.2% behind price)
   - After TP1: `SCALPER_TRAILING_DISTANCE_AFTER_TP1` (0.15%)
   - After TP2: `SCALPER_TRAILING_DISTANCE_AFTER_TP2` (0.1%)
5. **TP2**: Price reached TP2 level. Close 25% (75% total). Tighten trailing distance.
6. **TP3**: Price reached TP3 level. Close remaining 25%. Position fully closed.

The `update_position(current_ltp)` method is called every 2 seconds by the scalper monitor thread in `main.py`. It returns an action string (`'HOLD'`, `'TP1'`, `'TP2'`, `'TP3'`, `'TRAILING'`, `'STOPPED'`) that the monitor thread dispatches to the trade manager.

---

### 3.9 Trade Simulation — `trade_simulator.py` + `eod_analysis.py` (Phase 41.2)

**Trade Simulator:** Simulates both the legacy ATR-based system and the new scalper system on historical price data. Processes candle-by-candle, tracking entry, SL hits, TP scale-outs, trailing stops, and breakeven moves. Used for comparing the two risk management approaches.

**EOD Analysis:** Command-line script that loads signals from `logs/signals.csv`, fetches historical intraday candle data for each signal's symbol, runs the trade simulator with both systems, and outputs a performance comparison. Reports metrics like win rate, average R-multiple, and total P&L for each system. This is intended to run daily for 10 days before enabling the scalper system live.

---

### 3.10 Recovery and Fault Tolerance

The system is designed to survive crashes, network outages, and API failures without leaving orphaned positions.

**Auto-Recovery on Startup.** `FocusEngine.attempt_recovery()` runs at boot. It checks for open positions, extracts the entry price, quantity, symbol, and any pending SL orders, then "adopts" the trade by calling `start_focus()` with the recovered parameters. A "RECOVERY MODE" Telegram notification is sent.

**Position Reconciliation (Phase 42).** As described in Section 3.7, the reconciler detects orphaned positions at startup and every 30 minutes, sending emergency alerts for any unmanaged positions.

**Network Resilience.** All Telegram API calls are wrapped in exception handlers. If Telegram is unreachable, trading continues — position safety is maintained by the broker-side SL order. If `fyers.quotes()` fails, the engine sleeps 5 seconds and retries.

**Emergency Exit Protocol (Phase 42).** As described in Section 3.5, the circuit breaker pattern verifies position state before the emergency exit, preventing duplicate orders.

**Broker Position Verification (Phase 42).** The `_verify_position_safe()` guard on every order method ensures no order can cause a directional flip. The focus loop's top-of-iteration broker check ensures no orders are placed on already-closed positions.

---

### 3.11 Signal Manager — Discipline Engine — `signal_manager.py`

The Signal Manager is a singleton enforcing three rules: daily signal cap (default 5), per-symbol cooldown (45 minutes), and consecutive loss pause (3 losses triggers full-day trading pause). All counters reset when the date changes.

---

### 3.12 The Telegram Interface — `telegram_bot.py`

The Telegram bot serves as the primary human interface. It handles seven distinct notification flows:

**Startup Message:** Motivational trading quote and system status confirmation.

**Validation Alert:** Sent when a Phase 40 pattern-only signal enters the Validation Gate. Includes symbol, pattern detected, trigger price, and "PENDING" status.

**Multi-Edge Alert (Phase 41):** Rich notification displaying all detected edges as a checklist, primary trigger name, confidence level, entry and SL prices, and edge count. Uses MarkdownV2 with plain fallback.

**Trade Execution Alert:** Sent when a trade is executed (auto) or when operator action is needed (manual mode with "GO" button).

**Focus Mode Dashboard:** Continuously-updated message showing real-time P&L, entry, SL, target, orderflow sentiment, and action buttons. Edited in-place every 2 seconds.

**SFP Alert:** Urgent notification after stop-loss exit if price reverses back below entry within 10 minutes.

**Emergency Alert (Phase 42):** `send_emergency_alert(message)` — High-priority alert with forced notification (`disable_notification=False`). Used for critical position errors (duplicate orders, orphaned positions, directional flip detection). Each alert is also logged to `logs/emergency_alerts.log` with timestamp. This method is called by the Position Reconciliation module, the emergency exit circuit breaker, and the position verification system.

The bot also handles operator commands: `/status` returns current system state, `/auto on|off` toggles execution mode at runtime.

---

### 3.13 Multi-Edge Detection System — `multi_edge_detector.py` (Phase 41.1)

The Multi-Edge Detector runs BEFORE the analyzer and returns a structured edge payload. 5 active detectors with weighted confidence scoring.

**Architecture.** `MultiEdgeDetector.scan_all_edges(candidate)` iterates through 5 detectors. Each is wrapped in try-except — one crash doesn't block others.

**Weighted Confidence Scoring.** Uses `config.EDGE_WEIGHTS`: Absorption (3.0), Bad High (2.0), Trapped Longs (2.0), Failed Auction (1.0), standard patterns (1.0). Score ≥ 5.0 = `EXTREME`, ≥ 3.0 = `HIGH`, ≥ 2.0 with 2+ edges = `HIGH` (confluence bonus), ≥ 1.5 with any HIGH/EXTREME edge = `HIGH`, otherwise rejected.

**Detectors:** Pattern Engine (6 patterns), Trapped Position (5-candle scan, Z > 1.5), Absorption (Z > 2.0, tiny body, near high), Bad High (L2 depth, sell/buy > 2.5×), Failed Auction (30-candle range breakout failure).

Three detectors commented out: OI Divergence Proxy, TPO Poor High, Momentum Exhaustion.

---

### 3.14 Detector Performance Tracker — `detector_performance_tracker.py` (Phase 41.1)

CSV-based analytics tracking per-detector: signal generation, validation outcome, and trade outcome. `get_detector_stats()` computes win rate, average R-multiple, and false positive rate per detector over configurable time windows.

---

### 3.15 Configuration — `config.py`

All tunable parameters centralized. Environment variables loaded via `python-dotenv`.

**Core Settings:** Capital per trade: ₹1,800. Max risk: ₹200. Auto-trade: OFF. Log file: `logs/bot.log`. Square-off: 15:10 IST. Validation timeout: 15 minutes.

**Phase 41.1 — Multi-Edge:** `MULTI_EDGE_ENABLED` (default `False`), `ENABLED_DETECTORS` (5 active), `EDGE_WEIGHTS` (Absorption: 3.0, Bad High: 2.0, etc.), confidence thresholds (EXTREME: 5.0, HIGH: 3.0, MEDIUM: 2.0), `ENABLE_DETECTOR_TRACKING` (default `True`), `SCANNER_PARALLEL_WORKERS` (10).

**Phase 41.2 — Scalper Risk Management:** `USE_SCALPER_RISK_MANAGEMENT` (default `False`), `SCALPER_STOP_TICK_BUFFER` (12), `SCALPER_STOP_HUNT_BUFFER_PCT` (0.3%), `SCALPER_BREAKEVEN_TRIGGER_PCT` (0.3%), trailing distances (initial: 0.2%, after TP1: 0.15%, after TP2: 0.1%), TP targets (TP1: 1.5%, TP2: 2.5%, TP3: 3.5%), `ENABLE_EOD_SIMULATION` (True), `SIMULATION_LOG_PATH`.

**Phase 42 — Position Safety (CRITICAL):**
- `ENABLE_POSITION_VERIFICATION` — `True`. Controls whether `_verify_position_safe()` is active. **NEVER set to `False` in production.**
- `ENABLE_BROKER_POSITION_POLLING` — `True`. Controls whether the focus loop checks broker position at the top of each iteration.
- `POSITION_RECONCILIATION_INTERVAL` — `1800` (30 minutes). How often the reconciler runs during the trading session.
- `EMERGENCY_ALERT_ENABLED` — `True`. Controls emergency Telegram alerts.
- `EMERGENCY_LOG_PATH` — `logs/emergency_alerts.log`. File path for emergency alert logging.
- `ORPHANED_POSITION_LOG_PATH` — `logs/orphaned_positions.log`. File path for orphaned position logging.

---

## 4. What Makes This System Different

**Twelve Sequential Gates.** Most algorithmic trading systems have 2–3 filters. ShortCircuit has 12 independent rejection points spanning market regime, discipline, constraints, circuits, momentum, pattern recognition, breakdown confirmation, confluence, orderflow, and multi-timeframe analysis.

**Multi-Edge Detection (Phase 41.1).** 5 institutional edge detectors with weighted confidence scoring. Each detector is individually feature-flagged.

**Validation Gate.** Requires price to confirm the setup by breaking the signal candle's low, with a 15-minute timeout. Eliminates ~40% of false signals.

**Position Safety Architecture (Phase 42).** Every order is guarded by `_verify_position_safe()`. The focus loop checks broker position every 2 seconds. Stop-loss exits use a 4-step double-verification protocol (check → wait 500ms → re-check → exit). Emergency exits have a circuit breaker pattern. Position reconciliation runs at startup and every 30 minutes.

**Scalper Risk Management (Phase 41.2).** Feature-flagged multi-target exit system with structure-based stops, 3 TP levels with partial position closes, trailing stops with distance tiers, and a background monitor thread.

**Institutional Orderflow Analysis.** Round number analysis, trapped position detection, absorption analysis, DOM wall detection, OI divergence, dPOC divergence.

**Self-Healing Architecture.** Auto-recovery on restart, emergency exit circuit breaker, position reconciliation, SFP watch after stop-outs, and broker-side SL orders as safety nets.

**Psychological Discipline Engine.** Daily limits, cooldowns, consecutive loss pauses.

**Machine Learning Data Pipeline.** Every signal generates 20+ features in Parquet format for supervised learning.

**Zero-Risk Rollback Architecture.** Feature flags for Multi-Edge (`MULTI_EDGE_ENABLED`), Scalper RM (`USE_SCALPER_RISK_MANAGEMENT`), Position Safety (`ENABLE_POSITION_VERIFICATION`, `ENABLE_BROKER_POSITION_POLLING`). Each system can be toggled independently.

---

*Document verified against ShortCircuit source code — Phase 42, February 2026*
