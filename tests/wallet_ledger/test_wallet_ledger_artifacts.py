"""Slice 6 Task 6 — artifact registry writer tests.

`artifact_registry` is append-only on `content_hash`. Two paths to the
same content do not produce duplicate rows; instead the second call
PATCHes the row's `paths` JSONB array to include the new path. New
content always gets a new row.

* Allowed `kind` set: `wallet_warehouse`, `market_warehouse`,
  `fixture_export`, `report_blob`, `csv_export`. Unknown kind raises.
* `mark_superseded(table='artifact_registry', ...)` raises (D3) — that
  invariant is verified in `test_wallet_ledger_findings_writer.py` and
  re-stated here for orthogonality.
"""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from sniper.analysis.wallet_protocol.ledger.artifacts import (
    upsert_artifact_registry_entry,
)
from sniper.analysis.wallet_protocol.ledger.findings import mark_superseded
from sniper.analysis.wallet_protocol.ledger.supabase_io import StubSupabaseHttp


REPO_ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS_FILE = (
    REPO_ROOT
    / "sniper"
    / "analysis"
    / "wallet_protocol"
    / "ledger"
    / "artifacts.py"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_blob(tmp_path: Path, name: str, payload: bytes) -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(payload)
    return p


# ---------------------------------------------------------------------------
# Insert + content-hash
# ---------------------------------------------------------------------------


def test_insert_computes_sha256_and_writes_one_row(tmp_path: Path) -> None:
    blob = _write_blob(tmp_path, "wallet.duckdb", b"wallet-warehouse-bytes")
    expected = hashlib.sha256(b"wallet-warehouse-bytes").hexdigest()
    stub = StubSupabaseHttp()

    n = upsert_artifact_registry_entry(
        blob,
        kind="wallet_warehouse",
        supabase=stub,
    )
    assert n == 1
    rows = stub.rows_by_table["artifact_registry"]
    assert len(rows) == 1
    row = rows[0]
    assert row["content_hash"] == expected
    assert row["kind"] == "wallet_warehouse"
    assert row["byte_count"] == len(b"wallet-warehouse-bytes")
    assert row["paths"] == [str(blob)]


def test_explicit_content_hash_overrides_sha_compute(tmp_path: Path) -> None:
    blob = _write_blob(tmp_path, "report.json", b"any-bytes")
    stub = StubSupabaseHttp()

    upsert_artifact_registry_entry(
        blob,
        kind="report_blob",
        supabase=stub,
        content_hash="explicit-hash",
    )
    rows = stub.rows_by_table["artifact_registry"]
    assert rows[0]["content_hash"] == "explicit-hash"


# ---------------------------------------------------------------------------
# Idempotency on content_hash
# ---------------------------------------------------------------------------


def test_same_content_same_path_is_a_full_noop(tmp_path: Path) -> None:
    blob = _write_blob(tmp_path, "wallet.duckdb", b"identical")
    stub = StubSupabaseHttp()

    first = upsert_artifact_registry_entry(
        blob, kind="wallet_warehouse", supabase=stub
    )
    second = upsert_artifact_registry_entry(
        blob, kind="wallet_warehouse", supabase=stub
    )
    assert first == 1
    assert second == 0
    rows = stub.rows_by_table["artifact_registry"]
    assert len(rows) == 1
    assert rows[0]["paths"] == [str(blob)]
    # No PATCH needed — the path was already in the array.
    patch_calls = [c for c in stub.calls if c[0] == "patch"]
    assert patch_calls == []


def test_same_content_different_path_patches_paths_array(
    tmp_path: Path,
) -> None:
    blob_a = _write_blob(tmp_path / "a", "wallet.duckdb", b"identical")
    blob_b = _write_blob(tmp_path / "b", "wallet.duckdb", b"identical")
    # Two paths, same content -> same content_hash.
    stub = StubSupabaseHttp()

    first = upsert_artifact_registry_entry(
        blob_a, kind="wallet_warehouse", supabase=stub
    )
    second = upsert_artifact_registry_entry(
        blob_b, kind="wallet_warehouse", supabase=stub
    )

    assert first == 1
    assert second == 0  # No new row — same content_hash.
    rows = stub.rows_by_table["artifact_registry"]
    assert len(rows) == 1
    assert sorted(rows[0]["paths"]) == sorted([str(blob_a), str(blob_b)])

    patch_calls = [c for c in stub.calls if c[0] == "patch"]
    # Exactly one PATCH against the row's content_hash.
    assert len(patch_calls) == 1
    table, payload = patch_calls[0][1], patch_calls[0][2]
    assert table == "artifact_registry"
    assert payload["where"] == {
        "content_hash": rows[0]["content_hash"]
    }
    assert "paths" in payload["set"]


def test_different_content_creates_a_new_row(tmp_path: Path) -> None:
    blob_a = _write_blob(tmp_path, "a.json", b"AAA")
    blob_b = _write_blob(tmp_path, "b.json", b"BBB")
    stub = StubSupabaseHttp()

    upsert_artifact_registry_entry(
        blob_a, kind="report_blob", supabase=stub
    )
    upsert_artifact_registry_entry(
        blob_b, kind="report_blob", supabase=stub
    )
    rows = stub.rows_by_table["artifact_registry"]
    assert len(rows) == 2
    assert {r["content_hash"] for r in rows} == {
        hashlib.sha256(b"AAA").hexdigest(),
        hashlib.sha256(b"BBB").hexdigest(),
    }


# ---------------------------------------------------------------------------
# Allowed kinds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        "wallet_warehouse",
        "market_warehouse",
        "fixture_export",
        "report_blob",
        "csv_export",
    ],
)
def test_allowed_kinds_accepted(kind: str, tmp_path: Path) -> None:
    blob = _write_blob(tmp_path, f"{kind}.bin", b"x")
    stub = StubSupabaseHttp()
    upsert_artifact_registry_entry(blob, kind=kind, supabase=stub)
    rows = stub.rows_by_table["artifact_registry"]
    assert rows[0]["kind"] == kind


def test_unknown_kind_raises(tmp_path: Path) -> None:
    blob = _write_blob(tmp_path, "x.bin", b"x")
    stub = StubSupabaseHttp()
    with pytest.raises(ValueError):
        upsert_artifact_registry_entry(
            blob, kind="not_a_real_kind", supabase=stub
        )


# ---------------------------------------------------------------------------
# Append-only invariant (D3)
# ---------------------------------------------------------------------------


def test_mark_superseded_artifact_registry_raises_value_error() -> None:
    """Cross-check from the artifact-side test file. The reject is
    spec'd in D3 and pinned in the writer-side test_findings_writer
    file too — restated here so an artifact-only refactor that broke
    the invariant would still flag this test."""
    stub = StubSupabaseHttp()
    with pytest.raises(ValueError):
        mark_superseded(
            table="artifact_registry",
            natural_key={"content_hash": "deadbeef"},
            superseded_by="x",
            supabase=stub,
        )


# ---------------------------------------------------------------------------
# Module guard
# ---------------------------------------------------------------------------


def test_artifacts_module_does_not_import_live_supabase_modules() -> None:
    tree = ast.parse(ARTIFACTS_FILE.read_text())
    forbidden_roots = {"bot", "requests", "httpx"}
    forbidden_modules = {"urllib.request"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                assert root not in forbidden_roots
                assert alias.name not in forbidden_modules
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".", 1)[0]
            assert root not in forbidden_roots
            assert node.module not in forbidden_modules
