"""Tests for `BotSupabaseAdapter.select` Range-header pagination.

PostgREST caps a single response at `db-max-rows` (default 1000). The
old adapter issued one GET with no Range header — anything beyond that
cap was silently truncated. Once `wallet_flipper_tag_grain` grows past
1000 active rows the cached FH4 baseline would undercount peers
without error.

Codex deep-review finding #5.

These tests stub `BotSupabaseAdapter._helper._request` to simulate
paginated responses and assert:

  * The adapter issues multiple GETs with incrementing Range headers
    when the table exceeds one page.
  * Stops on the first partial page (natural end-of-data signal).
  * Concatenates pages in order.
  * Raises rather than silently truncating when `max_rows` is exceeded.
  * Single-page queries don't trigger extra requests.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _fake_supabase_env(monkeypatch):
    """The session-wide conftest nulls SUPABASE_* env to block live
    writes. Re-set fake values for this file so BotSupabaseAdapter's
    constructor can build (it raises on empty URL/KEY). The
    `_request` calls are all monkey-patched per test, so no live
    network is reachable from these fake creds."""
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "fake-key")


def _make_adapter():
    from sniper.analysis.wallet_protocol.ledger.bot_supabase_adapter import (
        BotSupabaseAdapter,
    )
    return BotSupabaseAdapter()


def _row(i: int) -> dict:
    return {"id": f"row-{i:05d}", "n": i}


def test_select_returns_single_page_unchanged(monkeypatch):
    """A query that fits in one page (< _PAGE_SIZE rows) must result
    in exactly ONE GET. No extra Range-header walks."""
    adapter = _make_adapter()
    calls: list[tuple[str, str, dict | None]] = []

    def fake_request(method, path, body=None, extra_headers=None):
        calls.append((method, path, dict(extra_headers or {})))
        return [_row(i) for i in range(50)]

    monkeypatch.setattr(adapter._helper, "_request", fake_request)

    result = adapter.select("some_table", where={"status": "active"})

    assert len(result) == 50
    assert len(calls) == 1
    assert calls[0][2]["Range"] == "0-999"


def test_select_walks_pages_until_partial(monkeypatch):
    """Walk Range pages until a page returns < _PAGE_SIZE rows."""
    adapter = _make_adapter()
    calls: list[tuple[str, str, dict | None]] = []

    pages: list[list[dict]] = [
        [_row(i) for i in range(0, 1000)],
        [_row(i) for i in range(1000, 2000)],
        [_row(i) for i in range(2000, 2300)],  # partial
    ]
    page_iter = iter(pages)

    def fake_request(method, path, body=None, extra_headers=None):
        calls.append((method, path, dict(extra_headers or {})))
        return next(page_iter)

    monkeypatch.setattr(adapter._helper, "_request", fake_request)

    result = adapter.select(
        "wallet_flipper_tag_grain",
        where={"status": "active", "protocol_version": "v2"},
    )

    assert len(result) == 2300
    # Result is concatenation of pages in order.
    assert result[0]["id"] == "row-00000"
    assert result[1000]["id"] == "row-01000"
    assert result[2299]["id"] == "row-02299"

    assert len(calls) == 3
    assert calls[0][2]["Range"] == "0-999"
    assert calls[1][2]["Range"] == "1000-1999"
    assert calls[2][2]["Range"] == "2000-2999"


def test_select_stops_after_exactly_one_full_page_then_empty(monkeypatch):
    """Edge case: one full page exactly, next page empty. The empty
    page counts as 'partial' (length 0 < _PAGE_SIZE) so we stop."""
    adapter = _make_adapter()
    pages = [
        [_row(i) for i in range(0, 1000)],
        [],
    ]
    page_iter = iter(pages)
    calls = []

    def fake_request(method, path, body=None, extra_headers=None):
        calls.append(dict(extra_headers or {}))
        return next(page_iter)

    monkeypatch.setattr(adapter._helper, "_request", fake_request)
    result = adapter.select("t", where={"a": 1})
    assert len(result) == 1000
    assert len(calls) == 2


def test_select_raises_when_max_rows_exceeded(monkeypatch):
    """If the paginator would fetch beyond `max_rows`, it must raise
    rather than silently truncating. A query matching > _DEFAULT_MAX_ROWS
    rows is almost certainly a missing where clause; better to fail
    loud."""
    adapter = _make_adapter()
    # 1000 rows per page, always full — would walk forever.
    def fake_request(method, path, body=None, extra_headers=None):
        return [_row(i) for i in range(1000)]

    monkeypatch.setattr(adapter._helper, "_request", fake_request)
    with pytest.raises(RuntimeError, match="exceeded max_rows"):
        adapter.select("t", where={"a": 1}, max_rows=2500)


def test_select_respects_custom_max_rows(monkeypatch):
    """`max_rows=` lets callers opt into a larger or smaller cap."""
    adapter = _make_adapter()
    pages = [
        [_row(i) for i in range(0, 1000)],
        [_row(i) for i in range(1000, 1500)],  # partial → stop
    ]
    page_iter = iter(pages)

    def fake_request(method, path, body=None, extra_headers=None):
        return next(page_iter)

    monkeypatch.setattr(adapter._helper, "_request", fake_request)
    # 1500 < 2000, so no raise.
    result = adapter.select("t", where={"a": 1}, max_rows=2000)
    assert len(result) == 1500


def test_select_empty_result_no_crash(monkeypatch):
    """Zero-match query returns an empty list, not a crash. Single
    request, no follow-up pages."""
    adapter = _make_adapter()
    calls = []

    def fake_request(method, path, body=None, extra_headers=None):
        calls.append(method)
        return []

    monkeypatch.setattr(adapter._helper, "_request", fake_request)
    result = adapter.select("t", where={"a": 1})
    assert result == []
    assert len(calls) == 1


def test_select_preserves_path_filters_across_pages(monkeypatch):
    """The query path (select=..., filter=...) must be identical
    across every paginated request — only the Range header changes."""
    adapter = _make_adapter()
    pages = [
        [_row(i) for i in range(0, 1000)],
        [_row(i) for i in range(1000, 1100)],
    ]
    page_iter = iter(pages)
    paths = []

    def fake_request(method, path, body=None, extra_headers=None):
        paths.append(path)
        return next(page_iter)

    monkeypatch.setattr(adapter._helper, "_request", fake_request)
    adapter.select(
        "wallet_flipper_tag_grain",
        where={"status": "active", "wallet": "0xabc"},
        columns=("wallet", "grain", "tag"),
    )
    assert len(paths) == 2
    assert paths[0] == paths[1], "path must be stable across paginated calls"
    assert "select=wallet,grain,tag" in paths[0]
    assert "status=eq.active" in paths[0]
    assert "wallet=eq.0xabc" in paths[0]


def test_select_handles_none_response(monkeypatch):
    """`_helper._request` can return None for some error paths.
    Adapter must treat that as 'no more rows' and return what it has
    so far without crashing."""
    adapter = _make_adapter()
    pages = [
        [_row(i) for i in range(0, 1000)],
        None,  # weird but possible
    ]
    page_iter = iter(pages)

    def fake_request(method, path, body=None, extra_headers=None):
        return next(page_iter)

    monkeypatch.setattr(adapter._helper, "_request", fake_request)
    result = adapter.select("t", where={"a": 1})
    assert len(result) == 1000


# ---------------------------------------------------------------------------
# where_ops — operator filter support (PostgREST gte/lte/gt/lt/neq/like/is)
#
# Rationale: equality-only `where=` forces callers either to over-fetch then
# filter in Python, or to bypass the adapter entirely with raw `_request()`
# calls — exactly the regression `ksg_flipper_population_check.py` shipped
# with (finding #1). `where_ops` keeps Range-header pagination centralized
# while letting callers express server-side range filters.
# ---------------------------------------------------------------------------


def test_select_passes_where_ops_as_operator_filters(monkeypatch):
    """`where_ops={"n_at_grain": ("gte", 30)}` must append the
    `&n_at_grain=gte.30` filter to the PostgREST path."""
    adapter = _make_adapter()
    captured = {}

    def fake_request(method, path, body=None, extra_headers=None):
        captured["path"] = path
        return []

    monkeypatch.setattr(adapter._helper, "_request", fake_request)
    adapter.select(
        "wallet_flipper_tag_grain",
        where={"status": "active"},
        where_ops={"n_at_grain": ("gte", 30)},
    )
    assert "n_at_grain=gte.30" in captured["path"]
    assert "status=eq.active" in captured["path"]


def test_select_combines_where_and_where_ops_in_one_path(monkeypatch):
    """Equality filters from `where` and operator filters from `where_ops`
    must both appear in the same path, joined with `&`."""
    adapter = _make_adapter()
    paths = []

    def fake_request(method, path, body=None, extra_headers=None):
        paths.append(path)
        return []

    monkeypatch.setattr(adapter._helper, "_request", fake_request)
    adapter.select(
        "wallet_flipper_tag_grain",
        where={"status": "active", "walk_full_history": True},
        where_ops={"n_at_grain": ("gte", 30), "grain_roi": ("gt", 0)},
    )
    p = paths[0]
    assert "status=eq.active" in p
    assert "walk_full_history=eq.true" in p
    assert "n_at_grain=gte.30" in p
    assert "grain_roi=gt.0" in p


def test_select_where_ops_supports_full_postgrest_operator_set(monkeypatch):
    """Whitelist of safe operators must include gte/gt/lte/lt/eq/neq/like/ilike/is.
    Each must produce a `{col}={op}.{value}` filter in the path."""
    adapter = _make_adapter()
    paths = []

    def fake_request(method, path, body=None, extra_headers=None):
        paths.append(path)
        return []

    monkeypatch.setattr(adapter._helper, "_request", fake_request)
    cases = {
        "gte": ("gte.10", 10),
        "gt": ("gt.10", 10),
        "lte": ("lte.10", 10),
        "lt": ("lt.10", 10),
        "neq": ("neq.foo", "foo"),
        "like": ("like.foo%25", "foo%"),
    }
    for op, (expected_suffix, value) in cases.items():
        paths.clear()
        adapter.select(
            "t",
            where={"status": "active"},
            where_ops={"col": (op, value)},
        )
        # Don't assert exact url-encoding; check the operator landed.
        assert f"col={op}." in paths[0], (op, paths[0])


def test_select_where_ops_rejects_unknown_operator(monkeypatch):
    """Unknown operators must raise instead of silently producing a
    nonsense PostgREST query (e.g. `col=delete.foo`). Defensive layer
    above the URL builder."""
    adapter = _make_adapter()

    def fake_request(*a, **kw):
        return []

    monkeypatch.setattr(adapter._helper, "_request", fake_request)
    with pytest.raises(ValueError, match="unsupported PostgREST operator"):
        adapter.select(
            "t",
            where={"status": "active"},
            where_ops={"col": ("DELETE", "anything")},
        )


def test_select_where_ops_paginates_just_like_equality_filters(monkeypatch):
    """`where_ops` must NOT break Range-header pagination — the operator
    filters must be stable across every page request, only Range changes."""
    adapter = _make_adapter()
    pages = [
        [_row(i) for i in range(0, 1000)],
        [_row(i) for i in range(1000, 1500)],  # partial → stop
    ]
    page_iter = iter(pages)
    paths = []
    ranges = []

    def fake_request(method, path, body=None, extra_headers=None):
        paths.append(path)
        ranges.append(dict(extra_headers or {}).get("Range"))
        return next(page_iter)

    monkeypatch.setattr(adapter._helper, "_request", fake_request)
    adapter.select(
        "wallet_flipper_tag_grain",
        where={"status": "active"},
        where_ops={"n_at_grain": ("gte", 30)},
    )
    assert len(paths) == 2
    assert paths[0] == paths[1], "where_ops filters must be stable across paginated calls"
    assert "n_at_grain=gte.30" in paths[0]
    assert ranges == ["0-999", "1000-1999"]


def test_select_where_ops_rejects_is_and_in_pending_proper_formatting(monkeypatch):
    """PostgREST `is.null` / `is.true` and `in.(a,b,c)` need format-
    specific serialization that the generic `op.<urlencoded value>`
    path can't produce safely (e.g. `is.None` would be wrong; a list
    value would render as the str repr of the list). Until proper
    support + tests land, the whitelist excludes them so callers get
    a loud error rather than a malformed query."""
    adapter = _make_adapter()
    monkeypatch.setattr(adapter._helper, "_request", lambda *a, **kw: [])
    for op, value in (("is", None), ("in", [1, 2])):
        with pytest.raises(ValueError, match="unsupported PostgREST operator"):
            adapter.select("t", where={"a": 1}, where_ops={"col": (op, value)})


def test_select_where_ops_none_default_unchanged_path(monkeypatch):
    """Backward compat: when `where_ops=None` (default), the path must
    match the legacy equality-only behavior byte-for-byte."""
    adapter = _make_adapter()
    paths = []

    def fake_request(method, path, body=None, extra_headers=None):
        paths.append(path)
        return []

    monkeypatch.setattr(adapter._helper, "_request", fake_request)
    adapter.select("t", where={"status": "active"})
    adapter.select("t", where={"status": "active"}, where_ops=None)
    assert paths[0] == paths[1]
