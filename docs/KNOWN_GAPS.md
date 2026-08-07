# Known gaps

Written to be accurate rather than flattering. Anything claimed as done in the
README traces to a file in [`evidence/`](evidence/); anything not done is here.

## Test coverage

Measured 2026-08-07 — see [`evidence/coverage-by-package.txt`](evidence/coverage-by-package.txt).

| Module | Coverage | Status |
|---|---|---|
| `market_utils.py` | 100% | Complete |
| `rest_limiter.py` | 91% | Complete |
| `strategy/features.py` | 89% | Meets the 85% strategy floor |
| `symbols.py` | 78% | Adequate |
| `capital_manager.py` | 74% | Below the 80% floor — the async sync path is untested |
| `strategy/market_profile.py` | 43% | Below floor — only the Dalton path is covered |
| `strategy/back_to_vwap.py` | 0% | **Not yet tested** |
| `strategy/htf_confluence.py` | 0% | **Not yet tested** |
| `strategy/market_context.py` | 0% | **Not yet tested** |

The three untested strategy modules are the most valuable remaining test work.
`back_to_vwap.py` is pure and needs no mocking; `htf_confluence.py` and
`market_context.py` take an injected broker client and need only a stub.

`market_profile.py` sits at 43% because `calculate_market_profile()` — the
non-Dalton TPO path — is only reached when `P65_AMT_ENABLED` is false, which is
not the shipped configuration. The covered path is the one that runs.

## Not tested, deliberately

Declared rather than omitted:

- **Live broker connectivity.** That is Fyers' correctness, not ours.
- **WebSocket transport.** The library's responsibility. The *parsing* of
  websocket payloads is covered separately by a replay harness that feeds real
  recorded frames through the handlers.
- **Telegram delivery.** Network-dependent and outside the trading path.
- **Anything requiring live market data.**
- **The strategy's profitability.** A test suite cannot validate an edge.
  Claiming otherwise would be dishonest. See [`DISCLOSURE.md`](DISCLOSURE.md).

## Modules that are hard to test as written

Not defects — observations, recorded so the reason is visible. Per the PRD's
prime directive, none of these were refactored to make testing easier; this
repository places real orders and a convenience refactor is not worth the risk.

| Module | Why it resists unit testing |
|---|---|
| `order_manager.py` | Order placement, fill waiting, and state mutation are interleaved in `enter_position`; testing a branch requires stubbing the broker, capital manager, Telegram and the database together |
| `focus_engine.py` | The monitor is a 5 Hz thread reading wall-clock time and broker state; deterministic testing needs a clock seam that does not currently exist |
| `fyers_broker_interface.py` | Constructor performs I/O (creates log directories, builds SDK clients), so the class cannot be instantiated in a unit test without patching |
| `main.py` | Orchestration only — meaningful coverage would be an integration test |

## Infrastructure not yet built

Sessions 3–6 of the PRD remain outstanding:

- `pyproject.toml` replacing `pytest.ini`
- GitHub Actions: CI, security, release workflows
- `deploy/Dockerfile`, `docker-compose.yml`, `docker-compose.test.yml`
- `Makefile`
- `docs/`: `STRATEGY.md`, `OPERATIONS.md`, `TESTING.md`, `DATA_MODEL.md`,
  `DECISIONS.md`, `DISCOVERIES.md`
- README restructure
- The `src/shortcircuit/` package layout (PRD Tier C)

`pytest.ini` is still present and now has tests beside it, so it is no longer a
negative signal — but it should be folded into `pyproject.toml`.

## Open defect

`state/database.py` ships `"password": "password"` as a connection default. If
`DB_PASS` is unset the process silently attempts that value instead of failing
loudly. Not changed here because it falls under the prime directive; worth
fixing in a dedicated commit with a test.

## Security

One finding from the full-history credential scan remains open. See
[`SECURITY.md`](SECURITY.md#current-findings).
