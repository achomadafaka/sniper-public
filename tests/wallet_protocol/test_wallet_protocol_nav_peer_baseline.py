"""C3b acceptance: nav-grain peer baselines are wired into H5."""

from __future__ import annotations

import sqlite3

import db
from sniper.analysis.wallet_protocol.held_analyst import run_held_branch
from sniper.analysis.wallet_protocol.types import WalletData


def _wallet_data(closed: list[dict]) -> WalletData:
    return WalletData(
        wallet="0xsubject",
        closed_positions=closed,
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=1_700_000_000,
    )


def _closed_position(i: int) -> dict:
    return {
        "conditionId": f"cid_{i}",
        "outcome": "Yes",
        "curPrice": "1",
        "avgPrice": "0.5",
        "size": "10",
        "totalBought": "5",
        "realizedPnl": "5",
        "timestamp": 1_700_000_000 + i,
    }


def _seed_nav_peers(conn: sqlite3.Connection) -> None:
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
    conn.execute(
        "INSERT INTO tag_nav_category(tag, nav_category, confidence, updated_at) "
        "VALUES ('nba', 'sports', 'manual', 0)"
    )
    for i in range(10):
        conn.execute(
            "INSERT INTO wallet_tag_pnl(wallet, tag, n_positions, realized_pnl, "
            "win_rate, conviction_concentration, computed_at, win_rate_held_only, "
            "n_held_to_resolution) VALUES (?, 'nba', 20, 0, 0.4, 0, 0, 0.4, 20)",
            (f"0xpeer{i}",),
        )


def test_run_held_branch_nav_grain_uses_db_peer_baseline():
    conn = sqlite3.connect(":memory:")
    _seed_nav_peers(conn)
    closed = [_closed_position(i) for i in range(20)]
    leafs_by_cid = {f"cid_{i}": ["nba"] for i in range(20)}
    nav_lookup = {"nba": "sports"}

    report = run_held_branch(
        _wallet_data(closed),
        peer_baseline_fn=lambda grain, tag, wallet: db.peer_baseline_for_grain(
            conn, grain, tag, exclude_wallet=wallet,
        ),
        leafs_by_cid=leafs_by_cid,
        nav_lookup=nav_lookup,
        n_iter=500,
    )

    nav_grain = next(g for g in report.tag_grains if g.grain == "nav")
    assert nav_grain.tag == "sports"
    assert nav_grain.peer_count == 10
    assert nav_grain.verdict != "inference_not_run"
