"""Read-only views: account status, open positions, armed setups, broker health."""
from __future__ import annotations

import asyncio
import logging
import time

from shortcircuit.observability.telegram import cards, fmt
from shortcircuit.observability.telegram.commands import Command

logger = logging.getLogger(__name__)

GROUP = "Monitoring"


async def status(bot, update, context):
    """Full snapshot, synced live from the broker with a 3 s cap on each call."""
    day_pnl, unrealised, open_count, live = 0.0, 0.0, 0, True
    om = bot.order_manager
    if om and getattr(om, "broker", None):
        try:
            if bot.capital_manager:
                try:
                    await asyncio.wait_for(bot.capital_manager.sync(om.broker), timeout=3.0)
                except asyncio.TimeoutError:
                    logger.warning("/status: capital sync timed out; cached values shown.")
            try:
                trades = await asyncio.wait_for(om.get_today_trades(), timeout=3.0)
            except asyncio.TimeoutError:
                logger.warning("/status: trade sync timed out; cached positions shown.")
                live = False
                trades = [{"symbol": s, "realised_pnl": 0.0,
                           "unrealised_pnl": p.get("unrealised_pnl", 0.0), "qty": p.get("qty", 0)}
                          for s, p in list(getattr(om, "active_positions", {}).items())]
            day_pnl = sum(t.get("realised_pnl", 0.0) for t in trades)
            unrealised = sum(t.get("unrealised_pnl", 0.0) for t in trades)
            open_count = len([t for t in trades if t.get("qty", 0) != 0])
        except Exception as exc:
            logger.error("Live sync failed in /status: %s", exc)
            live = False
            if bot.signal_manager:
                day_pnl = getattr(bot.signal_manager, "daily_pnl", 0.0)
            positions = getattr(om, "active_positions", {}) or {}
            open_count = len(positions)
            unrealised = sum(p.get("unrealised_pnl", 0.0) for p in list(positions.values()))

    await bot.reply(update, cards.status_card(
        sync_live=live, auto_text=bot.auto_text(), run_text=bot.run_text(),
        session_text=bot.session_block(), capital_block=bot.capital_block(),
        open_count=open_count, unrealised=unrealised, day_pnl=day_pnl,
        signal_block=bot.signal_block(), health_block=bot.health_block(),
        strategies_text=bot.strategies_text(),
    ))


async def positions(bot, update, context):
    om = bot.order_manager
    held = list((getattr(om, "active_positions", {}) or {}).items()) if om else []
    if not held:
        await bot.reply(update, "📭 No open positions.")
        return
    quotes = bot.quote_snapshot()
    blocks = [cards.position_block({"symbol": s, **p}, (quotes.get(s) or {}).get("ltp"))
              for s, p in held]
    await bot.reply(update, f"📂 <b>Open positions</b> · {len(held)}\n\n" + "\n\n".join(blocks)
                    + f"\n\n<i>As of {fmt.clock()} IST</i>")


async def signals(bot, update, context):
    fe = bot.focus_engine
    pending = list((getattr(fe, "pending_signals", {}) or {}).items()) if fe else []
    now = time.time()
    lines = [f"🎯 <b>Armed setups</b> · {len(pending)}"]
    if pending:
        lines += [""] + [cards.pending_block(sym, p, now) for sym, p in pending]
    else:
        lines.append("<i>None waiting for a trigger.</i>")
    lines += ["", "<b>Today, by strategy</b>"]
    names = list(bot.book.names) or []
    if names:
        lines += [f"<code>{fmt.he(n)}</code>: {bot.book.stats(n).line()}" for n in names]
    else:
        lines.append("<i>No strategy is active.</i>")
    await bot.reply(update, "\n".join(lines))


async def health(bot, update, context):
    """Broker websocket and cache health."""
    broker = getattr(bot.order_manager, "broker", None) if bot.order_manager else None
    if not broker or not hasattr(broker, "get_health_report"):
        await bot.reply(update, "❓ Broker not available.")
        return
    try:
        r = broker.get_health_report()
    except Exception as exc:
        logger.error("/health error: %s", exc)
        await bot.reply(update, f"❌ Health report error: {fmt.he(exc)}")
        return
    state = r.get("health_state", "UNKNOWN")
    icon = {
        "READY": "✅", "RECOVERED": "✅", "DEGRADED": "⚠️", "CRITICAL": "🟠",
        "SEVERE_DEGRADED": "🔴", "REPRIME_PENDING": "🔄", "FULL_RECONNECT_PENDING": "⚡",
        "HYBRID_REST_MODE": "🛑", "UNRECOVERABLE": "⛔", "PRIMING": "⏳",
        "CONNECTING": "🔌", "UNINITIALIZED": "❓",
    }.get(state, "❓")
    age = r.get("data_ws_last_event_age")
    fresh_pct = r.get("fresh_pct", 0)
    fresh_icon = "✅" if fresh_pct >= 85 else ("⚠️" if fresh_pct >= 50 else "🔴")
    await bot.reply(update, (
        "🩺 <b>Broker health</b>\n\n"
        f"State: {icon} <b>{fmt.he(state)}</b> ({r.get('time_in_state_secs', 0):.0f}s)\n\n"
        "━━━━━ WEBSOCKETS ━━━━━\n"
        f"Data:  {'✅ connected' if r.get('data_ws_connected') else '❌ disconnected'}"
        + (f" (last event {age:.0f}s ago)" if age is not None else "") + "\n"
        f"  reconnects {r.get('data_ws_reconnects', 0)}\n"
        f"Order: {'✅ connected' if r.get('order_ws_connected') else '❌ disconnected'}\n"
        f"  reconnects {r.get('order_ws_reconnects', 0)}\n\n"
        "━━━━━ CACHE ━━━━━\n"
        f"Fresh: {fresh_icon} {r.get('fresh_count', 0)}/{r.get('total_subscribed', 0)} ({fresh_pct:.1f}%)\n"
        f"Stale {r.get('stale_count', 0)} · seeded (REST) {r.get('seeded_count', 0)} · "
        f"missing {r.get('missing_count', 0)}\n"
        f"Age P50/P95: {r.get('age_p50', 0):.1f}s / {r.get('age_p95', 0):.1f}s\n\n"
        "━━━━━ RECOVERY ━━━━━\n"
        f"Reprimes {r.get('total_reprime_attempts', 0)} · reconnects {r.get('total_reconnect_attempts', 0)} · "
        f"consecutive failures {r.get('consecutive_reprime_fails', 0)}\n"
        f"Time degraded {r.get('session_degraded_secs', 0):.0f}s · capital timeouts "
        f"{r.get('capital_sync_timeouts', 0)} · reconcile timeouts {r.get('reconcile_timeouts', 0)}\n"
        f"\n<i>As of {fmt.clock()} IST</i>"
    ))


def register(registry) -> None:
    registry.add(Command("status", "Account, P&L, strategies and system snapshot", status,
                         group=GROUP, menu=True))
    registry.add(Command("positions", "Open positions with their strategy, stop and exit", positions,
                         group=GROUP, menu=True))
    registry.add(Command("signals", "Setups waiting for a trigger, and today's counts", signals,
                         group=GROUP, menu=True))
    registry.add(Command("health", "Broker websockets and quote cache", health, group=GROUP))
