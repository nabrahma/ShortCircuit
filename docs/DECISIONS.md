# Decisions

Architectural decisions already made and visible in the codebase, recorded
retroactively with the reasoning behind them. Each states what was chosen, what
it cost, and what would justify revisiting it.

---

## ADR-001 — Telegram is the only operator surface

**Decision.** No web UI. Alerts, commands, position snapshots and end-of-day
reports all flow through a single Telegram bot.

**Why.** A web UI for a single-operator system means another service, another
port, another authentication surface, and another thing that can be down when a
position is open. Telegram is already on the operator's phone, already
authenticated, and already reachable from anywhere.

**Cost.** Formatting is constrained. Telegram outages are visible to the
operator but do not stop trading — the bot keeps managing positions with no
observer, which is the correct failure mode but an uncomfortable one.

**Revisit if.** More than one person needs to operate it, or the alert volume
outgrows a chat window.

---

## ADR-002 — One open position at a time

**Decision.** A single capital slot, acquired after a confirmed fill and
released only after a confirmed close.

**Why.** This bounds the state space that reconciliation, capital control and
crash recovery must handle. With one position, "what does the broker think"
versus "what do we think" is a comparison of two small facts. With N positions
it is a set-difference problem with partial fills, and every recovery path
multiplies.

**Cost.** Concurrent opportunities are observed and logged but not taken. The
gate audit records these as `OBSERVED_NO_CAPITAL` so the cost is measurable
rather than invisible.

**Revisit if.** The reconciliation engine is proven against multi-position
divergence, with tests, first.

---

## ADR-003 — Stops are placed broker-side, not managed locally

**Decision.** On fill, a stop order is submitted to the exchange immediately.
The bot does not hold the stop in memory and fire it on a price trigger.

**Why.** A locally-managed stop protects nothing if the process dies, the host
loses power, or the network drops. A broker-side stop survives all three. This
is the single most important correctness decision in the system.

**Cost.** Modifying the stop (for breakeven) is a round-trip that can be
rejected. A stop resting at the exchange can also be seen by the exchange.

**Revisit if.** Never, without a very strong reason.

---

## ADR-004 — PostgreSQL rather than SQLite

**Decision.** PostgreSQL with `asyncpg` and a connection pool.

**Why.** Concurrent readers and writers — the trading loop, the reconciliation
engine and the Telegram handler all touch state simultaneously. SQLite's writer
lock would serialise them, and a lock contention stall during order placement is
not an acceptable failure mode. Postgres also gives real types for numeric
prices, which matters when the values are money.

**Cost.** An external service to run, back up and keep alive. Adds a dependency
to the deployment.

**Revisit if.** The system moves to a single-threaded design, which ADR-002
does not imply.

---

## ADR-005 — Parquet for machine-learning observations, Postgres for state

**Decision.** Trade and gate state live in Postgres. Feature observations are
appended to daily Parquet files with a CSV mirror.

**Why.** Different access patterns. State is transactional, queried by key, and
must be correct at every instant. Observations are append-only, columnar, read
in bulk at analysis time, and never read during trading. Putting them in
Postgres would grow the operational database without benefit.

**Cost.** Two storage systems. The Parquet files must be labelled at end of day
to be useful, which is a separate step that can fail independently.

---

## ADR-006 — Auto-trading is armed on boot

**Decision.** `AUTO_MODE` defaults to `True`.

**Why.** This reverses an earlier decision, and the reversal is the interesting
part. The original design required an explicit `/auto on` each morning as a
deliberate operator gate. In practice Telegram proved unreliable on this
connection, so the gate became a way to miss entire sessions without noticing.

Being armed at boot is not the same as trading at boot: `TRADING_ENABLED` is a
separate gate that `MarketSession` only opens at 09:30 IST.

**Cost.** A restart during market hours resumes trading without a human
confirming. Mitigated by the fact that nothing disarms automatically — only an
explicit `/auto off` — so the state is at least predictable.

---

## ADR-007 — Fail closed on missing data

**Decision.** Any gate that cannot obtain the data it needs blocks the trade
rather than passing it through.

**Why.** A false block costs a missed trade, which is bounded and recoverable.
A false pass during a hard trend is unbounded. The asymmetry is not close.

**Cost.** Degraded data availability produces zero signals rather than
lower-quality ones, which can look like the system is broken when it is
behaving correctly.

---

## ADR-008 — The strategy layer imports nothing from the runtime layer

**Decision.** `strategy/` receives clients by injection and never imports the
broker, database, or Telegram modules.

**Why.** It keeps the trading logic testable without mocking anything, and makes
the dependency direction obvious. Enforced by `tests/unit/test_brain_isolation.py`
rather than by convention — an architectural boundary that is not tested is a
boundary that quietly stops existing.

**Cost.** Some plumbing to pass clients down. `market_context` and
`htf_confluence` take an injected `fyers` client, which is the compromise.

---

## ADR-009 — No take-profit; positions run to the stop or the square-off

**Decision.** The take-profit exit was removed (2026-08-06), and the 45-minute
time-based exit disabled.

**Why.** The target sat at the midpoint between entry and VWAP, so the favourable
excursion was truncated by construction while the adverse excursion still ran the
full distance to the stop. That asymmetry is structural, not empirical — it
follows from where the two levels sit, regardless of outcome.

Recorded excursion data supported the same conclusion: mean favourable excursion
exceeded mean adverse excursion, while the target sat well inside the favourable
range. The time-based exit truncated the same excursion for the same reason.

**Cost.** Positions are held longer, so more exposure to intraday reversal, and
a position that would have been closed at a small profit may now reach the stop.

**Revisit if.** `data/ml/` accumulates enough labelled observations to show that
a specific target level beats letting the trade run. Not before — this decision
was made by removing an assumption, and reintroducing one needs evidence.

---

## ADR-010 — The repository publishes system metrics, never performance figures

**Decision.** Latency, throughput, gate volumes, coverage and recovery times are
published. Returns, win rates, P&L and backtests are not.

**Why.** Publishing returns from a personal trading account is a performance
claim, which carries regulatory weight in India and invites an argument beside
the point of what this repository demonstrates. Publishing system metrics tells
a reader more about the real behaviour of the pipeline anyway.

**Cost.** A reader cannot tell whether the strategy works. That is intentional.
See [`DISCLOSURE.md`](DISCLOSURE.md).
