# Changelog

Notable changes to this project. Format based on [Keep a Changelog](https://keepachangelog.com/1.1.0/);
this project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `ExitProfile.STAGED_RSI` with `StagedRsiExit`: an RSI-timed partial exit with
  the stop moved to cost, then a trailing stop on the remainder above each
  completed bar's high. The trail only tightens, and a level price has already
  crossed closes the position instead of placing a stop that is marketable on
  arrival.
- `ExitPlan.stop_pct`: a hard stop at a fixed percentage from the actual fill.
- **Data-only mode** (`SHORTCIRCUIT_MODE=data_only`): the full pipeline on live
  data with no ability to trade. The transport refuses every order write before
  it is sent; entry, adoption and square-off log what they would have done.
- **Backtester** (`python -m shortcircuit.backtest`): finds 10%+ gainer days,
  caches their 1-minute history and replays them through the live analyzer's
  feature code (`derive_features`, now shared) and the engine's exit rules.

### Fixed

- **F&O positions were squared off by the broker, not the bot.** Since 3 Aug
  2026 Fyers closes MIS in closing-auction (F&O) stocks at 15:05, before the
  engine's 15:10. These now close at 15:02 and take no new entries after it,
  decided per stock from Fyers' own F&O list; everything else keeps 15:10.
- The focus loop's 15:10 check read the machine clock, so in a UTC container it
  never fired. It now reads IST.
- A failed stop resize after a partial with no TP-1 level would have raised
  while building its alert, on the path that flattens an oversized stop.

## [2.0.0] — 2026-11-22

ShortCircuit is now a strategy-agnostic execution engine for Fyers. A strategy
is a plug-in that answers one question (is there a setup, and how should it be
traded?) and lives outside the repository. The engine owns everything that
touches the broker: the scan universe, market data, risk gates, order
placement, stops, exits, square-off and reconciliation. Every Fyers request is
now rate-metered at the HTTP layer.

### Upgrading from 1.0.0

- **No strategy is active by default.** Name one in `SHORTCIRCUIT_STRATEGIES`
  or in `ACTIVE` in `strategies/__init__.py`. With neither, the engine scans and
  manages open positions but opens none, and logs a warning saying so. To see
  the pipeline end to end, try
  `SHORTCIRCUIT_STRATEGIES=example_vwap_stretch_fade`.
- **Strategy settings left `config.py`.** `STRATEGY_*`, `TOPCOIL_*`,
  `BACKTOVWAP_ENABLED` and the RSI-cover settings now belong to the plug-in that
  uses them. A strategy is written against `shortcircuit.strategy.sdk`; see
  [docs/STRATEGIES.md](docs/STRATEGIES.md).
- **Re-run `make install`.** It now installs the package in editable mode, so
  `python -m shortcircuit` works from a checkout. `python main.py` still works.
- **Docker:** mount your strategies at `/app/strategies`, as
  `deploy/docker-compose.yml` does. Images never contain a strategy.
- ML observation files gain two columns, `strategy` and `exit_params`.

### Added

- **Strategy plug-ins** (`ADR-012`). `strategy/sdk.py` defines the contract,
  `strategy/registry.py` discovers and loads plug-ins from the git-ignored
  `strategies/` directory (or `SHORTCIRCUIT_STRATEGY_PATH`), and
  `strategy/examples/` ships a working example. Loading fails fast, before
  login, on an unknown or duplicate name or a strategy the engine cannot
  execute. A strategy that raises is treated as no setup, so it cannot stop
  the others. Guide: [docs/STRATEGIES.md](docs/STRATEGIES.md).
- **Metered transport** (`ADR-013`). Every Fyers SDK request is charged to the
  rate limiter inside the HTTP layer, at a priority derived from its endpoint,
  and a broker 429 pauses background traffic for a cooldown. Order-path calls
  are never paused.
- Scanner-level MIS leverage screen. Fyers grants either 4-5x or exactly 1.0x
  (which means intraday margin is refused), so symbols below the floor are
  dropped before any history is fetched. The screen fails open: an unknown
  reading never blocks, and the broker rejection remains the backstop.
- `ADR-011`, restoring the take-profit as a scale-out and superseding `ADR-009`.
- A release is cut from its `CHANGELOG.md` section, and the workflow refuses a
  tag that does not match the package version.

### Changed

- **The analyzer is strategy-agnostic.** The hard-wired detectors, their
  enable switches and their thresholds left the engine for private plug-ins.
  A golden-master comparison verified the move changes no behaviour.
- The RSI relief-rally cover reads its parameters from the position's exit plan
  rather than global config, in the live loop and in the end-of-day audit.
- **Exit policy.** `TP_MODE` selects between `'SCALE'` (default: half at the
  midpoint, remainder to the VWAP target, stop to breakeven once the partial
  fills), `'SINGLE'` and `'OFF'`. `ADR-009` had removed the take-profit
  entirely; a replay of every live trade against real 1-minute candles reversed
  that reading. `'OFF'` restores the previous behaviour exactly.
- The 45-minute time-based exit is re-enabled (`MAX_HOLD_TIME_MINUTES`).
- `VWAP_ANCHOR_MODE` makes the VWAP anchor explicit rather than an accident of
  which data tier answered. Previously a local-buffer read produced a rolling
  anchor and the REST fallback a session anchor.
- Scanner volume floor lowered to 111,111; low-float movers were clearing the
  old floor around 26 minutes after their move.
- Test safety guards (no network, no real credentials) moved to a root
  `conftest.py`, so they cover plug-in tests under `strategies/` as well as
  the engine suite.
- `make test` and CI now include `tests/strategy/`. Those 93 tests existed but
  were never run by either.
- The test suite runs on Windows.
- Comments across the codebase reduced to what a reader needs at the point of
  reading; the reasoning behind each decision lives in the commit history.

### Fixed

- **The Docker image could not start.** Nothing installed the package or put
  `src/` on the path, so `python -m shortcircuit` and the healthcheck both
  failed with `No module named 'shortcircuit'`. The image now sets
  `PYTHONPATH=/app/src`.
- **Plug-in tests did not work on a fresh clone.** `import shortcircuit` and
  `from strategies.x import Y` both failed, and the network and credential
  guards did not apply to them.
- The release workflow would have failed on its first run: the image name came
  from the mixed-case repository name, and image references must be lower case.
- **Broker lockouts from the hard-stop monitor.** It fetched the full orderbook
  on every 5 Hz focus tick, unmetered — ~280 requests/min against a 200/min
  limit — which throttled the account after entries and abandoned exits on
  5, 7 and 13 Oct. Now single-flight at a 2s minimum interval.
- About twenty REST call sites that bypassed the rate limiter, including the
  EOD square-off loop, which sent cancels and exits back-to-back.
- A short setup could be executed as a buy after `/mode buy`: the analyzer never
  checked direction. Strategies of the other direction are now skipped.
- `test_brain_isolation` passed vacuously under the src/ layout; it now checks
  every component of an import path, and scans plug-in modules too.
- Windows console logging failed on emoji under a legacy code page.
- `.env.example` shipped `DB_PASSWORD` and `FYERS_SECRET_KEY`, but
  `deploy/docker-compose.yml` requires `DB_PASS` and the code's canonical name
  is `FYERS_SECRET_ID`. Following the example file broke `docker compose up`.
- `.env.example` documented `AUTO_MODE`, which is not read from the environment
  at all. Setting it to `false` gave a false sense that the bot was disarmed.
- README described an exit policy the bot no longer had, and understated the
  test count.
- `docs/OPERATIONS.md` claimed migrations are applied at startup. Nothing applies
  them; the README now documents the one-time command.
- `.coverage` was committed as a tracked binary artefact.

### Security

- Container image: `zlib1g` upgraded for CVE-2026-85091.
- Eleven new `aiohttp` advisories are suppressed, with the reason recorded.
  `fyers_apiv3` pins `aiohttp==3.9.3` in every release through 3.3.0, so the
  fix cannot be installed. None is reachable, because no aiohttp code runs:
  the SDK uses it only in async mode, and every client here is synchronous.
  `tests/unit/test_dependency_reachability.py` fails the build if that stops
  being true. See [docs/SECURITY.md](docs/SECURITY.md).
- Bandit B324: the registry's SHA-1 builds a module name, not a security
  boundary, and is now declared `usedforsecurity=False`.

## [1.0.0] — 2026-08-07

First versioned release (never tagged). The system had been operating for months before it;
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

[Unreleased]: https://github.com/nabrahma/ShortCircuit/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/nabrahma/ShortCircuit/releases/tag/v2.0.0
