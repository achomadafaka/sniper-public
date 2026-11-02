#!/usr/bin/env python3
"""Land lag of our funded Solana fills, per armed wallet and lane (Gamma, 2026-10-09; operator order
via Alpha: Nozomi for every Solana wallet whose average funded fill lands later than S+1).

Population: memecoin_entry_attempts with a position, network_id 'solana', memecoin_positions
execution_mode 'real', decided in the window, source wallet armed now (memecoin_wallets real_approved=1,
'solana' in real_chains). Manual entries are out. Lanes: slot lane (delivery_lane slot_lane_*),
on-chain lane (signal_source onchain_watch), J7 (signal_source follow_activity).

S = the slot of the source transaction (the signature inside origin_event_id); our slot = the slot of
our fill transaction (trace fields.tx_sig, else slot_send_call.tx_sig). Both from getSignatureStatuses
(searchTransactionHistory) on the research key, flat-book guarded and paced; a signature the chain
does not return is censored and counted, never guessed. lag = our slot - S.

    python3 scripts/research/solana_land_lag_by_wallet.py [--days 14] [--max-calls 20]
    python3 scripts/research/solana_land_lag_by_wallet.py --selftest
"""
import argparse
import json
import re
import sqlite3
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "scripts" / "research")]
SIG = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{80,90}$")


def source_sig(origin_event_id):
    for part in str(origin_event_id or "").split(":"):
        if SIG.match(part):
            return part
    return None


def lane_of(delivery_lane, signal_source):
    if str(delivery_lane or "").startswith("slot_lane"):
        return "slot"
    if signal_source == "onchain_watch":
        return "onchain"
    if signal_source == "follow_activity":
        return "j7"
    return None


def our_sig(trace):
    sig = (trace.get("fields") or {}).get("tx_sig")
    if sig:
        return sig
    for s in trace.get("substage_events") or []:
        if s.get("name") == "slot_send_call" and s.get("tx_sig"):
            return s["tx_sig"]
    return None


def summary(lags):
    if not lags:
        return {"n": 0}
    n = len(lags)
    return {"n": n, "mean": round(statistics.mean(lags), 2), "median": statistics.median(lags),
            "S": round(sum(1 for x in lags if x <= 0) / n, 2),
            "S+1": round(sum(1 for x in lags if x == 1) / n, 2),
            "S+2+": round(sum(1 for x in lags if x >= 2) / n, 2)}


def rows(db, days):
    armed = {}
    for uid, handle, chains in db.execute("SELECT source_user_id, handle, real_chains FROM memecoin_wallets "
                                          "WHERE real_approved = 1"):
        if "solana" in [c.strip() for c in str(chains or "").split(",")]:
            armed[uid] = handle
    q = ("SELECT a.source_user_id, a.delivery_lane, a.signal_source, a.origin_event_id, a.trace_json "
         "FROM memecoin_entry_attempts a JOIN memecoin_positions p ON p.id = a.position_id "
         "WHERE a.network_id = 'solana' AND p.execution_mode = 'real' "
         "AND a.decision_started_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)")
    out, skipped = [], {"not_armed": 0, "manual_or_other_lane": 0, "no_source_sig": 0, "no_our_sig": 0}
    for uid, dl, ss, origin, tj in db.execute(q, ("-%d days" % days,)):
        if uid not in armed:
            skipped["not_armed"] += 1
            continue
        lane = lane_of(dl, ss)
        if lane is None:
            skipped["manual_or_other_lane"] += 1
            continue
        src = source_sig(origin)
        mine = our_sig(json.loads(tj or "{}"))
        if not src:
            skipped["no_source_sig"] += 1
            continue
        if not mine:
            skipped["no_our_sig"] += 1
            continue
        out.append({"uid": uid, "handle": armed[uid], "lane": lane, "src": src, "ours": mine})
    return armed, out, skipped


def slots(sigs, max_calls):
    import copier_crowd_and_fees as ccf
    import funded_exit_truth as fet
    ccf.install_flat_guard()
    got, calls = {}, 0
    sigs = list(dict.fromkeys(sigs))
    for i in range(0, len(sigs), 256):
        if calls >= max_calls:
            break
        chunk = sigs[i:i + 256]
        res = fet.rpc("getSignatureStatuses", [chunk, {"searchTransactionHistory": True}])
        calls += 1
        for sig, st in zip(chunk, ((res or {}).get("value") or [None] * len(chunk))):
            if isinstance(st, dict) and isinstance(st.get("slot"), int):
                got[sig] = st["slot"]
    return got, calls


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--max-calls", type=int, default=20)
    ap.add_argument("--selftest", action="store_true")
    ns = ap.parse_args()
    if ns.selftest:
        return selftest()
    from sniper.persistence.db_core import get_readonly_connection
    db = get_readonly_connection(str(REPO / "sniper_tracker.db"))
    armed, rs, skipped = rows(db, ns.days)
    got, calls = slots([r["src"] for r in rs] + [r["ours"] for r in rs], ns.max_calls)
    censored = 0
    per = {}
    for r in rs:
        if r["src"] not in got or r["ours"] not in got:
            censored += 1
            continue
        lag = got[r["ours"]] - got[r["src"]]
        per.setdefault(r["uid"], {"all": [], "slot": [], "onchain": [], "j7": []})
        per[r["uid"]]["all"].append(lag)
        per[r["uid"]][r["lane"]].append(lag)
    print("armed Solana wallets: %d; fills in window: %d; censored (slot unread): %d; skipped %s; "
          "RPC calls %d" % (len(armed), len(rs), censored, skipped, calls))
    pooled = {k: [x for v in per.values() for x in v[k]] for k in ("all", "slot", "onchain", "j7")}
    print("POOLED " + json.dumps({k: summary(v) for k, v in pooled.items()}))
    for uid in sorted(armed, key=lambda u: -len(per.get(u, {}).get("all", []))):
        v = per.get(uid, {"all": [], "slot": [], "onchain": [], "j7": []})
        print("%-18s %s %s" % (armed[uid], uid, json.dumps({k: summary(x) for k, x in v.items()})))
    late = [u for u in armed if len(per.get(u, {}).get("all", [])) >= 3
            and statistics.mean(per[u]["all"]) > 1]
    thin = [u for u in armed if len(per.get(u, {}).get("all", [])) < 3]
    print("MEAN>1 AND n>=3: %s" % ",".join(late))
    print("n<3: %s" % ",".join("%s(%s,n=%d)" % (armed[u], u, len(per.get(u, {}).get("all", []))) for u in thin))
    return 0


def selftest():
    sig = "4o4onMrz5WNVnRsbbzK2VLAcxMBTg3LhCNxk5dxFqwJFhsPuFEWUqieERtq8NiJ2X8yT1FuVz3gZaLXwpHxJz9K3"
    assert source_sig("slotlane:" + sig) == sig and source_sig("onchain:" + sig) == sig
    assert source_sig("swap:raybot:%s:6xmm:3xuw" % sig) == sig and source_sig("fomo-123") is None
    assert lane_of("slot_lane_processed", "slot_lane") == "slot"
    assert lane_of("slot_lane_confirmed_receipt", "slot_lane") == "slot"
    assert lane_of(None, "onchain_watch") == "onchain" and lane_of("onchain_fallback_after_slot_lane", "onchain_watch") == "onchain"
    assert lane_of("j7_wallets", "follow_activity") == "j7" and lane_of(None, "manual") is None
    assert our_sig({"fields": {"tx_sig": "a"}}) == "a"
    assert our_sig({"substage_events": [{"name": "slot_send_call", "tx_sig": "b"}]}) == "b"
    s = summary([0, 1, 1, 2, 5])
    assert s == {"n": 5, "mean": 1.8, "median": 1, "S": 0.2, "S+1": 0.4, "S+2+": 0.4}, s
    print("selftest ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
