"""The slot lane's own decision for each routed source buy, for fill timing's "why not S+1"
(operator via Alpha, 2026-10-02: "never routed it" does not say what went wrong).

`note` is one bounded deque append, called on the slot lane's routed thread and on the slot
signal's socket thread when a routed pair expires: no I/O, one existing memory lock. A runner daemon
thread (`run_drain`) moves the notes every second into `memecoin_slot_lane_skips` in the timing file
`data/memecoin_fill_timing.db`, never the ledger, so it never takes the write lock the fill path's
attempt and reservation writes wait on (Gamma M1). A note dropped by the bound or by a failed write
is lost: the cause then reads "slot lane never saw it", which is what we would have known anyway.
"""
from __future__ import annotations

import collections
import logging
import json
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

RING_MAX = 4096
_RING = collections.deque(maxlen=RING_MAX)
# THE SAME DECISION IN MEMORY (Alpha, 2026-10-06 night): the drain writes a note up to a second late,
# and the on-chain lane's fast-lane-only refusal comes ~0.3 s after the lane decided, so that refusal
# reads the decision here (`decision_for`) to name the real blocker. Bounded; same precedence as
# `flush`: an expiry or routing hint never replaces an actual lane result.
LAST_MAX = 4096
_LAST = collections.OrderedDict()
_LAST_LOCK = threading.Lock()


# Decoder hints, never a lane result: a later lane decision replaces them, they never replace one.
# metadata_invalid (2026-10-14): the processed validator refused a tracked signer's transaction.
HINT_OUTCOMES = ("pair_expired", "metadata_invalid")


def _rank(outcome):
    return 0 if outcome in HINT_OUTCOMES else 1 if outcome in ("lane", "handoff") else 2


def note(signature, outcome, reason=None, *, attempt_id=None, evidence=None):
    """Record the slot lane's decision for one source signature. Never raises."""
    try:
        with _LAST_LOCK:
            old = _LAST.get(signature)
            if old is not None and outcome in HINT_OUTCOMES and old[:2] == (outcome, reason):
                return                                      # one hint per signature (both feeds)
            if old is None or _rank(outcome) >= _rank(old[0]):
                _LAST[signature] = (outcome, reason, attempt_id)
                _LAST.move_to_end(signature)                # refresh recency without removing the key
                _RING.append((signature, outcome, reason, time.time(), dict(evidence)) if evidence else
                             (signature, outcome, reason, time.time()))
            if len(_LAST) > LAST_MAX:
                for sig in list(_LAST)[:len(_LAST) - LAST_MAX // 2]:
                    del _LAST[sig]
    except Exception:  # noqa: BLE001 - a note must never raise into the lane
        logger.debug("slot lane decision memory failed", exc_info=True)


def finish_note(signature, expected_attempt_id, outcome, reason=None, *, evidence=None):
    """Complete only the current submitted owner; stale settlements never reach the drain."""
    with _LAST_LOCK:
        old = _LAST.get(signature)
        if (expected_attempt_id is None or old is None or old[0] != "submitted"
                or old[2] != expected_attempt_id):
            return False
        _LAST[signature] = (outcome, reason, expected_attempt_id)
        _RING.append((signature, outcome, reason, time.time(), dict(evidence)) if evidence else
                     (signature, outcome, reason, time.time()))
        return True


def decision_for(signature):
    """(outcome, reason) the slot lane last recorded for this source signature, or None."""
    with _LAST_LOCK:
        row = _LAST.get(signature)
        return row[:2] if row else None


def peek_decision_for(signature):
    """CPython's atomic get sees one immutable tuple; replacement never removes the key."""
    row = _LAST.get(signature)
    return row[:2] if row else None


def drain(limit=RING_MAX):
    out = []
    while len(out) < limit:
        try:
            out.append(_RING.popleft())
        except IndexError:
            break
    return out


def flush(conn, rows):
    """Write drained notes; a later routing/expiry hint cannot erase an actual lane result."""
    params = [(sig, outcome, reason, datetime.fromtimestamp(at, timezone.utc).isoformat())
              for sig, outcome, reason, at in (row[:4] for row in rows) if sig]
    conn.executemany("INSERT INTO memecoin_slot_lane_skips (signature, outcome, reason, noted_at) "
                     "VALUES (?,?,?,?) ON CONFLICT(signature) DO UPDATE SET "
                     "outcome=excluded.outcome, reason=excluded.reason, noted_at=excluded.noted_at "
                     "WHERE CASE WHEN excluded.outcome IN ('pair_expired','metadata_invalid') THEN 0 "
                     "WHEN excluded.outcome IN ('lane','handoff') THEN 1 ELSE 2 END >= "
                     "CASE WHEN memecoin_slot_lane_skips.outcome IN ('pair_expired','metadata_invalid') "
                     "THEN 0 WHEN memecoin_slot_lane_skips.outcome IN ('lane','handoff') THEN 1 ELSE 2 END",
                     params)
    rich = [row for row in rows if len(row) > 4]
    if rich:
        columns = {r[1] for r in conn.execute('PRAGMA table_info(memecoin_slot_lane_skips)')}
        if 'evidence_json' not in columns:
            conn.execute('ALTER TABLE memecoin_slot_lane_skips ADD COLUMN evidence_json TEXT')
        for sig, outcome, reason, _at, evidence in rich:
            complete = bool(evidence.get('mint') and evidence.get('source_wallet')
                            and evidence.get('independent_inputs_complete'))
            evidence['eligibility'] = ('eligible' if outcome in ('submitted', 'filled') else
                'independent_policy_refused' if complete and evidence.get('policy_refused') else
                'eligibility_unknown')
            conn.execute('UPDATE memecoin_slot_lane_skips SET evidence_json=? WHERE signature=? '
                         'AND outcome IS ? AND reason IS ?',
                         (json.dumps(evidence, allow_nan=False), sig, outcome, reason))
    conn.commit()
    return len(params)


EMPTY_DECODE = 'empty_decode_unknown'


def flush_journal(conn, snapshot, cursor):
    """Persist one bounded immutable snapshot; the caller owns the post-commit cursor."""
    rows = [r for r in snapshot['rows'] if r['sequence'] > cursor][:512]
    if not rows:
        return cursor
    first = rows[0]
    if first['sequence'] > cursor + 1:
        gap = dict(producer_run_id=first['producer_run_id'], sequence=first['sequence'] - 1,
            component='receipt_journal', reason='producer_overwrite',
            connection_generation=first.get('connection_generation', 0),
            mono_start=snapshot['evicted_mono_start'] if cursor == 0 else None,
            mono_end=first.get('recv_monotonic', first.get('mono_start')),
            lost_count=first['sequence'] - cursor - 1)
        conn.execute('INSERT OR IGNORE INTO memecoin_execution_coverage VALUES (?,?,?,?,?,?,?,?,?)',
            tuple(gap[k] for k in ('producer_run_id', 'sequence', 'component', 'reason',
                'connection_generation', 'mono_start', 'mono_end', 'lost_count')) +
            (json.dumps(gap, allow_nan=False),))
    columns = {
        'source': ('memecoin_signal_receipts', ('producer_run_id', 'sequence', 'network_id',
            'source_sig', 'source_wallet', 'mint', 'pool', 'venue', 'feed', 'recv_monotonic',
            'boot_id', 'clock_domain', 'connection_generation', 'roster_generation',
            'config_generation', 'decode_state')),
        'processed': ('memecoin_processed_receipts', ('producer_run_id', 'sequence', 'network_id',
            'our_sig', 'slot', 'error_state', 'recv_monotonic', 'boot_id', 'clock_domain',
            'connection_generation')),
        'gap': ('memecoin_execution_coverage', ('producer_run_id', 'sequence', 'component', 'reason',
            'connection_generation', 'mono_start', 'mono_end', 'lost_count'))}
    for row in rows:
        table, names = columns[row['kind']]
        evidence = dict(row)
        if row['kind'] == 'source' and row.get('decode_state') == EMPTY_DECODE:
            # RECEIPT BLOAT (Gamma a61a5bb27, 2026-10-21): 99.6% of receipts were empty decodes,
            # each repeating the whole ~7.1 KB roster. The row itself stays (identity before any
            # network I/O); roster_generation, the roster's sha256, still names the exact roster.
            evidence.pop('roster_json', None)
        values = tuple(row.get(k) for k in names) + (json.dumps(evidence, allow_nan=False),)
        conn.execute('INSERT OR IGNORE INTO ' + table + ' VALUES (' +
                     ','.join('?' for _ in values) + ')', values)
    conn.commit()
    return rows[-1]['sequence']


def run_drain(stop, *, interval_s=1.0, connect=None, journals=()):
    """Timing-only background writer; receipts retry independently of legacy skip batches."""
    from sniper.services import memecoin_fill_timing as ft
    connect = connect or ft.connect
    conn, last_error, cursors = None, 0.0, {}
    try:
        while not stop.wait(interval_s):
            rows = drain()
            for journal in ((None,) if rows else ()) + tuple(journals):
                try:
                    if journal is None:
                        if conn is None:
                            conn = connect()
                        flush(conn, rows)
                        continue
                    snapshot = journal.snapshot()
                    run = snapshot['producer_run_id']
                    cursor = cursors.get(run, 0)
                    if not any(r['sequence'] > cursor for r in snapshot['rows']):
                        continue
                    if conn is None:
                        conn = connect()
                    cursors[run] = flush_journal(conn, snapshot, cursor)
                except Exception:
                    if conn is not None:
                        try:
                            conn.rollback()
                            conn.close()
                        except Exception:
                            pass
                    conn = None
                    if time.monotonic() - last_error >= 60.0:
                        last_error = time.monotonic()
                        logger.exception('slot lane timing drain failed; receipts retained for retry')

    finally:
        # Caller-provided test connections remain inspectable; production owns its handle.
        if conn is not None and connect is ft.connect:
            conn.close()
