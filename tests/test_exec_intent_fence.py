"""EVM-08: a write-ahead intent fence across processes (spec 2026-10-07, revision 2).

Every EVM broadcast post writes one intent record before its network I/O and removes only its own
record once the chain's generation stamp succeeded. The fast lanes refuse while any record exists on
the chain. A record left by a failed stamp or a crash is resolved only on chain proof (the nonce was
consumed, or a bundle-only identity is past its own maxBlockNumber), by the runner's sweep.
"""
import json
import logging
import os
import threading
import time

import pytest
from eth_account import Account

from sniper.execution import exec_lock
from sniper.services import memecoin_rh_fast_lane as lane
from sniper.settings import memecoin as cfg

KEY = b"\x02" * 32
SENDER = Account.from_key(KEY).address


def _raw(chain_id, nonce=3, *, to_byte=b"\x22"):
    tx = {"type": 2, "chainId": chain_id, "nonce": nonce, "maxFeePerGas": 10,
          "maxPriorityFeePerGas": 0, "gas": 21000, "to": "0x" + (to_byte * 20).hex(), "value": 0,
          "data": b""}
    return "0x" + bytes(Account.sign_transaction(tx, KEY).raw_transaction).hex()


@pytest.fixture
def fence(monkeypatch, tmp_path):
    monkeypatch.setattr(exec_lock, "_GENERATION_FAULT", threading.Event())
    monkeypatch.setattr(exec_lock, "_GENERATION_FAULT_REASON", [None])
    monkeypatch.setattr(exec_lock, "_default_lock_dir", lambda: str(tmp_path))
    monkeypatch.setattr(cfg, "MEMECOIN_EXEC_INTENT_STAMP", True)
    return tmp_path


def _records(root, chain):
    d = os.path.join(str(root), "exec_intents", str(chain))
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def _gen(root, chain):
    return exec_lock.exec_generation(exec_lock.chain_generation_key(chain), lock_dir=str(root))


def _reason(generation, read_at):
    payload = {"snapshot": {"read_at": read_at, "native_wei": str(10 ** 18), "gas_price_wei": "1"},
               "eth_usd": 2500.0, "eth_usd_at": read_at}
    return lane.inputs_reason(payload, generation=generation, now_host=read_at + 1.0,
                              clip_wei=10 ** 15, gas_limit=21000, low_wei=0,
                              max_snapshot_age_s=30.0, eth_usd_max_age_s=120.0,
                              generation_margin_s=1.0)


def _lane_reason(root, chain):
    generation = lane.lane_generation_fn("evm-%s-0xabc" % chain, chain, lock_dir=str(root))()
    return _reason(generation, time.time() + 5.0)


# ------------------------------------------------------------------------------------------------
# Writer and reader
# ------------------------------------------------------------------------------------------------

def test_concurrent_intents_stay_fenced_until_the_last_one_completes(fence):
    """Codex defect (a): A at 10, B at 11, A completes at 12. One mtime admitted B's window."""
    raw_a, raw_b = _raw(4663, 3), _raw(4663, 4)
    a = exec_lock.intent_send(raw_a, "rpc")
    b = exec_lock.intent_send(raw_b, "rpc")
    assert len(_records(fence, 4663)) == 2
    exec_lock.stamp_send(raw_a, intent=a)
    assert len(_records(fence, 4663)) == 1
    assert _lane_reason(fence, "4663") == "generation_inflight"
    exec_lock.stamp_send(raw_b, intent=b)
    assert _records(fence, 4663) == []
    assert _lane_reason(fence, "4663") is None                # a snapshot read after .gen + margin
    assert not exec_lock.generation_fault()


def test_a_snapshot_read_before_the_completion_stamp_still_predates_it(fence):
    raw = _raw(4663)
    exec_lock.stamp_send(raw, intent=exec_lock.intent_send(raw, "rpc"))
    generation = lane.lane_generation_fn("evm-4663-0xabc", "4663", lock_dir=str(fence))()
    assert _reason(generation, generation - 0.5) == "snapshot_predates_execution"


def test_another_chains_intent_does_not_fence_this_chain(fence):
    exec_lock.intent_send(_raw(56), "rpc")
    assert _lane_reason(fence, "4663") is None
    assert _lane_reason(fence, "56") == "generation_inflight"


def test_the_reader_scans_intents_before_it_reads_the_stamps(fence, monkeypatch):
    """A completion stamps .gen, then unlinks. Read in the other order, a completion landing between
    the two reads leaves an old .gen and an empty directory: a stale snapshot would pass."""
    order = []
    real_scan, real_gen = exec_lock.intents_outstanding, exec_lock.exec_generation
    monkeypatch.setattr(exec_lock, "intents_outstanding",
                        lambda *a, **k: order.append("intents") or real_scan(*a, **k))
    monkeypatch.setattr(exec_lock, "exec_generation",
                        lambda *a, **k: order.append("gen") or real_gen(*a, **k))
    lane.lane_generation_fn("evm-4663-0xabc", "4663", lock_dir=str(fence))()
    assert order[0] == "intents" and "gen" in order


def test_an_unreadable_intent_directory_is_unknown_and_latches(fence, monkeypatch):
    exec_lock.intent_send(_raw(4663), "rpc")
    real = os.scandir

    def denied(path=".", *a, **k):
        if "exec_intents" in str(path):
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, *a, **k)
    monkeypatch.setattr(exec_lock.os, "scandir", denied)
    assert _lane_reason(fence, "4663") == "generation_unknown"
    assert exec_lock.generation_fault() and "intent" in exec_lock.generation_fault_reason()


def test_a_missing_intent_directory_is_no_intents(fence):
    assert _lane_reason(fence, "4663") is None
    assert not exec_lock.generation_fault()


def test_a_failed_completion_stamp_keeps_the_record(fence, monkeypatch):
    raw = _raw(4663)
    token = exec_lock.intent_send(raw, "rpc")

    def eio(*a, **k):
        raise OSError(5, "Input/output error")
    monkeypatch.setattr(exec_lock, "touch_generation", eio)
    exec_lock.stamp_send(raw, intent=token)
    assert len(_records(fence, 4663)) == 1 and exec_lock.generation_fault()


# ------------------------------------------------------------------------------------------------
# Several processes
# ------------------------------------------------------------------------------------------------

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _child(root, body):
    """Run `body` in a separate Python process (not a fork of this threaded one: a fork can inherit
    a held lock and hang, Gamma 2026-10-07). It sees `exec_lock`, `os` and the lock dir `d`."""
    import subprocess
    import sys
    code = ("import os\nfrom sniper.execution import exec_lock\nd = %r\n" % str(root)) + body
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True,
                         timeout=120, env=dict(os.environ, MEMECOIN_EXEC_INTENT_STAMP="1"))
    assert out.returncode == 0, out.stderr[-3000:]
    return out.stdout.strip()


def test_two_processes_each_hold_their_own_record(fence):
    raw_child, raw_parent = _raw(4663, 5), _raw(4663, 6)
    child_path = _child(fence, "print(exec_lock.intent_send(%r, 'rpc', lock_dir=d)[0])\n" % raw_child)
    exec_lock.stamp_send(raw_parent, intent=exec_lock.intent_send(raw_parent, "rpc"))
    assert _lane_reason(fence, "4663") == "generation_inflight"   # the child's post is still open
    _child(fence, "exec_lock.stamp_send(%r, lock_dir=d, intent=(%r, 'rpc', 0.0))\n"
           % (raw_child, child_path))
    assert _records(fence, 4663) == [] and _lane_reason(fence, "4663") is None


def test_codex_two_process_probe_the_reader_refuses_until_the_sweep_resolves(fence, caplog):
    raw = _raw(4663, 9)
    latched = _child(fence, (
        "exec_lock.intent_send(%r, 'rpc', lock_dir=d)\n"          # then the (faked) send went out
        "def eio(*a, **k):\n    raise OSError(5, 'Input/output error')\n"
        "exec_lock.touch_generation = eio\n"                      # both completion stamps raise EIO
        "exec_lock.stamp_send(%r, lock_dir=d)\n"
        "print(exec_lock.generation_fault())\n") % (raw, raw))
    assert latched == "True"                                       # the writer latched locally
    assert not exec_lock.generation_fault()                        # the reader never saw that latch
    assert _lane_reason(fence, "4663") == "generation_inflight"
    _age(fence, 4663, 20)
    with caplog.at_level(logging.WARNING):
        done = exec_lock.sweep_intents(post=_chain(4663, latest_nonce=10), rpc_url_for=_url)
    assert [(c, basis) for c, _tx, basis in done] == [("4663", "nonce_consumed")]
    assert _records(fence, 4663) == [] and _lane_reason(fence, "4663") is None
    assert any("generation fence resolved chain=4663" in r.getMessage()
               and "basis=nonce_consumed" in r.getMessage() for r in caplog.records)


def test_a_crash_after_the_send_leaves_the_chain_fenced(fence):
    _child(fence, "exec_lock.intent_send(%r, 'rpc', lock_dir=d)\nos._exit(0)\n" % _raw(4663))
    assert len(_records(fence, 4663)) == 1
    assert _lane_reason(fence, "4663") == "generation_inflight"


# ------------------------------------------------------------------------------------------------
# The four send sites
# ------------------------------------------------------------------------------------------------

class _Conn:
    def __init__(self, gate=None, calls=None, entered=None):
        self.gate, self.calls = gate, calls if calls is not None else []
        self.entered = entered

    def request(self, method, path, body=None, headers=None):
        self.calls.append(json.loads(body)["method"])

    def getresponse(self):
        if self.entered is not None:
            self.entered.set()                    # the post is on the wire: its intent exists
        if self.gate is not None:
            self.gate.wait(10)

        class R:
            @staticmethod
            def read():
                return json.dumps({"jsonrpc": "2.0", "id": 1, "result": "0x" + "ab" * 32}).encode()
        return R()

    def close(self):
        pass


def test_detached_member_posts_keep_the_chain_fenced_until_the_last_completes(fence):
    from sniper.services import memecoin_bsc_fast_lane as bsc
    gate, entered = threading.Event(), threading.Event()
    fast = lane.SequencerSender("https://fast.example/", connect=lambda: _Conn())
    slow = lane.SequencerSender("https://slow.example/", connect=lambda: _Conn(gate, entered=entered))
    sender = bsc.BscSender([fast, slow], timeout_s=5.0)
    kind, _detail = sender.send(_raw(56))
    assert kind == "accepted"                              # returned on the first acceptance
    # Gamma: under load the slow member's thread may not have reached its intent yet when the
    # first acceptance returns; wait until its post is on the wire (production: harmless).
    assert entered.wait(10)
    assert _lane_reason(fence, "56") == "generation_inflight"
    gate.set()
    sender._pool.shutdown(wait=True)
    assert _records(fence, 56) == [] and _lane_reason(fence, "56") is None


@pytest.fixture
def unwritable(fence):
    """A file where the intent directory belongs: every create fails (works as root too)."""
    (fence / "exec_intents").write_text("not a directory")
    return fence


def test_intent_unwritable_eth_send_raw_sends_nothing_and_reads_rejected(unwritable):
    from sniper.execution import evm_real_seams as seams
    calls = []

    def http(method, url, headers=None, body=None, timeout=None):
        calls.append(body)
        return 200, {"result": "0x" + "ab" * 32}, None
    assert seams.eth_send_raw(_raw(4663), "https://custody.example/", http=http) is False
    assert calls == []
    assert "intent_unwritable" in (seams._last_send_rejection.message or "")
    assert exec_lock.generation_fault() and exec_lock.generation_fault_reason().startswith("intent:")


def test_intent_unwritable_fast_submit_is_not_sent(unwritable):
    from sniper.execution.evm_fast_submit import EvmRpcProvider
    from sniper.execution.submission_transport import AttemptState
    calls = []

    def http(method, url, **kw):
        calls.append(kw)
        return 200, {"result": "0x" + "ab" * 32}, None

    class Req:
        signed_payload = _raw(4663)
        tx_identity = "0x" + "ab" * 32
    attempt = EvmRpcProvider(http, "https://free.example/", provider_id="free").submit(Req())
    assert attempt.state == AttemptState.NOT_SENT and calls == []


def test_intent_unwritable_sequencer_and_bundle_reject_and_the_fanout_reads_rejected(unwritable):
    from sniper.services import memecoin_bsc_fast_lane as bsc
    conn_calls, posts = [], []
    member = lane.SequencerSender("https://m.example/", connect=lambda: _Conn(calls=conn_calls))
    bundle = bsc.blockrazor_bundle("token", post=lambda *a: posts.append(a) or {"result": "0x1"})
    assert member.send(_raw(56))[0] == "rejected"
    assert bundle(_raw(56), max_block=100)[0] == "rejected"
    kind, detail = bsc.BscSender([member], bundle=bundle).send(_raw(56), max_block=100)
    assert kind == "rejected" and "intent_unwritable" in detail
    assert conn_calls == [] and posts == []


# ------------------------------------------------------------------------------------------------
# The BSC bundle's provider-bound expiry
# ------------------------------------------------------------------------------------------------

def test_the_bundle_carries_its_max_block_and_records_it(fence):
    from sniper.services import memecoin_bsc_fast_lane as bsc
    bodies = []
    gate = threading.Event()

    def post(url, body, headers):
        bodies.append(json.loads(body))
        gate.wait(5)
        return {"result": "0xbundle"}
    bundle = bsc.blockrazor_bundle("token", post=post)
    t = threading.Thread(target=bundle, args=(_raw(56),), kwargs={"max_block": 1020})
    t.start()
    deadline = time.time() + 5
    while not bodies and time.time() < deadline:
        time.sleep(0.01)
    [name] = _records(fence, 56)
    with open(os.path.join(str(fence), "exec_intents", "56", name)) as fh:
        rec = json.loads(fh.read())
    assert rec["transport"] == "bundle" and rec["max_block"] == 1020
    gate.set()
    t.join(5)
    assert bodies[0]["params"][0]["maxBlockNumber"] == 1020
    assert _records(fence, 56) == []


def test_the_bsc_lane_sends_source_block_plus_k(fence, monkeypatch):
    from sniper.services import memecoin_bsc_fast_lane as bsc
    monkeypatch.setattr(cfg, "MEMECOIN_BSC_BUNDLE_MAX_BLOCKS", 20)
    seen = []

    class Sender:
        def send(self, raw, max_block=None):
            seen.append(max_block)
            return "accepted", "0x1"
    live = bsc.BscLive.__new__(bsc.BscLive)
    live.sender = Sender()
    assert live._post("0xraw", {"seq": 1000}) == ("accepted", "0x1")
    assert seen == [1020]


def test_the_4663_lane_post_carries_no_max_block(fence):
    seen = []

    class Sender:
        def send(self, raw):
            seen.append(raw)
            return "accepted", "0x1"
    live = lane.LiveLane.__new__(lane.LiveLane)
    live.sender = Sender()
    assert live._post("0xraw", {"seq": 1000}) == ("accepted", "0x1") and seen == ["0xraw"]


# ------------------------------------------------------------------------------------------------
# The sweep: resolution only on proof
# ------------------------------------------------------------------------------------------------

def _url(chain):
    return "https://custody-%s.example/" % chain


def _chain(chain_id, *, latest_nonce=0, head=0, calls=None, fail=None):
    def post(url, method, params):
        if calls is not None:
            calls.append(method)
        if fail == "timeout":
            raise TimeoutError("custody timed out")
        if fail == "error":
            return {"error": {"code": -32000, "message": "header not found"}}
        if method == "eth_chainId":
            return {"result": hex(999 if fail == "wrong_chain" else chain_id)}
        if method == "eth_getTransactionCount":
            assert params == [SENDER, "latest"]
            return {"result": hex(latest_nonce)}
        if method == "eth_blockNumber":
            return {"result": hex(head)}
        raise AssertionError(method)
    return post


def _age(root, chain, seconds):
    d = os.path.join(str(root), "exec_intents", str(chain))
    then = time.time() - seconds
    for name in os.listdir(d):
        os.utime(os.path.join(d, name), (then, then))


def test_a_consumed_nonce_resolves_and_stamps_before_it_unlinks(fence):
    exec_lock.intent_send(_raw(4663, 3), "rpc")
    _age(fence, 4663, 20)
    before = _gen(fence, 4663)
    done = exec_lock.sweep_intents(post=_chain(4663, latest_nonce=4), rpc_url_for=_url)
    assert [b for _c, _t, b in done] == ["nonce_consumed"]
    assert _records(fence, 4663) == [] and _gen(fence, 4663) > before


def test_an_unconsumed_nonce_stays_fenced(fence):
    exec_lock.intent_send(_raw(4663, 3), "rpc")
    _age(fence, 4663, 20)
    assert exec_lock.sweep_intents(post=_chain(4663, latest_nonce=3), rpc_url_for=_url) == []
    assert len(_records(fence, 4663)) == 1


def test_a_record_younger_than_w_is_not_examined(fence):
    exec_lock.intent_send(_raw(4663, 3), "rpc")
    calls = []
    assert exec_lock.sweep_intents(post=_chain(4663, latest_nonce=9, calls=calls),
                                   rpc_url_for=_url) == []
    assert calls == [] and len(_records(fence, 4663)) == 1


def test_a_bundle_only_identity_past_its_max_block_resolves(fence):
    exec_lock.intent_send(_raw(56, 3), "bundle", max_block=1020)
    _age(fence, 56, 20)
    assert exec_lock.sweep_intents(post=_chain(56, latest_nonce=3, head=1022),
                                   rpc_url_for=_url) == []            # max_block + 2 not passed
    done = exec_lock.sweep_intents(post=_chain(56, latest_nonce=3, head=1023), rpc_url_for=_url)
    assert [b for _c, _t, b in done] == ["bundle_expired"] and _records(fence, 56) == []


def test_a_bundle_with_an_open_public_post_of_the_same_bytes_never_resolves_by_expiry(fence):
    raw = _raw(56, 3)
    exec_lock.intent_send(raw, "bundle", max_block=1020)
    exec_lock.intent_send(raw, "rpc")
    _age(fence, 56, 20)
    assert exec_lock.sweep_intents(post=_chain(56, latest_nonce=3, head=5000),
                                   rpc_url_for=_url) == []
    assert len(_records(fence, 56)) == 2


def test_a_bundle_without_a_max_block_resolves_only_by_nonce(fence):
    exec_lock.intent_send(_raw(56, 3), "bundle", max_block=None)
    _age(fence, 56, 20)
    assert exec_lock.sweep_intents(post=_chain(56, latest_nonce=3, head=5000),
                                   rpc_url_for=_url) == []


@pytest.mark.parametrize("fail", ["timeout", "wrong_chain", "error"])
def test_a_failed_sweep_read_never_resolves(fence, fail):
    exec_lock.intent_send(_raw(4663, 3), "rpc")
    _age(fence, 4663, 20)
    assert exec_lock.sweep_intents(post=_chain(4663, latest_nonce=9, fail=fail),
                                   rpc_url_for=_url) == []
    assert len(_records(fence, 4663)) == 1


def test_no_custody_rpc_never_resolves(fence):
    exec_lock.intent_send(_raw(4663, 3), "rpc")
    _age(fence, 4663, 20)
    assert exec_lock.sweep_intents(post=_chain(4663, latest_nonce=9), rpc_url_for=lambda c: "") == []
    assert len(_records(fence, 4663)) == 1


DEAD_PID = 99999999                     # above any pid_max: never a live process


def _torn(root, content, pid=DEAD_PID):
    d = root / "exec_intents" / "4663"
    d.mkdir(parents=True, exist_ok=True)
    (d / ("deadbeef.%d.1.0" % pid)).write_text(content)


@pytest.mark.parametrize("content", ["", '{"raw": "0x02f8', "not json\n"])
def test_a_truncated_record_from_a_gone_writer_resolves_as_never_posted(fence, content, caplog):
    """Gamma MEDIUM / Alpha: a crash inside the write proves no post (the post runs only after the
    whole line was written and closed). No network read is needed."""
    _torn(fence, content)
    _age(fence, 4663, 20)
    calls = []
    with caplog.at_level(logging.WARNING):
        done = exec_lock.sweep_intents(post=_chain(4663, calls=calls), rpc_url_for=_url)
    assert [b for _c, _t, b in done] == ["never_posted"] and calls == []
    assert _records(fence, 4663) == [] and _lane_reason(fence, "4663") is None
    assert any("basis=never_posted" in r.getMessage() for r in caplog.records)


def test_a_full_record_from_a_gone_writer_still_waits_for_nonce_consumption(fence):
    _child(fence, "exec_lock.intent_send(%r, 'rpc', lock_dir=d)\nos._exit(0)\n" % _raw(4663, 3))
    _age(fence, 4663, 20)
    assert exec_lock.sweep_intents(post=_chain(4663, latest_nonce=3), rpc_url_for=_url) == []
    assert len(_records(fence, 4663)) == 1
    done = exec_lock.sweep_intents(post=_chain(4663, latest_nonce=4), rpc_url_for=_url)
    assert [b for _c, _t, b in done] == ["nonce_consumed"]


def test_a_torn_record_whose_writer_is_alive_stays(fence):
    _torn(fence, "", pid=os.getpid())
    _age(fence, 4663, 20)
    assert exec_lock.sweep_intents(post=_chain(4663), rpc_url_for=_url) == []
    assert _lane_reason(fence, "4663") == "generation_inflight"


def test_a_torn_record_from_before_this_boot_stays(fence):
    """Without fsync a power loss can leave the record of a post that DID leave the host empty."""
    _torn(fence, "")
    _age(fence, 4663, 20)
    assert exec_lock.sweep_intents(post=_chain(4663), rpc_url_for=_url,
                                   boot_time=time.time() - 5) == []
    assert _lane_reason(fence, "4663") == "generation_inflight"


def test_a_short_write_is_a_failed_intent_and_leaves_no_record(fence, monkeypatch):
    real = os.write
    monkeypatch.setattr(exec_lock.os, "write", lambda fd, data: real(fd, data[:10]))
    with pytest.raises(exec_lock.IntentUnwritable):
        exec_lock.intent_send(_raw(4663), "rpc")
    assert _records(fence, 4663) == []
    assert exec_lock.generation_fault() and "short intent write" in exec_lock.generation_fault_reason()


def test_the_alert_names_why_a_record_is_unresolved(fence):
    exec_lock.intent_send(_raw(4663, 3), "rpc")
    _age(fence, 4663, 601)
    alerts = []
    exec_lock.sweep_intents(post=_chain(4663, fail="timeout"), rpc_url_for=_url,
                            alert_fn=alerts.append, alerted=set())
    assert len(alerts) == 1 and "unresolved: custody read failed: TimeoutError" in alerts[0]
    alerts.clear()
    exec_lock.sweep_intents(post=_chain(4663, latest_nonce=3), rpc_url_for=_url,
                            alert_fn=alerts.append, alerted=set())
    assert "unresolved: nonce not consumed" in alerts[0]


def test_ten_minutes_fenced_alerts_once(fence):
    exec_lock.intent_send(_raw(4663, 3), "rpc")
    alerts, alerted = [], set()
    _age(fence, 4663, 599)
    exec_lock.sweep_intents(post=_chain(4663, latest_nonce=3), rpc_url_for=_url,
                            alert_fn=alerts.append, alerted=alerted)
    assert alerts == []
    _age(fence, 4663, 601)
    for _ in range(2):
        exec_lock.sweep_intents(post=_chain(4663, latest_nonce=3), rpc_url_for=_url,
                                alert_fn=alerts.append, alerted=alerted)
    assert len(alerts) == 1
    assert "chain 4663" in alerts[0] and "nonce 3" in alerts[0] and SENDER.lower() in alerts[0].lower()


def test_the_alert_fires_even_when_the_custody_read_fails(fence):
    exec_lock.intent_send(_raw(4663, 3), "rpc")
    _age(fence, 4663, 601)
    alerts = []
    exec_lock.sweep_intents(post=_chain(4663, fail="timeout"), rpc_url_for=_url,
                            alert_fn=alerts.append, alerted=set())
    assert len(alerts) == 1


# ------------------------------------------------------------------------------------------------
# Rollback
# ------------------------------------------------------------------------------------------------

def test_the_rollback_knob_writes_nothing_and_readers_ignore_records(fence, monkeypatch):
    from sniper.services import memecoin_bsc_fast_lane as bsc
    exec_lock.intent_send(_raw(4663, 3), "rpc")              # a record left from before the flip
    monkeypatch.setattr(cfg, "MEMECOIN_EXEC_INTENT_STAMP", False)
    assert exec_lock.intent_send(_raw(56), "rpc") is None and _records(fence, 56) == []
    assert _lane_reason(fence, "4663") is None
    bodies = []
    bsc.blockrazor_bundle("t", post=lambda u, b, h: bodies.append(json.loads(b)) or {})(
        _raw(56), max_block=1020)
    assert "maxBlockNumber" not in bodies[0]["params"][0]
    assert exec_lock.sweep_intents(post=_chain(4663, latest_nonce=9), rpc_url_for=_url) == []


@pytest.mark.parametrize('enabled', [True, False])
def test_required_standing_identity_survives_restart_and_clears_only_on_nonce_proof(
        fence, monkeypatch, enabled):
    monkeypatch.setattr(cfg, 'MEMECOIN_EXEC_INTENT_STAMP', enabled)
    root = exec_lock.standing_intent_lock_dir(lock_dir=str(fence))
    raw = _raw(56, nonce=7)
    token = exec_lock.intent_send(raw, 'rpc', lock_dir=root, required=True)
    assert token and exec_lock.intents_outstanding('56', lock_dir=root, required=True)
    assert not exec_lock.intents_outstanding('56', lock_dir=str(fence))
    assert _lane_reason(fence, '56') is None  # no receipt-duration fence on entry
    # A fresh process observes the record, independent of process cache and the global knob.
    assert _child(fence, "print(exec_lock.intents_outstanding('56', lock_dir="
                  "exec_lock.standing_intent_lock_dir(lock_dir=d), required=True))\n") == 'True'
    assert exec_lock.sweep_intents(
        post=_chain(56, latest_nonce=7), rpc_url_for=_url, min_age_s=0,
        lock_dir=root, required=True) == []
    assert exec_lock.intents_outstanding('56', lock_dir=root, required=True)
    done = exec_lock.sweep_intents(
        post=_chain(56, latest_nonce=8), rpc_url_for=_url, min_age_s=0,
        lock_dir=root, required=True)
    assert [(chain, basis) for chain, _, basis in done] == [('56', 'nonce_consumed')]
    assert not exec_lock.intents_outstanding('56', lock_dir=root, required=True)


def test_existing_optional_intent_setting_stays_effective(fence, monkeypatch):
    monkeypatch.setattr(cfg, 'MEMECOIN_EXEC_INTENT_STAMP', False)
    assert exec_lock.intent_send(_raw(56), 'rpc') is None
    assert not exec_lock.intents_outstanding('56')


def test_single_sweep_owner_recovers_both_namespaces(fence, monkeypatch):
    calls = []
    monkeypatch.setattr(exec_lock, 'sweep_intents', lambda **kw: calls.append(kw))
    class StopLoop(BaseException):
        pass
    def stop(_):
        raise StopLoop
    with pytest.raises(StopLoop):
        exec_lock.run_intent_sweep_forever(sleep=stop)
    assert len(calls) == 2
    assert calls[0].get('lock_dir') is None and not calls[0].get('required')
    assert calls[1]['lock_dir'] == exec_lock.standing_intent_lock_dir()
    assert calls[1]['required'] is True


def test_standing_fence_alert_names_holdings_without_claiming_entries_stopped(fence):
    root = exec_lock.standing_intent_lock_dir()
    exec_lock.intent_send(_raw(56, nonce=7), 'rpc', lock_dir=root, required=True)
    pages = []
    exec_lock.sweep_intents(post=_chain(56, latest_nonce=7), rpc_url_for=_url,
                           min_age_s=0, alert_after_s=0, lock_dir=root, required=True,
                           alert_scope='holdings sells', alert_fn=pages.append)
    assert len(pages) == 1 and 'holdings sells' in pages[0]
    assert 'fast lanes' not in pages[0]
