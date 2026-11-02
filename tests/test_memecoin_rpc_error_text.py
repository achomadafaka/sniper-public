"""An RPC refusal must keep the node's own words.

The classifier reduced every refusal to a bare `rejected` or `unclassified_error`. Live
2026-08-23: two funded Solana entries were refused by both real RPCs and the stored rows could not
say why. The cause was "transaction too large" against the 1232-byte packet limit, and finding it
took decoding the persisted payloads because the message itself had been thrown away.
"""
from sniper.execution import evm_fast_submit as E
from sniper.execution import solana_fast_submit as S
from sniper.execution.submission_transport import AttemptState


def _http_error(message):
    def http(method, url, **kw):
        return 200, {"error": {"code": -32602, "message": message}}, None
    return http


class _Req:
    tx_identity = "sig"
    signed_payload = "AQID"


def test_solana_rejection_carries_the_node_message():
    a = S._classify_rpc(_http_error("Transaction too large: 1346 > 1232"), "u", "sendTransaction",
                        [], _Req(), "solana:custody_rpc")
    assert a.state is AttemptState.EXPLICIT_REJECTED
    assert a.reason_code.startswith("rejected: ")
    assert "too large" in a.reason_code


def test_solana_unclassified_error_carries_the_node_message():
    a = S._classify_rpc(_http_error("blockhash not found"), "u", "sendTransaction",
                        [], _Req(), "solana:custody_rpc")
    assert a.state is AttemptState.AMBIGUOUS          # state-dependent: still ambiguous
    assert "blockhash not found" in a.reason_code


def test_evm_uses_the_same_helper():
    assert S._reason_with_error("rejected", "x") == E._reason_with_error("rejected", "x")


def test_the_classification_prefix_stays_greppable():
    for fn in (S._reason_with_error, E._reason_with_error):
        assert fn("rejected", "nonce too low").split(":")[0] == "rejected"
        assert fn("unclassified_error", "boom").split(":")[0] == "unclassified_error"


def test_a_missing_message_leaves_the_bare_code():
    for fn in (S._reason_with_error, E._reason_with_error):
        assert fn("rejected", None) == "rejected"
        assert fn("rejected", "") == "rejected"
        assert fn("rejected", "   ") == "rejected"


def test_a_pathological_message_cannot_bloat_the_audit_row():
    out = S._reason_with_error("rejected", "x" * 5000)
    assert len(out) <= 160
    assert out.startswith("rejected: ")


def test_whitespace_is_collapsed_so_one_row_stays_one_line():
    assert S._reason_with_error("rejected", "  multi\n  line\terror  ") == "rejected: multi line error"
