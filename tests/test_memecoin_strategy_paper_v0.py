import inspect
import sqlite3

import pytest

from sniper.research.memecoin_strategy import paper
from sniper.research.memecoin_strategy.evaluator import process_observation
from sniper.research.memecoin_strategy.seeds import seed_v0_candidates
from sniper.research.memecoin_strategy.store import ensure_strategy_tables, record_evaluation


START = "2026-07-11T00:00:00+00:00"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_strategy_tables(conn)
    return conn


def _obs(obs_id, at=START, uid="u", mint="M"):
    return {
        "id": obs_id, "upstream_event_id": f"e{obs_id}", "source_user_id": uid,
        "network_id": "solana", "mint": mint, "their_market_cap": 1_000_000,
        "their_size_usd": 5000, "was_curated": 1, "was_real_approved": 0,
        "normalized_side": "buy", "raw_type": "large_buy", "observed_at": at,
        "source_ts": at, "lag_s": 1.0, "liquidity": 100_000, "token_age_s": 3600,
        "thesis_stance": None, "thesis_score": None,
    }


def _signal(conn, candidate, at=START, mint="M"):
    first = _obs(1, "2026-07-10T23:59:00+00:00", "u1", mint)
    second = _obs(2, at, "u2", mint)
    evaluation = process_observation(candidate, second, [first, second], "replay", at)
    return record_evaluation(conn, candidate, second, evaluation, at)


def _quote(obs_id, at, price, *, mint="M", quality="ok"):
    return {"id": obs_id, "network_id": "solana", "mint": mint, "observed_at": at,
            "price": price, "quality": quality}


def test_open_position_models_costs_is_idempotent_and_paper_only():
    conn = _conn(); candidate = seed_v0_candidates(conn, START)[0]; signal = _signal(conn, candidate)
    quote = _quote(10, "2026-07-11T00:00:30+00:00", 1.0)
    first = paper.open_candidate_position(conn, candidate, signal, quote, quote["observed_at"])
    retry = paper.open_candidate_position(conn, candidate, signal, quote, quote["observed_at"])
    assert retry["position_id"] == first["position_id"]
    assert first["execution_mode"] == "paper" and first["timecap_s"] == 86400
    assert first["entry_notional"] == 100 and first["entry_cost"] == pytest.approx(1.0)
    fill = conn.execute("SELECT * FROM memecoin_candidate_fills").fetchone()
    assert fill["side"] == "entry" and fill["cost"] == pytest.approx(1.0)
    source = inspect.getsource(paper)
    assert "sniper.execution" not in source and "memecoin_real" not in source


def test_tp_exit_uses_ordered_quote_and_subtracts_both_costs():
    conn = _conn(); candidate = seed_v0_candidates(conn, START)[0]; signal = _signal(conn, candidate)
    position = paper.open_candidate_position(
        conn, candidate, signal, _quote(10, "2026-07-11T00:00:30+00:00", 1.0),
        "2026-07-11T00:00:30+00:00",
    )
    closed = paper.sweep_candidate_positions(conn, [
        _quote(11, "2026-07-11T00:01:00+00:00", 1.2),
        _quote(12, "2026-07-11T00:02:00+00:00", 1.6),
        _quote(13, "2026-07-11T00:03:00+00:00", 0.1),
    ], "2026-07-11T00:03:00+00:00")
    assert len(closed) == 1 and closed[0]["position_id"] == position["position_id"]
    assert closed[0]["exit_reason"] == "tp" and closed[0]["gross_pnl"] == pytest.approx(60)
    assert closed[0]["net_pnl"] == pytest.approx(57.4)


def test_stop_requires_four_qualifying_consecutive_observations_and_resets():
    conn = _conn(); candidate = seed_v0_candidates(conn, START)[0]; signal = _signal(conn, candidate)
    paper.open_candidate_position(
        conn, candidate, signal, _quote(10, "2026-07-11T00:00:30+00:00", 1.0),
        "2026-07-11T00:00:30+00:00",
    )
    quotes = [
        _quote(11, "2026-07-11T00:01:00+00:00", 0.5),
        _quote(12, "2026-07-11T00:02:00+00:00", 0.1, quality="ratio_outlier"),
        _quote(13, "2026-07-11T00:03:00+00:00", 0.6),  # resets
        _quote(14, "2026-07-11T00:04:00+00:00", 0.49),
        _quote(15, "2026-07-11T00:05:00+00:00", 0.48),
        _quote(16, "2026-07-11T00:06:00+00:00", 0.47),
    ]
    assert paper.sweep_candidate_positions(conn, quotes, "2026-07-11T00:06:00+00:00") == []
    row = conn.execute("SELECT stop_hits FROM memecoin_candidate_positions").fetchone()
    assert row[0] == 3
    closed = paper.sweep_candidate_positions(
        conn, [_quote(17, "2026-07-11T00:07:00+00:00", 0.46)],
        "2026-07-11T00:07:00+00:00",
    )
    assert closed[0]["exit_reason"] == "stop"
    assert closed[0]["net_pnl"] == pytest.approx(-55.46)


def test_timecap_is_feed_independent_and_ignores_post_deadline_quote():
    conn = _conn(); candidate = seed_v0_candidates(conn, START)[0]; signal = _signal(conn, candidate)
    opened = "2026-07-11T00:00:30+00:00"
    paper.open_candidate_position(conn, candidate, signal, _quote(10, opened, 1.0), opened)
    closed = paper.sweep_candidate_positions(
        conn, [_quote(11, "2026-07-12T00:00:31+00:00", 100.0)],
        "2026-07-12T00:00:31+00:00",
    )
    assert closed[0]["exit_reason"] == "timecap"
    assert closed[0]["exit_price"] == 1.0
    assert closed[0]["net_pnl"] == pytest.approx(-2.0)


def test_timecap_keeps_last_valid_quote_lineage_when_bad_quote_was_processed():
    conn = _conn(); candidate = seed_v0_candidates(conn, START)[0]; signal = _signal(conn, candidate)
    opened = "2026-07-11T00:00:30+00:00"
    paper.open_candidate_position(conn, candidate, signal, _quote(10, opened, 1.0), opened)
    closed = paper.sweep_candidate_positions(
        conn, [_quote(11, "2026-07-11T01:00:00+00:00", 0.1, quality="ratio_outlier")],
        "2026-07-12T00:00:31+00:00",
    )
    assert closed[0]["exit_observation_id"] == 10
    row = conn.execute(
        "SELECT last_observation_id,last_price_observation_id FROM memecoin_candidate_positions",
    ).fetchone()
    assert tuple(row) == (11, 10)


def test_quote_before_signal_and_duplicate_episode_cannot_open():
    conn = _conn(); candidate = seed_v0_candidates(conn, START)[0]; signal = _signal(conn, candidate)
    with pytest.raises(ValueError, match="precede"):
        paper.open_candidate_position(
            conn, candidate, signal, _quote(9, "2026-07-10T23:59:59+00:00", 1.0), START,
        )
    paper.open_candidate_position(conn, candidate, signal, _quote(10, START, 1.0), START)
    other_signal = dict(signal, signal_id="other")
    with pytest.raises(sqlite3.IntegrityError):
        paper.open_candidate_position(conn, candidate, other_signal, _quote(11, START, 1.0), START)
