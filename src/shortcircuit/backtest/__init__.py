"""
Backtest the active strategies on historical 1-minute candles.

    SHORTCIRCUIT_MODE=data_only is forced: a backtest can never place an order.

    python -m shortcircuit.backtest --from 2026-10-01 --to 2026-11-20
    python -m shortcircuit.backtest --from 2026-11-03 --to 2026-11-07 --symbols NSE:ABC-EQ

See replay.py for exactly what is the engine's own code and what is modelled.
"""
