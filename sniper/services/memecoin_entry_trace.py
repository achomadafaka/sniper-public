"""Immutable, monotonic-clock traces for real memecoin entry attempts."""
from __future__ import annotations

import json
import math
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sniper.persistence import db_memecoin
from sniper.util import receipt_clock as receipt_clocks


_STAGES = frozenset({
    "chain_resolution",
    "market_data",
    "cluster",
    "reservation",
    "safety_checks",
    "quote",
    "signing",
    "transaction_submitted",
    "chain_confirmation",
    "balance_delta",
    "persistence",
})
_REPEATABLE_SUBMISSION_STAGES = frozenset({
    "quote", "signing", "transaction_submitted", "chain_confirmation",
})


def _event_value(event: Any, name: str, default=None):
    if isinstance(event, dict):
        return event.get(name, default)
    return getattr(event, name, default)


def _resolve_decision_features(capture, decision_at):
    from sniper.services.memecoin_decision_features import resolve
    return resolve(capture, decision_at)


def _utc_anchor(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("decision_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("decision_at must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _require_nonblank_terminal_text(value, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonblank string")


def _read_cpu(clock):
    """(thread ident, CPU seconds of the calling thread) or None.

    None when the injected clock has no ``thread_time`` (fake clocks), or it raises, or it returns
    something non-finite: a trace mark must never fail an entry, and an unmeasured CPU figure must
    read as unknown, never as 0. The thread ident rides along because thread CPU time is per
    thread: a delta between readings taken on two different threads is meaningless."""
    thread_time = getattr(clock, "thread_time", None)
    if thread_time is None:
        return None
    try:
        value = float(thread_time())
    except Exception:
        return None
    return (threading.get_ident(), value) if math.isfinite(value) else None


def _read_mono_ns(clock):
    try:
        value = clock.monotonic_ns()
        return value if type(value) is int and value >= 0 else None
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def _cpu_window(previous, now, wall_ms: float) -> dict[str, Any]:
    """CPU and wall milliseconds over ONE window, so the two compare like for like.

    ``cpu_ms`` is None when either reading is missing, the two readings come from different
    threads, or the delta is negative. ``wall_ms`` is the same window's perf_counter gap, which is
    NOT the stage's ``duration_ms`` when a caller passed an explicit duration. Wall far above CPU
    says the thread was waiting (a lock, the GIL, the scheduler or I/O), not which of them."""
    cpu_ms = None
    if previous is not None and now is not None and previous[0] == now[0]:
        delta = (now[1] - previous[1]) * 1000.0
        cpu_ms = delta if delta >= 0 else None
    return {"cpu_ms": cpu_ms, "wall_ms": wall_ms}


def _duration_ms(measured: float, explicit) -> float:
    if explicit is None:
        return measured
    try:
        value = float(explicit)
    except (TypeError, ValueError) as exc:
        raise ValueError("duration_ms must be a finite nonnegative number") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError("duration_ms must be a finite nonnegative number")
    return value


@dataclass
class EntryAttemptTrace:
    conn: Any
    attempt_id: str
    _anchor: datetime
    _started_mono: float
    _last_mono: float
    _clock: Any
    _finished: bool = False
    _stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    _submission_events: list[dict[str, Any]] = field(default_factory=list)
    _substage_events: list[dict[str, Any]] = field(default_factory=list)
    _last_substage_mono: float | None = None
    _shadow_context: dict[str, Any] = field(default_factory=dict)
    _source_evidence: dict[str, Any] = field(default_factory=dict)
    # CPU diagnostics (2026-09-26). Kept OUT of `_stages` on purpose: finish_entry_attempt accepts
    # only {completed_at, duration_ms, measured} per stage and raises on any other key, which would
    # lose the trace of every funded entry. They reach trace_json only.
    _last_cpu: tuple | None = None
    _last_substage_cpu: tuple | None = None
    _stage_cpu_ms: dict[str, dict[str, Any]] = field(default_factory=dict)
    _receipt_clock: Any = None
    _receipt_boot_id: str | None = None
    _decision_receipt_ms: float | None = None
    _receipt_stages: tuple = ()
    # A lane's decision-time capture (memecoin_decision_features): called and written at finish,
    # after the outcome, never before the send (spec 2026-10-14). Measurement only.
    _decision_features: Any = None

    @classmethod
    def start(cls, conn, ev, decision_at: str, clock) -> "EntryAttemptTrace":
        """Commit the shell row and bind all later stage stamps to one clock domain."""
        anchor = _utc_anchor(decision_at)
        started_mono = float(clock.perf_counter())
        receipt_clock = receipt_clocks.freeze_receipt_clock(_event_value(ev, "receipt_clock"))
        receipt_boot_id = receipt_clocks.clock_domain() if receipt_clock else None
        decision_receipt_ms = (receipt_clocks.elapsed_receipt_ms(
            receipt_clock, _read_mono_ns(clock), boot_id=receipt_boot_id) if receipt_clock else None)
        receipt_stages = tuple(_event_value(ev, "receipt_stages") or ())
        # Read BEFORE the shell-row INSERT so the first substage's CPU window covers that write.
        started_cpu = _read_cpu(clock)
        attempt_id = uuid.uuid4().hex
        source_evidence = dict(_event_value(ev, "source_evidence") or {})
        received_at_normalized = _event_value(ev, "received_at_normalized")
        event_clock_domain = str(_event_value(ev, "clock_domain") or "").strip()
        if receipt_clock:
            observed_at = receipt_clock["corrected_utc"]
            receipt_clock_domain = "observed_real_utc"
        elif received_at_normalized:
            observed_at = received_at_normalized
            receipt_clock_domain = "observed_real_utc"
        elif event_clock_domain == "observed_real_utc":
            observed_at = _event_value(ev, "observed_at") or _event_value(ev, "received_at")
            receipt_clock_domain = "observed_real_utc"
        else:
            observed_at = None
            receipt_clock_domain = event_clock_domain or "legacy_unknown"
        initial = dict(source_evidence=source_evidence) if source_evidence else {}
        if receipt_clock:
            initial.update(receipt_clock=dict(receipt_clock), decision_receipt_ms=decision_receipt_ms,
                           receipt_stages=receipt_stages)
        db_memecoin.start_entry_attempt(
            conn,
            attempt_id=attempt_id,
            origin_event_id=_event_value(ev, "fomo_event_id"),
            signal_source=_event_value(ev, "signal_source"),
            network_id=str(_event_value(ev, "network_id") or ""),
            mint=_event_value(ev, "mint"),
            source_user_id=_event_value(ev, "source_user_id"),
            source_handle=_event_value(ev, "source_handle"),
            ticker=_event_value(ev, "ticker"),
            side=_event_value(ev, "side"),
            source_created_at=_event_value(ev, "created_at"),
            source_timestamp_trust=_event_value(ev, "source_timestamp_trust", "claimed_untrusted"),
            source_price=_event_value(ev, "their_price"),
            source_market_cap=_event_value(ev, "their_market_cap"),
            observed_at=observed_at,
            clock_domain=receipt_clock_domain,
            delivery_lane=_event_value(ev, "delivery_lane"),
            decision_started_at=anchor.isoformat(),
            **({"trace_json": json.dumps(initial,
                                        sort_keys=True, separators=(",", ":"))}
               if initial else {}),
        )
        return cls(conn, attempt_id, anchor, started_mono, started_mono, clock,
                   _source_evidence=source_evidence, _last_cpu=started_cpu,
                   _last_substage_cpu=started_cpu, _receipt_clock=receipt_clock,
                   _receipt_boot_id=receipt_boot_id, _decision_receipt_ms=decision_receipt_ms,
                   _receipt_stages=receipt_stages,
                   _decision_features=_event_value(ev, "decision_features"))

    def receipt_context(self) -> dict[str, Any]:
        """Original ingress metadata for bounded off-path publication; never restamps T0."""
        return {"receipt_clock": dict(self._receipt_clock) if self._receipt_clock else None,
                "decision_receipt_ms": self._decision_receipt_ms,
                "receipt_stages": [dict(stage) for stage in self._receipt_stages]}

    def _receipt_stage(self):
        if not self._receipt_clock:
            return {}
        mono = _read_mono_ns(self._clock)
        return {"monotonic_ns": mono, "boot_id": self._receipt_boot_id,
                "receipt_elapsed_ms": receipt_clocks.elapsed_receipt_ms(
                    self._receipt_clock, mono, boot_id=self._receipt_boot_id)}

    def _now(self) -> float:
        now = float(self._clock.perf_counter())
        if now < self._last_mono:
            raise RuntimeError("entry-attempt monotonic clock moved backwards")
        return now

    def elapsed_ms_at(self, perf_value: float) -> float:
        """Milliseconds from the decision to `perf_value`, a reading of this trace's perf_counter."""
        return (float(perf_value) - self._started_mono) * 1000.0

    def _absolute_at(self, monotonic_value: float) -> str:
        elapsed = monotonic_value - self._started_mono
        return (self._anchor + timedelta(seconds=elapsed)).isoformat()

    def mark(self, stage: str, *, duration_ms=None) -> bool:
        """Record one reached stage in memory. Missing or repeated stages stay unchanged."""
        if self._finished:
            return False
        if stage not in _STAGES:
            raise ValueError(f"unknown entry-attempt stage: {stage}")
        if stage in self._stages:
            return False
        now = self._now()
        completed_at = self._absolute_at(now)
        # `measured` is the difference between a duration and a guess. Without an explicit value the
        # fallback below is the gap since the PREVIOUS mark, which includes any work belonging to
        # neither stage — so it cannot be compared against a real duration, and the stage columns
        # cannot sum to a budget. Nine of the ten funded-entry stage marks infer; only `reservation`
        # measures. Recording which is which is what stops them being read side by side.
        measured = duration_ms is not None
        gap_ms = (now - self._last_mono) * 1000.0
        duration_ms = _duration_ms(gap_ms, duration_ms)
        cpu = _read_cpu(self._clock)
        self._stage_cpu_ms[stage] = _cpu_window(self._last_cpu, cpu, gap_ms)
        self._last_cpu = cpu
        self._last_mono = now
        self._stages[stage] = {
            "completed_at": completed_at,
            "duration_ms": duration_ms,
            "measured": measured,
        }
        return True

    def mark_submission(self, stage: str, attempt_no: int, **metadata) -> bool:
        """Append one retry-aware submission event and project its latest stamp to stage columns.

        The event list is immutable history; ``_stages`` intentionally points at the latest attempt
        so p50/p95 columns describe the quote/sign/submit/confirmation that produced the terminal
        result instead of silently retaining attempt 1.
        """
        if self._finished:
            return False
        if stage not in _REPEATABLE_SUBMISSION_STAGES:
            raise ValueError(f"non-repeatable entry-attempt stage: {stage}")
        if isinstance(attempt_no, bool) or int(attempt_no) < 1:
            raise ValueError("attempt_no must be a positive integer")
        reserved = {"stage", "attempt_no", "completed_at", "duration_ms", "measured",
                    "monotonic_ns", "boot_id", "receipt_elapsed_ms"}
        if reserved & set(metadata):
            raise ValueError("submission metadata overrides reserved fields")
        now = self._now()
        cpu = _read_cpu(self._clock)
        self._stage_cpu_ms[stage] = _cpu_window(self._last_cpu, cpu, (now - self._last_mono) * 1000.0)
        self._last_cpu = cpu
        stage_data = {
            "completed_at": self._absolute_at(now),
            # Inferred, like every mark() without an explicit duration: the gap since the previous
            # mark, not this submission's own work. The substage events carry the real durations.
            "duration_ms": (now - self._last_mono) * 1000.0,
            "measured": False,
        }
        self._last_mono = now
        self._stages[stage] = stage_data
        self._submission_events.append({
            "stage": stage,
            "attempt_no": int(attempt_no),
            **stage_data,
            **self._receipt_stage(),
            **metadata,
        })
        return True

    def stage_completed_at(self, stage: str) -> str | None:
        """Return a reached stage's timestamp derived from this trace's monotonic anchor."""
        if stage not in _STAGES:
            raise ValueError(f"unknown entry-attempt stage: {stage}")
        data = self._stages.get(stage)
        return data["completed_at"] if data else None

    def last_substage(self, name: str) -> dict[str, Any] | None:
        """A copy of the newest substage event of `name`, or None. Read-only."""
        for event in reversed(self._substage_events):
            if event.get("name") == name:
                return dict(event)
        return None

    def mark_substage(self, name: str, *, duration_ms=None, **metadata) -> bool:
        """Append a monotonic diagnostic event without changing top-level stage durations."""
        if self._finished:
            return False
        if not isinstance(name, str) or not name.strip():
            raise ValueError("substage name must be a nonblank string")
        reserved = {"name", "completed_at", "elapsed_ms", "cpu_ms", "cpu_wall_ms",
                    "monotonic_ns", "boot_id", "receipt_elapsed_ms"}
        if reserved & set(metadata):
            raise ValueError("substage metadata overrides reserved fields")
        now = self._now()
        previous = self._last_substage_mono
        if previous is None:
            previous = self._started_mono
        cpu = _read_cpu(self._clock)
        window = _cpu_window(self._last_substage_cpu, cpu, (now - previous) * 1000.0)
        self._substage_events.append({
            "name": name.strip(),
            "completed_at": self._absolute_at(now),
            "elapsed_ms": (now - self._started_mono) * 1000.0,
            "duration_ms": _duration_ms((now - previous) * 1000.0, duration_ms),
            "cpu_ms": window["cpu_ms"],
            "cpu_wall_ms": window["wall_ms"],
            **self._receipt_stage(),
            **metadata,
        })
        self._last_substage_mono = now
        self._last_substage_cpu = cpu
        return True

    def set_shadow_context(self, **metadata) -> bool:
        """Keep local observation facts for the terminal entry-shadow event."""
        if self._finished:
            return False
        self._shadow_context.update(metadata)
        return True

    def shadow_context(self) -> dict[str, Any]:
        """Return a copy of terminal observation facts."""
        return {**self._shadow_context, "gate_events": [dict(event) for event in self._substage_events]}

    def reconciled_now(self) -> str:
        """Read the current absolute time from this trace's monotonic local clock domain."""
        return self._absolute_at(self._now())

    def note_decision_features(self, capture) -> None:
        """A lane's decision-time capture; called and written at finish, never before it."""
        if not self._finished:
            self._decision_features = capture

    def finish(self, status: str, reason: str, **fields) -> bool:
        """Finalize once; terminal evidence and reached stages cannot be rewritten."""
        if self._finished:
            return False
        _require_nonblank_terminal_text(status, "terminal_status")
        _require_nonblank_terminal_text(reason, "terminal_reason")
        now = self._now()
        total_ms = (now - self._started_mono) * 1000.0
        completed_at = self._absolute_at(now)
        payload = {
            "attempt_id": self.attempt_id,
            "terminal_status": status,
            "terminal_reason": reason,
            "total_ms": total_ms,
            "stages": self._stages,
            "submission_events": self._submission_events,
            "substage_events": self._substage_events,
            "stage_cpu_ms": self._stage_cpu_ms,
            "fields": fields,
            **({"source_evidence": self._source_evidence} if self._source_evidence else {}),
            **({"decision_features": _resolve_decision_features(
                self._decision_features, self._anchor.isoformat())}
               if self._decision_features is not None else {}),
            **(dict(self.receipt_context(), receipt_clock_status=(
                "boot_monotonic_v1" if self._decision_receipt_ms is not None else "receipt_clock_unknown"))
               if self._receipt_clock else {}),
        }
        changed = db_memecoin.finish_entry_attempt(
            self.conn,
            self.attempt_id,
            status,
            reason,
            completed_at,
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str),
            stages=self._stages,
            total_ms=total_ms,
            **fields,
        )
        self._finished = True
        return changed
