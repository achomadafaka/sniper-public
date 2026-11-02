"""Database access for the dedicated funded cluster store."""
from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from sniper.util.clock import real_utc_now_iso


_MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "016_memecoin_cluster_gate.sql"


def connect(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_MIGRATION.read_text())
    return conn


def create_snapshot(conn, mint: str, snapshot_at: str) -> str:
    snapshot_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO cluster_snapshots(snapshot_id,mint,snapshot_at) VALUES (?,?,?)",
        (snapshot_id, mint, snapshot_at),
    )
    conn.commit()
    return snapshot_id


def snapshot(conn, snapshot_id: str):
    row = conn.execute(
        "SELECT * FROM cluster_snapshots WHERE snapshot_id=?", (snapshot_id,)
    ).fetchone()
    return dict(row) if row else None


def add_holder(conn, snapshot_id, ata, owner, balance_atomic, exclusion):
    conn.execute(
        "INSERT INTO cluster_snapshot_holders"
        "(snapshot_id,ata,owner,balance_atomic,exclusion) VALUES (?,?,?,?,?)",
        (snapshot_id, ata, owner, str(balance_atomic), exclusion),
    )
    conn.commit()


def holders(conn, snapshot_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM cluster_snapshot_holders WHERE snapshot_id=? ORDER BY rowid",
        (snapshot_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def insert_decision(conn, decision, snapshot_id, source_path):
    conn.execute(
        "INSERT INTO cluster_decisions"
        "(decision_id,snapshot_id,source_path,allowed,reason,policy_id,snapshot_at,"
        "completed_at,metrics_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            decision.decision_id,
            snapshot_id,
            source_path,
            int(decision.allowed),
            decision.reason,
            decision.policy_id,
            decision.snapshot_at,
            decision.completed_at,
            json.dumps(decision.metrics, sort_keys=True, separators=(",", ":")),
        ),
    )
    conn.commit()


def decision(conn, decision_id: str):
    row = conn.execute(
        "SELECT * FROM cluster_decisions WHERE decision_id=?", (decision_id,)
    ).fetchone()
    return dict(row) if row else None


def cached_funders(conn, owners: list[str], parser_version: int) -> dict[str, str]:
    if not owners:
        return {}
    marks = ",".join("?" for _ in owners)
    rows = conn.execute(
        f"SELECT owner,funder FROM cluster_funder_cache WHERE parser_version=? "
        f"AND status='resolved' AND funder IS NOT NULL AND owner IN ({marks})",
        (parser_version, *owners),
    ).fetchall()
    return {row["owner"]: row["funder"] for row in rows}


def ensure_source_columns(conn) -> None:
    """Desk only: funder_source ('direct_tx' | 'wrap_rule' | 'funded_by') and source_detail (JSON: funder type, slot, date, signature of a
    funded-by answer) on the funder cache. Additive and nullable; the funded gate never writes or reads them."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(cluster_funder_cache)")}
    for name in ("funder_source", "source_detail"):
        if name not in cols:
            conn.execute("ALTER TABLE cluster_funder_cache ADD COLUMN %s TEXT" % name)
    conn.commit()


def put_funder(
    conn, owner, funder, status, source_signature, parser_version, resolved_at=None, funder_source=None, source_detail=None
):
    if funder_source is not None:
        conn.execute(
            "INSERT INTO cluster_funder_cache"
            "(owner,parser_version,funder,status,source_signature,resolved_at,funder_source,source_detail) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(owner,parser_version) DO UPDATE SET funder=excluded.funder,"
            "status=excluded.status,source_signature=excluded.source_signature,"
            "resolved_at=excluded.resolved_at,funder_source=excluded.funder_source,source_detail=excluded.source_detail",
            (owner, int(parser_version), funder, status, source_signature, resolved_at or real_utc_now_iso(),
             funder_source, json.dumps(source_detail, sort_keys=True) if source_detail is not None else None),
        )
        conn.commit()
        return
    conn.execute(
        "INSERT INTO cluster_funder_cache"
        "(owner,parser_version,funder,status,source_signature,resolved_at) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(owner,parser_version) DO UPDATE SET funder=excluded.funder,"
        "status=excluded.status,source_signature=excluded.source_signature,"
        "resolved_at=excluded.resolved_at",
        (
            owner,
            int(parser_version),
            funder,
            status,
            source_signature,
            resolved_at or real_utc_now_iso(),
        ),
    )
    conn.commit()


def enqueue_unresolved(conn, owner, observed_at, status):
    conn.execute(
        "INSERT INTO cluster_work_queue(owner,observed_at,status) VALUES (?,?,?) "
        "ON CONFLICT(owner) DO UPDATE SET observed_at=excluded.observed_at,"
        "status=excluded.status",
        (owner, observed_at, status),
    )
    conn.commit()


def claim_cache_work(conn, limit, now_iso):
    rows = conn.execute(
        "SELECT owner FROM cluster_work_queue WHERE status IN ('pending','unresolved','deep') "
        "AND (next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY observed_at DESC LIMIT ?",
        (now_iso, int(limit)),
    ).fetchall()
    owners = [row["owner"] for row in rows]
    if owners:
        marks = ",".join("?" for _ in owners)
        conn.execute(
            f"UPDATE cluster_work_queue SET status='claimed',claimed_at=? WHERE owner IN ({marks})",
            (now_iso, *owners),
        )
        conn.commit()
    return owners


def finish_cache_work(conn, owner, status, next_attempt_at):
    conn.execute(
        "UPDATE cluster_work_queue SET status=?,next_attempt_at=?,claimed_at=NULL WHERE owner=?",
        (status, next_attempt_at, owner),
    )
    conn.commit()


def queue_depth(conn) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM cluster_work_queue WHERE status!='resolved'"
        ).fetchone()[0]
    )


def status_summary(conn) -> dict:
    cache = conn.execute(
        "SELECT status,COUNT(*) AS n FROM cluster_funder_cache GROUP BY status"
    ).fetchall()
    blocks = conn.execute(
        "SELECT reason,COUNT(*) AS n FROM cluster_decisions WHERE allowed=0 GROUP BY reason"
    ).fetchall()
    last = conn.execute(
        "SELECT MAX(completed_at) FROM cluster_decisions"
    ).fetchone()[0]
    return {
        "queue_depth": queue_depth(conn),
        "cache_counts": {row["status"]: row["n"] for row in cache},
        "block_counts": {row["reason"]: row["n"] for row in blocks},
        "last_decision_at": last,
    }
