"""Helius history backfill: swap storage (spec 2026-10-27-helius-history-backfill-design.md section 3). Offline logic only.

Every atomic amount and reserve is DECIMAL TEXT (never a float or JSON number: values above 2^53 must round-trip). A swap is unique GLOBALLY by
(signature, instruction_index, inner_index, event_ordinal) across all day files, through one seen.db. Day files are keyed by the UTC day of block_time.
Rows are `source='helius_history'`, carry block_time (not our receipt time) and are never written into the own-tape databases."""
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SOURCE = "helius_history"
KEY = ("signature", "instruction_index", "inner_index", "event_ordinal")
AMOUNTS = ("quote_atomic", "token_atomic", "pre_base", "pre_quote", "post_base", "post_quote", "fee_lp", "fee_protocol", "fee_creator")
COLS = ("network_id", "venue", "pool", "mint", "signature", "instruction_index", "inner_index", "event_ordinal", "slot", "tx_index", "block_time", "is_buy", "actor", "quote_mint",
        "quote_atomic", "token_atomic", "pre_base", "pre_quote", "post_base", "post_quote", "fee_lp", "fee_protocol", "fee_creator", "fee_bps", "source", "fetched_at")
_DEC = re.compile(r"^(0|[1-9][0-9]*)$")                    # unsigned canonical decimal: a negative reserve/quantity/fee is unknown, never a feature
TOP_LEVEL = -1                                                 # inner_index sentinel for a top-level instruction (not an inner one)
DDL = """create table if not exists swaps_hist(%s, primary key(signature, instruction_index, inner_index, event_ordinal));
create index if not exists swaps_hist_pool on swaps_hist(pool, slot, tx_index, instruction_index, inner_index, event_ordinal);""" % ", ".join(
    "%s %s" % (c, "TEXT" if c in AMOUNTS or c in ("network_id", "venue", "pool", "mint", "signature", "actor", "quote_mint", "source") else "INTEGER" if c != "fetched_at" else "REAL") for c in COLS)


class StoreConflict(Exception):
    """The same event identity arrived with a different day or payload than the one already claimed."""


def _amount(name, v):
    if v is None:
        return None                                             # absent stays NULL, never 0
    if isinstance(v, bool) or isinstance(v, float):
        raise ValueError("%s must be an integer or decimal string, got %r" % (name, v))
    s = str(v)
    if not _DEC.match(s):
        raise ValueError("%s is not an unsigned decimal integer: %r" % (name, v))
    return s


def _loc(name, v, lo):
    if isinstance(v, bool) or not isinstance(v, int) or v < lo:
        raise ValueError("%s must be an integer >= %d, got %r" % (name, lo, v))
    return v


class Store:
    def __init__(self, base_dir):
        self.dir = Path(base_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.seen = sqlite3.connect(self.dir / "seen.db", timeout=30, isolation_level=None)
        self.seen.execute("create table if not exists claims(signature text not null, instruction_index integer not null, inner_index integer not null, event_ordinal integer not null, "
                          "day text not null, payload_hash text not null, state text not null check(state in ('pending','done')), "
                          "primary key(signature, instruction_index, inner_index, event_ordinal))")

    def _day(self, block_time):
        return datetime.fromtimestamp(int(block_time), timezone.utc).date().isoformat()

    def pending(self):
        """Claims whose day write was not finalized. A pool must not be marked complete while any claim for it is pending."""
        return self.seen.execute("select signature, instruction_index, inner_index, event_ordinal, day from claims where state='pending'").fetchall()

    def _write_day(self, day, r):
        con = sqlite3.connect(self.dir / ("%s.db" % day), timeout=30)
        try:
            con.execute("pragma journal_mode=wal")
            con.executescript(DDL)
            cur = con.execute("insert or ignore into swaps_hist(%s) values(%s)" % (",".join(COLS), ",".join("?" * len(COLS))), [r.get(c) for c in COLS])
            con.commit()
            return cur.rowcount == 1
        finally:
            con.close()

    def _mark_done(self, key):
        self.seen.execute("update claims set state='done' where signature=? and instruction_index=? and inner_index=? and event_ordinal=?", key)

    def insert_swap(self, row):
        """True if THIS call wrote the day row (including a recovery of a crashed earlier attempt), False if it was already stored. Raises ValueError on a bad field and
        StoreConflict when the same identity arrives with another day or payload. The claim (day + payload hash) is durable BEFORE the day file is touched."""
        r = dict(row, source=SOURCE)
        for k, lo in (("instruction_index", 0), ("inner_index", TOP_LEVEL), ("event_ordinal", 0), ("slot", 0), ("tx_index", 0), ("block_time", 0)):
            _loc(k, r.get(k), lo)
        if not isinstance(r.get("signature"), str) or not r["signature"]:
            raise ValueError("missing signature")
        for a in AMOUNTS:
            r[a] = _amount(a, r.get(a))
        key = (r["signature"], r["instruction_index"], r["inner_index"], r["event_ordinal"])
        day = self._day(r["block_time"])
        digest = hashlib.sha256(json.dumps({c: r.get(c) for c in COLS if c != "fetched_at"}, sort_keys=True, default=str).encode()).hexdigest()
        self.seen.execute("begin immediate")
        try:
            got = self.seen.execute("select day, payload_hash, state from claims where signature=? and instruction_index=? and inner_index=? and event_ordinal=?", key).fetchone()
            if got is None:
                self.seen.execute("insert into claims values(?,?,?,?,?,?, 'pending')", key + (day, digest))
            elif (got[0], got[1]) != (day, digest):
                raise StoreConflict("identity %r already claimed for %s/%s" % (key, got[0], got[1][:8]))
        except BaseException:
            self.seen.execute("rollback")
            raise
        self.seen.execute("commit")
        if got is not None and got[2] == "done":
            return False
        wrote = self._write_day(day, r)                          # a pending claim (crash earlier) re-runs this write; insert-or-ignore makes it idempotent
        self._mark_done(key)
        return wrote
