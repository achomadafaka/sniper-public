"""Separate Ink stream journal. No high-frequency writes to the funded ledger."""

import json


def ensure_stream_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ink_stream_observations (
            observation_key TEXT PRIMARY KEY,
            payload_id TEXT NOT NULL,
            block_number INTEGER NOT NULL,
            tx_hash TEXT NOT NULL,
            owner TEXT NOT NULL,
            state TEXT NOT NULL,
            record_json TEXT NOT NULL,
            canonical_json TEXT,
            reconcile_checked_ns INTEGER NOT NULL DEFAULT 0,
            recheck_checked_ns INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS ink_stream_block ON ink_stream_observations(block_number);
        CREATE TABLE IF NOT EXISTS ink_stream_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observation_key TEXT NOT NULL,
            state TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ink_stream_cursor (
            id INTEGER PRIMARY KEY CHECK(id=1), last_change_id INTEGER NOT NULL DEFAULT 0
        );
        INSERT OR IGNORE INTO ink_stream_cursor(id,last_change_id) VALUES (1,0);
        CREATE TABLE IF NOT EXISTS ink_stream_gaps (
            block_number INTEGER NOT NULL,
            reason TEXT NOT NULL,
            end_block_number INTEGER,
            resolved_at TEXT,
            PRIMARY KEY (block_number,reason)
        );
        CREATE TABLE IF NOT EXISTS ink_canonical_blocks (
            block_number INTEGER PRIMARY KEY,
            block_hash TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ink_backfill_frontier (
            id INTEGER PRIMARY KEY CHECK(id=1), next_block INTEGER NOT NULL
        );
        -- G2 OBSERVATION PANEL (2026-09-30): senders the canonical backfill reads for ground truth
        -- only. Never projected into the funded ledger, never armed, never a registry owner.
        CREATE TABLE IF NOT EXISTS ink_observation_panel (
            address TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            source TEXT,
            frozen_at TEXT,
            recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(ink_stream_gaps)")}
    if "end_block_number" not in columns:
        conn.execute("ALTER TABLE ink_stream_gaps ADD COLUMN end_block_number INTEGER")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(ink_stream_observations)")}
    for name in ("reconcile_checked_ns", "recheck_checked_ns"):
        if name not in columns:
            conn.execute(f"ALTER TABLE ink_stream_observations ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")


def insert_observation(conn, record):
    key = str(record["observation_key"])
    cursor = conn.execute(
        "INSERT OR IGNORE INTO ink_stream_observations "
        "(observation_key,payload_id,block_number,tx_hash,owner,state,record_json,canonical_json) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (key, record["payload_id"], int(record["block_number"]), record["tx_hash"],
         record["owner"], record["state"], json.dumps(record, sort_keys=True),
         json.dumps(record["receipt"], sort_keys=True)
         if record["state"] == "canonical_unsafe" else None))
    if cursor.rowcount:
        conn.execute("INSERT INTO ink_stream_changes(observation_key,state) VALUES (?,?)",
                     (key, record["state"]))
    return bool(cursor.rowcount)


def next_unprojected(conn, limit=100):
    rows = conn.execute(
        "SELECT c.id,o.observation_key,o.payload_id,o.block_number,o.tx_hash,o.owner,o.state "
        "FROM ink_stream_changes c JOIN ink_stream_observations o "
        "ON o.observation_key=c.observation_key "
        "WHERE c.id>(SELECT last_change_id FROM ink_stream_cursor WHERE id=1) "
        "ORDER BY c.id LIMIT ?", (max(1, min(int(limit), 1000)),)).fetchall()
    return [dict(zip(("id", "observation_key", "payload_id", "block_number", "tx_hash", "owner", "state"), row))
            for row in rows]


def advance_cursor(conn, change_id):
    conn.execute("UPDATE ink_stream_cursor SET last_change_id=max(last_change_id,?) WHERE id=1",
                 (int(change_id),))


def observation(conn, key):
    row = conn.execute(
        "SELECT observation_key,payload_id,block_number,tx_hash,owner,state,record_json,canonical_json "
        "FROM ink_stream_observations WHERE observation_key=?", (key,)).fetchone()
    return dict(zip(("observation_key", "payload_id", "block_number", "tx_hash", "owner", "state",
                     "record_json", "canonical_json"), row)) if row else None


def provisional_for_reconcile(conn, limit=100):
    rows = conn.execute(
        "SELECT observation_key,tx_hash FROM ink_stream_observations WHERE state='provisional' "
        "ORDER BY reconcile_checked_ns,block_number,observation_key LIMIT ?",
        (max(1, min(int(limit), 1000)),)).fetchall()
    return [(row[0], row[1]) for row in rows]


def canonical_for_recheck(conn, limit=100):
    rows = conn.execute(
        "SELECT observation_key,canonical_json FROM ink_stream_observations "
        "WHERE state IN ('canonical_unsafe','safe') "
        "ORDER BY recheck_checked_ns,block_number DESC LIMIT ?",
        (max(1, min(int(limit), 1000)),)).fetchall()
    return [(row[0], json.loads(row[1])) for row in rows if row[1]]


def mark_receipt_checked(conn, key, *, recheck, at_ns):
    column = "recheck_checked_ns" if recheck else "reconcile_checked_ns"
    conn.execute(f"UPDATE ink_stream_observations SET {column}=? WHERE observation_key=?",
                 (int(at_ns), key))


def _change_state(conn, key, state, canonical=None):
    cursor = conn.execute(
        "UPDATE ink_stream_observations SET state=?,canonical_json=coalesce(?,canonical_json) "
        "WHERE observation_key=? AND state<>?",
        (state, json.dumps(canonical, sort_keys=True) if canonical is not None else None, key, state))
    if cursor.rowcount:
        conn.execute("INSERT INTO ink_stream_changes(observation_key,state) VALUES (?,?)", (key, state))
    return bool(cursor.rowcount)


def invalidate_payload(conn, payload_id):
    keys = [row[0] for row in conn.execute(
        "SELECT observation_key FROM ink_stream_observations WHERE payload_id=? AND state='provisional'",
        (payload_id,)).fetchall()]
    for key in keys:
        _change_state(conn, key, "retracted")
    return len(keys)


def reconcile_canonical(conn, key, receipt, *, block_hash):
    row = observation(conn, key)
    if row is None:
        raise KeyError(key)
    record = json.loads(row["record_json"])
    if not isinstance(receipt, dict) or not all(
            field in receipt for field in ("transactionHash", "blockHash", "blockNumber", "status", "logs")):
        return row["state"]
    if not isinstance(receipt["logs"], list):
        return row["state"]
    if (not isinstance(block_hash, str) or len(block_hash) != 66
            or not block_hash.startswith("0x") or not any(ch != "0" for ch in block_hash[2:])):
        return row["state"]
    try:
        int(block_hash[2:], 16)
        int(str(receipt["blockNumber"]), 16)
    except ValueError:
        return row["state"]
    indexes = [log.get("logIndex") for log in receipt["logs"] if isinstance(log, dict)]
    if (len(indexes) != len(receipt["logs"]) or any(index is None for index in indexes)
            or len(set(indexes)) != len(indexes)
            or any(str(log.get("transactionHash") or "").lower() != row["tx_hash"].lower()
                   or str(log.get("blockHash") or "").lower() != str(receipt["blockHash"]).lower()
                   for log in receipt["logs"])):
        return row["state"]
    matched = (isinstance(receipt, dict)
               and str(receipt.get("transactionHash") or "").lower() == row["tx_hash"].lower()
               and int(str(receipt["blockNumber"]), 16) == row["block_number"]
               and str(receipt.get("blockHash") or "").lower() == str(block_hash).lower()
               and receipt.get("status") in ("0x1", 1)
               and isinstance(receipt.get("logs"), list)
               and [(str(log.get("address") or "").lower(), log.get("topics"), log.get("data"))
                    for log in receipt["logs"]] ==
                   [(str(log.get("address") or "").lower(), log.get("topics"), log.get("data"))
                    for log in record["receipt"]["logs"]])
    state = (row["state"] if matched and row["state"] in {"safe", "finalized"}
             else "canonical_unsafe" if matched else "retracted")
    _change_state(conn, key, state, receipt if matched else None)
    return state


def promote_finality(conn, key, state, *, block_hash):
    if state not in {"safe", "finalized"}:
        raise ValueError("invalid finality state")
    row = observation(conn, key)
    if not row or not row["canonical_json"]:
        return False
    receipt = json.loads(row["canonical_json"])
    if str(receipt.get("blockHash") or "").lower() != str(block_hash).lower():
        return False
    if state == "safe" and row["state"] != "canonical_unsafe":
        return False
    if state == "finalized" and row["state"] not in {"canonical_unsafe", "safe"}:
        return False
    return _change_state(conn, key, state)


PANEL_LABEL = "observation_only_never_projected"


def record_observation_panel(conn, addresses, *, source, frozen_at):
    """Record the frozen G2 panel inside the journal, labelled observation-only."""
    conn.executemany(
        "INSERT INTO ink_observation_panel(address,label,source,frozen_at) VALUES (?,?,?,?) "
        "ON CONFLICT(address) DO UPDATE SET label=excluded.label, source=excluded.source, "
        "frozen_at=excluded.frozen_at",
        [(str(a).lower(), PANEL_LABEL, source, frozen_at) for a in sorted(addresses)])
    conn.commit()


def record_gap(conn, block_number, reason, *, end_block_number=None):
    start = int(block_number)
    end = start if end_block_number is None else int(end_block_number)
    if start < 0 or end < start:
        raise ValueError("invalid Ink gap range")
    return bool(conn.execute(
        "INSERT INTO ink_stream_gaps(block_number,reason,end_block_number) VALUES (?,?,?) "
        "ON CONFLICT(block_number,reason) DO UPDATE SET "
        "end_block_number=max(coalesce(end_block_number,block_number),excluded.end_block_number),"
        "resolved_at=NULL",
        (start, str(reason), end)).rowcount)


def record_canonical_block(conn, block_number, block_hash):
    """Call only after every transaction receipt in this block passed canonical verification."""
    try:
        valid_hash = (isinstance(block_hash, str) and len(block_hash) == 66
                      and block_hash.startswith("0x") and int(block_hash, 16) != 0)
    except ValueError:
        valid_hash = False
    if not valid_hash:
        return False
    number = int(block_number)
    if number < 0:
        return False
    old = conn.execute("SELECT block_hash FROM ink_canonical_blocks WHERE block_number=?",
                       (number,)).fetchone()
    if old and old[0].lower() != block_hash.lower():
        rows = conn.execute(
            "SELECT observation_key,canonical_json FROM ink_stream_observations "
            "WHERE block_number=? AND state IN ('canonical_unsafe','safe','finalized')",
            (number,)).fetchall()
        for key, receipt_json in rows:
            if receipt_json and json.loads(receipt_json).get("blockHash", "").lower() != block_hash.lower():
                _change_state(conn, key, "retracted")
    conn.execute("INSERT INTO ink_canonical_blocks(block_number,block_hash) VALUES (?,?) "
                 "ON CONFLICT(block_number) DO UPDATE SET block_hash=excluded.block_hash",
                 (number, block_hash.lower()))
    return True


def resolve_covered_gaps(conn):
    """A gap clears only when every block in its declared range has a full canonical read."""
    resolved = 0
    for start, reason, end in conn.execute(
            "SELECT block_number,reason,coalesce(end_block_number,block_number) "
            "FROM ink_stream_gaps WHERE resolved_at IS NULL").fetchall():
        count = conn.execute(
            "SELECT count(*) FROM ink_canonical_blocks WHERE block_number BETWEEN ? AND ?",
            (start, end)).fetchone()[0]
        if count == end - start + 1:
            conn.execute("UPDATE ink_stream_gaps SET resolved_at=datetime('now') "
                         "WHERE block_number=? AND reason=?", (start, reason))
            resolved += 1
    return resolved


def start_backfill(conn, start_block):
    start = int(start_block)
    if start < 0:
        raise ValueError("invalid Ink backfill start")
    conn.execute("INSERT OR IGNORE INTO ink_backfill_frontier(id,next_block) VALUES (1,?)",
                 (start,))
    return next_backfill_block(conn)


def next_backfill_block(conn):
    row = conn.execute("SELECT next_block FROM ink_backfill_frontier WHERE id=1").fetchone()
    return int(row[0]) if row else None


def canonical_block_hash(conn, block_number):
    row = conn.execute("SELECT block_hash FROM ink_canonical_blocks WHERE block_number=?",
                       (int(block_number),)).fetchone()
    return row[0] if row else None


def advance_backfill(conn, completed_block):
    block = int(completed_block)
    if not conn.execute("SELECT 1 FROM ink_canonical_blocks WHERE block_number=?",
                        (block,)).fetchone():
        return False
    return bool(conn.execute(
        "UPDATE ink_backfill_frontier SET next_block=? WHERE id=1 AND next_block=?",
        (block + 1, block)).rowcount)


def open_gaps(conn):
    return [{"block_number": row[0], "reason": row[1]} for row in conn.execute(
        "SELECT block_number,reason FROM ink_stream_gaps WHERE resolved_at IS NULL "
        "ORDER BY block_number,reason").fetchall()]


def has_open_gap_through(conn, block_number):
    return bool(conn.execute(
        "SELECT 1 FROM ink_stream_gaps WHERE resolved_at IS NULL AND block_number<=? LIMIT 1",
        (int(block_number),)).fetchone())
