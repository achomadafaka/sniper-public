"""Adapter that wires `scripts.execution_profile.compute_execution_profile`
into the wallet protocol.

Closes wallet-protocol-bulletproofing gap C4. Lets the protocol's CLI inject
an `execution_profile` using activity + closed positions already fetched by
`fetch_wallet_data` plus an externally-determined archetype label.

C4 amendment 2026-04-26 — adversarial review (A3 H4 force=True
investigation, brain memory `feature-study/h4-force-true-eatpraylove-2026-04-26`)
proved that the original adapter's hard-coded `force=True` produced
mechanistically invalid H4 verdicts for `LAYER1_TERMINAL` archetypes
(two_sided_inventory_trader / paired_outcome_arb). Drift-after-BUY on those
archetypes measures bid-ask spread, not predictive edge.

The amended contract:
- `force` defaults to `False`. The legacy gate (`scripts.execution_profile.
  _should_run`) runs naturally and skips terminal-archetype + insufficient-
  activity wallets with `status="not_run"`. The protocol's H4 then reads
  that status and returns `underpowered` — the correct fall-back.
- Callers SHOULD pass `archetype` so the LAYER1_TERMINAL check is meaningful.
  Without it the check is a no-op (`archetype=""` matches no terminal class).
- `closed_positions` populates `returns.resolved_position_count` so the
  insufficient_activity gate fires correctly.
- An explicit `force=True` override is still available for calibration
  workflows that want to bypass the gate; H4 verdicts produced under
  override should not enter the BH pool without an `archetype_mismatch`
  flag (separate gap, tracked in the bulletproofing spec).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Iterable


def build_execution_profile(
    wallet: str,
    activity: Iterable[dict],
    closed_positions: Sequence[dict] | None = None,
    *,
    archetype: str | None = None,
    client: Any | None = None,
    max_trades: int = 500,
    force: bool = False,
) -> dict:
    """Compute execution_profile for a wallet using already-fetched data.

    Args:
        wallet: 0x-prefixed wallet address.
        activity: iterable of /activity?type=TRADE row dicts. Eagerly
            realized into a list before being passed to compute_execution_profile,
            which calls len() and re-iterates internally.
        closed_positions: list of /closed-positions rows. Used only to populate
            the gate's `returns.resolved_position_count` so insufficient_activity
            fires correctly. Pass `[]` if unknown — the gate then relies on
            trade_records alone.
        archetype: Layer-1 archetype label. When set, the gate's
            LAYER1_TERMINAL check is meaningful. When None, the check is a
            no-op (`archetype=""` matches no terminal class).
        client: PolymarketClient or compatible; constructed lazily when None.
        max_trades: bounds per-fill prices-history calls. Default 500 matches
            the legacy extractor's `activity_limit`.
        force: when True, every gate check is bypassed regardless of inputs.
            DEFAULT FALSE. Use only for calibration/QA — H4 verdicts produced
            under force=True for LAYER1_TERMINAL archetypes are not valid
            entry-timing evidence (mechanistic spread capture, not edge).

    Returns:
        Dict with at minimum a 'status' key. Mirrors compute_execution_profile's
        own return contract (status='populated' | 'insufficient_sample' |
        'api_failure' | 'not_run' | …).
    """
    from scripts.execution_profile import compute_execution_profile

    if client is None:
        from data.polymarket_client import PolymarketClient
        client = PolymarketClient()

    activity_list = list(activity)
    classification = {"archetype": archetype} if archetype else None
    counts = {
        "trade_records": sum(
            1 for r in activity_list
            if str(r.get("type") or "").upper() == "TRADE"
        ),
    }
    returns_dict = {
        "resolved_position_count": len(closed_positions or []),
    }
    return compute_execution_profile(
        wallet,
        activity_list,
        client=client,
        classification=classification,
        counts=counts,
        returns=returns_dict,
        force=force,
        max_trades=max_trades,
    )


__all__ = ["build_execution_profile"]
