import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from sniper.research.memecoin_strategy.models import CandidateSpec
from sniper.research.memecoin_strategy.statistics import (
    apply_candidate_kills,
    approve_first_class_paper,
    candidate_read,
    paired_sign_flip_pvalue,
)
from sniper.research.memecoin_strategy.store import enroll_candidate, ensure_strategy_tables


START = "2026-06-01T00:00:00+00:00"
ASOF = "2026-07-11T00:00:00+00:00"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_strategy_tables(conn)
    return conn


def _candidate(conn, name="c", born=START, predicate_value=5_000_000):
    return enroll_candidate(conn, CandidateSpec(
        name=name,
        predicate={"clauses": [{"field": "market_cap", "op": "lt", "value": predicate_value}]},
        exit_policy={"tp_pct": 60, "sl_pct": 50, "sl_confirm": 4, "timecap_s": 86400},
        sizing={"mode": "fixed", "notional_usd": 100, "execution_mode": "paper"},
        costs={"entry_bps": 100, "exit_bps": 100}, controls={"enabled": True},
        gates={
            "base_alpha": 0.05, "read_closes": 40, "read_coins": 20, "read_days": 10,
            "kill_pnl": -150, "kill_closes": 25,
            "starvation_days": 30, "starvation_coins": 10,
        },
        discovery_cutoff=born,
    ), born)


def _closed(conn, candidate, index, *, pnl=10.0, control_pnl=0.0, paired=True,
            coins=20, days=10, stage="candidate"):
    coin = f"M{index % coins}"
    day = datetime(2026, 6, 1, tzinfo=timezone.utc) + timedelta(days=index % days)
    at = day.isoformat()
    signal_id = f"s-{candidate.candidate_id}-{index}"
    episode = f"{candidate.candidate_id}|{stage}|{coin}|{index}"
    conn.execute(
        "INSERT INTO memecoin_candidate_positions(candidate_id,signal_id,episode_key,network_id,"
        "mint,evidence_stage,execution_mode,timecap_s,entry_price,entry_notional,quantity,"
        "entry_cost,opened_at,status,closed_at,gross_pnl,net_pnl) "
        "VALUES (?,?,?,?,?,?,'paper',86400,1,100,100,1,?,'closed',?, ?,?)",
        (candidate.candidate_id, signal_id, episode, "solana", coin, stage, at, at, pnl + 1, pnl),
    )
    if paired:
        conn.execute(
            "INSERT INTO memecoin_candidate_positions(candidate_id,signal_id,episode_key,"
            "network_id,mint,evidence_stage,control_for_signal_id,execution_mode,timecap_s,"
            "entry_price,entry_notional,quantity,entry_cost,opened_at,status,closed_at,gross_pnl,"
            "net_pnl) VALUES (?,?,?,?,?,?,?,'paper',86400,1,100,100,1,?,'closed',?,?,?)",
            (candidate.candidate_id, f"ctrl-{signal_id}", episode, "solana", f"C{index}", stage,
             signal_id, at, at, control_pnl + 1, control_pnl),
        )


def test_read_gate_is_conjunction_and_ready_read_does_not_auto_promote():
    conn = _conn(); candidate = _candidate(conn)
    for index in range(40):
        _closed(conn, candidate, index)
    conn.commit()
    read = candidate_read(conn, candidate.candidate_id, ASOF)
    assert read.status == "blocked_protocol"
    assert (read.closes, read.distinct_coins, read.distinct_days) == (40, 20, 10)
    assert read.paired_closes == 40 and read.paired_coins == 20
    assert read.mean_delta > 0 and read.median_delta > 0 and read.lower_bound > 0
    assert read.p_value <= candidate.alpha_alloc
    assert conn.execute(
        "SELECT status FROM memecoin_strategy_candidates WHERE candidate_id=?",
        (candidate.candidate_id,),
    ).fetchone()[0] == "candidate_active"
    again = candidate_read(conn, candidate.candidate_id, ASOF)
    assert again.read_id == read.read_id
    for index in range(40, 50):
        _closed(conn, candidate, index, pnl=-100, control_pnl=100)
    conn.commit()
    later = candidate_read(conn, candidate.candidate_id, "2026-07-20T00:00:00+00:00")
    assert later.read_id != read.read_id and later.status == "blocked_protocol"


def test_missing_controls_and_each_count_dimension_block_read():
    cases = [(39, 20, 10, True), (40, 19, 10, True), (40, 20, 9, True), (40, 20, 10, False)]
    for closes, coins, days, paired in cases:
        conn = _conn(); candidate = _candidate(conn)
        for index in range(closes):
            _closed(conn, candidate, index, coins=coins, days=days, paired=paired)
        conn.commit()
        read = candidate_read(conn, candidate.candidate_id, ASOF)
        assert read.status == "blocked_protocol"
        assert read.deficits


def test_sign_flip_is_one_sided_deterministic_and_detects_uniform_positive_delta():
    positive = paired_sign_flip_pvalue([0.1] * 20)
    mixed = paired_sign_flip_pvalue([0.1, -0.1] * 10)
    assert positive == pytest.approx(1 / (2 ** 20))
    assert mixed > 0.4
    assert paired_sign_flip_pvalue([0.1] * 20) == positive


def test_loss_futility_and_starvation_kills_fill_waiting_slot():
    conn = _conn()
    candidates = [_candidate(conn, f"c{i}", predicate_value=1_000_000 + i) for i in range(6)]
    assert candidates[-1].status == "candidate_waiting"
    _closed(conn, candidates[0], 0, pnl=-151, paired=False)
    for index in range(25):
        _closed(conn, candidates[1], index, pnl=0, paired=False, coins=25, days=25)
    # candidates[2] has no closes and was born >30 days before ASOF -> starvation.
    conn.commit()
    killed = set(apply_candidate_kills(conn, ASOF))
    assert candidates[0].candidate_id in killed and candidates[2].candidate_id in killed
    assert candidates[1].candidate_id not in killed
    statuses = {row["candidate_id"]: row["status"] for row in conn.execute(
        "SELECT candidate_id,status FROM memecoin_strategy_candidates",
    )}
    assert statuses[candidates[-1].candidate_id] == "candidate_active"


def test_legacy_positive_read_cannot_start_a_promotion_stage():
    conn = _conn(); candidate = _candidate(conn)
    for index in range(40): _closed(conn, candidate, index)
    conn.commit(); read = candidate_read(conn, candidate.candidate_id, ASOF)
    with pytest.raises(ValueError, match='unsupported memecoin performance'):
        approve_first_class_paper(conn, candidate.candidate_id, ASOF, read.read_id)
    assert conn.execute('SELECT count(*) FROM memecoin_candidate_stages').fetchone()[0] == 0  # no unsupported promotion inserted


def test_nonready_read_cannot_be_approved():
    conn = _conn(); candidate = _candidate(conn)
    read = candidate_read(conn, candidate.candidate_id, ASOF)
    with pytest.raises(ValueError, match="unsupported memecoin performance"):
        approve_first_class_paper(conn, candidate.candidate_id, ASOF, read.read_id)
