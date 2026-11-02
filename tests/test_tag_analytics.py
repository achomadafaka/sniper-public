"""Tests for sniper/analysis/tag_analytics.py — Phase 1 (tag_market_stats).

Fixtures are synthetic in-memory sqlite. The goal is to prove the math
exactly; we don't hardcode tag slugs in assertions against real data.
"""

from __future__ import annotations

import sqlite3

import pytest

from sniper.analysis.tag_analytics import (
    compute_tag_market_stats,
    summarize_coverage,
)


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE market_tags (condition_id TEXT, tag TEXT, tag_label TEXT, "
        "PRIMARY KEY(condition_id, tag))"
    )
    conn.execute(
        "CREATE TABLE market_outcomes (condition_id TEXT, outcome TEXT, "
        "resolved_price REAL, PRIMARY KEY(condition_id, outcome))"
    )
    conn.execute(
        "CREATE TABLE market_metadata (condition_id TEXT PRIMARY KEY, "
        "volume_24h REAL)"
    )
    return conn


def _insert_tag(conn, cid: str, tag: str, label: str = "") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO market_tags(condition_id, tag, tag_label) VALUES (?,?,?)",
        (cid, tag, label),
    )


def _insert_binary_resolved(conn, cid: str, winning_side: str) -> None:
    """winning_side must be 'yes' or 'no'."""
    yes_p = 1.0 if winning_side == "yes" else 0.0
    no_p = 1.0 if winning_side == "no" else 0.0
    conn.execute(
        "INSERT OR REPLACE INTO market_outcomes(condition_id, outcome, resolved_price) VALUES (?,?,?)",
        (cid, "yes", yes_p),
    )
    conn.execute(
        "INSERT OR REPLACE INTO market_outcomes(condition_id, outcome, resolved_price) VALUES (?,?,?)",
        (cid, "no", no_p),
    )


def _insert_volume(conn, cid: str, volume: float) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO market_metadata(condition_id, volume_24h) VALUES (?,?)",
        (cid, volume),
    )


def test_single_tag_yes_heavy_share():
    conn = _make_conn()
    # 4 markets under tag "alpha", 3 resolve YES, 1 resolves NO
    for i, side in enumerate(["yes", "yes", "yes", "no"]):
        cid = f"0x{i:02d}"
        _insert_tag(conn, cid, "alpha")
        _insert_binary_resolved(conn, cid, side)

    stats = compute_tag_market_stats(conn)
    alpha = stats["alpha"]
    assert alpha["n_markets"] == 4
    assert alpha["n_resolved"] == 4
    assert alpha["n_binary_resolved"] == 4
    assert alpha["resolved_yes_count"] == 3
    assert alpha["resolved_no_count"] == 1
    assert alpha["yes_share"] == pytest.approx(0.75)


def test_yes_share_none_when_no_binary_resolved():
    conn = _make_conn()
    _insert_tag(conn, "0x01", "unresolved-tag")
    # no market_outcomes row → not resolved
    stats = compute_tag_market_stats(conn)
    row = stats["unresolved-tag"]
    assert row["n_markets"] == 1
    assert row["n_resolved"] == 0
    assert row["n_binary_resolved"] == 0
    assert row["yes_share"] is None
    assert row["avg_resolved_price_yes_side"] is None


def test_multi_outcome_markets_excluded_from_yes_share():
    conn = _make_conn()
    # Insert a multi-outcome market under tag "sports": team-A wins
    _insert_tag(conn, "0xmulti", "sports")
    conn.execute(
        "INSERT INTO market_outcomes(condition_id, outcome, resolved_price) VALUES (?,?,?)",
        ("0xmulti", "team-a", 1.0),
    )
    conn.execute(
        "INSERT INTO market_outcomes(condition_id, outcome, resolved_price) VALUES (?,?,?)",
        ("0xmulti", "team-b", 0.0),
    )
    stats = compute_tag_market_stats(conn)
    s = stats["sports"]
    assert s["n_markets"] == 1
    assert s["n_resolved"] == 1  # counted as resolved
    assert s["n_binary_resolved"] == 0  # no yes/no row → excluded from binary aggregate
    assert s["yes_share"] is None


def test_co_occurring_tags_top5():
    conn = _make_conn()
    # cid A: alpha + beta + gamma
    # cid B: alpha + beta
    # cid C: alpha + gamma
    # cid D: alpha
    # under alpha: beta co-occurs 2x, gamma 2x
    for cid, tags in [
        ("0xA", ["alpha", "beta", "gamma"]),
        ("0xB", ["alpha", "beta"]),
        ("0xC", ["alpha", "gamma"]),
        ("0xD", ["alpha"]),
    ]:
        for t in tags:
            _insert_tag(conn, cid, t)

    stats = compute_tag_market_stats(conn)
    alpha_co = dict(stats["alpha"]["distinct_co_occurring_tags_top5"])
    assert alpha_co.get("beta") == 2
    assert alpha_co.get("gamma") == 2
    # alpha itself should not be in its own co-occurrence list
    assert "alpha" not in alpha_co


def test_median_volume_over_tagged_markets():
    conn = _make_conn()
    vols = [100.0, 200.0, 300.0, 400.0]
    for i, v in enumerate(vols):
        cid = f"0x{i:02d}"
        _insert_tag(conn, cid, "alpha")
        _insert_volume(conn, cid, v)
    stats = compute_tag_market_stats(conn)
    # median of [100, 200, 300, 400] = 250
    assert stats["alpha"]["median_market_volume_24h"] == pytest.approx(250.0)


def test_avg_resolved_price_yes_side_averages_across_binary():
    conn = _make_conn()
    # 2 YES wins, 1 NO win: prices yes=[1.0, 1.0, 0.0] → mean 0.6666
    _insert_tag(conn, "0x01", "alpha")
    _insert_binary_resolved(conn, "0x01", "yes")
    _insert_tag(conn, "0x02", "alpha")
    _insert_binary_resolved(conn, "0x02", "yes")
    _insert_tag(conn, "0x03", "alpha")
    _insert_binary_resolved(conn, "0x03", "no")
    stats = compute_tag_market_stats(conn)
    assert stats["alpha"]["avg_resolved_price_yes_side"] == pytest.approx(
        2.0 / 3.0
    )


def test_min_markets_filters_long_tail():
    conn = _make_conn()
    _insert_tag(conn, "0xA", "popular")
    _insert_tag(conn, "0xB", "popular")
    _insert_tag(conn, "0xC", "popular")
    _insert_tag(conn, "0xA", "rare")  # only 1 market tagged 'rare'

    stats = compute_tag_market_stats(conn, min_markets=2)
    assert "popular" in stats
    assert "rare" not in stats


def test_tag_appears_even_with_no_resolution_data():
    conn = _make_conn()
    _insert_tag(conn, "0xA", "future-tag")
    stats = compute_tag_market_stats(conn)
    assert "future-tag" in stats
    assert stats["future-tag"]["n_markets"] == 1
    assert stats["future-tag"]["n_resolved"] == 0
    assert stats["future-tag"]["yes_share"] is None


def test_summarize_coverage_reports_tag_gap():
    conn = _make_conn()
    # 3 binary resolved markets; only 1 has tags
    _insert_binary_resolved(conn, "0xA", "yes")
    _insert_binary_resolved(conn, "0xB", "no")
    _insert_binary_resolved(conn, "0xC", "yes")
    _insert_tag(conn, "0xA", "covered")

    cov = summarize_coverage(conn)
    assert cov["binary_markets_total"] == 3
    assert cov["binary_markets_with_tags"] == 1
    assert cov["resolved_markets_total"] == 3
    assert cov["resolved_markets_with_tags"] == 1
    assert cov["tag_coverage_pct_of_resolved"] == pytest.approx(1.0 / 3.0)


def test_opaque_tag_discipline_unknown_slugs_supported():
    """Sanity: arbitrary slug strings (emoji, non-ascii, numbers) are
    treated identically. Opaque-tag principle."""
    conn = _make_conn()
    for cid, tag in [
        ("0x01", "some-new-2026-slug"),
        ("0x02", "12345"),
        ("0x03", "multi-word-slug-with-dashes"),
    ]:
        _insert_tag(conn, cid, tag)
        _insert_binary_resolved(conn, cid, "yes")
    stats = compute_tag_market_stats(conn)
    assert len(stats) == 3
    for tag in stats:
        assert stats[tag]["yes_share"] == 1.0


def test_yes_and_no_counts_sum_to_binary_resolved():
    conn = _make_conn()
    for i in range(7):
        cid = f"0x{i:02d}"
        _insert_tag(conn, cid, "alpha")
        _insert_binary_resolved(conn, cid, "yes" if i % 2 == 0 else "no")
    stats = compute_tag_market_stats(conn)
    row = stats["alpha"]
    assert row["resolved_yes_count"] + row["resolved_no_count"] == row["n_binary_resolved"]


# =============================================================================
# Workstream D: tag_market_stats persistence round-trip
# =============================================================================

import json  # noqa: E402

from sniper.analysis.tag_analytics import (  # noqa: E402
    TagMarketStats,
    get_tag_market_stats,
    persist_tag_market_stats,
)
from sniper.persistence.db_layer5 import ensure_tag_market_stats  # noqa: E402


def _make_conn_with_tag_market_stats() -> sqlite3.Connection:
    """Reuses the source-table fixture and adds the destination table.
    Sets ``row_factory`` so direct ``conn.execute(...).fetchone()`` returns
    ``sqlite3.Row`` (the existing fixtures don't need it because they only
    inspect ``compute_tag_market_stats`` output dicts)."""
    conn = _make_conn()
    conn.row_factory = sqlite3.Row
    ensure_tag_market_stats(conn)
    return conn


def test_persist_round_trip_from_compute_output():
    """compute_tag_market_stats → persist → get returns matching values."""
    conn = _make_conn_with_tag_market_stats()
    for i, side in enumerate(["yes", "yes", "yes", "no"]):
        cid = f"0x{i:02d}"
        _insert_tag(conn, cid, "alpha")
        _insert_binary_resolved(conn, cid, side)
        _insert_volume(conn, cid, 1000.0)
    _insert_tag(conn, "0x00", "beta")  # co-occurring on first market

    stats = compute_tag_market_stats(conn)
    persist_tag_market_stats(conn, stats, computed_at=1700000000)

    got = get_tag_market_stats(conn, "alpha")
    assert got is not None
    assert isinstance(got, TagMarketStats)
    assert got.tag == "alpha"
    assert got.n_markets == 4
    assert got.n_resolved == 4
    assert got.resolved_yes_count == 3
    assert got.yes_share == pytest.approx(0.75)
    assert got.median_market_volume_24h == pytest.approx(1000.0)
    assert got.computed_at == 1700000000
    assert got.inputs_version == "v1"
    # Co-tag entries are list of (tag, count) tuples; alpha's first market also
    # tagged 'beta' so beta should appear with count=1.
    assert ("beta", 1) in got.top_co_tags


def test_persist_idempotent_upsert():
    """Running persist twice produces ONE row per tag with identical content."""
    conn = _make_conn_with_tag_market_stats()
    _insert_tag(conn, "0x01", "alpha")
    _insert_binary_resolved(conn, "0x01", "yes")

    stats = compute_tag_market_stats(conn)
    persist_tag_market_stats(conn, stats, computed_at=1700000000)
    first = list(conn.execute("SELECT * FROM tag_market_stats"))

    persist_tag_market_stats(conn, stats, computed_at=1700000000)
    second = list(conn.execute("SELECT * FROM tag_market_stats"))

    assert len(first) == 1
    assert len(second) == 1
    # All columns identical across the two writes.
    assert dict(first[0]) == dict(second[0])


def test_persist_upsert_overwrites_on_change():
    """Same tag, different stats → row updated in place (no duplicate PK)."""
    conn = _make_conn_with_tag_market_stats()
    _insert_tag(conn, "0x01", "alpha")
    _insert_binary_resolved(conn, "0x01", "yes")

    stats_v1 = compute_tag_market_stats(conn)
    persist_tag_market_stats(conn, stats_v1, computed_at=1700000000)

    # Add a second market under alpha and recompute.
    _insert_tag(conn, "0x02", "alpha")
    _insert_binary_resolved(conn, "0x02", "no")
    stats_v2 = compute_tag_market_stats(conn)
    persist_tag_market_stats(conn, stats_v2, computed_at=1700000100)

    rows = list(conn.execute("SELECT * FROM tag_market_stats"))
    assert len(rows) == 1
    got = get_tag_market_stats(conn, "alpha")
    assert got.n_markets == 2
    assert got.computed_at == 1700000100  # newer write won


def test_get_returns_none_for_unknown_tag():
    conn = _make_conn_with_tag_market_stats()
    assert get_tag_market_stats(conn, "absolutely_nonexistent") is None


def test_persist_empty_input_writes_nothing():
    conn = _make_conn_with_tag_market_stats()
    persist_tag_market_stats(conn, {}, computed_at=1700000000)
    rows = list(conn.execute("SELECT COUNT(*) AS c FROM tag_market_stats"))
    assert rows[0]["c"] == 0


def test_persist_from_explicit_dataclass_list():
    """Plan signature: ``persist_tag_market_stats(conn, list[TagMarketStats])``.
    Supports the typed-list shape directly (not just compute output)."""
    conn = _make_conn_with_tag_market_stats()
    payload = [
        TagMarketStats(
            tag="political",
            n_markets=200,
            n_resolved=180,
            resolved_yes_count=90,
            yes_share=0.5,
            avg_resolved_price_yes=0.55,
            median_market_volume_24h=1500.0,
            top_co_tags=[("election", 40), ("usa", 25)],
            computed_at=1700000000,
            inputs_version="v1",
        ),
        TagMarketStats(
            tag="sports",
            n_markets=80,
            n_resolved=70,
            resolved_yes_count=35,
            yes_share=0.5,
            avg_resolved_price_yes=0.5,
            median_market_volume_24h=500.0,
            top_co_tags=[],
            computed_at=1700000000,
            inputs_version="v1",
        ),
    ]
    persist_tag_market_stats(conn, payload)

    pol = get_tag_market_stats(conn, "political")
    assert pol is not None
    assert pol.n_markets == 200
    assert pol.top_co_tags == [("election", 40), ("usa", 25)]
    spo = get_tag_market_stats(conn, "sports")
    assert spo is not None
    assert spo.top_co_tags == []


def test_persisted_top_co_tags_serialized_as_json_array():
    """Storage layer represents top_co_tags as JSON in top_co_tags_json column."""
    conn = _make_conn_with_tag_market_stats()
    payload = [
        TagMarketStats(
            tag="x",
            n_markets=1,
            n_resolved=1,
            resolved_yes_count=1,
            yes_share=1.0,
            avg_resolved_price_yes=0.99,
            median_market_volume_24h=None,
            top_co_tags=[("a", 5), ("b", 3)],
            computed_at=1700000000,
        ),
    ]
    persist_tag_market_stats(conn, payload)
    row = conn.execute(
        "SELECT top_co_tags_json FROM tag_market_stats WHERE tag = 'x'"
    ).fetchone()
    decoded = json.loads(row["top_co_tags_json"])
    # JSON arrays of [tag, count] preserve order.
    assert decoded == [["a", 5], ["b", 3]]


def test_default_computed_at_uses_current_time(monkeypatch):
    """When computed_at is None, persist stamps int(time.time()) at write."""
    import sniper.analysis.tag_analytics as ta

    conn = _make_conn_with_tag_market_stats()
    monkeypatch.setattr(ta.time, "time", lambda: 1750000000.7)
    persist_tag_market_stats(
        conn,
        [
            TagMarketStats(
                tag="t",
                n_markets=0, n_resolved=0, resolved_yes_count=0,
                yes_share=None, avg_resolved_price_yes=None,
                median_market_volume_24h=None,
                top_co_tags=[], computed_at=None,  # type: ignore[arg-type]
            ),
        ],
    )
    got = get_tag_market_stats(conn, "t")
    assert got is not None
    assert got.computed_at == 1750000000  # int truncation


def test_a11_dict_reader_consumes_d_writes():
    """Workstream A's wallet_protocol/tag_market_stats_reader (dict shape)
    successfully reads rows written by D's persist_tag_market_stats."""
    from sniper.analysis.wallet_protocol.tag_market_stats_reader import (
        fetch_tag_market_stats as a11_fetch,
        is_tag_market_stats_available as a11_available,
    )

    conn = _make_conn_with_tag_market_stats()
    persist_tag_market_stats(
        conn,
        [
            TagMarketStats(
                tag="political",
                n_markets=200,
                n_resolved=180,
                resolved_yes_count=90,
                yes_share=0.5,
                avg_resolved_price_yes=0.55,
                median_market_volume_24h=1500.0,
                top_co_tags=[("election", 40)],
                computed_at=1700000000,
            ),
        ],
    )
    assert a11_available(conn, "political") is True
    raw = a11_fetch(conn, "political")
    assert raw is not None
    assert raw["tag"] == "political"
    assert raw["n_markets"] == 200
    assert raw["yes_share"] == 0.5
    # A11's reader returns the JSON column verbatim; D's reader decodes it.
    assert raw["top_co_tags_json"] == '[["election", 40]]'
