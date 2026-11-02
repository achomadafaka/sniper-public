"""Flipper Inference Phase 1.2 — flipper-branch tag-grain persistence writer.

Persists `ProtocolReport.flipper_branch.tag_grains[]` to the Supabase
`wallet_flipper_tag_grain` table created in migration
`supabase_021_wallet_flipper_tag_grain.sql`.

Spec: `docs/superpowers/specs/2026-05-15-flipper-inference-phase1-persistence-design.md`.

Two public entry points:

    compute_wallet_fixture_hash(report, fixture_path=None)
        Deterministic content hash. Same data → same hash, regardless of
        wall-clock. Fixture mode hashes the file bytes; live mode hashes
        a canonical content fingerprint over the wallet's activity row
        IDs + counts + protocol_version.

    persist_flipper_tag_grains(report, *, supabase, fixture_path=None)
        Writes one row per (grain, tag) from the flipper branch's
        tag_grains. Marks any prior active rows for the same wallet (any
        prior fixture_hash) as superseded. Returns a summary dict.

The writer is **opt-in** at the analyzer-run level — `run_wallet_protocol.py`
only invokes it when `--persist-flipper-grains` is passed. Default off.

Pre-write guards (per spec §3.3):
  - flipper_branch.is_active must be True
  - walk_full_history must be True
  - Per-grain: n_at_grain ≥ 1
  - Phase 1.1 schema must exist (probe at write time)
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from sniper.analysis.wallet_protocol.ledger.supabase_io import SupabaseHttp
from sniper.analysis.wallet_protocol.wallet_fixture_hash import (
    compute_wallet_fixture_hash_from_data,
)


_TABLE = "wallet_flipper_tag_grain"
_PHASE3_PROBE_COLUMNS = ("entry_type",)  # kept as a marker — irrelevant here


# ----------------------------------------------------------------------
# wallet_fixture_hash (spec §3.1)
# ----------------------------------------------------------------------

def _sha256_hex16(payload: bytes) -> str:
    """SHA-256, truncated to 16 hex chars (64 bits) — enough collision-
    resistance for the per-wallet supersession key while remaining
    human-friendly in row inspections."""
    return hashlib.sha256(payload).hexdigest()[:16]


# NOTE: compute_wallet_fixture_hash_from_data lives in
# sniper.analysis.wallet_protocol.wallet_fixture_hash so that
# analyze_wallet can import it without breaking the
# analyze_wallet ↔ ledger separation guard. Re-exported via __all__
# below for backward compat.


def compute_wallet_fixture_hash(
    report: Any,
    fixture_path: str | Path | None = None,
) -> str:
    """Return the deterministic supersession key for this analyzer run.

    Fixture mode: sha256 of the fixture file bytes. Same fixture →
    same hash forever.

    Live mode: read `report.wallet_fixture_hash`, which `analyze_wallet`
    stamps from `WalletData` via `compute_wallet_fixture_hash_from_data`.
    Tests that construct `ProtocolReport` directly must stamp the hash
    themselves (see compute_wallet_fixture_hash_from_data).

    Raises:
        ValueError: if the fixture path does not exist, or if the report
            has no stamped hash.
    """
    if fixture_path is not None:
        p = Path(fixture_path)
        if not p.is_file():
            raise ValueError(
                f"fixture_path {p} does not exist on disk; cannot derive "
                f"wallet_fixture_hash"
            )
        return _sha256_hex16(p.read_bytes())

    stamped = getattr(report, "wallet_fixture_hash", None)
    if stamped:
        return str(stamped)

    raise ValueError(
        "report has no wallet_fixture_hash stamp and no fixture_path "
        "was provided. Either run via analyze_wallet (which stamps the "
        "hash from WalletData) or stamp the hash manually using "
        "compute_wallet_fixture_hash_from_data(data)."
    )


# ----------------------------------------------------------------------
# Writer
# ----------------------------------------------------------------------

@dataclass
class WriteResult:
    """Summary returned by persist_flipper_tag_grains."""

    wallet: str
    wallet_fixture_hash: str
    rows_written: int
    rows_superseded: int
    skipped_reason: str | None = None  # populated when no write happened


def _grain_row(
    *,
    wallet: str,
    grain: Any,
    wallet_fixture_hash: str,
    flip_rate: float,
    walk_full_history: bool,
    analyzed_at_iso: str,
    protocol_version: str,
    code_version: str,
) -> dict[str, Any]:
    """Build one wallet_flipper_tag_grain row dict from a TagGrainResult."""
    return {
        "wallet": wallet.lower(),
        "grain": grain["grain"] if isinstance(grain, Mapping) else grain.grain,
        "tag": grain["tag"] if isinstance(grain, Mapping) else grain.tag,
        "wallet_fixture_hash": wallet_fixture_hash,
        "n_at_grain": int(
            grain["n_at_grain"] if isinstance(grain, Mapping) else grain.n_at_grain
        ),
        "grain_roi": float(
            grain["grain_roi"] if isinstance(grain, Mapping) else grain.grain_roi
        ),
        "grain_win_rate": float(
            grain["grain_win_rate"]
            if isinstance(grain, Mapping)
            else grain.grain_win_rate
        ),
        "grain_total_pnl": float(
            grain["grain_total_pnl"]
            if isinstance(grain, Mapping)
            else grain.grain_total_pnl
        ),
        "grain_max_realized_loss": _maybe_float(
            grain.get("grain_max_realized_loss")
            if isinstance(grain, Mapping)
            else getattr(grain, "grain_max_realized_loss", None)
        ),
        "flip_rate": float(flip_rate),
        "walk_full_history": bool(walk_full_history),
        "analyzed_at": analyzed_at_iso,
        "protocol_version": protocol_version,
        "code_version": code_version,
        "status": "active",
    }


def _maybe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _iter_flipper_grains(report: Any) -> Iterable[Any]:
    """Yield each TagGrainResult-like object from report.flipper_branch."""
    branch = getattr(report, "flipper_branch", None)
    if branch is None:
        return
    grains = getattr(branch, "tag_grains", None)
    if isinstance(branch, Mapping):
        grains = branch.get("tag_grains")
    yield from (grains or [])


def persist_flipper_tag_grains(
    report: Any,
    *,
    supabase: SupabaseHttp,
    fixture_path: str | Path | None = None,
    code_version: str = "unknown",
) -> WriteResult:
    """Write per-grain rows for the analyzer run; supersede prior rows.

    Pre-write skip checks (per spec §3.3):
      - flipper_branch.is_active must be True
      - walk_full_history must be True
      - flipper_branch must have at least one grain with n_at_grain ≥ 1

    Behavior:
      1. Compute wallet_fixture_hash (deterministic content hash).
      2. SELECT any active rows at the SAME fixture_hash — if any
         exist, the write is a no-op (idempotent on identical data).
      3. PATCH any OTHER prior active rows (different fixture_hash)
         to status='superseded'. Must run BEFORE the insert so the
         partial unique index `idx_wftg_one_active_per_wgt` doesn't
         silently swallow the new rows.
      4. INSERT all current grain rows with status='active'.

    Returns:
        WriteResult with rows_written + rows_superseded counts.
        On skip, rows_written=0 and skipped_reason is set.
    """
    wallet = (getattr(report, "wallet", "") or "").lower()
    if not wallet:
        return WriteResult(
            wallet="", wallet_fixture_hash="", rows_written=0, rows_superseded=0,
            skipped_reason="report.wallet is empty",
        )

    flipper = getattr(report, "flipper_branch", None)
    if flipper is None or not getattr(flipper, "is_active", False):
        return WriteResult(
            wallet=wallet, wallet_fixture_hash="", rows_written=0, rows_superseded=0,
            skipped_reason="flipper_branch is None or inactive",
        )

    if not getattr(report, "walk_full_history", False):
        return WriteResult(
            wallet=wallet, wallet_fixture_hash="", rows_written=0, rows_superseded=0,
            skipped_reason="walk_full_history is False",
        )

    # Compute the supersession key
    wallet_fixture_hash = compute_wallet_fixture_hash(report, fixture_path)

    # Build rows — skip per-grain rows with n_at_grain == 0
    analyzed_at = getattr(report, "analyzed_at", 0)
    analyzed_at_iso = _epoch_to_iso(analyzed_at)
    flip_rate = float(getattr(report, "flip_rate", 0.0) or 0.0)
    protocol_version = getattr(report, "protocol_version", "unknown")

    rows: list[dict[str, Any]] = []
    for grain_obj in _iter_flipper_grains(report):
        n = int(
            grain_obj["n_at_grain"]
            if isinstance(grain_obj, Mapping)
            else grain_obj.n_at_grain
        )
        if n < 1:
            continue
        rows.append(_grain_row(
            wallet=wallet,
            grain=grain_obj,
            wallet_fixture_hash=wallet_fixture_hash,
            flip_rate=flip_rate,
            walk_full_history=getattr(report, "walk_full_history", False),
            analyzed_at_iso=analyzed_at_iso,
            protocol_version=protocol_version,
            code_version=code_version,
        ))

    if not rows:
        return WriteResult(
            wallet=wallet, wallet_fixture_hash=wallet_fixture_hash,
            rows_written=0, rows_superseded=0,
            skipped_reason="no flipper grains with n_at_grain >= 1",
        )

    # 2. Check existing same-hash active rows. Idempotency is keyed on
    # the (grain, tag) key SET — not on any-row presence. The pre-fix
    # behavior bailed on any same-hash row, so a partial prior write
    # (interrupted, partial-unique race, schema cache flap) left the
    # missing rows missing forever — same hash means same content per
    # the hash function, but the row set on disk can still be partial.
    # Reviewer finding #4 (2026-05-17).
    existing_same_hash = supabase.select(
        _TABLE,
        where={"wallet": wallet, "wallet_fixture_hash": wallet_fixture_hash, "status": "active"},
        columns=("wallet", "grain", "tag"),
    )
    existing_same_hash_keys = {
        (r["grain"], r["tag"]) for r in existing_same_hash
    }
    candidate_keys = {(r["grain"], r["tag"]) for r in rows}

    if existing_same_hash and candidate_keys <= existing_same_hash_keys:
        # Full coverage — every candidate (grain, tag) already exists at
        # this hash. True idempotent no-op.
        return WriteResult(
            wallet=wallet, wallet_fixture_hash=wallet_fixture_hash,
            rows_written=0, rows_superseded=0,
            skipped_reason=(
                f"idempotent — {len(existing_same_hash)} rows already exist "
                f"at this hash covering all {len(candidate_keys)} candidate keys"
            ),
        )

    # Partial coverage (or no coverage). Submit only the missing rows so
    # the present rows are not re-attempted. The partial unique index
    # would block re-inserts anyway, but skipping them upstream keeps
    # the rows_written count honest and avoids needless DB chatter.
    missing_rows = [
        r for r in rows
        if (r["grain"], r["tag"]) not in existing_same_hash_keys
    ]

    # 3. PATCH any OTHER active rows for this wallet (different
    # fixture_hash) to status='superseded' BEFORE inserting. The
    # migration's partial unique index `idx_wftg_one_active_per_wgt`
    # forbids two active rows for the same (wallet, grain, tag);
    # inserting first would silently drop the new rows via
    # `INSERT ... ON CONFLICT DO NOTHING`, leaving the wallet
    # frozen at the old fixture_hash with zero new data persisted.
    # Same-hash rows are explicitly NOT superseded — they're correct
    # content per the hash, and superseding then re-inserting would
    # produce churn for zero semantic change.
    prior_active = supabase.select(
        _TABLE,
        where={"wallet": wallet, "status": "active"},
        columns=("wallet", "grain", "tag", "wallet_fixture_hash"),
    )
    prior_to_supersede = [
        r for r in prior_active
        if r.get("wallet_fixture_hash") != wallet_fixture_hash
    ]
    rows_superseded = 0
    for prior in prior_to_supersede:
        # Include `status="active"` in the where clause so an already-
        # superseded row at the same (wallet, grain, tag, hash) is not
        # re-touched. Without it, a re-run that encounters a prior
        # superseded row at the same key counts it as superseded again,
        # inflating rows_superseded. Tested by
        # test_partial_same_hash_recovery_with_pending_different_hash_supersession.
        rows_superseded += supabase.patch(
            _TABLE,
            where={
                "wallet": prior["wallet"],
                "grain": prior["grain"],
                "tag": prior["tag"],
                "wallet_fixture_hash": prior["wallet_fixture_hash"],
                "status": "active",
            },
            set_columns={
                "status": "superseded",
                "superseded_by_fixture_hash": wallet_fixture_hash,
            },
        )

    # 4. INSERT the missing rows. Natural key for insert_ignore is
    # (wallet, grain, tag, wallet_fixture_hash). Idempotent on re-run.
    rows_written = supabase.insert_ignore(
        _TABLE, missing_rows,
        natural_key_columns=("wallet", "grain", "tag", "wallet_fixture_hash"),
    )

    return WriteResult(
        wallet=wallet,
        wallet_fixture_hash=wallet_fixture_hash,
        rows_written=rows_written,
        rows_superseded=rows_superseded,
    )


def _epoch_to_iso(epoch: Any) -> str:
    """Convert an integer epoch second to an ISO-8601 UTC timestamp.
    Falls back to 1970-01-01 if input is non-numeric/zero."""
    try:
        ts = int(epoch or 0)
    except (TypeError, ValueError):
        ts = 0
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat()


__all__ = [
    "compute_wallet_fixture_hash",
    "compute_wallet_fixture_hash_from_data",
    "persist_flipper_tag_grains",
    "WriteResult",
]
