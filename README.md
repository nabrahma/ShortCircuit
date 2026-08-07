# ShortCircuit

> Live intraday NSE execution engine: WebSocket-first market data with freshness
> tracking, sequential gate validation, broker-verified fills, capital-aware
> sizing, and automated state reconciliation.

[![CI](https://github.com/nabrahma/ShortCircuit/actions/workflows/ci.yaml/badge.svg)](https://github.com/nabrahma/ShortCircuit/actions/workflows/ci.yaml)
[![Security](https://github.com/nabrahma/ShortCircuit/actions/workflows/security.yaml/badge.svg)](https://github.com/nabrahma/ShortCircuit/actions/workflows/security.yaml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

> ⚠️ **Not investment advice. No performance claims.** This repository documents
> the engineering of a personal execution system. See [DISCLOSURE.md](docs/DISCLOSURE.md).

---

## What this is

**ShortCircuit is a live intraday NSE execution engine for detecting and trading
momentum exhaustion.**

It is not a chart pattern script. It is not a notification bot with a broker
wrapper bolted on. ShortCircuit is a trading system covering market data
ingestion, microstructure filtering, sequential gate evaluation, capital-aware
sizing, broker-side risk placement, reconciliation, audit logging, Telegram
command and control, and parquet-based ML feedback loops.

The design premise:

> Setups are only acted on when extension, rejection, and liquidity decay
> coincide — and only after execution can be verified.

It is built around one operating principle:

> **If the system cannot prove the setup, prove the fill, prove the stop, and
> prove the state, it should not trade.**

---

## Executive summary

ShortCircuit scans thousands of NSE equity symbols during the live session and
hunts for a specific event:

**an overextended intraday mover failing at or above value while momentum buyers
are trapped and upward auction quality is degrading.**

When the setup appears, the system does not immediately trade. It promotes the
symbol into a staged gate pipeline. The analyzer passes the data to the "Brain"
(the `BackToVWAPShort` strategy) to approve structure, momentum, volume, profile,
confluence, and higher-timeframe context. The focus engine then waits for a real
structural trigger: a break of the entry level, with invalidation above the setup
high.

Only after that does the order layer engage.

At runtime the bot coordinates Fyers REST and WebSocket connectivity, the NSE-EQ
scanner universe, a WebSocket-first quote cache with freshness tracking, a single
exhaustion strategy, signal validation, a single-position capital slot,
broker-side stop placement, an active trade monitor, Telegram control, a
PostgreSQL audit trail, ML parquet logging, EOD reporting, and broker/DB
reconciliation.

---

## The Brain vs Muscle architecture

ShortCircuit is split into two components with an **enforced dependency
boundary**: `strategy/` imports nothing from the runtime layer, and a test
asserts it.

### The Brain (`strategy/`)

All trading intelligence is physically sealed inside `strategy/`. It knows
nothing about brokers, websockets, or Telegram. It only knows math, risk, and
logic.

- `strategy/back_to_vwap.py` — the single unified execution logic
- `strategy/features.py` — stateless math (VWAP, RSI, volume fade, ATR)
- `strategy/market_profile.py` — Dalton value areas (VAH/VAL/POC)
- `strategy/market_context.py` — Nifty regime and broader trend
- `strategy/htf_confluence.py` — higher-timeframe risk gates

That boundary is verified, not asserted:
[`tests/unit/test_brain_isolation.py`](tests/unit/test_brain_isolation.py) parses
the AST of every strategy module and fails if one imports the runtime layer, an
I/O library, or opens a file.

### The Muscle (root)

The rest of the repository is the muscle, nervous system, and immune system. It
fetches data, asks the Brain for decisions, and pulls the physical triggers.

- `analyzer.py` — the orchestrator that asks the Brain what to do
- `order_manager.py` / `fyers_broker_interface.py` — the hands that execute
- `reconciliation.py` — the immune system verifying local state against the broker

---

## Strategy model: BackToVWAPShort

A single strategy: **BackToVWAPShort**.

It is primarily a short-side mean-reversion and exhaustion engine (it natively
supports `/mode buy` via Telegram for inverse logic), built around three
observations:

1. Strong intraday gain creates unstable positioning.
2. Momentum becomes tradable only when extension meets rejection.
3. A signal is not an entry until price confirms structural failure.

The bot is interested in stocks that have already moved aggressively, usually
above VWAP and value, where late momentum participants are vulnerable. It then
looks for evidence that the move is no longer being accepted:

- Intraday gain expansion
- VWAP standard-deviation stretch
- Value Area High or profile rejection
- VWAP flattening or momentum decay
- Volume fade after surge
- Failed continuation structure
- Higher-timeframe stall or fail-open guard
- Candle trigger through the signal low

It is not "short everything that is up." It is "short only when an up-move has
become statistically stretched, structurally rejected, and execution-confirmed."

Full methodology — the stretch calculation, the Dalton value-area algorithm,
volume-fade detection, and each gate's threshold — is in
[docs/STRATEGY.md](docs/STRATEGY.md).

---

## Data plane

WebSocket-first, with an explicit freshness state machine.

```text
UNINITIALIZED → PRIMING → READY → DEGRADED → REPRIME/RECOVER
```

- REST seed gives cold-start coverage.
- **REST seed does not count as true freshness.**
- WebSocket tick freshness determines readiness.
- Startup waits for cache readiness before scanning.
- Degraded cache can reprime. REST fallback is available during data stress.

The system separates **"known" from "fresh."** A stale cached quote is not live
market data. The bot treats that distinction as a first-class safety property —
and it exists because a reconnect once looked healthy while no tick had arrived
for the affected symbols ([D-002](docs/DISCOVERIES.md)).

---

## Execution plane

```text
Signal detected by the Brain
  → Telegram alert / ML observation / gate audit
  → Pending validation (Focus Engine)
  → Trigger break
  → Capital slot check (dynamic 4x/5x leverage)
  → Entry order (graceful margin fallback)
  → WebSocket fill confirmation, REST verification fallback
  → Broker-side stop placement
  → Active focus monitor
```

Exits: the broker-side stop, or the 15:10 IST square-off. There is no
take-profit — see [ADR-009](docs/DECISIONS.md) for why it was removed.

**Manual override.** If the operator changes a stop directly at the broker, the
bot detects the structural change, sets `manual_override`, and backs off — it
stops managing the stop and hands over without exiting the trade.

The system holds one position at a time. This bounds the state space that
reconciliation, capital control, and recovery must handle
([ADR-002](docs/DECISIONS.md)).

---

## Capital model

Sizing comes from live broker funds, not a hardcoded number. The capital manager
reads Fyers funds, parses multiple response shapes, derives real margin, applies
dynamic intraday leverage (5x primary, auto-scaling to 4x on margin rejection),
reserves one active slot, prevents concurrent entries, releases only after a
confirmed close, and resyncs after fills and exits.

The bot can scan continuously, but entry is blocked while capital is occupied.

---

## Operator surface

Telegram is the only operator interface: startup and status alerts, signal
discovery, validation updates, `/auto on|off`, `/mode buy|sell`, `/status`,
`/health`, broker health alerts, trade notifications, risk alerts, and EOD
reports.

There is no web UI in the runtime path — fewer services, fewer sockets, one
control plane ([ADR-001](docs/DECISIONS.md)).

---

## Persistence and auditability

PostgreSQL for orders, positions, reconciliation events and gate results; a CSV
signal log; daily session logs; a daily rejection summary; ML parquet
observations; and an EOD markdown report.

**Every candidate that reaches analyzer evaluation produces a gate result.** This
matters because the bot is not only an execution engine — it is a research
instrument. Schema: [docs/DATA_MODEL.md](docs/DATA_MODEL.md).

---

## Reconciliation and recovery

Live trading systems fail when internal state and broker state diverge.
Reconciliation is a core runtime service, not a maintenance script.

It detects orphaned broker positions, phantom internal positions, quantity
mismatches, manual closes, and broker-side fills missed by the internal loop.
Recovery adopts orphans with emergency protection, releases capital for phantoms,
updates DB state, marks the cache dirty, alerts the operator, and avoids
duplicate adoption.

**Seven divergence categories, each with a test** — plus an eighth asserting
adoption is idempotent, and two safety properties: a degraded broker API must not
be classified as flat, and settlement lag must not raise a false orphan. The full
table is in [docs/TESTING.md](docs/TESTING.md).

---

## Engineering practices

A system that places real orders is built differently from one that does not. A
bug here does not raise an exception in CI — it loses money during a live
session, days later, in a way that looks like bad luck.

### Testing

143 unit and property-based tests. Coverage is concentrated where an error is
hardest to notice: the strategy math (`features.py` at 89%) and the capital
layer (74%). Three strategy modules are not yet covered, and that is stated in
[docs/KNOWN_GAPS.md](docs/KNOWN_GAPS.md) rather than hidden behind a global
average.

The reconciliation divergence table is the highest-value file in the suite.
Property-based tests assert invariants over generated series rather than
examples — that VWAP stays inside the traded range, that a zero-volume bar does
not move it, and that read-only feature calls never mutate their input frame.

Deliberately not tested: live broker connectivity, WebSocket transport, Telegram
delivery, and **the strategy's profitability** — a test suite cannot validate an
edge, and claiming otherwise would be dishonest.

### Continuous integration

Lint, tests across Python 3.11 and 3.12, integration against a real PostgreSQL
service, and a container build that asserts no `.env` is baked into the image.
Every GitHub Action is pinned to a commit SHA rather than a tag.

### Failure handling

WebSocket drop, margin rejection, orphan detection, process death with a position
open — each has a documented procedure in
[docs/OPERATIONS.md](docs/OPERATIONS.md). Stops are placed broker-side
specifically so protection survives local process death.

### Reproducibility

`docker compose -f deploy/docker-compose.test.yml up --exit-code-from tests`
builds the system and runs the full suite on any machine with Docker, **with no
credentials**. Dependencies are pinned.

### Secret handling

Credentials come from `.env` only, never logged. `gitleaks` runs pre-commit and
in CI over the **full git history**, because a deleted credential still lives
there. Findings are disclosed in [docs/SECURITY.md](docs/SECURITY.md) rather
than quietly resolved.

### Auditability

Every gate evaluation produces a record. Structured session logs, parquet
observations with outcome labelling, and a daily rejection breakdown.

---

## Key discoveries

Eight real problems found while building and operating this system, with
evidence: [docs/DISCOVERIES.md](docs/DISCOVERIES.md).

Six of the eight share a shape — **something reported success while doing
nothing.** A websocket handler that parsed no messages. A cache with no writer. A
connection-pool fix applied to an attribute that does not exist. A timeout that
abandoned the request it was meant to bound.

None raised an exception. All were found by reading logs against the code and
asking whether the numbers agreed: 48 versus 0, 42 versus 42, 41 versus 41.

---

## System measurements

Engineering metrics only. No performance, profitability, or return figures — see
[DISCLOSURE.md](docs/DISCLOSURE.md). All traceable to
[`docs/evidence/system-measurements.txt`](docs/evidence/system-measurements.txt).

| Metric | Value |
|---|---|
| Session logs analysed | 28 |
| Scan cycles recorded | 4,776 |
| Scanner universe | ~2,400 symbols |
| Scan latency (p50 / p99) | 9 ms / 501 ms |
| Cache priming, cold start | 4–25 s observed |
| Reconciliation cadence | every 6 s during market hours |
| Gate rejections recorded | 878 across 27 sessions |
| Tests / runtime | 143 / ~6.5 s |

**Rejection breakdown.** Of 878 recorded gate rejections: **98.7% were rejected
by the strategy's six hard gates**, 1.0% by higher-timeframe confluence, 0.2% by
the signal manager. 55 candidates passed all six gates across those sessions.

That describes how the filter behaves, not what it earns. The pipeline is
overwhelmingly rejection-dominated by design — the first stage does nearly all
the work, and the later gates exist to catch what it lets through.

---

## Running it

```bash
python -m venv .venv && source .venv/bin/activate
make install
```

Create `.env` from [`.env.example`](.env.example) with your Fyers credentials,
Telegram token, and PostgreSQL connection.

```bash
python main.py      # live — requires credentials
make test           # tests — no credentials needed
make demo           # containerised test suite — no credentials needed
```

**Live operation requires broker credentials.** There is no offline or paper
mode, and one has not been faked for demonstration purposes. What is reproducible
without credentials is the build and the full test suite.

---

## Project layout

```text
main.py                      Runtime supervisor and task orchestration
config.py                    Strategy, risk, mode, and infrastructure parameters
scanner.py                   NSE-EQ scanner and candle prefetcher
analyzer.py                  Orchestrator: data → Brain → signal
focus_engine.py              Pending validation and active position monitoring
order_manager.py             Entry, stops, exits, fill verification
capital_manager.py           Funds sync, sizing, capital slot control
fyers_broker_interface.py    Data/order WebSocket and broker abstraction
telegram_bot.py              Operator command and alert interface
database.py                  PostgreSQL access layer
reconciliation.py            Broker/DB/internal-state reconciliation
gate_result_logger.py        Gate audit trail and EOD rejection summary
ml_logger.py                 Parquet/CSV ML observation logger
eod_analyzer.py              EOD analytics, missed-signal audit, ML labelling

strategy/                    The Brain — logic and math only
├── back_to_vwap.py          Single unified strategy
├── features.py              VWAP, RSI, volume, ATR, patterns
├── market_profile.py        Dalton value areas
├── market_context.py        Nifty regime
└── htf_confluence.py        Higher-timeframe gates

tests/                       143 unit, property and integration tests
docs/                        Architecture, strategy, operations, decisions, evidence
deploy/                      Dockerfile and compose stacks
migrations/                  PostgreSQL schema migrations
```

---

## Limitations

Stated plainly, because their absence would be the more interesting signal.

- **One position at a time.** Deliberate ([ADR-002](docs/DECISIONS.md)), but it
  means concurrent opportunities are logged and skipped.
- **One broker.** Fyers-specific. The broker interface is not abstracted behind a
  generic protocol.
- **No backtester in this repository.** Historical evaluation is done separately;
  nothing here validates the strategy against past data.
- **Coverage is uneven.** Concentrated in strategy math and the capital layer;
  three strategy modules are untested, and the order and focus engines are
  covered only indirectly ([KNOWN_GAPS.md](docs/KNOWN_GAPS.md)).
- **No offline mode.** Live operation requires real credentials.
- **Single operator, single account.** No multi-user or multi-account concept.

---

## Documentation

| Document | Contents |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design and runtime model |
| [STRATEGY.md](docs/STRATEGY.md) | Gate methodology and the quantitative work behind it |
| [OPERATIONS.md](docs/OPERATIONS.md) | Runbook: what to do when things fail |
| [TESTING.md](docs/TESTING.md) | Test levels, divergence table, invariants, non-goals |
| [DATA_MODEL.md](docs/DATA_MODEL.md) | PostgreSQL schema and parquet observation schema |
| [DECISIONS.md](docs/DECISIONS.md) | Architectural decision records |
| [DISCOVERIES.md](docs/DISCOVERIES.md) | Real problems hit, with evidence |
| [KNOWN_GAPS.md](docs/KNOWN_GAPS.md) | What is not done, honestly |
| [SECURITY.md](docs/SECURITY.md) | Credential handling and scan results |
| [DISCLOSURE.md](docs/DISCLOSURE.md) | Regulatory posture and non-claims |

---

## Related work

The reconciliation problem here — local records and an external system's records
disagreeing with neither side raising an error — turns out to generalize.
[driftwatch](https://github.com/nabrahma/driftwatch) is a Go tool that detects the
same divergence class in event-sourced caches.

---

## Risk statement

ShortCircuit is live trading infrastructure. It can place real orders, create
real exposure, and lose real money.

It is engineered for discipline, observability, and fast recovery. It is not
engineered to guarantee outcomes. Markets are adversarial, broker APIs fail,
liquidity disappears, and no gate stack can eliminate risk.

Use it like a production system:
- monitor it
- audit it
- keep secrets out of logs
- verify broker state
- review every EOD report
- never assume a green process means a flat broker account

---

## Disclosure

This repository is published as software, not as a recommendation. It publishes
no performance, profitability, or return figures, and none should be inferred.
The author is not a registered Investment Adviser or Research Analyst. Full
terms: [docs/DISCLOSURE.md](docs/DISCLOSURE.md).

---

## License

[Apache-2.0](LICENSE).
