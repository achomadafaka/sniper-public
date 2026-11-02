#!/usr/bin/env python3
"""Trading-setup inventory, STEP 1 (operator question via Alpha, 2026-09-30): what pre-signal
price/volume path we hold per decision, by chain, and how many decisions each proposed setup
covers. NO OUTCOME IS READ: realized_pnl, exit prices and exit reasons are never selected, so the
grading can be pre-registered (Gamma) before anything is joined.

Decision set: funded closes (execution_mode='real', closed, booked) keyed by their signal event
(origin_event_id -> memecoin_events.fomo_event_id), t = the signal event's created_at.

Pre-signal sources, executable or chain data only (never DexScreener/GT):
  feed   memecoin_events on the same (chain, mint) strictly before t: tracked wallets' own fills
         (their_price, their_size_usd, side). Irregular resolution, biased to wallets we track.
  quotes executable quotes on the same (chain, mint) before t: refusal captures from a route
         provider (never provider='dexscreener') and paper entry quotes (route_quote, paper_quote,
         funded_quote_reuse).
  venue  the coin's venue at entry: EVM route protocol (evm_route_json), Solana pool venue_kind
         (sol_position_pools, resolved from the fill transaction).
  entry  data/memecoin_entry_features.db flow/pre-source fields (evm_tape / helius_gtfa), if any.
Stored tapes with no pre-signal window are listed in the report, not read here.

    python3 scripts/research/setup_inventory.py --out <dir>
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOOKBACKS = (60, 300, 3600)
UNIT_RATIO_MAX = 50.0            # a within-coin price ratio above this is a unit mix, not a move
CURVE_PROTOCOLS = {"launchpad_curve", "flap_curve", "pons_v2"}

# Proposed thresholds (Gamma pre-registers or replaces them before any outcome is joined).
T = {"vertical_5m": 1.50, "dump_off_high_1h": 0.70, "dip_lo": 0.70, "dip_hi": 0.90,
     "burst_wallets_60s": 3, "new_high_min_prints": 2}


def ts(v):
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()


def decisions(c):
    rows = c.execute(
        "SELECT p.id, p.network_id, p.mint, p.origin_event_id, p.attempt_id, "
        "json_extract(p.evm_route_json, '$.protocol'), e.created_at, e.their_price, e.side "
        "FROM memecoin_positions p LEFT JOIN memecoin_events e ON e.fomo_event_id = p.origin_event_id "
        "WHERE p.execution_mode='real' AND p.is_open=0 AND p.realized_pnl IS NOT NULL "
        "ORDER BY p.id").fetchall()
    return [{"id": r[0], "chain": str(r[1]), "mint": r[2], "origin": r[3], "attempt": r[4],
             "protocol": r[5], "t": ts(r[6]) if r[6] else None, "p_src": r[7], "src_side": r[8]}
            for r in rows]


def feed_by_coin(c, coins):
    out = defaultdict(list)                        # (chain, mint) -> sorted [(t, price, side, uid, id)]
    mints = sorted({m for _, m in coins})
    for i in range(0, len(mints), 900):
        chunk = mints[i:i + 900]
        for fid, uid, chain, mint, created, price, side in c.execute(
                "SELECT fomo_event_id, source_user_id, network_id, mint, created_at, their_price, side "
                f"FROM memecoin_events WHERE mint IN ({','.join('?' * len(chunk))})", chunk):
            if (str(chain), mint) in coins and created:
                out[(str(chain), mint)].append((ts(created), price, side, uid, fid))
    for v in out.values():
        v.sort()
    return out


def quotes_by_coin(c, refusal_db, coins):
    out = defaultdict(list)                        # (chain, mint) -> sorted [t]
    if refusal_db.exists():
        r = sqlite3.connect(f"file:{refusal_db}?mode=ro", uri=True, timeout=10)
        for chain, mint, obs in r.execute(
                "SELECT network_id, mint, observed_at FROM memecoin_refusal_captures "
                "WHERE provider IS NOT NULL AND provider != 'dexscreener' AND quote_price > 0"):
            if (str(chain), mint) in coins:
                out[(str(chain), mint)].append(ts(obs))
    for chain, mint, opened in c.execute(
            "SELECT network_id, mint, opened_at FROM memecoin_positions WHERE execution_mode='paper' "
            "AND entry_fill_source IN ('route_quote','paper_quote','funded_quote_reuse')"):
        if (str(chain), mint) in coins:
            out[(str(chain), mint)].append(ts(opened))
    for v in out.values():
        v.sort()
    return out


def sol_venue(sol_db):
    if not sol_db.exists():
        return {}
    s = sqlite3.connect(f"file:{sol_db}?mode=ro", uri=True, timeout=10)
    return {pid: kind for pid, kind in s.execute("SELECT position_id, venue_kind FROM sol_position_pools")}


def entry_features(ef_db):
    if not ef_db.exists():
        return {}
    e = sqlite3.connect(f"file:{ef_db}?mode=ro", uri=True, timeout=10)
    out = {}
    for aid, cj in e.execute("SELECT attempt_id, coverage_json FROM entry_features"):
        cov = json.loads(cj or "{}")
        out[aid] = cov.get("pre_source_300s") == "ok"
    return out


def path_features(d, feed):
    """Features from tracked wallets' fills strictly before the signal; never the signal itself."""
    prints = [p for p in feed.get((d["chain"], d["mint"]), []) if p[0] < d["t"] and p[4] != d["origin"]]
    f = {"feed_age_s": (d["t"] - prints[0][0]) if prints else None}
    for lb in LOOKBACKS:
        win = [p for p in prints if p[0] >= d["t"] - lb]
        f[f"n_{lb}"] = len(win)
        f[f"wallets_buy_{lb}"] = len({p[3] for p in win if p[2] == "buy"})
        f[f"sells_{lb}"] = sum(1 for p in win if p[2] == "sell")
    p_src = d["p_src"]
    priced = lambda lb: [p[1] for p in prints if p[0] >= d["t"] - lb and p[1] and p[1] > 0]
    unit_bad = False
    if p_src and p_src > 0:
        hi1 = max(priced(3600), default=None)
        lo5 = min(priced(300), default=None)
        if hi1 and max(hi1 / p_src, p_src / hi1) > UNIT_RATIO_MAX or lo5 and max(lo5 / p_src, p_src / lo5) > UNIT_RATIO_MAX:
            unit_bad = True
        f["src_over_high_1h"] = None if unit_bad or not hi1 else p_src / hi1
        f["src_over_low_5m"] = None if unit_bad or not lo5 else p_src / lo5
        f["priced_1h"] = len(priced(3600))
    else:
        f["src_over_high_1h"] = f["src_over_low_5m"] = None
        f["priced_1h"] = 0
    f["unit_mix"] = unit_bad
    return f


def lifecycle(d, venue):
    if d["chain"] == "solana":
        k = venue.get(d["id"])
        return {"pump_curve": "pre_graduation", "amm": "post_graduation"}.get(k, "unknown")
    if d["protocol"] in CURVE_PROTOCOLS:
        return "pre_graduation"
    return "post_graduation" if d["protocol"] else "unknown"


def setup(f):
    """Priority order; first match wins. Every rule reads only prints strictly before the signal."""
    if f["n_3600"] == 0:
        return "S1_first_touch"                      # no tracked fill on the coin in the prior hour
    r_hi, r_lo = f["src_over_high_1h"], f["src_over_low_5m"]
    if r_hi is not None and r_hi <= T["dump_off_high_1h"] and f["sells_3600"] >= 1:
        return "S2_reentry_after_dump"               # >= 30% under the 1 h high, tracked sells seen
    if r_lo is not None and r_lo >= T["vertical_5m"]:
        return "S3_chase_vertical"                   # >= +50% over the 5 m low
    if r_hi is not None and r_hi > 1.0 and f["priced_1h"] >= T["new_high_min_prints"]:
        return "S4_breakout_new_high"                # above every tracked print of the hour
    if r_hi is not None and T["dip_lo"] < r_hi <= T["dip_hi"]:
        return "S5_dip_buy"                          # 10-30% under the 1 h high
    if f["wallets_buy_60"] >= T["burst_wallets_60s"]:
        return "S6_feed_burst"                       # >= 3 tracked wallets bought in the prior 60 s
    if r_hi is None:
        return "S8_unpriced_path"                    # prior prints, none usable for price
    return "S7_near_high_no_trigger"                 # priced path, 0.90 < src/high <= 1.0, no burst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "sniper_tracker.db"))
    ap.add_argument("--data", default=str(ROOT / "data"))
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    c = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True, timeout=30)
    data = Path(a.data)
    ds = decisions(c)
    coins = {(d["chain"], d["mint"]) for d in ds}
    feed = feed_by_coin(c, coins)
    quotes = quotes_by_coin(c, data / "memecoin_refusal_captures.db", coins)
    venue = sol_venue(data / "memecoin_sol_tape.db")
    ef = entry_features(data / "memecoin_entry_features.db")

    cov = defaultdict(Counter)
    setups = defaultdict(Counter)
    life = defaultdict(Counter)
    cross = defaultdict(Counter)
    rows = []
    for d in ds:
        ch = d["chain"]
        cov[ch]["decisions"] += 1
        if d["t"] is None:
            cov[ch]["no_signal_event"] += 1
            setups[ch]["S0_no_signal_event"] += 1
            continue
        f = path_features(d, feed)
        q = quotes.get((ch, d["mint"]), [])
        for lb in LOOKBACKS:
            cov[ch][f"feed_any_{lb}s"] += f[f"n_{lb}"] > 0
            cov[ch][f"feed_3plus_{lb}s"] += f[f"n_{lb}"] >= 3
            k = bisect_left(q, d["t"] - lb)
            cov[ch][f"exec_quote_{lb}s"] += k < len(q) and q[k] < d["t"]
        cov[ch]["unit_mix"] += f["unit_mix"]
        cov[ch]["entry_features_pre_source_ok"] += bool(ef.get(d["attempt"]))
        lc = lifecycle(d, venue)
        cov[ch]["venue_known"] += lc != "unknown"
        s = setup(f)
        setups[ch][s] += 1
        life[ch][lc] += 1
        cross[ch][f"{lc}|{s}"] += 1
        rows.append({"position_id": d["id"], "chain": ch, "setup": s, "lifecycle": lc,
                     **{k: v for k, v in f.items()}})
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    res = {"generated_at": datetime.now(timezone.utc).isoformat(), "thresholds": T,
           "coverage": {k: dict(v) for k, v in cov.items()},
           "setups": {k: dict(v) for k, v in setups.items()},
           "lifecycle": {k: dict(v) for k, v in life.items()},
           "lifecycle_x_setup": {k: dict(v) for k, v in cross.items()}}
    (out / "inventory.json").write_text(json.dumps(res, indent=1))
    (out / "decisions.json").write_text(json.dumps(rows))
    print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
