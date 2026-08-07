# Operations runbook

What to do when things fail. Procedures are derived from failures that have
actually occurred — each cross-references the incident in
[DISCOVERIES.md](DISCOVERIES.md) where one exists.

## Daily shape

| Time (IST) | What happens |
|---|---|
| before 09:15 | Process starts, authenticates, primes the quote cache, applies migrations if needed |
| 09:15 | Market opens. `TRADING_ENABLED` still false — warmup only |
| 09:30 | `MarketSession` opens the trading gate. Scanning and execution begin |
| 09:30–15:10 | Scan every 60s; reconciliation every 6s; capital resync every 5min |
| 15:10 | Forced square-off, retried until the account is provably flat or 15:25 |
| 15:32 | EOD analysis, gate audit flush, then graceful shutdown |
| 15:35 / 15:40 | Watchdog soft shutdown / hard kill, if the scheduler did not fire |

The process exits daily. It is not a long-running service.

---

## The websocket drops mid-position

**Symptom.** `Data WebSocket error` / `Connection to remote host was lost`, or a
`SEVERE DEGRADED` cache health line.

**What the system does on its own.** The data socket has a health monitor that
classifies freshness every 30s and escalates: reprime → full reconnect → hybrid
REST mode. The order socket has a keepalive that rebuilds it after 180s of
silence. Your broker-side stop is unaffected by any of this — it lives at the
exchange ([ADR-003](DECISIONS.md)).

**What you do.**
1. `/health` in Telegram. Look at `fresh_pct` and the two socket states.
2. If the data socket recovers, nothing further is needed — the cache reprimes.
3. If it enters `HYBRID_REST_MODE`, the bot keeps trading on REST data with
   degraded signal quality. Acceptable for an open position; consider `/auto off`
   before taking a new one.
4. If the **order** socket is down, fill confirmation falls back to REST polling.
   Entries still work but are slower. This is the condition that used to be
   permanent and invisible ([D-001](DISCOVERIES.md)).

**Do not restart with a position open** unless you have to — see below.

---

## A margin rejection on entry

**Symptom.** `Margin Shortfall` in the log, followed by
`Attempting 4.0x fallback`.

**What the system does.** Recomputes size at 4x and retries once. If that also
yields zero quantity, it aborts and sets a cooldown.

**What you do.** Usually nothing — the fallback is the designed behaviour. If it
happens repeatedly, the account's available margin has drifted from what the bot
believes; `/status` shows the live figure. A stale margin value usually means
`get_funds` has been timing out, which appears in `/health` as
`capital_sync_timeouts`.

---

## Reconciliation reports an orphan

**Symptom.** `🚨 ORPHAN DETECTED` and a `MANUAL ENTRY ADOPTED` Telegram alert.

**Meaning.** The broker has a position the bot was not tracking — either you
opened it manually, or a fill arrived that the internal loop missed.

**What the system does.** Adopts it: places an emergency 1% stop, registers it
internally, writes it to the database, and acquires the capital slot. Adoption is
idempotent, so repeated cycles will not stack stops.

**What you do.**
1. Confirm in the Fyers app that the emergency stop exists and is on the correct
   side.
2. Note that the emergency stop is **1%**, which is usually tighter than the
   strategy's ATR-based stop. If you want the original level, set it manually —
   the bot will detect the change and back off (`manual_override`).
3. If the alert says `ORPHAN SL FAILED`, the position is **naked**. Close it or
   place a stop manually, immediately.

---

## Reconciliation reports a phantom

**Symptom.** `👻 MANUAL CLOSE DETECTED`.

**Meaning.** The bot's registry says a position is open; the broker says flat.
Normally because you closed it in the app.

**What the system does.** Runs the full close path — releases the capital slot,
marks the database row closed, cancels leftover orders.

**What you do.** Usually nothing. If you did *not* close it, investigate: a
phantom you did not cause means a fill was missed in the other direction.

**Important.** A phantom is only acted on when the broker view is trustworthy. If
the position fetch failed, the cycle is skipped rather than treating an empty
result as "flat" — an API outage must not be able to force-close live state.

---

## The process dies with a position open

**This is the scenario the architecture is built for.**

Your stop is at the exchange, not in the process. It survives the process dying,
the host rebooting, and the network dropping.

**On restart:**
1. `StartupRecovery` queries positions before trading begins.
2. Any open position is adopted with an emergency stop if one is missing.
3. The capital slot is locked so the bot does not enter a second position.
4. You get a `STARTUP ORPHAN ADOPTED` alert.

**What you do.** Read the alert. Verify in the Fyers app that exactly one stop
exists for the position — if the original stop survived *and* recovery added
one, cancel the duplicate.

---

## The square-off does not complete

**Symptom.** `🚨 EOD SQUARE-OFF FAILED` naming open symbols, or
`EOD SQUARE-OFF INCOMPLETE`.

**What the system does.** Retries every 10 seconds from 15:10 until the account
is provably flat or 15:25, verifying against the broker after each attempt. It
will not report success it cannot prove — a lesson from a session where the
operator received "TIMED OUT" and "complete" one second apart.

**What you do.** If you see the failure alert, **close the position manually
before 15:30.** Anything left open is auto-squared by the broker at a price you
do not control.

---

## Telegram goes unresponsive

**Symptom.** No alerts; `/status` does not answer.

**Important:** the bot keeps trading. Telegram is the observability surface, not
a control dependency. An open position is still managed and the square-off still
fires.

**What you do.**
1. Check the process is alive: `ps aux | grep main.py`.
2. Check the session log directly — it has everything Telegram would have shown.
3. If the token or network is the problem, alerts are queued and retried; a
   parse failure now degrades to plain text rather than dropping the message.
4. To stop trading without Telegram, stop the process. The broker-side stop
   remains in force.

---

## You need to stop it right now

| Situation | Action |
|---|---|
| Stop taking *new* trades, keep managing the open one | `/auto off` |
| Stop everything, position is flat | `/stop`, or SIGTERM |
| Stop everything, position is open | Close the position in the Fyers app **first**, then stop the process |

Never kill the process as a way of closing a position. Killing it leaves the
position open with only the broker-side stop protecting it, and nothing watching.

---

## Reading a session log

```bash
grep -E "VALIDATED|EXECUTING|ENTRY COMPLETE|EXIT|ORPHAN|PHANTOM" logs/$(date +%F)_session.log
grep -E " - (ERROR|CRITICAL) - " logs/$(date +%F)_session.log
grep -c "SCAN #" logs/$(date +%F)_session.log          # scan cycles
tail -40 logs/rejections_$(date +%Y%m%d).log           # why signals were rejected
```

A healthy session shows scan cycles roughly every 60s, cache health above 90%
fresh, reconciliation under 500ms, and rejections dominated by `G5_STRATEGY` —
that last one is the strategy being selective, not a fault.

---

## Health checks worth running

```bash
make test                    # 143 tests, no credentials needed
make security                # gitleaks over full history, pip-audit, bandit
python -c "import main"      # every module imports
```

Before any live session after a code change: run `make verify`, and watch the
first entry closely — specifically that the stop lands on the correct side of the
fill, and that no phantom close fires in the first few seconds.
