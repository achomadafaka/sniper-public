"""Live off-path worker for the dump-group blocker (curve + PumpSwap tapes). One process, one writer of data/desk/dump_group/cache.json. On every start it replays the
last 24 h of both tapes (no durable cursor), merges them in `known_at` order behind a per-venue watermark, feeds the pure detector (memecoin_desk_dump_group) and
publishes per-pool scores through the atomic cache helper at ~1 Hz. It never trades, sizes or signs and holds no lock the funded runner takes. A dead, frozen or lagging
worker publishes nothing fresh, so the cache expires into a typed unknown on the consumer side; it can never certify a coin clean."""
from __future__ import annotations

import heapq
import json
import logging
import math
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sniper.services import memecoin_desk_dump_group_source as S
from sniper.services.memecoin_desk_dump_group import BurstDetector
from sniper.services.memecoin_desk_dump_group_cache import publish_cache

logger = logging.getLogger(__name__)

MARGIN_S = 2.0                    # the writer's flush lag: rows with received_at older than this are visible once the capture is alive
HEARTBEAT_MAX_AGE_S = 20.0
LOOKBACK_S = 86400.0              # the detector's flag memory (24 h)
REPLAY_S = LOOKBACK_S             # what a restart replays by default (Alpha 10-26, revised): the full 24 h; --replay-hours shortens it, and the shortened memory is published (history_start, memory_s) and changes the >=5-burst bot counts too
PUBLISH_EVERY_S = 1.0
RETIRE_CHECK_EVERY_S = 30.0
ENV_PATH = "/home/bigpapi/Desktop/Sniper/.env"                    # the funded checkout's .env (a pinned release worktree has none)
RECHECK_EVERY_S = 5.0
BATCH = 20000
POLLS_PER_CYCLE = 2
MAX_BUFFER = 400_000              # merged rows waiting for the slower venue's watermark: the faster venue is not polled past this


def capture_alive(desk_dir, now):
    """The universe worker's own heartbeat: the capture is running and connected. Distinguishes a quiet market from a stopped writer."""
    try:
        beat = json.loads((Path(desk_dir) / "universe.heartbeat").read_text())
        ts = float(beat["ts"])
    except Exception:
        return False
    return math.isfinite(ts) and ts <= now + 1.0 and now - ts <= HEARTBEAT_MAX_AGE_S and beat.get("connected") is True


def curve_capture_off(desk_dir, env_path=None, now=None):
    """The curve tape is retired when a FRESH universe heartbeat says curve_capture=false OR MEMECOIN_DESK_CURVE_CAPTURE is 0/false/off in the funded .env (explicit authority).
    A stale, future or timestamp-less heartbeat says nothing (a leftover false must not keep a re-enabled capture retired, and after the age-out must not license PS passes);
    unreadable or absent sources mean ON: a still-running capture keeps its watermark and its pages."""
    now = time.time() if now is None else now
    try:
        beat = json.loads((Path(desk_dir) / "universe.heartbeat").read_text())
        ts = float(beat["ts"])
        if beat.get("curve_capture") is False and math.isfinite(ts) and ts <= now + 1.0 and now - ts <= HEARTBEAT_MAX_AGE_S:
            return True
    except Exception:
        pass
    try:
        for line in open(env_path or ENV_PATH):
            if line.startswith("MEMECOIN_DESK_CURVE_CAPTURE="):
                return line.split("=", 1)[1].strip().strip("\"'").lower() in ("0", "false", "off", "no")
    except OSError:
        pass
    return False


def producer_retired_since(desk_dir, now):
    """The universe producer's curve_retired_since from a FRESH heartbeat that says curve_capture=false; None if absent, stale, non-finite or in the future."""
    try:
        beat = json.loads((Path(desk_dir) / "universe.heartbeat").read_text())
        ts, since = float(beat["ts"]), beat.get("curve_retired_since")
        if beat.get("curve_capture") is False and isinstance(since, (int, float)) and not isinstance(since, bool) and math.isfinite(since) and math.isfinite(ts) \
                and ts <= now + 1.0 and now - ts <= HEARTBEAT_MAX_AGE_S and 0 < since <= now:
            return float(since)
    except Exception:
        pass
    return None


def gap_rows(desk_dir, day):
    """Authoritative producer gap rows of one curve day (worker_start, stream errors, ...)."""
    p = Path(desk_dir) / "gaps" / ("%s.db" % day)
    if not p.exists():
        return []
    try:
        con = S._ro(p)
        try:
            return [(float(a), float(a), "gap:%s" % c, None, "curve") for a, c in con.execute("select at, cause from gaps")]
        finally:
            con.close()
    except sqlite3.Error:
        return None                                              # an unreadable gap table is NOT "no gaps": the caller fails closed


def ps_gap_rows(desk_dir, since):
    """Whole-tape PumpSwap gap rows (worker_start, stream errors, disk_cap, decode_blocked); the per-event decode_loss / virtual_quote_unproven rows carry no pool and
    are attributed by pool continuity in the normalizer instead."""
    p = Path(desk_dir) / "pumpswap" / "tape.db"
    if not p.exists():
        return []
    try:
        con = S._ro(p)
        try:
            return [(float(a), float(a), "gap:%s" % c, None, "ps") for a, c in
                    con.execute("select at, cause from gaps_ps where at > ? and cause not in ('decode_loss', 'virtual_quote_unproven')", (since,))]
        finally:
            con.close()
    except sqlite3.Error:
        return None


RECHECK_GAP_S = 90.0                                            # covers recheck_slots (150 slots ~ 60 s) with margin


class Stream:
    """One venue's tail plus its watermark: no row with known_at below `wm` can still arrive from this venue."""

    def __init__(self, name, tail, start_wm):
        self.name, self.tail, self.wm = name, tail, float(start_wm)
        self.exhausted, self.failed, self.absent, self.last_consumed = False, False, False, None
        self.retired_at = None                                       # curve only: capture switched off at this host time

    def update(self, res, now, alive):
        self.exhausted, self.failed, self.absent = bool(res["exhausted"]), bool(res["failed"]), bool(res.get("missing"))
        cur = self.tail.cursor
        if isinstance(cur, tuple):
            cur = cur[1]
        if cur is not None:
            self.last_consumed = cur
            self.wm = max(self.wm, cur)
        if self.absent or (self.retired_at is not None and self.exhausted and not self.failed):
            self.wm = math.inf                                       # a drained retired venue stops holding the other venue's watermark
        elif self.exhausted and not self.failed and alive:
            self.wm = max(self.wm, now - MARGIN_S)


class Worker:
    def __init__(self, desk_dir, cache_path, heartbeat_path, exclusions, *, clock=time.time, batch=BATCH, detector=None, publish=publish_cache,
                 alive=capture_alive, recheck_slots=150, ps=True, replay_from=None, replay_hours=None):
        self.desk, self.cache_path, self.hb_path, self.clock, self.publish, self.alive = desk_dir, cache_path, heartbeat_path, clock, publish, alive
        now = clock()
        self.started = now
        self.replay_from = (now - (REPLAY_S if replay_hours is None else float(replay_hours) * 3600.0)) if replay_from is None else float(replay_from)
        start_day = (datetime.fromtimestamp(self.replay_from, timezone.utc)).date().isoformat()
        today = lambda: datetime.fromtimestamp(self.clock(), timezone.utc).date().isoformat()          # noqa: E731
        self.tail = S.CurveTail(desk_dir, start_day, batch=batch, today=today, recheck_slots=recheck_slots)
        self.streams = [Stream("curve", self.tail, self.replay_from)]
        self.ps_tail = None
        if ps:
            self.ps_tail = S.PSTail(desk_dir, self.replay_from, batch=batch)
            self.streams.append(Stream("ps", self.ps_tail, self.replay_from))
        self.excl = exclusions
        self.state = S.GroupState(detector or BurstDetector(), exclusions)
        self.heap, self._seq = [], 0
        self.warm, self.frozen, self.history_start = False, None, None
        self.cutoff, self.last_publish, self.next_recheck, self.last_error = None, 0.0, 0.0, None
        self.clock_anomaly, self.published, self._hole_seen, self._last_hb = False, 0, set(), 0.0
        self.recheck_failed = False
        self.producer_read_errors = 0
        self._retire_check_at = 0.0
        self._created, self._loss, self._loss_at = {}, {"fraction": None, "hours": 0}, None

    # ------------------------------------------------------------------------------------------------------------ one cycle
    def cycle(self):
        """Poll, merge, feed, recheck, publish when due. Returns the heartbeat dict. Never raises on a tape or ledger problem."""
        now = self.clock()
        if self.frozen:
            return self._heartbeat(now)
        self.excl.maybe_refresh()
        alive = self.alive(self.desk, now)
        self._check_retired(now)
        self._merge_producer_gaps()
        for st in sorted(self.streams, key=lambda x: x.wm)[:POLLS_PER_CYCLE if len(self.heap) < MAX_BUFFER else 1]:
            res = st.tail.poll()
            st.update(res, now, alive)
            for kind, item in res["items"]:
                if kind == "gap":
                    item = item if len(item) == 5 else tuple(item) + (st.name,)
                    self._push(item[0], "gap", item)
                else:
                    self._push(item["known_at"], "row", item)
        w = min((st.wm for st in self.streams), default=now)
        if w == math.inf:
            w = now - MARGIN_S
        try:
            while self.heap and self.heap[0][0] < w:
                _, _, kind, item = heapq.heappop(self.heap)
                if kind == "gap":
                    self.state.add_gap(*item)
                    continue
                if self.history_start is None:
                    self.history_start = item["known_at"]
                self.state.feed(item)
        except Exception as exc:                                    # a raised feed freezes the worker: no fresh publish, the cache expires into unknown
            self.state.errors += 1
            self.frozen, self.last_error = "detector_error", "%s: %s" % (type(exc).__name__, str(exc)[:160])
            return self._heartbeat(now)
        caught_up = all((st.exhausted and not st.failed) or st.absent for st in self.streams) and self.tail.day == datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        self.cutoff = w if self.state.last_known_at is not None else None
        if self.cutoff is not None and self.state.last_known_at is not None:
            self.cutoff = max(self.cutoff, self.state.last_known_at) if w == now - MARGIN_S or caught_up else w
        if caught_up and not self.warm and self.state.last_known_at is not None and not self.heap:
            self.warm = True
        if now >= self.next_recheck:
            self.next_recheck = now + RECHECK_EVERY_S
            for st in self.streams:
                late = st.tail.recheck()
                if late is None:
                    self.recheck_failed = True
                    ref = self.cutoff if self.cutoff is not None else now                # unknown late rows anywhere in the trailing slot window: that window is a gap, not "nothing late"
                    self.state.add_gap(ref - RECHECK_GAP_S, ref, "recheck_failed", None, st.name)
                for recv, pool in late or ():
                    self.state.add_gap(recv - 0.5, recv + 0.5, "late_row", pool, st.name)
            if self.cutoff:
                self.state.prune(self.cutoff)
        if now < self.last_publish:                                  # the host clock went backwards: publish nothing, say so (never reorder published_at)
            self.clock_anomaly = True
        elif self.warm and self.cutoff and now - self.last_publish >= PUBLISH_EVERY_S:
            self.clock_anomaly = False
            self._publish(now, alive)
        return self._heartbeat(now, alive=alive, caught_up=caught_up)

    def _push(self, at, kind, item):
        self._seq += 1
        heapq.heappush(self.heap, (at, self._seq, kind, item))

    def _check_retired(self, now):
        if now < self._retire_check_at:
            return
        self._retire_check_at = now + RETIRE_CHECK_EVERY_S
        curve = self.streams[0]
        off = curve_capture_off(self.desk, now=now)
        if off and curve.retired_at is None:
            curve.retired_at = min(now, producer_retired_since(self.desk, now) or now)           # the producer's own stamp when fresh and sane: a worker restart must not restart the 24 h clock
            logger.warning("dump_group: curve capture retired; the curve venue reports retired/incomplete, PumpSwap continues")
        elif not off and curve.retired_at is not None:               # switched back on: the off span is a hole, not a quiet market
            self.state.add_gap(curve.retired_at, now, "curve_capture_retired", None, "curve")
            curve.retired_at = None
            curve.wm = curve.last_consumed if curve.last_consumed is not None else self.replay_from       # back to a finite watermark: the curve holds the merged cutoff until it catches up

    def _merge_producer_gaps(self):
        days = {self.tail.day, (datetime.fromisoformat(self.tail.day) - timedelta(days=1)).date().isoformat()}
        found, failed = [], []
        for d in days:
            rows = gap_rows(self.desk, d)
            if rows is None:
                failed.append("curve")
            else:
                found += rows
        if self.ps_tail is not None and not self.streams[-1].absent:
            rows = ps_gap_rows(self.desk, self.replay_from)
            if rows is None:
                failed.append("ps")
            else:
                found += rows
        now = self.clock()
        for venue in sorted(set(failed)):                            # fail closed: unknown producer gaps cover the present, one 30 s bucket at a time, until the read works again
            self.producer_read_errors += 1
            bucket = int(now // 30) * 30.0
            g = (bucket - 30.0, bucket + 30.0, "producer_gap_read_failed", None, venue)
            if g not in self._hole_seen:
                self._hole_seen.add(g)
                logger.warning("dump_group producer gap table unreadable venue=%s; coverage incomplete until it reads", venue)
                self._push(g[0], "gap", g)
        for g in found:
            if g not in self._hole_seen:
                self._hole_seen.add(g)
                self._push(g[0], "gap", g)

    def _holes(self):
        holes = [(datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp(), datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp() + 86400, c, None, "curve")
                 for d, c in self.tail.holes]
        return holes + list(self.state.gaps)

    def _publish(self, now, alive):
        cutoff = min(self.cutoff, now)
        cov = {}
        for st in self.streams:
            if st.retired_at is not None:                            # before the absent check: a retired curve stops writing day files, which must read 'retired', not 'tape_missing'
                cov[st.name] = {"complete": False, "warm": self.warm, "retired": True, "retired_since": st.retired_at, "reason": "curve_capture_retired"}
                continue
            if st.absent:
                cov[st.name] = {"complete": False, "warm": False, "reason": "tape_missing"}
                continue
            cov[st.name] = S.venue_coverage(warm=self.warm, cutoff=min(st.wm, cutoff) if math.isfinite(st.wm) else cutoff, now=now, state=self.state, tail=st.tail,
                                            holes=self._holes(), exclusions=self.excl, capture_alive=alive, history_start=self.replay_from, venue=st.name)
        if "ps" not in cov:
            cov["ps"] = {"complete": False, "warm": False, "reason": "ps_not_enabled"}
        scores = self.state.scores(cutoff)
        self._attach_ps_partial(scores, cov, cutoff, now)
        if self.streams[0].retired_at is not None:                   # retired curve: no curve verdicts at all (unknown, never 'no dumps'), not even the last 120 s of buys
            scores = {p: sc for p, sc in scores.items() if sc.get("venue") != "curve"}
        self.publish(self.cache_path, scores, coverage=cov, cutoff=cutoff, published_at=now)
        self.last_publish, self.published = now, self.published + 1

    def _ps_created_at(self, pool):
        """The coin's create_pool receipt time from the PS tape (`pools`, indexed by pool), memoised; None = unknown (a failed or empty read is not a creation time)."""
        if pool in self._created:
            return self._created[pool]
        path = Path(self.desk) / "pumpswap" / "tape.db"
        try:
            con = S._ro(path)
            try:
                row = con.execute("select min(received_at) from pools where pool = ?", (pool,)).fetchone()
            finally:
                con.close()
        except sqlite3.Error:
            return None
        value = row[0] if row and isinstance(row[0], (int, float)) and not isinstance(row[0], bool) else None
        if value is not None:
            if len(self._created) > 4096:
                self._created.clear()
            self._created[pool] = value
        return value

    def _attach_ps_partial(self, scores, cov, cutoff, now):
        """Amendment 31: the venue loss fraction (cached 60 s) goes on the ps coverage block and every PASSING PumpSwap coin gets its own-pool window holes; the cache decides."""
        ps = cov.get("ps")
        if not isinstance(ps, dict) or ps.get("reason") in ("tape_missing", "ps_not_enabled"):
            return
        if self._loss_at is None or now - self._loss_at >= 60.0 or now < self._loss_at:
            self._loss, self._loss_at = S.ps_loss_fraction(self.desk, now), now
        ps["loss_fraction_24h"], ps["loss_hours"] = self._loss["fraction"], self._loss["hours"]
        gaps = self._holes()
        for pool, sc in scores.items():
            if sc.get("venue") == "ps" and sc.get("status") == "pass":
                sc["ps_window"] = S.ps_window_holes(gaps, pool, self._ps_created_at(pool), cutoff)

    def _heartbeat(self, now, alive=None, caught_up=None):
        hb = {"ts": now, "pid": os.getpid(), "warm": self.warm, "frozen": self.frozen, "last_error": self.last_error, "cutoff": self.cutoff,
              "lag_s": None if self.cutoff is None else round(now - self.cutoff, 3), "rows_fed": self.state.rows_fed, "episodes": len(self.state.episodes),
              "retracted": len(self.state.retracted), "buffer": len(self.heap), "published": self.published, "last_publish": self.last_publish,
              "capture_alive": alive, "caught_up": caught_up, "clock_anomaly": self.clock_anomaly, "exclusions": self.excl.state(), "uptime_s": round(now - self.started, 1),
              "cursor_day": self.tail.day, "cursor": list(self.tail.cursor) if self.tail.cursor else None, "late_rows": self.tail.late_rows, "db_errors": self.tail.db_errors,
              "dropped": dict(self.tail.norm.dropped),
              "curve_retired_since": self.streams[0].retired_at,
              "streams": {st.name: {"wm": None if not math.isfinite(st.wm) else st.wm, "exhausted": st.exhausted, "failed": st.failed, "absent": st.absent,
                                    "rows_fed": st.tail.rows_fed, "late_rows": st.tail.late_rows, "db_errors": st.tail.db_errors,
                                    "dropped": dict(st.tail.norm.dropped)} for st in self.streams}}
        if now - self._last_hb < 1.0 and not self.frozen:
            return hb
        self._last_hb = now
        try:
            Path(self.hb_path).parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(self.hb_path).with_suffix(".tmp")
            tmp.write_text(json.dumps(hb, sort_keys=True, default=str))
            os.replace(tmp, self.hb_path)
        except OSError:
            pass
        return hb
