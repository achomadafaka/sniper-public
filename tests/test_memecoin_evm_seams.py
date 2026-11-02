"""Direct EVM quote, JSON-RPC, signing, and execution bindings for the funded EVM executor."""
import json
import sqlite3

import pytest
from eth_account import Account
from eth_utils import keccak

from sniper.execution import evm_real_seams as S
from sniper.execution.evm_token_safety import _nested_slot
from sniper.execution.evm_wallet import CHAIN_QUOTE, SEL_APPROVE
from sniper.persistence import db_memecoin as m


def _signed_raw(chain_id, nonce=1):
    """A real signed transaction: the EVM-08 intent fence never sends bytes without a chain id."""
    from eth_account import Account
    tx = {"type": 2, "chainId": chain_id, "nonce": nonce, "maxFeePerGas": 10,
          "maxPriorityFeePerGas": 0, "gas": 21000, "to": "0x" + "22" * 20, "value": 0, "data": b""}
    return "0x" + bytes(Account.sign_transaction(tx, b"\x03" * 32).raw_transaction).hex()


RAW = _signed_raw(4663)


def _fake_http(handler):
    return handler


# --- RPC helpers ---

def test_eth_nonce_and_gas_price_parse():
    http = lambda m_, u, headers=None, body=None, timeout=10: (200, {"result": "0x9"}, None)
    assert S.eth_nonce("0xabc", "https://rpc", http=http) == 9
    assert S.eth_gas_price("https://rpc", http=http) == 9


def test_eth_send_raw_returns_hash():
    http = lambda *a, **k: (200, {"result": "0xhash"}, None)
    assert S.eth_send_raw(RAW, "https://rpc", http=http) == "0xhash"


@pytest.mark.parametrize("message", [
    "nonce too low",
    "replacement transaction underpriced",
    "insufficient funds for gas * price + value",
    "intrinsic gas too low",
    "max fee per gas less than block base fee",
])
def test_eth_send_raw_distinguishes_definite_rpc_rejection(message):
    http = lambda *a, **k: (200, {"error": {"code": -32000, "message": message}}, None)
    assert S.eth_send_raw(RAW, "https://rpc", http=http) is False


def test_eth_send_raw_treats_already_known_as_accepted():
    http = lambda *a, **k: (200, {"error": {"code": -32000, "message": "already known"}}, None)
    assert S.eth_send_raw(RAW, "https://rpc", http=http) == "already_known"


def test_eth_send_raw_keeps_transport_and_unclassified_errors_ambiguous():
    def transport(*_a, **_k):
        raise OSError("rpc disconnected")

    assert S.eth_send_raw(RAW, "https://rpc", http=transport) is None
    http = lambda *a, **k: (200, {"error": {"code": -32000, "message": "provider busy"}}, None)
    assert S.eth_send_raw(RAW, "https://rpc", http=http) is None


def test_eth_confirm_status_mapping():
    def mk(status):
        return lambda *a, **k: (200, {"result": {"status": status}}, None)
    assert S.eth_confirm("0xh", "https://rpc", http=mk("0x1"), sleep=lambda s: None) == "confirmed"
    assert S.eth_confirm("0xh", "https://rpc", http=mk("0x0"), sleep=lambda s: None) == "failed"


def test_eth_confirm_unknown_when_no_receipt():
    http = lambda *a, **k: (200, {"result": None}, None)
    assert S.eth_confirm("0xh", "https://rpc", http=http, sleep=lambda s: None, max_attempts=3) == "unknown"


def test_weth_entry_preflight_refuses_exact_token_transfer_failure():
    taker = "0x" + "11" * 20
    weth = CHAIN_QUOTE["4663"]["token"]
    transfer_failed = keccak(b"ERC20TransferFailed()")[:4].hex()
    quote = {
        "pre_transactions": [{"to": weth, "data": "0x2e1a7d4d" + "0" * 64}],
        "transaction": {"to": "0x" + "22" * 20, "data": "0xdead", "value": "0x5",
                        "gas": "700000"},
        "issues": {"allowance": None},
        "route": {"protocol": "uniswap_v4"},
    }

    def http(_method, _url, *, body=None, **_kwargs):
        request = json.loads(body)
        assert request["method"] == "eth_call"
        call, block, overrides = request["params"]
        assert block == "latest"
        assert call == {"from": taker, "to": quote["transaction"]["to"],
                        "data": "0xdead", "value": "0x5", "gas": hex(700000)}
        assert int(overrides[taker]["balance"], 16) > 10 ** 17
        return 200, {"error": {"message": "execution reverted", "data": "0x" + transfer_failed}}, None

    result = S.preflight_weth_entry_quote(
        quote, quote_token=weth, taker=taker, rpc_url="https://rpc", http=http,
    )

    # 2026-09-22: the classified branches carry their payload now. ALLOC 09b6cdb8 stored a bare
    # `entry_simulation_min_out` label, and diagnosing it needed a live reproduction instead of
    # a query -- by which time the curve had moved and the revert was gone. Knowing the class is
    # not knowing the cause.
    assert result[0] is False
    assert result[1].startswith("entry_simulation_transfer_failed: exact route token transfer failed")
    assert "ERC20TransferFailed" in result[1] or transfer_failed in result[1], result[1]


def test_weth_entry_preflight_funds_the_relay_allowance():
    taker = "0x" + "11" * 20
    spender = "0x" + "22" * 20
    weth = CHAIN_QUOTE["4663"]["token"]
    quote = {
        "transaction": {"to": spender, "data": "0xf9e4bab4", "value": "0x0", "gas": "900000"},
        "issues": {"allowance": {"spender": spender}},
        "route": {"protocol": "relay_same_chain"},
    }

    def http(_method, _url, *, body=None, **_kwargs):
        request = json.loads(body)
        assert request["method"] == "eth_call"
        _call, _block, overrides = request["params"]
        state_diff = overrides[weth.lower()]["stateDiff"]
        assert _nested_slot(spender, taker, 1) in state_diff
        assert int(state_diff[_nested_slot(spender, taker, 1)], 16) >= 10 ** 24
        return 200, {"result": "0x"}, None

    assert S.preflight_weth_entry_quote(
        quote, quote_token=weth, taker=taker, rpc_url="https://rpc", http=http,
    ) == (True, "ok")


def test_weth_entry_preflight_funds_upgradeable_weth_allowance():
    taker = "0x" + "11" * 20
    spender = "0x" + "22" * 20
    weth = CHAIN_QUOTE["4663"]["token"]
    quote = {
        "transaction": {"to": spender, "data": "0xf9e4bab4", "value": "0x0"},
        "issues": {"allowance": {"spender": spender}},
        "route": {"protocol": "uniswap_v3"},
    }

    def http(_method, _url, *, body=None, **_kwargs):
        request = json.loads(body)
        _call, _block, overrides = request["params"]
        state_diff = overrides[weth.lower()]["stateDiff"]
        assert _nested_slot(spender, taker, 52) in state_diff
        return 200, {"result": "0x"}, None

    assert S.preflight_weth_entry_quote(
        quote, quote_token=weth, taker=taker, rpc_url="https://rpc", http=http,
    ) == (True, "ok")


def test_weth_entry_preflight_labels_exact_min_out():
    taker = "0x" + "11" * 20
    weth = CHAIN_QUOTE["4663"]["token"]
    quote = {
        "pre_transactions": [{"to": weth, "data": "0x2e1a7d4d" + "0" * 64}],
        "transaction": {"to": "0x" + "22" * 20, "data": "0xdead", "value": "0x5"},
        "issues": {"allowance": None},
    }
    http = lambda *_a, **_kw: (200, {"error": {"message": "Too little received"}}, None)

    ok, reason = S.preflight_weth_entry_quote(
        quote, quote_token=weth, taker=taker, rpc_url="https://rpc", http=http)
    assert ok is False
    assert reason.startswith("entry_simulation_min_out: exact route minimum output reverted")
    # the node's own words survive into the row (2026-09-22)
    assert "Too little received" in reason, reason


def test_weth_entry_preflight_keeps_unclassified_revert_detail():
    taker = "0x" + "11" * 20
    weth = CHAIN_QUOTE["4663"]["token"]
    quote = {
        "pre_transactions": [{"to": weth, "data": "0x2e1a7d4d" + "0" * 64}],
        "transaction": {"to": "0x" + "22" * 20, "data": "0xdead", "value": "0x5"},
        "issues": {"allowance": None},
    }
    http = lambda *_a, **_kw: (200, {
        "error": {"message": "execution reverted: route policy blocked"},
    }, None)

    assert S.preflight_weth_entry_quote(
        quote, quote_token=weth, taker=taker, rpc_url="https://rpc", http=http,
    ) == (False, "entry_simulation_reverted: exact route reverted; "
                "detail=execution reverted: route policy blocked")


def test_classify_evm_revert_labels_transfer_helper_stf():
    assert S.classify_evm_revert(None, "execution reverted: STF") == "transfer_failed"


def test_receipt_transfer_accepts_only_exact_token_and_custody_address():
    token = "0x" + "11" * 20
    custody = "0x" + "22" * 20
    other = "0x" + "33" * 20
    topic = "0x" + "0" * 24 + custody[2:]
    wrong_topic = "0x" + "0" * 24 + other[2:]
    transfer = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    word = lambda value: "0x" + format(value, "064x")
    receipt = {
        "status": "0x1", "transactionHash": "0xhash",
        "logs": [
            {"address": other, "topics": [transfer, wrong_topic, topic], "data": word(90)},
            {"address": token, "topics": [transfer, wrong_topic, wrong_topic], "data": word(80)},
            {"address": token, "topics": [transfer, wrong_topic, topic], "data": "0x5"},
            {"address": token, "topics": [transfer, wrong_topic, topic], "data": word(5)},
            {"address": token, "topics": [transfer, wrong_topic, topic], "data": word(7)},
        ],
    }
    http = lambda *_a, **_k: (200, {"result": receipt}, None)
    assert S.erc20_received_in_receipt(
        "0xhash", token, custody, "https://rpc", http=http,
    ) == 12
    assert S.erc20_received_in_receipt(
        "0xother", token, custody, "https://rpc", http=http,
    ) is None


def test_native_receipt_recovers_weth_unwrap_from_custody_balance():
    """A WETH exit can pay the custody wallet in native ETH, with no ERC-20 receipt transfer."""
    custody = "0x" + "22" * 20
    tx_hash = "0x" + "aa" * 32
    block = 17
    before = 1000
    gas_used = 21
    gas_price = 3
    received = 900
    receipt = {
        "status": "0x1", "transactionHash": tx_hash, "blockNumber": hex(block),
        "gasUsed": hex(gas_used), "effectiveGasPrice": hex(gas_price), "logs": [],
    }
    tx = {"hash": tx_hash, "from": custody, "value": "0x0"}

    def http(_method, _url, *, body=None, **_kwargs):
        body = json.loads(body)
        method, params = body["method"], body["params"]
        if method == "eth_getTransactionReceipt":
            result = receipt
        elif method == "eth_getTransactionByHash":
            result = tx
        elif method == "eth_getBalance":
            result = hex(before if params[1] == hex(block - 1) else before + received - gas_used * gas_price)
        else:
            raise AssertionError(method)
        return 200, {"result": result}, None

    assert S.native_received_in_receipt(tx_hash, custody, "https://rpc", http=http) == received


def test_eth_confirm_supports_bounded_fast_initial_poll_window():
    polls = iter((None, None, None, {"status": "0x1"}))
    sleeps = []

    def http(*_args, **_kwargs):
        return 200, {"result": next(polls)}, None

    assert S.eth_confirm(
        "0xh", "https://rpc", http=http, sleep=sleeps.append, max_attempts=10,
        poll_s=2.0, fast_poll_s=0.25, fast_attempts=8,
    ) == "confirmed"
    assert sleeps == [0.25, 0.25, 0.25]


def test_4663_executor_uses_fast_initial_confirmation_window(monkeypatch):
    calls = []
    monkeypatch.setattr(S, "eth_confirm", lambda *a, **kw: calls.append(kw) or "confirmed")
    ex = S.build_real_evm_executor(
        acct=Account.create(), chain_id="4663", rpc_url="rpc",
        http=lambda *_a, **_k: (200, {"result": "0x1"}, None),
    )
    assert ex["_confirm"]("0xhash") == "confirmed"
    assert calls[0]["http"] is ex["http"]
    assert {k: v for k, v in calls[0].items() if k != "http"} == {
        "max_attempts": 36, "poll_s": 2.0, "fast_poll_s": 0.25, "fast_attempts": 8,
        "resend_fn": None, "resend_at_attempts": (),
    }



def test_5042_confirm_window_stays_under_the_watchdog_stale_limit(monkeypatch):
    # 2026-09-16 BANK: a dropped Arc send held the main loop in a 120s receipt poll; the watchdog
    # killed the runner as stale. Arc blocks are 0.5s, so a short window loses nothing.
    calls = []
    monkeypatch.setattr(S, "eth_confirm", lambda *a, **kw: calls.append(kw) or "unknown")
    ex = S.build_real_evm_executor(
        acct=Account.create(), chain_id="5042", rpc_url="rpc",
        http=lambda *_a, **_k: (200, {"result": "0x1"}, None),
    )
    ex["_confirm"]("0xhash")
    kw = calls[0]
    worst = kw["fast_poll_s"] * kw["fast_attempts"] + kw["poll_s"] * (kw["max_attempts"] - kw["fast_attempts"])
    assert worst <= 45


@pytest.mark.parametrize("chain", ["4663", "5042", "8453", "56"])
def test_every_receipt_wait_ends_before_the_watchdog_sweep_limit(monkeypatch, chain):
    # The receipt poll runs on the main loop. MEMECOIN_SWEEP_MAX_AGE_S (90s) is the watchdog's kill
    # line; BANK (2026-09-16) proved a dropped send reaches it. Leave 30s for the rest of the pass.
    from sniper.settings import memecoin as cfg
    calls = []

    def fake_confirm(*_a, max_attempts=60, poll_s=2.0, fast_poll_s=None, fast_attempts=0, **_k):
        fast = fast_attempts if fast_poll_s is not None else 0
        calls.append((fast_poll_s or 0) * fast + poll_s * (max_attempts - fast))
        return "unknown"

    monkeypatch.setattr(S, "eth_confirm", fake_confirm)
    ex = S.build_real_evm_executor(
        acct=Account.create(), chain_id=chain, rpc_url="rpc",
        http=lambda *_a, **_k: (200, {"result": "0x1"}, None),
    )
    ex["_confirm"]("0xhash")
    assert calls and calls[0] <= cfg.MEMECOIN_SWEEP_MAX_AGE_S - 30


def test_eth_confirm_reoffers_identical_bytes_at_the_named_attempts():
    # 2026-09-16 CIRCLE (Arc): the exit was accepted, never mined, and waited out the whole poll.
    polls = iter((None, None, None, None, {"status": "0x1"}))
    resent = []

    def http(*_args, **_kwargs):
        return 200, {"result": next(polls)}, None

    assert S.eth_confirm("0xh", "https://rpc", http=http, sleep=lambda _s: None, max_attempts=10,
                         resend_fn=lambda: resent.append(1), resend_at_attempts=(1, 3, 7)) == "confirmed"
    assert len(resent) == 2          # attempts 1 and 3; the receipt arrived before 7


def test_eth_confirm_resend_failure_never_changes_the_answer():
    polls = iter((None, {"status": "0x0"}))

    def http(*_args, **_kwargs):
        return 200, {"result": next(polls)}, None

    def boom():
        raise RuntimeError("node down")

    assert S.eth_confirm("0xh", "https://rpc", http=http, sleep=lambda _s: None, max_attempts=5,
                         resend_fn=boom, resend_at_attempts=(0,)) == "failed"


@pytest.mark.parametrize("chain", ["4663", "5042", "8453", "56"])
def test_executor_confirm_reoffers_the_exact_bytes_it_sent(monkeypatch, chain):
    acct = Account.create()
    tx = {"to": "0x" + "22" * 20, "data": "0x", "value": 0, "gas": 21000,
          "gasPrice": 10 ** 9, "nonce": 1, "chainId": int(chain)}
    raw, tx_hash = S.sign_tx(tx, acct)
    captured = {}

    def fake_confirm(h, _url, *, resend_fn=None, resend_at_attempts=(), **_k):
        captured["hash"], captured["at"] = h, resend_at_attempts
        resend_fn()
        return "unknown"

    sent = []
    monkeypatch.setattr(S, "eth_confirm", fake_confirm)
    ex = S.build_real_evm_executor(acct=acct, chain_id=chain, rpc_url="rpc",
                                   http=lambda *_a, **_k: (200, {"result": "0x1"}, None))
    send = ex["_remember_sent"](lambda r: sent.append(r) or tx_hash)
    send(raw)
    ex["_confirm"](tx_hash)
    assert captured["hash"] == tx_hash and captured["at"]
    assert sent == [raw, raw]        # the original send, then the identical re-offer

# --- signing + approve encoding ---

def test_sign_tx_round_trips_locally():
    acct = Account.create()
    tx = {"to": "0x" + "22" * 20, "data": "0x", "value": 0, "gas": 21000,
          "gasPrice": 10 ** 9, "nonce": 1, "chainId": 8453}
    raw, h = S.sign_tx(tx, acct)
    assert raw.startswith("0x") and h.startswith("0x") and len(raw) > 10


def test_encode_approve_exact_amount():
    spender = "0x" + "33" * 20
    data = S.encode_approve(spender, 10_000_000)
    assert data.startswith("0x" + SEL_APPROVE)
    assert data.endswith(format(10_000_000, "x").rjust(64, "0"))       # exact amount, not max-uint
    assert "33" * 20 in data                                            # spender padded in


def test_bound_signer_accepts_hex_value_from_direct_quote():
    """Direct V2/V3 builders emit JSON-RPC quantities (``0x0``), not decimal strings."""
    acct = Account.create()

    def http(_method, _url, headers=None, body=None, timeout=10):
        method = json.loads(body)["method"]
        if method == "eth_getTransactionCount":
            return 200, {"result": "0x1"}, None
        if method == "eth_gasPrice":
            return 200, {"result": "0x3b9aca00"}, None
        raise AssertionError(method)

    ex = S.build_real_evm_executor(
        acct=acct, chain_id="4663", rpc_url="https://rpc", http=http,
        usdc_fn=lambda: 0.0, token_fn=lambda _mint: 0.0,
    )

    signed = ex["_sign"]({
        "to": "0x" + "44" * 20,
        "data": "0x",
        "value": "0x0",
    })

    assert signed is not None
    assert signed[0].startswith("0x") and signed[1].startswith("0x")


def test_bound_signer_fails_closed_on_malformed_transaction_quantity():
    acct = Account.create()

    def http(_method, _url, headers=None, body=None, timeout=10):
        method = json.loads(body)["method"]
        if method == "eth_getTransactionCount":
            return 200, {"result": "0x1"}, None
        if method == "eth_gasPrice":
            return 200, {"result": "0x3b9aca00"}, None
        raise AssertionError(method)

    ex = S.build_real_evm_executor(
        acct=acct, chain_id="4663", rpc_url="https://rpc", http=http,
        usdc_fn=lambda: 0.0, token_fn=lambda _mint: 0.0,
    )

    assert ex["_sign"]({
        "to": "0x" + "44" * 20,
        "data": "0x",
        "value": "not-a-quantity",
    }) is None


def test_bound_signer_fails_closed_on_malformed_quoted_gas_price():
    acct = Account.create()

    def http(_method, _url, headers=None, body=None, timeout=10):
        method = json.loads(body)["method"]
        if method == "eth_getTransactionCount":
            return 200, {"result": "0x1"}, None
        if method == "eth_gasPrice":
            return 200, {"result": "0x3b9aca00"}, None
        raise AssertionError(method)

    ex = S.build_real_evm_executor(
        acct=acct, chain_id="4663", rpc_url="https://rpc", http=http,
        usdc_fn=lambda: 0.0, token_fn=lambda _mint: 0.0,
    )

    assert ex["_sign"]({
        "to": "0x" + "44" * 20,
        "data": "0x",
        "value": "0x0",
        "gasPrice": "not-a-quantity",
    }) is None


# --- factory: execute_fn end to end (allowance already ok -> no approve) ---

def test_build_executor_execute_fn_fills(monkeypatch):
    # 8453 QUOTES IN WETH SINCE 2026-09-06, so the executor needs a live ETH/USD mark to size the
    # clip; without one it aborts `quote-token price unavailable`, which is the fail-closed rule
    # working. $1 keeps every number in this test identical to the USDC era.
    monkeypatch.setattr(S, "quote_usd_price", lambda *_a, **_k: 1.0)
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.reserve_pending(c, mint="0xtok", source_user_id="trader1", strategy="alpha_b",
                            notional=10.0, reserved_at="t", network_id="8453")
    acct = Account.create()
    swap_to = "0x" + "44" * 20

    _entries = {}

    def venue_quote(sell, buy, amount, *, slippage_bps=None):
        # DIRECTION-AWARE (2026-09-06). The entry ladder measures a round trip by quoting the
        # REVERSE of the bag it would buy. A stub answering "1" whichever way it is called reads as
        # a ~-100% round trip and the entry is then correctly refused -- the stub was wrong for a
        # ladder that measures, not the ladder. Keyed by TOKEN PAIR, never by call order, so a
        # second entry in the same test is not mistaken for the first one's reverse.
        pair = (str(sell).lower(), str(buy).lower())
        if pair[::-1] in _entries:
            return {"sellAmount": str(amount),
                    "buyAmount": str(int(_entries[pair[::-1]] * 0.97)),
                    "issues": {"allowance": None},
                    "transaction": {"to": swap_to, "data": "0x", "value": "0",
                                    "gas": "500000", "gasPrice": "1000000000"}}
        _entries[pair] = int(amount)
        return {"sellAmount": str(amount), "buyAmount": "1",
                "issues": {"allowance": None},
                "transaction": {"to": swap_to, "data": "0x", "value": "0",
                                "gas": "500000", "gasPrice": "1000000000"}}

    from evm_receipt_fake import MISS, answer
    from sniper.execution.evm_wallet import CHAIN_QUOTE

    def http(method, url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        meth = req["method"]
        if meth == "eth_getTransactionCount":
            return 200, {"result": "0x5"}, None
        if meth == "eth_sendRawTransaction":
            return 200, {"result": "0xhash"}, None
        hit = answer(meth, req.get("params") or [], owner=acct.address, mint="0xtok",
                     quote=CHAIN_QUOTE["8453"]["token"], coin=5, paid=10 * 10 ** 18)
        if hit is not MISS:                  # the receipt books it: 5 coins (0 decimals), 10 WETH
            return 200, {"result": hit}, None
        return 200, {"result": "0x0"}, None

    ex = S.build_real_evm_executor(acct=acct, chain_id="8453", rpc_url="https://rpc", http=http,
                                   venue_quote_fn=venue_quote,
                                   usdc_fn=_seq([100.0, 90.0]), token_fn=_seq([0.0, 5.0]))
    r = ex["execute_fn"](c, pid, "0xtok", 10.0, "2026-07-09T00:00:01Z")
    assert r["status"] == "filled"
    pos = m.get_position(c, r["position_id"])
    assert pos["execution_mode"] == "real" and pos["network_id"] == "8453"
    assert abs(pos["entry_price"] - 2.0) < 1e-9
    assert m.pending_row(c, pid)["nonce"] == 5                          # write-ahead nonce persisted


def _seq(vals):
    it = {"i": 0}

    def f(*_a, **_k):
        v = vals[min(it["i"], len(vals) - 1)]; it["i"] += 1
        return v
    return f


# --- direct venue quote seam -------------------------------------------------------------------


def test_executor_uses_injected_direct_quote_without_provider_lookup():
    seen = []

    def quote(sell, buy, amount, *, slippage_bps=None):
        seen.append((sell, buy, amount, slippage_bps))
        return {"buyAmount": "100", "transaction": {"to": "0x" + "44" * 20}}

    ex = S.build_real_evm_executor(
        acct=Account.create(), chain_id="4663", rpc_url="https://rpc",
        venue_quote_fn=quote, usdc_fn=lambda: 0.0, token_fn=lambda _m: 0.0,
    )

    assert ex["venue_quote_fn"]("0xa", "0xb", 1, slippage_bps=2500)["buyAmount"] == "100"
    assert seen == [("0xa", "0xb", 1, 2500)]

def _venue_probe(monkeypatch):
    from sniper.execution import evm_wallet, evm_dex_quote
    calls = {"direct": 0}
    monkeypatch.setattr(evm_wallet, "erc20_balance_raw", lambda *_a, **_k: 1000)
    monkeypatch.setattr(S, "quote_usd_price", lambda *_a, **_k: 2000.0)

    def direct(*_a, **_k):
        calls["direct"] += 1
        return {"buyAmount": "1000000000000000", "transaction": {"to": "0xrouter"}}

    monkeypatch.setattr(evm_dex_quote, "dex_v3_quote", direct)
    return calls


def test_executor_uses_direct_venue_on_4663_when_configured(monkeypatch):
    """The executor quote comes from the configured on-chain V3 quoter."""
    calls = _venue_probe(monkeypatch)
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_4663", "direct")
    ex = S.build_real_evm_executor(acct=Account.create(), chain_id="4663", rpc_url="https://rpc",
                                   http=lambda *_a, **_k: (200, {}, None),
                                   usdc_fn=lambda: 0.0, token_fn=lambda _m: 0.0)
    preview = ex["preview_exit_fn"]({"mint": "0xtok"})
    assert calls == {"direct": 1}
    assert preview is not None


def test_direct_4663_preview_falls_back_to_v2_exit_route(monkeypatch):
    """A funded V2-only bag must be previewable without widening the entry venue."""
    from sniper.execution import evm_dex_quote, evm_v2_exit_quote, evm_wallet

    calls = {"v3": 0, "v2": 0}
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_4663", "direct")
    monkeypatch.setattr(evm_wallet, "erc20_balance_raw", lambda *_a, **_k: 16_509)
    monkeypatch.setattr(S, "quote_usd_price", lambda *_a, **_k: 2_000.0)

    def no_v3(*_a, **_k):
        calls["v3"] += 1
        return None

    def v2(*_a, **kwargs):
        calls["v2"] += 1
        assert _a[3] == 16_509
        # The preview quotes at the FIRST rung so rung 0 can reuse its calldata (Codex P1).
        from sniper.settings.memecoin import MEMECOIN_EXIT_SLIPPAGE_LADDER as ladder
        assert kwargs["slippage_bps"] == int(round(ladder[0] * 100))
        return {"buyAmount": "20000000000000000",
                "transaction": {"to": "0xv2router"}}

    monkeypatch.setattr(evm_dex_quote, "dex_v3_quote", no_v3)
    monkeypatch.setattr(evm_v2_exit_quote, "dex_v2_exit_quote", v2)
    ex = S.build_real_evm_executor(
        acct=Account.create(), chain_id="4663", rpc_url="https://rpc",
        http=lambda *_a, **_k: (200, {}, None),
        usdc_fn=lambda: 0.0, token_fn=lambda _m: 0.0,
    )

    preview = ex["preview_exit_fn"]({"mint": "0xtok"})

    assert calls == {"v3": 1, "v2": 1}
    assert preview.expected_usdc == 40.0
    assert preview.route_id == "0xv2router"


def test_direct_4663_preview_falls_back_to_v4_after_v3_and_v2(monkeypatch):
    """A V4-funded bag must use the same V3 -> V2 -> V4 ladder on exit."""
    from sniper.execution import evm_dex_quote, evm_v2_exit_quote, evm_v4_quote, evm_wallet
    from sniper.settings import memecoin as cfg

    calls = []
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", True)
    monkeypatch.setattr(evm_wallet, "erc20_balance_raw", lambda *_a, **_k: 12_345)
    monkeypatch.setattr(S, "quote_usd_price", lambda *_a, **_k: 2_000.0)
    monkeypatch.setattr(evm_dex_quote, "dex_v3_quote",
                        lambda *_a, **_k: calls.append("v3") or None)
    monkeypatch.setattr(evm_v2_exit_quote, "dex_v2_exit_quote",
                        lambda *_a, **_k: calls.append("v2") or None)
    monkeypatch.setattr(evm_v4_quote, "dex_v4_quote",
                        lambda *_a, **_k: calls.append("v4") or {
                            "buyAmount": "20000000000000000",
                            "transaction": {"to": "0xv4router"},
                        })
    ex = S.build_real_evm_executor(
        acct=Account.create(), chain_id="4663", rpc_url="https://rpc",
        http=lambda *_a, **_k: (200, {}, None), usdc_fn=lambda: 0.0,
        token_fn=lambda _m: 0.0,
    )

    preview = ex["preview_exit_fn"]({"mint": "0xtok"})

    # The rungs run CONCURRENTLY (evm_real_seams: one thread per route, `as_completed`), so the
    # order the fakes were CALLED in is thread scheduling, not the ladder. CI lost this race three
    # runs in a row on 2026-09-03 (['v3', 'v4', 'v2']). The ladder's contract is which result is
    # SELECTED -- V3 and V2 answered None, so the V4 route must be the preview -- and that every
    # rung was asked.
    assert sorted(calls) == ["v2", "v3", "v4"]
    assert preview.expected_usdc == 40.0
    assert preview.route_id == "0xv4router"


def test_stored_v4_multihop_preview_uses_funded_http(monkeypatch):
    """A stored V4 route must use the funded RPC client on exit."""
    from sniper.execution import evm_v4_multihop, evm_wallet

    seen = {}

    def funded_http(*args, **kwargs):
        seen["funded_call"] = (args, kwargs)
        return 200, {}, None

    def multihop_exit(*_args, **kwargs):
        seen["http"] = kwargs.get("http")
        if seen["http"] is not None:
            seen["http"]("GET", "https://rpc")
        return {"buyAmount": "20000000000000000", "transaction": {"to": "0xv4router"}}

    monkeypatch.setattr(evm_wallet, "erc20_balance_raw", lambda *_a, **_k: 12_345)
    monkeypatch.setattr(S, "quote_usd_price", lambda *_a, **_k: 2_000.0)
    monkeypatch.setattr(evm_v4_multihop, "dex_v4_multihop_exit_quote", multihop_exit)
    ex = S.build_real_evm_executor(
        acct=Account.create(), chain_id="4663", rpc_url="https://rpc", http=funded_http,
        usdc_fn=lambda: 0.0, token_fn=lambda _m: 0.0,
    )

    preview = ex["preview_exit_fn"]({
        "mint": "0xtok",
        "evm_route_json": json.dumps({"protocol": "uniswap_v4_multihop", "bridge": "0xbridge"}),
    })

    assert seen["http"] is not None
    assert "funded_call" in seen
    assert preview.route_id == "0xv4router"


def test_direct_4663_preview_prefers_v3_without_calling_v2(monkeypatch):
    from sniper.execution import evm_dex_quote, evm_v2_exit_quote, evm_wallet

    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_4663", "direct")
    monkeypatch.setattr(evm_wallet, "erc20_balance_raw", lambda *_a, **_k: 1_000)
    monkeypatch.setattr(S, "quote_usd_price", lambda *_a, **_k: 2_000.0)
    monkeypatch.setattr(evm_dex_quote, "dex_v3_quote", lambda *_a, **_k: {
        "buyAmount": "10000000000000000", "transaction": {"to": "0xv3router"}})
    monkeypatch.setattr(evm_v2_exit_quote, "dex_v2_exit_quote",
                        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("V2 called")))
    ex = S.build_real_evm_executor(
        acct=Account.create(), chain_id="4663", rpc_url="https://rpc",
        http=lambda *_a, **_k: (200, {}, None),
        usdc_fn=lambda: 0.0, token_fn=lambda _m: 0.0,
    )

    preview = ex["preview_exit_fn"]({"mint": "0xtok"})

    assert preview.route_id == "0xv3router"


def test_direct_4663_venue_quote_gains_v2_fallback_unless_disabled(monkeypatch):
    """2026-07-16 (theveeman 0xf056…): entries + the pre-entry sell-route check now share the
    V3->V2 chain the real exit uses — fresh 4663 micros are born on V2-style pools. The old
    V3-only behavior survives behind MEMECOIN_EVM_V2_ENTRY_ENABLED=0."""
    from sniper.execution import evm_dex_quote
    from sniper.settings import memecoin as cfg

    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_4663", "direct")
    monkeypatch.setattr(evm_dex_quote, "dex_v3_quote", lambda *_a, **_k: None)
    v2_calls = []
    monkeypatch.setattr("sniper.execution.evm_v2_exit_quote.dex_v2_exit_quote",
                        lambda *a, **k: v2_calls.append(1) or {"buyAmount": "7"})
    ex = S.build_real_evm_executor(
        acct=Account.create(), chain_id="4663", rpc_url="https://rpc",
        http=lambda *_a, **_k: (200, {}, None),
        usdc_fn=lambda: 0.0, token_fn=lambda _m: 0.0,
    )
    assert ex["venue_quote_fn"]("0xtoken", "0xweth", 1_000)["buyAmount"] == "7"
    assert v2_calls == [1]
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", False)
    assert ex["venue_quote_fn"]("0xtoken", "0xweth", 1_000) is None
    assert v2_calls == [1]                     # V2 untouched when disabled


def test_evm_preview_quotes_exact_raw_balance_and_converts_to_usd(monkeypatch):
    from sniper.execution import evm_wallet

    acct = Account.create()
    seen = {}
    monkeypatch.setattr(evm_wallet, "erc20_balance_raw", lambda *_a, **_k: 987654321012345678)

    def quote(sell, buy, atomic, **_kwargs):
        seen.update(atomic=atomic)
        return {"buyAmount": str(17 * 10 ** 15), "transaction": {"to": "0xroute"}}

    # BASE IS WETH-QUOTED SINCE 2026-09-06: the venue returns wei and the mark is ETH/USD.
    # 0.017 WETH at $2,500 is the same $42.50 this test has always asserted.
    monkeypatch.setattr(S, "quote_usd_price", lambda *_a, **_k: 2500.0)
    ex = S.build_real_evm_executor(
        acct=acct, chain_id="8453", rpc_url="https://rpc", http=lambda *_a, **_k: (200, {}, None),
        venue_quote_fn=quote, usdc_fn=lambda: 0.0, token_fn=lambda _mint: 0.0,
    )
    preview = ex["preview_exit_fn"]({"mint": "0xtok"})
    assert preview.expected_usdc == 42.5
    assert preview.amount_atomic == 987654321012345678
    assert seen == {"atomic": 987654321012345678}


def test_evm_preview_returns_typed_provider_timeout(monkeypatch):
    from sniper.analysis.memecoin_quote_failures import QuoteFailure
    from sniper.execution import evm_wallet

    monkeypatch.setattr(evm_wallet, "erc20_balance_raw", lambda *_a, **_k: 987)
    def timeout_quote(*_a, **_k):
        raise TimeoutError("provider timed out")
    ex = S.build_real_evm_executor(
        acct=Account.create(), chain_id="8453", rpc_url="https://rpc",
        venue_quote_fn=timeout_quote, usdc_fn=lambda: 0.0, token_fn=lambda _mint: 0.0,
    )
    result = ex["preview_exit_fn"]({"mint": "0xtok"})
    assert result.failure == QuoteFailure.PROVIDER_TIMEOUT
    assert result.provider_id == "8453:direct"


def test_evm_balance_rpc_failure_is_infra_not_token_rejection():
    from sniper.analysis.memecoin_quote_failures import QuoteFailure

    ex = S.build_real_evm_executor(
        acct=Account.create(), chain_id="8453", rpc_url="https://rpc",
        http=lambda *_a, **_k: (429, {"error": "rate limited"}, None),
        venue_quote_fn=lambda *_a, **_k: None,
        usdc_fn=lambda: 0.0, token_fn=lambda _mint: 0.0,
    )
    result = ex["preview_exit_fn"]({"mint": "0xtok"})
    assert result.failure == QuoteFailure.RATE_LIMIT


def test_evm_balance_json_rpc_error_is_infra_not_token_rejection():
    from sniper.analysis.memecoin_quote_failures import QuoteFailure

    ex = S.build_real_evm_executor(
        acct=Account.create(), chain_id="8453", rpc_url="https://rpc",
        http=lambda *_a, **_k: (200, {"error": {"code": -32000, "message": "node busy"}}, None),
        venue_quote_fn=lambda *_a, **_k: None,
        usdc_fn=lambda: 0.0, token_fn=lambda _mint: 0.0,
    )
    result = ex["preview_exit_fn"]({"mint": "0xtok"})
    assert result.failure == QuoteFailure.RPC_TRANSPORT


# --- approval pipelining seam (2026-07-23): MEMECOIN_EVM_APPROVAL_PIPELINE=1 broadcasts the
#     approval WITHOUT polling its receipt and floors the swap nonce at approval+1 so a stale
#     pending count can never collide the two txs. ---

def _pipeline_http_recorder(nonce_hex="0x5", *, owner=None, mint="0xtok"):
    """`owner`: receipts book the swap (5 coins, 10 WETH) the way every chain books now."""
    from evm_receipt_fake import MISS, answer
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    seen = {"nonce_reads": 0, "receipt_polls": 0, "sends": 0}

    def http(method, url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        meth = req["method"]
        if meth == "eth_getTransactionReceipt":
            seen["receipt_polls"] += 1
        if owner is not None:
            hit = answer(meth, req.get("params") or [], owner=owner, mint=mint,
                         quote=CHAIN_QUOTE["8453"]["token"], coin=5, paid=10 * 10 ** 18)
            if hit is not MISS:
                return 200, {"result": hit}, None
        if meth == "eth_getTransactionCount":
            seen["nonce_reads"] += 1
            return 200, {"result": nonce_hex}, None          # STALE: never advances
        if meth == "eth_sendRawTransaction":
            seen["sends"] += 1
            return 200, {"result": "0xhash%d" % seen["sends"]}, None
        if meth == "eth_getTransactionReceipt":
            return 200, {"result": {"status": "0x1"}}, None
        if meth == "eth_gasPrice":
            return 200, {"result": "0x3b9aca00"}, None
        return 200, {"result": "0x0"}, None
    return http, seen


def test_pipelined_approve_skips_receipt_and_floors_swap_nonce(monkeypatch):
    # 8453 quotes in WETH since 2026-09-06, so sizing needs an ETH/USD mark. $1 keeps every
    # number below identical to the USDC era.
    monkeypatch.setattr(S, "quote_usd_price", lambda *_a, **_k: 1.0)
    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE", "1")
    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS", "8453")
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.reserve_pending(c, mint="0xtok", source_user_id="trader1", strategy="alpha_b",
                            notional=10.0, reserved_at="t", network_id="8453")
    swap_to = "0x" + "44" * 20

    _entries = {}

    def venue_quote(sell, buy, amount, *, slippage_bps=None):
        # DIRECTION-AWARE (2026-09-06). The entry ladder measures a round trip by quoting the
        # REVERSE of the bag it would buy. A stub answering "1" whichever way it is called reads as
        # a ~-100% round trip and the entry is then correctly refused -- the stub was wrong for a
        # ladder that measures, not the ladder. Keyed by TOKEN PAIR, never by call order, so a
        # second entry in the same test is not mistaken for the first one's reverse.
        pair = (str(sell).lower(), str(buy).lower())
        if pair[::-1] in _entries:
            return {"sellAmount": str(amount),
                    "buyAmount": str(int(_entries[pair[::-1]] * 0.97)),
                    "issues": {"allowance": None},
                    "transaction": {"to": swap_to, "data": "0x", "value": "0",
                                    "gas": "500000", "gasPrice": "1000000000"}}
        _entries[pair] = int(amount)
        return {"sellAmount": str(amount), "buyAmount": "1",
                "issues": {"allowance": {"spender": swap_to}},
                "transaction": {"to": swap_to, "data": "0x", "value": "0",
                                "gas": "500000", "gasPrice": "1000000000"}}

    acct = Account.create()
    http, seen = _pipeline_http_recorder(owner=acct.address)
    ex = S.build_real_evm_executor(acct=acct, chain_id="8453", rpc_url="https://rpc",
                                   http=http, venue_quote_fn=venue_quote,
                                   usdc_fn=_seq([100.0, 90.0]), token_fn=_seq([0.0, 5.0]))
    r = ex["execute_fn"](c, pid, "0xtok", 10.0, "2026-07-09T00:00:01Z")
    assert r["status"] == "filled"
    # approval + swap both broadcast; ONLY the swap's receipt is ever polled
    assert seen["sends"] == 2
    assert seen["receipt_polls"] >= 1
    assert seen["nonce_reads"] == 1
    # the swap's write-ahead nonce is approval(5)+1 even though pending count stayed 0x5
    assert m.pending_row(c, pid)["nonce"] == 6


def test_pipelined_approve_fails_closed_on_ambiguous_broadcast(monkeypatch):
    """2026-07-23 review P0: eth_send_raw is tri-state — a hash/`already_known` is accepted,
    `False` is a positive reject, and `None` is transport/unclassified AMBIGUITY (the approval
    MAY or may not have landed). Under pipelining, `None` must NOT be treated as accepted: the
    swap would sign at approval-nonce+1 on a maybe-missing approval, queue behind the gap, and a
    later tx at that nonce could unlock it. Fail closed — the swap must NOT broadcast."""
    # 8453 quotes in WETH since 2026-09-06, so sizing needs an ETH/USD mark. $1 keeps every
    # number below identical to the USDC era.
    monkeypatch.setattr(S, "quote_usd_price", lambda *_a, **_k: 1.0)
    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE", "1")
    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS", "8453")
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.reserve_pending(c, mint="0xtok", source_user_id="trader1", strategy="alpha_b",
                            notional=10.0, reserved_at="t", network_id="8453")
    swap_to = "0x" + "44" * 20

    _entries = {}

    def venue_quote(sell, buy, amount, *, slippage_bps=None):
        # DIRECTION-AWARE (2026-09-06). The entry ladder measures a round trip by quoting the
        # REVERSE of the bag it would buy. A stub answering "1" whichever way it is called reads as
        # a ~-100% round trip and the entry is then correctly refused -- the stub was wrong for a
        # ladder that measures, not the ladder. Keyed by TOKEN PAIR, never by call order, so a
        # second entry in the same test is not mistaken for the first one's reverse.
        pair = (str(sell).lower(), str(buy).lower())
        if pair[::-1] in _entries:
            return {"sellAmount": str(amount),
                    "buyAmount": str(int(_entries[pair[::-1]] * 0.97)),
                    "issues": {"allowance": None},
                    "transaction": {"to": swap_to, "data": "0x", "value": "0",
                                    "gas": "500000", "gasPrice": "1000000000"}}
        _entries[pair] = int(amount)
        return {"sellAmount": str(amount), "buyAmount": "1",
                "issues": {"allowance": {"spender": swap_to}},
                "transaction": {"to": swap_to, "data": "0x", "value": "0",
                                "gas": "500000", "gasPrice": "1000000000"}}

    seen = {"sends": 0}

    def http(method, url, headers=None, body=None, timeout=10):
        meth = json.loads(body)["method"]
        if meth == "eth_getTransactionCount":
            return 200, {"result": "0x5"}, None
        if meth == "eth_sendRawTransaction":
            seen["sends"] += 1
            # ambiguous: no result, unclassified error -> eth_send_raw returns None
            return 200, {"error": {"message": "gateway timeout, please retry"}}, None
        if meth == "eth_getTransactionReceipt":
            return 200, {"result": {"status": "0x1"}}, None   # pre-fix: swap would "confirm"
        if meth == "eth_gasPrice":
            return 200, {"result": "0x3b9aca00"}, None
        return 200, {"result": "0x0"}, None

    ex = S.build_real_evm_executor(acct=Account.create(), chain_id="8453", rpc_url="https://rpc",
                                   http=http, venue_quote_fn=venue_quote,
                                   usdc_fn=_seq([100.0, 90.0]), token_fn=_seq([0.0, 5.0]))
    r = ex["execute_fn"](c, pid, "0xtok", 10.0, "2026-07-09T00:00:01Z")
    assert r["status"] != "filled"                 # entry aborted on the ambiguous approval
    assert seen["sends"] == 1                       # ONLY the approval attempt — swap never broadcast


def test_pipelined_rejected_approval_does_not_poison_next_order_nonce(monkeypatch):
    """A provider-level explicit rejection means nonce 5 was not accepted.

    The next serialized order must reuse 5 for its approval and sign its swap at 6. Advancing the
    process floor on the rejected send creates an artificial nonce gap and stalls later orders.
    """
    # 8453 quotes in WETH since 2026-09-06, so sizing needs an ETH/USD mark. $1 keeps every
    # number below identical to the USDC era.
    monkeypatch.setattr(S, "quote_usd_price", lambda *_a, **_k: 1.0)
    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE", "1")
    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS", "8453")
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    swap_to = "0x" + "44" * 20

    _entries = {}

    def venue_quote(sell, buy, amount, *, slippage_bps=None):
        # DIRECTION-AWARE (2026-09-06). The entry ladder measures a round trip by quoting the
        # REVERSE of the bag it would buy. A stub answering "1" whichever way it is called reads as
        # a ~-100% round trip and the entry is then correctly refused -- the stub was wrong for a
        # ladder that measures, not the ladder. Keyed by TOKEN PAIR, never by call order, so a
        # second entry in the same test is not mistaken for the first one's reverse.
        pair = (str(sell).lower(), str(buy).lower())
        if pair[::-1] in _entries:
            return {"sellAmount": str(amount),
                    "buyAmount": str(int(_entries[pair[::-1]] * 0.97)),
                    "issues": {"allowance": None},
                    "transaction": {"to": swap_to, "data": "0x", "value": "0",
                                    "gas": "500000", "gasPrice": "1000000000"}}
        _entries[pair] = int(amount)
        return {"sellAmount": str(amount), "buyAmount": "1",
                "issues": {"allowance": {"spender": swap_to}},
                "transaction": {"to": swap_to, "data": "0x", "value": "0",
                                "gas": "500000", "gasPrice": "1000000000"}}

    seen = {"sends": 0}
    from evm_receipt_fake import MISS, answer
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    acct = Account.create()

    def http(method, url, headers=None, body=None, timeout=10):
        meth = json.loads(body)["method"]
        if meth == "eth_getTransactionCount":
            return 200, {"result": "0x5"}, None
        if meth == "eth_sendRawTransaction":
            seen["sends"] += 1
            if seen["sends"] == 1:
                return 200, {"error": {"code": -32000, "message": "insufficient funds"}}, None
            return 200, {"result": "0xhash%d" % seen["sends"]}, None
        hit = answer(meth, json.loads(body).get("params") or [], owner=acct.address, mint="0xtok2",
                     quote=CHAIN_QUOTE["8453"]["token"], coin=5, paid=10 * 10 ** 18)
        if hit is not MISS:
            return 200, {"result": hit}, None
        if meth == "eth_gasPrice":
            return 200, {"result": "0x3b9aca00"}, None
        return 200, {"result": "0x0"}, None

    ex = S.build_real_evm_executor(
        acct=acct, chain_id="8453", rpc_url="https://rpc", http=http,
        venue_quote_fn=venue_quote, usdc_fn=_seq([100.0, 100.0, 90.0]),
        token_fn=_seq([0.0, 0.0, 5.0]),
    )
    first = m.reserve_pending(c, mint="0xtok1", source_user_id="trader1", strategy="alpha_b",
                              notional=10.0, reserved_at="t1", network_id="8453")
    assert ex["execute_fn"](c, first, "0xtok1", 10.0, "2026-07-09T00:00:01Z")["status"] == "aborted"

    second = m.reserve_pending(c, mint="0xtok2", source_user_id="trader2", strategy="alpha_b",
                               notional=10.0, reserved_at="t2", network_id="8453")
    result = ex["execute_fn"](c, second, "0xtok2", 10.0, "2026-07-09T00:00:02Z")

    assert result["status"] == "filled"
    assert m.pending_row(c, second)["nonce"] == 6


def test_legacy_approve_still_waits_for_receipt_by_default(monkeypatch):
    # 8453 quotes in WETH since 2026-09-06, so sizing needs an ETH/USD mark. $1 keeps every
    # number below identical to the USDC era.
    monkeypatch.setattr(S, "quote_usd_price", lambda *_a, **_k: 1.0)
    monkeypatch.delenv("MEMECOIN_EVM_APPROVAL_PIPELINE", raising=False)
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.reserve_pending(c, mint="0xtok", source_user_id="trader1", strategy="alpha_b",
                            notional=10.0, reserved_at="t", network_id="8453")
    swap_to = "0x" + "44" * 20

    _entries = {}
    buy_slippage = []

    def venue_quote(sell, buy, amount, *, slippage_bps=None):
        if str(buy).lower() == "0xtok":
            buy_slippage.append(slippage_bps)
        # DIRECTION-AWARE (2026-09-06). The entry ladder measures a round trip by quoting the
        # REVERSE of the bag it would buy. A stub answering "1" whichever way it is called reads as
        # a ~-100% round trip and the entry is then correctly refused -- the stub was wrong for a
        # ladder that measures, not the ladder. Keyed by TOKEN PAIR, never by call order, so a
        # second entry in the same test is not mistaken for the first one's reverse.
        pair = (str(sell).lower(), str(buy).lower())
        if pair[::-1] in _entries:
            return {"sellAmount": str(amount),
                    "buyAmount": str(int(_entries[pair[::-1]] * 0.97)),
                    "issues": {"allowance": None},
                    "transaction": {"to": swap_to, "data": "0x", "value": "0",
                                    "gas": "500000", "gasPrice": "1000000000"}}
        _entries[pair] = int(amount)
        return {"sellAmount": str(amount), "buyAmount": "1",
                "issues": {"allowance": {"spender": swap_to}},
                "transaction": {"to": swap_to, "data": "0x", "value": "0",
                                "gas": "500000", "gasPrice": "1000000000"}}

    acct = Account.create()
    http, seen = _pipeline_http_recorder(owner=acct.address)
    ex = S.build_real_evm_executor(acct=acct, chain_id="8453", rpc_url="https://rpc",
                                   http=http, venue_quote_fn=venue_quote,
                                   usdc_fn=_seq([100.0, 90.0]), token_fn=_seq([0.0, 5.0]))
    r = ex["execute_fn"](c, pid, "0xtok", 10.0, "2026-07-09T00:00:01Z")
    assert r["status"] == "filled"
    # legacy: approval receipt polled (>=2 total: approval + swap)
    assert seen["receipt_polls"] >= 2
    assert len(buy_slippage) >= 2  # initial quote and post-approval requote
    assert buy_slippage == [300] * len(buy_slippage)


def test_approval_pipeline_is_chain_scoped(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE", "1")
    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS", "4663")
    from sniper.settings import memecoin as cfg
    assert cfg.memecoin_evm_approval_pipeline("4663") is True
    assert cfg.memecoin_evm_approval_pipeline("8453") is False


# --- entry round-trip reduction (2026-07-28): one live read per entry window, not per consumer ---

def test_quote_price_read_once_per_entry_window(monkeypatch):
    """Gate sizing and the executor accounting snapshot both need ETH/USD; two Binance round trips
    (286ms each) were paid for one number inside a single 4663 entry."""
    calls = []

    def price_fn():
        calls.append(1)
        return 1900.0

    ex = S.build_real_evm_executor(
        acct=Account.create(), chain_id="4663", rpc_url="rpc", price_fn=price_fn,
        http=lambda *_a, **_k: (200, {"result": "0x1"}, None))
    assert ex["usdc_fn"]() is not None
    ex["usdc_fn"]()
    ex["usdc_fn"]()
    assert len(calls) == 1, "one live read should serve the whole entry window"


def test_quote_price_refetches_after_the_window(monkeypatch):
    """The window is shorter than one entry attempt — a stalled attempt re-reads live, never sizes
    on a stale mark. This is the funded live-pricing invariant, not a cache."""
    calls = []
    clock = {"t": 1000.0}
    monkeypatch.setattr(S._time, "monotonic", lambda: clock["t"])

    def price_fn():
        calls.append(1)
        return 1900.0

    ex = S.build_real_evm_executor(
        acct=Account.create(), chain_id="4663", rpc_url="rpc", price_fn=price_fn,
        http=lambda *_a, **_k: (200, {"result": "0x1"}, None))
    ex["usdc_fn"]()
    clock["t"] += 5.0                                   # well past the window
    ex["usdc_fn"]()
    assert len(calls) == 2


def test_node_gas_price_collapses_approval_and_signing_reads(monkeypatch):
    """One entry read gasPrice twice (approval, then swap signing) = 2 x ~160ms on 4663."""
    calls = []

    import json as _json

    def http(_method, _url, body=None, **_k):
        payload = _json.loads(body) if isinstance(body, str) else (body or {})
        if payload.get("method") == "eth_gasPrice":
            calls.append(1)
            return 200, {"result": hex(1_000_000_000)}, None
        return 200, {"result": "0x1"}, None

    ex = S.build_real_evm_executor(
        acct=Account.create(), chain_id="4663", rpc_url="rpc", http=http)
    first = ex["_node_gas_price"]()
    assert first == int(1_000_000_000 * 1.5)
    assert ex["_node_gas_price"]() == first
    assert len(calls) == 1, "approval + signing must share one gasPrice round trip"


def test_bsc_approval_nonce_and_gas_reads_overlap_before_signing(monkeypatch):
    import threading
    gas_started = threading.Event()
    nonce_started = threading.Event()
    calls = []
    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE", "1")
    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS", "56")

    def nonce(*_a, **_k):
        calls.append("nonce")
        nonce_started.set()
        assert gas_started.wait(1.0), "gas still waits behind nonce"
        return 5

    def gas(*_a, **_k):
        calls.append("gas")
        gas_started.set()
        assert nonce_started.wait(1.0)
        return 10**9

    monkeypatch.setattr(S, "eth_nonce", nonce)
    monkeypatch.setattr(S, "eth_gas_price", gas)
    signed = []
    monkeypatch.setattr(S, "sign_tx", lambda tx, _acct: (signed.append(tx) or ("raw", "hash")))
    ex = S.build_real_evm_executor(acct=Account.create(), chain_id="56", rpc_url="rpc")
    result = ex["_approve"]("0x" + "33" * 20, 100, send_fn=lambda _raw: "accepted")
    assert result["nonce"] == 5
    assert sorted(calls) == ["gas", "nonce"]
    assert signed[0]["nonce"] == 5 and signed[0]["gasPrice"] > 0
    # Acceptance still alone advances the next nonce, and an unreadable gas price never signs.
    signed.clear()
    ex2 = S.build_real_evm_executor(acct=Account.create(), chain_id="56", rpc_url="rpc")
    monkeypatch.setattr(S, "eth_gas_price", lambda *_a, **_k: None)
    assert ex2["_approve"]("0x" + "33" * 20, 100, send_fn=lambda _raw: "accepted") is False
    assert not signed


# --- chain 56 fast initial receipt window (2026-09-26) -------------------------------------------
# A BSC approval is included ~0.39 s after the send, but the 2 s poll saw it ~1.8 s later, and the
# swap receipt the same: 56 j7 accept -> confirmation seen p50 2.067 s / p90 2.607 s (n=36, 30 d).
# BSC blocks are ~0.45 s. The receipt below appears at 0.5 s of (fake) time: the old cadence
# first looks again at 2.0 s, the fast window at 0.5 s.

def _clocked_http(t, appear_at=0.5):
    def http(method, url, headers=None, body=None, timeout=None):
        req = json.loads(body) if body else {}
        m_ = req.get("method")
        if m_ == "eth_getTransactionReceipt":
            return 200, {"result": {"status": "0x1"} if t[0] >= appear_at else None}, None
        if m_ == "eth_getTransactionCount":
            return 200, {"result": "0x5"}, None
        if m_ == "eth_gasPrice":
            return 200, {"result": hex(10 ** 9)}, None
        return 200, {"result": "0x1"}, None
    return http


def _fake_sleep(monkeypatch, t):
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: t.__setitem__(0, t[0] + s))


def test_56_swap_receipt_is_seen_inside_the_fast_window(monkeypatch):
    t = [0.0]
    _fake_sleep(monkeypatch, t)
    ex = S.build_real_evm_executor(acct=Account.create(), chain_id="56", rpc_url="rpc",
                                   http=_clocked_http(t))
    assert ex["_confirm"]("0x" + "ab" * 32) == "confirmed"
    assert t[0] <= 0.5                      # was 2.0: the second read of a 2 s cadence


def test_56_approval_receipt_is_seen_inside_the_fast_window(monkeypatch):
    t = [0.0]
    _fake_sleep(monkeypatch, t)
    ex = S.build_real_evm_executor(acct=Account.create(), chain_id="56", rpc_url="rpc",
                                   http=_clocked_http(t))
    assert ex["_approve"]("0x" + "33" * 20, 10 ** 6,
                          send_fn=lambda raw: "0x" + "cd" * 32) is True
    assert t[0] <= 0.5


def test_56_reoffers_still_fire_at_4s_and_14s(monkeypatch):
    calls = []
    monkeypatch.setattr(S, "eth_confirm", lambda *a, **kw: calls.append(kw) or "unknown")
    ex = S.build_real_evm_executor(acct=Account.create(), chain_id="56", rpc_url="rpc",
                                   http=lambda *_a, **_k: (200, {"result": "0x1"}, None))
    tx = {"to": "0x" + "22" * 20, "data": "0x", "value": 0, "gas": 21000,
          "gasPrice": 10 ** 9, "nonce": 1, "chainId": 56}
    raw, tx_hash = S.sign_tx(tx, Account.create())
    ex["_remember_sent"](lambda r: tx_hash)(raw)
    ex["_confirm"](tx_hash)
    kw = calls[0]

    def at(k):                              # seconds elapsed when attempt k runs
        fast = min(k, kw["fast_attempts"])
        return kw["fast_poll_s"] * fast + kw["poll_s"] * (k - fast)
    assert [at(k) for k in kw["resend_at_attempts"]] == [4.0, 14.0]
    assert at(kw["max_attempts"]) <= 60.0


@pytest.mark.parametrize("chain", ["4663", "5042"])
def test_other_chains_keep_todays_approval_cadence(monkeypatch, chain):
    # Base's bounded fast receipt window is covered in test_base_receipt_poll_window.
    # 4663's publicnode carries the live tape and 5042's approval cadence stays unchanged.
    calls = []
    monkeypatch.setattr(S, "eth_confirm", lambda *a, **kw: calls.append(kw) or "confirmed")
    t = [0.0]
    ex = S.build_real_evm_executor(acct=Account.create(), chain_id=chain, rpc_url="rpc",
                                   http=_clocked_http(t))
    assert ex["_approve"]("0x" + "33" * 20, 10 ** 6, send_fn=lambda raw: "0x" + "cd" * 32) is True
    assert [{k: v for k, v in kw.items() if k not in ("http", "resend_fn", "resend_at_attempts")}
            for kw in calls] == [{"max_attempts": 30}]


@pytest.mark.parametrize("first_send", ["accepted", "ambiguous"])
def test_base_approval_reoffers_identical_bytes_before_full_receipt_timeout(monkeypatch, first_send):
    t = [0.0]
    _fake_sleep(monkeypatch, t)
    sent = []
    base_http = _clocked_http(t)

    def http(*args, body=None, **kwargs):
        req = json.loads(body)
        if req["method"] == "eth_getTransactionReceipt":
            return 200, {"result": {"status": "0x1"} if len(sent) >= 2 else None}, None
        return base_http(*args, body=body, **kwargs)

    def send(raw):
        sent.append(raw)
        return None if first_send == "ambiguous" and len(sent) == 1 else "0x" + "cd" * 32

    ex = S.build_real_evm_executor(acct=Account.create(), chain_id="8453", rpc_url="rpc", http=http)
    assert ex["_approve"]("0x" + "33" * 20, 10 ** 6, send_fn=send) is True
    assert len(sent) == 2 and sent[0] == sent[1]
    assert t[0] <= 6.0


@pytest.mark.parametrize("outcome", ["rejected", "failed", "unknown"])
def test_base_approval_never_allows_swap_without_confirmed_receipt(monkeypatch, outcome):
    t = [0.0]
    _fake_sleep(monkeypatch, t)
    sent = []
    base_http = _clocked_http(t)

    def http(*args, body=None, **kwargs):
        req = json.loads(body)
        if req["method"] == "eth_getTransactionReceipt":
            return 200, {"result": {"status": "0x0"} if outcome == "failed" else None}, None
        return base_http(*args, body=body, **kwargs)

    ex = S.build_real_evm_executor(acct=Account.create(), chain_id="8453", rpc_url="rpc", http=http)
    assert ex["_approve"]("0x" + "33" * 20, 10 ** 6,
                          send_fn=lambda raw: sent.append(raw) or (False if outcome == "rejected" else "accepted")) is False
    assert len(set(sent)) == 1
    if outcome == "rejected":
        assert len(sent) == 1 and t[0] == 0
    assert t[0] <= 60.0
