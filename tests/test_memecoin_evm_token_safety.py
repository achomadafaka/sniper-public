"""Item 8 — EVM pre-entry token safety. EVM honeypots are nastier than Solana's: the load-bearing
checks are the sell-route probe and an eth_call SELL SIMULATION (a token you can buy but not sell
reverts on the simulated sell). Plus buy/sell tax asymmetry (fee-on-transfer) and a contract-code
check. Fail-CLOSED: any unreadable probe => unsafe => don't enter."""
import json
import threading
from contextlib import contextmanager
import pytest

from sniper.execution.evm_token_safety import (
    build_evm_token_safety,
    build_lifi_token_safety,
    token_safety_ok_evm,
)


def _ok_seams(**over):
    seams = dict(code_fn=lambda m: "0x60006000",           # non-empty contract code
                 sell_route_fn=lambda m: True,
                 sell_sim_fn=lambda m: True)
    seams.update(over)
    return seams


def test_all_pass_is_safe():
    ok, reason = token_safety_ok_evm("0xtok", **_ok_seams())
    assert ok and reason == "ok"


def test_not_a_contract_is_unsafe():
    ok, reason = token_safety_ok_evm("0xtok", **_ok_seams(code_fn=lambda m: "0x"))
    assert not ok and "contract" in reason.lower()


def test_missing_code_read_is_a_provider_refusal_not_a_contract_verdict():
    ok, reason = token_safety_ok_evm("0xtok", **_ok_seams(code_fn=lambda m: None))
    assert (ok, reason) == (False, "code read failed (fail-closed)")


def test_missing_configured_route_does_not_claim_global_unsellability():
    ok, reason = token_safety_ok_evm(
        "0xtok", sell_route_label="direct_v3_single_hop",
        **_ok_seams(sell_route_fn=lambda m: False),
    )
    assert not ok
    assert reason == (
        "configured sell venue returned no executable quote "
        "(route=direct_v3_single_hop; other venues not tested)"
    )
    assert "can't exit" not in reason.lower()


def test_sell_simulation_revert_is_unsafe():
    ok, reason = token_safety_ok_evm("0xtok", **_ok_seams(sell_sim_fn=lambda m: False))
    assert not ok and "simulation" in reason.lower()


def test_probe_exceptions_fail_closed():
    def boom(m):
        raise RuntimeError("rpc down")
    for k in ("code_fn", "sell_route_fn", "sell_sim_fn"):
        ok, reason = token_safety_ok_evm("0xtok", **_ok_seams(**{k: boom}))
        assert not ok and "fail-closed" in reason.lower()


def test_lifi_safety_keeps_contract_code_gate_without_direct_route_gate():
    safe = build_lifi_token_safety(code_fn=lambda _mint: "0x6000")
    missing = build_lifi_token_safety(code_fn=lambda _mint: "0x")
    failed = build_lifi_token_safety(
        code_fn=lambda _mint: (_ for _ in ()).throw(RuntimeError("rpc")))
    unavailable = build_lifi_token_safety(code_fn=lambda _mint: None)

    assert safe("0xtok") == (True, "ok")
    assert missing("0xtok") == (False, "not a contract (no code at address)")
    assert failed("0xtok") == (False, "code read failed (fail-closed)")
    assert unavailable("0xtok") == (False, "code read failed (fail-closed)")


def test_bound_safety_reports_a_typed_deadline_refusal():
    seen = {}

    @contextmanager
    def deadline_scope(deadline):
        seen["deadline"] = deadline
        yield lambda: True

    def http(*_args, **_kwargs):
        return 200, {"result": "0x6000"}, None

    safety = build_evm_token_safety(
        chain_id="8453", rpc_url="https://rpc", taker="0x" + "11" * 20,
        http=http, quote_fn=lambda *_args: None, safety_deadline_scope=deadline_scope,
    )

    assert safety("0xtok", deadline=42.0) == (False, "safety_deadline_exceeded")
    assert seen == {"deadline": 42.0}


# --- bound factory: the REAL sell_sim_fn must not call every token a honeypot ---
#
# Regression (2026-07-14): the bound sell_sim_fn eth_call'd the venue sell tx FROM our own wallet with
# no state override. We hold 0 balance / 0 allowance of a coin we have not bought yet, so the call
# reverted on insufficient balance for EVERY token — honeypot or not — and any revert was mapped to
# "sell simulation reverted (honeypot)". Verified live against DEGEN on Base (a blue-chip): it was
# refused as a honeypot. Result: the EVM leg refused 27/28 real entries and never opened a single
# real position. The seam-injected unit tests above all missed it because they stub sell_sim_fn=True.
#
# Fixture models the RPC truthfully: the sell eth_call REVERTS unless the call carries a state
# override that funds our balance slot AND our allowance slot. Standard ERC-20 layout (as DEGEN):
# balanceOf -> mapping at slot 0, allowance -> mapping at slot 1.

TAKER = "0x898BE9c27f8B391C47A3d9F2dA17781aa8aA3A1d"
ROUTER = "0x0000000000001ff3684f28c67538d4d072c22734"
PERMIT2 = "0x000000000022d473030f116ddee9f6b43ac78ba3"
_BIG = 10 ** 24


def _slot(*words):
    from eth_utils import keccak
    buf = b""
    for w in words:
        buf += w.to_bytes(32, "big") if isinstance(w, int) else bytes.fromhex(w[2:].rjust(64, "0"))
    return "0x" + keccak(buf).hex()


def _standard_erc20_http(mint_addr, *, sell_blocked=False, balance_slot=0, allowance_slot=1,
                         allowance_spender=ROUTER, required_balance=1):
    """A normal, sellable ERC-20 (unless sell_blocked=True -> a REAL honeypot: reverts even when
    funded). Sell simulation succeeds ONLY when the eth_call carries balance+allowance overrides."""
    bal_key = _slot(TAKER, balance_slot)
    allow_key = _slot(allowance_spender, _slot(TAKER, allowance_slot))

    def http(method, url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        meth, params = req["method"], req.get("params") or []
        if meth == "eth_getCode":
            return 200, {"result": "0x60006000"}, None
        if meth != "eth_call":
            return 200, {"result": "0x"}, None
        call = params[0]
        data = call.get("data") or ""
        override = params[2] if len(params) > 2 else {}
        diff = {}
        for acct in override.values():
            diff.update(acct.get("stateDiff") or {})
        if data.startswith("0x313ce567"):                        # decimals()
            return 200, {"result": "0x" + format(18, "x")}, None
        if data.startswith("0x70a08231"):                        # balanceOf(addr) -> slot probe
            return 200, {"result": diff.get(bal_key, "0x0")}, None
        if data.startswith("0xdd62ed3e"):                        # allowance(o,s) -> slot probe
            return 200, {"result": diff.get(allow_key, "0x0")}, None
        # the venue sell transaction itself
        funded = (int(diff.get(bal_key, "0x0"), 16) >= required_balance
                  and int(diff.get(allow_key, "0x0"), 16) >= required_balance)
        if sell_blocked or not funded:
            return 200, {"error": {"code": -32000, "message": "execution reverted"}}, None
        return 200, {"result": "0x"}, None
    return http


def _solady_erc20_http(mint_addr, *, permit2_infinite=False):
    """Solady ERC20 stores balances/allowances under compact hashed seeds, not mapping slots."""
    from eth_utils import keccak

    owner = bytes.fromhex(TAKER[2:])
    spender = bytes.fromhex(ROUTER[2:])
    bal_key = "0x" + keccak(owner + bytes(8) + bytes.fromhex("87a211a2")).hex()
    allow_key = "0x" + keccak(
        owner + bytes(8) + bytes.fromhex("7f5e9f20") + spender
    ).hex()

    def http(method, url, headers=None, body=None, timeout=10):
        if method == "GET":
            return 200, {"price": "3000"}, None
        req = json.loads(body)
        meth, params = req["method"], req.get("params") or []
        if meth == "eth_getCode":
            return 200, {"result": "0x60006000"}, None
        if meth != "eth_call":
            return 200, {"result": "0x"}, None
        call = params[0]
        data = call.get("data") or ""
        override = params[2] if len(params) > 2 else {}
        diff = (override.get(mint_addr) or {}).get("stateDiff") or {}
        if data.startswith("0x313ce567"):
            return 200, {"result": "0x" + format(18, "x")}, None
        if data.startswith("0x70a08231"):
            return 200, {"result": diff.get(bal_key, "0x0")}, None
        if data.startswith("0xdd62ed3e"):
            if permit2_infinite:
                return 200, {"result": hex(2**256 - 1)}, None
            return 200, {"result": diff.get(allow_key, "0x0")}, None
        funded = int(diff.get(bal_key, "0x0"), 16) > 0 and (
            permit2_infinite or int(diff.get(allow_key, "0x0"), 16) > 0)
        if not funded:
            return 200, {"error": {"code": -32000, "message": "execution reverted"}}, None
        return 200, {"result": "0x"}, None

    return http


def _standard_quote(mint_addr, calls=None):
    """Direct quote seam with a sane 2% round trip for the safety-boundary tests."""
    def quote(sell_token, buy_token, amount_atomic, *, slippage_bps=None):
        if calls is not None:
            calls.append((sell_token.lower(), buy_token.lower(), int(amount_atomic)))
        selling_token = sell_token.lower() == mint_addr.lower()
        out = (int(amount_atomic * 0.98 / 10 ** 12)
               if selling_token else int(amount_atomic) * 10 ** 12)
        return {
            "sellAmount": str(amount_atomic), "buyAmount": str(out),
            "transaction": {"to": ROUTER, "data": "0xdeadbeef", "value": "0"},
            "issues": {"allowance": {"spender": ROUTER}},
        }
    return quote


def _error_string_data(text):
    payload = text.encode()
    padded = payload.hex() + "0" * ((32 - len(payload) % 32) % 32 * 2)
    return "0x08c379a0" + format(0x20, "064x") + format(len(payload), "064x") + padded


def test_sellable_token_we_do_not_hold_is_not_a_honeypot():
    """A normal ERC-20 we hold ZERO of must pass the sell simulation. This is the regression."""
    fn = build_evm_token_safety(chain_id="8453", rpc_url="https://rpc", taker=TAKER,
                                http=_standard_erc20_http("0xdegen"),
                                quote_fn=_standard_quote("0xdegen"))
    ok, reason = fn("0xdegen")
    assert ok, f"sellable token misclassified: {reason}"


def test_contract_code_and_sell_route_reads_start_in_parallel():
    mint = "0x" + "31" * 20
    inner = _standard_erc20_http(mint)
    code_started = threading.Event()
    quote_started = threading.Event()
    overlap = {}

    def http(method, url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        if req["method"] == "eth_getCode":
            code_started.set()
            overlap["code"] = quote_started.wait(0.2)
        return inner(method, url, headers=headers, body=body, timeout=timeout)

    base_quote = _standard_quote(mint)

    def quote(*args, **kwargs):
        quote_started.set()
        overlap["quote"] = code_started.wait(0.2)
        return base_quote(*args, **kwargs)

    safety = build_evm_token_safety(
        chain_id="4663", rpc_url="https://rpc", taker=TAKER,
        http=http, quote_fn=quote,
    )

    assert safety(mint) == (True, "ok")
    assert overlap == {"code": True, "quote": True}


def test_balance_and_allowance_slot_reads_start_in_parallel():
    mint = "0x" + "32" * 20
    inner = _standard_erc20_http(mint)
    balance_started = threading.Event()
    allowance_started = threading.Event()
    overlap = {}

    def http(method, url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        first = (req.get("params") or [{}])[0]
        data = (first.get("data") or "") if isinstance(first, dict) else ""
        if data.startswith("0x70a08231"):
            balance_started.set()
            overlap.setdefault("balance", allowance_started.wait(0.2))
        elif data.startswith("0xdd62ed3e"):
            allowance_started.set()
            overlap.setdefault("allowance", balance_started.wait(0.2))
        return inner(method, url, headers=headers, body=body, timeout=timeout)

    safety = build_evm_token_safety(
        chain_id="4663", rpc_url="https://rpc", taker=TAKER,
        http=http, quote_fn=_standard_quote(mint),
    )

    assert safety(mint) == (True, "ok")
    assert overlap == {"balance": True, "allowance": True}


def test_parallel_safety_reads_keep_the_funded_deadline_scope():
    mint = "0x" + "33" * 20
    inner = _standard_erc20_http(mint)
    state = threading.local()
    scoped_reads = []

    @contextmanager
    def deadline_scope(_deadline):
        state.active = True
        try:
            yield lambda: False
        finally:
            state.active = False

    def http(method, url, headers=None, body=None, timeout=10):
        scoped_reads.append(bool(getattr(state, "active", False)))
        return inner(method, url, headers=headers, body=body, timeout=timeout)

    safety = build_evm_token_safety(
        chain_id="4663", rpc_url="https://rpc", taker=TAKER,
        http=http, quote_fn=_standard_quote(mint),
        safety_deadline_scope=deadline_scope,
    )

    assert safety(mint, deadline=42.0) == (True, "ok")
    assert scoped_reads and all(scoped_reads)


def test_safety_uses_one_sell_quote_without_a_round_trip_tax_probe():
    """GHOST: a sellable 4663 entry must not add two quote calls for a small round trip."""
    mint = "0x" + "45" * 20
    calls = []
    inner = _standard_erc20_http(mint)

    def http(method, url, headers=None, body=None, timeout=10):
        if method == "GET":
            return 200, {"price": "3000"}, None
        return inner(method, url, headers=headers, body=body, timeout=timeout)

    def quote(sell_token, buy_token, amount_atomic, *, slippage_bps=None):
        calls.append((sell_token, buy_token, amount_atomic))
        return {
            "sellAmount": str(amount_atomic),
            "buyAmount": str(int(amount_atomic) * 98 // 100),
            "transaction": {"to": ROUTER, "data": "0xdeadbeef", "value": "0"},
            "issues": {"allowance": {"spender": ROUTER}},
        }

    fn = build_evm_token_safety(
        chain_id="4663", rpc_url="https://rpc", taker=TAKER, http=http, quote_fn=quote,
    )

    assert fn(mint) == (True, "ok")
    assert len(calls) == 1
    assert calls[0][0].lower() == mint.lower()


def test_funded_sell_simulation_uses_quote_allowance_spender_not_transaction_target():
    """V4 executes at Universal Router, but Permit2 pulls the token from the wallet."""
    mint = "0x" + "44" * 20
    inner = _standard_erc20_http(mint, allowance_spender=PERMIT2)

    def http(method, url, headers=None, body=None, timeout=10):
        if method == "GET":
            return 200, {"price": "3000"}, None
        return inner(method, url, headers=headers, body=body, timeout=timeout)

    def v4_quote(sell_token, buy_token, amount_atomic, *, slippage_bps=None):
        selling_mint = sell_token.lower() == mint.lower()
        amount_out = (int(amount_atomic * 0.98 / 10 ** 12)
                      if selling_mint else int(amount_atomic) * 10 ** 12)
        return {
            "sellAmount": str(amount_atomic),
            "buyAmount": str(amount_out),
            "transaction": {"to": ROUTER, "data": "0xdeadbeef", "value": "0"},
            "issues": {"allowance": {"spender": PERMIT2}},
        }

    fn = build_evm_token_safety(
        chain_id="4663",
        rpc_url="https://rpc",
        taker=TAKER,
        http=http,
        quote_fn=v4_quote,
    )

    assert fn(mint) == (True, "ok")


def test_solady_seeded_storage_is_discoverable_for_funded_sell_simulation():
    """RIALTOES-style Solady clones must reach the sell simulation instead of false-refusal."""
    mint = "0x" + "c1" * 20

    def weth_quote(sell_token, buy_token, amount_atomic, *, slippage_bps=None):
        return {
            "sellAmount": str(amount_atomic),
            "buyAmount": str(int(amount_atomic) * 98 // 100),
            "transaction": {"to": ROUTER, "data": "0xdeadbeef", "value": "0"},
            "issues": {"allowance": {"spender": ROUTER}},
        }

    fn = build_evm_token_safety(
        chain_id="4663",
        rpc_url="https://rpc",
        taker=TAKER,
        http=_solady_erc20_http(mint),
        quote_fn=weth_quote,
    )

    assert fn(mint) == (True, "ok")


def test_solady_infinite_permit2_allowance_needs_no_storage_override():
    mint = "0x" + "c2" * 20

    def quote(_sell_token, _buy_token, amount_atomic, *, slippage_bps=None):
        return {
            "sellAmount": str(amount_atomic),
            "buyAmount": str(int(amount_atomic) * 98 // 100),
            "transaction": {"to": ROUTER, "data": "0xdeadbeef", "value": "0"},
            "issues": {"allowance": {"spender": PERMIT2}},
        }

    fn = build_evm_token_safety(
        chain_id="4663", rpc_url="https://rpc", taker=TAKER,
        http=_solady_erc20_http(mint, permit2_infinite=True), quote_fn=quote,
    )

    assert fn(mint) == (True, "ok")


def test_bound_safety_uses_injected_venue_quote_for_the_sell_check():
    """A direct-venue chain must not fall back to 0x for sellability."""
    mint = "0x" + "12" * 20
    inner = _standard_erc20_http(mint)
    calls = []

    def http(method, url, headers=None, body=None, timeout=10):
        assert method != "GET", "injected venue quote was bypassed for 0x"
        return inner(method, url, headers=headers, body=body, timeout=timeout)

    def venue_quote(sell_token, buy_token, amount_atomic, *, slippage_bps=None):
        calls.append((sell_token.lower(), buy_token.lower(), int(amount_atomic)))
        selling_mint = sell_token.lower() == mint.lower()
        amount_out = (int(amount_atomic * 0.98 / 10**12)
                      if selling_mint else int(amount_atomic) * 10**12)
        return {
            "sellAmount": str(amount_atomic),
            "buyAmount": str(amount_out),
            "transaction": {"to": ROUTER, "data": "0xdeadbeef", "value": "0"},
            "issues": {"allowance": {"spender": ROUTER}},
        }

    fn = build_evm_token_safety(
        chain_id="8453", rpc_url="https://rpc", taker=TAKER,
        http=http, quote_fn=venue_quote,
    )

    ok, reason = fn(mint)

    assert ok, reason
    assert len(calls) == 1


def test_safety_uses_the_probe_quantity_when_one_token_is_below_the_venue_minimum(monkeypatch):
    """Chain 1, a USD-quoted 6-decimal chain: the $5 probe converts to quote atomic
    without a price lookup. 8453 moved to 18-decimal WETH on 2026-09-06."""
    # Chain 1 quotes in WETH since 2026-09-30 (eth-mainnet spec); pin the USD-quoted 6-decimal shape
    # this test is about.
    from sniper.execution import evm_wallet as _W
    monkeypatch.setitem(_W.CHAIN_QUOTE, "1", {"token": _W.CHAIN_USDC["1"], "decimals": 6, "kind": "usd"})
    mint = "0x" + "13" * 20
    calls = []

    def quote(sell_token, buy_token, amount_atomic, *, slippage_bps=None):
        calls.append((sell_token.lower(), int(amount_atomic)))
        selling = sell_token.lower() == mint.lower()
        if selling and int(amount_atomic) == 10 ** 18:
            return None
        output = int(amount_atomic) * 10 ** 12 if not selling else int(amount_atomic) * 98 // 10 ** 14
        return {
            "sellAmount": str(amount_atomic), "buyAmount": str(output),
            "transaction": {"to": ROUTER, "data": "0xdeadbeef", "value": "0"},
            "issues": {"allowance": {"spender": ROUTER}},
        }

    safety = build_evm_token_safety(
        chain_id="1", rpc_url="https://rpc", taker=TAKER,
        http=_standard_erc20_http(mint), quote_fn=quote,
    )

    assert safety(mint) == (True, "ok")
    assert (mint.lower(), 5 * 10 ** 18) in calls


def test_unknown_funded_sell_revert_stays_refused_without_claiming_honeypot():
    """An unclassified funded revert is unsafe, but does not prove the token caused the revert."""
    quote_calls = []
    fn = build_evm_token_safety(chain_id="8453", rpc_url="https://rpc", taker=TAKER,
                                http=_standard_erc20_http("0xhoneypot", sell_blocked=True),
                                quote_fn=_standard_quote("0xhoneypot", quote_calls))
    ok, reason = fn("0xhoneypot")
    assert not ok
    assert reason.startswith("sell_sim_reverted_unknown")
    assert "detail=execution reverted" in reason
    assert "honeypot" not in reason.lower()
    assert sum(1 for sell, _buy, amount in quote_calls
               if sell == "0xhoneypot" and amount == 10 ** 18) == 2


def test_unknown_funded_sell_requotes_once_and_passes_on_fresh_simulation():
    mint = "0xretryunknown"
    inner = _standard_erc20_http(mint)
    sim_calls = {"count": 0}
    quote_calls = []

    def http(method, url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        first = (req.get("params") or [{}])[0]
        data = (first.get("data") or "") if isinstance(first, dict) else ""
        is_probe = data.startswith(("0x313ce567", "0x70a08231", "0xdd62ed3e"))
        if req["method"] == "eth_call" and not is_probe:
            sim_calls["count"] += 1
            if sim_calls["count"] == 1:
                return 200, {"error": {"code": 3, "message": "execution reverted"}}, None
        return inner(method, url, headers=headers, body=body, timeout=timeout)

    fn = build_evm_token_safety(
        chain_id="8453", rpc_url="https://rpc", taker=TAKER, http=http,
        quote_fn=_standard_quote(mint, quote_calls),
    )

    assert fn(mint) == (True, "ok")
    assert sim_calls["count"] == 2


def test_funded_sell_min_out_requotes_once_and_passes_on_fresh_simulation():
    mint = "0xmoved"
    inner = _standard_erc20_http(mint)
    sim_calls = {"count": 0}
    quote_calls = []

    def http(method, url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        first = (req.get("params") or [{}])[0]
        data = (first.get("data") or "") if isinstance(first, dict) else ""
        is_probe = data.startswith(("0x313ce567", "0x70a08231", "0xdd62ed3e"))
        if req["method"] == "eth_call" and not is_probe:
            sim_calls["count"] += 1
            if sim_calls["count"] == 1:
                return 200, {"error": {"code": 3, "message": "execution reverted",
                                       "data": _error_string_data("Too little received")}}, None
        return inner(method, url, headers=headers, body=body, timeout=timeout)

    fn = build_evm_token_safety(
        chain_id="8453", rpc_url="https://rpc", taker=TAKER, http=http,
        quote_fn=_standard_quote(mint, quote_calls),
    )

    assert fn(mint) == (True, "ok")
    nominal = 10 ** 18
    assert sum(1 for sell, _buy, amount in quote_calls
               if sell == mint and amount == nominal) == 2


def test_repeated_funded_sell_min_out_stays_blocked_without_honeypot_label():
    mint = "0xstillmoved"
    inner = _standard_erc20_http(mint)
    sim_calls = {"count": 0}

    def http(method, url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        first = (req.get("params") or [{}])[0]
        data = (first.get("data") or "") if isinstance(first, dict) else ""
        is_probe = data.startswith(("0x313ce567", "0x70a08231", "0xdd62ed3e"))
        if req["method"] == "eth_call" and not is_probe:
            sim_calls["count"] += 1
            return 200, {"error": {"code": 3, "message": "execution reverted",
                                   "data": _error_string_data("Too little received")}}, None
        return inner(method, url, headers=headers, body=body, timeout=timeout)

    fn = build_evm_token_safety(
        chain_id="8453", rpc_url="https://rpc", taker=TAKER, http=http,
        quote_fn=_standard_quote(mint),
    )

    ok, reason = fn(mint)
    assert not ok
    assert reason.startswith("sell_sim_min_out_repeated")
    assert "honeypot" not in reason.lower()
    assert sim_calls["count"] == 2


def test_state_override_is_well_formed_for_geth():
    """geth/reth encode an account `balance` override as hexutil.Big — a COMPACT hex quantity. A
    32-byte zero-padded value is rejected with -32602 'hex number with leading zero digits', which
    the caller then reads as a revert => 'honeypot'. Storage (stateDiff) values are the opposite:
    they MUST be full 32-byte words. Verified live on chain 4663 (2026-07-14)."""
    seen = {}

    def http(method, url, headers=None, body=None, timeout=10):
        if method != "GET":
            req = json.loads(body)
            if req["method"] == "eth_call" and len(req.get("params") or []) > 2:
                seen.update(req["params"][2])
        return _standard_erc20_http("0xtok")(method, url, headers=headers, body=body, timeout=timeout)

    fn = build_evm_token_safety(chain_id="8453", rpc_url="https://rpc", taker=TAKER,
                                http=http, quote_fn=_standard_quote("0xtok"))
    fn("0xtok")
    bal = (seen.get(TAKER) or {}).get("balance")
    assert bal, "no native balance override was sent"
    assert not bal[2:].startswith("0"), f"balance must be compact hex, got leading zeros: {bal}"
    for key, val in ((seen.get("0xtok") or {}).get("stateDiff") or {}).items():
        assert len(key) == 66 and len(val) == 66, f"stateDiff must be 32-byte words: {key}={val}"


def test_rpc_rate_limit_is_not_reported_as_honeypot():
    """A 429/transport error during the sell sim tells us NOTHING about the token. Reporting it as a
    honeypot is the same bug in a new coat — Base's public RPC 429s after ~4 calls (measured
    2026-07-14), so this is a live failure mode, not a hypothetical."""
    # Everything works — code, decimals, the slot probes — right up until the sell simulation, which
    # 429s. That is exactly how the rate limit bites in production: the slot scan burns the budget,
    # then the call that matters is throttled.
    inner = _standard_erc20_http("0xtok")

    def http(method, url, headers=None, body=None, timeout=10):
        if method != "GET":
            req = json.loads(body)
            first = (req.get("params") or [{}])[0]
            data = first.get("data") or "" if isinstance(first, dict) else ""
            is_probe = data.startswith(("0x313ce567", "0x70a08231", "0xdd62ed3e"))
            if req["method"] == "eth_call" and not is_probe:
                raise RuntimeError("HTTP Error 429: Too Many Requests")   # the sell sim itself
        return inner(method, url, headers=headers, body=body, timeout=timeout)

    fn = build_evm_token_safety(chain_id="8453", rpc_url="https://rpc", taker=TAKER,
                                http=http, quote_fn=_standard_quote("0xtok"))
    ok, reason = fn("0xtok")
    assert not ok                                     # still fail-closed — we cannot verify
    assert "honeypot" not in reason.lower(), f"RPC failure misreported as honeypot: {reason}"


def test_undiscoverable_storage_layout_fails_closed_with_distinct_reason():
    """Exotic (proxy/Vyper) layout -> we cannot fund the sim, so we cannot prove sellability.
    Stay fail-closed, but say WHY — never conflate 'unverifiable' with 'honeypot'."""
    fn = build_evm_token_safety(chain_id="8453", rpc_url="https://rpc", taker=TAKER,
                                http=_standard_erc20_http("0xproxy", balance_slot=99),
                                quote_fn=_standard_quote("0xproxy"))
    ok, reason = fn("0xproxy")
    assert not ok
    assert "honeypot" not in reason.lower(), f"unverifiable must not be reported as honeypot: {reason}"
    assert "unverifiable" in reason.lower()


def test_transient_rpc_during_slot_scan_is_not_poison_cached():
    """Regression (2026-07-15): the slot scan swallowed a transport error as 'slot not found' and
    then negative-cached it (_NO_SLOT) for the WHOLE process. A single RPC blip during the
    balanceOf slot probe permanently branded a normal, sellable token 'storage layout not
    discoverable' until the runner restarted — chain 4663 lost ~half its real entries this way, and
    the SAME mint both confirmed real fills and was refused. A transient failure must fail-closed for
    THAT attempt only, never poison the cache: the next event (RPC recovered) must be able to pass."""
    inner = _standard_erc20_http("0xtok")            # normal ERC-20, balances@0 allowance@1
    state = {"rpc_down": True}

    def http(method, url, headers=None, body=None, timeout=10):
        if method != "GET":
            req = json.loads(body)
            if req["method"] == "eth_call":
                first = (req.get("params") or [{}])[0]
                data = (first.get("data") or "") if isinstance(first, dict) else ""
                if data.startswith("0x70a08231") and state["rpc_down"]:   # balanceOf slot probe
                    raise RuntimeError("HTTP Error 503: Service Unavailable")
        return inner(method, url, headers=headers, body=body, timeout=timeout)

    fn = build_evm_token_safety(chain_id="8453", rpc_url="https://rpc", taker=TAKER,
                                http=http, quote_fn=_standard_quote("0xtok"))
    ok1, reason1 = fn("0xtok")                        # RPC degraded during the scan
    assert not ok1                                    # fail-closed for THIS attempt (correct)
    assert "honeypot" not in reason1.lower(), f"transient misreported as honeypot: {reason1}"

    state["rpc_down"] = False                         # RPC recovers before the next event
    ok2, reason2 = fn("0xtok")
    assert ok2, f"a transient RPC blip poison-cached a sellable token: {reason2}"


def test_genuinely_undiscoverable_layout_is_cached_not_rescanned():
    """The negative cache must survive for REAL undiscoverable layouts (HOODIO-style computed
    balanceOf) so we don't re-run the full 12+12 slot storm on every re-alert — only transient
    failures are barred from caching."""
    calls = {"balance_probes": 0}
    inner = _standard_erc20_http("0xproxy", balance_slot=99)   # override never lands -> clean not-found

    def http(method, url, headers=None, body=None, timeout=10):
        if method != "GET":
            req = json.loads(body)
            if req["method"] == "eth_call":
                first = (req.get("params") or [{}])[0]
                data = (first.get("data") or "") if isinstance(first, dict) else ""
                if data.startswith("0x70a08231"):
                    calls["balance_probes"] += 1
        return inner(method, url, headers=headers, body=body, timeout=timeout)

    fn = build_evm_token_safety(chain_id="8453", rpc_url="https://rpc", taker=TAKER,
                                http=http, quote_fn=_standard_quote("0xproxy"))
    fn("0xproxy")
    after_first = calls["balance_probes"]
    assert after_first > 0
    fn("0xproxy")
    assert calls["balance_probes"] == after_first, "clean undiscoverable layout was re-scanned (cache lost)"


def test_safety_nominal_sell_quote_is_shared_only_within_attempt():
    """Route + funded simulation share one quote, but a later event gets a fresh executable quote."""
    mint = "0xtok"
    nominal = 10 ** 18
    quote_calls = []

    def nominal_count():
        return sum(1 for sell, _buy, amount in quote_calls
                   if sell == mint and amount == nominal)

    fn = build_evm_token_safety(chain_id="8453", rpc_url="https://rpc", taker=TAKER,
                                http=_standard_erc20_http(mint),
                                quote_fn=_standard_quote(mint, quote_calls))
    assert fn(mint) == (True, "ok")
    assert nominal_count() == 1
    assert fn(mint) == (True, "ok")
    assert nominal_count() == 2


def test_safety_uses_the_recent_entry_probe_amount_for_the_sell_quote(monkeypatch):
    """Chain 1, a USD-quoted 6-decimal chain: the $5 probe converts to quote atomic
    without a price lookup. 8453 moved to 18-decimal WETH on 2026-09-06."""
    # Chain 1 quotes in WETH since 2026-09-30 (eth-mainnet spec); pin the USD-quoted 6-decimal shape
    # this test is about.
    from sniper.execution import evm_wallet as _W
    monkeypatch.setitem(_W.CHAIN_QUOTE, "1", {"token": _W.CHAIN_USDC["1"], "decimals": 6, "kind": "usd"})
    mint = "0xtok"
    quote_calls = []
    fn = build_evm_token_safety(
        chain_id="1", rpc_url="https://rpc", taker=TAKER,
        http=_standard_erc20_http(mint), quote_fn=_standard_quote(mint, quote_calls),
        probe_tokens_fn=lambda _mint: 7 * 10 ** 18,
    )

    assert fn(mint) == (True, "ok")
    assert quote_calls == [(mint, "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48".lower(),
                            7 * 10 ** 18)]


def test_safety_sell_quote_uses_the_execution_retry_slippage():
    mint = "0xtok"
    slips = []
    base_quote = _standard_quote(mint)

    def quote(*args, **kwargs):
        slips.append(kwargs.get("slippage_bps"))
        return base_quote(*args, **kwargs)

    fn = build_evm_token_safety(
        chain_id="8453", rpc_url="https://rpc", taker=TAKER,
        http=_standard_erc20_http(mint), quote_fn=quote,
        probe_tokens_fn=lambda _mint: 7 * 10 ** 18,
        sell_slippage_bps=800,
    )

    assert fn(mint) == (True, "ok")
    assert slips == [800]


def test_malformed_slot_read_is_not_negative_cached():
    """Malformed ABI results are inconclusive, so a later healthy attempt must rescan."""
    inner = _standard_erc20_http("0xtok")
    state = {"malformed": True}

    def http(method, url, headers=None, body=None, timeout=10):
        if method != "GET":
            req = json.loads(body)
            first = (req.get("params") or [{}])[0]
            data = (first.get("data") or "") if isinstance(first, dict) else ""
            if data.startswith("0x70a08231") and state["malformed"]:
                return 200, {"result": "not-hex"}, None
        return inner(method, url, headers=headers, body=body, timeout=timeout)

    fn = build_evm_token_safety(chain_id="8453", rpc_url="https://rpc", taker=TAKER,
                                http=http, quote_fn=_standard_quote("0xtok"))
    ok1, reason1 = fn("0xtok")
    assert not ok1 and "honeypot" not in reason1.lower()

    state["malformed"] = False
    assert fn("0xtok") == (True, "ok")


def test_rpc_error_payload_during_slot_scan_is_not_negative_cached():
    """A JSON-RPC error object is transport/infrastructure state, not a clean zero read."""
    inner = _standard_erc20_http("0xtok")
    state = {"rpc_error": True}

    def http(method, url, headers=None, body=None, timeout=10):
        if method != "GET":
            req = json.loads(body)
            first = (req.get("params") or [{}])[0]
            data = (first.get("data") or "") if isinstance(first, dict) else ""
            if data.startswith("0x70a08231") and state["rpc_error"]:
                return 200, {"error": {"code": -32005, "message": "rate limited"}}, None
        return inner(method, url, headers=headers, body=body, timeout=timeout)

    fn = build_evm_token_safety(chain_id="8453", rpc_url="https://rpc", taker=TAKER,
                                http=http, quote_fn=_standard_quote("0xtok"))
    ok1, reason1 = fn("0xtok")
    assert not ok1 and "honeypot" not in reason1.lower()

    state["rpc_error"] = False
    assert fn("0xtok") == (True, "ok")


# --- bound factory: a token with no sell route is rejected ---


def test_build_factory_requires_explicit_quote_fn():
    with pytest.raises(TypeError):
        build_evm_token_safety(chain_id="4663", rpc_url="https://rpc", taker=TAKER)


def test_build_factory_rejects_no_sell_route():
    def http(method, url, headers=None, body=None, timeout=10):
        meth = json.loads(body)["method"]
        if meth == "eth_getCode":
            return 200, {"result": "0x60006000"}, None         # is a contract
        if meth == "eth_call":                                 # decimals() -> 6
            return 200, {"result": "0x" + format(6, "x")}, None
        return 200, {"result": "0x"}, None
    fn = build_evm_token_safety(chain_id="8453", rpc_url="https://rpc", taker="0xtaker",
                                http=http, quote_fn=lambda *_a, **_k: None)
    ok, reason = fn("0xhoneypot")
    assert not ok
    assert "route=direct_configured" in reason
    assert "other venues not tested" in reason


# --- OZ-Upgradeable storage layout (slots 51/52) + machine-greppable refusal reasons -------------
#
# 2026-07-17: mint 0xe013… on 4663 uses OpenZeppelin-Upgradeable storage — Initializable(0) +
# ContextUpgradeable __gap[50] put _balances at slot 51 and _allowances at slot 52, past the old
# bounded scan (range(12)). Its funded sell simulation read as "unverifiable".


def test_oz_upgradeable_slots_51_52_are_discoverable():
    fn = build_evm_token_safety(
        chain_id="8453", rpc_url="https://rpc", taker=TAKER,
        http=_standard_erc20_http("0xoztok", balance_slot=51, allowance_slot=52),
        quote_fn=_standard_quote("0xoztok"))
    assert fn("0xoztok") == (True, "ok")


def test_funded_sell_simulation_covers_the_full_entry_probe_amount():
    mint = "0xlargeentry"
    required = 3 * _BIG
    fn = build_evm_token_safety(
        chain_id="8453", rpc_url="https://rpc", taker=TAKER,
        http=_standard_erc20_http(mint, required_balance=required),
        quote_fn=_standard_quote(mint),
        probe_tokens_fn=lambda _mint: required,
    )

    assert fn(mint) == (True, "ok")


# --- exit-at-clip-size (2026-09-16, Alpha's fix for the 1-token false-negative) --------------
# Regression this pins: a coin whose bought amount is KNOWN (a wallet-sized clip) but whose exit
# at that size has no route was passing safety anyway, because sell_quote() fell through to a
# 1-whole-token probe that could return a dust quote (amountOut > 0 but economically meaningless)
# and stop there. Reproduced live on a real Arc V4 pool 2026-09-16: 1-token probe -> OK (dust),
# $40 clip full exit -> REVERT. The fix: when the expected bought amount is known, quote a sell of
# THAT amount first; a miss there is the answer, not a cue to try something smaller.

def test_exit_unquotable_at_clip_refuses_without_trying_the_dust_probe():
    mint = "0xclipcoin"
    dust_calls = []

    def quote(sell_token, buy_token, amount_atomic, *, slippage_bps=None):
        if sell_token.lower() == mint.lower() and int(amount_atomic) != 9_000_000 * 10 ** 18:
            dust_calls.append(int(amount_atomic))    # any amount other than the real clip
            return {"sellAmount": str(amount_atomic), "buyAmount": "2",   # a real, tiny, honest
                    "transaction": {"to": ROUTER, "data": "0xdeadbeef", "value": "0"},   # dust quote
                    "issues": {"allowance": {"spender": ROUTER}}}
        return None    # the clip-sized sell has no route (the launch-cooldown case)

    fn = build_evm_token_safety(
        chain_id="5042", rpc_url="https://rpc", taker=TAKER,
        http=_standard_erc20_http(mint), quote_fn=quote,
        probe_tokens_fn=lambda _mint: 9_000_000 * 10 ** 18,
    )
    ok, reason = fn(mint)
    assert not ok
    assert reason.startswith("exit_unquotable_at_clip"), reason
    assert dust_calls == [], (
        "fell through to a smaller probe after the known clip size failed -- this is exactly the "
        "false negative: a dust quote at 1 token must never stand in for the real position")


def test_a_dust_output_at_the_known_clip_size_still_counts_as_sellable():
    """The fix is about SKIPPING SMALLER PROBES, not about rejecting a genuinely tiny amountOut --
    a real quote AT the clip size, however small the output, is still an honest answer."""
    mint = "0xdustclip"
    fn = build_evm_token_safety(
        chain_id="5042", rpc_url="https://rpc", taker=TAKER,
        http=_standard_erc20_http(mint), quote_fn=_standard_quote(mint),
        probe_tokens_fn=lambda _mint: 9_000_000 * 10 ** 18,
    )
    assert fn(mint) == (True, "ok")


def test_a_blind_miss_at_clip_size_is_not_exit_unquotable_and_not_a_honeypot():
    """An RPC transport/429 failure while quoting the KNOWN clip size proves nothing about the
    coin. It must read as blind, distinct from both 'no route' and 'honeypot'."""
    mint = "0xblindclip"

    def quote_with_miss(sell_token, buy_token, amount_atomic, *, slippage_bps=None):
        return None, "provider_rpc_transport"

    fn = build_evm_token_safety(
        chain_id="5042", rpc_url="https://rpc", taker=TAKER,
        http=_standard_erc20_http(mint), quote_fn=_standard_quote(mint),
        quote_with_miss_fn=quote_with_miss,
        probe_tokens_fn=lambda _mint: 9_000_000 * 10 ** 18,
    )
    ok, reason = fn(mint)
    assert not ok
    assert "blind" in reason.lower(), reason
    assert not reason.startswith("exit_unquotable_at_clip"), reason
    assert "honeypot" not in reason.lower(), reason


def test_a_clean_no_route_at_clip_size_is_exit_unquotable_at_clip():
    mint = "0xnoclip"

    def quote_with_miss(sell_token, buy_token, amount_atomic, *, slippage_bps=None):
        return None, "no_route"

    fn = build_evm_token_safety(
        chain_id="5042", rpc_url="https://rpc", taker=TAKER,
        http=_standard_erc20_http(mint), quote_fn=_standard_quote(mint),
        quote_with_miss_fn=quote_with_miss,
        probe_tokens_fn=lambda _mint: 9_000_000 * 10 ** 18,
    )
    ok, reason = fn(mint)
    assert not ok
    assert reason.startswith("exit_unquotable_at_clip"), reason


def test_route_capacity_at_clip_size_is_also_exit_unquotable_not_blind():
    """A rung that read cleanly and declined on capacity/economics is an ANSWER (see
    classify_quote_miss), not an infra failure -- must not be mislabeled blind."""
    mint = "0xcapclip"

    def quote_with_miss(sell_token, buy_token, amount_atomic, *, slippage_bps=None):
        return None, "route_capacity"

    fn = build_evm_token_safety(
        chain_id="5042", rpc_url="https://rpc", taker=TAKER,
        http=_standard_erc20_http(mint), quote_fn=_standard_quote(mint),
        quote_with_miss_fn=quote_with_miss,
        probe_tokens_fn=lambda _mint: 9_000_000 * 10 ** 18,
    )
    ok, reason = fn(mint)
    assert not ok
    assert reason.startswith("exit_unquotable_at_clip"), reason


def test_fallback_tiers_are_unchanged_when_the_bought_amount_is_unknown():
    """No probe_tokens_fn at all (the pre-fix, still-current situation on 56/8453/5042 until the
    hint is warmed) must behave exactly as before: 1-token, then $5-buy, still apply."""
    mint = "0xdegen"
    fn = build_evm_token_safety(chain_id="8453", rpc_url="https://rpc", taker=TAKER,
                                http=_standard_erc20_http(mint),
                                quote_fn=_standard_quote(mint))
    assert fn(mint) == (True, "ok")


def test_arc_size_requires_the_actual_quote_quantity_not_a_shared_hint():
    mint = "0xwarmme"
    calls = []
    fn = build_evm_token_safety(
        chain_id="5042", rpc_url="https://rpc", taker=TAKER,
        http=_standard_erc20_http(mint), quote_fn=_standard_quote(mint, calls),
        probe_tokens_fn=lambda _: 10 ** 18, entry_quote_probe_fn=lambda _mint, _usd: None,
    )
    ok, reason = fn(mint, usd=150)
    assert not ok and "blind" in reason
    assert calls == []


def test_no_warm_call_when_the_hint_is_already_populated():
    """4663 already warms this hint itself (route-capacity check) before the safety call. This
    must not fire a second, redundant buy quote when one is already fresh."""
    mint = "0xalreadywarm"
    warm_calls = []

    def entry_quote_probe_fn(_mint, usd):
        warm_calls.append((_mint, usd))
        return None

    fn = build_evm_token_safety(
        chain_id="4663", rpc_url="https://rpc", taker=TAKER,
        http=_standard_erc20_http(mint), quote_fn=_standard_quote(mint),
        probe_tokens_fn=lambda _mint: 7 * 10 ** 18, entry_quote_probe_fn=entry_quote_probe_fn,
    )
    assert fn(mint, usd=40.0) == (True, "ok")
    assert warm_calls == []


def test_usd_kwarg_is_optional_and_backward_compatible():
    """A caller that never learned about usd= (an older test, or a future non-EVM wrapper) must
    keep working exactly as before."""
    mint = "0xnousd"
    fn = build_evm_token_safety(chain_id="8453", rpc_url="https://rpc", taker=TAKER,
                                http=_standard_erc20_http(mint), quote_fn=_standard_quote(mint))
    assert fn(mint) == (True, "ok")


# --- MEMECOIN_EVM_CLIP_EXIT_CHECK_CHAINS (2026-09-16): the clip-size check is a per-chain
# allowlist, default {"5042"}. Alpha's ask: no hook-cap evidence was found on 4663, and the fix
# must not touch 4663/56's latency or behaviour on Alpha's own say-so -- a chain NOT listed keeps
# TODAY's shape byte-for-byte: a clip-known miss still widens to the 1-token/$5 probes, precisely
# as it did before this check existed at all.

def test_a_chain_outside_the_gate_still_falls_through_to_the_dust_probe(monkeypatch):
    """4663 is not in the default set. A clip-known miss there must NOT refuse outright -- it must
    behave exactly as it did before exit_unquotable_at_clip existed: fall through to smaller
    probes."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_CLIP_EXIT_CHECK_CHAINS", {"5042"}, raising=False)
    mint = "0xungated"
    dust_calls = []

    def quote(sell_token, buy_token, amount_atomic, *, slippage_bps=None):
        if sell_token.lower() == mint.lower() and int(amount_atomic) != 9_000_000 * 10 ** 18:
            dust_calls.append(int(amount_atomic))
            return {"sellAmount": str(amount_atomic), "buyAmount": "2",
                    "transaction": {"to": ROUTER, "data": "0xdeadbeef", "value": "0"},
                    "issues": {"allowance": {"spender": ROUTER}}}
        return None

    fn = build_evm_token_safety(
        chain_id="4663", rpc_url="https://rpc", taker=TAKER,
        http=_standard_erc20_http(mint), quote_fn=quote,
        probe_tokens_fn=lambda _mint: 9_000_000 * 10 ** 18,
    )
    ok, reason = fn(mint)
    assert ok, reason              # the dust probe answers OK, same as pre-fix
    assert dust_calls, "ungated chain must still try the smaller probe on a clip-size miss"


def test_56_is_also_outside_the_default_gate(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_CLIP_EXIT_CHECK_CHAINS", {"5042"}, raising=False)
    mint = "0xungated56"
    dust_calls = []

    def quote(sell_token, buy_token, amount_atomic, *, slippage_bps=None):
        if sell_token.lower() == mint.lower() and int(amount_atomic) != 9_000_000 * 10 ** 18:
            dust_calls.append(int(amount_atomic))
            return {"sellAmount": str(amount_atomic), "buyAmount": "2",
                    "transaction": {"to": ROUTER, "data": "0xdeadbeef", "value": "0"},
                    "issues": {"allowance": {"spender": ROUTER}}}
        return None

    fn = build_evm_token_safety(
        chain_id="56", rpc_url="https://rpc", taker=TAKER,
        http=_standard_erc20_http(mint), quote_fn=quote,
        probe_tokens_fn=lambda _mint: 9_000_000 * 10 ** 18,
    )
    ok, reason = fn(mint)
    assert ok, reason
    assert dust_calls


def test_a_chain_outside_the_gate_never_calls_the_warmup_seam(monkeypatch):
    """The entry_quote_probe_fn warm-up is part of the SAME gated behaviour -- an ungated chain
    must not take the extra buy quote either, matching its pre-fix RPC-call shape exactly."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_CLIP_EXIT_CHECK_CHAINS", {"5042"}, raising=False)
    mint = "0xnowarm"
    warm_calls = []

    def entry_quote_probe_fn(_mint, usd):
        warm_calls.append((_mint, usd))
        return None

    fn = build_evm_token_safety(
        chain_id="4663", rpc_url="https://rpc", taker=TAKER,
        http=_standard_erc20_http(mint), quote_fn=_standard_quote(mint),
        entry_quote_probe_fn=entry_quote_probe_fn,
    )
    assert fn(mint, usd=40.0) == (True, "ok")
    assert warm_calls == []


def test_a_chain_inside_the_gate_still_gets_the_new_behaviour(monkeypatch):
    """Regression: the allowlist must not accidentally gate OFF the chain it's meant for."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_CLIP_EXIT_CHECK_CHAINS", {"5042"}, raising=False)
    mint = "0xgated"
    dust_calls = []

    def quote(sell_token, buy_token, amount_atomic, *, slippage_bps=None):
        if sell_token.lower() == mint.lower() and int(amount_atomic) != 9_000_000 * 10 ** 18:
            dust_calls.append(int(amount_atomic))
            return {"sellAmount": str(amount_atomic), "buyAmount": "2",
                    "transaction": {"to": ROUTER, "data": "0xdeadbeef", "value": "0"},
                    "issues": {"allowance": {"spender": ROUTER}}}
        return None

    fn = build_evm_token_safety(
        chain_id="5042", rpc_url="https://rpc", taker=TAKER,
        http=_standard_erc20_http(mint), quote_fn=quote,
        probe_tokens_fn=lambda _mint: 9_000_000 * 10 ** 18,
    )
    ok, reason = fn(mint)
    assert not ok
    assert reason.startswith("exit_unquotable_at_clip"), reason
    assert dust_calls == []


def test_default_clip_gate_includes_ink():
    import os
    from sniper.settings import memecoin as cfg
    if os.getenv("MEMECOIN_EVM_CLIP_EXIT_CHECK_CHAINS"):
        return
    assert cfg.MEMECOIN_EVM_CLIP_EXIT_CHECK_CHAINS == {"5042", "57073"}


def test_honeypot_sim_reverted_reason_token():
    ok, reason = token_safety_ok_evm("0xtok", **_ok_seams(sell_sim_fn=lambda m: False))
    assert not ok
    assert reason.startswith("honeypot_sim_reverted")


def test_a_not_found_layout_expires_instead_of_branding_the_token_for_the_process(monkeypatch):
    """Regression (2026-08-12, mint 0xa3b6aee9…, smol_intern on 4663).

    A NOT-FOUND layout used to be permanent for the runner process. The coin was refused
    'storage layout not discoverable', and when the same coin came round again minutes later it was
    refused instantly WITHOUT re-probing. Verified live: `_find_slot` resolves BOTH the balance and
    the allowance slot for that mint against the real taker and both 4663 routers, so the token's
    layout is discoverable and the standing refusal was wrong.

    A found layout is immutable and stays cached. A not-found verdict is a claim about on-chain
    state at one instant — on 4663 the entries are fresh minimal-proxy clones — so it must expire.
    """
    from sniper.execution import evm_token_safety as ts

    clock = {"t": 1_000.0}
    monkeypatch.setattr(ts, "_now", lambda: clock["t"])

    state = {"slot": 99}                       # 99 = the override never lands -> clean not-found
    probes = {"n": 0}

    def http(method, url, headers=None, body=None, timeout=10):
        inner = _standard_erc20_http("0xclone", balance_slot=state["slot"])
        if method != "GET":
            req = json.loads(body)
            if req["method"] == "eth_call":
                first = (req.get("params") or [{}])[0]
                data = (first.get("data") or "") if isinstance(first, dict) else ""
                if data.startswith("0x70a08231"):
                    probes["n"] += 1
        return inner(method, url, headers=headers, body=body, timeout=timeout)

    # 8453 like the sibling tests: the TTL is chain-independent.
    fn = ts.build_evm_token_safety(chain_id="8453", rpc_url="https://rpc", taker=TAKER,
                                   http=http, quote_fn=_standard_quote("0xclone"))
    ok, reason = fn("0xclone")
    assert not ok and "not discoverable" in reason
    after_first = probes["n"]
    assert after_first > 0

    # Inside the TTL the verdict still holds, and costs no RPC — the existing caching guarantee.
    fn("0xclone")
    assert probes["n"] == after_first, "a not-found verdict re-scanned inside its TTL"

    # The clone initializes; its layout is now discoverable.
    state["slot"] = 0
    clock["t"] += ts._NO_SLOT_TTL_S + 1.0
    ok2, reason2 = fn("0xclone")
    assert probes["n"] > after_first, "the expired verdict was never re-probed"
    assert ok2, f"a token that became discoverable stayed refused: {reason2}"


def test_durable_slot_proof_survives_a_new_safety_factory():
    """A restart must retain a previously validated layout, then run a new sell simulation."""
    mint = "0x" + "d2" * 20
    proofs = {}
    first = build_evm_token_safety(
        chain_id="4663", rpc_url="https://rpc", taker=TAKER,
        http=_standard_erc20_http(mint), quote_fn=_standard_quote(mint),
        slot_proof_load=proofs.get, slot_proof_store=proofs.__setitem__,
    )
    assert first(mint) == (True, "ok")
    assert proofs

    inner = _standard_erc20_http(mint)

    def no_slot_scan(method, url, headers=None, body=None, timeout=10):
        if method != "GET":
            req = json.loads(body)
            call = (req.get("params") or [{}])[0]
            data = call.get("data") or "" if isinstance(call, dict) else ""
            assert not data.startswith(("0x70a08231", "0xdd62ed3e"))
        return inner(method, url, headers=headers, body=body, timeout=timeout)

    restarted = build_evm_token_safety(
        chain_id="4663", rpc_url="https://rpc", taker=TAKER,
        http=no_slot_scan, quote_fn=_standard_quote(mint),
        slot_proof_load=proofs.get, slot_proof_store=proofs.__setitem__,
    )
    assert restarted(mint) == (True, "ok")


def test_a_quote_without_a_spender_is_not_reported_as_an_undiscoverable_layout():
    """Two different causes used to return the same `None` and print the same reason, so the stored
    row could not say which fired — that is why the 2026-08-12 refusals were unattributable."""
    ok, reason = token_safety_ok_evm(
        "0xtok",
        **_ok_seams(sell_sim_fn=lambda m: "no_spender"))
    assert not ok
    assert reason.startswith("sell_sim_no_spender:")
    assert "storage layout" in reason        # still names what was skipped
    assert "not discoverable" not in reason  # but never claims the layout was probed and missed


# --- the funded balance must exceed the sell amount, never equal it -----------------------------

def test_simulation_word_always_leaves_headroom_above_the_sell_amount():
    """`max(_BIG, amount)` funded the balance to EXACTLY the amount once the amount passed _BIG.

    A fee-on-transfer or reflection token whose `transferFrom` needs more than it moves then reverts
    on its own accounting, and the caller reports `TRANSFER_FROM_FAILED` — i.e. unsellable. Measured
    on stored exit intents, 31 of 1153 sell amounts (2.7%) exceed _BIG, the largest by 8.7x, so the
    zero-headroom branch is reached in production.
    """
    from sniper.execution.evm_token_safety import _simulation_amount_word, _BIG
    for amount in (10 ** 18, _BIG, _BIG + 1, 5 * _BIG, 87 * _BIG // 10):
        word = _simulation_amount_word(amount)
        assert word is not None
        assert int(word, 16) > amount, f"no headroom at amount={amount}"


def test_simulation_word_saturates_instead_of_overflowing_uint256():
    from sniper.execution.evm_token_safety import _simulation_amount_word, _UINT256_MAX
    assert int(_simulation_amount_word(_UINT256_MAX), 16) == _UINT256_MAX
    assert int(_simulation_amount_word(_UINT256_MAX - 5), 16) == _UINT256_MAX


def test_simulation_word_rejects_non_amounts():
    from sniper.execution.evm_token_safety import _simulation_amount_word
    for bad in (0, -1, None, "x", _UINT256_MAX_PLUS := 2 ** 256):
        assert _simulation_amount_word(bad) is None


# --- failure-path attribution: did the TOKEN refuse, or the router path? ------------------------
# `TRANSFER_FROM_FAILED` out of a router simulation refuses the entry as unsellable, but it cannot
# tell the token rejecting the pull from a revert later in the router's own execution. Measured by
# hand 2026-09-02 on the three refused mints with stored slot proofs: overrides landed, plain
# `transfer` succeeded, and `transferFrom` by the named spender succeeded at 1e18/1e23/1e24 — the
# tokens were sellable and the refusals were FALSE. One extra eth_call, failure path only.

FACTORY = "0x00000000000000000000000000000000000fac70"
PAIR = "0x00000000000000000000000000000000000a1a12"
POOL = "0x00000000000000000000000000000000000b00b5"
CURVE = "0x00000000000000000000000000000000000c0c0c"


def _word(addr):
    return "0x" + "00" * 12 + addr[2:].lower()


BASE_WETH = "0x4200000000000000000000000000000000000006"
RT_PAID = 10 ** 15


def _round_trip_answer(params, mode, back_ratio, sims, buy_received=1000, gate=None):
    """A stateful eth_simulateV1 for the round trip: deposit, buy (0xb0b0), sell (0xfeed). The
    member probe (no calls) is answered but not recorded."""
    if not params[0]["blockStateCalls"][0]["calls"]:
        return ({"error": {"code": -32601, "message": "the method eth_simulateV1 does not exist"}}
                if mode == "unsupported" else {"result": [{"calls": []}]})
    sims.append(params)
    if mode == "unsupported":
        return {"error": {"code": -32601, "message": "the method eth_simulateV1 does not exist"}}
    mint_bal = quote_bal = 0
    out = []
    for c in params[0]["blockStateCalls"][0]["calls"]:
        d, to, status, ret = c.get("input") or "", c["to"].lower(), "0x1", "0x"
        if to == BASE_WETH and d.startswith("0xd0e30db0"):
            quote_bal += int(c["value"], 16)
        elif d.startswith("0x70a08231"):
            ret = "0x%064x" % (quote_bal if to == BASE_WETH else mint_bal)
        elif d == "0xb0b0":
            quote_bal, mint_bal = quote_bal - RT_PAID, mint_bal + 1000
        elif d == "0xfeed":
            if mode == "sell_reverts":
                status = "0x0"
            else:
                mint_bal, quote_bal = 0, quote_bal + int(RT_PAID * back_ratio)
        out.append({"status": status, "returnData": ret})
    return {"result": [{"calls": out}]}


def _router_reverts_http(*, token_pull_ok=True, pull_raises=False, counter=None,
                         router_error=None, blocked_to=None, reads=None, pair=PAIR, router_ok=False,
                         round_trip="unsupported", back_ratio=0.97, sims=None):
    """Router sell tx always reverts. The isolated transferFrom is controlled separately.
    `router_error` replaces the router's JSON-RPC error (default: a bare TRANSFER_FROM_FAILED).
    `blocked_to`: the token refuses a transfer INTO that address with TradingNotOpen() (a pair
    blocker whose selector is in no transfer-error list). `reads` collects destination lookups."""
    bal_key = _slot(TAKER, 0)
    allow_key = _slot(ROUTER, _slot(TAKER, 1))

    def http(method, url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        meth, params = req["method"], req.get("params") or []
        if meth == "eth_getCode":
            return 200, {"result": "0x60006000"}, None
        if meth == "eth_simulateV1":
            return 200, _round_trip_answer(params, round_trip, back_ratio,
                                           sims if sims is not None else []), None
        if meth != "eth_call":
            return 200, {"result": "0x"}, None
        call = params[0]
        data = call.get("data") or ""
        override = params[2] if len(params) > 2 else {}
        diff = {}
        for acct in override.values():
            diff.update(acct.get("stateDiff") or {})
        if data.startswith("0x313ce567"):
            return 200, {"result": "0x" + format(18, "x")}, None
        if data.startswith("0x70a08231"):
            return 200, {"result": diff.get(bal_key, "0x0")}, None
        if data.startswith("0xdd62ed3e"):
            return 200, {"result": diff.get(allow_key, "0x0")}, None
        if data.startswith("0xc45a0155"):                      # router.factory()
            if reads is not None:
                reads.append(("factory", call["to"]))
            return 200, {"result": _word(FACTORY)}, None
        if data.startswith("0xe6a43905") or data.startswith("0x1698ee82"):   # getPair / getPool
            if reads is not None:
                reads.append(("pair" if data.startswith("0xe6a43905") else "pool", call["to"]))
            return 200, {"result": _word(pair if data.startswith("0xe6a43905") else POOL)}, None
        if data.startswith("0x23b872dd"):                      # the isolated transferFrom probe
            if counter is not None:
                counter.append(call)
            if pull_raises:
                raise RuntimeError("rpc down")
            to = "0x" + data[10 + 64 + 24:10 + 128]
            if blocked_to is not None and to.lower() == blocked_to.lower():
                return 200, {"error": {"code": 3, "message": "execution reverted",
                                       "data": "0xe09f0331"}}, None
            if token_pull_ok:
                return 200, {"result": "0x"}, None
            return 200, {"error": {"code": -32000, "message": "TRANSFER_FROM_FAILED"}}, None
        if router_ok:
            return 200, {"result": "0x"}, None
        # the router's sell transaction — always reverts, unattributably
        return 200, {"error": router_error or {"code": -32000, "message": "TRANSFER_FROM_FAILED"}}, None
    return http


_V4_ROUTE = {"protocol": "uniswap_v4", "pool_id": "0x" + "ab" * 32}


def _quote(mint, route=_V4_ROUTE):
    return {"sellAmount": "1000", "buyAmount": "900", "transaction": {"to": ROUTER, "data": "0xfeed"},
            "issues": {"allowance": {"spender": ROUTER}}, "route": route}


def _safety_for(http, route=_V4_ROUTE):
    return build_evm_token_safety(
        chain_id="8453", rpc_url="https://rpc", taker=TAKER, http=http,
        quote_fn=lambda *a, **k: _quote("0xtok", route))


def test_router_revert_with_a_movable_token_still_fails_closed_when_disabled(monkeypatch):
    """The original contract, preserved. MEMECOIN_EVM_SAFETY_PULL_OK_ADMITS=0 restores it."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_SAFETY_PULL_OK_ADMITS", False, raising=False)
    ok, reason = _safety_for(_router_reverts_http(token_pull_ok=True))("0xtok")
    assert not ok
    assert "token_pull_ok=true" in reason, reason


@pytest.mark.parametrize("chain", ["56", 56])
def test_bsc_requires_successful_sell_transaction_not_just_token_transfer(monkeypatch, chain):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_SAFETY_PULL_OK_ADMITS", True)
    safety = build_evm_token_safety(
        chain_id=chain, rpc_url="https://rpc", taker=TAKER,
        http=_router_reverts_http(token_pull_ok=True),
        quote_fn=lambda *a, **k: _quote("0xtok", _V2_ROUTE))
    ok, reason = safety("0xtok")
    assert not ok
    assert reason.startswith("sell_sim_reverted_unknown:")
    assert "token_pull_ok=true" in reason
    assert "honeypot" not in reason


# STOCK (0x609bc137): an unnamed custom error from the pool's hook, the shape the exemption is for.
_STOCK_HOOK_ERROR = {"code": 3, "message": "execution reverted",
                     "data": "0x90bfb865" + "00" * 12 + "4e346895" + "11" * 16}


def test_router_revert_with_a_movable_token_now_admits(monkeypatch):
    """CONTRACT CHANGED 2026-09-04, deliberately (narrowed 2026-10-07 to custom errors only). This gate asks one question: can we move the coin
    back out. A successful `transferFrom` under the same overrides answers it YES, and the module's
    own `_attributed` already says so -- "the refusal is about our route, not the coin".

    STOCK (0x609bc137) was refused with `custom_error 0x90bfb865` whose payload decodes to the HOOK
    on one of its FIFTEEN pools, the only hooked one. The market showed $92,332 of liquidity with
    101 buys and 85 sells in five minutes. The hook rejects a synthetic state-override seller; it
    does not stop real ones, and we have closed 24 funded positions on tokens using that same hook.
    The custom-error allowlist path: MEMECOIN_EVM_SAFETY_ROUND_TRIP=0 (its rollback)."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_SAFETY_ROUND_TRIP", False)
    ok, reason = _safety_for(_router_reverts_http(token_pull_ok=True,
                                                  router_error=_STOCK_HOOK_ERROR))("0xtok")
    assert ok
    assert "token pull succeeded" in reason and "token_pull_ok=true" in reason, reason
    assert "custom_error 0x90bfb865" in reason, reason


# ---- 2026-10-07: SICAT 70895 (4663, written off -$50). The exemption admitted a honeypot. ----
# Its sell simulation reverted "TransferHelper: TRANSFER_FROM_FAILED" while the isolated pull,
# which moves taker -> ROUTER, succeeded. A V2 sell moves taker -> PAIR: a token that blocks
# transfers into its pair passes the probe and fails every real sale (30 failed sells). A string
# revert is the token's own words; only an unnamed custom error (a pool or hook) is the route's.

def _sicat_error(text):
    from eth_abi import encode
    data = "0x08c379a0" + encode(["string"], [text]).hex()             # Error(string)
    return {"code": 3, "message": "execution reverted: " + text, "data": data}


def test_sicat_a_string_revert_after_a_successful_pull_refuses():
    ok, reason = _safety_for(_router_reverts_http(
        token_pull_ok=True, router_error=_sicat_error("TransferHelper: TRANSFER_FROM_FAILED")))("0xtok")
    assert not ok, reason
    assert reason.startswith("sell_sim_reverted_unknown:"), reason
    assert "TRANSFER_FROM_FAILED" in reason and "token_pull_ok=true" in reason, reason


@pytest.mark.parametrize("text", [
    "TransferHelper: TRANSFER_FROM_FAILED", "TransferHelper: TRANSFER_FAILED", "TRANSFER_FAILED",
    "STF", "ST", "TF", "ERC20: transfer amount exceeds balance",
    "ERC20: transfer from the zero address", "UniswapV2: TRANSFER_FAILED",
])
def test_every_string_revert_refuses_even_when_the_pull_moved(text):
    for router_error in (_sicat_error(text), {"code": -32000, "message": text}):
        ok, reason = _safety_for(_router_reverts_http(
            token_pull_ok=True, router_error=router_error))("0xtok")
        assert not ok, (text, reason)


@pytest.mark.parametrize("signature", [
    "TransferFromFailed()", "TransferFailed()", "SafeERC20FailedOperation(address)",
    "ERC20InsufficientBalance(address,uint256,uint256)",
    "ERC20InsufficientAllowance(address,uint256,uint256)",
    "ERC20InvalidSender(address)", "ERC20InvalidReceiver(address)",
])
def test_a_token_transfer_custom_error_refuses_too(signature):
    from eth_utils import keccak
    data = "0x" + keccak(text=signature)[:4].hex() + "00" * 32
    ok, reason = _safety_for(_router_reverts_http(
        token_pull_ok=True, router_error={"code": 3, "message": "execution reverted",
                                          "data": data}))("0xtok")
    assert not ok, (signature, reason)


def test_router_revert_with_an_immovable_token_is_attributed_to_the_token():
    ok, reason = _safety_for(_router_reverts_http(token_pull_ok=False))("0xtok")
    assert not ok
    assert "token_pull_ok=false" in reason, reason


def test_the_probe_never_runs_on_the_success_path():
    """It must cost nothing when we are not already refusing."""
    calls = []
    http = _standard_erc20_http("0xtok")             # a normal sellable token: sim SUCCEEDS
    def counting(method, url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        first = (req.get("params") or [None])[0]
        if isinstance(first, dict) and str(first.get("data") or "").startswith("0x23b872dd"):
            calls.append(1)
        return http(method, url, headers=headers, body=body, timeout=timeout)
    ok, reason = _safety_for(counting)("0xtok")
    assert ok, reason
    assert calls == [], "the probe ran on the success path"


def test_a_probe_failure_leaves_the_verdict_exactly_where_it_was():
    ok, reason = _safety_for(_router_reverts_http(pull_raises=True))("0xtok")
    assert not ok
    assert "token_pull_ok" not in reason, reason      # unknown, never guessed


# --- a swallowed cause is a refusal that names the boundary, not the fault (2026-09-02) ---------
#
# BLAD 0xaa2d398d was refused as "sell simulation failed (fail-closed)" after evm.token_safety ran
# 7,502ms. Three bare `except Exception` blocks here discarded the exception entirely, and the
# module had no logger at all, so the only record was a string that says something failed.
#
# The reason strings are LOAD-BEARING: `_RETRY_TRANSIENT` in memecoin_real_runner matches
# ("unsafe", "sell simulation failed (fail-closed)") EXACTLY, so appending the cause would drop the
# outcome out of that set and silently stop the retry. The cause goes to the log; the string does
# not move.

import logging

import pytest

from sniper.execution import evm_token_safety as TS


_TRANSIENT_STRINGS = (
    "code read failed (fail-closed)",
    "sell simulation failed (fail-closed)",
)


def _check(**over):
    seams = dict(code_fn=lambda m: "0x60", sell_route_fn=lambda m: True,
                 sell_sim_fn=lambda m: True)
    seams.update(over)
    return TS.token_safety_ok_evm("0xmint", **seams)


def test_a_raising_sell_sim_still_returns_the_EXACT_retry_gated_string():
    """If this string moves, ("unsafe", "sell simulation failed (fail-closed)") stops matching
    _RETRY_TRANSIENT and the entry is no longer retried."""
    def boom(_m):
        raise TimeoutError("read timed out")
    ok, reason = _check(sell_sim_fn=boom)
    assert ok is False
    assert reason == "sell simulation failed (fail-closed)", "byte-identical or the retry breaks"


def test_the_cause_reaches_the_LOG_even_though_it_cannot_reach_the_reason(caplog):
    class Distinctive(RuntimeError):
        pass

    def boom(_m):
        raise Distinctive("the actual fault")
    with caplog.at_level(logging.ERROR, logger=TS.__name__):
        _check(sell_sim_fn=boom)
    blob = caplog.text
    assert "Distinctive" in blob, "the exception class must survive"
    assert "the actual fault" in blob
    assert "0xmint" in blob, "and it must name the mint"


def test_all_three_swallowing_sites_log(caplog):
    def boom(*_a, **_k):
        raise RuntimeError("nope")
    for kw in ("code_fn", "sell_route_fn", "sell_sim_fn"):
        caplog.clear()
        with caplog.at_level(logging.ERROR, logger=TS.__name__):
            ok, _ = _check(**{kw: boom})
        assert ok is False
        assert "RuntimeError" in caplog.text, f"{kw} discarded its cause"


def test_the_module_has_a_logger_at_all():
    """It had none. A funded gate with no logger cannot report why it refused."""
    assert isinstance(getattr(TS, "logger", None), logging.Logger)


@pytest.mark.parametrize("s", _TRANSIENT_STRINGS)
def test_the_retry_transient_set_still_contains_these_verbatim(s):
    """The guard that protects the guard: if a future edit appends a cause to either string, this
    fails rather than the retry silently stopping in production."""
    import pathlib
    from sniper.execution import evm_token_safety
    runner = (pathlib.Path(evm_token_safety.__file__).parent.parent
              / "services" / "memecoin_real_runner.py").read_text()
    assert f'"{s}"' in runner, f"{s!r} no longer appears in _RETRY_TRANSIENT"


# --- a deadline is not a token verdict (2026-09-02) ---------------------------------------------
#
# EvmSafetyDeadlineExceeded is a TimeoutError, so a generic `except Exception` swallowed it and
# reported "sell simulation failed (fail-closed)" -- an infrastructure fault wearing a token
# verdict. BLAD 0xaa2d398d refused that way after a 7,502ms safety run against a 3.0s per-read
# budget, and cleared on retry 21s later. Over 7 days on 4663, 24 of ~60 safety refusals are
# deadline/timeout. Both reasons are already in _RETRY_TRANSIENT, so retry behaviour is unchanged.


def test_a_deadline_is_reported_as_a_deadline_not_a_sell_failure():
    from sniper.execution import evm_token_safety as T

    def boom(mint):
        raise T.EvmSafetyDeadlineExceeded("deadline")

    ok, reason = T.token_safety_ok_evm(
        "0xaa2d398d66c36164c572783bff3da1450d988ed9",
        code_fn=lambda m: "0x6080",
        sell_route_fn=lambda m: True,
        sell_sim_fn=boom,
    )
    assert ok is False
    assert reason == "safety_deadline_exceeded", reason


def test_a_real_simulation_fault_is_still_a_sell_failure():
    """The guard must not swallow genuine faults into the deadline bucket."""
    from sniper.execution import evm_token_safety as T

    def boom(mint):
        raise ValueError("contract blew up")

    ok, reason = T.token_safety_ok_evm(
        "0xaa2d398d66c36164c572783bff3da1450d988ed9",
        code_fn=lambda m: "0x6080",
        sell_route_fn=lambda m: True,
        sell_sim_fn=boom,
    )
    assert ok is False and reason == "sell simulation failed (fail-closed)", reason


# --- namespaced storage: ask the node instead of guessing (OURO, 4663, 2026-09-04) --------------
from sniper.execution import evm_token_safety as S  # noqa: E402

def _echo_probe(real_key):
    """`read_back` for a token whose balance lives at exactly `real_key` and nowhere else."""
    def read_back(key):
        return S._BIG_HEX if key == real_key else "0x" + "0" * 64
    return read_back


def test_a_namespaced_layout_is_found_from_the_nodes_access_list():
    """OURO (0x8ea0eb35, chain 4663) is a 45-byte EIP-1167 proxy whose balance slot is
    0x8263e588…. No linear scan of 0..63 reaches it; `eth_createAccessList` returns it directly."""
    real = "0x8263e588474d490e92767ef5c7549780f0a479b9578841546f6daccc3dd5d77c"
    asked = []
    got = S._find_slot(
        _echo_probe(real),
        lambda i: "0x" + format(i, "064x"),
        late_slots=lambda: (asked.append(1), (real,))[1],
    )
    assert got == real
    assert asked == [1]        # consulted only after every known layout missed


def test_the_access_list_is_never_consulted_when_the_cheap_prefix_hits():
    # A standard ERC-20 must not pay an extra round trip.
    real = "0x" + format(0, "064x")
    asked = []
    got = S._find_slot(
        _echo_probe(real),
        lambda i: "0x" + format(i, "064x"),
        late_slots=lambda: (asked.append(1), (real,))[1],
    )
    assert got == real and asked == []


def test_an_access_list_key_that_does_not_land_is_still_rejected():
    # A key from the node is VERIFIED like any other candidate; it is not trusted on its word.
    got = S._find_slot(
        _echo_probe("0xdeadbeef"),
        lambda i: "0x" + format(i, "064x"),
        late_slots=lambda: ("0x" + "9" * 64,),
    )
    assert got is None


def test_an_endpoint_that_cannot_answer_changes_nothing():
    got = S._find_slot(
        _echo_probe("0xnever"),
        lambda i: "0x" + format(i, "064x"),
        late_slots=lambda: (),
    )
    assert got is None


def test_access_list_keys_come_only_from_the_token_and_never_the_implementation():
    """A minimal proxy's access list also names the implementation address. Its slots belong to a
    different account and would be verified against the wrong storage."""
    mint = "0x8ea0eb3505f5b3bd2bbea0febae0ce850cc73ecc"
    impl = "0xd6da7f07ee822c8538c901217b37d1e7d86c76e5"
    payload = {"result": {"accessList": [
        {"address": impl, "storageKeys": ["0x" + "1" * 64]},
        {"address": mint.upper(), "storageKeys": ["0x" + "2" * 64, "0x" + "2" * 64]},
    ]}}
    import sniper.execution.evm_wallet as W
    real_rpc = W._rpc
    W._rpc = lambda *_a, **_k: payload
    try:
        keys = S._access_list_slots(mint, "0xdata", "http://rpc")
    finally:
        W._rpc = real_rpc
    assert keys == ("0x" + "2" * 64,)      # token only, case-insensitive, de-duplicated


def test_an_access_list_failure_is_not_an_answer():
    import sniper.execution.evm_wallet as W
    real_rpc = W._rpc
    def boom(*_a, **_k):
        raise RuntimeError("EVM RPC provider HTTP failure")
    W._rpc = boom
    try:
        assert S._access_list_slots("0xmint", "0xdata", "http://rpc") == ()
    finally:
        W._rpc = real_rpc
    assert S._access_list_slots("0xmint", "0xdata", "") == ()   # no endpoint -> no answer


def test_arc_unknown_full_size_never_uses_a_dust_quote():
    mint = "0xmissingclip"
    calls = []
    fn = build_evm_token_safety(
        chain_id="5042", rpc_url="https://rpc", taker=TAKER,
        http=_standard_erc20_http(mint), quote_fn=_standard_quote(mint, calls),
        probe_tokens_fn=lambda _: None, entry_quote_probe_fn=lambda _mint, _usd: "provider_timeout",
    )
    ok, reason = fn(mint, usd=150)
    assert not ok
    assert "blind" in reason
    assert calls == []


def test_arc_explicit_quote_quantity_overrides_an_old_hint():
    mint = "0xexactclip"
    calls = []
    fn = build_evm_token_safety(
        chain_id="5042", rpc_url="https://rpc", taker=TAKER,
        http=_standard_erc20_http(mint), quote_fn=_standard_quote(mint, calls),
        probe_tokens_fn=lambda _: 10 ** 18,
    )
    assert fn(mint, usd=150, bought_amount_atomic=20 * 10 ** 18) == (True, "ok")
    assert [call[2] for call in calls] == [20 * 10 ** 18]


@pytest.mark.parametrize('chain', ['56', '4663'])
def test_nested_slot_scan_reads_keep_the_inline_deadline(chain):
    from sniper.services import memecoin_rh_fast_lane as lane
    mint = '0x' + '33' * 20
    # A layout no chain leads with (7/8), so both chains reach the concurrent tail this test pins;
    # 51/52 now leads on 56 (_CHAIN_SLOT_LEADS) and resolves before the tail.
    inner = _standard_erc20_http(mint, balance_slot=7, allowance_slot=8)
    reads = []

    def base(method, url, headers=None, body=None, timeout=10):
        if threading.current_thread().name.startswith('evm-slot-scan'):
            reads.append(timeout)
        return inner(method, url, headers=headers, body=body, timeout=timeout)

    url = 'https://inline.example'
    http = lane.lane_inline_http(url, lane.PacedHttp(min_gap_s=0.0), base=base)
    safety = build_evm_token_safety(
        chain_id=chain, rpc_url=url, taker=TAKER, http=http,
        quote_fn=_standard_quote(mint), safety_deadline_scope=lane.inline_deadline,
        safety_deadline_s=2.0)
    assert safety(mint) == (True, 'ok')
    assert reads and all(0 < timeout <= 2.0 for timeout in reads), reads


@pytest.mark.parametrize('fast_result', ['ok', 'revert', 'both_revert', 'raise', 'none'])
def test_fast_safety_route_still_simulates_and_failure_uses_normal_ladder(fast_result):
    mint = '0x' + '33' * 20
    ordinary = _standard_quote(mint)
    calls = []
    def fast(*args, **kwargs):
        calls.append('fast')
        if fast_result == 'raise':
            raise TimeoutError('provider')
        if fast_result == 'none':
            return None
        q = ordinary(*args, **kwargs)
        if fast_result in ('revert', 'both_revert'):
            q['transaction']['data'] = '0xbad'
        return q
    def quote(*args, **kwargs):
        calls.append('normal')
        q = ordinary(*args, **kwargs)
        if fast_result == 'both_revert':
            q['transaction']['data'] = '0xbad'
        return q
    base = _standard_erc20_http(mint, sell_blocked=fast_result == 'both_revert')
    simulated = []
    def http(method, url, headers=None, body=None, timeout=10):
        if method == 'GET':
            return 200, {'price': '3000'}, None
        req = json.loads(body)
        if req['method'] == 'eth_call' and req['params'][0].get('data') in ('0xbad', '0xdeadbeef'):
            simulated.append(req['params'][0]['data'])
            if simulated[-1] == '0xbad':
                return 200, {'error': {'code': -32000, 'message': 'execution reverted'}}, None
        return base(method, url, headers=headers, body=body, timeout=timeout)
    fn = build_evm_token_safety(chain_id='4663', rpc_url='https://rpc', taker=TAKER,
                                http=http, quote_fn=quote, fast_quote_fn=fast)
    ok, reason = fn(mint)
    assert ok == (fast_result in ('ok', 'revert'))
    if fast_result == 'ok':
        assert calls == ['fast'] and simulated == ['0xdeadbeef']
    elif fast_result == 'revert':
        assert calls == ['fast', 'normal'] and simulated == ['0xbad', '0xdeadbeef']
    elif fast_result == 'both_revert':
        assert simulated == ['0xbad', '0xbad']
    elif fast_result == 'raise':
        assert calls == ['fast'] and not simulated
    else:
        assert not simulated


# ---- 2026-10-07 (Alpha, operator): the pull probe moves the token where the SALE moves it. ----
# d0052ec7f closed string reverts; a pair blocker reverting with its own custom error (here
# TradingNotOpen(), 0xe09f0331) still passed a probe that moved taker -> ROUTER.

_V2_ROUTE = {"protocol": "uniswap_v2", "router": ROUTER, "tokens": ["0xtok", "0xweth"],
             "path": ["0xtok", "0xweth"]}
_V3_ROUTE = {"protocol": "uniswap_v3", "tokens": ["0xtok", "0xweth"], "fees": [3000]}
_PM_8453 = "0x498581ff718922c3f8e6a244956af099b2652b2b"


def _probe_to(calls):
    return ["0x" + c["data"][10 + 64 + 24:10 + 128] for c in calls]


def test_a_custom_error_pair_blocker_fails_the_probe_into_its_pair():
    hook = {"code": 3, "message": "execution reverted", "data": "0xe09f0331"}
    calls = []
    ok, reason = _safety_for(_router_reverts_http(
        router_error=hook, blocked_to=PAIR, counter=calls), _V2_ROUTE)("0xtok")
    assert not ok, reason
    assert "token_pull_ok=false" in reason, reason
    assert _probe_to(calls) == [PAIR]


@pytest.mark.parametrize("route, dest, lookups", [
    (_V2_ROUTE, PAIR, [("factory", ROUTER), ("pair", FACTORY)]),
    (_V3_ROUTE, POOL, [("factory", ROUTER), ("pool", FACTORY)]),
    (_V4_ROUTE, _PM_8453, []),
    ({"protocol": "uniswap_v4_multihop", "legs": 2}, _PM_8453, []),
    ({"protocol": "pons_v2", "curve": CURVE}, CURVE, []),
    ({"protocol": "launchpad_curve", "tokens": ["0xtok", "0xweth"], "curve": CURVE}, CURVE, []),
])
def test_the_probe_moves_the_token_to_the_route_s_own_sale_destination(route, dest, lookups,
                                                                     monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_SAFETY_ROUND_TRIP", False)   # the probe alone
    calls, reads = [], []
    hook = {"code": 3, "message": "execution reverted", "data": "0x90bfb865"}
    ok, _reason = _safety_for(_router_reverts_http(
        router_error=hook, counter=calls, reads=reads), route)("0xtok")
    assert ok                                         # a movable token, a route-side custom error
    assert _probe_to(calls) == [dest.lower()]
    assert reads == lookups


@pytest.mark.parametrize("route, pair", [
    ({"protocol": "ramses_v3", "tokens": ["0xtok", "0xweth"], "tick_spacings": [60]}, PAIR),
    ({"protocol": "something_new"}, PAIR),
    (None, PAIR),
    (_V2_ROUTE, "0x" + "00" * 20),                     # getPair answers no pair
])
def test_an_unresolved_sale_destination_never_admits(route, pair):
    calls = []
    hook = {"code": 3, "message": "execution reverted", "data": "0x90bfb865"}
    ok, reason = _safety_for(_router_reverts_http(
        router_error=hook, counter=calls, pair=pair), route)("0xtok")
    assert not ok, reason
    assert calls == []                               # nothing to probe: no move is made at all


@pytest.mark.parametrize("route", [_V2_ROUTE, _V3_ROUTE])
def test_the_success_path_makes_no_destination_lookup_and_no_probe(route):
    calls, reads = [], []
    ok, reason = _safety_for(_router_reverts_http(router_ok=True, counter=calls, reads=reads),
                             route)("0xtok")
    assert ok and reason == "ok", reason
    assert reads == [] and calls == []


@pytest.fixture(autouse=True)
def _fresh_simulate_members():
    """Which members answer eth_simulateV1 is frozen per process; each test starts unprobed."""
    from sniper.execution import evm_token_safety as ets
    ets._SIMULATE_MEMBERS.clear()
    ets._SIMULATE_RETRYING.clear()
    yield
    ets._SIMULATE_MEMBERS.clear()
    ets._SIMULATE_RETRYING.clear()


# ---- 2026-10-07 (spec EVM entry safety, section 1): a REAL BUYER'S ROUND TRIP decides. ----
# A V4 hook that refuses every seller reverts with the same WrappedError (0x90bfb865) as STOCK's
# hook refusing only a state-override seller. One eth_simulateV1 -- buy through the entry quote,
# approve, sell what was bought through the exit quote -- tells them apart.

_HOOK = {"code": 3, "message": "execution reverted", "data": "0x90bfb865"}


def _rt_safety(http, monkeypatch, usd_fn=None):
    from sniper.execution import evm_real_seams
    monkeypatch.setattr(evm_real_seams, "usd_to_quote_atomic", usd_fn or (lambda *a, **k: RT_PAID))

    def quote_fn(sell_token, buy_token, amount, **kw):
        if str(sell_token).lower() == BASE_WETH:                 # the round trip's buy
            return {"sellAmount": str(amount), "buyAmount": "1000",
                    "transaction": {"to": ROUTER, "data": "0xb0b0", "value": "0x0"},
                    "issues": {"allowance": {"spender": ROUTER}}, "route": _V4_ROUTE}
        return _quote("0xtok")                                   # every sell: 0xfeed
    return build_evm_token_safety(chain_id="8453", rpc_url="https://rpc", taker=TAKER, http=http,
                                  quote_fn=quote_fn)


def test_a_stock_shaped_hook_admits_when_a_real_buyer_s_round_trip_completes(monkeypatch):
    sims = []
    ok, reason = _rt_safety(_router_reverts_http(router_error=_HOOK, round_trip="ok",
                                                 sims=sims), monkeypatch)("0xtok")
    assert ok, reason
    assert "real buyer's round trip completed (returned 97.0% of paid)" in reason, reason
    assert len(sims) == 2                       # buy alone, then buy + sell


def test_a_hook_that_refuses_every_seller_refuses(monkeypatch):
    ok, reason = _rt_safety(_router_reverts_http(router_error=_HOOK, round_trip="sell_reverts"),
                            monkeypatch)("0xtok")
    assert not ok and reason.startswith("sell_sim_reverted_unknown:"), reason
    assert "round_trip=sell_reverted_for_a_real_buyer" in reason, reason


def test_a_round_trip_that_loses_more_than_the_tax_cap_refuses(monkeypatch):
    ok, reason = _rt_safety(_router_reverts_http(router_error=_HOOK, round_trip="ok",
                                                 back_ratio=0.5), monkeypatch)("0xtok")
    assert not ok and "round_trip=returned 50.0% of paid" in reason, reason


def test_an_unrunnable_round_trip_is_unknown_and_refuses(monkeypatch):
    ok, reason = _rt_safety(_router_reverts_http(router_error=_HOOK, round_trip="unsupported"),
                            monkeypatch)("0xtok")
    assert not ok and "round_trip=rpc_unknown" in reason, reason


def test_a_string_revert_admits_only_through_a_completed_round_trip(monkeypatch):
    text = _sicat_error("TransferHelper: TRANSFER_FROM_FAILED")
    ok, reason = _rt_safety(_router_reverts_http(router_error=text, round_trip="ok"),
                            monkeypatch)("0xtok")
    assert ok, reason
    ok, reason = _rt_safety(_router_reverts_http(router_error=text, round_trip="sell_reverts"),
                            monkeypatch)("0xtok")
    assert not ok, reason


def test_a_pair_blocker_never_reaches_the_round_trip(monkeypatch):
    sims = []
    ok, reason = _rt_safety(_router_reverts_http(router_error=_HOOK, round_trip="ok", sims=sims,
                                                 blocked_to=_PM_8453), monkeypatch)("0xtok")
    assert not ok and "token_pull_ok=false" in reason and sims == [], reason


def test_the_success_path_never_simulates_a_round_trip(monkeypatch):
    sims = []
    ok, reason = _rt_safety(_router_reverts_http(router_ok=True, round_trip="ok", sims=sims),
                            monkeypatch)("0xtok")
    assert ok and reason == "ok" and sims == []


def test_round_trip_off_restores_the_custom_error_allowlist(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_SAFETY_ROUND_TRIP", False)
    sims = []
    ok, reason = _rt_safety(_router_reverts_http(router_error=_HOOK, round_trip="sell_reverts",
                                                 sims=sims), monkeypatch)("0xtok")
    assert ok and "route-attributed" in reason and sims == [], reason


# ---- 2026-10-07 (Alpha): the simulation goes only to members that answer eth_simulateV1. ----

def test_the_round_trip_is_pinned_to_members_that_simulate_probed_once(monkeypatch, caplog):
    import logging
    from sniper.execution import evm_token_safety as ets
    refusing, supporting = "https://mainnet.base.example/", "https://good.example/k/"
    monkeypatch.setattr(ets, "_pool_members", lambda url: [refusing, supporting])
    inner = _router_reverts_http(router_error=_HOOK, round_trip="ok")
    asked = []

    def http(method, url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        if req["method"] == "eth_simulateV1":
            asked.append(url)
            if url == refusing:
                return 200, {"error": {"code": -32601, "message": "method not supported"}}, None
        return inner(method, url, headers=headers, body=body, timeout=timeout)
    with caplog.at_level(logging.WARNING):
        safety = _rt_safety(http, monkeypatch)
        for _ in range(2):
            ok, reason = safety("0xtok")
            assert ok, reason
    assert asked.count(refusing) == 1                # probed once, never used
    assert asked.count(supporting) == 1 + 2 * 2      # its probe, then two round trips of two calls
    assert any("evm.simulate_member_refused host=mainnet.base.example" in r.getMessage()
               for r in caplog.records)


def test_a_pool_with_no_simulating_member_is_unknown_and_refuses(monkeypatch):
    ok, reason = _rt_safety(_router_reverts_http(router_error=_HOOK, round_trip="unsupported"),
                            monkeypatch)("0xtok")
    assert not ok and "round_trip=rpc_unknown" in reason, reason


def test_the_round_trip_buys_the_entry_clip_not_the_probe(monkeypatch):
    seen = []
    safety = _rt_safety(_router_reverts_http(router_error=_HOOK, round_trip="ok"), monkeypatch,
                        usd_fn=lambda chain, usd, http=None: seen.append(usd) or RT_PAID)
    ok, _ = safety("0xtok", usd=150.0)
    assert ok and 150.0 in seen and 5.0 not in seen


def test_a_200_answer_with_a_method_error_body_is_a_definite_refusal(monkeypatch, caplog):
    """Gamma's shape: a refusing member that answers HTTP 200 with an error body (no code)."""
    import logging
    from sniper.execution import evm_token_safety as ets
    a, b = "https://refuses.example/", "https://works.example/"
    monkeypatch.setattr(ets, "_pool_members", lambda url: [a, b])

    def http(method, url, headers=None, body=None, timeout=10):
        if url == a:
            return 200, {"jsonrpc": "2.0", "id": 1,
                         "error": {"message": "eth_simulateV1 is not supported on this endpoint"}}, None
        return 200, {"jsonrpc": "2.0", "id": 1, "result": [{"calls": []}]}, None
    with caplog.at_level(logging.WARNING):
        assert ets.warm_simulate_members("https://rpc", http=http, sleep=lambda s: None) == (b,)
    assert ets._SIMULATE_MEMBERS["https://rpc"] == {a: "refused", b: "supported"}
    assert any("host=refuses.example" in r.getMessage() for r in caplog.records)


def test_a_transport_failure_is_re_probed_with_backoff_never_frozen(monkeypatch):
    from sniper.execution import evm_token_safety as ets
    a = "https://flaky.example/"
    monkeypatch.setattr(ets, "_pool_members", lambda url: [a])
    answers = iter([RuntimeError("connection reset"), (429, {}), (200, {"result": [{"calls": []}]})])
    slept = []

    def http(method, url, headers=None, body=None, timeout=10):
        got = next(answers)
        if isinstance(got, Exception):
            raise got
        return got[0], got[1], None
    assert ets.warm_simulate_members("https://rpc", http=http, sleep=slept.append) == (a,)
    assert slept == [2, 4]                      # two unknown probes, then the definite answer


def test_the_simulation_goes_straight_to_the_pinned_member_never_rotated(monkeypatch):
    """Never through evm_wallet._rpc, whose pool rotation re-chooses among all members (Gamma)."""
    from sniper.execution import evm_token_safety as ets, evm_wallet
    real_rpc = evm_wallet._rpc

    def no_rotated_simulation(url, method, params, http=None):
        assert method != "eth_simulateV1", "a simulation went through the pool's rotation"
        return real_rpc(url, method, params, http=http)
    monkeypatch.setattr(evm_wallet, "_rpc", no_rotated_simulation)
    first, second = "https://first.example/", "https://second.example/"
    monkeypatch.setattr(ets, "_pool_members", lambda url: [first, second])
    inner = _router_reverts_http(router_error=_HOOK, round_trip="ok")
    sims = []

    def http(method, url, headers=None, body=None, timeout=10):
        if json.loads(body)["method"] == "eth_simulateV1" and json.loads(body)["params"][0]["blockStateCalls"][0]["calls"]:
            sims.append(url)
        return inner(method, url, headers=headers, body=body, timeout=timeout)
    safety = _rt_safety(http, monkeypatch)
    for _ in range(3):
        assert safety("0xtok")[0]
    assert sims == [first] * 6


@pytest.mark.parametrize("error, verdict", [
    ({"code": -32601, "message": "the method eth_simulateV1 does not exist"}, "refused"),
    ({"code": -32000, "message": "eth_simulateV1 is not supported"}, "refused"),
    ({"message": "method not supported on this endpoint"}, "refused"),
    ({"code": -32000, "message": "service temporarily not available, try again"}, None),
    ({"code": -32005, "message": "request limit reached; resource not available"}, None),
    ({"code": -32000, "message": "header not available"}, None),
    ({"code": -32000, "message": "internal error"}, None),
])
def test_only_a_definite_method_refusal_freezes_a_member(error, verdict):
    """Gamma MEDIUM: a transient answer at a deploy-time probe must never freeze a member out."""
    from sniper.execution import evm_token_safety as ets
    got, _why = ets._probe_member("https://m.example/", lambda method, url, headers=None, body=None,
                                  timeout=10: (200, {"jsonrpc": "2.0", "id": 1, "error": error}, None))
    assert got == verdict


def test_an_unknown_member_keeps_being_re_probed_past_the_backoff(monkeypatch):
    """Gamma LOW: past the 2..128 s backoff, unknown members are re-probed every 128 s."""
    from sniper.execution import evm_token_safety as ets
    a = "https://late.example/"
    monkeypatch.setattr(ets, "_pool_members", lambda url: [a])
    answers = iter([RuntimeError("down")] * 9 + [(200, {"result": [{"calls": []}]})])
    slept = []

    def http(method, url, headers=None, body=None, timeout=10):
        got = next(answers)
        if isinstance(got, Exception):
            raise got
        return got[0], got[1], None
    assert ets.warm_simulate_members("https://rpc", http=http, sleep=slept.append, max_reprobes=5) == (a,)
    assert slept == [2, 4, 8, 16, 32, 64, 128, 128, 128]


# ---- B20 (Base's native token standard; operator order 2026-10-08 "Fix", Jez 0xb2000…da01) ----

B20_BASE = 0xC78B71FEE795DDD74AFF64EA9B2474194C938C3196430E10BB5F01ED48434000   # read back on chain
JEZ = "0xb2000000000000000000002927f6d5eeefbbda01"


def _b20_http(mint_addr, *, spender=ROUTER, sell_error=None, probes=None):
    """A B20 precompile: code 0xef, balances at keccak(owner . base+4), allowances at
    keccak(spender . keccak(owner . base+5)); `sell_error` makes the funded sell revert anyway."""
    bal_key = S._map_slot(TAKER, B20_BASE + 4)
    allow_key = S._nested_slot(spender, TAKER, B20_BASE + 5)

    def http(method, url, headers=None, body=None, timeout=10):
        if method == "GET":
            return 200, {"price": "3000"}, None
        req = json.loads(body)
        meth, params = req["method"], req.get("params") or []
        if meth == "eth_getCode":
            return 200, {"result": "0xef"}, None
        if meth == "eth_createAccessList":
            return 200, {"result": {"accessList": [], "gasUsed": "0x5c5c"}}, None     # as on chain
        if meth != "eth_call":
            return 200, {"result": "0x"}, None
        data = params[0].get("data") or ""
        diff = ((params[2] if len(params) > 2 else {}).get(mint_addr) or {}).get("stateDiff") or {}
        if data.startswith("0x313ce567"):
            return 200, {"result": "0x" + format(18, "x")}, None
        if data.startswith("0x70a08231"):
            if probes is not None and diff:
                probes.append(next(iter(diff)))
            return 200, {"result": diff.get(bal_key, "0x0")}, None
        if data.startswith("0xdd62ed3e"):
            return 200, {"result": diff.get(allow_key, "0x0")}, None
        funded = int(diff.get(bal_key, "0x0"), 16) > 0 and int(diff.get(allow_key, "0x0"), 16) > 0
        if not funded or sell_error is not None:
            return 200, {"error": {"code": 3, "message": "execution reverted",
                                   "data": sell_error or "0x"}}, None
        return 200, {"result": "0x"}, None
    return http


def _router_quote(_sell, _buy, amount_atomic, *, slippage_bps=None):
    return {"sellAmount": str(amount_atomic), "buyAmount": str(int(amount_atomic) * 98 // 100),
            "transaction": {"to": ROUTER, "data": "0xdeadbeef", "value": "0"},
            "issues": {"allowance": {"spender": ROUTER}}}


def test_the_b20_namespace_is_erc7201_base_b20():
    from eth_utils import keccak
    derived = int.from_bytes(keccak((int.from_bytes(keccak(text="base.b20"), "big") - 1)
                                    .to_bytes(32, "big")), "big") & ~0xff
    assert S._B20_STORAGE_BASE == derived == B20_BASE
    assert S._is_b20_address(JEZ) and not S._is_b20_address("0xb2" + "00" * 8 + "01" + "11" * 10)
    assert not S._is_b20_address("0x" + "44" * 20) and not S._is_b20_address(None)


def test_a_b20_sell_is_funded_through_its_namespaced_slots_on_the_first_probe():
    """Jez refused 'storage layout not discoverable' after a 537 ms scan: the precompile's reads are not
    in eth_createAccessList and the namespace base is a hash no index scan reaches."""
    probes = []
    fn = build_evm_token_safety(chain_id="8453", rpc_url="https://rpc", taker=TAKER,
                                http=_b20_http(JEZ, probes=probes), quote_fn=_router_quote)
    assert fn(JEZ) == (True, "ok")
    assert probes[0] == S._map_slot(TAKER, B20_BASE + 4)          # led, not reached after a scan


def test_a_b20_sell_that_reverts_still_fails_closed():
    paused = "0x" + keccak_text("ContractPaused(uint8)")[:8] + format(0, "064x")
    fn = build_evm_token_safety(chain_id="8453", rpc_url="https://rpc", taker=TAKER,
                                http=_b20_http(JEZ, sell_error=paused), quote_fn=_router_quote)
    ok, reason = fn(JEZ)
    assert ok is False and "unverifiable" not in reason


def test_a_mint_without_the_b20_prefix_probes_in_todays_order():
    probes = []
    mint = "0x" + "44" * 20
    fn = build_evm_token_safety(chain_id="8453", rpc_url="https://rpc", taker=TAKER,
                                http=_b20_http(mint, probes=probes), quote_fn=_router_quote)
    fn(mint)
    assert probes[0] == S._map_slot(TAKER, 0) and S._map_slot(TAKER, B20_BASE + 4) not in probes


def test_a_lead_slot_that_does_not_read_back_falls_through_to_the_normal_scan():
    real = "0x" + format(0, "064x")
    seen = []

    def read_back(key):
        seen.append(key)
        return S._BIG_HEX if key == real else "0x" + "0" * 64
    got = S._find_slot(read_back, lambda i: "0x" + format(i, "064x"), lead_slots=("0x" + "ab" * 32,))
    assert got == real and seen[:2] == ["0x" + "ab" * 32, real]


def keccak_text(text):
    from eth_utils import keccak
    return keccak(text=text).hex()


def test_a_round_trip_verdict_reports_the_clip_amount_it_sold(monkeypatch):
    """Gamma on 9b3223d0c (EVM06): when the sell simulation reverts and the real buyer's round trip
    decides the pass, probe_out carries the amount that round trip sold (the clip-size `got`), the
    amount the old shared `sold` held, not the dust probe's."""
    out = {}
    safety = _rt_safety(_router_reverts_http(router_error=_HOOK, round_trip="ok"), monkeypatch)
    ok, reason = safety("0xtok", probe_out=out)
    assert ok and "round trip completed" in reason, reason
    assert out["amount"] == 1000, out
