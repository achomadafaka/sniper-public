"""The committed receipt cut of the desk universe tape (moved from scripts/memecoin_desk_risk_watcher.py so the paper
engine and the RISK watcher read ONE proof): what time the tape is complete through, per venue, or None."""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

from sniper.services import memecoin_desk_risk as R


COMMIT_SLACK_S = 5.0        # the committed watermark may lag the host clock by this much (the worker commits about every second)


def capture_proof(desk_dir, now, venues=("curve",), window_s=300.0, *, pool=None, integrity=False, connection=None, dependency_cut=None, live_now=None, why=None):
    """The common committed receipt cut, or None. For EVERY venue the evidence uses: a fresh universe heartbeat, that venue's processed-and-committed
    watermark within COMMIT_SLACK_S of now (a 'connected' flag proves nothing: the heartbeat is written every second even when the stream stalls), and no capture
    gap (curve gaps table; PumpSwap point gaps and any legacy stop spans) inside the last `window_s`.
    Strict PS dependency_cut freezes the inclusive dependency end while now still checks current health/frontier.
    Unreadable or absent = not proven = False (the break is then unknown).
    live_now samples the clock just after the heartbeat is read: a frozen `now` from before a slow read would see the heartbeat
    ahead of it. Dependencies stay on the frozen values; only liveness moves. `why` receives the branch of a refusal."""
    def no(branch):
        if why is not None:why.append(branch)
        return None
    from sniper.services import memecoin_desk_structure_tape as T
    from sniper.services import memecoin_desk_ps_tape as PT
    from sniper.services import memecoin_desk_ps_losses as PSL
    try:
        beat = json.loads((Path(desk_dir) / "universe.heartbeat").read_text())
        ts = beat["ts"]
    except Exception:
        return no("heartbeat_unreadable")
    live = now if live_now is None else live_now()
    if live < now:return no("clock_regressed")     # a step back must not hide a heartbeat from the future behind the frozen time
    if not R._finite_number(ts):return no("heartbeat_unreadable")
    if ts > live:return no("heartbeat_ahead")
    if live - ts > T.HEARTBEAT_MAX_AGE_S:return no("heartbeat_stale")
    if beat.get("connected") is not True or not venues:return no("disconnected")
    # Frozen-price callers retain current liveness checks, but dependencies stop at their original target.
    frozen = dependency_cut is not None
    if frozen and (not integrity or not R._finite_number(dependency_cut) or not 0 <= dependency_cut <= now):
        return no("dependency_cut_invalid")
    loss_through = dependency_cut if frozen else now
    cuts, starts = [], []
    try:
        for venue in venues:
            if venue not in ("curve", "ps"):
                return no("venue_unknown")
            mark = beat.get("curve_committed_through" if venue == "curve" else "ps_committed_through")
            start = beat.get("curve_capture_from" if venue == "curve" else "ps_capture_from")
            if not R._finite_number(mark) or not R._finite_number(start) or not 0 <= start <= mark <= live:
                return no("commit_mark_invalid")
            if live - mark > COMMIT_SLACK_S:return no("commit_slack")
            cuts.append(mark)
            starts.append(start)
            requested_lo = max(0, now - window_s)
            lo = requested_lo if frozen and venue == "ps" else max(start, requested_lo)
            if venue == "curve" and T.gaps_between(desk_dir, lo, now):
                return no("gap")
            if venue == "ps":
                if beat.get("pumpswap_capture") is not True:
                    return no("capture_off")
                if not integrity and PT.ps_gaps(desk_dir, lo, now):
                    return no("gap")
                conn = connection if connection is not None else T.ro(PT.tape_path(desk_dir))
                if conn is None:
                    return no("tape_missing")
                try:
                    if integrity:
                        pending=beat.get('ps_pending_gaps',{})
                        if (not isinstance(pool,str) or not pool or not isinstance(pending,dict)
                                or type(beat.get('ps_gap_pending')) is not bool
                                or bool(pending)!=beat['ps_gap_pending']):
                            return no("pending_invalid")
                        if not conn.in_transaction:
                            conn.execute('BEGIN')
                        if frozen and start > requested_lo:
                            # A recovered post-target gap can reset the heartbeat's current run. Only real recorded
                            # selection+capture coverage may prove the earlier dependency interval; never backdate.
                            coverage_lo,coverage_hi=requested_lo,loss_through
                            if coverage_lo==coverage_hi:
                                # Discover an inclusive point whose stored frontier ends exactly here. The original
                                # target must still belong to the returned interval; partial coverage never qualifies.
                                coverage_lo=max(0.,math.nextafter(coverage_lo,-math.inf))
                                coverage_hi=math.nextafter(coverage_hi,math.inf)
                            intervals=PSL.pool_coverage(desk_dir,pool,coverage_lo,coverage_hi)
                            if not any(a<=requested_lo and b>=loss_through for a,b in intervals):
                                return no("coverage")
                            starts[-1]=requested_lo
                        if PSL.losses(desk_dir,pool,lo,math.nextafter(loss_through,math.inf),
                                      pending=pending,connection=conn):
                            return no("loss")
                    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='ps_gap_spans'").fetchone() and conn.execute("SELECT 1 FROM ps_gap_spans WHERE start_at<=? AND (end_at IS NULL OR end_at>=?) LIMIT 1",
                                    (loss_through if frozen and venue == "ps" else now, lo)).fetchone():
                        return no("gap")
                finally:
                    if connection is None:
                        conn.close()
    except (T.TapeUnreadable, PT.TapeUnreadable, PSL.LossUnreadable, sqlite3.Error):
        return no("unreadable")
    return {"from": max(starts), "through": min(cuts)}


RETIRED_BEAT_MAX_AGE_S = 20.0     # same freshness Lambda's dump-group worker requires for this flag


def curve_retired(desk_dir, now):
    """True when a FRESH universe heartbeat says the curve tape is retired (curve_capture is False), None when the
    heartbeat is unreadable, stale, from the future or lacks the field, False otherwise. Readers that find no curve
    rows use it to say 'curve_retired' instead of reporting an empty tape as zero activity (operator 2026-10-27)."""
    try:
        beat = json.loads((Path(desk_dir) / "universe.heartbeat").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(beat, dict) or not R._finite_number(beat.get("ts")) or not 0 <= now - beat["ts"] <= RETIRED_BEAT_MAX_AGE_S:
        return None
    flag = beat.get("curve_capture")
    return None if type(flag) is not bool else flag is False
