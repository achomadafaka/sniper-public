"""Chain-side pool activity for funded Solana entries, computed AFTER the fact.

Why: under J7 the entry arrives before DexScreener indexes the pair, so `facts.volume_m5` is
blind on ~40% of Solana fills (2026-09-11). Signature history supplies address transaction counts
for the 5 minutes before entry and a lower bound on pool age. Counts require full timestamp
coverage. This script uses `getSignaturesForAddress` on the route's last pool. It never touches the funded
process or the funded ledger's write lock: it reads the ledger read-only, calls the public read
endpoint, and writes its own SQLite file.

Usage: python3 scripts/research/pool_activity_backfill.py [--since 2026-08-28] [--rate 4]
"""
import argparse, calendar, json, os, sqlite3, sys, time, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LEDGER = os.path.join(ROOT, "sniper_tracker.db")
OUT = os.path.join(ROOT, "data", "research_pool_activity.db")
RPC = os.getenv("MEMECOIN_SOLANA_READ_RPC_URL", "https://api.mainnet-beta.solana.com")
PAGE, MAX_PAGES = 1000, 30


def rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC, data=body, headers={"Content-Type": "application/json"})
    for i in range(5):
        try:
            return json.loads(urllib.request.urlopen(req, timeout=25).read()).get("result")
        except Exception:
            time.sleep(2.0 * (i + 1))
    return None


def epoch(iso):
    return calendar.timegm(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S"))


def measure(pool, entry_epoch, rate):
    """Count of TRANSACTIONS TOUCHING THE POOL ADDRESS (Codex audit #11: renamed from "swap count"
    -- `getSignaturesForAddress` counts every transaction that references the address, which is
    not provably swaps-only) in [entry-300, entry) and [entry-60, entry), plus the pool's age at
    entry. Pages newest -> oldest until the 5-minute window is covered. Further bounded reads
    can extend the age bound.

    COVERAGE. A page the RPC call could not even return (failed after its own retries) is UNKNOWN
    coverage, never "we reached the end of history" -- before this fix a failed page and a
    genuinely short page collapsed into the same signal (both shorter than PAGE), so a transient
    RPC failure silently marked the 5-minute window covered when it might not have seen any of it.
    `addr_txns_m5`/`addr_txns_m1` are None unless the window is confirmed fully covered.

    A short page does not prove pool creation. The oldest observed timestamp gives only an age
    bound. Unknown timestamps before window coverage prevent exact counts.
    """
    before, times, pages, window_covered, coverage_failed = None, [], 0, False, False
    missing_time, sigs_seen = False, 0
    while pages < MAX_PAGES:
        params = [pool, {"limit": PAGE, **({"before": before} if before else {})}]
        page = rpc("getSignaturesForAddress", params)
        pages += 1
        if page is None:
            coverage_failed = True     # the call itself never answered -- unknown, not "done"
            break
        sigs_seen += len(page)
        bt = [s["blockTime"] for s in page
              if isinstance(s.get("blockTime"), (int, float)) and s["blockTime"] > 0]
        if not window_covered and len(bt) != len(page):
            missing_time = True
            coverage_failed = True
        times += bt
        if bt and min(bt) < entry_epoch - 300 and not missing_time:
            window_covered = True
        if len(page) < PAGE:
            break
        if window_covered and pages >= 5:
            break
        before = page[-1]["signature"]
        time.sleep(1.0 / rate)
    if not times:
        return None
    m5 = sum(1 for b in times if entry_epoch - 300 <= b < entry_epoch)
    m1 = sum(1 for b in times if entry_epoch - 60 <= b < entry_epoch)
    oldest_seen = min(times)
    age = entry_epoch - oldest_seen if oldest_seen <= entry_epoch else None
    return {"addr_txns_m5": m5 if window_covered else None,
            "addr_txns_m1": m1 if window_covered else None,
            "pool_age_s": age, "pool_age_is_bound": True,
            "pages": pages, "sigs_seen": sigs_seen, "coverage_failed": coverage_failed}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-08-28")
    ap.add_argument("--rate", type=float, default=4.0)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    out = sqlite3.connect(OUT)
    # SCHEMA CHANGED (Codex audit #11): txns_m5/txns_m1 renamed to addr_txns_m5/addr_txns_m1 (an
    # honest name -- getSignaturesForAddress counts transactions touching the address, not proven
    # swaps), and pool_age_is_bound added. A
    # `data/research_pool_activity.db` from before this fix has the OLD column names and rows that
    # may have read a failed page as complete history -- delete it to regenerate cleanly rather
    # than trying to migrate it; `done` below only skips positions already in THIS file.
    out.execute("""CREATE TABLE IF NOT EXISTS pool_activity(
        position_id INTEGER PRIMARY KEY, attempt_id TEXT, network_id TEXT, pool TEXT,
        entry_at TEXT, addr_txns_m5 INTEGER, addr_txns_m1 INTEGER, pool_age_s REAL,
        pool_age_is_bound INTEGER, pages INTEGER, sigs_seen INTEGER, volume_m5_blind INTEGER,
        realized_pnl REAL, computed_at TEXT, measurement_version INTEGER NOT NULL DEFAULT 2)""")
    if "measurement_version" not in {r[1] for r in out.execute("PRAGMA table_info(pool_activity)")}:
        out.execute("ALTER TABLE pool_activity ADD COLUMN measurement_version INTEGER NOT NULL DEFAULT 1")
    done = {r[0] for r in out.execute(
        "SELECT position_id FROM pool_activity "
        "WHERE measurement_version=2 AND addr_txns_m5 IS NOT NULL AND addr_txns_m1 IS NOT NULL")}
    led = sqlite3.connect("file:%s?mode=ro" % LEDGER, uri=True)
    led.row_factory = sqlite3.Row
    rows = led.execute("""SELECT p.id, p.opened_at, p.realized_pnl, a.attempt_id, a.quote_route_target rt,
        a.classification_json cj FROM memecoin_positions p
        JOIN memecoin_entry_attempts a ON a.position_id = p.id
        WHERE p.execution_mode='real' AND p.network_id='solana' AND p.signal_source!='manual'
          AND p.opened_at >= ? ORDER BY p.opened_at""", (a.since,)).fetchall()
    led.close()
    todo = [r for r in rows if r["id"] not in done and (r["rt"] or "").strip()]
    if a.limit:
        todo = todo[:a.limit]
    print("positions", len(rows), "todo", len(todo), flush=True)
    for r in todo:
        pool = r["rt"].split(",")[-1].strip()     # LAST hop is the pool that trades the coin
        try:
            blind = json.loads(r["cj"]).get("facts", {}).get("volume_m5") is None
        except Exception:
            blind = None
        m = measure(pool, epoch(r["opened_at"]), a.rate) or {}
        out.execute("INSERT OR REPLACE INTO pool_activity "
                    "(position_id,attempt_id,network_id,pool,entry_at,addr_txns_m5,addr_txns_m1,"
                    "pool_age_s,pool_age_is_bound,pages,sigs_seen,volume_m5_blind,realized_pnl,"
                    "computed_at,measurement_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,2)",
                    (r["id"], r["attempt_id"], "solana", pool, r["opened_at"], m.get("addr_txns_m5"),
                     m.get("addr_txns_m1"), m.get("pool_age_s"), m.get("pool_age_is_bound"),
                     m.get("pages"), m.get("sigs_seen"), blind, r["realized_pnl"],
                     time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
        out.commit()
        print(r["id"], r["opened_at"][:19], "blind" if blind else "read ", m, flush=True)
        time.sleep(1.0 / a.rate)


if __name__ == "__main__":
    main()
