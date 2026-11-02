"""KARP's safety sell probe, on the quote 8453 safety now asks for (2026-10-07).

8453 pos 71670, attempt 8b609206: evm.token_safety 4003 ms. The probe's rungs: v3 miss 19 ms, v2
miss 242, v4 miss 557, v4_multihop miss 3839 ms (no bridge candidate), kyber HIT 352 ms. The
priority ladder waited for multihop; the first_available quote (wired for 8453 in
memecoin_evm_leg, as for 56 and 4663) takes kyber when it answers. Exits are unchanged.
"""
import time

import sniper.execution.evm_real_seams as S
from sniper.execution import evm_v4_multihop as mh
from sniper.settings import memecoin as cfg

COIN = "0x" + "27" * 20


def _slow(seconds, result):
    def fn(*a, **k):
        time.sleep(seconds)
        return result
    return fn


def _karp_ladder(monkeypatch):
    from sniper.execution import geckoterminal_quote as gt
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    weth = CHAIN_QUOTE["8453"]["token"]
    kyber = {"buyAmount": "900", "sellAmount": "1000", "route": {"protocol": "kyberswap"},
             "transaction": {"to": "0x" + "44" * 20, "data": "0x", "value": "0x0"}}
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KYBER_AGG_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KYBER_AGG_CHAINS", ("8453",), raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", False, raising=False)
    monkeypatch.setattr(gt, "cooldown_active", lambda *a, **k: False)
    monkeypatch.setattr(mh, "dex_v4_multihop_quote", _slow(2.5, None))
    monkeypatch.setattr("sniper.execution.evm_kyber_quote.dex_kyber_exit_quote", _slow(0.35, kyber))
    fn = S.venue_quote_with_v2_fallback(
        _slow(0.02, None), chain="8453", quote_token=weth, taker="0x" + "aa" * 20,
        v2_quote_fn=_slow(0.24, None), v4_quote_fn=_slow(0.55, None),
        acct=object(), rpc_url="https://rpc")
    return fn, weth


def test_the_first_available_probe_takes_kyber_not_multihops_search(monkeypatch):
    fn, weth = _karp_ladder(monkeypatch)
    started = time.perf_counter()
    out = fn(COIN, weth, 10 ** 18, first_available=True)
    elapsed = time.perf_counter() - started
    assert out is not None and out["route"]["protocol"] == "kyberswap"
    assert elapsed < 1.0, "the probe waited for multihop: %.2fs" % elapsed


def test_the_priority_ladder_exits_use_still_waits_for_the_higher_rung(monkeypatch):
    """Exits are unchanged (Gamma's block on a sell grace): they keep the priority wait."""
    fn, weth = _karp_ladder(monkeypatch)
    started = time.perf_counter()
    out = fn(COIN, weth, 10 ** 18)
    assert out["route"]["protocol"] == "kyberswap" and time.perf_counter() - started >= 2.4
