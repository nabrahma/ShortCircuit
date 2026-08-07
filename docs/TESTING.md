# Testing

```bash
make test              # unit + property — no network, no credentials
make test-integration  # needs PostgreSQL
make coverage          # per-package coverage
make verify            # everything CI runs, locally
```

143 tests. Full output: [`evidence/test-suite-full.txt`](evidence/test-suite-full.txt).

## What is tested, and why that

The suite is concentrated where this codebase is strongest and where an error is
hardest to notice: the quantitative math and the state-divergence logic. A wrong
VWAP does not raise — it produces a slightly wrong signal forever.

| Area | Tests | Rationale |
|---|---|---|
| `strategy/features.py` | 33 | Pure functions feeding every gate; zero mocking required |
| `symbols`, `market_utils`, `config` | 31 | Symbol validation, session boundaries, config invariants |
| `capital_manager.py` | 20 | Multi-shape funds parser, leverage downgrade, slot control |
| `strategy/market_profile.py` | 14 | Dalton value area ordering and degenerate distributions |
| Brain isolation | 12 | Architectural boundary, enforced rather than asserted |
| Reconciliation | 12 | The divergence table below |
| `rest_limiter.py` | 12 | Sliding windows, priority reservation, daily quota |
| Property-based | 9 | Invariants over generated series |

## The reconciliation divergence table

The highest-value file in the suite. Live trading systems fail when local state
and broker state disagree while neither side raises an error, so every way they
can disagree is enumerated and asserted.

| Broker state | Local state | Expected classification |
|---|---|---|
| position present | absent | orphan → adopt with emergency protection |
| absent | position present | phantom → release capital |
| qty 100 | qty 50 | quantity mismatch |
| absent | absent | agree, no action |
| present | same position, same qty | agree, no action |
| closed manually | open | manual close detected |
| filled | not recorded | missed fill |

Plus an eighth asserting **adoption is idempotent** — two reconcile cycles must
not adopt the same orphan twice, which would place a second stop for a quantity
already protected.

And two safety properties, both regressions of real incidents:

- **A degraded broker API must not be classified as flat.** An empty position
  set from a failed fetch would make every database row a phantom, and phantom
  handling force-closes state and releases capital. An outage must not be able
  to trigger that.
- **Settlement lag must not raise a false orphan.** A position closed internally
  is still briefly visible at the broker.

Output: [`evidence/reconciliation-divergence-table.txt`](evidence/reconciliation-divergence-table.txt).

## Property-based invariants

Example tests prove a function works for cases you thought of. These assert
properties that must hold for *every* input, including the degenerate series
nobody writes by hand.

| # | Invariant |
|---|---|
| P1 | VWAP always lies within `[min(low), max(high)]` |
| P2 | The divergence gate always returns a bool and never raises |
| P3 | VAH ≥ POC ≥ VAL, always |
| P4 | The value area lies within the traded range |
| P5 | VWAP of an all-equal series equals that price |
| P6 | Appending a zero-volume bar does not move VWAP |
| P7 | Read-only feature calls are deterministic **and do not mutate their input** |

P6 and P7 are the ones that catch real bugs. P7 in particular: silent in-place
mutation of an input DataFrame is a classic pandas trap, and here the same frame
is passed to several gates in sequence, so a mutation in one changes the answer
of the next.

Output: [`evidence/property-tests-hypothesis.txt`](evidence/property-tests-hypothesis.txt).

## Brain isolation

The README claims trading intelligence is sealed inside `strategy/`. That is an
architectural claim, and architectural claims decay silently — one convenient
import during a late-night debugging session and it is quietly false.

`tests/unit/test_brain_isolation.py` parses the AST of every module under
`strategy/` and asserts it imports nothing from the runtime layer, no I/O
library, and never opens a file. A final test guards the allowlist itself, so
widening it to make a failure disappear requires a deliberate edit.

Output: [`evidence/brain-isolation-test.txt`](evidence/brain-isolation-test.txt).

## Structural guarantees

**No network from a unit test.** Enforced by an autouse fixture that replaces
`socket.connect` with a raising guard. Integration tests opt out via the
`integration` marker. This makes "no network in unit tests" true rather than
intended, and would catch a test accidentally reaching a live broker.

**No real credentials.** A second autouse fixture clears broker and database
environment variables, so a test cannot pass on the author's machine using live
credentials and fail everywhere else.

## Coverage

Reported honestly rather than padded. Current figures:
[`evidence/coverage-by-package.txt`](evidence/coverage-by-package.txt).

```
market_utils.py            100%
rest_limiter.py             91%
strategy/features.py        89%
symbols.py                  78%
capital_manager.py          74%
strategy/market_profile.py  43%
strategy/back_to_vwap.py     0%
strategy/htf_confluence.py   0%
strategy/market_context.py   0%
```

Coverage is concentrated in the strategy math and the capital layer. Three
strategy modules are not yet covered and the gap is recorded in
[`KNOWN_GAPS.md`](KNOWN_GAPS.md) rather than hidden behind a flattering global
average.

`market_profile.py` sits at 43% because the non-Dalton TPO path is only reached
when `P65_AMT_ENABLED` is false, which is not the shipped configuration. The
covered path is the one that runs.

## What is deliberately not tested

Declaring non-goals is part of the discipline.

- **Live broker connectivity** — Fyers' correctness, not ours.
- **WebSocket transport** — the library's. The *parsing* of websocket payloads
  is covered separately by a replay harness that feeds real recorded frames
  through the handlers.
- **Telegram delivery** — network-dependent, outside the trading path.
- **Anything requiring live market data.**
- **The strategy's profitability.** A test suite cannot validate an edge.
  Claiming otherwise would be dishonest. See [`DISCLOSURE.md`](DISCLOSURE.md).

## Fixtures

Fixtures are synthetic, constructed in `tests/conftest.py`, and deterministic —
no `random`, no `datetime.now()`, no dependence on machine timezone. Balances
are obviously-fake round numbers.

No fixture derives from a real broker response, so there is no account
identifier, order id, or balance to redact. `gitleaks` runs over `tests/` in CI
regardless.
