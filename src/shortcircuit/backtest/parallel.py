"""
Replay across processes. A day's replay is CPU-bound (the strategy and the
feature code run once a minute) and days are independent, so they spread across
cores. Each worker loads the strategies itself and is handed the parent's F&O
list, so every process applies the same square-off; nothing is fetched here.
"""
from __future__ import annotations

from typing import Optional, Sequence

from shortcircuit.backtest.replay import Costs, DayResult, replay_day
from shortcircuit.eod import squareoff

_strategies: list = []


def init_worker(names: Sequence[str], fno: Optional[frozenset]) -> None:
    from shortcircuit.backtest.__main__ import force_data_only, quiet_engine_logs
    from shortcircuit.strategy import registry

    force_data_only()
    quiet_engine_logs()
    squareoff.registry.adopt(fno)
    _strategies[:] = registry.load(list(names))


def replay(job: tuple) -> DayResult:
    symbol, bars, prev_close, costs, max_trades, upper_circuit, *rest = job
    return replay_day(symbol, bars, prev_close, _strategies, costs or Costs(), max_trades,
                      upper_circuit=upper_circuit, frame_bars=rest[0] if rest else None)
