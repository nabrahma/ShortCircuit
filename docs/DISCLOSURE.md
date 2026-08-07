# Disclosure

**Not investment advice. No performance claims.**

This repository is published as software. It documents the engineering of a
personal execution system. It is not a recommendation to trade any instrument,
to use any strategy, or to run this code.

---

## No investment advice

Nothing in this repository — code, documentation, logs, or commit messages —
constitutes investment advice, a recommendation, or a solicitation to buy or
sell any security. The trading logic reflects one person's opinion about market
structure. It is published to show how the system is built, not to suggest
anyone should trade this way.

## No performance claims

This repository deliberately publishes **no** performance, profitability, or
return figures. Specifically, you will not find: win rates, hit rates, profit
and loss in any currency or percentage, returns, CAGR, Sharpe or Sortino ratios,
drawdown, average win/loss, realised risk-reward, equity curves, or backtest
results of any kind.

None should be inferred from any part of this repository.

Publishing such figures would constitute a performance claim, which is out of
scope for this project and would require registrations the author does not hold.

The measurements that **are** published are engineering metrics — latency,
throughput, cache priming time, recovery time, gate evaluation volumes, test
counts, coverage. These describe how the system behaves, not what it earns.

## Not a service

Nothing here is offered as an advisory or research service. The author is not a
registered Investment Adviser or Research Analyst under SEBI regulations, and
does not manage money for anyone else.

## Personal use

This system was built and is operated by the author, for the author's own
account, at the author's own risk.

## No warranty

Licensed under Apache-2.0. The software is provided "as is", without warranty of
any kind, express or implied. Anyone who runs it does so entirely at their own
risk and is solely responsible for any resulting losses.

## Broker terms and regulation

Anyone using this code is responsible for their own compliance with their
broker's API terms of service and with all applicable law and regulation in
their jurisdiction. Automated order placement may be subject to restrictions
that vary by broker and by market.

## Educational purpose

This repository is published to document engineering practice in real-time
execution systems: websocket data handling with freshness tracking, state
reconciliation against an external system of record, concurrency control over a
shared capital resource, and failure handling in software where a failed
operation cannot simply be retried.

Those are the transferable parts. The trading strategy is the domain that
motivated them.
