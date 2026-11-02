"""Tests for db.bulk_upsert_market_tags — streamer-side tag persistence."""
from __future__ import annotations

import sqlite3
import pytest

from db import bulk_upsert_market_tags, init_db


def _new_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    return conn


def test_bulk_upsert_inserts_new_rows():
    conn = _new_db()
    n = bulk_upsert_market_tags(
        conn,
        {"0xAAA": [("politics", "Politics"), ("world", "World")]},
    )
    assert n == 2
    rows = list(conn.execute("SELECT condition_id, tag, tag_label FROM market_tags ORDER BY tag"))
    assert rows == [("0xaaa", "politics", "Politics"), ("0xaaa", "world", "World")]


def test_bulk_upsert_is_idempotent():
    conn = _new_db()
    data = {"0xAAA": [("politics", "Politics")]}
    first = bulk_upsert_market_tags(conn, data)
    second = bulk_upsert_market_tags(conn, data)
    assert first == 1 and second == 0
    n = conn.execute("SELECT COUNT(*) FROM market_tags").fetchone()[0]
    assert n == 1  # but only one real row


def test_bulk_upsert_lowercases_condition_and_slug():
    conn = _new_db()
    bulk_upsert_market_tags(
        conn,
        {"0xAbCdEf": [("Crypto", "Crypto")]},
    )
    row = conn.execute("SELECT condition_id, tag FROM market_tags").fetchone()
    assert row == ("0xabcdef", "crypto")


def test_bulk_upsert_skips_empty_slug_or_cid():
    conn = _new_db()
    data = {
        "": [("x", "X")],
        "0xaaa": [("", ""), (None, "nope"), ("valid", "V")],
    }
    bulk_upsert_market_tags(conn, data)
    rows = list(conn.execute("SELECT condition_id, tag FROM market_tags"))
    assert rows == [("0xaaa", "valid")]


def test_bulk_upsert_empty_label_stored_as_null():
    conn = _new_db()
    bulk_upsert_market_tags(conn, {"0xaaa": [("slug", "")]})
    lbl = conn.execute("SELECT tag_label FROM market_tags WHERE tag = 'slug'").fetchone()[0]
    assert lbl is None


def test_bulk_upsert_handles_empty_input():
    conn = _new_db()
    assert bulk_upsert_market_tags(conn, {}) == 0
    assert bulk_upsert_market_tags(conn, None) == 0


def test_bulk_upsert_does_not_overwrite_existing_label():
    """Slugs are opaque and shouldn't change. Preserve first-seen label."""
    conn = _new_db()
    bulk_upsert_market_tags(conn, {"0xaaa": [("slug", "FirstLabel")]})
    bulk_upsert_market_tags(conn, {"0xaaa": [("slug", "DifferentLabel")]})
    lbl = conn.execute("SELECT tag_label FROM market_tags WHERE tag = 'slug'").fetchone()[0]
    assert lbl == "FirstLabel"  # INSERT OR IGNORE: first write wins
