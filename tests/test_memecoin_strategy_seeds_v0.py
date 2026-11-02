import sqlite3

from sniper.research.memecoin_strategy.controls import select_past_control
from sniper.research.memecoin_strategy.evaluator import process_observation
from sniper.research.memecoin_strategy.seeds import seed_v0_candidates
from sniper.research.memecoin_strategy.store import ensure_strategy_tables, record_evaluation


NOW = "2026-07-11T12:00:00+00:00"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_strategy_tables(conn)
    return conn


def _obs(obs_id, at, *, uid="u", mint="M", network="solana", mcap=1_000_000,
         curated=True, raw_type="large_buy", side="buy", stance=None, score=None):
    return {
        "id": obs_id, "upstream_event_id": f"e{obs_id}", "source_user_id": uid,
        "network_id": network, "mint": mint, "their_market_cap": mcap,
        "their_size_usd": 5000, "was_curated": 1 if curated else 0,
        "was_real_approved": 0, "normalized_side": side, "raw_type": raw_type,
        "observed_at": at, "source_ts": at, "lag_s": 1.0,
        "thesis_stance": stance, "thesis_score": score,
        "liquidity": 100_000, "token_age_s": 3600,
    }


def test_seed_candidates_are_idempotent_frozen_and_terminal():
    conn = _conn()
    first = seed_v0_candidates(conn, NOW)
    second = seed_v0_candidates(conn, NOW)
    by_name = {candidate.name: candidate for candidate in first}
    assert {candidate.name for candidate in first} == {"cluster_momentum_v0", "thesis_momentum_v0"}
    assert [candidate.candidate_id for candidate in second] == [candidate.candidate_id for candidate in first]
    for candidate in first:
        assert candidate.execution_mode == "paper"
        assert candidate.timecap_s == 86400
        assert candidate.exit_policy == {
            "mirror": False, "sl_confirm": 4, "sl_pct": 50, "timecap_s": 86400,
            "tp_pct": 60,
        }
        assert candidate.sizing["notional_usd"] == 100
    assert by_name["cluster_momentum_v0"].predicate["window_s"] == 1800


def test_cluster_seed_requires_second_distinct_curated_buyer_same_chain_and_cap():
    candidate = seed_v0_candidates(_conn(), NOW)[0]
    first = _obs(1, "2026-07-11T11:59:00+00:00", uid="u1")
    cases = [
        (_obs(2, NOW, uid="u2"), True),
        (_obs(3, NOW, uid="u1"), False),
        (_obs(4, NOW, uid="u2", network="8453"), False),
        (_obs(5, NOW, uid="u2", mcap=5_000_000), False),
        (_obs(6, NOW, uid="u2", curated=False), False),
    ]
    for current, expected in cases:
        result = process_observation(candidate, current, [first, current], "live", NOW)
        assert result.matched is expected


def test_thesis_seed_requires_curated_winning_score_and_current_receipt():
    candidate = seed_v0_candidates(_conn(), NOW)[1]
    good = _obs(1, NOW, raw_type="thesis_created", side=None, stance="winning", score=20)
    assert process_observation(candidate, good, [good], "live", NOW).matched
    for current in (
        _obs(2, NOW, raw_type="thesis_created", side=None, stance="winning", score=19.9),
        _obs(3, NOW, raw_type="thesis_created", side=None, stance="flat", score=50),
        _obs(4, NOW, raw_type="thesis_created", side=None, stance="winning", score=50,
             curated=False),
        _obs(5, "2026-07-11T11:57:00+00:00", raw_type="thesis_created", side=None,
             stance="winning", score=50),
    ):
        assert not process_observation(candidate, current, [current], "live", NOW).matched


def test_daily_episode_allows_only_one_matched_signal_but_retains_duplicate_rejection():
    conn = _conn()
    candidate = seed_v0_candidates(conn, NOW)[0]
    first = _obs(1, "2026-07-11T11:59:00+00:00", uid="u1")
    second = _obs(2, NOW, uid="u2")
    third = _obs(3, "2026-07-11T12:01:00+00:00", uid="u3")
    first_eval = process_observation(candidate, second, [first, second], "live", NOW)
    duplicate_eval = process_observation(
        candidate, third, [first, second, third], "replay", "2026-07-11T12:01:00+00:00",
    )
    one = record_evaluation(conn, candidate, second, first_eval, NOW)
    duplicate = record_evaluation(conn, candidate, third, duplicate_eval, NOW)
    assert one["matched"] == 1 and one["reason"] == "control_missing"
    assert duplicate["matched"] == 0 and duplicate["reason"] == "episode_duplicate"
    assert conn.execute("SELECT COUNT(*) FROM memecoin_candidate_signals").fetchone()[0] == 2


def test_control_is_past_only_same_chain_band_nonmatching_and_deterministic():
    signal = _obs(10, NOW, mint="SIGNAL", mcap=2_000_000)
    rows = [
        {**_obs(1, "2026-07-11T11:55:00+00:00", mint="A", mcap=2_500_000),
         "predicate_matched": False},
        {**_obs(2, "2026-07-11T11:59:00+00:00", mint="B", mcap=2_500_000),
         "predicate_matched": True},
        {**_obs(3, "2026-07-11T11:59:30+00:00", mint="C", mcap=6_000_000),
         "predicate_matched": False},
        {**_obs(4, "2026-07-11T11:59:45+00:00", mint="D", network="8453", mcap=2_000_000),
         "predicate_matched": False},
        {**_obs(5, "2026-07-11T12:00:01+00:00", mint="E", mcap=2_000_000),
         "predicate_matched": False},
        {**_obs(6, "2026-07-11T11:58:00+00:00", mint="SIGNAL", mcap=2_000_000),
         "predicate_matched": False},
    ]
    control = select_past_control(signal, rows, window_s=1800)
    assert control.observation_id == 1 and control.mint == "A"
    assert select_past_control(signal, [], window_s=1800) is None
