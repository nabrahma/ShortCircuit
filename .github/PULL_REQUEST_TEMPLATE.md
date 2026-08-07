## What this changes

## Why

## Does this touch trading logic?
- [ ] No — documentation, tests, CI, or tooling only
- [ ] Yes — order placement, risk, state transitions, or gate evaluation

If yes, confirm:
- [ ] A test covers the new behaviour
- [ ] The change was reasoned about against a live session log, not only in theory

## Checklist
- [ ] `make verify` passes locally
- [ ] No performance figures added (no win rate, P&L, returns, Sharpe, drawdown,
      backtest results) — see docs/DISCLOSURE.md
- [ ] No credential, account identifier or real balance in code, tests, fixtures
      or captured output
