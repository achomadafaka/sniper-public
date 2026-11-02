"""Desk fast send (operator YES via Alpha 2026-10-23; no A/B): every desk entry signs one 5,000-lamport
Sender tip when the Sender provider is ready, through the real ordinary-entry seam. Nozomi stays off."""
import base64
import time

import pytest
from solders.transaction import VersionedTransaction

from sniper.execution import solana_sender_ab as S
from test_memecoin_nozomi_all_lanes import _executor, _Trace, conn  # noqa: F401

CAP = {"ref_price_usd": 1.0, "cap_pct": 5.0}


def _run(conn, monkeypatch, attempt_id="desk-attempt-1", desk=True, knob="1", no_sender=False):  # noqa: F811
    monkeypatch.setenv("MEMECOIN_DESK_FAST_SEND", knob)
    if no_sender:
        from sniper.execution.submission_transport import SubmissionTransport
        monkeypatch.setattr(SubmissionTransport, "provider", lambda self, pid: None)
    seam, pid, swap_b64, kp, tpu, sender_http, nozomi_http, signed = _executor(conn, monkeypatch, knob=False)
    trace = _Trace(attempt_id)
    seam["execute_fn"](conn, pid, "Coin", 50.0, "2026-10-09T14:00:01+00:00", trace=trace,
                       **({"desk_price_cap": CAP} if desk else {}))
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not sender_http.calls and \
            trace.stages.get("desk_fast_send", {}).get("arm") == "sender":
        time.sleep(0.005)
    return trace, tpu, sender_http, nozomi_http


def _tips(payload):
    msg = VersionedTransaction.from_bytes(base64.b64decode(payload)).message
    return S.tip_transfers(msg), S.tip_transfers(msg, S._NOZOMI_SET)


def test_every_desk_entry_signs_one_5k_sender_tip_and_reaches_sender_only(conn, monkeypatch):  # noqa: F811
    for aid in ("desk-attempt-1", "desk-attempt-2"):
        assert S.assign_desk_fast_send(aid)["arm"] == "sender"
    trace, tpu, sender_http, nozomi_http = _run(conn, monkeypatch)
    (sent,) = tpu
    arm = S.assign_desk_fast_send("desk-attempt-1")
    assert _tips(sent) == ([(arm["tip_account"], S.TIP_LAMPORTS)], [])
    assert trace.stages["desk_fast_send"]["arm"] == "sender" and trace.stages["desk_fast_send"]["provider_ready"]
    assert len(sender_http.calls) == 1 and nozomi_http.calls == []


def test_no_sender_provider_signs_untipped_and_says_so(conn, monkeypatch):  # noqa: F811
    trace, tpu, sender_http, _ = _run(conn, monkeypatch, no_sender=True)
    (sent,) = tpu
    assert _tips(sent) == ([], []) and sender_http.calls == []
    assert trace.stages["desk_fast_send"]["arm"] == "untipped_provider_absent"


@pytest.mark.parametrize("case", ["knob_off", "not_desk"])
def test_knob_off_and_non_desk_sign_todays_untipped_bytes(conn, monkeypatch, case):  # noqa: F811
    trace, tpu, sender_http, nozomi_http = _run(conn, monkeypatch, desk=case != "not_desk",
                                                knob="0" if case == "knob_off" else "1")
    (sent,) = tpu
    assert _tips(sent) == ([], []) and sender_http.calls == [] and "desk_fast_send" not in trace.stages


def test_an_ordinary_path_fast_send_trace_fault_still_signs_and_sends(conn, monkeypatch):  # noqa: F811
    import test_memecoin_nozomi_all_lanes as N

    class Boom(N._Trace):
        def mark_substage(self, name, **kw):
            if name == "desk_fast_send":
                raise RuntimeError("trace sink down")
            super().mark_substage(name, **kw)
    monkeypatch.setattr(__import__(__name__), "_Trace", Boom)
    trace, tpu, sender_http, _ = _run(conn, monkeypatch)
    (sent,) = tpu
    assert len(_tips(sent)[0]) == 1          # the entry continued, tipped, despite the trace fault


def test_a_fault_in_the_ordinary_tip_prep_signs_untipped(conn, monkeypatch):  # noqa: F811
    def boom(_aid):
        raise RuntimeError("assign failed")
    monkeypatch.setattr(S, "assign_desk_fast_send", boom)
    trace, tpu, sender_http, _ = _run(conn, monkeypatch)
    (sent,) = tpu
    assert _tips(sent) == ([], []) and sender_http.calls == []
    assert trace.stages["desk_fast_send"]["arm"] == "untipped_prep_fault"
