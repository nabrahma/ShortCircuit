
# ShortCircuit HFT System Architecture (v42.2)

> **Role**: Chief Technology Officer (CTO) / System Architect
> **System Status**: Hybrid HFT (Synchronous Strategy / Asynchronous Execution)
> **Latest Update**: Phase 42.1 (PostgreSQL + Asyncio Migration)

## 1. Executive Summary

ShortCircuit is a **hybrid high-frequency trading (HFT) system** designed for the NSE (National Stock Exchange). It operationalizes a complex proprietary strategy through a dual-loop architecture:

1.  **Synchronous Strategy Loop (The "Brain")**: Handles market scanning, technical analysis, and signal generation. It leverages legacy robust logic for pattern recognition.
2.  **Asynchronous Execution Loop (The "Muscle")**: Handles order placement, database writes, and state reconciliation. This ensures **ms-level** latency for critical actions (Entry, Exit, Cancellation) and prevents blocking operations (like DB writes) from stalling market monitoring.
3.  **Zero-Trust Reliability**: The system assumes the broker state and local state will drift. It uses a `ReconciliationEngine` to poll and correct these drifts every 500ms.

---

## 2. High-Level Architecture Diagram

The system uses an **`AsyncExecutor` Bridge** to allow the legacy synchronous main thread to offload critical tasks to a dedicated high-performance background thread.

```mermaid
graph TD
    subgraph "Main Thread (Synchronous Strategy Loop)"
        A[Startup] --> B[MarketSession Check]
        B --> C{Active?}
        C -- Yes --> D[Scanner]
        D --> E[Analyzer]
        E --> F[Validation Gate (FocusEngine)]
        F --> G[TradeManager (Legacy Controller)]
    end

    subgraph "Async Loop (Background Thread)"
        H[AsyncExecutor Bridge]
        I[OrderManager (Execution)]
        J[DatabaseManager (AsyncPG Pool)]
        K[ReconciliationEngine (Truth Source)]
        L[EmergencyLogger (Non-Blocking)]
    end

    G --1. SyncWrapper Call--> H
    H --2. Dispatch Task--> I
    I --3. Acquire Symbol Lock--> I
    I --4. Fyers API (Order)--> Brokers[(Fyers API)]
    I --5. Log Trade--> J
    J --6. Persist--> DB[(PostgreSQL)]
    
    K --Poll (500ms)--> Brokers
    K --Diff--> J
    
    L --Queue--> Disk[(Logs: CRITICAL_FAILURE.log)]
    
    subgraph "Logic Libraries"
        M[GodModeAnalyst]
        N[TapeReader]
        O[MarketProfile]
    end
    
    E -.-> M
    E -.-> N
    E -.-> O
```

---

## 3. Directory Structure & File Inventory (Strict)

Every entry in the codebase is categorized by its **Functional Role** and **Criticality**.

### **A. Core Infrastructure (The Backbone)**

| File | Criticality | Responsibility |
| :--- | :--- | :--- |
| **`main.py`** | 🔴 **CRITICAL** | **Orchestrator**. Initializes all modules, manages the main `while True` loop, handles graceful shutdown. The entry point. |
| **`async_utils.py`** | 🔴 **CRITICAL** | **The Bridge**. Contains `AsyncExecutor` (runs the background asyncio loop in a thread) and `SyncWrapper` (routes Sync calls to Async). |
| **`config.py`** | 🔴 **CRITICAL** | **Configuration**. Global settings (Credentials, Risk Parms, Time, Logging Paths). Loaded by ALL modules. |
| **`startup_recovery.py`** | 🟠 HIGH | **Boot Safety**. Runs pre-flight checks: DB connection verify, Order Reconciliation. Ensures clean start state. |
| **`fyers_connect.py`** | 🔴 **CRITICAL** | **Connectivity**. Wrapper for Fyers API. Handles Auth Token generation and validation. |
| **`telegram_bot.py`** | 🟡 MEDIUM | **Control Plane**. Remote control for the user. Sends alerts, accepts commands (`/status`, `/stop`). |
| **`requirements.txt`** | ⚪ LOW | **Dependencies**. List of Python packages required. |
| **`Dockerfile`** | ⚪ LOW | **Deployment**. Container configuration for production deployment. |

### **B. Data Layer (Persistence & Truth)**

| File | Criticality | Responsibility |
| :--- | :--- | :--- |
| **`database.py`** | 🔴 **CRITICAL** | **PostgreSQL Access**. Manages `asyncpg` connection pool (10-50 conns). Handles purely atomic async writes to DB. |
| **`reconciliation.py`** | 🔴 **CRITICAL** | **Truth Engine**. Background loop (500ms). Diffs Broker Pos vs DB Pos. Detects Orphans/Phantoms. |
| **`position_reconciliation.py`** | 🟡 LEGACY | **Backup**. Old sync reconciliation logic. Kept for manual tools/backward compat. |
| **`journal_manager.py`** | 🟡 MEDIUM | **Trade Logger**. Adapts trade data for CSV logging (Legacy/Backup). |
| **`migrations/`** | 🔴 **CRITICAL** | **Schema**. Contains SQL files (`v42_1_0_postgresql.sql`) defining the DB structure. |
| **`data/`** | ⚪ DATA | **Storage**. Local folder for CSV logs, ML parquet files, and access tokens. |
| **`logs/`** | ⚪ DATA | **Logs**. Folder for application logs. |

### **C. Logic Engines (The Strategy)**

| File | Criticality | Responsibility |
| :--- | :--- | :--- |
| **`scanner.py`** | 🟠 HIGH | **Discovery**. Scans NSE symbols for volatility/volume spikes. Returns `Candidates`. |
| **`analyzer.py`** | 🟠 HIGH | **Signal Gen**. The "Brain". Orchestrates technical checks (GodMode, Tape, etc.) to validate a Setup. |
| **`focus_engine.py`** | 🟠 HIGH | **Validation Gate**. "Watches" a candidate tick-by-tick. If price crosses Trigger, calls Execution. Reduces noise. |
| **`trade_manager.py`** | 🔴 **CRITICAL** | **Legacy Controller**. Manages Capital, Position Limits, and calls `OrderManager`. The "Manager" of the bot. |
| **`god_mode_logic.py`** | 🟠 HIGH | **TA Library**. Core math: VWAP Slope, Candle Structure, RSI Div, Fibonacci, Z-Score. |
| **`tape_reader.py`** | 🟠 HIGH | **Orderflow Lib**. Detects Absorption, Bad Highs/Lows, Trapped Traders using Volume/Price analysis. |
| **`market_profile.py`** | 🟡 MEDIUM | **AMT Lib**. Calculates TPO Profile, POC, Value Area, and 'Look Above & Fail' patterns. |
| **`htf_confluence.py`** | 🟡 MEDIUM | **Trend Filter**. Checks Higher Timeframe (15m/Daily) alignment preventing trades against major trend. |
| **`market_context.py`** | 🟡 MEDIUM | **Regime Detect**. Analyzes NIFTY50 to determine "Wide Trend", "Chop", or "Trend Day". |
| **`market_session.py`** | 🟡 MEDIUM | **Time Keeper**. Detects Market Phase (Open/Mid/Close). Adjusts aggression based on time. |
| **`multi_edge_detector.py`** | 🟡 MEDIUM | **Confluence**. Weighted scoring system combining multiple signals into a confidence score. |
| **`signal_manager.py`** | 🟡 MEDIUM | **Rate Limiter**. Prevents over-trading. Enforces daily limits and per-symbol cooldowns. |
| **`discretionary_engine.py`** | 🟡 MEDIUM | **Exit Logic**. "Soft Stop" logic. Decides when to close a trade early based on momentum loss. |
| **`discretionary_signals.py`** | ⚪ LEGACY | Old signal definitions. |
| **`symbols.py`** | 🟡 MEDIUM | **Universe**. Defines the list of active stocks to trade. |

### **D. Execution & Risk (The Guardrails)**

| File | Criticality | Responsibility |
| :--- | :--- | :--- |
| **`order_manager.py`** | 🔴 **CRITICAL** | **Execution Engine**. Async. Handles simple Entry/Exit, but adds HFT features: **Per-Symbol Locks**, **Safe Exit**. |
| **`capital_manager.py`** | 🟠 HIGH | **Money Mgmt**. Tracks available purchasing power, active allocations. Prevents blowing account. |
| **`scalper_position_manager.py`** | 🟡 MEDIUM | **Active Mgmt**. Manages fast scalp trades (Trailing SL, Quick TP). |
| **`scalper_risk_calculator.py`** | 🟡 MEDIUM | **Math**. Pure logic for calculating Stops (Structure based) and Targets (Risk:Reward). |
| **`emergency_logger.py`** | 🟠 HIGH | **Panic Log**. Async Non-blocking logger for critical failures (API down, DB disconnect). |

### **E. Analytics & Diagnostics (The Post-Game)**

| File | Criticality | Responsibility |
| :--- | :--- | :--- |
| **`ml_logger.py`** | 🟡 MEDIUM | **Data Collection**. Logs features (X) and outcomes (y) to Parquet for future ML training. |
| **`detector_performance_tracker.py`**| ⚪ LOW | **Stats**. Tracks win-rate of specific patterns (e.g., "Shooting Star vs Bearish Engulfing"). |
| **`diagnostic_analyzer.py`** | ⚪ TOOL | **"Why?" Tool**. Logic for `eod_why.py`. Runs all gates in diagnostic mode. |
| **`eod_why.py`** | ⚪ CLI | **CLI Tool**. User runs this to ask "Why didn't you take the Reliance trade?". |
| **`eod_analyzer.py`** | ⚪ REPORT | **Report Gen**. Generates daily performance report. |
| **`eod_analysis.py`** | ⚪ REPORT | **Report Logic**. Logic for analyzing daily dump. |
| **`run_eod_manual.py`** | ⚪ SCRIPT | **Manual Run**. Helper to run EOD analysis manually. |
| **`update_terminal_log.py`** | ⚪ SCRIPT | **Log Process**. Helper to clean logs for AI analysis. |
| **`trade_simulator.py`** | ⚪ TEST | **Backtest**. Simulates "What if" scenarios for risk models. |

### **F. Tools (`tools/`)**

| File | Criticality | Responsibility |
| :--- | :--- | :--- |
| **`get_auth_url.py`** | 🟡 SETUP | Helper to generate Fyers Auth URL. |
| **`set_token.py`** | 🟡 SETUP | Helper to manually save token if auto-login fails. |

---

## 4. Component Communication & Data Flow

### **A. The "Life of a Signal" (Crucial Flow)**

1.  **Generation (Sync Loop)**:
    *   `main.py` loop calls `scanner.py`.
    *   `scanner` finds `NSE:SBIN-EQ` (High Volatility).
    *   `analyzer.py` validates technicals using `god_mode_logic` + `tape_reader`.
    *   `market_context` validates NIFTY trend alignment.
2.  **Validation (Sync Monitoring)**:
    *   `analyzer` sends candidate to `focus_engine.py`.
    *   `focus_engine` starts a monitoring thread.
    *   **TRIGGER**: Price crosses `Signal Low` (Breakdown).
3.  **The Handover (Sync -> Async)**:
    *   `focus_engine` calls `trade_manager.execute_logic()`.
    *   `trade_manager` checks Capital (`capital_manager`) & Daily Limits (`signal_manager`).
    *   `trade_manager` calls `order_manager.enter_position()`.
    *   **BRIDGE**: `order_manager` is actually a **SyncWrapper**. It dispatches the call to `AsyncExecutor`'s queue.
4.  **Execution (Async Loop)**:
    *   The Background Thread picks up `enter_position`.
    *   **LOCK**: Acquires `asyncio.Lock("NSE:SBIN-EQ")`.
    *   **BROKER**: Calls `fyers.place_order(MARKET)`.
    *   **BROKER**: Calls `fyers.place_order(SL-M)` (Immediate Protection).
    *   **DB**: Calls `database.log_trade_entry()`.
    *   **UNLOCK**: Releases Lock.
    *   Returns Result back to Main Thread.

### **B. The Safety Net (Reconciliation)**

*   **When**: Every 500ms - 2000ms.
*   **Who**: `ReconciliationEngine` (Background Thread).
*   **What**:
    1.  Fetches `fyers.positions()`.
    2.  Fetches `SELECT * FROM positions WHERE state='OPEN'` from DB.
    3.  **Diffs** the two sets.
    4.  **Action**: If "Phantom" (We have open pos, Broker has none) -> Log Critical Error.

---

## 5. Critical HFT Design Principles

### **1. Non-Blocking Database I/O**
*   **Problem**: SQLite blocks the thread on write. In high vol, we might write 50 times/sec (Trail updates).
*   **Solution**: **PostgreSQL + Asyncpg**.
    *   Write operations are `await`ed on the BG thread.
    *   Connection Pool (`min=10`, `max=50`) ensures we never wait for a socket.
    *   Main thread continues scanning market while DB writes happen.

### **2. Race Condition Prevention (The "Double Fill" Fix)**
*   **Problem**: In high volatility, an "Exit" signal and a "Stop Loss" hit can happen simultaneously.
*   **Solution**: **Per-Symbol Async Locks**.
    *   `order_manager.safe_exit()` acquires `lock(Symbol)`.
    *   It **CANCELS** the SL order first.
    *   It **WAITS** for cancellation confirmation.
    *   Only **THEN** it places the Exit Market order.
    *   This prevents "Short -> Exit -> SL Hit -> Short Again" spiral.

### **3. The Bridge Pattern (Evolutionary Architecture)**
*   **Problem**: We cannot rewrite 20,000 lines of Sync code to Async overnight.
*   **Solution**: **Hybrid Architecture**.
    *   Logic stays Sync (Easy to reason about, sequential).
    *   IO/Execution becomes Async (Performant, concurrent).
    *   `AsyncExecutor` bridges them seamlessly.

---

## 6. Deployment & Environment

*   **Database**: PostgreSQL 15+ (Localhost:5432).
*   **Schema**: Managed via `migrations/` SQL files. Do not modify DB manually.
*   **Credentials**:
    *   `DB_USER`, `DB_PASSWORD`, `DB_HOST` (Env Vars).
    *   `FYERS_APP_ID`, `FYERS_ACCESS_TOKEN` (Auth).
*   **Logging**:
    *   `logs/bot.log` (Rotational, General).
    *   `logs/emergency/` (Critical Failures).
    *   `data/ml/` (Parquet ML Data).
