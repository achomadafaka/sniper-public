import sqlite3

import pytest

from sniper.research.memecoin_strategy.dsl import compile_predicate
from sniper.research.memecoin_strategy.evaluator import evaluate_stream, process_observation
from sniper.research.memecoin_strategy.models import CandidateSpec
from sniper.research.memecoin_strategy.store import enroll_candidate, ensure_strategy_tables


NOW = "2026-07-11T12:00:00+00:00"


def _candidate(predicate):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_strategy_tables(conn)
    spec = CandidateSpec(
        name="test", predicate=predicate,
        exit_policy={"tp_pct": 60, "sl_pct": 50, "sl_confirm": 4, "timecap_s": 86400},
        sizing={"mode": "fixed", "notional_usd": 100, "execution_mode": "paper"},
        costs={"entry_bps": 100, "exit_bps": 100}, controls={},
        gates={"base_alpha": 0.05}, discovery_cutoff=NOW,
    )
    return enroll_candidate(conn, spec, NOW)


def _obs(obs_id, observed_at, *, uid="u1", network="solana", mint="M", mcap=1_000_000,
         curated=True, side="buy", event_type="large_buy", source_ts=None, thesis_stance=None,
         thesis_score=None):
    return {
        "id": obs_id, "upstream_event_id": f"e{obs_id}", "source_user_id": uid,
        "network_id": network, "mint": mint, "their_market_cap": mcap,
        "their_size_usd": 10_000, "was_curated": 1 if curated else 0,
        "was_real_approved": 0, "normalized_side": side, "raw_type": event_type,
        "observed_at": observed_at, "source_ts": source_ts or observed_at,
        "lag_s": 1.0, "thesis_stance": thesis_stance, "thesis_score": thesis_score,
        "liquidity": 100_000, "token_age_s": 3600,
    }


def _cluster_predicate():
    return {
        "window_s": 1800,
        "freshness_s": 120,
        "clauses": [
            {"field": "event_type", "op": "eq", "value": "large_buy"},
            {"field": "market_cap", "op": "lt", "value": 5_000_000},
            {"field": "was_curated", "op": "eq", "value": True},
            {"field": "distinct_buyer_count", "op": "gte", "value": 2},
        ],
    }


def test_dsl_rejects_unknown_fields_bad_types_operators_and_boolean_trees():
    bad = [
        {"clauses": [{"field": "future_alpha", "op": "eq", "value": 1}]},
        {"clauses": [{"field": "market_cap", "op": "contains", "value": 1}]},
        {"clauses": [{"field": "market_cap", "op": "lt", "value": "cheap"}]},
        {"or": [{"field": "market_cap", "op": "lt", "value": 1}]},
        {"clauses": [{"field": "network", "op": "between", "value": ["a", "z"]}]},
    ]
    for raw in bad:
        with pytest.raises(ValueError):
            compile_predicate(raw)


def test_dsl_supports_all_allowlisted_operators_and_null_fails_closed():
    predicate = compile_predicate({"clauses": [
        {"field": "market_cap", "op": "between", "value": [1, 5_000_000]},
        {"field": "size_usd", "op": "gte", "value": 1_000},
        {"field": "source_lag_s", "op": "lte", "value": 5},
        {"field": "network", "op": "in", "value": ["solana", "8453"]},
        {"field": "event_type", "op": "eq", "value": "large_buy"},
        {"field": "token_age_s", "op": "gt", "value": 60},
    ]})
    assert predicate.matches({
        "market_cap": 10, "size_usd": 2_000, "source_lag_s": 1,
        "network": "solana", "event_type": "large_buy", "token_age_s": 100,
    })
    missing = dict(market_cap=None, size_usd=2_000, source_lag_s=1, network="solana",
                   event_type="large_buy", token_age_s=100)
    assert not predicate.matches(missing)
    nullable = compile_predicate({
        "clauses": [{"field": "thesis_stance", "op": "eq", "value": None}],
    })
    assert nullable.matches({"thesis_stance": None})


def test_cluster_feature_uses_distinct_curated_buyers_same_chain_coin_and_past_only():
    candidate = _candidate(_cluster_predicate())
    current = _obs(5, "2026-07-11T12:00:00+00:00", uid="u2")
    history = [
        _obs(1, "2026-07-11T11:40:00+00:00", uid="u1"),
        _obs(2, "2026-07-11T11:50:00+00:00", uid="u1"),  # repeat wallet
        _obs(3, "2026-07-11T11:55:00+00:00", uid="x", network="8453"),
        _obs(4, "2026-07-11T11:56:00+00:00", uid="y", curated=False),
        current,
        _obs(6, "2026-07-11T12:00:01+00:00", uid="future"),
    ]
    result = process_observation(candidate, current, history, mode="live", now_iso=NOW)
    assert result.matched
    assert result.features["distinct_buyer_count"] == 2
    assert result.source_observation_ids == (1, 2, 5)


def test_stale_live_observation_rejects_but_replay_evaluates_same_frozen_event():
    candidate = _candidate(_cluster_predicate())
    history = [
        _obs(1, "2026-07-11T11:00:00+00:00", uid="u1"),
        _obs(2, "2026-07-11T11:01:00+00:00", uid="u2"),
    ]
    live = process_observation(candidate, history[-1], history, mode="live", now_iso=NOW)
    replay = process_observation(candidate, history[-1], history, mode="replay", now_iso=NOW)
    assert not live.matched and live.reason == "stale_live_observation"
    assert replay.matched
    assert live.features == replay.features


def test_live_and_replay_match_parity_with_same_timestamp_ties():
    candidate = _candidate(_cluster_predicate())
    events = [
        _obs(3, NOW, uid="u3", source_ts="2026-07-11T11:59:59+00:00"),
        _obs(2, NOW, uid="u2", source_ts="2026-07-11T11:59:59+00:00"),
        _obs(1, NOW, uid="u1", source_ts="2026-07-11T11:59:58+00:00"),
    ]
    live = evaluate_stream(candidate, events, mode="live", now_iso=NOW)
    replay = evaluate_stream(candidate, events, mode="replay", now_iso=NOW)
    assert [result.observation_id for result in live if result.matched] == [2, 3]
    assert [(r.observation_id, r.features, r.source_observation_ids) for r in live] == [
        (r.observation_id, r.features, r.source_observation_ids) for r in replay
    ]


def test_malformed_runtime_feature_fails_closed_without_raising():
    candidate = _candidate(_cluster_predicate())
    current = _obs(2, NOW, uid="u2")
    current["their_market_cap"] = "not-a-number"
    current["was_curated"] = "0"
    result = process_observation(
        candidate, current, [_obs(1, NOW, uid="u1"), current], mode="live", now_iso=NOW,
    )
    assert not result.matched and result.reason == "predicate_no_match"
    assert result.features["was_curated"] is None
