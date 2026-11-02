import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_wallet_recommender import wallet_recommendations


NOW = "2026-07-17T12:00:00+00:00"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _position(conn, *, event, arm, pnl, uid="u1", handle="alice", network="4663",
              closed="2026-07-17T11:00:00+00:00", mint=None):
    cur = conn.execute(
        "INSERT INTO memecoin_positions(source_user_id, source_handle, network_id, mint, "
        "strategy, execution_mode, is_open, realized_pnl, origin_event_id, opened_at, "
        "closed_at) VALUES(?,?,?,?,?,'paper',0,?,?,?,?)",
        (uid, handle, network, mint or "mint-" + (event or "legacy"), arm, pnl, event,
         "2026-07-17T10:00:00+00:00", closed),
    )
    pid = cur.lastrowid
    # Entry + executable exit fills. The matrix requires positive entry quantity and an observed
    # exit (2026-08-04), matching the report's measurable-outcome test; production positions
    # always carry both. Callers that want a fomo/censored row add their own leg on top.
    conn.execute("UPDATE memecoin_positions SET entry_price=0.001, entry_notional=100.0, "
                 "entry_fill_source='route_quote' WHERE id=?", (pid,))
    m.insert_fill(conn, position_id=pid, kind="entry", price=0.001, qty=100_000.0,
                  notional=100.0, fill_source="route_quote", fraction=1.0, created_at=closed)
    m.insert_fill(conn, position_id=pid, kind="bracket_exit", price=0.0011, qty=100_000.0,
                  notional=110.0, fill_source="dexscreener", fraction=1.0, created_at=closed)
    return pid


def _paired(conn, control, shadow, c2=None, **kwargs):
    uid = kwargs.get("uid", "u1")
    handle = kwargs.get("handle", "alice")
    m.add_wallet(conn, uid, handle)
    for index, (a, b) in enumerate(zip(control, shadow), 1):
        event = f"e{index}"
        _position(conn, event=event, arm="control", pnl=a, **kwargs)
        _position(conn, event=event, arm="real_shadow", pnl=b, **kwargs)
        if c2 is not None:
            _position(conn, event=event, arm="scalp_c2", pnl=c2[index - 1], **kwargs)
    conn.commit()


def test_promote_requires_robust_control_and_shadow_exact_pairs():
    conn = _conn()
    _paired(conn, [10, 9, 8, 7, 6], [11, 10, 9, 8, 7], [5, 5, 5, 5, 5])

    report = wallet_recommendations(conn, min_paired=5, now_iso=NOW)
    row = report["recommendations"][0]
    assert row["diagnostic_recommendation"] == "promote"
    assert row["network_id"] == "4663"
    assert row["matched_primary"] == 5
    assert row["all_three_matched"] == 5
    assert row["exact_overlap_pct"] == 100.0
    assert row["metrics"]["control"]["pnl"] == 40.0
    assert row["metrics"]["control"]["drop_top_two_pnl"] == 21.0
    assert row["metrics"]["real_shadow"]["drop_top_two_pnl"] == 24.0
    assert row["metrics"]["scalp_c2"]["n"] == 5


def test_positive_but_tail_dependent_wallet_stays_watch():
    conn = _conn()
    _paired(conn, [100, 50, -10, -10, -10], [90, 40, -5, -5, -5])
    row = wallet_recommendations(conn, min_paired=5, now_iso=NOW)["recommendations"][0]
    assert row["diagnostic_recommendation"] == "watch"
    assert "control_tail_dependent" in row["diagnostic_reason_codes"]
    assert "real_shadow_tail_dependent" in row["diagnostic_reason_codes"]


def test_mature_negative_wallet_is_avoid_and_drawdown_is_realized_series():
    conn = _conn()
    _paired(conn, [-5, -4, 2, -3, -2], [-6, -5, 1, -4, -3])
    row = wallet_recommendations(conn, min_paired=5, now_iso=NOW)["recommendations"][0]
    assert row["diagnostic_recommendation"] == "avoid"
    assert row["diagnostic_reason_codes"] == ["both_primary_arms_nonpositive"]
    assert row["metrics"]["control"]["max_drawdown"] == 12.0


def test_thin_sample_is_watch_and_c2_does_not_create_wallet_discovery():
    conn = _conn()
    _paired(conn, [20, 20], [20, 20], [20, 20])
    _position(conn, event="c2-only", arm="scalp_c2", pnl=500, uid="u2", handle="bob")
    conn.commit()
    rows = wallet_recommendations(conn, min_paired=5, now_iso=NOW)["recommendations"]
    assert len(rows) == 1
    assert rows[0]["source_user_id"] == "u1"
    assert rows[0]["diagnostic_recommendation"] == "watch"
    assert "thin_primary_overlap" in rows[0]["diagnostic_reason_codes"]


def test_chain_is_part_of_recommendation_identity_and_filter():
    conn = _conn()
    _paired(conn, [10] * 5, [10] * 5, network="4663")
    _paired(conn, [-10] * 5, [-10] * 5, network="8453")
    rows = wallet_recommendations(conn, min_paired=5, now_iso=NOW)["recommendations"]
    assert [(row["network_id"], row["diagnostic_recommendation"]) for row in rows] == [
        ("4663", "promote"), ("8453", "avoid"),
    ]
    filtered = wallet_recommendations(
        conn, min_paired=5, chain="8453", now_iso=NOW,
    )["recommendations"]
    assert len(filtered) == 1 and filtered[0]["network_id"] == "8453"


def test_legacy_rows_are_reported_but_excluded_from_verdict_and_reads_never_write():
    conn = _conn()
    _paired(conn, [10] * 5, [10] * 5)
    _position(conn, event=None, arm="control", pnl=-1000)
    conn.commit()
    before = conn.total_changes
    report = wallet_recommendations(conn, min_paired=5, now_iso=NOW)
    row = report["recommendations"][0]
    assert row["diagnostic_recommendation"] == "promote"
    assert row["legacy_unattributed_closes"] == 1
    assert report["legacy_unattributed_closes"] == 1
    assert conn.total_changes == before


def test_exact_overlap_below_threshold_blocks_promotion():
    conn = _conn()
    _paired(conn, [10] * 5, [10] * 5)
    for index in range(5):
        _position(conn, event=f"control-extra-{index}", arm="control", pnl=2)
    conn.commit()
    row = wallet_recommendations(conn, min_paired=5, now_iso=NOW)["recommendations"][0]
    assert row["exact_overlap_pct"] == 50.0
    assert row["diagnostic_recommendation"] == "watch"
    assert "exact_overlap_below_75pct" in row["diagnostic_reason_codes"]


def test_unresolved_evm_identity_can_never_promote():
    conn = _conn()
    _paired(conn, [10] * 5, [10] * 5, network="evm-unknown")
    row = wallet_recommendations(conn, min_paired=5, now_iso=NOW)["recommendations"][0]
    assert row["network_id"] == "evm-unknown"
    assert row["diagnostic_recommendation"] == "watch"
    assert row["diagnostic_reason_codes"] == ["unresolved_network_identity"]


def test_fomo_priced_close_is_excluded_from_exact_overlap():
    conn = _conn()
    _paired(conn, [10] * 5, [10] * 5)
    pid = conn.execute(
        "SELECT id FROM memecoin_positions WHERE strategy='control' AND origin_event_id='e1'"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO memecoin_fills(position_id,kind,fill_source) VALUES (?,'entry','fomo')",
        (pid,))
    conn.commit()

    row = wallet_recommendations(conn, min_paired=5, now_iso=NOW)["recommendations"][0]
    assert row["matched_primary"] == 4
    assert row["diagnostic_recommendation"] == "watch"
    assert "thin_primary_overlap" in row["diagnostic_reason_codes"]


def test_excluded_wallet_is_hidden_unless_history_is_explicitly_requested():
    conn = _conn()
    _paired(conn, [10] * 5, [10] * 5)
    m.set_wallet_excluded(conn, "u1", True)

    assert wallet_recommendations(conn, min_paired=5, now_iso=NOW)["recommendations"] == []
    assert len(wallet_recommendations(
        conn, min_paired=5, now_iso=NOW, include_inactive=True,
    )["recommendations"]) == 1


def test_repeated_events_on_one_coin_cannot_promote():
    conn = _conn()
    _paired(conn, [10] * 5, [10] * 5, mint="ONE-COIN")

    row = wallet_recommendations(conn, min_paired=5, now_iso=NOW)["recommendations"][0]
    assert row["distinct_coins"] == 1
    assert row["diagnostic_recommendation"] == "watch"
    assert "thin_coin_diversity" in row["diagnostic_reason_codes"]


def test_arm_matrix_uses_only_honest_shared_origins_for_current_wallets():
    conn = _conn()
    _paired(conn, [10] * 5, [20] * 5, [30] * 5)
    _position(conn, event="control-only", arm="control", pnl=1_000)
    fomo_pid = _position(conn, event="fomo-all", arm="control", pnl=1_000)
    _position(conn, event="fomo-all", arm="real_shadow", pnl=1_000)
    _position(conn, event="fomo-all", arm="scalp_c2", pnl=1_000)
    conn.execute(
        "INSERT INTO memecoin_fills(position_id,kind,fill_source) VALUES (?,'entry','fomo')",
        (fomo_pid,))
    conn.commit()

    matrix = m.trader_arm_matrix(conn)
    row = matrix["wallets"][0]
    assert matrix["exact_paired"] is True and matrix["honest_only"] is True
    assert matrix["arms"] == ["control", "real_shadow", "scalp_c2"]
    assert {arm: row["by_arm"][arm]["closed"] for arm in matrix["arms"]} == {
        "control": 5, "real_shadow": 5, "scalp_c2": 5,
    }
