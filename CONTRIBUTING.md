# Contributing to ShortCircuit

Thank you for your interest in contributing to ShortCircuit! We welcome contributions from the community to help make this the best algorithmic trading engine for the NSE.

## 🤝 Logic & Strategy Contributions
Please note that the core **strategy logic** ("The Sniper" in `analyzer.py`) is considered the "Secret Sauce" of this engine. 
- **Bug Fixes:** Highly appreciated.
- **Infrastructure:** Improvements to `trade_manager.py`, `focus_engine.py`, or `scanner.py` (performance/stability) are welcome.
- **New Strategies:** If you want to add a *new* strategy, please implement it as a separate module (e.g., `strategies/mean_reversion.py`) and do not overwrite `analyzer.py` unless fixing a bug.

## 🛠 Development Workflow

1.  **Fork the Repository:** Click the "Fork" button on the top right of the repo page.
2.  **Clone your Fork:**
    ```bash
    git clone https://github.com/YOUR_USERNAME/ShortCircuit.git
    cd ShortCircuit
    ```
3.  **Create a Branch:**
    ```bash
    git checkout -b feature/my-new-feature
    ```
4.  **Make Changes:** Write your code.
5.  **Test:** Run the bot in **Manual Mode** (`/auto off`) to verify nothing breaks.
6.  **Commit:**
    ```bash
    git commit -m "feat: Added liquidity filter"
    ```
7.  **Push:**
    ```bash
    git push origin feature/my-new-feature
    ```
8.  **Pull Request:** Open a PR on the main repository.

## 📜 Coding Standards
*   **Python:** 3.9+
*   **Style:** PEP 8.
*   **Logging:** Use the existing `logger` object. Do not use `print()`.
*   **Async:** Only use async if absolutely necessary. The current architecture is Thread-based.

## 🐛 Reporting Bugs
Please open an Issue on GitHub with:
1.  Logs (from `logs/bot.log`)
2.  Steps to Reproduce
3.  Expected vs Actual Behavior

Happy Coding! 🚀
