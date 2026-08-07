# Contributing

This repository operates a live trading system against a real brokerage account.
That single fact drives every rule below.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
make install
pre-commit install
make verify        # lint + tests + coverage
```

## The one rule that matters

**Any change to trading behaviour requires a test.**

Trading behaviour means: order placement or cancellation, risk and position
sizing, stop-loss calculation, state transitions, gate evaluation, capital slot
handling, or reconciliation classification.

A bug in this code does not raise an exception in CI. It loses money during a
live session, days later, in a way that looks like bad luck. A test is the only
mechanism that distinguishes the two.

If a module is hard to test as written, **do not refactor it to make testing
easier as part of an unrelated change.** Record the difficulty in
`docs/KNOWN_GAPS.md` and test what you can.

## Never publish a performance figure

Do not add, in code, comments, documentation, screenshots or captured output:

- Win rate, hit rate, strike rate
- Profit and loss in any currency or percentage
- Returns, CAGR, Sharpe, Sortino, drawdown
- Average win/loss, realised risk-reward
- Equity curves or cumulative-return charts
- Backtest results of any kind
- Any statement implying the strategy is profitable, or that a reader should
  use it

This is not stylistic. See [`docs/DISCLOSURE.md`](docs/DISCLOSURE.md).

Engineering measurements are welcome and encouraged: latency, throughput, cache
priming time, recovery time, gate evaluation volumes, coverage, CI duration.

Every number that appears in the README must trace to a file in
`docs/evidence/`. One fabricated figure, once noticed, discredits every other
claim in the repository — correctly.

## Never commit a credential

`.env` is git-ignored and must stay that way. No account identifier, access
token, client id or real balance in code, tests, fixtures or captured output.

`gitleaks` runs pre-commit and in CI over the **full history**, because a
deleted credential still lives there.

If you find a leaked credential: rotate it first, then open an issue. Do not
open an issue naming the secret.

## Commits

Conventional prefixes: `feat:`, `fix:`, `docs:`, `test:`, `ci:`, `refactor:`,
`chore:`. Explain *why* in the body, not just what — the diff already says what.

Do not rewrite history. The commit log is a record of how this system was
actually built.

## Typing

`mypy` runs advisory only. Typing is incremental: new code should carry hints,
existing modules are converted opportunistically, and a missing hint never
blocks a merge.

## Pull requests

Fill in the template, particularly the "does this touch trading logic" box.
`make verify` must pass. CI runs lint, tests on Python 3.11 and 3.12,
integration against PostgreSQL, a container build, and the security suite.
