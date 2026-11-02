"""Bounded background EVM quote work for Radar scoring."""

from __future__ import annotations

import queue
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass, replace
from typing import Mapping

from .engine import RemoteRoundTripProbe
from .paper_completion import PaperQuoteCompletion
from .types import VenueId
from sniper.util.clock import real_utc_now_iso


@dataclass(frozen=True)
class _Task:
    key: tuple[str, str]
    state: object
    quote: object


@dataclass(frozen=True)
class _PaperTask:
    key: tuple[str, str]
    request_id: str
    venue_id: VenueId
    method: str
    payload: dict[str, object]
    requested_at: str


class OpportunityEvmProbe:
    """Run EVM quote calls outside the capture loop with fixed queue bounds."""

    def __init__(
        self,
        providers: Mapping[VenueId, object],
        *,
        queue_max: int = 16,
        completion_max: int = 32,
        start_worker: bool = True,
        utc_now_fn=real_utc_now_iso,
    ) -> None:
        if queue_max <= 0 or completion_max <= 0:
            raise ValueError("EVM probe bounds must be positive")
        self.providers = dict(providers)
        self._queue: queue.Queue[_Task] = queue.Queue(maxsize=int(queue_max))
        self._completions: deque[RemoteRoundTripProbe] = deque(
            maxlen=int(completion_max)
        )
        self._pending: set[tuple[str, str]] = set()
        self._paper_results: OrderedDict[str, dict[str, object]] = OrderedDict()
        self._paper_completions: deque[PaperQuoteCompletion] = deque()
        self._completion_max = int(completion_max)
        self._lock = threading.Lock()
        self._closed = False
        self._completed = 0
        self._failed = 0
        self._paper_completion_drops = 0
        self._utc_now_fn = utc_now_fn
        self._by_venue = {
            venue.value: {
                "requested": 0,
                "completed": 0,
                "failed": 0,
                "pending": 0,
                "last_request_at": None,
                "last_success_at": None,
                "last_failure_at": None,
                "last_failure": None,
            }
            for venue in self.providers
        }
        self._thread: threading.Thread | None = None
        if start_worker:
            self._thread = threading.Thread(
                target=self._run,
                name="opportunity-evm-quote",
                daemon=True,
            )
            self._thread.start()

    def _progress(self, venue_id: VenueId) -> dict[str, object]:
        return self._by_venue.setdefault(
            venue_id.value,
            {
                "requested": 0,
                "completed": 0,
                "failed": 0,
                "pending": 0,
                "last_request_at": None,
                "last_success_at": None,
                "last_failure_at": None,
                "last_failure": None,
            },
        )

    def _record_request(self, venue_id: VenueId) -> None:
        progress = self._progress(venue_id)
        progress["requested"] = int(progress["requested"]) + 1
        progress["pending"] = int(progress["pending"]) + 1
        progress["last_request_at"] = self._utc_now_fn()

    def _record_result(
        self,
        venue_id: VenueId,
        *,
        success: bool,
        failure: str | None = None,
    ) -> str:
        progress = self._progress(venue_id)
        progress["pending"] = max(0, int(progress["pending"]) - 1)
        at = self._utc_now_fn()
        if success:
            progress["completed"] = int(progress["completed"]) + 1
            progress["last_success_at"] = at
            self._completed += 1
            return at
        progress["failed"] = int(progress["failed"]) + 1
        progress["last_failure_at"] = at
        progress["last_failure"] = failure or "quote_unavailable"
        self._failed += 1
        return at

    @staticmethod
    def _blocked(state, quote, code: str) -> RemoteRoundTripProbe:
        return RemoteRoundTripProbe(
            False,
            False,
            "evm_quote_queue_v1",
            0,
            code,
            quote_in_atomic=int(getattr(quote, "quote_in_atomic", 0) or 0),
            launch_id=str(getattr(state, "launch_id", "") or ""),
            venue_id=getattr(getattr(state, "venue_id", None), "value", None),
            token_id=str(getattr(state, "token_id", "") or ""),
            state_fingerprint=getattr(quote, "state_fingerprint", None),
            requested_state_fingerprint=getattr(
                quote, "state_fingerprint", None,
            ),
            requested_block_identity=getattr(
                quote, "requested_block_identity", None,
            ),
        )

    def __call__(self, state, quote) -> RemoteRoundTripProbe:
        key = (str(state.launch_id), str(quote.state_fingerprint))
        with self._lock:
            if key in self._pending:
                return self._blocked(state, quote, "EVM_QUOTE_PENDING")
            try:
                self._queue.put_nowait(_Task(key, state, quote))
            except queue.Full:
                return self._blocked(state, quote, "EVM_QUOTE_CAPACITY")
            self._pending.add(key)
            self._record_request(state.venue_id)
        return self._blocked(state, quote, "EVM_QUOTE_PENDING")

    def _paper_submit(
        self,
        *,
        request_id: str,
        venue_id: VenueId,
        method: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        key = ("paper", str(request_id))
        with self._lock:
            completed = self._paper_results.pop(str(request_id), None)
            if completed is not None:
                return completed
            if key in self._pending:
                return {"status": "pending"}
            requested_at = self._utc_now_fn()
            try:
                self._queue.put_nowait(
                    _PaperTask(
                        key,
                        str(request_id),
                        VenueId(venue_id),
                        str(method),
                        dict(payload),
                        requested_at,
                    )
                )
            except queue.Full:
                return {"status": "rejected", "reason": "capacity_expired"}
            self._pending.add(key)
            progress = self._progress(venue_id)
            progress["requested"] = int(progress["requested"]) + 1
            progress["pending"] = int(progress["pending"]) + 1
            progress["last_request_at"] = requested_at
        return {"status": "pending"}

    def paper_entry(
        self,
        *,
        request_id: str,
        state,
        quote_in_atomic: int,
        state_fingerprint: str,
    ) -> dict[str, object]:
        return self._paper_submit(
            request_id=request_id,
            venue_id=state.venue_id,
            method="entry",
            payload={
                "state": state,
                "quote_in_atomic": int(quote_in_atomic),
                "state_fingerprint": str(state_fingerprint),
            },
        )

    def paper_exit(
        self,
        *,
        request_id: str,
        venue_id: VenueId,
        token_id: str,
        quantity_atomic: int,
        token_decimals: int,
        quote_decimals: int,
        route_binding: Mapping[str, object],
    ) -> dict[str, object]:
        return self._paper_submit(
            request_id=request_id,
            venue_id=venue_id,
            method="exit",
            payload={
                "token_id": str(token_id),
                "quantity_atomic": int(quantity_atomic),
                "token_decimals": int(token_decimals),
                "quote_decimals": int(quote_decimals),
                "route_binding": dict(route_binding),
            },
        )

    def paper_request_pending(self, request_id: str) -> bool:
        """Return true only when the probe owns this paper request."""
        with self._lock:
            return ("paper", str(request_id)) in self._pending

    def _execute(self, task: _Task | _PaperTask) -> None:
        venue_id = task.state.venue_id if isinstance(task, _Task) else task.venue_id
        provider = self.providers.get(venue_id)
        if isinstance(task, _PaperTask):
            try:
                if provider is None:
                    raise ValueError("EVM_QUOTE_PROVIDER_MISSING")
                if task.method == "entry":
                    economics = provider.entry(
                        task.payload["state"],
                        task.payload["quote_in_atomic"],
                        task.payload["state_fingerprint"],
                    )
                else:
                    economics = provider.exit(**task.payload)
                result = {"status": "filled", "economics": economics}
            except Exception:
                result = {"status": "rejected", "reason": "quote_unavailable"}
                success = False
                failure = "quote_unavailable"
            else:
                success = True
                failure = None
            with self._lock:
                self._pending.discard(task.key)
                completed_at = self._record_result(
                    venue_id,
                    success=success,
                    failure=failure,
                )
                self._paper_results[task.request_id] = result
                self._paper_results.move_to_end(task.request_id)
                while len(self._paper_results) > self._completion_max:
                    self._paper_results.popitem(last=False)
                if task.method == "exit":
                    if len(self._paper_completions) >= self._completion_max:
                        self._paper_completions.popleft()
                        self._paper_completion_drops += 1
                    self._paper_completions.append(PaperQuoteCompletion(
                        observation_id=task.request_id,
                        venue_id=venue_id.value,
                        status=str(result.get("status") or "rejected"),
                        economics=(
                            dict(result["economics"])
                            if isinstance(result.get("economics"), dict)
                            else None
                        ),
                        failure_reason=(
                            str(result.get("reason") or "quote_unavailable")
                            if result.get("status") != "filled"
                            else None
                        ),
                        requested_at=task.requested_at,
                        completed_at=completed_at,
                    ))
            self._queue.task_done()
            return
        try:
            if provider is None:
                result = self._blocked(
                    task.state, task.quote, "EVM_QUOTE_PROVIDER_MISSING"
                )
            else:
                result = provider.round_trip(task.state, task.quote)
            if not isinstance(result, RemoteRoundTripProbe):
                raise TypeError("EVM provider returned an invalid receipt")
            result = replace(
                result,
                requested_state_fingerprint=getattr(
                    task.quote, "state_fingerprint", None,
                ),
                requested_block_identity=getattr(
                    task.quote, "requested_block_identity", None,
                ),
            )
        except Exception as exc:
            result = self._blocked(task.state, task.quote, type(exc).__name__)
            success = False
            failure = type(exc).__name__
        else:
            success = bool(result.entry_quote_ok and result.acquired_quantity_sell_ok)
            failure = None if success else str(result.blocker_code or "quote_unavailable")
        with self._lock:
            self._pending.discard(task.key)
            self._record_result(
                venue_id,
                success=success,
                failure=failure,
            )
            self._completions.append(result)
        self._queue.task_done()

    def run_one(self) -> bool:
        try:
            task = self._queue.get_nowait()
        except queue.Empty:
            return False
        self._execute(task)
        return True

    def _run(self) -> None:
        while not self._closed:
            try:
                task = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            self._execute(task)

    def drain_completions(
        self, *, limit: int = 32
    ) -> tuple[RemoteRoundTripProbe, ...]:
        items = []
        with self._lock:
            while self._completions and len(items) < int(limit):
                items.append(self._completions.popleft())
        return tuple(items)

    def drain_paper_completions(
        self, *, limit: int = 32
    ) -> tuple[PaperQuoteCompletion, ...]:
        items = []
        with self._lock:
            while self._paper_completions and len(items) < int(limit):
                items.append(self._paper_completions.popleft())
        return tuple(items)

    def metrics(self) -> dict[str, int]:
        with self._lock:
            return {
                "queue_depth": self._queue.qsize(),
                "pending": len(self._pending),
                "completions": len(self._completions),
                "completed": self._completed,
                "failed": self._failed,
                "paper_results": len(self._paper_results),
                "paper_completions": len(self._paper_completions),
                "paper_completion_drops": self._paper_completion_drops,
                "by_venue": {
                    venue: dict(progress)
                    for venue, progress in sorted(self._by_venue.items())
                },
            }

    def close(self) -> None:
        self._closed = True
