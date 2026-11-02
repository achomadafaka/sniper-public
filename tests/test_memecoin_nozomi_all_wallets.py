"""Wildcard transport scope preserves funded authority and existing Nozomi tip rules."""
import base64
import sqlite3
import time
from types import SimpleNamespace

import pytest
from solders.transaction import VersionedTransaction

from sniper.execution import solana_sender_ab as S
from sniper.persistence import db_memecoin as m
from sniper.settings import memecoin as cfg
from test_memecoin_slot_lane_sender_ab import (
    BLOCKHASH, _force_ids, _ids, _substages, conn,  # noqa: F401
)
from test_memecoin_slot_lane_nozomi_ab import _production_lane
from test_memecoin_nozomi_all_lanes import _executor, _Trace


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setenv("MEMECOIN_NOZOMI_ALL_B", "0")
    monkeypatch.setenv("MEMECOIN_NOZOMI_ALL_LANES", "0")


@pytest.mark.parametrize("wallets,uid,eligible", [
    ((), "w1", False), (("w1",), "w1", True), (("w1",), "w2", False),
    (("w1",), " w1 ", False), (("*",), "future-wallet", True),
    (("*", "w1"), "future-wallet", True), (("*",), None, False),
    (("*",), "", False), (("*",), "  ", False), (("*",), 0, False),
])
def test_wallet_scope_is_explicit_or_wildcard_but_never_blank(monkeypatch, wallets, uid, eligible):
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_NOZOMI_AB_WALLETS", frozenset(wallets))
    assert S.nozomi_wallet_enabled(uid) is eligible


@pytest.mark.parametrize("all_lanes", ["0", "1"])
@pytest.mark.parametrize("all_b", ["0", "1"])
@pytest.mark.parametrize("uid", ["future-wallet", None, "  "])
def test_wildcard_keeps_entry_flags_and_attempt_identity(monkeypatch, all_lanes, all_b, uid):
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_NOZOMI_AB_WALLETS", frozenset({"*"}))
    monkeypatch.setenv("MEMECOIN_NOZOMI_ALL_LANES", all_lanes)
    monkeypatch.setenv("MEMECOIN_NOZOMI_ALL_B", all_b)
    aid = _ids()["A"]
    arm = S.nozomi_entry_arm(aid, uid)
    if all_lanes == "0" or not str(uid or "").strip():
        assert arm is None and S.nozomi_gas_extra_sol(aid, uid) == 0
    else:
        assert arm == (S.assign_nozomi_all(aid) if all_b == "1" else S.assign_nozomi(aid))
        assert S.nozomi_gas_extra_sol(aid, uid) == (0.005 if all_b == "1" else 0)
    assert S.nozomi_entry_arm(None, uid) is None


@pytest.mark.parametrize("native", [False, True])
def test_wildcard_slot_send_tips_an_unlisted_armed_wallet_once(conn, tmp_path, monkeypatch, native):
    from sniper.services import memecoin_slot_lane as lane
    aid = _ids()["A"]
    _force_ids(monkeypatch, [aid])
    monkeypatch.setenv("MEMECOIN_NOZOMI_ALL_B", "1")
    ln, buy, sent, sender, nozomi = _production_lane(conn, tmp_path, monkeypatch, wallets=("*",))
    if native:
        ln.sol_buffer_cap_usd, ln.shadow_clip_fn = 200.0, lambda: 50.0
        ln.snapshot_fn = lambda: lane.PrefetchSnapshot(
            sol_usd=150.0, usdc_balance=400.0, sol_balance=5.0, blockhash=BLOCKHASH,
            last_valid_height=1, observed_mono={k: time.monotonic() for k in
                                             ("sol_usd", "balances", "blockhash")})
    got = ln.handle(buy("wildcard-slot", wallet="w2"))
    assert got["status"] == "failed", got
    (payload,) = sent
    (_, _, body), = nozomi.calls
    assert body["params"][0] == payload and sender.calls == []
    message = VersionedTransaction.from_bytes(base64.b64decode(payload)).message
    assert S.tip_transfers(message, S._NOZOMI_SET) == [
        (S.assign_nozomi_all(aid)["tip_account"], S.NOZOMI_TIP_LAMPORTS)]
    row = m.pending_row(conn, got["pending_id"])
    assert row["funding_asset"] == ("sol" if native else "usdc")
    assert int(row["funding_overhead_atomic"]) == S.NOZOMI_TIP_LAMPORTS + (
        lane.NATIVE_ATA_RENT_RESERVE_LAMPORTS if native else 0)
    stages = _substages(conn, aid)
    assert stages["nozomi_ab"]["rule"] == S.NOZOMI_ALL_RULE
    assert stages["nozomi_ab_send"]["nozomi_payload_sha_match"] is True


def test_wildcard_does_not_admit_a_watch_only_wallet(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMECOIN_NOZOMI_ALL_B", "1")
    ln, buy, sent, sender, nozomi = _production_lane(conn, tmp_path, monkeypatch, wallets=("*",))
    got = ln.handle(buy("wildcard-watch-only", wallet="watch-only"))
    assert got["reason"] == "not_approved", got
    assert sent == [] and sender.calls == [] and nozomi.calls == []
    assert conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries").fetchone()[0] == 0


def test_wildcard_does_not_tip_without_a_ready_provider(conn, tmp_path, monkeypatch):
    aid = _ids()["A"]
    _force_ids(monkeypatch, [aid])
    monkeypatch.setenv("MEMECOIN_NOZOMI_ALL_B", "1")
    ln, buy, sent, _, nozomi = _production_lane(
        conn, tmp_path, monkeypatch, nozomi=False, wallets=("*",))
    assert ln.handle(buy("wildcard-no-provider", wallet="w2"))["status"] == "failed"
    message = VersionedTransaction.from_bytes(base64.b64decode(sent[0])).message
    assert S.tip_transfers(message, S._NOZOMI_SET) == [] and nozomi.calls == []
    assert "nozomi_ab" not in _substages(conn, aid)


def test_wildcard_ordinary_send_reaches_nozomi_with_the_same_signed_bytes(conn, monkeypatch):
    from sniper.execution import solana_real_seams as seams
    monkeypatch.setattr(seams, "_http", lambda *_a, **_k: pytest.fail("unexpected provider read"))
    monkeypatch.setenv("MEMECOIN_NOZOMI_ALL_B", "1")
    seam, pid, _, _, sent, sender, nozomi, _ = _executor(
        conn, monkeypatch, knob=True, wallets=("*",))
    trace = _Trace(_ids()["A"])
    result = seam["execute_fn"](
        conn, pid, "Coin", 50.0, "2026-10-09T14:00:01+00:00", trace=trace,
        entry_min_slot=100, entry_max_slot=101,
        entry_head_fn=lambda: (100, time.monotonic()))
    assert result["status"] == "failed" and result["reason"] == "fixture"
    (payload,) = sent
    (_, _, body), = nozomi.calls
    assert body["params"][0] == payload and sender.calls == []
    assert trace.stages["nozomi_ab"]["rule"] == S.NOZOMI_ALL_RULE
    assert trace.stages["nozomi_ab_send"]["nozomi_sig_match"] is True


@pytest.mark.parametrize("wallets,ready,scope", [
    (("*",), True, "all"), (("*",), False, "all"),
    (("w1",), True, "listed"), ((), False, "listed"),
])
def test_started_lane_exposes_actual_provider_readiness_and_scope(monkeypatch, wallets, ready, scope):
    from sniper.services import memecoin_slot_lane as lane, memecoin_real_runner as runner
    monkeypatch.setattr("threading.Thread.start", lambda self: None)
    monkeypatch.setattr(lane, "load_cu_demotions", lambda *_a, **_k: ())
    monkeypatch.setattr(runner, "_momentum_read_pool",
                        lambda: SimpleNamespace(submit=lambda *_a: None))
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_NOZOMI_AB_WALLETS", frozenset(wallets))
    rc = dict(usdc_fn=lambda: pytest.fail("provider read"), sol_fn=lambda: None,
              latest_blockhash_fn=lambda: None, slot_lock_key="fixture", nozomi_ab_ready=ready)
    status = lane.start_slot_lane(
        rc, builder=object(), signal_start_fn=lambda *_a, **_k: None,
        sol_usd_fn=lambda: None, log=lambda _: None, roster_path="unused",
        conn_factory=object, readonly_conn_factory=object)
    assert status["nozomi_ab_ready"] is ready
    assert status["nozomi_wallet_scope"] == scope


@pytest.mark.parametrize("tip_funds,allowed", [(0.004, False), (0.006, True)])
def test_wildcard_ordinary_gas_gate_preserves_exit_funds(conn, monkeypatch, tip_funds, allowed):
    from sniper.services import memecoin_risk as risk
    monkeypatch.setenv("MEMECOIN_NOZOMI_ALL_B", "1")
    monkeypatch.setenv("MEMECOIN_NOZOMI_ALL_LANES", "1")
    for name, value in (("MEMECOIN_REAL_ENABLED", True), ("MEMECOIN_MIN_TRADE_FLOOR_USD", 0.0),
                        ("MEMECOIN_DAILY_LOSS_LIMIT_USD", 0.0), ("MEMECOIN_GAS_ENTRY_SOL", 0.002),
                        ("MEMECOIN_GAS_EXIT_SOL", 0.002), ("MEMECOIN_GAS_RETRY_FACTOR", 2.0),
                        ("MEMECOIN_SLOT_LANE_NOZOMI_AB_WALLETS", frozenset({"*"}))):
        monkeypatch.setattr(cfg, name, value)
    gas = risk.required_entry_sol(0, 0.002, 0.002, 2.0)
    decision, pid = risk.reserve_entry(
        conn, mint="wildcard-gas", source_user_id="future-wallet", strategy=cfg.MEMECOIN_REAL_POLICY,
        day="2026-10-11", usdc_balance=300.0, sol_balance=gas + tip_funds,
        now_iso="2026-10-11T14:00:00+00:00", attempt_id=_ids()["A"])
    assert decision.allowed is allowed
    assert (pid is not None) is allowed
    if not allowed:
        assert "insufficient SOL" in decision.reason


def _grader_db():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE memecoin_entry_attempts (source_user_id TEXT, network_id TEXT)")
    db.execute("CREATE TABLE memecoin_wallets (source_user_id TEXT, handle TEXT, real_approved INTEGER)")
    db.executemany("INSERT INTO memecoin_entry_attempts VALUES (?, ?)", [
        ("retired", "solana"), ("future-wallet", "solana"), ("retired", "solana"),
        ("unlisted", "solana"), ("ethereum-only", "1"), (None, "solana"),
        ("", "solana"), ("  ", "solana"), ("\t\n", "solana")])
    db.executemany("INSERT INTO memecoin_wallets VALUES (?, ?, ?)", [
        ("retired", "Retired", 0), ("future-wallet", "Future", 1), ("ethereum-only", "EVM", 1)])
    return db


def test_wildcard_grader_keeps_historical_wallets_after_disarm(tmp_path, monkeypatch):
    from scripts.research import nozomi_ab_grade as grade
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text('MEMECOIN_SLOT_LANE_NOZOMI_AB_WALLETS="*,future-wallet"\n')
    with _grader_db() as db:
        assert grade.env_wallets(db) == {
            "retired": "Retired", "future-wallet": "Future", "unlisted": "unlisted"}


def test_grader_explicit_list_still_scopes_wallets(tmp_path, monkeypatch):
    from scripts.research import nozomi_ab_grade as grade
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("MEMECOIN_SLOT_LANE_NOZOMI_AB_WALLETS=retired,missing\n")
    with _grader_db() as db:
        assert grade.env_wallets(db) == {"retired": "Retired", "missing": "missing"}


def test_empty_wildcard_grader_does_not_restore_the_old_roster(tmp_path, monkeypatch):
    from scripts.research import nozomi_ab_grade as grade
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("MEMECOIN_SLOT_LANE_NOZOMI_AB_WALLETS=*\n")
    with _grader_db() as db:
        db.execute("DELETE FROM memecoin_entry_attempts")
        assert grade.env_wallets(db) == {}
