"""Native entry observations retain exact signed identity and call-local safety measurements."""
import json
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_fill_timing as timing
from sniper.services import memecoin_rh_fast_lane as lane
from test_memecoin_fill_timing import _okt_attempt
from test_memecoin_rh_parallel_safety import dedicated  # noqa: F401


TX = "0x" + "ab" * 32


def native_attempt(*, modern=False):
    attempt = _okt_attempt()
    trace = json.loads(attempt["trace_json"])
    clock = trace["receipt_clock"]
    events = []
    for name, ms in (("evm.sender_dispatch", 965), ("rh_fast_lane_send", 978),
                     ("rh_fast_lane_receipt", 1865)):
        event = dict(name=name, boot_id=clock["boot_id"],
                     monotonic_ns=clock["monotonic_ns"] + ms * 1_000_000)
        if name == "evm.sender_dispatch" or modern:
            event["tx_sig"] = TX
        if modern:
            event["attempt_no"] = 1
        if name == "rh_fast_lane_send":
            event["reply"] = "accepted"
        if name == "rh_fast_lane_receipt":
            event.update(block=503, block_time=1791826828)
        events.append(event)
    trace.update(substage_events=events, submission_events=[])
    attempt.update(signal_source="bsc_fast_lane", delivery_lane="bsc_fast_lane", tx_sig=TX,
                   transaction_submitted_at=None, chain_confirmation_completed_at=None,
                   source_slot=500, trace_json=json.dumps(trace))
    return attempt


@pytest.mark.parametrize("modern", [False, True])
def test_exact_native_singleton_keeps_dispatch_ack_and_receipt_observations(modern):
    facts = timing.facts(native_attempt(modern=modern))
    assert (facts["receipt_dispatch_ms"], facts["receipt_submit_ms"],
            facts["receipt_confirmation_ms"]) == (965, 978, 1865)
    assert timing.stages(facts)["decide_ms"] == 978
    assert timing.stages(facts)["land_ms"] == 887
    assert timing.gap_of(facts) == 3
    line = timing.landed_text(facts)
    assert "receipt-to-submit observed 978 ms" in line
    assert "receipt-to-confirmation observed 1865 ms" in line
    assert "no stage measured" not in line


@pytest.mark.parametrize("bad", ["shared", "foreign_tx", "foreign_boot", "second_send",
                                  "second_receipt", "missing_dispatch", "retry", "unordered"])
def test_legacy_native_ambiguity_never_recovers_a_span_or_cached_landing(bad):
    attempt = native_attempt()
    trace = json.loads(attempt["trace_json"])
    events = trace["substage_events"]
    if bad == "shared":
        attempt["signal_source"] = "follow_activity"
    elif bad == "foreign_tx":
        events[0]["tx_sig"] = "0x" + "cd" * 32
    elif bad == "foreign_boot":
        events[1]["boot_id"] = "other:boot"
    elif bad == "second_send":
        events.append(dict(events[1]))
    elif bad == "second_receipt":
        events.append(dict(events[2]))
    elif bad == "missing_dispatch":
        events.pop(0)
    elif bad == "retry":
        events.append(dict(name="another_attempt", attempt_no=2))
    else:
        events[2]["monotonic_ns"] = events[0]["monotonic_ns"] - 1
    attempt.update(trace_json=json.dumps(trace), landing_slot=503)
    facts = timing.facts(attempt, timing={"landing_slot": 503})
    assert all(facts[key] is None for key in
               ("receipt_dispatch_ms", "receipt_submit_ms", "receipt_confirmation_ms"))
    assert facts["landing_slot"] is None


@pytest.mark.parametrize("chain", [56, 4663])
def test_native_send_records_signed_attempt_identity_on_every_boundary(chain, monkeypatch):
    from sniper.persistence import db_memecoin as db
    monkeypatch.setattr(db, "bind_entry_submission", lambda *a, **kw: True)
    marks = []
    trace = SimpleNamespace(mark_substage=lambda name, **kw: marks.append((name, kw)))
    live = SimpleNamespace(ctx={"lane_claim_nonce_fn": lambda n: 7,
                               "sign_tx_fn": lambda tx: ("0xraw", TX)},
                           CHAIN_ID=chain, BACKEND="native", send_times=[],
                           _gas_price=lambda snap: 1, _quote_asset_usd=lambda p: 1,
                           _post=lambda raw, p: ("accepted", TX), host_clock=lambda: 1,
                           _send_evidence=lambda p: {}, _fanout_allowed=lambda p: True)
    assert lane.LiveLane._send(live, {"snapshot": {}}, {}, None, 1,
                               {"to": "addr", "data": "0x", "gas": 1}, 1, trace) == TX
    assert [name for name, _ in marks] == ["evm.sender_dispatch", "rh_fast_lane_send"]
    assert all(mark.get("tx_sig") == TX and mark.get("attempt_no") == 1 for _, mark in marks)


@pytest.mark.parametrize("timed", [False, True])
def test_routed_safety_forwards_only_explicit_call_local_timings(timed):
    calls = []
    if timed:
        def check(mint, usd=None, probe_out=None, timings=None):
            calls.append(mint)
            timings["code_ms"] = 12.0
            return True, "safe"
    else:
        def check(mint, usd=None, probe_out=None):
            calls.append(mint)
            return True, "safe"
    safety = lane.routed_safety(check, {}, "offline", None, path="inline", times=lane.SafetyTimes())
    measures = {}
    assert safety("mint", timings=measures) == (True, "safe")
    assert calls == ["mint"]
    assert measures == ({"code_ms": 12.0} if timed else {})


def test_native_inline_measurements_are_retained_with_gate_inputs(dedicated):
    built, _, _, _ = dedicated
    live = built["live"]
    def safety(mint, usd=None, timings=None):
        timings["code_ms"] = 12.0
        timings["simulation_ms"] = 34.0
        return True, "safe"
    live.inline_safety_fn = live._timed_inline_safety_fn = safety
    live.inline_facts_fn, live.stats_fn = lambda m: (18, 10 ** 27), None
    rec = {"inputs_ms": 17.0}
    assert live._inline("mint", rec) is None
    evidence = rec["execution_evidence"]
    assert evidence["token_safety_timings"] == {"code_ms": 12.0, "simulation_ms": 34.0}
    assert evidence["inline_inputs_path_ms"] == 17.0
    assert evidence["inline_safety_path_ms"] >= 0


def test_bsc_factory_retains_existing_checker_timings(monkeypatch):
    from sniper.execution import evm_token_safety
    from sniper.services import memecoin_bsc_fast_lane as bsc
    calls = []
    def build(**kwargs):
        def check(mint, usd=None, timings=None):
            calls.append(mint)
            timings["simulation_ms"] = 9.0
            return True, "safe"
        return check
    monkeypatch.setattr(evm_token_safety, "build_evm_token_safety", build)
    safety = bsc.lane_safety_fn("https://offline", None, "taker", {})
    timings = {}
    assert safety("mint", timings=timings) == (True, "safe")
    assert timings == {"simulation_ms": 9.0} and calls == ["mint"]


def test_safety_type_error_is_preserved_without_a_second_call():
    calls = []
    def check(mint, usd=None, probe_out=None, timings=None):
        calls.append(mint)
        raise TypeError("provider result malformed")
    safety = lane.routed_safety(check, {}, "offline", None, path="inline", times=lane.SafetyTimes())
    with pytest.raises(TypeError, match="provider result malformed"):
        safety("mint", timings={})
    assert calls == ["mint"]


def test_parallel_safety_does_not_publish_an_unfinished_call_dictionary(dedicated):
    import threading
    built, _, _, _ = dedicated
    live = built["live"]
    entered, release = threading.Event(), threading.Event()
    def safety(mint, usd=None, timings=None):
        timings["code_ms"] = 12.0
        entered.set()
        release.wait(1)
        timings["simulation_ms"] = 99.0
        return True, "safe"
    live.inline_safety_fn = live._timed_inline_safety_fn = safety
    live.inline_deadline_s = .02
    rec = {}
    try:
        assert live._start_safety("mint", rec) is None
        assert entered.wait(1)
        assert live._inline("mint", rec) == "inline_unsafe:safety_deadline_exceeded"
        assert "token_safety_timings" not in rec["execution_evidence"]
        assert not live._safety_future.done()
    finally:
        release.set()
        live._safety_future.result(timeout=1)
    assert "token_safety_timings" not in rec["execution_evidence"]
