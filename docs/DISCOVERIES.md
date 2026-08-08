# Discoveries

Problems found while building and operating this system, with what was wrong,
why it mattered, and how it was fixed.

Every entry here actually happened. Each cites the evidence it was diagnosed
from — a session log, a commit, or a test. Nothing is reconstructed from memory
or inferred from how the code looks now.

---

## D-001 — The order websocket had never once confirmed a fill

**Found:** auditing the order pipeline against session logs, 2026-07-29.

**What happened.** The Fyers order socket delivers its payload wrapped in an
envelope — `{'s': 'ok', 'orders': {...}}` — but all three handlers read fields
off the envelope's top level, so `message.get('id')` was always `None`. Every
order update was discarded before it reached a waiter.

The logs are unambiguous:

```
Order update with no ID          48
Order <id>: FILLED                0     ← never once
Entry fill timeouts              42
"WS drop recovered" REST rescues 42     ← a perfect 1:1
```

Forty-two timeouts, forty-two REST rescues. The emergency fallback was carrying
the entire system.

**Why it mattered.** `enter_position` waits for the fill before placing the
stop-loss. With the socket mute, every entry waited the full 15-second timeout,
was declared failed, then rescued by a REST orderbook check — meaning **every
filled position sat at the broker with no stop for roughly fifteen seconds.**
That is the window the design exists to eliminate.

**Second defect in the same path.** Fyers status `4` is TRANSIT, not a partial
fill — and it is the most common frame in these logs (353 occurrences). The
status map labelled it `PARTIAL`, and `wait_for_fill` returned on the *first*
event received. So even with the envelope fixed, the wait would have resolved on
an order that was not yet live at the exchange.

**Fix.** Unwrap the envelope; correct the status enum; loop until a genuinely
terminal state (`FILLED` / `CANCELLED` / `REJECTED`) with a periodic REST
reconcile so a dropped frame resolves in seconds rather than burning the budget.
Trade events correlate on `orderNumber`, not `id` — the SDK's own field mapper
renames it, and correlating on `id` silently never matches.

**Evidence.** Verified by replaying real recorded websocket frames from
`logs/` through the rewritten handlers: **89 distinct order ids extracted where
the old code extracted 0.**

---

## D-002 — A websocket reconnect does not imply the cache is fresh

**Found:** the reason the cache lifecycle exists at all.

**What happened.** After a network blip the connection re-established and the
client reported healthy, but no tick had arrived for the affected symbols.
Cached quotes from before the drop were being treated as live.

**Why it mattered.** Every gate downstream — VWAP stretch, volume fade, profile
rejection — was evaluating minutes-old prices with no indication anything was
wrong. A signal generated from stale data is worse than no signal, because it is
indistinguishable from a real one.

**Fix.** Readiness derives from per-symbol tick recency, not connection state.
REST seeding populates the cache but explicitly does **not** mark it fresh — the
system separates "known" from "fresh" and treats that distinction as a safety
property. The lifecycle became
`UNINITIALIZED → PRIMING → READY → DEGRADED → REPRIME`.

---

## D-003 — Position state was read from a cache nothing ever wrote

**Found:** during the same 2026-07-29 audit.

**What happened.** `self.position_cache` was read in two places and written in
none. The websocket position handler wrote to a different dict
(`self._position_cache`) which nothing read — and which, per D-001, was never
populated anyway because of the same envelope bug.

**Why it mattered.** `get_all_positions()` silently fell through to REST on
every single call, including the reconciliation cycle that runs **every six
seconds** during market hours. The "zero-cost cache fast path" described in the
architecture never executed once.

**Fix.** Wire the handler to the cache that consumers actually read, evict
symbols when they go flat so stale non-zero state cannot be re-read, and
normalise position records to one shape — Fyers returns both `qty` (absolute)
and `netQty` (signed), and different call sites were reading different keys.

---

## D-004 — The rate limiter enforced one window out of three

**Found:** 678 rate-limit responses across accumulated session logs.

**What happened.** Fyers enforces three simultaneous limits: 10/second,
200/minute, 100,000/day. The limiter implemented a single token bucket at 8
requests per second. Sustained, that is 480 requests per minute — **2.4× over
the per-minute cap.**

**Why it mattered.** Beyond the throttling itself, there was no priority: a
stop-loss placement queued behind however many scanner quote calls happened to
arrive first.

**Fix.** All three windows enforced simultaneously as sliding logs rather than
fixed-window counters, so a burst at t=0.9s cannot double-spend into the next
second. Order-path calls acquire at HIGH priority against a reserved slice that
background traffic cannot touch.

---

## D-005 — A timeout that abandons an in-flight request is worse than no timeout

**Found:** 2026-08-07, after adding a transport-level read timeout.

**What happened.** Adding an 8-second socket read timeout left every
`asyncio.wait_for` around a REST call *shorter* than the socket it wrapped —
`place_order` was capped at 5 seconds against a 12-second read. The outer
timeout always fired first.

That day's only validated signal was lost to it:

```
09:15:58  VALIDATED — broke 133.25 @ 133.20
09:16:03  place_order timed out after 5s
09:16:08  [RECONCILE-ORDER] Orderbook fetch timed out
09:16:15  urllib3: Retrying ... ReadTimeoutError     ← still alive, 12s after we gave up
```

**Why it mattered.** Abandoning an in-flight order placement leaves its state
genuinely unknown — the order may be live at the exchange. Worse, the reconcile
that exists precisely to answer that question had the *same* inversion and timed
out too, so the bot set a cooldown and moved on without ever learning whether a
live SELL order was resting at the broker.

**Fix.** Async budgets are now derived from the transport budget so they cannot
drift below it. The order reconcile retries with backoff and escalates to the
operator if it still cannot determine the state. `place_order` also stopped
awaiting a websocket subscribe before sending — that added 565ms in front of
every order for data the order does not need.

---

## D-006 — A stop can be marketable the instant it is placed

**Found:** 2026-08-06, a short position round-tripped in 130 milliseconds.

**What happened.** The stop level is anchored to the setup high, captured at
signal time. The fill drifted 1.02% above that level:

```
signal ₹813.00 → filled SHORT @ ₹821.30
stop computed from signal_high  = ₹816.00     ← BELOW the short entry
```

A BUY stop at 816 with price at ~821 is immediately marketable. It filled at
₹821.92, 130ms after entry.

**Why it mattered.** A short's stop must sit *above* its entry. When slippage
carries the fill past the structural level, the stop stops being protection and
becomes a guaranteed immediate round-trip. This was latent for a long time and
only surfaced once fills became fast and real — D-001's fix exposed it.

**Fix.** The stop is validated against the actual fill, not the hoped-for price,
and re-anchored if it lands on the wrong side. A hard gate refuses to send a
wrong-side stop at all and exits at market instead.

---

## D-007 — Absence of evidence read as evidence of absence

**Found:** 2026-08-06, a position was declared closed 23ms after entry.

**What happened.** A position-cache *miss* was treated as "the position is
flat", gated on a global last-event timestamp belonging to some other symbol.
The position's own first frame arrived 540ms later.

```
08:34:00,379  [FOCUS] Started NSE:EXAMPLE-EQ
08:34:00,402  Broker CONFIRMED FLAT (2 reads) — manual close detected!
08:34:00,942  Position: NSE:EXAMPLE-EQ netQty=-7      ← the real frame
```

It reached "2 consecutive flat reads" on a single bad read because the counter
was never reset between trades — the previous position had left it at 1.

**Why it mattered.** Telegram reported a close that had not happened, the
capital slot was released, the stop order was cancelled — leaving the position
**naked for 19 seconds** until reconciliation re-adopted it with a tighter
emergency stop than intended. The ML row was written as a closed trade that had not closed; the position
was in fact still open and ran to the square-off.

**Fix.** A cache miss now falls through to the authoritative REST check and is
never interpreted as flat. Per-trade detector state is reset when focus starts,
and a grace period blocks close-detection until the broker has had time to
register the fill.

**Lesson.** The cache is authoritative about what it *contains*, never about
what it *lacks*.

---

## D-009 — A startup alert that was never sent

**Found by:** mypy, running as an advisory step in CI.

`runtime/supervisor.py` validates that every critical dependency was
constructed before the bot is allowed to trade. When one is `None` it logs
CRITICAL, notifies Telegram, and raises. The notification line read:

```python
ctx.bot.send_alert(f"🚨 STARTUP FAIL: {failed} are None. Bot cannot trade.")
```

`send_alert` is a coroutine. Calling it without `await` constructs the
coroutine and discards it, so the alert was never delivered. The surrounding
`except Exception: pass` could not help, because building a coroutine does not
raise. The failure was visible in the log file and nowhere else.

The enclosing function was synchronous, so the fix was to make it `async` and
await the call with a timeout, since a hung send must not wedge a startup that
is already failing.

**Same shape as most of this list:** the code reported success while doing
nothing, and raised no exception. What makes this one different is that a type
checker found it in seconds, on a codebase with no annotations, running in a
mode that does not even gate the build. Of 49 mypy findings, 48 were cosmetic
and one was this.

## D-008 — A silent no-op that had been shipping for months

**Found:** 2026-08-07, while trying to explain persistent REST timeouts.

**What happened.** Connection-pool tuning was applied with:

```python
if hasattr(self.rest_client, 'session'):
    self.rest_client.session.mount(...)
```

`FyersModel` has no `.session` attribute. The real `requests.Session` lives on
an inner service object at `client.service.session`. The condition was always
false, so the mount never happened — the pool stayed at the library default of
10 connections and, more importantly, **no timeout was ever configured at all.**

**Why it mattered.** This is what let a single `/history` call hang for 974
seconds during an EOD square-off, and it had been silently inert since the
commit that "fixed" it.

**Fix.** Resolve the session by walking the known attribute paths, and log an
error if none is found rather than silently doing nothing. A guarded fix that
fails closed is a fix; one that fails silently is a comment.

---

## What these have in common

Six of the eight are the same shape: **something reported success while doing
nothing.** A handler that parsed no messages, a cache nobody wrote, a mount that
never applied, a timeout that abandoned its own work, a "complete" message sent
one second after a timeout warning.

None of them raised an exception. All of them were found by reading logs against
the code and asking whether the numbers agreed — 48 versus 0, 42 versus 42, 41
versus 41. That is the technique this system is now built around, and it is why
the reconciliation engine exists at all: in a distributed system the dangerous
failure is not the one that throws, it is the one where two sides disagree and
neither notices.
