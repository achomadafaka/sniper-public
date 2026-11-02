"""4663 fast lane M2 step 2: the cached inputs the lane reads instead of the network (spec
2026-10-01-memecoin-4663-fast-lane-design.md §2.5-§2.6; Alpha 2026-10-01: no RPC before the send).
Balances, the pending nonce and the gas price come from the detector's research-host poll inside
the datagram; they are valid only when no executor on this wallet has finished since they were
read, which the execution-generation stamp in serialize_executor tells."""
import os
import time

import pytest

from sniper.execution import exec_lock


def test_every_serialized_execution_advances_the_generation_even_when_it_raises(tmp_path):
    d = str(tmp_path)
    assert exec_lock.exec_generation("evm-4663-0xabc", lock_dir=d) == 0.0
    ok = exec_lock.serialize_executor(lambda: "done", "evm-4663-0xabc", lock_dir=d)
    t0 = time.time()
    assert ok() == "done"
    first = exec_lock.exec_generation("evm-4663-0xabc", lock_dir=d)
    assert first >= t0 - 1
    time.sleep(0.01)

    def boom():
        raise RuntimeError("executor failed")
    bad = exec_lock.serialize_executor(boom, "evm-4663-0xabc", lock_dir=d)
    with pytest.raises(RuntimeError):
        bad()
    assert exec_lock.exec_generation("evm-4663-0xabc", lock_dir=d) > first
    # Other keys (Base on the same address, Solana) keep their own generation.
    assert exec_lock.exec_generation("evm-8453-0xabc", lock_dir=d) == 0.0


def test_the_generation_is_stamped_while_the_lock_is_still_held(tmp_path):
    # A snapshot read that starts after the stamp can never predate the execution's last write.
    d = str(tmp_path)
    seen = {}

    def probe():
        seen["held_free"] = None
        return "x"
    wrapped = exec_lock.serialize_executor(probe, "k", lock_dir=d)
    real_touch = exec_lock.touch_generation

    def touch(key, *, lock_dir=None):
        with exec_lock.wallet_exec_lock(key, lock_dir=lock_dir, blocking=False) as free:
            seen["held_free"] = free
        real_touch(key, lock_dir=lock_dir)
    exec_lock.touch_generation = touch
    try:
        wrapped()
    finally:
        exec_lock.touch_generation = real_touch
    assert seen["held_free"] is False


def test_an_ordinary_refusal_or_unknown_result_still_invalidates_the_snapshot(tmp_path):
    for i, result in enumerate(({"status": "aborted", "wallet_state_unchanged": True}, None)):
        key = "opaque-%s" % i
        wrapped = exec_lock.serialize_executor(lambda: result, key, lock_dir=str(tmp_path))
        assert wrapped() == result
        assert exec_lock.exec_generation(key, lock_dir=str(tmp_path)) > 0


# ---- the runner-side checks on the datagram's inputs (no RPC) ----

from sniper.services import memecoin_rh_fast_lane as lane  # noqa: E402

SNAP = dict(native_wei=10 ** 17, weth_wei=5 * 10 ** 16, nonce=42, gas_price_wei=40_000_000,
            read_at=1000.0)
GOOD = dict(snapshot=SNAP, eth_usd=2500.0, eth_usd_at=1000.0)


def _reason(payload, **kw):
    args = dict(generation=900.0, now_host=1005.0, clip_wei=2 * 10 ** 16, gas_limit=1_200_000,
                low_wei=2 * 10 ** 15, max_snapshot_age_s=30.0, eth_usd_max_age_s=120.0)
    args.update(kw)
    return lane.inputs_reason(payload, **args)


def test_good_inputs_pass():
    assert _reason(GOOD) is None


@pytest.mark.parametrize("payload, kw, reason", [
    (dict(GOOD, snapshot=None), {}, "no_snapshot"),
    (GOOD, dict(generation=1000.0), "snapshot_predates_execution"),      # read AT the stamp: stale
    (GOOD, dict(generation=1003.0), "snapshot_predates_execution"),
    (GOOD, dict(now_host=1031.0), "snapshot_stale"),
    (dict(GOOD, eth_usd=None), {}, "no_eth_usd"),
    (dict(GOOD, eth_usd_at=None), {}, "no_eth_usd"),
    (GOOD, dict(now_host=1121.0, max_snapshot_age_s=999.0), "eth_usd_stale"),
    (dict(GOOD, snapshot=dict(SNAP, native_wei=2 * 10 ** 16)), {}, "float_short"),
    (dict(GOOD, snapshot=dict(SNAP, gas_price_wei=None)), {}, "float_short"),
])
def test_every_stale_absent_or_short_input_is_a_named_fallback(payload, kw, reason):
    assert _reason(payload, **kw) == reason


def test_the_clip_in_wei_comes_from_the_usd_clip_at_the_datagram_eth_price():
    assert lane.clip_wei_for(50.0, 2500.0) == 2 * 10 ** 16
    assert lane.clip_wei_for(50.0, None) is None and lane.clip_wei_for(50.0, 0) is None


# ---- Gamma MEDIUM on 040448cf0: every broadcast advances the generation; a failed stamp latches ----

from eth_account import Account  # noqa: E402


def _raw(chain_id, *, typed=True):
    acct = Account.from_key(b"\x02" * 32)
    tx = ({"type": 2, "chainId": chain_id, "nonce": 3, "maxFeePerGas": 10, "maxPriorityFeePerGas": 0,
           "gas": 21000, "to": acct.address, "value": 0, "data": b""} if typed else
          {"chainId": chain_id, "nonce": 3, "gasPrice": 10, "gas": 21000, "to": acct.address,
           "value": 0, "data": b""})
    return "0x" + bytes(Account.sign_transaction(tx, acct.key).raw_transaction).hex()


@pytest.fixture
def latch(monkeypatch, tmp_path):
    import threading
    monkeypatch.setattr(exec_lock, "_GENERATION_FAULT", threading.Event())
    monkeypatch.setattr(exec_lock, "_GENERATION_FAULT_REASON", [None])
    monkeypatch.setattr(exec_lock, "_default_lock_dir", lambda: str(tmp_path))
    return tmp_path


@pytest.mark.parametrize("typed", [True, False])
def test_a_broadcast_stamps_its_chains_generation(latch, typed):
    d = str(latch)
    assert exec_lock.exec_generation(exec_lock.chain_generation_key("4663"), lock_dir=d) == 0.0
    exec_lock.stamp_send(_raw(4663, typed=typed))
    assert exec_lock.exec_generation(exec_lock.chain_generation_key("4663"), lock_dir=d) > 0
    assert exec_lock.exec_generation(exec_lock.chain_generation_key("8453"), lock_dir=d) == 0.0
    assert not exec_lock.generation_fault()


def test_an_undecodable_broadcast_latches_the_fault(latch):
    exec_lock.stamp_send("0xdeadbeef")
    assert exec_lock.generation_fault()


def test_eth_send_raw_stamps_after_the_send_even_when_the_transport_raises(latch, monkeypatch):
    from sniper.execution import evm_real_seams as seams
    order = []

    def boom(*_a, **_k):
        order.append("send")
        raise TimeoutError("transport")
    real = exec_lock.stamp_send
    monkeypatch.setattr(seams, "_rpc", boom)
    monkeypatch.setattr(exec_lock, "stamp_send",
                        lambda raw, **k: (order.append("stamp"), real(raw, **k)))
    assert seams.eth_send_raw(_raw(4663), "http://x") is None
    assert order == ["send", "stamp"]
    assert exec_lock.exec_generation(exec_lock.chain_generation_key("4663"), lock_dir=str(latch)) > 0


def test_a_fanout_provider_submit_stamps_too(latch, monkeypatch):
    from types import SimpleNamespace
    from sniper.execution import evm_fast_submit as fs
    monkeypatch.setattr(fs, "_classify_evm", lambda *a, **k: "accepted")
    provider = fs.EvmRpcProvider(None, "http://x", provider_id="p")
    assert provider.submit(SimpleNamespace(signed_payload=_raw(4663))) == "accepted"
    assert exec_lock.exec_generation(exec_lock.chain_generation_key("4663"), lock_dir=str(latch)) > 0


def test_a_raising_stamp_never_fails_the_execution_and_latches_the_fault(latch, monkeypatch):
    def broken(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(exec_lock, "touch_generation", broken)
    assert exec_lock.serialize_executor(lambda: "filled", "k", lock_dir=str(latch))() == "filled"
    assert exec_lock.generation_fault()


def test_an_unknown_generation_is_a_fallback():
    assert _reason(GOOD, generation=None) == "generation_unknown"


def test_the_lane_generation_is_the_later_of_the_wallet_lock_and_the_chain_broadcast(latch):
    d = str(latch)
    gen = lane.lane_generation_fn("evm-4663-0xabc", "4663", lock_dir=d)
    assert gen() == 0.0
    exec_lock.stamp_send(_raw(4663), lock_dir=d)
    chain_stamp = gen()
    assert chain_stamp > 0
    exec_lock.touch_generation("evm-4663-0xabc", lock_dir=d)
    assert gen() >= chain_stamp
    exec_lock._GENERATION_FAULT.set()
    assert gen() is None


def test_a_broadcast_after_an_execution_still_advances_the_lane_generation(latch):
    import time
    d = str(latch)
    gen = lane.lane_generation_fn("evm-4663-0xabc", "4663", lock_dir=d)
    exec_lock.touch_generation("evm-4663-0xabc", lock_dir=d)
    first = gen()
    time.sleep(0.01)
    exec_lock.stamp_send(_raw(4663), lock_dir=d)             # e.g. the pre-approval daemon's send
    assert gen() > first


def test_the_fanout_stamp_runs_after_the_provider_send(latch, monkeypatch):
    # Alpha (a): the stamp runs only after the bytes went out.
    from types import SimpleNamespace
    from sniper.execution import evm_fast_submit as fs
    order = []
    monkeypatch.setattr(fs, "_classify_evm", lambda *a, **k: order.append("send") or "accepted")
    real = exec_lock.stamp_send
    monkeypatch.setattr(exec_lock, "stamp_send",
                        lambda raw, **k: (order.append("stamp"), real(raw, **k)))
    fs.EvmRpcProvider(None, "http://x", provider_id="p").submit(SimpleNamespace(signed_payload=_raw(4663)))
    assert order == ["send", "stamp"]


@pytest.mark.parametrize("answer, want", [({"result": "0xhash"}, "0xhash"),
                                          ({"error": {"message": "nonce too low"}}, False),
                                          ({"error": {"message": "odd"}}, None)])
def test_a_failing_stamp_never_changes_what_the_send_returns(latch, monkeypatch, answer, want):
    # Alpha (b): a stamp failure latches the lane fault and nothing else.
    from sniper.execution import evm_real_seams as seams

    def broken(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(seams, "_rpc", lambda *a, **k: answer)
    monkeypatch.setattr(exec_lock, "touch_generation", broken)
    assert seams.eth_send_raw(_raw(4663), "http://x") == want
    assert exec_lock.generation_fault()


def test_a_snapshot_must_start_the_margin_after_the_last_stamp():
    # Gamma LOW: the research node follows the sequencer; a poll right after our send can still
    # answer the old pending nonce.
    assert _reason(GOOD, generation=999.5, generation_margin_s=1.0) == "snapshot_predates_execution"
    assert _reason(GOOD, generation=998.9, generation_margin_s=1.0) is None


def test_no_test_inherits_a_latched_generation_fault():
    # The canary for conftest's _reset_broadcast_generation_fault: a sibling test that sends a fake
    # raw latches the process-wide fault, and without the reset every later lane test on the same
    # xdist worker falls back with generation_unknown (Alpha 2026-10-01, 22 failures).
    assert not exec_lock.generation_fault()


# ---- Alpha: a latched fault is loud once, and the lane's state row says so and why ----

def test_the_first_latch_reason_is_kept_and_logged_once_at_error(latch, caplog):
    import logging
    caplog.set_level(logging.DEBUG, logger="sniper.execution.exec_lock")
    exec_lock.latch_generation_fault("stamp_send: ValueError: not a signed EVM transaction")
    exec_lock.latch_generation_fault("execution stamp evm-4663-0xabc: OSError: disk full")
    assert exec_lock.generation_fault()
    assert exec_lock.generation_fault_reason() == (
        "stamp_send: ValueError: not a signed EVM transaction")
    loud = [r for r in caplog.records if "OFF until restart" in r.getMessage()]
    assert len(loud) == 1 and loud[0].levelname == "ERROR"


def test_each_latch_site_names_its_cause(latch, monkeypatch):
    exec_lock.stamp_send("0xnot-a-tx", lock_dir=str(latch))
    assert exec_lock.generation_fault_reason().startswith("stamp_send: ValueError")


def test_an_execution_stamp_failure_names_its_wallet_key(latch, monkeypatch):
    def broken(key, lock_dir=None):
        raise OSError("read-only file system")
    monkeypatch.setattr(exec_lock, "touch_generation", broken)
    wrapped = exec_lock.serialize_executor(lambda: "done", "evm-4663-0xabc", lock_dir=str(latch))
    assert wrapped() == "done"
    assert exec_lock.generation_fault_reason() == (
        "execution stamp evm-4663-0xabc: OSError: read-only file system")


def test_the_lane_state_row_says_latched_and_why(latch, tmp_path):
    import sqlite3
    db = str(tmp_path / "lane.db")
    lane.write_lane_state(db)
    exec_lock.latch_generation_fault("stamp_send: ValueError: x")
    lane.write_lane_state(db)
    rows = sqlite3.connect(db).execute(
        "SELECT generation_fault, reason FROM rh_fast_lane_state ORDER BY rowid").fetchall()
    assert rows == [(0, None), (1, "stamp_send: ValueError: x")]


def test_the_lane_announces_a_latch_once_in_its_log_and_by_alert(latch):
    logs, alerts = [], []
    watch = lane.LatchWatch(log=logs.append, alert_fn=alerts.append)
    assert watch.check() is False and logs == alerts == []
    exec_lock.latch_generation_fault("stamp_send: ValueError: x")
    assert watch.check() is True and watch.check() is False
    assert len(logs) == 1 and "OFF until restart" in logs[0] and "stamp_send" in logs[0]
    assert len(alerts) == 1 and "stamp_send" in alerts[0]


def test_an_unreadable_generation_is_a_fault_never_a_never_stamped_zero(tmp_path, monkeypatch):
    # Codex EVM-08: os.stat raising EACCES/EIO returned 0.0, the "never stamped" value, and left the
    # fault latch clear, so the lane could trust a stale balance/nonce snapshot.
    from sniper.services.memecoin_rh_fast_lane import lane_generation_fn
    d = str(tmp_path)
    exec_lock.touch_generation("evm-4663-0xabc", lock_dir=d)
    real_stat = os.stat

    def denied(path, *a, **k):
        if str(path).startswith(d):
            raise PermissionError(13, "Permission denied", str(path))
        return real_stat(path, *a, **k)
    monkeypatch.setattr(exec_lock.os, "stat", denied)
    assert exec_lock.exec_generation("evm-4663-0xabc", lock_dir=d) is None
    assert exec_lock.generation_fault() and "generation read" in exec_lock.generation_fault_reason()
    assert lane_generation_fn("evm-4663-0xabc", "4663", lock_dir=d)() is None


def test_a_missing_generation_is_still_never_stamped_and_no_fault(tmp_path):
    assert exec_lock.exec_generation("evm-4663-none", lock_dir=str(tmp_path)) == 0.0
    assert not exec_lock.generation_fault()


def test_safety_warmup_evidence_distinguishes_missing_delivery_expiry_and_refresh():
    from sniper.services.memecoin_rh_fast_lane import SafetyCache
    cache = SafetyCache(lambda m, usd=None: (True, 'safe'), ttl_s=10, usd=50,
                        refresh_lead_s=2)
    evidence = {}
    assert cache.reason('0xabc', now=100, evidence=evidence) == 'no_safety_verdict'
    assert evidence['candidate_first_at'] is None
    cache.note_candidates([{'mint': '0xabc', 'priority': 0}], now=100)
    cache.run_one('0xabc', now=101)
    evidence = {}
    assert cache.reason('0xabc', now=112, evidence=evidence) == 'safety_stale'
    assert evidence['candidate_first_at'] == 100
    assert evidence['warm_worker_start_at'] == 101
    assert evidence['warm_worker_end_at'] >= 101
    assert evidence['verdict_at'] >= 101
    cache.note_candidates([{'mint': '0xdef', 'priority': 0}], now=112)
    assert cache.next_due(112) == '0xdef'
    cache.run_one('0xdef', now=112)
    assert cache.next_due(112) == '0xabc'


def test_warmup_provider_failure_remains_unknown_and_diagnostics_expire_with_candidate():
    from sniper.services.memecoin_rh_fast_lane import SafetyCache
    def unavailable(m, usd=None):
        raise TimeoutError('provider overrun')
    cache = SafetyCache(unavailable, ttl_s=10, usd=50)
    cache.note_candidates([{'mint': '0xabc', 'priority': 0}], now=100)
    assert cache.run_one('0xabc', now=101) is None
    evidence = {}
    assert cache.reason('0xabc', now=102, evidence=evidence) == 'no_safety_verdict'
    assert evidence['warm_worker_outcome'] == 'provider_error:TimeoutError'
    assert evidence['verdict_at'] is None
    cache.note_candidates([], now=701)
    evidence = {}
    cache.reason('0xabc', now=701, evidence=evidence)
    assert evidence['candidate_first_at'] is None
    assert evidence['warm_worker_start_at'] is None
