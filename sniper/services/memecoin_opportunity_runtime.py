"""Single-writer bounded runtime for opportunity capture."""

from __future__ import annotations

import json
from collections import deque

from sniper.research.memecoin_opportunity.continuity import record_source_failure
from sniper.research.memecoin_opportunity.projection import upsert_runtime_status


class OpportunityRuntimeDisabled(RuntimeError):
    pass


_GAP_BLOCKERS = {
    "EVENT_GAP",
    "SOURCE_FAILURE",
    "DECODER_FAILURE",
    "CONFIRMATION_FAILURE",
    "RECOVERY_INCOMPLETE",
    "UNKNOWN_PROGRAM_OR_CONTRACT_VERSION",
    "MISSING_TOKEN_OR_CREATOR_IDENTITY",
}

_HEALTH_DETAIL_KEYS = {
    "event_expectation_started_at",
    "expected_event_max_age_s",
    "last_chain_event_at",
    "transport_stale_after_s",
}


def require_capture_enabled(settings) -> None:
    if not settings.enabled:
        raise OpportunityRuntimeDisabled(
            "memecoin opportunity capture is default OFF; set MEMECOIN_OPPORTUNITY_CAPTURE=1"
        )


class OpportunityCaptureRuntime:
    def __init__(self, conn, *, settings, process_fn, now_fn):
        self.conn = conn
        self.settings = settings
        self.process_fn = process_fn
        self.now_fn = now_fn
        self.queue = deque()
        self.dropped_by_source: dict[str, int] = {}
        # Reset per drain; read by the caller to decide whether a range persisted.
        self.drain_failures = 0
        self.gapped_sources = {
            row["source_key"]
            for row in conn.execute(
                "SELECT source_key,blocker_code FROM op_runtime_status "
                "WHERE blocker_code IS NOT NULL"
            ).fetchall()
            if row["blocker_code"] in _GAP_BLOCKERS
        }
        self.gapped_sources.update(
            row["source_key"]
            for row in conn.execute(
                "SELECT source_key FROM op_source_frontiers WHERE has_open_gap=1"
            ).fetchall()
        )

    def source_gapped(self, source_key: str) -> bool:
        return source_key in self.gapped_sources

    def _source_depth(self, source_key: str) -> int:
        return sum(1 for source, _ in self.queue if source == source_key)

    def submit(self, source_key: str, envelope) -> bool:
        if len(self.queue) >= self.settings.queue_max:
            self.gapped_sources.add(source_key)
            dropped = self.dropped_by_source.get(source_key, 0) + 1
            self.dropped_by_source[source_key] = dropped
            observed_at = self.now_fn()
            record_source_failure(
                self.conn,
                source_key=source_key,
                observed_at=observed_at,
                detail="bounded_queue_overflow",
            )
            upsert_runtime_status(
                self.conn,
                source_key=source_key,
                status="degraded",
                blocker_code="EVENT_GAP",
                heartbeat_at=observed_at,
                queue_depth=self._source_depth(source_key),
                dropped_events=dropped,
                detail={"reason": "bounded_queue_overflow"},
            )
            return False
        self.queue.append((source_key, envelope))
        return True

    def drain(self, *, max_items: int = 256) -> int:
        """Process queued envelopes; return how many were dequeued.

        `drain_failures` counts envelopes whose processing RAISED. A caller that needs
        to know whether a batch persisted completely must read that counter: comparing
        `gapped_sources` before and after only detects a NEW source key, so a second
        failure on an already-gapped source looked like success.
        """
        self.drain_failures = 0
        processed = 0
        touched: dict[str, dict] = {}
        while self.queue and processed < max_items:
            source_key, envelope = self.queue.popleft()
            processed += 1
            try:
                self.process_fn(envelope)
            except Exception as exc:
                self.drain_failures += 1
                self.gapped_sources.add(source_key)
                error_detail = str(exc).replace("\n", " ")[:300]
                record_source_failure(
                    self.conn,
                    source_key=source_key,
                    observed_at=self.now_fn(),
                    detail=(
                        f"runtime_processing_failure:{type(exc).__name__}:"
                        f"{error_detail}"
                    ),
                )
                touched[source_key] = {
                    "blocker_code": "DECODER_FAILURE",
                    "detail": {"error_type": type(exc).__name__, "error": str(exc)[:500]},
                }
                continue
            touched.setdefault(source_key, {"blocker_code": None, "detail": {}})

        for source_key, outcome in touched.items():
            gapped = self.source_gapped(source_key)
            blocker = outcome["blocker_code"]
            prior = self.conn.execute(
                "SELECT blocker_code,detail_json FROM op_runtime_status WHERE source_key=?",
                (source_key,),
            ).fetchone()
            if gapped and blocker is None:
                blocker = prior[0] if prior is not None and prior[0] else "EVENT_GAP"
            try:
                detail = json.loads(prior[1] or "{}") if prior is not None else {}
            except (TypeError, ValueError):
                detail = {}
            detail.update(outcome["detail"])
            upsert_runtime_status(
                self.conn,
                source_key=source_key,
                status="degraded" if gapped else "healthy",
                blocker_code=blocker,
                heartbeat_at=self.now_fn(),
                last_event_at=self.now_fn(),
                queue_depth=self._source_depth(source_key),
                dropped_events=self.dropped_by_source.get(source_key, 0),
                detail=detail,
            )
        return processed

    def heartbeat(
        self,
        source_key: str,
        *,
        status: str = "healthy",
        blocker_code: str | None = None,
        detail=None,
    ) -> None:
        if blocker_code in _GAP_BLOCKERS:
            self.gapped_sources.add(source_key)
        if detail is None:
            row = self.conn.execute(
                "SELECT detail_json FROM op_runtime_status WHERE source_key=?",
                (source_key,),
            ).fetchone()
            try:
                prior_detail = json.loads(row[0] or "{}") if row is not None else {}
            except (TypeError, ValueError):
                prior_detail = {}
            detail = {
                key: prior_detail[key]
                for key in _HEALTH_DETAIL_KEYS
                if key in prior_detail
            }
        upsert_runtime_status(
            self.conn,
            source_key=source_key,
            status=status,
            blocker_code=blocker_code,
            heartbeat_at=self.now_fn(),
            queue_depth=self._source_depth(source_key),
            dropped_events=self.dropped_by_source.get(source_key, 0),
            detail=detail,
        )

    def mark_recovered(self, source_key: str, *, detail=None) -> None:
        self.gapped_sources.discard(source_key)
        self.heartbeat(source_key, detail=detail)
