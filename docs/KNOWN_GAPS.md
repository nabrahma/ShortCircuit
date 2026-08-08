# Known gaps

Written to be accurate rather than flattering. Anything claimed as done in the
README traces to a file in [`evidence/`](evidence/); anything not done is here.

## Test coverage

Measured 2026-08-08, see [`evidence/coverage-by-package.txt`](evidence/coverage-by-package.txt).

| Module | Coverage | Status |
|---|---|---|
| `market_utils.py` | 100% | Complete |
| `strategy/back_to_vwap.py` | 100% | Complete, statements and branches |
| `strategy/htf_confluence.py` | 100% | Complete, statements and branches |
| `strategy/market_context.py` | 97% | Complete |
| `rest_limiter.py` | 91% | Complete |
| `strategy/features.py` | 86% | Meets the 85% strategy floor |
| `symbols.py` | 78% | Adequate |
| `capital_manager.py` | 74% | Below the 80% floor, the async sync path is untested |
| `strategy/market_profile.py` | 40% | Below floor, only the Dalton path is covered |

**`strategy/` package total: 86%**, which meets the PRD floor.

The three previously untested strategy modules are now covered. `back_to_vwap.py`
is tested as a rejection table: one golden input passes all six gates, and every
other case perturbs a single variable and asserts rejection, so a gate that stops
rejecting fails exactly one named row.

Two findings came out of writing them, both recorded rather than changed, because
fixing either means editing `strategy/`:

- **G9 fails open twice.** `htf_confluence.check_trend_exhaustion` returns *allow*
  when the frame has no recognisable close column, and again when a candle holds a
  zero price. Everywhere else in the system, unusable data blocks. Both paths are
  pinned by tests named `..._is_fail_open` so the behaviour is visible.
- **Confidence tiers are genuinely inert.** Asserted directly: a signal at the
  lowest tier still passes, and no tier value changes whether a signal is produced.

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

## Infrastructure

Built. `pyproject.toml`, the CI, security and release workflows, `deploy/`,
the `Makefile`, the full `docs/` set and the `src/shortcircuit/` layout are all
in place.

## Open defect

`state/database.py` ships `"password": "password"` as a connection default. If
`DB_PASS` is unset the process silently attempts that value instead of failing
loudly. Not changed here because it falls under the prime directive; worth
fixing in a dedicated commit with a test.

## Security

One finding from the full-history credential scan remains open. See
[`SECURITY.md`](SECURITY.md#current-findings).
