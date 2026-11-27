"""
Replay one symbol-day through the engine's own decision code.

What is the engine's own code here, not a copy of it:
  * the strategy's `evaluate`, fed a SetupContext built by the live analyzer's
    `derive_features`;
  * `signal_problem`, the direction check, the G2 candle count and the trade
    window;
  * the staged exits' bar aggregation (`focus_engine.completed_bars`) and RSI
    (`features.compute_rsi_wilder`);
  * the per-symbol square-off (`eod.squareoff`).

What is modelled, because history cannot show it:
  * Evaluation happens at the START of each minute, with that minute's open as
    the last price and a forming bar carrying nothing else. Nothing from inside
    or after the minute is visible to the strategy.
  * Fills: entry at the trigger (or the bar's open if it opened through it),
    stops at the stop (or the open if it gapped through), partials and trail
    exits at the next bar's open — every fill then moved against the trade by
    `slippage_pct`. When one bar could have hit both the stop and something
    better, the stop is assumed to have come first.
  * The live depth feed is unavailable. The upper circuit is inferred from the
    day itself (`infer_upper_circuit`): NSE bands a non-F&O stock at 5, 10 or
    20%, and a day that stopped dead at +10% was banded at 10. A strategy's
    circuit guards then run on that, and `is_circuit_hitter` is set once the
    session high has reached it, as the live analyzer marks it. Spread is
    unknown (0.0) and leverage is not screened.
  * One position per symbol-day; the engine's single capital slot across
    symbols is not modelled.

Profiles, each as the focus engine runs it, decided bar by bar:
  * STAGED_RSI: the partial to cost, then the trail.
  * DEFAULT: the take-profit ladder of config.TP_MODE toward the signal's VWAP
    (SCALE: half at the midpoint, the stop to breakeven if
    P52_BREAKEVEN_AFTER_TP1, the rest at the VWAP; SINGLE: all at the
    midpoint) and the MAX_HOLD_TIME_MINUTES time stop. A level a bar's low
    reached fills at the level, or at the open if the bar opened through it.
  * EOD_HOLD: the hard stop to the square-off, covered early by the strategy's
    RSI relief-rally rule (ExitPlan.rsi_cover) at the next open.
`exit_model` on every trade says which ran.

G9: a strategy with `requires_htf_confirmation` is held to the analyzer's HTF
check, on 15-minute bars built from the session (the last one forming, closing
at the minute's open). `frame_bars` hands the strategy only the last N bars,
as VWAP_ANCHOR_MODE='ROLLING' does live; the default is the whole session.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Optional, Sequence

import pandas as pd

from shortcircuit import config
from shortcircuit.eod import squareoff
from shortcircuit.execution.analyzer import derive_features, signal_problem
from shortcircuit.execution.focus_engine import completed_bars, compute_tp_levels
from shortcircuit.strategy import features as F
from shortcircuit.strategy.htf_confluence import HTFConfluence
from shortcircuit.strategy.market_context import MarketContext
from shortcircuit.strategy.market_profile import ProfileAnalyzer
from shortcircuit.strategy.sdk import ExitProfile, SetupContext, Signal, Strategy

logger = logging.getLogger(__name__)

IST = "Asia/Kolkata"
FIRST_ENTRY = dt.time(9, 30)            # G7: no entries in the opening noise
LAST_ENTRY = dt.time(15, 10)
GATE_MINUTES = 15                       # validation gate expiry
INVALIDATION = 1.002                    # gate abandons above setup high x this


@dataclass
class Costs:
    slippage_pct: float = 0.10          # per fill, against the trade
    cost_pct: float = 0.11              # statutory + brokerage, per round trip


@dataclass
class Trade:
    symbol: str
    date: dt.date
    strategy: str
    pattern: str
    signal_time: pd.Timestamp
    entry_time: pd.Timestamp
    entry: float
    stop: float
    exit_model: str
    legs: list = field(default_factory=list)       # (fraction, price, reason, time)
    mfe_pct: float = 0.0
    mae_pct: float = 0.0
    features: dict = field(default_factory=dict)
    trigger: float = 0.0
    stop_reference: float = 0.0
    # (time, stop) every time the stop moved, the first entry the initial stop.
    stop_path: list = field(default_factory=list)

    @property
    def exit_reason(self) -> str:
        return self.legs[-1][2] if self.legs else "OPEN"

    @property
    def gross_pct(self) -> float:
        return sum(f * (self.entry - px) / self.entry * 100 for f, px, _, _ in self.legs)

    def net_pct(self, costs: Costs) -> float:
        return self.gross_pct - costs.cost_pct

    def row(self, costs: Costs) -> dict:
        return {
            "date": self.date.isoformat(), "symbol": self.symbol, "strategy": self.strategy,
            "pattern": self.pattern, "signal_time": self.signal_time.strftime("%H:%M"),
            "entry_time": self.entry_time.strftime("%H:%M"), "entry": round(self.entry, 2),
            "stop": round(self.stop, 2), "exit_reason": self.exit_reason,
            "exit_time": self.legs[-1][3].strftime("%H:%M") if self.legs else "",
            "legs": " | ".join(f"{f:.0%}@{p:.2f} {r}" for f, p, r, _ in self.legs),
            "gross_pct": round(self.gross_pct, 3), "net_pct": round(self.net_pct(costs), 3),
            "mfe_pct": round(self.mfe_pct, 3), "mae_pct": round(self.mae_pct, 3),
            "exit_model": self.exit_model,
            "trigger": round(self.trigger, 2), "stop_reference": round(self.stop_reference, 2),
            # Machine-readable twins of `legs`, for the dashboard.
            "legs_json": json.dumps([[round(f, 4), round(p, 2), r, w.isoformat()]
                                     for f, p, r, w in self.legs]),
            "stop_path_json": json.dumps([[w.isoformat(), round(p, 2)] for w, p in self.stop_path]),
            **self.features,
        }


@dataclass
class DayResult:
    signals: int = 0
    invalidated: int = 0
    expired: int = 0
    g9_blocked: int = 0
    trades: list = field(default_factory=list)
    # One dict per signal: what became of it (ENTERED, INVALIDATED, EXPIRED).
    events: list = field(default_factory=list)


def infer_upper_circuit(prev_close: float, day_high: float, fno: Optional[bool]) -> float:
    """
    The day's upper circuit as well as daily bars can tell it; 0.0 when unknown.

    F&O stocks have no fixed band, only dynamic ones that widen, so 0.0. Other
    stocks are banded at 2, 5, 10 or 20%; one that topped out within a tick or
    two of +10% was banded at 10 (it could not go higher), and one that went
    beyond it at 20. Days that never reached +9.9% leave the band unknown: a
    strategy shorting smaller moves against a 2% or 5% band runs without its
    circuit guard here. The band is a fixed exchange parameter the live engine reads
    at the open, so deriving it from the whole day leaks no price direction.
    """
    if fno or prev_close <= 0 or day_high <= 0:
        return 0.0
    gain = (day_high / prev_close - 1) * 100
    if 9.9 <= gain <= 10.1:
        band = 10.0
    elif 10.1 < gain <= 20.1:
        band = 20.0
    else:
        return 0.0
    return round(round(prev_close * (1 + band / 100) / 0.05) * 0.05, 2)


def _tick_up(price: float, tick: float = 0.05) -> float:
    return round(math.ceil(round(price / tick, 6)) * tick, 2)


def _candles(df: pd.DataFrame) -> list:
    return [SimpleNamespace(epoch=int(e), high=float(h), close=float(c))
            for e, h, c in zip(df["epoch"], df["high"], df["close"], strict=True)]


def _frame_at(day: pd.DataFrame, i: int, bars: Optional[int] = None) -> pd.DataFrame:
    """
    Completed bars before minute i, plus minute i as a forming bar: open only.
    With `bars`, the frame is that many rows, forming bar included — the live
    ROLLING anchor's window.
    """
    done = day.iloc[max(0, i - (bars - 1)):i] if bars else day.iloc[:i]
    o = float(day["open"].iloc[i])
    forming = day.iloc[[i]].copy()
    forming[["high", "low", "close"]] = o
    forming["volume"] = 0
    return pd.concat([done, forming], ignore_index=True)


def htf_bars(frame: pd.DataFrame, minutes: int = 15) -> pd.DataFrame:
    """
    `minutes` bars aligned to 09:15 from a 1-minute frame. The last is still
    forming and closes at the frame's last price, as a REST fetch returns it
    mid-bar to the live G9.
    """
    dts = frame["datetime"]
    bucket = (dts.dt.hour * 60 + dts.dt.minute - (9 * 60 + 15)) // minutes
    return frame.groupby(bucket.values, sort=True).agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), volume=("volume", "sum"),
    ).reset_index(drop=True)


_HTF = HTFConfluence(None)      # given its bars, the check fetches nothing


def _first_signal(strategies: Sequence[Strategy], ctx: SetupContext):
    for s in strategies:
        if s.direction.value != config.TRADE_DIRECTION:
            continue
        try:
            sig = s.evaluate(ctx)
        except Exception as exc:
            logger.debug("[BACKTEST] %s raised on %s: %s", s.name, ctx.symbol, exc)
            continue
        if sig is not None and signal_problem(sig) is None:
            return s, sig
    return None, None


def replay_day(symbol: str, day: pd.DataFrame, prev_close: float,
               strategies: Sequence[Strategy], costs: Costs = Costs(),
               max_trades: int = 1, upper_circuit: float = 0.0,
               frame_bars: Optional[int] = None) -> DayResult:
    """
    `day` is one session's 1-minute bars: epoch, datetime (IST), open, high,
    low, close, volume — completed bars only. `upper_circuit` is the day's
    band, 0.0 when unknown (see `infer_upper_circuit`). `frame_bars`: hand the
    strategy only the last N bars (live ROLLING anchor); None = the session.
    """
    out = DayResult()
    if day is None or len(day) < 20:
        return out
    day = day.reset_index(drop=True)
    profile, market = ProfileAnalyzer(), MarketContext(None)
    cutoff_time = squareoff.squareoff_time(symbol)

    i = 0
    while i < len(day) and len(out.trades) < max_trades:
        t = day["datetime"].iloc[i]
        if t.time() < FIRST_ENTRY or i + 1 < config.RVOL_MIN_CANDLES:
            i += 1
            continue
        if t.time() >= min(LAST_ENTRY, cutoff_time):
            break
        ltp = float(day["open"].iloc[i])
        # The scanner's admission filters, as they would read at this moment.
        gain = (ltp - prev_close) / prev_close * 100 if prev_close > 0 else 0.0
        if not (config.SCANNER_GAIN_MIN_PCT <= gain <= config.SCANNER_GAIN_MAX_PCT) \
                or ltp < config.SCANNER_MIN_LTP \
                or float(day["volume"].iloc[:i].sum()) < config.SCANNER_MIN_VOLUME:
            i += 1
            continue

        frame = _frame_at(day, i, frame_bars)
        feats = derive_features(symbol, frame, ltp, prev_close, profile, market)
        high_so_far = max(float(day["high"].iloc[:i].max()), ltp)
        hitter = upper_circuit > 0 and high_so_far >= upper_circuit * 0.999
        ctx = SetupContext(symbol=symbol, ltp=ltp, now=t.to_pydatetime(), df=frame,
                           df_15m=None, upper_circuit=upper_circuit, lower_circuit=0.0,
                           spread_pct=0.0, is_circuit_hitter=hitter, **feats)
        strategy, sig = _first_signal(strategies, ctx)
        if sig is None:
            i += 1
            continue

        out.signals += 1
        trigger = float(sig.trigger_price) if sig.trigger_price is not None \
            else float(frame["low"].iloc[-2])
        event = {
            "date": t.date().isoformat(), "symbol": symbol, "strategy": strategy.name,
            "time": t.strftime("%H:%M"), "trigger": round(trigger, 2),
            "stop_reference": round(float(sig.stop_reference), 2),
        }
        # G9, as the analyzer runs it after evaluate(). The 15-minute bars come
        # from a REST fetch of the day, whatever the 1-minute frame's anchor.
        if strategy.requires_htf_confirmation:
            ok, _msg = _HTF.check_trend_exhaustion(
                symbol, df_15m=htf_bars(_frame_at(day, i)), vwap_sd=feats["vwap_sd"])
            if not ok:
                out.g9_blocked += 1
                out.events.append({**event, "outcome": "G9_BLOCKED"})
                i += 1
                continue
        entry_i, fill = _gate(day, i, trigger, float(sig.stop_reference) * INVALIDATION)
        out.events.append({**event, "outcome": "ENTERED" if entry_i is not None else fill})
        if entry_i is None:
            out.invalidated += fill == "INVALIDATED"
            out.expired += fill == "EXPIRED"
            i += 1
            continue

        vwap_target = float(frame["vwap"].iloc[-1]) if "vwap" in frame.columns else 0.0
        trade = _run_trade(symbol, day, entry_i, float(fill) * (1 - costs.slippage_pct / 100),
                           sig, strategy, feats["atr"], t, costs, cutoff_time, vwap_target)
        trade.trigger = trigger
        out.trades.append(trade)
        exit_t = trade.legs[-1][3] if trade.legs else day["datetime"].iloc[-1]
        i = int((day["datetime"] > exit_t).idxmax()) if (day["datetime"] > exit_t).any() else len(day)
    return out


def _gate(day: pd.DataFrame, i: int, trigger: float, invalid_above: float):
    """(bar index, fill price) on entry; (None, 'INVALIDATED' | 'EXPIRED') otherwise."""
    for j in range(i, min(i + GATE_MINUTES, len(day))):
        o, h, lo = (float(day[c].iloc[j]) for c in ("open", "high", "low"))
        if o < trigger:
            return j, o                         # opened through the trigger
        if h > invalid_above:
            return None, "INVALIDATED"          # assume the adverse move came first
        if lo < trigger:
            return j, trigger
    return None, "EXPIRED"


def _ladder(plan, entry: float, vwap_target: float):
    """
    The DEFAULT profile's take-profit levels as the focus engine sets them at
    entry: (mode, tp_1, tp_2, full-exit level). The VWAP target falls back to
    1% below entry, as OrderManager.compute_take_profits does without a VWAP.
    """
    mode = str(getattr(config, "TP_MODE", "OFF")).upper()
    if plan.profile is not ExitProfile.DEFAULT or mode not in ("SCALE", "SINGLE"):
        return "OFF", None, None, None
    target = vwap_target if vwap_target and vwap_target > 0 else entry * 0.99
    target = round(round(target / 0.05) * 0.05, 2)
    tp_1, tp_2 = compute_tp_levels(entry, target, 0.05, False)
    if tp_1 is None:
        return "OFF", None, None, None
    return mode, tp_1, tp_2, (tp_1 if mode == "SINGLE" else tp_2)


def _run_trade(symbol, day, j, entry, sig: Signal, strategy: Strategy, atr: float,
               signal_time, costs: Costs, cutoff_time: dt.time,
               vwap_target: float = 0.0) -> Trade:
    plan = sig.exit
    slip = costs.slippage_pct / 100
    if plan.stop_pct is not None:
        stop = _tick_up(entry * (1 + plan.stop_pct / 100))
    else:
        stop = _tick_up(float(sig.stop_reference) + max(atr * 0.5, 0.15))
    staged = plan.staged_rsi if plan.profile is ExitProfile.STAGED_RSI else None
    cover = plan.rsi_cover if plan.profile is ExitProfile.EOD_HOLD else None
    tp_mode, tp_1, _tp_2, full_level = _ladder(plan, entry, vwap_target)
    max_hold = int(getattr(config, "MAX_HOLD_TIME_MINUTES", 0) or 0) \
        if plan.profile is ExitProfile.DEFAULT else 0
    breakeven = bool(getattr(config, "P52_BREAKEVEN_AFTER_TP1", False))
    model = ("staged_rsi" if staged else "eod_hold_rsi_cover" if cover
             else "tp_ladder" if tp_mode != "OFF" else "stop_or_squareoff")
    trade = Trade(
        symbol=symbol, date=day["datetime"].iloc[0].date(), strategy=strategy.name,
        pattern=sig.pattern, signal_time=signal_time, entry_time=day["datetime"].iloc[j],
        entry=entry, stop=stop,
        exit_model=model,
        features={k: v for k, v in sig.ml_features.items() if isinstance(v, (int, float, str))},
        stop_reference=float(sig.stop_reference),
        stop_path=[(day["datetime"].iloc[j], stop)],
    )
    remaining, scaled, trailing = 1.0, False, False
    rsi_seen, trail_seen = None, None
    entry_epoch = int(day["epoch"].iloc[j]) + 30
    candles = _candles(day)
    pending: Optional[str] = None                 # action decided at a close, done at the next open

    def close(frac, px, reason, when):
        nonlocal remaining
        trade.legs.append((frac, px * (1 + slip), reason, when))
        remaining -= frac

    for k in range(j, len(day)):
        o, h, lo, c = (float(day[x].iloc[k]) for x in ("open", "high", "low", "close"))
        when = day["datetime"].iloc[k]
        if k > j:
            if when.time() >= cutoff_time:
                close(remaining, o, "EOD_SQUAREOFF", when)
                return trade
            # The time stop runs before the square-off check live; both are at
            # this open, so the order between them does not matter here.
            if max_hold and (when - trade.entry_time).total_seconds() / 60 >= max_hold:
                close(remaining, o, "TIME_STOP", when)
                return trade
            if pending == "RSI_TP":
                close(remaining, o, "RSI_TP", when)
                return trade
            if pending == "SCALE_OUT" and remaining > 0:
                if o < entry:
                    close(remaining * staged.scale_out_fraction, o, "RSI_SCALE_OUT", when)
                    scaled = True
                    if staged.stop_to_cost and entry < stop:
                        stop = entry
                        trade.stop_path.append((when, stop))
                pending = None
            elif pending == "TRAIL_EXIT":
                close(remaining, o, "TRAIL_STOP", when)
                return trade
            if o >= stop:
                close(remaining, o, _stop_reason(scaled, trailing, stop, entry), when)
                return trade
        trade.mfe_pct = max(trade.mfe_pct, (entry - lo) / entry * 100)
        trade.mae_pct = max(trade.mae_pct, (h - entry) / entry * 100)
        if h >= stop:
            close(remaining, stop, _stop_reason(scaled, trailing, stop, entry), when)
            return trade

        # DEFAULT: the take-profit ladder. Not on the entry bar, whose low may
        # have come before the fill. The full exit is tested first, as live.
        if tp_mode != "OFF" and k > j:
            if lo <= full_level:
                close(remaining, min(o, full_level), "TP_HIT", when)
                return trade
            if tp_mode == "SCALE" and not scaled and lo <= tp_1:
                close(remaining * 0.5, min(o, tp_1), "TP1_PARTIAL", when)
                scaled = True
                if breakeven and entry < stop:
                    stop = entry
                    trade.stop_path.append((when, stop))
                    # Which came first inside this bar is unknown: the stop.
                    if h >= stop:
                        close(remaining, stop, "COST_STOP", when)
                        return trade

        # EOD_HOLD: the RSI relief-rally cover, read on this completed bar and
        # done at the next open.
        if cover is not None and pending is None and k + 1 >= cover.min_candles \
                and k - j >= cover.skip_minutes and c < entry:
            rsi = F.compute_rsi_wilder(day["close"].iloc[: k + 1].tolist(), cover.period)
            if rsi == rsi and rsi < cover.threshold:
                pending = "RSI_TP"

        if staged is None:
            continue
        now_epoch = int(day["epoch"].iloc[k]) + 60     # this bar has just closed
        done = candles[: k + 1]
        bars = completed_bars(done, staged.bar_minutes, now_epoch)
        if len(bars) >= staged.min_bars and bars[-1].epoch != rsi_seen:
            rsi_seen = bars[-1].epoch
            if bars[-1].epoch + staged.bar_minutes * 60 > entry_epoch:
                rsi = F.compute_rsi_wilder([b.close for b in bars], staged.period)
                if rsi == rsi:
                    if not scaled and pending is None and rsi <= staged.scale_out_rsi \
                            and bars[-1].close < entry:
                        pending = "SCALE_OUT"
                    if scaled and not trailing and rsi <= staged.trail_rsi:
                        trailing = True
        if trailing:
            tb = completed_bars(done, staged.trail_bar_minutes, now_epoch)
            if tb and tb[-1].epoch != trail_seen:
                trail_seen = tb[-1].epoch
                level = _tick_up(tb[-1].high * (1 + staged.trail_buffer_pct / 100))
                if level < stop:
                    # Live, the trail is decided on the price just after the
                    # close: already above the level means the trail is hit.
                    if c >= level:
                        pending = "TRAIL_EXIT"
                    else:
                        stop = level
                        trade.stop_path.append((when, stop))

    if remaining > 1e-9:
        close(remaining, float(day["close"].iloc[-1]), "EOD_SQUAREOFF", day["datetime"].iloc[-1])
    return trade


def _stop_reason(scaled: bool, trailing: bool, stop: float, entry: float) -> str:
    if trailing:
        return "TRAIL_STOP"
    if scaled and stop <= entry:
        return "COST_STOP"
    return "SL_HIT"


def summarise(trades: Sequence[Trade], costs: Costs) -> dict:
    if not trades:
        return {"trades": 0}
    net = pd.Series([t.net_pct(costs) for t in trades])
    wins, losses = net[net > 0], net[net <= 0]
    reasons = pd.Series([t.exit_reason for t in trades]).value_counts().to_dict()
    return {
        "trades": len(trades),
        "win_rate_pct": round(len(wins) / len(net) * 100, 1),
        "avg_net_pct": round(net.mean(), 3),
        "median_net_pct": round(net.median(), 3),
        "total_net_pct": round(net.sum(), 2),
        "avg_win_pct": round(wins.mean(), 3) if len(wins) else 0.0,
        "avg_loss_pct": round(losses.mean(), 3) if len(losses) else 0.0,
        "profit_factor": round(wins.sum() / -losses.sum(), 2) if len(losses) and losses.sum() < 0 else None,
        "worst_pct": round(net.min(), 3),
        "best_pct": round(net.max(), 3),
        "exit_reasons": reasons,
    }


__all__ = ["Costs", "DayResult", "Trade", "infer_upper_circuit", "replay_day", "summarise"]
