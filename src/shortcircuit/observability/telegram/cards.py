"""
Message bodies, as Telegram HTML. Pure builders: each takes plain data and returns
text, so every card can be tested without a bot, a broker or a network.

Cards are strategy-agnostic. A signal card shows whatever the signal carries:
its strategy, side, pattern, levels, exit plan and the strategy's own `display`
lines. Nothing here knows any particular strategy.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping, Optional, Sequence

from shortcircuit.execution import directions
from shortcircuit.observability.telegram import exits, fmt
from shortcircuit.observability.telegram.book import DayStats, StrategyBook, blurb

MAX_DISPLAY_LINES = 8
PENDING_MINUTES = 15  # the focus engine's fixed expiry for an armed setup

_OUTCOME_REASONS = {
    "GATE12_TRIGGER_BROKEN": "the trigger broke",
    "G12_INVALIDATED_BUFFER": "price ran through the setup level first",
    "G11_DYNAMIC_TIMEOUT": f"no trigger within {PENDING_MINUTES} min",
}


def _trigger(signal: Mapping[str, Any]) -> Any:
    side = directions.of_signal(signal)
    key = "signal_high" if directions.is_long(side) else "signal_low"
    return signal.get("trigger_price") or signal.get(key) or signal.get("entry_price")


def _strategy_line(signal: Mapping[str, Any]) -> str:
    name = signal.get("strategy") or "—"
    return f"Strategy: <code>{fmt.he(name)}</code> · {fmt.side_label(directions.of_signal(signal))}"


def _pattern_line(signal: Mapping[str, Any]) -> str:
    pattern = signal.get("pattern_bonus") or signal.get("pattern") or "—"
    conf = signal.get("confidence")
    return f"Pattern: {fmt.he(pattern)}" + (f" ({fmt.he(conf)})" if conf else "")


def display_lines(signal: Mapping[str, Any]) -> list[str]:
    """The strategy's own lines (Signal.display), escaped and capped."""
    raw = signal.get("display") or {}
    if not isinstance(raw, Mapping):
        return []
    items = list(raw.items())
    lines = [f"{fmt.he(k)}: {fmt.value(v)}" for k, v in items[:MAX_DISPLAY_LINES]]
    if len(items) > MAX_DISPLAY_LINES:
        lines.append(f"<i>…{len(items) - MAX_DISPLAY_LINES} more</i>")
    return lines


def action_line(*, auto: bool, data_only: bool) -> str:
    if data_only:
        return "Action: 🧪 data-only mode, no order"
    return "Action: ⚡ auto, to execution" if auto else "Action: 🔔 alert only (auto off)"


def signal_armed(signal: Mapping[str, Any], *, auto: bool, data_only: bool,
                 margin: Optional[str] = None) -> str:
    """A setup was found and is waiting for its trigger."""
    symbol = signal.get("symbol", "UNKNOWN")
    side = directions.of_signal(signal)
    lines = [
        f"🔍 <b>SIGNAL ARMED</b> · <code>{fmt.he(symbol)}</code>",
        "",
        _strategy_line(signal),
        _pattern_line(signal),
        f"LTP {fmt.price(signal.get('ltp'))} · trigger {fmt.trigger_sign(side)} "
        f"{fmt.price(_trigger(signal))} · stop {fmt.price(signal.get('stop_loss'))}",
        f"Exit: {fmt.he(exits.describe(signal))} · square-off {exits.squareoff_at(symbol)}",
    ]
    extra = display_lines(signal)
    if extra:
        lines += [""] + extra
    if margin:
        lines.append(f"Est. margin: {fmt.he(margin)}")
    lines += ["", action_line(auto=auto, data_only=data_only),
              f"<i>Waiting up to {PENDING_MINUTES} min for the trigger…</i>"]
    return "\n".join(lines)


def signal_outcome(signal: Mapping[str, Any], outcome: str, details: Optional[Mapping[str, Any]] = None,
                   *, auto: bool, data_only: bool, margin: Optional[str] = None) -> str:
    """What became of an armed setup: triggered, expired or invalidated."""
    details = details or {}
    symbol = signal.get("symbol", "UNKNOWN")
    side = directions.of_signal(signal)
    ltp = details.get("ltp", signal.get("ltp"))
    trigger = details.get("trigger_price") or _trigger(signal)
    reason_code = str(details.get("reason") or "")
    reason = _OUTCOME_REASONS.get(reason_code, reason_code or "—")
    head = {
        "VALIDATED": "✅ <b>TRIGGERED</b>",
        "TIMEOUT": "⌛ <b>EXPIRED</b>",
    }.get(str(outcome).upper(), "⛔ <b>INVALIDATED</b>")
    lines = [
        f"{head} · <code>{fmt.he(symbol)}</code>",
        "",
        _strategy_line(signal),
        _pattern_line(signal),
        f"LTP {fmt.price(ltp)} · trigger {fmt.trigger_sign(side)} {fmt.price(trigger)}",
        f"Why: {fmt.he(reason)}",
    ]
    if str(outcome).upper() == "VALIDATED":
        entry = details.get("entry_price") or signal.get("entry_price")
        lines += [
            f"Entry ≈ {fmt.price(entry)} · stop {fmt.price(signal.get('stop_loss'))}",
            f"Exit: {fmt.he(exits.describe(signal))} · square-off {exits.squareoff_at(symbol)}",
        ]
        if margin:
            lines.append(f"Est. margin: {fmt.he(margin)}")
        lines += ["", action_line(auto=auto, data_only=data_only)]
    return "\n".join(lines)


# Strategies

def strategy_row(book: StrategyBook, strategy, stats: Optional[DayStats] = None) -> str:
    stats = stats or book.stats(strategy.name)
    state = "⏸️ paused" if book.is_paused(strategy.name) else "▶️ running"
    text = blurb(strategy)
    lines = [
        f"<b>{fmt.he(strategy.name)}</b> · {fmt.side_label(strategy.direction)} · {state}",
        f"  universe {fmt.he(strategy.universe.value)} · "
        f"HTF check {'on' if strategy.requires_htf_confirmation else 'off'}",
        f"  today: {stats.line()}",
    ]
    if text:
        lines.insert(1, f"  <i>{fmt.he(text[:160])}</i>")
    return "\n".join(lines)


def strategies_list(book: StrategyBook) -> str:
    strategies = book.strategies
    if not strategies:
        return ("📭 <b>No strategy is active.</b>\n\n"
                "The engine scans and manages open positions but opens nothing. "
                "Set SHORTCIRCUIT_STRATEGIES or ACTIVE in strategies/__init__.py and restart.")
    paused = sum(book.is_paused(s.name) for s in strategies)
    head = f"🧩 <b>Strategies</b> · {len(strategies)} active" + (f", {paused} paused" if paused else "")
    body = "\n\n".join(strategy_row(book, s) for s in strategies)
    return (f"{head}\n<i>In priority order: the first to return a signal wins.</i>\n\n{body}\n\n"
            "/strategy &lt;name&gt; for details · /pause or /resume &lt;name|all&gt;")


def strategy_detail(book: StrategyBook, strategy) -> str:
    lines = [strategy_row(book, strategy), ""]
    details = book.details(strategy)
    if details:
        lines.append("<b>Settings</b>")
        lines += [f"  {k}: {v}" for k, v in details]
    else:
        lines.append("<i>This strategy publishes no settings (Strategy.describe).</i>")
    return "\n".join(lines)


# Status blocks

def status_card(*, sync_live: bool, auto_text: str, run_text: str, session_text: str,
                capital_block: str, open_count: int, unrealised: float, day_pnl: float,
                signal_block: str, health_block: str, strategies_text: str) -> str:
    return (
        f"📊 <b>ShortCircuit Status</b> ({'⚡ Live' if sync_live else '🕒 Cached'})\n\n"
        f"Run:        {run_text}\n"
        f"Auto:       {auto_text}\n"
        f"Direction:  {fmt.mode_label()}\n"
        f"Strategies: {strategies_text}\n"
        f"{session_text}"
        f"\n━━━━━ CAPITAL ━━━━━\n{capital_block}"
        f"\n━━━━━ TRADING ━━━━━\n"
        f"Open:      {open_count} position(s)\n"
        f"Unrealised: <b>{fmt.money(unrealised, signed=True)}</b>\n"
        f"Day P&amp;L:   <b>{fmt.money(day_pnl, signed=True)}</b>\n"
        f"{signal_block}"
        f"\n━━━━━ HEALTH ━━━━━\n{health_block}"
        f"\n<i>As of {fmt.clock()} IST</i>"
    )


def position_block(pos: Mapping[str, Any], ltp: Optional[float]) -> str:
    symbol = pos.get("symbol", "?")
    side = directions.of_signal(pos)
    qty = int(pos.get("qty") or 0)
    entry = pos.get("entry_price")
    lines = [
        f"<b>{fmt.he(symbol)}</b> · {fmt.side_label(side)} ×{qty}"
        + (f" · <code>{fmt.he(pos['strategy'])}</code>" if pos.get("strategy") else ""),
        f"  entry {fmt.price(entry)} · stop {fmt.price(pos.get('stop_loss'))}"
        + (f" · LTP {fmt.price(ltp)}" if ltp else ""),
    ]
    if ltp and entry:
        sign = 1 if directions.is_long(side) else -1
        pnl = (float(ltp) - float(entry)) * qty * sign
        lines.append(f"  unrealised {fmt.money(pnl, signed=True)}")
    lines.append(f"  exit: {fmt.he(exits.describe(pos))} · square-off {exits.squareoff_at(symbol)}")
    started = pos.get("entry_time")
    if isinstance(started, datetime):
        lines.append(f"  since {started.strftime('%H:%M')}")
    return "\n".join(lines)


def pending_block(symbol: str, pending: Mapping[str, Any], now_ts: float) -> str:
    data = pending.get("data") or {}
    side = pending.get("direction") or directions.of_signal(data)
    age = max(0.0, now_ts - float(pending.get("timestamp") or now_ts)) / 60
    return (
        f"<b>{fmt.he(symbol)}</b> · {fmt.side_label(side)}"
        + (f" · <code>{fmt.he(data['strategy'])}</code>" if data.get("strategy") else "")
        + f"\n  trigger {fmt.trigger_sign(side)} {fmt.price(pending.get('trigger'))} · "
        f"abandon past {fmt.price(pending.get('invalidate'))} · armed {age:.0f} min ago"
    )


def morning_brief(*, date_text: str, time_text: str, quote: str, range_lines: Sequence[str],
                  data_ok: bool, order_ok: bool, cache_text: str, candle_ok: bool,
                  auto_text: str, run_text: str, strategy_names: Iterable[str],
                  squareoff_text: str) -> str:
    names = [fmt.he(n) for n in strategy_names]
    return "\n".join([
        "🌅 <b>ShortCircuit · Market Open</b>",
        f"📅 {fmt.he(date_text)}",
        "",
        f"<i>{fmt.he(quote)}</i>",
        "",
        "📊 <b>NIFTY 50 morning range</b>",
        *[f"   {line}" for line in range_lines],
        "",
        "🔌 <b>System</b>",
        f"   WS data {'✅' if data_ok else '❌'} · WS order {'✅' if order_ok else '❌'}",
        f"   {fmt.he(cache_text)}",
        f"   Candle API {'✅ verified' if candle_ok else '❌ failed'}",
        f"   Run: {run_text} · Auto: {auto_text}",
        f"   Direction: {fmt.mode_label()}",
        f"   Square-off: {fmt.he(squareoff_text)}",
        "",
        "🧩 <b>Strategies</b>",
        f"   {', '.join(names) if names else 'none active: the engine will open nothing'}",
        "",
        f"⏱ Ready at {fmt.he(time_text)}",
    ])
