"""Which slot a funded entry landed in, and why not S+1 (operator via Alpha, 2026-10-02: "we need
to know every time what slot we filled and see what went wrong").

Spec docs/superpowers/specs/2026-10-02-memecoin-fill-timing-design.md (rev 2). Pure: `facts` merges
what is stored for one submitted attempt (its trace, the on-chain detection, the slot lane's attempt
for the same signature, the slot lane's skip row, the EVM fast lane's decision, the resolver's row);
`gap_of`, `stages`, `classify` and `landed_text` read only that. The resolver and every view use the
same functions, so the alert and the stored row cannot disagree.

Absent is None, never 0: a gap needs both slots. Solana slots and EVM blocks share the slot fields and
are always read with `network_id`.

The timing file `data/memecoin_fill_timing.db` holds `memecoin_fill_timing` and
`memecoin_slot_lane_skips`, never the ledger, so its writers never take the ledger's write lock.
Readers use `open_readonly()`, their own URI connection: an ATTACH from a connection not opened with
uri=True would make the URI a literal, writable filename (Gamma LOW1, trap 984bcf0a).
"""
from __future__ import annotations

import json
import math
import re
import logging
import queue
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from sniper.util.receipt_clock import elapsed_receipt_ms

# The latest landing that counts as on time, in slots (Solana) or blocks (EVM). A copy cannot land in
# the source's own next block on 4663 (about 0.12 s blocks), so 4663 allows about 1 s (Gamma Q2(c)).
ON_TIME = {"solana": 1, "8453": 1, "56": 2, "5042": 2, "4663": 8}
# Rank elapsed observations only; feed-head slot distances are separate diagnostics.
FALLBACK_LANE = "onchain_fallback_after_slot_lane"
MANUAL_SOURCES = ("manual", "manual_buy", "operator")      # no source buy: nothing to be late for
# The EVM fast lanes ("rhfast:<chain>:<tx>:<mint>", "bscfast:<chain>:<tx>:<mint>"): their attempt IS
# the fast lane, so it is never "fast_lane_skipped", and its trace holds its own source block (the
# lane's `seq`), send and receipt (R8, Codex review 2026-10-06).
FAST_LANE_SOURCES = ("rh_fast_lane", "bsc_fast_lane")
_BSC_SHARED_FAST_LANES = ("bsc_v3_fast_lane", "bsc_flap_fast_lane")
_FAST_ORIGINS = ("rhfast:", "bscfast:")
_EVM_TX = re.compile(r"0x[0-9a-f]{64}")
HEAD_FRESH_MS = 2000.0      # an older head at the decision is not a recv-S measurement
_PRICE_WORDS = ("slippage", "price", "chase", "premium", "min_out", "too little received")

SCHEMA = """
CREATE TABLE IF NOT EXISTS memecoin_fill_timing (
    attempt_id TEXT PRIMARY KEY, network_id TEXT, lane TEXT, source_sig TEXT,
    source_slot INTEGER, source_slot_basis TEXT, source_time REAL,
    landing_slot INTEGER, landing_slot_basis TEXT, landing_time REAL,
    recv_slot INTEGER, gap INTEGER, gap_s REAL, cause TEXT, cause_detail TEXT, stages_json TEXT,
    state TEXT NOT NULL, reads INTEGER NOT NULL DEFAULT 0, first_seen_at TEXT, updated_at TEXT,
    completed_at TEXT, followup_sent INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_fill_timing_state ON memecoin_fill_timing(state, updated_at);
CREATE INDEX IF NOT EXISTS idx_fill_timing_completed ON memecoin_fill_timing(completed_at);
CREATE INDEX IF NOT EXISTS idx_fill_timing_incomplete_completed ON memecoin_fill_timing(updated_at)
    WHERE state='complete' AND gap IS NULL;
CREATE TABLE IF NOT EXISTS memecoin_slot_lane_skips (
    signature TEXT PRIMARY KEY, outcome TEXT, reason TEXT, noted_at TEXT);
CREATE TABLE IF NOT EXISTS memecoin_fill_timing_cursor (
    name TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS memecoin_watcher_no_row (
    signature TEXT NOT NULL, owner TEXT NOT NULL, source_user_id TEXT, feed TEXT, reason TEXT,
    noted_at TEXT, PRIMARY KEY (signature, owner));
CREATE TABLE IF NOT EXISTS memecoin_fast_lane_notes (
    network_id TEXT, source_sig TEXT, mint TEXT, reason TEXT, source_slot INTEGER,
    source_slot_basis TEXT, PRIMARY KEY (network_id, source_sig, mint));
CREATE TABLE IF NOT EXISTS memecoin_signal_receipts (
  producer_run_id TEXT NOT NULL, sequence INTEGER NOT NULL,
  network_id TEXT NOT NULL, source_sig TEXT, source_wallet TEXT,
  mint TEXT, pool TEXT, venue TEXT, feed TEXT,
  recv_monotonic REAL, boot_id TEXT NOT NULL, clock_domain TEXT NOT NULL,
  connection_generation INTEGER NOT NULL,
  roster_generation TEXT, config_generation TEXT, decode_state TEXT NOT NULL,
  evidence_json TEXT NOT NULL, PRIMARY KEY (producer_run_id, sequence));
CREATE INDEX IF NOT EXISTS idx_signal_receipt_identity
  ON memecoin_signal_receipts(network_id, source_sig, source_wallet, mint);
CREATE TABLE IF NOT EXISTS memecoin_processed_receipts (
  producer_run_id TEXT NOT NULL, sequence INTEGER NOT NULL,
  network_id TEXT NOT NULL, our_sig TEXT NOT NULL, slot INTEGER NOT NULL,
  error_state TEXT NOT NULL, recv_monotonic REAL NOT NULL,
  boot_id TEXT NOT NULL, clock_domain TEXT NOT NULL,
  connection_generation INTEGER NOT NULL, evidence_json TEXT NOT NULL,
  PRIMARY KEY (producer_run_id, sequence));
CREATE INDEX IF NOT EXISTS idx_processed_receipt_identity
  ON memecoin_processed_receipts(network_id, our_sig, recv_monotonic);
CREATE TABLE IF NOT EXISTS memecoin_execution_coverage (
  producer_run_id TEXT NOT NULL, sequence INTEGER NOT NULL,
  component TEXT NOT NULL, reason TEXT NOT NULL,
  connection_generation INTEGER NOT NULL, mono_start REAL, mono_end REAL, lost_count INTEGER,
  evidence_json TEXT NOT NULL, PRIMARY KEY (producer_run_id, sequence));
CREATE TABLE IF NOT EXISTS memecoin_execution_observations (
    generation TEXT NOT NULL, attempt_id TEXT NOT NULL, tx_hash TEXT NOT NULL,
    dispatch_json TEXT, first_success_json TEXT, booking_json TEXT,
    observation_state TEXT NOT NULL, validation_state TEXT NOT NULL DEFAULT 'pending',
    PRIMARY KEY (generation, attempt_id, tx_hash));
CREATE TABLE IF NOT EXISTS memecoin_execution_observer_events (
    generation TEXT NOT NULL, sequence INTEGER NOT NULL, kind TEXT NOT NULL,
    record_json TEXT NOT NULL, PRIMARY KEY (generation, sequence));
CREATE TABLE IF NOT EXISTS memecoin_execution_observer_stats (
    name TEXT PRIMARY KEY, count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS memecoin_execution_observer_health (
    owner TEXT PRIMARY KEY, state_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_execution_witness_pending
    ON memecoin_execution_observations(validation_state, attempt_id);
CREATE INDEX IF NOT EXISTS idx_execution_witness_attempt
    ON memecoin_execution_observations(attempt_id, tx_hash);
"""


def timing_db_path():
    from sniper.util.state_paths import state_path
    return state_path("data", "memecoin_fill_timing.db")


def connect(path=None):
    """The writers' connection (the skip drain, the resolver): WAL, schema ensured. Never the ledger."""
    path = Path(path or timing_db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def prune_empty_receipts(conn, *, batch=10000):
    """Delete empty-decode source receipts of every producer run except the newest one.

    RUN ONLY WITH THE RUNNER STOPPED (deploy stop window; scripts/prune_empty_receipts.py checks the
    heartbeat). Never on the live file during trading: the drain thread writes here, and a long
    DELETE would hold the writer lock against it. Receipts carry no wall clock (recv_monotonic is
    per boot, run ids are UUIDs), so "older than 24 h" cannot be computed from them; the newest run
    is the retention unit instead (it covers the time since the last restart). Decoded rows, gaps
    and processed receipts are never touched. Deleted pages are reused by later rows; the file
    itself only shrinks through a VACUUM in a stop window. Returns the number of rows deleted.
    """
    latest = conn.execute("SELECT producer_run_id FROM memecoin_signal_receipts "
                          "ORDER BY rowid DESC LIMIT 1").fetchone()
    if latest is None:
        return 0
    total = 0
    while True:
        cur = conn.execute(
            "DELETE FROM memecoin_signal_receipts WHERE rowid IN (SELECT rowid FROM "
            "memecoin_signal_receipts WHERE decode_state='empty_decode_unknown' "
            "AND producer_run_id<>? LIMIT ?)", (latest[0], int(batch)))
        conn.commit()
        total += cur.rowcount
        if cur.rowcount < int(batch):
            return total


WATCHER_NO_ROW_DDL = ("CREATE TABLE IF NOT EXISTS memecoin_watcher_no_row (signature TEXT NOT NULL, "
                      "owner TEXT NOT NULL, source_user_id TEXT, feed TEXT, reason TEXT, noted_at TEXT, "
                      "PRIMARY KEY (signature, owner))")


_NO_ROW_QUEUE_MAX = 1024
_NO_ROW = {"queue": None, "thread": None, "lock": threading.Lock(),
           "stats": {"queued": 0, "written": 0, "dropped_full": 0, "failed": 0}}


def watcher_no_row_stats():
    """{queued, written, dropped_full, failed, pending} for the watcher's heartbeat. Never raises."""
    stats = dict(_NO_ROW["stats"])
    q = _NO_ROW["queue"]
    stats["pending"] = q.qsize() if q is not None else 0
    return stats


def note_watcher_no_row(signature, owner, source_user_id, feed, reason, *, path=None):
    """The confirmed watcher's decision for a buy it classified but wrote no detection row for (Alpha
    2026-10-16, 58Wk6q9f: JDQKD's Xsc9-funded creation buy, judged by nothing).

    ENQUEUE ONLY (Gamma B1, 2026-10-16): the caller is the watcher's one confirmed-decode thread, whose
    detections feed the funded on-chain lane, so it must never wait on the file. A synchronous write
    measured p50 29 ms / p99 45 ms and 506 ms with another writer holding the file. One daemon writer
    drains a bounded queue into memecoin_watcher_no_row in the timing file (never the ledger); a full
    queue drops the note and counts it. First note per (signature, owner) wins. True when queued;
    never raises."""
    from datetime import datetime, timezone
    try:
        if _NO_ROW["queue"] is None:
            with _NO_ROW["lock"]:
                if _NO_ROW["queue"] is None:
                    _NO_ROW["queue"] = queue.Queue(maxsize=_NO_ROW_QUEUE_MAX)
                    _NO_ROW["thread"] = threading.Thread(target=_no_row_writer, args=(_NO_ROW["queue"],),
                                                         name="watcher-no-row-writer", daemon=True)
                    _NO_ROW["thread"].start()
        _NO_ROW["queue"].put_nowait((str(signature), str(owner), source_user_id, feed, str(reason)[:160],
                                     datetime.now(timezone.utc).isoformat(), path))
        _NO_ROW["stats"]["queued"] += 1
        return True
    except queue.Full:
        _NO_ROW["stats"]["dropped_full"] += 1
        return False
    except Exception:
        logging.getLogger(__name__).exception("watcher no-row note failed sig=%s owner=%s", signature, owner)
        return False


def _no_row_writer(q):
    """The one writer: a connection per file kept open (synchronous NORMAL, the DDL once), a commit
    per note. A failed write is counted and its connection dropped; the loop never dies."""
    import contextlib
    conns = {}
    while True:
        item = q.get()
        target = None                       # never a previous note's file (Gamma LOW)
        try:
            *row, path = item
            target = str(Path(path or timing_db_path()))
            conn = conns.get(target)
            if conn is None:
                Path(target).parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(target, timeout=5)
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute(WATCHER_NO_ROW_DDL)
                conns[target] = conn
            conn.execute("INSERT OR IGNORE INTO memecoin_watcher_no_row (signature, owner, source_user_id, feed, "
                         "reason, noted_at) VALUES (?,?,?,?,?,?)", row)
            conn.commit()
            _NO_ROW["stats"]["written"] += 1
        except Exception:
            _NO_ROW["stats"]["failed"] += 1
            logging.getLogger(__name__).exception("watcher no-row write failed")
            bad = conns.pop(target, None) if target is not None else None
            if bad is not None:
                with contextlib.suppress(Exception):
                    bad.close()
        finally:
            q.task_done()


def flush_watcher_no_row(timeout_s=5.0):
    """Wait until every queued note is written or failed (tests, shutdown). True when drained."""
    import time
    q = _NO_ROW["queue"]
    deadline = time.monotonic() + timeout_s
    while q is not None and q.unfinished_tasks:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.005)
    return True


def open_readonly(path=None):
    """A read-only connection to the timing file, or None when it does not exist yet (first boot).
    Never creates it."""
    path = Path(path or timing_db_path())
    if not path.exists():
        return None
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=1)
    conn.row_factory = sqlite3.Row
    return conn


# ------------------------------------------------------------------------------------------ facts

def _is_evm_fast_lane(row):
    """Shared BSC deliveries retain watcher identity for deduplication; their lane is explicit."""
    network = str(row.get("network_id") or "solana")
    return network != "solana" and (
        row.get("signal_source") in FAST_LANE_SOURCES
        or (network == "56" and row.get("signal_source") == "onchain_watch"
            and row.get("delivery_lane") in _BSC_SHARED_FAST_LANES))


def _trace(row):
    raw = (row or {}).get("trace_json")
    if not raw:
        return {}
    try:
        out = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def _int(value):
    try:
        return None if value is None or isinstance(value, bool) else int(value)
    except (TypeError, ValueError):
        return None


def _slot(value):
    # A block/slot is a positive integer; zero, booleans and fractional values are unknown.
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    return int(value) if str(value).isdigit() and int(value) > 0 else None


def _trace_slots(trace):
    """(source_slot, landing_slot, recv_slot, head_at_send, landing_time) held in an attempt's own
    trace. landing_time is our block's chain timestamp, held only by the EVM fast lanes."""
    source = _slot((trace.get("source_evidence") or {}).get("source_slot"))
    landing = recv = head = landing_time = None
    for ev in trace.get("submission_events") or ():
        if ev.get("stage") == "chain_confirmation":
            for key in ("landing_slot", "block"):            # Solana slot / EVM block
                if _slot(ev.get(key)) is not None:
                    landing = _slot(ev.get(key))
    for ev in trace.get("substage_events") or ():
        name = ev.get("name")
        if name == "slot_receipt":
            source = source if source is not None else _slot(ev.get("source_slot"))
            recv = _slot(ev.get("head_at_recv"))
        elif name == "attempt_row" and recv is None and ev.get("head_slot") is not None:
            # The runner's head at its decision (any lane but the slot lane); a stale head is no
            # measurement.
            age = ev.get("head_age_ms")
            if age is None or float(age) <= HEAD_FRESH_MS:
                recv = _slot(ev.get("head_slot"))
        elif name == "slot_pre_send":
            head = _slot(ev.get("head_slot"))
        elif name == "slot_resend" and landing is None:
            landing = _slot(ev.get("landing_slot"))
        elif name == "evm.confirmation" and landing is None:
            landing = _slot(ev.get("block"))
        elif name == "rh_fast_lane_receipt":
            landing = _slot(ev.get("block")) if landing is None else landing
            landing_time = _int(ev.get("block_time"))
    return source, landing, recv, head, landing_time


def _receipt_observation_slot(attempt, trace):
    """A terminal measurement marker must match this attempt's chain and exact source hash."""
    chain, source = str(attempt.get("network_id") or ""), source_ref(attempt)
    if chain == "solana" or not _EVM_TX.fullmatch(str(source or "")):
        return None
    events = trace.get("substage_events")
    for event in events if isinstance(events, list) else ():
        if not isinstance(event, dict) or event.get("name") != "evm_source_receipt_observation":
            continue
        observation = event.get("observation")
        if (not isinstance(observation, dict) or str(observation.get("network_id") or "") != chain
                or str(observation.get("source_tx") or "").lower() != source):
            continue
        raw_block = observation.get("source_block_number")
        if isinstance(raw_block, str) and len(raw_block) > 19:
            continue
        block = _slot(raw_block)
        block_hash = observation.get("source_block_hash")
        if (block is not None and block < 2 ** 63
                and (block_hash is None or _EVM_TX.fullmatch(str(block_hash).lower()))):
            return block
    return None


def _fast_lane_clock(trace):
    """(sent_at, receipt_at) from an EVM fast lane's own send and receipt substages, or None each:
    those lanes leave transaction_submitted_at and chain_confirmation_completed_at empty."""
    found = {}
    for ev in trace.get("substage_events") or ():
        if ev.get("name") in ("rh_fast_lane_send", "rh_fast_lane_receipt"):
            found[ev["name"]] = ev.get("completed_at")
    return found.get("rh_fast_lane_send"), found.get("rh_fast_lane_receipt")


def _ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _ms(start, end):
    a, b = _ts(start), _ts(end)
    if a is None or b is None or b < a:
        return None
    return (b - a) * 1000.0


def _aware_stamp(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp if stamp.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def _quote_boundaries(attempt, trace, fast_lane):
    """Separate selected quote -> submission ACK and quote -> confirmation observation.

    Main-row stamps share the observed_real_utc clock. A legacy source clock, or a missing
    main-row stamp, needs matching boundaries from this attempt's single-clock trace. A native
    send/receipt alone does not prove when its quote was observed. Never infer from source time
    or the legacy quote_age_ms, which retains its quote -> confirmation meaning.
    """
    trace_stages = trace.get("stages")
    trace_stages = trace_stages if isinstance(trace_stages, dict) else {}

    def stage_stamp(name):
        stage = trace_stages.get(name)
        return _aware_stamp(stage.get("completed_at")) if isinstance(stage, dict) else None

    trace_quote = stage_stamp("quote")
    quote_raw = attempt.get("quote_observed_at")
    quote = trace_quote if quote_raw in (None, "") else _aware_stamp(quote_raw)
    out = {"quote_to_submission_ms": None, "quote_to_confirmation_ms": None}
    if quote is None:
        return out
    lane_stamps = {}
    if fast_lane:
        events = trace.get("substage_events")
        for event in events if isinstance(events, list) else ():
            if isinstance(event, dict) and event.get("name") in ("rh_fast_lane_send", "rh_fast_lane_receipt"):
                lane_stamps[event["name"]] = _aware_stamp(event.get("completed_at"))
    for field, stage, lane_stage, key in (
        ("transaction_submitted_at", "transaction_submitted", "rh_fast_lane_send", "quote_to_submission_ms"),
        ("chain_confirmation_completed_at", "chain_confirmation", "rh_fast_lane_receipt", "quote_to_confirmation_ms"),
    ):
        trace_end = stage_stamp(stage) or lane_stamps.get(lane_stage)
        raw_end = attempt.get(field)
        end = trace_end if raw_end in (None, "") else _aware_stamp(raw_end)
        if end is None or end < quote:
            continue
        main_clock = (attempt.get("clock_domain") == "observed_real_utc"
                      and quote_raw not in (None, "") and raw_end not in (None, ""))
        if main_clock or (trace_quote == quote and trace_end == end):
            out[key] = (end - quote).total_seconds() * 1000.0
    if (out["quote_to_submission_ms"] is not None and out["quote_to_confirmation_ms"] is not None
            and out["quote_to_confirmation_ms"] < out["quote_to_submission_ms"]):
        out["quote_to_confirmation_ms"] = None
    return out


def _receipt_boundaries(attempt, trace):
    """Same-boot observations bound to the terminal swap, never a prior retry."""
    out = dict(receipt_dispatch_ms=None, receipt_submit_ms=None, receipt_confirmation_ms=None,
               receipt_boundaries_explicit=False, receipt_attempt_no=None, receipt_legacy_native=False)
    names = {"evm.sender_dispatch": "receipt_dispatch_ms",
             "transaction_submitted": "receipt_submit_ms", "chain_confirmation": "receipt_confirmation_ms"}
    if attempt.get("signal_source") in FAST_LANE_SOURCES:
        names.update(rh_fast_lane_send="receipt_submit_ms", rh_fast_lane_receipt="receipt_confirmation_ms")
    records, all_events = [], []
    for events, name_key in ((trace.get("substage_events") or [], "name"),
                             (trace.get("submission_events") or [], "stage")):
        for event in events:
            if not isinstance(event, dict):
                continue
            all_events.append(event)
            name = event.get(name_key)
            if name in names or name == "evm.confirmation":
                records.append((name, event))
    out["receipt_boundaries_explicit"] = bool(records) and (
        "receipt_clock" in trace or any("monotonic_ns" in event for _, event in records))
    if not out["receipt_boundaries_explicit"]:
        return out
    numbers = [event["attempt_no"] for event in all_events
               if type(event.get("attempt_no")) is int and event["attempt_no"] > 0]
    sig = attempt.get("tx_sig")
    if not numbers:
        # Legacy native sends have one signed dispatch and one ACK/receipt, without retry numbers.
        if (attempt.get("signal_source") not in FAST_LANE_SOURCES
                or not _EVM_TX.fullmatch(str(sig or ""))
                or any("attempt_no" in event for event in all_events)
                or [name for name, _ in records] != ["evm.sender_dispatch", "rh_fast_lane_send",
                                                    "rh_fast_lane_receipt"]
                or records[0][1].get("tx_sig") != sig
                or any(event.get("tx_sig") not in (None, sig) for _, event in records)
                or records[1][1].get("reply") not in ("accepted", "ambiguous")
                or _slot(records[2][1].get("block")) is None):
            return out
        elapsed = [elapsed_receipt_ms(trace.get("receipt_clock"), event.get("monotonic_ns"),
                                      boot_id=event.get("boot_id"))
                   if isinstance(event.get("boot_id"), str) and event["boot_id"] else None
                   for _, event in records]
        if any(value is None for value in elapsed) or elapsed != sorted(elapsed):
            return out
        out["receipt_legacy_native"] = True
        records = [(name, {**event, "attempt_no": 1, "tx_sig": sig}) for name, event in records]
        numbers = [1]
    if not numbers or any(type(event.get("attempt_no")) is not int or event["attempt_no"] <= 0
                          for _, event in records):
        return out
    current = max(numbers)
    selected = [(name, event) for name, event in records if event["attempt_no"] == current]
    identities = [event["tx_sig"] for _, event in selected if event.get("tx_sig") is not None]
    if not sig or not identities or any(value != sig for value in identities):
        return out
    out["receipt_attempt_no"] = current
    for key in set(names.values()):
        values = set()
        for event_name, event in selected:
            if names.get(event_name) != key:
                continue
            boot = event.get("boot_id")
            values.add(elapsed_receipt_ms(trace.get("receipt_clock"), event.get("monotonic_ns"), boot_id=boot)
                       if isinstance(boot, str) and boot
                       and (event_name == "transaction_submitted" or event.get("tx_sig") == sig) else None)
        out[key] = values.pop() if len(values) == 1 else None
    for earlier, later in (("receipt_dispatch_ms", "receipt_submit_ms"),
                           ("receipt_submit_ms", "receipt_confirmation_ms"),
                           ("receipt_dispatch_ms", "receipt_confirmation_ms")):
        if out[earlier] is not None and out[later] is not None and out[earlier] > out[later]:
            out[later] = None
    submitted = {}
    for name, event in records:
        if name != "transaction_submitted":
            continue
        if event.get("outcome") not in ("accepted", "ambiguous"):
            return out
        number = event["attempt_no"]
        boot = event.get("boot_id")
        try:
            identities = [row["tx_sig"] for _, row in records
                          if row["attempt_no"] == number and row.get("tx_sig") is not None
                          and isinstance(row.get("boot_id"), str) and row["boot_id"]
                          and elapsed_receipt_ms(trace.get("receipt_clock"), row.get("monotonic_ns"),
                                                 boot_id=row["boot_id"]) is not None]
            elapsed = (elapsed_receipt_ms(trace.get("receipt_clock"), event.get("monotonic_ns"), boot_id=boot)
                       if isinstance(boot, str) and boot else None)
        except (OverflowError, ValueError, TypeError):
            return out
        if (not identities or not _EVM_TX.fullmatch(str(identities[0]))
                or any(value != identities[0] for value in identities)):
            return out
        if elapsed is None or not math.isfinite(elapsed):
            return out
        value = identities[0], elapsed
        if number in submitted and submitted[number] != value:
            return out
        submitted[number] = value
    ordered = [submitted[number] for number in sorted(submitted)]
    if (len(ordered) > 1 and current in submitted and submitted[current][0] == sig
            and len({value[0] for value in ordered}) == len(ordered)
            and all(a[1] < b[1] for a, b in zip(ordered, ordered[1:]))):
        out.update(receipt_submission_count=len(ordered), first_receipt_submit_ms=ordered[0][1])
    return out


def facts(attempt, detection=None, slot_attempt=None, skip=None, fast=None, timing=None, live=None):
    """Everything stored about one submitted attempt, merged. Precedence for a slot: the attempt's
    own explicit field, its trace, the detection (watcher), then the resolver's row. `live` is an
    EVM fast lane's own row for this fill (lane_live_of)."""
    attempt, timing = dict(attempt or {}), dict(timing or {})
    detection = dict(detection or {})
    try:
        valuation = json.loads(detection.get("source_valuation_json") or "{}")
        valuation = valuation if isinstance(valuation, dict) else {}
    except (TypeError, ValueError):
        valuation = {}
    trace = _trace(attempt)
    network = str(attempt.get("network_id") or "solana")
    boundaries = _receipt_boundaries(attempt, trace) if network != "solana" else {}
    explicit = boundaries.get("receipt_boundaries_explicit", False)
    slot_trace = trace
    allow_landing_cache = True
    if explicit:
        current, sig = boundaries["receipt_attempt_no"], attempt.get("tx_sig")
        confirmations = (("submission_events", "stage", "chain_confirmation"),
                         ("substage_events", "name", "evm.confirmation"),
                         ("substage_events", "name", "rh_fast_lane_receipt"))
        def terminal_confirmation(event, name_key, name):
            if event.get(name_key) != name:
                return True
            if name == "rh_fast_lane_receipt" and attempt.get("signal_source") not in FAST_LANE_SOURCES:
                return False
            return current is not None and (
                (boundaries["receipt_legacy_native"] and name == "rh_fast_lane_receipt")
                or (event.get("attempt_no") == current and event.get("tx_sig") == sig))
        slot_trace = {**trace, **{
            key: [event for event in trace.get(key) or []
                  if isinstance(event, dict) and all(terminal_confirmation(event, name_key, name)
                      for event_key, name_key, name in confirmations if event_key == key)]
            for key in {key for key, _, _ in confirmations}}}
        allow_landing_cache = any(event.get(name_key) == name
                                  for key, name_key, name in confirmations for event in slot_trace[key])
    t_source, t_landing, t_recv, head, t_landing_time = _trace_slots(slot_trace)
    fast_lane = _is_evm_fast_lane(attempt)
    source, basis = _slot(attempt.get("source_slot")), "attempt"
    if source is None and t_source is not None:
        source, basis = t_source, ("slot_lane" if attempt.get("signal_source") == "slot_lane" else "trace")
    evidence = trace.get("source_evidence") or {}
    if (source is None and fast_lane and isinstance(evidence, dict)
            and str(evidence.get("network_id") or "") == network
            and _slot(evidence.get("source_block_number")) is not None):
        source, basis = _slot(evidence["source_block_number"]), "fast_lane"
    if source is None and _slot(detection.get("source_slot")) is not None:
        source, basis = _slot(detection["source_slot"]), "watcher"
    if source is None and _slot(valuation.get("source_slot")) is not None:
        source, basis = _slot(valuation["source_slot"]), "detection_json"
    if source is None and fast and _slot(fast.get('source_slot')) is not None:
        source, basis = _slot(fast['source_slot']), fast.get('source_slot_basis') or 'watcher_note'
    if source is None and _slot(timing.get("source_slot")) is not None:
        source, basis = _slot(timing["source_slot"]), timing.get("source_slot_basis") or "rpc"
    if source is None:
        source = _receipt_observation_slot(attempt, trace)
        if source is not None:
            basis = "receipt_observation"
    landing, lbasis = (_slot(attempt.get("landing_slot")) if allow_landing_cache else None), "attempt"
    if landing is None and t_landing is not None:
        landing, lbasis = t_landing, "confirmation"
    if allow_landing_cache and landing is None and _slot(timing.get("landing_slot")) is not None:
        landing, lbasis = _slot(timing["landing_slot"]), timing.get("landing_slot_basis") or "rpc"
    # The lane's own measure (Gamma INFO on R8): blocks_behind = our receipt block - the source's
    # (its datagram seq), on the row bound to OUR transaction. Used only when nothing else names
    # the source block; 52807e7b's trace predates source_block_number.
    behind = (live or {}).get("blocks_behind")
    if (source is None and fast_lane and landing is not None and type(behind) is int
            and 0 <= behind <= landing):
        source, basis = landing - behind, "fast_lane_live"
    recv = _slot(attempt.get("recv_slot"))
    recv = recv if recv is not None else t_recv
    recv = recv if recv is not None else _slot(valuation.get("recv_slot"))
    sent_at, receipt_at = _fast_lane_clock(slot_trace) if fast_lane else (None, None)
    source_time = timing.get("source_time") if timing.get("source_time") is not None else detection.get("block_time")
    landing_time = (t_landing_time if not allow_landing_cache else timing.get("landing_time")
                    if timing.get("landing_time") is not None else t_landing_time)
    gap_s = None
    if network != "solana" and source_time is not None and landing_time is not None:
        gap_s = float(landing_time) - float(source_time)
    slot = None
    if slot_attempt:
        s_src, s_land, _r, _h, _t = _trace_slots(_trace(slot_attempt))
        slot = {"status": slot_attempt.get("terminal_status"), "reason": slot_attempt.get("terminal_reason"),
                "completed_at": slot_attempt.get("completed_at"),
                "gap": (s_land - s_src) if s_src is not None and s_land is not None else None}
    detect_s = detection.get("detect_latency_s")
    if detect_s is None:
        a, b = detection.get("block_time"), _ts(detection.get("detected_at"))
        detect_s = (b - float(a)) if a is not None and b is not None else None
    # A J7 event may arrive before the watcher decodes the same transaction. Measure
    # the lane that actually supplied this attempt, not the later watcher's receipt.
    if network != 'solana' and attempt.get('observed_at') and source_time is not None:
        try:
            received = datetime.fromisoformat(str(attempt['observed_at']).replace('Z', '+00:00'))
            delta = received.timestamp() - float(source_time)
            detect_s = delta if received.tzinfo is not None and math.isfinite(delta) and delta >= 0 else None
        except (TypeError, ValueError, OverflowError):
            detect_s = None
    return {
        "attempt_id": attempt.get("attempt_id"), "network_id": network,
        "source_ref": source_ref(attempt),
        "signal_source": attempt.get("signal_source"), "delivery_lane": attempt.get("delivery_lane"),
        "terminal_status": attempt.get("terminal_status"), "terminal_reason": attempt.get("terminal_reason"),
        "source_slot": source, "source_basis": basis if source is not None else None,
        "landing_slot": landing, "landing_basis": lbasis if landing is not None else None,
        "recv_slot": recv, "head_at_send": head, "detect_s": detect_s,
        "source_time": source_time, "landing_time": landing_time, "gap_s": gap_s,
        "observed_at": attempt.get("observed_at"), "decision_started_at": attempt.get("decision_started_at"),
        "submitted_at": attempt.get("transaction_submitted_at") or sent_at,
        "confirmed_at": attempt.get("chain_confirmation_completed_at") or receipt_at,
        "receipt_dispatch_ms": (execution_stage_spans(attempt)["first_transport_dispatch_ms"]
                                if network == "solana" else None),
        **boundaries,
        **_quote_boundaries(attempt, trace, fast_lane),
        "slot": slot, "skip": dict(skip) if skip else None, "fast": dict(fast) if fast else None,
        "live": dict(live) if live else None,
        "source_venue": (valuation.get("evm_post")
                         if isinstance(valuation.get("evm_post"), dict) else None),
        "state": timing.get("state"),
    }


# ------------------------------------------------------------------------------------- derived

def gap_of(f):
    if f.get("source_slot") is None or f.get("landing_slot") is None:
        return None
    return int(f["landing_slot"]) - int(f["source_slot"])


def stages(f):
    """Keep Solana slot distances and coarse EVM block clocks separate from elapsed observations.

    detect is recv-S in slots on Solana, detected_at-block_time in seconds on EVM.
    land_slots is landing-head_at_send; land_ms is submission to confirmation observation.
    Missing boundaries remain None.
    """
    solana = f.get("network_id") == "solana"
    detect = None
    if solana and f.get("recv_slot") is not None and f.get("source_slot") is not None:
        detect = int(f["recv_slot"]) - int(f["source_slot"])
    elif not solana and f.get("detect_s") is not None:
        detect = float(f["detect_s"])
    wait = None
    if f.get("delivery_lane") == FALLBACK_LANE and f.get("slot"):
        wait = _ms((f["slot"] or {}).get("completed_at"), f.get("decision_started_at"))
    land_slots = None
    if solana and f.get("landing_slot") is not None and f.get("head_at_send") is not None:
        land_slots = int(f["landing_slot"]) - int(f["head_at_send"])
    submit, confirmation = f.get("receipt_submit_ms"), f.get("receipt_confirmation_ms")
    land_ms = (confirmation - submit if submit is not None and confirmation is not None
               else None if f.get("receipt_boundaries_explicit")
               else _ms(f.get("submitted_at"), f.get("confirmed_at")))
    return {"detect": detect, "fallback_wait_ms": wait,
            "decide_ms": submit if f.get("receipt_boundaries_explicit") else _ms(f.get("observed_at"), f.get("submitted_at")),
            "land_slots": land_slots, "land_ms": land_ms if land_ms is None or land_ms >= 0 else None}


def _largest(f, stage_values=None):
    """Largest local elapsed observation; slots and coarse block clocks do not rank."""
    st = stage_values if stage_values is not None else stages(f)
    ranked = []
    if st["fallback_wait_ms"] is not None:
        ranked.append((st["fallback_wait_ms"], "fallback_wait", "%.0f ms" % st["fallback_wait_ms"]))
    if st["decide_ms"] is not None:
        ranked.append((st["decide_ms"], "decide", "%.0f ms" % st["decide_ms"]))
    if st["land_ms"] is not None:
        ranked.append((st["land_ms"], "land", "%.0f ms" % st["land_ms"]))
    if not ranked:
        return None
    _v, name, words = max(ranked, key=lambda r: r[0])
    return name, words


_BSC_DUPLICATE_REASON = "hook:rejected:unresolved signed transaction on 56"


def _late_bsc_duplicate(f):
    fast = f.get("fast") or {}
    if f.get("network_id") != "56" or fast.get("reason") != _BSC_DUPLICATE_REASON:
        return False
    try:
        decision, submitted, written = (datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                                       for value in (fast.get("decision_started_at"), f.get("submitted_at"),
                                                     fast.get("written_at")))
        return (all(t.tzinfo is not None for t in (decision, submitted, written))
                and submitted < decision <= written)
    except (TypeError, ValueError):
        return False


def classify(f, *, stage_values=None):
    """(cause, detail), first match wins (spec 2.5). detail always ends with the largest stage."""
    gap, chain = gap_of(f), f.get("network_id") or "solana"
    fast_lane = _is_evm_fast_lane(f)
    reason = str(f.get("terminal_reason") or "")
    slot, skip, fast = f.get("slot"), f.get("skip"), f.get("fast")
    if f.get("signal_source") in MANUAL_SOURCES:
        cause, words = "manual", "operator entry: no source slot"
    elif f.get("signal_source") == "desk":
        # A desk ticket has no source buy either; a failed one keeps its reason (2026-10-23).
        cause, words = "desk", reason or "desk entry: no source slot"
    elif f.get("terminal_status") == "failed" and any(w in reason.lower() for w in _PRICE_WORDS):
        cause, words = "price", reason
    elif gap is not None and gap <= ON_TIME.get(chain, 1):
        cause, words = "on_time", ""
    elif f.get("state") == "not_landed":
        cause, words = "not_landed", reason or "no landing slot"
    elif f.get("delivery_lane") == FALLBACK_LANE and slot and slot.get("status") == "failed":
        cause = "slot_lane_failed"
        words = "slot lane %s %s" % ("S+%d" % slot["gap"] if slot.get("gap") is not None else "S+?",
                                    slot.get("reason") or "failed")
        wait = (stage_values if stage_values is not None else stages(f))["fallback_wait_ms"]
        if wait is not None:
            words += "; fallback_wait %.0f ms" % wait
    elif chain == "solana" and f.get("signal_source") != "slot_lane":
        cause = "slot_lane_skipped"
        if (slot and slot.get("status") in ("refused", "rejected", "aborted")
                and slot.get("reason") == "slot_lane_exotic_leg_pending"
                and skip and skip.get("outcome") == "rejected" and skip.get("reason")
                and skip["reason"] != "slot_lane_exotic_leg_pending"):
            words = "slot lane refused: %s" % skip["reason"]
        elif slot and slot.get("status") in ("refused", "rejected", "aborted", "source_initiation_unknown"):
            words = "slot lane refused: %s" % (slot.get("reason") or "unknown")
        elif skip:
            words = "slot lane: %s %s" % (skip.get("outcome") or "?", skip.get("reason") or "")
        elif slot:
            words = "slot lane %s: %s" % (slot.get("status") or "?", slot.get("reason") or "unknown")
        else:
            words = "slot lane never saw it"
        words = words.strip()
    elif chain != "solana" and not fast_lane and _late_bsc_duplicate(f):
        largest = _largest(f, stage_values)
        cause = largest[0] + "_slow" if largest else "unknown"
        words = "BSC duplicate suppressed after broadcast"
    elif chain != "solana" and f.get("source_slot") is None and not fast:
        cause, words = "source_unknown", ("source block unresolved" if f.get("source_ref")
                                         else "source transaction unavailable")
    elif chain != "solana" and not fast_lane:
        cause = "fast_lane_skipped"
        words = ("%s: %s" % (fast.get("lane") or "fast lane", fast.get("reason") or "unknown")
                 if fast else "no fast-lane decision")
        if chain == "56" and not fast:
            post = f.get("source_venue") or {}
            if post.get('venue') == 'v3':
                words = 'BSC V3 source; ' + words
            elif post.get("reason") == "no_venue_leg":
                words = "BSC source venue unresolved (no_venue_leg) · " + words
    else:
        largest = _largest(f, stage_values)
        cause, words = (largest[0] + "_slow") if largest else "unknown", ""
        live = f.get("live") or {}
        if live.get("safety_path") and isinstance(live.get("safety_ms"), (int, float)):
            # The fast lane's token-safety step sits inside decide (Gamma INFO on R8): name it.
            label = "safety wait" if live.get("parallel_safety") in ("used", "intent_used") else "safety"
            words = "%s %s %.0f ms" % (label, live["safety_path"], live["safety_ms"])
    if chain == "solana":
        if f.get("receipt_dispatch_ms") is not None:
            words = " · ".join(filter(None, (words, "receipt-to-dispatch %.0f ms" % f["receipt_dispatch_ms"])))
    else:
        measured = stage_values if stage_values is not None else stages(f)
        notes = []
        if measured["detect"] is not None:
            notes.append("source block-to-receipt %.1f s (coarse)" % measured["detect"])
        for key, label in (("receipt_dispatch_ms", "receipt-to-dispatch"),
                           ("receipt_submit_ms", "receipt-to-submit observed"),
                           ("receipt_confirmation_ms", "receipt-to-confirmation observed")):
            if f.get(key) is not None:
                notes.append("%s %.0f ms" % (label, f[key]))
        if f.get("receipt_submission_count"):
            notes.append("%d submitted attempts; first receipt-to-submit observed %.0f ms" % (
                f["receipt_submission_count"], f["first_receipt_submit_ms"]))
        words = " · ".join(([words] if words else []) + notes)
    largest = _largest(f, stage_values)
    decide_label = "receipt-to-submit" if f.get("receipt_submit_ms") is not None else "detect-to-submit"
    tail = ("largest: %s %s" % ({"decide": decide_label, "land": "confirmation observed"}.get(largest[0], largest[0]),
                                    largest[1]) if largest else "no stage measured")
    return cause, (words + " · " + tail) if words else tail


def resolving_line(chain, *, source=False):
    if chain == "solana":
        return "Landed S+? (%s)" % ("source slot resolving" if source else "resolving")
    if chain:
        return "Landed +? blocks (%s)" % ("source block resolving" if source else "resolving")
    return "Landed (timing resolving)"


def _format(chain, gap, gap_s, landing, cause, detail, *, source_available):
    show_detail = chain != "solana" or "receipt-to-dispatch " in detail
    if cause == "not_landed":
        return "Not landed · " + detail
    if landing is None:
        line = "Landing %s resolving" % ("slot" if chain == "solana" else "block")
        return line + " · " + detail if show_detail and detail else line
    if gap is None and cause in ("manual", "desk"):
        return "Landed %s %s · %s entry" % ("slot" if chain == "solana" else "block", landing,
                                            "operator" if cause == "manual" else "desk")
    if gap is None:
        line = ("Landed block %s · block gap unavailable" % landing
                if chain != "solana" and not source_available else resolving_line(chain, source=True))
        return line + " · " + detail if show_detail and detail else line
    if chain == "solana":
        head = "Landed S+%d" % gap
    else:
        head = "Landed +%d blocks" % gap + (" (%.1f s)" % gap_s if gap_s is not None else "")
    return head if not detail or (cause == "on_time" and not show_detail) else head + " · " + detail


def landed_text(f, *, stage_values=None):
    """The one line the Telegram alert and the dashboard show; '' for an attempt that never sent."""
    if f.get("terminal_status") not in ("filled", "failed"):
        return ""
    cause, detail = classify(f, stage_values=stage_values)
    return _format(f.get("network_id"), gap_of(f), f.get("gap_s"), f.get("landing_slot"), cause, detail,
                   source_available=bool(f.get("source_ref")))


def line_from_row(row):
    """The same line from a resolver row as stored (its own cause and detail)."""
    return _format(row.get("network_id"), row.get("gap"), row.get("gap_s"), row.get("landing_slot"),
                   row.get("cause"), row.get("cause_detail") or "", source_available=bool(row.get("source_sig")))


# ---------------------------------------------------------- stored inputs (resolver + views)

def source_ref(attempt):
    """The source transaction of an attempt: a Solana signature or an EVM tx hash, or None. A J7
    Solana id that was lowercased (base58 is case-sensitive) is no reference."""
    oid, chain = str(attempt.get("origin_event_id") or ""), str(attempt.get("network_id") or "")
    if oid.startswith("slotlane:"):
        return oid[len("slotlane:"):] or None
    if oid.startswith("onchain:"):
        parts = oid[len("onchain:"):].split(":")
        if len(parts) >= 2 and parts[0] == chain and chain != "solana":
            return parts[1].lower()
        return parts[0] or None
    if oid.startswith(_FAST_ORIGINS):
        parts = oid.split(":")
        tx = parts[2].lower() if len(parts) == 4 else ""
        return tx if parts[1] == chain and _EVM_TX.fullmatch(tx) else None
    if oid.startswith("swap:raybot:"):
        parts = oid.split(":")
        ref = parts[2] if len(parts) > 2 else ""
        if chain == "solana":
            return ref if ref and ref != ref.lower() else None
        return ref.lower() if ref.startswith("0x") else None
    return None


def one(conn, sql, args):
    """One row as a dict, or None. The row factory is set on a CURSOR, never on the caller's
    connection, which other code shares (the notification worker's, the web's)."""
    if conn is None:
        return None
    try:
        cur = conn.cursor()
        cur.row_factory = sqlite3.Row
        row = cur.execute(sql, args).fetchone()
    except sqlite3.Error:
        return None
    return dict(row) if row else None


def detection_of(ledger, chain, ref):
    if not ref:
        return None
    if chain == "solana":
        return one(ledger, "SELECT * FROM onchain_wallet_detections WHERE signature=?", (ref,))
    return one(ledger, "SELECT * FROM onchain_wallet_detections WHERE signature >= ? AND signature < ? "
                "ORDER BY id LIMIT 1", ("%s:%s:" % (chain, ref), "%s:%s;" % (chain, ref)))


def slot_attempt_of(ledger, chain, ref):
    if chain != "solana" or not ref:
        return None
    return one(ledger, "SELECT terminal_status, terminal_reason, completed_at, trace_json "
                "FROM memecoin_entry_attempts INDEXED BY idx_mc_entry_attempt_origin "
                "WHERE origin_event_id=? ORDER BY decision_started_at DESC LIMIT 1", ("slotlane:" + ref,))


def fast_of(lane_conns, chain, ref, *, timing=None, mint=None, ledger=None):
    if chain == "solana" or not ref:
        return None
    if chain == "4663":
        hit = one(lane_conns.get("rh_lane"), "SELECT reason FROM rh_fast_lane_events WHERE tx_hash=? "
                   "ORDER BY id DESC LIMIT 1", (ref,))
        if hit:
            return {"lane": "rh fast lane", "reason": hit["reason"]}
        hit = one(lane_conns.get("rh_decide"), "SELECT reason FROM rh_fast_decisions WHERE tx_hash=? "
                   "ORDER BY id DESC LIMIT 1", (ref,))
        return {"lane": "rh fast decide", "reason": hit["reason"]} if hit else None
    if chain == "8453":                   # the Base lane: the shared executor's records, its own file
        hit = one(lane_conns.get("base_lane"), "SELECT reason FROM rh_fast_lane_events WHERE tx_hash=? "
                  "ORDER BY id DESC LIMIT 1", (ref,))
        return {"lane": "base fast lane", "reason": hit["reason"]} if hit else None
    if chain == "56":
        hit = one(lane_conns.get("bsc"), "SELECT reason, written_at FROM bsc_lane_events WHERE tx_hash=? AND mint=? "
                   "ORDER BY id DESC LIMIT 1", (ref, str(mint or '').lower()))
        note = one(timing, 'SELECT reason, source_slot, source_slot_basis FROM memecoin_fast_lane_notes '
                   'WHERE network_id=? AND source_sig=? AND mint=?', (chain, ref, str(mint or '').lower()))
        if hit or note:
            fast = dict(note or {}, lane='bsc lane' if hit else 'bsc watcher',
                        reason=hit['reason'] if hit else note['reason'])
            if hit and hit['reason'] == _BSC_DUPLICATE_REASON:
                fast['written_at'] = hit['written_at']
                # Corrected UTC attempt time, not the lane's raw host epoch or log completion.
                duplicate = one(ledger, "SELECT decision_started_at FROM memecoin_entry_attempts "
                                "INDEXED BY idx_mc_entry_attempt_origin WHERE origin_event_id=? "
                                "AND terminal_status='refused' AND terminal_reason=? AND decision_started_at<=? "
                                "ORDER BY decision_started_at DESC LIMIT 1",
                                ("onchain:56:%s:%s" % (ref, str(mint or '').lower()),
                                 "unresolved signed transaction on 56", hit['written_at']))
                if duplicate:
                    fast.update(duplicate)
            return fast
        return None
    return None


# The Base 8453 lane is the shared executor in its own file, so its live rows keep the shared table.
_LANE_LIVE = {"4663": ("rh_lane", "rh_fast_lane_live"), "56": ("bsc", "bsc_lane_live"),
              "8453": ("base_lane", "rh_fast_lane_live")}


def lane_live_of(lane_conns, chain, ref, ours):
    """An EVM fast lane's own row for OUR fill of source `ref` (blocks_behind, safety path and
    time), bound by our transaction hash, or None."""
    key = _LANE_LIVE.get(chain)
    if not key or not ref or not ours:
        return None
    return one(lane_conns.get(key[0]), "SELECT * FROM %s "
               "WHERE source_tx=? AND lower(our_tx)=? ORDER BY id DESC LIMIT 1" % key[1],
               (ref, str(ours).lower()))


def skip_of(timing, chain, ref):
    """The slot lane's own note for a Solana source signature, from the timing file."""
    if chain != "solana" or not ref:
        return None
    return one(timing, "SELECT outcome, reason FROM memecoin_slot_lane_skips WHERE signature=?", (ref,))


_LANE_FILES = (("rh_decide", "memecoin_rh_feed_shadow.db"), ("rh_lane", "memecoin_rh_fast_lane.db"),
               ("bsc", "memecoin_bsc_fast_lane.db"), ("base_lane", "memecoin_base_fast_lane.db"))


def open_lanes_readonly():
    """{key: read-only connection} for the EVM fast lanes' own files that exist (never ATTACH)."""
    from sniper.util.state_paths import state_path
    out = {}
    for key, name in _LANE_FILES:
        path = Path(state_path("data", name))
        if path.exists():
            out[key] = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=1)
    return out


def _details(ledger, attempt, timing, lanes, include_stages):
    chain = str(attempt.get("network_id") or "")
    ref = source_ref(attempt)
    stored = one(timing, "SELECT * FROM memecoin_fill_timing WHERE attempt_id=?",
                 (attempt.get("attempt_id"),)) if timing is not None else None
    f = facts(attempt, detection_of(ledger, chain, ref), slot_attempt_of(ledger, chain, ref),
              skip_of(timing, chain, ref) if timing is not None else None,
              fast_of(lanes, chain, ref, timing=timing, mint=attempt.get('mint'), ledger=ledger), stored,
              live=lane_live_of(lanes, chain, ref, attempt.get("tx_sig")))
    measured = stages(f)
    try:
        saved = json.loads((stored or {}).get("stages_json") or "{}")
    except (TypeError, ValueError):
        saved = {}
    if isinstance(saved, dict):
        saved = {key: value if isinstance(value, (int, float)) and not isinstance(value, bool)
                 and math.isfinite(value) and value >= 0 else None for key, value in saved.items()}
        measured = {key: value if (value is not None or (
                        key in ('decide_ms', 'land_ms') and f.get('receipt_boundaries_explicit')) or (
                        key == 'detect' and chain != 'solana' and f.get('observed_at')
                        and f.get('source_time') is not None)) else saved.get(key)
                    for key, value in measured.items()}
    # Cache measurements, not explanations. A later venue/verdict must reach closed rows too.
    line = landed_text(f, stage_values=measured)
    cause, detail = classify(f, stage_values=measured)
    result = {"line": line, "cause": cause, "detail": detail,
              "quote_to_submission_ms": f["quote_to_submission_ms"],
              "quote_to_confirmation_ms": f["quote_to_confirmation_ms"]}
    if chain == "56":
        witness = one(timing, "SELECT first_success_json,validation_state FROM "
                      "memecoin_execution_observations WHERE attempt_id=? "
                      "ORDER BY validation_state='verified' DESC LIMIT 1", (attempt.get("attempt_id"),))
        value = {}
        try:
            value = json.loads((witness or {}).get("first_success_json") or "{}")
        except (TypeError, ValueError):
            pass
        result["execution_witness_state"] = (witness or {}).get("validation_state", "unavailable")
        result["successful_receipt_observation_ms"] = (
            value.get("receipt_elapsed_ms") if result["execution_witness_state"] == "verified" else None)
    if include_stages:
        result["stages"] = measured
    return result


def _landed_by_own_fill(ledger, attempt):
    """An attempt whose position holds the entry fill on the attempt's own signature landed,
    whatever its terminal said then (73884: anomaly receipt_unreadable, booked by the recovery; the
    board read "Landed Unknown"). The view's copy reads filled; the stored terminal never changes."""
    sig, aid = attempt.get("tx_sig"), attempt.get("attempt_id")
    if attempt.get("terminal_status") in ("filled", "failed") or not sig or not aid or ledger is None:
        return attempt
    try:
        row = one(ledger, "SELECT 1 FROM memecoin_fills f JOIN memecoin_positions p ON p.id = f.position_id "
                  "WHERE p.attempt_id = ? AND f.kind = 'entry' AND (f.tx_sig = ? OR "
                  "(substr(?, 1, 2) = '0x' AND lower(f.tx_sig) = lower(?))) LIMIT 1", (aid, sig, sig, sig))
    except Exception:
        return attempt
    return dict(attempt, terminal_status="filled") if row else attempt


def view_details(ledger, attempts, *, include_stages=True):
    """Stored timing per attempt, opening read-only timing and lane files once. No provider calls."""
    out, timing, lanes = {}, None, {}
    attempts = [dict(a) for a in attempts or () if a]
    try:
        timing = open_readonly()
        if any(str(a.get("network_id") or "") != "solana" for a in attempts):
            lanes = open_lanes_readonly()
    except Exception:
        timing, lanes = None, {}
    try:
        for attempt in attempts:
            attempt = _landed_by_own_fill(ledger, attempt)
            if attempt.get("terminal_status") not in ("filled", "failed") or not attempt.get("tx_sig"):
                out[attempt.get("attempt_id")] = {"line": "", "stages": {}}
                continue
            try:
                out[attempt.get("attempt_id")] = _details(ledger, attempt, timing, lanes, include_stages)
            except Exception:
                out[attempt.get("attempt_id")] = {"line": resolving_line(attempt.get("network_id")), "stages": {}}
    finally:
        for conn in [timing, *lanes.values()]:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    return out


def view_lines(ledger, attempts):
    """{attempt_id: line} for the entry alert and open positions board."""
    return {aid: detail["line"] for aid, detail in view_details(ledger, attempts, include_stages=False).items()}


def view_line(ledger, attempt):
    """The alert and dashboard line for one attempt, from what is stored right now (the resolver's
    row when it has one). Never raises: a missing timing file or any error degrades (Gamma LOW1)."""
    try:
        attempt = dict(attempt or {})
    except Exception:
        return resolving_line(None)
    return view_lines(ledger, [attempt]).get(attempt.get("attempt_id"), "")


def execution_bound(source, observations, attempt, pending, fill, *, custody_pubkey):
    """Confirmed persisted identity plus first processed receipt gives an observed upper bound.

    Normalized dicts are the resolver adapter contract, never main-ledger column names.
    """
    result = dict(state='unknown', reason=None, observed_bound_ms=None, sub200=None,
                  canonical_execution_time='unverified')
    def unknown(reason):
        return dict(result, reason=reason)
    if not all(isinstance(row, dict) for row in (source, attempt, pending, fill)):
        return unknown('missing_join')
    if attempt.get('terminal_status') != 'filled':
        return unknown('attempt_not_filled')
    if pending.get('state') != 'confirmed' or fill.get('chain_confirmed') is not True:
        return unknown('not_confirmed')
    if fill.get('side') != 'buy':
        return unknown('not_entry_fill')
    if not custody_pubkey or any(row.get('custody_pubkey') != custody_pubkey for row in (pending, fill)):
        return unknown('payer_mismatch')
    for key in ('network_id', 'mint'):
        value = source.get(key)
        if not value or any(row.get(key) != value for row in (attempt, pending, fill)):
            return unknown(key + '_mismatch')
    if source.get('network_id') != 'solana':
        return unknown('unsupported_chain')
    for key in ('source_sig', 'source_wallet'):
        if not source.get(key) or attempt.get(key) != source[key]:
            return unknown(key + '_mismatch')
    for key in ('attempt_id', 'pending_id'):
        if attempt.get(key) is None or any(row.get(key) != attempt[key] for row in (pending, fill)):
            return unknown(key + '_mismatch')
    sig = attempt.get('tx_sig')
    if not sig or any(row.get('tx_sig') != sig for row in (pending, fill)):
        return unknown('signature_mismatch')
    for key in ('payment_atomic', 'acquired_atomic'):
        atoms = fill.get(key)
        if (not isinstance(atoms, str) or not atoms.isascii() or not atoms.isdigit()
                or int(atoms) <= 0):
            return unknown(key + '_unknown')
    candidates = [row for row in observations if isinstance(row, dict)
                  and row.get('our_sig') == sig and row.get('network_id') == 'solana']
    if not candidates:
        return unknown('processed_missing')
    # Invalid or cross-domain receipts cannot be sorted into an invented successful first receipt.
    for row in (source, *candidates):
        value = row.get('recv_monotonic')
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0):
            return unknown('invalid_clock')
        if (not source.get('boot_id') or not source.get('clock_domain')
                or row.get('boot_id') != source['boot_id']
                or row.get('clock_domain') != source['clock_domain']):
            return unknown('clock_domain_mismatch')
    first = min(candidates, key=lambda row: row['recv_monotonic'])
    if any(row.get('error_state') != 'success' for row in candidates):
        return unknown('processed_error_observed')
    if (type(attempt.get('landing_slot')) is not int
            or type(fill.get('receipt_slot')) is not int
            or any(type(row.get('slot')) is not int or row['slot'] <= 0
                   or row['slot'] != attempt['landing_slot']
                   or row['slot'] != fill['receipt_slot'] for row in candidates)):
        return unknown('receipt_slot_mismatch')
    elapsed = (first['recv_monotonic'] - source['recv_monotonic']) * 1000.0
    if not math.isfinite(elapsed):
        return unknown('invalid_interval')
    if elapsed < 0:
        return unknown('negative_interval')
    return dict(result, state='witnessed' if elapsed <= 200 else 'observed_over_target',
                reason=None, observed_bound_ms=elapsed, sub200=elapsed <= 200)


def _positive_atoms(value):
    try:
        return (isinstance(value, str) and value.isascii() and value.isdigit() and int(value) > 0)
    except ValueError:
        return False


def same_slot_outcome(*, source_slot, landing_slot, confirmed, chain_error, acquired_atomic):
    """Receipt grade only; head notifications, ACK and local clocks grant no evidence."""
    if confirmed is not True:
        return 'unknown'
    if chain_error is not None:
        return 'failed'
    source, landing = _slot(source_slot), _slot(landing_slot)
    if source is None or landing is None or landing < source or not _positive_atoms(acquired_atomic):
        return 'unknown'
    return 'same_slot' if landing == source else 'later_slot'


def same_slot_receipt_evidence(source, attempt, receipt, fill):
    """Exact normalized confirmed-receipt join, independent of processed observers/boot.

    Receipt adapters must provide chain_confirmed, chain_error (including explicit None),
    receipt_slot and exact signature/payer/mint identity. Successful fills additionally carry
    receipt-derived atomic payment/acquisition, never float booked quantities.
    """
    result = dict(outcome='unknown', reason=None, contradiction=False,
                  source_slot=None, landing_slot=None, slot_delta=None)
    def unknown(reason, *, contradiction=False):
        return dict(result, reason=reason, contradiction=contradiction)
    if not all(isinstance(row, dict) for row in (source, attempt, receipt)):
        return unknown('missing_receipt_join')
    if source.get('network_id') != 'solana':
        return unknown('unsupported_chain')
    for key in ('network_id', 'mint'):
        if not source.get(key) or any(row.get(key) != source[key] for row in (attempt, receipt)):
            return unknown(key + '_mismatch', contradiction=True)
    for key in ('source_sig', 'source_wallet'):
        if not source.get(key) or attempt.get(key) != source[key]:
            return unknown(key + '_mismatch', contradiction=True)
    sig, owner = attempt.get('tx_sig'), attempt.get('custody_pubkey')
    if not sig or receipt.get('tx_sig') != sig:
        return unknown('signature_mismatch', contradiction=True)
    if not owner or receipt.get('custody_pubkey') != owner:
        return unknown('payer_mismatch', contradiction=True)
    if receipt.get('chain_confirmed') is not True or 'chain_error' not in receipt:
        return unknown('receipt_unresolved')
    source_slot, landing = _slot(source.get('source_slot')), _slot(receipt.get('receipt_slot'))
    result.update(source_slot=source_slot, landing_slot=landing,
                  slot_delta=landing - source_slot if source_slot is not None and landing is not None else None)
    # A failed confirmed receipt is counted even when no fill exists or the source slot is unknown.
    if receipt['chain_error'] is not None:
        return dict(result, outcome='failed')
    if landing is None:
        invalid = receipt.get('receipt_slot') is not None
        return unknown('receipt_slot_invalid' if invalid else 'receipt_slot_unknown', contradiction=invalid)
    if source_slot is not None and landing < source_slot:
        return unknown('receipt_slot_mismatch', contradiction=True)
    if attempt.get('landing_slot') is not None and _slot(attempt['landing_slot']) != landing:
        return unknown('receipt_slot_mismatch', contradiction=True)
    if not isinstance(fill, dict):
        return unknown('missing_fill')
    if fill.get('side') is not None and fill['side'] != 'buy':
        return unknown('side_mismatch', contradiction=True)
    if fill.get('chain_confirmed') is not True or fill.get('side') != 'buy':
        return unknown('not_confirmed_entry')
    for key in ('network_id', 'mint', 'tx_sig', 'custody_pubkey', 'attempt_id', 'pending_id'):
        if attempt.get(key) is None or fill.get(key) != attempt[key]:
            return unknown(key + '_mismatch', contradiction=True)
    if _slot(fill.get('receipt_slot')) != landing:
        return unknown('receipt_slot_mismatch', contradiction=True)
    for key in ('payment_atomic', 'acquired_atomic'):
        values = (fill.get(key), receipt.get(key))
        if any(value is not None and not _positive_atoms(value) for value in values):
            return unknown(key + '_invalid', contradiction=True)
        if any(value is None for value in values):
            return unknown(key + '_unknown')
        if int(receipt[key]) != int(fill[key]):
            return unknown(key + '_mismatch', contradiction=True)
    outcome = same_slot_outcome(source_slot=source_slot, landing_slot=landing, confirmed=True,
                               chain_error=None, acquired_atomic=fill['acquired_atomic'])
    return dict(result, outcome=outcome, reason='slot_unknown' if outcome == 'unknown' else None)


SAME_SLOT_STRATA = ('native_local_proof', 'routed_confirmed_proof', 'graduation', 'ordinary_fallback')
SAME_SLOT_COUNTS = ('S+0', 'S+1', 'S+2+', 'failed', 'refused', 'no_submission', 'unresolved', 'missing')


def same_slot_cohort_summary(sources, executions):
    """All unique caller-frozen eligible source buys, including cold/missed preparation.

    Sources supply stratum before reading outcomes. Executions contain normalized attempt,
    receipt and fill dicts. No source enrollment is inferred from submission or warmth.
    Unknown source slots may be enriched; contradictory known slots require reconciliation.
    """
    def identity(row):
        key = tuple(row.get(k) for k in ('network_id', 'source_sig', 'source_wallet', 'mint'))
        if not all(isinstance(value, str) and value for value in key) or key[0] != 'solana':
            raise ValueError('invalid frozen source identity')
        return key
    frozen = {}
    for source in sources:
        key = identity(source)
        if source.get('stratum') not in SAME_SLOT_STRATA:
            raise ValueError('invalid frozen source stratum')
        source_slot = _slot(source.get('source_slot'))
        if key in frozen:
            if frozen[key]['stratum'] != source['stratum']:
                raise ValueError('conflicting frozen source stratum')
            previous_slot = frozen[key]['source_slot']
            if previous_slot is not None and source_slot is not None and previous_slot != source_slot:
                raise ValueError('conflicting frozen source slot')
            if source_slot is not None:
                frozen[key]['source_slot'] = source_slot
        else:
            frozen[key] = dict(source, source_slot=source_slot)
    joined = {}
    for execution in executions:
        attempt = execution.get('attempt')
        if not isinstance(attempt, dict):
            continue
        key = tuple(attempt.get(k) for k in ('network_id', 'source_sig', 'source_wallet', 'mint'))
        if key in frozen:
            joined.setdefault(key, []).append(execution)
    def blank():
        return dict(n=0, counts=dict.fromkeys(SAME_SLOT_COUNTS, 0), same_slot_rate=None)
    out = dict(blank(), strata={name: blank() for name in SAME_SLOT_STRATA},
               secondary=dict(warm_n=0, warm_same_slot_n=0, warm_same_slot_rate=None,
                              submitted_n=0, submitted_same_slot_n=0, submitted_same_slot_rate=None))
    for key, source in frozen.items():
        rows = joined.get(key, [])
        submitted = [row for row in rows if row['attempt'].get('tx_sig')]
        # Repeated diagnostic joins of one identity do not create a second source buy.
        signatures = {row['attempt']['tx_sig'] for row in submitted}
        if len(signatures) > 1:
            bucket = 'unresolved'  # competing execution identities must be reconciled first
        elif submitted:
            grades = [same_slot_receipt_evidence(source, row['attempt'], row.get('receipt'), row.get('fill'))
                      for row in submitted]
            resolved = [grade for grade in grades if grade['outcome'] != 'unknown']
            states = {(grade['outcome'], grade['slot_delta']) for grade in resolved}
            conflicts = any(grade['contradiction'] for grade in grades)
            if conflicts or len(states) != 1:
                bucket = 'unresolved'
            else:
                outcome, delta = next(iter(states))
                bucket = ('failed' if outcome == 'failed' else 'S+0' if outcome == 'same_slot'
                          else 'S+1' if delta == 1 else 'S+2+')
        elif not rows:
            bucket = 'missing'
        elif any(row['attempt'].get('terminal_status') not in (
                'refused', 'rejected', 'aborted', 'source_initiation_unknown', 'no_submission') for row in rows):
            bucket = 'unresolved'
        elif any(row['attempt'].get('terminal_status') != 'no_submission' for row in rows):
            bucket = 'refused'
        else:
            bucket = 'no_submission'
        for group in (out, out['strata'][source['stratum']]):
            group['n'] += 1
            group['counts'][bucket] += 1
        for label, eligible in (('warm', source.get('warm_hit') is True), ('submitted', bool(submitted))):
            if eligible:
                out['secondary'][label + '_n'] += 1
                out['secondary'][label + '_same_slot_n'] += bucket == 'S+0'
    for group in (out, *out['strata'].values()):
        if group['n']:
            group['same_slot_rate'] = group['counts']['S+0'] / group['n']
    for label in ('warm', 'submitted'):
        n = out['secondary'][label + '_n']
        if n:
            out['secondary'][label + '_same_slot_rate'] = out['secondary'][label + '_same_slot_n'] / n
    return out


EXECUTION_SPANS = {
    'attempt_shell_insert_ms': ('attempt_row', 'duration_ms'),
    'pretrace_total_ms': ('slot_receipt', 'pretrace_total_ms'),
    'pretrace_cpu_ms': ('slot_receipt', 'pretrace_cpu_ms'),
    'decode_ms': ('slot_receipt', 'decode_ms'),
    'receipt_to_enqueue_ms': ('slot_receipt', 'receipt_to_enqueue_ms'),
    'queue_ms': ('slot_receipt', 'queue_ms'),
    'proof_ready_ms': ('slot_receipt', 'receipt_to_proof_ms'),
    'proof_to_dispatch_ms': ('slot_pre_send', 'proof_to_dispatch_ms'),
    'wallet_ownership_ms': ('slot_receipt', 'wallet_ownership_ms'),
    'classification_reads_ms': ('slot_classification', 'duration_ms'),
    'reservation_ms': ('slot_reservation', 'duration_ms'),
    'unsigned_build_ms': ('slot_build', 'duration_ms'),
    'local_sign_ms': ('timing_local_sign', 'duration_ms'),
    'signed_identity_commit_ms': ('slot_transport_identity_bound', 'duration_ms'),
    'first_transport_dispatch_ms': ('slot_transport_dispatch', 'receipt_to_dispatch_ms'),
    'first_transport_write_ms': ('slot_transport_write', 'receipt_to_write_ms'),
    'transport_ack_ms': ('slot_transport_ack', 'duration_ms'),
}


def execution_stage_spans(attempt):
    """Explicit local spans; missing network writes/clocks remain unknown, never inferred."""
    raw = (attempt or {}).get('trace_json')
    trace = raw if isinstance(raw, dict) else _trace(attempt)
    events = trace.get('substage_events') or []
    out = dict.fromkeys(EXECUTION_SPANS)
    for event in events:
        if not isinstance(event, dict):
            continue
        for field, (name, key) in EXECUTION_SPANS.items():
            value = event.get(key)
            if (out[field] is None and event.get('name') == name and type(value) in (int, float)
                    and math.isfinite(value) and value >= 0):
                out[field] = value
    # Historical native traces have the same explicit pre-dispatch observation, before the seam.
    if out['first_transport_dispatch_ms'] is None:
        for event in events:
            if not isinstance(event, dict) or event.get('name') != 'slot_pre_send':
                continue
            value = event.get('receipt_to_dispatch_ms')
            if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                out['first_transport_dispatch_ms'] = value
                break
    return out


def execution_stage_summary(attempts):
    """Bounded caller-supplied population p50/p90 with n/missing for every stage."""
    from statistics import median
    rows = [execution_stage_spans(attempt) for attempt in attempts]
    out = {}
    for field in EXECUTION_SPANS:
        values = sorted(row[field] for row in rows if row[field] is not None)
        out[field] = dict(n=len(values), missing=len(rows) - len(values),
                          p50_ms=median(values) if values else None,
                          p90_ms=values[math.ceil(.9 * len(values)) - 1] if values else None)
    return out
