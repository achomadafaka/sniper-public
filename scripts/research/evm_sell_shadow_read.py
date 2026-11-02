#!/usr/bin/env python3
"""EVM sell-path SHADOW read: our proved on-chain sales against J7's sells, per chain.

Alpha's activation rule (2026-09-29, spec docs/superpowers/specs/2026-09-29-memecoin-evm-onchain-sell-path-design.md):
the mirror may consume our rows on a chain only after >= 20 matched frames there with 0 false sells.

Read-only: the ledger (onchain_evm_sell_shadow, written by the watcher under
MEMECOIN_ONCHAIN_EVM_SELL_SHADOW_CHAINS; memecoin_events for J7). A J7 sell is `side='sell'` from
follow_activity whose id `swap:raybot:<tx>:<trader>:<mint>` names a trader in the watcher's ACKED
transfer set at the sell's time, rebuilt from the watcher log's "acked transfer set" lines (2026-09-29;
the current set called 25 sells by not-yet-watched traders missed). Matching is on (tx, mint).
  matched       a J7 sell with our row: delivered AND proved; its proof kind; the lead over J7
                at arrival (J7 received_at - our detected_at) and at decode (J7 - our decoded_at).
                Mixed clocks: ours is the host clock, J7's the corrected one (host ~+0.05 s).
  missed        a J7 sell from a trader in the set at its time, with no row of ours.
  unscorable    no row of ours and the set at its time is unknown (a process start before its
                first acked line, or no log): never counted as missed. Only current-set traders.
  ours_only     a row of ours with no J7 sell of that tx:
    other_tx       J7 has a sell of the same wallet and coin within +-10 min (another transaction);
    false_sell     none, and the wallet STILL HOLDS the coin after it (post_atomic > 0);
    unmatched_exit none, and the wallet holds nothing after it (J7 may have missed a real exit).
READY needs matched >= 20 and false_sell == 0.
a_native is reported on its own line (Gamma LOW, 2026-09-29): its proof is the wallet's BLOCK-level
native delta, so a plain transfer in a block where the wallet also received native from another
transaction reads as a sale.

    python3 scripts/research/evm_sell_shadow_read.py [--since 2026-09-29T00:00:00] [--list 10]
    python3 scripts/research/evm_sell_shadow_read.py --selftest
"""
import argparse
import bisect
import collections
import re
import contextlib
import importlib.util
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "scripts" / "research")]

LEDGER = REPO / "sniper_tracker.db"
CHAINS = ("4663", "56", "8453")
READY_N = 20
OTHER_TX_WINDOW_S = 600.0
WATCHER_LOGS = {"4663": "logs/onchain_evm_armed.log", "56": "logs/onchain_evm_armed.56.log",
                "8453": "logs/onchain_evm_armed.8453.log"}
_START = re.compile(r"^(\S+) \[onchain-evm\] watching \d+ verified address")
_ACKED = re.compile(r"^(\S+) \[onchain-evm\] acked transfer set \(\d+\): (.*)$")


def ts(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def j7_sell(fomo_event_id):
    """(tx, trader, mint) from a J7 swap id, or None."""
    parts = str(fomo_event_id or "").split(":")
    if len(parts) < 5 or parts[0] != "swap" or not parts[2].lower().startswith("0x"):
        return None
    return parts[2].lower(), parts[3].lower(), parts[4].lower()


def watched_timeline(path):
    """[(ts, frozenset | None)] from a watcher log: a process start is None (unknown) until that
    process logs its first acked transfer set. A missing log is an empty timeline (all unknown)."""
    out = []
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                m = _ACKED.match(line)
                if m:
                    out.append((ts(m.group(1)), frozenset(a for a in m.group(2).strip().split(",") if a)))
                    continue
                m = _START.match(line)
                if m:
                    out.append((ts(m.group(1)), None))
    except OSError:
        return []
    out.sort(key=lambda row: row[0])
    return out


def set_at(timeline):
    """`at(iso)`: the acked set in force at that time, or None when it is unknown."""
    times = [t for t, _s in timeline]

    def at(when):
        i = bisect.bisect_right(times, ts(when)) - 1
        return timeline[i][1] if i >= 0 else None
    return at


def classify(ours, j7, watched_at, current=frozenset()):
    """Pure. ours: [{tx, mint, address, proof, detected_at, decoded_at, post_atomic, source_user_id}];
    j7: [{tx, trader, mint, source_user_id, received_at}]; watched_at(received_at) -> set | None, the
    set subscribed when the sell arrived. A sell with our row is matched. Without one it is missed
    only when its trader was in the set in force; when that set is unknown it is unscorable (counted
    for traders in `current` only, so it is not every J7 sell on the chain), never missed."""
    ours_by = {(o["tx"], o["mint"]): o for o in ours}
    j7_keys = {(j["tx"], j["mint"]) for j in j7}
    j7w, unscorable = [], 0
    for j in j7:
        at = watched_at(j["received_at"])
        if (j["tx"], j["mint"]) in ours_by or (at is not None and j["trader"] in at):
            j7w.append(j)
        elif at is None and j["trader"] in current:
            unscorable += 1
    out = {"j7_watched": len(j7w), "unscorable": unscorable, "matched": 0, "missed": 0,
           "proofs": collections.Counter(),
           "lead_arrival": [], "lead_decode": [], "other_tx": 0, "false_sell": 0,
           "unmatched_exit": 0, "false_rows": [], "missed_rows": [],
           "ours_only_by_proof": collections.Counter(), "false_by_proof": collections.Counter()}
    for j in j7w:
        o = ours_by.get((j["tx"], j["mint"]))
        if o is None:
            out["missed"] += 1
            out["missed_rows"].append(j)
            continue
        out["matched"] += 1
        out["proofs"][o["proof"]] += 1
        out["lead_arrival"].append(ts(j["received_at"]) - ts(o["detected_at"]))
        out["lead_decode"].append(ts(j["received_at"]) - ts(o["decoded_at"]))
    for o in ours:
        if (o["tx"], o["mint"]) in j7_keys:
            continue
        out["ours_only_by_proof"][o["proof"]] += 1
        near = any(j["mint"] == o["mint"] and j["source_user_id"] == o["source_user_id"]
                   and abs(ts(j["received_at"]) - ts(o["detected_at"])) <= OTHER_TX_WINDOW_S
                   for j in j7)
        if near:
            out["other_tx"] += 1
        elif o.get("post_atomic") is not None and int(o["post_atomic"]) > 0:
            out["false_sell"] += 1
            out["false_by_proof"][o["proof"]] += 1
            out["false_rows"].append(o)
        else:
            out["unmatched_exit"] += 1
    out["ready"] = out["matched"] >= READY_N and out["false_sell"] == 0
    return out


def _watched(conn, chain):
    spec = importlib.util.spec_from_file_location(
        "evmwatch_read", REPO / "scripts" / "research" / "onchain_evm_wallet_watch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._configure(chain)
    return set(mod._verified_addresses(conn))


def report(args):
    from sniper.persistence.db_core import get_readonly_connection
    since = args.since or (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    with contextlib.closing(get_readonly_connection(str(LEDGER))) as c:
        try:
            ours_all = [dict(r) for r in c.execute(
                "SELECT network_id, tx_hash, mint, address, source_user_id, proof, detected_at, "
                "decoded_at, post_atomic FROM onchain_evm_sell_shadow WHERE detected_at>=?", (since,))]
        except Exception:
            ours_all = []                     # the table exists only once the watcher wrote a row
        j7_all = []
        for fid, uid, chain, recv in c.execute(
                "SELECT fomo_event_id, source_user_id, network_id, received_at FROM memecoin_events "
                "WHERE side='sell' AND signal_source='follow_activity' AND network_id IN (?,?,?) "
                "AND received_at>=?", (*CHAINS, since)):
            parsed = j7_sell(fid)
            if parsed:
                j7_all.append({"chain": chain, "tx": parsed[0], "trader": parsed[1], "mint": parsed[2],
                               "source_user_id": uid, "received_at": recv})
        watched = {chain: _watched(c, chain) for chain in CHAINS}
    print("EVM sell shadow since %s (J7 sells scored against the watcher's acked set AT THEIR TIME; "
          "unknown set = unscorable; READY at >= %d matched and 0 false sells)" % (since, READY_N))
    for chain in CHAINS:
        ours = [{"tx": o["tx_hash"], **o} for o in ours_all if o["network_id"] == chain]
        at = set_at(watched_timeline(REPO / WATCHER_LOGS[chain]))
        r = classify(ours, [j for j in j7_all if j["chain"] == chain], at, current=watched[chain])
        q = lambda xs, f: "%.2f" % sorted(xs)[int(f * (len(xs) - 1))] if xs else "-"   # noqa: E731
        print("%-5s J7 watched sells %3d  matched %3d  missed %3d  unscorable %3d  proofs %s  lead arrival"
              " p50 %s p90 %s  lead decode p50 %s  ours-only: other_tx %d, false %d, unmatched exit %d  %s" % (
                  chain, r["j7_watched"], r["matched"], r["missed"], r["unscorable"], dict(r["proofs"]),
                  q(r["lead_arrival"], .5), q(r["lead_arrival"], .9), q(r["lead_decode"], .5),
                  r["other_tx"], r["false_sell"], r["unmatched_exit"],
                  "READY" if r["ready"] else "not ready"))
        print("      a_native (block-level proof): matched %d, ours-only %d, false %d" % (
            r["proofs"]["a_native"], r["ours_only_by_proof"]["a_native"], r["false_by_proof"]["a_native"]))
        for row in (r["false_rows"] + r["missed_rows"])[:args.list]:
            print("   ", {k: row.get(k) for k in ("tx", "mint", "address", "trader", "proof",
                                                    "received_at", "detected_at")})
    return 0


def selftest():
    W, OTHER_W = "0x" + "aa" * 20, "0x" + "bb" * 20
    j7 = [{"tx": "0x1", "trader": W, "mint": "0xm", "source_user_id": "u", "received_at": "2026-09-29T00:00:02+00:00"},
          {"tx": "0x2", "trader": W, "mint": "0xm", "source_user_id": "u", "received_at": "2026-09-29T00:05:00+00:00"},
          {"tx": "0x9", "trader": OTHER_W, "mint": "0xm", "source_user_id": "v", "received_at": "2026-09-29T00:06:00+00:00"}]
    row = lambda tx, post, mint="0xm", at="2026-09-29T00:00:01+00:00": {    # noqa: E731
        "tx": tx, "mint": mint, "address": W, "source_user_id": "u", "proof": "b_relay",
        "detected_at": at, "decoded_at": at, "post_atomic": post}
    ours = [row("0x1", "0"),                                   # matched
            row("0x3", "5", at="2026-09-29T00:04:00+00:00"),   # J7 sold the same coin 60 s later: other tx
            row("0x4", "5", mint="0xz"),                        # no J7 near, still holding: FALSE
            row("0x5", "0", mint="0xy")]                        # no J7 near, holds nothing: unmatched exit
    always = lambda _when: {W}                                                   # noqa: E731
    r = classify(ours, j7, always)
    assert (r["j7_watched"], r["matched"], r["missed"]) == (2, 1, 1), r
    assert (r["other_tx"], r["false_sell"], r["unmatched_exit"]) == (1, 1, 1), r
    assert r["lead_arrival"] == [1.0] and r["proofs"] == {"b_relay": 1}
    native = classify([dict(ours[2], proof="a_native")], j7, always)
    assert native["false_by_proof"] == {"a_native": 1} and native["ours_only_by_proof"] == {"a_native": 1}
    assert r["ready"] is False
    import copy
    many_j7 = [dict(j7[0], tx="0x%d" % (100 + i)) for i in range(20)]
    many_ours = [dict(ours[0], tx="0x%d" % (100 + i)) for i in range(20)]
    assert classify(many_ours, many_j7, always)["ready"] is True
    assert classify(many_ours + [copy.deepcopy(ours[2])], many_j7, always)["ready"] is False, "a false sell blocks"
    assert j7_sell("swap:raybot:0xAB:0xCD:0xEF") == ("0xab", "0xcd", "0xef")
    assert j7_sell("swap:uuid-no-tx") is None
    print("selftest ok")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since")
    ap.add_argument("--list", type=int, default=10)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    return selftest() if args.selftest else report(args)


if __name__ == "__main__":
    raise SystemExit(main())
