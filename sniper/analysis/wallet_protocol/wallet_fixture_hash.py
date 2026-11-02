"""Deterministic supersession key derivation for `WalletData`.

Lives outside `ledger/` so the analyzer can import it without breaking
the analyze_wallet ↔ ledger separation boundary (enforced by
`tests/test_wallet_protocol_position_building.py::test_phase3_no_ledger_imports_in_analyze_wallet_or_report`).

The writer in `ledger.flipper_tag_grain_writer` re-exports the same
function via its public API.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


def _sha256_hex16(payload: bytes) -> str:
    """SHA-256 truncated to 16 hex chars. Mirrors the ledger writer's
    helper so identical inputs produce identical hashes regardless of
    which entry point computed them."""
    return hashlib.sha256(payload).hexdigest()[:16]


def _activity_row_id(row: Mapping[str, Any]) -> str:
    """Deterministic ID for an activity row. Goldsky/REST rows carry
    `transaction_hash` / `transactionHash` / `id`. Fall back to a
    content hash so unfamiliar shapes still produce a stable ID."""
    for k in ("id", "transaction_hash", "transactionHash", "txn_hash"):
        v = row.get(k) if hasattr(row, "get") else None
        if v:
            return f"{k}:{v}"
    return _sha256_hex16(
        json.dumps(row, sort_keys=True, default=str).encode("utf-8")
    )


def compute_wallet_fixture_hash_from_data(data: Any) -> str:
    """Live-mode hash derivation over `WalletData`. The analyzer calls
    this once and stamps the result onto `ProtocolReport.wallet_fixture_hash`.

    Excludes wall-clock fields (`fetched_at`, `analyzed_at`) so two
    runs over identical data produce the same hash. Activity row IDs
    are sorted, so order changes don't perturb the fingerprint.

    Raises:
        ValueError: when nothing usable is on `data` to fingerprint —
            no activity rows, no closed positions, no open positions.
    """
    wallet = (getattr(data, "wallet", "") or "").lower()
    closed = list(getattr(data, "closed_positions", None) or [])
    open_positions = list(getattr(data, "open_positions", None) or [])
    activity_rows = list(getattr(data, "activity_rows", None) or [])

    row_ids = sorted(
        _activity_row_id(r) for r in activity_rows if isinstance(r, Mapping)
    )

    if not row_ids and not closed and not open_positions:
        raise ValueError(
            "WalletData has no activity rows, closed positions, or open "
            "positions; cannot derive a stable wallet_fixture_hash"
        )

    fingerprint = json.dumps(
        {
            "wallet": wallet,
            "activity_row_ids": row_ids,
            "n_closed_positions": len(closed),
            "n_open_positions": len(open_positions),
        },
        sort_keys=True,
        default=str,
    )
    return _sha256_hex16(fingerprint.encode("utf-8"))


__all__ = [
    "compute_wallet_fixture_hash_from_data",
]
