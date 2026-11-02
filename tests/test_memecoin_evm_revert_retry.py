"""Router revert decoding + bounded min-out retry for real EVM entries (2026-07-17).

Three real 4663 entries reverted 'Too little received' during violent pumps: the pool moved
13.6%/3.5% between quote and landing, so the swap's amountOutMinimum was stale. That is QUOTE
MOVEMENT, not a honeypot or a generic failure. Contract:

  * classify_evm_revert() recognizes the V3 SwapRouter02 / V2 Router02 / V4 router min-out
    signatures; anything else is 'unknown'.
  * evm_revert_reason() replays a mined-reverted tx via eth_call at its block; fail-closed to
    ('unknown', None) on any transport/parse problem.
  * execute_entry_evm retries ONCE with a FRESH quote ONLY for a proven min-out revert —
    never for unknown reverts, never more than once. The reservation is KEPT through the
    retry and released on the second failure.
  * after an approval confirms, the entry quote is refreshed before signing; a changed sell
    amount or spender aborts fail-closed.
  * terminal reasons are specific: 'reverted_min_out' / 'reverted_unknown', never bare 'failed'.
"""
import json
import sqlite3
from types import SimpleNamespace

import pytest
from eth_utils import keccak

from sniper.execution.evm_real_seams import classify_evm_revert, evm_revert_reason
from sniper.execution.evm_swap_executor import execute_entry_evm
from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_entry_trace import EntryAttemptTrace


# --- classification ------------------------------------------------------------------------------

def _error_string_data(text):
    """ABI-encode Error(string) revert data the way an RPC returns it."""
    payload = text.encode()
    padded = payload.hex() + "0" * ((32 - len(payload) % 32) % 32 * 2)
    return ("0x08c379a0" + format(0x20, "064x") + format(len(payload), "064x") + padded)


def test_v3_too_little_received_is_min_out():
    assert classify_evm_revert(_error_string_data("Too little received")) == "min_out"


def test_relay_return_amount_is_not_enough_is_min_out():
    assert classify_evm_revert(_error_string_data("Return amount is not enough")) == "min_out"


def test_v2_insufficient_output_amount_is_min_out():
    data = _error_string_data("UniswapV2Router: INSUFFICIENT_OUTPUT_AMOUNT")
    assert classify_evm_revert(data) == "min_out"


def test_v4_too_little_received_selector_is_min_out():
    sel = keccak(b"V4TooLittleReceived(uint256,uint256)")[:4].hex()
    data = "0x" + sel + format(100, "064x") + format(90, "064x")
    assert classify_evm_revert(data) == "min_out"


def test_relay_too_much_slippage_selector_is_min_out():
    """Relay uses this custom error when output moves below its minimum."""
    data = (
        "0x97a6f3b9"
        "000000000000000000000000547f9035360552f4eba72a409caafeca1cf02f82"
        "00000000000000000000000000000000000000000005b4ea5b78b0cdf609e177"
        "000000000000000000000000000000000000000000055fb94e8c5905c1011ec3"
    )

    assert classify_evm_revert(data) == "min_out"


def test_unknown_revert_is_unknown():
    assert classify_evm_revert(_error_string_data("TRANSFER_FAILED")) == "unknown"
    assert classify_evm_revert("0x") == "unknown"
    assert classify_evm_revert(None) == "unknown"


def test_message_only_classification():
    assert classify_evm_revert(None, "execution reverted: Too little received") == "min_out"
    assert classify_evm_revert(
        None, "execution reverted: ERR_EXCEEDED_SLIPPAGE:minReturn not reached",
    ) == "min_out"
    assert classify_evm_revert(None, "execution reverted") == "unknown"


def test_uniswap_tf_is_a_transfer_failure():
    assert classify_evm_revert(None, "execution reverted: TF") == "transfer_failed"


_FLAP_ACTUAL = 141482400137246027949895
_FLAP_MINIMUM = 2607942064582592198579398
_FLAP_SLIPPAGE = "0x76baadda" + format(_FLAP_ACTUAL, "064x") + format(_FLAP_MINIMUM, "064x")


def test_flap_slippage_error_is_min_out_from_verified_abi_arguments():
    assert keccak(b"SlippageTooHigh(uint256,uint256)")[:4].hex() == "76baadda"
    assert classify_evm_revert(_FLAP_SLIPPAGE) == "min_out"
    assert classify_evm_revert({"data": _FLAP_SLIPPAGE}) == "min_out"


@pytest.mark.parametrize("payload", [
    "0x76baadda", _FLAP_SLIPPAGE[:-2], _FLAP_SLIPPAGE[:-1], _FLAP_SLIPPAGE + "00",
    _FLAP_SLIPPAGE[:20] + "zz" + _FLAP_SLIPPAGE[22:],
    _FLAP_SLIPPAGE.replace("0x", "", 1),
    "0x76baadda" + format(100, "064x") + format(100, "064x"),
    "0x76baadda" + format(101, "064x") + format(100, "064x"),
    "0x76baadda" + format(0, "064x") + format(0, "064x"),
    None, True, 76, [_FLAP_SLIPPAGE], {"other": _FLAP_SLIPPAGE},
    {"data": {"data": _FLAP_SLIPPAGE}},
])
def test_flap_slippage_needs_canonical_amounts_before_retry(payload):
    assert classify_evm_revert(payload, "execution reverted: SlippageTooHigh") == "unknown"


def test_flap_zero_output_below_positive_minimum_is_min_out():
    assert classify_evm_revert("0x76baadda" + "0" * 64 + format(1, "064x")) == "min_out"


@pytest.mark.parametrize("payload, expected", [
    ("0x76baadda" + format(0xf27f64e4, "064x") + format(0xf27f64e5, "064x"), "min_out"),
    ("0x76baadda" + format(0xf27f64e4, "064x") + format(0xf27f64e4, "064x"), "unknown"),
    ("0x76baadda" + format(0x8b063d74, "064x") + format(0x8b063d73, "064x"), "unknown"),
    ("0x76baadda" + format(0x8b063d73, "064x"), "unknown"),
    (_FLAP_SLIPPAGE + "8b063d73", "unknown"),
])
def test_flap_selector_owns_its_arguments_before_legacy_substring_markers(payload, expected):
    # These words contain ERC20TransferFailed/V4TooLittleReceived selectors as integers.
    assert classify_evm_revert(payload, "execution reverted") == expected


def test_flap_shape_cannot_fall_back_to_a_generic_minimum_message():
    assert classify_evm_revert("0x76baadda", "execution reverted: Too little received") == "unknown"


def test_flap_selector_embedded_in_another_error_remains_unknown():
    assert classify_evm_revert("0xdeadbeef" + _FLAP_SLIPPAGE[2:]) == "unknown"


def test_flap_uppercase_hex_retains_the_same_abi_amounts():
    assert classify_evm_revert(_FLAP_SLIPPAGE.upper()) == "min_out"


# --- replay fetch --------------------------------------------------------------------------------

def _replay_http(*, error=None, tx_missing=False):
    import json as _json

    def http(method, url, headers=None, body=None, timeout=10):
        req = _json.loads(body)
        meth = req["method"]
        if meth == "eth_getTransactionByHash":
            if tx_missing:
                return 200, {"result": None}, None
            return 200, {"result": {"from": "0xus", "to": "0xrouter", "input": "0xdead",
                                    "value": "0x0", "gas": "0x7a120"}}, None
        if meth == "eth_getTransactionReceipt":
            return 200, {"result": {"status": "0x0", "blockNumber": "0x10"}}, None
        if meth == "eth_call":
            if error is not None:
                return 200, {"error": error}, None
            return 200, {"result": "0x"}, None
        return 200, {}, None
    return http


def test_replay_decodes_min_out():
    err = {"code": 3, "message": "execution reverted",
           "data": _error_string_data("Too little received")}
    kind, detail = evm_revert_reason("0xhash", "https://rpc", http=_replay_http(error=err))
    assert kind == "min_out"
    assert "Too little received" in (detail or "")
    assert "replay_basis=block_end" in detail


def test_replay_uses_verified_block_prefix_when_end_of_block_succeeds():
    import json as _json

    methods = []
    selector = keccak(b"V4TooLittleReceived(uint256,uint256)")[:4].hex()
    revert_data = "0x" + selector + format(100, "064x") + format(90, "064x")
    prior_log = {"address": "0xpool", "topics": ["0xtopic"], "data": "0x01"}
    target = {
        "hash": "0xhash", "blockHash": "0xblock", "transactionIndex": "0x2",
        "from": "0xus", "to": "0xrouter", "input": "0xdead", "value": "0x0",
        "gas": "0x7a120", "gasPrice": "0x1",
    }
    prior = {
        "hash": "0xprior", "transactionIndex": "0x1", "from": "0xthem",
        "to": "0xpool", "input": "0xbeef", "value": "0x0", "gas": "0x10000",
        "gasPrice": "0x1",
    }

    def http(method, url, headers=None, body=None, timeout=10):
        request = _json.loads(body)
        methods.append(request["method"])
        if request["method"] == "eth_getTransactionByHash":
            return 200, {"result": target}, None
        if request["method"] == "eth_getTransactionReceipt":
            return 200, {"result": {
                "transactionHash": "0xhash", "status": "0x0", "blockNumber": "0x10",
                "blockHash": "0xblock", "logs": [],
            }}, None
        if request["method"] == "eth_call":
            return 200, {"result": "0x"}, None
        if request["method"] == "eth_getBlockByHash":
            return 200, {"result": {
                "number": "0x10", "timestamp": "0x20", "gasLimit": "0x1000000",
                "baseFeePerGas": "0x1", "transactions": [
                    {"hash": "0xsystem", "type": "0x6a", "gas": "0x0"}, prior, target,
                ],
            }}, None
        if request["method"] == "eth_getBlockReceipts":
            return 200, {"result": [
                {"transactionHash": "0xprior", "status": "0x1", "logs": [prior_log]},
                {"transactionHash": "0xhash", "status": "0x0", "logs": []},
            ]}, None
        if request["method"] == "eth_simulateV1":
            simulation, parent = request["params"]
            state_call = simulation["blockStateCalls"][0]
            assert parent == "0xf"
            assert state_call["blockOverrides"]["time"] == "0x20"
            assert len(state_call["calls"]) == 2
            return 200, {"result": [{"calls": [
                {"status": "0x1", "logs": [prior_log]},
                {"status": "0x0", "logs": [], "error": {
                    "code": 3, "message": "execution reverted", "data": revert_data,
                }},
            ]}]}, None
        raise AssertionError(request)

    kind, _detail = evm_revert_reason("0xhash", "https://rpc", http=http)

    assert kind == "min_out"
    assert "replay_basis=verified_transaction_prefix" in _detail
    assert methods[-3:] == ["eth_getBlockByHash", "eth_getBlockReceipts", "eth_simulateV1"]


def test_replay_unknown_revert():
    err = {"code": 3, "message": "execution reverted",
           "data": _error_string_data("PAUSED")}
    kind, _ = evm_revert_reason("0xhash", "https://rpc", http=_replay_http(error=err))
    assert kind == "unknown"


def test_replay_transport_failure_is_unknown():
    def http(*_a, **_k):
        raise RuntimeError("rpc down")
    assert evm_revert_reason("0xhash", "https://rpc", http=http) == ("unknown", None)


def test_replay_missing_tx_is_unknown():
    kind, detail = evm_revert_reason("0xhash", "https://rpc", http=_replay_http(tx_missing=True))
    assert kind == "unknown"


def test_flap_slippage_replay_retains_actual_and_minimum_amounts():
    error = {"code": 3, "message": "execution reverted", "data": _FLAP_SLIPPAGE}
    kind, detail = evm_revert_reason("0xhash", "https://rpc", http=_replay_http(error=error))
    assert kind == "min_out"
    assert "SlippageTooHigh" in detail and "76baadda" in detail
    assert "actual=141482400137246027949895" in detail
    assert "minimum=2607942064582592198579398" in detail
    assert "replay_basis=block_end" in detail


# --- executor retry orchestration ----------------------------------------------------------------

def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _reserve(c, mint="0xtok"):
    return m.reserve_pending(c, mint=mint, source_user_id="trader1", strategy="alpha_b",
                             notional=10.0, reserved_at="2026-07-09T00:00:00Z", network_id="4663")


def _seq(vals):
    it = {"i": 0}

    def f(*_a, **_k):
        v = vals[min(it["i"], len(vals) - 1)]; it["i"] += 1
        return v
    return f


def _quote(min_out, spender="0xspender", sell="10000000"):
    return {"sellAmount": sell,
            "transaction": {"to": "0xrouter", "data": "0x" + format(min_out, "x")},
            "issues": {"allowance": {"spender": spender}}}


def _run(c, pid, **seams):
    return execute_entry_evm(c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
                             strategy="alpha_b", source_user_id="t", now_iso="2026-07-09T00:00:01Z",
                             sleep_fn=lambda s: None, **seams)


def _base_seams(quotes, *, confirms, reverts=(), usdc=(100.0, 90.0), token=(0.0, 5.0)):
    calls = {"send": 0, "signed": [], "approve": [], "revert": 0}
    quote_it = _seq(list(quotes))
    confirm_it = _seq(list(confirms))
    revert_list = list(reverts)

    def sign_fn(tx):
        calls["signed"].append(tx)
        return ("0xraw%d" % len(calls["signed"]), "0xhash%d" % len(calls["signed"]),
                6 + len(calls["signed"]))

    def send_fn(raw):
        calls["send"] += 1
        return "0xsent"

    def approve_fn(spender, amount):
        calls["approve"].append((spender, amount)); return True

    def revert_reason_fn(tx_hash):
        i = calls["revert"]; calls["revert"] += 1
        return revert_list[min(i, len(revert_list) - 1)] if revert_list else ("unknown", None)

    return dict(quote_fn=lambda mint, usd: quote_it(), approve_fn=approve_fn, sign_fn=sign_fn,
                send_fn=send_fn, confirm_fn=lambda h: confirm_it(),
                usdc_fn=_seq(list(usdc)), token_fn=_seq(list(token)),
                revert_reason_fn=revert_reason_fn), calls


def test_min_out_revert_retries_once_with_fresh_quote_and_fills():
    c = _c(); pid = _reserve(c)
    q1, q2 = _quote(100), _quote(80)          # pool moved -> fresh quote carries a new min-out
    q2["route"] = {"protocol": "uniswap_v4_multihop", "bridge": "0xbridge"}
    # quote sequence: attempt1 quote, attempt1 post-approve refresh, attempt2 quote, attempt2 refresh
    seams, calls = _base_seams([q1, q1, q2, q2], confirms=["failed", "confirmed"],
                               reverts=[("min_out", "Too little received")],
                               usdc=(100.0, 100.0, 90.0), token=(0.0, 0.0, 5.0))
    r = _run(c, pid, **seams)
    assert r["status"] == "filled"
    assert calls["send"] == 2                                  # one retry, no more
    # the retried swap was signed from the FRESH quote's transaction, not the stale one
    assert calls["signed"][-1]["data"] == q2["transaction"]["data"]
    # reservation survived the retry and is now confirmed
    assert m.pending_row(c, pid)["status"] == "confirmed"
    assert len(m.list_open_positions(c, mode="real")) == 1
    assert "uniswap_v4_multihop" in m.list_open_positions(c, mode="real")[0]["evm_route_json"]


def _run_flap_entry(c, pid, *, payload=_FLAP_SLIPPAGE, **seams):
    error = {"code": 3, "message": "execution reverted", "data": payload}
    return execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="56", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        sleep_fn=lambda _s: None, retry_slippage_bps=800,
        revert_reason_fn=lambda h: evm_revert_reason(h, "https://rpc", http=_replay_http(error=error)),
        **seams,
    )


def _reserve_flap(c):
    return m.reserve_pending(
        c, mint="0xtok", source_user_id="trader1", strategy="alpha_b", notional=10.0,
        reserved_at="2026-07-09T00:00:00Z", network_id="56",
    )


@pytest.mark.parametrize("last_receipt, terminal", [("confirmed", "filled"), ("failed", "failed")])
def test_flap_decoded_slippage_uses_only_one_fresh_bsc_retry(last_receipt, terminal):
    c = _c(); pid = _reserve_flap(c)
    first = {**_quote(100), "issues": {}, "route": {"protocol": "flap_curve"}}
    fresh = {**_quote(80), "issues": {}, "route": {"protocol": "flap_curve"}}
    quotes, quote_calls = iter([first, fresh]), []
    seams, calls = _base_seams(
        [], confirms=["failed", last_receipt],
        usdc=(100.0, 100.0, 90.0), token=(0.0, 0.0, 5.0),
    )
    seams.pop("revert_reason_fn")

    def quote_fn(mint, usd, *, slippage_bps=None):
        quote_calls.append((mint, usd, slippage_bps))
        return next(quotes)

    seams["quote_fn"] = quote_fn
    result = _run_flap_entry(c, pid, **seams)

    assert result["status"] == terminal
    assert quote_calls == [("0xtok", 10.0, None), ("0xtok", 10.0, 800)]
    assert calls["send"] == 2 and len(calls["signed"]) == 2
    assert calls["signed"][-1]["data"] == fresh["transaction"]["data"]
    assert m.pending_totals(c) == (0, 0.0)
    if terminal == "filled":
        assert m.pending_row(c, pid)["status"] == "confirmed"
        assert len(m.list_open_positions(c, mode="real")) == 1
    else:
        assert result["reason"] == "reverted_min_out"
        assert "SlippageTooHigh" in result["revert_detail"]
        assert m.pending_row(c, pid)["resolution_reason"] == "reverted_min_out"
        assert m.list_open_positions(c, mode="real") == []


@pytest.mark.parametrize("payload", ["0x76baadda", "0x76baadda" + "0" * 128, "0xdeadbeef"])
def test_flap_unknown_payload_never_sends_a_second_bsc_entry(payload):
    c = _c(); pid = _reserve_flap(c)
    seams, calls = _base_seams([{**_quote(100), "issues": {}}], confirms=["failed"])
    seams.pop("revert_reason_fn")
    result = _run_flap_entry(c, pid, payload=payload, **seams)
    assert result["status"] == "failed" and result["reason"] == "reverted_unknown"
    assert calls["send"] == 1 and len(calls["signed"]) == 1
    assert m.pending_totals(c) == (0, 0.0)


def test_flap_decoded_retry_still_obeys_exact_execution_premium(monkeypatch):
    from sniper.execution import evm_swap_executor as executor
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM", 1.5)
    monkeypatch.setattr(executor.m, "validated_pending_funded_contract", lambda *_a: {
        "funded_contract_json": json.dumps({"classification": {"facts": {"display_price": 1.0}}})})
    c = _c(); pid = _reserve_flap(c)
    first = {**_quote(100), "sellAmount": "1000", "buyAmount": "1000", "issues": {}}
    expensive = {**_quote(80), "sellAmount": "2000", "buyAmount": "1000", "issues": {}}
    seams, calls = _base_seams(
        [first, expensive], confirms=["failed"],
        usdc=(100.0, 100.0), token=((0.0, 0), (0.0, 0)),
    )
    seams.pop("revert_reason_fn")
    result = _run_flap_entry(c, pid, sell_decimals=0, **seams)
    assert result["status"] == "skipped_chase" and result["exact_price"] == 2.0
    assert calls["send"] == 1 and len(calls["signed"]) == 1
    assert m.pending_totals(c) == (0, 0.0)


def test_min_out_retry_uses_the_bounded_retry_slippage():
    c = _c(); pid = _reserve(c)
    seen = []
    quotes = iter(({**_quote(100), "issues": {}}, {**_quote(80), "issues": {}}))

    def quote_fn(_mint, _usd, *, slippage_bps=None):
        seen.append(slippage_bps)
        return next(quotes)

    seams, _ = _base_seams([], confirms=["failed", "confirmed"],
                           reverts=[("min_out", "Too little received")],
                           usdc=(100.0, 100.0, 90.0), token=(0.0, 0.0, 5.0))
    seams["quote_fn"] = quote_fn
    result = _run(c, pid, retry_slippage_bps=800, **seams)
    assert result["status"] == "filled"
    assert seen == [None, 800]


def test_min_out_retry_uses_the_exact_quote_for_the_premium_cap(monkeypatch):
    from sniper.execution import evm_swap_executor as executor
    from sniper.settings import memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM", 1.95)
    monkeypatch.setattr(
        executor.m, "validated_pending_funded_contract",
        lambda _conn, _pending: {"funded_contract_json": json.dumps({
            "classification": {"facts": {"display_price": 1.0}},
        })},
    )
    c = _c(); pid = _reserve(c)
    first = {**_quote(100), "sellAmount": "1500", "buyAmount": "1000", "issues": {}}
    retry = {**_quote(80), "sellAmount": "1918", "buyAmount": "1000", "issues": {}}
    seams, calls = _base_seams(
        [first, retry], confirms=["failed", "confirmed"],
        reverts=[("min_out", "Too little received")],
        usdc=(100.0, 100.0, 90.0), token=((0.0, 0), (0.0, 0), (5.0, 0)),
    )

    result = _run(
        c, pid, retry_slippage_bps=800, sell_decimals=0, **seams,
    )

    assert result["status"] == "filled"
    assert calls["send"] == 2


def test_a_price_refusal_after_a_mined_swap_still_invalidates_the_snapshot(monkeypatch, tmp_path):
    from sniper.execution import exec_lock, evm_swap_executor as executor
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM", 1.5)
    monkeypatch.setattr(executor.m, "validated_pending_funded_contract",
        lambda *_a: {"funded_contract_json": json.dumps({
            "classification": {"facts": {"display_price": 1.0}}})})
    c = _c(); pid = _reserve(c)
    first = {**_quote(100), "sellAmount": "1000", "buyAmount": "1000", "issues": {}}
    retry = {**_quote(80), "sellAmount": "2000", "buyAmount": "1000", "issues": {}}
    seams, calls = _base_seams([first, retry], confirms=["failed"],
        reverts=[("min_out", "Too little received")],
        usdc=(100.0, 100.0), token=((0.0, 0), (0.0, 0)))
    run = exec_lock.serialize_executor(_run, "evm-4663-test", lock_dir=str(tmp_path))
    result = run(c, pid, retry_slippage_bps=800, sell_decimals=0, **seams)
    assert calls["send"] == 1 and result["exact_price"] == 2.0
    assert not isinstance(result, exec_lock.UnchangedWalletResult)
    assert exec_lock.exec_generation("evm-4663-test", lock_dir=str(tmp_path)) > 0


def test_min_out_retry_projects_final_attempt_stages_and_keeps_first_failure_history():
    c = _c(); clock = _Clock(); trace = _trace(c, clock)
    pid = m.reserve_pending(
        c, mint="0xtok", source_user_id="trader1", strategy="alpha_b", notional=10.0,
        reserved_at="2026-07-09T00:00:01Z", network_id="4663", attempt_id=trace.attempt_id,
    )
    q1, q2 = _quote(100), _quote(80)
    seams, _ = _base_seams(
        [q1, q1, q2, q2], confirms=["failed", "confirmed"],
        reverts=[("min_out", "Too little received")],
        usdc=(100.0, 100.0, 90.0), token=(0.0, 0.0, 5.0),
    )
    original_quote = seams["quote_fn"]
    original_sign = seams["sign_fn"]
    original_send = seams["send_fn"]
    original_confirm = seams["confirm_fn"]

    def advancing(fn, seconds):
        def wrapped(*args, **kwargs):
            result = fn(*args, **kwargs)
            clock.advance(seconds)
            return result
        return wrapped

    seams["quote_fn"] = advancing(original_quote, 0.010)
    seams["sign_fn"] = advancing(original_sign, 0.020)
    seams["send_fn"] = advancing(original_send, 0.030)
    seams["confirm_fn"] = advancing(original_confirm, 0.040)
    result = _run(c, pid, trace=trace, **seams)
    trace.finish("filled", "confirmed", tx_sig=result["tx_hash"])

    row = dict(c.execute(
        "SELECT quote_completed_at, signing_completed_at, transaction_submitted_at, "
        "chain_confirmation_completed_at, trace_json FROM memecoin_entry_attempts "
        "WHERE attempt_id=?", (trace.attempt_id,),
    ).fetchone())
    payload = __import__("json").loads(row["trace_json"])
    reasons = [event for event in payload["substage_events"]
               if event["name"] == "evm.revert_reason"]
    assert [(event["attempt_no"], event["kind"], event["detail"]) for event in reasons] == [
        (1, "min_out", "Too little received")]
    confirmations = [event for event in payload["submission_events"]
                     if event["stage"] == "chain_confirmation"]
    assert [(event["attempt_no"], event["outcome"]) for event in confirmations] == [
        (1, "failed"), (2, "confirmed")]
    assert [event["tx_sig"] for event in confirmations] == ["0xhash1", "0xhash2"]
    assert row["chain_confirmation_completed_at"] == confirmations[-1]["completed_at"]
    assert row["quote_completed_at"] > confirmations[0]["completed_at"]


def test_min_out_retry_reuses_confirmed_weth_unwrap_once():
    c = _c(); pid = _reserve(c)
    pre = {"to": "0xweth", "data": "0x2e1a7d4d" + format(10, "064x"), "value": "0x0"}
    q = {"sellAmount": "10", "buyAmount": "100", "issues": {},
         "pre_transactions": [pre],
         "transaction": {"to": "0xrouter", "data": "0xswap"}}
    seams, calls = _base_seams(
        [q, q], confirms=["confirmed", "failed", "confirmed", "confirmed"],
        reverts=[("min_out", "Too little received")],
        # The first attempt unwraps WETH before its swap reverts. The retry therefore starts with
        # the lower WETH balance. Fill accounting must keep the balance from before the unwrap.
        usdc=(100.0, 90.0, 90.0), token=(0.0, 0.0, 5.0),
    )

    result = _run(c, pid, **seams)

    assert result["status"] == "filled"
    assert result["notional"] == 10.0
    assert sum(1 for tx in calls["signed"] if tx.get("to") == "0xweth") == 1
    assert calls["send"] == 3  # one unwrap + failed swap + successful retry swap


def test_min_out_revert_second_failure_releases_with_specific_reason():
    c = _c(); pid = _reserve(c)
    seams, calls = _base_seams([_quote(100)] * 4, confirms=["failed", "failed"],
                               reverts=[("min_out", "Too little received")] * 2)
    r = _run(c, pid, **seams)
    assert r["status"] == "failed"
    assert r["reason"] == "reverted_min_out"
    assert calls["send"] == 2                                  # exactly one retry, never a third
    assert m.pending_totals(c) == (0, 0.0)                     # released after the second failure
    row = m.pending_row(c, pid)
    assert row["status"] == "failed" and row["resolution_reason"] == "reverted_min_out"


def test_terminal_min_out_rewraps_the_confirmed_native_clip_before_release():
    c = _c(); pid = _reserve(c)
    pre = {"to": "0xweth", "data": "0x2e1a7d4d" + format(10, "064x"), "value": "0x0"}
    q = {"sellAmount": "10", "buyAmount": "100", "issues": {},
         "pre_transactions": [pre], "transaction": {"to": "0xrouter", "data": "0xswap"}}
    seams, _ = _base_seams([q, q], confirms=["confirmed", "failed", "failed"],
                            reverts=[("min_out", "Too little received")] * 2)
    cleaned = []
    result = _run(c, pid, cleanup_native_fn=lambda amount: cleaned.append(amount) or "confirmed",
                  **seams)
    assert result["status"] == "failed"
    assert result["cleanup_status"] == "confirmed"
    assert cleaned == [10]
    assert m.pending_row(c, pid)["status"] == "failed"


def test_unknown_native_cleanup_releases_a_failed_entry_slot():
    c = _c(); pid = _reserve(c)
    pre = {"to": "0xweth", "data": "0x2e1a7d4d" + format(10, "064x"), "value": "0x0"}
    q = {"sellAmount": "10", "buyAmount": "100", "issues": {},
         "pre_transactions": [pre], "transaction": {"to": "0xrouter", "data": "0xswap"}}
    seams, _ = _base_seams([q, q], confirms=["confirmed", "failed", "failed"],
                            reverts=[("min_out", "Too little received")] * 2)
    result = _run(c, pid, cleanup_native_fn=lambda _amount: "unknown", **seams)
    assert result["cleanup_status"] == "unknown"
    row = m.pending_row(c, pid)
    assert row["status"] == "failed"
    assert row["resolution_reason"] == "reverted_min_out"
    assert m.unresolved_signed_pending_count(c, "4663") == 0


def test_cleanup_identity_is_stored_before_broadcast():
    c = _c(); pid = _reserve(c)
    assert m.bind_pending_cleanup(
        c, pid, tx_sig="0xcleanup", nonce=9, amount_atomic=10,
        payload_sha256="abc", signed_at="2026-07-09T00:00:02Z")
    row = m.pending_row(c, pid)
    assert row["cleanup_tx_sig"] == "0xcleanup"
    assert row["cleanup_status"] == "signed"


def test_unknown_revert_never_retries():
    c = _c(); pid = _reserve(c)
    seams, calls = _base_seams([_quote(100)] * 2, confirms=["failed"],
                               reverts=[("unknown", "PAUSED")])
    r = _run(c, pid, **seams)
    assert r["status"] == "failed"
    assert r["reason"] == "reverted_unknown"
    assert calls["send"] == 1                                  # genuine revert -> no retry
    assert m.pending_totals(c) == (0, 0.0)


def test_no_revert_seam_means_no_retry():
    c = _c(); pid = _reserve(c)
    seams, calls = _base_seams([_quote(100)] * 2, confirms=["failed"])
    seams.pop("revert_reason_fn")
    r = _run(c, pid, **seams)
    assert r["status"] == "failed" and r["reason"] == "reverted_unknown"
    assert calls["send"] == 1


def test_unknown_confirm_keeps_reservation_and_never_retries():
    c = _c(); pid = _reserve(c)
    seams, calls = _base_seams([_quote(100)] * 2, confirms=["unknown"],
                               reverts=[("min_out", "x")])
    r = _run(c, pid, **seams)
    assert r["status"] == "unknown"
    assert calls["send"] == 1                                  # tx may still land -> NEVER resubmit
    assert m.pending_totals(c) == (1, 10.0)


# --- post-approval quote refresh -----------------------------------------------------------------

def test_post_approval_requote_signs_fresh_transaction():
    c = _c(); pid = _reserve(c)
    q1, q2 = _quote(100), _quote(97)          # same sellAmount + spender, moved min-out
    seams, calls = _base_seams([q1, q2], confirms=["confirmed"])
    r = _run(c, pid, **seams)
    assert r["status"] == "filled"
    assert calls["approve"] == [("0xspender", 10000000)]
    assert calls["signed"][-1]["data"] == q2["transaction"]["data"]


class _Clock:
    def __init__(self):
        self.value = 100.0

    def perf_counter(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def _trace(c, clock):
    event = SimpleNamespace(
        fomo_event_id="event-requote", source_user_id="trader1", source_handle="alice",
        signal_source="global_feed", network_id="4663", mint="0xtok", ticker="TOK",
        side="buy", their_price=1.0, their_market_cap=1_000_000.0,
        created_at="2026-07-09T00:00:00+00:00",
        received_at_normalized="2026-07-09T00:00:00+00:00",
        clock_domain="observed_real_utc",
    )
    return EntryAttemptTrace.start(c, event, "2026-07-09T00:00:01+00:00", clock)


def test_post_approval_requote_uses_fresh_reconciled_timestamp_and_age():
    c = _c()
    clock = _Clock()
    trace = _trace(c, clock)
    pid = m.reserve_pending(
        c, mint="0xtok", source_user_id="trader1", strategy="alpha_b", notional=10.0,
        reserved_at="2026-07-09T00:00:01Z", network_id="4663", attempt_id=trace.attempt_id,
    )
    q1, q2 = _quote(100), _quote(97)
    quotes = iter((q1, q2))
    usdc = iter((100.0, 90.0))
    token = iter(((0.0, 6), (5.0, 6)))

    def quote_fn(*_):
        quote = next(quotes)
        clock.advance(1.0)
        return quote

    def approve_fn(*_):
        clock.advance(2.0)
        return True

    def confirm_fn(*_):
        clock.advance(4.0)
        return "confirmed"

    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        quote_fn=quote_fn, approve_fn=approve_fn,
        sign_fn=lambda _: ("0xraw", "0xhash", 7), send_fn=lambda _: "0xhash",
        confirm_fn=confirm_fn, usdc_fn=lambda: next(usdc), token_fn=lambda _: next(token),
        sleep_fn=lambda _: None, trace=trace,
    )

    assert result["status"] == "filled"
    assert result["quote_observed_at"] == "2026-07-09T00:00:05+00:00"
    assert result["quote_age_ms"] == 4000.0


def test_post_approval_requote_sell_amount_change_aborts():
    c = _c(); pid = _reserve(c)
    q1, q2 = _quote(100), _quote(100, sell="20000000")
    seams, calls = _base_seams([q1, q2], confirms=["confirmed"])
    r = _run(c, pid, **seams)
    assert r["status"] == "aborted"
    assert calls["send"] == 0
    assert m.pending_totals(c) == (0, 0.0)


def test_post_approval_requote_spender_change_aborts():
    c = _c(); pid = _reserve(c)
    q1, q2 = _quote(100), _quote(100, spender="0xother")
    seams, calls = _base_seams([q1, q2], confirms=["confirmed"])
    r = _run(c, pid, **seams)
    assert r["status"] == "aborted"
    assert calls["send"] == 0
    assert m.pending_totals(c) == (0, 0.0)


def test_post_approval_requote_unavailable_aborts():
    c = _c(); pid = _reserve(c)
    seams, calls = _base_seams([_quote(100), None], confirms=["confirmed"])
    r = _run(c, pid, **seams)
    assert r["status"] == "aborted"
    assert calls["send"] == 0
    assert m.pending_totals(c) == (0, 0.0)


def test_mined_min_out_retry_requotes_the_winner_without_restarting_ladder():
    c = _c()
    pid = m.reserve_pending(c, mint="0xtok", source_user_id="trader1", strategy="alpha_b",
                            notional=10.0, reserved_at="2026-07-09T00:00:00Z", network_id="8453")
    q1, q2 = _quote(100), _quote(80)
    q1['issues'] = {}; q2['issues'] = {}
    ladder_calls, slips = [], []
    q1['_requote'] = lambda slippage_bps=None: slips.append(slippage_bps) or q2
    seams, calls = _base_seams([], confirms=['failed', 'confirmed'],
                               reverts=[('min_out', 'Too little received')],
                               usdc=(100.0, 100.0, 90.0), token=(0.0, 0.0, 5.0))
    def quote_fn(*args, **kw):
        ladder_calls.append(1)
        assert len(ladder_calls) == 1, 'min-out retry reran the ladder'
        return q1
    seams['quote_fn'] = quote_fn
    result = execute_entry_evm(c, pending_id=pid, mint="0xtok", network_id="8453", usd_amount=10.0,
                               strategy="alpha_b", source_user_id="trader1",
                               now_iso="2026-07-09T00:00:01Z", retry_slippage_bps=800,
                               sleep_fn=lambda s: None, **seams)
    assert result['status'] == 'filled'
    assert ladder_calls == [1] and slips == [800]
    assert calls['send'] == 2 and calls['signed'][-1]['data'] == q2['transaction']['data']
