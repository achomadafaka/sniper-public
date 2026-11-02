"""Coins we got sold into vs our other funded closes on the same wallets: traits known AT ENTRY.

Operator order via Alpha, 2026-09-28. EXPLORATORY: the question came after the losses; nothing here is
a pre-registered result. Read-only ledger + census JSON files; no network.

  Population  funded Solana closes opened >= --since (default 14 d), exit_reason != 'manual'.
              SOLD-INTO = exit_reason mirror / mirror_source_sell (the source sold and we mirrored);
              CONTROL   = every other close on the SAME wallets (wallets with >= 1 sold-into close).
  Traits and their writers (all as of entry; a missing value is dropped from that trait, never zeroed):
    mcap_usd          positions.entry_mcap: solana_swap_executor.py:165, the reservation's
                      source_market_cap (J7's market cap), else resolve_mint_mcap.
    depth_quote_usd   positions.entry_liquidity / 2. entry_liquidity is BACKFILLED from the first
                      DexScreener mark after entry (memecoin_exits.py:531-543): seconds after entry, a
                      proxy, and half of a two-sided pool figure.
    source_size_usd   positions.their_size_usd (resolve_source_size_usd).
    size_over_depth   source_size_usd / depth_quote_usd.
    coin_age_s        positions.entry_token_age_s (pending.source_token_age_s; DexScreener pair age).
    entry_premium_pct positions.entry_price (our balance-delta fill) / positions.their_price - 1.
    hour_utc          positions.opened_at hour.
    src_prior_hold_s  median buy -> first sell time over the source's OTHER coins completed before
                      entry, last 14 d (memecoin_events, J7 writer, received_at host clock).
    src_bought_before 1 if the source has a buy on this mint >= 60 s before our entry (J7 events).
    other_armed_n     distinct OTHER currently-armed wallets with a buy on this mint in the 24 h before
                      entry (J7 events; armed = memecoin_wallets today, an approximation).
    flow_*            trace substage pre_entry_flow (local Solana tape, observation only, J7/watcher
                      path): swaps_n, buy_n, sell_n, buy_share, gross_quote (m5-style flow).
    regime_buyers     classification_json facts.regime_current_buyers (funded classifier).
    chain_m5_*        with --flow: trades on the mint in the 300 s before entry, from the chain
                      (pre_entry_flow_chain.py; SOL-quoted only; incomplete windows dropped).
    venue             census v2 coin-sender label on the source signature (JSON files given).
  Separation  AUC = P(sold-into value > control value), ties 1/2, with a bootstrap 95% CI (2000 draws,
              both groups resampled). FLAG when the CI excludes [0.4, 0.6].

    python3 scripts/research/sold_into_traits.py [--since ISO] [--census F ...]
    python3 scripts/research/sold_into_traits.py --selftest
"""
import argparse
import collections
import contextlib
import json
import random
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "scripts" / "research")]
LEDGER = str(REPO / "sniper_tracker.db")
MIRROR = ("mirror", "mirror_source_sell")


def ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def auc(a, b):
    """P(x_a > x_b) + 0.5 P(equal) over all pairs."""
    if not a or not b:
        return None
    s = sum((x > y) + 0.5 * (x == y) for x in a for y in b)
    return s / (len(a) * len(b))


def auc_ci(a, b, draws=2000, seed=1):
    if len(a) < 3 or len(b) < 3:
        return None, None
    rng = random.Random(seed)
    vals = sorted(auc([rng.choice(a) for _ in a], [rng.choice(b) for _ in b]) for _ in range(draws))
    return vals[int(0.025 * draws)], vals[int(0.975 * draws) - 1]


def source_history(events):
    """events: [(uid, mint, side, received_at_epoch)] sorted by time -> per uid list of
    (first_buy_t, first_sell_t, mint) round trips."""
    first_buy, first_sell = {}, {}
    for uid, mint, side, t in events:
        key = (uid, mint)
        if side == "buy":
            first_buy.setdefault(key, t)
        elif side == "sell" and key in first_buy:
            first_sell.setdefault(key, t)
    trips = collections.defaultdict(list)
    for key, tb in first_buy.items():
        if key in first_sell:
            trips[key[0]].append((tb, first_sell[key], key[1]))
    return trips


def load(since, census_files):
    from sniper.persistence.db_memecoin import event_transaction_signature
    from venue_census import coin_venue
    senders, owners = {}, {}
    for f in census_files:
        d = json.loads(Path(f).read_text())
        senders.update(d.get("senders") or {})
        owners.update(d.get("owners") or {})
    with contextlib.closing(sqlite3.connect("file:%s?mode=ro" % LEDGER, uri=True, timeout=30)) as c:
        c.row_factory = sqlite3.Row
        pos = c.execute(
            "SELECT id, source_user_id, source_handle, mint, entry_mcap, entry_liquidity, their_size_usd, "
            "entry_token_age_s, entry_price, their_price, opened_at, exit_reason, realized_pnl, origin_event_id, "
            "attempt_id FROM memecoin_positions WHERE execution_mode='real' AND network_id='solana' AND is_open=0 "
            "AND opened_at >= ? AND COALESCE(exit_reason,'') <> 'manual'", (since,)).fetchall()
        armed = {r[0] for r in c.execute("SELECT source_user_id FROM memecoin_wallets WHERE real_approved=1 "
                                          "AND COALESCE(real_chains,'') LIKE '%solana%'")}
        lo = (datetime.fromisoformat(since.replace("Z", "+00:00")) - timedelta(days=14)).isoformat()
        evs = [(u, m, s, ts(t)) for u, m, s, t in c.execute(
            "SELECT source_user_id, mint, side, received_at FROM memecoin_events WHERE network_id='solana' "
            "AND received_at >= ? ORDER BY received_at", (lo,))]
        by_mint = collections.defaultdict(list)
        for u, m, s, t in evs:
            if s == "buy":
                by_mint[m].append((t, u))
        trips = source_history(evs)
        rows = []
        for p in pos:
            t0 = ts(p["opened_at"])
            depth = p["entry_liquidity"] / 2.0 if p["entry_liquidity"] else None
            a = c.execute("SELECT trace_json, classification_json FROM memecoin_entry_attempts WHERE attempt_id=?",
                          (p["attempt_id"],)).fetchone() if p["attempt_id"] else None
            flow, facts = {}, {}
            if a:
                for e in (json.loads(a["trace_json"] or "{}").get("substage_events") or []):
                    if e.get("name") == "pre_entry_flow":
                        flow = e
                try:
                    facts = (json.loads(a["classification_json"] or "{}") or {}).get("facts") or {}
                except ValueError:
                    facts = {}
            holds = [ts2 - ts1 for ts1, ts2, m in trips.get(p["source_user_id"], ()) if m != p["mint"] and ts2 < t0
                     and ts1 >= t0 - 14 * 86400]
            prior = [t for t, u in by_mint.get(p["mint"], ()) if u == p["source_user_id"] and t < t0 - 60]
            others = {u for t, u in by_mint.get(p["mint"], ()) if u != p["source_user_id"] and u in armed
                      and t0 - 86400 <= t < t0}
            bn, sn = flow.get("buy_n"), flow.get("sell_n")
            sig = event_transaction_signature(p["origin_event_id"]) if p["origin_event_id"] else None
            if p["origin_event_id"] and str(p["origin_event_id"]).startswith("slotlane:"):
                sig = p["origin_event_id"].split(":", 1)[1]
            rows.append({
                "id": p["id"], "handle": p["source_handle"], "sold": p["exit_reason"] in MIRROR, "pnl": p["realized_pnl"],
                "mcap_usd": p["entry_mcap"], "depth_quote_usd": depth, "source_size_usd": p["their_size_usd"],
                "size_over_depth": (p["their_size_usd"] / depth) if depth and p["their_size_usd"] else None,
                "coin_age_s": p["entry_token_age_s"],
                "entry_premium_pct": ((p["entry_price"] / p["their_price"] - 1.0) * 100.0
                                      if p["entry_price"] and p["their_price"] else None),
                "hour_utc": datetime.fromtimestamp(t0, timezone.utc).hour,
                "src_prior_hold_s": statistics.median(holds) if holds else None,
                "src_bought_before": 1.0 if prior else 0.0,
                "other_armed_n": float(len(others)),
                "flow_swaps_n": flow.get("swaps_n"), "flow_buy_n": bn, "flow_sell_n": sn,
                "flow_buy_share": (bn / (bn + sn)) if bn is not None and sn is not None and (bn + sn) else None,
                "flow_gross_quote": flow.get("gross_quote"),
                "regime_buyers": facts.get("regime_current_buyers"),
                "venue": coin_venue(senders[sig], owners) if sig and sig in senders else "unread"})
    wallets = {r["handle"] for r in rows if r["sold"]}
    return [r for r in rows if r["handle"] in wallets]


TRAITS = ["mcap_usd", "depth_quote_usd", "source_size_usd", "size_over_depth", "coin_age_s", "entry_premium_pct",
          "hour_utc", "src_prior_hold_s", "src_bought_before", "other_armed_n", "flow_swaps_n", "flow_buy_n",
          "flow_sell_n", "flow_buy_share", "flow_gross_quote", "regime_buyers"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=(datetime.now(timezone.utc) - timedelta(days=14)).isoformat())
    ap.add_argument("--census", action="append", default=[])
    ap.add_argument("--flow", help="pre_entry_flow_chain.py output: chain m5 flow before each entry")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    rows = load(a.since, a.census)
    traits = list(TRAITS)
    if a.flow:
        fl = {j["id"]: j for j in (json.loads(l) for l in Path(a.flow).read_text().splitlines())}
        for r in rows:
            f = fl.get(r["id"]) or {}
            ok = bool(f) and f.get("complete")
            r["chain_m5_buys_n"] = f.get("buys_n") if ok else None
            r["chain_m5_sells_n"] = f.get("sells_n") if ok else None
            r["chain_m5_buy_sol"] = f.get("buy_sol") if ok else None
            r["chain_m5_sell_sol"] = f.get("sell_sol") if ok else None
            tot = (f.get("buy_sol") or 0) + (f.get("sell_sol") or 0)
            r["chain_m5_buy_share_sol"] = (f["buy_sol"] / tot) if ok and tot else None
        traits += ["chain_m5_buys_n", "chain_m5_sells_n", "chain_m5_buy_sol", "chain_m5_sell_sol", "chain_m5_buy_share_sol"]
    s = [r for r in rows if r["sold"]]
    k = [r for r in rows if not r["sold"]]
    print("EXPLORATORY. Solana funded closes since %s on the %d wallets with >= 1 sold-into close: sold-into n=%d "
          "(P&L $%+.2f), control n=%d (P&L $%+.2f)" % (a.since[:19], len({r["handle"] for r in s}), len(s),
                                                       sum(r["pnl"] or 0 for r in s), len(k), sum(r["pnl"] or 0 for r in k)))
    print("%-18s %14s %14s %6s %6s  %s" % ("trait", "sold-into med", "control med", "n_s", "n_c", "AUC [95% CI]"))
    for t in traits:
        a_ = [r[t] for r in s if r[t] is not None]
        b_ = [r[t] for r in k if r[t] is not None]
        au = auc(a_, b_)
        lo, hi = auc_ci(a_, b_)
        flag = "  FLAG" if lo is not None and (lo > 0.6 or hi < 0.4) else ""
        print("%-18s %14s %14s %6d %6d  %s%s" % (
            t, "%.4g" % statistics.median(a_) if a_ else "-", "%.4g" % statistics.median(b_) if b_ else "-",
            len(a_), len(b_), ("%.2f [%.2f, %.2f]" % (au, lo, hi)) if lo is not None else ("%.2f" % au if au else "-"), flag))
    for label, grp in (("sold-into", s), ("control", k)):
        print("venue %-10s %s" % (label, dict(collections.Counter(r["venue"] for r in grp).most_common())))


def selftest():
    assert auc([3, 4], [1, 2]) == 1.0 and auc([1, 2], [3, 4]) == 0.0 and auc([1], [1]) == 0.5
    lo, hi = auc_ci([5, 6, 7, 8], [1, 2, 3, 4], draws=200)
    assert lo > 0.9, (lo, hi)
    trips = source_history([("u", "A", "buy", 0), ("u", "A", "sell", 30), ("u", "B", "buy", 40), ("u", "A", "sell", 99)])
    assert trips["u"] == [(0, 30, "A")]          # first sell only; B never sold
    print("selftest ok")


if __name__ == "__main__":
    main()
