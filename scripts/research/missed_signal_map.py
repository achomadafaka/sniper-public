#!/usr/bin/env python3
"""Missed-signal map: armed-wallet buys that never became an entry attempt, blind windows, latency.

Alpha overnight order 2026-09-29 (operator: "stats on all refusals and entries to see what is in our
power to adjust"); Lambda's part is the MISSED side. Pre-registered as brain episodes d9dced52 and
ada852e7 before any outcome was computed. Read-only: the funded ledger through a read-only
connection bounded by id and time, plus `journalctl --list-boots` for host uptime.

SIGNAL: a source BUY by a wallet ARMED on that chain at the signal instant, from J7 events
(memecoin_events j7_wallets / j7_pump, t = received_at) and on-chain watcher detections
(onchain_wallet_detections, t = detected_at). Rows of one (network, wallet, mint) within 120 s of
the cluster's first row are one signal. Armed-at: the wallet's last approvals row at or before t
among chain_arm / chain_revoke for that chain and the global revoke / retire (None = unknown).

FUNNEL per chain (first match): filled; roster_mismatch (the runner decided not_approved/disarmed
while the ledger says armed); shadow_unfollowed (J7 'shadow', no watcher-lane attempt);
no_decision (no J7 decision and no attempt within [t0-30 s, t0+600 s]); refused (anything else).
MISSED = no_decision + shadow_unfollowed. OUTCOME of a missed or mismatched signal: its first J7
event's memecoin_signal_outcomes_v2 marks, graded by memecoin_missed_entry_grader.
simulate_first_touch (diagnostic 60/60, $50 clip: a gate comparison, not an executable fill).

    python3 scripts/research/missed_signal_map.py --since 2026-09-13T00:00:00+00:00 --out map.json
    python3 scripts/research/missed_signal_map.py --selftest
"""
import argparse
import bisect
import collections
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

CLUSTER_S = 120.0
ATTEMPT_BEFORE_S, ATTEMPT_AFTER_S = 30.0, 600.0
RUNNER_GAP_S = 600.0
FOLLOW_MAX_AGE_S = 60.0                  # MEMECOIN_REAL_FOLLOW_MAX_AGE_S
J7_GAP_S, WATCH_GAP_S, ATTEMPT_GAP_S = 1800.0, 3600.0, 3600.0
UNARMED_DECISIONS = ("not_approved", "disarmed")
WATCHER_ORIGINS = ("onchain:", "slotlane:")


def ts(iso):
    return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


def pct(values, q):
    v = sorted(values)
    return round(v[min(len(v) - 1, int(q * (len(v) - 1) + 0.5))], 2) if v else None


class ArmBook:
    """armed(uid, chain, t) -> True / False / None from the approvals ledger rows."""

    def __init__(self, rows):
        self.by = collections.defaultdict(list)
        for uid, action, chain, at in rows:
            if action in ("chain_arm", "chain_revoke") and chain:
                self.by[(str(uid), str(chain))].append((ts(at), action == "chain_arm"))
            elif action in ("revoke", "retire") and not chain:
                self.by[(str(uid), None)].append((ts(at), False))
        for v in self.by.values():
            v.sort()

    def armed(self, uid, chain, t):
        best = None
        for key in ((str(uid), str(chain)), (str(uid), None)):
            rows = self.by.get(key) or []
            i = bisect.bisect_right(rows, (t, True)) - 1
            if i >= 0 and (best is None or rows[i][0] > best[0]):
                best = rows[i]
        return None if best is None else best[1]


def clusters(rows):
    """rows: dicts with network, uid, mint, t, kind ('j7' | 'watch'). One list per signal."""
    out = []
    by = collections.defaultdict(list)
    for r in rows:
        by[(r["network"], r["uid"], r["mint"])].append(r)
    for key, rs in by.items():
        rs.sort(key=lambda r: r["t"])
        cur = [rs[0]]
        for r in rs[1:]:
            if r["t"] - cur[0]["t"] <= CLUSTER_S:
                cur.append(r)
            else:
                out.append(cur)
                cur = [r]
        out.append(cur)
    return out


class Attempts:
    def __init__(self, rows):
        self.by = collections.defaultdict(list)
        self.times = []
        for a in rows:
            t = ts(a["decision_started_at"])
            self.by[(str(a["network_id"]), str(a["source_user_id"]), a["mint"])].append((t, a))
            self.times.append(t)
        for v in self.by.values():
            v.sort(key=lambda x: x[0])
        self.times.sort()

    def near(self, network, uid, mint, t0):
        return [a for t, a in self.by.get((network, uid, mint), [])
                if t0 - ATTEMPT_BEFORE_S <= t <= t0 + ATTEMPT_AFTER_S]

    def any_within(self, t0, span):
        i = bisect.bisect_left(self.times, t0 - span)
        return i < len(self.times) and self.times[i] <= t0 + span


def classify(cluster, attempts):
    """(class, detail) for one armed signal cluster."""
    first = cluster[0]
    near = attempts.near(first["network"], first["uid"], first["mint"], first["t"])
    decisions = [r.get("decision") for r in cluster if r["kind"] == "j7" and r.get("decision")]
    if any(d == "filled" for d in decisions) or any(a.get("terminal_status") == "filled" for a in near):
        return "filled", None
    if any(d in UNARMED_DECISIONS for d in decisions):
        r = next(r for r in cluster if r.get("decision") in UNARMED_DECISIONS)
        reason = str(r.get("reason") or "")
        family = ("quiet_hours" if "quiet_hours" in reason else "kill_switch" if "kill_switch" in reason
                  else "not_armed_by_runner")
        return "roster_mismatch", family
    watcher_attempt = any(str(a.get("origin_event_id") or "").startswith(WATCHER_ORIGINS) for a in near)
    if decisions and all(d == "shadow" for d in decisions) and not watcher_attempt:
        return "shadow_unfollowed", None
    if not decisions and not near:
        return "no_decision", None
    return "refused", (decisions[0] if decisions else (near[0].get("terminal_reason") or "")[:60])


def missed_cause(cluster, attempts, boots, open_positions):
    """Why nothing acted, first match: host_down, runner_gap, already_open, watcher_only, other."""
    first = cluster[0]
    t0 = first["t"]
    if any(a <= t0 <= b for a, b in down_windows(boots)):
        return "host_down"
    if not attempts.any_within(t0, RUNNER_GAP_S):
        return "runner_gap"
    for o, c in open_positions.get((first["network"], first["mint"]), []):
        if o <= t0 and (c is None or c > t0):
            return "already_open"
    if all(r["kind"] == "watch" for r in cluster):
        return "watcher_only"
    return "other"


def down_windows(boots):
    """[(last entry of a boot, first entry of the next)] gaps longer than 60 s."""
    b = sorted(boots)
    return [(b[i][1], b[i + 1][0]) for i in range(len(b) - 1) if b[i + 1][0] - b[i][1] > 60]


def gaps(times, min_gap, lo, hi):
    """[(start, end)] gaps of at least min_gap seconds in sorted `times` inside [lo, hi]."""
    t = [lo] + sorted(x for x in times if lo <= x <= hi) + [hi]
    return [(a, b) for a, b in zip(t, t[1:]) if b - a >= min_gap]


def boots_from_journal():
    out = subprocess.run(["journalctl", "--list-boots", "-o", "json", "--no-pager"],
                         capture_output=True, text=True, timeout=30)
    return [(b["first_entry"] / 1e6, b["last_entry"] / 1e6) for b in json.loads(out.stdout or "[]")]


def load(ledger, since):
    from scripts.research.wallet_link_graph import first_id_since
    lo = first_id_since(ledger, since)
    j7 = [dict(r) for r in ledger.execute(
        "SELECT fomo_event_id, source_user_id, network_id, mint, received_at, real_decision, delivery_lane, "
        "real_decision_reason "
        "FROM memecoin_events WHERE id >= ? AND received_at >= ? AND side='buy' "
        "AND delivery_lane IN ('j7_wallets','j7_pump') AND source_user_id IS NOT NULL AND mint IS NOT NULL",
        (lo, since))]
    j7_any = [ts(r[0]) for r in ledger.execute(
        "SELECT received_at FROM memecoin_events WHERE id >= ? AND received_at >= ? "
        "AND delivery_lane IN ('j7_wallets','j7_pump')", (lo, since))]
    det = [dict(r) for r in ledger.execute(
        "SELECT signature, source_user_id, network_id, mint, block_time, detected_at, detect_latency_s "
        "FROM onchain_wallet_detections WHERE detected_at >= ?", (since,))]
    att = [dict(r) for r in ledger.execute(
        "SELECT attempt_id, origin_event_id, delivery_lane, signal_source, network_id, mint, source_user_id, "
        "decision_started_at, terminal_status, terminal_reason FROM memecoin_entry_attempts "
        "WHERE decision_started_at >= ?", ((datetime.fromisoformat(since) - timedelta(hours=1)).isoformat(),))]
    approvals = [tuple(r) for r in ledger.execute(
        "SELECT source_user_id, action, chain, created_at FROM memecoin_wallet_approvals")]
    positions = collections.defaultdict(list)
    for net, mint, o, c in ledger.execute(
            "SELECT network_id, mint, opened_at, closed_at FROM memecoin_positions WHERE execution_mode='real' "
            "AND opened_at >= ?", ((datetime.fromisoformat(since) - timedelta(days=2)).isoformat(),)):
        positions[(str(net), mint)].append((ts(o), ts(c) if c else None))
    return j7, j7_any, det, att, approvals, positions


def grade(ledger, fomo_event_id):
    from sniper.services.memecoin_missed_entry_grader import simulate_first_touch
    out = ledger.execute("SELECT outcome_id, ref_price FROM memecoin_signal_outcomes_v2 "
                         "WHERE upstream_event_id=?", (fomo_event_id,)).fetchone()
    if out is None:
        return None
    marks = [dict(m) for m in ledger.execute(
        "SELECT observed_at, price FROM memecoin_horizon_marks_v2 WHERE outcome_id=? AND status='observed' "
        "AND price IS NOT NULL ORDER BY observed_at, id", (out["outcome_id"],))]
    return simulate_first_touch(out["ref_price"], marks) if marks else None


def build(ledger, since, now, boots):
    j7, j7_any, det, att, approvals, positions = load(ledger, since)
    book, attempts = ArmBook(approvals), Attempts(att)
    rows = [{"kind": "j7", "network": str(r["network_id"]), "uid": str(r["source_user_id"]), "mint": r["mint"],
             "t": ts(r["received_at"]), "decision": r["real_decision"], "fomo": r["fomo_event_id"],
             "lane": r["delivery_lane"], "reason": r.get("real_decision_reason")} for r in j7]
    rows += [{"kind": "watch", "network": str(r["network_id"]), "uid": str(r["source_user_id"]), "mint": r["mint"],
              "t": ts(r["detected_at"]), "block_time": r["block_time"], "latency": r["detect_latency_s"]}
             for r in det]
    funnel = collections.defaultdict(collections.Counter)
    causes = collections.defaultdict(collections.Counter)
    arm_state = collections.defaultdict(collections.Counter)
    graded = collections.defaultdict(list)
    refused = collections.defaultdict(collections.Counter)
    missed_examples = []
    for c in clusters(rows):
        first = c[0]
        armed = book.armed(first["uid"], first["network"], first["t"])
        arm_state[first["network"]]["armed" if armed else ("unknown" if armed is None else "not_armed")] += 1
        if not armed:
            continue
        cls, detail = classify(c, attempts)
        funnel[first["network"]][cls] += 1
        if cls == "refused":
            refused[first["network"]][detail or "unknown"] += 1
        if cls in ("no_decision", "shadow_unfollowed"):
            cause = missed_cause(c, attempts, boots, positions)
            causes[first["network"]][cls + ":" + cause] += 1
            if len(missed_examples) < 40:
                missed_examples.append({"network": first["network"], "uid": first["uid"], "mint": first["mint"],
                                        "t": iso(first["t"]), "class": cls, "cause": cause,
                                        "kinds": sorted({r["kind"] for r in c})})
        if cls in ("no_decision", "shadow_unfollowed", "roster_mismatch"):
            fomo = next((r["fomo"] for r in c if r["kind"] == "j7" and r.get("fomo")), None)
            g = grade(ledger, fomo) if fomo else None
            graded[(first["network"], cls if cls != "roster_mismatch" else cls + ":" + detail)].append(g)
    # latency
    watch_lat = collections.defaultdict(list)
    for r in det:
        if r["detect_latency_s"] is not None:
            watch_lat[str(r["network_id"])].append(float(r["detect_latency_s"]))
    chain_time = {}
    for r in det:
        if r["block_time"]:
            chain_time.setdefault((str(r["network_id"]), str(r["source_user_id"]), r["mint"]), []).append(float(r["block_time"]))
    j7_lat = collections.defaultdict(list)
    for r in j7:
        bts = chain_time.get((str(r["network_id"]), str(r["source_user_id"]), r["mint"])) or []
        t = ts(r["received_at"])
        near = [t - b for b in bts if -5 <= t - b <= CLUSTER_S * 5]
        if near:
            j7_lat[str(r["network_id"])].append(min(near, key=abs))
    stale = collections.Counter()
    for r in j7:
        if r["real_decision"] == "stale":
            stale[("j7", str(r["network_id"]))] += 1
    for a in att:
        reason = str(a.get("terminal_reason") or "").lower()
        if "stale" in reason or "too old" in reason or "max age" in reason:
            stale[(a.get("delivery_lane") or a.get("signal_source") or "?", str(a["network_id"]))] += 1
    recall = watcher_recall(rows, book)
    lo, hi = ts(since), ts(now)
    down = [(max(a, lo), min(b, hi)) for a, b in down_windows(boots) if b >= lo and a <= hi]
    armed_t = [r["t"] for c in clusters(rows) for r in c[:1] if book.armed(r["uid"], r["network"], r["t"])]
    blind = {
        "host_down": [(iso(a), iso(b), round((b - a) / 3600, 2)) for a, b in down],
        "j7_gaps": [(iso(a), iso(b), round((b - a) / 3600, 2)) for a, b in gaps(j7_any, J7_GAP_S, lo, hi)],
        "attempt_gaps_with_armed_signals": [
            (iso(a), iso(b), round((b - a) / 3600, 2), sum(1 for t in armed_t if a < t < b))
            for a, b in gaps(attempts.times, ATTEMPT_GAP_S, lo, hi) if any(a < t < b for t in armed_t)],
        "watcher_gaps": {},
    }
    for net in sorted({str(r["network_id"]) for r in det}):
        dt = [ts(r["detected_at"]) for r in det if str(r["network_id"]) == net]
        armed_j7 = [r["t"] for r in rows if r["kind"] == "j7" and r["network"] == net
                    and book.armed(r["uid"], net, r["t"])]
        first_det = min(dt) if dt else hi
        blind["watcher_gaps"][net] = [
            (iso(a), iso(b), round((b - a) / 3600, 2), sum(1 for t in armed_j7 if a < t < b))
            for a, b in gaps(dt, WATCH_GAP_S, first_det, hi) if any(a < t < b for t in armed_j7)]

    def money(items):
        got = [g for g in items if g]
        return {"signals": len(items), "graded": len(got),
                "pnl_usd_at_50": round(sum(g["pnl_usd"] for g in got), 2),
                "mean_return_pct": round(sum(g["exit_return_pct"] for g in got) / len(got), 2) if got else None,
                "tp_hits": sum(1 for g in got if g["exit_reason"] == "take_profit"),
                "sl_hits": sum(1 for g in got if g["exit_reason"] == "stop_loss"),
                "mfe_p50": pct([g["mfe_pct"] for g in got], 0.5)}
    return {
        "window": [since, now],
        "arm_state": {k: dict(v) for k, v in arm_state.items()},
        "funnel": {k: dict(v) for k, v in funnel.items()},
        "missed_causes": {k: dict(v) for k, v in causes.items()},
        "refused_top": {k: dict(v.most_common(8)) for k, v in refused.items()},
        "outcomes": {"%s:%s" % k: money(v) for k, v in graded.items()},
        "latency": {
            "watcher_detect_s": {k: {"n": len(v), "p50": pct(v, .5), "p90": pct(v, .9), "p99": pct(v, .99),
                                     "over_60s": sum(1 for x in v if x > FOLLOW_MAX_AGE_S)}
                                 for k, v in watch_lat.items()},
            "j7_after_chain_s": {k: {"n": len(v), "p50": pct(v, .5), "p90": pct(v, .9), "p99": pct(v, .99),
                                     "over_60s": sum(1 for x in v if x > FOLLOW_MAX_AGE_S)}
                                 for k, v in j7_lat.items()},
            "refused_on_age": {"%s:%s" % k: v for k, v in stale.items()},
        },
        "blind": blind,
        "watcher_recall": recall,
        "missed_examples": missed_examples,
    }


def watcher_recall(rows, book, min_buys=3, min_span_s=3600.0):
    """Per chain: of the J7 armed buys by wallets the watcher already covered (a detection of that
    wallet on that chain at or before the buy), how many had a detection within CLUSTER_S; and the
    runs of consecutive unmatched covered buys spanning >= min_span_s with >= min_buys (blind)."""
    first_det, det_at = {}, collections.defaultdict(list)
    for r in rows:
        if r["kind"] == "watch":
            k = (r["network"], r["uid"])
            first_det[k] = min(first_det.get(k, r["t"]), r["t"])
            det_at[(r["network"], r["uid"], r["mint"])].append(r["t"])
    out = {}
    by_net = collections.defaultdict(list)
    for r in sorted((r for r in rows if r["kind"] == "j7"), key=lambda r: r["t"]):
        k = (r["network"], r["uid"])
        if k in first_det and first_det[k] <= r["t"] and book.armed(r["uid"], r["network"], r["t"]):
            hit = any(abs(t - r["t"]) <= CLUSTER_S for t in det_at.get((r["network"], r["uid"], r["mint"]), []))
            by_net[r["network"]].append((r["t"], hit))
    for net, seq in by_net.items():
        runs, cur = [], []
        for t, hit in seq + [(None, True)]:
            if hit:
                if len(cur) >= min_buys and cur[-1] - cur[0] >= min_span_s:
                    runs.append((iso(cur[0]), iso(cur[-1]), round((cur[-1] - cur[0]) / 3600, 2), len(cur)))
                cur = []
            else:
                cur.append(t)
        out[net] = {"covered_buys": len(seq), "matched": sum(1 for _t, h in seq if h),
                    "recall_pct": round(100.0 * sum(1 for _t, h in seq if h) / len(seq), 1) if seq else None,
                    "blind_runs": runs}
    return out


def selftest():
    book = ArmBook([("u", "chain_arm", "solana", "2026-09-20T00:00:00+00:00"),
                    ("u", "retire", None, "2026-09-22T00:00:00+00:00"),
                    ("u", "chain_arm", "solana", "2026-09-23T00:00:00+00:00")])
    assert book.armed("u", "solana", ts("2026-09-21T00:00:00+00:00")) is True
    assert book.armed("u", "solana", ts("2026-09-22T12:00:00+00:00")) is False      # global retire
    assert book.armed("u", "solana", ts("2026-09-24T00:00:00+00:00")) is True
    assert book.armed("u", "solana", ts("2026-09-19T00:00:00+00:00")) is None
    assert book.armed("u", "56", ts("2026-09-21T00:00:00+00:00")) is None
    rows = [{"kind": "j7", "network": "solana", "uid": "u", "mint": "m", "t": 0.0, "decision": "shadow"},
            {"kind": "watch", "network": "solana", "uid": "u", "mint": "m", "t": 100.0},
            {"kind": "j7", "network": "solana", "uid": "u", "mint": "m", "t": 500.0, "decision": None}]
    cs = clusters(rows)
    assert [len(c) for c in cs] == [2, 1]
    att = Attempts([{"network_id": "solana", "source_user_id": "u", "mint": "m",
                     "decision_started_at": iso(90.0), "origin_event_id": "onchain:sig", "terminal_status": "refused"}])
    assert classify(cs[0], att) == ("refused", "shadow")                              # watcher took it
    assert classify(cs[1], Attempts([])) == ("no_decision", None)
    assert classify(cs[0], Attempts([])) == ("shadow_unfollowed", None)
    mm = [{"kind": "j7", "network": "4663", "uid": "u", "mint": "x", "t": 0.0, "decision": "not_approved"}]
    assert classify(mm, Attempts([]))[0] == "roster_mismatch"
    boots = [(0.0, 1000.0), (5000.0, 9000.0)]
    assert down_windows(boots) == [(1000.0, 5000.0)]
    late = [{"kind": "j7", "network": "solana", "uid": "u", "mint": "m", "t": 2000.0, "decision": None}]
    assert missed_cause(late, Attempts([]), boots, {}) == "host_down"
    assert missed_cause(cs[1], Attempts([]), boots, {}) == "runner_gap"             # t=500: host up
    busy = Attempts([{"network_id": "solana", "source_user_id": "z", "mint": "q", "decision_started_at": iso(6000.0)}])
    held = [{"kind": "j7", "network": "solana", "uid": "u", "mint": "m", "t": 6100.0, "decision": None}]
    assert missed_cause(held, busy, boots, {("solana", "m"): [(5500.0, None)]}) == "already_open"
    assert missed_cause(held, busy, boots, {("solana", "m"): [(5500.0, 6000.0)]}) == "other"
    assert gaps([10.0, 20.0, 100.0], 50, 0.0, 120.0) == [(20.0, 100.0)]
    qh = [{"kind": "j7", "network": "solana", "uid": "u", "mint": "q", "t": 0.0, "decision": "disarmed",
           "reason": "quiet_hours"}]
    assert classify(qh, Attempts([])) == ("roster_mismatch", "quiet_hours")
    armed = ArmBook([("w", "chain_arm", "solana", iso(0.0))])
    seq = [{"kind": "watch", "network": "solana", "uid": "w", "mint": "a", "t": 10.0}]
    seq += [{"kind": "j7", "network": "solana", "uid": "w", "mint": "a", "t": 20.0}]           # matched
    seq += [{"kind": "j7", "network": "solana", "uid": "w", "mint": "b%d" % i, "t": 100.0 + 2000 * i}
            for i in range(3)]                                                                  # blind 4000 s
    seq += [{"kind": "j7", "network": "solana", "uid": "x", "mint": "c", "t": 50.0}]           # not covered
    r = watcher_recall(seq, armed)["solana"]
    assert (r["covered_buys"], r["matched"], len(r["blind_runs"])) == (4, 1, 1)
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since", default="2026-09-13T00:00:00+00:00")
    ap.add_argument("--out")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    from sniper.persistence.db_core import get_readonly_connection
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_root
    load_env_files(str(state_root()))
    ledger = get_readonly_connection(str(state_root() / "sniper_tracker.db"))
    try:
        result = build(ledger, args.since, datetime.now(timezone.utc).isoformat(), boots_from_journal())
    finally:
        ledger.close()
    text = json.dumps(result, indent=1, default=str)
    if args.out:
        Path(args.out).write_text(text)
    print(text)


if __name__ == "__main__":
    sys.exit(main())
