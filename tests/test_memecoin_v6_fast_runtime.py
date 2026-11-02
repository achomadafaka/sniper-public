import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone

from sniper.services.memecoin_v6_events import V6MarketEvent, event_identity
from sniper.services.memecoin_v6_fast_runtime import (
    ActionQuoteJob, DirtyPositionQueue, EntryQuoteJob, FastRuntimeContext, FollowupQuoteJob,
    V6FastRuntime, V6QuoteScheduler,
)
from sniper.services.memecoin_v6_incremental_state import PoolAccumulator, PoolMeta


NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
POOL = "0x" + "ab" * 20


def _event(index):
    at = (NOW + timedelta(milliseconds=index)).isoformat()
    return V6MarketEvent(
        network_id="4663", pool=POOL, block_height=10 + index,
        block_hash=f"0xb{index}", tx_id=f"0xt{index}", event_index=index,
        source_event_at=at, received_at=at, kind="v2_swap",
        payload={"amount0": "100", "amount1": "-50"},
    )


def test_burst_coalesces_without_losing_latest_evidence():
    async def case():
        queue = DirtyPositionQueue(coalesce_ms=20)
        burst = [_event(i) for i in range(5)]
        for event in burst:
            queue.mark("p1", event_identity(event))
        work = await asyncio.wait_for(queue.next_ready(), 0.2)
        assert work.position_id == "p1"
        assert work.latest_evidence_id == event_identity(burst[-1])
        assert queue.pending_count == 0

    asyncio.run(case())


def test_quote_scheduler_isolates_slow_positions_and_coalesces_newest_request():
    async def case():
        scheduler = V6QuoteScheduler(max_workers=2)
        release = threading.Event()

        def slow():
            release.wait(1)
            return "slow"

        scheduler.request("p1", "old", slow)
        scheduler.request("p1", "new", lambda: "newest")
        scheduler.request("p2", "p2", lambda: "fast")
        result = await asyncio.wait_for(scheduler.next_result(), 0.2)
        assert result.position_id == "p2" and result.value == "fast"
        release.set()
        seen = [await asyncio.wait_for(scheduler.next_result(), 0.5) for _ in range(2)]
        assert [item.request_id for item in seen] == ["old", "new"]
        await scheduler.close()

    asyncio.run(case())


def test_solana_provider_wait_cannot_block_evm_lane():
    async def case():
        scheduler = V6QuoteScheduler(max_workers=1)
        release = threading.Event()
        scheduler.request(
            "sol", "sol", lambda: release.wait(1) or "sol", lane="solana")
        scheduler.request("evm", "evm", lambda: "evm", lane="4663")

        result = await asyncio.wait_for(scheduler.next_result(), 0.2)
        assert result.position_id == "evm" and result.value == "evm"
        release.set()
        await asyncio.wait_for(scheduler.next_result(), 0.5)
        await scheduler.close()

    asyncio.run(case())


def test_queued_requests_dispatch_by_priority_within_provider_lane():
    async def case():
        scheduler = V6QuoteScheduler(max_workers=1)
        release = threading.Event()
        scheduler.request("running", "running", lambda: (release.wait(1), "running")[1])
        scheduler.request("low", "low", lambda: "low", priority=10)
        scheduler.request("action", "action", lambda: "action", priority=100)
        release.set()

        results = [await asyncio.wait_for(scheduler.next_result(), 0.5) for _ in range(3)]
        assert [result.value for result in results] == ["running", "action", "low"]
        await scheduler.close()

    asyncio.run(case())


def test_evm_lane_reserves_capacity_for_late_action():
    async def case():
        scheduler = V6QuoteScheduler(max_workers=2)
        release = threading.Event()
        scheduler.request("low-1", "low-1", lambda: release.wait(1) or "low-1")
        scheduler.request("low-2", "low-2", lambda: release.wait(1) or "low-2")
        assert scheduler.in_flight_count == 2

        scheduler.request("action", "action", lambda: "action", priority=100)
        result = await asyncio.wait_for(scheduler.next_result(), 0.2)
        assert result.value == "action"
        release.set()
        await asyncio.wait_for(scheduler.next_result(), 0.5)
        await asyncio.wait_for(scheduler.next_result(), 0.5)
        await scheduler.close()

    asyncio.run(case())


def test_market_decision_does_not_wait_for_slow_quote():
    async def case():
        release = threading.Event()
        observations = []
        acc = PoolAccumulator(PoolMeta("4663", POOL, "0xmint", "v2", 0))

        def quote(_position_id, _now_iso):
            release.wait(1)
            return "quote"

        def observe(position_id, facts, quote_result, now_iso, wake_id):
            observations.append((position_id, facts.market_evidence_id, quote_result, wake_id))

        context = FastRuntimeContext(
            accumulators={("4663", POOL): acc},
            positions_for_pool=lambda network, pool: ("p1",),
            all_position_ids=lambda: ("p1",), quote_position=quote,
            observe_position=observe,
            now_iso=lambda: NOW.isoformat(), coalesce_ms=20, max_open=2,
        )
        runtime = V6FastRuntime(context)
        started = time.perf_counter()
        await runtime.inject(_event(1))
        await asyncio.wait_for(runtime.process_one_dirty(), 0.2)
        elapsed_ms = (time.perf_counter() - started) * 1000
        assert elapsed_ms < 100
        assert observations == [("p1", event_identity(_event(1)), None,
                                 event_identity(_event(1)))]
        release.set()
        await runtime.close()

    asyncio.run(case())


def test_market_burst_does_not_queue_quotes_inside_refresh_window():
    async def case():
        release = threading.Event()
        acc = PoolAccumulator(PoolMeta("4663", POOL, "0xmint", "v2", 0))

        def quote(*_args):
            release.wait(1)
            return "quote"

        context = FastRuntimeContext(
            accumulators={("4663", POOL): acc},
            positions_for_pool=lambda *_: ("p1",), all_position_ids=lambda: ("p1",),
            quote_position=quote, observe_position=lambda *_: None,
            now_iso=lambda: NOW.isoformat(), coalesce_ms=1, max_open=1, quote_refresh_s=5,
        )
        runtime = V6FastRuntime(context)
        await runtime.inject(_event(1))
        await runtime.inject(_event(2))
        try:
            assert runtime.quotes.in_flight_count == 1
            assert runtime.quotes._pending == {}
        finally:
            release.set()
            await runtime.close()

    asyncio.run(case())


def test_quote_completion_requeues_position_with_exact_result():
    async def case():
        observations = []
        acc = PoolAccumulator(PoolMeta("4663", POOL, "0xmint", "v2", 0))
        context = FastRuntimeContext(
            accumulators={("4663", POOL): acc},
            positions_for_pool=lambda network, pool: ("p1",),
            all_position_ids=lambda: ("p1",), quote_position=lambda *_: "quote-1",
            observe_position=lambda *args: observations.append(args),
            now_iso=lambda: NOW.isoformat(), coalesce_ms=1, max_open=1,
        )
        runtime = V6FastRuntime(context)
        await runtime.inject(_event(1))
        await runtime.process_one_dirty()
        await asyncio.wait_for(runtime.process_one_quote_result(), 0.2)
        await runtime.process_one_dirty()
        assert observations[-1][2] == "quote-1"
        await runtime.close()

    asyncio.run(case())


def test_action_quote_has_priority_and_is_applied_on_owner_loop():
    async def case():
        release = threading.Event()
        applied = []
        acc = PoolAccumulator(PoolMeta("4663", POOL, "0xmint", "v2", 0))

        def observation_quote(*_):
            release.wait(1)
            return "whole-bag"

        def observe(*_args):
            return ActionQuoteJob("intent-1", lambda: "exact-action")

        context = FastRuntimeContext(
            accumulators={("4663", POOL): acc},
            positions_for_pool=lambda network, pool: ("p1",),
            all_position_ids=lambda: ("p1",), quote_position=observation_quote,
            observe_position=observe,
            apply_action_quote=lambda position_id, token, result, now:
                applied.append((position_id, token, result, now)),
            now_iso=lambda: NOW.isoformat(), coalesce_ms=1, max_open=1,
        )
        runtime = V6FastRuntime(context)
        await runtime.inject(_event(1))
        await runtime.process_one_dirty()
        # A newer low-priority observation request cannot replace the exact active-intent quote.
        runtime._schedule_quote("p1", "market-newer", NOW.isoformat())
        release.set()
        assert await runtime.process_one_quote_result() is True
        assert await asyncio.wait_for(runtime.process_one_quote_result(), 0.5) is True
        assert applied[0][:3] == ("p1", "intent-1", "exact-action")
        await runtime.close()

    asyncio.run(case())


def test_slow_entry_quote_does_not_block_open_position_quote():
    async def case():
        release = threading.Event()
        applied = []
        acc = PoolAccumulator(PoolMeta("4663", POOL, "0xmint", "v2", 0))
        def entry_quote():
            release.wait(1)
            return "entry-fill"

        jobs = [EntryQuoteJob("signal-1", entry_quote)]
        context = FastRuntimeContext(
            accumulators={("4663", POOL): acc},
            positions_for_pool=lambda *_: ("p1",), all_position_ids=lambda: ("p1",),
            quote_position=lambda *_: "position-quote", observe_position=lambda *_: None,
            poll_entry_quote=lambda _now: jobs.pop() if jobs else None,
            apply_entry_quote=lambda job, result, now: applied.append((job.job_id, result)),
            now_iso=lambda: NOW.isoformat(), coalesce_ms=1, max_open=2,
        )
        runtime = V6FastRuntime(context)
        await runtime.poll_entry_once()
        await runtime.inject(_event(1))
        # Position quote completes although the entry quote is still blocked in another worker.
        assert await asyncio.wait_for(runtime.process_one_quote_result(), 0.2) is True
        await runtime.process_one_dirty()
        release.set()
        assert await asyncio.wait_for(runtime.process_one_quote_result(), 0.5) is True
        assert applied == [("signal-1", "entry-fill")]
        await runtime.close()

    asyncio.run(case())


def test_entry_quotes_fill_available_normal_evm_lanes():
    async def case():
        release = threading.Event()
        jobs = [
            EntryQuoteJob("signal-1", lambda: release.wait(1) or "fill-1"),
            EntryQuoteJob("signal-2", lambda: "fill-2"),
        ]
        applied = []
        context = FastRuntimeContext(
            accumulators={}, positions_for_pool=lambda *_: (), all_position_ids=lambda: (),
            quote_position=lambda *_: None, observe_position=lambda *_: None,
            poll_entry_quote=lambda _now: jobs.pop(0) if jobs else None,
            apply_entry_quote=lambda job, result, _now: applied.append((job.job_id, result)),
            now_iso=lambda: NOW.isoformat(), max_open=2,
        )
        runtime = V6FastRuntime(context)
        assert await runtime.poll_entry_once() is True
        assert await runtime.poll_entry_once() is True
        assert runtime.quotes.in_flight_count == 2
        assert await asyncio.wait_for(runtime.process_one_quote_result(), 0.2) is True
        release.set()
        assert await asyncio.wait_for(runtime.process_one_quote_result(), 0.5) is True
        assert {item[0] for item in applied} == {"signal-1", "signal-2"}
        await runtime.close()

    asyncio.run(case())


def test_completed_entry_is_bound_immediately_without_waiting_for_recovery_tick():
    async def case():
        recovered = []
        opened = []
        observed = []
        accumulators = {}
        accumulator = PoolAccumulator(PoolMeta("4663", POOL, "0xmint", "v2", 0))
        jobs = [EntryQuoteJob("signal-1", lambda: "fill")]

        def apply_entry(*_args):
            opened.append("new-position")

        def recover(now):
            recovered.append(now)
            accumulators[("4663", POOL)] = accumulator

        context = FastRuntimeContext(
            accumulators=accumulators,
            positions_for_pool=lambda network, pool: tuple(opened)
                if (network, pool) == ("4663", POOL) else (),
            all_position_ids=lambda: tuple(opened),
            quote_position=lambda *_: "initial-exit-quote",
            observe_position=lambda *args: observed.append(args),
            poll_entry_quote=lambda _now: jobs.pop() if jobs else None,
            apply_entry_quote=apply_entry, recover=recover,
            now_iso=lambda: NOW.isoformat(), coalesce_ms=0, max_open=1,
        )
        runtime = V6FastRuntime(context)
        assert await runtime.poll_entry_once() is True
        assert await runtime.process_one_quote_result() is True
        assert recovered == [NOW.isoformat()]
        assert runtime.quotes.in_flight_count == 1
        assert await runtime.process_one_quote_result() is True
        await runtime.process_one_dirty()
        assert observed[0][0] == "new-position"
        assert observed[0][2] == "initial-exit-quote"
        await runtime.close()

    asyncio.run(case())


def test_followup_quote_is_low_priority_single_flight_and_applied_on_owner_loop():
    async def case():
        applied = []
        jobs = [FollowupQuoteJob("follow-1", lambda: "followup-fill")]
        context = FastRuntimeContext(
            accumulators={}, positions_for_pool=lambda *_: (), all_position_ids=lambda: (),
            quote_position=lambda *_: None, observe_position=lambda *_: None,
            poll_followup_quote=lambda _now: jobs.pop() if jobs else None,
            apply_followup_quote=lambda job, result, now:
                applied.append((job.followup_id, result, now)),
            now_iso=lambda: NOW.isoformat(), max_open=1,
        )
        runtime = V6FastRuntime(context)

        assert await runtime.poll_followup_once() is True
        assert await runtime.poll_followup_once() is False
        request = next(iter(runtime.quotes._inflight.values()))
        assert request.priority == 20
        assert await runtime.process_one_quote_result() is True
        assert applied == [("follow-1", "followup-fill", NOW.isoformat())]
        await runtime.close()

    asyncio.run(case())


def test_unbound_solana_vault_can_bind_position_on_first_decoded_event():
    async def case():
        event = V6MarketEvent(
            network_id="solana", pool="VaultNew", block_height=123, block_hash=None,
            tx_id="sig-1", event_index=0, source_event_at=NOW.isoformat(),
            received_at=NOW.isoformat(), kind="sol_swap",
            payload={"mint": "MintNew", "amount0": "10", "amount1": "-2"},
        )
        accumulators = {}

        def resolve(got):
            acc = PoolAccumulator(PoolMeta(
                "solana", got.pool, "MintNew", "sol", 1, tape_complete=False))
            accumulators[("solana", got.pool)] = acc
            return acc, ("p1",)

        context = FastRuntimeContext(
            accumulators=accumulators, positions_for_pool=lambda *_: (),
            all_position_ids=lambda: ("p1",), quote_position=lambda *_: "q",
            observe_position=lambda *_: None, resolve_event=resolve,
            now_iso=lambda: NOW.isoformat(), coalesce_ms=1, max_open=1,
        )
        runtime = V6FastRuntime(context)
        assert await runtime.inject(event) is True
        assert runtime._position_pool["p1"] == ("solana", "VaultNew")
        await runtime.process_one_dirty()
        await runtime.close()

    asyncio.run(case())


def test_late_event_for_removed_pool_is_dropped_without_killing_runtime():
    async def case():
        event = _event(1)

        def stale_pool(_event):
            raise KeyError("pool no longer belongs to an open V6 position")

        context = FastRuntimeContext(
            accumulators={}, positions_for_pool=lambda *_: (), all_position_ids=lambda: (),
            quote_position=lambda *_: pytest.fail("quoted removed position"),
            observe_position=lambda *_: pytest.fail("observed removed position"),
            resolve_event=stale_pool, now_iso=lambda: NOW.isoformat(), max_open=1,
        )
        runtime = V6FastRuntime(context)

        assert await runtime.inject(event) is False
        assert runtime.ignored_unbound_events == 1
        assert runtime.dirty.pending_count == 0
        await runtime.close()

    asyncio.run(case())


def test_incomplete_pool_persists_an_explicit_insufficient_observation():
    async def case():
        observed = []
        acc = PoolAccumulator(PoolMeta(
            "solana", "VaultA", "MintA", "sol", 1, tape_complete=False))
        context = FastRuntimeContext(
            accumulators={("solana", "VaultA"): acc},
            positions_for_pool=lambda *_: ("p1",), all_position_ids=lambda: ("p1",),
            quote_position=lambda *_: "quote", observe_position=lambda *args: observed.append(args),
            now_iso=lambda: NOW.isoformat(), coalesce_ms=1, max_open=1,
        )
        runtime = V6FastRuntime(context)
        await runtime.inject(V6MarketEvent(
            network_id="solana", pool="VaultA", block_height=123, block_hash=None,
            tx_id="sig-1", event_index=0, source_event_at=NOW.isoformat(),
            received_at=NOW.isoformat(), kind="sol_swap",
            payload={"mint": "MintA", "amount0": "10", "amount1": "-2"},
        ))
        await runtime.process_one_dirty()
        assert len(observed) == 1
        assert observed[0][1].tape_complete is False
        assert runtime._received_at == {}
        await runtime.close()

    asyncio.run(case())


def test_censored_position_pending_dirty_work_is_discarded():
    async def case():
        active = ["p1"]
        accumulators = {("4663", POOL): PoolAccumulator(
            PoolMeta("4663", POOL, "0xmint", "v2", 0))}
        context = FastRuntimeContext(
            accumulators=accumulators,
            positions_for_pool=lambda *_: tuple(active),
            all_position_ids=lambda: tuple(active),
            quote_position=lambda *_: "quote",
            observe_position=lambda *_: pytest.fail("observed censored position"),
            now_iso=lambda: NOW.isoformat(), coalesce_ms=1, max_open=1,
        )
        runtime = V6FastRuntime(context)
        await runtime.inject(_event(1))
        active.clear()
        accumulators.clear()
        await runtime.process_one_dirty()
        assert "p1" not in runtime._position_pool
        await runtime.close()

    asyncio.run(case())


def test_dirty_work_survives_recovery_removing_its_accumulator():
    async def case():
        observed = []
        accumulators = {("4663", POOL): PoolAccumulator(
            PoolMeta("4663", POOL, "0xmint", "v2", 0))}
        context = FastRuntimeContext(
            accumulators=accumulators, positions_for_pool=lambda *_: ("p1",),
            all_position_ids=lambda: ("p1",), quote_position=lambda *_: "quote",
            observe_position=lambda *args: observed.append(args),
            now_iso=lambda: NOW.isoformat(), coalesce_ms=1, max_open=1,
        )
        runtime = V6FastRuntime(context)
        await runtime.inject(_event(1))
        accumulators.clear()

        await runtime.process_one_dirty()

        assert observed == []
        assert runtime.discarded_stale_work == 1
        assert "p1" not in runtime._position_pool
        await runtime.close()

    asyncio.run(case())
