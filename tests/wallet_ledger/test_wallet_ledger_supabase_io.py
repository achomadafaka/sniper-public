"""Slice 6 Task 2 — SupabaseHttp protocol surface tests.

Two tracks:

* `StubSupabaseHttp` semantics — calls log grows on every request, second
  insert_ignore against the same natural key returns 0 yet appends a
  second `("insert_ignore", ...)` entry.
* `RestSupabaseHttp` URL/header/body construction — verifies the wire
  format matches the PostgREST contract Slice 6 depends on
  (`Prefer: resolution=ignore-duplicates,return=representation`, never
  `merge-duplicates`; explicit constructor with no `os.environ` reads).

Plus a module-level grep guard: nothing in
`sniper.analysis.wallet_protocol.ledger.supabase_io` may import
`bot.supabase_helper`, `requests`, or `httpx`. (Slice 6 writers will
inherit the same guard from per-module tests as they land.)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sniper.analysis.wallet_protocol.ledger.supabase_io import (
    HTTPTransport,
    RestSupabaseHttp,
    StubSupabaseHttp,
    SupabaseHttp,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SUPABASE_IO_FILE = (
    REPO_ROOT
    / "sniper"
    / "analysis"
    / "wallet_protocol"
    / "ledger"
    / "supabase_io.py"
)


# ---------------------------------------------------------------------------
# Protocol shape
# ---------------------------------------------------------------------------


def test_stub_satisfies_supabasehttp_protocol() -> None:
    stub: SupabaseHttp = StubSupabaseHttp()
    # Smoke: protocol-typed reference is callable as advertised.
    assert stub.insert_ignore("t", [], natural_key_columns=("k",)) == 0


def test_rest_satisfies_supabasehttp_protocol() -> None:
    rest: SupabaseHttp = RestSupabaseHttp(url="https://x", key="k")
    assert hasattr(rest, "insert_ignore")
    assert hasattr(rest, "patch")
    assert hasattr(rest, "select")


# ---------------------------------------------------------------------------
# StubSupabaseHttp
# ---------------------------------------------------------------------------


def test_stub_records_every_insert_ignore_call() -> None:
    """Every request appends to `calls`. Second pass over the same
    natural key returns 0 (no new row) but the log still grows — the log
    is a request audit trail, not a deduplicated state store."""
    stub = StubSupabaseHttp()

    first = stub.insert_ignore(
        "wallet_analysis_results",
        [{"wallet": "0xa", "run_id": "r1", "window_id": "w1", "value": 1}],
        natural_key_columns=("wallet", "run_id", "window_id"),
    )
    second = stub.insert_ignore(
        "wallet_analysis_results",
        [{"wallet": "0xa", "run_id": "r1", "window_id": "w1", "value": 1}],
        natural_key_columns=("wallet", "run_id", "window_id"),
    )

    assert first == 1
    assert second == 0
    assert len(stub.calls) == 2
    assert stub.calls[0][0] == "insert_ignore"
    assert stub.calls[1][0] == "insert_ignore"
    # rows_by_table contract: idempotent — only one stored row.
    assert len(stub.rows_by_table["wallet_analysis_results"]) == 1


def test_stub_select_reads_stored_rows_with_filter() -> None:
    stub = StubSupabaseHttp()
    stub.insert_ignore(
        "wallet_findings",
        [
            {"finding_uid": "u1", "wallet": "0xa", "status": "active"},
            {"finding_uid": "u2", "wallet": "0xa", "status": "superseded"},
            {"finding_uid": "u3", "wallet": "0xb", "status": "active"},
        ],
        natural_key_columns=("finding_uid",),
    )

    rows = stub.select(
        "wallet_findings",
        where={"wallet": "0xa", "status": "active"},
    )
    assert [r["finding_uid"] for r in rows] == ["u1"]
    # select also recorded a call.
    assert any(c[0] == "select" for c in stub.calls)


def test_stub_select_projects_columns() -> None:
    stub = StubSupabaseHttp()
    stub.insert_ignore(
        "artifact_registry",
        [{"content_hash": "h1", "kind": "wallet_warehouse", "byte_count": 100}],
        natural_key_columns=("content_hash",),
    )
    rows = stub.select(
        "artifact_registry",
        where={"content_hash": "h1"},
        columns=("content_hash", "byte_count"),
    )
    assert rows == [{"content_hash": "h1", "byte_count": 100}]


def test_stub_patch_updates_in_place_and_returns_count() -> None:
    stub = StubSupabaseHttp()
    stub.insert_ignore(
        "wallet_findings",
        [
            {"finding_uid": "u1", "status": "active"},
            {"finding_uid": "u2", "status": "active"},
        ],
        natural_key_columns=("finding_uid",),
    )

    touched = stub.patch(
        "wallet_findings",
        where={"finding_uid": "u1"},
        set_columns={"status": "superseded"},
    )
    assert touched == 1

    rows = stub.select("wallet_findings", where={"finding_uid": "u1"})
    assert rows[0]["status"] == "superseded"

    # Patch was logged with both where and set payloads.
    patch_calls = [c for c in stub.calls if c[0] == "patch"]
    assert patch_calls, "patch was not logged"
    last = patch_calls[-1][2]
    assert last["where"] == {"finding_uid": "u1"}
    assert last["set"] == {"status": "superseded"}


def test_stub_insert_ignore_empty_rows_is_noop() -> None:
    stub = StubSupabaseHttp()
    n = stub.insert_ignore("t", [], natural_key_columns=("k",))
    assert n == 0
    assert stub.rows_by_table.get("t", []) == []


# ---------------------------------------------------------------------------
# RestSupabaseHttp — URL / header / body wire format
# ---------------------------------------------------------------------------


class _RecordingTransport:
    """Captures every (method, url, headers, body, response) and returns
    a canned status/payload."""

    def __init__(self, *, status: int = 200, payload: bytes = b"[]") -> None:
        self.status = status
        self.payload = payload
        self.calls: list[dict] = []

    def __call__(
        self,
        method: str,
        url: str,
        headers,
        body,
    ):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
            }
        )
        return self.status, self.payload


def test_rest_constructor_requires_url_and_key() -> None:
    with pytest.raises(ValueError):
        RestSupabaseHttp(url="", key="k")
    with pytest.raises(ValueError):
        RestSupabaseHttp(url="https://x", key="")


def test_rest_constructor_does_not_read_os_environ(monkeypatch) -> None:
    """Construction must NOT pull from `SUPABASE_URL`/`SUPABASE_*KEY` —
    Slice 6 D5: live tests pass `CALIBRATION_LIVE_*` values explicitly.

    Set bait values on the environment; the constructor must use the
    arguments instead.
    """
    monkeypatch.setenv("SUPABASE_URL", "https://wrong.example")
    monkeypatch.setenv("SUPABASE_KEY", "wrong-key")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "wrong-service")
    monkeypatch.setenv("CALIBRATION_LIVE_SUPABASE_URL", "https://wrong2")
    monkeypatch.setenv("CALIBRATION_LIVE_SUPABASE_KEY", "wrong2")

    transport = _RecordingTransport(payload=b"[]")
    rest = RestSupabaseHttp(
        url="https://right.supabase.co",
        key="right-key",
        transport=transport,
    )
    rest.select("t", where={"k": "v"})

    call = transport.calls[0]
    assert call["url"].startswith("https://right.supabase.co/")
    assert call["headers"]["apikey"] == "right-key"
    assert call["headers"]["Authorization"] == "Bearer right-key"
    # No bait values must leak through.
    assert "wrong" not in call["url"]
    assert "wrong" not in call["headers"]["apikey"]


def test_rest_insert_ignore_uses_resolution_ignore_duplicates_header() -> None:
    transport = _RecordingTransport(
        status=201, payload=b'[{"finding_uid":"u1"}]'
    )
    rest = RestSupabaseHttp(
        url="https://x.supabase.co",
        key="k",
        transport=transport,
    )
    n = rest.insert_ignore(
        "wallet_findings",
        [{"finding_uid": "u1", "wallet": "0xa"}],
        natural_key_columns=("finding_uid",),
    )

    assert n == 1
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://x.supabase.co/rest/v1/wallet_findings"
    prefer = call["headers"]["Prefer"]
    assert "resolution=ignore-duplicates" in prefer
    assert "return=representation" in prefer
    # D1: merge-duplicates is forbidden anywhere in Slice 6.
    assert "merge-duplicates" not in prefer.lower()


def test_rest_insert_ignore_skips_post_on_empty_rows() -> None:
    transport = _RecordingTransport()
    rest = RestSupabaseHttp(
        url="https://x.supabase.co", key="k", transport=transport
    )
    assert rest.insert_ignore("t", [], natural_key_columns=("k",)) == 0
    assert transport.calls == []


def test_rest_patch_builds_postgrest_filter_query() -> None:
    transport = _RecordingTransport(payload=b'[{"finding_uid":"u1"}]')
    rest = RestSupabaseHttp(
        url="https://x.supabase.co", key="k", transport=transport
    )
    n = rest.patch(
        "wallet_findings",
        where={"finding_uid": "u1"},
        set_columns={"status": "superseded"},
    )
    assert n == 1

    call = transport.calls[0]
    assert call["method"] == "PATCH"
    assert (
        call["url"]
        == "https://x.supabase.co/rest/v1/wallet_findings?finding_uid=eq.u1"
    )
    assert call["headers"]["Prefer"] == "return=representation"
    assert call["body"] == b'{"status":"superseded"}'


def test_rest_patch_requires_where() -> None:
    rest = RestSupabaseHttp(
        url="https://x", key="k", transport=_RecordingTransport()
    )
    with pytest.raises(ValueError):
        rest.patch("t", where={}, set_columns={"x": 1})


def test_rest_select_builds_filter_and_optional_projection() -> None:
    transport = _RecordingTransport(
        payload=b'[{"content_hash":"h1","byte_count":42}]'
    )
    rest = RestSupabaseHttp(
        url="https://x.supabase.co", key="k", transport=transport
    )
    rows = rest.select(
        "artifact_registry",
        where={"content_hash": "h1"},
        columns=("content_hash", "byte_count"),
    )
    assert rows == [{"content_hash": "h1", "byte_count": 42}]

    call = transport.calls[0]
    assert call["method"] == "GET"
    assert call["body"] is None
    assert (
        call["url"]
        == "https://x.supabase.co/rest/v1/artifact_registry"
        "?content_hash=eq.h1&select=content_hash,byte_count"
    )


def test_rest_raises_on_http_error() -> None:
    transport = _RecordingTransport(status=500, payload=b'{"msg":"boom"}')
    rest = RestSupabaseHttp(
        url="https://x", key="k", transport=transport
    )
    with pytest.raises(RuntimeError):
        rest.select("t", where={"k": "v"})


def test_rest_url_encodes_filter_values() -> None:
    transport = _RecordingTransport(payload=b"[]")
    rest = RestSupabaseHttp(
        url="https://x.supabase.co", key="k", transport=transport
    )
    rest.select("wallet_findings", where={"wallet": "0xAB CD"})
    call = transport.calls[0]
    # Space is percent-encoded; the eq.<value> shape is preserved.
    assert "wallet=eq.0xAB%20CD" in call["url"]


# ---------------------------------------------------------------------------
# Module-level guard: no live Supabase imports inside the protocol module
# ---------------------------------------------------------------------------


def test_supabase_io_module_does_not_import_legacy_supabase_helper() -> None:
    """`bot.supabase_helper` reads `SUPABASE_URL`/`SUPABASE_*KEY` globals
    at module import time. The Slice 6 protocol surface MUST avoid that
    coupling — the explicit constructor is the whole point.

    Slice 6 writer modules (Tasks 3, 5, 6) will inherit this guard.
    """
    import ast

    tree = ast.parse(SUPABASE_IO_FILE.read_text())
    forbidden = {"bot", "bot.supabase_helper", "requests", "httpx"}
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    leaks = forbidden & imported_modules
    assert not leaks, f"forbidden imports leaked into supabase_io: {leaks}"
    # Also reject `bot.<anything>` and dotted submodules of forbidden roots.
    for mod in imported_modules:
        root = mod.split(".", 1)[0]
        assert root not in {"bot", "requests", "httpx"}, (
            f"forbidden import root: {mod}"
        )

    # `urllib.request` IS allowed here — it is the canonical real-network
    # transport. The guard for that import lives in writer-module tests,
    # which depend only on the SupabaseHttp protocol.
    assert "urllib.request" in imported_modules
