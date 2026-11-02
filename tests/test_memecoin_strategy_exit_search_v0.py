import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from scripts.research.memecoin_exit_search import load_candidate_paths
from sniper.persistence import db_memecoin as m
from sniper.research.memecoin_strategy.exit_search import (
    data_gate,
    simulate_exit_path,
    walk_forward_search,
)
from sniper.research.memecoin_strategy.models import CandidateSpec
from sniper.research.memecoin_strategy.store import enroll_candidate, ensure_strategy_tables


START = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_strategy_tables(conn)
    return conn


def _candidate(conn):
    return enroll_candidate(conn, CandidateSpec(
        name="source",
        predicate={"clauses": [{"field": "market_cap", "op": "lt", "value": 5_000_000}]},
        exit_policy={"tp_pct": 60, "sl_pct": 50, "sl_confirm": 4, "timecap_s": 86400,
                     "mirror": False},
        sizing={"mode": "fixed", "notional_usd": 100, "execution_mode": "paper"},
        costs={"entry_bps": 100, "exit_bps": 100}, controls={"enabled": True},
        gates={"base_alpha": 0.05}, discovery_cutoff=START.isoformat(),
    ), START.isoformat())


def _path(index=0, *, complete=True, observations=None, coin=None, day=None, band="1-5M"):
    signal = START + timedelta(days=index % 21 if day is None else day)
    observations = observations if observations is not None else [
        {"id": index * 10 + 1, "observed_at": (signal + timedelta(hours=1)).isoformat(),
         "price": 1.3, "quality": "ok"},
        {"id": index * 10 + 2, "observed_at": (signal + timedelta(hours=2)).isoformat(),
         "price": 1.0, "quality": "ok"},
        {"id": index * 10 + 3, "observed_at": (signal + timedelta(hours=24)).isoformat(),
         "price": 1.0, "quality": "ok"},
    ]
    return {
        "path_id": f"p{index}", "network_id": "solana", "mint": coin or f"M{index % 105}",
        "signal_at": signal.isoformat(), "entry_price": 1.0, "notional": 100,
        "observations": observations, "complete": complete, "market_cap_band": band,
    }


def test_data_gate_reports_every_deficit_and_counts_chain_coin_days():
    result = data_gate(
        [_path(i) for i in range(10)], {"expected": 1000, "observed": 800},
    )
    assert not result.passed
    assert set(result.deficits) == {"distinct_days", "complete_paths", "distinct_coins", "coverage", "protocol_missing_or_legacy"}
    assert result.coverage == 0.8
    passed = data_gate(
        [_path(i) for i in range(210)], {"expected": 1000, "observed": 900,
         "budget_skipped": 100},
    )
    assert not passed.passed and passed.complete_paths == 210 and passed.distinct_coins == 105


def test_simulator_respects_first_touch_stop_confirmation_costs_and_missing_quality():
    signal = START
    observations = [
        {"id": 1, "observed_at": (signal + timedelta(minutes=1)).isoformat(),
         "price": 0.4, "quality": "ratio_outlier"},
        *[{"id": 2 + i, "observed_at": (signal + timedelta(minutes=2 + i)).isoformat(),
           "price": 0.5 - i * 0.01, "quality": "ok"} for i in range(4)],
        {"id": 9, "observed_at": (signal + timedelta(minutes=10)).isoformat(),
         "price": 2.0, "quality": "ok"},
    ]
    path = _path(observations=observations)
    policy = {"tp_pct": 60, "sl_pct": 50, "sl_confirm": 4, "timecap_s": 86400}
    result = simulate_exit_path(path, policy, {"entry_bps": 100, "exit_bps": 100})
    assert result.valid and result.reason == "stop" and result.exit_price == pytest.approx(0.47)
    assert result.net_pnl == pytest.approx(-54.47)
    assert not simulate_exit_path(
        _path(complete=False), policy, {"entry_bps": 0, "exit_bps": 0},
    ).valid


def test_same_timestamp_order_is_conservative_and_timecap_uses_last_prior_quote():
    at = (START + timedelta(minutes=1)).isoformat()
    path = _path(observations=[
        {"id": 1, "observed_at": at, "price": 2.0, "quality": "ok"},
        {"id": 2, "observed_at": at, "price": 0.4, "quality": "ok"},
    ])
    policy = {"tp_pct": 60, "sl_pct": 50, "sl_confirm": 1, "timecap_s": 3600}
    assert simulate_exit_path(path, policy, {}).reason == "stop"
    time_path = _path(observations=[
        {"id": 3, "observed_at": (START + timedelta(minutes=30)).isoformat(),
         "price": 1.1, "quality": "ok"},
        {"id": 4, "observed_at": (START + timedelta(hours=2)).isoformat(),
         "price": 5.0, "quality": "ok"},
    ])
    timed = simulate_exit_path(time_path, policy | {"sl_confirm": 4}, {})
    assert timed.reason == "timecap" and timed.exit_price == 1.1


def test_walk_forward_search_is_locked_while_accruing_and_spawns_nothing():
    conn = _conn(); candidate = _candidate(conn)
    result = walk_forward_search(
        [_path(i) for i in range(20)], candidate, "2026-07-11T00:00:00+00:00",
        coverage={"expected": 100, "observed": 100}, conn=conn,
    )
    assert result.status == "accruing" and result.spawned_candidate is None
    assert result.search_read_id is not None
    assert conn.execute("SELECT COUNT(*) FROM memecoin_strategy_candidates").fetchone()[0] == 1


def test_legacy_walk_forward_cannot_enroll_a_strategy_from_sparse_paths():
    conn = _conn(); candidate = _candidate(conn)
    paths = [_path(i) for i in range(210)]
    result = walk_forward_search(paths, candidate, "2026-07-11T00:00:00+00:00",
                                 coverage={"expected":10000,"observed":9500},conn=conn)
    assert result.status == 'accruing' and result.selected_policy is None
    assert result.spawned_candidate is None
    assert 'protocol_missing_or_legacy' in result.gate.deficits


def test_db_loader_uses_candidate_quotes_not_horizon_marks_and_requires_24h_coverage():
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn); ensure_strategy_tables(conn)
    candidate = _candidate(conn)
    conn.execute(
        "INSERT INTO memecoin_candidate_signals(signal_id,candidate_id,episode_key,evidence_stage,"
        "network_id,mint,matched,reason,feature_json,source_observation_ids_json,params_hash,"
        "observed_at,created_at) VALUES ('s',?,'e','candidate','solana','M',1,'matched',"
        "'{\"market_cap\":2000000}','[]',?,?,?)",
        (candidate.candidate_id, candidate.params_hash, START.isoformat(), START.isoformat()),
    )
    conn.execute(
        "INSERT INTO memecoin_candidate_positions(candidate_id,signal_id,episode_key,network_id,"
        "mint,evidence_stage,execution_mode,timecap_s,entry_price,entry_notional,quantity,entry_cost,"
        "opened_at,status) VALUES (?,'s','e','solana','M','candidate','paper',86400,1,100,100,0,?,"
        "'open')", (candidate.candidate_id, START.isoformat()),
    )
    for index, hours in enumerate((12, 24), start=1):
        at = (START + timedelta(hours=hours)).isoformat(); sweep = f"s{index}"
        conn.execute(
            "INSERT INTO memecoin_quote_sweeps_v2(sweep_id,observed_at,eligible_count,"
            "requested_count,captured_count,budget_skipped_count) VALUES (?,?,1,1,1,0)",
            (sweep, at),
        )
        conn.execute(
            "INSERT INTO memecoin_quote_observations_v2(sweep_id,network_id,mint,observed_at,"
            "price,quality,contexts) VALUES (?,'solana','M',?,1.1,'ok','candidate')",
            (sweep, at),
        )
    conn.commit()
    paths, coverage = load_candidate_paths(conn, candidate.candidate_id, capture_period_s=43200)
    assert len(paths) == 1 and paths[0]["complete"]
    assert paths[0]["market_cap_band"] == "1-5M"
    assert coverage == {"expected": 2, "observed": 2, "budget_skipped": 0}


import pytest as _pytest
from sniper.research.wallet_execution.publication import diagnostic_mode as _diagnostic_mode
@_pytest.fixture(autouse=True)
def _explicit_legacy_diagnostics():
    with _diagnostic_mode():
        yield
