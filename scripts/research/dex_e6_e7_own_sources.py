#!/usr/bin/env python3
"""Can our own sources replace DexScreener for E6 (liquidity) and E7 (m5 buy/sell counts)?

Alpha, 2026-09-29 (operator item 2 of the DexScreener removal). This is read-only. It reports
coverage at decision time, agreement with the DexScreener values the attempt recorded, and the
latency class of each own source.

  recorded   Traces only, no network. The two DexScreener E7 inputs (sell_share share_pct and txns),
             set against the on-chain m5 read that already runs at decision on 4663/8453/56
             (substage onchain_volume_m5: swaps per pool). It covers all known pools and
             DexScreener's picked pair (dex_pair_selection). E6: the DexScreener pair liquidity,
             the pool-store fill, and for Solana the quote reserve implied by the slot lane's own
             source_impact_ratio (onchain_wallet_detections.source_valuation_json). For a
             constant-product leg, Q = spent / (sqrt(r) - 1).
  v4sign     Which sign convention V4 rows use on the tape. A buy of the coin must move the coin's
             price up. V3 pools (house rule: positive = into the pool) are the control.
  forward    One pass over EVM attempts decided 1-55 min ago, while the one-hour tape still holds
             their window. Own buys and sells in (T-300, T] come from rows the tape had written
             by T (observed_at <= T), on all known pools and on DexScreener's pair. Appends to
             --out.
  report     Summarises the forward jsonl.
  live       The instrument, not the decision: for the mints of attempts in the last 6 h, one
             DexScreener /latest/dex/tokens batch per chain (the endpoint the gate reads). Each
             returned pair's txns.m5 buys/sells is set against our tape's buys/sells on the same
             pool in (now-300-L, now-L] for L = 0 and 30 s (DexScreener lags; cache plus index).
             Appends to --out; live-report summarises.
  solana     Helius getTransactionsForAddress on the mint over [T-300, T-1] for sampled attempts
             with a DexScreener count, decoded by dump_forensics.trade_of (one trade per tx).
             It times the calls to cover the window. Research key, gated flat per coin.

    python3 scripts/research/dex_e6_e7_own_sources.py recorded [--since 2026-09-22T00:00:00]
    python3 scripts/research/dex_e6_e7_own_sources.py v4sign
    python3 scripts/research/dex_e6_e7_own_sources.py forward --out logs/acceptance/e7_forward.jsonl
    python3 scripts/research/dex_e6_e7_own_sources.py report --out logs/acceptance/e7_forward.jsonl
    python3 scripts/research/dex_e6_e7_own_sources.py solana --n 30 --out logs/acceptance/e7_solana.jsonl
    python3 scripts/research/dex_e6_e7_own_sources.py live --out logs/acceptance/e7_live.jsonl
    python3 scripts/research/dex_e6_e7_own_sources.py --selftest
"""
import argparse
import collections
import contextlib
import json
import math
import random
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "scripts" / "research")]
LEDGER = str(REPO / "sniper_tracker.db")
TAPE = str(REPO / "data" / "memecoin_onchain_volume.db")
WINDOW_S = 300
SWAP_KINDS = ("v3_swap", "v4_swap", "pons_v2_swap", "v2_swap")
SIG = re.compile(r"^(?:swap:raybot:|onchain:)([1-9A-HJ-NP-Za-km-z]{64,90})")
CPMM_LEGS = ("raydium_cpmm", "pump_amm", "raydium_amm", "raydium_v4")


def ro(path=LEDGER):
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * (len(xs) - 1) + 0.5))] if xs else None


def subs(trace_json):
    by = {}
    for s in (json.loads(trace_json or "{}").get("substage_events") or []):
        by.setdefault(s.get("name"), s)
    return by


def dex_counts(ss):
    """(buys, sells) from the sell_share substage, or None when DexScreener gave no counts."""
    if not ss or ss.get("share_pct") is None or not ss.get("txns"):
        return None
    total = int(ss["txns"])
    sells = round(total * float(ss["share_pct"]) / 100.0)
    return total - sells, sells


def pair_of(dps):
    pick = (dps or {}).get("liquidity_pick") or {}
    return pick.get("pair"), pick.get("liquidity_usd"), pick.get("quote_side_usd")


def implied_quote_usd(valuation):
    """Quote reserve in USD implied by the source's own trade on a constant-product leg, or None.
    The slot lane's `source_impact_ratio` is source_post_price / source_price, i.e. the post-trade
    spot over the AVERAGE fill (not over the pre-trade spot). Spending dQ on reserve Q: average
    fill = (Q+dQ)/C, post spot = (Q+dQ)^2/(Q*C), so r = 1 + dQ/Q and Q = dQ/(r-1). The fee makes dQ
    slightly smaller than spent, so this reads Q a little high."""
    try:
        r = float(valuation.get("source_impact_ratio"))
        spent = float(valuation.get("source_size_usd"))
    except (TypeError, ValueError, AttributeError):
        return None
    if not (math.isfinite(r) and r > 1.0 and spent > 0):
        return None
    q = spent / (r - 1.0)
    return q if math.isfinite(q) and q > 0 else None


def line(label, xs, band=(0.8, 1.25)):
    if not xs:
        print("    %-44s n=0" % label)
        return
    w = sum(1 for x in xs if band[0] <= x <= band[1])
    print("    %-44s n=%4d p10 %6.2f p50 %6.2f p90 %6.2f  within %.2f-%.2f: %d (%.0f%%)" % (
        label, len(xs), pct(xs, .1), pct(xs, .5), pct(xs, .9), band[0], band[1], w, 100.0 * w / len(xs)))


# ---------------------------------------------------------------------------------------------
def recorded(args):
    per = collections.defaultdict(lambda: collections.defaultdict(list))
    cnt = collections.defaultdict(collections.Counter)
    seen = set()
    with contextlib.closing(ro()) as c:
        det = {}
        for sig, vj in c.execute("SELECT signature, source_valuation_json FROM onchain_wallet_detections "
                                 "WHERE network_id='solana' AND detected_at>=? AND source_valuation_json IS NOT NULL",
                                 (args.since,)):
            det[sig] = vj
        rows = c.execute("SELECT network_id, mint, origin_event_id, decision_started_at, classification_json, "
                         "trace_json, terminal_reason FROM memecoin_entry_attempts WHERE decision_started_at>=? "
                         "ORDER BY decision_started_at", (args.since,)).fetchall()
    for r in rows:
        key = (r["network_id"], r["origin_event_id"] or r["mint"])
        if key in seen:
            continue
        seen.add(key)
        net, by = r["network_id"], subs(r["trace_json"])
        if "sell_share" not in by and "dex_pair_selection" not in by:
            continue                                   # died before any market-data read
        k = cnt[net]
        k["attempts_reaching_market_read"] += 1
        k["dex_pair_selection_recorded"] += int("dex_pair_selection" in by)
        pair, liq, qside = pair_of(by.get("dex_pair_selection"))
        # E7 ----------------------------------------------------------------------------------
        ss = by.get("sell_share")
        dc = dex_counts(ss)
        if ss:
            k["e7_reached_gate"] += 1
            if ss.get("allowed") is False:
                k["e7_refused"] += 1
            if ss.get("share_pct") is not None and (ss.get("txns") or 0) >= (ss.get("min_txns") or 8) \
                    and float(ss["share_pct"]) > float(ss.get("cap_pct") or 80):
                k["e7_over_cap_evaluated"] += 1
        if dc:
            k["e7_dex_known"] += 1
        ov = by.get("onchain_volume_m5")
        if ov:
            k["own_m5_read_ran"] += 1
            k["own_m5_reason:" + str(ov.get("reason"))] += 1
            if ov.get("duration_ms") is not None:
                per[net]["own_m5_ms"].append(float(ov["duration_ms"]))
        own_ok = ov and ov.get("reason") == "ok"
        if own_ok:
            k["own_m5_ok"] += 1
        if dc and own_ok:
            k["e7_both"] += 1
            own_all = int(ov.get("swaps") or 0) + (1 if ov.get("excluded_tx_seen") else 0)
            own_pair = sum(int(p.get("swaps") or 0) for p in ov.get("pools") or []
                           if pair and str(p.get("pool", "")).lower() == str(pair).lower())
            dex_total = sum(dc)
            per[net]["e7_all_ratio"].append(own_all / dex_total)
            if any(str(p.get("pool", "")).lower() == str(pair or "").lower() for p in ov.get("pools") or []):
                k["e7_pair_in_own_pools"] += 1
                per[net]["e7_pair_ratio"].append(own_pair / dex_total)
            mn = int((ss or {}).get("min_txns") or 8)
            k["e7_floor_agree"] += int((own_all >= mn) == (dex_total >= mn))
        # E6 ----------------------------------------------------------------------------------
        facts = (json.loads(r["classification_json"]).get("facts") or {}) if r["classification_json"] else {}
        if liq is not None:
            k["e6_dex_pair_liq"] += 1
        if facts.get("liquidity") is not None:
            k["e6_facts_liq:%s" % (facts.get("liquidity_source") or "dexscreener")] += 1
        if net == "solana":
            m = SIG.match(str(r["origin_event_id"] or ""))
            vj = det.get(m.group(1)) if m else None
            v = json.loads(vj) if vj else {}
            if v.get("source_impact_ratio") is not None:
                leg = str(v.get("impact_leg"))
                k["e6_sol_impact_ratio"] += 1
                k["e6_sol_leg:" + leg] += 1
                q = implied_quote_usd(v)
                if q and qside:
                    per[net]["e6_sol_%s_q_over_dex_qside" % ("cpmm" if leg in CPMM_LEGS else leg)].append(
                        q / float(qside))
    print("recorded since %s (one row per origin; attempts that reached the sell-share gate or a DexScreener pair pick)" % args.since)
    for net in sorted(cnt):
        k = cnt[net]
        print("\n== %s" % net)
        for key in sorted(k):
            print("    %-44s %d" % (key, k[key]))
        p = per[net]
        if p.get("own_m5_ms"):
            print("    own m5 read duration ms: p50 %.1f p90 %.1f" % (pct(p["own_m5_ms"], .5), pct(p["own_m5_ms"], .9)))
        line("E7 own all-pools swaps / DexScreener txns", p.get("e7_all_ratio"))
        line("E7 own on DexScreener's pair / DexScreener txns", p.get("e7_pair_ratio"))
        for name in sorted(x for x in p if x.startswith("e6_sol_")):
            line("E6 %s" % name[3:], p[name], band=(0.5, 2.0))


# ---------------------------------------------------------------------------------------------
def _into_pool_agreement(rows, meta):
    """{family: [n, agree]}: swaps whose positive quote amount went with the coin price rising.
    rows: (chain, pool, amount0, amount1, sqrt_price_x96) ordered by pool, block, log index; meta:
    {(chain, pool): quote_side}. A buy moves the coin price up, so into-pool rows agree ~100%."""
    out = collections.defaultdict(lambda: [0, 0])
    prev = {}
    for chain, pool, a0, a1, sqrt_px in rows:
        key = (chain, pool)
        qs = meta.get(key)
        sp = int(sqrt_px)
        last = prev.get(key)
        prev[key] = sp
        if qs is None or last is None or sp == last:
            continue
        q = int(a0 if qs == 0 else a1)
        if q == 0:
            continue
        coin_up = (sp > last) if qs == 1 else (sp < last)
        fam = "%s %s" % (chain, "v4" if len(pool) == 66 else "v3")
        out[fam][0] += 1
        out[fam][1] += int((q > 0) == coin_up)
    return out


def v4sign(args):
    """Share of swaps whose quote-amount sign agrees with the coin price moving up, per family."""
    since = (datetime.now(timezone.utc) - timedelta(minutes=50)).isoformat()
    with contextlib.closing(ro(TAPE)) as t:
        meta = {(r["chain_id"], r["pool"]): int(r["quote_side"]) for r in t.execute(
            "SELECT chain_id, pool, quote_side FROM pool_meta WHERE quote_side IN (0,1)")}
        out = _into_pool_agreement(t.execute(
            "SELECT chain_id, pool, amount0, amount1, sqrt_price_x96 FROM pool_ticks "
            "WHERE chain_id IN ('4663','8453') AND observed_at>=? AND kind='v3_swap' "
            "AND sqrt_price_x96 IS NOT NULL ORDER BY chain_id, pool, block_no, log_index", (since,)), meta)
    for fam, (n, agree) in sorted(out.items()):
        print("%-10s swaps %6d  positive quote = coin up: %.1f%%" % (fam, n, 100.0 * agree / n))


def v4_convention(t, chain, lo_iso, hi_iso, *, min_n=20):
    """The sign the tape's V4 rows carry in [lo, hi] (observed): +1 into-pool (05293598e, deployed
    2026-09-29), -1 the swapper's (before it), None when mixed (a window across the deploy) or too
    few to tell. Rows written before the deploy keep the swapper sign until the 1 h tape ages them."""
    rows = list(t.execute(
        "SELECT chain_id, pool, amount0, amount1, sqrt_price_x96 FROM pool_ticks WHERE chain_id=? "
        "AND kind='v3_swap' AND length(pool)=66 AND observed_at>=? AND observed_at<=? "
        "AND sqrt_price_x96 IS NOT NULL ORDER BY pool, block_no, log_index", (chain, lo_iso, hi_iso)))
    pools = sorted({r[1] for r in rows})
    meta = {}
    for i in range(0, len(pools), 500):
        chunk = pools[i:i + 500]
        meta.update({(chain, r[0]): int(r[1]) for r in t.execute(
            "SELECT pool, quote_side FROM pool_meta WHERE chain_id=? AND quote_side IN (0,1) AND pool IN (%s)"
            % ",".join("?" * len(chunk)), (chain, *chunk))})
    n, agree = _into_pool_agreement(rows, meta).get("%s v4" % chain, [0, 0])
    if n < min_n:
        return None
    share_ = agree / n
    return 1 if share_ >= 0.98 else -1 if share_ <= 0.02 else None


def own_counts(t, chain, mint, T_iso, *, v4_sign, pair=None):
    """(counts dict) of own buys/sells in (T-300, T] from rows written by T. Pure SQL on the tape."""
    T = ts(T_iso)
    pools = {r[0]: r[1] for r in t.execute(
        "SELECT pool, protocol FROM onchain_pool_protocols WHERE chain_id=? AND mint=?", (chain, mint.lower()))}
    res = {"pools": len(pools), "buys": 0, "sells": 0, "pair_buys": 0, "pair_sells": 0,
           "unsigned": 0, "late_rows": 0, "pair_known": bool(pair and pair.lower() in pools)}
    if not pools:
        return res
    meta = {r[0]: r[1] for r in t.execute(
        "SELECT pool, quote_side FROM pool_meta WHERE chain_id=? AND pool IN (%s)" % ",".join("?" * len(pools)),
        (chain, *pools))}
    bt = ("(CASE WHEN typeof(b.block_ts) IN ('integer','real') THEN b.block_ts ELSE unixepoch(b.block_ts) END)")
    for r in t.execute(
            "SELECT t.pool, t.amount0, t.amount1, t.observed_at FROM pool_ticks t JOIN block_times b "
            "ON b.chain_id=t.chain_id AND b.block_no=t.block_no WHERE t.chain_id=? AND t.pool IN (%s) "
            "AND t.kind IN ('v3_swap','v4_swap','pons_v2_swap','v2_swap') AND %s>? AND %s<=? "
            "AND lower(t.block_hash)=lower(b.block_hash)" % (",".join("?" * len(pools)), bt, bt),
            (chain, *pools, T - WINDOW_S, T)):
        if ts(r["observed_at"]) > T:
            res["late_rows"] += 1                      # the tape wrote it after the decision
            continue
        qs = meta.get(r["pool"])
        if qs not in (0, 1):
            res["unsigned"] += 1
            continue
        q = int(r["amount0"] if qs == 0 else r["amount1"])
        if len(r["pool"]) == 66:
            if v4_sign is None:
                res["unsigned"] += 1                   # the window's V4 convention is unknown
                continue
            q *= v4_sign
        side = "buys" if q > 0 else "sells" if q < 0 else None
        if side is None:
            continue
        res[side] += 1
        if pair and r["pool"].lower() == pair.lower():
            res["pair_" + side] += 1
    return res


def forward(args):
    now = time.time()
    lo = datetime.fromtimestamp(now - 3300, timezone.utc).isoformat()
    hi = datetime.fromtimestamp(now - 60, timezone.utc).isoformat()
    out = Path(args.out)
    done = {json.loads(l)["key"] for l in out.read_text().splitlines() if l.strip()} if out.exists() else set()
    with contextlib.closing(ro()) as c:
        rows = c.execute("SELECT network_id, mint, origin_event_id, decision_started_at, trace_json FROM "
                         "memecoin_entry_attempts WHERE network_id IN ('4663','8453') AND decision_started_at>=? "
                         "AND decision_started_at<=? "
                         "ORDER BY decision_started_at", (lo, hi)).fetchall()
    n = 0
    with contextlib.closing(ro(TAPE)) as t, out.open("a") as fh:
        for r in rows:
            key = "%s|%s" % (r["network_id"], r["origin_event_id"] or r["mint"])
            if key in done:
                continue
            done.add(key)
            by = subs(r["trace_json"])
            if "sell_share" not in by and "dex_pair_selection" not in by:
                continue                               # died before any market-data read
            pair, liq, qside = pair_of(by.get("dex_pair_selection"))
            dc = dex_counts(by.get("sell_share"))
            t0 = time.monotonic()
            T = ts(r["decision_started_at"])
            sign = args.v4_sign or v4_convention(
                t, r["network_id"], datetime.fromtimestamp(T - WINDOW_S - 60, timezone.utc).isoformat(),
                r["decision_started_at"])
            own = own_counts(t, r["network_id"], r["mint"], r["decision_started_at"], v4_sign=sign, pair=pair)
            rec = {"key": key, "chain": r["network_id"], "mint": r["mint"], "T": r["decision_started_at"],
                   "pair": pair, "dex": dc, "own": own, "v4_sign": sign,
                   "read_ms": 1000.0 * (time.monotonic() - t0)}
            fh.write(json.dumps(rec) + "\n")
            n += 1
    print("%s forward pass: %d new attempts" % (datetime.now(timezone.utc).isoformat(timespec="seconds"), n))


def share(b, s):
    return 100.0 * s / (b + s) if (b + s) else None


def report(args):
    recs = [json.loads(l) for l in Path(args.out).read_text().splitlines() if l.strip()]
    for chain in sorted({r["chain"] for r in recs}):
        rs = [r for r in recs if r["chain"] == chain]
        cov_all = [r for r in rs if r["own"]["buys"] + r["own"]["sells"] > 0]
        print("\n== %s  attempts %d  own pools known %d  own swaps>0 %d  DexScreener counts %d  pair in own pools %d" % (
            chain, len(rs), sum(r["own"]["pools"] > 0 for r in rs), len(cov_all), sum(bool(r["dex"]) for r in rs),
            sum(r["own"]["pair_known"] for r in rs)))
        print("    rows written after T (dropped): total %d; unsigned %d; read ms p50 %.1f p90 %.1f" % (
            sum(r["own"]["late_rows"] for r in rs), sum(r["own"]["unsigned"] for r in rs),
            pct([r["read_ms"] for r in rs], .5), pct([r["read_ms"] for r in rs], .9)))
        for scope, b, s in (("all pools", "buys", "sells"), ("DexScreener pair", "pair_buys", "pair_sells")):
            both = [r for r in rs if r["dex"] and sum(r["dex"]) >= 8 and (r["own"][b] + r["own"][s]) > 0
                    and (scope == "all pools" or r["own"]["pair_known"])]
            line("count ratio own/dex (%s, dex>=8)" % scope,
                 [(r["own"][b] + r["own"][s]) / sum(r["dex"]) for r in both])
            diffs = [share(r["own"][b], r["own"][s]) - share(*r["dex"]) for r in both]
            if diffs:
                print("    sell-share own - dex (%s): n=%d p10 %+.1f p50 %+.1f p90 %+.1f; |diff|<=10pp %d" % (
                    scope, len(diffs), pct(diffs, .1), pct(diffs, .5), pct(diffs, .9),
                    sum(abs(d) <= 10 for d in diffs)))


# ---------------------------------------------------------------------------------------------
DEX_CHAIN = {"4663": "robinhood", "8453": "base"}
DEX_TOKENS = "https://api.dexscreener.com/latest/dex/tokens/"


def pool_counts(t, chain, pool, lo_ts, hi_ts, *, v4_sign):
    """{buys, sells, quote_side} for one pool over block time (lo, hi]; quote_side None = unsigned."""
    row = t.execute("SELECT quote_side FROM pool_meta WHERE chain_id=? AND pool=?", (chain, pool)).fetchone()
    qs = row[0] if row else None
    out = {"buys": 0, "sells": 0, "quote_side": qs, "rows": 0, "v4_sign": v4_sign if len(pool) == 66 else 1}
    if len(pool) == 66 and v4_sign is None:
        qs = None                                      # the window's V4 convention is unknown: unsigned
    bt = ("(CASE WHEN typeof(b.block_ts) IN ('integer','real') THEN b.block_ts ELSE unixepoch(b.block_ts) END)")
    for r in t.execute(
            "SELECT t.amount0, t.amount1 FROM pool_ticks t JOIN block_times b ON b.chain_id=t.chain_id "
            "AND b.block_no=t.block_no WHERE t.chain_id=? AND t.pool=? AND t.kind IN "
            "('v3_swap','v4_swap','pons_v2_swap','v2_swap') AND %s>? AND %s<=? "
            "AND lower(t.block_hash)=lower(b.block_hash)" % (bt, bt), (chain, pool, lo_ts, hi_ts)):
        out["rows"] += 1
        if qs not in (0, 1):
            continue
        q = int(r["amount0"] if qs == 0 else r["amount1"]) * (v4_sign if len(pool) == 66 else 1)  # noqa: E501
        if q > 0:
            out["buys"] += 1
        elif q < 0:
            out["sells"] += 1
    return out


def live(args):
    import urllib.request
    now = time.time()
    since = datetime.fromtimestamp(now - 6 * 3600, timezone.utc).isoformat()
    at = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")
    n = 0
    with contextlib.closing(ro()) as c, contextlib.closing(ro(TAPE)) as t, Path(args.out).open("a") as fh:
        for chain, dex_chain in DEX_CHAIN.items():
            mints = [m for (m,) in c.execute(
                "SELECT mint FROM memecoin_entry_attempts WHERE network_id=? AND decision_started_at>=? "
                "GROUP BY lower(mint) ORDER BY MAX(decision_started_at) DESC LIMIT 30", (chain, since))]
            if not mints:
                continue
            req = urllib.request.Request(DEX_TOKENS + ",".join(mints), headers={"User-Agent": "curl/8.5.0"})
            t_get = time.time()
            body = json.loads(urllib.request.urlopen(req, timeout=20).read())
            wanted = {m.lower() for m in mints}
            known = {r[0] for r in t.execute(
                "SELECT pool FROM onchain_pool_protocols WHERE chain_id=? AND mint IN (%s)" % ",".join("?" * len(wanted)),
                (chain, *wanted))}
            signs = {lag: args.v4_sign or v4_convention(
                t, chain, datetime.fromtimestamp(t_get - WINDOW_S - lag - 60, timezone.utc).isoformat(),
                datetime.fromtimestamp(t_get, timezone.utc).isoformat()) for lag in (0, 30)}
            for p in body.get("pairs") or []:
                if p.get("chainId") != dex_chain:
                    continue
                base = str((p.get("baseToken") or {}).get("address", "")).lower()
                quote = str((p.get("quoteToken") or {}).get("address", "")).lower()
                mint = base if base in wanted else quote if quote in wanted else None
                if mint is None:
                    continue
                pool = str(p.get("pairAddress", "")).lower()
                m5 = (p.get("txns") or {}).get("m5") or {}
                rec = {"at": at, "chain": chain, "mint": mint, "pair": pool, "dex_id": p.get("dexId"),
                       "labels": p.get("labels"), "dex_buys": m5.get("buys"), "dex_sells": m5.get("sells"),
                       "liquidity_usd": (p.get("liquidity") or {}).get("usd"), "pool_known": pool in known,
                       "base_is_mint": base == mint}
                for lag in (0, 30):
                    rec["own_L%d" % lag] = pool_counts(t, chain, pool, t_get - WINDOW_S - lag, t_get - lag,
                                                       v4_sign=signs[lag])
                fh.write(json.dumps(rec) + "\n")
                n += 1
    print("%s live pass: %d pairs" % (at, n))


def live_report(args):
    recs = [json.loads(l) for l in Path(args.out).read_text().splitlines() if l.strip()]
    for chain in sorted({r["chain"] for r in recs}):
        rs = [r for r in recs if r["chain"] == chain and (r["dex_buys"] or 0) + (r["dex_sells"] or 0) >= 8]
        print("\n== %s  pair-snapshots with DexScreener m5 >= 8: %d (passes %d, pairs %d)" % (
            chain, len(rs), len({r["at"] for r in recs if r["chain"] == chain}), len({r["pair"] for r in rs})))
        print("    pool known to the tape: %d; signed (quote side known): %d" % (
            sum(r["pool_known"] for r in rs), sum(r["own_L0"]["quote_side"] in (0, 1) for r in rs)))
        by_dex = collections.Counter((r["dex_id"], bool(r["pool_known"])) for r in rs)
        print("    by dexId (known to tape): %s" % dict(sorted(by_dex.items(), key=lambda x: -x[1])))
        for lag in (0, 30):
            k = "own_L%d" % lag
            both = [r for r in rs if r[k]["quote_side"] in (0, 1) and r[k]["buys"] + r[k]["sells"] > 0]
            line("L=%ds count own/dex (signed pools)" % lag,
                 [(r[k]["buys"] + r[k]["sells"]) / (r["dex_buys"] + r["dex_sells"]) for r in both])
            diffs = [share(r[k]["buys"], r[k]["sells"]) - share(r["dex_buys"], r["dex_sells"]) for r in both]
            if diffs:
                print("    L=%ds sell-share own - dex: n=%d p10 %+.1f p50 %+.1f p90 %+.1f; |diff|<=10pp %d (%.0f%%)" % (
                    lag, len(diffs), pct(diffs, .1), pct(diffs, .5), pct(diffs, .9),
                    sum(abs(d) <= 10 for d in diffs), 100.0 * sum(abs(d) <= 10 for d in diffs) / len(diffs)))
            zero = [r for r in rs if r["pool_known"] and r[k]["rows"] == 0]
            print("    L=%ds known pool, DexScreener >= 8, tape 0 rows: %d" % (lag, len(zero)))


# ---------------------------------------------------------------------------------------------
def solana(args):
    import dump_forensics as dfx
    import funded_exit_truth as fet
    import slot_lane_send_monitor as mon
    from slot_minout_grading import wait_flat
    from sniper.execution.solana_rpc_pool import key_id
    print("research key", key_id(mon.rpc_url()), flush=True)
    pool = {}
    with contextlib.closing(ro()) as c:
        for r in c.execute("SELECT mint, origin_event_id, decision_started_at, trace_json FROM memecoin_entry_attempts "
                           "WHERE network_id='solana' AND decision_started_at>=? AND trace_json LIKE '%\"sell_share\"%'",
                           (args.since,)):
            dc = dex_counts(subs(r["trace_json"]).get("sell_share"))
            if dc and sum(dc) >= 8:
                pool.setdefault(r["mint"], {"mint": r["mint"], "T": r["decision_started_at"], "dex": dc,
                                            "pair": pair_of(subs(r["trace_json"]).get("dex_pair_selection"))[0]})
    rows = sorted(pool.values(), key=lambda x: x["T"])
    random.Random(20260929).shuffle(rows)
    out = Path(args.out)
    done = {json.loads(l)["mint"] for l in out.read_text().splitlines() if l.strip()} if out.exists() else set()
    with out.open("a") as fh:
        for r in rows[:args.n]:
            if r["mint"] in done:
                continue
            wait_flat(ro)
            T = int(ts(r["T"]))
            trades, txs, pages, cum, token, complete = collections.Counter(), 0, 0, 0.0, None, False
            for _ in range(10):
                opts = {"transactionDetails": "full", "sortOrder": "desc", "limit": 100, "encoding": "json",
                        "maxSupportedTransactionVersion": 1, "commitment": "confirmed",
                        "filters": {"status": "succeeded", "blockTime": {"gte": T - WINDOW_S, "lte": T - 1}}}
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
                cum += max(0.0, time.monotonic() - t_call - fet.SPACING_S)
                pages += 1
                data = res.get("data") or []
                for tx in data:
                    txs += 1
                    tr = dfx.trade_of(tx, r["mint"], 1.0)
                    trades[tr["side"] if tr else "undecoded"] += 1
                token = res.get("paginationToken")
                if not token or len(data) < 100:
                    complete = True
                    break
            rec = {**r, "own_buys": trades["buy"], "own_sells": trades["sell"], "undecoded": trades["undecoded"],
                   "txs": txs, "pages": pages, "complete": complete, "wall_ms": cum * 1000.0}
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            print("%s dex %s own b/s %d/%d undecoded %d pages %d %s %.0f ms" % (
                r["mint"][:8], r["dex"], trades["buy"], trades["sell"], trades["undecoded"], pages,
                "complete" if complete else "CAPPED", cum * 1000.0), flush=True)
    print("research-key 429s %d" % fet.RATE_429[0], flush=True)


def solana_report(args):
    recs = [json.loads(l) for l in Path(args.out).read_text().splitlines() if l.strip()]
    comp = [r for r in recs if r["complete"]]
    print("coins %d, window covered inside 10 pages %d; wall ms p50 %.0f p90 %.0f (complete)" % (
        len(recs), len(comp), pct([r["wall_ms"] for r in comp], .5) or 0, pct([r["wall_ms"] for r in comp], .9) or 0))
    both = [r for r in comp if r["own_buys"] + r["own_sells"] > 0]
    line("count ratio own decoded / dex txns", [(r["own_buys"] + r["own_sells"]) / sum(r["dex"]) for r in both])
    line("decoded / all txs in window", [(r["own_buys"] + r["own_sells"]) / r["txs"] for r in comp if r["txs"]],
         band=(0.8, 1.0))
    for label, rs in (("all complete", both),
                      ("decoded >= 80% of txs", [r for r in both if (r["own_buys"] + r["own_sells"]) >= 0.8 * r["txs"]]),
                      ("decoded < 80% of txs", [r for r in both if (r["own_buys"] + r["own_sells"]) < 0.8 * r["txs"]])):
        diffs = [share(r["own_buys"], r["own_sells"]) - share(*r["dex"]) for r in rs]
        if diffs:
            print("    sell-share own - dex, %s: n=%d p10 %+.1f p50 %+.1f p90 %+.1f; |diff|<=10pp %d" % (
                label, len(diffs), pct(diffs, .1), pct(diffs, .5), pct(diffs, .9), sum(abs(d) <= 10 for d in diffs)))
            line("count ratio own/dex, %s" % label, [(r["own_buys"] + r["own_sells"]) / sum(r["dex"]) for r in rs])


def selftest():
    assert dex_counts({"share_pct": 25.0, "txns": 8}) == (6, 2)
    assert dex_counts({"share_pct": None, "txns": 0}) is None and dex_counts(None) is None
    q = implied_quote_usd({"source_impact_ratio": 1.0046, "source_size_usd": 100.0})
    assert 21700 < q < 21750, q                      # 100 / 0.0046
    assert implied_quote_usd({"source_impact_ratio": 0.99, "source_size_usd": 100}) is None
    assert SIG.match("swap:raybot:" + "3" * 88 + ":x").group(1) == "3" * 88
    assert SIG.match("onchain:" + "5" * 87).group(1) == "5" * 87
    assert share(3, 1) == 25.0 and share(0, 0) is None
    # own_counts on a two-pool fixture: a V3 buy (quote in), a V4 row whose sign is flipped, a row
    # the tape wrote after T (dropped), one outside the window, and a pool with no quote side.
    t = sqlite3.connect(":memory:")
    t.row_factory = sqlite3.Row
    t.executescript(
        "CREATE TABLE onchain_pool_protocols(chain_id, pool, protocol, mint);"
        "CREATE TABLE pool_meta(chain_id, pool, quote_side);"
        "CREATE TABLE block_times(chain_id, block_no, block_ts, block_hash);"
        "CREATE TABLE pool_ticks(chain_id, pool, kind, block_no, amount0, amount1, observed_at, block_hash);")
    v3, v4, nq = "0x" + "a" * 40, "0x" + "b" * 64, "0x" + "c" * 40
    T = "2026-09-29T00:10:00+00:00"
    t0 = ts(T)
    for p in (v3, v4, nq):
        t.execute("INSERT INTO onchain_pool_protocols VALUES('4663',?,?,'0xmint')", (p, "v3"))
    t.execute("INSERT INTO pool_meta VALUES('4663',?,1)", (v3,))
    t.execute("INSERT INTO pool_meta VALUES('4663',?,0)", (v4,))
    for b, off in ((1, -100), (2, -50), (3, -400)):
        t.execute("INSERT INTO block_times(chain_id, block_no, block_ts, block_hash) VALUES('4663',?,?,?)", (b, t0 + off, "h%d" % b))
    iso = lambda off: datetime.fromtimestamp(t0 + off, timezone.utc).isoformat()   # noqa: E731
    t.executemany("INSERT INTO pool_ticks VALUES('4663',?,?,?,?,?,?,?)", [
        (v3, "v3_swap", 1, "-5", "7", iso(-99), "h1"),     # quote (token1) in: buy
        (v4, "v3_swap", 1, "-3", "9", iso(-99), "h1"),     # v4 quote token0 = -3; flipped: buy
        (v3, "v3_swap", 2, "5", "-7", iso(+2), "h2"),      # written after T: dropped
        (v3, "v3_swap", 3, "5", "-7", iso(-399), "h3"),    # outside the window
        (nq, "v3_swap", 1, "1", "-1", iso(-99), "h1"),     # no quote side: unsigned
    ])
    got = own_counts(t, "4663", "0xMINT", T, v4_sign=-1, pair=v3)
    assert (got["buys"], got["sells"], got["late_rows"], got["unsigned"]) == (2, 0, 1, 1), got
    assert (got["pair_buys"], got["pair_sells"], got["pair_known"]) == (1, 0, True), got
    got = own_counts(t, "4663", "0xmint", T, v4_sign=1, pair=None)
    assert (got["buys"], got["sells"]) == (1, 1), got
    got = pool_counts(t, "4663", v3, t0 - 300, t0, v4_sign=-1)     # no as-of filter: the late row counts
    assert (got["buys"], got["sells"], got["quote_side"]) == (1, 1, 1), got
    got = pool_counts(t, "4663", v4, t0 - 300, t0, v4_sign=-1)
    assert (got["buys"], got["sells"]) == (1, 0), got
    got = pool_counts(t, "4663", nq, t0 - 300, t0, v4_sign=-1)
    assert (got["buys"], got["sells"], got["rows"], got["quote_side"]) == (0, 0, 1, None), got
    # v4_convention: one V4 pool, quote = token1, coin price (sqrtP) rising on buys.
    t2 = sqlite3.connect(":memory:")
    t2.execute("CREATE TABLE pool_ticks(chain_id, pool, kind, block_no, log_index, amount0, amount1, "
               "sqrt_price_x96, observed_at)")
    t2.execute("CREATE TABLE pool_meta(chain_id, pool, quote_side)")
    t2.execute("INSERT INTO pool_meta(chain_id, pool, quote_side) VALUES('4663', ?, 1)", (v4,))

    def fill(sign_of_buy, n0, start):
        for i in range(n0):
            buy = i % 2 == 0
            q = (5 if buy else -5) * sign_of_buy
            t2.execute("INSERT INTO pool_ticks(chain_id, pool, kind, block_no, log_index, amount0, amount1, "
                       "sqrt_price_x96, observed_at) VALUES('4663', ?, 'v3_swap', ?, 0, ?, ?, ?, ?)",
                       (v4, start + i, str(-q), str(q), str(1000 + (i + 1) % 2), iso(start + i)))
    fill(-1, 30, 0)                                   # swapper sign: a buy pays quote in = negative
    assert v4_convention(t2, "4663", iso(0), iso(29), min_n=5) == -1
    fill(+1, 30, 100)                                 # into-pool sign after the deploy
    assert v4_convention(t2, "4663", iso(100), iso(129), min_n=5) == 1
    assert v4_convention(t2, "4663", iso(0), iso(129), min_n=5) is None    # a window across the deploy
    assert v4_convention(t2, "4663", iso(0), iso(2), min_n=5) is None      # too few to tell
    got = own_counts(t, "4663", "0xmint", T, v4_sign=None, pair=None)
    assert (got["buys"], got["sells"], got["unsigned"]) == (1, 0, 2), got   # the V4 row is unsigned
    got = pool_counts(t, "4663", v4, t0 - 300, t0, v4_sign=None)
    assert (got["buys"], got["sells"], got["v4_sign"]) == (0, 0, None), got
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("recorded")
    s.add_argument("--since", default="2026-09-22T00:00:00")
    sub.add_parser("v4sign")
    f = sub.add_parser("forward")
    f.add_argument("--out", required=True)
    f.add_argument("--v4-sign", type=int, choices=(1, -1, 0), default=0,
                   help="0 (default): detect the tape's V4 convention per window")
    r = sub.add_parser("report")
    r.add_argument("--out", required=True)
    so = sub.add_parser("solana")
    so.add_argument("--n", type=int, default=30)
    so.add_argument("--since", default="2026-09-27T00:00:00")
    so.add_argument("--out", required=True)
    sr = sub.add_parser("solana-report")
    sr.add_argument("--out", required=True)
    lv = sub.add_parser("live")
    lv.add_argument("--out", required=True)
    lv.add_argument("--v4-sign", type=int, choices=(1, -1, 0), default=0,
                   help="0 (default): detect the tape's V4 convention per window")
    lr = sub.add_parser("live-report")
    lr.add_argument("--out", required=True)
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    return {"recorded": recorded, "v4sign": v4sign, "forward": forward, "report": report,
            "solana": solana, "solana-report": solana_report, "live": live,
            "live-report": live_report}[a.cmd](a)


if __name__ == "__main__":
    main()
