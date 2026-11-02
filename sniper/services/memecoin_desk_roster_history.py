"""Desk-side append-only history of the memecoin roster's state (cohort C prereg b90ddeb59, Amendment 1 226c0144e).

memecoin_wallets keeps only the current paused/excluded/real_approved/real_chains; the writers record no history,
so a past cut's state is unknowable from it. The snapshot worker polls it read-only every POLL_S seconds and
appends a row here only when a wallet's state differs from its last stored row, plus one baseline row per wallet
at its first poll (T). It never writes the main ledger and is never on an entry path.

A row's `at` is our observation time, which is at or after the true change: an as-of read errs late.
state_at() answers from rows with at <= cut only, and returns unknown when:
  - the cut is before T (grade_asof_unknown);
  - the last successful poll at or before the cut is more than GAP_S old (roster_unknown, heartbeat gap);
  - the wallet's state changed within LAG_S before the cut (roster_unknown).
"""
from __future__ import annotations

import sqlite3

POLL_S = 30.0
LAG_S = 30.0       # roster_state_lag_s: one poll interval
GAP_S = 90.0
FIELDS = ("paused", "excluded", "real_approved", "real_chains")
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS roster_events(source_user_id TEXT NOT NULL, at REAL NOT NULL, kind TEXT NOT NULL, "
    "paused INTEGER, excluded INTEGER, real_approved INTEGER, real_chains TEXT)",
    "CREATE INDEX IF NOT EXISTS roster_events_uid_at ON roster_events(source_user_id, at)",
    "CREATE TABLE IF NOT EXISTS roster_polls(at REAL PRIMARY KEY, wallets INTEGER NOT NULL)",
)


def open_store(path):
    conn = sqlite3.connect(str(path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    for ddl in SCHEMA:
        conn.execute(ddl)
    conn.commit()
    return conn


def open_ro(path):
    """A read-only handle for readers (the JUDGE sheet). Raises sqlite3.OperationalError when the store is absent."""
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=0.5)


def last_states(store):
    """{uid: state tuple or None (removed)} from the latest stored row per wallet."""
    rows = store.execute("SELECT e.source_user_id, e.kind, e.paused, e.excluded, e.real_approved, e.real_chains "
                         "FROM roster_events e JOIN (SELECT source_user_id, MAX(rowid) r FROM roster_events "
                         "GROUP BY source_user_id) m ON e.rowid = m.r").fetchall()
    return {uid: None if kind == "removed" else tuple(rest) for uid, kind, *rest in rows}


def poll_once(store, current, at, last=None):
    """Append baseline/change/removed rows for `current` ({uid: state tuple}) observed at `at`.
    Returns the new last-state map. One transaction; the poll row is written with the events."""
    last = last_states(store) if last is None else last
    baseline = not store.execute("SELECT 1 FROM roster_polls LIMIT 1").fetchone()
    rows = []
    for uid, state in current.items():
        if baseline:
            rows.append((uid, at, "baseline", *state))
        elif last.get(uid) != state:
            rows.append((uid, at, "change", *state))
    if not baseline:
        rows += [(uid, at, "removed", None, None, None, None) for uid, s in last.items()
                 if s is not None and uid not in current]
    with store:
        store.executemany("INSERT INTO roster_events VALUES(?,?,?,?,?,?,?)", rows)
        store.execute("INSERT INTO roster_polls VALUES(?,?)", (at, len(current)))
    return {**last, **{r[0]: None if r[2] == "removed" else tuple(r[3:]) for r in rows}}


def history_reason(store, cut):
    """None when the history covers `cut`; else grade_asof_unknown (before T) or roster_unknown (poll gap)."""
    first = store.execute("SELECT MIN(at) FROM roster_polls").fetchone()[0]
    if first is None or cut < first:
        return "grade_asof_unknown"
    poll = store.execute("SELECT MAX(at) FROM roster_polls WHERE at <= ?", (cut,)).fetchone()[0]
    return "roster_unknown" if cut - poll > GAP_S else None


def state_at(store, uid, cut):
    """(state dict, None) known at `cut`, or (None, reason). Reads only rows with at <= cut."""
    reason = history_reason(store, cut)
    if reason:
        return None, reason
    row = store.execute("SELECT at, kind, paused, excluded, real_approved, real_chains FROM roster_events "
                        "WHERE source_user_id=? AND at <= ? ORDER BY at DESC, rowid DESC LIMIT 1", (uid, cut)).fetchone()
    if row is None or row[1] == "removed":
        return None, "not_on_roster"
    if row[1] != "baseline" and cut - row[0] < LAG_S:
        return None, "roster_unknown"
    return dict(zip(FIELDS, row[2:])), None
