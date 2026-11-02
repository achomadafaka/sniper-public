"""Why each funded J7 Solana fill did not go through the slot lane: venue x reason, count, PnL.

Operator priority via Alpha (2026-09-28 06:0xZ): 138 of 181 Solana fills in 7 days went through
J7 follow_activity (decision->fill p50 1.89 s) and 18 through the slot lane (p50 0.64 s). For each
J7 fill: the source venue (census v2 label: the pool the coin came from), whether the slot lane
SAW the source buy, and if so what it decided; if not, why not. The output is the list of what to
move onto the fast lane next.

Sources, all read-only:
  * fills: memecoin_positions execution_mode='real', network 'solana', signal_source
    'follow_activity', opened_at >= --since; the source signature from origin_event_id.
  * venue: census v2 JSON files (venue_census.py fetch2 output), merged; a fill no file covers is
    'venue_unread'. The routed flag (source tx behind an aggregator/router) comes from v1 fetch
    files (--census-v1). A J7 uuid event carries no signature; it takes the watcher's detection of
    the same (wallet, mint) within 120 s when exactly one exists.
  * the lane: runner-log lines, HOST clock. A decision line names handle, mint8 and S, not the
    signature, so it is matched on mint8 inside [decision - 30 s, decision + 10 s]; a routed line
    carries sig= and is matched exactly. decision_started_at is the corrected clock (host minus a
    ~0.1 s J7 offset), negligible against that window.
  * not seen, why: before the lane's first start ('lane_not_live'); a venue the lane does not decode
    ('not_a_lane_venue:<venue>' -- it decodes pump curve and PumpSwap, plus routed frames);
    the owner had no verified Solana address when the fill started ('owner_not_subscribed');
    a routed pump buy before the routed lane went live ('routed_before_routed_lane');
    otherwise 'pump_venue_not_seen' (unexplained). A second table says what the CURRENT release's
    lane would do with each fill's venue and path.

    python3 scripts/research/j7_fill_lane_gap.py --census A.json [--census B.json ...] [--since ISO]
    python3 scripts/research/j7_fill_lane_gap.py --selftest
"""
import argparse
import collections
import contextlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "scripts" / "research")]
LEDGER = REPO / "sniper_tracker.db"
LOG = REPO / "logs" / "memecoin_runner.log"
LANE_VENUES = {"pump_curve", "pumpswap"}
ROUTED_LIVE_AT = "2026-09-27T20:18:00+00:00"   # routed lane live (scoreboard; shadow lines from 17:25:45Z)
ROUTED_PUMP_AT = "2026-09-27T22:51:04.199+00:00"  # routed pump buys convert to the lane (46fe73f5)
SHADOW_VENUES = {"raydium_cpmm", "raydium_launchlab"}

DEC = re.compile(r"^(\S+) \[memecoin\] slot lane (\S+) (\S{8}) S=(\d+) -> (.+?) ms=")
ROUTED = re.compile(r"^(\S+) \[memecoin\] slot lane routed (live|shadow|lane|pump) (\S+) (\S{8}) S=(\d+) "
                    r"sig=(\S+) .*-> (\S+)\s*$")
START = re.compile(r"^(\S+) \[memecoin\] slot lane: started")
NUM = re.compile(r"[+-]?\$?[0-9][0-9,.]*%?")


def ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def norm(outcome):
    """A lane outcome with its numbers masked, so refusals of one kind group together."""
    return NUM.sub("N", outcome.strip())


def parse_log(lines):
    dec, routed, starts = collections.defaultdict(list), {}, []
    for line in lines:
        if "slot lane" not in line:
            continue
        m = ROUTED.match(line)
        if m:
            routed[m.group(6)] = (m.group(1), "routed_%s" % m.group(2), m.group(7))
            continue
        m = DEC.match(line)
        if m and m.group(2) not in ("routed", "handoff", "miss-learn", "cpmm", "launchlab", "pool"):
            dec[m.group(3)].append((m.group(1), norm(m.group(5))))
            continue
        m = START.match(line)
        if m:
            starts.append(m.group(1))
    return dec, routed, starts


def lane_view(sig, mint, t0, dec, routed, window=(-30.0, 10.0)):
    """(seen, outcome) for one source buy."""
    if sig in routed:
        at, kind, out = routed[sig]
        return True, "%s -> %s" % (kind, out)
    lo, hi = t0 + timedelta(seconds=window[0]), t0 + timedelta(seconds=window[1])
    for at, out in dec.get((mint or "")[:8], ()):
        if lo <= ts(at) <= hi:
            return True, out
    return False, None


def why_unseen(t0, venue, subscribed, first_start, routed=None):
    if first_start is None or t0 < ts(first_start):
        return "lane_not_live"
    if venue in ("no_signature", "venue_unread"):
        return "venue_unknown:%s" % venue
    if venue not in LANE_VENUES:
        return "not_a_lane_venue:%s" % venue
    if not subscribed:
        return "owner_not_subscribed"
    if routed and t0 < ts(ROUTED_LIVE_AT):
        return "routed_before_routed_lane"
    return "pump_venue_not_seen"


def lane_today(venue, routed):
    """What the lane would do with this source buy on the current release."""
    if venue in LANE_VENUES:
        return "routed pump: convert (since 22:51Z)" if routed else "direct: lane builds"
    if venue in SHADOW_VENUES:
        return "shadow only (no live build)"
    if venue in ("no_signature", "venue_unread"):
        return "unknown"
    return "no builder"


def load_venues(paths, v1_paths=()):
    from venue_census import coin_venue
    senders, owners, routed = {}, {}, {}
    for p in paths:
        d = json.loads(Path(p).read_text())
        senders.update(d.get("senders") or {})
        owners.update(d.get("owners") or {})
    for p in v1_paths:
        for sig, v in (json.loads(Path(p).read_text()).get("venues") or {}).items():
            routed[sig] = v.get("routed")
    return (lambda sig: coin_venue(senders[sig], owners) if sig in senders else "venue_unread",
            lambda sig: routed.get(sig))


def fills(conn, since):
    from sniper.persistence.db_memecoin import event_transaction_signature
    rows = conn.execute(
        "SELECT p.id, p.source_user_id, p.source_handle, p.mint, p.origin_event_id, p.realized_pnl, "
        "p.is_open, p.opened_recorded_at, a.decision_started_at FROM memecoin_positions p "
        "LEFT JOIN memecoin_entry_attempts a ON a.attempt_id = p.attempt_id "
        "WHERE p.execution_mode='real' AND p.network_id='solana' AND p.signal_source='follow_activity' "
        "AND p.opened_at >= ? ORDER BY p.opened_at", (since,)).fetchall()
    reg = collections.defaultdict(list)
    for uid, st, at in conn.execute("SELECT source_user_id, status, status_at FROM memecoin_address_registry "
                                    "WHERE network_id='solana'"):
        reg[str(uid)].append((st, at))
    dets = collections.defaultdict(list)
    for sig, uid, mint, at in conn.execute("SELECT signature, source_user_id, mint, detected_at FROM "
                                           "onchain_wallet_detections WHERE network_id='solana' AND detected_at >= ?",
                                           (since,)):
        dets[(str(uid), mint)].append((at, sig))
    out = []
    for pid, uid, handle, mint, origin, pnl, is_open, filled_at, started in rows:
        t0 = started or filled_at
        subscribed = str(uid).startswith("pump:") or any(
            st == "verified" and at <= t0 for st, at in reg.get(str(uid), ()))
        sig, via = event_transaction_signature(origin), "j7"
        if sig is None:        # a J7 uuid event: take the watcher's detection of the same buy, if any
            near = [s for at, s in dets.get((str(uid), mint), ())
                    if abs((ts(at) - ts(t0)).total_seconds()) <= 120]
            sig, via = (near[0], "watcher_match") if len(near) == 1 else (None, "none")
        out.append({"id": pid, "handle": handle, "mint": mint, "sig": sig, "sig_via": via,
                    "pnl": pnl, "open": is_open, "t0": t0, "filled_at": filled_at,
                    "subscribed": subscribed})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--census", action="append", default=[])
    ap.add_argument("--census-v1", action="append", default=[], help="fetch output: the routed flag")
    ap.add_argument("--since")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    since = a.since or (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    venue_of, routed_of = load_venues(a.census, a.census_v1)
    with open(LOG, errors="replace") as fh:
        dec, routed, starts = parse_log(fh)
    first_start = min(starts) if starts else None
    with contextlib.closing(sqlite3.connect("file:%s?mode=ro" % LEDGER, uri=True, timeout=20)) as c:
        rows = fills(c, since)
    table = collections.defaultdict(lambda: [0, 0, 0.0, []])
    today = collections.defaultdict(list)
    for r in rows:
        venue = venue_of(r["sig"]) if r["sig"] else "no_signature"
        rflag = routed_of(r["sig"]) if r["sig"] else None
        seen, outcome = lane_view(r["sig"], r["mint"], ts(r["t0"]), dec, routed)
        reason = ("SEEN: " + outcome) if seen else why_unseen(ts(r["t0"]), venue, r["subscribed"],
                                                              first_start, rflag)
        today[(venue, "routed" if rflag else "direct" if rflag is False else "?", lane_today(venue, rflag))] \
            .append(r)
        cell = table[(venue, reason)]
        cell[0] += 1
        cell[1] += 1 if (r["pnl"] or 0) > 0 else 0
        cell[2] += r["pnl"] or 0.0
        cell[3].append(r)
    print("J7 follow_activity Solana fills since %s: %d (open %d); lane first started %s"
          % (since[:19], len(rows), sum(1 for r in rows if r["open"]), first_start))
    print("%-22s %-62s %4s %5s %10s" % ("venue", "reason", "n", "wins", "PnL $"))
    for (venue, reason), (n, w, pnl, rs) in sorted(table.items(), key=lambda kv: (-kv[1][0], kv[0])):
        print("%-22s %-62s %4d %5d %+10.2f" % (venue, reason[:62], n, w, pnl))
        if a.list:
            for r in rs:
                print("      %s %s %s %s" % (r["id"], r["handle"], (r["mint"] or "")[:8], r["t0"][:19]))
    print("\nWHAT THE LANE WOULD DO TODAY (venue, source tx direct/routed, current release):")
    print("%-20s %-7s %-36s %4s %5s %10s" % ("venue", "path", "lane today", "n", "wins", "PnL $"))
    for (venue, path, what), rs in sorted(today.items(), key=lambda kv: -len(kv[1])):
        print("%-20s %-7s %-36s %4d %5d %+10.2f" % (venue, path, what, len(rs),
              sum(1 for r in rs if (r["pnl"] or 0) > 0), sum(r["pnl"] or 0 for r in rs)))
    print("signature source: %s" % dict(collections.Counter(r["sig_via"] for r in rows)))
    by_v = collections.defaultdict(lambda: [0, 0.0])
    for (venue, _r), (n, _w, pnl, _rs) in table.items():
        by_v[venue][0] += n
        by_v[venue][1] += pnl
    print("by venue: " + ", ".join("%s %d ($%+.2f)" % (v, n, p) for v, (n, p) in
                                   sorted(by_v.items(), key=lambda kv: -kv[1][0])))


def selftest():
    lines = [
        "2026-09-26T21:07:13.773806+00:00 [memecoin] slot lane: started",
        "2026-09-27T01:00:00.100000+00:00 [memecoin] slot lane notdecu 39B7BPHg S=450790796 -> rejected source "
        "buy $10 (0.040%) under mcap floor 0.500% ($121) ms=0.0 head-S=None",
        "2026-09-28T03:49:29.066164+00:00 [memecoin] slot lane routed live LingardSol DC4xMbLm S=451202811 "
        "sig=SIGR usdc=2500.00 tokens=9 recv=x pair_ms=3.7 -> inserted",
        "2026-09-28T03:36:54.110052+00:00 [memecoin] slot lane miss-learn ResellCalendar AKKjxxmV S=1 "
        "learn_ms=76.8 read_ms=76.5 -> retry",
    ]
    dec, routed, starts = parse_log(lines)
    assert starts == ["2026-09-26T21:07:13.773806+00:00"]
    assert routed["SIGR"][1:] == ("routed_live", "inserted")
    assert list(dec) == ["39B7BPHg"] and "miss-learn" not in str(dec)       # side lines are not decisions
    assert dec["39B7BPHg"][0][1] == "rejected source buy N (N) under mcap floor N (N)", dec
    t = ts("2026-09-27T01:00:05+00:00")
    assert lane_view("X", "39B7BPHgZZ", t, dec, routed) == (True, dec["39B7BPHg"][0][1])
    assert lane_view("X", "39B7BPHgZZ", t + timedelta(minutes=5), dec, routed) == (False, None)
    assert lane_view("SIGR", "whatever", t, dec, routed) == (True, "routed_live -> inserted")
    first = starts[0]
    assert why_unseen(ts("2026-09-25T00:00:00+00:00"), "pumpswap", True, first) == "lane_not_live"
    assert why_unseen(t, "meteora_dlmm", True, first) == "not_a_lane_venue:meteora_dlmm"
    assert why_unseen(t, "pumpswap", False, first) == "owner_not_subscribed"
    assert why_unseen(t, "pump_curve", True, first) == "pump_venue_not_seen"
    assert why_unseen(t, "no_signature", True, first) == "venue_unknown:no_signature"
    assert why_unseen(t, "pumpswap", True, first, routed=True) == "routed_before_routed_lane"
    assert why_unseen(ts("2026-09-28T00:00:00+00:00"), "pumpswap", True, first, routed=True) == "pump_venue_not_seen"
    assert lane_today("pumpswap", True).startswith("routed pump") and lane_today("pump_curve", False) == "direct: lane builds"
    assert lane_today("raydium_cpmm", False).startswith("shadow") and lane_today("meteora_dlmm", True) == "no builder"
    print("selftest ok")


if __name__ == "__main__":
    main()
