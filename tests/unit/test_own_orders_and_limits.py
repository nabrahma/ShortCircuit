"""
Three ways the bot mistook itself, or the broker's budget, for something else.

All three showed up in the 7-10 Sep 2026 sessions:

  * NSE:ASIANENE-EQ, 8 Sep — the bot stood down 131ms after its own TP-1 partial
    filled, because manual-override detection compared every working order
    against the stop's id alone. Its own exit order was indistinguishable from
    the operator's.
  * 54 leverage lookups failed, all HTTP 429, on a `requests.post` that never
    took a rate-limit token. (Budget metering is now covered by test_transport.)
"""
from __future__ import annotations

import pathlib

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "shortcircuit"


# ── a bot order is not an override ───────────────────────────────────────────

def test_every_placed_order_id_is_remembered():
    """
    Membership of `bot_order_ids` is the only honest test of "did we place this".
    It has to be written on the success path of place_order, which every order
    in the process goes through.
    """
    src = (SRC / "broker" / "fyers_broker_interface.py").read_text(encoding="utf-8")
    assert "self.bot_order_ids: set[str] = set()" in src, "the set must exist"

    ok_branch = src.index("if isinstance(response, dict) and response.get('s') == 'ok':")
    returned = src.index("return order_id", ok_branch)
    remembered = src.index("self.bot_order_ids.add(order_id)", ok_branch)
    assert remembered < returned, (
        "the id must be recorded before place_order hands it back, or a fast "
        "reconcile can see the order before we admit to owning it"
    )


def test_override_detection_consults_the_bot_order_set():
    src = (SRC / "execution" / "order_manager.py").read_text(encoding="utf-8")
    start = src.index("# Manual-override detection")
    end = src.index("manual_override_detected and not pos.get('manual_override')")
    block = src[start:end]
    assert "bot_order_ids" in block, (
        "without this the bot's own partial-exit order reads as operator "
        "intervention and it stops managing a position it is mid-way through "
        "scaling out of"
    )


def test_an_operator_stop_drag_is_still_an_override():
    """The fix must not disarm the detector — rule 2 has to survive."""
    src = (SRC / "execution" / "order_manager.py").read_text(encoding="utf-8")
    start = src.index("# Manual-override detection")
    end = src.index("manual_override_detected and not pos.get('manual_override')")
    block = src[start:end]
    assert "stopPrice" in block and "internal_sl" in block, (
        "a stop the operator dragged keeps its id, so the price comparison is "
        "the only thing that catches it"
    )


# ── nothing may spend the broker's budget without telling the limiter ────────
#
# These used to check, call site by call site, that a `rest_limiter.acquire`
# preceded each SDK call. About twenty sites never had one. Metering now lives in
# the transport, so the guarantee is structural; see tests/unit/test_transport.py.
