import json
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_exit_tournament import exit_policy_tournament


NOW = "2026-07-17T12:00:00+00:00"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _position(conn, *, event, arm, pnl, network="4663", mcap=500_000.0, lag=30.0,
              position_id=None):
    cur = conn.execute(
        "INSERT INTO memecoin_positions(id, source_user_id, source_handle, network_id, mint, "
        "strategy, execution_mode, is_open, realized_pnl, origin_event_id, entry_mcap, "
        "entry_lag_s, opened_at, closed_at, exit_reason) "
        "VALUES(?, 'u1', 'alice', ?, ?, ?, 'paper', 0, ?, ?, ?, ?, "
        "'2026-07-17T10:00:00+00:00','2026-07-17T11:00:00+00:00','test')",
        (position_id, network, "mint-" + (event or "legacy"), arm, pnl, event, mcap, lag),
    )
    return cur.lastrowid


def _event(conn, index, pnls, *, network="4663", mcap=500_000.0, lag=30.0,
           consensus=1):
    event = f"{network}-e{index}"
    for arm, pnl in pnls.items():
        _position(conn, event=event, arm=arm, pnl=pnl, network=network, mcap=mcap, lag=lag)
    conn.execute(
        "INSERT INTO memecoin_entry_snapshots(upstream_event_id, chain, mint, action, "
        "decided_at, mcap, consensus_n_60m) VALUES(?,?,?,?,?,?,?)",
        (event, network, "mint-" + event, "paper", "2026-07-17T10:00:01+00:00",
         mcap, consensus),
    )
    return event


def test_exact_origin_pair_is_segmented_and_positive_challenger_is_shadow_candidate():
    conn = _conn()
    for index in range(1, 4):
        _event(conn, index, {"control": 1, "real_shadow": 2, "scalp_c2": 3,
                             "peak_lock_e": 4})
    conn.commit()

    report = exit_policy_tournament(conn, min_paired=3, now_iso=NOW)
    segment = report["segments"][0]
    assert segment["segment"] == {
        "network_id": "4663", "mcap_band": "250k_1m",
        "entry_lag_band": "under_60s", "consensus_band": "solo",
    }
    assert segment["all_four_matched"] == 3
    shadow = {row["challenger"]: row for row in segment["comparisons"]}["real_shadow"]
    assert shadow["paired_closes"] == 3
    assert shadow["control"]["pnl"] == 3.0
    assert shadow["challenger_metrics"]["pnl"] == 6.0
    assert shadow["pnl_delta"] == 3.0
    assert shadow["drop_top_two_delta"] == 1.0
    assert shadow["winner_swallow_count"] == 0
    assert shadow["readiness"] == "shadow_candidate"


def test_legacy_rows_are_counted_but_never_mint_time_matched():
    conn = _conn()
    _event(conn, 1, {"control": 2, "real_shadow": 3})
    _position(conn, event=None, arm="control", pnl=1000)
    _position(conn, event=None, arm="real_shadow", pnl=1000)
    conn.commit()
    report = exit_policy_tournament(conn, min_paired=1, now_iso=NOW)
    comparison = report["segments"][0]["comparisons"][0]
    assert comparison["paired_closes"] == 1
    assert report["legacy_unattributed_closes"] == 2
    assert report["legacy_matching_used"] is False


def test_chain_and_feature_bands_never_pool():
    conn = _conn()
    _event(conn, 1, {"control": 1, "real_shadow": 2}, network="4663",
           mcap=800_000, lag=20, consensus=1)
    _event(conn, 1, {"control": -1, "real_shadow": -2}, network="8453",
           mcap=2_000_000, lag=90, consensus=3)
    conn.commit()
    report = exit_policy_tournament(conn, min_paired=1, now_iso=NOW)
    keys = [row["segment"] for row in report["segments"]]
    assert keys == [
        {"network_id": "4663", "mcap_band": "250k_1m",
         "entry_lag_band": "under_60s", "consensus_band": "solo"},
        {"network_id": "8453", "mcap_band": "1m_5m",
         "entry_lag_band": "60s_or_more", "consensus_band": "consensus"},
    ]
    filtered = exit_policy_tournament(
        conn, min_paired=1, chain="8453", now_iso=NOW,
    )
    assert len(filtered["segments"]) == 1
    assert filtered["segments"][0]["segment"]["network_id"] == "8453"


def test_winner_swallow_rejects_even_profitable_tail_robust_challenger():
    conn = _conn()
    for index in range(1, 11):
        challenger = -1 if index <= 2 else 5
        _event(conn, index, {"control": 1, "peak_lock_e": challenger})
    conn.commit()
    comparison = exit_policy_tournament(
        conn, min_paired=10, now_iso=NOW,
    )["segments"][0]["comparisons"][0]
    assert comparison["pnl_delta"] > 0
    assert comparison["drop_top_two_delta"] > 0
    assert comparison["winner_swallow_count"] == 2
    assert comparison["winner_swallow_rate"] == 20.0
    assert comparison["readiness"] == "rejected"
    assert comparison["reason_codes"] == ["winner_swallow_over_10pct"]


def test_thin_and_negative_comparisons_have_deterministic_readiness():
    conn = _conn()
    _event(conn, 1, {"control": 3, "real_shadow": 2, "scalp_c2": 1})
    conn.commit()
    comparisons = {
        row["challenger"]: row for row in exit_policy_tournament(
            conn, min_paired=2, now_iso=NOW,
        )["segments"][0]["comparisons"]
    }
    assert comparisons["real_shadow"]["readiness"] == "insufficient"
    assert comparisons["real_shadow"]["reason_codes"] == ["paired_closes_below_2"]

    mature = exit_policy_tournament(conn, min_paired=1, now_iso=NOW)["segments"][0]
    by_arm = {row["challenger"]: row for row in mature["comparisons"]}
    assert by_arm["scalp_c2"]["readiness"] == "rejected"
    assert "pnl_delta_nonpositive" in by_arm["scalp_c2"]["reason_codes"]


def test_protected_hold_is_evidence_counter_not_modeled_pnl():
    conn = _conn()
    real_position = _position(
        conn, event="real-e", arm="alpha_b", pnl=-20, position_id=900,
    )
    conn.execute("UPDATE memecoin_positions SET execution_mode='real' WHERE id=?", (real_position,))
    for minute, allowed in (("00", 1), ("01", 0)):
        detail = json.dumps({"would_protect": bool(allowed), "funded_stop_suppressed": False})
        conn.execute(
            "INSERT INTO memecoin_exit_trigger_audit(position_id, reason, checked_minute, "
            "allowed, detail, checked_at) VALUES(?, 'protected_hold_shadow', ?, ?, ?, ?)",
            (real_position, f"2026-07-17T11:{minute}", allowed, detail,
             f"2026-07-17T11:{minute}:00+00:00"),
        )
    conn.commit()
    counter = exit_policy_tournament(conn, now_iso=NOW)["protected_hold_shadow"]
    assert counter == {
        "evaluations": 2, "would_protect": 1, "would_not_protect": 1,
        "funded_stops_suppressed": 0, "by_chain": {"4663": {"evaluations": 2,
        "would_protect": 1}},
    }
    assert "pnl" not in counter


def test_tournament_is_select_only():
    conn = _conn()
    _event(conn, 1, {"control": 1, "real_shadow": 2})
    conn.commit()
    before = conn.total_changes
    exit_policy_tournament(conn, min_paired=1, now_iso=NOW)
    assert conn.total_changes == before


def test_unresolved_evm_segment_can_never_become_strategy_ready():
    conn = _conn()
    for index in range(1, 4):
        _event(conn, index, {"control": 1, "peak_lock_e": 3}, network="evm-unknown")
    conn.commit()
    comparison = exit_policy_tournament(
        conn, min_paired=3, now_iso=NOW,
    )["segments"][0]["comparisons"][0]
    assert comparison["readiness"] == "insufficient"
    assert comparison["reason_codes"] == ["unresolved_network_identity"]
