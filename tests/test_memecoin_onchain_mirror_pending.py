"""A source mirror survives only proved unsigned deferrals; unknown attempts never resend."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from sniper.execution.exec_lock import serialize_executor
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_onchain_lane as lane
from sniper.services import memecoin_real_runner as R
from sniper.settings import memecoin as cfg
from test_memecoin_bsc_sniper_arm import COIN, NOW, UID, _copied_buy, _position, _sale


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("SNIPER_CLOCK_OFFSET_FILE", str(tmp_path / "no-clock-offset.json"))
    monkeypatch.setenv("MEMECOIN_REAL_ONCHAIN_SELL_MIRROR_WALLETS", "56:" + UID)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION", .30)
    monkeypatch.setattr(R, "_alert_exit_result", lambda *a, **kw: None)
    monkeypatch.setattr(R, "check_wallet_demotion", lambda *a, **kw: None)
    path, calls = tmp_path / "ledger.db", []
    def connect():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        return c
    c = connect()
    m.ensure_memecoin_tables(c)
    def body(conn, pos, reason, at):
        calls.append(pos["id"])
        claim = m.claim_position_close(conn, pos["id"], at, reason)
        assert claim is not None
        conn.execute("UPDATE memecoin_positions SET is_open=0,closed_at=?,close_claim_id=NULL "
                     "WHERE id=?", (at, pos["id"]))
        conn.commit()
        return {"status": "closed"}
    exit_fn = serialize_executor(body, "offline-mirror", lock_dir=str(tmp_path),
        release_unsigned_close_on_error=True, maintenance_chain="56")
    w = SimpleNamespace(c=c, connect=connect, calls=calls, body=body, mp=monkeypatch, lock_dir=str(tmp_path),
                        contexts={"56": {"exit_fn": exit_fn}})
    w.run = lambda row: R.real_onchain_sell_mirror(w.c, row, w.contexts, now_fn=lambda: NOW)
    yield w
    w.c.close()


def _gas_hold(c):
    return m.record_evm_gas_topup_submission(c, now_iso=NOW, day=NOW[:10], chain_id="56",
        native_amount_atomic=1, native_before_atomic=1, tx_hash="0x" + "c" * 64, nonce=0)


def _audit(c):
    return json.loads(c.execute("SELECT detail FROM memecoin_exit_trigger_audit "
                                "WHERE reason='mirror'").fetchone()[0])


def test_proved_unsigned_gas_hold_keeps_the_mirror_pending_and_retries_once(world):
    pid, debt = _position(world.c), _gas_hold(world.c)
    row = _sale(world.c)
    result = world.run(row)
    assert result["status"] == "mirror_pending" and result["reason"] == "gas_unresolved"
    assert _audit(world.c)["mirror_state"] == "pending" and _audit(world.c)["sell_row_id"] == row["id"]
    assert world.calls == [] and m.get_position(world.c, pid)["close_claim_id"] is None
    m.finish_evm_gas_topup(world.c, row_id=debt, status="confirmed")
    assert world.run(row)["status"] == "closed"
    assert world.calls == [pid] and _audit(world.c)["mirror_state"] == "done"
    world.run(row)
    assert world.calls == [pid]


def test_pending_mirror_survives_cursor_advance_restart_and_early_copied_source_sale(world):
    _copied_buy(world.c, block=1000)
    pid = _position(world.c, attempt_id="att1")
    row = _sale(world.c, at="2026-10-07T14:59:59+00:00", block=1002)
    debt = _gas_hold(world.c)
    cursor, _ = lane.sweep_onchain_sells(world.c, lambda c, r: world.run(r), cursor=0,
        network_id="56", wallets={UID}, held={}, clock=lambda: 0)
    assert cursor == row["id"] and world.calls == []
    world.c.close()
    world.c = world.connect()
    assert lane.sell_startup_cursor(world.c, "56", {UID}) == row["id"] - 1
    m.finish_evm_gas_topup(world.c, row_id=debt, status="confirmed")
    # A running sweep may already have advanced past the source row and lost its in-memory held map.
    cursor, _ = lane.sweep_onchain_sells(world.c, lambda c, r: world.run(r), cursor=cursor,
        network_id="56", wallets={UID}, held={}, clock=lambda: 10000)
    assert world.calls == [pid] and _audit(world.c)["source_block"] == 1000
    assert _audit(world.c)["sold_fraction"] == .4 and _audit(world.c)["mirror_state"] == "done"


def test_plain_deferred_dict_is_not_proof_that_a_mirror_can_resend(world):
    _position(world.c)
    row, calls = _sale(world.c), []
    def unknown(*args):
        calls.append("attempt")
        return {"status": "deferred", "reason": "gas_unresolved"}
    world.contexts["56"]["exit_fn"] = unknown
    assert world.run(row)["status"] == "mirror_pending"
    assert world.run(row)["reason"] == "execution_unknown"
    assert calls == ["attempt"] and _audit(world.c)["mirror_state"] == "executing"


def test_pending_mirror_retains_original_source_block_and_ratio_when_history_changes(world):
    _copied_buy(world.c, block=1000)
    pid = _position(world.c, attempt_id="att1")
    row = _sale(world.c, at="2026-10-07T14:59:59+00:00", block=1002)
    debt = _gas_hold(world.c)
    assert world.run(row)["status"] == "mirror_pending"
    world.c.execute("DELETE FROM onchain_wallet_detections")
    world.c.commit()
    m.finish_evm_gas_topup(world.c, row_id=debt, status="confirmed")
    assert world.run({**row, "sold_atomic": "1", "block_number": 999})["status"] == "closed"
    assert world.calls == [pid]
    assert _audit(world.c)["source_block"] == 1000 and _audit(world.c)["sold_fraction"] == .4


@pytest.mark.parametrize("signed", [False, True])
def test_a_crash_retains_unknown_or_signed_ownership_without_another_exit_body(world, signed):
    pid, row = _position(world.c), _sale(world.c)
    def crash(conn, pos, reason, at):
        world.calls.append(pos["id"])
        if signed:
            claim = m.claim_position_close(conn, pos["id"], at, reason)
            assert m.mark_position_close_submitted(conn, pos["id"], claim, at, "0x" + "d" * 64)
        raise RuntimeError("crash before a conclusive result")
    world.contexts["56"]["exit_fn"] = serialize_executor(crash, "offline-mirror",
        lock_dir=world.lock_dir,
        release_unsigned_close_on_error=True, maintenance_chain="56")
    with pytest.raises(RuntimeError):
        world.run(row)
    world.c.close()
    world.c = world.connect()
    assert world.run(row)["reason"] == ("close_owned" if signed else "execution_unknown")
    assert world.calls == [pid]
    if signed:
        assert m.get_position(world.c, pid)["close_sig"] == "0x" + "d" * 64


def test_canonical_close_after_a_crash_finishes_the_pending_journal_without_resend(world):
    pid, row = _position(world.c), _sale(world.c)
    def close_then_crash(conn, pos, reason, at):
        world.body(conn, pos, reason, at)
        raise RuntimeError("crash after canonical close")
    world.contexts["56"]["exit_fn"] = close_then_crash
    with pytest.raises(RuntimeError):
        world.run(row)
    assert world.run(row)["status"] == "already_closed"
    assert _audit(world.c)["mirror_state"] == "done" and world.calls == [pid]


def test_a_closed_pending_owner_cannot_retarget_a_new_position_in_the_same_coin(world):
    pid, row = _position(world.c), _sale(world.c)
    def close_then_crash(conn, pos, reason, at):
        world.body(conn, pos, reason, at)
        raise RuntimeError("crash after canonical close")
    world.contexts["56"]["exit_fn"] = close_then_crash
    with pytest.raises(RuntimeError):
        world.run(row)
    newer = _position(world.c, opened="2026-10-07T15:15:00+00:00")
    assert world.run(row)["status"] == "already_closed"
    assert _audit(world.c)["mirror_state"] == "done"
    assert m.get_position(world.c, newer)["is_open"] == 1 and world.calls == [pid]


def test_missing_fresh_position_metadata_does_not_prove_a_canonical_close(world):
    _position(world.c)
    row = _sale(world.c)
    world.contexts["56"]["exit_fn"] = lambda *args: {"status": "deferred"}
    world.mp.setattr(m, "get_position", lambda *args: None)
    assert world.run(row)["reason"] == "execution_unknown"
    assert _audit(world.c)["mirror_state"] == "executing"


def test_insert_only_race_preserves_an_existing_malformed_legacy_audit(world):
    pid, row = _position(world.c), _sale(world.c)
    original = m.record_exit_trigger_audit
    def race(conn, **kwargs):
        original(conn, **{**kwargs, "detail": "42", "replace": True})
        return original(conn, **kwargs)
    world.mp.setattr(m, "record_exit_trigger_audit", race)
    assert world.run(row)["status"] == "already_decided"
    assert world.calls == []
    assert world.c.execute("SELECT detail FROM memecoin_exit_trigger_audit WHERE position_id=?",
                           (pid,)).fetchone()[0] == "42"


@pytest.mark.parametrize("hold", ["tracking_paused_at", "mirror_disabled", "source_authority"])
def test_pending_mirror_rechecks_current_operator_and_source_authority(world, hold):
    pid, debt = _position(world.c), _gas_hold(world.c)
    row = _sale(world.c)
    assert world.run(row)["status"] == "mirror_pending"
    m.finish_evm_gas_topup(world.c, row_id=debt, status="confirmed")
    if hold == "source_authority":
        world.mp.setenv("MEMECOIN_REAL_ONCHAIN_SELL_MIRROR_WALLETS", "")
        assert world.run(row) is None
    else:
        world.c.execute("UPDATE memecoin_positions SET %s=? WHERE id=?" % hold,
                        (NOW if hold == "tracking_paused_at" else 1, pid))
        world.c.commit()
        assert world.run(row)["reason"] == ("tracking_paused" if hold == "tracking_paused_at" else hold)
        assert _audit(world.c)["mirror_state"] == "done"
        assert world.c.execute("SELECT allowed FROM memecoin_exit_trigger_audit").fetchone()[0] == 0
    assert world.calls == []


def test_missing_context_is_a_durable_pending_item_that_can_start_after_context_recovery(world):
    pid, row = _position(world.c), _sale(world.c)
    contexts = world.contexts
    world.contexts = {}
    assert world.run(row)["status"] == "mirror_pending"
    assert _audit(world.c)["mirror_state"] == "pending"
    world.contexts = contexts
    assert world.run(row)["status"] == "closed" and world.calls == [pid]


def test_an_existing_legacy_mirror_audit_stays_already_decided(world):
    pid, row = _position(world.c), _sale(world.c)
    old = json.dumps({"code": "size_mirror", "sell_tx": row["tx_hash"]})
    m.record_exit_trigger_audit(world.c, position_id=pid, reason="mirror", display_price=None,
        preview_usdc=None, allowed=True, detail=old, checked_at=NOW, key="onchain_sell:" + row["tx_hash"])
    assert world.run(row)["status"] == "already_decided" and world.calls == []
    assert world.c.execute("SELECT detail FROM memecoin_exit_trigger_audit").fetchone()[0] == old


def test_concurrent_delivery_keeps_the_existing_close_owner_and_one_exit_body(world):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    pid, row = _position(world.c), _sale(world.c)
    entered, release = threading.Event(), threading.Event()
    def body(conn, pos, reason, at):
        world.calls.append(pos["id"])
        assert m.claim_position_close(conn, pos["id"], at, reason) is not None
        entered.set()
        assert release.wait(3)
        conn.execute("UPDATE memecoin_positions SET is_open=0,closed_at=?,close_claim_id=NULL "
                     "WHERE id=?", (at, pos["id"]))
        conn.commit()
        return {"status": "closed"}
    world.contexts["56"]["exit_fn"] = serialize_executor(body, "offline-mirror",
        lock_dir=world.lock_dir, release_unsigned_close_on_error=True, maintenance_chain="56")
    def first():
        c = world.connect()
        try:
            return R.real_onchain_sell_mirror(c, row, world.contexts, now_fn=lambda: NOW)
        finally:
            c.close()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(first)
        try:
            assert entered.wait(3)
            assert world.run(row)["reason"] == "close_owned"
            assert world.calls == [pid]
        finally:
            release.set()
        assert future.result(timeout=3)["status"] == "closed"
    assert _audit(world.c)["mirror_state"] == "done"
