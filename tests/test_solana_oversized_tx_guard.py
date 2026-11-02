"""An oversized Solana payload must never reach a provider.

Live 2026-08-23: reservations 664 (1346 bytes) and 665 (1248 bytes) were rejected by both real
RPCs with "transaction too large", but the keyless TPU sidecar only delivers datagrams and
reported ACCEPTED, so aggregate() returned ACCEPTED and each reservation held its mint and its
exposure slot for ~14 minutes until the recovery path proved the blockhash expired.
"""
import base64

from sniper.execution import submission_wiring as w
from sniper.execution.submission_transport import AttemptState, SubmissionResult


class _SpyTransport:
    def __init__(self):
        self.calls = []

    def submit(self, request):
        self.calls.append(request)
        return SubmissionResult(AttemptState.AMBIGUOUS, request.tx_identity, (), None,
                                request.signed_payload_sha256)


def _payload(nbytes):
    return base64.b64encode(b"\x01" * nbytes).decode()


def _send_fn(transport, owner_id):
    return w.make_solana_send_fn(transport, conn=None, action="entry", owner_kind="pending",
                                 owner_id=owner_id, persist=False, record=False)


def test_oversized_payload_is_refused_before_any_provider():
    transport = _SpyTransport()
    result = _send_fn(transport, 665)(_payload(w.SOLANA_PACKET_LIMIT + 1))
    # False is the seam's explicit rejection: the caller releases the reservation at once.
    assert result is False
    assert transport.calls == []


def test_limit_sized_payload_still_reaches_the_transport():
    transport = _SpyTransport()
    _send_fn(transport, 1)(_payload(w.SOLANA_PACKET_LIMIT))
    assert len(transport.calls) == 1
