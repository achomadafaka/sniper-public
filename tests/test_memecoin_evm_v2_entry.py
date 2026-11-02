"""V2 fallback for ENTRY quotes + the pre-entry sell-route check on blind chains (2026-07-16).

The theveeman 0xf056… case (20:09 operator report): a 56s fast-lane 4663 catch passed the mcap
gate (assume-micro) and was then refused ENTRY-ROUTE-REFUSAL — the pre-entry "can we sell this?"
check quotes ONLY direct_v3_single_hop, while fresh 4663 micros are born on V2-style pools (the
VIM precedent, which is why the EXIT path already has a V2 fallback). Entries and the safety
sell-check now use the same V3->V2 chain the real exit uses: same verified Router02 + immutable
connectors already trusted with real sells. Buys zero the fee-on-transfer input buffer (the input
is the quote token, which does not tax); sells keep it."""
import pytest
import threading

from eth_abi import decode, encode
from eth_account import Account

from sniper.execution.evm_real_seams import venue_quote_with_v2_fallback

WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
MINT = "0xf0568863195770965a6d8abb0aa87f4314b80320"


@pytest.fixture(autouse=True)
def _route_cost_is_not_what_this_file_tests():
    """Every hit measures cheap, so these fixtures pin what they are actually about.

    The entry ladder measures a round trip by quoting the reverse of the bag a hit would buy
    (2026-09-06). A fixture with no `sellAmount`, or one answering the same amount in both
    directions, cannot be measured and the hit is correctly REFUSED as `round_trip_unmeasured`.
    Nothing in this file is about the cost of a route, so without this each test would be asserting
    on a refusal it was never written to describe. The tests that ARE about measurement live in
    tests/test_memecoin_evm_measured_ladder.py.
    """
    import sniper.execution.evm_real_seams as _S
    original = _S._round_trip_for_hit
    _S._round_trip_for_hit = lambda *_a, **_k: (-3.0, None)
    try:
        yield
    finally:
        _S._round_trip_for_hit = original


def _combined(primary_result, v2_spy, chain="4663", enabled=True, v2_started=None):
    def primary(sell, buy, amount, *, slippage_bps=None):
        # The rungs race in a thread pool and the winner CANCELS the losers, so "did v2 run?" is a
        # scheduler question unless the test makes it one. Measured 2026-09-02: with an instant
        # primary, v2 was cancelled before it started in 88 of 300 runs under load, and that is
        # what refused a deploy. Waiting on v2's own start signal removes the race from the test
        # WITHOUT weakening it: v2 has now certainly quoted when v3 answers, so a priority
        # assertion is about priority.
        if v2_started is not None:
            v2_started.wait(timeout=5)
        return primary_result

    def v2(chain_, sell, buy, amount, taker, *, http=None, slippage_bps=None,
           tax_buffer_bps=None):
        v2_spy.append({"sell": sell, "buy": buy, "tax": tax_buffer_bps})
        if v2_started is not None:
            v2_started.set()
        return {"buyAmount": "123", "transaction": {"to": "0xrouter"},
                "issues": {"allowance": {"spender": "0xrouter"}}}

    return venue_quote_with_v2_fallback(
        primary, chain=chain, quote_token=WETH, taker="0xtaker",
        http=None, v2_quote_fn=v2, enabled=enabled)


def test_v3_priority_wins_when_v2_also_quotes():
    calls = []
    started = threading.Event()
    fn = _combined({"buyAmount": "9"}, calls, v2_started=started)
    assert fn(WETH, MINT, 10**18)["buyAmount"] == "9"
    assert started.is_set()                      # v2 really did quote; this is not a race artifact
    assert calls == [{"sell": WETH, "buy": MINT, "tax": 0}]


def test_buy_falls_back_to_v2_with_zero_input_tax_buffer():
    calls = []
    fn = _combined(None, calls)
    res = fn(WETH, MINT, 10**18)                    # BUY: sell side is the quote token
    assert res["buyAmount"] == "123"
    assert calls == [{"sell": WETH, "buy": MINT, "tax": 0}]


def test_sell_falls_back_to_v2_with_default_tax_buffer():
    calls = []
    fn = _combined(None, calls)
    res = fn(MINT, WETH, 10**18)                    # SELL: memecoin in, quote token out
    assert res is not None
    assert calls == [{"sell": MINT, "buy": WETH, "tax": None}]


def test_no_v2_fallback_off_blind_chains_or_when_disabled():
    # chain 137, not 8453, 56 or 1: those joined the rung ladder (2026-09-06, 1 on 2026-09-30) and
    # none demonstrates a chain whose v2/v4 rungs are off any more.
    for kw in ({"chain": "137"}, {"enabled": False}):
        calls = []
        fn = _combined(None, calls, **kw)
        assert fn(WETH, MINT, 10**18) is None
        assert calls == []


# ---------- V4 third rung (2026-07-17, SPACEHOOD: V4-native-pool coins) ------------------------


def test_v4_is_tried_only_after_v2_returns_none(monkeypatch):
    from sniper.execution.evm_real_seams import venue_quote_with_v2_fallback
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", True)
    calls = []
    fn = venue_quote_with_v2_fallback(
        lambda s, b, a, slippage_bps=None: None, chain="4663", quote_token=WETH,
        taker="0xtaker", v2_quote_fn=lambda *a, **k: None,
        v4_quote_fn=lambda *a, **k: calls.append(1) or {"buyAmount": "42"},
        acct=object(), rpc_url="https://rpc")
    assert fn(WETH, MINT, 10**18)["buyAmount"] == "42"
    assert calls == [1]
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", False)
    assert fn(WETH, MINT, 10**18) is None
    assert calls == [1]


def test_pons_v2_priority_wins_without_waiting_for_lower_route(monkeypatch):
    from sniper.settings import memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", True)
    calls = []
    quote = {"buyAmount": "42"}
    fn = venue_quote_with_v2_fallback(
        lambda *_a, **_k: None, chain="4663", quote_token=WETH, taker="0xtaker",
        v2_quote_fn=lambda *_a, **_k: calls.append("uniswap_v2"),
        pons_v2_quote_fn=lambda *_a, **_k: calls.append("pons_v2") or quote,
        v4_quote_fn=lambda *_a, **_k: calls.append("v4"), rpc_url="https://rpc",
    )

    assert fn(WETH, MINT, 10**18) is quote
    assert {"uniswap_v2", "pons_v2"} <= set(calls)


def test_dex_v4_quote_buy_shape_with_injected_seams(monkeypatch):
    """BUY: native-paired pool -> UR tx with value=amountIn, one WETH-unwrap pre-tx, NO allowance."""
    from sniper.execution import evm_v4_quote as V

    pool_id = "0x" + "ab" * 32
    monkeypatch.setattr(V, "_pool_key", lambda *a, **k: {
        "currency0": "0x" + "00" * 20, "currency1": MINT, "fee": 70000,
        "tick_spacing": 1400, "hooks": "0x" + "00" * 20})
    monkeypatch.setattr(V, "_quote_pools_for_mint", lambda *a, **k: [pool_id])
    monkeypatch.setattr(V, "_quote_single", lambda *a, **k: 1_000_000)
    q = V.dex_v4_quote("4663", WETH, MINT, 10**14, "0xtaker", rpc_url="https://rpc",
                       slippage_bps=1500, now_fn=lambda: 1_000_000)
    assert q is not None
    assert q["issues"]["allowance"] is None
    assert q["transaction"]["value"] == hex(10**14)
    assert int(q["buyAmount"]) == 1_000_000
    assert q["route"]["pool_id"] == pool_id
    assert len(q["pre_transactions"]) == 1
    assert q["pre_transactions"][0]["data"].startswith("0x2e1a7d4d")   # WETH.withdraw
    assert q["transaction"]["data"].startswith("0x3593564c")           # UR.execute
    # min-out (85% of quote) must be embedded in the calldata
    assert format(850_000, "064x") in q["transaction"]["data"]


def test_v4_finds_a_poolmanager_event_when_indexers_lag(monkeypatch):
    """A fresh 4663 V4 pool must not wait for GeckoTerminal or DexScreener indexing."""
    from sniper.execution import evm_v4_quote as V

    pool_id = "0x" + "ab" * 32
    mint_topic = "0x" + MINT.removeprefix("0x").rjust(64, "0")
    seen = []

    def rpc(_url, method, params, http=None):
        if method == "eth_blockNumber":
            return {"result": "0x3000"}
        assert method == "eth_getLogs"
        topics = params[0]["topics"]
        seen.append((topics, params[0]["fromBlock"], params[0]["toBlock"]))
        if topics == [V._INIT_TOPIC, None, None, mint_topic]:
            return {"result": [{"topics": [V._INIT_TOPIC, pool_id]}]}
        return {"result": []}

    monkeypatch.setattr(V, "_rpc", rpc)
    # The durable PoolKey cache reads the REAL ledger, and MINT is a real 4663 coin with stored
    # pools, so without this the test passed only in a checkout that has no ledger (2026-09-12).
    monkeypatch.setattr(V, "_stored_pools_for_mint", lambda *_a, **_k: [])
    V._pool_cache.clear()
    try:
        monkeypatch.setattr(V, "_ds_pools_ranked", lambda *_a, **_k: ([], False))
        pools = V._quote_pools_for_mint(
            "4663", MINT, gt_http=lambda _url: {"data": []}, rpc_url="https://rpc",
        )
    finally:
        V._pool_cache.clear()

    assert pools == [pool_id]
    assert seen == [
        ([V._INIT_TOPIC, None, mint_topic], "0x1000", "0x3000"),
        ([V._INIT_TOPIC, None, None, mint_topic], "0x1000", "0x3000"),
    ]


def test_v4_reads_a_recent_pool_key_before_an_archive_lookup(monkeypatch):
    """A new V4 pool must not require an archive provider for its Initialize event."""
    from sniper.execution import evm_v4_quote as V

    pool_id = "0x" + "ab" * 32
    key = _v4_key()
    data = (format(key["fee"], "064x")
            + format(key["tick_spacing"] % (1 << 256), "064x")
            + key["hooks"][2:].rjust(64, "0"))
    seen = []

    def rpc(_url, method, params, http=None):
        if method == "eth_blockNumber":
            return {"result": "0x3000"}
        assert method == "eth_getLogs"
        query = params[0]
        seen.append(query["fromBlock"])
        if query["fromBlock"] == "0x1000":
            return {"result": [{
                "topics": [V._INIT_TOPIC, pool_id,
                           "0x" + "00" * 32,
                           "0x" + MINT[2:].rjust(64, "0")],
                "data": "0x" + data,
            }]}
        raise RuntimeError("archive unavailable")

    monkeypatch.setattr(V, "_rpc", rpc)
    monkeypatch.setattr(V, "_stored_pool_key", lambda *_a: None)
    stored = []
    monkeypatch.setattr(V, "_store_pool_key", lambda *args: stored.append(args))
    V._key_cache.clear()
    try:
        assert V._pool_key("4663", pool_id, "https://rpc") == key
    finally:
        V._key_cache.clear()

    assert seen == ["0x1000"]
    assert stored


def _v4_key():
    return {
        "currency0": "0x" + "00" * 20,
        "currency1": MINT,
        "fee": 70_000,
        "tick_spacing": 1_400,
        "hooks": "0x" + "00" * 20,
    }


def test_v4_exact_input_single_action_param_matches_canonical_abi():
    from sniper.execution import evm_v4_quote as V

    key = _v4_key()
    encoded = bytes.fromhex(V._v4_swap_input(
        key, True, 123, 100, take_all_to_sender=True,
    ))
    _actions, params = decode(["bytes", "bytes[]"], encoded)
    canonical = encode(
        ["((address,address,uint24,int24,address),bool,uint128,uint128,bytes)"],
        [((key["currency0"], key["currency1"], key["fee"], key["tick_spacing"],
           key["hooks"]), True, 123, 100, b"")],
    )

    assert params[0] == canonical
    assert int.from_bytes(params[0][:32], "big") == 0x20


def test_permit2_input_decodes_with_canonical_signature_offset(monkeypatch):
    from sniper.execution import evm_v4_quote as V

    acct = Account.create()
    monkeypatch.setattr(V, "_permit2_nonce", lambda *_a, **_k: 7)
    body = V._permit2_input(
        "4663", acct, MINT, V.V4["4663"]["universal_router"], 123,
        "https://rpc", now_fn=lambda: 1_000_000,
    )
    permit, signature = decode(
        ["((address,uint160,uint48,uint48),address,uint256)", "bytes"],
        bytes.fromhex(body),
    )

    assert permit[0][0].lower() == MINT.lower()
    assert permit[0][1] == 123
    assert permit[0][2] == 1_001_800
    assert permit[0][3] == 7
    assert len(signature) == 65
    assert int(body[6 * 64:7 * 64], 16) == 0xE0


def test_v4_quote_rejects_uint128_overflow_before_building_calldata(monkeypatch):
    from sniper.execution import evm_v4_quote as V

    monkeypatch.setattr(V, "_gt_pools_for_mint", lambda *_a, **_k: ["0x" + "ab" * 32])
    monkeypatch.setattr(V, "_pool_key", lambda *_a, **_k: _v4_key())
    monkeypatch.setattr(V, "_quote_single", lambda *_a, **_k: 100)

    assert V.dex_v4_quote(
        "4663", WETH, MINT, 1 << 128, "0xtaker", rpc_url="https://rpc",
        now_fn=lambda: 1_000_000,
    ) is None
