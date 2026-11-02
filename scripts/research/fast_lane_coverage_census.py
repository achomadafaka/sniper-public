#!/usr/bin/env python3
"""Could our fast lanes have taken what J7 took? One bucket per J7 fill and quoted J7 attempt.

Operator direction 2026-09-28 (via Alpha): retire J7 and fill only from our own fast lanes. J7 is
retired on a chain only when the fast lanes cover >= 90% of its fills there. This census puts every
J7 (`follow_activity`) FILL, and every J7 ATTEMPT that reached the quote, since --since into exactly
one bucket, per chain:

  f  a fast lane filled the same source buy too (duplicate)
  d  a fast lane saw it and J7 decided first (the lane skipped it as duplicate_of_feed or deferred);
     how much slower is detected_at (host clock) minus J7's observed_at (corrected clock): a mixed
     clock pair, so each value carries the J7 offset, about 0.05-0.1 s
  c  a fast lane saw it and refused it on a gate (named)
  a  only the slot lane saw it, and refused it for the venue or pool (named)
  b  the lane did not know the wallet: no verified address on that chain at J7's decision time, or
     the chain's watcher was not live yet
  e  the wallet was known and no fast lane saw the buy: a delivery gap

Fast lanes: Solana = slot lane + on-chain lane (watcher -> entry hook); EVM = on-chain lane only.
Evidence, read-only: memecoin_entry_attempts / memecoin_positions / onchain_wallet_detections /
memecoin_address_registry (Solana) / axiom_verified_addresses (EVM), plus the runner log's lane lines
(host clock), matched to J7 within +-30 s on the handle and the mint or signature prefix the line
prints. The log keeps only its current file (from 2026-09-15 11:17Z).

    python3 scripts/research/fast_lane_coverage_census.py --since 2026-09-26
    python3 scripts/research/fast_lane_coverage_census.py --selftest
"""
import argparse
import collections
import contextlib
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
LOG = REPO / "logs" / "memecoin_runner.log"
WINDOW_S = 30.0
# First live instant of each chain's fast lane (HANDOFF 2026-09-28): the EVM watchers started with
# 3ea5c652 (4663, 56 at ~06:37:30Z) and 6241f0b2 (8453 at ~06:43Z).
EVM_LANE_LIVE = {"4663": "2026-09-28T06:37:30+00:00", "56": "2026-09-28T06:37:30+00:00",
                 "8453": "2026-09-28T06:43:05+00:00"}
VENUE_REFUSALS = ("pool_unknown", "slot_lane_cpmm_quote_not_supported", "slot_lane_quote_not_sol",
                  "slot_lane_not_pump_mint", "slot_lane_launchlab_quote_not_supported",
                  "slot_lane_price_unreadable", "pool_unknown_learning")
SLOT_LINE = re.compile(r"^(\S+) \[memecoin\] slot lane (\S+) (\S{8}) S=\d+ -> (.+?) ms=")
ONCHAIN_LINE = re.compile(r"^(\S+) \[memecoin\] onchain lane: (\S+) (\S+) -> (.+)$")
NUM = re.compile(r"[+-]?\$?[0-9][0-9,.]*%?")


def utc(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def source_sig(origin):
    """The source transaction signature an origin id carries, or None (a J7 uuid carries none)."""
    o = str(origin or "")
    if o.startswith("swap:raybot:"):
        return o.split(":")[2]
    if o.startswith("pump:"):
        # `pump:<sig>:<address>:buy:<mint>`: the signature is the FIRST field (Beta, 2026-09-29).
        # parts[2] is an address, so every pump origin read as "no sighting by tx".
        parts = o.split(":")
        return (parts[1] or None) if len(parts) > 1 else None
    if o.startswith("onchain:") or o.startswith("slotlane:"):
        tail = o.split(":", 1)[1]
        return tail.split(":")[1] if tail.count(":") == 2 else tail
    return None


def parse_log(path):
    slot, onchain = collections.defaultdict(list), collections.defaultdict(list)
    with open(path, errors="replace") as fh:
        for line in fh:
            if "lane" not in line or not line[:4].isdigit():
                continue
            m = SLOT_LINE.match(line)
            if m:
                slot[(m.group(2), m.group(3).lower())].append((utc(m.group(1)), m.group(4).strip()))
                continue
            m = ONCHAIN_LINE.match(line)
            if m:
                onchain[(m.group(2), m.group(3).lower())].append((utc(m.group(1)), m.group(4).strip()))
    return slot, onchain


def near(events, t0, window=WINDOW_S):
    hits = [(abs((t - t0).total_seconds()), out) for t, out in events
            if abs((t - t0).total_seconds()) <= window]
    return min(hits)[1] if hits else None


def family(text):
    return NUM.sub("N", str(text or "")).strip()[:60]


def known_wallet(conn, net, uid, at):
    if net == "solana":
        row = conn.execute("SELECT 1 FROM memecoin_address_registry WHERE network_id='solana' AND "
                           "source_user_id=? AND status='verified' AND status_at<=? LIMIT 1",
                           (uid, at.isoformat())).fetchone()
    else:
        row = conn.execute("SELECT 1 FROM axiom_verified_addresses WHERE network_id=? AND "
                           "source_user_id=? AND verify_status='axiom_confirmed' AND verified_at<=? "
                           "LIMIT 1", (net, uid, at.isoformat())).fetchone()
    return row is not None


def classify(item, conn, slot_lines, onchain_lines):
    net, t0, uid, mint = item["net"], item["t0"], item["uid"], item["mint"]
    sig = source_sig(item["origin"])
    lo, hi = (t0 - timedelta(seconds=WINDOW_S)).isoformat(), (t0 + timedelta(seconds=WINDOW_S)).isoformat()
    fast = conn.execute(
        "SELECT signal_source, terminal_status, terminal_reason, origin_event_id, decision_started_at "
        "FROM memecoin_entry_attempts WHERE network_id=? AND mint=? AND source_user_id=? "
        "AND signal_source IN ('slot_lane','onchain_watch') AND decision_started_at>=? "
        "AND decision_started_at<=?", (net, mint, uid, lo[:19], hi)).fetchall()
    if sig:
        fast = [f for f in fast if source_sig(f["origin_event_id"]) in (sig, None)] or fast
    if any(f["terminal_status"] == "filled" for f in fast):
        return "f", "fast lane filled: " + ",".join(sorted({f["signal_source"] for f in fast
                                                            if f["terminal_status"] == "filled"}))
    det = conn.execute(
        "SELECT signature, detected_at FROM onchain_wallet_detections WHERE network_id=? AND "
        "source_user_id=? AND LOWER(mint)=LOWER(?) AND detected_at>=? AND detected_at<=?",
        (net, uid, mint, lo[:19], hi)).fetchall()
    if sig:
        det = [d for d in det if sig.lower() in str(d["signature"]).lower()] or det
    handle = item["handle"] or ""
    keys = [(handle, (sig or "")[:8].lower()), (handle, str(mint)[:8].lower()),
            (handle, str(mint)[:6].lower())]
    oc = next((near(onchain_lines.get(k, ()), t0) for k in keys if near(onchain_lines.get(k, ()), t0)), None)
    sl = near(slot_lines.get((handle, str(mint)[:8].lower()), ()), t0)
    lane_out = [f"{f['signal_source']} {f['terminal_status']} {family(f['terminal_reason'])}" for f in fast]
    if det or oc or fast:
        slower = [d for d in det if utc(d["detected_at"]) >= item["observed"]]
        # "Skipped as the feed's" needs NO fast-lane attempt. A lane that attempted did not skip:
        # the on-chain lane's "deferred slot_lane_unresolved" line (waiting on the slot lane) read as
        # a skip beside a slot-lane chain failure and an on-chain chase refusal (2EUKLvNQ, 2026-09-28).
        if not fast and ((oc and ("duplicate_of_feed" in oc or "deferred" in oc)) or (det and slower)):
            gap = [(utc(d["detected_at"]) - item["observed"]).total_seconds() for d in det]
            return "d", "J7 first by %s s" % (",".join("%+.2f" % g for g in gap) if gap else "?")
        # LOST THE RACE TO J7 (Gamma, 2026-09-28). A fast lane refused "one position per coin"
        # because J7's own fill already held the coin: with J7 off, the lane would have taken it, so
        # for the retire-J7 question it is a "d". Only a J7 (follow_activity) position open at the
        # lane's decision counts; one another fast lane opened (BW1HeTnP, slot lane) stays a "c".
        for f in fast:
            if "one position per coin" not in str(f["terminal_reason"] or ""):
                continue
            j7 = conn.execute(
                "SELECT MIN(opened_at) FROM memecoin_positions WHERE execution_mode='real' AND "
                "network_id=? AND LOWER(mint)=LOWER(?) AND signal_source='follow_activity' AND "
                "opened_at<=? AND (closed_at IS NULL OR closed_at>=?)",
                (net, mint, f["decision_started_at"], f["decision_started_at"])).fetchone()[0]
            if j7:
                ahead = (utc(f["decision_started_at"]) - utc(j7)).total_seconds()
                return "d", "lost the race to J7's fill (one position per coin, J7 %.1f s earlier)" % ahead
        refusal = lane_out or ([f"onchain {family(oc)}"] if oc else [])
        return "c", "; ".join(refusal) or "detected, no lane decision found"
    if sl:
        if any(v in sl for v in VENUE_REFUSALS):
            return "a", "slot " + family(sl)
        return "c", "slot " + family(sl)
    if net != "solana" and t0 < utc(EVM_LANE_LIVE.get(net, "2100-01-01T00:00:00+00:00")):
        return "b", "EVM watcher not live yet"
    if not known_wallet(conn, net, uid, t0):
        return "b", "no verified address on %s at decision" % net
    return "e", "wallet known, no fast-lane sighting"


def load(conn, since):
    items = []
    for r in conn.execute(
            "SELECT a.attempt_id, a.network_id, a.mint, a.source_user_id, a.source_handle, "
            "a.origin_event_id, a.decision_started_at, a.observed_at, a.terminal_status, "
            "a.trace_json, p.realized_pnl, p.is_open FROM memecoin_entry_attempts a "
            # Joined on the POSITION's attempt_id, not the attempt's position_id: 33 fills from
            # 2026-09-25/26 carry an attempt row that never finished (the entry-trace loss fixed in
            # cde6b9ee), so their position_id is NULL while the position still names the attempt.
            "LEFT JOIN memecoin_positions p ON p.attempt_id=a.attempt_id AND p.execution_mode='real' "
            "WHERE a.signal_source='follow_activity' AND a.decision_started_at>=?", (since,)):
        filled = r["terminal_status"] == "filled" or r["realized_pnl"] is not None or r["is_open"] == 1
        trace = r["trace_json"] or ""
        quoted = filled or '"chase_quote_confirmation"' in trace or '"evm.quote_initial"' in trace
        if not quoted:
            continue
        items.append({"attempt": r["attempt_id"], "net": r["network_id"], "mint": r["mint"],
                      "uid": r["source_user_id"], "handle": r["source_handle"],
                      "origin": r["origin_event_id"], "t0": utc(r["decision_started_at"]),
                      "observed": utc(r["observed_at"] or r["decision_started_at"]),
                      "kind": "fill" if filled else "quoted", "pnl": r["realized_pnl"],
                      "open": r["is_open"] == 1})
    return items


def evm_seen(conn, since, chains=("4663", "56", "8453")):
    """EVM readiness by SIGHT, not by fill (2026-09-28, Alpha): every J7 attempt (any status) whose
    trader the chain's watcher holds, since `since`, in one of four rows:

      detected   a watcher detection row carries the same transaction
      decided    no row, but the on-chain lane made its own attempt on that transaction
      delivered  no row: the watcher logged the transfer with a non-buy outcome (a decode miss)
      unseen     none of the above: a delivery gap, or a transfer the watcher never received

    Decision lag = the on-chain lane attempt's decision_started_at minus J7's (both corrected
    clock). A lane that skipped as duplicate_of_feed made no attempt, so it has no lag here.
    Fills are too few on EVM for the >=90% rule alone; sight is what a retirement needs first."""
    import subprocess
    sys.path.insert(0, str(REPO / "scripts" / "research"))
    import onchain_evm_wallet_watch as w
    out = {}
    for chain in chains:
        w._configure(chain)
        watched = w._verified_addresses(conn)
        log = REPO / "logs" / ("onchain_evm_armed.log" if chain == "4663" else "onchain_evm_armed.%s.log" % chain)
        text = log.read_text(errors="replace") if log.exists() else ""
        rows = conn.execute(
            "SELECT decision_started_at, source_handle, origin_event_id, terminal_status FROM memecoin_entry_attempts "
            "WHERE network_id=? AND signal_source='follow_activity' AND decision_started_at>=? "
            "ORDER BY decision_started_at", (chain, since)).fetchall()
        seen, items = set(), []
        for at, handle, origin, status in rows:
            parts = str(origin or "").split(":")
            if parts[:2] != ["swap", "raybot"] or len(parts) < 5 or parts[3].lower() not in watched:
                continue
            tx = parts[2].lower()
            if tx in seen:
                continue
            seen.add(tx)
            det = conn.execute("SELECT id FROM onchain_wallet_detections WHERE network_id=? AND "
                               "LOWER(signature) LIKE ?", (chain, "%" + tx + "%")).fetchone()
            lane = conn.execute("SELECT decision_started_at FROM memecoin_entry_attempts WHERE network_id=? "
                                "AND signal_source='onchain_watch' AND LOWER(origin_event_id) LIKE ?",
                                (chain, "%" + tx + "%")).fetchone()
            lag = (utc(lane[0]) - utc(at)).total_seconds() if lane else None
            line = next((ln for ln in text.splitlines() if tx[:12] in ln and "outcome=" in ln), None)
            row = ("detected" if det else "decided" if lane else "delivered" if line else "unseen")
            items.append({"at": at, "handle": handle, "tx": tx, "row": row, "lag": lag, "status": status,
                          "line": line})
        out[chain] = items
    return out


def selftest():
    assert source_sig("swap:raybot:SIGabc:trader:mint") == "SIGabc"
    assert source_sig("onchain:4663:0xtx:0xmint") == "0xtx"
    assert source_sig("onchain:SolSig") == "SolSig" and source_sig("swap:uuid-1") is None
    assert family("source buy $100 under absolute floor $197") == "source buy N under absolute floor N"
    t = utc("2026-09-28T11:00:00+00:00")
    ev = [(t + timedelta(seconds=40), "far"), (t + timedelta(seconds=2), "near")]
    assert near(ev, t) == "near" and near(ev[:1], t) is None
    print("selftest ok")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", default="2026-09-26")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--evm-seen", action="store_true", help="EVM readiness by sight since --since")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if args.evm_seen:
        from sniper.persistence.db_core import get_readonly_connection
        import sqlite3
        with contextlib.closing(get_readonly_connection(str(REPO / "sniper_tracker.db"))) as conn:
            conn.row_factory = sqlite3.Row
            table = evm_seen(conn, args.since)
        for chain, items in table.items():
            c = collections.Counter(i["row"] for i in items)
            lags = sorted(i["lag"] for i in items if i["lag"] is not None)
            print("%s watched J7 buys since %s: %d  detected %d  decided %d  delivered %d  unseen %d%s" % (
                chain, args.since, len(items), c["detected"], c["decided"], c["delivered"], c["unseen"],
                "  lane-minus-J7 decision p50 %+.2f s (n=%d)" % (lags[len(lags) // 2], len(lags)) if lags else ""))
            for i in items:
                print("   %s %-16s %s %-9s J7=%s%s" % (i["at"][11:19], (i["handle"] or "")[:16], i["tx"][:12], i["row"],
                                                   i["status"], "" if i["lag"] is None else " lag %+.2f s" % i["lag"]))
        return 0
    from sniper.persistence.db_core import get_readonly_connection
    slot_lines, onchain_lines = parse_log(LOG)
    with contextlib.closing(get_readonly_connection(str(REPO / "sniper_tracker.db"))) as conn:
        items = load(conn, args.since)
        for it in items:
            it["bucket"], it["why"] = classify(it, conn, slot_lines, onchain_lines)
    print("J7 fills and quoted J7 attempts since %s: %d" % (args.since, len(items)))
    for net in sorted({i["net"] for i in items}, key=lambda n: (n != "solana", n)):
        for kind in ("fill", "quoted"):
            sub = [i for i in items if i["net"] == net and i["kind"] == kind]
            if not sub:
                continue
            print("\n  %s %s n=%d" % (net, kind + "s" if kind == "fill" else "attempts (quoted, not filled)", len(sub)))
            for b in "fdcabe":
                grp = [i for i in sub if i["bucket"] == b]
                if not grp:
                    continue
                pnl = [i["pnl"] for i in grp if i["pnl"] is not None]
                extra = (" pnl $%+.2f over %d closed, %d open" % (sum(pnl), len(pnl), sum(i["open"] for i in grp))
                         if kind == "fill" else "")
                print("    %s %4d%s" % (b, len(grp), extra))
                for why, n in collections.Counter(i["why"] if b != "d" else "J7 first" for i in grp).most_common(6):
                    print("        %4d  %s" % (n, why[:110]))
                if b == "d":
                    gaps = sorted(float(g) for i in grp for g in re.findall(r"[+-]\d+\.\d+", i["why"]))
                    if gaps:
                        print("        lane detected after J7's receipt by p50 %+.2f s, max %+.2f s (n=%d, mixed clock)"
                              % (gaps[len(gaps) // 2], gaps[-1], len(gaps)))
    if args.list:
        for i in items:
            print(json.dumps({k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in i.items()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
