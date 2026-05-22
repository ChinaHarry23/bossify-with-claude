"""Token-to-USD pricing.

Every ingested event carries an optional ``model`` field. Token totals
are priced *per model* because an Opus 4.7 input token costs 5× a
Sonnet 4 one, and the older Opus 4 / 4.1 billed at 3× the price of
today's Opus 4.5+. Applying a single blended rate across a team's
sessions lies to the boss by up to an order of magnitude.

The per-category rates (input / output / cache read / cache write)
reflect Anthropic's published pricing. See https://claude.com/pricing
or https://docs.claude.com/en/docs/about-claude/pricing for the
authoritative table. The 5-minute cache-write rate is used (not the
1-hour one) because Anthropic's streaming usage block reports
``cache_creation_input_tokens`` without distinguishing TTL; 5m is the
default bucket and the conservative pick.

Lookup is substring-based so date-stamped ids resolve to the right
family without a hard-coded id map that'd need a monthly update. Order
in ``_RULES`` matters — more specific prefixes come first so
``claude-opus-4-7-20260401`` hits ``opus-4-7`` before falling through
to a broader ``opus`` rule.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPricing:
    input_per_m: float        # USD per 1M input tokens
    output_per_m: float       # USD per 1M output tokens
    cache_read_per_m: float   # USD per 1M cache-read input tokens
    cache_write_per_m: float  # USD per 1M cache-creation input tokens (5m TTL)


# ---- Anthropic pricing tiers ----
# Numbers are the *5-minute cache write* rate for the cache-write column.
# Anthropic's usage block doesn't distinguish 5m vs 1h in the data we
# ingest, so we pick 5m (cheaper, and the default cache TTL).

# Opus 4.5 / 4.6 / 4.7 — the current Opus generation. Dropped ~3× from
# Opus 4 / 4.1 when Anthropic rebalanced the tier in 2026.
OPUS_CURRENT = ModelPricing(
    input_per_m=5.0, output_per_m=25.0,
    cache_read_per_m=0.50, cache_write_per_m=6.25,
)

# Opus 4 / 4.1 — the older, expensive tier. Still in use for some
# pinned workloads; prices above apply only to 4.5+.
OPUS_LEGACY = ModelPricing(
    input_per_m=15.0, output_per_m=75.0,
    cache_read_per_m=1.50, cache_write_per_m=18.75,
)

# Sonnet 4 / 4.5 / 4.6 / 3.7 — all share the same price sheet.
SONNET_4_X = ModelPricing(
    input_per_m=3.0, output_per_m=15.0,
    cache_read_per_m=0.30, cache_write_per_m=3.75,
)

# Haiku 4.5 — current Haiku generation.
HAIKU_4_5 = ModelPricing(
    input_per_m=1.0, output_per_m=5.0,
    cache_read_per_m=0.10, cache_write_per_m=1.25,
)

# Haiku 3.5 — older Haiku, ~20% cheaper input.
HAIKU_3_5 = ModelPricing(
    input_per_m=0.80, output_per_m=4.0,
    cache_read_per_m=0.08, cache_write_per_m=1.0,
)

# Haiku 3 — legacy, very cheap.
HAIKU_3 = ModelPricing(
    input_per_m=0.25, output_per_m=1.25,
    cache_read_per_m=0.03, cache_write_per_m=0.30,
)

# Kept for back-compat with older call sites. Sonnet 3.5 billed the
# same as Sonnet 4 at the time of deprecation, so the alias just
# points at the Sonnet rate card.
SONNET_3_5 = SONNET_4_X

# Back-compat aliases for the previous naming scheme. Some downstream
# callers import these names directly; keep them pointing at the
# corrected-current prices so a stale import doesn't silently revert
# the fix.
OPUS_4_X = OPUS_CURRENT
HAIKU_4_X = HAIKU_4_5

# Default when a session's ``model`` is missing. Current Opus is the
# conservative pick — under-reporting real Opus spend is worse for a
# boss dashboard than slightly over-reporting Sonnet spend, but the
# old OPUS_LEGACY default ($15/$75) was over-reporting by 3× in
# practice because almost nobody is still on Opus 4.0.
DEFAULT_PRICING = OPUS_CURRENT


# Specific matches come first. "opus-4-7-20260401" must hit "opus-4-7"
# before falling through to "opus", which would bill at legacy rates.
_RULES: tuple[tuple[str, ModelPricing], ...] = (
    # Current Opus generation (4.5 / 4.6 / 4.7). Handle the most specific
    # ids first; fall back to a bare "opus-4-5" / "opus-4-6" etc. that
    # any future date-stamped id will still match.
    ("opus-4-7",     OPUS_CURRENT),
    ("opus-4-6",     OPUS_CURRENT),
    ("opus-4-5",     OPUS_CURRENT),
    # Legacy Opus (4.0, 4.1, 3). Keep these BEFORE the bare "opus" rule
    # so the distinction holds for every id format.
    ("opus-4-1",     OPUS_LEGACY),
    ("opus-4-0",     OPUS_LEGACY),
    ("opus-4",       OPUS_LEGACY),   # matches "claude-opus-4-20240...", "opus-4-0-..."
    ("opus-3",       OPUS_LEGACY),
    ("opus",         OPUS_LEGACY),   # anything else with "opus" → old tier, conservative

    # Sonnet family — all current generations bill identically.
    ("3-7-sonnet",   SONNET_4_X),
    ("3-5-sonnet",   SONNET_4_X),
    ("3.5-sonnet",   SONNET_4_X),
    ("sonnet-4",     SONNET_4_X),
    ("sonnet-3-7",   SONNET_4_X),
    ("sonnet-3-5",   SONNET_4_X),
    ("sonnet-3.5",   SONNET_4_X),
    ("sonnet",       SONNET_4_X),

    # Haiku family.
    ("haiku-4-5",    HAIKU_4_5),
    ("haiku-4",      HAIKU_4_5),     # no other Haiku 4 tier exists yet
    ("haiku-3-5",    HAIKU_3_5),
    ("haiku-3",      HAIKU_3),       # plain Haiku 3 (not 3.5)
    ("haiku",        HAIKU_4_5),     # default to current Haiku
)


def lookup_pricing(model: str | None) -> ModelPricing:
    if not model:
        return DEFAULT_PRICING
    lower = model.lower()
    for key, pricing in _RULES:
        if key in lower:
            return pricing
    return DEFAULT_PRICING


def calculate_usd_cost(
    tokens_in: int,
    tokens_out: int,
    cache_read: int = 0,
    cache_creation: int = 0,
    *,
    pricing: ModelPricing | None = None,
) -> float:
    """Estimated USD cost for a token count under a given pricing table.

    Each category is priced separately at its own rate — you cannot
    just multiply a single "per-token" number by the sum of tokens,
    because cache-read tokens are billed at ~10% of input and cache-
    creation tokens are billed at ~125% of input. Anthropic's usage
    block reports each component separately; we keep them separate.
    """
    p = pricing or DEFAULT_PRICING
    return (
        tokens_in        * p.input_per_m       / 1_000_000
        + tokens_out     * p.output_per_m      / 1_000_000
        + cache_read     * p.cache_read_per_m  / 1_000_000
        + cache_creation * p.cache_write_per_m / 1_000_000
    )


def format_currency(amount: float) -> str:
    """Format a USD amount for display.

    Negatives use accounting parens: ``($5.00)``. Values under 1¢ show
    four decimals so sub-cent amounts don't collapse to ``$0.00``.
    Everything else gets comma thousands-separators so team rollups
    stay readable at $10k+.
    """
    if amount < 0:
        return f"(${-amount:,.2f})"
    if amount < 0.01:
        return f"${amount:.4f}"
    return f"${amount:,.2f}"
