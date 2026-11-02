"""Office Task 11 adapter (Lambda): the memory core over the Office store (Alpha Option B: office memory lives ONLY in office.db; no write to the crew brain). Tables memory_entries and memory_snapshots are Epsilon's
append-only hash chains (store.py); this module is the domain layer: validation, field mapping, the SQL read paths and the sealed-snapshot persistence.
Identity: entry_id IS the row's row_hash (content-addressed: payload + prev chain + the store-stamped known_at). Core field `section` is the payload `namespace`, core `text` is `content`.
known_at is stamped by memory.write from the trusted clock and bound by the chain; the cut is applied in SQL through the known_at index. Retrieval filters (cut, supersession, same-source, pitch barrier, quarantine) run in the
core BEFORE ranking; rank() orders only the pre-filtered pool by bm25 through FTS5 (a JSON id-set CTE), so a top-k can never be starved by invisible rows. No raw SQL is exposed to agents."""
import hashlib
import json
import math
import re

from sniper.office import memory as M
from sniper.office import store as S

NAMESPACES = frozenset({"office/lessons", "office/playbook", "office/notes", "office/practice"})
KINDS = frozenset({"lesson", "playbook", "note", "practice", "pitch"})
MAX_CONTENT = 8000
_TOKEN = re.compile(r"\w+", re.UNICODE)


def _validate(row):
    bad = []
    if row.get("section") not in NAMESPACES:
        bad.append("namespace")
    if row.get("kind") not in KINDS:
        bad.append("kind")
    t = row.get("text")
    if not isinstance(t, str) or not t.strip() or len(t) > MAX_CONTENT:
        bad.append("content")
    if not isinstance(row.get("author"), str) or not row["author"].strip():
        bad.append("author")
    k = row.get("known_at")
    if isinstance(k, bool) or not isinstance(k, (int, float)) or not math.isfinite(k):
        bad.append("known_at")
    if not isinstance(row.get("source_trade_ids", []), list) or not all(isinstance(x, str) for x in row.get("source_trade_ids", [])):
        bad.append("source_trade_ids")
    if not isinstance(row.get("sources", []), list) or not all(isinstance(x, dict) for x in row.get("sources", [])):
        bad.append("sources")
    if row.get("kind") == "pitch" and not row.get("decision_id"):
        bad.append("decision_id")
    if not isinstance(row.get("evaluation_derived", False), bool):
        bad.append("evaluation_derived")
    v = row.get("version", 1)
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        bad.append("version")
    if bad:
        raise M.Refused("invalid memory entry: " + ",".join(bad))


def _to_payload(row):
    p = {k: v for k, v in row.items() if k not in ("section", "text", "entry_id")}
    p.update(namespace=row["section"], content=row["text"])
    p.setdefault("source_trade_ids", [])
    p.setdefault("sources", [])
    p.setdefault("evaluation_derived", False)
    p.setdefault("version", 1)
    return p


def _verified(conn, table, seq, payload_json, prev_hash, row_hash):
    """Bounded per-SELECTED-row integrity (no history scan): the stored payload is canonical, sha256(payload + prev_hash) equals the stored row_hash, and prev_hash is the row_hash of the row just before it (one PK lookup).
    A row that fails is REFUSED before it is converted, returned, ranked or sealed: verify_chain on a copy would catch it too, but the live read must not trust a stored hash it did not recompute."""
    try:
        canon = S.canonical_json(json.loads(payload_json)).decode("utf-8")
    except (ValueError, TypeError):
        raise M.Refused("%s row %s is not valid canonical JSON" % (table, seq))
    if canon != payload_json or hashlib.sha256(canon.encode("utf-8") + prev_hash.encode("ascii")).hexdigest() != row_hash:
        raise M.Refused("%s row %s failed its hash check (payload/prev_hash/row_hash)" % (table, seq))
    if seq == 1:
        ok = prev_hash == ""
    else:
        before = conn.execute("SELECT row_hash FROM %s WHERE seq = ?" % table, (seq - 1,)).fetchone()
        ok = before is not None and before[0] == prev_hash
    if not ok:
        raise M.Refused("%s row %s is not linked to its predecessor" % (table, seq))


def _from_row(seq, payload_json, row_hash):
    p = json.loads(payload_json)
    e = {k: v for k, v in p.items() if k not in ("namespace", "content")}
    e.update(section=p["namespace"], text=p["content"], entry_id=row_hash)
    return e


class OfficeMemory:
    """The core's backend protocol over an open Office store connection (office.db, not evaluation.db)."""

    def __init__(self, conn):
        self.conn = conn

    def _row(self, r):
        seq, payload_json, prev_hash, row_hash = r
        _verified(self.conn, "memory_entries", seq, payload_json, prev_hash, row_hash)
        return _from_row(seq, payload_json, row_hash)

    def entries(self, section=None, cut=None):
        sql, args = "SELECT seq, payload_json, prev_hash, row_hash FROM memory_entries WHERE 1=1", []
        if cut is not None:
            sql += " AND known_at <= ?"
            args.append(float(cut))
        if section is not None:
            sql += " AND namespace = ?"
            args.append(section)
        order = " ORDER BY known_at, seq" if section is None else " ORDER BY namespace, known_at, seq"          # matches the (known_at, seq) / (namespace, known_at, seq) indexes: a range SEARCH, never a table SCAN
        return [self._row(r) for r in self.conn.execute(sql + order, args)]

    def get(self, ids):
        ids = list(dict.fromkeys(ids))
        if not ids:
            return {}
        out = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for r in self.conn.execute("SELECT seq, payload_json, prev_hash, row_hash FROM memory_entries WHERE row_hash IN (%s)" % ",".join("?" * len(chunk)), chunk):
                e = self._row(r)
                out[r[3]] = e
        return out

    def add(self, row):
        _validate(row)
        with S.write_transaction(self.conn):
            if row.get("supersedes"):
                old = self.get([row["supersedes"]]).get(row["supersedes"])
                if old is None or old["section"] != row["section"]:
                    raise M.Refused("supersedes an entry that does not exist in this namespace")
            return S.append(self.conn, "memory_entries", _to_payload(row))

    def rank(self, query, pool):
        """bm25 order of the PRE-FILTERED pool (FTS5 over content); entries with no term match are not relevant and are dropped. An empty query returns the newest first."""
        if not pool:
            return []
        ids = [e["entry_id"] for e in pool]
        toks = _TOKEN.findall(query or "")
        if not toks:
            return sorted(pool, key=lambda e: (-e["known_at"], e["entry_id"]))
        match = " OR ".join('"%s"' % t.replace('"', "") for t in toks)
        rows = self.conn.execute(
            "WITH visible(rh) AS (SELECT value FROM json_each(?)) "
            "SELECT e.row_hash FROM memory_entries_fts JOIN memory_entries e ON e.seq = memory_entries_fts.rowid JOIN visible v ON v.rh = e.row_hash "
            "WHERE memory_entries_fts MATCH ? ORDER BY bm25(memory_entries_fts), e.seq", (json.dumps(ids), match)).fetchall()
        by = {e["entry_id"]: e for e in pool}
        return [by[r[0]] for r in rows if r[0] in by]

    def put_snapshot(self, snap):
        """Trusted sealer only: one row per snapshot_id (unique); idempotent."""
        with S.write_transaction(self.conn):
            if S.latest(self.conn, "memory_snapshots", "snapshot_id", snap["snapshot_id"]) is None:
                S.append(self.conn, "memory_snapshots", dict(snap))
        return snap["snapshot_id"]

    def get_snapshot(self, snapshot_id):
        r = S.latest(self.conn, "memory_snapshots", "snapshot_id", snapshot_id)
        if r is None:
            return None
        _verified(self.conn, "memory_snapshots", r["seq"], r["payload_json"], r["prev_hash"], r["row_hash"])
        return json.loads(r["payload_json"])


def seal(mem, *, epoch_id, cut):
    """Seal and persist (idempotent for the same epoch/cut/membership). -> snapshot_id"""
    return mem.put_snapshot(M.seal_snapshot(mem, epoch_id=epoch_id, cut=cut))


def retrieve_sealed(mem, query, *, snapshot_id, decision_cut, candidate, viewer_role, **kw):
    """Retrieve from a persisted snapshot: the snapshot dict is loaded from the chain by its trusted id, never taken from the caller."""
    snap = mem.get_snapshot(snapshot_id)
    if snap is None:
        raise M.Refused("unknown sealed snapshot %s" % snapshot_id)
    return M.retrieve(mem, query, decision_cut=decision_cut, candidate=candidate, viewer_role=viewer_role, snapshot=snap, trusted_snapshot_id=snapshot_id, **kw)
