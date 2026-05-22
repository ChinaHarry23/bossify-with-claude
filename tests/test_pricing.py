"""Pricing lookup + cost-formatting tests.

Cost math is load-bearing for the boss view — a silently wrong pricing
table would mis-report team spend by up to an order of magnitude when
sessions mix Opus and Sonnet, or when the Opus generation changes rate
cards. These tests pin the per-model rates against Anthropic's
published pricing and the formatting conventions.
"""
from __future__ import annotations

import pytest

from token_roi.pricing import (
    DEFAULT_PRICING,
    HAIKU_3,
    HAIKU_3_5,
    HAIKU_4_5,
    HAIKU_4_X,
    OPUS_4_X,
    OPUS_CURRENT,
    OPUS_LEGACY,
    SONNET_3_5,
    SONNET_4_X,
    calculate_usd_cost,
    format_currency,
    lookup_pricing,
)


def test_opus_current_uses_published_rates():
    """Opus 4.5/4.6/4.7 bill at $5 / $25 / $0.50 / $6.25 per MTok.

    Anthropic dropped this tier ~3× from Opus 4.0/4.1. An earlier
    version of this module pinned OPUS_4_X to the legacy $15/$75 rate,
    which over-reported real team spend by ~3×. Lock the new rates in.
    """
    assert OPUS_CURRENT.input_per_m       == 5.0
    assert OPUS_CURRENT.output_per_m      == 25.0
    assert OPUS_CURRENT.cache_read_per_m  == 0.50
    assert OPUS_CURRENT.cache_write_per_m == 6.25


def test_opus_legacy_is_triple_of_current():
    """Opus 4 / 4.1 / 3 billed at $15 / $75 — exactly 3× Opus 4.5+."""
    assert OPUS_LEGACY.input_per_m       == 15.0
    assert OPUS_LEGACY.output_per_m      == 75.0
    assert OPUS_LEGACY.cache_read_per_m  == 1.50
    assert OPUS_LEGACY.cache_write_per_m == 18.75


def test_sonnet_rate_card():
    assert SONNET_4_X.input_per_m       == 3.0
    assert SONNET_4_X.output_per_m      == 15.0
    assert SONNET_4_X.cache_read_per_m  == 0.30
    assert SONNET_4_X.cache_write_per_m == 3.75


def test_haiku_4_5_rate_card():
    assert HAIKU_4_5.input_per_m       == 1.0
    assert HAIKU_4_5.output_per_m      == 5.0
    assert HAIKU_4_5.cache_read_per_m  == 0.10
    assert HAIKU_4_5.cache_write_per_m == 1.25


def test_haiku_3_5_rate_card():
    assert HAIKU_3_5.input_per_m       == 0.80
    assert HAIKU_3_5.output_per_m      == 4.0
    assert HAIKU_3_5.cache_read_per_m  == 0.08
    assert HAIKU_3_5.cache_write_per_m == 1.0


def test_haiku_3_rate_card():
    assert HAIKU_3.input_per_m       == 0.25
    assert HAIKU_3.output_per_m      == 1.25
    assert HAIKU_3.cache_read_per_m  == 0.03
    assert HAIKU_3.cache_write_per_m == 0.30


def test_lookup_resolves_opus_generations_separately():
    """The critical correctness bug: Opus 4.7 must NOT pick up the
    legacy $15/$75 rate and Opus 4.0 must NOT pick up the current
    $5/$25 rate. Mix-up would mis-report by 3×."""
    # Current Opus: 4.5 / 4.6 / 4.7 (date-stamped or bare).
    assert lookup_pricing("claude-opus-4-7-20260401") is OPUS_CURRENT
    assert lookup_pricing("claude-opus-4-6")          is OPUS_CURRENT
    assert lookup_pricing("claude-opus-4-5")          is OPUS_CURRENT
    # Legacy Opus: 4.0 / 4.1 / 3.
    assert lookup_pricing("claude-opus-4-1-20250514") is OPUS_LEGACY
    assert lookup_pricing("claude-opus-4-0")          is OPUS_LEGACY
    assert lookup_pricing("claude-opus-3")            is OPUS_LEGACY


def test_lookup_resolves_sonnet_variants():
    assert lookup_pricing("claude-sonnet-4-6")        is SONNET_4_X
    assert lookup_pricing("claude-sonnet-4")          is SONNET_4_X
    assert lookup_pricing("claude-3-7-sonnet")        is SONNET_4_X
    assert lookup_pricing("claude-3-5-sonnet-20241022") is SONNET_3_5  # alias → SONNET_4_X
    assert lookup_pricing("claude-3.5-sonnet")        is SONNET_3_5


def test_lookup_resolves_haiku_generations_separately():
    assert lookup_pricing("claude-haiku-4-5-20251001") is HAIKU_4_5
    assert lookup_pricing("claude-haiku-3-5")          is HAIKU_3_5
    assert lookup_pricing("claude-haiku-3")            is HAIKU_3


def test_lookup_default_when_missing_or_unknown():
    # Default is current Opus (safe conservative pick — under-reporting
    # real Opus is worse than over-reporting unknowns).
    assert lookup_pricing(None) is DEFAULT_PRICING
    assert lookup_pricing("") is DEFAULT_PRICING
    assert lookup_pricing("gpt-4") is DEFAULT_PRICING
    assert DEFAULT_PRICING is OPUS_CURRENT


def test_backcompat_aliases_point_at_corrected_prices():
    """OPUS_4_X and HAIKU_4_X are legacy import names kept around so
    stale imports don't silently revert to a WRONG price. They must
    point at the current (corrected) rate cards."""
    assert OPUS_4_X is OPUS_CURRENT
    assert HAIKU_4_X is HAIKU_4_5


def test_calculate_usd_cost_applies_per_category_rate():
    # 1M input tokens under Sonnet 4.x should equal input_per_m exactly.
    c = calculate_usd_cost(
        tokens_in=1_000_000, tokens_out=0,
        cache_read=0, cache_creation=0,
        pricing=SONNET_4_X,
    )
    assert c == pytest.approx(SONNET_4_X.input_per_m)

    # All four categories summed — check linearity.
    c = calculate_usd_cost(
        tokens_in=1_000_000, tokens_out=500_000,
        cache_read=2_000_000, cache_creation=100_000,
        pricing=SONNET_4_X,
    )
    expected = (
        SONNET_4_X.input_per_m
        + SONNET_4_X.output_per_m * 0.5
        + SONNET_4_X.cache_read_per_m * 2
        + SONNET_4_X.cache_write_per_m * 0.1
    )
    assert c == pytest.approx(expected)


def test_opus_current_bills_above_sonnet_but_below_legacy_opus():
    """Same tokens, three pricing tiers → legacy Opus > current Opus
    > Sonnet. This is the whole point of having separate rate cards."""
    tokens = dict(tokens_in=1_000_000, tokens_out=500_000,
                  cache_read=200_000, cache_creation=50_000)
    sonnet = calculate_usd_cost(**tokens, pricing=SONNET_4_X)
    opus   = calculate_usd_cost(**tokens, pricing=OPUS_CURRENT)
    legacy = calculate_usd_cost(**tokens, pricing=OPUS_LEGACY)
    assert sonnet < opus < legacy
    # Current Opus is ~1.67× Sonnet (5/3 input, 25/15 output).
    assert 1.5 < (opus / sonnet) < 2.0
    # Legacy Opus is exactly 3× current Opus for every category.
    assert legacy / opus == pytest.approx(3.0, rel=1e-6)


def test_format_currency_handles_three_bands():
    # Sub-cent shows four decimals so fractional spend is visible.
    assert format_currency(0.0005) == "$0.0005"
    # Normal range: two decimals, comma thousands.
    assert format_currency(1234567.891) == "$1,234,567.89"
    assert format_currency(42.0) == "$42.00"
    # Negative renders in accounting parens.
    assert format_currency(-5.0) == "($5.00)"


def test_calculate_cost_with_zero_tokens():
    assert calculate_usd_cost(0, 0, 0, 0, pricing=OPUS_CURRENT) == 0.0
