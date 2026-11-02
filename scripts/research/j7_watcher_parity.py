#!/usr/bin/env python3
"""J7 vs the EVM watcher: could EVM go J7-shadow with NO fallback? (Alpha order (2), 2026-09-30.)

Pre-registered as brain episode 02c9d9c4 before any number was computed. Read-only: the funded
ledger through a read-only connection, bounded by id (missed_signal_map.load).

- Tx recall per chain: J7 armed buys whose exact transaction the watcher detected (signature
  '<chain>:<tx>:<mint>'). The missed-signal map matched by (wallet, mint) within 120 s, which
  scored a missed repeat buy as a hit.
- Miss cause: J7's trader address is proven for that wallet on that chain (a delivery miss), or it
  is not (the address is not watched).
- Signal timing: J7 buys grouped by (chain, wallet, mint) within 120 s; the watcher's first
  detection of that key against the J7 first receipt.
- Stake: attempts near the signals the watcher never saw or saw more than 10 s late.

    python3 scripts/research/j7_watcher_parity.py --since 2026-09-13T00:00:00+00:00
    python3 scripts/research/j7_watcher_parity.py --selftest
"""
import argparse
import collections
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from scripts.research.missed_signal_map import ArmBook, Attempts, CLUSTER_S, load, pct, ts  # noqa: E402
from scripts.research.missed_signal_map import iso as iso_  # noqa: E402

CHAINS = ("4663", "56", "8453")
LATE_S = 10.0
# The forward gate (spec 2026-09-30-memecoin-j7-data-only-evm-design.md, pre-registered): a chain may
# be listed in MEMECOIN_J7_DATA_ONLY_EVM_CHAINS only after n >= GATE_N hits within GATE_HIT_S, at
# >= GATE_RATE. GATE_HIT_S is the follow age gate: a later detection could not have entered anyway.
GATE_N, GATE_RATE, GATE_HIT_S = 50, 0.98, 60.0
# FORWARD ONLY (Gamma MEDIUM 1, Alpha 2026-09-30): the population amendment f1888d26d was written
# after the first live read, so only J7 receipts after its commit count toward the gate. What was
# already seen is reported separately and never inside the gate.
GATE_FROM = "2026-09-30T08:20:59+00:00"
SEARCH_BEFORE_S, SEARCH_AFTER_S = 120.0, 600.0


def swap_parts(fomo_event_id):
    """(tx, trader) lowercased from 'swap:<bot>:<tx>:<trader>:<mint>', or None."""
    parts = str(fomo_event_id or "").split(":")
    if len(parts) < 5 or parts[0] != "swap":
        return None
    return parts[2].lower(), parts[3].lower()


def timing_class(first_det, t0):
    if first_det is None:
        return "never"
    lag = first_det - t0
    return "<=1s" if lag <= 1.0 else ("1-10s" if lag <= LATE_S else "10-600s")


def analyse(j7, det, att, approvals, proven, positions_pnl, chains=CHAINS):
    """j7/det/att/approvals as missed_signal_map.load returns them; proven {(chain, uid): {addr}};
    positions_pnl {position_id: realized_pnl or None}."""
    book, attempts = ArmBook(approvals), Attempts(att)
    det_tx, det_key, det_txs = {}, collections.defaultdict(list), set()
    # (net, uid) / (net, address) -> [(detected_at, tx)]. Coverage is a detection of ANOTHER tx
    # before the buy: the buy's own detection would make every hit covered by definition.
    seen_wallet, seen_addr = collections.defaultdict(list), collections.defaultdict(list)
    for d in det:
        net = str(d["network_id"])
        if net not in chains:
            continue
        parts = str(d["signature"]).split(":")
        if len(parts) < 3:
            continue
        t = ts(d["detected_at"])
        det_tx[(net, parts[1].lower())] = min(t, det_tx.get((net, parts[1].lower()), t))
        det_txs.add((net, parts[1].lower()))
        det_key[(net, str(d["source_user_id"]), str(d["mint"]).lower())].append(t)
        seen_wallet[(net, str(d["source_user_id"]))].append((t, parts[1].lower()))
        seen_addr[(net, str(d.get("address") or "").lower())].append((t, parts[1].lower()))
    out = {}
    for net in chains:
        buys = []
        for e in j7:
            if str(e["network_id"]) != net or e.get("delivery_lane") != "j7_wallets":
                continue
            sp = swap_parts(e["fomo_event_id"])
            t = ts(e["received_at"])
            if sp is None or book.armed(e["source_user_id"], net, t) is not True:
                continue
            buys.append({"uid": str(e["source_user_id"]), "mint": str(e["mint"]).lower(), "t": t,
                         "tx": sp[0], "trader": sp[1]})
        hits, lat, causes, miss_by_wallet = 0, [], collections.Counter(), collections.Counter()
        cov = collections.Counter()
        for b in buys:
            covered = any(t < b["t"] and tx != b["tx"] for t, tx in seen_wallet.get((net, b["uid"]), ()))
            b["covered"] = covered
            dt = det_tx.get((net, b["tx"]))
            cov["covered_buys" if covered else "uncovered_buys"] += 1
            if dt is not None:
                hits += 1
                cov["covered_hits" if covered else "uncovered_hits"] += 1
                lat.append(dt - b["t"])
                continue
            if not covered:
                causes["wallet_never_detected_before"] += 1
            elif any(t < b["t"] and tx != b["tx"] for t, tx in seen_addr.get((net, b["trader"]), ())):
                causes["address_seen_before_so_delivery_miss"] += 1
            else:
                causes["other_address_" + ("proven_now" if b["trader"] in proven.get((net, b["uid"]), set())
                                           else "unproven")] += 1
            miss_by_wallet[b["uid"]] += 1
        # signals: (wallet, mint) clusters from the first J7 receipt
        signals = []
        by = collections.defaultdict(list)
        for b in sorted(buys, key=lambda b: b["t"]):
            by[(b["uid"], b["mint"])].append(b)
        for (uid, mint), bs in by.items():
            first = None
            for b in bs:
                if first is None or b["t"] - first["t"] > CLUSTER_S:
                    first = b
                    signals.append(b)
        timing, timing_cov = collections.Counter(), collections.Counter()
        stake = {"attempts": 0, "filled": 0, "realized_pnl": 0.0, "open_or_unknown": 0}
        for s in signals:
            ds = [t for t in det_key.get((net, s["uid"], s["mint"]), [])
                  if s["t"] - SEARCH_BEFORE_S <= t <= s["t"] + SEARCH_AFTER_S]
            ds += [det_tx[(net, b["tx"])] for b in by[(s["uid"], s["mint"])]
                   if (net, b["tx"]) in det_tx and abs(b["t"] - s["t"]) <= CLUSTER_S]
            cls = timing_class(min(ds) if ds else None, s["t"])
            timing[cls] += 1
            if s["covered"]:
                timing_cov[cls] += 1
            if cls in ("never", "10-600s"):
                for a in attempts.near(net, s["uid"], s["mint"], s["t"]):
                    stake["attempts"] += 1
                    pid = a.get("position_id")
                    if pid is not None:
                        stake["filled"] += 1
                        pnl = positions_pnl.get(pid)
                        if pnl is None:
                            stake["open_or_unknown"] += 1
                        else:
                            stake["realized_pnl"] += float(pnl)
        j7_txs = {(net, b["tx"]) for b in buys}
        out[net] = {
            "j7_armed_buys": len(buys), "tx_detected": hits,
            "tx_recall_pct": round(100.0 * hits / len(buys), 1) if buys else None,
            "detect_minus_j7_receipt_s": {"p10": pct(lat, 0.1), "p50": pct(lat, 0.5),
                                          "p90": pct(lat, 0.9), "max": max(lat) if lat else None},
            **dict(cov),
            "covered_tx_recall_pct": (round(100.0 * cov["covered_hits"] / cov["covered_buys"], 1)
                                      if cov["covered_buys"] else None),
            "miss_causes": dict(causes),
            "misses_by_wallet": dict(miss_by_wallet.most_common(8)),
            "signals": len(signals), "signal_timing": dict(timing),
            "covered_signal_timing": dict(timing_cov),
            "stake_never_or_late": {**stake, "realized_pnl": round(stake["realized_pnl"], 2)},
            "watcher_only_detections": sum(1 for k in det_txs if k[0] == net and k not in j7_txs),
        }
    return out


def roster_view(events, detected_txs, armed, watched, net, side="buy", since_t=None):
    """Today's roster: J7 events by wallets armed NOW, whether J7's trader address is one the
    watcher subscribes NOW, and how many of those the watcher detected (exact tx). For sells,
    `detected_txs` is the sell shadow and `since_t` its first row on the chain."""
    out = collections.Counter()
    for e in events:
        if str(e["network_id"]) != net or e.get("delivery_lane") != "j7_wallets":
            continue
        uid, sp = str(e["source_user_id"]), swap_parts(e["fomo_event_id"])
        if uid not in armed or sp is None or (since_t is not None and ts(e["received_at"]) < since_t):
            continue
        out[side + "s"] += 1
        if sp[1] in watched.get(uid, ()):
            out["trader_watched_now"] += 1
            out["detected"] += (net, sp[0]) in detected_txs
    return dict(out)


def acked_history(path):
    """[(epoch, frozenset | None)] from an EVM watcher log. An 'acked transfer set (n): a,b' line
    (logged on every change since 2026-09-29) sets the subscribed addresses; a process-start
    'watching N verified address(es)' line makes them unknown until the next acked line. Lines
    without an ISO stamp (older format) are skipped."""
    out = []
    with open(path, errors="replace") as fh:
        for line in fh:
            acked = "acked transfer set (" in line
            if not acked and not ("watching " in line and " verified address" in line):
                continue
            try:
                t = ts(line.split(" ", 1)[0])
            except ValueError:
                continue
            out.append((t, frozenset(a.strip().lower() for a in line.rsplit(": ", 1)[1].split(",")
                                     if a.strip()) if acked else None))
    return sorted(out, key=lambda x: x[0])


def watched_at_fn(history):
    """watched(t) -> the acked set in effect at t, or None when unknown."""
    import bisect
    times = [t for t, _s in history]

    def at(t):
        i = bisect.bisect_right(times, t) - 1
        return history[i][1] if i >= 0 else None
    return at


def gate_start(since_iso):
    """The gate never counts a J7 receipt before GATE_FROM, whatever the caller asks."""
    return max(ts(since_iso), ts(GATE_FROM))


def watched_around_fn(history):
    """around(t) -> the union of the last known acked set before t and the first after it: the
    addresses a restarting watcher was, and was about to be, subscribed to."""
    import bisect
    known = [(t, a) for t, a in history if a is not None]
    times = [t for t, _a in known]

    def around(t):
        i = bisect.bisect_right(times, t)
        out = set()
        if i > 0:
            out |= known[i - 1][1]
        if i < len(known):
            out |= known[i][1]
        return frozenset(out)
    return around


def gate_view(events, detected, watched_at, net, since_t=None, until_t=None, around=None, armed=None):
    """The pre-registered forward gate for one chain (spec 2026-09-30, amended the same day).
    Population: J7 follow buys whose J7 trader address was in the watcher's acked transfer set at
    the J7 receipt. An unknown instant (a restart before its first acked line) COUNTS when the
    address is in the nearest known sets (`around`), and is tallied: restart windows are where
    misses live (Gamma MEDIUM 2). Hit: the exact tx detected no later
    than GATE_HIT_S after the J7 receipt; a hit later than LATE_S is also counted late (a backfill
    recovery). `detected` {(chain, tx): earliest detected_at epoch}.

    A J7 buy whose id carries no transaction ('swap:<uuid>') cannot be matched to a detection and
    is in NEITHER n nor hits (amendment 2026-09-30): `no_tx_id` counts them in the same window, and
    `no_tx_id_armed` those by a wallet in `armed`. A PASS says nothing about them."""
    n = hits = late = unknown = no_tx = no_tx_armed = 0
    for e in events:
        if str(e["network_id"]) != net or e.get("delivery_lane") != "j7_wallets":
            continue
        sp, t = swap_parts(e["fomo_event_id"]), ts(e["received_at"])
        if (since_t is not None and t < since_t) or (until_t is not None and t >= until_t):
            continue
        if sp is None:
            no_tx += 1
            no_tx_armed += armed is not None and e.get("source_user_id") in armed
            continue
        acked = watched_at(t)
        if acked is None:
            acked = around(t) if around is not None else frozenset()
            if sp[1] in acked:
                unknown += 1
        if sp[1] not in acked:
            continue
        n += 1
        d = detected.get((net, sp[0]))
        if d is not None and d - t <= GATE_HIT_S:
            hits += 1
            late += (d - t) > LATE_S
    rate = hits / n if n else None
    return {"n": n, "hits": hits, "late": late, "unknown_watch_state": unknown,
            "rate_pct": round(100.0 * rate, 1) if rate is not None else None,
            "pass": bool(n >= GATE_N and rate is not None and rate >= GATE_RATE),
            "no_tx_id": no_tx, "no_tx_id_armed": None if armed is None else no_tx_armed}


def selftest():
    j7 = [
        {"fomo_event_id": "swap:b:0xAA:0xT1:0xM", "source_user_id": "u1", "network_id": "56",
         "mint": "0xM", "received_at": "2026-09-20T00:00:02+00:00", "delivery_lane": "j7_wallets"},
        {"fomo_event_id": "swap:b:0xBB:0xT1:0xM", "source_user_id": "u1", "network_id": "56",
         "mint": "0xM", "received_at": "2026-09-20T00:00:30+00:00", "delivery_lane": "j7_wallets"},
        {"fomo_event_id": "swap:b:0xCC:0xRELAY:0xN", "source_user_id": "u1", "network_id": "56",
         "mint": "0xN", "received_at": "2026-09-20T01:00:00+00:00", "delivery_lane": "j7_wallets"},
        {"fomo_event_id": "swap:b:0xDD:0xT2:0xM", "source_user_id": "u2", "network_id": "56",
         "mint": "0xM", "received_at": "2026-09-20T02:00:00+00:00", "delivery_lane": "j7_wallets"},
    ]
    det = [{"signature": "56:0xaa:0xm", "source_user_id": "u1", "network_id": "56", "mint": "0xm",
            "detected_at": "2026-09-20T00:00:01+00:00", "address": "0xT1"},
           {"signature": "56:0xee:0xz", "source_user_id": "u1", "network_id": "56", "mint": "0xz",
            "detected_at": "2026-09-20T03:00:00+00:00"}]
    approvals = [("u1", "chain_arm", "56", "2026-09-01T00:00:00+00:00")]          # u2 never armed
    att = [{"network_id": "56", "source_user_id": "u1", "mint": "0xn", "position_id": 7,
            "decision_started_at": "2026-09-20T01:00:01+00:00"}]
    r = analyse(j7, det, att, approvals, {("56", "u1"): {"0xt1"}}, {7: -3.5}, chains=("56",))["56"]
    assert r["j7_armed_buys"] == 3 and r["tx_detected"] == 1, r        # u2 unarmed; 0xBB repeat missed
    # 0xBB: u1 covered (detected 00:00:01) and its trader 0xt1 seen before -> delivery miss.
    # 0xCC: covered wallet, relay address never detected and not proven -> other_address_unproven.
    assert r["miss_causes"] == {"address_seen_before_so_delivery_miss": 1,
                                "other_address_unproven": 1}, r
    # 0xAA is the wallet's first detection, so it is NOT covered by itself (no circularity).
    assert r["covered_buys"] == 2 and r["uncovered_buys"] == 1 and r["covered_tx_recall_pct"] == 0.0, r
    assert r["uncovered_hits"] == 1, r
    assert r["signals"] == 2 and r["signal_timing"] == {"<=1s": 1, "never": 1}, r
    assert r["stake_never_or_late"]["filled"] == 1 and r["stake_never_or_late"]["realized_pnl"] == -3.5
    assert r["detect_minus_j7_receipt_s"]["p50"] == -1.0 and r["watcher_only_detections"] == 1
    rv = roster_view(j7, {("56", "0xaa")}, {"u1"}, {"u1": {"0xt1"}}, "56")
    assert rv == {"buys": 3, "trader_watched_now": 2, "detected": 1}, rv
    print("selftest ok")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since", default="2026-09-13T00:00:00+00:00")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--gate", action="store_true",
                    help="the pre-registered data-only gate per chain since --since (default: 7 days)")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if args.gate and "--since" not in sys.argv:
        # Load from the acked log's start so the already-seen window can be reported; the gate
        # itself never counts a receipt before GATE_FROM, whatever --since says.
        args.since = "2026-09-29T21:00:00+00:00"
    from sniper.persistence.db_core import get_readonly_connection
    from sniper.services.axiom_address_verify import watcher_address_owners
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_root
    load_env_files(str(state_root()))
    ledger = get_readonly_connection(str(state_root() / "sniper_tracker.db"))
    try:
        j7, _j7_any, _det, _att, approvals, _positions = load(ledger, args.since)
        det = [dict(r) for r in ledger.execute(          # all EVM detections: coverage needs history
            "SELECT signature, source_user_id, network_id, mint, detected_at, address "
            "FROM onchain_wallet_detections WHERE network_id IN (?,?,?)", CHAINS)]
        # load() reads attempts without position_id; Attempts keys on the mint, so lowercase it.
        att = [dict(r, mint=str(r["mint"]).lower()) for r in ledger.execute(
            "SELECT network_id, source_user_id, mint, decision_started_at, position_id "
            "FROM memecoin_entry_attempts WHERE decision_started_at >= ? AND network_id IN (?,?,?)",
            (args.since, *CHAINS))]
        proven = collections.defaultdict(set)
        for (net, addr), (uid, _handle) in watcher_address_owners(ledger).items():
            proven[(str(net), str(uid))].add(str(addr).lower())
        pids = [a["position_id"] for a in att if a.get("position_id") is not None]
        pnl = {}
        for i in range(0, len(pids), 500):
            chunk = pids[i:i + 500]
            pnl.update({r[0]: r[1] for r in ledger.execute(
                f"SELECT id, realized_pnl FROM memecoin_positions WHERE id IN ({','.join('?' * len(chunk))})",
                chunk)})
        if args.gate:
            from sniper.persistence import db_memecoin as mdb
            from sniper.services.axiom_address_verify import evm_watched_owners
            detected = {}
            for d in det:
                key = (str(d["network_id"]), str(d["signature"]).split(":")[1].lower())
                detected[key] = min(ts(d["detected_at"]), detected.get(key, float("inf")))
            out = {}
            for net in CHAINS:
                armed = set(mdb.real_approved_user_ids(ledger, network_id=net) or ())
                watched = {uid for uid, _h in evm_watched_owners(ledger, net).values()}
                log = state_root() / "logs" / ("onchain_evm_armed.log" if net == "4663"
                                               else "onchain_evm_armed.%s.log" % net)
                history = acked_history(log) if log.exists() else []
                at, around = watched_at_fn(history), watched_around_fn(history)
                first = next((t for t, a in history if a is not None), None)
                out[net] = {**gate_view(j7, detected, at, net, since_t=gate_start(args.since), around=around,
                                        armed=armed),
                            "seen_before_gate_not_counted": (gate_view(
                                j7, detected, at, net, since_t=first, until_t=ts(GATE_FROM), around=around,
                                armed=armed)
                                if first is not None else None),
                            "acked_history_from": next((iso_(t) for t, a in history if a is not None), None),
                            "armed_unwatched": sorted(armed - watched)}
            print(json.dumps({"since": args.since, "gate": out}, indent=1))
            return 0
        result = analyse(j7, det, att, approvals, proven, pnl)
        from sniper.persistence import db_memecoin as mdb
        from sniper.services.axiom_address_verify import evm_watched_owners
        from scripts.research.wallet_link_graph import first_id_since
        lo = first_id_since(ledger, args.since)
        sells = [dict(r) for r in ledger.execute(
            "SELECT fomo_event_id, source_user_id, network_id, received_at, delivery_lane FROM memecoin_events "
            "WHERE id >= ? AND received_at >= ? AND side='sell' AND delivery_lane='j7_wallets' "
            "AND network_id IN (?,?,?)", (lo, args.since, *CHAINS))]
        shadow = {(str(r[0]), str(r[1]).lower()): ts(r[2]) for r in ledger.execute(
            "SELECT network_id, tx_hash, detected_at FROM onchain_evm_sell_shadow")}
        buy_txs = {(str(d["network_id"]), str(d["signature"]).split(":")[1].lower()) for d in det}
        for net in CHAINS:
            armed = set(mdb.real_approved_user_ids(ledger, network_id=net) or ())
            watched = collections.defaultdict(set)
            for addr, (uid, _h) in evm_watched_owners(ledger, net).items():
                watched[uid].add(addr)
            first_shadow = min((t for (n, _tx), t in shadow.items() if n == net), default=None)
            result[net]["roster_now"] = {
                "armed": len(armed), "watched": len(armed & set(watched)),
                "armed_unwatched": sorted(armed - set(watched)),
                "buys": roster_view(j7, buy_txs, armed, watched, net),
                "sells_since_sell_shadow": (roster_view(sells, shadow, armed, watched, net, "sell",
                                                        first_shadow) if first_shadow else None)}
    finally:
        ledger.close()
    print(json.dumps({"since": args.since, **result}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
