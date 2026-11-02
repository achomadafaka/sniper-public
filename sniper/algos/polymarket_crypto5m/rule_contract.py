"""Deterministic ``rule_supported`` validator for the BTC 5-minute Up/Down market contract.

Supported contract (locked):
  * series identity = the BTC-5m series (id 10684),
  * a present condition id,
  * exactly two outcomes ("Up", "Down") with exactly two distinct token ids,
  * resolution source = the Chainlink BTC/USD data stream (EXACT, not a prefix),
  * rule text asserts Up wins when end >= start,
  * a 5-minute window (``window_end - window_start == 300``).

This gate answers ONLY "is this the instrument we understand?". Whether the market is live
(``capture_ready``) and whether we have a start reference (price-to-beat quality) are separate gates.
``priceToBeat`` is deliberately not an input here. Bump ``RULE_CONTRACT_VERSION`` on any change.
"""
from __future__ import annotations

from typing import Optional

from .models import RuleReason, RuleVerdict

RULE_CONTRACT_VERSION = 2  # v2: 3-gate split, exact source, series identity, exactly-2-tokens

BTC_5M_SERIES_ID = "10684"
# exact normalized allowlist (a versioned/foreign suffix like 'btc-usd-v2' must NOT pass)
_SUPPORTED_SOURCES = frozenset({"https://data.chain.link/streams/btc-usd"})
WINDOW_DURATION_S = 300
_REQUIRED_RULE_PHRASE = "greater than or equal"  # guards against a ">=" -> ">" operator flip
EXPECTED_OUTCOMES = ("Up", "Down")


def outcome_for(*, start_price: float, end_price: float) -> str:
    """Chainlink settlement rule: Up wins when end >= start (a flat window resolves Up)."""
    return "Up" if float(end_price) >= float(start_price) else "Down"


def _normalize_source(src: Optional[str]) -> str:
    """Strip a harmless trailing slash and query/fragment so an exact match is robust."""
    s = (src or "").strip()
    for sep in ("?", "#"):
        s = s.split(sep, 1)[0]
    return s.rstrip("/")


def _verdict(reason: RuleReason, *, detail: str = "") -> RuleVerdict:
    return RuleVerdict(
        reason=reason,
        rule_supported=reason is RuleReason.SUPPORTED,
        rule_contract_version=RULE_CONTRACT_VERSION,
        detail=detail,
    )


def validate_rule(
    *,
    series_id: Optional[str],
    condition_id: Optional[str],
    outcomes,
    resolution_source: Optional[str],
    rule_text: Optional[str],
    window_start_ts: Optional[int],
    window_end_ts: Optional[int],
    up_token_id: Optional[str],
    down_token_id: Optional[str],
    token_count: Optional[int],
) -> RuleVerdict:
    """Validate the contract. Failures are reported in identity-first order (see tests)."""

    # 1. series identity — a direct slug fetch must not accept a look-alike copying the rule text
    if str(series_id or "") != BTC_5M_SERIES_ID:
        return _verdict(RuleReason.WRONG_SERIES, detail=f"series_id={series_id!r}")

    # 2. condition id present (persistence key; a default "" would collide / be unusable)
    if not (condition_id or "").strip():
        return _verdict(RuleReason.MISSING_CONDITION_ID)

    # 3. outcome shape
    if tuple(outcomes or ()) != EXPECTED_OUTCOMES:
        return _verdict(RuleReason.UNEXPECTED_OUTCOME_SHAPE, detail=f"outcomes={outcomes!r}")

    # 4. resolution source — EXACT normalized match, not startswith
    if _normalize_source(resolution_source) not in _SUPPORTED_SOURCES:
        return _verdict(RuleReason.UNSUPPORTED_SOURCE, detail=f"source={resolution_source!r}")

    # 5. rule text asserts the end>=start => Up semantics
    if _REQUIRED_RULE_PHRASE not in (rule_text or "").lower():
        return _verdict(RuleReason.UNSUPPORTED_RULE, detail="rule text missing '>=' semantics")

    # 6. window is exactly 5 minutes
    if (window_start_ts is None or window_end_ts is None
            or int(window_end_ts) - int(window_start_ts) != WINDOW_DURATION_S):
        return _verdict(RuleReason.MALFORMED_WINDOW, detail=f"start={window_start_ts} end={window_end_ts}")

    # 7. exactly two distinct token ids (do NOT silently take the first two of a 3-token market)
    if (token_count != 2 or not up_token_id or not down_token_id or up_token_id == down_token_id):
        return _verdict(RuleReason.MISSING_TOKEN_MAPPING,
                        detail=f"n={token_count} up={up_token_id!r} down={down_token_id!r}")

    return _verdict(RuleReason.SUPPORTED)
