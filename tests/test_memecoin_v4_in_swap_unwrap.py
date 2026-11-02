"""The 4663 V4 hooked-pool buy unwraps WETH INSIDE the Universal Router call (2026-09-28, Alpha GO).

Spec: docs/superpowers/specs/2026-09-28-memecoin-4663-v4-in-swap-unwrap-design.md. Today a
WETH-bankroll buy sends WETH.withdraw(amount) first and waits for its receipt (p50 1203 ms / p90
1825 ms on 18 V4 fills since 09-21), then UR [V4_SWAP] with value=amount. Behind
MEMECOIN_EVM_V4_IN_SWAP_UNWRAP_CHAINS (default empty = today), a hooked single-hop buy becomes ONE
transaction, value 0: UR [PERMIT2_PERMIT(WETH), PERMIT2_TRANSFER_FROM(WETH -> router),
UNWRAP_WETH(router) on a native pool, V4_SWAP(unchanged)], and the executor's existing exact
WETH->Permit2 approval (pipelined on 4663) replaces the unwrap. Proven by eth_call on live pools
(scripts/research/v4_in_swap_unwrap_proof.py). Any failure to build the permit falls back to
today's shape and says why on route.in_swap_unwrap_fallback.
"""
import threading

import pytest
from eth_abi import decode

import sniper.execution.evm_v4_quote as Q
from sniper.execution.evm_swap_executor import execute_entry_evm
from sniper.persistence import db_memecoin as m
from sniper.settings import memecoin as cfg
from test_memecoin_evm_entry_executor import _TraceRecorder, _c, _seams

WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
COIN = "0xf7d2a651188ea9f52cc802b3d962eb6c88bfaba3"
NATIVE = "0x" + "00" * 20
TAKER = "0x898be9c27f8b391c47a3d9f2da17781aa8aa3a1d"
HOOKED_NATIVE = {"currency0": NATIVE, "currency1": COIN, "fee": 0, "tick_spacing": 200,
                 "hooks": "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"}
HOOKED_WETH = {"currency0": WETH, "currency1": COIN, "fee": 0x800000, "tick_spacing": 200,
               "hooks": "0x4e3468951d49f2eea976ed0d6e75ffcb44a9a544"}
PLAIN_NATIVE = {"currency0": NATIVE, "currency1": COIN, "fee": 2500, "tick_spacing": 25, "hooks": NATIVE}
AMT, OUT = 61157840231258176, 24095309325775143415517060
PERMIT = "ab" * 64
UR = Q.V4["4663"]["universal_router"]


class _Acct:
    address = TAKER


@pytest.fixture
def on(monkeypatch):
    """The knob names 4663 and the 4663 approval pipeline is on; the permit read and signer are stubbed."""
    monkeypatch.setenv("MEMECOIN_EVM_V4_IN_SWAP_UNWRAP_CHAINS", "4663")
    monkeypatch.setattr(cfg, "memecoin_evm_approval_pipeline", lambda chain=None: str(chain) == "4663")
    nonces = []
    monkeypatch.setattr(Q, "_permit2_nonce", lambda *a, **k: nonces.append(a) or 3)
    signed = []

    def permit(chain, acct, token, spender, amount, rpc_url, http=None, now_fn=None, nonce=None):
        signed.append((token, spender, amount, nonce))
        return PERMIT
    monkeypatch.setattr(Q, "_permit2_input", permit)
    return {"nonces": nonces, "signed": signed}


def _quote(monkeypatch, key, *, acct=_Acct()):
    monkeypatch.setattr(Q, "v4_quote_amount_out", lambda *a, **k: {
        "amount_out": OUT, "pool_id": "0x" + "ff" * 32, "pool_key": key, "zero_for_one": True,
        "quote_currency": key["currency0"]})
    return Q.dex_v4_quote("4663", WETH, COIN, AMT, TAKER, rpc_url="https://rpc", acct=acct,
                          now_fn=lambda: 1_800_000_000)


def _ur(q):
    return decode(["bytes", "bytes[]", "uint256"], bytes.fromhex(q["transaction"]["data"][10:]))


@pytest.mark.parametrize("key", [HOOKED_NATIVE, HOOKED_WETH], ids=["native", "weth"])
def test_the_knob_off_keeps_todays_bytes(monkeypatch, key):
    monkeypatch.delenv("MEMECOIN_EVM_V4_IN_SWAP_UNWRAP_CHAINS", raising=False)
    q = _quote(monkeypatch, key)
    assert len(q["pre_transactions"]) == 1 and q["transaction"]["value"] == hex(AMT)
    assert "in_swap_unwrap" not in q["route"] and "in_swap_unwrap_fallback" not in q["route"]


def test_a_hooked_native_buy_unwraps_inside_the_router_call(monkeypatch, on):
    monkeypatch.delenv("MEMECOIN_EVM_V4_IN_SWAP_UNWRAP_CHAINS")
    legacy_swap = _ur(_quote(monkeypatch, HOOKED_NATIVE))[1][-1]
    monkeypatch.setenv("MEMECOIN_EVM_V4_IN_SWAP_UNWRAP_CHAINS", "4663")
    q = _quote(monkeypatch, HOOKED_NATIVE)
    commands, inputs, _ = _ur(q)
    assert commands == bytes([Q._CMD_PERMIT2_PERMIT, 0x02, 0x0C, Q._CMD_V4_SWAP])
    assert inputs[0].hex() == PERMIT
    assert decode(["address", "address", "uint160"], inputs[1]) == (WETH, "0x" + "00" * 19 + "02", AMT)
    assert decode(["address", "uint256"], inputs[2]) == ("0x" + "00" * 19 + "02", AMT)
    assert inputs[3] == legacy_swap, "the V4_SWAP input is byte-identical to today's"
    params = decode(["bytes", "bytes[]"], inputs[3])[1]
    assert decode(["address", "uint256", "bool"], params[1]) == (NATIVE, 0, False), "router pays"
    assert q["transaction"]["value"] == "0x0" and q["pre_transactions"] == []
    assert q["transaction"]["gas"] == Q._HOOKED_POOL_GAS
    assert q["issues"]["allowance"] == {"spender": Q.V4["4663"]["permit2"]}
    assert q["route"]["in_swap_unwrap"] is True
    assert on["signed"] == [(WETH, UR, AMT, 3)], "the permit is WETH -> the router, exact amount"


def test_a_hooked_weth_buy_pays_the_pool_weth_with_no_unwrap_and_no_wrap(monkeypatch, on):
    q = _quote(monkeypatch, HOOKED_WETH)
    commands, inputs, _ = _ur(q)
    assert commands == bytes([Q._CMD_PERMIT2_PERMIT, 0x02, Q._CMD_V4_SWAP])
    params = decode(["bytes", "bytes[]"], inputs[2])[1]
    assert decode(["address", "uint256", "bool"], params[1]) == (WETH, 0, False)
    assert q["transaction"]["value"] == "0x0" and q["pre_transactions"] == []
    assert q["route"]["in_swap_unwrap"] is True


def test_a_plain_native_pool_is_out_of_v1_scope(monkeypatch, on):
    q = _quote(monkeypatch, PLAIN_NATIVE)
    assert len(q["pre_transactions"]) == 1 and q["transaction"]["value"] == hex(AMT)
    assert "in_swap_unwrap" not in q["route"]


@pytest.mark.parametrize("case, why", [("no_signer", "no_signer"), ("nonce", "permit_unavailable"),
                                       ("pipeline", "approval_not_pipelined")])
def test_a_failed_permit_falls_back_to_todays_shape_and_names_why(monkeypatch, on, case, why):
    acct = _Acct()
    if case == "no_signer":
        acct = None
    elif case == "nonce":
        monkeypatch.setattr(Q, "_permit2_nonce", lambda *a, **k: None)
    else:
        monkeypatch.setattr(cfg, "memecoin_evm_approval_pipeline", lambda chain=None: False)
    q = _quote(monkeypatch, HOOKED_NATIVE, acct=acct)
    assert len(q["pre_transactions"]) == 1 and q["transaction"]["value"] == hex(AMT)
    assert q["route"]["in_swap_unwrap_fallback"] == why and "in_swap_unwrap" not in q["route"]


def test_the_permit_nonce_is_read_beside_the_quote_not_after_it(monkeypatch, on):
    started = threading.Event()
    monkeypatch.setattr(Q, "_permit2_nonce", lambda *a, **k: started.set() or 3)

    def slow_quote(*a, **k):
        assert started.wait(2.0), "the nonce read must already be running while the quoter runs"
        return {"amount_out": OUT, "pool_id": "0x" + "ff" * 32, "pool_key": HOOKED_NATIVE,
                "zero_for_one": True, "quote_currency": NATIVE}
    monkeypatch.setattr(Q, "v4_quote_amount_out", slow_quote)
    q = Q.dex_v4_quote("4663", WETH, COIN, AMT, TAKER, rpc_url="https://rpc", acct=_Acct(),
                       now_fn=lambda: 1_800_000_000)
    assert q["route"]["in_swap_unwrap"] is True


# --- the executor ---------------------------------------------------------------------------------

def _usdc_with_raw(raw):
    values = iter((100.0, 90.0))

    def fn():
        return next(values, 90.0)
    fn.last_raw = lambda: raw
    return fn


def test_an_in_swap_buy_above_the_weth_balance_refuses_before_the_approval():
    c = _c()
    pid = m.reserve_pending(c, mint="0xtok", source_user_id="trader1", strategy="alpha_b",
                            notional=10.0, reserved_at="2026-07-09T00:00:00Z", network_id="4663")
    quote = {"sellAmount": str(AMT), "transaction": {"to": UR, "data": "0xdead", "value": "0x0"},
             "issues": {"allowance": {"spender": Q.V4["4663"]["permit2"]}},
             "pre_transactions": [], "route": {"in_swap_unwrap": True}}
    seams, calls = _seams(quote=quote)
    seams["usdc_fn"] = _usdc_with_raw(AMT - 1)
    trace = _TraceRecorder()
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0, strategy="alpha_b",
        source_user_id="trader1", now_iso="2026-07-09T00:00:01Z", sell_token_usd=2655.73,
        trace=trace, **seams)
    assert result["status"] == "aborted" and result["reason"] == "unwrap exceeds quote balance"
    assert calls["approve"] == [] and calls["sign"] == 0 and calls["send"] == 0
    facts = [meta for name, meta in trace.substages if name == "evm.unwrap_facts"]
    assert facts and facts[-1]["outcome"] == "refused" and facts[-1]["in_swap"] is True


def test_an_in_swap_buy_within_the_weth_balance_approves_permit2_and_says_so():
    c = _c()
    pid = m.reserve_pending(c, mint="0xtok", source_user_id="trader1", strategy="alpha_b",
                            notional=10.0, reserved_at="2026-07-09T00:00:00Z", network_id="4663")
    quote = {"sellAmount": str(AMT), "transaction": {"to": UR, "data": "0xdead", "value": "0x0"},
             "issues": {"allowance": {"spender": Q.V4["4663"]["permit2"]}},
             "pre_transactions": [], "route": {"in_swap_unwrap": True}}
    seams, calls = _seams(quote=quote)
    seams["usdc_fn"] = _usdc_with_raw(AMT)
    trace = _TraceRecorder()
    execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0, strategy="alpha_b",
        source_user_id="trader1", now_iso="2026-07-09T00:00:01Z", sell_token_usd=2655.73,
        trace=trace, **seams)
    assert calls["approve"] == [(Q.V4["4663"]["permit2"], AMT)]
    marks = [meta for name, meta in trace.substages if name == "evm.v4_in_swap_unwrap"]
    assert marks and marks[-1]["outcome"] == "applied"


def test_the_preflight_simulates_the_in_swap_buy_before_its_approval_lands(monkeypatch, on):
    """The exact-route simulation runs before the approval is mined, so it must fabricate the WETH ->
    Permit2 allowance the pipelined approval will set; without it every in-swap buy would refuse."""
    import json
    from sniper.execution import evm_real_seams as S
    from sniper.execution.evm_token_safety import _nested_slot
    q = _quote(monkeypatch, HOOKED_NATIVE)
    seen = {}

    def http(_method, _url, *, body=None, **_kwargs):
        seen["overrides"] = json.loads(body)["params"][2]
        return 200, {"result": "0x"}, None
    assert S.preflight_weth_entry_quote(q, quote_token=WETH, taker=TAKER, rpc_url="https://rpc",
                                        http=http) == (True, "ok")
    diff = seen["overrides"][WETH]["stateDiff"]
    assert _nested_slot(Q.V4["4663"]["permit2"], TAKER, 3) in diff


def _tapped_quote(monkeypatch, key):
    from sniper.execution.evm_wallet import start_http_tap, take_http_tap
    start_http_tap()
    try:
        q = _quote(monkeypatch, key)
    finally:
        tapped = take_http_tap() or []
    return q, [h for h in tapped if h["label"] == "v4.permit2_nonce_wait"]


def test_the_rungs_wait_on_the_permit_nonce_is_timed_in_its_http_tap(monkeypatch, on):
    # Gamma on f45b72cb8 (3): the nonce read runs on its own pool thread, so the rung's RPC tap
    # never saw it. The rung's wait for it is a timed stage in the rung's own `http` list.
    q, [wait] = _tapped_quote(monkeypatch, HOOKED_NATIVE)
    assert q["route"]["in_swap_unwrap"] is True
    assert wait["outcome"] == "ok" and wait["rtt_ms"] >= 0


def test_a_failed_permit_nonce_wait_is_timed_and_still_falls_back(monkeypatch, on):
    def boom(*a, **k):
        raise RuntimeError("rpc down")
    monkeypatch.setattr(Q, "_permit2_nonce", boom)
    q, [wait] = _tapped_quote(monkeypatch, HOOKED_NATIVE)
    assert wait["outcome"] == "raise:RuntimeError"
    assert q["route"].get("in_swap_unwrap_fallback") and "in_swap_unwrap" not in q["route"]


def test_no_wait_is_recorded_when_the_build_reads_no_nonce(monkeypatch):
    monkeypatch.delenv("MEMECOIN_EVM_V4_IN_SWAP_UNWRAP_CHAINS", raising=False)
    _q, waits = _tapped_quote(monkeypatch, HOOKED_NATIVE)
    assert waits == []
