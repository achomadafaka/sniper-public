"""Shared priority-aware RPC governor for opportunity capture."""

from __future__ import annotations

import asyncio
from enum import IntEnum
import itertools
import random
import threading
import time
from urllib.error import HTTPError


class RpcPriority(IntEnum):
    CONFIRMATION = 0
    CURRENT = 1
    QUOTE = 2
    RECOVERY = 3


class OpportunityRpcGovernor:
    def __init__(
        self, *, concurrency: int = 2, initial_rps: float = 8,
        min_rps: float = 1, max_rps: float = 8,
    ) -> None:
        self.concurrency = concurrency
        self.current_rps = float(initial_rps)
        self.min_rps = float(min_rps)
        self.max_rps = float(max_rps)
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._sequence = itertools.count()
        self._workers: list[asyncio.Task] = []
        self._pace_lock = threading.Lock()
        self._capacity = threading.BoundedSemaphore(concurrency)
        self._next_allowed = 0.0
        self._active = 0
        self._max_active = 0
        self._rate_limits = 0
        self._last_rate_limit = 0.0
        self._success_streak = 0
        self._closed = False

    def _ensure_workers(self) -> None:
        if not self._workers:
            self._workers = [asyncio.create_task(self._worker()) for _ in range(self.concurrency)]

    async def call(self, priority: RpcPriority, fn, *args, **kwargs):
        if self._closed:
            raise RuntimeError("RPC governor is closed")
        self._ensure_workers()
        future = asyncio.get_running_loop().create_future()
        await self._queue.put(
            (int(priority), next(self._sequence), 0, fn, args, kwargs, future)
        )
        return await future

    def _pace(self) -> None:
        with self._pace_lock:
            now = time.monotonic()
            delay = max(0.0, self._next_allowed - now)
            self._next_allowed = max(now, self._next_allowed) + (1.0 / self.current_rps)
        if delay:
            time.sleep(delay)

    @staticmethod
    def _is_rate_limit(exc: BaseException) -> bool:
        return isinstance(exc, HTTPError) and exc.code == 429

    @staticmethod
    def _retry_after(exc: HTTPError) -> float:
        try:
            value = exc.headers.get("Retry-After") if exc.headers else None
            return min(30.0, max(0.0, float(value))) if value is not None else random.uniform(0.5, 8.0)
        except (TypeError, ValueError):
            return random.uniform(0.5, 8.0)

    async def _worker(self) -> None:
        while True:
            priority, sequence, attempts, fn, args, kwargs, future = await self._queue.get()
            try:
                if future.cancelled():
                    continue
                result = await asyncio.to_thread(
                    self._execute_sync, fn, args, kwargs, attempts
                )
                if not future.done():
                    future.set_result(result)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                if not future.done():
                    future.set_exception(exc)
            finally:
                self._queue.task_done()

    def _execute_sync(self, fn, args, kwargs, attempts: int = 0):
        while True:
            self._pace()
            with self._capacity:
                self._active += 1
                self._max_active = max(self._max_active, self._active)
                try:
                    result = fn(*args, **kwargs)
                except BaseException as exc:
                    if self._is_rate_limit(exc) and attempts < 3:
                        self._rate_limits += 1
                        self._last_rate_limit = time.monotonic()
                        self._success_streak = 0
                        self.current_rps = max(self.min_rps, self.current_rps / 2.0)
                        attempts += 1
                        time.sleep(self._retry_after(exc))
                        continue
                    raise
                finally:
                    self._active -= 1
            self._success_streak += 1
            if self._success_streak >= 30:
                self.current_rps = min(self.max_rps, self.current_rps + 0.5)
                self._success_streak = 0
            return result

    def call_sync(self, priority: RpcPriority, fn, *args, **kwargs):
        """Use the same limiter from synchronous quote probes."""
        del priority
        if self._closed:
            raise RuntimeError("RPC governor is closed")
        return self._execute_sync(fn, args, kwargs)

    def metrics(self) -> dict[str, int | float]:
        return {
            "current_rps": round(self.current_rps, 3),
            "configured_concurrency": self.concurrency,
            "active_calls": self._active,
            "max_observed_concurrency": self._max_active,
            "queue_depth": self._queue.qsize(),
            "rate_limit_events": self._rate_limits,
            "throttled": bool(
                self._last_rate_limit and time.monotonic() - self._last_rate_limit < 30.0
            ),
        }

    async def close(self) -> None:
        self._closed = True
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
