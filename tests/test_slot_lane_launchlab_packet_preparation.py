"""LaunchLab preparation repairs only an exact packet overflow inside its original age budget."""
import base64
import dataclasses
from types import SimpleNamespace

import pytest

from sniper.execution import pump_curve_buy as P
from test_slot_lane_launchlab import LL, L, NVDA_B, _jup, _routed, _snap, _worker


def _preparation(monkeypatch, *, padded=472, tight=0, live=True, mode="oversize",
                 statuses=None, initial_status=None, initial_payload=None, elapsed=None, admission=None):
    clock = SimpleNamespace(now=50.0)
    clock.monotonic = lambda: clock.now
    calls, published, refused, snapshots = [], [], [], []
    worker, _ = _worker(NVDA_B, calls, out=44_000_000,
                         admission=admission or (lambda buy, quote: None))
    worker.clock = clock
    worker.live_fn = lambda venue: live
    worker.requeue_fn = published.append
    worker.refuse_fn = lambda buy, reason: refused.append((buy, reason))
    worker._quote_seen[NVDA_B["ix_accounts"][10]] = (clock.now, (8, 0, 0))
    worker.snapshot_fn = lambda: snapshots.append(_snap()) or snapshots[-1]
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_EXOTIC_REQUOTE", mode)
    original = _jup(calls, out=44_000_000)
    current_tight = False

    def provider(method, path, body=None, **kw):
        nonlocal current_tight
        if path.startswith("/quote"):
            current_tight = "maxAccounts=20" in path
        status, payload = original(method, path, body)
        if not current_tight and initial_status is not None:
            status, payload = initial_status, initial_payload
        if current_tight:
            if path.startswith("/quote"):
                payload.update(outAmount="40000000", otherAmountThreshold="39400000")
            if statuses:
                status, payload = statuses[0], None
        if payload and not path.startswith("/quote"):
            payload["swapInstruction"]["data"] = base64.b64encode(
                bytes(tight if current_tight else padded)).decode()
        if elapsed:
            clock.now = elapsed(current_tight, path)
        return status, payload

    monkeypatch.setattr(P, "_funded_jup_call", provider)
    worker.jup_call = provider
    buy = dataclasses.replace(L.launchlab_slot_buy(_routed(NVDA_B)), recv_monotonic=49.9)
    return worker, buy, calls, published, refused, snapshots


def test_a_1272_byte_leg_is_tightened_before_publish_without_renewing_origin(monkeypatch):
    admitted = []
    w, buy, calls, published, refused, snapshots = _preparation(
        monkeypatch, admission=lambda buy, quote: admitted.append(quote))
    result = w.process(buy)
    assert len(calls) == 4 and "maxAccounts=32" in calls[0][1] and "maxAccounts=20" in calls[2][1]
    assert result["status"] == "requeued" and len(published) == 1 and not refused
    prepared = published[0]
    assert dataclasses.replace(prepared, ondemand=None) == buy
    assert not prepared.ondemand.get("requote_attempts") and not prepared.ondemand.get("resized")
    assert prepared.ondemand["leg"]["out_lamports"] == 40_000_000
    assert admitted == [(8, 100 / .44), (8, 100 / .4)]
    assert len(snapshots) == 2
    message, _ = w.builders[buy.venue].build(buy, 100., _snap(),
        leg=prepared.ondemand["leg"], quote=prepared.ondemand["quote"])
    from solders.signature import Signature
    from solders.transaction import VersionedTransaction
    assert len(bytes(VersionedTransaction.populate(message, [Signature.default()]))) <= 1232


def test_a_fitting_packet_keeps_its_original_quote_without_guard_only_tightening(monkeypatch):
    shadow, shadow_buy, *_ = _preparation(monkeypatch, padded=400, live=False)
    assert len(base64.b64decode(shadow.process(shadow_buy)["unsigned_b64"])) == 1200
    w, buy, calls, published, *_ = _preparation(monkeypatch, padded=400)
    result = w.process(buy)
    assert result["status"] == "requeued" and len(calls) == 2 and len(published) == 1
    assert published[0].ondemand['leg']['out_lamports'] == 44_000_000


@pytest.mark.parametrize("padded,reason", [(472, "1272 bytes")])
def test_second_packet_overflow_is_terminal(monkeypatch, padded, reason):
    w, buy, calls, published, refused, _ = _preparation(monkeypatch, padded=padded, tight=padded)
    result = w.process(buy)
    assert result["status"] == "live_refused" and reason in result["reason"]
    assert len(calls) == 4 and not published and len(refused) == 1


def test_a_non_packet_build_refusal_never_tightens(monkeypatch):
    w, buy, calls, published, refused, _ = _preparation(monkeypatch, padded=0)
    w.snapshot_fn = lambda: SimpleNamespace(sol_usd=122.5, blockhash=None)
    result = w.process(buy)
    assert result["reason"] == "no_blockhash" and len(calls) == 2
    assert not published and len(refused) == 1


@pytest.mark.parametrize("status", [401, 429, 500, -1])
def test_provider_failure_during_the_single_retry_never_publishes(monkeypatch, status):
    w, buy, calls, published, refused, _ = _preparation(monkeypatch, statuses=[status])
    result = w.process(buy)
    assert result["status"] == "live_refused" and len(calls) == 3 and not published
    assert len(refused) == 1
    if status in (401, 429):
        assert w.stats["leg_rate_limited"] == 1 and w._backoff_until > 50


@pytest.mark.parametrize("status", [401, 429, 500, -1])
def test_initial_provider_failure_does_not_trigger_packet_repair(monkeypatch, status):
    w, buy, calls, published, refused, _ = _preparation(monkeypatch, initial_status=status)
    result = w.process(buy)
    assert result["status"] == "live_refused" and len(calls) == 1 and not published
    assert len(refused) == 1


@pytest.mark.parametrize("payload", [{"error": "NO_ROUTES_FOUND"}, {"outAmount": "1"}, None])
def test_initial_missing_or_invalid_route_does_not_trigger_packet_repair(monkeypatch, payload):
    w, buy, calls, published, refused, _ = _preparation(
        monkeypatch, initial_status=200, initial_payload=payload)
    result = w.process(buy)
    assert result["reason"] == "slot_lane_launchlab_leg_failed" and len(calls) == 1
    assert not published and len(refused) == 1


@pytest.mark.parametrize("when,expected_calls", [("before_retry", 2), ("before_publish", 4)])
def test_retry_and_publish_both_keep_the_original_500ms_age(monkeypatch, when, expected_calls):
    def elapsed(tight, path):
        return 50.4 if when == "before_retry" or tight else 50.0
    w, buy, calls, published, refused, _ = _preparation(monkeypatch, elapsed=elapsed)
    result = w.process(buy)
    assert result["reason"] == "slot_lane_exotic_aged_out"
    assert len(calls) == expected_calls and not published and len(refused) == 1


def test_an_expired_fitting_candidate_never_starts_warm_refreshes(monkeypatch):
    w, buy, calls, published, refused, _ = _preparation(
        monkeypatch, padded=0, elapsed=lambda *_: 50.4)
    w.warm = SimpleNamespace(get=lambda *a, **kw: None,
        note=lambda *a, **kw: pytest.fail("an expired candidate started warming"))
    monkeypatch.setattr(LL, "warm_legs_on", lambda: True)
    result = w.process(buy)
    assert result["reason"] == "slot_lane_exotic_aged_out" and len(calls) == 2
    assert not published and len(refused) == 1


def test_a_tightened_quote_repeats_admission_before_publish(monkeypatch):
    admitted = []
    def admission(buy, quote):
        admitted.append(quote)
        return "mcap_changed" if len(admitted) == 2 else None
    w, buy, calls, published, refused, _ = _preparation(monkeypatch, admission=admission)
    result = w.process(buy)
    assert result["reason"] == "mcap_changed" and len(calls) == 4 and not published
    assert len(admitted) == 2 and len(refused) == 1


@pytest.mark.parametrize("live,mode", [(False, "oversize"), (True, "off")])
def test_shadow_and_disabled_preparation_keep_their_existing_path(monkeypatch, live, mode):
    w, buy, calls, published, refused, _ = _preparation(monkeypatch, live=live, mode=mode)
    result = w.process(buy)
    assert len(calls) == 2
    if live:
        assert result["status"] == "requeued" and len(published) == 1 and not refused
    else:
        assert result["status"] == "shadow" and result["reason"] == "transaction is 1272 bytes, over the 1232-byte packet"
        assert not published and not refused
