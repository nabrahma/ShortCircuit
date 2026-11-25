# ShortCircuit

> Live intraday NSE execution engine for Fyers, with plug-in strategies:
> WebSocket-first market data with freshness tracking, sequential gate
> validation, broker-verified fills, capital-aware sizing, and automated state
> reconciliation. Bring a strategy; the engine does the rest.

[![CI](https://github.com/nabrahma/ShortCircuit/actions/workflows/ci.yaml/badge.svg)](https://github.com/nabrahma/ShortCircuit/actions/workflows/ci.yaml)
[![Security](https://github.com/nabrahma/ShortCircuit/actions/workflows/security.yaml/badge.svg)](https://github.com/nabrahma/ShortCircuit/actions/workflows/security.yaml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

> ⚠️ **Not investment advice. No performance claims.** This repository documents
> the engineering of a personal execution system. See [DISCLOSURE.md](docs/DISCLOSURE.md).

---

## What this is

**ShortCircuit is a live intraday NSE execution engine for Fyers. Strategies
plug into it.**

It is not a chart pattern script. It is not a notification bot with a broker
wrapper bolted on. ShortCircuit is a trading system covering market data
ingestion, microstructure filtering, sequential gate evaluation, capital-aware
sizing, broker-side risk placement, reconciliation, audit logging, Telegram
command and control, and parquet-based ML feedback loops.

A strategy is a small class that looks at one candidate and answers one
question: *is there a setup here, and how should it be traded?* Everything else
— the universe, data, risk gates, orders, stops, exits, square-off, reconciliation
and logging — is the engine's. See [Writing a strategy](#writing-a-strategy).

The design premise:

> Setups are only acted on when extension, rejection, and liquidity decay
> coincide, and only once execution can be verified.

It is built around one operating principle:

> **If the system cannot prove the setup, prove the fill, prove the stop, and
> prove the state, it should not trade.**

---

## Executive summary

ShortCircuit scans thousands of NSE equity symbols during the live session. Each
candidate that clears the scanner is enriched once — session VWAP and its
standard-deviation stretch, ATR, VWAP slopes, volume z-score, market profile,
order-book spread and circuit state — and handed to the active strategy
plug-ins, in priority order.

When a strategy reports a setup, the system does not immediately trade. The
signal passes the engine's own gates (trade window, higher-timeframe
confirmation where the strategy asks for it, cooldown and daily target), and the
focus engine then waits for a real structural trigger: a break of the entry
level, with invalidation above the setup high.

Only after that does the order layer engage.

At runtime the bot coordinates Fyers REST and WebSocket connectivity, a metered
REST transport that keeps every call inside the broker's rate limits, the NSE-EQ
scanner universe, a WebSocket-first quote cache with freshness tracking, signal
validation, a single-position capital slot, broker-side stop placement, an
active trade monitor, Telegram control, a PostgreSQL audit trail, ML parquet
logging, EOD reporting, and broker/DB reconciliation.

---

## The Brain vs Muscle architecture

ShortCircuit is split into two components with an **enforced dependency
boundary**: `strategy/` imports nothing from the runtime layer, and a test
asserts it.

### The Brain (`src/shortcircuit/strategy/`, plus your plug-ins)

All trading intelligence is sealed away from the runtime. It knows nothing about
brokers, websockets, or Telegram. It only knows math, risk, and logic.

- `strategy/sdk.py`: the plug-in contract — `Strategy`, `SetupContext`, `Signal`, `ExitPlan`
- `strategy/registry.py`: discovery and loading of plug-ins
- `strategy/examples/`: a complete example strategy
- `strategy/features.py`: stateless math (VWAP, RSI, volume fade, ATR)
- `strategy/market_profile.py`: Dalton value areas (VAH/VAL/POC)
- `strategy/market_context.py`: Nifty regime and broader trend
- `strategy/htf_confluence.py`: higher-timeframe risk gates
- `strategies/` at the repository root: **your** strategies, git-ignored

That boundary is verified, not asserted:
[`tests/unit/test_brain_isolation.py`](tests/unit/test_brain_isolation.py) parses
the AST of every strategy module and fails if one imports the runtime layer, an
I/O library, or opens a file.

### The Muscle (the rest of the package)

The rest of the repository is the muscle, nervous system, and immune system. It
fetches data, asks the Brain for decisions, and pulls the physical triggers.

- `execution/analyzer.py`: the orchestrator that asks the active strategies what to do
- `execution/order_manager.py` / `broker/fyers_broker_interface.py`: the hands that execute
- `state/reconciliation.py`: the immune system verifying local state against the broker

---

## Writing a strategy

```python
from shortcircuit.strategy.sdk import Direction, SetupContext, Signal, Strategy

class MyFade(Strategy):
    name = "my_fade"
    direction = Direction.SHORT

    def evaluate(self, ctx: SetupContext) -> Signal | None:
        if ctx.gain_pct < 10 or ctx.vwap_sd < 3 or not ctx.is_decaying:
            return None
        return Signal(pattern="MY_FADE", stop_reference=ctx.day_high)
```

Save it as `strategies/my_fade.py`, then run it:

```bash
SHORTCIRCUIT_STRATEGIES=my_fade python -m shortcircuit
```

`strategies/` is git-ignored apart from its README, so a private strategy never
reaches a public fork. The full guide — the context a strategy receives, exit
plans, re-arm windows, testing, and the rules a strategy must keep — is
[docs/STRATEGIES.md](docs/STRATEGIES.md).

The engine currently executes SHORT strategies only; its validation gate enters
on a break below the trigger. The contract already carries `Direction.LONG`, and
the registry refuses a LONG plug-in rather than let it trade through short-side
mechanics.

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
market data. The bot treats that distinction as a first-class safety property.
It exists because a reconnect once looked healthy while no tick had arrived for
the affected symbols ([D-002](docs/DISCOVERIES.md)).

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

Exits: a staged take-profit, the broker-side stop, or the 15:10 IST square-off.
Under the default `TP_MODE='SCALE'` the position sheds half at the midpoint
between entry and the VWAP target and runs the remainder to that target, moving
the stop to breakeven once the partial fills. `'SINGLE'` closes fully at the
midpoint; `'OFF'` leaves the stop and the square-off as the only exits.

The take-profit was removed in August and restored on 2026-08-30 after a replay
of every live trade reversed the original reading. [ADR-009](docs/DECISIONS.md)
records the removal, [ADR-011](docs/DECISIONS.md) the reversal and why the
evidence for it is a point estimate rather than a proven result.

**Manual override.** If the operator changes a stop directly at the broker, the
bot detects the structural change, sets `manual_override` and backs off. It
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

There is no web UI in the runtime path. Fewer services, fewer sockets, one
control plane ([ADR-001](docs/DECISIONS.md)).

---

## Persistence and auditability

PostgreSQL for orders, positions, reconciliation events and gate results; a CSV
signal log; daily session logs; a daily rejection summary; ML parquet
observations; and an EOD markdown report.

**Every candidate that reaches analyzer evaluation produces a gate result.** This
matters because the bot is not only an execution engine. It is also a research
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

**Seven divergence categories, each with a test.** Plus an eighth asserting
adoption is idempotent, and two safety properties: a degraded broker API must not
be classified as flat, and settlement lag must not raise a false orphan. The full
table is in [docs/TESTING.md](docs/TESTING.md).

---

## Engineering practices

A system that places real orders gets built differently. A bug here does not raise an exception in CI. It loses money during a live
session, days later, in a way that looks like bad luck.

### Testing

Unit, property-based and pipeline tests, all deterministic and offline: a
fixture blocks every outbound socket, so a test that reaches for a live broker
fails rather than placing an order. The strategy plug-in system is tested
against throwaway strategies written in the tests, so the public suite needs
nothing from any deployment's private strategies; those carry their own tests in
`strategies/tests/`, which pytest collects when present. Coverage is
concentrated where an error is hardest to notice: `htf_confluence.py` is at 100%
on statements and branches and `market_context.py` at 97%. `market_profile.py` (40%) and the capital layer (75%)
are both below their floors, and that is stated in
[docs/KNOWN_GAPS.md](docs/KNOWN_GAPS.md) rather than hidden behind a global
average.

The reconciliation divergence table is the highest-value file in the suite.
Property-based tests assert invariants over generated series rather than
examples: that VWAP stays inside the traded range, that a zero-volume bar does
not move it, and that read-only feature calls never mutate their input frame.

Deliberately not tested: live broker connectivity, WebSocket transport, Telegram
delivery, and **the strategy's profitability**. A test suite cannot validate an
edge, and claiming otherwise would be dishonest.

### Continuous integration

Lint, tests across Python 3.11 and 3.12, integration against a real PostgreSQL
service, and a container build that asserts no `.env` is baked into the image.
Every GitHub Action is pinned to a commit SHA rather than a tag.

### Failure handling

WebSocket drop, margin rejection, orphan detection, process death with a position
open. Each has a documented procedure in
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

Six of the eight share a shape: **something reported success while doing
nothing.** A websocket handler that parsed no messages. A cache with no writer. A
connection-pool fix applied to an attribute that does not exist. A timeout that
abandoned the request it was meant to bound.

None raised an exception. All were found by reading logs against the code and
asking whether the numbers agreed: 48 versus 0, 42 versus 42, 41 versus 41.

---

## System measurements

Engineering metrics only. No performance, profitability or return figures. See
[DISCLOSURE.md](docs/DISCLOSURE.md). All traceable to
[`docs/evidence/system-measurements.txt`](docs/evidence/system-measurements.txt).

| Metric | Value |
|---|---|
| Session logs analysed | 28 |
| Scan cycles recorded | 4,776 |
| Scanner universe | ~2,400 symbols |
| Scan latency (p50 / p99) | 9 ms / 501 ms |
| Cache priming, cold start | 4 to 25 s observed |
| Reconciliation cadence | every 6 s during market hours |
| Tests (public suite) / runtime | 496 / ~30 s |

These describe how the system runs, not how the strategy performs. Gate
thresholds, rejection distributions and anything else that characterises the
filter are deliberately not published.

---

## Running it

**Prerequisites:** Python 3.11+, PostgreSQL 14+ (tested on 16), a Fyers API app,
and a Telegram bot. There is no way to run this without a broker account.

```bash
python -m venv .venv && source .venv/bin/activate
make install
```

Copy [`.env.example`](.env.example) to `.env` and fill it in. Variable names
matter — `DB_PASS`, not `DB_PASSWORD`, is what `deploy/docker-compose.yml`
requires, and `AUTO_MODE` there is documentation only: it is a constant in
`config.py`, armed on boot, and disarmed at runtime with `/auto off`.

Create the database and apply the schema. **Migrations are not applied
automatically** — run them once, in lexical order:

```bash
createdb shortcircuit_trading
for f in migrations/*.sql; do psql -d shortcircuit_trading -v ON_ERROR_STOP=1 -f "$f"; done
```

The role you connect as should own the database; `v42_1_0_postgresql.sql` creates
the `uuid-ossp` extension, which a database owner may do on PostgreSQL 14+.

```bash
make test           # 496 tests, no credentials or database needed
make demo           # the same suite in a container, no credentials needed
python main.py      # live — requires credentials and a migrated database
```

**Live operation requires broker credentials.** There is no paper-trading mode,
and one has not been faked for demonstration purposes. What is reproducible
without credentials is the build and the full test suite.

### Data-only mode and backtesting

```bash
SHORTCIRCUIT_MODE=data_only python main.py            # live data, scans, signals — no orders
python -m shortcircuit.backtest --from 2026-10-01 --to 2026-11-20
```

Data-only logs in and runs the whole pipeline against the live market, logging
`[DATA-ONLY] would SHORT …` where it would have entered. It cannot place, modify
or cancel an order: the transport refuses every order write before it is sent.
Fyers enforces the static-IP rule on orders only, so this works from any
connection.

The backtest is always data-only. It finds every session in which a stock traded
10%+ above its previous close, fetches those days' 1-minute candles (cached
under `data/backtest/`), and replays them minute by minute through the live
analyzer's feature code and the engine's exit rules. Trades, every signal and
a summary go to `reports/backtest/`. What it models rather than replays (fills,
slippage, the circuit band it infers from the day) is listed at the top of
[`backtest/replay.py`](src/shortcircuit/backtest/replay.py).

Each run also writes an HTML dashboard beside the CSV: the account curve and
drawdown, a scorecard and funnel, a candlestick replay of every trade with its
entry, partials, stop path and RSI, a daily P&L calendar, and exit, return,
time-of-day and monthly breakdowns. Leverage (1×/4×/5×) and single-slot versus
every-signal are toggles on the page. It is built from cached data, so it can
be rebuilt without logging in:

```bash
python -m shortcircuit.backtest.report reports/backtest/<run>.csv --leverage 5
```

---

## Project layout

```text
main.py                       Entry-point shim, so `python main.py` still works
src/shortcircuit/
├── config.py                 Risk, mode and infrastructure parameters
├── paths.py                  Repository-anchored runtime paths
├── runtime/                  Supervisor, market session, startup recovery
├── marketdata/               Scanner, symbols, session helpers
├── broker/                   Fyers REST + WebSocket, auth, metered transport, rate limiter
├── execution/                Analyzer, focus engine, order/signal/trade managers
├── capital/                  Funds sync, sizing, capital slot
├── state/                    PostgreSQL access, reconciliation
├── observability/            Telegram, gate audit, ML logger
├── eod/                      EOD analyzer, scheduler, watchdog
└── strategy/                 The Brain: logic and math only
    ├── sdk.py                The strategy plug-in contract
    ├── registry.py           Plug-in discovery and loading
    ├── examples/             A complete example strategy
    ├── features.py           VWAP, RSI, volume, ATR, patterns
    ├── market_profile.py     Dalton value areas
    ├── market_context.py     Nifty regime
    └── htf_confluence.py     Higher-timeframe gates

strategies/                   Your strategy plug-ins (git-ignored, except README)
tests/                        Unit, property and pipeline tests
docs/                         Architecture, strategies, operations, decisions, evidence
deploy/                       Dockerfile and compose stacks
scripts/                      Operational scripts
migrations/                   PostgreSQL schema migrations
```

---

## Limitations

Worth stating plainly. A repo with no limitations section usually just has undocumented ones.

- **One position at a time.** Deliberate ([ADR-002](docs/DECISIONS.md)), but it
  means concurrent opportunities are logged and skipped.
- **One broker.** Fyers-specific. The broker interface is not abstracted behind a
  generic protocol.
- **The backtest models what history cannot show.** Fills, slippage, the
  circuit band and the single capital slot are approximated, and MIS leverage
  is not screened; see the list at the top of `backtest/replay.py`.
- **Short side only.** The validation gate and stop mechanics are short-only;
  LONG strategies are refused at load time until they are supported.
- **One scan universe.** The scanner's gain, volume and price filters are engine
  config shared by every strategy, not chosen per strategy.
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
| [STRATEGIES.md](docs/STRATEGIES.md) | Writing, testing and running a strategy plug-in |
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

The reconciliation problem here, where local records and an external system's
records disagree without either side raising an error, turns out to generalise.
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
