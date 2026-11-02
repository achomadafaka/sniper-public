"""WO-12A — non-blocking hot-lane prewarm scheduler.

The proven defect: `preview_fn` ran SYNCHRONOUSLY inside the asyncio housekeeping loop, so one slow
quote HTTP call blocked WebSocket frame processing AND the pull-fallback trigger. WO-12A moves preview
work OFF the loop with:
- bounded concurrency + a hard per-call deadline;
- per-position/route coalescing (at most one in-flight + one newest-pending request);
- results applied ON the owning loop (execution/signing/SQLite/state never leave the loop thread);
- priority by construction: prewarm has its OWN small executor (spare capacity) — sale-shaped work
  (reconciliation, exact confirm, decode, pull fallback) runs on the loop / its own path and is never
  blocked or delayed by prewarm.
"""
import asyncio
import threading

from sniper.services.memecoin_hot_supervisor import PrewarmScheduler, PrewarmWorkerPool


def test_schedule_is_non_blocking_when_preview_stalls():
    """schedule() must return immediately and the loop must stay responsive while a preview stalls —
    this is exactly the WS-ingestion / pull-fallback non-blocking guarantee."""
    release = threading.Event()

    def preview_fn(key, pos):
        release.wait(2.0)            # stalls well past the deadline
        return None

    async def main():
        sched = PrewarmScheduler(preview_fn, lambda *a: None, now_fn=lambda: 0.0,
                                 max_workers=2, deadline_s=0.15)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        sched.schedule(("solana", "P"), {"id": 1})
        assert loop.time() - t0 < 0.1          # schedule() did not block on the stalled preview
        # the loop keeps servicing other work (WS frames / pull fallback) promptly
        t1 = loop.time()
        await asyncio.sleep(0.03)
        assert loop.time() - t1 < 0.3
        release.set()
        sched.shutdown()

    asyncio.run(main())


def test_preview_results_applied_on_owning_loop():
    loop_ident = {}
    applied_ident = {}

    def preview_fn(key, pos):
        return object()             # non-None, no `.failure` -> applied

    def apply_fn(key, preview, now_ts):
        applied_ident["id"] = threading.get_ident()

    async def main():
        loop_ident["id"] = threading.get_ident()
        sched = PrewarmScheduler(preview_fn, apply_fn, now_fn=lambda: 0.0, deadline_s=1.0)
        sched.schedule(("solana", "P"), {"id": 1})
        await asyncio.sleep(0.2)
        sched.shutdown()

    asyncio.run(main())
    assert applied_ident.get("id") == loop_ident["id"]   # apply ran on the loop, NOT the worker thread


def test_repeated_prewarm_requests_coalesce():
    calls = []
    gate = threading.Event()

    def preview_fn(key, pos):
        calls.append(pos["v"])
        gate.wait(1.0)              # hold the first in-flight so the rest must coalesce
        return None

    async def main():
        sched = PrewarmScheduler(preview_fn, lambda *a: None, now_fn=lambda: 0.0,
                                 max_workers=2, deadline_s=5.0)
        for v in range(5):
            sched.schedule(("solana", "P"), {"v": v})     # 5 rapid requests, SAME key
        await asyncio.sleep(0.1)
        assert sched.inflight_keys() == {("solana", "P")}   # exactly one in-flight
        assert calls == [0]                                 # only the first launched; rest coalesced
        gate.set()
        await asyncio.sleep(0.3)
        assert calls == [0, 4]                              # only the NEWEST pending ran after
        sched.shutdown()

    asyncio.run(main())


def test_older_result_is_not_applied_when_newer_request_is_queued():
    calls = []
    applied = []
    first_started = threading.Event()
    release_first = threading.Event()

    def preview_fn(key, pos):
        calls.append(pos["v"])
        if pos["v"] == 0:
            first_started.set()
            release_first.wait(1.0)
        return pos

    async def main():
        sched = PrewarmScheduler(
            preview_fn, lambda _key, preview, _ts: applied.append(preview["v"]),
            now_fn=lambda: 0.0, max_workers=1, deadline_s=2.0)
        sched.schedule(("solana", "P"), {"v": 0})
        assert await asyncio.to_thread(first_started.wait, 1.0)
        sched.schedule(("solana", "P"), {"v": 1})
        release_first.set()
        for _ in range(100):
            if applied == [1]:
                break
            await asyncio.sleep(0.01)
        sched.shutdown()

    asyncio.run(main())
    assert calls == [0, 1]
    assert applied == [1]


def test_shared_worker_pool_bounds_timed_out_work_across_lane_epochs():
    """The runner rebuilds the lane every 30s. A timed-out thread from the prior epoch must still
    consume the shared capacity, rather than a new executor creating another overlapping call."""
    calls = []
    release = threading.Event()
    pool = PrewarmWorkerPool(max_workers=1)

    def preview_fn(key, pos):
        calls.append(pos["epoch"])
        release.wait(2.0)

    async def epoch(epoch_no):
        sched = PrewarmScheduler(
            preview_fn, lambda *args: None, now_fn=lambda: 0.0,
            max_workers=1, deadline_s=0.03, worker_pool=pool)
        sched.schedule(("solana", "P"), {"epoch": epoch_no})
        await asyncio.sleep(0.08)
        sched.shutdown()

    asyncio.run(epoch(1))
    asyncio.run(epoch(2))
    assert calls == [1]
    release.set()
    pool.shutdown()


def test_timeout_does_not_accumulate_worker_work():
    calls = []
    gate = threading.Event()

    def preview_fn(key, pos):
        calls.append(1)
        gate.wait(3.0)             # every preview exceeds the deadline (hangs)
        return None

    async def main():
        sched = PrewarmScheduler(preview_fn, lambda *a: None, now_fn=lambda: 0.0,
                                 max_workers=2, deadline_s=0.05)
        for _ in range(30):
            sched.schedule(("solana", "P"), {"id": 1})
            await asyncio.sleep(0.01)
        # 30 requests, all timing out: coalescing + the concurrency gate bound the worker work — the
        # executor NEVER accumulates a growing backlog of hung threads.
        assert len(calls) <= 2                              # at most max_workers threads ever started
        assert len(sched.pending_keys()) <= 1
        assert len(sched.inflight_keys()) <= 1
        gate.set()
        sched.shutdown()

    asyncio.run(main())


def _evm_book(monkeypatch):
    """A warmed V2 4663 pool + engine-envelope position, ready to CUT on an LP burn (WO-9 harness)."""
    import sqlite3
    from datetime import datetime, timezone

    from sniper.analysis.tick_features import ensure_feature_tables
    from sniper.persistence import db_ticks as t
    from sniper.services.memecoin_funded_policy import ENGINE_ENVELOPE_POLICY_ID
    from sniper.services.memecoin_hot_supervisor import build_evm_hot_book

    base = 1_800_000_000
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    t.ensure_tick_tables(c)
    t.ensure_block_times(c)
    ensure_feature_tables(c)
    c.execute("INSERT INTO pool_meta(chain_id,pool,token0,token1,quote_side,"
              "token0_decimals,token1_decimals,resolved_at) "
              "VALUES('4663','0xpool','0xmint','0xq',1,18,18,'t')")
    t.insert_ticks(c, [{"chain_id": "4663", "pool": "0xpool", "kind": "v2_sync", "block_no": 1,
                        "tx_sig": "0x1", "log_index": 0, "amount0": "1000000", "amount1": "2000000",
                        "observed_at": "i"}])
    for n in range(2, 8):
        t.insert_ticks(c, [{"chain_id": "4663", "pool": "0xpool", "kind": "v2_swap", "block_no": n,
                            "tx_sig": f"0x{n}", "log_index": 0, "amount0": str(100 + n),
                            "amount1": str(-(40 + n)), "observed_at": "i"}])
    for n in range(1, 8):
        t.backfill_block_times(c, "4663", [n], lambda _b, n=n: base + n, "i")
    policy = type("P", (), {"policy_id": ENGINE_ENVELOPE_POLICY_ID, "coin_band": "nano",
                            "decision_engine_version": "v5"})()
    monkeypatch.setattr("sniper.services.memecoin_funded_policy.frozen_policy_from_position",
                        lambda _pos: policy)
    pos = {"id": 42, "network_id": "4663", "mint": "0xmint", "quote_route_target": "0xr",
           "derisked_at": None, "exact_quote_misses": 0,
           "opened_at": datetime.fromtimestamp(base - 100, tz=timezone.utc).isoformat()}
    engines, entries, held = build_evm_hot_book([pos], c, now_fn=lambda: base + 8)
    return base, engines, entries, held


def _burn_log(base):
    def _w(v):
        return format(v & (2 ** 256 - 1), "064x")
    from sniper.services import memecoin_tick_stream as tsm
    return {"topics": [tsm.V2_BURN, "0x" + _w(0xdead)],
            "data": "0x" + _w(500_000) + _w(1_000_000), "address": "0xpool",
            "blockNumber": hex(8), "transactionHash": "0xburn", "logIndex": hex(1),
            "blockHash": "0xblockhash", "blockTimestamp": hex(base + 8), "removed": False}


def test_stalled_preview_does_not_delay_evm_decision(monkeypatch):
    """End-to-end: a preview_fn hung past its deadline must NOT delay the EVM rug reaching V5 through
    the real run_hot_exit_lane (WS/poll ingestion + decision are on the loop; preview is off it)."""
    from sniper.services.memecoin_hot_supervisor import run_hot_exit_lane
    base, engines, entries, held = _evm_book(monkeypatch)
    stall = threading.Event()

    def preview_fn(key, pos):
        stall.wait(5.0)                  # hang every prewarm well past the deadline
        return None

    seen = []
    state = {"stop": False, "gets": 0}

    def rpc_call(method, params):
        if method == "eth_blockNumber":
            return hex(8)
        state["gets"] += 1
        return [_burn_log(base)] if state["gets"] == 1 else []

    def on_decision(key, emit, row, preview):
        seen.append(emit)
        state["stop"] = True

    asyncio.run(run_hot_exit_lane(
        engines, {}, entries, now_fn=lambda: float(base + 8), on_decision=on_decision,
        pull_fn=lambda: None, preview_fn=preview_fn,
        evm_lanes=[{"chain_id": "4663", "held_pools": held["4663"], "rpc_call": rpc_call}],
        now_iso_fn=lambda: "iso", evm_poll_s=0.01, prewarm_period_s=0.01,
        prewarm_workers=2, prewarm_deadline_s=0.05, duration_s=3.0, stop=lambda: state["stop"]))
    stall.set()
    assert seen and seen[0].action == "CUT"    # decision reached V5 despite the permanently-stalled preview


def test_sale_shaped_loop_work_is_not_blocked_by_saturated_prewarm():
    """Priority by construction: with the prewarm pool fully saturated by hung previews, sale-shaped
    work (which runs on the loop / its own path) still completes promptly."""
    gate = threading.Event()

    def preview_fn(key, pos):
        gate.wait(3.0)

    async def main():
        sched = PrewarmScheduler(preview_fn, lambda *a: None, now_fn=lambda: 0.0,
                                 max_workers=2, deadline_s=5.0)
        for k in ("A", "B", "C", "D"):
            sched.schedule(("solana", k), {"id": k})      # saturate + queue the prewarm pool
        await asyncio.sleep(0.05)
        # a sale-shaped confirmation is just loop work here — it must not wait on prewarm
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await asyncio.sleep(0)                             # yield once; sale work runs
        assert loop.time() - t0 < 0.3
        gate.set()
        sched.shutdown()

    asyncio.run(main())
