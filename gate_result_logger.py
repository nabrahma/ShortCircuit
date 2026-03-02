"""
gate_result_logger.py — PRD-008: Signal Rejection Audit Trail

Records every gate evaluation that occurs in check_setup() (G1-G8) and
focus_engine (G9-G12). Generates structured [REJECTED]/[SIGNAL]/[SUPPRESSED]
log lines and a daily EOD rejection summary.

Architecture:
  - GateResult: dataclass for a single candidate evaluation
  - GateResultLogger: singleton per session; accumulates records in memory
  - Bulk flushes to PostgreSQL at EOD (not per-evaluation)
  - EOD summary written to logs/rejections_YYYYMMDD.log
"""

from __future__ import annotations

import asyncio
import asyncpg
import datetime
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ================================================================
# Dataclass
# ================================================================

@dataclass
class GateResult:
    """Holds the full gate evaluation record for one candidate in one scan."""

    # Identity
    symbol:       str
    scan_id:      int
    evaluated_at: datetime.datetime = field(default_factory=datetime.datetime.now)

    # Market context (populated by analyzer)
    nifty_regime: Optional[str]   = None
    nifty_level:  Optional[float] = None

    # Gate verdicts: True=PASS, False=FAIL, None=not-evaluated
    # Analyzer gates (G1-G8)
    g1_pass: Optional[bool]  = None;  g1_value: Optional[float] = None   # gain range
    g2_pass: Optional[bool]  = None;  g2_value: Optional[float] = None   # RVOL validity
    g3_pass: Optional[bool]  = None;  g3_value: Optional[float] = None   # circuit guard
    g4_pass: Optional[bool]  = None;  g4_value: Optional[float] = None   # momentum
    g5_pass: Optional[bool]  = None;  g5_value: Optional[float] = None   # exhaustion at stretch
    g6_pass: Optional[bool]  = None;  g6_value: Optional[str]   = None   # pro confluence / POC
    g7_pass: Optional[bool]  = None;  g7_value: Optional[str]   = None   # market regime
    g8_pass: Optional[bool]  = None;  g8_value: Optional[float] = None   # signal manager

    # G9: HTF Confluence (still in analyzer — runs after G8, before _finalize_signal)
    g9_pass:  Optional[bool]  = None; g9_value:  Optional[str]  = None   # HTF confluence reason

    # Focus engine gates (G10-G12) — run after analyzer hands off
    g10_pass: Optional[bool]  = None; g10_value: Optional[float] = None  # cooldown spacing
    g11_pass: Optional[bool]  = None; g11_value: Optional[float] = None  # capital availability
    g12_pass: Optional[bool]  = None; g12_value: Optional[float] = None  # pre-entry conviction (LTP broke trigger)

    # Outcome
    verdict:          str            = "PENDING"   # SIGNAL_FIRED | REJECTED | DATA_ERROR | SUPPRESSED
    first_fail_gate:  Optional[str]  = None        # e.g. "G5_EXHAUSTION"
    rejection_reason: str            = ""
    data_tier:        Optional[str]  = None        # WS_CACHE | HYBRID | REST_EMERGENCY

    # If signal fired
    entry_price: Optional[float] = None
    qty:         Optional[int]   = None


# ================================================================
# Logger Singleton
# ================================================================

class GateResultLogger:
    """
    Session-scoped singleton.
    - Accumulates GateResult records in memory.
    - Provides smart suppression (same gate + reason + symbol within 60s, but force-log every 300s).
    - Bulk-flushes to PostgreSQL at EOD.
    - Writes human-readable rejection summary to logs/.
    """

    def __init__(self):
        self._records: List[GateResult] = []

        # Periodic flush state
        self._flushed_count: int = 0        # Records already inserted into DB
        self._unsaved_count: int = 0        # Records since last flush trigger
        self._flush_lock = threading.Lock() # Prevents concurrent flushes
        self._db_dsn: Optional[str] = None  # Set once by main.py after DB init

        # Suppression state: key -> (last_logged_ts, count_since_last_log)
        # key = (symbol, first_fail_gate, reason_category)
        self._suppression: Dict[Tuple, Tuple[float, int]] = {}
        self._force_log_interval = 300.0   # Force log every 5 min regardless
        self._suppress_window    =  60.0   # Suppress within this window if key unchanged

    def set_dsn(self, dsn: str) -> None:
        """Called once from main.py after DB pool init. Enables periodic flush."""
        self._db_dsn = dsn
        logger.info("[GateResultLogger] DSN configured — periodic flush enabled (every 100 records).")

    def _make_suppression_key(self, gr: GateResult) -> Tuple:
        reason_cat = (gr.rejection_reason or "")[:40]   # first 40 chars as category
        return (gr.symbol, gr.first_fail_gate or "", reason_cat)

    def _should_suppress(self, gr: GateResult) -> Tuple[bool, int]:
        """
        Returns (should_suppress, duplicates_since_last_log).
        Suppresses only if: same symbol + gate + reason_category, within 60s,
        AND last log was less than 300s ago (force-log every 5 min).
        """
        if gr.verdict in ("SIGNAL_FIRED", "DATA_ERROR"):
            return False, 0  # Never suppress signals or errors

        key = self._make_suppression_key(gr)
        now = time.time()
        entry = self._suppression.get(key)

        if entry is None:
            return False, 0

        last_ts, dup_count = entry
        age = now - last_ts

        if age > self._force_log_interval:
            # Force log after 300s
            return False, dup_count

        if age <= self._suppress_window:
            # Within suppression window — suppress
            return True, dup_count

        return False, dup_count

    def _update_suppression(self, gr: GateResult, suppressed: bool):
        key = self._make_suppression_key(gr)
        now = time.time()
        if suppressed:
            entry = self._suppression.get(key, (now, 0))
            self._suppression[key] = (entry[0], entry[1] + 1)
        else:
            self._suppression[key] = (now, 0)

    # ----------------------------------------------------------------
    # Public API
    # ----------------------------------------------------------------

    def record(self, gr: GateResult, *, force: bool = False) -> None:
        """
        Accept a GateResult. Emit appropriate log line. Handle suppression.
        Always appends to in-memory list (for EOD DB flush).
        Triggers a background periodic flush every 100 unsaved records.
        """
        self._records.append(gr)
        self._unsaved_count += 1
        self._emit_log(gr, force=force)

        if self._unsaved_count >= 100 and self._db_dsn:
            self._unsaved_count = 0
            t = threading.Thread(
                target=self._periodic_flush_sync,
                daemon=True,
                name="GateResultPeriodicFlush",
            )
            t.start()

    def _periodic_flush_sync(self) -> None:
        """Runs in a background thread. Creates its own event loop."""
        if not self._flush_lock.acquire(blocking=False):
            return   # Another flush already running — skip this cycle
        try:
            asyncio.run(self._flush_batch())
        finally:
            self._flush_lock.release()

    def _emit_log(self, gr: GateResult, force: bool = False) -> None:
        """Emit the structured log line for this gate result."""
        should_suppress, dup_count = self._should_suppress(gr)

        if should_suppress and not force:
            self._update_suppression(gr, suppressed=True)
            return

        self._update_suppression(gr, suppressed=False)

        gate_summary = self._format_gate_summary(gr)
        prefix = f"[{'SIGNAL' if gr.verdict == 'SIGNAL_FIRED' else gr.verdict}]"
        scan_tag = f"Scan#{gr.scan_id}"
        sym = gr.symbol.replace("NSE:", "").replace("-EQ", "")

        # Show dup count if we've been suppressing
        dup_suffix = f" (x{dup_count} earlier suppressed)" if dup_count > 0 else ""

        if gr.verdict == "SIGNAL_FIRED":
            logger.info(
                f"{prefix} {sym} | {scan_tag} | ALL GATES PASSED{dup_suffix}\n"
                f"  {gate_summary}\n"
                f"  → ENTRY ₹{gr.entry_price} | QTY: {gr.qty} | Tier: {gr.data_tier}"
            )
        elif gr.verdict == "REJECTED":
            logger.info(
                f"{prefix} {sym} | {scan_tag} | FAILED at {gr.first_fail_gate}{dup_suffix}\n"
                f"  {gate_summary}\n"
                f"  Reason: {gr.rejection_reason} | Tier: {gr.data_tier}"
            )
        elif gr.verdict == "DATA_ERROR":
            logger.warning(
                f"{prefix} {sym} | {scan_tag} | {gr.rejection_reason}{dup_suffix}"
            )
        elif gr.verdict == "SUPPRESSED":
            logger.debug(
                f"{prefix} {sym} | {scan_tag} | {gr.rejection_reason}"
            )

    def _format_gate_summary(self, gr: GateResult) -> str:
        """Formats one-line gate verdict summary for log output."""
        gates = [
            ("G1", gr.g1_pass, gr.g1_value),
            ("G2", gr.g2_pass, gr.g2_value),
            ("G3", gr.g3_pass, gr.g3_value),
            ("G4", gr.g4_pass, gr.g4_value),
            ("G5", gr.g5_pass, gr.g5_value),
            ("G6", gr.g6_pass, gr.g6_value),
            ("G7", gr.g7_pass, gr.g7_value),
            ("G8", gr.g8_pass, gr.g8_value),
            ("G9", gr.g9_pass, gr.g9_value),
            ("G10", gr.g10_pass, gr.g10_value),
            ("G11", gr.g11_pass, gr.g11_value),
            ("G12", gr.g12_pass, gr.g12_value),
        ]
        parts = []
        for name, passed, val in gates:
            if passed is None:
                continue  # Not evaluated
            status = "PASS" if passed else "FAIL"
            val_str = f"({val})" if val is not None else ""
            arrow = " ←" if (not passed and name == f"G{gr.first_fail_gate[-2:] if gr.first_fail_gate else '?'}") else ""
            parts.append(f"{name}:{status}{val_str}{arrow}")
        return " ".join(parts) if parts else "(no gates evaluated)"

    # ----------------------------------------------------------------
    # EOD Summary
    # ----------------------------------------------------------------

    def write_eod_summary(self, session_date: Optional[datetime.date] = None) -> str:
        """
        Writes a human-readable rejection summary to logs/rejections_YYYYMMDD.log.
        Returns the file path written.
        """
        if session_date is None:
            session_date = datetime.date.today()

        os.makedirs("logs", exist_ok=True)
        path = f"logs/rejections_{session_date.strftime('%Y%m%d')}.log"

        # Aggregate stats
        total_scans   = max((r.scan_id for r in self._records), default=0)
        total_evals   = len(self._records)
        signals_fired = sum(1 for r in self._records if r.verdict == "SIGNAL_FIRED")

        # Per symbol breakdown
        by_symbol: Dict[str, Dict[str, int]] = {}
        tier_counts: Dict[str, int] = {}
        for r in self._records:
            sym = r.symbol
            if sym not in by_symbol:
                by_symbol[sym] = {}
            gate = r.first_fail_gate or r.verdict
            by_symbol[sym][gate] = by_symbol[sym].get(gate, 0) + 1

            tier = r.data_tier or "UNKNOWN"
            tier_counts[tier] = tier_counts.get(tier, 0) + 1

        lines = [
            "═" * 60,
            f"SHORTCIRCUIT SIGNAL REJECTION SUMMARY — {session_date}",
            "═" * 60,
            f"Scans completed:          {total_scans}",
            f"Total evaluations:        {total_evals}",
            f"Unique symbols:           {len(by_symbol)}",
            f"Signals fired:            {signals_fired}",
            "",
            "Rejection breakdown:",
            "",
        ]

        # Sort symbols by total evaluation count desc
        for sym, gates in sorted(by_symbol.items(), key=lambda x: sum(x[1].values()), reverse=True):
            total_sym = sum(gates.values())
            lines.append(f"{sym.replace('NSE:', '').replace('-EQ', '')} (seen {total_sym} times)")
            for gate, count in sorted(gates.items(), key=lambda x: x[1], reverse=True):
                pct = count / total_sym * 100
                lines.append(f"  {gate}: {count:>4} rejections ({pct:.1f}%)")
            lines.append("")

        # Systemic issues
        lines.append("SYSTEMIC ISSUES DETECTED:")
        rest_pct = tier_counts.get("REST_EMERGENCY", 0) / max(total_evals, 1) * 100
        if rest_pct >= 50:
            lines.append(
                f"  ⚠️  Data tier: REST_EMERGENCY used for "
                f"{tier_counts.get('REST_EMERGENCY', 0)}/{total_evals} scans ({rest_pct:.0f}%)\n"
                f"      → CRITICAL: WS cache failed. PRD-007 must be verified."
            )
        for sym, gates in by_symbol.items():
            for gate, count in gates.items():
                if count == sum(gates.values()) and count >= 5:
                    lines.append(
                        f"  ⚠️  {gate} failed 100% of the time for {sym.replace('NSE:', '').replace('-EQ', '')}"
                    )

        lines.append("═" * 60)

        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        logger.info(f"[GateResultLogger] EOD summary written to {path}")
        return path

    # ----------------------------------------------------------------
    # PostgreSQL Flush — Shared logic for periodic + EOD
    # ----------------------------------------------------------------

    _INSERT_SQL = """
        INSERT INTO gate_results (
            session_date, scan_id, evaluated_at, symbol,
            nifty_regime, nifty_level,
            g1_pass, g1_value, g2_pass, g2_value,
            g3_pass, g3_value, g4_pass, g4_value,
            g5_pass, g5_value, g6_pass, g6_value,
            g7_pass, g7_value, g8_pass, g8_value,
            g9_pass, g9_value, g10_pass, g10_value,
            g11_pass, g11_value, g12_pass, g12_value,
            verdict, first_fail_gate, rejection_reason,
            data_tier, entry_price, qty
        ) VALUES (
            $1, $2, $3, $4, $5, $6,
            $7, $8, $9, $10, $11, $12, $13, $14,
            $15, $16, $17, $18, $19, $20, $21, $22,
            $23, $24, $25, $26, $27, $28, $29, $30,
            $31, $32, $33, $34, $35, $36
        )
        ON CONFLICT DO NOTHING
    """

    def _build_rows(self, records: List[GateResult]) -> List[tuple]:
        """Assemble DB row tuples from a list of GateResult objects."""
        rows = []
        today = datetime.date.today()
        for r in records:
            rows.append((
                today,
                r.scan_id,
                r.evaluated_at,
                r.symbol,
                r.nifty_regime,
                r.nifty_level,
                r.g1_pass,  _to_num(r.g1_value),
                r.g2_pass,  _to_num(r.g2_value),
                r.g3_pass,  _to_num(r.g3_value),
                r.g4_pass,  _to_num(r.g4_value),
                r.g5_pass,  _to_num(r.g5_value),
                r.g6_pass,  str(r.g6_value) if r.g6_value is not None else None,
                r.g7_pass,  str(r.g7_value) if r.g7_value is not None else None,
                r.g8_pass,  _to_num(r.g8_value),
                r.g9_pass,  str(r.g9_value) if r.g9_value is not None else None,
                r.g10_pass, _to_num(r.g10_value),
                r.g11_pass, _to_num(r.g11_value),
                r.g12_pass, _to_num(r.g12_value),
                r.verdict,
                r.first_fail_gate,
                r.rejection_reason or None,
                r.data_tier,
                r.entry_price,
                r.qty,
            ))
        return rows

    async def _flush_batch(self) -> int:
        """
        Inserts only the records not yet flushed (_flushed_count cursor).
        Opens its own asyncpg connection — safe to call from asyncio.run()
        in a daemon thread (periodic) OR from the main event loop (EOD).
        Advances _flushed_count only on success so EOD catches any failures.
        Returns number of rows inserted.
        """
        if not self._db_dsn:
            logger.debug("[GateResultLogger] No DSN set — flush skipped.")
            return 0

        pending = self._records[self._flushed_count:]
        if not pending:
            return 0

        rows = self._build_rows(pending)
        conn = None
        try:
            conn = await asyncpg.connect(self._db_dsn)
            await conn.executemany(self._INSERT_SQL, rows)
            # Advance count AFTER successful insert, BEFORE close
            self._flushed_count += len(rows)
            logger.info(
                f"[GateResultLogger] GATE FLUSH: {len(rows)} records saved "
                f"(session total: {self._flushed_count})"
            )
            return len(rows)
        except Exception as e:
            logger.error(
                f"[GateResultLogger] GATE FLUSH ERROR: {e} — "
                f"will retry at EOD ({len(rows)} records pending)"
            )
            return 0
        finally:
            if conn:
                try:
                    await conn.close()
                except Exception:
                    pass  # Close failure doesn't affect data integrity

    async def flush_to_db(self, db_manager=None) -> int:
        """
        EOD flush — inserts whatever periodic flush missed.
        db_manager is accepted for backward-compat but not used;
        _flush_batch() opens its own connection via DSN.
        """
        flushed = await self._flush_batch()
        if flushed == 0 and self._flushed_count == 0:
            logger.info("[GateResultLogger] No gate results to flush.")
        else:
            logger.info(
                f"[GateResultLogger] EOD flush complete. "
                f"Session total in DB: {self._flushed_count} records."
            )
        return flushed

    def get_scan_count(self) -> int:
        return max((r.scan_id for r in self._records), default=0)

    def get_records(self) -> List[GateResult]:
        return list(self._records)


def _to_num(val) -> Optional[float]:
    """Coerce to float or None for DB insert."""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


# ================================================================
# Module-level singleton
# ================================================================

_gate_result_logger: Optional[GateResultLogger] = None


def get_gate_result_logger() -> GateResultLogger:
    """Returns (or creates) the session-scoped singleton."""
    global _gate_result_logger
    if _gate_result_logger is None:
        _gate_result_logger = GateResultLogger()
        logger.info("[GateResultLogger] Singleton created for this session.")
    return _gate_result_logger


def reset_gate_result_logger() -> None:
    """Resets the singleton — used in tests only."""
    global _gate_result_logger
    _gate_result_logger = None
