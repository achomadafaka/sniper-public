"""The slot lane re-sends its IDENTICAL signed bytes through the TPU sidecar (2026-09-28, Alpha GO).

Measured 2026-09-28 (current fee regime, 17 landed sends): pump_curve landed S+1 on 6/7, PumpSwap on
4/10. In every slot we missed, cheaper untipped transactions on the same mint landed, so our
transaction was not in that leader's buffer; the keyless sidecar delivers exactly once. The lane now
re-sends the same bytes at MEMECOIN_SLOT_LANE_RESEND_OFFSETS_MS (default 150,350) after the first
send, on its own timer thread, until the lane's confirm returns.

Same signature, same blockhash: the network processes a signature at most once, so a re-send can
never fill twice. A down sidecar makes a re-send NOT_SENT and nothing else.
"""
import sqlite3
import threading
import time

import pytest

from sniper.execution import solana_slot_resend as R
from sniper.execution.solana_slot_executor import execute_prebuilt_entry
from sniper.execution.submission_transport import (
    AttemptState, SubmissionAttempt, SubmissionTransport, build_request)
from sniper.persistence import db_memecoin as m

NOW = "2026-09-28T08:00:00+00:00"
REQ = build_request(chain_id="solana", action="entry", tx_identity="SIG", signed_payload="AAEC")


def _accepting(log, delay=0.0):
    def submit(request):
        if delay:
            time.sleep(delay)
        log.append((time.monotonic(), request))
        return SubmissionAttempt("solana:tpu", AttemptState.ACCEPTED, "SIG", 1.0, "2", None)
    return submit


def _run(resender, t0, wait_s):
    resender.start(t0)
    time.sleep(wait_s)
    resender.stop()
    return resender.report()


def test_two_resends_of_the_identical_request_at_the_offsets():
    log = []
    r = R.SlotResender(_accepting(log), lambda: REQ, expected_sha256=REQ.signed_payload_sha256,
                       offsets_ms=(30, 70), source_slot=100, head_fn=lambda: 100)
    t0 = time.monotonic()
    rep = _run(r, t0, 0.2)
    assert rep["sent"] == 2 and [s["state"] for s in rep["sends"]] == ["accepted", "accepted"]
    assert [req for _t, req in log] == [REQ, REQ]
    assert log[0][0] - t0 >= 0.030 and log[1][0] - t0 >= 0.070
    assert [s["head_slot"] for s in rep["sends"]] == [100, 100]


def test_stop_before_the_first_offset_sends_nothing():
    log = []
    r = R.SlotResender(_accepting(log), lambda: REQ, expected_sha256=REQ.signed_payload_sha256,
                       offsets_ms=(200, 400), source_slot=100)
    rep = _run(r, time.monotonic(), 0.02)
    time.sleep(0.25)
    assert rep["sent"] == 0 and log == []


def test_stop_never_waits_for_a_resend_in_flight():
    """The lane thread only sets the event: a slow sidecar must not delay booking the fill."""
    r = R.SlotResender(_accepting([], delay=0.5), lambda: REQ,
                       expected_sha256=REQ.signed_payload_sha256, offsets_ms=(0, 10), source_slot=100)
    r.start(time.monotonic())
    time.sleep(0.05)
    t = time.monotonic()
    r.stop()
    assert time.monotonic() - t < 0.02
    assert r.report()["in_flight"] == 1


def test_different_bytes_are_never_sent():
    other = build_request(chain_id="solana", action="entry", tx_identity="SIG", signed_payload="BBBB")
    log = []
    r = R.SlotResender(_accepting(log), lambda: other, expected_sha256=REQ.signed_payload_sha256,
                       offsets_ms=(0, 10), source_slot=100)
    rep = _run(r, time.monotonic(), 0.1)
    assert log == [] and rep["sent"] == 0
    assert [s["state"] for s in rep["sends"]] == ["identity_mismatch"]


@pytest.mark.parametrize("submit,state", [
    (lambda req: SubmissionAttempt("solana:tpu", AttemptState.NOT_SENT, None, 0.0, None,
                                   "sidecar_unavailable"), "not_sent"),
    (lambda req: (_ for _ in ()).throw(OSError("socket gone")), "error"),
    (lambda req: None, "no_provider"),
])
def test_a_down_sidecar_skips_the_resend_and_never_raises(submit, state):
    r = R.SlotResender(submit, lambda: REQ, expected_sha256=REQ.signed_payload_sha256,
                       offsets_ms=(0, 10), source_slot=100)
    rep = _run(r, time.monotonic(), 0.1)
    assert rep["sent"] == 0 and {s["state"] for s in rep["sends"]} == {state}


def test_unguarded_requests_keep_both_offsets_after_the_head_passes_s_plus_2():
    log = []
    r = R.SlotResender(_accepting(log), lambda: REQ, expected_sha256=REQ.signed_payload_sha256,
                       offsets_ms=(0, 10), source_slot=100, head_fn=lambda: 103)
    rep = _run(r, time.monotonic(), 0.1)
    assert [request for _, request in log] == [REQ, REQ]
    assert [s["state"] for s in rep["sends"]] == ["accepted", "accepted"]


@pytest.mark.parametrize("head", [103, 104, 105, 1000])
def test_late_first_dispatch_has_no_implicit_slot_resend_deadline(head):
    log = []
    r = R.SlotResender(_accepting(log), lambda: REQ, expected_sha256=REQ.signed_payload_sha256,
                       offsets_ms=(0,), source_slot=100, dispatch_slot=102, head_fn=lambda: head)
    rep = _run(r, time.monotonic(), .05)
    assert rep["sent"] == 1 and len(log) == 1


def test_an_unknown_head_does_not_stop_the_resend():
    log = []
    r = R.SlotResender(_accepting(log), lambda: REQ, expected_sha256=REQ.signed_payload_sha256,
                       offsets_ms=(0,), source_slot=100, head_fn=lambda: (_ for _ in ()).throw(KeyError()))
    assert _run(r, time.monotonic(), 0.05)["sent"] == 1


# --- the knob -----------------------------------------------------------------------------------

@pytest.mark.parametrize("flag,offsets,expected", [
    (None, None, (150, 350)), ("1", None, (150, 350)), ("0", None, ()),
    ("1", "100,250", (100, 250)), ("1", "100,250,400", (100, 250)), ("1", "250,100", ()),
    ("1", "-5,100", ()), ("1", "abc", ()), ("1", "", ()),
])
def test_the_offsets_knob(monkeypatch, flag, offsets, expected):
    for name, value in (("MEMECOIN_SLOT_LANE_RESEND", flag),
                        ("MEMECOIN_SLOT_LANE_RESEND_OFFSETS_MS", offsets)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    from sniper.settings import memecoin as cfg
    assert cfg.memecoin_slot_lane_resend_offsets_ms() == expected


# --- the transport's single-provider submit ------------------------------------------------------

class _Provider:
    provider_id = "solana:tpu"

    def __init__(self):
        self.calls = []

    def submit(self, request):
        self.calls.append(request)
        return SubmissionAttempt("solana:tpu", AttemptState.ACCEPTED, request.tx_identity, 1.0, "2")


def test_submit_one_reaches_only_the_named_provider_and_respects_its_lease():
    tpu, rpc = _Provider(), _Provider()
    rpc.provider_id = "solana:custody_rpc"
    t = SubmissionTransport([rpc, tpu], deadline_s=1.0)
    assert t.submit_one("solana:tpu", REQ).state is AttemptState.ACCEPTED
    assert tpu.calls == [REQ] and rpc.calls == []
    assert t.submit_one("solana:jito", REQ) is None
    lease = dict((p.provider_id, lk) for p, lk in t._provider_leases)["solana:tpu"]
    with lease:
        busy = t.submit_one("solana:tpu", REQ)
    assert busy.state is AttemptState.NOT_SENT and busy.reason_code == "provider_busy"


class _ResendTimer:
    def __init__(self):
        self.now, self.stopped = 0.0, False
        self.on_wait = lambda: None

    def wait(self, seconds):
        self.now = round(self.now + seconds, 9)
        if seconds:
            self.on_wait()
        return self.stopped

    def set(self):
        self.stopped = True


@pytest.fixture
def busy_tpu_resender():
    """Keep the real transport lease; replace only wire I/O and elapsed time."""
    provider, timer, probes = _Provider(), _ResendTimer(), []
    transport = SubmissionTransport([provider], deadline_s=1.0)
    lease = transport._provider_leases[0][1]
    lease.acquire()

    def submit(request):
        probes.append((timer.now, request))
        return transport.submit_one("solana:tpu", request)

    resender = R.SlotResender(submit, lambda: REQ, expected_sha256=REQ.signed_payload_sha256,
                             offsets_ms=(150, 350), source_slot=100, head_fn=lambda: 100,
                             clock=lambda: timer.now)
    resender._stop = timer
    try:
        yield resender, timer, lease, provider, probes
    finally:
        if lease.locked():
            lease.release()
        transport.close()


def test_a_busy_first_offset_waits_for_the_lease_before_the_next_offset(busy_tpu_resender):
    r, timer, lease, provider, probes = busy_tpu_resender

    def release():
        if timer.now >= .2038 and lease.locked():
            lease.release()

    timer.on_wait = release
    r._run(0.0)
    assert provider.calls == [REQ, REQ]
    assert [s["at_ms"] for s in r.report()["sends"]] == pytest.approx([205, 350])
    assert r.report()["sent"] == 2
    assert all(request is REQ for _, request in probes)
    assert max(right[0] - left[0] for left, right in zip(probes, probes[1:])
               if right[0] <= .205) <= .005000001


@pytest.mark.parametrize("change,state", [
    ("stop", None), ("head", "window_ended"), ("payload", "identity_mismatch"),
])
def test_a_deferred_resend_rechecks_stop_head_and_identity(busy_tpu_resender, change, state):
    r, timer, _, provider, probes = busy_tpu_resender
    if change == "head":
        guarded = build_request(chain_id="solana", action="entry", tx_identity="SIG",
            signed_payload="AAEC", entry_max_slot=101,
            entry_head_fn=lambda: (r._head(), timer.now))
        r._request_fn = lambda: guarded

    def invalidate():
        if timer.now < .175:
            return
        if change == "stop":
            r.stop()
        elif change == "head":
            r._head_fn = lambda: 103
        else:
            other = build_request(chain_id="solana", action="entry", tx_identity="SIG",
                                  signed_payload="BBBB")
            r._request_fn = lambda: other

    timer.on_wait = invalidate
    r._run(0.0)
    assert provider.calls == []
    assert timer.now == pytest.approx(.175)
    assert all(at < .175 for at, _ in probes)
    if state is not None:
        assert r.report()["sends"][-1]["state"] == state


def test_forever_busy_uses_no_deferral_after_the_final_offset(busy_tpu_resender):
    r, timer, _, provider, probes = busy_tpu_resender
    r._run(0.0)
    assert provider.calls == []
    assert timer.now == pytest.approx(.350)
    assert sum(at == .350 for at, _ in probes) == 1
    assert [s["state"] for s in r.report()["sends"]] == ["not_sent", "not_sent"]
    assert all(s["reason"] == "provider_busy" for s in r.report()["sends"])


def test_a_single_busy_offset_has_no_deferral_grace(busy_tpu_resender):
    r, timer, _, provider, probes = busy_tpu_resender
    r._offsets = (150,)
    r._run(0.0)
    assert provider.calls == [] and probes == [(.150, REQ)]
    assert timer.now == pytest.approx(.150)


def test_oversleeping_the_next_offset_drops_the_deferred_send(busy_tpu_resender):
    r, timer, lease, provider, probes = busy_tpu_resender

    def oversleep():
        if .150 < timer.now < .350:
            timer.now = .400
            lease.release()

    timer.on_wait = oversleep
    r._run(0.0)
    assert provider.calls == [REQ]
    assert probes == [(.150, REQ), (.400, REQ)]
    assert [s["state"] for s in r.report()["sends"]] == ["not_sent", "accepted"]
    assert r.report()["sends"][-1]["offset_ms"] == 350


@pytest.mark.parametrize("state,reason", [
    (AttemptState.AMBIGUOUS, "provider_busy"),
    (AttemptState.EXPLICIT_REJECTED, "provider_busy"),
    (AttemptState.NOT_SENT, "sidecar_unavailable"),
])
def test_only_proved_busy_not_sent_may_defer(busy_tpu_resender, state, reason):
    r, _, _, provider, probes = busy_tpu_resender

    def submit(request):
        probes.append(request)
        return SubmissionAttempt("solana:tpu", state, None, 0.0, None, reason)

    r._submit = submit
    r._run(0.0)
    assert probes == [REQ, REQ] and provider.calls == []
    assert len(r.report()["sends"]) == 2


def test_the_send_seam_exposes_the_request_it_bound(monkeypatch):
    from sniper.execution import submission_wiring as W
    monkeypatch.setattr(W, "solana_tx_identity", lambda b64: "SIG")
    monkeypatch.setattr(W, "solana_blockhash", lambda b64: "HASH")
    tpu = _Provider()
    send = W.make_solana_send_fn(SubmissionTransport([tpu], deadline_s=1.0), action="entry",
                                 owner_kind="reservation", owner_id="1", record=False)
    assert send("AAEC") == "SIG"
    assert send.last_request == tpu.calls[0]
    assert send.last_request.signed_payload_sha256 == REQ.signed_payload_sha256


# --- the executor -------------------------------------------------------------------------------

class _Trace:
    def __init__(self):
        self.substages = []

    def mark_substage(self, name, **kw):
        self.substages.append((name, kw))

    def mark(self, *a, **k):
        pass

    def mark_submission(self, *a, **k):
        pass


class _Resender:
    def __init__(self, order):
        self.order = order

    def start(self, t0):
        self.order.append("resend_start")

    def stop(self):
        self.order.append("resend_stop")

    def report(self):
        return {"sent": 1, "in_flight": 0, "sends": [{"offset_ms": 150, "state": "accepted",
                                                         "head_slot": 101}]}


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "ledger.db")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    yield c
    c.close()


def _pending(conn):
    pid = m.reserve_pending(conn, mint="MINT", source_user_id="u", strategy="alpha_b", notional=100.0,
                            reserved_at=NOW, network_id="solana", origin_event_id="slotlane:sig",
                            signal_source="slot_lane", source_price=1e-6)
    return m.pending_row(conn, pid)


def test_the_executor_sends_first_resends_after_and_stops_when_the_outcome_is_known(conn):
    from sniper.execution import solana_real_seams as seams
    order, trace = [], _Trace()

    def send(_b64):
        order.append("send")
        return True

    def confirm(_sig):
        order.append("confirm")
        return seams.ConfirmState("failed", err={"x": 1}, landing_slot=102)
    result = execute_prebuilt_entry(
        conn, pending=_pending(conn), signed_b64="AA==", sig="SIG", validity=None, send_fn=send,
        confirm_fn=confirm, receipt_fill_fn=lambda *a: None, now_iso=NOW, trace=trace,
        resender=_Resender(order))
    assert order == ["send", "resend_start", "confirm", "resend_stop"]
    assert result["resends_sent"] == 1
    marks = dict(trace.substages)
    assert marks["slot_resend"]["sent"] == 1 and marks["slot_resend"]["landing_slot"] == 102
    assert marks["slot_resend"]["sends"][0]["landing_minus_head"] == 1


def test_a_rejected_first_send_starts_no_resend(conn):
    order = []
    result = execute_prebuilt_entry(
        conn, pending=_pending(conn), signed_b64="AA==", sig="SIG", validity=None,
        send_fn=lambda _b64: False, confirm_fn=lambda _s: pytest.fail("no confirm"),
        receipt_fill_fn=lambda *a: None, now_iso=NOW, trace=_Trace(), resender=_Resender(order))
    assert result["status"] == "failed" and order == []
