#!/usr/bin/env python3
"""Office public reporting rollups (spec docs/superpowers/specs/2026-10-29-office-live-floor-ui.md, "Producer gaps"): a periodic job, never a web GET.

    nice -n 19 ionice -c3 python3 scripts/office_public_reporting.py [--office-db data/office/office.db] [--ledger-db data/office/ledger/ledger.db] [--desk-dir data/desk]

Writes ONLY two summary tables, from recorded facts only (unknown stays unknown, never 0, never a guess):
  office.db        agent_daily(utc_day, actor_id, generation, ...)   one row per UTC day, actor and generation (generation -1 = ledger facts, which carry no generation)
  ledger.db        equity_snapshots(book_id, at, ...)                one row per book per 5 minutes, written once (INSERT OR IGNORE); an unknown valuation is a row with NULL equity, never a skipped point
Reads are bounded and indexed: model_calls by (ended_at, call_id), handoff_events by (at, event_id), turn_runs by an expression index on created_at, decision_receipts by created_at and order_id, orders by
closed_at. THE SCRIPT CREATES NO INDEX: those indexes (REQUIRED_INDEXES below) are schema on tables other owners write, so the store owner adds them; until an index exists the source that needs it is
reported unknown (its columns NULL, source_heads names the missing index) and is never scanned. It opens no Office store (no hash chain is read or written).

Watermark (Codex's contract, runtime_observations.mark_consumed): every UTC day holding a raw row above consumed_seq is recomputed from the raw rows, then consumed_seq moves, in the SAME transaction as the
agent_daily upserts, to (lowest seq of any row dated today or later, or with no end time) - 1, or the table's max seq when there is none. A raw row at or below consumed_seq is therefore in a CLOSED UTC day already
rolled up, so retention can never release today's rows, and a late row for a closed day (higher seq) simply triggers a recompute of that day.
Retention: agent_daily and equity_snapshots keep 365 days. One instance at a time (flock). Day boundaries are host UTC."""
import argparse
import fcntl
import json
import re
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE_SHA = ROOT.name if len(ROOT.name) == 40 and all(ch in "0123456789abcdef" for ch in ROOT.name) else None      # the pinned release this job runs from (None outside one)
sys.path.insert(0, str(ROOT))

RETENTION_DAYS = 365
EQUITY_STEP_S = 300.0                     # one snapshot per book per 5 minutes
MARK_MAX_AGE_S, FX_MAX_AGE_S = 60.0, 120.0
OK_OUTCOMES = ("ok", "success")           # any other call outcome (including a vocabulary this job has not seen) counts as not ok, conservatively
GENERATION_UNKNOWN = -1

AGENT_DAILY = """CREATE TABLE IF NOT EXISTS agent_daily(
    utc_day TEXT NOT NULL, actor_id TEXT NOT NULL, generation INTEGER NOT NULL, epoch_id TEXT,
    turns_started INTEGER, turns_completed INTEGER, turns_partial INTEGER,
    calls INTEGER, calls_not_ok INTEGER, measured_cost_usd REAL, cost_unknown_n INTEGER,
    handoffs_out INTEGER, handoffs_in INTEGER, late_after_close INTEGER,
    decisions INTEGER, linked_closed_n INTEGER, wins_n INTEGER, realized_paper_pnl_usd REAL, pnl_unknown_n INTEGER, open_n INTEGER, overlap_n INTEGER,
    source_heads TEXT NOT NULL, covered_through REAL NOT NULL, missing_n INTEGER NOT NULL, written_at REAL NOT NULL);
-- epoch_id comes from the fact's own epoch identity (ledger facts: orders.epoch_id); a fact whose epoch is unknown (turns, calls, handoffs carry none) is epoch_id NULL and is never merged into a known epoch
CREATE UNIQUE INDEX IF NOT EXISTS agent_daily_key ON agent_daily(utc_day, actor_id, generation, COALESCE(epoch_id, ''));
CREATE INDEX IF NOT EXISTS agent_daily_actor_day ON agent_daily(actor_id, utc_day);
-- exactly-once late-row tracking: the highest raw seq already counted for a (day, table), independent of the held global watermark
CREATE TABLE IF NOT EXISTS agent_daily_late(utc_day TEXT NOT NULL, table_name TEXT NOT NULL, last_seq INTEGER NOT NULL, PRIMARY KEY(utc_day, table_name)) WITHOUT ROWID;"""
# Near-pass funnel (Gamma prereg near-pass-v1 section 3): one count per (UTC day, feed, step); n NULL = the source is unknown or its read was capped, never 0. Rates are computed at read time
# as n / the previous step's n (NULL when either is NULL). feed: all (step a only), pass, near_pass, unknown (a chain that could not be traced to a sheet).
FUNNEL_DAILY = """CREATE TABLE IF NOT EXISTS funnel_daily(
    utc_day TEXT NOT NULL, feed TEXT NOT NULL, step TEXT NOT NULL, n INTEGER, source_heads TEXT NOT NULL, covered_through REAL NOT NULL, written_at REAL NOT NULL,
    PRIMARY KEY(utc_day, feed, step)) WITHOUT ROWID;"""
REFUSED_TOKENS = """CREATE TABLE IF NOT EXISTS funnel_refused_token(
    utc_day TEXT NOT NULL, token TEXT NOT NULL, n INTEGER, share REAL, source_heads TEXT NOT NULL, covered_through REAL NOT NULL, written_at REAL NOT NULL,
    PRIMARY KEY(utc_day, token)) WITHOUT ROWID;"""
# One row per token: hard_filter tickets that meet every near-pass condition except ONE non-pass check whose reason is outside near-pass-allow-v1 or denied. token = that reason;
# n = such tickets; share = n / all such tickets of the day. Unknown day (no desk, cap, no rule): the single row token='*' with n and share NULL.
FUNNEL_STEPS = ("a_tickets", "b_eligible", "c_delivered", "d_sheet", "e_verdicts", "f_entries", "g_closes")
FUNNEL_CAP = 5000                          # rows read per day and step; more than this is reported unknown (NULL), never a partial count
EQUITY_SNAPSHOTS = """CREATE TABLE IF NOT EXISTS equity_snapshots(
    book_id TEXT NOT NULL, at REAL NOT NULL, bucket_key REAL NOT NULL, cash_usd REAL, known_liq_usd REAL, equity_usd REAL, priced_n INTEGER, unpriced_n INTEGER,
    fx_usd REAL, fx_at REAL, source_heads TEXT NOT NULL, state TEXT NOT NULL, fill_model_version TEXT NOT NULL, written_at REAL NOT NULL,
    PRIMARY KEY(book_id, at)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS equity_snapshots_at_book ON equity_snapshots(at, book_id);
CREATE UNIQUE INDEX IF NOT EXISTS equity_snapshots_bucket ON equity_snapshots(book_id, bucket_key);"""

# every read this job makes, kept as constants so the tests can EXPLAIN them (each must use an index)
Q_TURNS = ("SELECT json_extract(payload_json,'$.actor_id'), json_extract(payload_json,'$.generation'), json_extract(payload_json,'$.state'), COUNT(*) FROM turn_runs "
           "WHERE json_extract(payload_json,'$.created_at') >= ? AND json_extract(payload_json,'$.created_at') < ? GROUP BY 1, 2, 3")
Q_CALLS = ("SELECT actor_id, generation, COUNT(*), SUM(CASE WHEN outcome IN ('ok','success') THEN 0 ELSE 1 END), SUM(CASE WHEN cost_usd IS NULL THEN 0 ELSE cost_usd END), "
           "SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END), MAX(seq) FROM model_calls WHERE ended_at >= ? AND ended_at < ? GROUP BY actor_id, generation")
Q_HANDOFFS = "SELECT from_actor_id, to_actor_id, seq FROM handoff_events WHERE at >= ? AND at < ?"
Q_RECEIPTS = ("SELECT r.actor_id, r.order_id, r.created_at, o.epoch_id FROM decision_receipts r LEFT JOIN orders o ON o.order_id = r.order_id "
              "WHERE r.created_at >= ? AND r.created_at < ?")
Q_CLOSED = ("SELECT o.order_id, o.spend_usd, o.exit_usd, o.costs_usd, r.actor_id, o.epoch_id FROM orders o JOIN decision_receipts r ON r.order_id = o.order_id "
            "WHERE o.state = 'closed' AND o.closed_at >= ? AND o.closed_at < ? ORDER BY r.created_at, r.action_key")
# Inventory open AS OF the cut: filled before it and not closed before it = (a) the orders open now (orders_open) union (b) the orders closed at or after the cut (orders_closed_at); both are index-bounded
Q_OPEN = ("SELECT r.actor_id, r.order_id, r.created_at, r.action_key, o.epoch_id FROM orders o INDEXED BY orders_open JOIN decision_receipts r ON r.order_id = o.order_id "
          "WHERE o.state = 'open' AND o.filled_at < ? AND r.created_at < ?")
Q_OPEN_CLOSED_LATER = ("SELECT r.actor_id, r.order_id, r.created_at, r.action_key, o.epoch_id FROM orders o INDEXED BY orders_closed_at JOIN decision_receipts r ON r.order_id = o.order_id "
                       "WHERE o.state = 'closed' AND o.closed_at >= ? AND o.filled_at < ? AND r.created_at < ?")
# MIN(seq) with a plain filter lets SQLite walk the rowid from the start (unbounded): force the time index and take the min over its (bounded) matches
Q_FIRST_TODAY = "SELECT MIN(seq) FROM (SELECT seq FROM %(table)s INDEXED BY %(index)s WHERE %(col)s >= ?)"
Q_FIRST_NULL = "SELECT MIN(seq) FROM (SELECT seq FROM %(table)s INDEXED BY %(index)s WHERE %(col)s IS NULL)"
Q_NEW_SEQ = "SELECT %(t)s FROM %(table)s WHERE seq > ? ORDER BY seq LIMIT ?"
# The exact schema this job reads through, for the store owner (it is NOT created here). Producer indexes (Codex) are model_calls_ended_at_call_id and handoff_events_at_event_id.
REQUIRED_INDEXES = {
    "turn_runs_created_at": "CREATE INDEX IF NOT EXISTS turn_runs_created_at ON turn_runs(json_extract(payload_json,'$.created_at'))",
    "decision_receipts_created_at": "CREATE INDEX IF NOT EXISTS decision_receipts_created_at ON decision_receipts(created_at)",
    "decision_receipts_order": "CREATE INDEX IF NOT EXISTS decision_receipts_order ON decision_receipts(order_id)",
    "orders_closed_at": "CREATE INDEX IF NOT EXISTS orders_closed_at ON orders(closed_at)",
    "orders_open": "CREATE INDEX IF NOT EXISTS orders_open ON orders(book_id) WHERE state = 'open'",
    "orders_filled_at": "CREATE INDEX IF NOT EXISTS orders_filled_at ON orders(filled_at)",
    "receipts_sheet_at": "(office store: receipts expression index on $.clock_sample.corrected_utc, kind sheet)",
    "receipts_broker_at": "(office store: receipts expression index on $.cut.receipt_cut, tool desk.near_pass_list)",
    "model_calls_ended_at_call_id": "(Codex store: model_calls(ended_at, call_id))",
    "handoff_events_at_event_id": "(Codex store: handoff_events(at, event_id))"}
RAW = {"model_calls": "ended_at", "handoff_events": "at"}
RAW_ACTOR = {"model_calls": "actor_id, generation, outcome, cost_usd, NULL", "handoff_events": "from_actor_id, -1, NULL, NULL, to_actor_id"}
PRODUCER_INDEX = {"model_calls": "model_calls_ended_at_call_id", "handoff_events": "handoff_events_at_event_id"}
NEW_ROWS_LIMIT = 5000                      # raw rows scanned above the watermark per run (the rest next run)


def num(v):
    return type(v) in (int, float) and math.isfinite(v)


def day_of(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def day_bounds(day):
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    return start, start + 86400.0


def has_table(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def has_index(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone() is not None


def ensure_schema(office, ledger):
    """Idempotent and additive: the summary tables and their own indexes. No other table or index is touched."""
    office.executescript(AGENT_DAILY)
    office.executescript(FUNNEL_DAILY)
    office.executescript(REFUSED_TOKENS)
    ledger.executescript(EQUITY_SNAPSHOTS)


# ------------------------------------------------------------------------------------------------------------------ agent_daily
def _blank(**kw):
    row = dict(late_after_close=None, turns_started=None, turns_completed=None, turns_partial=None, calls=None, calls_not_ok=None, measured_cost_usd=None, cost_unknown_n=None,
               handoffs_out=None, handoffs_in=None, decisions=None, linked_closed_n=None, wins_n=None, realized_paper_pnl_usd=None, pnl_unknown_n=None,
               open_n=None, overlap_n=None)
    row.update(kw)
    return row


def rollup_day(office, ledger, day, *, now, today, frozen=(), existing=()):
    """-> ({(actor_id, generation, epoch_id): row}, heads, missing_n) for one UTC day, from the raw facts only. A source that does not exist leaves its columns NULL (unknown); an existing source with no rows is a known 0.
    Every read is capped at the coverage cut min(day_end, now): an event dated after the cut is not part of this rollup yet. A table in `frozen` (its raw coverage of this day is no longer fully retained) is not read at all."""
    lo, hi = day_bounds(day)
    hi = min(hi, now)
    rows, heads, missing = {}, {}, 0

    def row(actor, gen, epoch=None):
        return rows.setdefault((actor, gen, epoch or None), _blank())
    heads["release_sha"] = RELEASE_SHA
    yesterday = day_of(day_bounds(today)[0] - 1)
    read = set()                                                      # the sources read successfully (an empty read is a known 0, not unknown)
    def usable(conn, table, *indexes):
        """The source exists AND every index it is read through exists; a missing index is reported, and the source is unknown rather than scanned."""
        if not has_table(conn, table):
            return False
        gone = [i for i in indexes if not has_index(conn, i)]
        if gone:
            heads.setdefault("index_missing", []).extend(gone)
            return False
        return True
    if usable(office, "turn_runs", "turn_runs_created_at"):
        read.add("turns")
        for actor, gen, state, n in office.execute(Q_TURNS, (lo, hi)):
            if not isinstance(actor, str) or type(gen) is not int:
                missing += n                                  # a turn we cannot attribute is counted, never dropped silently
                continue
            r = row(actor, gen)
            r["turns_started"] = (r["turns_started"] or 0) + n
            r["turns_completed"] = (r["turns_completed"] or 0) + (n if state == "completed" else 0)
            r["turns_partial"] = (r["turns_partial"] or 0) + (n if state == "partial" else 0)
    if "model_calls" not in frozen and usable(office, "model_calls", "model_calls_ended_at_call_id"):
        read.add("calls")
        top = 0
        for actor, gen, n, bad, cost, unknown, seq in office.execute(Q_CALLS, (lo, hi)):
            r = row(actor, gen)
            r["calls"], r["calls_not_ok"], r["measured_cost_usd"], r["cost_unknown_n"] = n, bad, (cost if unknown < n else None), unknown
            top = max(top, seq)
        heads["model_calls_seq"] = top or None
        for (actor, gen, epoch), r in rows.items():             # every actor with other facts but no call that day: known 0 calls (calls carry no epoch: only the epoch-unknown row)
            if epoch is None and r["calls"] is None:
                r["calls"], r["calls_not_ok"], r["cost_unknown_n"] = 0, 0, 0
        missing += office.execute("SELECT COUNT(*) FROM model_calls WHERE ended_at IS NULL").fetchone()[0]       # in flight or end unknown: cannot be placed on a day
    if "handoff_events" not in frozen and usable(office, "handoff_events", "handoff_events_at_event_id"):
        read.add("handoffs")
        top = 0
        for out_actor, in_actor, seq in office.execute(Q_HANDOFFS, (lo, hi)):
            top = max(top, seq)
            for actor, key in ((out_actor, "handoffs_out"), (in_actor, "handoffs_in")):
                if isinstance(actor, str):
                    r = row(actor, GENERATION_UNKNOWN)
                    r[key] = (r[key] or 0) + 1
        heads["handoff_events_seq"] = top or None
    ledger_ok = ledger is not None
    if ledger_ok:
        ok_receipts = usable(ledger, "decision_receipts", "decision_receipts_created_at", "decision_receipts_order")
        ok_orders = usable(ledger, "orders", "orders_closed_at", "orders_open")             # both are evaluated so every missing index is reported
        ledger_ok = ok_receipts and ok_orders
    if ledger_ok:
        claimed = {}                                                    # order_id -> the FIRST actor credited (receipt order); a second actor's claim is an overlap, never a second credit
        for actor, order_id, _created, epoch in ledger.execute(Q_RECEIPTS, (lo, hi)):
            r = row(actor, GENERATION_UNKNOWN, epoch)
            r["decisions"] = (r["decisions"] or 0) + 1
        for order_id, spend, exit_usd, costs, actor, epoch in ledger.execute(Q_CLOSED, (lo, hi)):
            r = row(actor, GENERATION_UNKNOWN, epoch)
            for k in ("linked_closed_n", "wins_n", "pnl_unknown_n", "overlap_n"):
                r[k] = r[k] or 0
            if order_id in claimed:
                if claimed[order_id] != actor:
                    r["overlap_n"] += 1
                continue
            claimed[order_id] = actor
            r["linked_closed_n"] += 1
            if num(spend) and num(exit_usd) and num(costs):
                pnl = exit_usd - spend - costs
                r["realized_paper_pnl_usd"] = (r["realized_paper_pnl_usd"] or 0.0) + pnl
                r["wins_n"] += 1 if pnl > 0 else 0
            else:
                r["pnl_unknown_n"] += 1
        for r in rows.values():
            if r["decisions"] is None and (r["linked_closed_n"] is not None):
                r["decisions"] = 0
        read.add("ledger")
        if day in (today, yesterday):                                   # open inventory is causal at the cut; only today and yesterday (a bounded close range) compute it
            seen = set()
            candidates = list(ledger.execute(Q_OPEN, (hi, hi))) + list(ledger.execute(Q_OPEN_CLOSED_LATER, (hi, hi, hi)))
            for actor, order_id, _created, _key, epoch in sorted(candidates, key=lambda x: (x[2], x[3])):
                if order_id in seen:
                    continue
                seen.add(order_id)
                r = row(actor, GENERATION_UNKNOWN, epoch)
                r["open_n"] = (r["open_n"] or 0) + 1
        heads["receipts_max_created_at"] = ledger.execute("SELECT MAX(created_at) FROM decision_receipts").fetchone()[0]
    for key in existing:                                              # a stored row this recount did not produce (its open order closed, say) is recounted too: a fresh snapshot, so its zeros are written
        rows.setdefault(key, _blank())
    for (actor, gen, epoch), r in rows.items():
        if epoch is None:
            if "turns" in read:
                for k in ("turns_started", "turns_completed", "turns_partial"):
                    r[k] = r[k] or 0
            if "calls" in read:
                for k in ("calls", "calls_not_ok", "cost_unknown_n"):
                    r[k] = r[k] or 0
            if "handoffs" in read:
                for k in ("handoffs_out", "handoffs_in"):
                    r[k] = r[k] or 0
        elif "ledger" in read:                                        # calls, turns and handoffs carry no epoch: they stay NULL on a known-epoch row
            for k in ("decisions", "linked_closed_n", "wins_n", "pnl_unknown_n", "overlap_n"):
                r[k] = r[k] or 0
            if r["realized_paper_pnl_usd"] is None and not r["linked_closed_n"]:
                r["realized_paper_pnl_usd"] = 0.0
            if day in (today, yesterday):
                r["open_n"] = r["open_n"] or 0
    return rows, heads, missing


def affected_days(office, today):
    """-> (days, scanned, new): the UTC days that hold a raw row above its table's watermark (bounded scan), plus today and yesterday (their rows may carry a changed ledger side).
    scanned[table] = the last seq actually scanned when the scan hit NEW_ROWS_LIMIT, else None: the watermark must never pass a row this run did not read.
    new[table] = the scanned rows as (day, seq, at, actor_id, generation, outcome, cost, to_actor): a frozen day counts the ones above its last counted seq, a closed day records its highest seq."""
    days = {today, day_of(day_bounds(today)[0] - 1)}
    scanned, new = {}, {}
    for table, col in RAW.items():
        if not has_table(office, table) or not has_table(office, "runtime_observation_state") or not has_index(office, PRODUCER_INDEX[table]):
            continue
        row = office.execute("SELECT consumed_seq FROM runtime_observation_state WHERE table_name=?", (table,)).fetchone()
        consumed = row[0] if row else 0
        seen = list(office.execute("SELECT %s, seq, %s FROM %s WHERE seq > ? ORDER BY seq LIMIT ?" % (col, RAW_ACTOR[table], table), (consumed, NEW_ROWS_LIMIT)))
        bucket = new.setdefault(table, [])
        for at, seq, actor, gen, outcome, cost, to_actor in seen:
            if num(at):
                days.add(day_of(at))
                bucket.append((day_of(at), seq, at, actor, gen, outcome, cost, to_actor))
        scanned[table] = seen[-1][1] if len(seen) >= NEW_ROWS_LIMIT else None
    return sorted(days), scanned, new


def aggregate_late(entries, day, after_seq):
    """-> ({(actor, gen): e}, max_seq) over the entries of `day` with seq > after_seq."""
    out, top = {}, after_seq
    for kday, seq, _at, actor, gen, outcome, cost, to_actor in entries:
        if kday != day or seq <= after_seq:
            continue
        top = max(top, seq)
        blank = lambda: dict(n=0, bad=0, cost=0.0, cost_known=0, cost_unknown=0, out=0, inn=0)
        if isinstance(actor, str) and type(gen) is int:
            e = out.setdefault((actor, gen), blank())
            e["n"] += 1
            if outcome is None and cost is None and to_actor is not None:      # a handoff row
                e["out"] += 1
            else:
                e["bad"] += 0 if outcome in OK_OUTCOMES else 1
                if num(cost):
                    e["cost"] += cost
                    e["cost_known"] += 1
                else:
                    e["cost_unknown"] += 1
        if to_actor is not None and isinstance(to_actor, str):
            out.setdefault((to_actor, GENERATION_UNKNOWN), blank())["inn"] += 1
    return out, top


def frozen_tables(office, day):
    """The raw tables whose coverage of `day` is no longer fully retained: the producer pruned from a point after the day began (runtime_observation_state.retained_from). Recomputing such a day from the
    retained subset would shrink its counts, so it is frozen (its raw-derived columns keep their previous values) and a late row only counts toward late_after_close."""
    if not has_table(office, "runtime_observation_state"):
        return set()
    start = day_bounds(day)[0]
    out = set()
    for table, retained in office.execute("SELECT table_name, retained_from FROM runtime_observation_state"):
        if table in RAW and num(retained) and start < retained:
            out.add(table)
    return out


def new_consumed(office, table, today_start):
    """(lowest seq of any row dated today or later or with no end/event time) - 1, else the table's max seq, else None."""
    col = RAW[table]
    firsts = [office.execute(Q_FIRST_TODAY % dict(table=table, col=col, index=PRODUCER_INDEX[table]), (today_start,)).fetchone()[0],
              office.execute(Q_FIRST_NULL % dict(table=table, col=col, index=PRODUCER_INDEX[table])).fetchone()[0]]
    firsts = [x for x in firsts if x is not None]
    if firsts:
        return min(firsts) - 1
    return office.execute("SELECT MAX(seq) FROM %s" % table).fetchone()[0]


RAW_COLUMNS = ("calls", "calls_not_ok", "measured_cost_usd", "cost_unknown_n", "handoffs_out", "handoffs_in")
UPSERT = ("INSERT INTO agent_daily(utc_day, actor_id, generation, epoch_id, turns_started, turns_completed, turns_partial, calls, calls_not_ok, measured_cost_usd, cost_unknown_n, handoffs_out, handoffs_in, late_after_close, "
          "decisions, linked_closed_n, wins_n, realized_paper_pnl_usd, pnl_unknown_n, open_n, overlap_n, source_heads, covered_through, missing_n, written_at) "
          "VALUES(:utc_day,:actor_id,:generation,:epoch_id,:turns_started,:turns_completed,:turns_partial,:calls,:calls_not_ok,:measured_cost_usd,:cost_unknown_n,:handoffs_out,:handoffs_in,:late_after_close,"
          ":decisions,:linked_closed_n,:wins_n,:realized_paper_pnl_usd,:pnl_unknown_n,:open_n,:overlap_n,:source_heads,:covered_through,:missing_n,:written_at) "
          "ON CONFLICT(utc_day, actor_id, generation, COALESCE(epoch_id, '')) DO UPDATE SET "
          # A recounted day is a fresh snapshot: every column is overwritten (an empty successful read was written as 0 by rollup_day, NULL means the read failed or is unknown).
          # COALESCE (keep the previous value) applies only to a column group that is NOT recounted: a source frozen for this day (:keep_calls / :keep_handoffs) and open_n outside the two days that compute it (:keep_open).
          + ", ".join("%s=excluded.%s" % (c, c) for c in ("turns_started", "turns_completed", "turns_partial", "decisions", "linked_closed_n", "wins_n", "realized_paper_pnl_usd", "pnl_unknown_n", "overlap_n"))
          + ", " + ", ".join("%s=CASE WHEN :keep_calls THEN COALESCE(excluded.%s, agent_daily.%s) ELSE excluded.%s END" % (c, c, c, c) for c in ("calls", "calls_not_ok", "measured_cost_usd", "cost_unknown_n"))
          + ", " + ", ".join("%s=CASE WHEN :keep_handoffs THEN COALESCE(excluded.%s, agent_daily.%s) ELSE excluded.%s END" % (c, c, c, c) for c in ("handoffs_out", "handoffs_in"))
          + ", open_n=CASE WHEN :keep_open THEN COALESCE(excluded.open_n, agent_daily.open_n) ELSE excluded.open_n END"
          + ", late_after_close=COALESCE(agent_daily.late_after_close, 0) + COALESCE(excluded.late_after_close, 0)"
          + ", source_heads=excluded.source_heads, covered_through=excluded.covered_through, missing_n=excluded.missing_n, written_at=excluded.written_at")


def run_agent_daily(office, ledger, *, now):
    """One transaction: upsert every affected day, then move the watermarks. -> {'days': [...], 'rows': n, 'consumed': {...}}"""
    today = day_of(now)
    today_start = day_bounds(today)[0]
    days, scanned, new = affected_days(office, today)
    out = {"days": days, "rows": 0, "consumed": {}, "index_missing": []}
    office.execute("BEGIN IMMEDIATE")
    try:
        for day in days:
            frozen = frozen_tables(office, day)
            existing = [(a, g, e) for a, g, e in office.execute("SELECT actor_id, generation, epoch_id FROM agent_daily WHERE utc_day=?", (day,))]
            rows, heads, missing = rollup_day(office, ledger, day, now=now, today=today, frozen=frozen, existing=existing)
            covered = min(now, day_bounds(day)[1])
            out["index_missing"] = sorted(set(out["index_missing"]) | set(heads.get("index_missing", ())))
            if frozen:
                heads["frozen"] = sorted(frozen)
            increments = []
            for table in RAW:
                if not has_table(office, "agent_daily_late"):
                    break
                got = office.execute("SELECT last_seq FROM agent_daily_late WHERE utc_day=? AND table_name=?", (day, table)).fetchone()
                last = got[0] if got else 0
                if table in frozen:                                       # a late row for a frozen day cannot be recomputed from the pruned subset: it is counted ONCE (by seq, independent of the held
                    agg, top = aggregate_late(new.get(table, []), day, last)   # watermark), added exactly to the totals known at close, and late_after_close counts it
                    for (actor, gen), e in agg.items():                       # model_calls / handoff_events carry no epoch: the epoch-unknown row
                        r = rows.setdefault((actor, gen, None), _blank())
                        r["late_after_close"] = (r.get("late_after_close") or 0) + e["n"]
                        increments.append((table, actor, gen, e))
                elif day_bounds(day)[1] <= now:                           # a closed, fully retained day: this rollup counted every scanned row, so a later freeze must not count them again
                    _agg, top = aggregate_late(new.get(table, []), day, last)
                else:
                    continue
                if top > last:
                    office.execute("INSERT INTO agent_daily_late(utc_day, table_name, last_seq) VALUES(?,?,?) ON CONFLICT(utc_day, table_name) DO UPDATE SET last_seq=excluded.last_seq", (day, table, top))
            for (actor, gen, epoch), r in rows.items():
                office.execute(UPSERT, dict(r, utc_day=day, actor_id=actor, generation=gen, epoch_id=epoch, late_after_close=r.get("late_after_close"), keep_calls=int("model_calls" in frozen), keep_handoffs=int("handoff_events" in frozen), keep_open=int(day not in (today, day_of(today_start - 1))), source_heads=json.dumps(heads, sort_keys=True),
                                            covered_through=covered, missing_n=missing, written_at=now))
                out["rows"] += 1
            for table, actor, gen, e in increments:
                key = dict(d=day, a=actor, g=gen)
                if table == "model_calls":
                    office.execute("UPDATE agent_daily SET calls = calls + :n, calls_not_ok = calls_not_ok + :bad, cost_unknown_n = cost_unknown_n + :unk, "
                                   "measured_cost_usd = CASE WHEN :ck > 0 THEN COALESCE(measured_cost_usd, 0) + :cost ELSE measured_cost_usd END "
                                   "WHERE utc_day = :d AND actor_id = :a AND generation = :g AND epoch_id IS NULL AND calls IS NOT NULL", dict(key, n=e["n"], bad=e["bad"], unk=e["cost_unknown"], ck=e["cost_known"], cost=e["cost"]))
                else:
                    office.execute("UPDATE agent_daily SET handoffs_out = handoffs_out + :out, handoffs_in = handoffs_in + :inn "
                                   "WHERE utc_day = :d AND actor_id = :a AND generation = :g AND epoch_id IS NULL AND handoffs_out IS NOT NULL", dict(key, out=e["out"], inn=e["inn"]))
        if has_table(office, "runtime_observation_state"):
            from sniper.office import runtime_observations as RO
            for table in RAW:
                if has_table(office, table) and has_index(office, PRODUCER_INDEX[table]):
                    seq = new_consumed(office, table, today_start)
                    if seq is not None and scanned.get(table) is not None:
                        seq = min(seq, scanned[table])               # a backlog beyond the scan limit is read next run, never marked consumed unread
                    if seq:
                        RO.mark_consumed(office, table, seq)           # joins this transaction; monotonic; refuses a future seq
                        out["consumed"][table] = seq
        cut = day_of(now - RETENTION_DAYS * 86400.0)
        office.execute("DELETE FROM agent_daily WHERE utc_day < ?", (cut,))
        office.execute("DELETE FROM agent_daily_late WHERE utc_day < ?", (cut,))
        office.commit()
    except BaseException:
        office.rollback()
        raise
    return out


# ------------------------------------------------------------------------------------------------------------------ equity_snapshots
def read_fx(desk_dir, now):
    """(sol_usd, sol_at) from the universe worker's heartbeat (the value memecoin_desk_asof uses), or (None, None) when absent, unreadable, stale or invalid: an unknown rate is never a guess."""
    try:
        beat = json.loads((Path(desk_dir) / "universe.heartbeat").read_text())
        ts, usd, age = float(beat["ts"]), beat["sol_usd"], beat["sol_age_s"]
    except (OSError, ValueError, KeyError, TypeError):
        return None, None
    if not (num(usd) and usd > 0 and num(age) and 0 <= age <= FX_MAX_AGE_S and 0 <= now - ts <= FX_MAX_AGE_S):
        return None, None
    return float(usd), ts - age


def run_equity(ledger_rw, ledger_ro, *, now, desk_dir):
    """One snapshot per book at the 5-minute step, written once. Every valuation is computed on the READ-ONLY handle first; ONE short write transaction (the inserts and the retention delete) follows,
    so the writer lock is never held across ledger.equity. ledger.equity is causal at the `now` passed here (capital flows, cashflows and inventory at or before it: Beta 9b41d41c1), so this job keeps no guard of its own. -> number of rows written."""
    from sniper.office import ledger as L
    bucket = math.floor(now / EQUITY_STEP_S) * EQUITY_STEP_S          # one point per book per 5 minutes; `at` is the ACTUAL as-of time (never backdated into the bucket), so marks and FX are <= at
    at = now
    sol_usd, sol_at = read_fx(desk_dir, now)
    pending = []
    for (book_id,) in ledger_ro.execute("SELECT book_id FROM books ORDER BY book_id"):
        if ledger_rw.execute("SELECT 1 FROM equity_snapshots WHERE book_id=? AND bucket_key=?", (book_id, bucket)).fetchone():
            continue
        heads = {"release_sha": RELEASE_SHA, "marks_max_id": ledger_ro.execute("SELECT MAX(id) FROM marks").fetchone()[0],
                 "capital_flows_max_id": ledger_ro.execute("SELECT MAX(id) FROM capital_flows").fetchone()[0]}
        eq = L.equity(ledger_ro, now=now, mark_max_age_s=MARK_MAX_AGE_S, sol_usd=sol_usd, sol_at=sol_at, book_id=book_id)
        # the causal inventory at `now` (the same predicate ledger.equity values: filled by now and not closed by now), so priced + unpriced is exactly that inventory; never the current open-state count
        inventory = ledger_ro.execute("SELECT COUNT(*) FROM orders WHERE book_id=? AND filled_at IS NOT NULL AND filled_at<=? AND (closed_at IS NULL OR closed_at>?)", (book_id, now, now)).fetchone()[0]
        priced = inventory - eq.unpriced_count
        known = eq.state == "known"
        pending.append((book_id, at, bucket, eq.cash, eq.known_liq_lower, (eq.cash + eq.known_liq_lower) if known and eq.known_liq_lower is not None else None, priced, eq.unpriced_count,
                        sol_usd, sol_at, json.dumps(heads, sort_keys=True), eq.state, eq.fill_model_version, now))
    written = 0
    ledger_rw.execute("BEGIN IMMEDIATE")
    try:
        for row in pending:
            written += ledger_rw.execute(
                "INSERT OR IGNORE INTO equity_snapshots(book_id, at, bucket_key, cash_usd, known_liq_usd, equity_usd, priced_n, unpriced_n, fx_usd, fx_at, source_heads, state, fill_model_version, written_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row).rowcount
        ledger_rw.execute("DELETE FROM equity_snapshots WHERE at < ?", (now - RETENTION_DAYS * 86400.0,))
        ledger_rw.commit()
    except BaseException:
        ledger_rw.rollback()
        raise
    return written


# ------------------------------------------------------------------------------------------------------------------ near-pass funnel
def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


KILL_REASON = re.compile(r"(?:^|[:_])kill:")


def refusal_token(SH, row):
    """The one reason that keeps an otherwise near-pass-eligible ticket out, or None. Eligible except for exactly ONE check: market_data / token_safety / sell_probe pass, no kill anywhere,
    every other non-pass check allowed, and one check that is not (status other than pass/unknown, a reason outside near-pass-allow-v1, or a denied reason). Two or more such checks, a kill,
    or a failed required check is not 'one token' and returns None. Token = that check's reason ('malformed:<check>' when the check has none)."""
    if SH.near_pass_blockers(row) is not None or not isinstance(row.get("reason"), str) or not row["reason"].startswith("unknown:"):
        return None
    evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
    checks = evidence.get("checks") if isinstance(evidence.get("checks"), dict) else None
    if checks is None or KILL_REASON.search(row["reason"]) or any(isinstance(v, dict) and (v.get("status") == "kill" or KILL_REASON.search(str(v.get("reason")))) for v in checks.values()):
        return None                                                    # a kill anywhere (status or a kill: reason, whatever the status) is a different refusal, never a token miss
    if any(not isinstance(checks.get(k), dict) or checks[k].get("status") != "pass" or SH.NEAR_PASS_REFUSED.search(str(checks[k].get("reason"))) for k in SH.NEAR_PASS_REQUIRED):
        return None
    tokens = []
    for name, c in checks.items():
        if name in SH.NEAR_PASS_REQUIRED:
            continue
        reason = c.get("reason") if isinstance(c, dict) else None
        refused = isinstance(reason, str) and SH.NEAR_PASS_REFUSED.search(reason)
        ok = isinstance(c, dict) and (c.get("status") == "pass" and not refused or c.get("status") == "unknown" and not refused and name in SH.NEAR_PASS_ALLOWED and SH._allowed(name, reason))
        if not ok:
            tokens.append(reason if isinstance(reason, str) and reason else "malformed:%s" % name)
    return tokens[0][:120] if len(tokens) == 1 else None


def desk_counts(desk, lo, hi):
    """-> (a_tickets, pass_eligible, near_pass_eligible, refused_tokens {token: n} or None, heads): hard_filter tickets of [lo, hi) from the desk store through idx_desk_tickets_stage_ts. A cap or a missing index/table makes the dependent counts None."""
    heads = {}
    if desk is None or not has_table(desk, "desk_tickets") or not has_index(desk, "idx_desk_tickets_stage_ts"):
        heads["desk"] = "unavailable"
        return None, None, None, None, heads
    lo_s, hi_s = _iso(lo), _iso(hi)
    where = "stage='hard_filter' AND ts >= ? AND ts < ?"
    a = desk.execute("SELECT COUNT(*) FROM desk_tickets INDEXED BY idx_desk_tickets_stage_ts WHERE " + where, (lo_s, hi_s)).fetchone()[0]
    if a > FUNNEL_CAP:
        heads["desk"] = "capped"
        return a, None, None, None, heads
    passed = desk.execute("SELECT COUNT(*) FROM desk_tickets INDEXED BY idx_desk_tickets_stage_ts WHERE " + where + " AND verdict='PASS'", (lo_s, hi_s)).fetchone()[0]
    try:
        from sniper.office.tools import sheet as SH
    except Exception:                                                  # the eligibility rule is unavailable: unknown, not 0
        heads["near_pass_rule"] = "unavailable"
        return a, passed, None, None, heads
    eligible, refused = 0, {}
    for verdict, reason, inputs_json in desk.execute("SELECT verdict, reason, substr(inputs_json, 1, 65536) FROM desk_tickets INDEXED BY idx_desk_tickets_stage_ts WHERE " + where
                                                     + " AND verdict='SKIP' AND reason LIKE 'unknown:%'", (lo_s, hi_s)):
        try:
            inputs = json.loads(inputs_json)
        except (TypeError, ValueError, RecursionError):
            continue
        row = {"verdict": verdict, "reason": reason, "evidence": inputs.get("evidence") if isinstance(inputs, dict) else None}
        if SH.near_pass_blockers(row) is not None:
            eligible += 1
        else:
            token = refusal_token(SH, row)
            if token is not None:
                refused[token] = refused.get(token, 0) + 1
    return a, passed, eligible, refused, heads


def order_feeds(office, ledger, column, lo, hi):
    """-> ({feed: n}, heads): the orders whose `column` (filled_at | closed_at) falls in [lo, hi), each traced orders.decision_bundle_hash -> decision_bundles -> payload.receipt_row_hash -> receipts
    -> request.feed (a sheet with no feed key is pass). A chain that cannot be traced is feed 'unknown', never pass. None when a source index is missing or the day exceeds the cap."""
    index = {"filled_at": "orders_filled_at", "closed_at": "orders_closed_at"}[column]
    if not has_table(ledger, "orders") or not has_index(ledger, index) or not has_table(office, "decision_bundles") or not has_table(office, "receipts"):
        return None, {"%s_source" % column: "unavailable"}
    rows = ledger.execute("SELECT decision_bundle_hash FROM orders INDEXED BY %s WHERE %s >= ? AND %s < ? LIMIT ?" % (index, column, column), (lo, hi, FUNNEL_CAP + 1)).fetchall()
    if len(rows) > FUNNEL_CAP:
        return None, {"%s_source" % column: "capped"}
    out = {}
    for (bundle_hash,) in rows:
        feed = "unknown"
        got = office.execute("SELECT json_extract(payload_json,'$.receipt_row_hash') FROM decision_bundles WHERE row_hash=?", (bundle_hash,)).fetchone() if isinstance(bundle_hash, str) else None
        if got and isinstance(got[0], str):
            r = office.execute("SELECT json_extract(payload_json,'$.request.feed') FROM receipts WHERE row_hash=? AND json_extract(payload_json,'$.kind')='sheet'", (got[0],)).fetchone()
            if r is not None:
                feed = r[0] if r[0] in ("near_pass",) else ("pass" if r[0] is None else "unknown")
        out[feed] = out.get(feed, 0) + 1
    return out, {}


RECEIPT_INDEXES = ("receipts_sheet_at", "receipts_broker_at")      # timestamp expression indexes owned by sniper/office/store.py; absent = day unknown


def receipt_steps(office, lo, hi):
    """-> ({'c': distinct near-pass tickets delivered, 'd_pass': n, 'd_near_pass': n, 'd_unknown': n} or None, heads). Times live inside the payload (corrected UTC), rowid order is not time order,
    so each count is an indexed range on the timestamp expression over [lo, hi), capped at FUNNEL_CAP rows. Sheets: $.clock_sample.corrected_utc, feed $.request.feed (absent = pass, 'pass' and
    'near_pass' map to themselves, any other value = unknown). Deliveries: desk.near_pass_list receipts at $.cut.receipt_cut (basis corrected_utc), tickets $.result.items[*].ticket_id.
    Missing index or cap hit -> None (unknown), never a partial count."""
    if not has_table(office, "receipts"):
        return None, {"receipts": "unavailable"}
    have = {r[0] for r in office.execute("SELECT name FROM sqlite_master WHERE type='index' AND name IN (?,?)", RECEIPT_INDEXES)}
    if len(have) < len(RECEIPT_INDEXES):
        return None, {"receipts": "index_missing"}
    sheets = {"pass": 0, "near_pass": 0, "unknown": 0}
    rows = office.execute("SELECT json_extract(payload_json,'$.request.feed') FROM receipts INDEXED BY receipts_sheet_at WHERE json_extract(payload_json,'$.kind')='sheet' "
                          "AND json_extract(payload_json,'$.clock_sample.corrected_utc') >= ? AND json_extract(payload_json,'$.clock_sample.corrected_utc') < ? LIMIT ?", (lo, hi, FUNNEL_CAP + 1)).fetchall()
    if len(rows) > FUNNEL_CAP:
        return None, {"receipts": "cap_hit"}
    for (feed,) in rows:
        sheets["pass" if feed is None else feed if feed in ("pass", "near_pass") else "unknown"] += 1
    delivered = set()
    rows = office.execute("SELECT json_extract(payload_json,'$.result.items') FROM receipts INDEXED BY receipts_broker_at WHERE json_extract(payload_json,'$.tool')='desk.near_pass_list' "
                          "AND json_extract(payload_json,'$.cut.basis')='corrected_utc' AND json_extract(payload_json,'$.cut.receipt_cut') >= ? AND json_extract(payload_json,'$.cut.receipt_cut') < ? LIMIT ?",
                          (lo, hi, FUNNEL_CAP + 1)).fetchall()
    if len(rows) > FUNNEL_CAP:
        return None, {"receipts": "cap_hit"}
    for (items,) in rows:
        try:
            delivered.update(i["ticket_id"] for i in json.loads(items) if isinstance(i, dict) and isinstance(i.get("ticket_id"), str))
        except (TypeError, ValueError, RecursionError):
            pass
    return {"c": len(delivered), "d_pass": sheets["pass"], "d_near_pass": sheets["near_pass"], "d_unknown": sheets["unknown"]}, {}


def run_funnel(office, ledger, desk, *, now):
    """Recompute today and yesterday (fresh snapshots) and write them in one short transaction. Step e has no writer yet (unknown, NULL); the PASS feed has no delivery receipt (c unknown)."""
    today = day_of(now)
    days = [day_of(day_bounds(today)[0] - 1), today]
    rows, token_rows = [], []
    for day in days:
        lo, hi = day_bounds(day)
        hi = min(hi, now)
        heads = {"release_sha": RELEASE_SHA}
        a, passed, near, refused, desk_heads = desk_counts(desk, lo, hi)
        heads.update(desk_heads)
        entries, h1 = order_feeds(office, ledger, "filled_at", lo, hi) if ledger is not None else (None, {"ledger": "unavailable"})
        closes, h2 = order_feeds(office, ledger, "closed_at", lo, hi) if ledger is not None else (None, {"ledger": "unavailable"})
        heads.update(h1)
        heads.update(h2)
        total = None if refused is None else sum(refused.values())
        token_rows += ([(day, "*", None, None)] if refused is None else [(day, t, n, n / total) for t, n in sorted(refused.items())])
        steps, h3 = receipt_steps(office, lo, hi)
        heads.update(h3)
        cells = [("all", "a_tickets", a), ("pass", "b_eligible", passed), ("near_pass", "b_eligible", near)]
        cells += [("pass", "c_delivered", None), ("near_pass", "c_delivered", None if steps is None else steps["c"]),     # the PASS feed has no Scout delivery receipt: unknown
                  ("pass", "d_sheet", None if steps is None else steps["d_pass"]), ("near_pass", "d_sheet", None if steps is None else steps["d_near_pass"]), ("unknown", "d_sheet", None if steps is None else steps["d_unknown"]),
                  ("pass", "e_verdicts", None), ("near_pass", "e_verdicts", None)]                                         # no Judge verdict writer exists yet (Beta): unknown
        for step, counts in (("f_entries", entries), ("g_closes", closes)):
            for feed in ("pass", "near_pass", "unknown"):
                cells.append((feed, step, None if counts is None else counts.get(feed, 0)))
        rows += [(day, feed, step, n, json.dumps(heads, sort_keys=True), min(now, day_bounds(day)[1]), now) for feed, step, n in cells]
    office.execute("BEGIN IMMEDIATE")
    try:
        office.executemany("INSERT INTO funnel_daily(utc_day, feed, step, n, source_heads, covered_through, written_at) VALUES(?,?,?,?,?,?,?) "
                           "ON CONFLICT(utc_day, feed, step) DO UPDATE SET n=excluded.n, source_heads=excluded.source_heads, covered_through=excluded.covered_through, written_at=excluded.written_at", rows)
        office.execute("DELETE FROM funnel_daily WHERE utc_day < ?", (day_of(now - RETENTION_DAYS * 86400.0),))
        office.executemany("DELETE FROM funnel_refused_token WHERE utc_day = ?", [(d,) for d in days])     # a fresh snapshot: tokens that vanished must not linger
        office.executemany("INSERT INTO funnel_refused_token(utc_day, token, n, share, source_heads, covered_through, written_at) VALUES(?,?,?,?,'{}',?,?)",
                           [(d, t, n, sh, min(now, day_bounds(d)[1]), now) for d, t, n, sh in token_rows])
        office.execute("DELETE FROM funnel_refused_token WHERE utc_day < ?", (day_of(now - RETENTION_DAYS * 86400.0),))
        office.commit()
    except BaseException:
        office.rollback()
        raise
    return {"funnel_rows": len(rows)}


# ------------------------------------------------------------------------------------------------------------------ main
def run_once(office_db, ledger_db, desk_dir, *, now=None, desk_db=None):
    now = time.time() if now is None else now
    office = sqlite3.connect(str(office_db), timeout=30, isolation_level=None)
    ledger_rw = sqlite3.connect(str(ledger_db), timeout=30, isolation_level=None)
    ledger_ro = sqlite3.connect(Path(ledger_db).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    ledger_ro.row_factory = sqlite3.Row
    try:
        ensure_schema(office, ledger_rw)
        result = {"agent_daily": run_agent_daily(office, ledger_ro, now=now), "equity_rows": run_equity(ledger_rw, ledger_ro, now=now, desk_dir=desk_dir)}
        desk = sqlite3.connect(Path(desk_db).resolve().as_uri() + "?mode=ro", uri=True, timeout=30) if desk_db and Path(desk_db).is_file() else None
        try:
            result["funnel"] = run_funnel(office, ledger_ro, desk, now=now)
        finally:
            if desk is not None:
                desk.close()
    finally:
        for c in (office, ledger_rw, ledger_ro):
            c.close()
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    root = Path(os.environ.get("SNIPER_STATE_ROOT") or ROOT)
    ap.add_argument("--office-db", default=str(root / "data" / "office" / "office.db"))
    ap.add_argument("--ledger-db", default=str(root / "data" / "office" / "ledger" / "ledger.db"))
    ap.add_argument("--desk-dir", default=str(root / "data" / "desk"))
    ap.add_argument("--desk-db", default=None, help="default: memecoin_desk.db in the parent of --desk-dir (data/desk -> data/memecoin_desk.db)")
    a = ap.parse_args(argv)
    if a.desk_db is None:
        a.desk_db = str(Path(a.desk_dir).parent / "memecoin_desk.db")      # derived from the state the job was pointed at, never from the pinned worktree
    for path in (a.office_db, a.ledger_db, a.desk_db):
        if not Path(path).is_file():
            print(json.dumps({"error": "missing_db", "which": Path(path).name}))
            return 2                                              # never creates a database, and never runs the funnel blind (desk=None) by default
    lock = open(Path(a.office_db).parent / "reporting.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 75                                                 # another instance is running
    result = run_once(a.office_db, a.ledger_db, a.desk_dir, desk_db=a.desk_db)
    print(json.dumps(result, sort_keys=True, default=str))
    if result["agent_daily"]["index_missing"]:                    # the cron log says why a source is unknown: the store owner has not created these indexes yet
        print("office_public_reporting: sources unknown, index missing: " + ", ".join(result["agent_daily"]["index_missing"]), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
