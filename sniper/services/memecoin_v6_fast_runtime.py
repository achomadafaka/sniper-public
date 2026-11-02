"""Subsecond event orchestration for the isolated V6 live-paper book.

Chain events update one shared accumulator per pool. A bounded dirty map coalesces bursts, quote
calls run outside the SQLite owner loop, and each ready position is observed independently. This
module owns no funded execution surface and performs no database writes itself.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Mapping

from sniper.services.memecoin_v6_events import V6MarketEvent, event_identity
from sniper.services.memecoin_v6_incremental_state import PoolAccumulator


logger = logging.getLogger("memecoin")


@dataclass(frozen=True)
class DirtyWork:
    position_id: str
    latest_evidence_id: str


class DirtyPositionQueue:
    """One coalesced deadline and newest evidence identity per position."""

    def __init__(self, *, coalesce_ms: int = 50, clock: Callable[[], float] | None = None):
        if isinstance(coalesce_ms, bool) or not isinstance(coalesce_ms, int) or coalesce_ms < 0:
            raise ValueError("coalesce_ms must be a non-negative integer")
        self._delay_s = coalesce_ms / 1000.0
        self._clock = clock
        self._pending: dict[str, tuple[float, str]] = {}
        self._changed = asyncio.Event()

    def _now(self) -> float:
        return self._clock() if self._clock is not None else asyncio.get_running_loop().time()

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def mark(self, position_id: str, evidence_id: str) -> None:
        if not position_id or not evidence_id:
            raise ValueError("dirty work identities must not be empty")
        current = self._pending.get(position_id)
        deadline = current[0] if current is not None else self._now() + self._delay_s
        self._pending[position_id] = (deadline, evidence_id)
        self._changed.set()

    async def next_ready(self) -> DirtyWork:
        while True:
            if not self._pending:
                self._changed.clear()
                await self._changed.wait()
                continue
            position_id, (deadline, evidence_id) = min(
                self._pending.items(), key=lambda item: (item[1][0], item[0]))
            delay = deadline - self._now()
            if delay <= 0:
                del self._pending[position_id]
                return DirtyWork(position_id, evidence_id)
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=delay)
            except TimeoutError:
                pass


@dataclass(frozen=True)
class QuoteResult:
    position_id: str
    request_id: str
    value: object | None
    error: BaseException | None
    duration_us: int


@dataclass(frozen=True)
class _QuoteRequest:
    position_id: str
    request_id: str
    call: Callable[[], object]
    priority: int
    lane: str
    order: int


@dataclass(frozen=True)
class ActionQuoteJob:
    intent_token: str
    call: Callable[[], object]

    def __post_init__(self):
        if not self.intent_token or not callable(self.call):
            raise ValueError("action quote job is invalid")


@dataclass(frozen=True)
class EntryQuoteJob:
    job_id: str
    call: Callable[[], object]
    payload: object | None = None
    network_id: str = "4663"

    def __post_init__(self):
        if not self.job_id or not callable(self.call):
            raise ValueError("entry quote job is invalid")


@dataclass(frozen=True)
class FollowupQuoteJob:
    followup_id: str
    call: Callable[[], object]
    network_id: str = "4663"

    def __post_init__(self):
        if not self.followup_id or not callable(self.call):
            raise ValueError("followup quote job is invalid")


class V6QuoteScheduler:
    """Bounded quote executor with one in-flight and one newest pending request per position."""

    def __init__(self, *, max_workers: int):
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
            raise ValueError("max_workers must be a positive integer")
        self._executors = {
            "evm": ThreadPoolExecutor(
                max_workers=max_workers, thread_name_prefix="v6-quote-evm"),
            "evm_action": ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="v6-quote-evm-action"),
            "solana": ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="v6-quote-solana"),
        }
        self._lane_capacity = {"evm": max_workers, "evm_action": 1, "solana": 1}
        self._inflight: dict[str, _QuoteRequest] = {}
        self._pending: dict[str, _QuoteRequest] = {}
        self._tasks: set[asyncio.Task] = set()
        self._results: asyncio.Queue[QuoteResult] = asyncio.Queue()
        self._closed = False
        self._order = 0

    @staticmethod
    def _lane(value: str) -> str:
        normalized = str(value).lower()
        if normalized == "solana":
            return "solana"
        if normalized == "evm_action":
            return "evm_action"
        return "evm"

    @property
    def in_flight_count(self) -> int:
        return len(self._inflight)

    def request(self, position_id: str, request_id: str, call: Callable[[], object], *,
                priority: int = 10, lane: str = "4663") -> None:
        if self._closed:
            raise RuntimeError("quote scheduler is closed")
        if not position_id or not request_id or not callable(call):
            raise ValueError("quote request is invalid")
        if isinstance(priority, bool) or not isinstance(priority, int) or priority < 1:
            raise ValueError("quote priority must be a positive integer")
        self._order += 1
        normalized_lane = self._lane(lane)
        if normalized_lane == "evm" and priority >= 100:
            normalized_lane = "evm_action"
        request = _QuoteRequest(
            position_id, request_id, call, priority, normalized_lane, self._order)
        if position_id in self._inflight:
            pending = self._pending.get(position_id)
            if pending is None or request.priority >= pending.priority:
                self._pending[position_id] = request
            return
        pending = self._pending.get(position_id)
        if pending is None or request.priority >= pending.priority:
            self._pending[position_id] = request
        self._dispatch(request.lane)

    def has_work_above(self, priority: int, *, lane: str) -> bool:
        normalized = self._lane(lane)
        return any(
            (request.lane == normalized
             or normalized == "evm" and request.lane == "evm_action")
            and request.priority > priority
            for request in (*self._inflight.values(), *self._pending.values()))

    def _dispatch(self, lane: str) -> None:
        active = sum(request.lane == lane for request in self._inflight.values())
        while active < self._lane_capacity[lane]:
            candidates = [request for request in self._pending.values()
                          if request.lane == lane and request.position_id not in self._inflight
                          ]
            if not candidates:
                return
            request = min(candidates, key=lambda item: (-item.priority, item.order))
            self._pending.pop(request.position_id, None)
            self._launch(request)
            active += 1

    def _launch(self, request: _QuoteRequest) -> None:
        self._inflight[request.position_id] = request
        task = asyncio.create_task(self._run(request))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, request: _QuoteRequest) -> None:
        loop = asyncio.get_running_loop()
        value, error = None, None
        started = time.perf_counter_ns()
        try:
            value = await loop.run_in_executor(self._executors[request.lane], request.call)
        except BaseException as exc:  # preserve typed provider errors for the observation adapter
            error = exc
        duration_us = max(0, (time.perf_counter_ns() - started) // 1000)
        await self._results.put(QuoteResult(
            request.position_id, request.request_id, value, error, duration_us))
        self._inflight.pop(request.position_id, None)
        if not self._closed:
            for lane in self._lane_capacity:
                self._dispatch(lane)

    async def next_result(self) -> QuoteResult:
        return await self._results.get()

    async def close(self) -> None:
        self._closed = True
        self._pending.clear()
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        for executor in self._executors.values():
            executor.shutdown(wait=True, cancel_futures=True)


@dataclass(frozen=True)
class FastRuntimeContext:
    accumulators: Mapping[tuple[str, str], PoolAccumulator]
    positions_for_pool: Callable[[str, str], tuple[str, ...] | list[str]]
    all_position_ids: Callable[[], tuple[str, ...] | list[str]]
    quote_position: Callable[[str, str], object]
    observe_position: Callable[[str, object, object | None, str, str], object]
    now_iso: Callable[[], str]
    coalesce_ms: int = 50
    max_open: int = 4
    heartbeat_s: float = 1.0
    quote_refresh_s: float = 5.0
    recovery_s: float = 5.0
    event_streams: tuple = ()
    heartbeat: Callable[[str], object] | None = None
    recover: Callable[[str], object] | None = None
    apply_action_quote: Callable[[str, str, object, str], object] | None = None
    prepare_quote: Callable[[str, str], Callable[[], object]] | None = None
    record_feed_batch: Callable[[object, str], object] | None = None
    record_latency: Callable[[str, int, str, str, str, str, str], object] | None = None
    poll_entry_quote: Callable[[str], EntryQuoteJob | None] | None = None
    apply_entry_quote: Callable[[EntryQuoteJob, object, str], object] | None = None
    poll_followup_quote: Callable[[str], FollowupQuoteJob | None] | None = None
    apply_followup_quote: Callable[[FollowupQuoteJob, object, str], object] | None = None
    resolve_event: Callable[[V6MarketEvent], tuple[PoolAccumulator, tuple[str, ...]]] | None = None
    evm_pool_start_heights: dict[str, int] = field(default_factory=dict)

    def __post_init__(self):
        if isinstance(self.max_open, bool) or not isinstance(self.max_open, int) or self.max_open < 1:
            raise ValueError("max_open must be a positive integer")
        for name in ("heartbeat_s", "quote_refresh_s", "recovery_s"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")


class V6FastRuntime:
    """Single-event-loop owner for accumulators, observations, and callback-driven DB writes."""

    def __init__(self, context: FastRuntimeContext):
        self.context = context
        self.dirty = DirtyPositionQueue(coalesce_ms=context.coalesce_ms)
        self.quotes = V6QuoteScheduler(max_workers=min(4, context.max_open))
        self._position_pool: dict[str, tuple[str, str]] = {}
        self._quote_cache: dict[str, object] = {}
        self._latest_quote_request: dict[str, str] = {}
        self._last_quote_request_at: dict[str, float] = {}
        self._action_requests: set[tuple[str, str]] = set()
        self._entry_requests: dict[tuple[str, str], EntryQuoteJob] = {}
        self._followup_requests: dict[tuple[str, str], FollowupQuoteJob] = {}
        self._received_at: dict[str, str] = {}
        self.ignored_unbound_events = 0
        self._warned_unbound_pools: set[tuple[str, str]] = set()
        self.discarded_stale_work = 0
        self._warned_stale_positions: set[str] = set()
        self._closed = False

    def _pool_key(self, network_id: str, pool: str) -> tuple[str, str]:
        return network_id, pool.lower() if network_id == "4663" else pool

    def _accumulator(self, network_id: str, pool: str) -> PoolAccumulator:
        key = self._pool_key(network_id, pool)
        accumulator = self.context.accumulators.get(key)
        if accumulator is None:
            # Accept exact caller keys for case-sensitive Solana and older EVM fixtures.
            accumulator = self.context.accumulators.get((network_id, pool))
        if accumulator is None:
            raise KeyError(f"no V6 accumulator for {network_id}:{pool}")
        return accumulator

    def _schedule_quote(self, position_id: str, request_id: str, now_iso: str) -> None:
        requested_at = time.monotonic()
        previous = self._last_quote_request_at.get(position_id)
        if previous is not None and requested_at - previous < self.context.quote_refresh_s:
            return
        self._last_quote_request_at[position_id] = requested_at
        self._latest_quote_request[position_id] = request_id
        call = (self.context.prepare_quote(position_id, now_iso)
                if self.context.prepare_quote is not None else
                functools.partial(self.context.quote_position, position_id, now_iso))
        if not callable(call):
            raise ValueError("prepare_quote must return a callable")
        lane = self._position_pool.get(position_id, ("4663", ""))[0]
        self.quotes.request(position_id, request_id, call, priority=10, lane=lane)

    def _schedule_action_quote(self, position_id: str, job: ActionQuoteJob) -> None:
        request_id = f"intent:{job.intent_token}"
        self._action_requests.add((position_id, request_id))
        lane = self._position_pool.get(position_id, ("4663", ""))[0]
        self.quotes.request(position_id, request_id, job.call, priority=100, lane=lane)

    def _refresh_position_bindings(self) -> None:
        for network_id, pool in tuple(self.context.accumulators):
            key = self._pool_key(network_id, pool)
            for position_id in tuple(self.context.positions_for_pool(network_id, pool)):
                self._position_pool[position_id] = key

    async def poll_entry_once(self) -> bool:
        if (self.context.poll_entry_quote is None
                or len(self._entry_requests) >= self.context.max_open):
            return False
        job = self.context.poll_entry_quote(self.context.now_iso())
        if job is None:
            return False
        owner_id = f"entry:{job.job_id}"
        request_id = f"entry:{job.job_id}"
        self._entry_requests[(owner_id, request_id)] = job
        self.quotes.request(
            owner_id, request_id, job.call, priority=50, lane=job.network_id)
        return True

    async def poll_entries(self) -> int:
        opened = 0
        while len(self._entry_requests) < self.context.max_open:
            if not await self.poll_entry_once():
                break
            opened += 1
        return opened

    async def poll_followup_once(self) -> bool:
        if self.context.poll_followup_quote is None or self._followup_requests:
            return False
        job = self.context.poll_followup_quote(self.context.now_iso())
        if job is None:
            return False
        if self.quotes.has_work_above(20, lane=job.network_id):
            return False
        owner_id = f"followup:{job.followup_id}"
        request_id = owner_id
        self._followup_requests[(owner_id, request_id)] = job
        self.quotes.request(
            owner_id, request_id, job.call, priority=20, lane=job.network_id)
        return True

    async def inject(self, event: V6MarketEvent) -> bool:
        resolved_positions = None
        try:
            accumulator = self._accumulator(event.network_id, event.pool)
        except KeyError:
            if self.context.resolve_event is None:
                raise
            try:
                accumulator, resolved_positions = self.context.resolve_event(event)
            except KeyError as exc:
                self.ignored_unbound_events += 1
                key = self._pool_key(event.network_id, event.pool)
                if key not in self._warned_unbound_pools:
                    self._warned_unbound_pools.add(key)
                    logger.warning(
                        "V6 PAPER — NO FUNDS ignored late event for unbound pool %s:%s: %s",
                        event.network_id, event.pool, exc,
                    )
                return False
        if not accumulator.apply(event):
            return False
        evidence_id = event_identity(event)
        key = self._pool_key(event.network_id, event.pool)
        now_iso = self.context.now_iso()
        position_ids = (resolved_positions if resolved_positions is not None else
                        tuple(self.context.positions_for_pool(event.network_id, event.pool)))
        for position_id in position_ids:
            self._position_pool[position_id] = key
            # One coalesced dirty item exists per position, so retain only its newest receive time.
            self._received_at[position_id] = event.received_at
            self.dirty.mark(position_id, evidence_id)
            self._schedule_quote(position_id, evidence_id, now_iso)
        return True

    async def process_one_dirty(self) -> DirtyWork:
        work = await self.dirty.next_ready()
        if work.position_id not in set(self.context.all_position_ids()):
            self._position_pool.pop(work.position_id, None)
            self._quote_cache.pop(work.position_id, None)
            self._latest_quote_request.pop(work.position_id, None)
            self._last_quote_request_at.pop(work.position_id, None)
            self._received_at.pop(work.position_id, None)
            return work
        key = self._position_pool.get(work.position_id)
        if key is None:
            raise KeyError(f"position {work.position_id} has no pool binding")
        try:
            accumulator = self._accumulator(*key)
        except KeyError as exc:
            # Recovery can remove a closed/rebound pool after its event was queued. The next
            # recovery/heartbeat rebuilds the binding; observing stale facts would be worse than
            # dropping this superseded work item.
            self.discarded_stale_work += 1
            self._position_pool.pop(work.position_id, None)
            self._quote_cache.pop(work.position_id, None)
            self._latest_quote_request.pop(work.position_id, None)
            self._last_quote_request_at.pop(work.position_id, None)
            self._received_at.pop(work.position_id, None)
            if work.position_id not in self._warned_stale_positions:
                self._warned_stale_positions.add(work.position_id)
                logger.warning(
                    "V6 PAPER — NO FUNDS discarded stale work for %s: %s",
                    work.position_id, exc,
                )
            return work
        now_iso = self.context.now_iso()
        facts = accumulator.snapshot(now_iso)
        if self.context.record_latency is not None:
            from datetime import datetime
            received_at = self._received_at.pop(work.position_id, None)
            if received_at is not None:
                received = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
                observed = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
                duration_us = max(0, int((observed - received).total_seconds() * 1_000_000))
                self.context.record_latency(
                    "receive_to_observation", duration_us, facts.network_id, facts.pool,
                    "local", "event", now_iso)
        else:
            self._received_at.pop(work.position_id, None)
        started = time.perf_counter_ns()
        result = self.context.observe_position(
            work.position_id, facts, self._quote_cache.get(work.position_id),
            now_iso, work.latest_evidence_id)
        if self.context.record_latency is not None:
            self.context.record_latency(
                "observation_to_decision", max(0, (time.perf_counter_ns() - started) // 1000),
                facts.network_id, facts.pool, "local", "event", now_iso)
        if isinstance(result, ActionQuoteJob):
            self._schedule_action_quote(work.position_id, result)
        return work

    async def process_one_quote_result(self) -> bool:
        result = await self.quotes.next_result()
        entry_key = (result.position_id, result.request_id)
        followup_job = self._followup_requests.pop(entry_key, None)
        if followup_job is not None:
            if self.context.apply_followup_quote is None:
                raise RuntimeError("followup quote completed without an apply callback")
            value = result.value if result.error is None else result.error
            self.context.apply_followup_quote(
                followup_job, value, self.context.now_iso())
            await self.poll_followup_once()
            return True
        entry_job = self._entry_requests.pop(entry_key, None)
        if entry_job is not None:
            if self.context.apply_entry_quote is None:
                raise RuntimeError("entry quote completed without an apply callback")
            positions_before = set(self.context.all_position_ids())
            value = result.value if result.error is None else result.error
            now_iso = self.context.now_iso()
            self.context.apply_entry_quote(entry_job, value, now_iso)
            if self.context.recover is not None:
                self.context.recover(now_iso)
            self._refresh_position_bindings()
            for position_id in set(self.context.all_position_ids()) - positions_before:
                if position_id in self._position_pool:
                    self._schedule_quote(position_id, f"entry:{entry_job.job_id}", now_iso)
            await self.poll_entries()
            await self.poll_followup_once()
            return True
        key = self._position_pool.get(result.position_id)
        if key is not None and self.context.record_latency is not None:
            self.context.record_latency(
                "quote", result.duration_us, key[0], key[1], "quote_adapter", "async",
                self.context.now_iso())
        action_key = (result.position_id, result.request_id)
        if action_key in self._action_requests:
            self._action_requests.discard(action_key)
            if self.context.apply_action_quote is None:
                raise RuntimeError("action quote completed without an apply callback")
            token = result.request_id.removeprefix("intent:")
            value = result.value if result.error is None else result.error
            self.context.apply_action_quote(
                result.position_id, token, value, self.context.now_iso())
            return True
        if self._latest_quote_request.get(result.position_id) != result.request_id:
            return False
        self._quote_cache[result.position_id] = (
            result.value if result.error is None else result.error)
        self.dirty.mark(result.position_id, f"quote:{result.request_id}")
        return True

    async def _dirty_loop(self, stop_event) -> None:
        while not stop_event.is_set():
            await self.process_one_dirty()

    async def _quote_loop(self, stop_event) -> None:
        while not stop_event.is_set():
            await self.process_one_quote_result()

    async def _heartbeat_loop(self, stop_event) -> None:
        next_quote_refresh = 0.0
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.context.heartbeat_s)
                continue
            except TimeoutError:
                pass
            now_iso = self.context.now_iso()
            if self.context.heartbeat is not None:
                self.context.heartbeat(now_iso)
            await self.poll_entries()
            self._refresh_position_bindings()
            loop_now = asyncio.get_running_loop().time()
            if loop_now < next_quote_refresh:
                continue
            next_quote_refresh = loop_now + self.context.quote_refresh_s
            for position_id in tuple(self.context.all_position_ids()):
                if position_id not in self._position_pool:
                    continue
                request_id = f"heartbeat:{now_iso}"
                self._schedule_quote(position_id, request_id, now_iso)

    async def _recovery_loop(self, stop_event) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.context.recovery_s)
                continue
            except TimeoutError:
                pass
            if self.context.recover is not None:
                self.context.recover(self.context.now_iso())
            await self.poll_entries()
            await self.poll_followup_once()

    async def _feed_loop(self, stream, stop_event) -> None:
        async for batch in stream:
            if stop_event.is_set():
                return
            for event in batch.events:
                await self.inject(event)
            if self.context.record_feed_batch is not None:
                self.context.record_feed_batch(batch, self.context.now_iso())

    async def run(self, stop_event) -> None:
        await self.poll_entries()
        await self.poll_followup_once()
        tasks = [
            asyncio.create_task(self._dirty_loop(stop_event)),
            asyncio.create_task(self._quote_loop(stop_event)),
            asyncio.create_task(self._heartbeat_loop(stop_event)),
            asyncio.create_task(self._recovery_loop(stop_event)),
            *(asyncio.create_task(self._feed_loop(stream, stop_event))
              for stream in self.context.event_streams),
        ]
        stop_wait = asyncio.create_task(stop_event.wait())
        try:
            done, _pending = await asyncio.wait(
                (*tasks, stop_wait), return_when=asyncio.FIRST_COMPLETED)
            if stop_wait not in done:
                completed = next(iter(done))
                error = completed.exception()
                if error is not None:
                    raise error
                raise RuntimeError("V6 fast runtime task exited unexpectedly")
        finally:
            stop_wait.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(stop_wait, *tasks, return_exceptions=True)
            await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.quotes.close()


async def run_fast_runtime(context: FastRuntimeContext, stop_event) -> None:
    await V6FastRuntime(context).run(stop_event)
