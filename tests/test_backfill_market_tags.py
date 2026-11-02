"""Tests for wallet-taxonomy migration Stage 1 — market_tags + backfill."""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from sniper.markets.polymarket_api import _extract_raw_tags, fetch_event_tags_by_condition
from scripts.backfill_market_tags import (
    backfill_market_tags_from_event_pages,
    coverage_gap,
    ensure_market_tags_schema,
    extract_tags_by_condition_from_events,
    insert_tags,
    iter_gamma_event_pages,
)


def _seed_market_tags_schema(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE market_tags (
            condition_id TEXT NOT NULL,
            tag TEXT NOT NULL,
            tag_label TEXT,
            ingested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (condition_id, tag)
        )
        """
    )
    conn.execute(
        "CREATE TABLE market_metadata (condition_id TEXT PRIMARY KEY)"
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# _extract_raw_tags
# ---------------------------------------------------------------------------


def test_extract_raw_tags_returns_all_slugs():
    event = {
        "tags": [
            {"slug": "politics", "label": "Politics"},
            {"slug": "world", "label": "World"},
            {"slug": "ukraine", "label": "Ukraine"},
        ]
    }
    assert _extract_raw_tags(event) == [
        ("politics", "Politics"),
        ("world", "World"),
        ("ukraine", "Ukraine"),
    ]


def test_extract_raw_tags_lowercases_slug_only():
    event = {"tags": [{"slug": "Crypto", "label": "Crypto"}]}
    pairs = _extract_raw_tags(event)
    assert pairs == [("crypto", "Crypto")]


def test_extract_raw_tags_dedupes_within_event():
    event = {"tags": [
        {"slug": "politics", "label": "Politics"},
        {"slug": "politics", "label": "politics-duplicate"},
    ]}
    assert _extract_raw_tags(event) == [("politics", "Politics")]


def test_extract_raw_tags_handles_malformed_inputs():
    assert _extract_raw_tags(None) == []
    assert _extract_raw_tags({}) == []
    assert _extract_raw_tags({"tags": None}) == []
    assert _extract_raw_tags({"tags": "not a list"}) == []
    assert _extract_raw_tags({"tags": [{"slug": ""}, None, {"not_slug": "x"}]}) == []


def test_extract_raw_tags_tolerates_missing_label():
    event = {"tags": [{"slug": "earn-4"}]}
    assert _extract_raw_tags(event) == [("earn-4", "")]


# ---------------------------------------------------------------------------
# fetch_event_tags_by_condition — mock session
# ---------------------------------------------------------------------------


def test_fetch_event_tags_by_condition_maps_condition_to_tags():
    session = object()  # placeholder; we monkeypatch _request_json below

    events_page = [
        {
            "tags": [{"slug": "politics", "label": "Politics"}, {"slug": "elections", "label": ""}],
            "markets": [{"conditionId": "0xAAA"}, {"conditionId": "0xBBB"}],
        },
        {
            "tags": [{"slug": "crypto", "label": ""}],
            "markets": [{"conditionId": "0xCCC"}],
        },
    ]

    def fake_request_json(sess, url, params=None):
        if params and params.get("offset", 0) == 0:
            return events_page
        return []

    with patch("sniper.markets.polymarket_api._request_json", side_effect=fake_request_json):
        out = fetch_event_tags_by_condition(session)

    # Both markets of event 1 share its tag set
    assert out["0xaaa"] == [("politics", "Politics"), ("elections", "")]
    assert out["0xbbb"] == [("politics", "Politics"), ("elections", "")]
    assert out["0xccc"] == [("crypto", "")]


def test_fetch_event_tags_by_condition_skips_events_without_tags():
    def fake_request_json(sess, url, params=None):
        if params and params.get("offset", 0) == 0:
            return [
                {"tags": [], "markets": [{"conditionId": "0xAAA"}]},
                {"tags": [{"slug": "x", "label": ""}], "markets": [{"conditionId": "0xBBB"}]},
            ]
        return []

    with patch("sniper.markets.polymarket_api._request_json", side_effect=fake_request_json):
        out = fetch_event_tags_by_condition(object())
    assert "0xaaa" not in out
    assert out["0xbbb"] == [("x", "")]


def test_fetch_event_tags_by_condition_first_event_wins_on_duplicate_condition():
    """If a conditionId appears in two events (unlikely but possible),
    first event's tag set is kept."""
    def fake_request_json(sess, url, params=None):
        if params and params.get("offset", 0) == 0:
            return [
                {"tags": [{"slug": "first", "label": ""}], "markets": [{"conditionId": "0xSAME"}]},
                {"tags": [{"slug": "second", "label": ""}], "markets": [{"conditionId": "0xSAME"}]},
            ]
        return []

    with patch("sniper.markets.polymarket_api._request_json", side_effect=fake_request_json):
        out = fetch_event_tags_by_condition(object())
    assert out["0xsame"] == [("first", "")]


# ---------------------------------------------------------------------------
# insert_tags — idempotent
# ---------------------------------------------------------------------------


def test_insert_tags_inserts_new_rows(tmp_path):
    conn = _seed_market_tags_schema(tmp_path / "t.db")
    tags = {"0xaaa": [("politics", "Politics"), ("world", "World")]}
    n = insert_tags(conn, tags)
    assert n == 2
    rows = list(conn.execute("SELECT condition_id, tag, tag_label FROM market_tags ORDER BY tag"))
    assert rows == [("0xaaa", "politics", "Politics"), ("0xaaa", "world", "World")]


def test_insert_tags_is_idempotent(tmp_path):
    conn = _seed_market_tags_schema(tmp_path / "t.db")
    tags = {"0xaaa": [("politics", "Politics")]}
    first = insert_tags(conn, tags)
    second = insert_tags(conn, tags)
    assert first == 1
    assert second == 0  # INSERT OR IGNORE ⇒ no duplicate
    n = conn.execute("SELECT COUNT(*) FROM market_tags").fetchone()[0]
    assert n == 1


def test_insert_tags_handles_empty_label(tmp_path):
    conn = _seed_market_tags_schema(tmp_path / "t.db")
    tags = {"0xaaa": [("earn-4", "")]}
    insert_tags(conn, tags)
    row = conn.execute("SELECT tag_label FROM market_tags WHERE tag = 'earn-4'").fetchone()
    assert row[0] is None  # empty label stored as NULL


def test_insert_tags_creates_market_tags_schema_on_empty_db(tmp_path):
    conn = sqlite3.connect(tmp_path / "empty.db")
    n = insert_tags(conn, {"0xAAA": [("Politics", "Politics")]})
    assert n == 1
    rows = list(conn.execute("SELECT condition_id, tag, tag_label FROM market_tags"))
    assert rows == [("0xaaa", "politics", "Politics")]


def test_extract_tags_by_condition_skips_event_without_tags():
    page = [
        {"tags": [], "markets": [{"conditionId": "0xAAA"}]},
        {"tags": [{"slug": "sports", "label": "Sports"}], "markets": [{"conditionId": "0xBBB"}]},
    ]
    out = extract_tags_by_condition_from_events(page)
    assert "0xaaa" not in out
    assert out == {"0xbbb": [("sports", "Sports")]}


def test_extract_tags_by_condition_unions_duplicate_conditions_without_duplicate_rows():
    page = [
        {"tags": [{"slug": "politics", "label": "Politics"}], "markets": [{"conditionId": "0xSAME"}]},
        {
            "tags": [
                {"slug": "politics", "label": "Politics"},
                {"slug": "elections", "label": "Elections"},
            ],
            "markets": [{"conditionId": "0xSAME"}],
        },
    ]
    out = extract_tags_by_condition_from_events(page)
    assert out == {"0xsame": [("politics", "Politics"), ("elections", "Elections")]}


def test_extract_tags_by_condition_logs_and_skips_malformed_page(caplog):
    caplog.set_level(logging.WARNING, logger="backfill_market_tags")
    assert extract_tags_by_condition_from_events({"not": "a list"}) == {}
    assert "malformed gamma events page" in caplog.text


def test_extract_tags_by_condition_logs_and_skips_empty_page(caplog):
    caplog.set_level(logging.WARNING, logger="backfill_market_tags")
    assert extract_tags_by_condition_from_events([]) == {}
    assert "empty gamma events page" in caplog.text


def test_backfill_market_tags_from_pages_is_resumable_after_failure(tmp_path):
    conn = sqlite3.connect(tmp_path / "resume.db")
    page1 = [{"tags": [{"slug": "politics", "label": "Politics"}], "markets": [{"conditionId": "0xAAA"}]}]
    page2 = [{"tags": [{"slug": "sports", "label": "Sports"}], "markets": [{"conditionId": "0xBBB"}]}]

    def broken_pages():
        yield page1
        raise RuntimeError("gamma degraded")

    first = backfill_market_tags_from_event_pages(conn, broken_pages())
    assert first.inserted_rows == 1
    assert first.failed_pages == 1

    second = backfill_market_tags_from_event_pages(conn, [page1, page2])
    assert second.inserted_rows == 1
    rows = list(conn.execute("SELECT condition_id, tag FROM market_tags ORDER BY condition_id, tag"))
    assert rows == [("0xaaa", "politics"), ("0xbbb", "sports")]


def test_backfill_market_tags_rerun_stable_row_count(tmp_path):
    conn = sqlite3.connect(tmp_path / "stable.db")
    pages = [[
        {"tags": [{"slug": "finance", "label": "Finance"}], "markets": [{"conditionId": "0xAAA"}]},
        {"tags": [{"slug": "finance", "label": "Finance"}], "markets": [{"conditionId": "0xAAA"}]},
    ]]
    first = backfill_market_tags_from_event_pages(conn, pages)
    second = backfill_market_tags_from_event_pages(conn, pages)
    assert first.inserted_rows == 1
    assert second.inserted_rows == 0
    assert conn.execute("SELECT COUNT(*) FROM market_tags").fetchone()[0] == 1


def test_backfill_market_tags_deterministic_table_state(tmp_path):
    pages = [[
        {"tags": [{"slug": "sports", "label": "Sports"}], "markets": [{"conditionId": "0xBBB"}]},
        {"tags": [{"slug": "politics", "label": "Politics"}], "markets": [{"conditionId": "0xAAA"}]},
    ]]
    states = []
    for name in ("a.db", "b.db"):
        conn = sqlite3.connect(tmp_path / name)
        backfill_market_tags_from_event_pages(conn, pages)
        states.append(list(conn.execute("SELECT condition_id, tag, tag_label FROM market_tags ORDER BY condition_id, tag")))
    assert states[0] == states[1] == [
        ("0xaaa", "politics", "Politics"),
        ("0xbbb", "sports", "Sports"),
    ]


# ---------------------------------------------------------------------------
# coverage_gap
# ---------------------------------------------------------------------------


def test_coverage_gap_reports_untagged_metadata_rows(tmp_path):
    conn = _seed_market_tags_schema(tmp_path / "t.db")
    conn.execute("INSERT INTO market_metadata (condition_id) VALUES ('0xAAA'), ('0xBBB'), ('0xCCC')")
    conn.execute("INSERT INTO market_tags (condition_id, tag) VALUES ('0xAAA', 'politics')")
    conn.commit()
    total, untagged = coverage_gap(conn)
    assert total == 3
    assert untagged == 2


def test_coverage_gap_zero_when_fully_tagged(tmp_path):
    conn = _seed_market_tags_schema(tmp_path / "t.db")
    conn.execute("INSERT INTO market_metadata (condition_id) VALUES ('0xAAA')")
    conn.execute("INSERT INTO market_tags (condition_id, tag) VALUES ('0xAAA', 'x')")
    conn.commit()
    total, untagged = coverage_gap(conn)
    assert total == 1
    assert untagged == 0


def test_coverage_gap_handles_missing_market_metadata(tmp_path):
    conn = sqlite3.connect(tmp_path / "no_metadata.db")
    ensure_market_tags_schema(conn)
    assert coverage_gap(conn) == (0, 0)


# ---------------------------------------------------------------------------
# Schema sanity — the table is created by init_db
# ---------------------------------------------------------------------------


def test_market_tags_table_shape():
    from db import init_db
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(market_tags)")]
    assert cols == ["condition_id", "tag", "tag_label", "ingested_at"]
    # PK is composite
    pk_cols = [r[1] for r in conn.execute("PRAGMA table_info(market_tags)") if r[5]]
    assert pk_cols == ["condition_id", "tag"]
    indexes = {r[1] for r in conn.execute("PRAGMA index_list(market_tags)")}
    assert {"idx_market_tags_tag", "idx_market_tags_condition_id"}.issubset(indexes)


def test_migration_015_creates_market_tags_only():
    import importlib

    migration = importlib.import_module("migrations.015_market_tags")
    conn = sqlite3.connect(":memory:")
    migration.run(conn)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('market_tags', 'tag_nav_category')"
        )
    }
    assert tables == {"market_tags"}


def test_iter_gamma_event_pages_marks_cap_per_pass(monkeypatch):
    calls = []

    def fake_request_json(session, url, params=None):
        calls.append(params)
        return [{"id": params["offset"]}]

    monkeypatch.setattr("scripts.backfill_market_tags._request_json", fake_request_json)
    pages = list(iter_gamma_event_pages(object(), max_pages=1, include_closed=True, limit=1))
    assert [p.page_cap_hit for p in pages] == [True, True]
    assert [p.params["active"] for p in pages] == [True, False]
    assert len(calls) == 2


def test_iter_gamma_event_pages_does_not_false_cap_on_short_page(monkeypatch):
    def fake_request_json(session, url, params=None):
        return [{"id": "short"}]

    monkeypatch.setattr("scripts.backfill_market_tags._request_json", fake_request_json)
    pages = list(iter_gamma_event_pages(object(), max_pages=2, include_closed=False, limit=2))
    assert len(pages) == 1
    assert pages[0].page_cap_hit is False


def test_iter_gamma_event_pages_default_limit_matches_gamma_cap():
    """Regression (2026-05-28): Gamma /events caps pages at 100 regardless of a
    larger requested limit. The default must be 100 so a full page does not trip
    the short-page break and offset advances by the real page size. Requesting
    500 (the old default) returned 100 → broke after page 1 and skipped 400/page."""
    import inspect
    sig = inspect.signature(iter_gamma_event_pages)
    assert sig.parameters["limit"].default == 100


def test_iter_gamma_event_pages_full_page_continues_to_next(monkeypatch):
    """A full page (len == limit) must NOT break — pagination continues until a
    short/empty page. Models Gamma returning exactly `limit` events per page."""
    calls = []

    def fake_request_json(session, url, params=None):
        calls.append(params)
        off = params["offset"]
        # two full pages of size 2, then a short page → stop
        if off == 0:
            return [{"id": 0}, {"id": 1}]
        if off == 2:
            return [{"id": 2}, {"id": 3}]
        return [{"id": 4}]  # short page < limit → break

    monkeypatch.setattr("scripts.backfill_market_tags._request_json", fake_request_json)
    pages = list(iter_gamma_event_pages(object(), max_pages=10, include_closed=False, limit=2))
    # 3 pages consumed (2 full + 1 short), offsets advanced by limit each time
    assert [p.params["offset"] for p in pages] == [0, 2, 4]


def test_iter_gamma_event_pages_requests_newest_first():
    """Recent markets (the stale-table gap) must be covered first → newest-first
    ordering on the query params."""
    calls = []

    def fake_request_json(session, url, params=None):
        calls.append(params)
        return []  # empty → immediate stop per pass

    import scripts.backfill_market_tags as mod
    orig = mod._request_json
    mod._request_json = fake_request_json
    try:
        list(iter_gamma_event_pages(object(), max_pages=1, include_closed=False, limit=100))
    finally:
        mod._request_json = orig
    assert calls and calls[0].get("order") == "startDate"
    assert calls[0].get("ascending") == "false"


def test_iter_gamma_event_pages_flags_truncation_on_offset_wall(monkeypatch):
    """Gamma rejects offset > 10000 with HTTP 422. Hitting that wall mid-walk
    (full pages right up to the cutoff) is a TRUNCATION, not a clean finish —
    the paginator must yield a terminal page flagged page_cap_hit=True so the
    crawl is never reported complete (regression: the 422 used to fall through
    to a generic failed_page with page_cap_hit=False)."""
    import httpx

    def fake_request_json(session, url, params=None):
        if params["offset"] >= 200:  # simulate the offset wall after 2 full pages
            req = httpx.Request("GET", url)
            raise httpx.HTTPStatusError("422", request=req, response=httpx.Response(422, request=req))
        return [{"id": i} for i in range(params["limit"])]  # full page

    monkeypatch.setattr("scripts.backfill_market_tags._request_json", fake_request_json)
    pages = list(iter_gamma_event_pages(object(), max_pages=50, include_closed=False, limit=100))
    assert pages[-1].page_cap_hit is True
    assert any(p.page_cap_hit for p in pages)


def test_iter_gamma_event_pages_non_422_http_error_propagates(monkeypatch):
    """A non-offset-wall 4xx (e.g. 404) is a real error, not a truncation —
    it must propagate, not be silently swallowed as a clean cap."""
    import httpx

    def fake_request_json(session, url, params=None):
        req = httpx.Request("GET", url)
        raise httpx.HTTPStatusError("404", request=req, response=httpx.Response(404, request=req))

    monkeypatch.setattr("scripts.backfill_market_tags._request_json", fake_request_json)
    with pytest.raises(httpx.HTTPStatusError):
        list(iter_gamma_event_pages(object(), max_pages=5, include_closed=False, limit=100))


def test_backfill_marks_incomplete_when_page_fetch_aborts():
    """A fetch error mid-crawl aborts the walk → results are incomplete, so
    page_cap_hit must report True (regression: it stayed False, making a
    truncated crawl look like a clean finish)."""
    from scripts.backfill_market_tags import GammaEventPage

    conn = sqlite3.connect(":memory:")

    def exploding_pages():
        yield GammaEventPage(events=[], params={"offset": 0}, page_cap_hit=False)
        raise RuntimeError("network died mid-crawl")

    stats = backfill_market_tags_from_event_pages(conn, exploding_pages())
    assert stats.failed_pages == 1
    assert stats.page_cap_hit is True
