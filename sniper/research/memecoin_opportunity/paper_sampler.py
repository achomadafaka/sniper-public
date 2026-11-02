"""Bind asynchronous paper quote results and measure the sampler."""

from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping

from sniper.util.clock import real_utc_now_iso

from .identity import require_utc_iso
from .paper_completion import PaperQuoteCompletion
from .projection import upsert_runtime_status


# A window SEALS permanently: `persistence_due` returns False once `_sealed` is set, and
# `restore` carries that flag back. Nothing reopens it. So a second measurement needs a
# new IDENTITY, and `restore` provides exactly that -- a stored `detail_version` that
# does not match returns a fresh sampler on a fresh 24-hour window.
#
# v1 measured the load BEFORE `radar-realistic-v3`. Under v3 the v2 lifecycle keeps
# quoting a decided position all the way to the 90 s terminal age instead of stopping at
# its own outcome, so v1's round trips are not production-shaped for the v3 baseline
# rule. The real v1 row never sealed either -- the a893d40e cutover cut it off mid-window
# at 176 requests. Both facts point the same way: it cannot set MAX_BASELINE_AGE_MS.
#
# v1 keeps its own source key and its row stays readable as the before-and-after record.
# Nothing writes that key again, so `projection._RETIRED_RUNTIME_SOURCES` drops it from
# the health snapshot; otherwise its heartbeat ages without bound and grades degraded
# forever.
SOURCE_KEY = "opportunity:paper_sampler_v2"
DETAIL_VERSION = "paper-sampler-v2"
WINDOW_SECONDS = 86_400
PERSIST_SECONDS = 60
ROUND_TRIP_BOUNDS_MS = (1_000, 2_000, 5_000, 10_000, 15_000, 30_000, 60_000)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(require_utc_iso(value).replace("Z", "+00:00"))


class _Histogram:
    def __init__(self, stored: Mapping[str, object] | None = None) -> None:
        if not isinstance(stored, Mapping):
            stored = {}
        stored_buckets = stored.get("buckets")
        if not isinstance(stored_buckets, Mapping):
            stored_buckets = {}
        self.count = int(stored.get("count", 0) or 0)
        self.total = int(stored.get("sum", 0) or 0)
        self.maximum = int(stored.get("max", 0) or 0)
        self.buckets = {
            str(bound): int(stored_buckets.get(str(bound), 0) or 0)
            for bound in ROUND_TRIP_BOUNDS_MS
        }
        self.buckets["overflow"] = int(stored_buckets.get("overflow", 0) or 0)

    def observe(self, value: int) -> None:
        value = max(0, int(value))
        self.count += 1
        self.total += value
        self.maximum = max(self.maximum, value)
        for bound in ROUND_TRIP_BOUNDS_MS:
            if value <= bound:
                self.buckets[str(bound)] += 1
                return
        self.buckets["overflow"] += 1

    def snapshot(self) -> dict[str, object]:
        return {
            "count": self.count,
            "sum": self.total,
            "max": self.maximum,
            "buckets": dict(self.buckets),
        }


@dataclass(frozen=True)
class BoundPaperQuote:
    """A quote completion and the immutable request that owns it."""

    request: Mapping[str, object]
    completion: PaperQuoteCompletion


class PaperSampler:
    """Keep bounded request context and fixed aggregate metrics."""

    def __init__(
        self,
        *,
        context_max: int = 1_024,
        now_iso: str | None = None,
        window_started_at: str | None = None,
    ) -> None:
        if context_max <= 0:
            raise ValueError("paper sampler context bound must be positive")
        started_at = require_utc_iso(
            window_started_at or now_iso or real_utc_now_iso()
        )
        self.context_max = int(context_max)
        self.window_started_at = started_at
        self.window_ends_at = (_parse(started_at) + timedelta(
            seconds=WINDOW_SECONDS
        )).isoformat()
        self._contexts: OrderedDict[str, dict[str, object]] = OrderedDict()
        self._retries: OrderedDict[str, BoundPaperQuote] = OrderedDict()
        self._position_active: dict[str, int] = {}
        self._by_venue: dict[str, dict[str, object]] = {}
        self.requests = 0
        self.completions = 0
        self.filled = 0
        self.rejected = 0
        self.context_evictions = 0
        self.orphan_completions = 0
        self.retry_drops = 0
        self.max_inflight_buckets = 0
        self.max_position_inflight_buckets = 0
        self.worker_epochs = 1
        self._round_trip = _Histogram()
        self._probe_metrics: dict[str, object] = {}
        self._last_persisted_at: str | None = None
        self._sealed = False

    def _venue(self, venue: str) -> dict[str, object]:
        return self._by_venue.setdefault(venue, {
            "requests": 0,
            "completions": 0,
            "filled": 0,
            "rejected": 0,
            "active_contexts": 0,
            "max_inflight_buckets": 0,
            "round_trip": _Histogram(),
        })

    def _remove_active(self, request: Mapping[str, object]) -> None:
        venue = str(request["venue_id"])
        venue_data = self._venue(venue)
        venue_data["active_contexts"] = max(
            0, int(venue_data["active_contexts"]) - 1
        )
        entry_id = str(request["request_id"])
        remaining = max(0, self._position_active.get(entry_id, 0) - 1)
        if remaining:
            self._position_active[entry_id] = remaining
        else:
            self._position_active.pop(entry_id, None)

    def track(
        self,
        request: Mapping[str, object],
        *,
        requested_at: str,
    ) -> bool:
        observation_id = str(request.get("observation_id") or "")
        entry_id = str(request.get("request_id") or "")
        venue = str(request.get("venue_id") or "")
        if not observation_id or not entry_id or not venue:
            raise ValueError("paper sampler request identity is invalid")
        if observation_id in self._contexts:
            return False
        if len(self._contexts) >= self.context_max:
            _, evicted = self._contexts.popitem(last=False)
            self._remove_active(evicted)
            self.context_evictions += 1
        self._contexts[observation_id] = {
            **dict(request),
            "requested_at": require_utc_iso(requested_at),
        }
        self.requests += 1
        venue_data = self._venue(venue)
        venue_data["requests"] = int(venue_data["requests"]) + 1
        venue_data["active_contexts"] = int(venue_data["active_contexts"]) + 1
        venue_data["max_inflight_buckets"] = max(
            int(venue_data["max_inflight_buckets"]),
            int(venue_data["active_contexts"]),
        )
        self._position_active[entry_id] = self._position_active.get(entry_id, 0) + 1
        self.max_inflight_buckets = max(
            self.max_inflight_buckets, len(self._contexts)
        )
        self.max_position_inflight_buckets = max(
            self.max_position_inflight_buckets,
            self._position_active[entry_id],
        )
        return True

    def consume(self, completion: PaperQuoteCompletion) -> BoundPaperQuote | None:
        request = self._contexts.pop(completion.observation_id, None)
        if request is None:
            self.orphan_completions += 1
            return None
        self._remove_active(request)
        round_trip_ms = max(0, int(
            (_parse(completion.completed_at) - _parse(completion.requested_at))
            .total_seconds() * 1_000
        ))
        self.completions += 1
        self._round_trip.observe(round_trip_ms)
        venue_data = self._venue(completion.venue_id)
        venue_data["completions"] = int(venue_data["completions"]) + 1
        venue_data["round_trip"].observe(round_trip_ms)
        if completion.status == "filled":
            self.filled += 1
            venue_data["filled"] = int(venue_data["filled"]) + 1
        else:
            self.rejected += 1
            venue_data["rejected"] = int(venue_data["rejected"]) + 1
        return BoundPaperQuote(request=request, completion=completion)

    @property
    def active_count(self) -> int:
        return len(self._contexts)

    @property
    def retry_count(self) -> int:
        return len(self._retries)

    def retry(self, bound: BoundPaperQuote) -> None:
        observation_id = bound.completion.observation_id
        if observation_id in self._retries:
            return
        if len(self._retries) >= self.context_max:
            self._retries.popitem(last=False)
            self.retry_drops += 1
        self._retries[observation_id] = bound

    def take_retries(self) -> tuple[BoundPaperQuote, ...]:
        values = tuple(self._retries.values())
        self._retries.clear()
        return values

    def snapshot(self, *, now_iso: str) -> dict[str, object]:
        now = require_utc_iso(now_iso)
        return {
            "detail_version": DETAIL_VERSION,
            "window_started_at": self.window_started_at,
            "window_ends_at": self.window_ends_at,
            "window_complete": _parse(now) >= _parse(self.window_ends_at),
            "last_sample_at": now,
            "worker_epochs": self.worker_epochs,
            "requests": self.requests,
            "completions": self.completions,
            "filled": self.filled,
            "rejected": self.rejected,
            "active_contexts": self.active_count,
            "max_inflight_buckets": self.max_inflight_buckets,
            "max_position_inflight_buckets": self.max_position_inflight_buckets,
            "context_evictions": self.context_evictions,
            "orphan_completions": self.orphan_completions,
            "retry_count": self.retry_count,
            "retry_drops": self.retry_drops,
            "round_trip_ms": self._round_trip.snapshot(),
            "by_venue": {
                venue: {
                    **{
                        key: value
                        for key, value in data.items()
                        if key != "round_trip"
                    },
                    "round_trip_ms": data["round_trip"].snapshot(),
                }
                for venue, data in sorted(self._by_venue.items())
            },
            "probe_metrics": dict(self._probe_metrics),
        }

    def maybe_persist(
        self,
        conn,
        *,
        now_iso: str,
        probe_metrics: Mapping[str, object],
    ) -> bool:
        now = require_utc_iso(now_iso)
        if not self.persistence_due(now_iso=now):
            return False
        self._probe_metrics = dict(probe_metrics)
        detail = self.snapshot(now_iso=now)
        upsert_runtime_status(
            conn,
            source_key=SOURCE_KEY,
            status="healthy",
            blocker_code=None,
            heartbeat_at=now,
            queue_depth=self.active_count,
            dropped_events=self.context_evictions + self.orphan_completions,
            detail=detail,
        )
        self._last_persisted_at = now
        self._sealed = bool(detail["window_complete"])
        return True

    def persistence_due(self, *, now_iso: str) -> bool:
        now = require_utc_iso(now_iso)
        if self._sealed:
            return False
        return (
            self._last_persisted_at is None
            or (_parse(now) - _parse(self._last_persisted_at)).total_seconds()
            >= PERSIST_SECONDS
        )

    @classmethod
    def restore(cls, conn, *, now_iso: str) -> "PaperSampler":
        row = conn.execute(
            "SELECT heartbeat_at,detail_json FROM op_runtime_status WHERE source_key=?",
            (SOURCE_KEY,),
        ).fetchone()
        if row is None:
            return cls(now_iso=now_iso)
        try:
            detail = json.loads(row["detail_json"])
        except (TypeError, ValueError):
            return cls(now_iso=now_iso)
        if (
            not isinstance(detail, Mapping)
            or detail.get("detail_version") != DETAIL_VERSION
            or not isinstance(detail.get("window_started_at"), str)
        ):
            return cls(now_iso=now_iso)
        try:
            sampler = cls(
                now_iso=now_iso,
                window_started_at=detail["window_started_at"],
            )
        except (TypeError, ValueError):
            return cls(now_iso=now_iso)
        sampler.requests = int(detail.get("requests", 0) or 0)
        sampler.completions = int(detail.get("completions", 0) or 0)
        sampler.filled = int(detail.get("filled", 0) or 0)
        sampler.rejected = int(detail.get("rejected", 0) or 0)
        sampler.context_evictions = int(detail.get("context_evictions", 0) or 0)
        sampler.orphan_completions = int(detail.get("orphan_completions", 0) or 0)
        sampler.retry_drops = int(detail.get("retry_drops", 0) or 0)
        sampler.max_inflight_buckets = int(
            detail.get("max_inflight_buckets", 0) or 0
        )
        sampler.max_position_inflight_buckets = int(
            detail.get("max_position_inflight_buckets", 0) or 0
        )
        sampler.worker_epochs = int(detail.get("worker_epochs", 1) or 1) + 1
        sampler._round_trip = _Histogram(detail.get("round_trip_ms"))
        by_venue = detail.get("by_venue")
        if isinstance(by_venue, Mapping):
            for venue, stored in by_venue.items():
                if not isinstance(stored, Mapping):
                    continue
                sampler._by_venue[str(venue)] = {
                    "requests": int(stored.get("requests", 0) or 0),
                    "completions": int(stored.get("completions", 0) or 0),
                    "filled": int(stored.get("filled", 0) or 0),
                    "rejected": int(stored.get("rejected", 0) or 0),
                    "active_contexts": 0,
                    "max_inflight_buckets": int(
                        stored.get("max_inflight_buckets", 0) or 0
                    ),
                    "round_trip": _Histogram(stored.get("round_trip_ms")),
                }
        probe_metrics = detail.get("probe_metrics")
        if isinstance(probe_metrics, Mapping):
            sampler._probe_metrics = dict(probe_metrics)
        sampler._last_persisted_at = row["heartbeat_at"]
        sampler._sealed = bool(detail.get("window_complete"))
        return sampler
