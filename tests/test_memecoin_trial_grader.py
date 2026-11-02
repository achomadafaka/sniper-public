"""Trial arm grading and actions (spec 5.3, 5.4, 5.6), on the production rows.

Positions are reserved through `reserve_entry` with the trial marker and booked through
`open_position`, the same rows the runner writes; the grader reads them by the contract marker.
"""
import json
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_risk as risk
from sniper.services import memecoin_trial as T
from sniper.services.memecoin_market_state import EntryClassification
from sniper.settings import memecoin as cfg

START = "2026-10-01T00:00:00+00:00"
LATER = "2026-10-02T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "fixed")
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING_BY_CHAIN", {})
    monkeypatch.setattr(cfg, "MEMECOIN_TRADE_SIZE_USD", 100.0)
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_FLOOR_USD", 5.0)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_TOTAL_EXPOSURE_USD", 10_000.0)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", 50)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT_TOTAL", 50)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT_BY_CHAIN", {})
    monkeypatch.setattr(cfg, "MEMECOIN_DAILY_LOSS_LIMIT_USD", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TRIAL_MAX_CONCURRENT", 50)
    monkeypatch.setenv("MEMECOIN_REAL_TRIAL_AUTO", "demote")


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.add_wallet(c, "u", "trialwallet")
    assert m.set_wallet_chain_armed(c, "u", "solana", True, force=True)["ok"]
    return c


def _classification(mint):
    return EntryClassification(
        classification_version=1, network_id="solana", mint=mint, signal_source="follow_activity",
        decision_at=START, coin_band="small", lifecycle="young", liquidity_class="unknown",
        momentum_phase="climbing", market_regime="hot_narrow", data_quality="partial",
        risk_scale=1.0, reason_codes=("coin_band:small",),
        facts={"network_id": "solana", "mint": mint, "effective_mcap": 500_000.0,
               "liquidity": 5_000.0, "base_clip_usd": None})


def _start(conn, rule=T.RULE):
    return m.start_trial(conn, source_user_id="u", chain="solana", clip_usd=20.0, rule=rule,
                         actor="test", now_iso=START)


def _close(conn, i, r, *, exit_reason="real_tp", uid="u"):
    """Reserve, book and close one trial position with return r (fraction)."""
    mint, aid = "Mint%d" % i, "att%d" % i
    m.start_entry_attempt(conn, attempt_id=aid, decision_started_at=START, network_id="solana",
                          mint=mint)
    marker = T.marker(m.open_trial(conn, uid, "solana"))
    decision, pid = risk.reserve_entry(
        conn, mint=mint, source_user_id=uid, strategy="alpha_b", day=START[:10],
        usdc_balance=1000.0, sol_balance=1.0, now_iso=START, attempt_id=aid,
        signal_source="follow_activity", origin_event_id="ev%d" % i,
        classification=_classification(mint), trial=marker)
    assert decision.allowed, decision.reason
    pending = m.pending_row(conn, pid)
    pos = m.open_position(
        conn, source_user_id=uid, source_handle="trialwallet", mint=mint, ticker="T",
        entry_price=1.0, entry_notional=pending["reserved_notional_usd"], entry_fill_source="t",
        entry_lag_s=0.0, their_size_usd=100.0, opened_at=START, signal_source="follow_activity",
        strategy="alpha_b", execution_mode="real", network_id="solana", attempt_id=aid,
        origin_event_id="ev%d" % i, funded_contract_version=pending["funded_contract_version"],
        funded_contract_hash=pending["funded_contract_hash"],
        funded_contract_json=pending["funded_contract_json"])
    m.confirm_pending(conn, pid)
    conn.execute("UPDATE memecoin_positions SET is_open=0, realized_pnl=?, closed_at=?, "
                 "exit_reason=? WHERE id=?",
                 (r * pending["reserved_notional_usd"], "2026-10-01T%02d:00:00+00:00" % (1 + i),
                  exit_reason, pos))
    conn.commit()
    return pos


def _grade(conn, pages, now=LATER):
    return T.grade_open_trials(conn, now_iso=now, alert_fn=lambda kind, text: pages.append((kind, text)))


def test_a_demote_at_five_ends_the_trial_and_disarms_the_chain(conn):
    tid = _start(conn)
    for i in range(5):
        _close(conn, i, -0.4)
    pages = []
    _grade(conn, pages)
    row = conn.execute("SELECT ended_at, end_reason FROM memecoin_trial_arms WHERE id=?",
                       (tid,)).fetchone()
    assert row["ended_at"] and row["end_reason"] == "demote"
    assert m.wallet_armed_chains(conn, "u") == []
    audit = conn.execute("SELECT actor FROM memecoin_wallet_approvals ORDER BY id DESC LIMIT 1").fetchone()
    assert audit["actor"] == "trial_rule"
    verdict = conn.execute("SELECT checkpoint, verdict, action, closes FROM memecoin_trial_verdicts").fetchone()
    assert tuple(verdict) == ("5", "DEMOTE", "ended_disarmed", 5)
    assert len(pages) == 1 and "DEMOTE" in pages[0][1] and "Mint4" in pages[0][1]


def test_auto_off_only_stops_the_trial_and_pages(conn, monkeypatch):
    monkeypatch.setenv("MEMECOIN_REAL_TRIAL_AUTO", "off")
    tid = _start(conn)
    for i in range(5):
        _close(conn, i, -0.4)
    pages = []
    _grade(conn, pages)
    row = conn.execute("SELECT stopped_at, ended_at FROM memecoin_trial_arms WHERE id=?", (tid,)).fetchone()
    assert row["stopped_at"] and row["ended_at"] is None
    assert m.wallet_armed_chains(conn, "u") == ["solana"] and len(pages) == 1


@pytest.mark.parametrize("auto,stopped,ended", [("demote", True, False), ("both", True, True)])
def test_a_promote_is_automatic_only_under_both(conn, monkeypatch, auto, stopped, ended):
    monkeypatch.setenv("MEMECOIN_REAL_TRIAL_AUTO", auto)
    tid = _start(conn)
    for i, r in enumerate([0.3, 0.2, 0.2, 0.2, 0.1, 0.1, 0.1, 0.0, -0.05, -0.05]):
        _close(conn, i, r)
    conn.execute("INSERT INTO memecoin_trial_verdicts (trial_id, checkpoint, checked_at, closes, "
                 "verdict, detail_json) VALUES (?, '5', ?, 5, 'CONTINUE', '{}')", (tid, START))
    pages = []
    _grade(conn, pages)
    row = conn.execute("SELECT stopped_at, ended_at, end_reason FROM memecoin_trial_arms WHERE id=?",
                       (tid,)).fetchone()
    assert bool(row["stopped_at"]) is stopped and bool(row["ended_at"]) is ended
    assert m.wallet_armed_chains(conn, "u") == ["solana"]          # never disarmed by a promote
    assert "PROMOTE" in pages[0][1]


def test_continue_writes_its_checkpoint_and_says_nothing(conn):
    tid = _start(conn)
    for i in range(5):
        _close(conn, i, 0.0)
    pages = []
    _grade(conn, pages)
    assert conn.execute("SELECT verdict FROM memecoin_trial_verdicts").fetchone()[0] == "CONTINUE"
    assert pages == [] and m.open_trial(conn, "u", "solana")["stopped_at"] is None
    _grade(conn, pages)                                            # written once
    assert conn.execute("SELECT COUNT(*) FROM memecoin_trial_verdicts").fetchone()[0] == 1
    assert tid


def test_a_cleared_approval_ends_the_trial(conn):
    tid = _start(conn)
    m.set_wallet_chain_armed(conn, "u", "solana", False, actor="operator", reason="roster")
    pages = []
    _grade(conn, pages)
    assert conn.execute("SELECT end_reason FROM memecoin_trial_arms WHERE id=?",
                        (tid,)).fetchone()[0] == "approval_cleared"
    assert pages and "approval_cleared" in pages[0][1]


def test_a_refused_disarm_keeps_the_trial_stopped_and_pages(conn, monkeypatch):
    tid = _start(conn)
    for i in range(5):
        _close(conn, i, -0.4)
    monkeypatch.setattr(m, "set_wallet_chain_armed", lambda *a, **k: {"ok": False, "reason": "no"})
    pages = []
    _grade(conn, pages)
    row = conn.execute("SELECT stopped_at, ended_at FROM memecoin_trial_arms WHERE id=?", (tid,)).fetchone()
    assert row["stopped_at"] and row["ended_at"] is None
    assert any("DISARM FAILED" in text for _k, text in pages)


def test_a_grading_error_writes_nothing_and_the_next_pass_retries(conn, monkeypatch):
    _start(conn)
    for i in range(5):
        _close(conn, i, -0.4)
    real = T.trial_verdict
    monkeypatch.setattr(T, "trial_verdict", lambda *a, **k: 1 / 0)
    _grade(conn, [])
    assert conn.execute("SELECT COUNT(*) FROM memecoin_trial_verdicts").fetchone()[0] == 0
    monkeypatch.setattr(T, "trial_verdict", real)
    _grade(conn, [])
    assert conn.execute("SELECT COUNT(*) FROM memecoin_trial_verdicts").fetchone()[0] == 1


def test_a_close_bought_by_another_armed_wallet_within_30_s_is_shared(conn):
    m.add_wallet(conn, "armed2", "other")
    m.set_wallet_chain_armed(conn, "armed2", "solana", True, force=True)
    _start(conn)
    pos = _close(conn, 0, 0.1)
    for fid, uid, at in (("ev0", "u", "2026-10-01T00:00:00.000Z"),
                         ("x1", "armed2", "2026-10-01T00:00:29.000Z"),
                         ("x2", "u", "2026-10-01T00:00:01.000Z")):
        conn.execute("INSERT INTO memecoin_events (fomo_event_id, source_user_id, mint, side, "
                     "created_at, network_id) VALUES (?,?,?,?,?,?)", (fid, uid, "Mint0", "buy", at, "solana"))
    closes = T.closes_with_shared(conn, m.open_trial(conn, "u", "solana"))
    assert [(c["position_id"], c["shared"]) for c in closes] == [(pos, True)]
    conn.execute("UPDATE memecoin_events SET created_at='2026-10-01T00:00:31.000Z' WHERE fomo_event_id='x1'")
    assert T.closes_with_shared(conn, m.open_trial(conn, "u", "solana"))[0]["shared"] is False


def test_the_page_lists_every_close_and_the_fee_share(conn):
    _start(conn)
    for i in range(5):
        _close(conn, i, -0.4, exit_reason="manual" if i == 0 else "b_stop")
    pages = []
    _grade(conn, pages)
    text = pages[0][1]
    for i in range(5):
        assert "Mint%d" % i in text
    assert "manual" in text and "fee" in text
    detail = json.loads(conn.execute("SELECT detail_json FROM memecoin_trial_verdicts").fetchone()[0])
    assert detail["n"] == 5 and len(detail["closes"]) == 5


def test_the_grader_thread_survives_a_failed_pass_and_reconnects(monkeypatch):
    connects, passes = [], []

    def connect():
        connects.append(1)
        return object()

    def grade(conn, *, now_iso, alert_fn=None):
        passes.append(conn)
        if len(passes) == 1:
            raise RuntimeError("locked")

    class Stop(Exception):
        pass

    def sleep(_s):
        if len(passes) >= 2:
            raise Stop
    monkeypatch.setattr(T, "grade_open_trials", grade)
    with pytest.raises(Stop):
        T.run_grader_forever(connect, now_iso=lambda: LATER, sleep=sleep)
    assert len(passes) == 2 and len(connects) == 2 and passes[0] is not passes[1]


def test_the_runner_loop_starts_the_grader_on_its_own_thread_and_connection():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "scripts" / "memecoin_runner_loop.py").read_text()
    assert "run_grader_forever" in src and 'name="memecoin-trial-grader"' in src


def test_a_stopped_trial_is_not_graded_again(conn, monkeypatch):
    monkeypatch.setenv("MEMECOIN_REAL_TRIAL_AUTO", "off")
    _start(conn)
    for i in range(10):                      # 10 closes in hand: checkpoint 5 stops the trial
        _close(conn, i, -0.4)
    pages = []
    _grade(conn, pages)
    _grade(conn, pages)                      # checkpoint 10 is reached but the trial is stopped
    assert conn.execute("SELECT COUNT(*) FROM memecoin_trial_verdicts").fetchone()[0] == 1
    assert len(pages) == 1


@pytest.mark.parametrize("auto", ["demote", "both"])
def test_insufficient_and_shadow_stop_and_page_but_never_disarm(conn, monkeypatch, auto):
    """Gamma MEDIUM on 92646580c: both are reports in rule 3a0b141ee; a disarm needs funded losses."""
    monkeypatch.setenv("MEMECOIN_REAL_TRIAL_AUTO", auto)
    tid = _start(conn)
    for i in range(3):
        _close(conn, i, 0.2)
    pages = []
    _grade(conn, pages, now="2026-10-15T00:00:01+00:00")          # day 14, 3 closes
    row = conn.execute("SELECT stopped_at, ended_at FROM memecoin_trial_arms WHERE id=?", (tid,)).fetchone()
    assert row["stopped_at"] and row["ended_at"] is None
    assert m.wallet_armed_chains(conn, "u") == ["solana"]
    assert "INSUFFICIENT" in pages[0][1]
    assert T._action("SHADOW", auto) == "stopped"


TWO_OF_THREE = "tp-two-of-three-v1"


@pytest.mark.parametrize("auto", ["off", "demote", "both"])
@pytest.mark.parametrize("outcomes,verdict", [
    ([(0.2, "real_tp"), (0.1, "tp")], "PROMOTE"),
    ([(0.2, "tp"), (-0.1, "real_sl"), (0.1, "real_tp")], "PROMOTE"),
    ([(-0.1, "real_sl"), (0.2, "tp"), (0.3, "manual")], "DEMOTE"),
    ([(0.3, "manual"), (0.3, "trailing")], "DEMOTE"),
    ([(0.0, "real_tp"), (-0.1, "tp")], "DEMOTE"),
])
def test_two_of_three_acts_on_actual_tp_exits_independent_of_global_auto(
        conn, monkeypatch, auto, outcomes, verdict):
    monkeypatch.setenv("MEMECOIN_REAL_TRIAL_AUTO", auto)
    tid = _start(conn, TWO_OF_THREE)
    for i, (r, reason) in enumerate(outcomes):
        _close(conn, i, r, exit_reason=reason)
    pages = []
    _grade(conn, pages)
    _grade(conn, pages)
    trial = conn.execute("SELECT ended_at,end_reason FROM memecoin_trial_arms WHERE id=?", (tid,)).fetchone()
    assert trial["ended_at"] and trial["end_reason"] == verdict.lower()
    rows = conn.execute("SELECT checkpoint,verdict,action,closes,detail_json FROM memecoin_trial_verdicts").fetchall()
    assert len(rows) == 1 and rows[0]["checkpoint"] == str(len(outcomes))
    assert rows[0]["verdict"] == verdict and rows[0]["closes"] == len(outcomes)
    assert rows[0]["action"] == ("ended_kept_armed" if verdict == "PROMOTE" else "ended_disarmed")
    detail = json.loads(rows[0]["detail_json"])
    assert detail["tp_count"] == sum(r > 0 and reason in {"real_tp", "tp"} for r, reason in outcomes)
    assert detail["non_tp_count"] == len(outcomes) - detail["tp_count"]
    assert m.wallet_armed_chains(conn, "u") == (["solana"] if verdict == "PROMOTE" else [])
    assert len(pages) == 1 and "TP" in pages[0][1]
    if verdict == "DEMOTE":
        reason = conn.execute("SELECT reason FROM memecoin_wallet_approvals ORDER BY id DESC LIMIT 1").fetchone()[0]
        assert TWO_OF_THREE in reason


def test_two_of_three_waits_for_the_third_after_a_split_without_expiring(conn):
    _start(conn, TWO_OF_THREE)
    _close(conn, 0, 0.2)
    _close(conn, 1, 0.4, exit_reason="manual")
    pages = []
    _grade(conn, pages, now="2027-01-01T00:00:00+00:00")
    assert conn.execute("SELECT COUNT(*) FROM memecoin_trial_verdicts").fetchone()[0] == 0
    assert m.open_trial(conn, "u", "solana")["stopped_at"] is None and pages == []


@pytest.mark.parametrize("change", [
    "is_open=1", "realized_pnl=NULL", "closed_at=NULL", "closed_at='unknown'",
    "closed_at='2026-10-01T01:00:00'",
    "entry_notional=0", "exit_reason=NULL", "exit_reason=''",
    "signal_source='manual'", "execution_mode='paper'",
])
def test_two_of_three_never_grades_partial_unknown_or_nontrial_close_population(conn, change):
    _start(conn, TWO_OF_THREE)
    _close(conn, 0, 0.2)
    pos = _close(conn, 1, 0.2)
    conn.execute("UPDATE memecoin_positions SET " + change + " WHERE id=?", (pos,))
    conn.commit()
    _grade(conn, [])
    assert conn.execute("SELECT COUNT(*) FROM memecoin_trial_verdicts").fetchone()[0] == 0
    assert m.open_trial(conn, "u", "solana")["stopped_at"] is None


def test_two_of_three_uses_first_three_by_time_then_position_id_and_stops_after_two():
    def close(pid, reason, pnl):
        return dict(position_id=pid, mint="M%d" % pid, realized_pnl=pnl, entry_notional=50.0,
                    closed_at=START, exit_reason=reason, shared=True)
    closes = [close(4, "tp", 20), close(2, "tp", 5), close(3, "real_sl", -5), close(1, "real_tp", 5)]
    assert T.TP_TWO_OF_THREE_RULE == TWO_OF_THREE
    assert [c["position_id"] for c in T.tp_two_of_three_closes(closes)] == [1, 2, 3]
    result = T.tp_two_of_three_verdict(closes)
    assert (result["checkpoint"], result["verdict"], result["detail"]["n"]) == (2, "PROMOTE", 2)
    assert [c["position_id"] for c in result["detail"]["closes"]] == [1, 2]
    assert T.tp_two_of_three_verdict(closes, done={2}) is None


@pytest.mark.parametrize("pnl", [None, float("nan"), float("inf"), float("-inf")])
def test_two_of_three_excludes_unknown_or_nonfinite_pnl(pnl):
    close = dict(position_id=1, mint="M", realized_pnl=pnl, entry_notional=50.0,
                 closed_at=START, exit_reason="real_tp")
    assert T.tp_two_of_three_closes([close]) == []
    assert T.tp_two_of_three_verdict([close, dict(close, position_id=2)]) is None


def test_two_of_three_orders_actual_close_instants_across_timezone_offsets():
    def close(pid, at, reason):
        return dict(position_id=pid, mint="M%d" % pid, realized_pnl=10.0, entry_notional=50.0,
                    closed_at=at, exit_reason=reason)
    rows = [close(3, "2026-10-15T00:00:00Z", "manual"),
            close(2, "2026-10-14T23:30:00+00:00", "tp"),
            close(1, "2026-10-15T01:00:00+02:00", "real_tp")]
    result = T.tp_two_of_three_verdict(rows)
    assert (result["checkpoint"], result["verdict"]) == (2, "PROMOTE")
    assert [c["position_id"] for c in result["detail"]["closes"]] == [1, 2]


def test_two_of_three_promotion_and_durable_verdict_commit_together(tmp_path):
    path = tmp_path / "trial.db"
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.add_wallet(c, "u", "trialwallet")
    assert m.set_wallet_chain_armed(c, "u", "solana", True, force=True)["ok"]
    tid = _start(c, TWO_OF_THREE)
    _close(c, 0, 0.2)
    _close(c, 1, 0.2)
    observer = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.2)
    seen = []

    def observe():
        seen.append(observer.execute("SELECT ended_at FROM memecoin_trial_arms WHERE id=?", (tid,)).fetchone()[0])
        return 0

    c.create_function("observe_uncommitted_trial", 0, observe)
    c.execute("CREATE TEMP TRIGGER reject_trial_verdict BEFORE INSERT ON memecoin_trial_verdicts "
              "BEGIN SELECT observe_uncommitted_trial(); SELECT RAISE(ABORT,'offline insert failure'); END")
    c.commit()
    pages = []
    _grade(c, pages)
    assert seen == [None] and pages == []
    assert observer.execute("SELECT ended_at FROM memecoin_trial_arms WHERE id=?", (tid,)).fetchone()[0] is None
    assert observer.execute("SELECT COUNT(*) FROM memecoin_trial_verdicts").fetchone()[0] == 0
    c.execute("DROP TRIGGER reject_trial_verdict")
    c.commit()
    _grade(c, pages)
    assert observer.execute("SELECT ended_at FROM memecoin_trial_arms WHERE id=?", (tid,)).fetchone()[0]
    assert observer.execute("SELECT verdict FROM memecoin_trial_verdicts").fetchone()[0] == "PROMOTE"
    observer.close()
    c.close()
