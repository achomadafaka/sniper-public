"""Keep the route quotes the funded exit path already computes instead of discarding them.

THE GAP. The exit path computes exact whole-bag route quotes constantly — executable-peak previews,
exit-decision previews, exit-confirmation audits, equity marks — and throws every VALUE away, keeping
only a ratcheted scalar. `route_quote_value` is populated on 0 of 105 current-era funded rows. So
there is no route-space price series anywhere, not because nothing computed one, but because nothing
persisted one.

WHY THAT MATTERS ENOUGH TO FIX. `memecoin_position_marks.executable_exit_value` is a reserved ALIAS
for `mid` on 3,246,299 of 3,246,299 rows, so every historical study of our own exits runs in CHART
space. Measured against real fills on fresh marks (n=69): median +0.28% but p10 −9.65%, p90 +20.14%,
range −33% to +76%. That dispersion is the ceiling on any conclusion drawn from the mark series, and
it is wider than the differences between the exit policies we would want to compare.

This is KEEPING, NOT FETCHING. The quotes already happen; nothing here adds an API call, a route
computation, or a millisecond of latency to a decision.

SEAM-TAGGED, BECAUSE THE SEAMS PRICE DIFFERENT THINGS. A peak preview, an exit-decision preview and
an equity mark are taken at different moments against different quantities. Pooling them into one
undifferentiated "route price" series would produce a number that means nothing — the same class of
error as a column carrying two units. Every row carries the seam that produced it, and a consumer
that does not filter by seam is holding a mixture.

BUFFERED, NEVER WRITTEN IN THE DECISION LOOP. Recording appends to memory; the maintenance pass
flushes. A DB write inside the exit hot path would put an instrument on the critical path of a money
decision, and the whole point is that this cannot affect what the rails do.

Observation doctrine throughout: never raises, never blocks, holds no lock the exit path wants, and
counts what it wrote AND what it dropped — an observer that silently stops must not look like a quiet
market.

GAPS ARE ROWS, NOT SILENCE (2026-10-16, Alpha's order, Gamma's PASS conditions). A hole in the price
series used to mean nothing: a failed quote, a full buffer and a restart all looked the same.
`memecoin_route_series_gaps` names each one, and no NULL or zero ever enters the price table:
  - `quote_miss`: a seam was handed a preview with no usable value. reason = the quote failure's
    class (`memecoin_quote_failures.QuoteFailure` value, else no_preview / zero_value / no_value)
    and the provider group, as "<class>|<group>".
  - `buffer_drop`: the bounded buffer dropped observations. One row per position and seam per
    drain, with the count and the first/last observed_at it lost.
  - `restart`: one row per open real position, stamped with the runner's start time, written by the
    writer thread when it starts. Every gap that ends at a restart is the restart's.

THE WRITER THREAD (MEMECOIN_ROUTE_SERIES_WRITER_THREAD, default on). The flush used to run in the
runner loop's maintenance pass, on the funded thread and its connection. A dedicated daemon thread
now drains the buffer every DRAIN_S on its own autocommit connection: one short BEGIN IMMEDIATE,
two executemany, COMMIT, and no read transaction left open. It measures how long it held the write
lock and how long it waited for it (snapshot `lock_hold_ms_p50/p90`, `lock_wait_ms_p50/p90`). While
it runs, the maintenance flush refuses, and its own drain refuses on any other thread. A crash loses
at most the last DRAIN_S of buffered rows; the next start's restart marker covers that hole. Knob
off: no thread, no restart marker, the maintenance flush as before.
"""
from __future__ import annotations

import collections
import logging
import json
import math
import sqlite3
import threading

logger = logging.getLogger(__name__)

# The seams. Named, because a consumer that pools them is holding a mixture.
SEAM_PEAK_PREVIEW = "peak_preview"        # the executable-peak ratchet's own read
SEAM_EXIT_DECISION = "exit_decision"      # the sweep's per-position exit read
SEAM_EXIT_AUDIT = "exit_audit"            # exact-balance confirmation at exit time
SEAM_EQUITY_MARK = "equity_mark"          # whole-book equity marking

KIND_QUOTE_MISS = "quote_miss"
KIND_BUFFER_DROP = "buffer_drop"
KIND_RESTART = "restart"
SEAM_ALL = "*"                            # a restart interrupts every seam

COUNTS = {"recorded": 0, "flushed": 0, "dropped_full": 0, "errors": 0,
          "gaps_recorded": 0, "gaps_flushed": 0, "gaps_dropped_full": 0, "refused_off_thread": 0,
          "restart_markers": 0}

_LOCK = threading.Lock()
_BUFFER = []
_GAPS = []
_DROPPED = {}                             # (position_id, network_id, mint, seam) -> [n, first, last]
_MAX_BUFFER = 2000                        # ~an hour of dense marking; drops oldest with a counter
DRAIN_S = 2.0
_HOLD_MS = collections.deque(maxlen=512)
_WAIT_MS = collections.deque(maxlen=512)
_WRITER = None


def reset_state():
    """Test hook. Production never calls this."""
    global _WRITER
    with _LOCK:
        _BUFFER.clear()
        _GAPS.clear()
        _DROPPED.clear()
        _HOLD_MS.clear()
        _WAIT_MS.clear()
        for k in COUNTS:
            COUNTS[k] = 0
    _WRITER = None


def _miss_reason(preview, expected, value_fn):
    if preview is None:
        cls = "no_preview"
    else:
        failure = value_fn(preview, "failure")
        if failure is not None:
            cls = str(getattr(failure, "value", failure))
        elif expected is None:
            cls = "no_value"
        else:
            cls = "zero_value" if expected == 0 else "bad_value"
    group = None
    if preview is not None:
        group = value_fn(preview, "provider_group") or value_fn(preview, "provider_id")
    return "%s|%s" % (cls, group or "unknown")


def _gap(pos, *, seam, kind, reason, observed_at, last=None, count=1):
    return {"position_id": pos.get("id"), "network_id": pos.get("network_id"),
            "mint": pos.get("mint"), "seam": str(seam), "kind": kind, "reason": reason,
            "observed_at": observed_at, "last_observed_at": last or observed_at, "count": count}


def _append_gap(row):
    with _LOCK:
        if len(_GAPS) >= _MAX_BUFFER:
            _GAPS.pop(0)
            COUNTS["gaps_dropped_full"] += 1
        _GAPS.append(row)
        COUNTS["gaps_recorded"] += 1


def record(pos, preview, *, seam, now_iso, value_fn=None, attempted=False):
    """Buffer one route observation. Cheap, in-memory, never raises.

    A preview carrying no usable value is DROPPED rather than stored as a null row: this series
    exists to hold real route prices, and a null here would be indistinguishable from a quote that
    priced the bag at nothing. It becomes a quote_miss gap row instead, except a None preview from a
    site that did not ask for a quote (`attempted` False): not asked is not failed.
    """
    try:
        if not pos or (preview is None and not attempted):
            return False
        if value_fn is None:
            from sniper.services.memecoin_real_runner import _preview_value as value_fn
        expected = None if preview is None else value_fn(preview, "expected_usdc")
        expected = None if expected is None else float(expected)
        if expected is None or not math.isfinite(expected) or expected <= 0:
            # not a price: a gap row says why, the price table never sees it
            _append_gap(_gap(pos, seam=seam, kind=KIND_QUOTE_MISS, observed_at=now_iso,
                             reason=_miss_reason(preview, expected, value_fn)))
            return False
        row = {
            "position_id": pos.get("id"), "mint": pos.get("mint"),
            "network_id": pos.get("network_id"), "seam": str(seam),
            "observed_at": now_iso,
            "quoted_at": value_fn(preview, "quoted_at"),
            "expected_usdc": expected,
            "open_qty": pos.get("open_qty"),
            # Per-unit price, so this is comparable to entry_price and positions.quote_price
            # without a conversion at read time. None when the quantity is unknown — never faked
            # from a nominal price, which is the mistake this whole series exists to escape.
            "unit_price": None,
            "quote_evidence_json": None,
        }
        evidence = value_fn(preview, "quote_evidence")
        if evidence is not None:
            try:
                row["quote_evidence_json"] = json.dumps(evidence, sort_keys=True, allow_nan=False)
            except (TypeError, ValueError):
                logger.warning("route evidence unavailable seam=%s", seam)
        try:
            qty = float(pos.get("open_qty") or 0.0)
            if qty > 0:
                row["unit_price"] = expected / qty
        except (TypeError, ValueError):
            pass
        with _LOCK:
            if len(_BUFFER) >= _MAX_BUFFER:
                lost = _BUFFER.pop(0)
                COUNTS["dropped_full"] += 1
                key = (lost["position_id"], lost["network_id"], lost["mint"], lost["seam"])
                note = _DROPPED.setdefault(key, [0, lost["observed_at"], lost["observed_at"]])
                note[0] += 1
                note[2] = max(note[2], lost["observed_at"])
            _BUFFER.append(row)
            COUNTS["recorded"] += 1
        return True
    except Exception:
        with _LOCK:
            COUNTS["errors"] += 1
        logger.exception("route series could not record a %s observation", seam)
        return False


def _take(limit):
    """Prices and gaps off the buffers, plus one buffer_drop row per position and seam that lost
    observations since the last take."""
    with _LOCK:
        batch = _BUFFER[:int(limit)]
        del _BUFFER[:len(batch)]
        gaps = _GAPS[:]
        _GAPS.clear()
        drops = dict(_DROPPED)
        _DROPPED.clear()
    for (pid, net, mint, seam), (n, first, last) in drops.items():
        gaps.append(_gap({"id": pid, "network_id": net, "mint": mint}, seam=seam,
                         kind=KIND_BUFFER_DROP, reason="buffer_full", observed_at=first,
                         last=last, count=n))
    return batch, gaps


def _write(conn, batch, gaps, write_fn):
    """Rows are taken off the buffer BEFORE the write; a failed write loses that batch rather than
    retrying forever against a broken table and growing without bound."""
    if not batch and not gaps:
        return 0
    try:
        if write_fn is None:
            from sniper.persistence import db_memecoin as m
            write_fn = m.record_route_series
        write_fn(conn, batch, gaps, on_lock=note_lock)
        with _LOCK:
            COUNTS["flushed"] += len(batch)
            COUNTS["gaps_flushed"] += len(gaps)
        return len(batch)
    except Exception:
        with _LOCK:
            COUNTS["errors"] += 1
        logger.exception("route series write failed; %d observation(s) and %d gap row(s) lost",
                         len(batch), len(gaps))
        return 0


def flush(conn, *, write_fn=None, limit=500):
    """The maintenance-pass write, for MEMECOIN_ROUTE_SERIES_WRITER_THREAD off. Refuses while the
    writer thread runs: the funded thread's connection must not take the write lock for this."""
    if _WRITER is not None and _WRITER.is_alive():
        return 0                          # by design while the writer runs; not a refusal count
    batch, gaps = _take(limit)
    return _write(conn, batch, gaps, write_fn)


def _drain(conn, *, write_fn=None, limit=500):
    """The writer thread's write. Refuses on any other thread, and counts it: a caller other than
    the writer reaching this is a defect."""
    if _WRITER is None or threading.current_thread() is not _WRITER:
        with _LOCK:
            COUNTS["refused_off_thread"] += 1
        return 0
    batch, gaps = _take(limit)
    return _write(conn, batch, gaps, write_fn)


def note_lock(wait_ms, hold_ms):
    """How long one write waited for, then held, the ledger's write lock."""
    with _LOCK:
        _WAIT_MS.append(float(wait_ms))
        _HOLD_MS.append(float(hold_ms))


def _connect_writer(db_path):
    conn = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


class _Writer(threading.Thread):
    def __init__(self, db_path, started_at, drain_s, connect, open_positions_fn):
        super().__init__(name="route-series-writer", daemon=True)
        self.db_path, self.started_at, self.drain_s = db_path, started_at, float(drain_s)
        self.connect, self.open_positions_fn = connect, open_positions_fn
        self.stop_event = threading.Event()
        self.conn = None

    def run(self):
        try:
            self.conn = self.connect(self.db_path)
            self._mark_restart()
        except Exception:
            with _LOCK:
                COUNTS["errors"] += 1
            logger.exception("route series writer could not start its connection or marker")
            if self.conn is None:
                return
        while not self.stop_event.wait(self.drain_s):
            self._drain_logged()
        self._drain_logged()

    def _drain_logged(self):
        try:
            _drain(self.conn)
        except Exception:
            with _LOCK:
                COUNTS["errors"] += 1
            logger.exception("route series drain failed")

    def _mark_restart(self):
        if self.open_positions_fn is None:
            from sniper.persistence import db_memecoin as m
            self.open_positions_fn = m.open_real_position_keys
        rows = [_gap(p, seam=SEAM_ALL, kind=KIND_RESTART, reason="runner_start",
                     observed_at=self.started_at) for p in self.open_positions_fn(self.conn)]
        if rows:
            _write(self.conn, [], rows, None)
        with _LOCK:
            COUNTS["restart_markers"] += len(rows)


def start_writer(*, started_at, db_path=None, drain_s=DRAIN_S, connect=_connect_writer,
                 open_positions_fn=None):
    """Start the writer thread once per process; returns it. `started_at` is the runner's start."""
    global _WRITER
    if _WRITER is not None and _WRITER.is_alive():
        return _WRITER
    if db_path is None:
        from sniper.persistence.db_core import get_db_path
        db_path = get_db_path()
    _WRITER = _Writer(db_path, started_at, drain_s, connect, open_positions_fn)
    _WRITER.start()
    return _WRITER


def _pct(xs, q):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))], 3) if xs else None


def snapshot():
    """Heartbeat projection.

    THE ASSEMBLY IS A WHITELIST — name this field where the heartbeat is built or it silently never
    appears, as the RPC pool's `degraded` did on 2026-08-25.
    """
    with _LOCK:
        out = dict(COUNTS)
        out["buffered"] = len(_BUFFER)
        out["gaps_buffered"] = len(_GAPS)
        out["writer_thread"] = bool(_WRITER is not None and _WRITER.is_alive())
        out["lock_hold_ms_p50"], out["lock_hold_ms_p90"] = _pct(_HOLD_MS, .5), _pct(_HOLD_MS, .9)
        out["lock_wait_ms_p50"], out["lock_wait_ms_p90"] = _pct(_WAIT_MS, .5), _pct(_WAIT_MS, .9)
        return out
