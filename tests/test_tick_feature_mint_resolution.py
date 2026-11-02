"""Targeted mint→pool resolution via factory-creation logs (coverage audit 2026-07-26)."""
import sqlite3

from sniper.analysis.tick_features import (
    V2_PAIRCREATED, V3_POOLCREATED, ensure_feature_tables, pools_for_token_via_factory,
    resolve_mint_pools,
)

MINT = "0x" + "aa" * 20
QUOTE = "0x" + "bb" * 20
PAIR = "0x" + "11" * 20
V3POOL = "0x" + "22" * 20


def _word(addr):
    return addr[2:].rjust(64, "0")


def _fake_rpc(method, params):
    if method == "eth_blockNumber":
        return {"result": "0x64"}
    assert method == "eth_getLogs"
    topics = params[0]["topics"]
    if topics[0] == V2_PAIRCREATED and topics[1] is not None:
        return {"result": [{"data": "0x" + _word(PAIR) + "0" * 64}]}
    if topics[0] == V3_POOLCREATED and len(topics) > 2 and topics[2] is not None:
        return {"result": [{"data": "0x" + "0" * 64 + _word(V3POOL)}]}
    return {"result": []}


def test_factory_lookup_extracts_v2_and_v3_pool_addresses():
    assert pools_for_token_via_factory(_fake_rpc, MINT) == sorted([PAIR, V3POOL])


def test_factory_lookup_fails_closed_on_rpc_error():
    def boom(method, params):
        raise RuntimeError("rpc down")
    assert pools_for_token_via_factory(boom, MINT) == []


def test_transfer_fallback_ranks_counterparties():
    from sniper.analysis.tick_features import TRANSFER_TOPIC, pools_for_token_via_transfers

    def rpc(method, params):
        if method == "eth_blockNumber":
            return {"result": "0x64"}
        assert params[0]["address"] == MINT
        def log(frm, to):
            return {"topics": [TRANSFER_TOPIC, "0x" + _word(frm), "0x" + _word(to)]}
        return {"result": [log(PAIR, QUOTE), log(QUOTE, PAIR), log(PAIR, "0x" + "0" * 40)]}

    got, complete = pools_for_token_via_transfers(rpc, MINT, pace_s=0)
    assert complete
    assert got[0] == PAIR                     # 3 mentions beats QUOTE's 2; zero addr dropped


def test_transfer_fallback_marks_any_failed_chunk_incomplete():
    from sniper.analysis.tick_features import pools_for_token_via_transfers

    def rpc(method, params):
        if method == "eth_blockNumber":
            return {"result": "0x9"}
        if params[0]["fromBlock"] == "0x0":
            raise TimeoutError("provider truncated the history")
        return {"result": []}

    candidates, complete = pools_for_token_via_transfers(
        rpc, MINT, lookback_blocks=9, chunk_blocks=5, pace_s=0)
    assert candidates == []
    assert complete is False


def _tc():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_feature_tables(c)
    return c


def _fake_eth_call(pool, selector):
    from sniper.analysis.tick_features import SEL_DECIMALS, SEL_TOKEN0, SEL_TOKEN1
    if selector == SEL_TOKEN0:
        return "0x" + _word(MINT)
    if selector == SEL_TOKEN1:
        return "0x" + _word(QUOTE)
    if selector == SEL_DECIMALS:
        return "0x12"
    return None


def test_resolve_mint_pools_writes_meta_for_unmapped_mint():
    tc = _tc()
    n = resolve_mint_pools(tc, "4663", [MINT], _fake_rpc, _fake_eth_call, {QUOTE},
                           "2026-07-26T00:00:00+00:00", budget=10)
    assert n == 1
    rows = tc.execute("SELECT pool, token0, token1, quote_side FROM pool_meta "
                      "WHERE chain_id='4663'").fetchall()
    assert {r["pool"] for r in rows} == {PAIR, V3POOL}
    assert all(r["token0"] == MINT and r["token1"] == QUOTE and r["quote_side"] == 1 for r in rows)


def test_resolve_mint_pools_skips_already_mapped_and_respects_budget():
    tc = _tc()
    tc.execute("INSERT INTO pool_meta (chain_id, pool, token0, token1, quote_side, resolved_at) "
               "VALUES ('4663', ?, ?, ?, 1, 'x')", (PAIR, MINT, QUOTE))
    calls = []

    def counting_rpc(method, params):
        calls.append(method)
        return {"result": "0x64"} if method == "eth_blockNumber" else {"result": []}

    assert resolve_mint_pools(tc, "4663", [MINT], counting_rpc, _fake_eth_call, {QUOTE},
                              "2026-07-26T00:00:00+00:00", budget=10) == 0
    assert calls == []          # mapped mint costs zero RPC
    other = "0x" + "cc" * 20
    assert resolve_mint_pools(tc, "4663", [other, other], counting_rpc, _fake_eth_call, {QUOTE},
                              "2026-07-26T00:00:00+00:00", budget=0) == 0
