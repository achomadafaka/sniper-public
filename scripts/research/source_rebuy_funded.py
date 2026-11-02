"""Funded copy outcome when the copied buy is the source's FIRST buy of the coin vs a RE-BUY.

Question (Gamma, 2026-09-28): do funded copies of a source's re-buy of a coin do worse than copies of
its first buy? Prompted by 66271 (LingardSol DC4xMbLm, b_stop -40% in 128 s), which on a mint-only
read looked like a re-buy. It was not: the earlier buy and the sells were a DIFFERENT armed source
(badabeepp, buy 03:32Z at ~1/12 the price, selling into the run before and after LingardSol's first
buy). Hence the EXPLORATORY second split below, set after the primary null and labelled as such.

`memecoin_events.is_first_buy` cannot answer it: its only live writer (memecoin_follow_activity)
hard-codes 0, so every row since 09-20 reads 0. The class is derived here from the source's own
earlier events on that coin instead.

PRE-REGISTERED before the first run:
  * Sample: funded (execution_mode='real') closed positions opened >= 2026-09-12 (>= 3 days of
    follow_activity history behind each), manual closes excluded.
  * Class, from memecoin_events of the SAME source_user_id, mint and network, received_at (HOST
    receipt clock; created_at is the source's claim and can sit minutes off chain time) before the
    position's opened_at, within 7 days, the trigger event itself excluded by transaction signature:
      FIRST             no earlier buy and no earlier sell
      REBUY_HELD        an earlier buy, no earlier sell
      REBUY_AFTER_SELL  an earlier sell (with or without a recorded earlier buy)
  * Primary: mean return (realized_pnl / entry_notional), REBUY_AFTER_SELL minus FIRST, with a
    95% bootstrap CI resampled BY SOURCE WALLET (positions of one wallet move together).
    A difference is reported as real only if that CI excludes 0 AND n(REBUY_AFTER_SELL) >= 30.
  * Denominators are ours: positions we opened after our own gates. This describes our copies,
    not the sources' re-buys in general.
EXPLORATORY (added after the primary read, same metric and CI): OTHER_SOLD = a DIFFERENT
source_user_id has a sell of the same mint/network before our opened_at (7 days); OTHER_HELD = a
different source bought it but none sold; SOLO = no other source on it. OTHER_SOLD - SOLO.
Limits: an earlier buy J7 did not deliver (before follow_activity, or a lane J7 never saw) makes a
re-buy read as FIRST, so FIRST is contaminated toward REBUY, never the reverse.

Read-only: the ledger in mode=ro. No network.
"""
import argparse
import collections
import contextlib
import random
import sqlite3
from datetime import datetime, timedelta

LEDGER = "/home/bigpapi/Desktop/Sniper/sniper_tracker.db"


def sig_of(event_id):
    """Transaction id carried by an origin/event id, or None. EVM 0x hashes included."""
    p = str(event_id or "").split(":")
    if len(p) >= 3 and p[0] == "swap" and p[1] == "raybot":
        return p[2].lower() if p[2].startswith("0x") else p[2]
    if len(p) >= 2 and p[0] in ("onchain", "slotlane", "pump"):
        return p[1]
    return None


def classify(prior):
    """prior: iterable of 'buy'/'sell' sides before the trigger."""
    sides = set(prior)
    if "sell" in sides:
        return "REBUY_AFTER_SELL"
    return "REBUY_HELD" if "buy" in sides else "FIRST"


def ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def cluster_boot_diff(a, b, n_boot=4000, seed=7):
    """a, b: {wallet: [returns]}. Mean(a) - mean(b), CI from resampling wallets in each group."""
    rng = random.Random(seed)
    ka, kb = list(a), list(b)
    if not ka or not kb:
        return None
    out = []
    for _ in range(n_boot):
        xa = [x for w in (rng.choice(ka) for _ in ka) for x in a[w]]
        xb = [x for w in (rng.choice(kb) for _ in kb) for x in b[w]]
        out.append(sum(xa) / len(xa) - sum(xb) / len(xb))
    out.sort()
    return out[int(0.025 * n_boot)], out[int(0.975 * n_boot) - 1]


def load(conn, since, network=None):
    q = ("SELECT id, network_id, source_user_id, source_handle, mint, opened_at, origin_event_id, entry_notional, "
         "realized_pnl FROM memecoin_positions WHERE execution_mode='real' AND is_open=0 AND opened_at >= ? "
         "AND entry_notional > 0 AND realized_pnl IS NOT NULL AND COALESCE(exit_reason,'') NOT LIKE 'manual%'")
    args = [since]
    if network:
        q += " AND network_id = ?"
        args.append(network)
    rows = []
    for pid, net, uid, handle, mint, opened, origin, notional, pnl in conn.execute(q, args):
        trig = sig_of(origin)
        lo = (ts(opened) - timedelta(days=7)).isoformat()
        prior = [side for side, eid in conn.execute(
            "SELECT side, fomo_event_id FROM memecoin_events WHERE source_user_id=? AND mint=? AND network_id=? "
            "AND received_at >= ? AND received_at < ?", (uid, mint, net, lo, opened))
            if not (trig and sig_of(eid) == trig) and eid != origin]
        others = {side for side, in conn.execute(
            "SELECT side FROM memecoin_events WHERE source_user_id<>? AND mint=? AND network_id=? "
            "AND received_at >= ? AND received_at < ?", (uid, mint, net, lo, opened))}
        cross = "OTHER_SOLD" if "sell" in others else ("OTHER_HELD" if "buy" in others else "SOLO")
        rows.append({"id": pid, "net": net, "wallet": handle or uid, "cls": classify(prior),
                     "cross": cross, "ret": pnl / notional, "pnl": pnl})
    return rows


def report(rows, label, key="cls", base="FIRST", classes=("FIRST", "REBUY_HELD", "REBUY_AFTER_SELL")):
    by = collections.defaultdict(lambda: collections.defaultdict(list))
    for r in rows:
        by[r[key]][r["wallet"]].append(r)
    print("\n%s: %d closes" % (label, len(rows)))
    for cls in classes:
        xs = [r for w in by[cls].values() for r in w]
        if not xs:
            print("  %-17s n=0" % cls)
            continue
        rets = sorted(r["ret"] for r in xs)
        wins = sum(1 for r in xs if r["pnl"] > 0)
        print("  %-17s n=%-4d wallets=%-3d mean ret %+6.1f%%  median %+6.1f%%  win %d/%d  PnL $%+.2f" % (
            cls, len(xs), len(by[cls]), 100 * sum(rets) / len(rets), 100 * rets[len(rets) // 2], wins, len(xs),
            sum(r["pnl"] for r in xs)))
    ret = lambda d: {w: [r["ret"] for r in v] for w, v in d.items()}
    for cls in classes[:0:-1]:
        ci = cluster_boot_diff(ret(by[cls]), ret(by[base]))
        n = sum(len(v) for v in by[cls].values())
        if ci:
            a = [x for v in ret(by[cls]).values() for x in v]
            b = [x for v in ret(by[base]).values() for x in v]
            d = sum(a) / len(a) - sum(b) / len(b)
            call = "SEPARATED" if (ci[0] > 0 or ci[1] < 0) and n >= 30 else "not separated"
            print("  %s - %s: %+.1f pp, 95%% CI by wallet [%+.1f, %+.1f] (n=%d) -> %s" % (
                cls, base, 100 * d, 100 * ci[0], 100 * ci[1], n, call))


def selftest():
    assert sig_of("swap:raybot:ABC:x:y") == "ABC" and sig_of("onchain:SIG") == "SIG"
    assert sig_of("slotlane:S2") == "S2" and sig_of("swap:raybot:0xAB:x") == "0xab" and sig_of("swap:uuid") is None
    assert classify([]) == "FIRST" and classify(["buy"]) == "REBUY_HELD"
    assert classify(["buy", "sell"]) == "REBUY_AFTER_SELL" and classify(["sell"]) == "REBUY_AFTER_SELL"
    lo, hi = cluster_boot_diff({"w1": [0.0, 0.1], "w2": [0.2]}, {"w3": [-0.5], "w4": [-0.4, -0.6]}, n_boot=500)
    assert lo > 0 and hi > lo, (lo, hi)                 # a clean +0.6 gap must separate
    # the ledger join (trigger exclusion, another source's sell) is tested on a fixture DB in
    # tests/test_research_source_rebuy_funded.py; kept out of here so the brain index does not read
    # this read-only script as a ledger writer.
    print("selftest ok")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-12")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
    else:
        with contextlib.closing(sqlite3.connect("file:%s?mode=ro" % LEDGER, uri=True, timeout=10)) as c:
            report(load(c, a.since), "all funded chains since %s" % a.since)
            sol = load(c, a.since, "solana")
            report(sol, "solana since %s" % a.since)
            x = ("SOLO", "OTHER_HELD", "OTHER_SOLD")
            report(load(c, a.since), "EXPLORATORY all chains, other sources", "cross", "SOLO", x)
            report(sol, "EXPLORATORY solana, other sources", "cross", "SOLO", x)
