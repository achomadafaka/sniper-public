"""Verified PCS candidates warm native inputs before ingress; no candidate grants authority."""
import json
import os
import threading
import time
import statistics
from pathlib import Path

import pytest

from sniper.services import memecoin_bsc_fast_lane as bsc
from sniper.services import memecoin_bsc_v2_live as tape
from test_memecoin_bsc_fast_lane_live import SUPPLY, _payload, _signal, live56


@pytest.fixture(autouse=True)
def enable_presignal_warming(monkeypatch):
    monkeypatch.setenv("MEMECOIN_BSC_PRESIGNAL_WARM_ENABLED", "1")


def candidate(mint=None, quote=bsc.WBNB, observed_at=None):
    mint = mint or _payload()["mint"]
    return {"message_kind": "candidate_v1", "chain": "56", "mint": mint, "quote": quote,
            "pool": bsc.our_pair(mint, quote),
            "observed_at": time.time() if observed_at is None else observed_at}


def clear(live, mint):
    for name in ("_verdicts", "_facts", "_facts_at", "_stats"):
        getattr(live.cache, name).pop(mint, None)


def test_verified_candidate_before_ingress_prepares_first_buy_without_preparation_reads(live56):
    lane, _, net, sender, _ = live56
    live, payload = lane["live"], candidate()
    clear(live, payload["mint"])
    live.cache.facts_fn = lambda m: (18, SUPPLY)
    live.cache.safety_fn = lambda m, usd=None: (True, "native prepared")
    lane["dispatch"].candidate_identity.pid = os.getpid()
    before = time.time()
    assert lane["dispatch"].on_datagram(payload, os.getpid(), os.getuid(), before) is None
    assert lane["dispatch"].counts["candidates"] == 1
    assert live.cache.next_due(before) == payload["mint"]
    assert live.cache.run_one(payload["mint"], now=before)[0]
    counters = bsc.lane_counters(live)
    assert counters["candidates_ready"] == 1 and counters["candidates_queued"] == 0
    live.inline_safety_fn = lambda *a, **k: pytest.fail("warm first buy repeated safety")
    live.inline_facts_fn = lambda *a, **k: pytest.fail("warm first buy repeated facts")
    live.stats_fn = lambda *a, **k: pytest.fail("warm first buy repeated stats")
    from sniper.util.receipt_clock import ReceiptClock
    stamp = ReceiptClock("test").capture()
    rec = lane["dispatch"].on_datagram(_payload(receipt_clock=dict(stamp)), os.getpid(),
                                       os.getuid(), time.time())
    assert sender.raws and rec["safety_path"] == "cache", rec
    assert not [c for c in net.calls if not c[1]]
    evidence = rec["execution_evidence"]["safety_warmup"]
    assert evidence["ready_at_ingress"] is True and evidence["candidate_first_at"] <= before
    assert evidence["ready_at_original_ingress"] is True


@pytest.mark.parametrize("change", [{"chain": "1"}, {"pool": "0x" + "aa" * 20},
    {"quote": "bad"}, {"mint": bsc.WBNB}, {"observed_at": float("nan")},
    {"observed_at": True}, {"observed_at": time.time() - 601}])
def test_candidate_validation_rejects_wrong_identity_and_expiry(live56, change):
    lane, _, _, _, _ = live56
    lane["dispatch"].candidate_identity.pid = os.getpid()
    assert lane["dispatch"].on_datagram(dict(candidate(), **change), os.getpid(), os.getuid(),
                                         time.time()) is None
    assert lane["dispatch"].counts["candidates"] == 0


def test_candidate_pid_uid_never_broaden_buy_auth(live56):
    lane, _, _, sender, _ = live56
    dispatch = lane["dispatch"]
    dispatch.candidate_identity.pid = os.getpid() + 1
    for pid, uid in ((os.getpid(), os.getuid()), (os.getpid() + 1, os.getuid() + 1)):
        assert dispatch.on_datagram(candidate(), pid, uid, time.time()) is None
    assert dispatch.counts["candidates"] == 0
    assert dispatch.on_datagram(_payload(), os.getpid() + 1, os.getuid(), time.time()) is None
    assert not sender.raws and dispatch.drops["not_the_watcher"] == 1


def test_disabled_presignal_warming_drops_candidates_but_keeps_buy_preparation(live56):
    lane, _, _, sender, _ = live56
    live, dispatch = lane["live"], lane["dispatch"]
    dispatch.presignal_warm_enabled = live.presignal_warm_enabled = False
    dispatch.candidate_identity.pid = os.getpid()
    assert dispatch.on_datagram(candidate(), os.getpid(), os.getuid(), time.time()) is None
    assert dispatch.counts["candidates"] == 0 and live.cache.size() == 0
    rec = _signal(lane)
    assert sender.raws and live.cache.size() == 1, rec
    assert bsc.lane_counters(live)["presignal_warm_enabled"] == 0


def test_presignal_warming_defaults_off_and_publication_respects_it(monkeypatch):
    monkeypatch.delenv("MEMECOIN_BSC_PRESIGNAL_WARM_ENABLED")
    assert bsc.presignal_warm_enabled() is False
    monkeypatch.setattr(tape, "created_native_candidates", lambda log, at: [candidate(observed_at=at)])
    assert tape.publish_native_candidates({}, time.time(), send_fn=lambda *a: pytest.fail("disabled sent")) == 0


def test_candidate_identity_requires_pid_uid_and_pinned_manifest(tmp_path):
    root, proc = tmp_path / "state", tmp_path / "proc"
    release = Path(bsc.__file__).resolve().parents[2]
    (root / "data").mkdir(parents=True)
    (root / "data/memecoin_bsc_v2_live.pid").write_text("123")
    (root / "data/memecoin_deploy.json").write_text(json.dumps(
        {"release_path": str(release), "state_root": str(root), "sha": "a" * 40}))
    (proc / "123").mkdir(parents=True)
    (proc / "123/cmdline").write_bytes(
        b"python3\0-u\0" + str(release / "sniper/services/memecoin_bsc_v2_live.py").encode() + b"\0")
    identity = bsc.CandidateIdentity(root, proc_root=proc)
    assert identity.refresh() == 123
    (proc / "123/cmdline").write_bytes(b"python3\0-c\0print('worker path')\0")
    assert identity.refresh() is None
    (root / "data/memecoin_deploy.json").write_text("{}")
    assert identity.refresh() is None


def test_candidate_bound_capacity_expiry_and_fairness(live56):
    lane, _, _, _, _ = live56
    cache = lane["live"].cache
    now = time.time()
    for i in range(35):
        cache.note_candidates([dict(candidate("0x%040x" % (i + 1), observed_at=now), priority=99)], now=now)
    assert cache.size() == 32 and cache.counts["candidate_evicted"] == 3
    assert cache.next_due(now + 601) is None and cache.size() == 0
    assert cache.counts["candidate_expired"] == 32


def test_quote_change_cancels_stale_inflight_publication(live56):
    lane, _, _, _, _ = live56
    live, cache = lane["live"], lane["live"].cache
    mint = candidate()["mint"]
    clear(live, mint)
    cache.note_candidates([candidate(mint)], now=time.time())
    started, release = threading.Event(), threading.Event()
    cache.facts_fn = lambda m: (18, SUPPLY)
    def safety(m, usd=None):
        started.set()
        assert release.wait(2)
        return True, None
    cache.safety_fn = safety
    worker = threading.Thread(target=lambda: cache.run_one(mint, now=time.time()))
    worker.start()
    try:
        assert started.wait(1)
        cache.note_candidates([candidate(mint, "0x" + "12" * 20)], now=time.time())
        release.set()
        worker.join(2)
        assert cache.reason(mint, now=time.time()) == "no_safety_verdict"
        assert cache.facts(mint) is None and cache.stats(mint, now=time.time()) is None
        assert cache.counts["preparation_cancelled"] == 1
    finally:
        release.set()
        worker.join(2)


def test_generation_change_cancels_inflight_preparation(live56):
    lane, _, _, _, _ = live56
    live, cache = lane["live"], lane["live"].cache
    mint, generation = candidate()["mint"], [0.0]
    clear(live, mint)
    cache.generation_fn = lambda: generation[0]
    cache.note_candidates([candidate(mint)], now=time.time())
    cache.facts_fn = lambda m: (18, SUPPLY)
    cache.safety_fn = lambda m, usd=None: (generation.__setitem__(0, 1.0) or (True, None))
    cache.run_one(mint, now=time.time())
    assert cache.reason(mint, now=time.time()) == "no_safety_verdict"
    assert cache.counts["preparation_cancelled"] == 1


def test_paused_and_essential_refresh_late_skip_preparation_without_calls(live56):
    lane, _, _, _, _ = live56
    live, cache = lane["live"], lane["live"].cache
    mint = candidate()["mint"]
    clear(live, mint)
    cache.note_candidates([candidate(mint)], now=time.time())
    cache.facts_fn = lambda *a: pytest.fail("speculation borrowed essential capacity")
    live.paced.pause()
    assert cache.run_one(mint, now=time.time()) is None
    live.paced._paused_until = 0
    live.snapshot._last["read_at"] = time.time() - 10
    assert cache.run_one(mint, now=time.time()) is None
    assert cache.counts["preparation_skipped"] == 2


def _batched(one):
    """A fake base answering one JSON-RPC request; a batch (the lane facts since cut 4) gets one
    answer per row, each with its row's id, as a JSON-RPC host replies."""
    def base(method, url, **kw):
        request = json.loads(kw["body"])
        if not isinstance(request, list):
            return one(request)
        return 200, [dict(one(row)[1], jsonrpc="2.0", id=row["id"]) for row in request], {}
    return base


def real_preparation(live56, base):
    live = live56[0]["live"]
    mint = candidate()["mint"]
    clear(live, mint)
    live.cache.note_candidates([candidate(mint)], now=time.time())
    live.paced.base, live.paced.min_gap_s = base, 0.0
    return live, mint


@pytest.mark.parametrize("pressure", ["entry", "snapshot"])
def test_actual_token_safety_midjob_deferral_is_a_miss_not_unsafe(live56, pressure):
    calls = []
    def one(request):
        calls.append(request["method"])
        data = request["params"][0]["data"]
        if data == "0x18160ddd":
            if pressure == "entry":
                live.entry_active = True
            else:
                live.snapshot.refreshing.set()
        return 200, {"result": hex(18 if data == "0x313ce567" else SUPPLY)}, {}
    live, mint = real_preparation(live56, _batched(one))
    if pressure == "snapshot":
        elapsed = [10.0]
        live.paced.clock = lambda: elapsed[0]
        live.paced.sleep = lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)
    try:
        assert live.cache.run_one(mint, now=time.time()) is None
        assert calls == ["eth_call", "eth_call"]
        assert live.cache.reason(mint, now=time.time()) == "no_safety_verdict"
        assert live.cache.facts(mint) is None and live.cache.stats(mint, now=time.time()) is None
    finally:
        live.entry_active = False
        live.snapshot.refreshing.clear()
    live.inline_safety_fn = lambda m, usd=None: (True, None)
    live.inline_facts_fn = lambda m: (18, SUPPLY)
    rec = _signal(live56[0])
    assert live56[3].raws and rec["safety_path"] == "inline", rec


def actual_code_safety(live):
    from sniper.execution.evm_token_safety import token_safety_ok_evm
    from sniper.execution.evm_wallet import eth_get_code
    def for_quote(mint, quote, *, request_http=None):
        def checked(coin, usd=None):
            return token_safety_ok_evm(coin, code_fn=lambda m: eth_get_code(
                m, live.snapshot.host.url, http=request_http), sell_route_fn=lambda m: False,
                sell_sim_fn=lambda m: True)
        return checked
    def checked(mint, usd=None):
        return for_quote(mint, bsc.WBNB)(mint, usd=usd)
    checked.for_quote = for_quote
    return checked


def test_actual_token_safety_genuine_unsafe_is_still_cached(live56):
    requests = []
    def one(request):
        requests.append(request)
        if request["method"] == "eth_chainId":
            return 200, {"result": "0x38"}, {}
        if request["method"] == "eth_getCode":
            return 200, {"result": "0x"}, {}
        data = request["params"][0]["data"]
        return 200, {"result": hex(18 if data == "0x313ce567" else SUPPLY)}, {}
    live, mint = real_preparation(live56, _batched(one))
    # Actual bytecode RPC and safety predicate; no independent route probe is needed for no code.
    live.cache.safety_fn = actual_code_safety(live)
    result = live.cache.run_one(mint, now=time.time())
    assert result is not None, (requests, dict(live.cache.counts))
    assert result[0] is False
    assert live.cache.reason(mint, now=time.time()) == "unsafe:not a contract (no code at address)"
    live.inline_safety_fn = lambda *a, **kw: pytest.fail("genuine unsafe verdict retried inline")
    rec = _signal(live56[0])
    assert rec["reason"].startswith("unsafe:") and not live56[3].raws


@pytest.mark.parametrize("failure", ["429", "transport", "timeout"])
def test_actual_token_safety_read_failure_is_unavailable(live56, failure):
    def base(method, url, **kw):
        request = json.loads(kw["body"])
        if request["method"] == "eth_getCode":
            if failure == "timeout":
                raise TimeoutError("offline provider timeout")
            if failure == "transport":
                raise OSError("offline transport failure")
            return 429, {"error": {"code": -32000, "message": "rate limited"}}, {}
        data = request["params"][0]["data"]
        return 200, {"result": hex(18 if data == "0x313ce567" else SUPPLY)}, {}
    live, mint = real_preparation(live56, base)
    live.cache.safety_fn = actual_code_safety(live)
    assert live.cache.run_one(mint, now=time.time()) is None
    assert live.cache.reason(mint, now=time.time()) == "no_safety_verdict"
    assert live.cache.facts(mint) is None


@pytest.mark.parametrize("answer", [
    {"error": {"code": -32005, "message": "rate limit exceeded"}},
    {"error": {"code": -32603, "message": "internal error"}},
    {"result": None}, {}, None, [],
])
def test_actual_token_safety_incomplete_rpc_reply_is_unavailable(live56, answer):
    live, mint = real_preparation(live56, lambda *a, **kw: (200, answer, {}))
    live.cache.facts_fn = lambda m: (18, SUPPLY)
    live.cache.safety_fn = actual_code_safety(live)
    assert live.cache.run_one(mint, now=time.time()) is None
    assert live.cache.reason(mint, now=time.time()) == "no_safety_verdict"
    assert live.cache.facts(mint) is None and live.cache.stats(mint, now=time.time()) is None
    assert live.cache.counts["preparation_cancelled"] == 1
    assert live.paced.counts["calls"] == 1


@pytest.mark.parametrize("error", [
    {"code": 3, "message": "execution reverted", "data": "0x"},
    {"code": -32000, "message": "execution reverted: transfer blocked", "data": "0x"},
])
def test_actual_token_safety_contract_revert_stays_unsafe(live56, error):
    from sniper.execution.evm_token_safety import token_safety_ok_evm
    from sniper.execution.evm_wallet import eth_get_code
    from sniper.execution.evm_real_seams import _rpc_eth_call_result
    def base(method, url, **kw):
        request = json.loads(kw["body"])
        return (200, {"result": "0x6001"}, {}) if request["method"] == "eth_getCode" \
            else (200, {"error": error}, {})
    live, mint = real_preparation(live56, base)
    live.cache.facts_fn = lambda m: (18, SUPPLY)
    def for_quote(mint, quote, *, request_http=None):
        def checked(coin, usd=None):
            return token_safety_ok_evm(coin, code_fn=lambda m: eth_get_code(
                m, live.snapshot.host.url, http=request_http), sell_route_fn=lambda m: True,
                sell_sim_fn=lambda m: _rpc_eth_call_result(
                    {"to": bsc.PCS_V2_ROUTER, "data": "0x"}, live.snapshot.address,
                    live.snapshot.host.url, http=request_http)[0])
        return checked
    def checked(mint, usd=None):
        return for_quote(mint, bsc.WBNB)(mint, usd=usd)
    checked.for_quote = for_quote
    live.cache.safety_fn = checked
    assert live.cache.run_one(mint, now=time.time())[0] is False
    assert live.cache.reason(mint, now=time.time()).startswith("unsafe:")
    assert live.cache.counts["preparation_cancelled"] == 0


def test_existing_worker_yields_account_locks_between_paced_reads(live56, monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_BSC_FAST_LANE_SNAPSHOT_PERIOD_S", 9.0)
    now, calls, waits = [10.0], [], []
    def one(request):
        if request["method"] == "eth_getCode":
            return 200, {"result": "0x"}, {}
        data = request["params"][0]["data"]
        return 200, {"result": hex(18 if data == "0x313ce567" else SUPPLY)}, {}
    batched = _batched(one)
    def base(method, url, **kw):
        calls.append(now[0])                          # one entry per paced request
        return batched(method, url, **kw)
    live, mint = real_preparation(live56, base)
    live.cache.safety_fn = actual_code_safety(live)
    def sleep(seconds):
        for lock in (live.paced._background_lock, live.paced._lock):
            assert lock.acquire(blocking=False)
            lock.release()
        waits.append(seconds)
        now[0] += max(seconds, 1e-6)
        # Age the real admission snapshot along with the synthetic account clock.
        live.snapshot._last["read_at"] -= max(seconds, 1e-6)
    live.paced.clock, live.paced.sleep = lambda: now[0], sleep
    live.paced.min_gap_s = 1.1
    assert live.cache.run_one(mint, now=time.time())[0] is False
    # code, then the facts batch (decimals + totalSupply in one request since cut 4)
    assert calls == pytest.approx([10.0, 11.1], abs=1e-5)
    assert waits and max(waits) <= 0.05 and live.paced.counts["calls"] == 2
    # the admission snapshot aged with every simulated wait, and stayed inside its 9 s period
    assert sum(waits) - 1e-3 <= time.time() - live.snapshot.current()["read_at"] < 9.0


@pytest.mark.parametrize("status, eligible", [("filled", True), ("failed", None), ("aborted", None)])
def test_native_observer_eligibility_copies_canonical_hook_status(live56, status, eligible):
    from types import SimpleNamespace
    live, outcomes = live56[0]["live"], []
    live.ctx["bsc_execution_observer"] = SimpleNamespace(
        record_signal=lambda signal: None, record_outcome=outcomes.append)
    # A failure with a misleading reason/hash must remain unknown, never become eligible.
    answer = {"status": status, "reason": "filled", "tx_hash": "0x" + "ab" * 32,
              "pending_id": 1, "position_id": 2}
    live.hook_factory = lambda *a: SimpleNamespace(attempt=lambda *args: answer)
    result = live.handle(_payload(), recv_epoch=time.time())
    assert outcomes[0]["eligibility"] is eligible
    assert result["status"] == status


def test_cancelled_preparation_skips_new_market_stats_work(live56):
    stats_calls = []
    def base(method, url, **kw):
        request = json.loads(kw["body"])
        data = request["params"][0]["data"]
        if data == "0x18160ddd":
            live.entry_active = True
        return 200, {"result": hex(18 if data == "0x313ce567" else SUPPLY)}, {}
    live, mint = real_preparation(live56, base)
    live.cache.stats_fn = lambda m: stats_calls.append(m) or {"fdv": 20000}
    try:
        assert live.cache.run_one(mint, now=time.time()) is None
        assert not stats_calls
    finally:
        live.entry_active = False


@pytest.mark.parametrize("pressure", ["entry", "snapshot"])
def test_actual_pace_wait_rechecks_pressure_before_physical_request(live56, pressure):
    calls, free_during_wait = [], []
    live, mint = real_preparation(live56, lambda *a, **kw: calls.append(1) or
                                  (200, {"result": hex(18)}, {}))
    elapsed = [10.0]
    live.paced.clock = lambda: elapsed[0]
    def sleep(seconds):
        # A paced worker's own wait must never own the account's request lock.
        free = live.paced._background_lock.acquire(blocking=False)
        free_during_wait.append(free)
        if free:
            live.paced._background_lock.release()
        elapsed[0] += seconds
        if pressure == "entry":
            live.entry_active = True
        else:
            live.snapshot.refreshing.set()
    live.paced.sleep, live.paced.min_gap_s = sleep, 1.1
    last = live.paced._last = live.paced.clock()
    try:
        result = live.cache.run_one(mint, now=time.time())
        assert calls == [] and live.paced.counts["calls"] == 0
        assert result is None and free_during_wait and all(free_during_wait)
        if pressure == "entry":
            assert free_during_wait == [True]
        else:
            assert elapsed[0] - last >= 60.0
        assert live.paced._last == last
        assert live.cache.reason(mint, now=time.time()) == "no_safety_verdict"
    finally:
        live.entry_active = False
        live.snapshot.refreshing.clear()


def test_candidate_skips_busy_account_lock_without_waiting(live56):
    calls = []
    live, mint = real_preparation(live56, lambda *a, **kw: calls.append(1) or
                                  (200, {"result": hex(18)}, {}))
    finished = threading.Event()
    def run():
        try:
            live.cache.run_one(mint, now=time.time())
        finally:
            finished.set()
    live.paced._background_lock.acquire()
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert finished.wait(0.2), "candidate blocked on essential account owner"
        assert not calls
    finally:
        live.paced._background_lock.release()
        worker.join(2)


def test_inline_replacement_does_not_reuse_worker_readiness(live56):
    from sniper.util.receipt_clock import ReceiptClock
    live, cache = live56[0]["live"], live56[0]["live"].cache
    mint = candidate()["mint"]
    clear(live, mint)
    cache.note_candidates([candidate(mint)], now=time.time())
    cache.facts_fn = lambda m: (18, SUPPLY)
    cache.safety_fn = lambda m, usd=None: (True, None)
    assert cache.run_one(mint, now=time.time())[0]
    original = ReceiptClock("test").capture()
    cache.put(mint, now=time.time(), facts=(18, SUPPLY), stats={"fdv": 20000})
    rec = live.handle(_payload(receipt_clock=dict(original)), recv_epoch=time.time())
    assert rec["execution_evidence"]["safety_warmup"]["ready_at_original_ingress"] is False


def test_inline_replacement_cancels_prior_inflight_worker(live56):
    live, cache = live56[0]["live"], live56[0]["live"].cache
    mint = candidate()["mint"]
    clear(live, mint)
    cache.note_candidates([candidate(mint)], now=time.time())
    started, release = threading.Event(), threading.Event()
    cache.facts_fn = lambda m: (18, SUPPLY)
    def safety(m, usd=None):
        started.set()
        assert release.wait(2)
        return True, "older worker"
    cache.safety_fn = safety
    worker = threading.Thread(target=lambda: cache.run_one(mint, now=time.time()))
    worker.start()
    try:
        assert started.wait(1)
        cache.put(mint, now=time.time(), facts=(18, SUPPLY * 2), reason="inline replacement")
        release.set()
        worker.join(2)
        assert cache._verdicts[mint][1] == "inline replacement"
        assert cache.facts(mint) == (18, SUPPLY * 2)
    finally:
        release.set()
        worker.join(2)


def test_optional_physical_grant_rechecks_admission_and_keeps_account_pace():
    from sniper.services.memecoin_rh_fast_lane import PacedHttp, ResearchPaused
    now, sends, sleeps, checks = [10.0], [], [], []
    def sleep(seconds):
        assert paced._background_lock.acquire(blocking=False)
        paced._background_lock.release()
        sleeps.append(seconds)
        now[0] += max(seconds, 1e-6)
    paced = PacedHttp(lambda *a, **kw: sends.append(now[0]) or (200, {}, {}), min_gap_s=1.1,
                      clock=lambda: now[0], sleep=sleep)
    paced._last = now[0]
    with pytest.raises(ResearchPaused):
        paced.try_background("POST", "u", admission_fn=lambda: True)
    assert not sends and not sleeps and paced.counts["calls"] == 0
    assert paced.try_background("POST", "u", admission_fn=lambda: True, wait_for_pace=True)[0] == 200
    assert sends == [pytest.approx(11.1)] and paced.counts["calls"] == 1
    now[0] += 1.1
    def admission():
        checks.append(True)
        return len(checks) == 1
    with pytest.raises(ResearchPaused):
        paced.try_background("POST", "u", admission_fn=admission)
    assert len(sends) == 1 and paced.counts["calls"] == 1


def test_created_pairs_publish_canonical_candidates_without_reads():
    mint, quote = "0x" + "12" * 20, bsc.WBNB
    a, b = sorted((mint, quote), key=lambda t: int(t, 16))
    pool = bsc.our_pair(a, b)
    log = {"address": tape.PCS_V2_FACTORY, "topics": [tape.PAIR_CREATED,
        "0x" + a[2:].rjust(64, "0"), "0x" + b[2:].rjust(64, "0")],
        "data": "0x" + pool[2:].rjust(64, "0") + format(1, "064x")}
    assert tape.created_native_candidates(log, 123.0) == [candidate(mint, quote, 123.0)]
    assert tape.created_native_candidates(dict(log, removed=True), 123.0) == []


def test_native_payload_and_event_preserve_original_receipt_clock():
    from sniper.util.receipt_clock import ReceiptClock
    from test_memecoin_bsc_fast_lane import ROW, _decide, _synthetic
    clock = ReceiptClock("test").capture()
    receipt, mint, post = _synthetic()
    stage = {"name": "decode_done", "monotonic_ns": clock["monotonic_ns"],
             "boot_id": clock["boot_id"], "receipt_elapsed_ms": 0.0}
    row = dict(ROW, buy=dict(ROW["buy"], mint=mint), receipt_clock=clock,
               receipt_stages=[stage, {"name": "malformed"}])
    payload = bsc.lane_payload(row, _decide(receipt, mint, post), decimals=18, bnb_usd=600,
                               bnb_usd_at=time.time(), sent_epoch=time.time())
    assert payload["receipt_clock"] == clock
    assert bsc.BscLaneEvent(payload, market_cap=10000).receipt_clock == clock
    assert payload["receipt_stages"] == [stage]
    event = bsc.BscLaneEvent(payload, market_cap=10000)
    stage["name"] = "mutated"
    assert event.receipt_stages[0]["name"] == "decode_done"


def test_readiness_uses_original_ingress_not_later_dispatch(live56):
    from sniper.util.receipt_clock import ReceiptClock
    lane, _, _, _, _ = live56
    live, cache = lane["live"], lane["live"].cache
    original = ReceiptClock("test").capture()
    mint = candidate()["mint"]
    clear(live, mint)
    cache.note_candidates([candidate(mint)], now=time.time())
    cache.facts_fn = lambda m: (18, SUPPLY)
    cache.safety_fn = lambda m, usd=None: (True, None)
    cache.run_one(mint, now=time.time())
    rec = live.handle(_payload(receipt_clock=dict(original)), recv_epoch=time.time())
    assert rec["execution_evidence"]["safety_warmup"]["ready_at_original_ingress"] is False


def test_job_request_cost_and_candidate_observation_are_not_restamped(live56):
    lane, _, _, _, _ = live56
    live, cache = lane["live"], lane["live"].cache
    mint, observed = candidate()["mint"], time.time() - 2
    clear(live, mint)
    cache.note_candidates([candidate(mint, observed_at=observed)], now=time.time())
    cache.facts_fn = lambda m: (18, SUPPLY)
    cache.safety_fn = lambda m, usd=None: (True, None)
    cache.run_one(mint, now=time.time())
    evidence = {}
    assert cache.reason(mint, now=time.time(), evidence=evidence) is None
    assert evidence["candidate_first_at"] == observed
    assert evidence["warm_request_attempts"] == 0


def test_cross_boot_original_readiness_is_unknown(live56):
    from sniper.util.receipt_clock import ReceiptClock
    lane, _, _, _, _ = live56
    stamp = dict(ReceiptClock("test").capture(), boot_id="different boot")
    rec = lane["live"].handle(_payload(receipt_clock=stamp), recv_epoch=time.time())
    assert rec["execution_evidence"]["safety_warmup"]["ready_at_original_ingress"] is None


def test_delayed_speculation_has_one_slot_and_eviction_cancels_without_live_reads(live56):
    lane, _, _, sender, _ = live56
    live, cache = lane["live"], lane["live"].cache
    started, release = threading.Event(), threading.Event()
    speculative_mint = "0x" + "01" * 20
    cache.note_candidates([candidate(speculative_mint)], now=time.time())
    def facts(m):
        started.set()
        assert release.wait(2)
        return 18, SUPPLY
    cache.facts_fn = facts
    cache.safety_fn = lambda m, usd=None: (True, None)
    worker = threading.Thread(target=lambda: cache.run_one(speculative_mint, now=time.time()))
    worker.start()
    try:
        assert started.wait(1)
        assert cache.run_one(speculative_mint, now=time.time()) is None
        rec = _signal(lane)
        assert sender.raws and rec["safety_path"] == "cache"
        for i in range(32):
            cache.note_candidates([candidate("0x%040x" % (i + 1))], now=time.time())
        assert cache.size() == 32 and cache.counts["candidate_evicted"] >= 1
        release.set()
        worker.join(2)
        assert cache.reason(speculative_mint, now=time.time()) == "no_safety_verdict"
        assert cache.counts["preparation_cancelled"] == 1
    finally:
        release.set()
        worker.join(2)


@pytest.mark.parametrize("case", ["cold", "expired", "wrong_quote", "generation", "unsafe"])
def test_cold_or_invalid_preparation_retains_current_inline_or_refusal(live56, case):
    lane, _, _, sender, _ = live56
    live, cache = lane["live"], lane["live"].cache
    mint, reads = candidate()["mint"], []
    clear(live, mint)
    if case != "cold":
        cache.note_candidates([candidate(mint)], now=time.time())
        cache.facts_fn = lambda m: (18, SUPPLY)
        cache.safety_fn = lambda m, usd=None: (case != "unsafe", "honeypot" if case == "unsafe" else None)
        cache.run_one(mint, now=time.time())
    if case == "expired":
        cache._priority[mint] = (99, time.time() - 601)
    elif case == "wrong_quote":
        cache.note_candidates([candidate(mint, "0x" + "12" * 20)], now=time.time())
    elif case == "generation":
        live.generation_fn = lambda: 1.0
    live.inline_safety_fn = lambda m, usd=None: (reads.append("safety") or (True, None))
    live.inline_facts_fn = lambda m: (18, SUPPLY)
    rec = _signal(lane)
    if case == "unsafe":
        assert rec["reason"] == "unsafe:honeypot" and not sender.raws and not reads
    else:
        assert sender.raws and reads == ["safety"] and rec["safety_path"] == "inline", rec


def test_bounded_native_cache_lookup_latency(live56):
    lane, _, _, _, _ = live56
    live, cache = lane["live"], lane["live"].cache
    payload, samples = _payload(), []
    now = time.time()
    for i in range(31):
        cache.note_candidates([candidate("0x%040x" % (i + 1))], now=now)
    entry = {"mint": payload["mint"], "quote": payload["quote"], "pool": payload["pool"], "priority": 0}
    for _ in range(1000):
        before = time.perf_counter_ns()
        cache.note_candidates([entry], now=now, generation=0.0)
        assert cache.reason(payload["mint"], now=now, evidence={}) is None
        samples.append((time.perf_counter_ns() - before) / 1000)
    print("BSC candidate note+complete cache lookup microseconds (32 candidates, 1000 samples):",
          {"p50": round(statistics.median(samples), 3), "p90": round(sorted(samples)[899], 3)})
