"""A dropped EVM sell must be distinguishable from an unreadable provider.

`eth_confirm` returns only confirmed|failed|unknown and swallows the transport error, so the close
recovery's four `state == "not_found"` branches (two provably-dead claim releases and the
identical-bytes re-offer) were unreachable on EVM: every dropped sell parked in submitted_unknown,
"never auto-released", until a human freed it. Live case: position 49252 (PRC, 4663) pinned at
+23.6% while all three endpoints answered a clean null receipt.
"""
import pytest

# Imported inside the helper for the same reason as the dropped-close suite: a module-scope
# import of the seams pulls settings in at collection time.


def _seams():
    from sniper.execution import evm_real_seams as S
    return S


def _patch(monkeypatch, fn):
    monkeypatch.setattr(_seams(), "_rpc", fn)


def test_clean_empty_receipt_is_not_found(monkeypatch):
    _patch(monkeypatch, lambda *a, **k: {"jsonrpc": "2.0", "id": 1, "result": None})
    assert _seams().eth_receipt_state("0xabc", "http://rpc") == "not_found"


def test_unreadable_provider_stays_unknown(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("EVM RPC provider HTTP failure")
    _patch(monkeypatch, boom)
    # fail-closed: an outage must never be read as "the transaction is not there", or the recovery
    # would release a claim on a sell that may still land.
    assert _seams().eth_receipt_state("0xabc", "http://rpc") == "unknown"


def test_malformed_answer_is_not_evidence_of_absence(monkeypatch):
    _patch(monkeypatch, lambda *a, **k: {"jsonrpc": "2.0", "id": 1})      # no "result" key
    assert _seams().eth_receipt_state("0xabc", "http://rpc") == "unknown"


@pytest.mark.parametrize("status,expected", [
    ("0x1", "confirmed"), ("0x01", "confirmed"), (1, "confirmed"),
    ("0x0", "failed"), ("0x00", "failed"), (0, "failed"),
])
def test_mined_receipt_keeps_its_verdict(monkeypatch, status, expected):
    _patch(monkeypatch, lambda *a, **k: {"result": {"status": status}})
    assert _seams().eth_receipt_state("0xabc", "http://rpc") == expected


def test_the_four_recovery_answers_are_all_reachable(monkeypatch):
    """The regression itself: the recovery branches on a string the old probe never produced."""
    seen = set()
    for response in ({"result": None}, {"result": {"status": "0x1"}},
                     {"result": {"status": "0x0"}}, {}):
        _patch(monkeypatch, lambda *a, _r=response, **k: _r)
        seen.add(_seams().eth_receipt_state("0xabc", "http://rpc"))
    assert seen == {"not_found", "confirmed", "failed", "unknown"}
