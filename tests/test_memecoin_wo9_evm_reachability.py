"""WO-9 production reachability — an EVM event reaches V5 through the REAL supervisor boundary.

`build_evm_hot_book` warms a real `HotPositionEngine` from the confirmed-block EVM tape; the real
`HotSupervisor` plus the real `poll_evm_hot_once`/`fetch_evm_hot_logs` deliver one confirmed rug
block; the decision (real `propose_v5`) reaches `on_decision`. No live socket, no flags — the
production wiring exercised end to end. 4663 is NEVER added to the V3-depth-verified set (the rug
here is a V2 LP burn, whose depth is trusted without any verified-chain claim).
"""
import sqlite3
from datetime import datetime, timezone

from sniper.analysis.tick_features import ensure_feature_tables
from sniper.persistence import db_ticks as t
from sniper.services import memecoin_evm_hot as eh
from sniper.services import memecoin_tick_stream as ts
from sniper.services.memecoin_funded_policy import ENGINE_ENVELOPE_POLICY_ID
from sniper.services.memecoin_hot_supervisor import (
    HotSupervisor, build_evm_hot_book, row_is_transaction_grounded)

BASE = 1_800_000_000
CHAIN = "4663"
POOL = "0xpool"
MINT = "0xmint"


def _w(v):
    return format(v & (2 ** 256 - 1), "064x")


def _warm_v2_pool(c):
    """A V2 pool past WARMUP: one reserve sync + 6 structured swaps, each on its own true-time block.
    V2 depth (reserve_base) is trusted with NO verified-chain claim, so an LP burn CUTs deterministically."""
    t.insert_ticks(c, [{"chain_id": CHAIN, "pool": POOL, "kind": "v2_sync", "block_no": 1,
                        "tx_sig": "0x1", "log_index": 0, "maker": None,
                        "amount0": "1000000", "amount1": "2000000", "observed_at": "i"}])
    for n in range(2, 8):
        t.insert_ticks(c, [{"chain_id": CHAIN, "pool": POOL, "kind": "v2_swap", "block_no": n,
                            "tx_sig": f"0x{n}", "log_index": 0, "maker": None,
                            "amount0": str(100 + n), "amount1": str(-(40 + n)), "observed_at": "i"}])
    for n in range(1, 8):
        t.backfill_block_times(c, CHAIN, [n], lambda _b, n=n: BASE + n, "i")


def _ticks_conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    t.ensure_tick_tables(c)
    t.ensure_block_times(c)
    ensure_feature_tables(c)
    # The canonical resolve_pool_for_position keys on pool_meta (mint -> base side), NEVER
    # quote_route_target. Seed the EVM pool row so the hot lane resolves the SAME pool the pull
    # materializer would (quote_side=1 -> token0 is the base/mint side).
    c.execute("INSERT INTO pool_meta(chain_id,pool,token0,token1,quote_side,"
              "token0_decimals,token1_decimals,resolved_at) VALUES(?,?,?,?,1,18,18,'t')",
              (CHAIN, POOL, MINT, "0xquote"))
    _warm_v2_pool(c)
    return c


def _position():
    # quote_route_target is the router/settler (NOT the pool) — it must be ignored by resolution.
    return {"id": 42, "network_id": CHAIN, "mint": MINT, "quote_route_target": "0xrouter",
            "derisked_at": None, "exact_quote_misses": 0,
            "opened_at": datetime.fromtimestamp(BASE - 100, tz=timezone.utc).isoformat()}


def _engine_policy(monkeypatch):
    policy = type("P", (), {"policy_id": ENGINE_ENVELOPE_POLICY_ID, "coin_band": "nano",
                            "decision_engine_version": "v5"})()
    monkeypatch.setattr("sniper.services.memecoin_funded_policy.frozen_policy_from_position",
                        lambda _pos: policy)


def _burn_log():
    return {"topics": [ts.V2_BURN, "0x" + _w(0xdead)],
            "data": "0x" + _w(500_000) + _w(1_000_000), "address": POOL,
            "blockNumber": hex(8), "transactionHash": "0xburn", "logIndex": hex(1),
            "blockHash": "0xblockhash", "blockTimestamp": hex(BASE + 8), "removed": False}


def test_build_evm_hot_book_warms_engine_and_scopes_held_pool(monkeypatch):
    _engine_policy(monkeypatch)
    engines, entries, held = build_evm_hot_book([_position()], _ticks_conn(), now_fn=lambda: BASE + 8)
    assert engines.keys() == {(CHAIN, POOL)}
    assert held == {CHAIN: [POOL]}
    assert entries[(CHAIN, POOL)]["engine"].ticks_seen == 7      # 1 sync + 6 swaps warmed


def test_build_evm_hot_book_uses_resolved_quote_side(monkeypatch):
    _engine_policy(monkeypatch)
    ticks = _ticks_conn()
    ticks.execute(
        "UPDATE pool_meta SET token0='0xquote',token1=?,quote_side=0 "
        "WHERE chain_id=? AND pool=?", (MINT, CHAIN, POOL))
    engines, entries, held = build_evm_hot_book([_position()], ticks, now_fn=lambda: BASE + 8)

    assert engines.keys() == {(CHAIN, POOL)}
    assert entries[(CHAIN, POOL)]["engine"].quote_side == 0


def test_evm_event_reaches_v5_through_real_supervisor(monkeypatch):
    """The production boundary: real book -> real poll -> real supervisor -> real propose_v5 -> CUT."""
    _engine_policy(monkeypatch)
    engines, entries, held = build_evm_hot_book([_position()], _ticks_conn(), now_fn=lambda: BASE + 8)

    seen = []
    sup = HotSupervisor(engines, now_fn=lambda: BASE + 8,
                        on_decision=lambda key, emit, row, preview: seen.append((key, emit, row)))

    def rpc_call(method, params):
        if method == "eth_blockNumber":
            return hex(8)
        if method == "eth_getLogs":
            return [_burn_log()]
        raise AssertionError(f"unexpected rpc {method}")

    new_cursor = eh.poll_evm_hot_once(sup, CHAIN, held[CHAIN], rpc_call, lambda: "iso", cursor=7)
    assert new_cursor == 8
    assert len(seen) == 1, "the EVM rug must reach on_decision through the real supervisor"
    key, emit, row = seen[0]
    assert key == (CHAIN, POOL)
    assert emit.action == "CUT"                                  # real propose_v5 authorized the sale
    assert row_is_transaction_grounded(row)                     # via explicit tx-grounded provenance


def test_fetch_evm_hot_logs_is_rpc_only_and_feed_is_separate(monkeypatch):
    """WO-9 loop-safety: the RPC phase is thread-safe (no supervisor), feeding is a separate step."""
    _engine_policy(monkeypatch)
    engines, entries, held = build_evm_hot_book([_position()], _ticks_conn(), now_fn=lambda: BASE + 8)
    seen = []
    sup = HotSupervisor(engines, now_fn=lambda: BASE + 8,
                        on_decision=lambda key, emit, row, preview: seen.append(emit))

    def rpc_call(method, params):
        return hex(8) if method == "eth_blockNumber" else [_burn_log()]

    cursor, blocks = eh.fetch_evm_hot_logs(CHAIN, held[CHAIN], rpc_call, cursor=7)
    assert cursor == 8 and len(blocks) == 1 and seen == []       # fetch touches no supervisor
    eh.feed_evm_blocks(sup, CHAIN, blocks, observed_at="iso")
    assert len(seen) == 1 and seen[0].action == "CUT"


def test_run_hot_exit_lane_drives_evm_lane_to_v5(monkeypatch):
    """The full async orchestration: an EVM-only book (no Solana vaults) runs through run_hot_exit_lane
    and one confirmed rug block reaches V5 via the shared supervisor + enforced pull fallback."""
    import asyncio

    from sniper.services.memecoin_hot_supervisor import run_hot_exit_lane
    _engine_policy(monkeypatch)
    engines, entries, held = build_evm_hot_book([_position()], _ticks_conn(), now_fn=lambda: BASE + 8)

    seen = []
    state = {"stop": False, "gets": 0}

    def rpc_call(method, params):
        if method == "eth_blockNumber":
            return hex(8)
        state["gets"] += 1
        return [_burn_log()] if state["gets"] == 1 else []

    def on_decision(key, emit, row, preview):
        seen.append(emit)
        state["stop"] = True

    asyncio.run(run_hot_exit_lane(
        engines, {}, entries, now_fn=lambda: float(BASE + 8), on_decision=on_decision,
        pull_fn=lambda: None, preview_fn=lambda k, p: None,
        evm_lanes=[{"chain_id": CHAIN, "held_pools": held[CHAIN], "rpc_call": rpc_call}],
        now_iso_fn=lambda: "iso", evm_poll_s=0.01, duration_s=3.0, stop=lambda: state["stop"]))
    assert seen and seen[0].action == "CUT"    # EVM rug reached V5 through the real async lane


def test_4663_v3_depth_verified_after_wo10_evidence_closure():
    # WO-9 shipped with 4663 UNverified (no captured evidence). WO-10 closed the evidence (real
    # Swap/Mint/Burn/Collect fixtures + parity), so 4663 is now depth-verified; 8453 remains out.
    from sniper.settings import memecoin as cfg
    assert "4663" in cfg.MEMECOIN_V3_DEPTH_VERIFIED_CHAINS
    assert "8453" not in cfg.MEMECOIN_V3_DEPTH_VERIFIED_CHAINS
