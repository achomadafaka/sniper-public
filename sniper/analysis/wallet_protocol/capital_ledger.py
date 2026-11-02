"""C1 — capital ledger aggregator.

Closes wallet-protocol-bulletproofing C1 (deposit / withdraw history,
capital base unknowable). Aggregates onchain Polymarket conditional-
tokens events from the public Goldsky activity-subgraph:

  SPLIT       — USDC → outcome tokens. Deposit-equivalent.
  MERGE       — outcome tokens → USDC. Withdrawal-equivalent.
  REDEMPTION  — settlement of resolved condition. Payout in USDC.
                NOT a withdrawal — the wallet does not control timing
                or amount; this is the contract paying out a winning
                position. Tracked separately so consumers can decompose
                "money flowing back to wallet" into voluntary exits
                (merge) vs settlements (redemption).

Net capital funded = deposits - withdrawals. This is the canonical
"capital base" the protocol's I1 DW/EW divergence currently can't
ground (without C1, eatpraylove looks like 1.03% DW vs 24.8% EW with
no way to verify the divisor).

USDC is 6-decimal on Polygon; subgraph stores `BigInt` strings which
we convert to float USDC. At the $1M-scale we still have ~9 sig figs
of precision in float64, so float is acceptable here. If we ever push
to 10^14+ scale this should swap to Decimal.

Collateral-awareness:
Both USDC.e and pUSD are 6-decimal USD-pegged stablecoins used by
Polymarket's CLOB (pre- vs post-2026-04-28 cutover). USDC_SCALE is
correct for both. Events whose collateral resolves to "unknown" (no
timestamp hint and no on-row token field) cannot be reliably scaled
and are EXCLUDED from the USDC totals; they are counted in
`n_non_usdc_collateral_events` and their token symbols recorded in
`non_usdc_collateral_tokens` so callers can flag the gap. This prevents
silent mis-scaling if a future collateral type with different decimals
appears in the subgraph.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import requests

from sniper.analysis.wallet_protocol.subgraph_reader import (
    fetch_merges,
    fetch_redemptions,
    fetch_splits,
)
from sniper.analysis.wallet_protocol.ledger.collateral import resolve_collateral

USDC_DECIMALS = 6
USDC_SCALE = 10 ** USDC_DECIMALS

# Collateral tokens that are confirmed 6-decimal USD-pegged stablecoins.
# USDC_SCALE is correct for all of these. Anything not in this set is
# treated as non-USDC and excluded from USDC totals.
_USDC_COLLATERAL_TOKENS: frozenset[str] = frozenset({"USDC.e", "pUSD"})


@dataclass
class CapitalLedger:
    """Aggregated capital flow for one wallet."""

    wallet: str
    n_splits: int = 0
    n_merges: int = 0
    n_redemptions: int = 0
    total_deposits_usdc: float = 0.0
    total_withdrawals_usdc: float = 0.0
    total_redemption_payout_usdc: float = 0.0
    net_capital_funded_usdc: float = 0.0
    first_event_ts: int | None = None
    last_event_ts: int | None = None
    collateral_summary: dict[str, dict[str, float]] = field(default_factory=dict)
    # Union of all events, sorted by timestamp ascending. Each row is the
    # raw subgraph dict augmented with `kind` ∈ {"split", "merge",
    # "redemption"}, `usdc` (float — converted from BigInt amount/payout),
    # and collateral token attribution.
    events: list[dict[str, Any]] = field(default_factory=list)
    # Count of events whose collateral token could not be identified as a
    # known 6-decimal USD stablecoin. These are excluded from the USDC
    # totals above to avoid silent mis-scaling.
    n_non_usdc_collateral_events: int = 0
    # Distinct non-USDC collateral token symbols encountered (e.g. "unknown"
    # or a future token name). Empty list means all events were USDC-collateral.
    non_usdc_collateral_tokens: list[str] = field(default_factory=list)


def _amount_usdc(amount_str: str) -> float:
    """Convert BigInt 6-decimal string to float dollars.

    Applies USDC_SCALE unconditionally. Callers are responsible for only
    invoking this when the event's collateral is confirmed USDC-denominated
    (i.e. in _USDC_COLLATERAL_TOKENS). For events with unknown collateral,
    the caller should skip accumulation rather than call this.
    """
    try:
        return float(int(amount_str)) / USDC_SCALE
    except (TypeError, ValueError):
        return 0.0


def compute_capital_ledger(
    wallet: str,
    *,
    session: requests.Session | None = None,
) -> CapitalLedger:
    """Fetch + aggregate splits/merges/redemptions into a CapitalLedger.

    Issues three independent paginated subgraph queries (one per entity).
    The fetches honor the standard 1000/page Goldsky cap with id_gt
    cursor pagination.
    """
    sess = session or requests.Session()

    splits = fetch_splits(wallet, session=sess)
    merges = fetch_merges(wallet, session=sess)
    redemptions = fetch_redemptions(wallet, session=sess)

    deposits_total = 0.0
    withdrawals_total = 0.0
    redemption_total = 0.0

    events: list[dict[str, Any]] = []
    collateral_summary: dict[str, dict[str, float]] = {}
    n_non_usdc = 0
    non_usdc_tokens_seen: set[str] = set()

    def _stamp_event(
        row: dict[str, Any],
        *,
        kind: str,
        amount_field: str,
    ) -> tuple[dict[str, Any], float, bool]:
        """Resolve collateral, scale amount if USDC, return (event_dict, usdc, is_usdc).

        Returns `usdc=0.0` and `is_usdc=False` for non-USDC collateral so the
        caller can count but not accumulate them into the headline totals.
        """
        ts = int(row.get("timestamp", 0))
        token, token_address, note = resolve_collateral(row, capture_ts=ts or None)
        token_key = token or "unknown"

        is_usdc = token_key in _USDC_COLLATERAL_TOKENS
        usdc = _amount_usdc(row.get(amount_field, "0")) if is_usdc else 0.0

        bucket = collateral_summary.setdefault(
            token_key,
            {
                "n_events": 0.0,
                "deposits_usdc": 0.0,
                "withdrawals_usdc": 0.0,
                "redemption_payout_usdc": 0.0,
            },
        )
        bucket["n_events"] += 1.0
        if is_usdc:
            if kind == "split":
                bucket["deposits_usdc"] += usdc
            elif kind == "merge":
                bucket["withdrawals_usdc"] += usdc
            elif kind == "redemption":
                bucket["redemption_payout_usdc"] += usdc

        return (
            {
                **row,
                "kind": kind,
                "usdc": usdc,
                "timestamp": ts,
                "collateral_token": token,
                "collateral_token_address": token_address,
                "collateral_source": note.get("collateral_source"),
                "usdc_collateral": is_usdc,
            },
            usdc,
            is_usdc,
        )

    for s in splits:
        stamped, usdc, is_usdc = _stamp_event(s, kind="split", amount_field="amount")
        if is_usdc:
            deposits_total += usdc
        else:
            n_non_usdc += 1
            non_usdc_tokens_seen.add(stamped["collateral_token"] or "unknown")
        events.append(stamped)
    for m in merges:
        stamped, usdc, is_usdc = _stamp_event(m, kind="merge", amount_field="amount")
        if is_usdc:
            withdrawals_total += usdc
        else:
            n_non_usdc += 1
            non_usdc_tokens_seen.add(stamped["collateral_token"] or "unknown")
        events.append(stamped)
    for r in redemptions:
        stamped, usdc, is_usdc = _stamp_event(r, kind="redemption", amount_field="payout")
        if is_usdc:
            redemption_total += usdc
        else:
            n_non_usdc += 1
            non_usdc_tokens_seen.add(stamped["collateral_token"] or "unknown")
        events.append(stamped)

    events.sort(key=lambda e: e["timestamp"])

    first_ts = events[0]["timestamp"] if events else None
    last_ts = events[-1]["timestamp"] if events else None

    return CapitalLedger(
        wallet=wallet.lower(),
        n_splits=len(splits),
        n_merges=len(merges),
        n_redemptions=len(redemptions),
        total_deposits_usdc=deposits_total,
        total_withdrawals_usdc=withdrawals_total,
        total_redemption_payout_usdc=redemption_total,
        net_capital_funded_usdc=deposits_total - withdrawals_total,
        first_event_ts=first_ts,
        last_event_ts=last_ts,
        collateral_summary=collateral_summary,
        events=events,
        n_non_usdc_collateral_events=n_non_usdc,
        non_usdc_collateral_tokens=sorted(non_usdc_tokens_seen),
    )


def capital_summary(
    closed_positions: list[dict[str, Any]],
    open_positions: list[dict[str, Any]],
    *,
    ledger: "CapitalLedger | None" = None,
    holdings_value: float | None = None,
) -> dict[str, Any]:
    """Compute an ADR-004-correct capital summary from position data.

    Uses only the fields that correctly represent cost and value per ADR-004:
      - closed positions: ``realizedPnl`` (settlement P&L)
      - open positions: ``initialValue`` (avgPrice * size = correct cost basis),
        ``currentValue`` (curPrice * size = mark-to-market value),
        ``cashPnl`` (unrealized P&L)

    Explicitly does NOT use ``totalBought`` (re-buy-inflated, can be 80x actual
    cost — see ADR-004). Callers that pass positions with both fields set can
    verify this by checking that ``open_cost_basis`` != sum(totalBought).

    Parameters
    ----------
    closed_positions:
        List of closed-position dicts from the Polymarket /closed-positions API.
        Each dict may carry ``realizedPnl`` (float or string).
    open_positions:
        List of open-position dicts from the Polymarket /positions API.
        Each dict may carry ``initialValue``, ``currentValue``, ``cashPnl``
        (all float or string). ``totalBought`` is ignored even if present.
    ledger:
        Optional CapitalLedger from C1 onchain events. When provided,
        ``net_capital_funded`` is populated from ``ledger.net_capital_funded_usdc``.
    holdings_value:
        Optional authoritative current holdings value from the /value endpoint.
        Passed through unchanged when provided.

    Returns
    -------
    dict with keys:
        realized_pnl          — sum of closed position realizedPnl (float or None)
        unrealized_pnl        — sum of open position cashPnl (float or None)
        open_cost_basis       — sum of open position initialValue (float or None)
        open_current_value    — sum of open position currentValue (float or None)
        net_capital_funded    — from ledger (float) or None if no ledger provided
        holdings_value        — passthrough from argument (float or None)
        data_gaps             — list[str] naming missing data sources
    """
    data_gaps: list[str] = []

    def _to_float(val: Any) -> float | None:
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    def _sum_field(positions: list[dict[str, Any]], field: str) -> float | None:
        """Sum a numeric field; None (+ data gap) if NO position carries it, so a
        missing field reads as unknown rather than a spurious 0.0."""
        vals = [_to_float(p.get(field)) for p in positions]
        valid = [v for v in vals if v is not None]
        if not valid:
            data_gaps.append(f"{field}_missing_on_all({len(positions)})")
            return None
        if len(valid) < len(positions):
            data_gaps.append(f"{field}_partial({len(valid)}/{len(positions)})")
        return sum(valid)

    # Realized P&L from closed positions.
    realized_pnl: float | None = None
    if closed_positions:
        realized_pnl = _sum_field(closed_positions, "realizedPnl")
    else:
        data_gaps.append("no_closed_positions")

    # Open position metrics — using initialValue (ADR-004 correct cost basis).
    unrealized_pnl: float | None = None
    open_cost_basis: float | None = None
    open_current_value: float | None = None
    if open_positions:
        unrealized_pnl = _sum_field(open_positions, "cashPnl")
        open_cost_basis = _sum_field(open_positions, "initialValue")
        open_current_value = _sum_field(open_positions, "currentValue")

    # Net capital funded from onchain ledger.
    net_capital_funded: float | None = None
    if ledger is not None:
        net_capital_funded = ledger.net_capital_funded_usdc
        if ledger.n_non_usdc_collateral_events > 0:
            tokens = ", ".join(ledger.non_usdc_collateral_tokens or ["unknown"])
            data_gaps.append(
                f"non_usdc_collateral_events_excluded({ledger.n_non_usdc_collateral_events}:{tokens})"
            )
    else:
        data_gaps.append("capital_ledger_not_captured")

    if holdings_value is None:
        data_gaps.append("holdings_value_not_provided")

    return {
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "open_cost_basis": open_cost_basis,
        "open_current_value": open_current_value,
        "net_capital_funded": net_capital_funded,
        "holdings_value": holdings_value,
        "data_gaps": data_gaps,
    }


__all__ = [
    "CapitalLedger",
    "USDC_DECIMALS",
    "compute_capital_ledger",
    "capital_summary",
]
