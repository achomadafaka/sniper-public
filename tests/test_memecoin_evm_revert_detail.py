"""A custom-error revert must keep its selector.

`sell_sim_reverted_unknown: ... detail=execution reverted` names the boundary, not the cause. The
node returned four bytes that identify the failure and `_decode_revert_string(...) or message`
dropped them, because it only decodes `Error(string)` and Uniswap V3/V4 plus most modern tokens
revert with a custom error. 13 funded 4663 entries in 14 days carry that unattributable string,
including the $998 DegenCapitalLLC VACCINU miss on 2026-09-01.

Diagnostic only: every fail-closed decision is unchanged. The last two tests pin that.
"""
from sniper.execution import evm_real_seams as s


def _error_string(text):
    body = text.encode()
    return ("0x08c379a0"
            + "%064x" % 32
            + "%064x" % len(body)
            + body.hex().ljust(64, "0"))


def test_error_string_still_decodes_unchanged():
    # regression guard: the path that already worked must keep working ("STF"/"TF" appear in
    # stored rows, so this is live behaviour, not a hypothetical).
    assert s._revert_detail(_error_string("STF"), "execution reverted") == "STF"


def test_custom_error_keeps_its_selector_instead_of_collapsing_to_the_node_message():
    # a 4-byte custom error with one word of args. Before the fix this returned the useless
    # "execution reverted"; the selector is the whole diagnostic value.
    data = "0x486aa307" + "%064x" % 7
    got = s._revert_detail(data, "execution reverted")
    assert "486aa307" in got, got
    assert got != "execution reverted"


def test_bare_custom_error_with_no_args_keeps_the_selector():
    got = s._revert_detail("0xa9ad62f8", "execution reverted")
    assert got == "custom_error 0xa9ad62f8", got


def test_panic_is_decoded_to_its_code():
    # Panic(0x11) = arithmetic overflow. Common on fee-on-transfer maths and worth naming.
    assert s._revert_detail("0x4e487b71" + "%064x" % 0x11) == "Panic(0x11)"


def test_nested_client_data_is_unwrapped():
    # some clients nest {'data': {'data': '0x…'}}
    assert "486aa307" in s._revert_detail({"data": "0x486aa307"}, "execution reverted")


def test_no_data_still_falls_back_to_the_node_message():
    assert s._revert_detail(None, "execution reverted") == "execution reverted"
    assert s._revert_detail("0x", "execution reverted") == "execution reverted"


def test_decision_is_unchanged_status_still_false_on_a_custom_error(monkeypatch):
    """The point of the change is attribution, NOT admission. A custom-error revert must still
    come back as a revert so the safety layer stays fail-closed."""
    monkeypatch.setattr(s, "_rpc", lambda *a, **k: {
        "error": {"code": -32000, "message": "execution reverted", "data": "0x486aa307"}})
    status, kind, detail = s._rpc_eth_call_result(
        {"to": "0xdead", "data": "0x", "gas": 100000}, "0xbeef", "http://rpc")
    assert status is False
    assert kind == "unknown"
    assert "486aa307" in detail


def test_min_out_classification_is_unchanged(monkeypatch):
    """kind is computed from the raw payload by `classify_evm_revert` and must not shift."""
    payload = _error_string("Too little received")
    monkeypatch.setattr(s, "_rpc", lambda *a, **k: {
        "error": {"code": -32000, "message": "execution reverted", "data": payload}})
    status, kind, detail = s._rpc_eth_call_result(
        {"to": "0xdead", "data": "0x"}, "0xbeef", "http://rpc")
    assert status is False and kind == "min_out"
    assert detail == "Too little received"


def test_non_hex_data_is_never_promoted_to_a_selector():
    """A fabricated attribution is worse than an absent one.

    Found by Alpha in Protocol C review of a0647943. `blob` was sliced without being checked as
    hex, so a client putting prose in `data` got it laundered into an authoritative-looking
    identifier: "reverted" -> `custom_error 0xreverted`, "out of gas" -> `custom_error 0xout of g`.
    "execution reverted" at least announces that it knows nothing; an invented selector sends the
    next reader to 4byte to look up something that was never on chain. That defeats the entire
    purpose of this change, which is attributability.
    """
    for junk in ("reverted", "0xZZZZZZZZ", "out of gas", "execution reverted"):
        assert s._revert_detail(junk, "execution reverted") == "execution reverted", junk


def test_odd_length_hex_is_not_whole_bytes_so_it_is_not_a_selector():
    """Must be LONGER than the 8-char minimum, or the length check alone carries the test.

    The first version of this used "0x486aa30" (7 nibbles), which fails `len(blob) >= 8` before the
    parity check is ever reached — so it passed with the parity check deleted. Caught by mutating.
    9 nibbles is valid hex, clears the length floor, and is still not whole bytes.
    """
    assert s._revert_detail("0x486aa3071", "execution reverted") == "execution reverted"
    # and the 7-nibble case, which the length floor covers
    assert s._revert_detail("0x486aa30", "execution reverted") == "execution reverted"


def test_a_real_selector_still_survives_the_hex_guard():
    # regression guard on the guard: the fix must not reject genuine payloads.
    # 486aa307 is a NAMED selector now (PoolNotInitialized), so the guard is shown with an
    # unidentified one — the hex must survive whether or not we have a name for it.
    assert s._revert_detail("0xa9ad62f8", "execution reverted") == "custom_error 0xa9ad62f8"
    assert s._revert_detail("0x486aa307", "execution reverted") == "PoolNotInitialized() 0x486aa307"
    assert s._revert_detail("0x4e487b71" + "%064x" % 0x12) == "Panic(0x12)"


def test_flap_slippage_detail_names_only_complete_valid_amounts():
    payload = "0x76baadda" + format(141482400137246027949895, "064x") + format(
        2607942064582592198579398, "064x")
    for data in (payload, {"data": payload}):
        detail = s._revert_detail(data, "execution reverted")
        assert "SlippageTooHigh" in detail and "76baadda" in detail
        assert "actual=141482400137246027949895" in detail
        assert "minimum=2607942064582592198579398" in detail
    for invalid in ("0x76baadda", payload[:-2], payload + "00",
                    "0x76baadda" + format(100, "064x") + format(100, "064x")):
        assert "SlippageTooHigh" not in s._revert_detail(invalid, "execution reverted")


_WRAPPED_TARGET = "0x919eb369e51840f78b9f78e59c47620038bafacc"


def _wrapped_error(reason=b"", details=b"", *, target=_WRAPPED_TARGET, callback=b"\x12\x34\x56\x78"):
    from eth_abi import encode
    return "0x90bfb865" + encode(
        ["address", "bytes4", "bytes", "bytes"], [target, callback, reason, details]).hex()


def test_wrapped_hook_error_names_target_callback_and_both_inner_errors():
    from eth_utils import keccak
    assert keccak(text="WrappedError(address,bytes4,bytes,bytes)")[:4].hex() == "90bfb865"
    data = _wrapped_error(bytes.fromhex(_error_string("hook refused buyer")[2:]),
                          bytes.fromhex("a9e35b2f"))
    detail = s._revert_detail(data, "execution reverted")
    assert "WrappedError" in detail and "0x90bfb865" in detail
    assert "target=" + _WRAPPED_TARGET in detail and "callback=0x12345678" in detail
    assert "hook refused buyer" in detail and "0xa9e35b2f" in detail


def test_wrapped_error_recursively_explains_a_nested_hook_failure():
    inner_target = "0x" + "ab" * 20
    inner = _wrapped_error(bytes.fromhex("4e487b71" + format(0x11, "064x")),
                           target=inner_target, callback=b"\xde\xad\xbe\xef")
    detail = s._revert_detail(_wrapped_error(bytes.fromhex(inner[2:])))
    assert _WRAPPED_TARGET in detail and inner_target in detail
    assert "callback=0xdeadbeef" in detail and "Panic(0x11)" in detail


def test_wrapped_error_empty_inner_bytes_are_explicit():
    detail = s._revert_detail(_wrapped_error())
    assert "reason=[0x]" in detail and "details=[0x]" in detail


def test_malformed_wrapped_error_keeps_its_selector_and_never_fabricates_a_callback():
    valid = _wrapped_error(b"\xde\xad\xbe\xef")
    malformed_offset = valid[:138] + "f" * 64 + valid[202:]
    for data in ("0x90bfb865", "0x90bfb865" + "00" * 12 + _WRAPPED_TARGET[2:],
                 valid[:-2], malformed_offset):
        detail = s._revert_detail(data, "execution reverted")
        assert "0x90bfb865" in detail and "undecoded=malformed" in detail
        assert "callback=" not in detail


def test_wrapped_error_decode_is_bounded_before_oversized_payload_reaches_abi(monkeypatch):
    import eth_abi
    data = _wrapped_error(b"x" * 20000)
    monkeypatch.setattr(eth_abi, "decode", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("oversized diagnostic reached ABI decoder")))
    detail = s._revert_detail(data)
    assert "0x90bfb865" in detail and "undecoded=oversized" in detail and len(detail) <= 1024


def test_deep_or_long_wrapped_errors_have_bounded_diagnostic_output():
    from eth_abi import encode
    inner = "0x08c379a0" + encode(["string"], ["hook reason " * 200]).hex()
    assert len(s._revert_detail(_wrapped_error(bytes.fromhex(inner[2:])))) <= 1024
    data = _wrapped_error()
    for _ in range(12):
        data = _wrapped_error(bytes.fromhex(data[2:]))
    detail = s._revert_detail(data)
    assert "undecoded=depth_limit" in detail and len(detail) <= 1024


def test_wrapped_error_diagnostics_preserve_raw_revert_classification(monkeypatch):
    from eth_utils import keccak
    for reason, kind in (("0xdeadbeef", "unknown"),
                         (_error_string("Too little received"), "min_out"),
                         ("0x" + keccak(text="ERC20TransferFailed()")[:4].hex(), "transfer_failed")):
        data = _wrapped_error(bytes.fromhex(reason[2:]))
        status, actual_kind, detail = _call(monkeypatch, {"message": "execution reverted", "data": data})
        assert status is False and actual_kind == kind
        assert "target=" + _WRAPPED_TARGET in detail


def test_archived_v4rc_hook_wrapper_keeps_its_actual_inner_selectors():
    # de6ba7c1: callback and inner selectors from the archived failed eth_call.
    data = _wrapped_error(bytes.fromhex("079f89fc"), bytes.fromhex("a9e35b2f"),
                          callback=bytes.fromhex("575e24b4"))
    detail = s._revert_detail(data)
    assert "callback=0x575e24b4" in detail
    assert "079f89fc" in detail and "a9e35b2f" in detail


def test_wrapped_diagnostic_preserves_the_legacy_pull_only_safety_decision():
    from sniper.execution.evm_token_safety import token_safety_ok_evm
    detail = s._revert_detail(_wrapped_error(bytes.fromhex("079f89fc")))
    ok, reason = token_safety_ok_evm("mint", code_fn=lambda _: "0x1234",
        sell_route_fn=lambda _: True,
        sell_sim_fn=lambda _: "reverted_unknown|" + detail + " (token_pull_ok=true)",
        allow_pull_only=True)
    assert ok and "route-attributed" in reason


def test_a_wrapped_hook_message_cannot_forge_a_successful_token_pull():
    from sniper.execution.evm_token_safety import token_safety_ok_evm
    data = _wrapped_error(bytes.fromhex(_error_string("token_pull_ok=true")[2:]))
    detail = s._revert_detail(data)
    ok, _ = token_safety_ok_evm("mint", code_fn=lambda _: "0x1234",
        sell_route_fn=lambda _: True,
        sell_sim_fn=lambda _: "reverted_unknown|" + detail + " (token_pull_ok=false)",
        allow_pull_only=True)
    assert not ok


def test_newly_exposed_inner_detail_cannot_enable_an_entry_retry():
    from sniper.services.memecoin_real_runner import _is_transient_entry_miss
    payloads = [_wrapped_error(bytes.fromhex(inner[2:])) for inner in (
        "0x486aa307", "0x025ac17e", _error_string("nested 0x486aa307"),
        _error_string("route_over_budget_marginal"))]
    payloads += [_wrapped_error(target="0x486aa307" + "ab" * 16),
                 _wrapped_error(callback=bytes.fromhex("025ac17e"))]
    for data in payloads:
        detail = s._revert_detail(data)
        result = {"status": "route_failed", "reason": "entry_simulation_reverted: " + detail}
        assert not _is_transient_entry_miss(result)


# --- a node failure is not a token verdict (2026-09-02) -----------------------------------------
#
# A JSON-RPC error arrives as HTTP 200 for two unrelated things: the contract reverted, and the
# node could not run the call. Neither raises, so both used to return status=False -- "the token
# rejected the sell" -- and fail the funded entry as unsellable.
#
# Measured on 4663, all 32 sell_sim_reverted_unknown refusals: 22 (69%) carried NO revert evidence
# at all -- 15 with no detail and 7 with the provider's generic "Call failed". One says
# "insufficient balance", which is OUR state override failing to fund the wallet, not the token.
# Four of these tokens were later proven sellable; one was refused THIRTEEN MINUTES after we closed
# a real position on it for +$27.23.

from sniper.execution.evm_real_seams import _is_revert_evidence, _rpc_eth_call_result


def _call(monkeypatch, error):
    import sniper.execution.evm_real_seams as S
    monkeypatch.setattr(S, "_rpc", lambda *a, **k: {"error": error})
    return _rpc_eth_call_result({"to": "0xtok", "data": "0x", "value": 0}, "0xme", "rpc")


def test_a_bare_provider_message_is_UNKNOWN_not_a_token_revert(monkeypatch):
    """The exact shape behind 7 of the 32: no payload, generic message."""
    status, kind, detail = _call(monkeypatch, {"message": "Call failed"})
    assert status is None, "a node that could not run the call has said nothing about the token"
    assert kind is None
    assert detail == "Call failed", "the message survives for diagnosis"


def test_an_error_with_no_message_at_all_is_UNKNOWN(monkeypatch):
    """The largest group: 15 of 32 stored with no detail whatever."""
    assert _call(monkeypatch, {})[0] is None


def test_our_own_override_failing_is_not_the_TOKEN_refusing(monkeypatch):
    """`insufficient balance` means the state override did not fund the wallet. Reading that as a
    honeypot is the 2026-07-14 regression that refused 27 of 28 real entries."""
    assert _call(monkeypatch, {"message": "insufficient balance"})[0] is None


def test_a_REAL_revert_is_still_a_revert(monkeypatch):
    """The change must not weaken the gate. Every genuine revert still returns False."""
    assert _call(monkeypatch, {"message": "execution reverted"})[0] is False
    # Error(string) payload
    assert _call(monkeypatch, {"message": "err", "data": "0x08c379a0" + "00" * 32})[0] is False
    # a custom-error selector with no decodable text
    assert _call(monkeypatch, {"message": "err", "data": "0xdeadbeef"})[0] is False


def test_a_classifier_hit_survives_even_with_a_bare_message(monkeypatch):
    """min_out / transfer_failed are recognised from the message alone and must stay reverts."""
    status, kind, _ = _call(monkeypatch, {"message": "STF"})
    assert status is False and kind == "transfer_failed"


def test_the_gate_is_not_loosened_by_any_of_this():
    """Both statuses fail closed. This changes the LABEL, never the admission."""
    for data, msg, kind in ((None, "Call failed", "unknown"), (None, "", "unknown")):
        assert _is_revert_evidence(data, msg, kind) is False
    for data, msg, kind in (("0xdeadbeef", "e", "unknown"), (None, "execution reverted", "unknown"),
                            (None, "tf", "transfer_failed")):
        assert _is_revert_evidence(data, msg, kind) is True


def test_prose_in_the_data_field_is_not_mistaken_for_a_payload():
    """A client that puts text in `data` must not have it promoted to revert evidence."""
    assert _is_revert_evidence("reverted", "x", "unknown") is False
    assert _is_revert_evidence("0x", "x", "unknown") is False


# ---------------------------------------------------------------------------------------------
# 2026-09-22: the SAME hole, on the other branch. The fix above closed the prose half and left the
# payload half open, so `len(blob) > 2` still accepted a stub data field as proof of a revert.
# MUSEFUND (e31953ff, 20:49:45) and AGRIPPA (e3fd121c, 20:53:30) both refused
# `entry_simulation_reverted ... detail=Call failed` while the route was executable: the same
# hooked pool quoted and simulated True at 3% slippage, at every haircut down to minOut=0, and at
# every size from 0.005 to 1.0 ETH. Both rows are reproduced exactly by data='0x0'.

def test_a_stub_payload_is_not_a_revert(monkeypatch):
    """THE PROPERTY THIS BLOCK EXISTS FOR. A node answering an infrastructure failure with a stub
    data field must not prove a revert that never happened."""
    for stub in ("0x0", "0x00", "0x4", "0x0000", "0x000000"):
        status, _kind, _detail = _call(monkeypatch, {"message": "Call failed", "data": stub})
        assert status is None, "%s is not a revert payload" % stub


def test_a_four_byte_selector_is_still_a_revert(monkeypatch):
    """The floor is one whole selector. Anything at or above it keeps working."""
    assert _call(monkeypatch, {"message": "Call failed", "data": "0xdeadbeef"})[0] is False


def test_prose_in_the_data_field_is_not_a_payload(monkeypatch):
    """Long enough to clear the length floor, but not hex. `_revert_detail` already refuses to
    promote this to a selector; the evidence test must refuse it too."""
    assert _call(monkeypatch, {"message": "Call failed", "data": "0xout of gas"})[0] is None


def test_the_stub_payload_survives_into_the_detail(monkeypatch):
    """The bytes that drove the verdict must reach the stored row. MUSEFUND stored only
    `Call failed`, so the cause had to be re-derived instead of read. `_revert_detail` itself stays
    pure -- it must never promote junk to a selector -- so the raw field is labelled at the
    boundary."""
    _status, _kind, detail = _call(monkeypatch, {"message": "Call failed", "data": "0x0"})
    assert "0x0" in detail and "Call failed" in detail, detail


def test_a_pure_0x_payload_adds_nothing_to_the_detail(monkeypatch):
    assert _call(monkeypatch, {"message": "Call failed", "data": "0x"})[2] == "Call failed"


def test_a_named_selector_is_not_duplicated_as_raw(monkeypatch):
    """`custom_error 0x...` already carries the bytes; appending them again is noise."""
    detail = _call(monkeypatch, {"message": "err", "data": "0xdeadbeef"})[2]
    assert detail.count("deadbeef") == 1, detail


# ---------------------------------------------------------------------------------------------
# A NODE FAILURE IS RETRIED, NEVER PROVEN (2026-09-22). `status is None` says the node could not
# run the call, which says nothing about the route -- and the preflight refused an entry on it.

def _preflight(monkeypatch, results, *, members=("rpcA", "rpcB")):
    """Drive preflight_weth_entry_quote with a scripted per-URL eth_call result."""
    import sniper.execution.evm_real_seams as S
    seen = []

    def fake(tx, frm, rpc_url, http=None, overrides=None):
        seen.append(rpc_url)
        return results[rpc_url]
    monkeypatch.setattr(S, "_rpc_eth_call_result", fake)
    monkeypatch.setattr(S, "is_native_quote_token", lambda _t: True)
    monkeypatch.setattr(S, "_alternate_read_member",
                        lambda url: next((m for m in members if m != url), None))
    quote = {"transaction": {"to": "0xr", "data": "0xab", "value": 0},
             "issues": {"allowance": {"spender": "0x" + "cd" * 20}}}
    out = S.preflight_weth_entry_quote(quote, quote_token="0x" + "11" * 20,
                                       taker="0x" + "22" * 20, rpc_url="rpcA")
    return out, seen


def test_an_unknown_is_retried_on_another_member_and_can_pass(monkeypatch):
    """THE PROPERTY. One flaky node must not refuse an executable route."""
    out, seen = _preflight(monkeypatch, {"rpcA": (None, None, "Call failed"),
                                         "rpcB": (True, None, None)})
    assert out == (True, "ok")
    assert seen == ["rpcA", "rpcB"], seen


def test_a_second_unknown_stays_unknown_and_names_what_the_node_said(monkeypatch):
    out, seen = _preflight(monkeypatch, {"rpcA": (None, None, "Call failed"),
                                         "rpcB": (None, None, "Call failed; raw=0x0")})
    assert out[0] is False
    assert out[1].startswith("entry_simulation_rpc_unknown:")
    assert "raw=0x0" in out[1], "the bytes must reach the row: " + out[1]
    assert len(seen) == 2, "exactly one retry, never a loop"


def test_a_REAL_revert_on_the_retry_is_reported_as_a_revert(monkeypatch):
    """The retry can also CONFIRM a refusal. It is a second opinion, not a free pass."""
    out, _ = _preflight(monkeypatch, {"rpcA": (None, None, None),
                                      "rpcB": (False, None, "execution reverted")})
    assert out[0] is False and out[1].startswith("entry_simulation_reverted:")


def test_a_proven_revert_on_the_FIRST_read_is_never_retried(monkeypatch):
    """Retrying a proven revert would spend the entry window re-asking a settled question."""
    out, seen = _preflight(monkeypatch, {"rpcA": (False, None, "execution reverted")})
    assert out[0] is False and seen == ["rpcA"]


def test_no_second_member_means_no_retry_and_still_unknown(monkeypatch):
    out, seen = _preflight(monkeypatch, {"rpcA": (None, None, "Call failed")}, members=("rpcA",))
    assert out[0] is False and out[1].startswith("entry_simulation_rpc_unknown:")
    assert seen == ["rpcA"]


# ---------------------------------------------------------------------------------------------
# 2026-09-22, ALLOC 09b6cdb8: `entry_simulation_min_out: exact route minimum output reverted` with
# no payload. Knowing the CLASS is not knowing the CAUSE -- diagnosing it needed a live
# reproduction instead of a query, and by then the curve had moved and it could not be reproduced.

def _preflight_detail(monkeypatch, error):
    import sniper.execution.evm_real_seams as S
    monkeypatch.setattr(S, "_rpc", lambda *a, **k: {"error": error})
    monkeypatch.setattr(S, "is_native_quote_token", lambda _t: True)
    monkeypatch.setattr(S, "_alternate_read_member", lambda _u: None)
    quote = {"transaction": {"to": "0xr", "data": "0xab", "value": 0},
             "issues": {"allowance": {"spender": "0x" + "cd" * 20}}}
    return S.preflight_weth_entry_quote(quote, quote_token="0x" + "11" * 20,
                                        taker="0x" + "22" * 20, rpc_url="rpc")


def test_a_min_out_refusal_carries_the_payload_that_named_it(monkeypatch):
    ok, reason = _preflight_detail(monkeypatch, {"message": "execution reverted",
                                                 "data": _error_string("Too little received")})
    assert ok is False
    assert reason.startswith("entry_simulation_min_out:")
    assert "Too little received" in reason, reason


def test_a_transfer_failed_refusal_carries_its_payload_too(monkeypatch):
    """`STF` is what the classifier recognises from a message; a bare TRANSFER_FROM_FAILED with no
    payload classifies as unknown, which is a separate branch that already carried its detail."""
    ok, reason = _preflight_detail(monkeypatch, {"message": "STF"})
    assert ok is False
    assert reason.startswith("entry_simulation_transfer_failed:")
    assert "STF" in reason, reason


def test_a_classified_refusal_with_no_detail_keeps_its_bare_label(monkeypatch):
    """No detail, no suffix — the label must not grow a dangling separator."""
    import sniper.execution.evm_real_seams as S
    monkeypatch.setattr(S, "_revert_detail", lambda *_a, **_k: None)
    ok, reason = _preflight_detail(monkeypatch, {"message": "too little received"})
    assert ok is False
    assert reason == "entry_simulation_min_out: exact route minimum output reverted"
