# ShortCircuit (SC-Quant) ⚡
**Institutional-Grade Algorithmic Trading Ecosystem**

> *"The goal of a successful trader is to make the best trades. Money is secondary."* — Alexander Elder

## 📚 About The Project
**ShortCircuit** is a sophisticated, Python-based algorithmic trading engine engineered for the Indian Equity Markets (NSE). Unlike basic indicator-based bots, ShortCircuit synthesizes **Market Microstructure**, **Auction Market Theory**, and **Statistical Mean Reversion** to identify high-probability intraday setups.

Built on the **Fyers APIv3**, the system is designed for **low-latency execution** and **robust risk management**. It operates as a "Glass Box" system—providing full transparency into its decision-making process via a real-time Telegram dashboard.

### Key Capabilities
*   **Hybrid Execution:** Switch seamlessly between **Fully Autonomous Mode** (Type `/auto on`) and **Manual Alert Mode** (Human Verification).
*   **Focus Engine:** A dedicated thread that manages active trades with **Dynamic Trailing Stops** and **Real-Time P&L Tracking**.
*   **Microstructure Filter:** Rejects stocks with "gappy" or illiquid charts (Zombie Candles) to ensure clean execution.
*   **Safety Net:** Automated **Hard Stop Loss** placement, **Time-Based Exits** (3:10 PM), and **Capital Protection** logic.

---

## ✨ Levels of Automation

ShortCircuit is designed as a "Pilot-Assist" system. It handles the high-speed complexity while you stay in command.

### ✅ Fully Automated (The "Bot")
1.  **Scanning:** Scans 2000+ NSE stocks every minute for Volume & Momentum anomalies.
2.  **Filtering:** Automatically rejects 99% of stocks based on Trend, Time, and Chart Quality.
3.  **Execution:**
    *   Calculates Position Size based on Risk (e.g., ₹2000 per trade).
    *   Places **Limit Entry** orders.
    *   Places **Hard Stop Loss** (Rounded to 0.05 tick).
    *   Aborts if Entry fails (Safety First).
4.  **Trade Management:**
    *   Trails Stop Loss to Breakeven at 1:1 Profit.
    *   Trails aggressively at 1:2 Profit.
    *   Exits position on Reversal.

### ✋ Manual Control (The "Human")
1.  **Strategy Selection:** The bot runs the specific "Sniper" logic (Breakdown/Rejection). You cannot change strategy dynamics on the fly without code edits.
2.  **Authentication:** Daily Fyers Login (OTP/Auth Code) is manual for security.
3.  **Emergency Kill:** You can stop any trade via Telegram (`/stop` or clicking "Close Trade").
4.  **Funds:** Adding funds to the broker is manual.

---

## 🦅 Strategy: "The Sniper" (Phase 24)
The current active strategy is a Trend-Following system designed to capture large intraday moves (1:3+ Risk/Reward) while filtering out noise.

### The Filter Funnel (6 Gates)
Every signal must pass a rigorous set of checks before execution:
1.  **Regime Filter:** Blocks Short trades if Nifty/BankNifty is trending up.
2.  **Time Protection:** Blocks trading during high-noise (09:15-10:00) and low-volume (12:00-13:00) periods.
3.  **HTF Confluence:** Verifies structure on the **15-Minute** timeframe (Lower Highs required).
4.  **VWAP Extension:** Only initiates trades when price is statistically overextended (>2 SD from VWAP).
5.  **Signal Cap:** Limits exposure to **5 High-Quality Trades** per day to prevent overtrading.
6.  **Key Levels:** Prioritizes setups at Day High (PDH) or Week High (PWH).

### Setup Logic
*   **Pattern:** Rejection Candles (Shooting Star, Engulfing) at liquidity zones.
*   **Entry:** Momentum breakdown of the setup candle.
*   **Stop Loss:** Technical Stop placed above the Swing High (+ ATR Buffer).
*   **Target:** Open (Trend Follows until EOD or Trailing Stop Hit).

## 🔬 Microstructure & Auction Theory (The "Why")
ShortCircuit doesn't just look at candles; it looks at the **Auction Logic**.

### 1. Auction Market Theory (AMT)
The bot views the market as a mechanism to facilitate trade.
*   **Look Above and Fail:** If price breaks a key level (Day High) but fails to hold, it signals rejection. The bot shorts this failure targeting a return to value.
*   **Mean Reversion:** Prices extended >2 SD from VWAP are considered "expensive". The bot shorts these extensions to capture the reversion to the mean.

### 2. Order Flow Dynamics (Tape Reading)
The `FocusEngine` simulates institutional tape reading:
*   **Absorption:** Detects when Aggressive Buyers are hitting the Offer but Price isn't moving (Iceberg Orders).
*   **Exhaustion:** Detects when Volume dries up at new highs (Lack of participants).
*   **Delta Divergence:** Visualizes when Price makes a New High but Net Buying (Delta) is dropping.

### 3. Smart Trailing (The "Focus Engine")
Unlike static bots, SC-Quant manages potential winners:
*   **Latching:** Once a trade is entered, the Focus Engine "Latches" onto it.
*   **Risk-Free:** Automatically moves SL to Breakeven when Profit = Risk.
*   **Dynamic Trail:** If Profit > 2R, it aggressively trails the stop to lock in "outlier" moves (3-4% runs).

---

## 🛠 Features & Modules

| Module | Description |
| :--- | :--- |
| `main.py` | The central nervous system. Manages threads, scanning loops, and EOD shutdown. |
| `analyzer.py` | The "Brain". Implements the 6-Gate Filter Funnel and Pattern Recognition. |
| `focus_engine.py` | The "Manager". Tracks active trades, updates Telegram dashboard, and trails stop losses. |
| `trade_manager.py` | The "Executor". Handles order placement, sizing (Capital Split), and square-offs. |
| `telegram_bot.py` | The "Interface". Provides a rich UI for monitoring and commands. |

### 🛡️ Risk Management (Built-in)
*   **Hard Stop Loss:** A Limit Order (SL-L) is placed **immediately** upon entry. No position is ever left naked.
*   **Auto-Trailing:** Once profit > 2x Risk, the Focus Engine automatically tightens the Stop Loss.
*   **Daily Cap:** Maximum 5 trades/day.
*   **Auto-Square Off:** At **15:10 IST**, the system force-closes all open positions to avoid broker penalties.
*   **Margin Safety:** Automatically adjusts position sizing (`CAPITAL = 1800`) to prevent "Insufficient Funds" errors on small accounts.

---

## 🚀 Getting Started

### Prerequisites
*   Python 3.9+
*   Fyers API Account

### Installation
1.  Clone the repository:
    ```bash
    git clone https://github.com/nabrahma/ShortCircuit.git
    cd ShortCircuit
    ```
2.  Install dependencies:
    ```bash
    pip install fyers-apiv3 telebot pandas numpy colorama python-dotenv
    ```
3.  Configure Environment:
    Create a `.env` file in the root:
    ```env
    FYERS_CLIENT_ID=your_client_id
    FYERS_SECRET_ID=your_secret_id
    FYERS_REDIRECT_URI=https://trade.fyers.in/api-login/redirect-uri/index.html
    TELEGRAM_BOT_TOKEN=your_bot_token
    TELEGRAM_CHAT_ID=your_chat_id
    ```

### Usage
1.  **Start the Server:**
    ```bash
    python main.py
    ```
2.  **Authenticate:**
    Follow the prompt to login to Fyers and paste the `auth_code`.
3.  **Control via Telegram:**
    *   `/status` - Check system health.
    *   `/auto on` - Enable Fully Autonomous Trading.
    *   `/auto off` - Switch to Manual Alert Mode.

---

## 📊 Performance
The system logs every signal to `logs/signals.csv`.
Run the analysis scripts to verify performance:
```bash
python scripts/eod_simulation_jan30.py
```
*(Recent Result: +60% Win Rate, +38% ROI on Jan 30, 2026)*

---

## 📜 License
Private Proprietary Software. All Rights Reserved.
