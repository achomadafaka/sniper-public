#!/usr/bin/env python3
"""Sample funded Jupiter load from the runner heartbeat, and report it conditioned on book occupancy.

WHY THIS IS NOT A SINGLE READ. `JUPITER_LOAD` (sniper/execution/solana_real_seams.py:263) is a
module-level dict in the runner process. It resets to zero on every restart and deploys are frequent,
so reading it once at the 24h mark reports only hours-since-last-restart. That is an UNDERCOUNT, and
it fails in the direction that would wrongly greenlight a second consumer of the shared Jupiter IP.

WHY OCCUPANCY IS CONDITIONED ON. Funded Jupiter load is driven by open positions -- each one is
quoted for its exact exit on a cadence. A new consumer stacks on the funded PEAK (a full book,
MEMECOIN_MAX_CONCURRENT=7), so a rate measured at occupancy 0 says nothing about occupancy 7.

WHY OCCUPANCY IS COMPUTED, NOT SAMPLED. Funded positions are short: median lifetime 198s, p10 23s,
18% under 60 seconds (n=164, since 2026-08-19). A snapshot taken on a sampling cadence therefore
MISSES whole positions between samples and reads their interval as an empty book -- an undercount, in
the direction that would wrongly greenlight a second consumer. `opened_at` and `closed_at` are exact
and already in the ledger, so occupancy is reconstructed per interval by overlap at report time and
carries no sampling error. The sampled count is kept only as a cross-check.

Read-only throughout: the heartbeat is a file read, the ledger is opened `mode=ro`.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from datetime import datetime, timezone

HEARTBEAT = "data/memecoin_runner.heartbeat"
LEDGER = "sniper_tracker.db"
OUT = "data/jupiter_load_samples.jsonl"


def live_cap(default=3):
    """MEMECOIN_MAX_CONCURRENT as the runner actually has it.

    Not the rollover spec's 7: that value is not what runs, and projecting a peak at 7 would
    over-refuse a new consumer by more than double. Env first, then .env, then a conservative 3.
    """
    v = os.environ.get("MEMECOIN_MAX_CONCURRENT")
    if v:
        try:
            return int(v)
        except ValueError:
            pass
    try:
        with open(".env") as fh:
            for line in fh:                      # only this key is read; nothing else is touched
                if line.startswith("MEMECOIN_MAX_CONCURRENT="):
                    return int(line.split("=", 1)[1].strip())
    except (OSError, ValueError):
        pass
    return default


def entry_attempts(start_wall, end_wall):
    """Funded entry attempts in the window. Each carries its own Jupiter quote cost."""
    s_iso = datetime.fromtimestamp(start_wall, tz=timezone.utc).isoformat()
    e_iso = datetime.fromtimestamp(end_wall, tz=timezone.utc).isoformat()
    try:
        con = sqlite3.connect(f"file:{LEDGER}?mode=ro", uri=True, timeout=30)
        try:
            return con.execute("SELECT COUNT(*) FROM memecoin_entry_attempts "
                               "WHERE decision_started_at >= ? AND decision_started_at < ?",
                               (s_iso, e_iso)).fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return None


def mean_occupancy(start_wall, end_wall):
    """Exact time-weighted mean open funded positions over [start, end], from the ledger.

    Reconstructed by overlap rather than sampled, because funded positions are shorter than any
    sane sampling cadence and a snapshot silently reads a whole position as an empty book.
    """
    span = end_wall - start_wall
    if span <= 0:
        return None
    s_iso = datetime.fromtimestamp(start_wall, tz=timezone.utc).isoformat()
    e_iso = datetime.fromtimestamp(end_wall, tz=timezone.utc).isoformat()
    try:
        con = sqlite3.connect(f"file:{LEDGER}?mode=ro", uri=True, timeout=30)
        try:
            rows = con.execute(
                "SELECT opened_at, closed_at FROM memecoin_positions "
                "WHERE execution_mode='real' AND opened_at < ? "
                "  AND (closed_at IS NULL OR closed_at > ?)", (e_iso, s_iso)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    total = 0.0
    for opened, closed in rows:
        try:
            o = datetime.fromisoformat(opened).timestamp()
        except (TypeError, ValueError):
            continue
        try:
            cl = datetime.fromisoformat(closed).timestamp() if closed else end_wall
        except (TypeError, ValueError):
            cl = end_wall
        total += max(0.0, min(cl, end_wall) - max(o, start_wall))
    return total / span


def open_funded_positions():
    """Snapshot book size, kept as a cross-check only. Never a writable handle."""
    try:
        con = sqlite3.connect(f"file:{LEDGER}?mode=ro", uri=True, timeout=30)
        try:
            return con.execute("SELECT COUNT(*) FROM memecoin_positions "
                               "WHERE is_open=1 AND execution_mode='real'").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return None            # unknown, never 0 -- 0 is a claim about the book


def sample():
    with open(HEARTBEAT) as fh:
        h = json.load(fh)
    jl = h.get("jupiter_load") or {}
    return {
        "wall": time.time(),
        "hb_ts": h.get("ts"),
        "started_at": h.get("started_at"),      # segment identity: a change means a restart
        "quotes": jl.get("quotes"),
        "rate_limited": jl.get("rate_limited"),
        "errors": jl.get("errors"),
        # WHICH HOST 429'd (added 2026-08-27 with the funded key). Without it the 26.4% measured
        # that morning could not be attributed, so "did the key help" was unanswerable in the only
        # direction that matters. Absent on a pre-key runner, which reads as None, never as {}.
        "by_host": jl.get("by_host"),
        "keyed": jl.get("keyed"),
        "key_role": jl.get("key_role"),
        # ⚠️ WHETHER THE KEY WAS BEING REJECTED (added 2026-09-24, after the 401 incident). On
        # 2026-09-24 the operator's new Jupiter key answered 401 from the 16:01:06Z restart, the
        # funded lane fell back to keyless `lite-api` and 119 quotes were rate-limited before the
        # old key was restored at 18:29:50Z. This file recorded `keyed: true` and the right
        # `key_role` throughout, because both describe what was CONFIGURED, not whether it worked
        # -- so on this series the incident was invisible except as a rate-limit jump, and I nearly
        # reported "auth_failed 0" from a field that was never written.
        #
        # `jl.get` returns None when the runner predates the field, so an OLD ROW READS AS ABSENT
        # and never as a zero. Zero is a claim that the key was accepted, and the 148 samples of
        # the incident window are entitled to make no claim at all.
        "auth_failed": jl.get("auth_failed"),
        "open_positions": open_funded_positions(),
        "events": h.get("events"),
    }


def report(path):
    rows = [json.loads(l) for l in open(path) if l.strip()]
    rows = [r for r in rows if r.get("quotes") is not None and r.get("started_at")]
    if len(rows) < 2:
        print(f"{len(rows)} sample(s): need at least 2 to form an interval. Nothing to report.")
        return
    rows.sort(key=lambda r: r["wall"])

    segments, cur = [], [rows[0]]
    for r in rows[1:]:
        # A restart is a NEW segment. Detect it by identity, not by the counter going backwards:
        # a restart during an idle stretch leaves the counter at 0 and a decrease never shows.
        if r["started_at"] != cur[-1]["started_at"] or r["quotes"] < cur[-1]["quotes"]:
            segments.append(cur)
            cur = [r]
        else:
            cur.append(r)
    segments.append(cur)

    by_occ, observed_s, total_q, total_429, total_err, stalls = {}, 0.0, 0, 0, 0, 0
    for seg in segments:
        for a, b in zip(seg, seg[1:]):
            dt = b["wall"] - a["wall"]
            if dt <= 0:
                continue
            if a["hb_ts"] is not None and b["hb_ts"] is not None and b["hb_ts"] == a["hb_ts"]:
                stalls += 1        # heartbeat frozen: the runner was not running. Not observed time.
                continue
            dq = b["quotes"] - a["quotes"]
            observed_s += dt
            total_q += dq
            total_429 += (b["rate_limited"] or 0) - (a["rate_limited"] or 0)
            total_err += (b["errors"] or 0) - (a["errors"] or 0)
            occ = mean_occupancy(a["wall"], b["wall"])       # exact, not the sampled snapshot
            key = "unknown" if occ is None else round(occ)
            d = by_occ.setdefault(key, {"s": 0.0, "q": 0, "n": 0, "peak": 0.0})
            d["s"] += dt; d["q"] += dq; d["n"] += 1
            d["peak"] = max(d["peak"], dq / dt * 3600.0)

    span = rows[-1]["wall"] - rows[0]["wall"]
    print(f"samples {len(rows)}   segments {len(segments)} (restarts: {len(segments)-1})   "
          f"frozen-heartbeat intervals skipped: {stalls}")
    print(f"wall span {span/3600:.2f}h   observed {observed_s/3600:.2f}h   "
          f"coverage {100*observed_s/span if span else 0:.0f}%")
    print(f"quotes {total_q}   rate_limited {total_429}   errors {total_err}")
    if observed_s:
        print(f"overall {total_q/observed_s*3600:.1f} quotes/hour  "
              f"({total_q/observed_s:.4f}/sec) across ALL occupancies -- see the split below")
    print("\nopen positions | observed |  quotes | mean q/h | peak q/h")
    for k in sorted(by_occ, key=lambda x: (x == "unknown", x)):
        d = by_occ[k]
        mean = d["q"] / d["s"] * 3600.0 if d["s"] else 0.0
        print(f"{str(k):>14} | {d['s']/3600:7.2f}h | {d['q']:7d} | {mean:8.1f} | {d['peak']:8.1f}")

    seen = [k for k in by_occ if isinstance(k, int)]
    cap = live_cap()
    missing = [n for n in range(cap + 1) if n not in seen]
    if missing:
        print(f"\nNOT MEASURED at occupancy {missing} (cap {cap}). The peak-book rate that a second "
              f"Jupiter consumer would stack onto is UNOBSERVED -- do not extrapolate to it.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="append one sample and exit (cron form)")
    ap.add_argument("--loop", type=float, metavar="SECONDS", help="sample forever at this cadence")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--project", action="store_true",
                    help="constructed peak from per-position attribution")
    ap.add_argument("--limit-per-hour", type=float)
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args()

    if a.project:
        return project(a.out, a.limit_per_hour)
    if a.report:
        return report(a.out)
    if not (a.once or a.loop):
        ap.error("choose --once, --loop SECONDS, --report, or --project")

    while True:
        try:
            with open(a.out, "a") as fh:
                fh.write(json.dumps(sample()) + "\n")
        except (OSError, ValueError, KeyError) as exc:
            print(f"{datetime.now().isoformat()} sample failed: {exc!r}", flush=True)
        if a.once:
            return
        time.sleep(a.loop)



# --- projection -------------------------------------------------------------------------------
# A CONSTRUCTED peak, not an observed one. The funded book is occupied under three hours on a
# typical day and almost never at the cap, so waiting for a natural full-book interval could take
# many days. This projects the peak from per-position attribution instead, which is a WEAKER claim
# than measuring it, and every report says so in those words.

MIN_IDLE_INTERVALS = 10        # below this the floor is noise
MIN_POS_SAMPLES = 15           # below this a p95 is not a p95
MIN_R2 = 0.70                  # below this the linear extrapolation is not defensible


def _p(vals, q):
    if not vals:
        return None
    v = sorted(vals)
    return v[min(len(v) - 1, int(q * len(v)))]


def _fit(xs, ys):
    """Least squares slope/intercept/R^2. Returns None when x has no spread to fit."""
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 1e-12:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    inter = my - slope * mx
    sst = sum((y - my) ** 2 for y in ys)
    ssr = sum((y - (inter + slope * x)) ** 2 for x, y in zip(xs, ys))
    return slope, inter, (1.0 - ssr / sst if sst > 1e-12 else 1.0)


def project(path, limit_per_hour=None):
    rows = [json.loads(l) for l in open(path) if l.strip()]
    rows = [r for r in rows if r.get("quotes") is not None and r.get("started_at")]
    rows.sort(key=lambda r: r["wall"])

    iv = []
    for a, b in zip(rows, rows[1:]):
        dt = b["wall"] - a["wall"]
        if dt <= 0 or b["started_at"] != a["started_at"] or b["quotes"] < a["quotes"]:
            continue                                   # restart boundary: not an interval
        if a["hb_ts"] is not None and b["hb_ts"] == a["hb_ts"]:
            continue                                   # runner not running
        occ = mean_occupancy(a["wall"], b["wall"])
        if occ is None:
            continue
        iv.append({"dt": dt, "rate": (b["quotes"] - a["quotes"]) / dt * 3600.0, "occ": occ,
                   "att": entry_attempts(a["wall"], b["wall"]) or 0,
                   "r429": (b["rate_limited"] or 0) - (a["rate_limited"] or 0)})

    print(f"intervals {len(iv)}   observed {sum(i['dt'] for i in iv)/3600:.2f}h   "
          f"cap {live_cap()} (live .env, not the spec's 7)")
    if not iv:
        print("\nNOTHING TO PROJECT: no usable intervals yet.")
        return

    idle = [i for i in iv if i["occ"] < 0.05 and i["att"] == 0]
    withpos = [i for i in iv if i["occ"] >= 0.5]
    r429 = sum(i["r429"] for i in iv)
    occ_seen = sorted({round(i["occ"]) for i in iv})

    print(f"idle intervals {len(idle)}   occupied intervals {len(withpos)}   "
          f"occupancy levels seen {occ_seen}   429s observed {r429}")

    blocked = []
    if len(idle) < MIN_IDLE_INTERVALS:
        blocked.append(f"idle floor needs >={MIN_IDLE_INTERVALS} idle intervals, have {len(idle)}")
    if len(withpos) < MIN_POS_SAMPLES:
        blocked.append(f"per-position p95 needs >={MIN_POS_SAMPLES} occupied intervals, "
                       f"have {len(withpos)}")

    floor = sum(i["rate"] * i["dt"] for i in idle) / sum(i["dt"] for i in idle) if idle else None
    per_pos = [(i["rate"] - (floor or 0.0)) / i["occ"] for i in withpos] if withpos else []
    p95 = _p(per_pos, 0.95)

    # CONSTRAINT 2: linearity is checked, never assumed.
    fit = _fit([i["occ"] for i in iv], [i["rate"] for i in iv])
    if fit is None:
        blocked.append("linearity unverifiable: occupancy has no spread across intervals yet")
    else:
        slope, inter, r2 = fit
        lo, hi = min(i["occ"] for i in iv), max(i["occ"] for i in iv)
        print(f"\nlinearity  rate ~ {inter:.1f} + {slope:.1f} x occupancy   R2 {r2:.2f}   "
              f"checked over occupancy [{lo:.2f}, {hi:.2f}]")
        if r2 < MIN_R2:
            blocked.append(f"rate vs occupancy is NOT linear (R2 {r2:.2f} < {MIN_R2}): the x{live_cap()} "
                           f"construction is dead, go back to waiting for a natural full book")
        elif hi < live_cap() - 0.5:
            print(f"  NOTE: extrapolating to occupancy {live_cap()} BEYOND the checked range "
                  f"(max observed {hi:.2f}). The projection is an extension, not an interpolation.")

    if blocked:
        print("\nGATE NOT EVALUABLE — insufficient evidence:")
        for b in blocked:
            print("  -", b)
        print("\nNo projection is produced from insufficient data. Keep sampling.")
        return

    cap = live_cap()
    ent_rate = sum(i["att"] for i in iv) / sum(i["dt"] for i in iv) * 3600.0
    ent_cost = (sum(i["rate"] * i["dt"] for i in iv if i["att"] and i["occ"] < 0.05)
                / max(sum(i["dt"] for i in iv if i["att"] and i["occ"] < 0.05), 1e-9)) - floor
    ent_cost = max(0.0, ent_cost)
    projected = floor + cap * p95 + ent_cost

    cap_conc = int(os.environ.get("MEMECOIN_CAPTURE_MAX_CONCURRENT", "6"))
    cap_cad = float(os.environ.get("MEMECOIN_CAPTURE_CADENCE_S", "5"))
    capture_share = cap_conc * 3600.0 / cap_cad

    print(f"\nidle floor            {floor:9.1f} q/h   (n={len(idle)})")
    print(f"per-position p95      {p95:9.1f} q/h   (n={len(per_pos)}, p50 {_p(per_pos,0.5):.1f})")
    print(f"entry overhead        {ent_cost:9.1f} q/h   ({ent_rate:.1f} attempts/h)")
    print(f"PROJECTED PEAK        {projected:9.1f} q/h   = floor + {cap} x p95 + entry")
    print(f"capture would add     {capture_share:9.1f} q/h   ({cap_conc} concurrent / {cap_cad:.0f}s)")
    print(f"combined              {projected+capture_share:9.1f} q/h")
    print("\nCONSTRUCTED PEAK, PROJECTED FROM PER-POSITION ATTRIBUTION — NEVER OBSERVED AT FULL BOOK.")

    # CONSTRAINT 3: if the book ever naturally reaches the cap, the observation is the cross-check
    # and it goes in the report whichever way it cuts.
    at_cap = [i for i in iv if i["occ"] >= cap - 0.5]
    if at_cap:
        obs = sum(i["rate"] * i["dt"] for i in at_cap) / sum(i["dt"] for i in at_cap)
        err = (obs - projected) / projected * 100.0
        print(f"VALIDATION ROW: {len(at_cap)} natural interval(s) at occupancy >= {cap-0.5:.1f} "
              f"observed {obs:.1f} q/h vs projected {projected:.1f} ({err:+.0f}%). "
              f"{'Projection UNDERSTATES the real peak.' if err > 0 else 'Projection is conservative.'}")
    else:
        print(f"VALIDATION ROW: none yet — the book has not naturally reached occupancy {cap}.")

    if limit_per_hour:
        head = limit_per_hour / max(projected + capture_share, 1e-9)
        src = "observed 429 ceiling" if r429 else "published/apparent limit, NOT observed here"
        print(f"limit {limit_per_hour:.0f} q/h ({src}) -> margin {head:.2f}x "
              f"({'PASS' if head >= 2.0 else 'FAIL'}, need 2.0x)")
    else:
        need = 2.0 * (projected + capture_share)
        print(f"NO LIMIT SUPPLIED and {'no 429 has ever been observed' if not r429 else f'{r429} 429s seen'}. "
              f"The gate cannot pass on measurement alone.")
        print(f"  Enabling would require a Jupiter ceiling of at least {need:.0f} q/h for 2x margin.")
        print("  Supply one with --limit-per-hour and it will be labelled as published, not observed.")

if __name__ == "__main__":
    main()
