"""V4 as an independent rung, decoupled from MEMECOIN_EVM_RUNG_LADDER_CHAINS and
MEMECOIN_DEX_BLIND_CHAINS (2026-09-16, Arc build).

MEMECOIN_EVM_RUNG_LADDER_CHAINS gates v2 AND v4 together, rooted in a pricing-source history
(MEMECOIN_DEX_BLIND_CHAINS) and a V2-entry flag that have nothing to do with Arc: real V3 core,
not DexScreener-blind, no V2 pools at all. MEMECOIN_EVM_V4_RUNG_CHAINS lets a chain try V4 without
any of that — this file proves it does so WITHOUT also opening V2, and that every existing chain's
behavior is untouched.
"""
import pytest

from sniper.execution.evm_real_seams import venue_quote_with_v2_fallback
from sniper.settings import memecoin as cfg

WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
MINT = "0xf0568863195770965a6d8abb0aa87f4314b80320"
ARC = "5042"


@pytest.fixture(autouse=True)
def _route_cost_is_not_what_this_file_tests():
    """Same fixture as test_memecoin_evm_v2_entry.py: a fixture quote cannot be measured, so
    without this every hit here would be refused as round_trip_unmeasured for reasons unrelated
    to what these tests check."""
    import sniper.execution.evm_real_seams as _S
    original = _S._round_trip_for_hit
    _S._round_trip_for_hit = lambda *_a, **_k: (-3.0, None)
    try:
        yield
    finally:
        _S._round_trip_for_hit = original


def _combined(*, chain, v2_spy, v4_result, enabled=False):
    def primary(sell, buy, amount, *, slippage_bps=None):
        return None   # v3 always misses in these tests -- the point is what tries next

    def v2(chain_, sell, buy, amount, taker, *, http=None, slippage_bps=None,
           tax_buffer_bps=None):
        v2_spy.append({"chain": chain_, "sell": sell, "buy": buy})
        return {"buyAmount": "999", "transaction": {"to": "0xrouter"},
                "issues": {"allowance": {"spender": "0xrouter"}}}

    def v4(chain_, sell, buy, amount, taker, **kwargs):
        return v4_result

    return venue_quote_with_v2_fallback(
        primary, chain=chain, quote_token=WETH, taker="0xtaker", http=None,
        v2_quote_fn=v2, v4_quote_fn=v4, enabled=enabled)


def test_v4_only_chain_tries_v4_without_opening_v2(monkeypatch):
    """Arc: not in the rung ladder, V2 entry disabled, but listed in the new V4-only set."""
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RUNG_LADDER_CHAINS", set(), raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_RUNG_CHAINS", {ARC}, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", True, raising=False)
    v2_spy = []

    fn = _combined(chain=ARC, v2_spy=v2_spy, v4_result={"buyAmount": "42"}, enabled=False)
    res = fn(WETH, MINT, 10 ** 18)

    assert res is not None and res["buyAmount"] == "42"   # v4 answered
    assert v2_spy == []                                    # v2 was never tried


def test_a_v4_miss_on_the_v4_only_set_still_never_tries_v2(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RUNG_LADDER_CHAINS", set(), raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_RUNG_CHAINS", {ARC}, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", True, raising=False)
    v2_spy = []

    fn = _combined(chain=ARC, v2_spy=v2_spy, v4_result=None, enabled=False)
    res = fn(WETH, MINT, 10 ** 18)

    assert res is None      # honest miss: v3 missed, v4 missed, nothing left to try
    assert v2_spy == []     # still never tried v2 -- membership here is v4-only, not a v2 grant


def test_a_chain_not_in_either_set_tries_neither(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RUNG_LADDER_CHAINS", set(), raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_RUNG_CHAINS", set(), raising=False)
    v2_spy = []

    fn = _combined(chain=ARC, v2_spy=v2_spy, v4_result={"buyAmount": "42"}, enabled=True)
    res = fn(WETH, MINT, 10 ** 18)

    assert res is None
    assert v2_spy == []


def test_existing_ladder_chains_are_byte_for_byte_unaffected(monkeypatch):
    """4663/8453/56 keep trying v2+v4 exactly as before -- the new set is additive, not a
    replacement, and its default (empty) must change nothing for them."""
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RUNG_LADDER_CHAINS", {"4663", "8453", "56"},
                        raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_RUNG_CHAINS", set(), raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", True, raising=False)
    for chain in ("4663", "8453", "56"):
        v2_spy = []
        fn = _combined(chain=chain, v2_spy=v2_spy, v4_result={"buyAmount": "7"}, enabled=True)
        res = fn(WETH, MINT, 10 ** 18)
        assert res is not None and res["buyAmount"] in ("999", "7"), chain
        # v2 is the FIRST rung tried after v3 in the real ladder, so its spy fires whenever v2
        # is in scope at all -- present here proves the existing chains still open v2.
        assert v2_spy, f"{chain} lost its v2 rung"


# --- pool-key resolution: the property the rung gate above actually depends on ----------------

def test_a_v4_coin_with_a_stored_arc_key_resolves_without_any_live_read(monkeypatch):
    """`_pool_key` checks the durable store before ever touching the chain. For Arc that matters
    more than for 4663: the QuickNode plan caps eth_getLogs at 5 blocks, so the live-scan fallback
    this same function falls through to cannot work there -- the store is the only path."""
    from sniper.execution import evm_v4_quote as V

    stored_key = {"currency0": "0x3600000000000000000000000000000000000000",
                 "currency1": "0xece5ca8bf9220718e5727754026757512212cb3c",
                 "fee": 0, "tick_spacing": 1,
                 "hooks": "0x01ef4b86be3e26cf1ac65f12c9daffaecca62acc"}
    monkeypatch.setattr(V, "_stored_pool_key", lambda chain, pool_id: stored_key)

    def _boom_rpc(*_a, **_k):
        raise AssertionError("a stored key must resolve without any RPC call")
    monkeypatch.setattr(V, "_rpc", _boom_rpc)

    key = V._pool_key(ARC, "0x" + "ab" * 32, "wss://unused")
    assert key == stored_key


def test_a_v4_coin_with_no_stored_key_and_a_dead_scan_resolves_to_none(monkeypatch):
    """The honest-miss side: nothing stored, and the live scan (which cannot work on Arc's
    5-block-capped endpoint) also finds nothing -- the coin genuinely has no known route yet."""
    from sniper.execution import evm_v4_quote as V

    monkeypatch.setattr(V, "_stored_pool_key", lambda chain, pool_id: None)

    def _capped_getlogs(*_a, **_k):
        raise RuntimeError("eth_getLogs is limited to a 5 range")
    monkeypatch.setattr(V, "_rpc", lambda url, method, params, http=None:
                        {"result": "0x142e346"} if method == "eth_blockNumber"
                        else _capped_getlogs())
    monkeypatch.setattr(V, "_key_scan_page_blocks", lambda chain: 0)   # scan disabled, as it is here

    # A DIFFERENT pool_id than the "stored" test above: _pool_key checks an in-process cache
    # before the store, and a shared id here would silently pass on a leftover cache hit instead
    # of exercising the honest-miss path this test is actually about.
    key = V._pool_key(ARC, "0x" + "cd" * 32, "wss://unused")
    assert key is None


def test_membership_in_both_sets_is_not_double_counted(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RUNG_LADDER_CHAINS", {ARC}, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_RUNG_CHAINS", {ARC}, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", True, raising=False)
    v2_spy = []

    fn = _combined(chain=ARC, v2_spy=v2_spy, v4_result={"buyAmount": "5"}, enabled=True)
    res = fn(WETH, MINT, 10 ** 18)

    assert v2_spy   # ladder membership still opens v2 normally
    assert res is not None
