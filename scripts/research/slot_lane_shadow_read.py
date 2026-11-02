"""The slot lane's venue shadow read: would the CPMM / LaunchLab shadow builds have landed?

Reads the runner log's per-event shadow lines (`[memecoin] slot lane cpmm shadow ...` and
`... launchlab shadow ...`, written by each venue's ShadowVerifier after it simulated the build
once, never sent) from --since on, and prints per venue: events, builds, refusal reasons, the
simulations that ran, how many would have landed (sim_ok: no error at the simulated slot with our
own min_out), our predicted gross output against the simulated one, and the slot gap S -> sim.
Since 2026-10-06 a LIVE-lane outcome offered to the same verifier prints `... cpmm live ...` /
`... launchlab live ...` instead (memecoin_slot_lane.outcome_mode); MARK matches only `shadow`, so
those lines, which were never shadow builds, stay out of this read.

A simulation runs a few hundred ms after the build, at a slot after S, so sim_ok is a landing
test against a LATER pool than our S+1 send would meet: it errs toward refusing. `exact` counts
sim_ok events whose simulated gross equals our prediction to the unit, and `exact_when_still`
the same among events whose fresh reserve read found the pool unchanged since S (CPMM only).

AGAINST THE ACTUAL FILL (Alpha, pre-registered 2026-09-27 before any data): for every
would-have-landed build, the same source buy's funded fill on whichever lane filled it (J7, the
watcher, the routed row), joined by source signature (sig=) on the ledger, read-only: the lane's
receipt -> built ms next to the fill's receipt -> fill ms, and with --slots the fill's landing
slot minus S (getSignatureStatuses on the research key, only while the Solana book is flat). A fill
at or before S+2 is within one slot of the lane's best case (S+1): CPMM live would add speed there
only on the margin. A landed build with no fill is where live would have added a fill.

Usage: python3 scripts/research/slot_lane_shadow_read.py --since 2026-09-27T23:16:00 [--slots]
       python3 scripts/research/slot_lane_shadow_read.py --selftest
"""
import argparse
import collections
import re
import sys

LOG = "logs/memecoin_runner.log"
MARK = re.compile(r"^(\S+) \[memecoin\] slot lane (cpmm|launchlab) shadow (\S+) (\S+) pool=(\S+) "
                  r"S=(\d+) -> (.*?) age_ms=")
FIELD = re.compile(r"(\w+)=(\S+)")
SLIPPAGE = {"cpmm": "6005", "launchlab": "6004"}


def parse(line):
    m = MARK.match(line)
    if not m:
        return None
    ts, venue, handle, mint, pool, slot, reason = m.groups()
    tail = line[m.end() - len("age_ms="):]
    f = {k: (None if v == "None" else v) for k, v in FIELD.findall(tail)}
    num = lambda k, cast=int: cast(f[k]) if f.get(k) is not None else None
    return {"ts": ts, "venue": venue, "handle": handle, "mint": mint, "pool": pool, "S": int(slot),
            "reason": reason, "built": reason.startswith("built"),
            "age_ms": num("age_ms", float), "build_ms": num("build_ms", float),
            "gross": num("gross"), "sim_ok": {"True": True, "False": False}.get(f.get("sim_ok")),
            "sim_out": num("sim_out"), "sim_err": f.get("sim_err"), "sim_slot": num("sim_slot"),
            "in_delta": num("reserve_in_delta"), "out_delta": num("reserve_out_delta"),
            "sig": f.get("sig"), "recv": f.get("recv"), "quote": f.get("quote"),
            "leg_ms": num("leg_ms", float), "residue_usd": num("residue_usd", float)}


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


def report(rows):
    out = []
    for venue in ("cpmm", "launchlab"):
        rs = [r for r in rows if r["venue"] == venue]
        if not rs:
            out.append("%s: no shadow lines" % venue)
            continue
        built = [r for r in rs if r["built"]]
        sims = [r for r in built if r["sim_ok"] is not None]
        ok = [r for r in sims if r["sim_ok"]]
        exact = [r for r in ok if r["gross"] is not None and r["sim_out"] == r["gross"]]
        still = [r for r in ok if r["in_delta"] == 0 and r["out_delta"] == 0]
        slip = [r for r in sims if not r["sim_ok"] and SLIPPAGE[venue] in (r["sim_err"] or "")]
        ratio = [r["sim_out"] / r["gross"] - 1 for r in ok if r["gross"]]
        gap = [r["sim_slot"] - r["S"] for r in sims if r["sim_slot"] is not None]
        refusals = collections.Counter(r["reason"] for r in rs if not r["built"])
        out.append("%s: %d events, %d built, %d simulated, %d would have landed (%s), %d failed on "
                   "slippage, %d on anything else" % (
                       venue, len(rs), len(built), len(sims), len(ok),
                       "%.0f%%" % (100.0 * len(ok) / len(sims)) if sims else "-", len(slip),
                       len(sims) - len(ok) - len(slip)))
        out.append("  predicted gross exact on %d of %d landed; exact when the pool was still: %d of %d"
                   % (len(exact), len(ok), sum(1 for r in still if r in exact), len(still)))
        if ratio:
            out.append("  sim/predicted - 1: p10 %+.4f p50 %+.4f p90 %+.4f" % (
                pct(ratio, 0.1), pct(ratio, 0.5), pct(ratio, 0.9)))
        if gap:
            out.append("  sim slot - S: p50 %d p90 %d" % (pct(gap, 0.5), pct(gap, 0.9)))
        ages = [r["age_ms"] for r in built if r["age_ms"] is not None]
        builds = [r["build_ms"] for r in built if r["build_ms"] is not None]
        if ages:
            out.append("  signal age ms p50 %.1f p90 %.1f; build ms p50 %.1f p90 %.1f" % (
                pct(ages, 0.5), pct(ages, 0.9), pct(builds, 0.5) or 0.0, pct(builds, 0.9) or 0.0))
        for reason, n in refusals.most_common(8):
            out.append("  refused %3d  %s" % (n, reason[:100]))
        unsupported = collections.Counter(r["quote"] for r in rs if r["reason"].endswith("_quote_not_supported"))
        if unsupported:                       # which quote assets an on-demand leg would open
            out.append("  unsupported quotes: %s" % ", ".join("%s %d" % kv for kv in unsupported.most_common(8)))
        legs = [r for r in rs if r["leg_ms"] is not None]
        if legs:                              # LaunchLab phase (b): the on-demand quote leg
            out.append("  on-demand legs: %d; residue left in the quote asset $%.2f total, per build "
                       "p50 $%.4f" % (len(legs), sum(r["residue_usd"] or 0.0 for r in legs),
                                      pct([r["residue_usd"] for r in legs if r["residue_usd"] is not None], 0.5) or 0.0))
            by_quote = collections.defaultdict(list)
            for r in legs:
                by_quote[r["quote"]].append(r["leg_ms"])
            for quote, ms in sorted(by_quote.items(), key=lambda kv: -len(kv[1])):
                out.append("    quote %s  n=%d  leg ms p50 %.0f p90 %.0f" % (quote, len(ms), pct(ms, 0.5), pct(ms, 0.9)))
    return "\n".join(out)


def _ms(later, earlier):
    from datetime import datetime
    try:
        a = datetime.fromisoformat(str(later).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(earlier).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (a - b).total_seconds() * 1000.0


def join(rows, buys, slots=None):
    """Per would-have-landed build: the first funded fill of the same source buy, or None. Pure.
    `buys` is venue_census.universe's {sig: {"attempts": [...]}}; `slots` maps a fill's tx_sig to
    its landing slot (None = not read)."""
    out = []
    for r in rows:
        if not (r["built"] and r["sim_ok"] and r["sig"]):
            continue
        attempts = (buys.get(r["sig"]) or {}).get("attempts") or []
        fill = next((a for a in attempts if a["status"] == "filled" and a.get("fill_at")), None)
        lane_ms = ((r["age_ms"] or 0.0) + (r["build_ms"] or 0.0)) if r["age_ms"] is not None else None
        slot = (slots or {}).get(fill.get("tx_sig")) if fill else None
        out.append({"venue": r["venue"], "sig": r["sig"], "lane_ms": lane_ms, "S": r["S"],
                    "fill_lane": fill["lane"] if fill else None,
                    "fill_ms": _ms(fill["fill_at"], r["recv"]) if fill else None,
                    "fill_slot_minus_s": (slot - r["S"]) if slot is not None else None,
                    "attempted": bool(attempts)})
    return out


def join_report(joined, slots_read):
    lines = []
    for venue in ("cpmm", "launchlab"):
        js = [j for j in joined if j["venue"] == venue]
        if not js:
            continue
        filled = [j for j in js if j["fill_lane"]]
        lines.append("%s vs the actual fill: %d landed builds; %d filled by a funded lane %s; %d not "
                     "filled (%d attempted)" % (
                         venue, len(js), len(filled),
                         dict(collections.Counter(j["fill_lane"] for j in filled)),
                         len(js) - len(filled), sum(1 for j in js if not j["fill_lane"] and j["attempted"])))
        pairs = [(j["lane_ms"], j["fill_ms"]) for j in filled if j["lane_ms"] is not None and j["fill_ms"] is not None]
        if pairs:
            lines.append("  same buys, receipt -> lane built p50 %.0f ms / p90 %.0f; receipt -> actual fill "
                         "p50 %.0f ms / p90 %.0f" % (pct([a for a, _ in pairs], 0.5), pct([a for a, _ in pairs], 0.9),
                                                    pct([b for _, b in pairs], 0.5), pct([b for _, b in pairs], 0.9)))
        gaps = [j["fill_slot_minus_s"] for j in filled if j["fill_slot_minus_s"] is not None]
        if gaps:
            lines.append("  actual fill slot - S p50 %d p90 %d; %d of %d at or before S+2 (within one slot "
                         "of the lane's S+1)" % (pct(gaps, 0.5), pct(gaps, 0.9), sum(1 for g in gaps if g <= 2),
                                                 len(gaps)))
        elif filled:
            lines.append("  fill slots: %s" % ("none resolved" if slots_read else
                                                "censored (not read: pass --slots on a flat book)"))
    return "\n".join(lines)


def fill_slots(ledger, tx_sigs):
    """{tx_sig: landing slot} on the research key, or None when the Solana book is not flat."""
    import venue_census as vc
    import funded_exit_truth as fet
    out = {}
    tx_sigs = sorted(s for s in tx_sigs if s)
    for i in range(0, len(tx_sigs), 256):
        if vc.book_open(ledger):
            return None
        res = fet.rpc("getSignatureStatuses", [tx_sigs[i:i + 256], {"searchTransactionHistory": True}])
        for sig, st in zip(tx_sigs[i:i + 256], (res or {}).get("value") or []):
            if st and st.get("slot") is not None:
                out[sig] = int(st["slot"])
    return out


def last_per_sig(rows):
    """One row per venue and source signature, the LAST line: a pool miss logs pool_unknown_learning
    and then the retry's line for the same buy. Rows with no sig= (older lines) stay as they are."""
    keep = {}
    for i, row in enumerate(rows):
        keep[(row["venue"], row["sig"]) if row["sig"] else i] = row
    return list(keep.values())


def read(path, since):
    rows = []
    with open(path, errors="replace") as fh:
        for line in fh:
            if " shadow " not in line or "slot lane" not in line:
                continue
            row = parse(line.rstrip("\n"))
            if row and row["ts"] >= since:
                rows.append(row)
    return last_per_sig(rows)


def selftest():
    lines = [
        "2026-09-27T23:20:00+00:00 [memecoin] slot lane cpmm shadow alice AbCdEfGh pool=PoolAAAA "
        "S=100 -> built exp=90 min=80 age_ms=12.0 build_ms=0.3 exp=90 min=80 gross=95 "
        "reserve_in_delta=0 reserve_out_delta=0 fresh_slot=101 sim_ok=True sim_out=95 sim_err=None "
        "sim_slot=102",
        "2026-09-27T23:21:00+00:00 [memecoin] slot lane cpmm shadow bob AbCdEfGh pool=PoolAAAA "
        "S=200 -> built exp=90 min=80 age_ms=20.0 build_ms=0.4 exp=90 min=80 gross=95 "
        "reserve_in_delta=5 reserve_out_delta=-7 fresh_slot=201 sim_ok=False sim_out=None "
        'sim_err={"InstructionError":[5,{"Custom":6005}]} sim_slot=203',
        "2026-09-27T23:22:00+00:00 [memecoin] slot lane launchlab shadow carol Mint1234 pool=PoolBBBB "
        "S=300 -> pool_unknown age_ms=None build_ms=None exp=None min=None gross=None sim_ok=None "
        "sim_out=None sim_err=None sim_slot=None sim_minus_gross=None",
        "2026-09-27T23:10:00+00:00 [memecoin] slot lane cpmm shadow early x pool=P S=1 -> built "
        "age_ms=1 build_ms=1 exp=1 min=1 gross=1 sim_ok=True sim_out=1 sim_err=None sim_slot=2",
    ]
    rows = [r for r in map(parse, lines) if r and r["ts"] >= "2026-09-27T23:16"]
    assert [r["venue"] for r in rows] == ["cpmm", "cpmm", "launchlab"], rows
    assert rows[1]["sim_ok"] is False and "6005" in rows[1]["sim_err"] and rows[1]["in_delta"] == 5
    text = report(rows)
    assert "cpmm: 2 events, 2 built, 2 simulated, 1 would have landed (50%), 1 failed on slippage" in text, text
    assert "exact on 1 of 1 landed; exact when the pool was still: 1 of 1" in text, text
    assert "launchlab: 1 events, 0 built" in text and "pool_unknown" in text, text
    landed = dict(rows[0], sig="SrcSig1", recv="2026-09-27T23:20:00+00:00")
    also = dict(rows[0], sig="SrcSig2", recv="2026-09-27T23:20:00+00:00")
    buys = {"SrcSig1": {"attempts": [{"status": "rejected", "fill_at": None, "lane": "j7_wallets"},
                                     {"status": "filled", "fill_at": "2026-09-27T23:20:02.000+00:00",
                                      "lane": "j7_wallets", "tx_sig": "OurTx1"}]},
            "SrcSig2": {"attempts": [{"status": "rejected", "fill_at": None, "lane": "onchain_watch"}]}}
    joined = join([landed, also, rows[1]], buys, {"OurTx1": 104})
    assert [(j["fill_lane"], j["fill_ms"], j["fill_slot_minus_s"], j["attempted"]) for j in joined] == [
        ("j7_wallets", 2000.0, 4, True), (None, None, None, True)], joined
    assert joined[0]["lane_ms"] == 12.3
    text = join_report(joined, True)
    assert "2 landed builds; 1 filled by a funded lane {'j7_wallets': 1}; 1 not filled (1 attempted)" in text, text
    assert "0 of 1 at or before S+2" in text, text
    assert "censored" in join_report(join([landed], buys, None), False)
    hook = parse("2026-09-27T23:41:00+00:00 [memecoin] slot lane cpmm shadow eve Mint9999 pool=PoolDDDD "
                 "S=500 -> slot_lane_cpmm_coin_extension_transferHook age_ms=1.0 build_ms=None exp=None "
                 "min=None gross=None reserve_in_delta=None reserve_out_delta=None fresh_slot=None "
                 "sim_ok=None sim_out=None sim_err=None sim_slot=None sig=S9 recv=t")
    assert "refused   1  slot_lane_cpmm_coin_extension_transferHook" in report([hook]), report([hook])
    exotic = parse("2026-09-27T23:42:00+00:00 [memecoin] slot lane cpmm shadow ResellCalendar AjxqTiS6 "
                   "pool=7ckTVPGF S=451137892 -> slot_lane_cpmm_quote_not_supported age_ms=None build_ms=None "
                   "exp=None min=None gross=None reserve_in_delta=None reserve_out_delta=None fresh_slot=None "
                   "sim_ok=None sim_out=None sim_err=None sim_slot=None quote=Pren1FvF sig=S8 recv=t")
    assert "unsupported quotes: Pren1FvF 1" in report([exotic]), report([exotic])
    leg_line = ("2026-09-27T23:40:00+00:00 [memecoin] slot lane launchlab shadow dan Mint5678 pool=PoolCCCC "
                "S=400 -> built exp=5 min=4 age_ms=3.0 build_ms=420.0 exp=5 min=4 gross=6 sim_ok=True "
                "sim_out=6 sim_err=None sim_slot=402 sim_minus_gross=0 quote=Xsc9qvGR leg_ms=410.5 "
                "residue_usd=0.3750 sig=SrcSig3 recv=2026-09-27T23:39:59+00:00")
    row = parse(leg_line)
    assert (row["quote"], row["leg_ms"], row["residue_usd"]) == ("Xsc9qvGR", 410.5, 0.375), row
    assert "quote Xsc9qvGR  n=1  leg ms p50 410 p90 410" in report([row]), report([row])
    # the live pair from 2026-09-28T02:02:46Z: a pool miss, then the retry, one source buy
    pair = [parse("2026-09-28T02:02:46.641925+00:00 [memecoin] slot lane launchlab shadow starcatcher444 "
                  "3XUwb3QQ pool=Dn3JraZc S=451178902 -> %s age_ms=None build_ms=None exp=None min=None "
                  "gross=None sim_ok=None sim_out=None sim_err=None sim_slot=None sim_minus_gross=None "
                  "quote=%s leg_ms=None residue_usd=None sig=4U66QJee recv=t" % (reason, quote))
            for reason, quote in (("pool_unknown_learning", "None"),
                                  ("slot_lane_launchlab_quote_not_supported", "USD1ttGY"))]
    kept = last_per_sig(pair + [dict(pair[0], venue="cpmm"), dict(pair[0], sig=None), dict(pair[0], sig=None)])
    assert [(r["venue"], r["reason"]) for r in kept] == [
        ("launchlab", "slot_lane_launchlab_quote_not_supported"), ("cpmm", "pool_unknown_learning"),
        ("launchlab", "pool_unknown_learning"), ("launchlab", "pool_unknown_learning")], kept
    assert "launchlab: 1 events" in report(last_per_sig(pair)), report(last_per_sig(pair))
    assert "unsupported quotes: USD1ttGY 1" in report(last_per_sig(pair)), report(last_per_sig(pair))
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="ISO UTC; lines at or after it")
    ap.add_argument("--log", default=LOG)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--slots", action="store_true",
                    help="read the fills' landing slots on the research key (flat Solana book only)")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.since:
        sys.exit("--since is required (the deploy that carried the shadow simulation)")
    rows = read(args.log, args.since)
    print(report(rows))
    if any(r["sim_ok"] for r in rows):
        sys.path[:0] = [str(__import__("pathlib").Path(__file__).resolve().parent)]
        from dotenv import load_dotenv
        load_dotenv(__import__("pathlib").Path(__file__).resolve().parents[2] / ".env")
        import venue_census as vc
        ledger = vc.STATE_ROOT + "/sniper_tracker.db"
        buys, _ = vc.universe(ledger, args.since)
        slots = None
        if args.slots:
            tx = {a.get("tx_sig") for r in rows if r["sig"] in buys
                  for a in buys[r["sig"]]["attempts"] if a["status"] == "filled"}
            slots = fill_slots(ledger, tx)
            if slots is None:
                print("fill slots: not read, the funded Solana book is not flat")
        print(join_report(join(rows, buys, slots), slots is not None))


if __name__ == "__main__":
    main()
