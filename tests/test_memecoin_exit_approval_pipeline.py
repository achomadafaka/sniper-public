"""The EXIT approval pipelines the same way the ENTRY one has since July.

Measured on the 4663 exit stage map: exits needing an approval ran p50 6.0s against 2.3s for those
that did not, and the approve stage alone was p50 2,482ms -- 41% of an approving exit, on 44% of
exits. The entry used `_approve` (pipelined); the exit used a separate `_approve_token` that always
waited for `eth_confirm`. One path got the fix and the other did not.
"""
import inspect

import pytest

from sniper.execution import evm_real_seams as seams
from test_memecoin_bsc_signing_reads import entry_seams

SRC = inspect.getsource(seams.build_real_evm_executor)


def test_the_exit_approver_delegates_rather_than_duplicating():
    """A second copy is how the two drifted for two months."""
    body = SRC[SRC.index("def _approve_token("):]
    body = body[:body.index("\n    def ", 1)]
    assert "return _approve(" in body, "the exit approver must delegate to the pipelined one"
    assert "eth_confirm" not in body, (
        "the exit approver still waits for a receipt; that is the 2.5s this removes")


def test_the_shared_approver_still_honours_the_pipeline_flag():
    body = SRC[SRC.index("def _approve("):]
    body = body[:body.index("\n    def ", 1)]
    assert "_approval_pipelined()" in body and "_floored_nonce()" in body


def test_a_pipelined_approval_proceeds_ONLY_on_positive_acceptance():
    """Fail-closed rule from the 2026-07-23 review: `False` (rejected) and `None` (transport
    ambiguity) must BOTH abort. Broadcasting a swap on a maybe-missing approval queues it behind a
    nonce gap, and a later transaction at that nonce can unlock it unexpectedly."""
    body = SRC[SRC.index("def _approve("):]
    body = body[:body.index("\n    def ", 1)]
    assert "isinstance(sent, str) and sent" in body, (
        "a truthy-string check is what distinguishes accepted from ambiguous")


def test_a_proven_rejection_releases_the_floored_nonce():
    """Otherwise the reserved nonce leaks and every later signature is off by one."""
    body = SRC[SRC.index("def _approve("):]
    body = body[:body.index("\n    def ", 1)]
    assert "_release_floored_nonce(nonce)" in body


def test_the_token_argument_defaults_to_the_quote_token():
    """The entry approves the quote token and passes no `token`; changing that default would
    silently make every entry approve the wrong asset."""
    body = SRC[SRC.index("def _approve("):]
    body = body[:body.index("\n    def ", 1)]
    assert "(token or quote_token)" in body


@pytest.mark.parametrize("chain,quote_token", [
    ("8453", "0x4200000000000000000000000000000000000006"),
    ("4663", "0x0bd7d308f8e1639fab988df18a8011f41eacad73"),
])
@pytest.mark.parametrize("acceptance", ["accepted", False, None])
def test_the_entry_still_calls_it_without_a_token(monkeypatch, entry_seams, chain, quote_token,
                                                 acceptance):
    signed, sent = [], []
    monkeypatch.setattr(seams, "eth_nonce", lambda *_a, **_kw: 5)
    monkeypatch.setattr(seams, "eth_gas_price", lambda *_a, **_kw: 100)
    monkeypatch.setattr(seams, "sign_tx", lambda tx, _acct, **_kw:
                        (signed.append(tx), ("raw", "hash"))[1])
    _ex, entry = entry_seams(chain)
    monkeypatch.setattr(seams, "eth_send_raw", lambda raw, *_a, **_kw:
                        (sent.append(raw), acceptance)[1])
    approved = entry["approve_fn"]("0x" + "33" * 20, 123)
    assert signed[0]["to"].lower() == quote_token
    assert signed[0]["data"] == "0x095ea7b3" + "0" * 24 + "33" * 20 + f"{123:064x}"
    assert sent == ["raw"]
    # Base requires the confirmed receipt supplied by the fixture; 4663 requires acceptance.
    if chain == "4663" and acceptance == "accepted":
        assert approved == {"pipelined": True, "nonce": 5, "tx_hash": "hash"}
    else:
        assert approved is (chain == "8453" and acceptance is not False)
