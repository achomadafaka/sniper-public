"""Local service and fail-closed client for funded cluster admission."""
from __future__ import annotations

import json
import logging
import socket
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sniper.services.memecoin_cluster_admission import (
    POLICY_ID,
    ClusterDecision,
    decide_cluster,
)
from sniper.services.memecoin_cluster_rpc import ResearchRpcQuotaError
from sniper.util.clock import real_utc_now_iso


logger = logging.getLogger(__name__)
PARSER_VERSION = 1
MAX_LINE_BYTES = 16_384


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("cluster time requires a UTC offset")
    return parsed.astimezone(timezone.utc)


def _blocked(reason: str, now_fn=real_utc_now_iso) -> ClusterDecision:
    now = now_fn()
    return ClusterDecision(
        decision_id=uuid.uuid4().hex,
        allowed=False,
        reason=reason,
        policy_id=POLICY_ID,
        snapshot_at=now,
        completed_at=now,
        metrics={},
    )


class ClusterGateClient:
    """Request one fresh decision and block on all client or service failures."""

    def __init__(self, socket_path, *, timeout_s: float = 13.0, now_fn=real_utc_now_iso):
        self.socket_path = str(socket_path)
        self.timeout_s = float(timeout_s)
        self.now_fn = now_fn

    def decide(self, mint: str, *, source_path: str) -> ClusterDecision:
        request = {
            "mint": mint,
            "source_path": source_path,
            "requested_at": self.now_fn(),
        }
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self.timeout_s)
                client.connect(self.socket_path)
                client.sendall(json.dumps(request, separators=(",", ":")).encode() + b"\n")
                payload = b""
                while b"\n" not in payload and len(payload) <= MAX_LINE_BYTES:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    payload += chunk
            if b"\n" not in payload or len(payload) > MAX_LINE_BYTES:
                raise ValueError("invalid cluster service response")
            raw = json.loads(payload.split(b"\n", 1)[0])
            decision = ClusterDecision(**raw)
            if decision.allowed and self._stale(decision):
                return ClusterDecision(
                    **{**asdict(decision), "allowed": False, "reason": "cluster_decision_stale"}
                )
            return decision
        except (OSError, TimeoutError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            logger.exception("cluster gate request failed mint=%s", mint)
            return _blocked("cluster_store_unavailable", self.now_fn)

    def _stale(self, decision: ClusterDecision) -> bool:
        now = _parse_utc(self.now_fn())
        completed_age = (now - _parse_utc(decision.completed_at)).total_seconds()
        snapshot_age = (now - _parse_utc(decision.snapshot_at)).total_seconds()
        return (
            completed_age < 0
            or snapshot_age < 0
            or completed_age > 5.0
            or snapshot_age > 17.0
        )


class ClusterAdmissionService:
    def __init__(self, rpc, store, *, now_fn=real_utc_now_iso, workers=8, deadline_s=12.0,
                 thin_owner_pass=False, holder_failure_reason=None, early_allow=False, parser_version=None,
                 non_clusterable=frozenset()):
        # The defaults are the funded gate's, unchanged. The desk passes a paced single worker, a
        # deadline sized to its call budget, and the thin-book rule (operator 2026-10-21, b).
        self.rpc = rpc
        self.store = store
        self.now_fn = now_fn
        self.workers, self.deadline_s, self.thin_owner_pass = workers, deadline_s, thin_owner_pass
        # The desk names every holder/owner read failure cluster_unknown_dependency (Gamma's name);
        # None keeps the funded gate's existing quota / trace_error reasons.
        self._holder_failure = holder_failure_reason
        self.early_allow = early_allow           # desk only: allow on the exact unresolved-supply bound
        self.non_clusterable = frozenset(non_clusterable)   # desk: the frozen shared-infrastructure funder list (empty = funded behaviour)
        self.parser_version = PARSER_VERSION if parser_version is None else int(parser_version)   # desk: its own funder cache version

    def _failure(self, cause, funded_reason):
        """The funded gate (no holder_failure_reason) keeps its exact reasons; the desk keeps its one name and appends
        the cause so live tickets say which dependency failed: cluster_unknown_dependency:quota|trace_error|owner_program."""
        return funded_reason if self._holder_failure is None else "%s:%s" % (self._holder_failure, cause)

    def decide(self, mint: str, *, source_path: str, requested_at: str) -> ClusterDecision:
        if self.rpc is None:
            decision = _blocked("cluster_no_rpc", self.now_fn)
            self.store.insert_decision(decision, None, source_path)
            return decision
        try:
            holders = self.rpc.eligible_holders(mint)
        except ResearchRpcQuotaError:
            decision = _blocked(self._failure("quota", "cluster_unknown_quota"), self.now_fn)
            self.store.insert_decision(decision, None, source_path)
            return decision
        except Exception:
            logger.exception("cluster holder read failed mint=%s", mint)
            decision = _blocked(self._failure("trace_error", "cluster_unknown_trace_error"), self.now_fn)
            self.store.insert_decision(decision, None, source_path)
            return decision
        # The thin-book rule passes on the owners it can trace, so an owner whose program read came
        # back null or missing ({value: [null]} / {value: []}) could be a program account traced as a
        # clean wallet (Codex, 85f4fdcbb). The desk (the only thin_owner_pass caller) decides
        # nothing on such a book; the funded gate is unchanged.
        if self.thin_owner_pass and any(h.program_unknown for h in holders if h.exclusion is None):
            decision = _blocked(self._failure("owner_program", "cluster_unknown_owner_program"), self.now_fn)
            self.store.insert_decision(decision, None, source_path)
            return decision
        snapshot_at = self.now_fn()
        snapshot_id = self.store.create_snapshot(mint, snapshot_at)
        owners_by_supply: dict[str, int] = {}
        for holder in holders:
            self.store.add_holder(
                snapshot_id,
                holder.ata,
                holder.owner,
                str(holder.balance_atomic),
                holder.exclusion,
            )
            if holder.exclusion is None:
                owner = holder.owner or f"unresolved:{holder.ata}"
                owners_by_supply[owner] = (
                    owners_by_supply.get(owner, 0) + holder.balance_atomic
                )
        real_owners = [owner for owner in owners_by_supply if not owner.startswith("unresolved:")]
        cached = self.store.cached_funders(real_owners, self.parser_version)
        for owner in real_owners:
            if owner not in cached:
                self.store.enqueue_unresolved(owner, snapshot_at, "pending")

        # The exact signature each funder was read from is captured per owner and persisted
        # with the cache row. Without it a future parser fix cannot identify which cached
        # rows it invalidated, leaving stale funder evidence with no way to find it.
        source_signatures: dict[str, str | None] = {}
        source_infos: dict[str, dict] = {}

        def trace(owner: str, should_stop=None):
            # decide_cluster hands its stop flag only to a trace_fn that takes `should_stop`; this
            # closure used to drop it, so every deadlined trace, funded or desk, kept paging after
            # the decision returned (Codex, 669b0b0a4). A late trace is never persisted (persist
            # runs only inside decide_cluster's loop), so stopping it changes no decision or cache.
            if owner.startswith("unresolved:"):
                return None, "unresolved"
            result = self.rpc.trace_first_funder(owner, should_stop=should_stop)
            source_signatures[owner] = getattr(self.rpc, "last_source_signature", None)
            info = getattr(self.rpc, "last_source_info", None)       # desk rpc only: how this funder was found
            if info is not None:
                source_infos[owner] = info
            return result

        def persist(owner: str, funder: str | None, status: str) -> None:
            info = source_infos.get(owner)
            extra = ({"funder_source": info["funder_source"],
                      "source_detail": {"funder_type": info.get("funder_type"), **(info.get("detail") or {})}}
                     if info is not None and status == "resolved" else {})
            self.store.record_funder(
                owner, funder, status,
                source_signature=source_signatures.get(owner),
                parser_version=self.parser_version,
                **extra,
            )

        decision = decide_cluster(
            list(owners_by_supply.items()),
            cached,
            trace_fn=trace,
            persist_fn=persist,
            workers=self.workers,
            deadline_s=self.deadline_s,
            snapshot_at=snapshot_at,
            now_fn=self.now_fn,
            thin_owner_pass=self.thin_owner_pass, early_allow=self.early_allow,
            non_clusterable=self.non_clusterable,
        )
        self.store.insert_decision(decision, snapshot_id, source_path)
        return decision


QUOTA_BACKOFF_MIN_S = 30.0
QUOTA_BACKOFF_MAX_S = 900.0


class ClusterCacheWorker:
    def __init__(self, store, trace_fn: Callable[[str], tuple[str | None, str]], *, now_fn=real_utc_now_iso):
        self.store = store
        self.trace_fn = trace_fn
        self.now_fn = now_fn
        self._test_queue: list[str] | None = None

    _quota_backoff_until = 0.0
    _quota_backoff_s = 0.0
    skipped_for_backoff = False

    @classmethod
    def for_test(cls, *, queue: list[str], trace_fn):
        worker = cls(None, trace_fn)
        worker._test_queue = list(queue)
        return worker

    def cache_pass(self, limit: int = 20, *, monotonic=time.monotonic) -> dict[str, int]:
        """One bounded warming pass.

        Two quota rules, both required (audit 2026-08-22):
          1. A 429 RELEASES every owner still claimed in this batch. Previously the loop
             broke and left them claimed with no owner and no expiry, so each 429 leaked
             work permanently out of the queue.
          2. A 429 backs off with exponential delay. Previously the next pass re-claimed
             immediately and hit the same cap, which is how a research loop burned ~7,000
             requests in four hours on 2026-08-21.
        """
        now = monotonic()
        self.skipped_for_backoff = False
        if now < self._quota_backoff_until:
            self.skipped_for_backoff = True
            return {"resolved": 0, "quota": 0}
        owners = (
            list(self._test_queue)
            if self._test_queue is not None
            else self.store.claim_cache_work(limit, self.now_fn())
        )
        counts = {"resolved": 0, "quota": 0}
        for index, owner in enumerate(owners):
            try:
                funder, status = self.trace_fn(owner)
            except ResearchRpcQuotaError:
                counts["quota"] += 1
                self._quota_backoff_s = min(
                    max(self._quota_backoff_s * 2, QUOTA_BACKOFF_MIN_S), QUOTA_BACKOFF_MAX_S
                )
                self._quota_backoff_until = monotonic() + self._quota_backoff_s
                # release THIS owner and every owner still claimed behind it
                if self.store is not None:
                    for stranded in owners[index:]:
                        self.store.finish_cache_work(stranded, "pending", self.now_fn())
                break
            except Exception:
                logger.exception("cluster cache trace failed owner=%s", owner)
                if self.store is not None:
                    self.store.finish_cache_work(owner, "unresolved", self.now_fn())
                continue
            if self.store is not None:
                self.store.put_funder_result(owner, funder, status)
            if status == "resolved" and funder:
                counts["resolved"] += 1
                self._quota_backoff_s = 0.0          # a clean pass clears the backoff
        return counts


def serve_one(server: socket.socket, service: ClusterAdmissionService) -> None:
    client, _address = server.accept()
    with client:
        payload = b""
        while b"\n" not in payload and len(payload) <= MAX_LINE_BYTES:
            chunk = client.recv(4096)
            if not chunk:
                break
            payload += chunk
        if b"\n" not in payload or len(payload) > MAX_LINE_BYTES:
            decision = _blocked("cluster_store_unavailable")
        else:
            try:
                request = json.loads(payload.split(b"\n", 1)[0])
                decision = service.decide(
                    request["mint"],
                    source_path=request["source_path"],
                    requested_at=request["requested_at"],
                )
            except Exception:
                logger.exception("cluster service request failed")
                decision = _blocked("cluster_store_unavailable")
        client.sendall(json.dumps(asdict(decision), separators=(",", ":")).encode() + b"\n")


def bind_server(path: str | Path) -> socket.socket:
    socket_path = Path(path)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    server.listen(8)
    return server
