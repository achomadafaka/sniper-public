"""Acceptance tests for `db.peer_baseline_for_grain`.

Closure of gap C3 (wallet-protocol-bulletproofing spec): the held-branch H5
analyst takes a `peer_baseline_fn(grain, tag) -> (mean_wr, peer_count) | None`
callable. This reader is the SQLite-backed concrete implementation against
the existing `wallet_tag_pnl` table.

Contract:
    - Returns `None` when the table is missing, the grain is unsupported,
      or no peers meet the n_held_to_resolution floor.
    - Returns `(mean_wr, peer_count)` where mean_wr is the equal-weighted
      average of `win_rate_held_only` across qualifying peers.
    - "leaf" grain compares directly against `wallet_tag_pnl.tag`.
    - "nav" grain joins `wallet_tag_pnl.tag` through `tag_nav_category`
      and aggregates each peer wallet once within that nav category.
"""

from __future__ import annotations

import sqlite3

import pytest


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE wallet_tag_pnl (
            wallet                    TEXT    NOT NULL,
            tag                       TEXT    NOT NULL,
            n_positions               INTEGER NOT NULL,
            realized_pnl              REAL    NOT NULL,
            win_rate                  REAL    NOT NULL,
            conviction_concentration  REAL    NOT NULL,
            computed_at               INTEGER NOT NULL,
            win_rate_held_only        REAL,
            n_held_to_resolution      INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (wallet, tag)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE tag_nav_category (
            tag           TEXT PRIMARY KEY,
            nav_category  TEXT NOT NULL,
            confidence    TEXT NOT NULL DEFAULT 'manual',
            updated_at    INTEGER NOT NULL
        )
        """
    )
    return conn


def _insert(conn, wallet, tag, *, n_held, wr_held, win_rate=None):
    conn.execute(
        "INSERT INTO wallet_tag_pnl(wallet, tag, n_positions, realized_pnl, "
        "win_rate, conviction_concentration, computed_at, win_rate_held_only, "
        "n_held_to_resolution) VALUES (?,?,?,?,?,?,?,?,?)",
        (wallet, tag, n_held, 0.0, win_rate if win_rate is not None else wr_held,
         0.0, 0, wr_held, n_held),
    )


def test_returns_none_when_table_missing():
    import db
    conn = sqlite3.connect(":memory:")
    assert db.peer_baseline_for_grain(conn, "leaf", "geopolitics") is None


def test_returns_none_when_no_rows_for_tag():
    import db
    conn = _make_conn()
    _insert(conn, "0xa", "sports", n_held=20, wr_held=0.6)
    assert db.peer_baseline_for_grain(conn, "leaf", "geopolitics") is None


def test_returns_none_when_all_peers_below_floor():
    import db
    conn = _make_conn()
    _insert(conn, "0xa", "geopolitics", n_held=5, wr_held=0.5)
    _insert(conn, "0xb", "geopolitics", n_held=9, wr_held=0.6)
    # default min_n_held=10, so both filtered out
    assert db.peer_baseline_for_grain(conn, "leaf", "geopolitics") is None


def test_returns_none_when_held_wr_is_null():
    import db
    conn = _make_conn()
    # peer with NULL win_rate_held_only — exclude from mean
    conn.execute(
        "INSERT INTO wallet_tag_pnl(wallet, tag, n_positions, realized_pnl, "
        "win_rate, conviction_concentration, computed_at, win_rate_held_only, "
        "n_held_to_resolution) VALUES (?,?,?,?,?,?,?,NULL,?)",
        ("0xa", "geopolitics", 20, 0.0, 0.5, 0.0, 0, 20),
    )
    assert db.peer_baseline_for_grain(conn, "leaf", "geopolitics") is None


def test_equal_weighted_mean_two_peers():
    import db
    conn = _make_conn()
    _insert(conn, "0xa", "geopolitics", n_held=20, wr_held=0.5)
    _insert(conn, "0xb", "geopolitics", n_held=15, wr_held=0.7)
    result = db.peer_baseline_for_grain(conn, "leaf", "geopolitics")
    assert result is not None
    mean_wr, peer_count = result
    assert peer_count == 2
    assert mean_wr == pytest.approx(0.6, abs=1e-9)


def test_floor_excludes_under_threshold_peers():
    import db
    conn = _make_conn()
    _insert(conn, "0xa", "geopolitics", n_held=20, wr_held=0.5)
    _insert(conn, "0xb", "geopolitics", n_held=20, wr_held=0.7)
    _insert(conn, "0xc", "geopolitics", n_held=5, wr_held=0.95)  # excluded
    result = db.peer_baseline_for_grain(conn, "leaf", "geopolitics")
    assert result is not None
    mean_wr, peer_count = result
    assert peer_count == 2
    assert mean_wr == pytest.approx(0.6, abs=1e-9)


def test_min_n_held_override():
    import db
    conn = _make_conn()
    _insert(conn, "0xa", "geopolitics", n_held=5, wr_held=0.5)
    _insert(conn, "0xb", "geopolitics", n_held=6, wr_held=0.7)
    # tighter floor than default (10) excludes both
    assert db.peer_baseline_for_grain(
        conn, "leaf", "geopolitics", min_n_held=10
    ) is None
    # looser floor includes both
    result = db.peer_baseline_for_grain(
        conn, "leaf", "geopolitics", min_n_held=5,
    )
    assert result == pytest.approx((0.6, 2))


def test_nav_grain_aggregates_peer_wallets_once():
    import db
    conn = _make_conn()
    conn.executemany(
        "INSERT INTO tag_nav_category(tag, nav_category, confidence, updated_at) "
        "VALUES (?, ?, ?, 0)",
        [
            ("nba", "sports", "manual"),
            ("basketball", "sports", "manual"),
            ("awards", "sports", "ambiguous"),
        ],
    )
    _insert(conn, "0xa", "nba", n_held=10, wr_held=0.4)
    _insert(conn, "0xa", "basketball", n_held=30, wr_held=0.8)
    _insert(conn, "0xb", "nba", n_held=20, wr_held=0.6)
    _insert(conn, "0xc", "awards", n_held=20, wr_held=1.0)

    result = db.peer_baseline_for_grain(conn, "nav", "sports")

    assert result is not None
    mean_wr, peer_count = result
    assert peer_count == 2
    # 0xa nav WR is weighted by held samples: (10*.4 + 30*.8) / 40 = .7.
    # 0xb nav WR is .6. Ambiguous "awards" row is excluded.
    assert mean_wr == pytest.approx(0.65, abs=1e-9)


def test_nav_grain_exclude_wallet_removes_subject_peer():
    import db
    conn = _make_conn()
    conn.executemany(
        "INSERT INTO tag_nav_category(tag, nav_category, confidence, updated_at) "
        "VALUES (?, ?, ?, 0)",
        [("nba", "sports", "manual"), ("basketball", "sports", "manual")],
    )
    _insert(conn, "0xa", "nba", n_held=20, wr_held=0.5)
    _insert(conn, "0xb", "basketball", n_held=20, wr_held=0.7)

    assert db.peer_baseline_for_grain(
        conn, "nav", "sports", exclude_wallet="0xa",
    ) == pytest.approx((0.7, 1))


def test_unknown_grain_returns_none():
    import db
    conn = _make_conn()
    _insert(conn, "0xa", "geopolitics", n_held=20, wr_held=0.5)
    assert db.peer_baseline_for_grain(conn, "made_up", "geopolitics") is None


def test_exclude_wallet_removes_self_from_peer_set():
    """Self-contamination guard: the analyzed wallet must not appear in its
    own peer baseline (gap C3 follow-up bulletproofing).
    """
    import db
    conn = _make_conn()
    _insert(conn, "0xa", "geopolitics", n_held=20, wr_held=0.5)
    _insert(conn, "0xb", "geopolitics", n_held=20, wr_held=0.7)
    _insert(conn, "0xc", "geopolitics", n_held=20, wr_held=0.9)
    # Without exclude_wallet → 3 peers, mean = 0.7
    full = db.peer_baseline_for_grain(conn, "leaf", "geopolitics")
    assert full == pytest.approx((0.7, 3))
    # Excluding 0xa → 2 peers, mean = 0.8
    excl = db.peer_baseline_for_grain(
        conn, "leaf", "geopolitics", exclude_wallet="0xa",
    )
    assert excl is not None
    mean_wr, peer_count = excl
    assert peer_count == 2
    assert mean_wr == pytest.approx(0.8, abs=1e-9)
    # exclude_wallet=None preserves baseline behaviour
    none = db.peer_baseline_for_grain(
        conn, "leaf", "geopolitics", exclude_wallet=None,
    )
    assert none == pytest.approx((0.7, 3))


def test_exclude_wallet_below_floor_after_self_drop():
    """If excluding the analyzed wallet drops peer count below the floor for
    a non-default `min_n_held`, return None."""
    import db
    conn = _make_conn()
    _insert(conn, "0xa", "geopolitics", n_held=20, wr_held=0.5)
    _insert(conn, "0xb", "geopolitics", n_held=20, wr_held=0.7)
    # Without exclude → 2 qualifying peers
    assert db.peer_baseline_for_grain(conn, "leaf", "geopolitics") == pytest.approx((0.6, 2))
    # Excluding 0xa leaves 1 qualifying peer — still returns it (no floor on count;
    # the inference floor is enforced upstream in held_analyst).
    excl = db.peer_baseline_for_grain(
        conn, "leaf", "geopolitics", exclude_wallet="0xa",
    )
    assert excl == pytest.approx((0.7, 1))
    # Excluding both → None (no rows)
    conn.execute("DELETE FROM wallet_tag_pnl WHERE wallet = '0xb'")
    excl2 = db.peer_baseline_for_grain(
        conn, "leaf", "geopolitics", exclude_wallet="0xa",
    )
    assert excl2 is None


def test_compatible_with_h5_caller_signature():
    """Smoke-test: caller in held_analyst._build_h5_grain_results does
    `baseline_info[1] if baseline_info else 0` — both code paths must be safe.
    """
    import db
    conn = _make_conn()
    _insert(conn, "0xa", "geopolitics", n_held=20, wr_held=0.5)
    info_present = db.peer_baseline_for_grain(conn, "leaf", "geopolitics")
    info_absent = db.peer_baseline_for_grain(conn, "leaf", "no_such_tag")
    # caller-style probe must not raise
    pc1 = info_present[1] if info_present else 0
    pc2 = info_absent[1] if info_absent else 0
    assert pc1 == 1
    assert pc2 == 0
