"""Relationships between the engine's gate thresholds that must hold for the funnel
to work. Each value is individually plausible and silently wrong in combination.

Invariants that relate an engine threshold to a strategy's own parameter (the G9
bypass against a strategy's stretch floor, say) belong to that strategy's tests,
in the private strategies/ plug-in, since the strategy's numbers live there.

Measured on the 2-4 Sep 2026 sessions, which is where these values came from.
"""
from __future__ import annotations

import pytest

from shortcircuit import config


def test_gain_window_is_a_window():
    assert config.SCANNER_GAIN_MIN_PCT < config.SCANNER_GAIN_MAX_PCT, (
        "the scanner would admit nothing"
    )


def test_gain_ceiling_stays_clear_of_the_common_circuit_band():
    """A short that goes limit-up cannot be covered, so this must stay under 20%.

    C0 in back_to_vwap checks distance from the broker's real upper_ckt, but it only
    runs when the analyzer actually read that value: the depth fetch is wrapped in a
    bare `except: pass`, so on failure upper_circuit stays 0.0 and C0's circuit branch
    is skipped. This ceiling is the backstop for that case, which is why it is NOT
    redundant with C0 and must not be raised to "let big movers through".

    Raising it to 25.0 on 2026-09-04 was wrong and reverted the same day:
    NSE:JINDWORLD-EQ peaked at 19.83% that session, effectively pinned at a 20% band.
    """
    assert config.SCANNER_GAIN_MAX_PCT < 20.0, (
        "NSE upper circuits are commonly 20%; entering above this risks a short "
        "that cannot be covered because the stock is locked limit-up"
    )
    assert config.SCANNER_GAIN_MAX_PCT > config.SCANNER_GAIN_MIN_PCT


def test_body_ratio_is_a_fraction_of_the_candle_range():
    assert 0.0 < config.CANDLE_BODY_RATIO_MIN < 1.0


def test_body_ratio_does_not_sit_on_top_of_the_population():
    """0.382 was chosen for being a Fibonacci number, not from measurement.

    Over 2-4 Sep it produced 155 quality rejections, 98 of them at 0.34 or better —
    including NSE:ANTELOPUS-EQ at 0.38, which was then dropped for the rest of the
    day while it ran to +17%. Violent movers are wick-heavy by construction.
    """
    assert config.CANDLE_BODY_RATIO_MIN <= 0.35, (
        "a floor above ~0.35 rejects the wick-heavy candles that violent movers "
        "produce, which is the setup this strategy exists to trade"
    )


@pytest.mark.parametrize("name,expected", [
    ("SCANNER_GAIN_MAX_PCT", 18.0),
    ("CANDLE_BODY_RATIO_MIN", 0.34),
    ("P61_G9_BYPASS_SD_THRESHOLD", 4.0),
])
def test_the_2026_09_04_values_are_deliberate(name, expected):
    """Pins the engine values changed together on 2026-09-04 (the strategy's
    stretch floor, changed with them, is pinned in its own plug-in's tests).

    They are recorded here so a later edit is a decision rather than a drift. If you
    are changing one, change this test in the same commit and say why.
    """
    assert getattr(config, name) == expected
