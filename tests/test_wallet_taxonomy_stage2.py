"""Tests for wallet-taxonomy migration Stage 2 — domain_tags metrics."""
from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from scripts.wallet_archetype_extractor import (
    _domain_tags_horizon_metrics,
    _fetch_tag_map,
    _tag_distribution,
    _tags_for_row,
)


# ---------------------------------------------------------------------------
# _fetch_tag_map — SQL prefetch
# ---------------------------------------------------------------------------


def _seed(db_path, rows):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE market_tags (condition_id TEXT NOT NULL, tag TEXT NOT NULL, "
        "tag_label TEXT, ingested_at TEXT, PRIMARY KEY (condition_id, tag))"
    )
    for cid, tag in rows:
        conn.execute(
            "INSERT INTO market_tags (condition_id, tag) VALUES (?, ?)", (cid, tag)
        )
    conn.commit()
    conn.close()


def test_fetch_tag_map_returns_frozensets(tmp_path):
    db = tmp_path / "t.db"
    _seed(db, [("0xaaa", "politics"), ("0xaaa", "world"), ("0xbbb", "sports")])
    out = _fetch_tag_map(["0xAAA", "0xbbb", "0xmissing"], db_path=str(db))
    assert out == {
        "0xaaa": frozenset({"politics", "world"}),
        "0xbbb": frozenset({"sports"}),
    }


def test_fetch_tag_map_handles_empty_input(tmp_path):
    db = tmp_path / "t.db"
    _seed(db, [])
    assert _fetch_tag_map([], db_path=str(db)) == {}
    assert _fetch_tag_map([None, ""], db_path=str(db)) == {}


def test_fetch_tag_map_resilient_to_missing_db():
    # Bogus path → return {} rather than crash
    out = _fetch_tag_map(["0xaaa"], db_path="/tmp/nonexistent/__no__.db")
    assert out == {}


def test_fetch_tag_map_lowercases_query_keys(tmp_path):
    db = tmp_path / "t.db"
    _seed(db, [("0xabc", "politics")])
    out = _fetch_tag_map(["0xABC"], db_path=str(db))
    assert "0xabc" in out
    assert out["0xabc"] == frozenset({"politics"})


# ---------------------------------------------------------------------------
# _tags_for_row
# ---------------------------------------------------------------------------


def test_tags_for_row_reads_conditionId_key():
    tag_map = {"0xaaa": frozenset({"politics"})}
    row = {"conditionId": "0xAAA"}
    assert _tags_for_row(row, tag_map) == frozenset({"politics"})


def test_tags_for_row_missing_key_is_empty():
    assert _tags_for_row({}, {}) == frozenset()
    assert _tags_for_row({"conditionId": "0xzzz"}, {"0xaaa": frozenset({"politics"})}) == frozenset()


# ---------------------------------------------------------------------------
# _tag_distribution — equal-weight envelope shape
# ---------------------------------------------------------------------------


def _row(cid, title="t"):
    return {"conditionId": cid, "title": title}


def test_tag_distribution_counts_multi_tag_membership():
    rows = [
        _row("0xaaa", "alpha"),
        _row("0xbbb", "beta"),
        _row("0xccc", "gamma"),
    ]
    tag_map = {
        "0xaaa": frozenset({"politics", "world"}),
        "0xbbb": frozenset({"politics"}),
        "0xccc": frozenset({"sports"}),
    }
    out = _tag_distribution(rows, tag_map)
    # politics appears on 2/3 rows, world on 1/3, sports on 1/3
    assert out["tag_fill_counts"] == {"politics": 2, "world": 1, "sports": 1}
    # primary_tag_share uses denominator = total_rows, not sum of tag counts
    assert out["primary_tag"] == "politics"
    assert out["primary_tag_share"] == pytest.approx(2 / 3, rel=0.01)
    assert out["distinct_tag_count"] == 3
    assert out["distinct_titles"] == 3
    assert out["unclassified_share"] == 0.0


def test_tag_distribution_reports_unclassified_share():
    rows = [
        _row("0xaaa"),
        _row("0xmissing"),
        _row("0xmissing2"),
    ]
    tag_map = {"0xaaa": frozenset({"politics"})}
    out = _tag_distribution(rows, tag_map)
    # unclassified: 2/3 rows
    assert out["unclassified_share"] == pytest.approx(2 / 3, rel=0.01)
    assert out["primary_tag"] == "politics"
    assert out["primary_tag_share"] == pytest.approx(1 / 3, rel=0.01)


def test_tag_distribution_empty_rows_returns_zeroes():
    out = _tag_distribution([], {})
    assert out["primary_tag"] is None
    assert out["primary_tag_share"] == 0.0
    assert out["distinct_tag_count"] == 0
    assert out["unclassified_share"] == 0.0


def test_tag_distribution_top_tags_sorted_desc():
    rows = [_row(f"0x{i}") for i in range(5)]
    tag_map = {
        "0x0": frozenset({"a"}),
        "0x1": frozenset({"a"}),
        "0x2": frozenset({"a", "b"}),
        "0x3": frozenset({"b", "c"}),
        "0x4": frozenset({"c"}),
    }
    out = _tag_distribution(rows, tag_map)
    # a: 3, b: 2, c: 2 (ties broken alphabetically in our sort)
    top = [(t["tag"], t["count"]) for t in out["top_tags"]]
    assert top[0] == ("a", 3)
    assert top[1] == ("b", 2)
    assert top[2] == ("c", 2)


def test_tag_distribution_no_hardcoded_tag_names():
    """The function must work identically regardless of tag string content."""
    rows = [_row("0xaaa"), _row("0xbbb")]
    # Pass totally made-up future-hypothetical slugs
    tag_map = {
        "0xaaa": frozenset({"zzz-future-tag-2030", "mars-colony"}),
        "0xbbb": frozenset({"zzz-future-tag-2030"}),
    }
    out = _tag_distribution(rows, tag_map)
    assert out["primary_tag"] == "zzz-future-tag-2030"
    assert out["tag_fill_counts"]["zzz-future-tag-2030"] == 2
    # Nothing in the distribution code cares what the tags ARE


# ---------------------------------------------------------------------------
# _tag_distribution — PnL-weighted variant
# ---------------------------------------------------------------------------


def test_tag_distribution_pnl_weighted():
    rows = [
        {"condition_id": "0xaaa", "realized_pnl": 100, "title": "a"},
        {"condition_id": "0xbbb", "realized_pnl": -50, "title": "b"},
        {"condition_id": "0xccc", "realized_pnl": 20, "title": "c"},
    ]
    tag_map = {
        "0xaaa": frozenset({"winner-tag"}),
        "0xbbb": frozenset({"loser-tag"}),
        "0xccc": frozenset({"winner-tag"}),
    }
    # Adapter: extractor rewrites condition_id→conditionId before calling
    rows = [{**r, "conditionId": r["condition_id"]} for r in rows]
    out = _tag_distribution(
        rows,
        tag_map,
        weight_fn=lambda r: abs(float(r.get("realized_pnl", 0) or 0)),
    )
    # total weight = 170; winner-tag = 120, loser-tag = 50
    assert out["tag_fill_counts"]["winner-tag"] == 120
    assert out["tag_fill_counts"]["loser-tag"] == 50
    assert out["primary_tag"] == "winner-tag"
    assert out["primary_tag_share"] == pytest.approx(120 / 170, rel=0.01)


def test_tag_distribution_skips_zero_weight_rows():
    rows = [
        {"conditionId": "0xaaa", "realized_pnl": 100, "title": "a"},
        {"conditionId": "0xbbb", "realized_pnl": 0, "title": "b"},  # zero weight → skipped
    ]
    tag_map = {
        "0xaaa": frozenset({"a"}),
        "0xbbb": frozenset({"b"}),
    }
    out = _tag_distribution(
        rows,
        tag_map,
        weight_fn=lambda r: abs(float(r.get("realized_pnl", 0) or 0)),
    )
    assert out["tag_fill_counts"] == {"a": 100}
    assert out["primary_tag"] == "a"


# ---------------------------------------------------------------------------
# _domain_tags_horizon_metrics — three-horizon split
# ---------------------------------------------------------------------------


def test_horizon_metrics_detects_rotation():
    """Recent trade primary ≠ live book primary → rotation_signal=True."""
    trades = [_row("0xaaa"), _row("0xaaa"), _row("0xaaa")]
    positions = [_row("0xbbb"), _row("0xbbb")]
    resolved = []
    tag_map = {
        "0xaaa": frozenset({"sports"}),
        "0xbbb": frozenset({"politics"}),
    }
    out = _domain_tags_horizon_metrics(trades, positions, resolved, tag_map)
    assert out["recent_trade"]["primary_tag"] == "sports"
    assert out["live_book"]["primary_tag"] == "politics"
    assert out["alignment"]["recent_live_primary_match"] is False
    assert out["rotation_signal"] is True


def test_horizon_metrics_aligns_when_primaries_match():
    trades = [_row("0xaaa"), _row("0xaaa")]
    positions = [_row("0xaaa"), _row("0xaaa")]
    resolved = []
    tag_map = {"0xaaa": frozenset({"politics"})}
    out = _domain_tags_horizon_metrics(trades, positions, resolved, tag_map)
    assert out["alignment"]["recent_live_primary_match"] is True
    assert out["rotation_signal"] is False


def test_horizon_metrics_realized_pnl_uses_abs_pnl_weighting():
    resolved = [
        {"condition_id": "0xaaa", "realized_pnl": 200, "title": "a"},
        {"condition_id": "0xaaa", "realized_pnl": -150, "title": "a"},
        {"condition_id": "0xbbb", "realized_pnl": 50, "title": "b"},
    ]
    tag_map = {
        "0xaaa": frozenset({"bigmoney"}),
        "0xbbb": frozenset({"smallmoney"}),
    }
    out = _domain_tags_horizon_metrics([], [], resolved, tag_map)
    # bigmoney: |200| + |150| = 350; smallmoney: |50| = 50
    assert out["realized_pnl"]["primary_tag"] == "bigmoney"


def test_horizon_metrics_rotation_signal_when_top_overlap_low():
    """Even when primaries agree, low Jaccard overlap of top-tag sets fires rotation."""
    # Build rows where recent and live share only their #1 tag
    trades = [_row(f"0xr{i}") for i in range(5)]
    positions = [_row(f"0xl{i}") for i in range(5)]
    tag_map = {}
    for i in range(5):
        tag_map[f"0xr{i}"] = frozenset({"shared", f"recent-only-{i}"})
        tag_map[f"0xl{i}"] = frozenset({"shared", f"live-only-{i}"})
    out = _domain_tags_horizon_metrics(trades, positions, [], tag_map)
    # Primaries match (both "shared"), but top-5 sets are mostly disjoint
    assert out["alignment"]["recent_live_primary_match"] is True
    assert out["alignment"]["top_tag_overlap_recent_live_jaccard"] < 0.3
    assert out["rotation_signal"] is True
