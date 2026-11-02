"""4663 fast lane M2 step 1: the detector -> runner datagram and the in-runner runner_shadow thread
(spec 2026-10-01-memecoin-4663-fast-lane-design.md §2.9; Alpha's build order 2026-10-01). The
runner_shadow thread signs nothing, reserves nothing and sends nothing: it measures the handoff,
the wallet-lock contention and its own busy window, which the GIL-overlap read joins."""
import json
import os
import socket
import time
from fractions import Fraction
from pathlib import Path

import pytest

from sniper.services import memecoin_rh_fast_lane as lane

NATIVE = "0x" + "00" * 20
TOKEN = "0xcabb5484150745a3349b956da5d6e97bd7c8dfd9"
HOOK = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"
KEY = dict(currency0=NATIVE, currency1=TOKEN, fee=0, tick_spacing=200, hooks=HOOK)
CAP = Fraction(7, 3)                                     # wei per token atomic
REC = dict(tx_hash="0xsrc", seq=77689412, wallet="0xw", mint=TOKEN, handle="Degen",
           source_user_id="uid", permit_nonce=9, arrival_utc="2026-10-01T22:00:00+00:00")
OUT = dict(reason="would_send", pool="0x" + "a1" * 32, key=KEY, cap_price=CAP, clip_wei=10 ** 16,
           min_out=10 ** 16 * 3 // 7, state_kind="tape_post", tape_lag_blocks=2,
           est_post_sqrt=874745060896749537239184449426278, src_in_wei=25854436920000000,
           src_out=3182688391454788035426447, paid_usd=65.3)


INPUTS = dict(snapshot=dict(native_wei=10 ** 17, weth_wei=5 * 10 ** 16, nonce=42,
                            gas_price_wei=40_000_000, read_at=1000.0),
              eth_usd=2500.0, eth_usd_at=1000.0)


def _payload(**over):
    return dict(lane.lane_payload(REC, OUT, sent_epoch=1000.0, inputs=INPUTS), **over)


class PassingHook:
    """The funded hook's shadow answer when every gate passes (the J7 shadow stop)."""
    def __init__(self, contexts, stats):
        self.contexts, self.stats = contexts, stats

    def raw(self, conn, ev):
        return {"status": "shadow", "reason": "j7_shadow: pre-entry checks passed"}


STATS = ({"price": 2.05e-05, "volume_h1": 20000.0}, 1000.0)


def _shadow(tmp_path, **kw):
    import sqlite3
    args = dict(lock_key="evm-4663-0xabc", lock_dir=str(tmp_path), taker="0x" + "11" * 20,
                now_fn=time.time, clip_usd=50.0, low_wei=2 * 10 ** 15,
                generation_fn=lambda: 900.0, host_clock=lambda: 1005.0,
                facts_fn=lambda mint: (18, 10 ** 27), ctx={"address": "0x" + "11" * 20},
                ro_connect=lambda: sqlite3.connect(":memory:", isolation_level=None),
                hook_factory=PassingHook)
    args.update(kw)
    shadow = lane.RunnerShadow(**args)
    handle = shadow.handle
    shadow.handle = lambda payload, **k: handle(payload, **{"stats": STATS, **k})
    return shadow


@pytest.mark.parametrize("raw,want", [("", ""), ("shadow", "shadow"), ("runner_shadow", "runner_shadow"),
                                      (" Runner_Shadow ", "runner_shadow"), ("live", "live"),
                                      ("LIVE", "live"), ("1", ""), ("on", "")])
def test_the_mode_knob_takes_shadow_runner_shadow_and_live_only(monkeypatch, raw, want):
    # Anything else is off; a typo never falls back to a shadow or to live.
    from sniper.settings import memecoin as cfg
    monkeypatch.setenv("MEMECOIN_RH_FAST_LANE", raw)
    assert cfg.memecoin_rh_fast_lane_mode() == want


def test_the_payload_carries_the_decision_and_survives_json():
    p = json.loads(json.dumps(_payload()))
    assert p["tx_hash"] == "0xsrc" and p["mint"] == TOKEN and p["key"] == KEY
    assert (p["cap_num"], p["cap_den"]) == (7, 3) and p["clip_wei"] == 10 ** 16
    assert p["sent_epoch"] == 1000.0 and p["permit_nonce"] == 9


def test_each_state_root_has_its_own_abstract_address(tmp_path):
    a, b = lane.lane_address(tmp_path / "a"), lane.lane_address(tmp_path / "b")
    assert a != b and a.startswith("\0") and lane.lane_address(tmp_path / "a") == a


def test_a_datagram_arrives_with_the_kernel_verified_sender(tmp_path):
    addr = lane.lane_address(tmp_path)
    sock = lane.LaneSocket(addr)
    try:
        assert lane.send_signal(addr, _payload()) is True
        got = sock.recv(timeout=2.0)
        assert got is not None
        payload, pid, uid = got
        assert payload["tx_hash"] == "0xsrc" and pid == os.getpid() and uid == os.getuid()
        assert sock.recv(timeout=0.05) is None                         # nothing else queued
    finally:
        sock.close()


def test_sending_with_nothing_bound_never_raises(tmp_path):
    assert lane.send_signal(lane.lane_address(tmp_path / "nobody"), _payload()) is False


@pytest.mark.parametrize("pid, uid, detector, payload, reason", [
    (111, os.getuid(), 111, None, None),
    (111, os.getuid() + 1, 111, None, "foreign_uid"),
    (222, os.getuid(), 111, None, "not_the_detector"),
    (111, os.getuid(), None, None, "no_detector"),
    (111, os.getuid(), 111, {"tx_hash": "0x1"}, "malformed"),
])
def test_only_the_detector_process_of_this_user_is_heard(pid, uid, detector, payload, reason):
    got = lane.check_sender(payload or _payload(), pid, uid, detector_pid=detector)
    assert got == reason


def test_the_detector_pid_comes_from_a_fresh_heartbeat(tmp_path):
    beat = tmp_path / "hb"
    assert lane.detector_pid(beat, now=1000.0) is None
    beat.write_text(json.dumps({"ts": 990.0, "pid": 4242}))
    assert lane.detector_pid(beat, now=1000.0) == 4242
    assert lane.detector_pid(beat, now=1061.0) is None                   # stale: no detector
    beat.write_text(json.dumps({"ts": 999.0}))
    assert lane.detector_pid(beat, now=1000.0) is None


def test_a_signal_is_handled_once_by_tx_and_once_by_permit():
    seen = lane.Seen()
    assert seen.first(_payload()) is True
    assert seen.first(_payload()) is False
    assert seen.first(_payload(tx_hash="0xother")) is False             # same (wallet, permit nonce)
    assert seen.first(_payload(tx_hash="0xthird", permit_nonce=10)) is True


def test_runner_shadow_measures_handoff_lock_and_busy_window_and_builds_unsigned(tmp_path):
    clock = iter([1000.041, 1000.043])                                  # busy start, busy end
    shadow = _shadow(tmp_path, now_fn=lambda: next(clock))
    rec = shadow.handle(_payload(), recv_epoch=1000.040)
    assert rec["reason"] == "built" and rec["lock_busy"] == 0
    assert rec["handoff_ms"] == pytest.approx(40.0)
    assert rec["busy_start_epoch"] == 1000.041 and rec["busy_end_epoch"] == 1000.043
    clip = 2 * 10 ** 16                                                 # $50 at 2500 USD/ETH
    assert rec["clip_wei"] == clip and rec["min_out"] == clip * 3 // 7 and rec["tx_bytes"] > 0


def test_runner_shadow_sees_a_held_wallet_lock_and_never_waits_for_it(tmp_path):
    from sniper.execution.exec_lock import wallet_exec_lock
    shadow = _shadow(tmp_path)
    import threading
    got = []
    with wallet_exec_lock("evm-4663-0xabc", lock_dir=str(tmp_path)):
        worker = threading.Thread(target=lambda: got.append(
            shadow.handle(_payload(), recv_epoch=time.time())), daemon=True)
        worker.start()
        worker.join(0.5)
        assert not worker.is_alive(), "the lane waited for the wallet lock"
    (rec,) = got
    assert rec["reason"] == "lock_busy" and rec["lock_busy"] == 1


def test_a_payload_that_cannot_build_is_a_named_reason_not_a_crash(tmp_path):
    rec = _shadow(tmp_path).handle(_payload(cap_num=1, cap_den=10 ** 30), recv_epoch=time.time())
    assert rec["reason"] == "build_refused" and rec["tx_bytes"] is None


def test_records_are_written_to_the_lanes_own_file(tmp_path):
    import sqlite3
    db = str(tmp_path / "lane.db")
    lane.write_record(db, dict(_shadow(tmp_path).handle(_payload(), recv_epoch=time.time()),
                               mode="runner_shadow"))
    row = sqlite3.connect(db).execute(
        "SELECT tx_hash, mode, reason, lock_busy FROM rh_fast_lane_events").fetchone()
    assert row == ("0xsrc", "runner_shadow", "built", 0)


def test_runner_shadow_never_signs_sends_or_reserves():
    # Step 3 put the live lane in the same module; the runner_shadow path stays side-effect free.
    # Identifiers in the CODE (AST), never the prose: a guard must not read its own docstring.
    import ast
    import inspect
    import textwrap
    names = set()
    for obj in (lane.RunnerShadow, lane._LaneChecks, lane.inputs_reason):
        for node in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(obj)))):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
    for name in ("sign_tx", "sign_tx_fn", "sign_transaction", "send", "sender", "fanout_fn",
                 "reserve_entry_evm", "reserve_fn", "bind_entry_submission", "release_pending",
                 "lane_claim_nonce_fn", "_freshest_read_member"):
        assert name not in names, name


def test_the_detector_never_runs_inside_the_runner_and_the_lane_starts_only_in_runner_shadow():
    # Gamma MEDIUM (M0): the per-block detector stays out of the runner. M2's per-signal lane is the
    # only fast-lane code the runner hosts, started by start_lane, which honours the mode knob.
    src = (Path(lane.__file__).resolve().parents[2] / "scripts" / "memecoin_runner_loop.py").read_text()
    assert "memecoin_rh_feed_shadow" not in src and "memecoin_rh_fast_decide" not in src
    assert "start_lane(" in src


def test_start_lane_is_off_unless_runner_shadow_or_live(monkeypatch, tmp_path):
    for mode in ("", "shadow"):
        monkeypatch.setenv("MEMECOIN_RH_FAST_LANE", mode)
        assert lane.start_lane({"4663": {"address": "0xabc"}}, log=lambda *_: None,
                               root=tmp_path) is None
    monkeypatch.setenv("MEMECOIN_RH_FAST_LANE", "runner_shadow")
    assert lane.start_lane({}, log=lambda *_: None, root=tmp_path) is None       # no 4663 context


@pytest.mark.parametrize("mode", ["runner_shadow", "live"])
def test_the_detector_sends_only_would_send_decisions_when_the_runner_lane_runs(mode):
    sent = []
    on = lane.make_lane_sender(mode, "\0addr", send_fn=lambda a, p: sent.append((a, p)),
                               clock=lambda: 1000.0)
    assert on is not None, "in %s the lane would never hear a signal" % mode
    on(REC, OUT)
    on(REC, dict(OUT, reason="no_pool"))       # a fallback (float_short: see the test below)
    assert [(a, p["tx_hash"], p["sent_epoch"]) for a, p in sent] == [("\0addr", "0xsrc", 1000.0)]
    assert lane.make_lane_sender("shadow", "\0addr", send_fn=lambda a, p: sent.append(p)) is None
    assert lane.make_lane_sender("", "\0addr") is None


def test_the_decision_reaches_the_lane_before_its_row_is_written(tmp_path):
    # The datagram goes out right after the timed decide; ranking and the SQLite write come after.
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    import test_memecoin_rh_fast_decide as t
    from sniper.services import memecoin_rh_fast_decide as fast
    order = []
    tape = t._tape([t._row(t.ZPOOL, t.B - 3, "0xpre", t.ZUP_PRE["sqrt"])])
    bad_db = str(tmp_path / "missing-dir" / "x.db")                     # the write will fail
    with pytest.raises(Exception):
        fast.handle_signal(dict(t.SIG, paid=t._paid_for(t.ZUP_IN)), time.perf_counter(), tape=tape,
                           live=dict(t.CTX, now=None), db_path=bad_db, now_fn=lambda: 1000.0,
                           on_decided=lambda rec, out, inputs: order.append(out))
    (out,) = order
    assert out["reason"] == "would_send" and out["key"]["hooks"] == t.HOOK


# ---- Gamma's review of 02a3c0f36 ----

def test_the_lane_and_the_evm_leg_share_one_lock_key_helper():
    # Gamma MEDIUM: a drifted key (case, format) would let the lane and an executor hold "the" 4663
    # lock at once. One helper, byte-exact, used by both; the lane keeps the default lock dir too.
    from sniper.execution.exec_lock import evm_exec_lock_key
    assert evm_exec_lock_key("4663", "0xAbCd") == "evm-4663-0xAbCd"
    root = Path(lane.__file__).resolve().parents[2]
    leg = (root / "sniper" / "services" / "memecoin_evm_leg.py").read_text()
    src = Path(lane.__file__).read_text()
    assert "evm_exec_lock_key(chain, address)" in leg and '"evm-%s-%s"' not in leg
    assert "evm_exec_lock_key(" in src and "evm-%s-%s" not in src and "lock_dir=str(" not in src


def test_the_runner_lane_never_loads_the_detector_module():
    # Gamma LOW: the buy builder lives under sniper/execution; the lane imports nothing of M1.
    from sniper.execution.evm_v4_quote import native_buy_tx                # noqa: F401
    assert "memecoin_rh_fast_decide" not in Path(lane.__file__).read_text()


def test_dropped_datagrams_are_counted_in_memory_and_flushed_together(tmp_path):
    import sqlite3
    drops = lane.Drops()
    for reason in ("foreign_uid", "foreign_uid", "duplicate"):
        drops.add(reason)
    db = str(tmp_path / "lane.db")
    drops.flush(db)
    drops.flush(db)                                                        # nothing new: no row
    rows = sorted(sqlite3.connect(db).execute("SELECT reason, n FROM rh_fast_lane_drop_counts"))
    assert rows == [("duplicate", 1), ("foreign_uid", 2)]


def test_a_squatted_lane_name_is_logged_once(tmp_path):
    addr = lane.lane_address(tmp_path)
    squatter = lane.LaneSocket(addr)
    try:
        logged, state = [], {}
        assert lane.bind_or_log(addr, state, logged.append) is None
        assert lane.bind_or_log(addr, state, logged.append) is None
        assert len(logged) == 1 and "in use" in logged[0]
    finally:
        squatter.close()
    sock = lane.bind_or_log(addr, state, logged.append)
    assert sock is not None
    sock.close()


def test_runner_shadow_names_an_input_fallback_and_never_touches_the_lock(tmp_path):
    from sniper.execution.exec_lock import wallet_exec_lock
    shadow = _shadow(tmp_path, generation_fn=lambda: 1001.0)        # an execution after the read
    with wallet_exec_lock("evm-4663-0xabc", lock_dir=str(tmp_path)):   # held: must not matter
        rec = shadow.handle(_payload(), recv_epoch=time.time())
    assert rec["reason"] == "snapshot_predates_execution" and rec["lock_busy"] == 0
    assert _shadow(tmp_path).handle(_payload(eth_usd=None), recv_epoch=1.0)["reason"] == "no_eth_usd"


def test_the_datagram_carries_the_detectors_research_inputs():
    sent = []
    on = lane.make_lane_sender("runner_shadow", "\0a", send_fn=lambda a, p: sent.append(p),
                               clock=lambda: 1.0)
    on(REC, OUT, dict(INPUTS, float_wei=1, decimals={}))
    (p,) = sent
    assert p["snapshot"] == INPUTS["snapshot"] and (p["eth_usd"], p["eth_usd_at"]) == (2500.0, 1000.0)
    assert "float_wei" not in p and "decimals" not in p


def test_the_datagram_carries_the_post_state_and_source_amounts_the_live_lane_prices_from():
    p = lane.lane_payload(REC, dict(OUT, est_post_sqrt=77, src_in_wei=5, src_out=9, paid_usd=4.5),
                          sent_epoch=1.0)
    assert (p["post_sqrt"], p["src_in_wei"], p["src_out"], p["paid_usd"]) == (77, 5, 9, 4.5)


# ---- Gamma 2026-10-02: the detector's own float check stopped every shadow signal before the runner ----

def test_runner_shadow_also_hears_a_float_short_decision_and_live_does_not():
    sent = []
    short = dict(OUT, reason="float_short")
    shadow = lane.make_lane_sender("runner_shadow", "\0a", send_fn=lambda a, p: sent.append(p),
                                   clock=lambda: 1.0)
    shadow(REC, short, INPUTS)
    shadow(REC, dict(OUT, reason="float_unknown"), INPUTS)          # no snapshot: nothing to judge
    shadow(REC, dict(OUT, reason="no_pool"), INPUTS)
    assert [p["decide_reason"] for p in sent] == ["float_short"]
    live = lane.make_lane_sender("live", "\0a", send_fn=lambda a, p: sent.append(p),
                                 clock=lambda: 1.0)
    live(REC, short, INPUTS)
    assert len(sent) == 1                                           # live never hears it


def test_a_float_short_datagram_is_judged_by_the_runner_shadow_past_the_float(tmp_path):
    shadow = _shadow(tmp_path)
    payload = _payload(snapshot=dict(INPUTS["snapshot"], native_wei=10 ** 14), decide_reason="float_short")
    rec = shadow.handle(payload, recv_epoch=time.time())
    assert rec["reason"] == "float_short+built"
