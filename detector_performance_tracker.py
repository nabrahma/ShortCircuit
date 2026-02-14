"""
Detector Performance Tracker — Phase 41.1
Tracks per-detector metrics: signals generated, validation rate,
win rate, and average R-multiple.

Logs to CSV at config.DETECTOR_LOG_PATH (default: logs/detector_performance.csv).
"""
import pandas as pd
import logging
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

COLUMNS = [
    'date', 'detector', 'signal_id', 'symbol',
    'validated', 'outcome', 'r_multiple', 'hold_time_min'
]


class DetectorPerformanceTracker:
    """
    Tracks performance metrics for each edge detector.
    Logs: signals generated, validation rate, win rate, avg R-multiple.
    """

    def __init__(self, log_path: str = 'logs/detector_performance.csv'):
        self.log_path = Path(log_path)
        self.pending_signals: dict = {}  # {signal_id: {detectors, symbol, timestamp}}

        # Create log file with headers if doesn't exist
        if not self.log_path.exists():
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(columns=COLUMNS).to_csv(self.log_path, index=False)
            logger.info(f"[TRACKER] Created performance log: {self.log_path}")

    # ------------------------------------------------------------------
    # LOGGING METHODS
    # ------------------------------------------------------------------
    def log_signal_generated(self, signal_id: str, detectors: list, symbol: str):
        """Called when signal enters validation queue."""
        self.pending_signals[signal_id] = {
            'detectors': detectors,
            'symbol': symbol,
            'timestamp': datetime.now()
        }

        # Log generation event for each detector
        for detector in detectors:
            self._append_log({
                'date': datetime.now().strftime('%Y-%m-%d'),
                'detector': detector,
                'signal_id': signal_id,
                'symbol': symbol,
                'validated': False,
                'outcome': 'PENDING',
                'r_multiple': None,
                'hold_time_min': None
            })
        logger.info(f"[TRACKER] Logged signal {signal_id}: {detectors}")

    def log_validation_outcome(self, signal_id: str, validated: bool):
        """Called when signal validates or times out."""
        if signal_id not in self.pending_signals:
            return

        signal_data = self.pending_signals[signal_id]
        outcome = 'VALIDATED' if validated else 'TIMEOUT'

        for detector in signal_data['detectors']:
            self._update_log(signal_id, detector, {
                'validated': validated,
                'outcome': outcome
            })
        logger.info(f"[TRACKER] {signal_id} → {outcome}")

    def log_trade_outcome(self, signal_id: str, outcome: str,
                          r_multiple: float, hold_time_min: int):
        """Called when trade closes (WIN/LOSS/STOPPED)."""
        if signal_id not in self.pending_signals:
            return

        signal_data = self.pending_signals[signal_id]

        for detector in signal_data['detectors']:
            self._update_log(signal_id, detector, {
                'outcome': outcome,
                'r_multiple': r_multiple,
                'hold_time_min': hold_time_min
            })

        # Clean up
        del self.pending_signals[signal_id]
        logger.info(f"[TRACKER] {signal_id} closed: {outcome} ({r_multiple:.2f}R)")

    # ------------------------------------------------------------------
    # ANALYTICS
    # ------------------------------------------------------------------
    def get_detector_stats(self, detector_name: str, days: int = 30) -> dict:
        """Calculate performance metrics for a detector."""
        try:
            df = pd.read_csv(self.log_path)
        except Exception:
            return self._empty_stats(detector_name)

        df['date'] = pd.to_datetime(df['date'])

        # Filter last N days
        cutoff = datetime.now() - timedelta(days=days)
        df = df[(df['detector'] == detector_name) & (df['date'] >= cutoff)]

        total_signals = len(df)
        if total_signals == 0:
            return self._empty_stats(detector_name)

        validated = int(df['validated'].sum())
        validation_rate = (validated / total_signals * 100)

        # Win rate (only for completed trades)
        completed = df[df['outcome'].isin(['WIN', 'LOSS'])]
        wins = len(completed[completed['outcome'] == 'WIN'])
        win_rate = (wins / len(completed) * 100) if len(completed) > 0 else 0

        avg_r = float(completed['r_multiple'].mean()) if len(completed) > 0 else 0.0

        return {
            'detector': detector_name,
            'signals_generated': total_signals,
            'validation_rate': round(validation_rate, 1),
            'trades_completed': len(completed),
            'win_rate': round(win_rate, 1),
            'avg_r_multiple': round(avg_r, 2),
            'false_positive_rate': round(100 - validation_rate, 1),
        }

    def get_all_stats(self, days: int = 30) -> list:
        """Get stats for all detectors."""
        detectors = [
            'PATTERN_BEARISH_ENGULFING', 'PATTERN_EVENING_STAR',
            'PATTERN_SHOOTING_STAR', 'PATTERN_ABSORPTION_DOJI',
            'PATTERN_MOMENTUM_BREAKDOWN', 'PATTERN_VOLUME_TRAP',
            'TRAPPED_LONGS', 'ABSORPTION', 'BAD_HIGH', 'FAILED_AUCTION'
        ]
        return [self.get_detector_stats(d, days) for d in detectors]

    # ------------------------------------------------------------------
    # INTERNAL
    # ------------------------------------------------------------------
    @staticmethod
    def _empty_stats(detector_name: str) -> dict:
        return {
            'detector': detector_name,
            'signals_generated': 0,
            'validation_rate': 0.0,
            'trades_completed': 0,
            'win_rate': 0.0,
            'avg_r_multiple': 0.0,
            'false_positive_rate': 0.0,
        }

    def _append_log(self, row: dict):
        """Append row to CSV."""
        try:
            pd.DataFrame([row]).to_csv(self.log_path, mode='a', header=False, index=False)
        except Exception as e:
            logger.error(f"[TRACKER] Append failed: {e}")

    def _update_log(self, signal_id: str, detector: str, updates: dict):
        """Update existing log entry."""
        try:
            df = pd.read_csv(self.log_path)
            mask = (df['signal_id'] == signal_id) & (df['detector'] == detector)
            for key, value in updates.items():
                df.loc[mask, key] = value
            df.to_csv(self.log_path, index=False)
        except Exception as e:
            logger.error(f"[TRACKER] Update failed: {e}")
