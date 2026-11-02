"""Tests for `bot.supabase_helper.upsert_ksg_entry`.

Pins the KSG-aware upsert path that `scripts/knowledge_sync.py` uses in
commit mode. The legacy `upsert_memory` silently drops the Phase 3
metadata columns (`entry_type`, `tags`, `source_branch`, `source_pr`,
`source_files`, `supersedes_ids`, `valid_to_commit`, `confidence`,
`reviewed_by`, `reviewed_at`, `metadata`); these tests verify the new
function preserves every one of them, supports the read-then-PATCH/POST
idempotency contract, and falls back gracefully when the target
Supabase instance lacks the Phase 3 schema.

Codex deep-review finding #4.
"""
from __future__ import annotations

import pytest

import bot.supabase_helper as sb


def _capture_request_calls(monkeypatch):
    """Helper: monkeypatch `sb._request` with a recording stub and
    return the call log."""
    calls: list[tuple[str, str, dict | None]] = []

    def fake_request(method, path, body=None, extra_headers=None):
        calls.append((method, path, dict(body) if body else body))
        return [body] if body else []

    monkeypatch.setattr(sb, "_request", fake_request)
    return calls


def test_upsert_ksg_entry_writes_all_metadata_columns(monkeypatch):
    """Every Phase 3 column the migration added must land in the POST
    body. Regression for the bug where KSG validators accepted entries
    but persistence stripped the metadata."""
    monkeypatch.setattr(
        sb, "get_memory",
        lambda name, type_filter=None, active_only=True: None,
    )
    monkeypatch.setattr(sb, "_current_git_sha", lambda: "deadbeef")
    calls = _capture_request_calls(monkeypatch)

    result = sb.upsert_ksg_entry(
        type="knowledge",
        name="finding/test/example",
        description="example",
        content="body",
        source="agent",
        entry_type="finding",
        tags=["wallet-protocol", "fh4"],
        source_branch="feat/something",
        source_pr=42,
        source_files=["sniper/foo.py", "tests/test_foo.py"],
        supersedes_ids=["11111111-1111-1111-1111-111111111111"],
        valid_to_commit=None,
        confidence="high",
        reviewed_by="achomadafaka",
        reviewed_at="2026-05-16T12:00:00Z",
        metadata={"phase": "1.2", "shipped": True},
        summary="one-line summary",
        slug="example",
    )

    assert len(calls) == 1
    method, path, body = calls[0]
    assert method == "POST"
    assert path == "memories"

    # Every Phase 3 column must be in the body.
    assert body["entry_type"] == "finding"
    assert body["tags"] == ["wallet-protocol", "fh4"]
    assert body["source_branch"] == "feat/something"
    assert body["source_pr"] == 42
    assert body["source_files"] == ["sniper/foo.py", "tests/test_foo.py"]
    assert body["supersedes_ids"] == ["11111111-1111-1111-1111-111111111111"]
    assert body["confidence"] == "high"
    assert body["reviewed_by"] == "achomadafaka"
    assert body["reviewed_at"] == "2026-05-16T12:00:00Z"
    assert body["metadata"] == {"phase": "1.2", "shipped": True}
    assert body["summary"] == "one-line summary"
    assert body["slug"] == "example"

    # Legacy columns also preserved.
    assert body["type"] == "knowledge"
    assert body["status"] == "active"
    assert body["valid_as_of_commit"] == "deadbeef"

    # Return shape includes the KSG fields.
    assert result["entry_type"] == "finding"
    assert result["tags"] == ["wallet-protocol", "fh4"]


def test_upsert_ksg_entry_omits_none_ksg_fields(monkeypatch):
    """None-valued KSG fields must NOT serialize into the body — that
    would clobber prior values on PATCH. Only explicitly-provided
    fields land in the payload."""
    monkeypatch.setattr(
        sb, "get_memory",
        lambda name, type_filter=None, active_only=True: None,
    )
    monkeypatch.setattr(sb, "_current_git_sha", lambda: "abc123")
    calls = _capture_request_calls(monkeypatch)

    sb.upsert_ksg_entry(
        type="knowledge",
        name="finding/foo",
        description="d",
        content="c",
        source="agent",
        entry_type="finding",
        # All other KSG fields omitted → None.
    )

    body = calls[0][2]
    assert body["entry_type"] == "finding"  # the one we set
    for col in (
        "tags", "source_branch", "source_pr", "source_files",
        "supersedes_ids", "valid_to_commit", "confidence",
        "reviewed_by", "reviewed_at", "metadata", "summary", "slug",
    ):
        assert col not in body, f"None-valued KSG column {col!r} leaked into payload"


def test_upsert_ksg_entry_patches_existing_row(monkeypatch):
    """When a row with the same (type, name) already exists, the upsert
    must PATCH it (not duplicate). Mirrors `upsert_memory` behavior."""
    existing = {
        "id": "row-123",
        "type": "knowledge",
        "name": "finding/foo",
        "description": "old",
        "content": "old body",
    }
    monkeypatch.setattr(
        sb, "get_memory",
        lambda name, type_filter=None, active_only=True: existing,
    )
    monkeypatch.setattr(sb, "_current_git_sha", lambda: "abc123")
    calls = _capture_request_calls(monkeypatch)

    sb.upsert_ksg_entry(
        type="knowledge",
        name="finding/foo",
        description="new",
        content="new body",
        source="agent",
        entry_type="finding",
        tags=["x"],
    )

    method, path, body = calls[0]
    assert method == "PATCH"
    assert path == "memories?id=eq.row-123"
    assert body["entry_type"] == "finding"
    assert body["tags"] == ["x"]
    assert body["description"] == "new"


def test_upsert_ksg_entry_falls_back_when_phase3_columns_missing(monkeypatch):
    """If the target instance hasn't applied
    `supabase_020_memories_ksg_extension.sql`, the first POST fails
    with a `column "entry_type" does not exist` error. The function
    must retry with a legacy-shape payload (no KSG columns) so partial
    deployments don't hard-fail."""
    monkeypatch.setattr(
        sb, "get_memory",
        lambda name, type_filter=None, active_only=True: None,
    )
    monkeypatch.setattr(sb, "_current_git_sha", lambda: "abc123")

    attempt_count = {"n": 0}
    calls: list[tuple[str, str, dict | None]] = []

    def fake_request(method, path, body=None, extra_headers=None):
        attempt_count["n"] += 1
        calls.append((method, path, dict(body) if body else body))
        if attempt_count["n"] == 1:
            # Simulate PostgREST schema-cache error for KSG column.
            raise RuntimeError(
                "PGRST204: column \"entry_type\" of relation memories "
                "does not exist in schema cache"
            )
        return [body]

    monkeypatch.setattr(sb, "_request", fake_request)

    result = sb.upsert_ksg_entry(
        type="knowledge",
        name="finding/foo",
        description="d",
        content="c",
        source="agent",
        entry_type="finding",
        tags=["x"],
        confidence="high",
    )

    # Two attempts: full payload (failed), retry with KSG fields stripped.
    assert attempt_count["n"] == 2
    first_body = calls[0][2]
    assert "entry_type" in first_body
    assert "tags" in first_body
    assert "confidence" in first_body

    retry_body = calls[1][2]
    for col in (
        "slug", "entry_type", "summary", "tags",
        "source_branch", "source_pr", "source_files",
        "supersedes_ids", "valid_to_commit",
        "confidence", "reviewed_by", "reviewed_at", "metadata",
    ):
        assert col not in retry_body, (
            f"fallback retry still carried KSG column {col!r}"
        )
    # Legacy + status fields preserved.
    assert retry_body["status"] == "active"
    assert retry_body["type"] == "knowledge"

    # Return reflects the fallback shape (no KSG columns).
    assert "entry_type" not in result


def test_upsert_ksg_entry_validates_type_status_source(monkeypatch):
    """Validation must match `upsert_memory`'s vocabularies."""
    monkeypatch.setattr(
        sb, "get_memory",
        lambda name, type_filter=None, active_only=True: None,
    )
    monkeypatch.setattr(sb, "_request", lambda *a, **kw: [{}])
    monkeypatch.setattr(sb, "_current_git_sha", lambda: "abc123")

    # Bad type
    import pytest
    with pytest.raises(ValueError, match="type must be one of"):
        sb.upsert_ksg_entry(
            type="bogus", name="n", description="d", content="c",
        )

    # Bad status
    with pytest.raises(ValueError, match="status must be one of"):
        sb.upsert_ksg_entry(
            type="knowledge", name="n", description="d", content="c",
            status="not-a-real-status",
        )

    # Bad source
    with pytest.raises(ValueError, match="source must be"):
        sb.upsert_ksg_entry(
            type="knowledge", name="n", description="d", content="c",
            source="external-host",
        )


@pytest.mark.parametrize(
    "status", ["draft", "archived", "rejected", "needs_review"],
)
def test_upsert_ksg_entry_accepts_widened_statuses(monkeypatch, status):
    """Each new lifecycle status must reach the POST body via upsert_ksg_entry.

    Previously these all raised ValueError before any Supabase call;
    knowledge_sync validators allowed them but the writer rejected them,
    so any draft entry produced a runtime error at commit time. The
    widened helper closes that gap.
    """
    monkeypatch.setattr(
        sb, "get_memory",
        lambda name, type_filter=None, active_only=True: None,
    )
    monkeypatch.setattr(sb, "_current_git_sha", lambda: "deadbeef")
    bodies = []

    def fake_request(method, path, body=None, extra_headers=None):
        bodies.append(body)
        return [body] if body else []

    monkeypatch.setattr(sb, "_request", fake_request)

    result = sb.upsert_ksg_entry(
        type="knowledge",
        name=f"finding/widened/{status}",
        description="desc",
        content="content",
        source="agent",
        entry_type="finding",
        status=status,
    )

    assert result["status"] == status
    assert bodies[0]["status"] == status


def test_legacy_upsert_memory_unchanged(monkeypatch):
    """The legacy `upsert_memory` must remain a narrow function — no
    new KSG columns in its payload — so existing callers (brain
    hygiene scripts, ad-hoc memory writers) keep their current shape."""
    monkeypatch.setattr(
        sb, "get_memory",
        lambda name, type_filter=None, active_only=True: None,
    )
    monkeypatch.setattr(sb, "_current_git_sha", lambda: "abc")
    calls = _capture_request_calls(monkeypatch)

    sb.upsert_memory(
        "knowledge", "name", "desc", "content", source="agent",
    )

    body = calls[0][2]
    # Sanity: only legacy columns present.
    for col in (
        "entry_type", "tags", "source_branch", "source_pr",
        "source_files", "supersedes_ids", "valid_to_commit",
        "confidence", "reviewed_by", "reviewed_at", "metadata",
        "summary", "slug",
    ):
        assert col not in body
