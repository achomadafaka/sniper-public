#!/usr/bin/env python3
"""Crowd + dump scoreboard for every armed Solana wallet: who draws copiers, who sells into them.

Operator order 2026-09-27 (via Alpha). For each armed Solana wallet's registry owner, over its last
~20 on-chain buys, from the chain only (research key; never our marks):
  crowd   other buys on the mint after the source's buy, in slots S..S+2 (count, SOL); our own
          funded entries are excluded, so the number describes the market, not us;
  dump    the source sold >= 30% of its post-buy balance of the mint within 120 s (from its own
          token account for the mint, exact);
  ret60   the first decodable trade price >= 60 s after the buy (within 90 s) against the source's
          average fill, and against its post-trade spot (the S+1 entry proxy).
Buys come from our watcher's detections (onchain_wallet_detections) first, then from the owner's
own succeeded transactions. The owner address list is mostly noise for some wallets (notdecu: 78%
failed transactions by other signers), so the owner scan filters to succeeded transactions that
changed the owner's token balances.

Research key only (funded_exit_truth.rpc: the first MEMECOIN_RESEARCH_HELIUS_RPC_URLS entry, funded
overlap refused, bounded 429 backoff, stop on quota). Uses Helius getTransactionsForAddress (slot,
blockTime and status filters). One JSON line per wallet; resumes by source_user_id.

    python3 scripts/research/wallet_crowd_dump_scoreboard.py run --out logs/wallet_scoreboard_20260927.jsonl
    python3 scripts/research/wallet_crowd_dump_scoreboard.py report --out logs/wallet_scoreboard_20260927.jsonl
    python3 scripts/research/wallet_crowd_dump_scoreboard.py --selftest
"""
import argparse
import contextlib
import json
import math
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "research"))
import funded_exit_truth as fet  # noqa: E402
import dump_forensics as dfx  # noqa: E402

fet.SPACING_S = 0.2          # e1f95763 (paid, 2026-09-27 08:28Z); was 0.5 on the shared dea05c94
CALLS = [0]
_rpc = fet.rpc


def _counted_rpc(method, params):
    CALLS[0] += 1
    return _rpc(method, params)


fet.rpc = _counted_rpc          # dfx.get_tx calls fet.rpc through the module, so it is counted too
LEDGER = f"file:{REPO / 'sniper_tracker.db'}?mode=ro"
QUOTES = frozenset({fet.WSOL, fet.USDC, dfx.WETH})
BUYS = 20
DUMP_SHARE, DUMP_WINDOW_S = 0.30, 120
RET_AT_S, RET_LATEST_S = 60, 90
CROWD_SLOTS = 2
DUST_SOL = 0.1             # ~$12; excluded from summaries (untaxxable re-bought 0.0099 SOL after dumping)
# Disarmed 2026-09-27 as source dumpers: the calibration rows.
CALIBRATION = ("pump:2T5NgDDidkvhJQg8AHDi74uCFwgp25pYFMRZXBaCUNBH",   # untaxxable
               "pump:DNsh1UfJdxmze6T6GV9QK5SoFm7HsM5TRNxVuwVgo8Zj",   # hashbergers
               "pump:4DdrfiDHpmx55i4SPssxVzS9ZaKLb8qr45NKY9Er9nNh")   # TheMisterTurtle


def gtfa(address, filters, order="asc", limit=100, pages=1, token_accounts=None):
    """Full transactions for `address` (Helius getTransactionsForAddress), succeeded only."""
    out, token = [], None
    for _ in range(pages):
        opts = {"transactionDetails": "full", "sortOrder": order, "limit": limit, "encoding": "json",
                "maxSupportedTransactionVersion": 1, "filters": {"status": "succeeded", **filters}}
        if token_accounts:
            opts["filters"]["tokenAccounts"] = token_accounts
        if token:
            opts["paginationToken"] = token
        res = None
        for _attempt in range(3):   # a full-detail page can outrun the 30 s read (08:25Z): halve it
            try:
                res = fet.rpc("getTransactionsForAddress", [address, opts]) or {}
                break
            except (TimeoutError, OSError) as exc:
                print(f"gtfa timeout on {address[:8]} limit={opts['limit']}: {exc}", file=sys.stderr, flush=True)
                opts["limit"] = max(10, opts["limit"] // 2)
        if res is None:
            return out, False
        out.extend(res.get("data") or [])
        token = res.get("paginationToken")
        if not token or len(res.get("data") or []) < opts["limit"]:
            return out, True
    return out, False


def owner_balance(tx, owner, mint):
    pre, post = dfx.owner_bag(tx, owner, mint)
    return pre, post


def buy_of(tx, owner, sol_usd):
    """The owner's buy in this tx as {mint, bought, bag, fill, spot, sol} or None. A buy is an increase
    in the owner's balance of a non-quote mint in a tx whose decoded trade on that mint is a buy."""
    if not tx or (tx.get("meta") or {}).get("err") is not None:
        return None
    mints = {b.get("mint") for b in tx["meta"].get("postTokenBalances") or [] if b.get("owner") == owner}
    for mint in sorted(m for m in mints if m not in QUOTES):
        pre, post = owner_balance(tx, owner, mint)
        if post <= pre:
            continue
        t = dfx.trade_of(tx, mint, sol_usd)
        if not t or t["side"] != "buy" or not t["tokens"]:
            continue
        fill = t["sol"] * sol_usd / t["tokens"] if t["kind"] == "curve" else t["price"]
        return {"mint": mint, "bought": post - pre, "bag": post, "fill": fill, "spot": t["price"],
                "sol": round(t["sol"], 4), "kind": t["kind"]}
    return None


def wallet_buys(uid, owner, sol_usd, want=BUYS):
    """Newest `want` buys: watcher detections first, then the owner's own succeeded txs."""
    with contextlib.closing(sqlite3.connect(LEDGER, uri=True, timeout=30)) as c:
        det = [r[0] for r in c.execute(
            "SELECT signature FROM onchain_wallet_detections WHERE source_user_id=? AND network_id='solana' "
            "ORDER BY id DESC LIMIT ?", (uid, want))]
    buys, seen, source = [], set(), {}
    for sig in det:
        tx = dfx.get_tx(sig)
        b = buy_of(tx, owner, sol_usd)
        if b:
            buys.append({"sig": sig, "slot": tx["slot"], "t": tx.get("blockTime"), "tx": tx, **b})
            seen.add(sig)
            source[sig] = "detection"
    if len(buys) < want:
        txs, _ = gtfa(owner, {}, order="desc", limit=50, pages=6, token_accounts="balanceChanged")
        for tx in txs:
            sig = tx["transaction"]["signatures"][0]
            if sig in seen:
                continue
            b = buy_of(tx, owner, sol_usd)
            if b:
                buys.append({"sig": sig, "slot": tx["slot"], "t": tx.get("blockTime"), "tx": tx, **b})
                seen.add(sig)
                source[sig] = "owner_scan"
    buys.sort(key=lambda b: (b["slot"], b["t"] or 0), reverse=True)
    dust = [b for b in buys if (b["sol"] or 0) < DUST_SOL]
    source["dust_skipped"] = len(dust)
    return [b for b in buys if (b["sol"] or 0) >= DUST_SOL][:want], source


def our_entry_sigs():
    with contextlib.closing(sqlite3.connect(LEDGER, uri=True, timeout=30)) as c:
        return {r[0] for r in c.execute(
            "SELECT f.tx_sig FROM memecoin_fills f JOIN memecoin_positions p ON p.id=f.position_id "
            "WHERE f.kind='entry' AND p.network_id='solana' AND f.tx_sig IS NOT NULL")}


def _swap_like(tx, mint):
    """The tx moves `mint` and a quote token (wSOL/USDC/WETH): a trade, decoded or not."""
    moved = set()
    for side in ("preTokenBalances", "postTokenBalances"):
        for b in (tx.get("meta") or {}).get(side) or []:
            moved.add(b.get("mint"))
    return mint in moved and bool(moved & QUOTES)


def crowd_after(txs, owner, mint, slot, index, ours, sol_usd):
    """(buys, SOL, undecoded) for other buyers on `mint` after (slot, index), excluding the owner
    and our own entries. `undecoded` counts swap-like transactions no decoder read (multi-pool
    arbitrage, a coin quoted in another token): the buy count is a lower bound by that many. Plain
    token transfers are not trades and are not counted at all."""
    n, sol, undecoded = 0, 0.0, 0
    for tx in txs:
        key = (tx.get("slot"), tx.get("transactionIndex") or 0)
        if key <= (slot, index) or tx["transaction"]["signatures"][0] in ours:
            continue
        t = dfx.trade_of(tx, mint, sol_usd)
        if t is None:
            undecoded += _swap_like(tx, mint)
        elif t["side"] == "buy" and t["trader"] != owner:
            n += 1
            sol += t["sol"]
    return n, round(sol, 3), undecoded


def sold_share(txs, owner, mint, buy_sig, bag):
    """Share of the post-buy balance the owner let go of in these txs (sells and transfers out)."""
    sold = 0
    for tx in txs:
        if tx["transaction"]["signatures"][0] == buy_sig:
            continue
        pre, post = owner_balance(tx, owner, mint)
        sold += max(0, pre - post)
    return round(sold / bag, 3) if bag else None


def measure(b, owner, ours, sol_usd):
    mint, S, t = b["mint"], b["slot"], b["t"]
    crowd_txs, crowd_complete = gtfa(mint, {"slot": {"gte": S, "lte": S + CROWD_SLOTS}}, limit=100, pages=3)
    idx = b["tx"].get("transactionIndex")
    if idx is None:   # getTransaction carries no block index; the slot-filtered list does
        idx = next((x.get("transactionIndex") for x in crowd_txs
                    if x["transaction"]["signatures"][0] == b["sig"]), None)
    crowd_n, crowd_sol, crowd_undecoded = (crowd_after(crowd_txs, owner, mint, S, idx, ours, sol_usd)
                                           if idx is not None else (None, None, None))
    ata = dfx.owner_token_account(b["tx"], owner, mint)
    share = None
    if ata and t:
        ata_txs, _ = gtfa(ata, {"blockTime": {"gte": t, "lte": t + DUMP_WINDOW_S}}, limit=50, pages=2)
        share = sold_share(ata_txs, owner, mint, b["sig"], b["bag"])
    p60 = None
    if t:
        later, _ = gtfa(mint, {"blockTime": {"gte": t + RET_AT_S, "lte": t + RET_LATEST_S}}, limit=10)
        for tx in later:
            tr = dfx.trade_of(tx, mint, sol_usd)
            if tr:
                p60 = tr["price"]
                break
    return {"sig": b["sig"], "mint": mint, "slot": S, "t": t, "index": idx, "kind": b["kind"],
            "src_sol": b["sol"], "crowd_n": crowd_n, "crowd_sol": crowd_sol, "crowd_undecoded": crowd_undecoded,
            "crowd_complete": crowd_complete, "sold_share_120s": share,
            "dump": (share >= DUMP_SHARE) if share is not None else None,
            "ret60_vs_fill": round(p60 / b["fill"] - 1, 4) if p60 and b["fill"] else None,
            "ret60_vs_spot": round(p60 / b["spot"] - 1, 4) if p60 and b["spot"] else None}


def quantiles(values):
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    q = lambda f: v[min(len(v) - 1, max(0, round(f * (len(v) - 1))))]
    return {"n": len(v), "p25": q(0.25), "median": q(0.5), "p75": q(0.75)}


def wilson(k, n, z=1.96):
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return round(max(0.0, c - h), 3), round(min(1.0, c + h), 3)


def summarise(all_rows):
    """Over buys of at least DUST_SOL: a dust re-buy is never a signal any funded floor would copy."""
    rows = [r for r in all_rows if (r.get("src_sol") or 0) >= DUST_SOL]
    dumps = [r["dump"] for r in rows if r["dump"] is not None]
    k = sum(dumps)
    return {"n": len(rows), "n_dust": len(all_rows) - len(rows), "crowd_sol": quantiles(r["crowd_sol"] for r in rows),
            "crowd_n": quantiles(r["crowd_n"] for r in rows),
            "crowd_undecoded": quantiles(r.get("crowd_undecoded") for r in rows),
            "dump_rate": round(k / len(dumps), 3) if dumps else None, "dump_k": k, "dump_n": len(dumps),
            "dump_ci95": wilson(k, len(dumps)),
            "ret60_vs_fill": quantiles(r["ret60_vs_fill"] for r in rows),
            "ret60_vs_spot": quantiles(r["ret60_vs_spot"] for r in rows)}


def wallets(candidates=()):
    from sniper.services.memecoin_address_registry import armed_verified_owners
    with contextlib.closing(sqlite3.connect(LEDGER, uri=True, timeout=30)) as c:
        c.row_factory = sqlite3.Row
        owners = armed_verified_owners(c) or {}
        armed = [(r["source_user_id"], r["handle"]) for r in c.execute(
            "SELECT source_user_id, handle, real_chains FROM memecoin_wallets WHERE real_approved=1 "
            "AND COALESCE(paused,0)=0 AND COALESCE(excluded,0)=0") if "solana" in (r["real_chains"] or "")]
        handles = {r[0]: r[1] for r in c.execute("SELECT source_user_id, handle FROM memecoin_wallets")}
        addr_of = {r[0]: r[1] for r in c.execute(
            "SELECT source_user_id, address FROM memecoin_address_registry WHERE network_id='solana' "
            "AND status IN ('verified','registry_verified')")}
    by_uid = {uid: addr for addr, (uid, _h) in owners.items()}
    out = [{"uid": uid, "handle": h, "owner": by_uid.get(uid), "role": "armed"} for uid, h in armed]
    out += [{"uid": uid, "handle": handles.get(uid), "owner": uid[5:], "role": "calibration"}
            for uid in CALIBRATION]
    out += [{"uid": uid, "handle": handles.get(uid), "owner": addr_of.get(uid), "role": "candidate"}
            for uid in candidates]
    return out


def run(out_path, sol_usd, candidates=()):
    done = set()
    if out_path.exists():
        done = {json.loads(l)["uid"] for l in out_path.read_text().splitlines() if l.strip()}
    ours = our_entry_sigs()
    for w in wallets(candidates):
        if w["uid"] in done:
            continue
        rec = {**w, "rows": [], "buy_source": {}}
        if w["owner"]:
            try:
                buys, source = wallet_buys(w["uid"], w["owner"], sol_usd)
                rec["dust_skipped"] = source.pop("dust_skipped", 0)
                rec["buy_source"] = {k: sum(1 for v in source.values() if v == k)
                                     for k in ("detection", "owner_scan")}
                for b in buys:
                    rec["rows"].append(measure(b, w["owner"], ours, sol_usd))
            except fet.Stop as exc:
                print(json.dumps({"uid": w["uid"], "STOPPED": str(exc)}), file=sys.stderr, flush=True)
                return
            except OSError as exc:      # a transport failure skips the wallet unwritten: a rerun retries it
                print(json.dumps({"uid": w["uid"], "handle": w["handle"], "SKIPPED": repr(exc)}),
                      file=sys.stderr, flush=True)
                continue
        rec["summary"] = summarise(rec["rows"])
        with open(out_path, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
        s = rec["summary"]
        print(json.dumps({"handle": w["handle"], "role": w["role"], "n": s["n"],
                          "crowd_sol_med": (s["crowd_sol"] or {}).get("median"), "dump_rate": s["dump_rate"],
                          "ret60_fill_med": (s["ret60_vs_fill"] or {}).get("median"),
                          "calls": CALLS[0], "r429": fet.RATE_429[0]}), flush=True)
    print(f"calls: {CALLS[0]} 429s: {fet.RATE_429[0]}", file=sys.stderr)


def selftest():
    def bal(owner, mint, amt):
        return {"owner": owner, "mint": mint, "accountIndex": 0,
                "uiTokenAmount": {"amount": str(amt), "decimals": 9 if mint == fet.WSOL else 6}}

    def tx(sig, slot, idx, pre, post, err=None):
        return {"transaction": {"signatures": [sig], "message": {"accountKeys": ["X"]}}, "slot": slot,
                "transactionIndex": idx, "meta": {"err": err, "logMessages": [], "preTokenBalances": pre,
                                                  "postTokenBalances": post}}
    # A pool buy by B (100 M for 1 SOL) and a pool buy by the owner O before it in the same slot.
    wsol = fet.WSOL
    buy_b = tx("b", 10, 5, [bal("P", "M", 1000), bal("P", wsol, 10**10), bal("B", "M", 0)],
               [bal("P", "M", 900), bal("P", wsol, 11 * 10**9), bal("B", "M", 100)])
    buy_o = tx("o", 10, 3, [bal("P", "M", 1000), bal("P", wsol, 10**10), bal("O", "M", 0)],
               [bal("P", "M", 900), bal("P", wsol, 11 * 10**9), bal("O", "M", 100)])
    assert crowd_after([buy_o, buy_b], "O", "M", 10, 3, set(), 100.0) == (1, 1.0, 0)
    assert crowd_after([buy_o, buy_b], "O", "M", 10, 3, {"b"}, 100.0) == (0, 0.0, 0)      # ours excluded
    assert crowd_after([buy_o, buy_b], "O", "M", 10, 6, set(), 100.0) == (0, 0.0, 0)      # before us
    transfer = tx("t", 11, 0, [bal("X", "M", 5), bal("Y", "M", 0)], [bal("X", "M", 4), bal("Y", "M", 1)])
    arb = tx("a", 11, 1, [bal("P", "M", 9), bal("Q", "M", 9), bal("P", wsol, 10**9), bal("Q", wsol, 10**9)],
             [bal("P", "M", 8), bal("Q", "M", 10), bal("P", wsol, 2 * 10**9), bal("Q", wsol, 0)])
    assert crowd_after([buy_o, transfer], "O", "M", 10, 3, set(), 100.0) == (0, 0.0, 0)   # not a trade
    swap = tx("w", 11, 2, [bal("R", "M", 9), bal("Z", "M", 0), bal("Q", wsol, 10**9)],
              [bal("R", "M", 8), bal("Z", "M", 1), bal("Q", wsol, 0)])   # multi-hop: no one pool holds both
    assert crowd_after([buy_o, arb], "O", "M", 10, 3, set(), 100.0) == (0, 0.0, 0)        # arb reads as a sell
    assert crowd_after([buy_o, swap], "O", "M", 10, 3, set(), 100.0) == (0, 0.0, 1)       # swap-like, unread
    b = buy_of(buy_o, "O", 100.0)
    assert b["mint"] == "M" and b["bought"] == 100 and b["bag"] == 100, b
    assert buy_of(tx("s", 1, 0, [bal("O", "M", 0)], [bal("O", "M", 50)]), "O", 100.0) is None   # airdrop
    assert buy_of({**buy_o, "meta": {**buy_o["meta"], "err": {"x": 1}}}, "O", 100.0) is None
    sell = tx("s", 11, 0, [bal("O", "M", 100)], [bal("O", "M", 40)])
    assert sold_share([buy_o, sell], "O", "M", "o", 100) == 0.6
    assert wilson(0, 10)[0] == 0.0 and wilson(10, 10)[1] == 1.0 and wilson(0, 0) is None
    assert quantiles([3, 1, 2])["median"] == 2 and quantiles([None]) is None
    s = summarise([{"dump": True, "crowd_sol": 1, "crowd_n": 1, "ret60_vs_fill": -0.1, "ret60_vs_spot": 0,
                    "src_sol": 1.0},
                   {"dump": False, "crowd_sol": 3, "crowd_n": 2, "ret60_vs_fill": 0.2, "ret60_vs_spot": 0.1,
                    "src_sol": 2.0},
                   {"dump": True, "crowd_sol": 9, "crowd_n": 9, "ret60_vs_fill": -0.9, "ret60_vs_spot": 0,
                    "src_sol": 0.0099}])
    assert s["dump_rate"] == 0.5 and s["dump_k"] == 1 and s["n"] == 2 and s["n_dust"] == 1, s
    print("selftest ok")


def _pct(q, key="median"):
    return "—" if not q or q.get(key) is None else f"{q[key] * 100:+.1f}%"


def _iqr_pct(q):
    return "" if not q else f" [{q['p25'] * 100:+.0f}, {q['p75'] * 100:+.0f}]"


def funded_solana():
    """{source_user_id: (n, wins, pnl)} over closed funded Solana positions, all time."""
    with contextlib.closing(sqlite3.connect(LEDGER, uri=True, timeout=30)) as c:
        return {r[0]: (r[1], r[2], r[3]) for r in c.execute(
            "SELECT source_user_id, COUNT(*), SUM(realized_pnl > 0), ROUND(SUM(COALESCE(realized_pnl, 0)), 2) "
            "FROM memecoin_positions WHERE execution_mode='real' AND network_id='solana' AND is_open=0 "
            "GROUP BY source_user_id")}


def table(records, min_n=5, funded=None):
    """Markdown rows: calibration first, then ranked by median ret60 vs the source's fill; n < min_n
    is a no-call. `funded` adds each wallet's closed funded Solana record (roster calls use it)."""
    funded = funded or {}
    def key(r):
        s = r["summary"]
        med = (s["ret60_vs_fill"] or {}).get("median")
        return (r["role"] != "calibration", s["n"] < min_n, -(med if med is not None else -9))
    lines = ["| # | wallet | role | n | buys from | crowd SOL S..S+2 med [p25, p75] | crowd buys med | "
             "dump ≥30% in 120 s: k/n (95% CI) | ret60 vs source fill med [p25, p75] | "
             "ret60 vs post-trade spot med | funded Solana n, wins, PnL | call |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    rank = 0
    for r in sorted(records, key=key):
        s = r["summary"]
        call = "no-call (n<5)" if s["n"] < min_n else ""
        if not r.get("owner"):
            call = "no address: unmeasurable"
        if s["n"] >= min_n and r["role"] != "calibration":
            rank += 1
        fn = funded.get(r["uid"])
        fcell = f"{fn[0]}, {fn[1]}, ${fn[2]:+,.2f}" if fn else "none"
        cs, cn = s["crowd_sol"], s["crowd_n"]
        src = r.get("buy_source") or {}
        ci = s["dump_ci95"]
        lines.append(
            f"| {rank if s['n'] >= min_n and r['role'] != 'calibration' else ''} | {r['handle']} | {r['role']} | {s['n']} | "
            f"det {src.get('detection', 0)} / scan {src.get('owner_scan', 0)} | "
            + (f"{cs['median']:.2f} [{cs['p25']:.2f}, {cs['p75']:.2f}]" if cs else "—") + " | "
            + (f"{cn['median']}" if cn else "—")
            + (f" (+{s['crowd_undecoded']['median']} unread)" if (s.get("crowd_undecoded") or {}).get("median") else "")
            + " | "
            + (f"{s['dump_k']}/{s['dump_n']} ({ci[0]:.2f}–{ci[1]:.2f})" if ci else "—") + " | "
            + _pct(s["ret60_vs_fill"]) + _iqr_pct(s["ret60_vs_fill"]) + " | "
            + _pct(s["ret60_vs_spot"]) + f" | {fcell} | {call} |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", choices=("run", "table"))
    ap.add_argument("--out", default="logs/wallet_scoreboard_20260927.jsonl")
    ap.add_argument("--sol-usd", type=float, default=121.0)
    ap.add_argument("--candidate", action="append", default=[], help="source_user_id to score beside the armed set")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
        return
    if a.cmd == "run":
        run(REPO / a.out, a.sol_usd, a.candidate)
    if a.cmd == "table":
        print(table([json.loads(l) for l in (REPO / a.out).read_text().splitlines() if l.strip()],
                    funded=funded_solana()))


if __name__ == "__main__":
    main()
