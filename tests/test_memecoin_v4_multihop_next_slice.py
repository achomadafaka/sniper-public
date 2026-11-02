"""A slice of paths that all fail to quote is followed by the next slice (2026-09-28, Alpha GO).

Once the batched liquidity read (extsload(bytes32[])) reached every pair, coin 0x941cf3f6 on 4663
lost its route on the provider path: the WETH/0x4fc59c pair, never read under the old 32-read cap,
now supplied 12 paths at static fee 0-100 whose hooked coin-side pools do not quote, and the stable
fee sort cut the working 0xc0d6 path (static fee 100) at max_paths=12. Budget left: 73 requests and
2.75 s. When no path of a slice quotes, the search now measures the next slice while the budget
allows. A slice with any measured path ends the search: the best measured round trip still wins.
"""
import threading

import pytest

from sniper.execution import evm_v4_multihop as MH

CHAIN = "4663"
NATIVE = "0x" + "00" * 20
DEAD = "0x" + "dd" * 20
GOOD = "0x" + "99" * 20
COIN = "0x" + "c0" * 20
AMOUNT = 10 ** 16


def _key(c0, c1, fee, hooks=NATIVE):
    return {"currency0": c0, "currency1": c1, "fee": fee, "tick_spacing": 60, "hooks": hooks}


def _transport(*_a, **_kw):
    return 200, {"jsonrpc": "2.0", "id": 1, "result": "0x0"}, None


@pytest.fixture
def search(monkeypatch):
    """16 two-leg paths through DEAD at static fee 0 that never quote, one through GOOD at 500."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_MAX_LEGS", 2, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_MAX_PATHS", 12, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_POOLS_PER_STEP", 4, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_STORE_FIRST", False, raising=False)
    st = {"quoted": [], "good": {("g_bridge", "g_coin")}, "lock": threading.Lock()}
    monkeypatch.setattr(MH, "_pools_with_depth", lambda *a, **k: [])
    monkeypatch.setattr(MH, "_candidate_bridges", lambda *a, **k: iter([DEAD, GOOD]))
    monkeypatch.setattr(MH, "_decision_block", lambda *a, **k: "0xabc")
    bridge = {DEAD: [("d_bridge%d" % i, _key(NATIVE, DEAD, 0), 1e6) for i in range(4)],
              GOOD: [("g_bridge", _key(NATIVE, GOOD, 500), 1e6)]}
    coin = {DEAD: [("d_coin%d" % i, _key(DEAD, COIN, 0, "0x" + "ab" * 20), None) for i in range(4)],
            GOOD: [("g_coin", _key(GOOD, COIN, 0), None)]}
    monkeypatch.setattr(MH, "_bridge_options",
                        lambda _c, _cash, b, _r, *, limit, **k: bridge[b][:limit])
    monkeypatch.setattr(MH, "_coin_leg_options",
                        lambda _c, b, _m, _r, _d, *, limit, **k: coin[b][:limit])
    ids = {id(k): pid for opts in (bridge, coin) for rows in opts.values() for pid, k, _d in rows}

    def quote(_chain, currency_in, path, amount_in, _rpc, block="latest", **kw):
        try:
            kw["http"]("POST", "http://rpc", body="{}")
        except MH.RequestNotSent:
            return None
        pools = tuple(sorted(ids[id(k)] for _c, k in path))
        with st["lock"]:
            st["quoted"].append(pools)
        if pools not in {tuple(sorted(g)) for g in st["good"]}:
            return None
        return AMOUNT if str(currency_in).lower() != NATIVE else 1_000
    monkeypatch.setattr(MH, "_quote_path", quote)
    MH._BRIDGE_LAST_GOOD.clear()
    return st


def _run():
    return MH.multihop_quote(CHAIN, NATIVE, COIN, AMOUNT, rpc_url="http://rpc",
                             bridge_currencies=(), clip_usd=25.0, min_depth_clips=20.0,
                             http=_transport)


def test_a_slice_that_never_quotes_is_followed_by_the_next(search):
    route = _run()
    assert route is not None, MH.last_multihop_refusal()
    assert route["bridge"] == GOOD and route["coin_pool_id"] == "g_coin"
    assert route["paths_considered"] == 17 and route["paths_measured"] == 1


def test_a_slice_with_a_measured_path_ends_the_search(search):
    search["good"].add(("d_bridge0", "d_coin0"))
    route = _run()
    assert route is not None and route["bridge"] == DEAD
    assert len({p for p in search["quoted"]}) <= 12, "the second slice is never measured"


def test_the_request_budget_still_bounds_the_next_slice(search, monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_REQUEST_BUDGET", 12, raising=False)
    route = _run()
    assert route is None, "the first slice spent every request; the next slice is never sent"
    assert len(search["quoted"]) == 12
    assert ("g_bridge", "g_coin") not in search["quoted"]
