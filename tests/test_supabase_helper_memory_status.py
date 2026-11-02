from __future__ import annotations

import pytest

import bot.supabase_helper as sb


def test_read_memories_filters_active_by_default(monkeypatch):
    calls = []

    def fake_request(method, path, body=None, extra_headers=None, timeout=10):
        calls.append((method, path, body))
        return []

    monkeypatch.setattr(sb, "_request", fake_request)

    sb.read_memories(type_filter="knowledge")

    assert calls[0][0] == "GET"
    assert "type=eq.knowledge" in calls[0][1]
    assert "status=eq.active" in calls[0][1]


def test_read_memories_can_include_inactive(monkeypatch):
    calls = []

    def fake_request(method, path, body=None, extra_headers=None, timeout=10):
        calls.append(path)
        return []

    monkeypatch.setattr(sb, "_request", fake_request)

    sb.read_memories(active_only=False)

    assert "status=eq.active" not in calls[0]


def test_search_memories_filters_active_by_default(monkeypatch):
    calls = []

    def fake_request(method, path, body=None, extra_headers=None, timeout=10):
        calls.append(path)
        return []

    monkeypatch.setattr(sb, "_request", fake_request)

    sb.search_memories("country:")

    assert "status=eq.active" in calls[0]


def test_search_memories_fts_uses_rpc(monkeypatch):
    calls = []

    def fake_request(method, path, body=None, extra_headers=None, timeout=10):
        calls.append((method, path, body))
        return [{"name": "finding/x"}]

    monkeypatch.setattr(sb, "_request", fake_request)

    rows = sb.search_memories_fts("memecoin exit latency", limit=25)

    assert calls[0][0] == "POST"
    assert calls[0][1] == "rpc/search_memories_fts"
    assert calls[0][2] == {"q": "memecoin exit latency", "active_only": True, "max_results": 25}
    assert rows == [{"name": "finding/x"}]


def test_search_memories_fts_falls_back_to_substring(monkeypatch):
    """If the FTS RPC is unavailable, fall back to substring search_memories()
    (which uses ilike) instead of raising — callers always get a best-effort result."""
    calls = []

    def fake_request(method, path, body=None, extra_headers=None, timeout=10):
        calls.append((method, path))
        if method == "POST":
            raise RuntimeError("HTTP 404 rpc/search_memories_fts not found")
        return [{"name": "session/foo"}]

    monkeypatch.setattr(sb, "_request", fake_request)

    rows = sb.search_memories_fts("session/", active_only=True)

    assert calls[0][0] == "POST"
    assert any(m == "GET" and "ilike" in p for m, p in calls)
    assert rows == [{"name": "session/foo"}]


def test_semantic_search_uses_match_memories_rpc(monkeypatch):
    import bot.embeddings as emb
    monkeypatch.setattr(emb, "embed_query", lambda q: [0.1, 0.2, 0.3])
    calls = []

    def fake_request(method, path, body=None, extra_headers=None, timeout=10):
        calls.append((method, path, body))
        return [{"name": "finding/x", "similarity": 0.9}]

    monkeypatch.setattr(sb, "_request", fake_request)

    rows = sb.semantic_search_memories("overfitting backtests", limit=7)

    assert calls[0][0] == "POST"
    assert calls[0][1] == "rpc/match_memories"
    assert calls[0][2]["match_count"] == 7
    assert calls[0][2]["active_only"] is True
    assert calls[0][2]["query_embedding"].startswith("[")
    assert rows[0]["name"] == "finding/x"


def test_semantic_search_falls_back_to_fts_when_embed_unavailable(monkeypatch):
    import bot.embeddings as emb

    def boom(q):
        raise RuntimeError("model not seeded")

    monkeypatch.setattr(emb, "embed_query", boom)
    calls = []

    def fake_request(method, path, body=None, extra_headers=None, timeout=10):
        calls.append((method, path))
        return [{"name": "session/foo"}]

    monkeypatch.setattr(sb, "_request", fake_request)

    rows = sb.semantic_search_memories("overfitting")

    assert all("match_memories" not in p for _, p in calls)
    assert any("search_memories_fts" in p for _, p in calls)
    assert rows == [{"name": "session/foo"}]


def test_semantic_search_falls_back_to_fts_when_rpc_fails(monkeypatch):
    import bot.embeddings as emb
    monkeypatch.setattr(emb, "embed_query", lambda q: [0.1, 0.2])

    def fake_request(method, path, body=None, extra_headers=None, timeout=10):
        if path == "rpc/match_memories":
            raise RuntimeError("HTTP 404 match_memories not found")
        return [{"name": "session/bar"}]

    monkeypatch.setattr(sb, "_request", fake_request)

    rows = sb.semantic_search_memories("x")

    assert rows == [{"name": "session/bar"}]


def test_upsert_memory_defaults_status_and_commit(monkeypatch):
    calls = []

    monkeypatch.setattr(sb, "get_memory", lambda name, type_filter=None, active_only=True: None)
    monkeypatch.setattr(sb, "_current_git_sha", lambda: "abc123")

    def fake_request(method, path, body=None, extra_headers=None, timeout=10):
        calls.append((method, path, body))
        return [body]

    monkeypatch.setattr(sb, "_request", fake_request)

    result = sb.upsert_memory("knowledge", "x", "desc", "content", source="agent")

    assert result["status"] == "active"
    assert result["superseded_by"] is None
    assert result["valid_as_of_commit"] == "abc123"
    assert calls[0][0] == "POST"
    assert calls[0][2]["status"] == "active"


def test_valid_statuses_covers_full_ksg_lifecycle():
    """Helper VALID_STATUSES must cover the full KSG lifecycle vocabulary.

    Drift between this set and scripts/knowledge_sync.VALID_STATUSES (or the
    Supabase memories_status_check constraint) lets validators accept
    statuses the writer/DB then rejects. Pin the contract here.
    """
    assert sb.VALID_STATUSES == {
        "active", "superseded", "retired",
        "draft", "archived", "rejected", "needs_review",
    }


@pytest.mark.parametrize(
    "status", ["draft", "archived", "rejected", "needs_review"],
)
def test_upsert_memory_accepts_widened_statuses(monkeypatch, status):
    """Every new lifecycle status must pass helper validation and reach the
    POST body. Regression target: previously these raised ValueError before
    ever touching Supabase."""
    monkeypatch.setattr(sb, "get_memory", lambda name, type_filter=None, active_only=True: None)
    monkeypatch.setattr(sb, "_current_git_sha", lambda: "deadbeef")
    bodies = []

    def fake_request(method, path, body=None, extra_headers=None, timeout=10):
        bodies.append(body)
        return [body] if body else []

    monkeypatch.setattr(sb, "_request", fake_request)

    result = sb.upsert_memory(
        "knowledge", f"x-{status}", "desc", "content",
        source="agent", status=status,
    )

    assert result["status"] == status
    assert bodies[0]["status"] == status


def test_upsert_memory_still_rejects_unknown_status(monkeypatch):
    """Widening must not become anything-goes — unknown statuses still raise."""
    monkeypatch.setattr(sb, "get_memory", lambda name, type_filter=None, active_only=True: None)
    monkeypatch.setattr(sb, "_current_git_sha", lambda: "abc")
    monkeypatch.setattr(sb, "_request", lambda *a, **kw: [{}])

    with pytest.raises(ValueError, match="status must be one of"):
        sb.upsert_memory(
            "knowledge", "x-bogus", "desc", "content",
            source="agent", status="totally-fake-status",
        )


def test_save_finding_preserves_lifecycle_metadata(monkeypatch):
    captured = {}

    def fake_upsert(**kwargs):
        captured.update(kwargs)
        return kwargs

    # save_finding routes through the KSG-aware writer now, not upsert_memory.
    monkeypatch.setattr(sb, "upsert_ksg_entry", fake_upsert)

    sb.save_finding(
        "brain-hygiene/test",
        "desc",
        "content",
        tags=["hygiene", "test"],
        status="superseded",
        superseded_by="replacement",
        valid_as_of_commit="abc123",
    )

    assert captured["name"] == "finding/brain-hygiene/test"
    assert captured["source"] == "agent"
    assert captured["status"] == "superseded"
    assert captured["superseded_by"] == "replacement"
    assert captured["valid_as_of_commit"] == "abc123"
    # KSG fields now preserved (the legacy path silently dropped these)
    assert captured["entry_type"] == "finding"
    assert captured["slug"] == "brain-hygiene-test"
    assert captured["tags"] == ["hygiene", "test"]


def test_semantic_search_threads_timeout(monkeypatch):
    import bot.embeddings as emb
    monkeypatch.setattr(emb, "embed_query", lambda q: [0.1, 0.2])
    seen = {}

    def fake_request(method, path, body=None, extra_headers=None, timeout=10):
        seen["timeout"] = timeout
        return [{"name": "finding/x", "similarity": 0.9}]

    monkeypatch.setattr(sb, "_request", fake_request)
    sb.semantic_search_memories("q", limit=3, timeout=3)
    assert seen["timeout"] == 3
