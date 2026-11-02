"""Phase-3 minimal helpers over the tag_market_stats schema.

No analyzer-behavior assertions — those land in Phase 4 alongside the
Layer 3/4 stub-fill in wallet_archetype_extractor (see migration plan §
A.2). Phase 3 just verifies the reader returns None / False against
empty rows and the persisted dict against seeded rows.

Phase 3 plan § A11.
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from db import init_db
from sniper.analysis.wallet_protocol.tag_market_stats_reader import (
    fetch_tag_market_stats,
    is_tag_market_stats_available,
)


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "tms.db"
    c = sqlite3.connect(str(p))
    c.row_factory = sqlite3.Row
    init_db(c)
    yield c
    c.close()


def _seed_geo(db) -> None:
    db.execute(
        """
        INSERT INTO tag_market_stats
        (tag, n_markets, n_resolved, resolved_yes_count, yes_share,
         avg_resolved_price_yes, median_market_volume_24h, top_co_tags_json,
         computed_at, inputs_version)
        VALUES ('geo', 200, 180, 90, 0.5, 0.55, 1000.0, '[]', ?, 'v1')
        """,
        (int(time.time()),),
    )
    db.commit()


def test_unavailable_when_table_empty(db):
    assert is_tag_market_stats_available(db, "geo") is False
    assert fetch_tag_market_stats(db, "geo") is None


def test_available_when_row_seeded(db):
    _seed_geo(db)
    assert is_tag_market_stats_available(db, "geo") is True
    stats = fetch_tag_market_stats(db, "geo")
    assert stats is not None
    assert stats["tag"] == "geo"
    assert stats["n_markets"] == 200
    assert stats["yes_share"] == 0.5
    assert stats["inputs_version"] == "v1"


def test_fetch_returns_none_for_unknown_tag(db):
    _seed_geo(db)
    assert fetch_tag_market_stats(db, "absolutely_nonexistent") is None
    assert is_tag_market_stats_available(db, "absolutely_nonexistent") is False


def test_no_side_effects(db):
    """Reader does not mutate the table (no INSERT, UPDATE, DELETE)."""
    _seed_geo(db)
    before = [dict(r) for r in db.execute("SELECT * FROM tag_market_stats")]
    is_tag_market_stats_available(db, "geo")
    fetch_tag_market_stats(db, "geo")
    fetch_tag_market_stats(db, "missing")
    is_tag_market_stats_available(db, "missing")
    after = [dict(r) for r in db.execute("SELECT * FROM tag_market_stats")]
    assert before == after
