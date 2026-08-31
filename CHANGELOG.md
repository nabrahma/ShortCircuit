# Changelog

Notable changes to this project. Format based on [Keep a Changelog](https://keepachangelog.com/1.1.0/);
this project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Scanner-level MIS leverage screen. Fyers grants either 4-5x or exactly 1.0x
  (which means intraday margin is refused), so symbols below the floor are
  dropped before any history is fetched. The screen fails open: an unknown
  reading never blocks, and the broker rejection remains the backstop.
- `ADR-011`, restoring the take-profit as a scale-out and superseding `ADR-009`.

### Changed

- **Exit policy.** `TP_MODE` now selects between `'SCALE'` (default: half at the
  midpoint, remainder to the VWAP target, stop to breakeven once the partial
  fills), `'SINGLE'` and `'OFF'`. `ADR-009` had removed the take-profit entirely;
  a replay of every live trade against real 1-minute candles reversed that
  reading. `'OFF'` restores the previous behaviour exactly.
- The 45-minute time-based exit is re-enabled (`MAX_HOLD_TIME_MINUTES`).
- `VWAP_ANCHOR_MODE` makes the VWAP anchor explicit rather than an accident of
  which data tier answered. Previously a local-buffer read produced a rolling
  anchor and the REST fallback a session anchor, so C1 measured different things
  depending on the source.
- Scanner volume floor lowered to 111,111; low-float movers were clearing the
  old floor around 26 minutes after their move.
- `make test` and CI now include `tests/strategy/`. Those 93 tests existed but
  were never run by either — the documented command silently skipped the Brain's
  own test suite.
- Comments across the codebase reduced to what a reader needs at the point of
  reading; the reasoning behind each decision lives in the commit history.

### Fixed

- `.env.example` shipped `DB_PASSWORD` and `FYERS_SECRET_KEY`, but
  `deploy/docker-compose.yml` hard-requires `DB_PASS` and the code's canonical
  name is `FYERS_SECRET_ID`. Following the example file broke `docker compose up`.
- `.env.example` documented `AUTO_MODE`, which is not read from the environment
  at all. Setting it to `false` gave a false sense that the bot was disarmed.
- README described an exit policy the bot no longer had, and reported 155 tests
  when there were 344.
- `docs/OPERATIONS.md` claimed migrations are applied at startup. Nothing applies
  them; the README now documents the one-time command.
- `.coverage` was committed as a tracked binary artefact.

## [1.0.0] — 2026-08-07

First tagged release. The system had been operating for months before this tag;
`1.0.0` marks the point at which its engineering became verifiable rather than
merely asserted.

### Added

- Test suite: 143 unit and property-based tests, none of which existed before.
  Includes the reconciliation divergence table (seven categories plus idempotent
  adoption), property-based invariants over generated OHLCV series, and a
  structural test enforcing that `strategy/` imports nothing from the runtime
  layer.
- Continuous integration: lint, tests across Python 3.11 and 3.12, integration
  against a real PostgreSQL service, and a container build that asserts no
  `.env` is baked into the image.
- Security workflow scanning the full git history for credentials, plus
  dependency audit, static analysis and container scanning, weekly and on push.
- Containerised test run requiring no credentials
  (`deploy/docker-compose.test.yml`).
- Documentation: architecture, strategy, operations runbook, testing, data
  model, decisions, discoveries, known gaps, security and disclosure.
- `docs/evidence/` — every measurable claim traces to captured output.

### Changed

- Order fill confirmation now resolves from the websocket rather than always
  falling back to a REST poll after a 15-second timeout. See
  [D-001](docs/DISCOVERIES.md).
- Stop-loss placement is validated against the actual fill price, so a stop
  cannot land on the wrong side of the entry and fire on arrival.
  See [D-006](docs/DISCOVERIES.md).
- Rate limiting enforces all three broker windows simultaneously (per-second,
  per-minute, per-day) with a reserved lane for order-path calls.
  See [D-004](docs/DISCOVERIES.md).
- Exit policy: the take-profit was removed and the time-based exit disabled.
  Positions now run to the stop-loss or the end-of-day square-off.
  See [ADR-009](docs/DECISIONS.md).
- `pytest.ini` superseded by `pyproject.toml`.

### Fixed

- Websocket order, position and trade payloads were read from the wrong level of
  the message envelope, so no fill was ever confirmed over the socket.
- The position cache had no writer, so every consumer silently fell through to
  REST polling.
- A position-cache miss was treated as evidence the position was flat, producing
  a phantom close 23ms after entry. See [D-007](docs/DISCOVERIES.md).
- Async timeouts were shorter than the transport timeouts they wrapped, so every
  one abandoned an in-flight request. See [D-005](docs/DISCOVERIES.md).
- Connection-pool and timeout configuration was applied to an attribute that
  does not exist, and had never taken effect. See [D-008](docs/DISCOVERIES.md).
- End-of-day square-off reported success it could not prove; it now verifies
  against the broker and retries until flat or a hard deadline.
- Telegram alerts composed in Markdown were sent as HTML, so formatting rendered
  literally and any `<` in a broker error caused Telegram to reject a bundle of
  up to five alerts.
- Four strategy features declared in the ML schema were never written, leaving
  them null in every historical observation.
