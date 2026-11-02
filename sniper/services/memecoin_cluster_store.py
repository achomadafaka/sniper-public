"""Store interface for immutable cluster snapshots and decisions."""
from __future__ import annotations

from pathlib import Path

from sniper.persistence import db_memecoin_cluster as db


class ClusterStore:
    def __init__(self, path: str | Path):
        self.conn = db.connect(path)

    def create_snapshot(self, mint: str, snapshot_at: str) -> str:
        return db.create_snapshot(self.conn, mint, snapshot_at)

    def snapshot(self, snapshot_id: str):
        return db.snapshot(self.conn, snapshot_id)

    def add_holder(self, snapshot_id, ata, owner, balance_atomic, exclusion):
        return db.add_holder(
            self.conn, snapshot_id, ata, owner, balance_atomic, exclusion
        )

    def holders(self, snapshot_id: str) -> list[dict]:
        return db.holders(self.conn, snapshot_id)

    def insert_decision(self, decision, snapshot_id, source_path):
        return db.insert_decision(
            self.conn, decision, snapshot_id, source_path
        )

    def decision(self, decision_id: str):
        return db.decision(self.conn, decision_id)

    def cached_funders(self, owners: list[str], parser_version: int):
        return db.cached_funders(self.conn, owners, parser_version)

    def put_funder(
        self, owner, funder, status, source_signature=None, parser_version=1, funder_source=None, source_detail=None
    ):
        return db.put_funder(
            self.conn, owner, funder, status, source_signature, parser_version,
            funder_source=funder_source, source_detail=source_detail
        )

    def ensure_source_columns(self):
        return db.ensure_source_columns(self.conn)

    def record_funder(self, owner, funder, status, *, source_signature=None, parser_version=1, funder_source=None,
                      source_detail=None):
        """The name ClusterAdmissionService.decide persists through (it was missing: every live
        trace raised AttributeError; only fake stores in tests had it)."""
        if funder_source is None:                  # the funded gate: exactly the old call
            return self.put_funder(owner, funder, status, source_signature, parser_version)
        return self.put_funder(owner, funder, status, source_signature, parser_version, funder_source, source_detail)

    def put_funder_result(self, owner, funder, status):
        self.put_funder(owner, funder, status)
        self.finish_cache_work(owner, status)

    def enqueue_unresolved(self, owner, observed_at, status):
        return db.enqueue_unresolved(self.conn, owner, observed_at, status)

    def claim_cache_work(self, limit, now_iso):
        return db.claim_cache_work(self.conn, limit, now_iso)

    def finish_cache_work(self, owner, status, next_attempt_at=None):
        return db.finish_cache_work(self.conn, owner, status, next_attempt_at)

    def queue_depth(self) -> int:
        return db.queue_depth(self.conn)

    def status_summary(self) -> dict:
        return db.status_summary(self.conn)
