"""Market identity safety.

A market's identity (condition/market/token ids, window, series) is **immutable after first
confirmation**. If Gamma ever reports a different value for one of these, that is an anomaly to
quarantine and review — never a silent overwrite. Operational fields (active/closed/accepting/fees/
priceToBeat/raw payload) update normally. This narrows the Phase-0 round-1 fix that updated *all*
fields on conflict.
"""
from __future__ import annotations

# Frozen after first insert. A change here emits MARKET_IDENTITY_CHANGED and is NOT overwritten.
# ``slug`` is identity: its suffix IS window_start_ts (see market_resolver.window_from_slug).
IMMUTABLE_FIELDS = (
    "condition_id", "market_id", "up_token_id", "down_token_id",
    "window_start_ts", "window_end_ts", "series_id", "slug",
)

# Safe to update on every upsert. MUST stay in sync with the upsert_market ON CONFLICT DO UPDATE SET
# clause (a drift guard test asserts equality).
OPERATIONAL_FIELDS = (
    "question", "price_to_beat", "fees_enabled", "fee_type", "taker_base_fee",
    "maker_base_fee", "tick_size", "min_order_size", "active", "closed", "accepting_orders",
    "enable_order_book", "raw_payload", "code_version", "last_updated_ts",
)


def diff_identity(existing: dict, spec) -> list:
    """Return ``[(field, old, new), ...]`` for immutable fields whose value changed.

    ``existing`` is a stored ``crypto5m_markets`` row (dict-like); ``spec`` is the incoming
    ``MarketSpec``. Values are compared as strings to avoid int/str storage mismatches.
    """
    out = []
    for f in IMMUTABLE_FIELDS:
        old = existing.get(f)
        new = getattr(spec, f, None)
        if old is None:
            continue
        if str(old) != str(new):
            out.append((f, old, new))
    return out
