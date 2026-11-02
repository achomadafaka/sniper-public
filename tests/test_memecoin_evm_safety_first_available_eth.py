"""The chain-1 safety sell probe takes the first hit (2026-10-08, a1e93afb).

Chain 1, attempt a1e93afb: evm.token_safety 7512 ms. The probe's rungs: v3 miss 117 ms, v2 miss
225, v4 HIT 376, v4_multihop hit 7291 ms. Chain 1's sell ladder waits for every rung and keeps the
largest output, so v4's hit sat behind the bridge search. Safety now asks with first_available
(wired in memecoin_evm_leg, as for 56, 4663 and 8453). Exits keep the largest-output wait.
"""
import time

import sniper.execution.evm_real_seams as S
from sniper.execution import evm_v4_multihop as mh
from sniper.settings import memecoin as cfg

COIN = "0x" + "bd" * 20


def _slow(seconds, result):
    def fn(*a, **k):
        time.sleep(seconds)
        return result
    return fn


def _quote(protocol, out):
    return {"buyAmount": str(out), "sellAmount": "1000", "route": {"protocol": protocol},
            "transaction": {"to": "0x" + "44" * 20, "data": "0x", "value": "0x0"}}


def _eth_ladder(monkeypatch):
    from sniper.execution import geckoterminal_quote as gt
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    weth = CHAIN_QUOTE["1"]["token"]
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RUNG_LADDER_CHAINS", {"1"}, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KYBER_AGG_ENABLED", False, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", False, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_LADDER_WAIT_S", 8.0, raising=False)
    monkeypatch.setattr(gt, "cooldown_active", lambda *a, **k: False)
    # The bridge search answers last and sells for more: the largest-output wait picks it.
    monkeypatch.setattr(mh, "dex_v4_multihop_quote", _slow(7.0, _quote("uniswap_v4_multihop", 1000)))
    fn = S.venue_quote_with_v2_fallback(
        _slow(0.02, None), chain="1", quote_token=weth, taker="0x" + "aa" * 20,
        v2_quote_fn=_slow(0.05, None), v4_quote_fn=_slow(0.1, _quote("uniswap_v4", 900)),
        acct=object(), rpc_url="https://rpc")
    return fn, weth


def test_the_chain_1_first_available_probe_takes_v4_not_the_bridge_search(monkeypatch):
    fn, weth = _eth_ladder(monkeypatch)
    started = time.perf_counter()
    out = fn(COIN, weth, 10 ** 18, first_available=True)
    elapsed = time.perf_counter() - started
    assert out is not None and out["route"]["protocol"] == "uniswap_v4"
    assert elapsed < 1.0, "the probe waited for the bridge search: %.2fs" % elapsed


def test_chain_1_exits_still_wait_for_the_largest_output(monkeypatch):
    """Exits are unchanged: chain 1 compares exact proceeds across every rung."""
    fn, weth = _eth_ladder(monkeypatch)
    started = time.perf_counter()
    out = fn(COIN, weth, 10 ** 18)
    assert out["route"]["protocol"] == "uniswap_v4_multihop"
    assert time.perf_counter() - started >= 6.9
