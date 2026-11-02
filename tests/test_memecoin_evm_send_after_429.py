"""A state-dependent rejection after a 429 in the same send is ambiguous (review R-01; operator yes
2026-10-08).

A send that draws HTTP 429 from member A still rotates to B (the pinned-close re-offer relies on
that). But a 429 does not prove A never forwarded the bytes: a proxy can dispatch and then throttle
its reply. If A did forward, B's 'nonce too low' (our copy mined), 'replacement transaction
underpriced' (our copy is pending) or 'insufficient funds' (our copy spent them) describes OUR
transaction, and reading it as a clean rejection (False) lets a caller release a live send. Such an
answer after a 429 is now None. A deterministic rejection (bad RLP, wrong chain id) stays False, and
the same answers with no 429 before them are unchanged.
"""
import pytest

from sniper.execution import evm_real_seams as S
from test_memecoin_evm_send_never_rotates import A, B, RAW, two  # noqa: F401


def _after_429(b_answer):
    seen = []

    def http(method, url, headers=None, body=None, timeout=10):
        seen.append(url)
        if url == A:
            return 429, {"error": {"code": -32005, "message": "Too Many Requests"}}, {}
        return 200, {"jsonrpc": "2.0", "id": 1, **b_answer}, {}
    return http, seen


@pytest.mark.parametrize("message", ["nonce too low", "replacement transaction underpriced",
                                     "insufficient funds for gas * price + value",
                                     # Gamma on 9059898d2: B's own fee view, not a property of the
                                     # bytes; A may have accepted them and they can still mine.
                                     "transaction underpriced",
                                     "max fee per gas less than block base fee",
                                     "transaction type not supported"])
def test_a_state_dependent_rejection_after_a_429_is_ambiguous(message):
    http, seen = _after_429({"error": {"code": -32000, "message": message}})
    assert S._eth_send_raw(RAW, A, http) is None, "after a 429 this may describe our own copy"
    assert seen == [A, B], "the rotation is kept"


@pytest.mark.parametrize("message", ["invalid chain id", "rlp: expected input list", "invalid sender",
                                     "intrinsic gas too low", "exceeds block gas limit",
                                     "gas required exceeds allowance"])
def test_a_deterministic_rejection_after_a_429_stays_a_rejection(message):
    http, _seen = _after_429({"error": {"code": -32000, "message": message}})
    assert S._eth_send_raw(RAW, A, http) is False


def test_an_acceptance_after_a_429_is_still_an_acceptance():
    http, _seen = _after_429({"error": {"code": -32000, "message": "already known"}})
    assert S._eth_send_raw(RAW, A, http) == "already_known"


@pytest.mark.parametrize("message", ["nonce too low", "replacement transaction underpriced"])
def test_the_same_rejection_with_no_429_before_it_is_unchanged(message):
    def http(method, url, headers=None, body=None, timeout=10):
        return 200, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": message}}, {}
    assert S._eth_send_raw(RAW, A, http) is False
