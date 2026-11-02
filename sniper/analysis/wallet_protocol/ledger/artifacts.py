"""Slice 6 Task 6 — append-only artifact registry writer.

`upsert_artifact_registry_entry(path, *, kind, supabase,
content_hash=None)` is the only callsite. Behaviour:

* Compute sha256 over file bytes when `content_hash` is None.
* Insert-ignore on `content_hash` (the table's primary key, D2).
* When the natural key already exists and the new `path` is NOT yet
  in the row's `paths` array, PATCH the row to append the new path.
  The same-content / same-path call is a full no-op (no PATCH).
* Append-only: there is no `status` column, no supersede semantics.
  `mark_superseded(table='artifact_registry', ...)` raises (enforced
  in `findings.py`).

Allowed `kind` values are pinned at the table CHECK constraint and
re-stated here so a typo at the call site fails offline.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from sniper.analysis.wallet_protocol.ledger.supabase_io import SupabaseHttp


_TABLE = "artifact_registry"

ArtifactKind = Literal[
    "wallet_warehouse",
    "market_warehouse",
    "fixture_export",
    "report_blob",
    "csv_export",
]
_ALLOWED_KINDS: frozenset[str] = frozenset(
    {
        "wallet_warehouse",
        "market_warehouse",
        "fixture_export",
        "report_blob",
        "csv_export",
    }
)


def upsert_artifact_registry_entry(
    path: Path,
    *,
    kind: str,
    supabase: SupabaseHttp,
    content_hash: str | None = None,
) -> int:
    """Register an artifact at `path`.

    Returns 1 when a NEW row is inserted, 0 otherwise (no-op or paths
    PATCH). Two paths to the same content always collapse to one row;
    the second call PATCHes the row's `paths` array to include the new
    path.
    """
    if kind not in _ALLOWED_KINDS:
        raise ValueError(
            f"unknown artifact kind: {kind!r}; "
            f"allowed: {sorted(_ALLOWED_KINDS)}"
        )

    digest = content_hash or _sha256_file(path)
    payload_path = str(path)
    byte_count = _byte_count_or_none(path)

    inserted = supabase.insert_ignore(
        _TABLE,
        [
            {
                "content_hash": digest,
                "kind": kind,
                "paths": [payload_path],
                "byte_count": byte_count,
            }
        ],
        natural_key_columns=("content_hash",),
    )
    if inserted:
        return 1

    # Conflict on `content_hash`. Read the existing row's paths and
    # append the new one if absent.
    existing = supabase.select(
        _TABLE,
        where={"content_hash": digest},
        columns=("content_hash", "paths"),
    )
    if not existing:
        # Row was reported as a conflict yet is not visible — fall
        # through silently. The next pass re-attempts insert.
        return 0
    current_paths = list(existing[0].get("paths") or [])
    if payload_path in current_paths:
        return 0

    new_paths = current_paths + [payload_path]
    supabase.patch(
        _TABLE,
        where={"content_hash": digest},
        set_columns={
            "paths": new_paths,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return 0


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _byte_count_or_none(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


__all__ = [
    "ArtifactKind",
    "upsert_artifact_registry_entry",
]
