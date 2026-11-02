#!/usr/bin/env python3
"""Move-possible floor by size band: does pre-entry activity predict that a coin can still move?

Pre-registered in docs/audits/2026-09-28-move-possible-floor-by-band.md (3abfd975) before any outcome
was computed. Operator order 2026-09-28: a floor that guarantees a move is possible without
admitting dead coins, different per coin size.

Population: buy attempts on solana / 4663 decided 2026-09-14T00:00Z .. 2026-09-28T14:45Z whose
classification carries pre-entry facts (facts.volume_h1 and an mcap), coin_band nano (incl.
assumed_nano) / micro / small / mid; one decision per (chain, mint) per 900 s, the first.

Outcome, Solana, from the chain (research key via funded_exit_truth.rpc, flat-gated per decision,
one job at a time): every SOL-quoted print on the mint with blockTime in [T-300, T+900]
(getTransactionsForAddress, succeeded, ascending), decoded by dump_forensics.trade_of. P0 = the last
print at or before T, else the first after. M = a print in (T, T+300] >= 1.25 P0. D = no print in
(T, T+900] reaches 1.10 P0 (a complete read with no print after T is D). A truncated read decides
only what its coverage reaches; the rest is censored.
4663: no historical path (tape ~1 h). Descriptive only, from funded dexscreener marks.

    python3 scripts/research/move_possible_floor.py fetch --out logs/acceptance/move_possible.jsonl
    python3 scripts/research/move_possible_floor.py report --out logs/acceptance/move_possible.jsonl
    python3 scripts/research/move_possible_floor.py --selftest
"""
import argparse
import collections
import contextlib
import json
import random
import sqlite3
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "scripts" / "research")]
LEDGER = str(REPO / "sniper_tracker.db")
SINCE, UNTIL = "2026-09-14T00:00:00", "2026-09-28T14:45:00"
DEDUPE_S, PRE_S, M_S, D_S = 900, 300, 300, 900
M_UP, D_UP = 1.25, 1.10
PAGES = 20
# AMENDMENTS 2026-09-28 before any Solana outcome was read (Lambda review of 3abfd975):
FILTER_X = 50        # F2: outlier band vs the pre-T median; a unit/decimals error is >= 10x, a real nano pump can be 6x
FILL_LAG_S = 2       # F4: funded Solana decision -> fill p50 1.89 s; P0 includes prints up to T + 2 s, M/D start after
SEED, DRAWS = 20260928, 2000
TERCILES = {("solana", "nano"): (0.1679, 1.6616), ("solana", "micro"): (0.1645, 1.2042),
            ("solana", "small"): (0.2356, 1.5092), ("4663", "nano"): (0.0108, 0.6428),
            ("4663", "micro"): (0.2339, 1.1069)}          # fixed in the pre-registration, pre-outcome


def ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def ro():
    return sqlite3.connect("file:%s?mode=ro" % LEDGER, uri=True, timeout=30)


def measures(cj, tj):
    """Pre-entry activity from the attempt's own classification facts and trace marks."""
    f = (cj or {}).get("facts") or {}
    ev = {}
    for e in (tj or {}).get("substage_events") or []:
        ev.setdefault(e.get("name"), e)
    mcap = f.get("volume_mcap") or f.get("effective_mcap")
    num = lambda v: float(v) if isinstance(v, (int, float)) else None
    h1, m5, liq, mc = num(f.get("volume_h1")), num(f.get("volume_m5")), num(f.get("liquidity")), num(mcap)
    pick = ((ev.get("dex_pair_selection") or {}).get("activity_pick") or {})
    flow = ev.get("pre_entry_flow") or {}
    return {"volume_h1": h1, "volume_m5": m5, "mcap": mc, "liquidity": liq,
            "turnover_h1": h1 / mc if h1 is not None and mc else None,
            "turnover_m5": m5 / mc if m5 is not None and mc else None,
            "liq_mcap": liq / mc if liq is not None and mc else None,
            "txns_m5": num((ev.get("volume_gate") or {}).get("txns")), "txns_h1": num(pick.get("txns_h1")),
            "flow_buy_n": num(flow.get("buy_n")), "flow_makers_n": num(flow.get("makers_n")),
            # F6: the gate reads ORGANIC h1 (source buy removed); keep the source size as a covariate
            "source_size_usd": num((ev.get("source_buy_floor") or {}).get("source_size_usd")),
            "turnover_h1_ex_source": (max(0.0, h1 - (num((ev.get("source_buy_floor") or {}).get("source_size_usd")) or 0.0)) / mc
                                      if h1 is not None and mc else None)}


def population():
    with contextlib.closing(ro()) as c:
        rows = c.execute(
            "SELECT attempt_id, network_id, mint, decision_started_at, terminal_status, terminal_reason, "
            "source_handle, classification_json, trace_json, transaction_submitted_at FROM memecoin_entry_attempts WHERE side='buy' "
            "AND network_id IN ('solana','4663') AND decision_started_at>=? AND decision_started_at<? "
            "ORDER BY decision_started_at", (SINCE, UNTIL)).fetchall()
    last, out = {}, []
    for aid, net, mint, dec, status, reason, handle, cj, tj, submitted in rows:
        cj = json.loads(cj or "{}")
        band = cj.get("coin_band")
        band = "nano" if band == "assumed_nano" else band
        if band not in ("nano", "micro", "small", "mid"):
            continue
        m = measures(cj, json.loads(tj or "{}"))
        if m["volume_h1"] is None or not m["mcap"] or m["mcap"] <= 0:
            continue
        T, key = ts(dec), (net, mint)
        if key in last and T - last[key] < DEDUPE_S:
            continue
        last[key] = T
        out.append({"attempt_id": aid, "net": net, "mint": mint, "T": T, "decided": dec, "band": band,
                    "status": status, "reason": (reason or "")[:120], "handle": handle,
                    "submitted_at": submitted, **m})
    return out


def our_fill_sigs():
    """{tx_sig: 1 entry | 2 exit} for every funded Solana fill (F3: exits print too, and down)."""
    with contextlib.closing(ro()) as c:
        out = {r[0]: (1 if r[1] == "entry" else 2) for r in c.execute(
            "SELECT f.tx_sig, f.kind FROM memecoin_fills f JOIN memecoin_positions p ON p.id=f.position_id "
            "WHERE p.execution_mode='real' AND p.network_id='solana' AND f.tx_sig IS NOT NULL")}
        for (sig,) in c.execute("SELECT close_sig FROM memecoin_positions WHERE execution_mode='real' "
                                "AND network_id='solana' AND close_sig IS NOT NULL"):
            out.setdefault(sig, 2)
    return out


def fetch(args):
    import dump_forensics as dfx
    import funded_exit_truth as fet
    import slot_lane_send_monitor as mon
    from slot_minout_grading import wait_flat
    from sniper.execution.solana_rpc_pool import key_id
    from wallet_crowd_dump_scoreboard import gtfa
    print("research key", key_id(mon.rpc_url()), flush=True)
    decs = [d for d in population() if d["net"] == "solana"]
    out = Path(args.out)
    done = {json.loads(l)["attempt_id"] for l in out.read_text().splitlines() if l.strip()} if out.exists() else set()
    todo = [d for d in decs if d["attempt_id"] not in done]
    print("solana decisions %d, todo %d" % (len(decs), len(todo)), flush=True)
    ours = our_fill_sigs()
    with out.open("a") as fh:
        for d in todo:
            wait_flat(ro)
            T = int(d["T"])
            try:
                txs, complete = gtfa(d["mint"], {"blockTime": {"gte": T - PRE_S, "lte": T + D_S}},
                                     limit=100, pages=PAGES)
            except fet.Stop as exc:
                print("STOPPED:", exc, flush=True)
                break
            except OSError as exc:
                print("SKIPPED %s: %r" % (d["attempt_id"][:8], exc), flush=True)
                continue
            prints = []
            for tx in txs:
                t1 = dfx.trade_of(tx, d["mint"], 1.0)
                if not t1 or not t1.get("price"):
                    continue
                t2 = dfx.trade_of(tx, d["mint"], 1000.0)
                if not (t2 and t2.get("price") and abs(t2["price"] / t1["price"] - 1000.0) < 1.0):
                    continue
                sig = tx["transaction"]["signatures"][0]
                prints.append([tx.get("blockTime"), tx.get("slot"), tx.get("transactionIndex") or 0, t1["price"],
                               t1["side"], t1["trader"], round(t1["sol"], 4), ours.get(sig, 0)])
            rec = {**d, "prints": prints, "complete": complete, "n_tx": len(txs),
                   "last_bt": max([tx.get("blockTime") or 0 for tx in txs] or [0])}
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            print("%s %-6s %-8s tx %d prints %d complete %s" % (d["decided"][:16], d["band"], d["status"],
                                                             len(txs), len(prints), complete), flush=True)
    print("research-key 429s %d" % fet.RATE_429[0], flush=True)


# ------------------------------------------------------------------ outcomes (pure; tested)

def outcome(rec, filter_x=None):
    """(M, D, P0, n_filtered) with None for censored. Prints: [bt, slot, idx, price, side, trader, sol, ours]
    where ours is 0 other, 1 our entry, 2 our exit.

    Amended before any Solana outcome was read (Lambda review): the outlier median comes from prints at
    or before T only, so a run-up after T cannot remove the reference (F1); the band is FILTER_X (F2);
    every print of ours is excluded, and a filled row is anchored at the first other print after our
    entry, so our own impact is not graded as the market's move (F3); P0 is the highest print up to
    T + FILL_LAG_S and M/D count from after it, since we would buy into that wave, not ahead of it (F4)."""
    fx = FILTER_X if filter_x is None else filter_x
    T = rec["T"]
    allp = sorted((p[0], p[1], p[2], p[3], p[7]) for p in rec["prints"] if p[0] is not None and p[3] > 0)
    ps = [p for p in allp if not p[4]]
    ref = [p[3] for p in ps if p[0] <= T] or [p[3] for p in ps[:5]]
    n_filtered = 0
    if ref:
        med = statistics.median(ref)
        kept = [p for p in ps if med / fx <= p[3] <= med * fx]
        n_filtered, ps = len(ps) - len(kept), kept
    upto = [p for p in ps if p[0] <= T + FILL_LAG_S]
    after = [p for p in ps if p[0] > T + FILL_LAG_S]
    p0 = max((p[3] for p in upto if p[0] > T), default=None)
    before = [p for p in upto if p[0] <= T]
    if before:
        p0 = max(p0 or 0.0, before[-1][3])
    our_entry = next((p for p in allp if p[4] == 1), None)
    if our_entry is not None:
        post = [p for p in ps if (p[0], p[1], p[2]) > our_entry[:3]]
        if post:
            p0 = max(p0 or 0.0, post[0][3])
            after = [p for p in after if (p[0], p[1], p[2]) > post[0][:3]]
    if p0 is None and after:
        p0, after = after[0][3], after[1:]
    covered = T + D_S if rec["complete"] else rec["last_bt"]
    if p0 is None:
        return (False if rec["complete"] else None), (True if rec["complete"] else None), None, n_filtered
    m_hit = any(p[3] >= M_UP * p0 for p in after if p[0] <= T + M_S)
    M = True if m_hit else (False if covered >= T + M_S else None)
    d_miss = any(p[3] >= D_UP * p0 for p in after if p[0] <= T + D_S)
    D = False if d_miss else (True if covered >= T + D_S else None)
    return M, D, p0, n_filtered


def pre_activity(rec):
    T = rec["T"]
    pre = [p for p in rec["prints"] if p[0] is not None and T - PRE_S < p[0] <= T and not p[7]]
    buys = [p for p in pre if p[4] == "buy"]
    return {"chain_buys_5m": len(buys), "chain_buyers_5m": len({p[5] for p in buys}),
            "chain_sol_5m": sum(p[6] for p in pre)}


def auc(xs, ys):
    """P(x of a random positive > x of a random negative), ties half. None when a class is empty."""
    pos = [x for x, y in zip(xs, ys) if y]
    neg = [x for x, y in zip(xs, ys) if not y]
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def boot(fn, rows, rng):
    """Bootstrap 95% CI resampling MINTS, not rows (F7): one mint's repeated decisions are correlated."""
    groups = collections.defaultdict(list)
    for i, r in enumerate(rows):
        groups[r.get("mint") or i].append(r)
    keys = list(groups)
    vals = []
    for _ in range(DRAWS):
        s = [r for _k in keys for r in groups[keys[rng.randrange(len(keys))]]]
        v = fn(s)
        if v is not None:
            vals.append(v)
    vals.sort()
    return (vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals)) - 1]) if len(vals) > 100 else (None, None)


def rate(rows, key):
    v = [r[key] for r in rows if r[key] is not None]
    return (sum(v) / len(v), len(v)) if v else (None, 0)


WINDOWS_DB = REPO / "data" / "research_signal_windows.db"


def path_4663(ticks, meta):
    """[(block_ts, rel_price)] for one pool from sqrt_price_x96, in the token's quote units up to a
    constant (the ratio is all M and D need). quote_side 0: token = token1, price ~ 1/sqrtP^2."""
    out = []
    for bts, sqrtp in ticks:
        try:
            x = int(sqrtp)
        except (TypeError, ValueError):
            continue
        if x <= 0:
            continue
        r = (2 ** 96 / x) ** 2 if meta.get("quote_side") == 0 else (x / 2 ** 96) ** 2 if meta.get("quote_side") == 1 else None
        if r:
            out.append((bts, r))
    return out


def forward_4663(db=WINDOWS_DB):
    """4663 decisions graded FORWARD from the signal-window recorder (live since 2026-09-28 14:17Z).
    One pool per signal: the one with the most priced ticks. A window shorter than T+900 leaves D
    censored; an unresolved tape gap inside the window censors both."""
    if not Path(db).exists():
        return []
    recs = []
    with contextlib.closing(sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)) as w, \
            contextlib.closing(ro()) as led:
        for key, first, attempts, src_ts, end_ts, pools, partial, src_block in w.execute(
                "SELECT signal_key, first_decided_at, attempts_json, src_ts, end_ts, pools_json, partial_start, "
                "src_block FROM signals WHERE closed=1 AND src_block IS NOT NULL"):
            aid = (json.loads(attempts or "[]") or [{}])[0].get("attempt_id")
            row = led.execute("SELECT classification_json, trace_json, terminal_status, terminal_reason FROM "
                              "memecoin_entry_attempts WHERE attempt_id=?", (aid,)).fetchone()
            if not row:
                continue
            cj = json.loads(row[0] or "{}")
            band = "nano" if cj.get("coin_band") == "assumed_nano" else cj.get("coin_band")
            if band not in ("nano", "micro", "small", "mid"):
                continue
            m = measures(cj, json.loads(row[1] or "{}"))
            best = []
            for pool in json.loads(pools or "[]"):
                meta = w.execute("SELECT meta_json FROM pool_meta WHERE chain_id='4663' AND pool=?", (pool,)).fetchone()
                ticks = w.execute(
                    "SELECT b.block_ts, t.sqrt_price_x96 FROM ticks t JOIN block_times b ON b.chain_id=t.chain_id "
                    "AND b.block_no=t.block_no WHERE t.chain_id='4663' AND t.pool=? AND t.sqrt_price_x96 IS NOT NULL "
                    "ORDER BY t.block_no, t.log_index", (pool,)).fetchall()
                pth = path_4663([(ts(b), sp) for b, sp in ticks], json.loads(meta[0]) if meta else {})
                if len(pth) > len(best):
                    best = pth
            T = ts(first)
            end_block = w.execute("SELECT MAX(block_no) FROM block_times WHERE chain_id='4663' AND block_ts<=?",
                                  (datetime.fromtimestamp(end_ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),)
                                  ).fetchone()[0] or src_block
            gap = w.execute("SELECT COUNT(*) FROM tick_gaps WHERE chain_id='4663' AND resolved_at IS NULL "
                            "AND from_block<=? AND to_block>=?", (end_block, src_block)).fetchone()[0]
            prints = [[bt, 0, 0, pr, "", "", 0.0, 0] for bt, pr in best if src_ts - 1 <= bt <= end_ts]
            # The recorded window ends at source + window_s; a window that stops before T + D_S cannot
            # decide D, so it is marked incomplete and outcome() reads its coverage from last_bt.
            ok = not partial and not gap
            recs.append({"net": "4663", "band": band, "T": T, "status": row[2], "reason": row[3] or "", "mint": key,
                         "prints": prints, "complete": ok and end_ts >= T + D_S,
                         "last_bt": end_ts if ok else src_ts, **m})
    return recs


def report(args):
    recs = [json.loads(l) for l in Path(args.out).read_text().splitlines() if l.strip()] if Path(args.out).exists() else []
    fwd = forward_4663()
    print("4663 FORWARD from the signal-window recorder: %d closed signals (window end = source + 600 s, "
          "so D, which needs T+900, stays censored until the window is widened)" % len(fwd))
    recs += fwd
    for r in recs:
        r["M"], r["D"], r["P0"], r["n_filtered"] = outcome(r)
        r["M5"] = outcome(r, 5)[0]
        r.update(pre_activity(r))
    offs = sorted(next(p[0] for p in r["prints"] if p[7] == 1) - ts(r["submitted_at"]) for r in recs
                  if r.get("submitted_at") and any(p[7] == 1 and p[0] for p in r["prints"]))
    print("F5 clock check: our entry tx blockTime minus transaction_submitted_at, n=%d, p10/p50/p90 %s s" % (
        len(offs), "/".join("%.1f" % offs[int(q * (len(offs) - 1))] for q in (0.1, 0.5, 0.9)) if offs else "na"))
    fl = [r["n_filtered"] for r in recs]
    print("F2 outlier filter (%dx, pre-T median): prints removed per record p50 %s max %s; P(M) at 5x vs %dx: %s vs %s" % (
        FILTER_X, statistics.median(fl) if fl else "na", max(fl) if fl else "na", FILTER_X,
        "%.3f" % rate(recs, "M5")[0] if rate(recs, "M5")[0] is not None else "na",
        "%.3f" % rate(recs, "M")[0] if rate(recs, "M")[0] is not None else "na"))
    print("MULTIPLICITY: 5 cells x (AUC + floor rule), uncorrected. The floor rule (two CIs, AND) is the decision; "
          "a single AUC 'PREDICTS' line is not.")
    rng = random.Random(SEED)
    print("records %d; truncated %d; M decided %d; D decided %d" % (
        len(recs), sum(not r["complete"] for r in recs), sum(r["M"] is not None for r in recs),
        sum(r["D"] is not None for r in recs)))
    cells = collections.defaultdict(list)
    for r in recs:
        cells[(r["net"], r["band"])].append(r)
    for cell in sorted(cells):
        rs = cells[cell]
        dm = [r for r in rs if r["M"] is not None and r["turnover_h1"] is not None]
        pm, nm = rate(rs, "M")
        pd, nd = rate(rs, "D")
        print("\n%s %s: decisions %d, M decided %d P(M) %s, D decided %d P(D) %s" % (
            cell[0], cell[1], len(rs), nm, "%.3f" % pm if pm is not None else "na", nd,
            "%.3f" % pd if pd is not None else "na"))
        if len(dm) >= 30:
            a = auc([r["turnover_h1"] for r in dm], [r["M"] for r in dm])
            lo, hi = boot(lambda s: auc([r["turnover_h1"] for r in s], [r["M"] for r in s]), dm, rng)
            print("  PRIMARY AUC(turnover_h1 -> M) = %.3f [%s, %s] n=%d -> %s" % (
                a, "%.3f" % lo if lo is not None else "na", "%.3f" % hi if hi is not None else "na", len(dm),
                "PREDICTS" if lo is not None and (lo > 0.5 or hi < 0.5) else "does not predict"))
        else:
            print("  PRIMARY not run: %d decided rows < 30" % len(dm))
        cut = TERCILES.get(cell)
        rs = [r for r in rs if r["turnover_h1"] is not None]     # the floor rule needs the predictor
        if cut:
            low = [r for r in rs if r["turnover_h1"] < cut[0]]
            rest = [r for r in rs if r["turnover_h1"] >= cut[0]]
            for name, part in (("T1 < %.4f" % cut[0], low), ("T2 %.4f-%.4f" % cut, [r for r in rest if r["turnover_h1"] < cut[1]]),
                               ("T3 >= %.4f" % cut[1], [r for r in rest if r["turnover_h1"] >= cut[1]])):
                a, na = rate(part, "M")
                b, nb = rate(part, "D")
                print("  %-22s n %3d  P(M) %s (n %d, censored %d)  P(D) %s (n %d, censored %d)  filled %d  h1-refused %d" % (
                    name, len(part), "%.3f" % a if a is not None else "na", na, len(part) - na,
                    "%.3f" % b if b is not None else "na", nb, len(part) - nb,
                    sum(r["status"] == "filled" for r in part),
                    sum("under_abs_floor" in r["reason"] or "turnover_under_floor" in r["reason"] for r in part)))
            dM = lambda s: (lambda l, h: None if l[0] is None or h[0] is None else h[0] - l[0])(
                rate([r for r in s if r["turnover_h1"] < cut[0]], "M"), rate([r for r in s if r["turnover_h1"] >= cut[0]], "M"))
            dD = lambda s: (lambda l, h: None if l[0] is None or h[0] is None else l[0] - h[0])(
                rate([r for r in s if r["turnover_h1"] < cut[0]], "D"), rate([r for r in s if r["turnover_h1"] >= cut[0]], "D"))
            m_lo, m_hi = boot(dM, rs, rng)
            d_lo, d_hi = boot(dD, rs, rng)
            ok = m_lo is not None and m_lo > 0 and d_lo is not None and d_lo > 0
            print("  FLOOR RULE: P(M) rest minus T1 %s [%s, %s]; P(D) T1 minus rest %s [%s, %s] -> %s" % (
                "%.3f" % dM(rs) if dM(rs) is not None else "na", "%.3f" % m_lo if m_lo is not None else "na",
                "%.3f" % m_hi if m_hi is not None else "na", "%.3f" % dD(rs) if dD(rs) is not None else "na",
                "%.3f" % d_lo if d_lo is not None else "na", "%.3f" % d_hi if d_hi is not None else "na",
                "FLOOR SUPPORTED at turnover_h1 %.4f" % cut[0] if ok else "no floor supported"))
        if len(dm) >= 30:
            print("  secondary (exploratory) AUC for M:")
            for key in ("turnover_h1_ex_source", "source_size_usd",
                        "volume_h1", "volume_m5", "turnover_m5", "liquidity", "liq_mcap", "txns_m5", "txns_h1",
                        "flow_buy_n", "flow_makers_n", "chain_buys_5m", "chain_buyers_5m", "chain_sol_5m"):
                sub = [r for r in dm if r.get(key) is not None]
                if len(sub) >= 30:
                    a = auc([r[key] for r in sub], [r["M"] for r in sub])
                    lo, hi = boot(lambda s: auc([r[key] for r in s], [r["M"] for r in s]), sub, rng)
                    print("    %-16s %.3f [%s, %s] n=%d" % (key, a, "%.3f" % lo if lo is not None else "na",
                                                          "%.3f" % hi if hi is not None else "na", len(sub)))


def selftest():
    T = 1000.0
    base = {"T": T, "complete": True, "last_bt": T + 900}
    pr = lambda bt, price, ours=0: [bt, 1, 0, price, "buy", "x", 1.0, ours]
    assert outcome({**base, "prints": [pr(990, 1.0), pr(1100, 1.30)]})[:2] == (True, False)
    assert outcome({**base, "prints": [pr(990, 1.0), pr(1500, 1.30)]})[:2] == (False, False)   # +30% after 300 s
    assert outcome({**base, "prints": [pr(990, 1.0), pr(1200, 1.05)]})[:2] == (False, True)
    assert outcome({**base, "prints": [pr(990, 1.0)]})[:2] == (False, True)                      # no trade after T
    assert outcome({**base, "prints": [pr(990, 1.0), pr(1100, 1.30, ours=2)]})[:2] == (False, True)  # ours excluded
    # F1: a run-up after T must not filter out the pre-T reference
    # (reachable only beyond the band: a 60x run-up; an all-prints median would drop the 1.0 reference)
    pump = {**base, "prints": [pr(990, 1.0)] + [pr(1100 + i, 60.0) for i in range(20)]}
    assert outcome(pump)[2] == 1.0, outcome(pump)
    # F3: our exit print is excluded; a filled row is anchored after our own entry
    assert outcome({**base, "prints": [pr(990, 1.0), pr(1200, 0.5, ours=2), pr(1300, 1.05)]})[:2] == (False, True)
    anchored = {**base, "prints": [pr(990, 1.0), pr(1001, 1.2, ours=1), pr(1003, 1.2), pr(1100, 1.30)]}
    assert outcome(anchored)[:3] == (False, True, 1.2), outcome(anchored)     # +8% from the anchor, not +30%
    # F4: P0 is the highest print up to T + FILL_LAG_S
    assert outcome({**base, "prints": [pr(990, 1.0), pr(1002, 1.10), pr(1100, 1.30)]})[:3] == (False, False, 1.10)
    trunc = {"T": T, "complete": False, "last_bt": T + 120}
    assert outcome({**trunc, "prints": [pr(990, 1.0), pr(1050, 1.02)]})[:2] == (None, None)
    assert outcome({**trunc, "prints": [pr(990, 1.0), pr(1050, 1.30)]})[:2] == (True, False)
    assert auc([1, 2, 3, 4], [0, 0, 1, 1]) == 1.0 and auc([1, 1], [0, 1]) == 0.5 and auc([1], [1]) is None
    m = measures({"facts": {"volume_h1": 3000, "volume_mcap": 30000, "liquidity": 9000}}, {})
    assert abs(m["turnover_h1"] - 0.1) < 1e-12 and abs(m["liq_mcap"] - 0.3) < 1e-12
    q = 2 ** 96
    p = path_4663([(1, str(q)), (2, str(q // 2))], {"quote_side": 0})
    assert abs(p[1][1] / p[0][1] - 4.0) < 1e-9          # sqrtP halves -> token price x4 when token0 is the quote
    assert path_4663([(1, str(q))], {}) == []            # unknown orientation: no price
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("fetch", "report"):
        p = sub.add_parser(name)
        p.add_argument("--out", required=True)
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    return fetch(a) if a.cmd == "fetch" else report(a)


if __name__ == "__main__":
    main()
