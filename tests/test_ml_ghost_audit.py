import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from eod_analyzer import EODAnalyzer
from ml_logger import MLDataLogger
from tools import trainer


IST = ZoneInfo("Asia/Kolkata")


def _epoch_ist(hour: int, minute: int) -> int:
    return int(datetime(2026, 4, 29, hour, minute, tzinfo=IST).timestamp())


def test_ml_logger_records_ist_timestamp_and_label_source(tmp_path):
    logger = MLDataLogger(data_dir=str(tmp_path))

    obs_id = logger.log_observation("NSE:TEST-EQ", 100.0, {"direction": "SHORT"})
    assert logger.update_outcome(
        obs_id,
        "WIN",
        99.0,
        max_favorable=1.2,
        max_adverse=0.1,
        hold_time_mins=5,
        pnl_pct=1.0,
        label_source="LIVE",
        exit_reason="TP_HIT",
    )

    df = pd.read_parquet(logger.daily_file)
    row = df.iloc[0]
    assert row["timestamp_ist"].endswith("+05:30")
    assert row["label_source"] == "LIVE"
    assert row["exit_reason"] == "TP_HIT"


def test_ghost_audit_skips_signal_minute_and_marks_label_source(monkeypatch):
    updates = []

    class DummyMLLogger:
        today = "2026-04-29"

        def get_unlabeled_observations(self, session_date=None):
            assert session_date == "2026-04-29"
            return pd.DataFrame(
                [
                    {
                        "obs_id": "obs1",
                        "date": "2026-04-29",
                        "time": "09:30:00",
                        "timestamp_ist": "2026-04-29T09:30:00+05:30",
                        "symbol": "NSE:TEST-EQ",
                        "ltp": 100.0,
                        "sl_price": 101.0,
                        "tp_price": 99.0,
                        "direction": "SHORT",
                        "outcome": None,
                    }
                ]
            )

        def update_outcome(self, **kwargs):
            updates.append(kwargs)
            return True

    class DummyFyers:
        def history(self, data):
            return {
                "s": "ok",
                "candles": [
                    [_epoch_ist(9, 30), 100.0, 100.2, 99.0, 99.5, 1000],
                    [_epoch_ist(9, 31), 100.0, 101.2, 100.1, 101.0, 1000],
                ],
            }

    import ml_logger as ml_logger_module

    monkeypatch.setattr(ml_logger_module, "get_ml_logger", lambda: DummyMLLogger())

    analyzer = EODAnalyzer(fyers=DummyFyers(), db_manager=object())
    result = asyncio.run(analyzer.audit_missed_signals(date(2026, 4, 29)))

    assert result["processed"] == 1
    assert result["losses"] == 1
    assert result["tp_hits"] == 0
    assert updates[0]["label_source"] == "GHOST"
    assert updates[0]["exit_reason"] == "SL_HIT"
    assert updates[0]["pnl_pct"] == -1.0


def test_trainer_filters_ghost_and_legacy_by_default(tmp_path, monkeypatch):
    df = pd.DataFrame(
        [
            {"obs_id": "live", "outcome": "WIN", "label_source": "LIVE", "direction": "SHORT"},
            {"obs_id": "ghost", "outcome": "WIN", "label_source": "GHOST", "direction": "SHORT"},
            {"obs_id": "legacy", "outcome": "WIN", "direction": "SHORT"},
        ]
    )
    df.to_parquet(tmp_path / "observations_2099-01-01.parquet", index=False)
    monkeypatch.setattr(trainer, "TRAINING_DATA_PATH", tmp_path)

    trusted = trainer.load_historical_data()
    assert trusted["obs_id"].tolist() == ["live"]

    with_ghost = trainer.load_historical_data(include_ghost=True)
    assert set(with_ghost["obs_id"]) == {"live", "ghost"}

    with_all = trainer.load_historical_data(include_ghost=True, include_legacy=True)
    assert set(with_all["obs_id"]) == {"live", "ghost", "legacy"}
