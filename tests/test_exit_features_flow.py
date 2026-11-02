"""Phase 1 Task 5 — pool-level signed flow (dust-only wash), exec-price path, capability gating.

Router != buyer (maker = pool sender = router): the maker both-sides exclusion is GONE, so valid
flow behind one router is retained. Only amount-based dust is filtered. Direction comes from the
signed quote amount (positive = quote INTO pool = a buy of the coin).
"""
from sniper.analysis.exit_features import pool_flow, profile_capability


def test_flow_keeps_router_both_sides_and_signs():
    sw = [{"maker": "0xROUTER", "amount0": "100", "amount1": "-50", "block_ts": 1},   # buy (quote0 in)
          {"maker": "0xROUTER", "amount0": "-100", "amount1": "60", "block_ts": 2}]   # sell (quote0 out)
    f = pool_flow(sw, quote_side=0, dust_atomic=0)
    assert f.buy_n == 1 and f.sell_n == 1                 # NOT collapsed by same-maker wash
    assert f.wash_filter == "disabled_router_ambiguity"
    assert int(f.net_flow_quote) == 0                     # +100 - 100
    assert f.rejected_dust_n == 0


def test_flow_dust_is_filtered_not_wash():
    sw = [{"maker": "a", "amount0": "5", "amount1": "-1", "block_ts": 1},      # dust
          {"maker": "b", "amount0": "1000", "amount1": "-500", "block_ts": 2}]  # real buy
    f = pool_flow(sw, quote_side=0, dust_atomic=10)
    assert f.buy_n == 1 and f.rejected_dust_n == 1


def test_profile_capability_chain_and_kind():
    sol = profile_capability("solana", None)
    assert sol.forced_legacy and not sol.flow_trusted

    # 4663 depth is now evidence-verified; 8453 remains the V3 no-depth profile.
    v3 = profile_capability("8453", "v3")
    assert v3.flow_trusted and v3.vol_trusted and v3.structure_trusted and not v3.depth_trusted

    v2 = profile_capability("4663", "v2")
    assert v2.flow_trusted and v2.depth_trusted and not v2.forced_legacy

    unknown = profile_capability("4663", "mystery")
    assert unknown.forced_legacy and not unknown.flow_trusted   # fail-closed on unknown pool kind


# --- P1.3: V2 Mint/Burn decode -> real depth_pull_flag (2026-07-24) ---

def _word(n):
    return f"{n & (2**256 - 1):064x}"


def test_v2_burn_and_mint_decode_with_into_pool_sign_convention():
    from sniper.services.memecoin_tick_stream import V2_BURN, V2_MINT, decode_evm_log
    burn = decode_evm_log("4663", {
        "address": "0xp", "blockNumber": "0x5", "logIndex": "0x0", "transactionHash": "0xt",
        "topics": [V2_BURN, "0x" + _word(0xABC), "0x" + _word(0xDEF)],
        "data": "0x" + _word(1000) + _word(2000)}, observed_at="t")
    assert burn["kind"] == "v2_burn"
    assert burn["amount0"] == "-1000" and burn["amount1"] == "-2000"   # OUT of the pool -> negative
    mint = decode_evm_log("4663", {
        "address": "0xp", "blockNumber": "0x5", "logIndex": "0x1", "transactionHash": "0xt",
        "topics": [V2_MINT, "0x" + _word(0xABC)],
        "data": "0x" + _word(1000) + _word(2000)}, observed_at="t")
    assert mint["kind"] == "v2_mint" and mint["amount0"] == "1000"     # INTO the pool -> positive


def test_getlogs_topic_filter_requests_mint_and_burn():
    """A decoder that can read Burn is useless if the scan never asks for it (the `assets_ids`
    lesson: a silently-unsubscribed topic delivers zero data forever)."""
    import sniper.services.memecoin_tick_stream as ts
    seen = {}

    def rpc(method, params):
        if method == "eth_blockNumber":
            return "0x2"
        seen["topics"] = params[0]["topics"][0]
        return []

    import sqlite3
    from sniper.persistence import db_ticks as t
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row; t.ensure_tick_tables(c)
    t.set_cursor(c, "4663", 1, "t")
    ts.evm_tick_pass(c, "4663", rpc, "2026-07-24T00:00:00+00:00")
    assert ts.V2_BURN in seen["topics"] and ts.V2_MINT in seen["topics"]
