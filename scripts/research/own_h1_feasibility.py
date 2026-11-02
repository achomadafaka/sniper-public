#!/usr/bin/env python3
"""Can we build our own 60-minute volume per coin (DexScreener E2 replacement)? Completeness, latency.

Alpha, 2026-09-28 (operator: "no dex anywhere"; E2, the volume gate, is blocked on an own h1).

  solana   A sample of recent Solana attempts that reached the volume gate with a DexScreener
           volume_h1 in facts. Per coin, the mint's succeeded transactions in [T - 3600 s, T), newest
           first, from Helius getTransactionsForAddress (research key, flat-gated per coin), timed
           per page. Each is decoded by dump_forensics.trade_of (one trade per transaction; USD =
           SOL at the Kraken 5-minute close for T, or the USDC leg). Reports: pages to cover the
           hour and the last 5 minutes, wall time for each, whether the hour completed inside the
           page cap, and own/DexScreener for h1 and m5. T is decision_started_at (corrected clock);
           the window is chain blockTime, whole seconds, so a boundary trade can fall either side.
  evm      The on-chain Swap tape (data/memecoin_onchain_volume.db, pruned at 3,600 s; 8453 decodes
           v3_swap only) summed per coin over the last hour, against DexScreener's h1 for the same
           coins now (one /tokens/v1 batch call), for the coins of recent 8453 attempts.

    python3 scripts/research/own_h1_feasibility.py solana --n 30 --out logs/acceptance/own_h1_solana.jsonl
    python3 scripts/research/own_h1_feasibility.py report --out logs/acceptance/own_h1_solana.jsonl
    python3 scripts/research/own_h1_feasibility.py evm
    python3 scripts/research/own_h1_feasibility.py --selftest
"""
import argparse
import contextlib
import json
import random
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "scripts" / "research")]
LEDGER = str(REPO / "sniper_tracker.db")
TAPE = str(REPO / "data" / "memecoin_onchain_volume.db")
H1_S, M5_S, PAGE, PAGES = 3600, 300, 100, 40


def ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def ro(path=LEDGER):
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=30)


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * (len(xs) - 1) + 0.5))] if xs else None


def sample(n, since, seed=20260928):
    """One attempt per mint: Solana, reached volume_gate, DexScreener volume_h1 in facts."""
    out = {}
    with contextlib.closing(ro()) as c:
        for aid, mint, at, cj, tj in c.execute(
                "SELECT attempt_id, mint, decision_started_at, classification_json, trace_json "
                "FROM memecoin_entry_attempts WHERE network_id='solana' AND decision_started_at>=? "
                "AND classification_json IS NOT NULL", (since,)):
            f = (json.loads(cj).get("facts") or {})
            if f.get("volume_h1") is None or '"volume_gate"' not in (tj or ""):
                continue
            out.setdefault(mint, {"attempt_id": aid, "mint": mint, "T": at, "dex_h1": f["volume_h1"],
                                  "dex_m5": f.get("volume_m5"), "mcap": f.get("effective_mcap")})
    rows = sorted(out.values(), key=lambda r: r["T"])
    random.Random(seed).shuffle(rows)
    return rows[:n]


def sol_usd_series(since_epoch):
    """[(epoch, close)] Kraken SOLUSD 5-minute candles (public, one call)."""
    url = "https://api.kraken.com/0/public/OHLC?pair=SOLUSD&interval=5&since=%d" % int(since_epoch)
    body = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "curl/8.5.0"}),
                                             timeout=20).read())
    res = body["result"]
    rows = next(v for k, v in res.items() if k != "last")
    return [(float(r[0]), float(r[4])) for r in rows]


def price_at(series, t):
    prior = [p for e, p in series if e <= t]
    return prior[-1] if prior else None


def summarize(pages, T, sol_usd, mint, decode):
    """pages: [(elapsed_ms, [tx...])] newest first. Pure except `decode(tx, mint, sol_usd)`."""
    h1 = m5 = 0.0
    trades = txs = 0
    m5_ms = h1_ms = None
    for ms, batch in pages:
        for tx in batch:
            bt = tx.get("blockTime")
            if bt is None or not (T - H1_S <= bt < T):
                continue
            txs += 1
            t = decode(tx, mint, sol_usd)
            if t and t.get("sol"):
                usd = t["sol"] * sol_usd
                trades += 1
                h1 += usd
                if bt >= T - M5_S:
                    m5 += usd
        oldest = min((tx.get("blockTime") for tx in batch if tx.get("blockTime") is not None), default=None)
        if m5_ms is None and (oldest is None or oldest < T - M5_S):
            m5_ms = ms
        if h1_ms is None and (oldest is None or oldest < T - H1_S):
            h1_ms = ms
    return {"own_h1": h1, "own_m5": m5, "trades": trades, "txs": txs, "m5_ms": m5_ms, "h1_ms": h1_ms}


def run_solana(args):
    import dump_forensics as dfx
    import funded_exit_truth as fet
    import slot_lane_send_monitor as mon
    from slot_minout_grading import wait_flat
    from sniper.execution.solana_rpc_pool import key_id
    print("research key", key_id(mon.rpc_url()), flush=True)
    rows = sample(args.n, args.since)
    series = sol_usd_series(min(ts(r["T"]) for r in rows) - 600)
    out = Path(args.out)
    done = {json.loads(l)["mint"] for l in out.read_text().splitlines() if l.strip()} if out.exists() else set()
    with out.open("a") as fh:
        for r in rows:
            if r["mint"] in done:
                continue
            wait_flat(ro)
            T = int(ts(r["T"]))
            sol = price_at(series, T)
            # Latency = the calls alone: fet.rpc sleeps SPACING_S after every response (key etiquette),
            # which a live pager would not, so that sleep is taken back out of each page.
            pages, token, cum, complete = [], None, 0.0, False
            for _ in range(PAGES):
                opts = {"transactionDetails": "full", "sortOrder": "desc", "limit": PAGE, "encoding": "json",
                        "maxSupportedTransactionVersion": 1, "commitment": "confirmed",
                        "filters": {"status": "succeeded", "blockTime": {"gte": T - H1_S, "lte": T - 1}}}
                if token:
                    opts["paginationToken"] = token
                t_call = time.monotonic()
                try:
                    res = fet.rpc("getTransactionsForAddress", [r["mint"], opts]) or {}
                except fet.Stop as exc:
                    print("STOPPED:", exc, flush=True)
                    return
                except (TimeoutError, OSError) as exc:
                    print("page failed %s: %r" % (r["mint"][:8], exc), flush=True)
                    break
                data = res.get("data") or []
                cum += max(0.0, time.monotonic() - t_call - fet.SPACING_S)
                pages.append((cum * 1000.0, data))
                token = res.get("paginationToken")
                if not token or len(data) < PAGE:
                    complete = True
                    break
            s = summarize(pages, T, sol, r["mint"], dfx.trade_of)
            rec = {**r, **s, "sol_usd": sol, "pages": len(pages), "complete": complete,
                   "wall_ms": pages[-1][0] if pages else None}
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            print("%s pages %2d complete %-5s wall %6.0f ms  m5 %s ms  own h1 $%.0f dex h1 $%.0f" % (
                r["mint"][:8], len(pages), complete, rec["wall_ms"] or 0, s["m5_ms"] and "%.0f" % s["m5_ms"],
                s["own_h1"], r["dex_h1"]), flush=True)
    print("research-key 429s %d" % fet.RATE_429[0], flush=True)


def report(args):
    recs = [json.loads(l) for l in Path(args.out).read_text().splitlines() if l.strip()]
    comp = [r for r in recs if r["complete"]]
    print("coins read %d; hour complete inside %d pages: %d" % (len(recs), PAGES, len(comp)))
    print("pages per hour (complete): p50 %s p90 %s max %s" % (
        pct([r["pages"] for r in comp], .5), pct([r["pages"] for r in comp], .9), max((r["pages"] for r in comp), default=None)))
    print("wall ms to the hour (complete): p50 %s p90 %s" % (
        pct([r["wall_ms"] for r in comp], .5), pct([r["wall_ms"] for r in comp], .9)))
    m5 = [r["m5_ms"] for r in recs if r["m5_ms"] is not None]
    print("wall ms to the last 5 min: p50 %s p90 %s (n=%d)" % (pct(m5, .5), pct(m5, .9), len(m5)))
    for name, own, dex in (("h1", "own_h1", "dex_h1"), ("m5", "own_m5", "dex_m5")):
        rs = [r[own] / r[dex] for r in comp if r.get(dex)]
        if rs:
            w = sum(1 for x in rs if 0.8 <= x <= 1.25)
            print("%s own/DexScreener (complete, dex > 0): n=%d p10 %.2f p50 %.2f p90 %.2f; within 0.8-1.25: %d" % (
                name, len(rs), pct(rs, .1), pct(rs, .5), pct(rs, .9), w))
    print("decoded trades / txs in window (complete): p50 %.2f" % pct(
        [r["trades"] / r["txs"] for r in comp if r["txs"]], .5))


def run_evm(args):
    with contextlib.closing(ro()) as c:
        mints = sorted({str(m).lower() for (m,) in c.execute(
            "SELECT DISTINCT mint FROM memecoin_entry_attempts WHERE network_id='8453' AND decision_started_at>=?",
            (args.since,))})[:30]
    with contextlib.closing(ro(TAPE)) as t:
        lo, hi = t.execute("SELECT MIN(observed_at), MAX(observed_at) FROM pool_ticks WHERE chain_id='8453'").fetchone()
        pools = {}
        for pool, mint in t.execute("SELECT pool, mint FROM onchain_pool_protocols WHERE chain_id='8453'"):
            pools.setdefault(str(mint).lower(), set()).add(pool)
        ticks = {m: t.execute("SELECT COUNT(*) FROM pool_ticks WHERE chain_id='8453' AND pool IN (%s)" % ",".join(
            "?" * len(pools[m])), tuple(pools[m])).fetchone()[0] if pools.get(m) else 0 for m in mints}
    url = "https://api.dexscreener.com/tokens/v1/base/" + ",".join(mints)
    pairs = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "curl/8.5.0"}),
                                              timeout=20).read())
    dex = {}
    for p in pairs:
        m = str((p.get("baseToken") or {}).get("address", "")).lower()
        dex.setdefault(m, []).append((p.get("dexId"), (p.get("volume") or {}).get("h1") or 0.0))
    print("8453 tape window %s .. %s; coins %d" % (lo, hi, len(mints)))
    for m in mints:
        d = dex.get(m, [])
        print("  %s tape pools %d ticks %d | DexScreener h1 $%.0f on %s" % (
            m[:10], len(pools.get(m, ())), ticks[m], sum(v for _d, v in d), sorted({x for x, _v in d})))


def selftest():
    T = 10_000
    dec = lambda tx, mint, s: {"sol": tx["sol"]} if tx.get("sol") else None   # noqa: E731
    pages = [(120.0, [{"blockTime": T - 10, "sol": 1.0}, {"blockTime": T - 200, "sol": 2.0}]),
             (250.0, [{"blockTime": T - 400, "sol": 4.0}, {"blockTime": T - 3599}]),
             (400.0, [{"blockTime": T - 3601, "sol": 8.0}])]
    s = summarize(pages, T, 100.0, "M", dec)
    assert s["own_h1"] == 700.0 and s["own_m5"] == 300.0, s        # T-3601 is outside, T-400 outside m5
    assert s["m5_ms"] == 250.0 and s["h1_ms"] == 400.0, s          # coverage proven by the OLDEST row
    assert s["trades"] == 3 and s["txs"] == 4, s
    assert price_at([(0, 1.0), (300, 2.0)], 299) == 1.0 and price_at([(10, 1.0)], 5) is None
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("solana")
    s.add_argument("--n", type=int, default=30)
    s.add_argument("--since", default="2026-09-27T00:00:00")
    s.add_argument("--out", required=True)
    r = sub.add_parser("report")
    r.add_argument("--out", required=True)
    e = sub.add_parser("evm")
    e.add_argument("--since", default="2026-09-27T00:00:00")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    return {"solana": run_solana, "report": report, "evm": run_evm}[a.cmd](a)


if __name__ == "__main__":
    main()
