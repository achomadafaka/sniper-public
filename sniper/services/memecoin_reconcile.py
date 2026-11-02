"""Item 17 (min bar) — real position vs on-chain reconciliation.

Each real exit sweep, compare the ledger's expected token quantity (open_qty from fills) to the
wallet's actual on-chain balance for that mint. A divergence beyond tolerance means the ledger and
chain disagree — a missed sell, a manual move, a mis-recorded partial, or a position we think we
hold but don't (can't exit what isn't there). Surface it as DRIFT and alert. Detection is the min
bar before arming; the full auto-heal loop comes before raising past 1 concurrent.

`token_balance_fn(mint) -> float` is injected so this is unit-tested without a live endpoint. Pass a
reader that RAISES on a failed read (solana_wallet.token_balance_checked /
evm_wallet.erc20_balance_checked) — the plain readers return 0.0 on error, which this check would
otherwise report as a total drift. Only execution_mode='real' positions are checked.
"""
from __future__ import annotations

import logging

from sniper.persistence import db_memecoin as m

logger = logging.getLogger(__name__)


def reconcile_real_positions(conn, token_balance_fn=None, *, balance_fn=None, alert_fn=None,
                             tolerance_pct=5.0, network_ids=None, exclude_network_ids=()):
    """Return findings [{position_id, mint, expected, actual, status}] and (best-effort) alert each
    genuine drift. tolerance_pct absorbs dust / rounding; anything larger is real drift.

    `status` is "drift" (ledger and a SUCCESSFUL chain read disagree — alerted) or "unreadable" (the
    balance read itself failed — NOT alerted, re-checked next pass). Never conflate the two: the
    readers report 0.0 on any RPC error, so an unreadable balance looks identical to an emptied
    wallet.

    Balance reads are venue-specific, so callers spanning both legs pass a position-aware
    `balance_fn(position) -> float` (routes Solana vs EVM by network_id). `token_balance_fn(mint)`
    remains for the single-venue (Solana) caller.

    `network_ids` (only these) and `exclude_network_ids` (all but these) split the book between
    callers that own different chains (the Ethereum worker owns chain 1). Exclusion keeps a network
    with inventory but no context on the excluding caller, where it stays 'unreadable', not silent."""
    reader = balance_fn or (lambda pos: token_balance_fn(pos["mint"]))
    findings = []
    for pos in m.list_open_positions(conn, mode="real"):
        if pos.get("tracking_paused_at"):
            continue
        network = m.normalized_coin_network(pos.get("network_id"), pos.get("mint"))
        if (network_ids is not None and network not in network_ids) or network in exclude_network_ids:
            continue
        expected = m.open_qty(conn, pos["id"])
        if expected <= 0:
            continue
        try:
            actual = float(reader(pos))
        except Exception as exc:
            # A FAILED read is not evidence of drift. The balance readers collapse any RPC error to
            # 0.0, so treating an exception (or that zero) as drift pages the operator that a fully
            # held position is 100% gone every time the RPC rate-limits — which is how this check
            # taught itself to be ignored (2026-07-29, pos#23496: ledger 39,178.307636 vs an actual
            # on-chain 39,178.307637, alerted as total drift while both RPCs returned 429). Surface
            # it as unverified and re-read next pass; callers must not alert drift on these.
            # ponytail: no consecutive-failure escalation — a persistent RPC outage is already
            # surfaced by the provider-level quote alerts. Add one here if that stops being true.
            logger.warning("reconcile: balance unreadable for pos=%s mint=%s (%s: %s)",
                           pos["id"], pos["mint"], type(exc).__name__, exc)
            findings.append({"position_id": pos["id"], "mint": pos["mint"],
                             "expected": expected, "actual": None, "status": "unreadable"})
            continue
        if abs(actual - expected) > (tolerance_pct / 100.0) * expected:
            findings.append({"position_id": pos["id"], "mint": pos["mint"],
                             "expected": expected, "actual": actual, "status": "drift"})
            if alert_fn:
                try:
                    alert_fn(pos["mint"], pos["id"])
                except Exception:
                    pass
    return findings
