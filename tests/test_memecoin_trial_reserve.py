"""Trial arm on the reservation rail: marker, clip, caps, counter (spec sections 3, 5.2, 7.3, 7.8).

Every trial entry reserves through `reserve_entry` / `reserve_entry_evm`, which own the funded
contract. The trial marker is frozen into the contract's `source` block there, inside the same
BEGIN IMMEDIATE as global capacity, so two threads cannot both take the last trial slot.
"""
import json
import sqlite3
import threading

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_evm_risk as evm_risk
from sniper.services import memecoin_risk as risk
from sniper.services import memecoin_trial as T
from sniper.services.memecoin_market_state import EntryClassification
from sniper.settings import memecoin as cfg

NOW = "2026-10-01T12:00:00+00:00"
DAY = NOW[:10]


def _classification(network_id="solana", mint="MINT", signal_source="follow_activity"):
    return EntryClassification(
        classification_version=1, network_id=network_id, mint=mint, signal_source=signal_source,
        decision_at=NOW, coin_band="small", lifecycle="young", liquidity_class="unknown",
        momentum_phase="climbing", market_regime="hot_narrow", data_quality="partial",
        risk_scale=0.75, reason_codes=("coin_band:small",),
        facts={"network_id": network_id, "mint": mint, "effective_mcap": 500_000.0,
               "liquidity": 5_000.0, "base_clip_usd": None})


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "fixed")
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING_BY_CHAIN", {})
    monkeypatch.setattr(cfg, "MEMECOIN_TRADE_SIZE_USD", 100.0)
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_FLOOR_USD", 5.0)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_TOTAL_EXPOSURE_USD", 700.0)
    monkeypatch.setattr(cfg, "MEMECOIN_DAILY_LOSS_LIMIT_USD", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT_BY_CHAIN", {})
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TRIAL_MAX_CONCURRENT", 2)


def _connect(path=":memory:"):
    conn = sqlite3.connect(path, timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


@pytest.fixture
def conn():
    c = _connect()
    m.ensure_memecoin_tables(c)
    return c


def _trial(conn, uid="u", chain="solana", clip=20.0, rule=T.RULE):
    tid = m.start_trial(conn, source_user_id=uid, chain=chain, clip_usd=clip, rule=rule,
                        actor="test", now_iso=NOW)
    return tid, T.marker(m.open_trial(conn, uid, chain))


def _attempt(conn, aid, network="solana", mint="MINT"):
    m.start_entry_attempt(conn, attempt_id=aid, decision_started_at=NOW, network_id=network,
                          mint=mint)


def _sol(conn, *, uid="u", mint="MINT", aid="a1", trial=None, signal_source="follow_activity"):
    _attempt(conn, aid, mint=mint)
    manual = signal_source == "manual"      # what the leg passes for a manual buy
    return risk.reserve_entry(
        conn, mint=mint, source_user_id=uid, strategy="alpha_b", day=DAY, usdc_balance=300.0,
        sol_balance=1.0, now_iso=NOW, attempt_id=aid, signal_source=signal_source,
        classification=_classification(mint=mint, signal_source=signal_source), trial=trial,
        enforce_concurrency=not manual, size_override=(20.0 if manual else None))


def _evm(conn, *, uid="u", mint="0x" + "ab" * 20, aid="e1", trial=None):
    _attempt(conn, aid, network="4663", mint=mint)
    return evm_risk.reserve_entry_evm(
        conn, mint=mint, source_user_id=uid, strategy="alpha_b", network_id="4663", day=DAY,
        usdc_balance=300.0, native_balance=1.0, now_iso=NOW, attempt_id=aid,
        signal_source="follow_activity",
        classification=_classification("4663", mint), trial=trial)


def _contract(conn, pid):
    return json.loads(m.pending_row(conn, pid)["funded_contract_json"])


# --- marker and clip ---

def test_a_solana_trial_reservation_freezes_the_marker_and_takes_the_trial_clip(conn):
    tid, marker = _trial(conn)
    decision, pid = _sol(conn, trial=marker)
    assert decision.allowed and decision.approved_size_usdc == 20.0      # 100 x 0.75 capped at 20
    contract = _contract(conn, pid)
    assert contract["source"]["trial"] == {"trial_id": tid, "rule": "3a0b141ee", "clip_usd": 20.0}
    assert contract["sizing"]["approved_clip_usd"] == 20.0


def test_an_evm_trial_reservation_freezes_the_marker_and_takes_the_trial_clip(conn):
    tid, marker = _trial(conn, chain="4663")
    decision, pid = _evm(conn, trial=marker)
    assert decision.allowed and decision.approved_size_usdc == 20.0
    assert _contract(conn, pid)["source"]["trial"]["trial_id"] == tid


def test_a_non_trial_reservation_carries_no_marker(conn):
    decision, pid = _sol(conn)
    assert decision.allowed and decision.approved_size_usdc == 75.0
    assert "trial" not in _contract(conn, pid)["source"]


# --- refusals ---

def test_an_unmarked_reservation_for_a_wallet_with_an_open_trial_is_refused(conn):
    # the slot lane, or any path that cannot carry the clip and marker, must not reserve at full size
    _trial(conn)
    decision, pid = _sol(conn)
    assert pid is None and decision.reason.startswith("trial_unmarked")
    _trial(conn, chain="4663")
    decision, pid = _evm(conn)
    assert pid is None and decision.reason.startswith("trial_unmarked")


def test_a_trial_on_another_chain_does_not_touch_this_one(conn):
    _trial(conn, chain="4663")
    decision, pid = _sol(conn)
    assert decision.allowed and "trial" not in _contract(conn, pid)["source"]


@pytest.mark.parametrize("rule", [T.RULE, "tp-two-of-three-v1"])
def test_a_manual_entry_is_the_operators_own_and_is_not_refused(conn, rule):
    _tid, marker = _trial(conn, clip=7.0, rule=rule)
    decision, pid = _sol(conn, signal_source="manual", trial=marker)   # even handed a marker
    assert decision.allowed and decision.approved_size_usdc == 15.0      # 20 x 0.75, not the 7 clip
    assert "trial" not in _contract(conn, pid)["source"]


def test_a_stopped_trial_refuses_and_never_falls_back_to_the_full_clip(conn):
    tid, marker = _trial(conn)
    m.stop_trial(conn, tid, now_iso=NOW)
    for trial in (marker, None):
        decision, pid = _sol(conn, trial=trial, aid="a-%s" % bool(trial))
        assert pid is None and "trial_stopped" in decision.reason


def test_a_marker_for_an_ended_trial_is_refused(conn):
    tid, marker = _trial(conn)
    m.end_trial(conn, tid, now_iso=NOW, reason="operator")
    decision, pid = _sol(conn, trial=marker)
    assert pid is None and decision.reason.startswith("trial_ended")


def test_trial_concurrency_counts_open_trial_positions_and_trial_reservations(conn, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TRIAL_MAX_CONCURRENT", 1)
    _tid, a = _trial(conn, uid="ua")
    _tid, b = _trial(conn, uid="ub")
    assert _sol(conn, uid="ua", mint="M1", aid="a1", trial=a)[0].allowed
    decision, pid = _sol(conn, uid="ub", mint="M2", aid="a2", trial=b)
    assert pid is None and decision.reason.startswith("trial_concurrency")
    assert _sol(conn, uid="uc", mint="M3", aid="a3")[0].allowed     # a normal wallet is untouched


def test_a_trial_holding_20_positions_is_full(conn):
    tid, marker = _trial(conn)
    conn.execute("UPDATE memecoin_trial_arms SET positions=19 WHERE id=?", (tid,))
    conn.commit()
    assert _sol(conn, mint="M1", aid="a1", trial=marker)[0].allowed     # 19 + 0 reserved
    decision, pid = _sol(conn, mint="M2", aid="a2", trial=marker)       # 19 + 1 reserved
    assert pid is None and decision.reason.startswith("trial_full")


def test_two_threads_cannot_both_take_the_last_trial_slot(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TRIAL_MAX_CONCURRENT", 1)
    path = str(tmp_path / "ledger.db")
    setup = _connect(path)
    setup.execute("PRAGMA journal_mode=WAL")
    m.ensure_memecoin_tables(setup)
    markers = [_trial(setup, uid=u)[1] for u in ("ua", "ub")]
    for i in range(2):
        _attempt(setup, "t%d" % i, mint="MT%d" % i)
    setup.close()
    meet = threading.Barrier(2)
    real_gate = risk.memecoin_entry_allowed

    def gate(ctx, **kw):
        try:
            meet.wait(timeout=1.0)
        except threading.BrokenBarrierError:
            pass
        return real_gate(ctx, **kw)
    monkeypatch.setattr(risk, "memecoin_entry_allowed", gate)
    out, errors = [None, None], []

    def run(i):
        c = _connect(path)
        try:
            out[i] = risk.reserve_entry(
                c, mint="MT%d" % i, source_user_id=("ua", "ub")[i], strategy="alpha_b", day=DAY,
                usdc_balance=300.0, sol_balance=1.0, now_iso=NOW, attempt_id="t%d" % i,
                signal_source="follow_activity", classification=_classification(mint="MT%d" % i),
                trial=markers[i])
        except Exception as exc:
            errors.append(exc)
        finally:
            c.close()
    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    assert sorted(pid is not None for _d, pid in out) == [False, True]


# --- the counter, the contract on read, the auto-demote ---

def _book(conn, contract_json, *, pid_attempt="a1", uid="u", mint="MINT", pnl=None):
    pos = m.open_position(
        conn, source_user_id=uid, source_handle="h", mint=mint, ticker="T", entry_price=1.0,
        entry_notional=20.0, entry_fill_source="test", entry_lag_s=0.0, their_size_usd=100.0,
        opened_at=NOW, signal_source="follow_activity", strategy="alpha_b",
        execution_mode="real", network_id="solana", attempt_id=pid_attempt,
        funded_contract_version=1, funded_contract_hash="h",
        funded_contract_json=contract_json)
    if pnl is not None:
        conn.execute("UPDATE memecoin_positions SET is_open=0, realized_pnl=?, closed_at=? "
                     "WHERE id=?", (pnl, NOW, pos))
        conn.commit()
    return pos


# The two-of-three cohort must await each full outcome, rather than race the minute grader.
@pytest.mark.parametrize("chain", ["solana", "4663"])
@pytest.mark.parametrize("signed", [False, True])
def test_two_of_three_serializes_unsigned_and_unknown_signed_reservations(conn, chain, signed):
    _tid, marker = _trial(conn, chain=chain, rule="tp-two-of-three-v1")
    reserve = _sol if chain == "solana" else _evm
    mints = ["M1", "M2", "M3"] if chain == "solana" else ["0x" + "%040x" % i for i in (1, 2, 3)]
    first, pid = reserve(conn, trial=marker, mint=mints[0], aid="first")
    assert first.allowed
    if signed:
        m.set_pending_sig(conn, pid, "signed-identity", NOW)
    decision, blocked = reserve(conn, trial=marker, mint=mints[1], aid="second")
    assert blocked is None and decision.reason.startswith("trial_outcome_pending")
    m.release_pending(conn, pid, "confirmed_failed")
    assert reserve(conn, trial=marker, mint=mints[2], aid="third")[0].allowed


@pytest.mark.parametrize("state", [
    {"is_open": 1, "realized_pnl": None, "closed_at": None},
    {"is_open": 1, "realized_pnl": 4.0, "closed_at": None},  # partial sale
    {"is_open": 0, "realized_pnl": None, "closed_at": NOW},
    {"is_open": 0, "realized_pnl": 4.0, "closed_at": None},
    {"is_open": 0, "realized_pnl": float("inf"), "closed_at": NOW},
    {"is_open": 0, "realized_pnl": 4.0, "closed_at": NOW, "entry_notional": 0.0},
    {"is_open": 0, "realized_pnl": 4.0, "closed_at": NOW, "exit_reason": None},
    {"is_open": 0, "realized_pnl": 4.0, "closed_at": NOW, "exit_reason": ""},
])
def test_two_of_three_waits_for_active_partial_or_unknown_position_outcomes(conn, state):
    _tid, marker = _trial(conn, rule="tp-two-of-three-v1")
    _decision, pid = _sol(conn, trial=marker)
    pos = _book(conn, m.pending_row(conn, pid)["funded_contract_json"])
    m.confirm_pending(conn, pid)
    updates = {"exit_reason": "real_tp", **state}
    conn.execute("UPDATE memecoin_positions SET " + ",".join(k + "=?" for k in updates)
                 + " WHERE id=?", (*updates.values(), pos))
    conn.commit()
    decision, blocked = _sol(conn, trial=marker, mint="M2", aid="second")
    assert blocked is None and decision.reason.startswith("trial_outcome_pending")
    conn.execute("UPDATE memecoin_positions SET is_open=0, realized_pnl=4.0, closed_at=?, "
                 "entry_notional=20.0, exit_reason='real_tp' WHERE id=?", (NOW, pos))
    conn.commit()
    assert _sol(conn, trial=marker, mint="M3", aid="third")[0].allowed


def _two_of_three_close(conn, marker, i, *, reason, pnl):
    aid, mint = "sequence-%d" % i, "S%d" % i
    decision, pid = _sol(conn, trial=marker, mint=mint, aid=aid)
    assert decision.allowed, decision.reason
    pos = _book(conn, m.pending_row(conn, pid)["funded_contract_json"],
                pid_attempt=aid, mint=mint, pnl=pnl)
    m.confirm_pending(conn, pid)
    conn.execute("UPDATE memecoin_positions SET exit_reason=? WHERE id=?", (reason, pos))
    conn.commit()


@pytest.mark.parametrize("reason,pnl", [("real_tp", 4.0), ("manual", 4.0), ("real_tp", 0.0)])
def test_two_of_three_stops_after_two_same_outcomes_before_the_grader(conn, reason, pnl):
    _tid, marker = _trial(conn, rule="tp-two-of-three-v1")
    for i in range(2):
        _two_of_three_close(conn, marker, i, reason=reason, pnl=pnl)
    decision, pid = _sol(conn, trial=marker, mint="M3", aid="third")
    assert pid is None and decision.reason.startswith("trial_verdict_pending")


def test_two_of_three_mixed_outcomes_admit_only_one_third_reservation(conn):
    _tid, marker = _trial(conn, rule="tp-two-of-three-v1")
    _two_of_three_close(conn, marker, 0, reason="real_tp", pnl=4.0)
    _two_of_three_close(conn, marker, 1, reason="manual", pnl=4.0)
    decision, pid = _sol(conn, trial=marker, mint="M3", aid="third")
    assert decision.allowed and pid is not None
    blocked, fourth = _sol(conn, trial=marker, mint="M4", aid="fourth")
    assert fourth is None and blocked.reason.startswith("trial_full")


def test_two_of_three_cannot_race_two_first_admissions(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TRIAL_MAX_CONCURRENT", 50)
    path = str(tmp_path / "trial-sequence.db")
    setup = _connect(path)
    setup.execute("PRAGMA journal_mode=WAL")
    m.ensure_memecoin_tables(setup)
    _tid, marker = _trial(setup, rule="tp-two-of-three-v1")
    for i in range(2):
        _attempt(setup, "race-%d" % i, mint="R%d" % i)
    setup.close()
    meet = threading.Barrier(2)
    out, errors = [], []

    def run(i):
        c = _connect(path)
        try:
            meet.wait(timeout=2)
            out.append(risk.reserve_entry(
                c, mint="R%d" % i, source_user_id="u", strategy="alpha_b", day=DAY,
                usdc_balance=300.0, sol_balance=1.0, now_iso=NOW, attempt_id="race-%d" % i,
                signal_source="follow_activity", classification=_classification(mint="R%d" % i),
                trial=marker))
        except Exception as exc:
            errors.append(exc)
        finally:
            c.close()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=12)
    assert not any(t.is_alive() for t in threads)
    assert not errors, errors
    assert sorted(pid is not None for _decision, pid in out) == [False, True]


def test_the_legacy_trial_still_allows_two_unresolved_reservations(conn):
    _tid, marker = _trial(conn)
    assert _sol(conn, trial=marker, mint="M1", aid="first")[0].allowed
    assert _sol(conn, trial=marker, mint="M2", aid="second")[0].allowed


def test_two_of_three_does_not_apply_to_an_explicit_operator_risk_override(conn):
    _tid, marker = _trial(conn, rule="tp-two-of-three-v1")
    assert _sol(conn, trial=marker, mint="M1", aid="first")[0].allowed
    _attempt(conn, "override", mint="M2")
    decision, pid = risk.reserve_entry(
        conn, mint="M2", source_user_id="u", strategy="alpha_b", day=DAY,
        usdc_balance=300.0, sol_balance=1.0, now_iso=NOW, attempt_id="override",
        signal_source="follow_activity", classification=_classification(mint="M2"),
        trial=marker, override_risk_caps=True)
    assert decision.allowed and decision.approved_size_usdc == 75.0
    assert "trial" not in _contract(conn, pid)["source"]


def test_the_positions_counter_counts_a_booked_trial_position_once(conn):
    tid, marker = _trial(conn)
    _d, pid = _sol(conn, trial=marker)
    contract_json = m.pending_row(conn, pid)["funded_contract_json"]
    pos = _book(conn, contract_json)
    assert m.open_trial(conn, "u", "solana")["positions"] == 1
    m.link_trial_position(conn, pos, contract_json)          # a retried or recovered booking
    assert m.open_trial(conn, "u", "solana")["positions"] == 1
    _book(conn, contract_json, pid_attempt="a2", mint="M2")      # a second booked position
    assert m.open_trial(conn, "u", "solana")["positions"] == 2
    assert tid == m.open_trial(conn, "u", "solana")["id"]


def test_a_trial_contract_is_readable_by_the_frozen_policy_and_the_verifier(conn):
    from sniper.services.memecoin_funded_policy import _verified_contract, frozen_policy_from_position
    _tid, marker = _trial(conn)
    _d, pid = _sol(conn, trial=marker)
    row = m.pending_row(conn, pid)
    pos = {"mint": "MINT", "network_id": "solana",
           "funded_contract_version": row["funded_contract_version"],
           "funded_contract_hash": row["funded_contract_hash"],
           "funded_contract_json": row["funded_contract_json"]}
    assert _verified_contract(pos) is not None
    assert frozen_policy_from_position(pos) is not None


def test_the_auto_demote_cohort_leaves_trial_closes_out(conn):
    _tid, marker = _trial(conn)
    trial_json = m.pending_row(conn, _sol(conn, trial=marker)[1])["funded_contract_json"]
    for i in range(4):
        _book(conn, trial_json, pid_attempt="x%d" % i, mint="T%d" % i, pnl=-10.0)
    _book(conn, None, pid_attempt="y", mint="N1", pnl=-3.0)
    rec = m.wallet_real_record(conn, "u", network_id="solana")
    assert (rec["closed_count"], rec["consecutive_losses"], rec["realized_total"]) == (1, 1, -3.0)


# --- Codex P2 (2026-10-15): the two-of-three admission reads the trial's own position links ---

def _wallet_history(conn, n, uid="u"):
    """n closed real positions of the same wallet, none of them trial positions."""
    conn.executemany(
        "INSERT INTO memecoin_positions (source_user_id, mint, execution_mode, is_open, "
        "realized_pnl, entry_notional, closed_at, exit_reason, signal_source, network_id, "
        "opened_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(uid, "H%d" % i, "real", 0, 1.0, 20.0, NOW, "real_tp", "follow_activity", "solana", NOW)
         for i in range(n)])
    conn.commit()


def test_the_linked_closes_are_the_marker_closes(conn):
    tid, marker = _trial(conn, rule="tp-two-of-three-v1")
    _two_of_three_close(conn, marker, 0, reason="real_tp", pnl=4.0)
    _two_of_three_close(conn, marker, 1, reason="manual", pnl=-2.0)
    _wallet_history(conn, 50)
    row = m.open_trial(conn, "u", "solana")
    linked = m.trial_closes_linked(conn, row)
    assert linked == m.trial_closes(conn, row) and len(linked) == 2


def test_the_linked_read_is_an_index_search_not_a_wallet_scan(conn):
    import re
    from sniper.persistence import db_memecoin as dm
    _trial(conn, rule="tp-two-of-three-v1")
    row = m.open_trial(conn, "u", "solana")
    sql = []
    conn.set_trace_callback(sql.append)
    dm.trial_closes_linked(conn, row)
    conn.set_trace_callback(None)
    query = next(s for s in sql if "memecoin_trial_positions" in s)
    plan = " | ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + query))
    assert "idx_trial_positions_trial" in plan, plan
    assert not re.search(r"\bSCAN p\b", plan), plan          # never every position of the wallet


def test_two_of_three_admission_does_not_grow_with_the_wallets_history(conn):
    """Codex: 0.010 ms -> 4.105 ms under the writer lock on a 10,000-close wallet. Bound the
    admission's close read by the trial's own links, not by the history."""
    import statistics
    import time as _time
    tid, marker = _trial(conn, rule="tp-two-of-three-v1")
    _two_of_three_close(conn, marker, 0, reason="real_tp", pnl=4.0)
    row = m.open_trial(conn, "u", "solana")

    def p50():
        times = []
        for _ in range(30):
            started = _time.perf_counter()
            risk.trial_refusal(conn, source_user_id="u", network_id="solana", trial=marker,
                               classification=_classification())
            times.append(_time.perf_counter() - started)
        return statistics.median(times)
    small = p50()
    _wallet_history(conn, 10_000)
    big = p50()
    assert m.trial_closes_linked(conn, row) == m.trial_closes(conn, row)
    assert big < small * 3 + 0.0005, (small, big)
