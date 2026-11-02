"""How many funded Solana signals did we lose because no route could be built, and on which venue?

Question (Alpha, 2026-09-27): is a Meteora DBC builder worth it? jpegkite's 2iCpCQRK (a DBC
Token-2022 coin) got "jupiter 400 TOKEN_NOT_TRADABLE; raydium ROUTE_NOT_FOUND" and cost a fill.

Unit is the ORIGIN event, not the attempt: the retry lane re-attempts one origin several times, and
a route failure can be followed by a fill 10 s later (4A9j9RjC on 2026-09-25). An origin is LOST
when at least one attempt failed on the route and none filled. A route failure happens at the quote
or build stage, after the policy gates, so a lost origin is a fill we would otherwise have tried.

Venue of a coin = the owner program of DexScreener's pair account (classification facts
`liquidity_pair_address`, written at classification time), read with one getMultipleAccounts per
100 pairs. A coin with no pair address falls back to the program ids of its raybot origin
transaction; uuid-era origins carry no signature and stay UNKNOWN. Owners print raw beside a label,
so an unlabelled program is visible, never folded into "other".

Denominator caveat: the pair address exists only on attempts that reached classification, so the
filled-by-venue counts describe coins that got that far, not every signal.

Research key only (first MEMECOIN_RESEARCH_HELIUS_RPC_URLS entry via slot_lane_send_monitor), and
it refuses to run while a real Solana position is open (standing rule).

    python3 scripts/research/dbc_venue_gap.py --since 2026-09-13
    python3 scripts/research/dbc_venue_gap.py --selftest
"""
import argparse
import collections
import contextlib
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path[:0] = [HERE, REPO]
LEDGER = f"file:{os.path.join(REPO, 'sniper_tracker.db')}?mode=ro"

from audit_unbound_solana_venues import INFRA, KNOWN  # noqa: E402

DBC = "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"   # sniper/research/memecoin_opportunity/bags_solana.py
LABELS = {**KNOWN, DBC: "meteora dbc"}
ROUTE_GAP = ("no quote", "route build refused: swap build returned nothing")


def is_route_gap(reason):
    return bool(reason) and reason.startswith(ROUTE_GAP)


def tally(attempts, venue_of_mint):
    """attempts: dicts with origin, mint, handle, status, reason. Returns {venue: counters}."""
    by_origin = collections.defaultdict(list)
    for a in attempts:
        by_origin[a["origin"] or a["attempt_id"]].append(a)
    out = collections.defaultdict(lambda: {"origins": 0, "filled": 0, "lost": 0, "lost_attempts": 0,
                                           "lost_mints": set(), "lost_wallets": set(),
                                           "filled_mints": set(), "lost_reasons": collections.Counter()})
    for rows in by_origin.values():
        mint = rows[0]["mint"]
        v = out[venue_of_mint.get(mint, "unknown")]
        v["origins"] += 1
        gaps = [r for r in rows if is_route_gap(r["reason"])]
        if any(r["status"] == "filled" for r in rows):
            v["filled"] += 1
            v["filled_mints"].add(mint)
        elif gaps:
            v["lost"] += 1
            v["lost_attempts"] += len(gaps)
            v["lost_mints"].add(mint)
            v["lost_wallets"].add(rows[0]["handle"])
            v["lost_reasons"][gaps[-1]["reason"][:90]] += 1
    return out


def load(since):
    with contextlib.closing(sqlite3.connect(LEDGER, uri=True, timeout=10)) as c:
        if c.execute("SELECT count(*) FROM memecoin_positions WHERE execution_mode='real' AND is_open=1 "
                     "AND network_id='solana' AND COALESCE(tracking_paused_at,'')=''").fetchone()[0]:
            raise SystemExit("a real Solana position is open: no research-key reads (standing rule)")
        attempts, pair_of_mint = [], {}
        for aid, origin, mint, handle, status, reason, cj in c.execute(
                "SELECT attempt_id, origin_event_id, mint, source_handle, terminal_status, terminal_reason, "
                "classification_json FROM memecoin_entry_attempts WHERE network_id='solana' "
                "AND decision_started_at >= ? ORDER BY decision_started_at", (since,)):
            attempts.append({"attempt_id": aid, "origin": origin, "mint": mint, "handle": handle,
                             "status": status, "reason": reason})
            pair = ((json.loads(cj).get("facts") or {}) if cj else {}).get("liquidity_pair_address")
            if pair:
                pair_of_mint[mint] = pair          # latest wins (ordered by decision time)
    return attempts, pair_of_mint


def venues(attempts, pair_of_mint):
    import dump_forensics as dfx
    import funded_exit_truth as fet
    pairs = sorted(set(pair_of_mint.values()))
    owner = {}
    for i in range(0, len(pairs), 100):
        chunk = pairs[i:i + 100]
        res = fet.rpc("getMultipleAccounts", [chunk, {"encoding": "base64",
                                                       "dataSlice": {"offset": 0, "length": 0}}])
        for addr, acc in zip(chunk, res["value"]):
            owner[addr] = acc["owner"] if acc else "closed_account"
    venue = {m: owner.get(p, "unknown") for m, p in pair_of_mint.items()}
    # Coins lost on the route with no pair address: the raybot origin tx's non-infra programs.
    for a in attempts:
        m, o = a["mint"], a["origin"] or ""
        if m in venue or not is_route_gap(a["reason"]) or not o.startswith("swap:raybot:"):
            continue
        tx = dfx.get_tx(o.split(":")[2])
        if not tx:
            venue[m] = "unknown_null_receipt"
            continue
        loaded = tx["meta"].get("loadedAddresses") or {}
        keys = tx["transaction"]["message"]["accountKeys"] + loaded.get("writable", []) + loaded.get("readonly", [])
        progs = {keys[ix["programIdIndex"]] for ix in tx["transaction"]["message"]["instructions"]}
        progs |= {keys[ix["programIdIndex"]] for inner in tx["meta"].get("innerInstructions") or []
                  for ix in inner["instructions"]}
        progs = sorted(p for p in progs if p not in INFRA and p in LABELS) or ["unlabelled_tx_programs"]
        venue[m] = "tx:" + "+".join(progs)
    return venue, fet.RATE_429[0]


def report(out):
    def label(v):
        core = v[3:] if v.startswith("tx:") else v
        return "%s (%s)" % (v, "+".join(LABELS.get(p, "?") for p in core.split("+")))
    rows = sorted(out.items(), key=lambda kv: (-kv[1]["lost"], -kv[1]["origins"]))
    print(f"{'venue owner program':<70}{'origins':>8}{'filled':>7}{'lost':>5}{'lost_att':>9}"
          f"{'lost_mints':>11}{'wallets':>8}")
    for v, s in rows:
        print(f"{label(v)[:69]:<70}{s['origins']:>8}{s['filled']:>7}{s['lost']:>5}{s['lost_attempts']:>9}"
              f"{len(s['lost_mints']):>11}{len(s['lost_wallets']):>8}")
    for v, s in rows:
        if s["lost"]:
            print(f"\n{label(v)}: lost mints {sorted(m[:8] for m in s['lost_mints'])} "
                  f"wallets {sorted(s['lost_wallets'])}")
            for reason, n in s["lost_reasons"].most_common():
                print(f"  {n} x {reason}")


def selftest():
    A = lambda o, m, st, rs, h="w": {"attempt_id": o + st, "origin": o, "mint": m, "handle": h,  # noqa: E731
                                     "status": st, "reason": rs}
    atts = [A("o1", "M1", "aborted", "no quote"), A("o1", "M1", "filled", "filled"),       # retried into a fill
            A("o2", "M2", "aborted", "no quote (no_route: jupiter 400 TOKEN_NOT_TRADABLE)"),
            A("o2", "M2", "aborted", "no quote"),                                           # one origin, two tries
            A("o3", "M3", "refused", "chase cap"),
            A("o4", "M4", "aborted", "route build refused: swap build returned nothing", "x")]
    out = tally(atts, {"M1": DBC, "M2": DBC, "M3": DBC})
    d, u = out[DBC], out["unknown"]
    assert (d["origins"], d["filled"], d["lost"], d["lost_attempts"]) == (3, 1, 1, 2), d
    assert d["lost_mints"] == {"M2"} and u["lost"] == 1 and u["lost_wallets"] == {"x"}, (d, u)
    assert not is_route_gap("route quote is 2x the chart mid") and not is_route_gap(None)
    print("selftest ok")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-13")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
    else:
        attempts, pair_of_mint = load(args.since)
        venue, n429 = venues(attempts, pair_of_mint)
        print(f"attempts {len(attempts)}  coins with a venue {len(venue)}  429s {n429}")
        report(tally(attempts, venue))
