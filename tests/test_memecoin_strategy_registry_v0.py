import sqlite3

import pytest

from sniper.research.memecoin_strategy.models import CandidateSpec
from sniper.research.memecoin_strategy.store import (
    canonical_params_hash,
    enroll_candidate,
    ensure_strategy_tables,
)


NOW = "2026-07-11T12:00:00+00:00"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_strategy_tables(conn)
    return conn


def _spec(name="seed", *, timecap=86400, mode="paper", predicate=None):
    return CandidateSpec(
        name=name,
        predicate=predicate or {"clauses": [{"field": "market_cap", "op": "lt", "value": 5_000_000}]},
        exit_policy={"tp_pct": 60, "sl_pct": 50, "sl_confirm": 4, "timecap_s": timecap},
        sizing={"mode": "fixed", "notional_usd": 100, "execution_mode": mode},
        costs={"entry_bps": 100, "exit_bps": 100},
        controls={"enabled": True, "window_s": 1800},
        gates={"base_alpha": 0.05, "read_closes": 40, "read_coins": 20, "read_days": 10},
        discovery_cutoff=NOW,
    )


def test_canonical_hash_is_stable_across_dict_order_and_excludes_name():
    first = _spec("first")
    second = _spec("second", predicate={
        "clauses": [{"value": 5_000_000, "op": "lt", "field": "market_cap"}],
    })
    assert canonical_params_hash(first) == canonical_params_hash(second)


def test_enrollment_is_idempotent_and_assigns_spawn_alpha():
    conn = _conn()
    first = enroll_candidate(conn, _spec(), NOW)
    retry = enroll_candidate(conn, _spec("renamed"), NOW)
    assert retry.candidate_id == first.candidate_id
    assert first.status == "candidate_active" and first.spawn_index == 1
    assert first.alpha_alloc == pytest.approx(0.025)
    assert conn.execute("SELECT COUNT(*) FROM memecoin_strategy_candidates").fetchone()[0] == 1


def test_parameters_are_immutable_but_lifecycle_status_can_change():
    conn = _conn()
    candidate = enroll_candidate(conn, _spec(), NOW)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE memecoin_strategy_candidates SET predicate_json='{}' WHERE candidate_id=?",
            (candidate.candidate_id,),
        )
    conn.rollback()
    conn.execute(
        "UPDATE memecoin_strategy_candidates SET status='retired' WHERE candidate_id=?",
        (candidate.candidate_id,),
    )
    conn.commit()


def test_python_and_database_reject_nonpaper_or_nonterminal_candidates():
    conn = _conn()
    for spec in (_spec(timecap=0), _spec(mode="real")):
        with pytest.raises(ValueError):
            enroll_candidate(conn, spec, NOW)
    valid = enroll_candidate(conn, _spec(), NOW)
    row = conn.execute(
        "SELECT * FROM memecoin_strategy_candidates WHERE candidate_id=?",
        (valid.candidate_id,),
    ).fetchone()
    values = list(row)
    columns = [item[1] for item in conn.execute("PRAGMA table_info(memecoin_strategy_candidates)")]
    values[columns.index("candidate_id")] = "bad-db"
    values[columns.index("params_hash")] = "bad-hash"
    values[columns.index("execution_mode")] = "real"
    placeholders = ",".join("?" for _ in columns)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            f"INSERT INTO memecoin_strategy_candidates({','.join(columns)}) VALUES ({placeholders})",
            values,
        )


def test_five_active_slot_cap_and_spawn_index_are_transactional():
    conn = _conn()
    candidates = [
        enroll_candidate(
            conn,
            _spec(f"c{i}", predicate={
                "clauses": [{"field": "market_cap", "op": "lt", "value": 1_000_000 + i}],
            }),
            NOW,
        )
        for i in range(6)
    ]
    assert [candidate.spawn_index for candidate in candidates] == [1, 2, 3, 4, 5, 6]
    assert [candidate.status for candidate in candidates] == [
        "candidate_active", "candidate_active", "candidate_active", "candidate_active",
        "candidate_active", "candidate_waiting",
    ]
    with pytest.raises(sqlite3.IntegrityError, match="active candidate cap"):
        conn.execute(
            "UPDATE memecoin_strategy_candidates SET status='candidate_active' "
            "WHERE candidate_id=?", (candidates[-1].candidate_id,),
        )


def test_candidate_position_constraints_and_read_immutability_live_in_database():
    conn = _conn()
    candidate = enroll_candidate(conn, _spec(), NOW)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO memecoin_candidate_positions(candidate_id,signal_id,episode_key,"
            "network_id,mint,execution_mode,timecap_s,entry_price,entry_notional,opened_at,status) "
            "VALUES (?,?,?,?,?,'real',86400,1,100,?,'open')",
            (candidate.candidate_id, "s", "e", "solana", "M", NOW),
        )
    conn.execute(
        "INSERT INTO memecoin_candidate_reads(read_id,candidate_id,read_type,asof,manifest_json,"
        "evidence_hash,created_at) VALUES ('r',?,'fixed',?,'{}','h',?)",
        (candidate.candidate_id, NOW, NOW),
    )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE memecoin_candidate_reads SET manifest_json='x' WHERE read_id='r'")
