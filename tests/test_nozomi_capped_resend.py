"""Capped Nozomi posts once per retained entry identity; free-direct still races and resends."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading

import pytest
from solders.keypair import Keypair

from sniper.execution import solana_sender_ab as S
from sniper.execution.submission_transport import (
    AttemptState, SubmissionAttempt, SubmissionTransport, aggregate,
)
from test_memecoin_slot_lane_sender_ab import _SenderHttp, _request, _signed


URL = "https://fra2.nozomi.temporal.xyz/?c=private-test-key"


def request(*, tip=1_000_000):
    kp = Keypair()
    ixs = S.tip_instructions(kp.pubkey(), (S.NOZOMI_TIP_ACCOUNTS[0], tip)) if tip else []
    return _request(_signed(kp, ixs))


def provider(http, **kwargs):
    return S.NozomiProvider(http, url=URL, dynamic_tips=True, **kwargs)


def test_concurrent_repeat_does_not_post_or_replace_pending_outcome(caplog):
    posted, release = threading.Event(), threading.Event()

    class BlockedHttp(_SenderHttp):
        def __call__(self, *args, **kwargs):
            posted.set()
            assert release.wait(2)
            return super().__call__(*args, **kwargs)

    http, req = BlockedHttp(), request()
    nozomi = provider(http)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(nozomi.submit, req)
        try:
            assert posted.wait(1)
            pending = nozomi.outcome(req.tx_identity)
            assert pending["nozomi_state"] == "pending"
            repeat = pool.submit(nozomi.submit, req).result(timeout=1)
            assert repeat.state is AttemptState.NOT_SENT
            assert repeat.reason_code == "already_dispatched"
            assert nozomi.outcome(req.tx_identity) == pending
        finally:
            release.set()
        assert first.result(timeout=1).state is AttemptState.ACCEPTED
    assert len(http.calls) == 1
    assert http.calls[0][2]["params"][0] == req.signed_payload
    assert URL not in caplog.text and "private-test-key" not in caplog.text


@pytest.mark.parametrize("answer,state", [
    ("echo", AttemptState.ACCEPTED),
    (OSError("timeout"), AttemptState.AMBIGUOUS),
    ({"error": {"code": -32002, "message": "signature verification failure"}},
     AttemptState.EXPLICIT_REJECTED),
])
def test_repeat_after_first_outcome_is_neutral_and_retains_first_evidence(answer, state):
    http, req = _SenderHttp(answer=answer), request()
    nozomi = provider(http)
    first = nozomi.submit(req)
    assert first.state is state
    evidence = nozomi.outcome(req.tx_identity)
    for _ in range(2):
        repeat = nozomi.submit(req)
        assert repeat.state is AttemptState.NOT_SENT
        assert repeat.reason_code == "already_dispatched"
        assert nozomi.outcome(req.tx_identity) == evidence
    assert evidence["nozomi_sha256"] == req.signed_payload_sha256
    assert len(http.calls) == 1
    # All-neutral repeats never invent a safe release after an unknown submission.
    assert aggregate(req, [repeat]).state is AttemptState.AMBIGUOUS


def test_same_identity_different_bytes_refuses_without_replacing_evidence():
    http, req = _SenderHttp(), request()
    nozomi = provider(http)
    nozomi.submit(req)
    evidence = nozomi.outcome(req.tx_identity)
    changed = replace(request(), tx_identity=req.tx_identity)
    repeat = nozomi.submit(changed)
    assert repeat.state is AttemptState.NOT_SENT
    assert repeat.reason_code == "identity_payload_mismatch"
    assert nozomi.outcome(req.tx_identity) == evidence
    assert len(http.calls) == 1


@pytest.mark.parametrize("action", ["entry", "exit"])
def test_untipped_payload_does_not_reserve_paid_identity(action):
    http, req = _SenderHttp(), request()
    nozomi = provider(http)
    untipped = replace(request(tip=0), action=action, tx_identity=req.tx_identity)
    assert nozomi.submit(untipped).reason_code == "untipped"
    assert nozomi.outcome(req.tx_identity) is None
    assert nozomi.submit(req).state is AttemptState.ACCEPTED
    assert len(http.calls) == 1


def test_legacy_fixed_tip_repeats_keep_existing_behavior():
    http, req = _SenderHttp(), request(tip=5_000_000)
    nozomi = S.NozomiProvider(http, url=URL)
    assert nozomi.submit(req).state is AttemptState.ACCEPTED
    assert nozomi.submit(req).state is AttemptState.ACCEPTED
    assert len(http.calls) == 2


def test_tipped_exit_repeats_keep_existing_behavior():
    http, req = _SenderHttp(), replace(request(), action="exit")
    nozomi = provider(http)
    assert nozomi.submit(req).state is AttemptState.ACCEPTED
    assert nozomi.submit(req).state is AttemptState.ACCEPTED
    assert len(http.calls) == 2


def test_guard_uses_existing_bounded_outcome_map():
    http = _SenderHttp()
    nozomi = provider(http, cap=2)
    requests = [request() for _ in range(3)]
    for req in requests:
        nozomi.submit(req)
    assert len(nozomi._outcomes) == 2
    assert nozomi.outcome(requests[0].tx_identity) is None
    assert nozomi.submit(requests[-1]).reason_code == "already_dispatched"
    assert len(http.calls) == 3


def test_first_nozomi_post_still_races_tpu_and_finishes_after_accept():
    posted, release = threading.Event(), threading.Event()

    class BlockedHttp(_SenderHttp):
        def __call__(self, *args, **kwargs):
            posted.set()
            assert release.wait(2)
            return super().__call__(*args, **kwargs)

    class Tpu:
        provider_id = "solana:tpu"
        calls = 0

        def submit(self, req):
            self.calls += 1
            assert posted.wait(1)
            return SubmissionAttempt(self.provider_id, AttemptState.ACCEPTED,
                                     req.tx_identity, 0.0, None)

    http, req = BlockedHttp(), request()
    nozomi = provider(http)
    tpu = Tpu()
    transport = SubmissionTransport([tpu, nozomi], deadline_s=1)
    try:
        result = transport.submit(req)
        assert result.accepted_by == "solana:tpu"
        assert nozomi.send_after_accept is True
        release.set()
        for future in result.late_results:
            assert future.result(timeout=1).state is AttemptState.ACCEPTED
        evidence = nozomi.outcome(req.tx_identity)
        assert evidence["nozomi_state"] == "accepted"
        assert evidence["nozomi_returned_sig"] == req.tx_identity
        again = transport.submit(req)
        assert again.accepted_by == "solana:tpu" and tpu.calls == 2
        for future in again.late_results:
            assert future.result(timeout=1).state is AttemptState.NOT_SENT
        assert nozomi.outcome(req.tx_identity) == evidence
        assert len(http.calls) == 1
    finally:
        release.set()
        transport.close()
