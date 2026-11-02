"""Desk engine attention, the runner side (pre-registration docs/audits/2026-10-22-desk-attention-signals-
preregistration.md; Beta). Off the fill path: called from the funded exit sweep for open DESK positions only.

- quote_collapse: the exact sell price (the sweep's own exact preview, expected_usdc / qty) vs the same
  position's previous exact price within ATTENTION_N_S falls by >= Q %. Beyond the age bound the row is
  'unknown' (stale_mark). Never a display mid. The second form (vs the fresh U-tape curve price) is
  'unknown' (tape_price_unavailable) until the tape side publishes a price.
- peak_drawdown: a prompt fact only, carried in the same row (drop from executable_peak_price).
- Rows go to data/desk/attention/runner.db, written ONLY by the runner's one writer thread: the sweep only
  enqueues (bounded; full = a counted drop). A failure is logged and counted (attention_insert_dropped);
  insert latency is sampled in the writer.
- auto_tighten_rails(): the ONE tighten function, imported by the live path (memecoin_desk_risk_apply) and
  by the replay: trail to its minimum bound; stop the stricter of the current stop and
  1 - price*(1-S%)/entry, only while price*(1-S%) < entry. Tighten-only.

Thresholds (Q, Q2, S, N, N_t) are named knobs, unset until Gamma's dev-day addendum: unset = DRY (rows
record the measured value with fired NULL; nothing acts).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import queue
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

SOURCE = "runner"
DDL = ("CREATE TABLE IF NOT EXISTS attention(id TEXT PRIMARY KEY, position_id INTEGER NOT NULL, mint TEXT, "
       "signal TEXT NOT NULL, value_json TEXT, observed_at REAL NOT NULL, source TEXT NOT NULL, fired INTEGER, "
       "thresholds_version TEXT)")
STATS = {"attention_insert_dropped": 0, "attention_rows": 0}
_INSERT_MS: list[float] = []
_LAST: dict[int, tuple[float, float]] = {}          # position_id -> (observed_at, exact price)
_LOCK = threading.Lock()


def runner_db_path():
    from sniper.util.state_paths import state_path
    return Path(state_path("data/desk/attention/runner.db"))


def _knob(name):
    import sniper.settings.memecoin as cfg
    v = getattr(cfg, name, None)
    return float(v) if type(v) in (int, float) and math.isfinite(v) and v > 0 else None


def thresholds():
    """The frozen knobs. Q > Q2 is malformed (Q2 could then never act): Q2 is dropped with a warning
    (Gamma note 1)."""
    import sniper.settings.memecoin as cfg
    t = {"N_s": _knob("MEMECOIN_DESK_ATTENTION_N_S"), "Q_pct": _knob("MEMECOIN_DESK_ATTENTION_Q_PCT"),
         "Q2_pct": _knob("MEMECOIN_DESK_ATTENTION_Q2_PCT"), "S_pct": _knob("MEMECOIN_DESK_ATTENTION_S_PCT"),
         "version": str(getattr(cfg, "MEMECOIN_DESK_ATTENTION_THRESHOLDS_VERSION", "") or "unset")}
    if t["Q_pct"] is not None and t["Q2_pct"] is not None and t["Q_pct"] > t["Q2_pct"]:
        logger.warning("desk attention: Q %.2f > Q2 %.2f, Q2 disabled", t["Q_pct"], t["Q2_pct"])
        t["Q2_pct"] = None
    return t


def armed(t=None):
    """Nothing runs (no row, no enqueue, no work) while every threshold knob is unset (Alpha)."""
    t = t or thresholds()
    return any(t[k] is not None for k in ("N_s", "Q_pct", "Q2_pct", "S_pct"))


def event_id(position_id, signal, observed_at, source=SOURCE, bucket_s=1.0):
    """hash(position_id, signal, observed_at bucket, source): a restart replays nothing twice."""
    b = int(math.floor(float(observed_at) / bucket_s))
    return hashlib.sha256(("%s|%s|%d|%s" % (position_id, signal, b, source)).encode()).hexdigest()[:32]


def bump(key, n=1):
    """Every STATS increment, from the writer and the sweep threads, under _LOCK (Gamma LOW1)."""
    with _LOCK:
        STATS[key] = STATS.get(key, 0) + n


def latency_ms():
    with _LOCK:
        xs = sorted(_INSERT_MS)
    if not xs:
        return {"n": 0}
    pick = lambda q: xs[min(len(xs) - 1, int(q * (len(xs) - 1)))]
    return {"n": len(xs), "p50_ms": pick(0.5), "p90_ms": pick(0.9)}


_QUEUE: "queue.Queue" = queue.Queue(maxsize=2000)
_WRITER: list = []


def _writer_loop():
    """The ONE writer (Gamma BLOCK on 2c77de146: the sweep paid ~24 ms per synchronous connect+commit). A
    persistent WAL connection per path, synchronous=NORMAL; the sweep thread only enqueues."""
    conns = {}
    while True:
        path, row = _QUEUE.get()
        started = time.perf_counter()
        try:
            conn = conns.get(path)
            if conn is None:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(str(path), timeout=5, check_same_thread=False)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute(DDL)
                conns[path] = conn
            conn.execute("INSERT OR IGNORE INTO attention VALUES (?,?,?,?,?,?,?,?,?)", row)
            conn.commit()
            bump("attention_rows")
        except Exception:
            bump("attention_insert_dropped")
            logger.warning("desk attention insert dropped pos=%s signal=%s", row[1], row[3], exc_info=True)
            bad = conns.pop(path, None)
            if bad is not None:
                try:
                    bad.close()
                except Exception:
                    pass
        finally:
            with _LOCK:
                _INSERT_MS.append((time.perf_counter() - started) * 1000.0)
                del _INSERT_MS[:-1000]
            _QUEUE.task_done()


def _insert(path, row):
    """Enqueue only: never blocks the sweep. A full queue is a counted drop, never 'no signal'."""
    with _LOCK:
        if not _WRITER or not _WRITER[0].is_alive():
            t = threading.Thread(target=_writer_loop, name="desk-attention-writer", daemon=True)
            t.start()
            _WRITER[:] = [t]
    try:
        _QUEUE.put_nowait((str(path), row))
    except queue.Full:
        bump("attention_insert_dropped")


def flush(timeout=5.0):
    """Tests and shutdown: wait until the writer has drained the queue (bounded)."""
    deadline = time.monotonic() + timeout
    while _QUEUE.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.005)
    return not _QUEUE.unfinished_tasks


def on_exact_price(pos, price, *, now_epoch, path=None):
    """One sweep's exact price for a desk position: append the quote_collapse row (fired, dry or unknown).
    Desk positions only; never raises."""
    try:
        if pos.get("signal_source") != "desk":
            return None
        t = thresholds()
        if not armed(t):
            return None
        pid = int(pos["id"])
        peak = pos.get("executable_peak_price")
        value = {"form": "exact_vs_exact"}
        if type(price) not in (int, float) or not math.isfinite(price) or price <= 0:
            value.update(unknown="no_exact_price")
            fired = None
        else:
            if type(peak) in (int, float) and peak and peak > 0:
                value["peak_drawdown_pct"] = (price / peak - 1.0) * 100.0
            with _LOCK:
                prev = _LAST.get(pid)
                _LAST[pid] = (float(now_epoch), float(price))
                if len(_LAST) > 256:                       # closed positions age out (Gamma note 4)
                    for k in [k for k, v in _LAST.items() if now_epoch - v[0] > 3600]:
                        _LAST.pop(k, None)
            age_bound = t["N_s"]
            if prev is None:
                value.update(unknown="no_prior_exact")
                fired = None
            elif age_bound is not None and now_epoch - prev[0] > age_bound:
                value.update(unknown="stale_mark", prior_age_s=now_epoch - prev[0])
                fired = None
            else:
                drop = (1.0 - price / prev[1]) * 100.0
                value.update(drop_pct=drop, prior_age_s=now_epoch - prev[0], price=price, prior_price=prev[1])
                fired = None if t["Q_pct"] is None or age_bound is None else int(drop >= t["Q_pct"])
        row = (event_id(pid, "quote_collapse", now_epoch), pid, pos.get("mint"), "quote_collapse",
               json.dumps(value, sort_keys=True), float(now_epoch), SOURCE, fired, t["version"])
        _insert(path or runner_db_path(), row)
        return value
    except Exception:
        bump("attention_insert_dropped")
        logger.exception("desk attention failed pos=%s", pos.get("id"))
        return None


def auto_tighten_rails(effective, *, price, entry, s_pct, trail_min):
    """THE tighten (live and replay). Returns the rails dict for memecoin_desk_risk_apply.tightened, or {}
    when nothing would tighten. trail -> trail_min; stop -> 1 - price*(1-S)/entry only while that price is
    below entry and it is stricter than the current stop."""
    out = {}
    trail = float(effective.get("trail_pct") or 0.0)
    if trail_min and (trail == 0 or trail_min < trail):
        out["trail_pct"] = float(trail_min)
    if all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in (price, entry, s_pct)):
        stop_price = price * (1.0 - s_pct / 100.0)
        if stop_price < entry:
            stop = (1.0 - stop_price / entry) * 100.0
            current = float(effective.get("stop_pct") or 0.0)
            if stop > 0 and (current == 0 or stop < current):
                out["sl_pct"] = stop
    return out
