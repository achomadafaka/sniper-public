#!/usr/bin/env python3
"""Was the market alive when we entered? Chain activity around a funded Solana copy.

Operator question 2026-09-27: were the losing entries dead or dying markets where we were the exit
liquidity? The slot lane runs no volume/activity gate, and the Solana nano/micro m5/h1 floors are
off on the J7/onchain path too. Per position, from the chain only (research key; never our marks):
  - the 5 min BEFORE the source's buy: txs on the mint, decoded trades, unique traders, SOL volume
  - the curve at our fill (virtual/real SOL reserves) or the pool's wSOL depth
  - coin age at our fill (the mint's oldest signature, when reachable)
  - our buy as a share of that 5-min volume
  - the same activity in the 5 min AFTER our fill
Counts are exact (signature lists); traders and volume come from decoded trades, sampled evenly
beyond CAP per window and marked so ('decoded' < 'txs'). Research key, bounded 429 backoff.

    python3 scripts/research/market_activity_at_entry.py 65860 65813 ...
    python3 scripts/research/market_activity_at_entry.py --selftest
"""
import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "research"))
import funded_exit_truth as fet  # noqa: E402
import dump_forensics as dfx  # noqa: E402

fet.SPACING_S = 0.35     # set while runner_scanner was paused (2026-09-27); raise it when the key is shared
CAP = 40                 # decoded trades per window
WINDOW_S = 300


def sig_page(address, **opts):
    page = fet.rpc("getSignaturesForAddress", [address, {"limit": 1000, "commitment": "confirmed", **opts}]) or []
    time.sleep(1.5)
    return page


def window_before(mint, anchor_sig, t_anchor, span=WINDOW_S, age_pages=4):
    """(sigs within span before the anchor, oldest sig time seen, reached_creation)."""
    out, cursor, oldest, reached = [], anchor_sig, None, False
    for _ in range(age_pages + 1):
        page = sig_page(mint, before=cursor)
        if not page:
            reached = True
            break
        out.extend(s for s in page if s.get("err") is None and (s.get("blockTime") or 0) >= t_anchor - span)
        oldest = page[-1].get("blockTime")
        if len(page) < 1000:
            reached = True
            break
        cursor = page[-1]["signature"]
    return out, oldest, reached


def window_after(mint, anchor_sig, t_anchor, span=WINDOW_S, max_pages=4):
    """Sigs within span after the anchor. Pages newest-first down to the anchor; None if too many."""
    out, cursor = [], None
    for _ in range(max_pages):
        page = sig_page(mint, until=anchor_sig, **({"before": cursor} if cursor else {}))
        out.extend(page)
        if len(page) < 1000:
            return [s for s in out if s.get("err") is None and (s.get("blockTime") or 0) <= t_anchor + span]
        cursor = page[-1]["signature"]
    return None


def activity(sigs, mint, sol_usd):
    if sigs is None:
        return {"txs": None}
    step = max(1, len(sigs) // CAP)
    sample = sigs[::step]
    trades = [t for t in (dfx.trade_of(dfx.get_tx(s["signature"]), mint, sol_usd) for s in sample) if t]
    scale = len(sigs) / len(sample) if sample else 0
    return {"txs": len(sigs), "decoded": len(trades), "sampled": step > 1,
            "unique_traders": len({t["trader"] for t in trades}),
            "buys": sum(t["side"] == "buy" for t in trades), "sells": sum(t["side"] == "sell" for t in trades),
            "sol_volume": round(sum(t["sol"] for t in trades) * scale, 2)}


def analyse(p, sol_usd):
    mint = p["mint"]
    t_fill = fet.epoch(p["opened_recorded_at"])
    fill_tx = dfx.get_tx(p["entry_sig"]) if p["entry_sig"] else None
    src_tx = dfx.get_tx(p["source_sig"]) if p["source_sig"] else None
    t_src = (src_tx or {}).get("blockTime") or (t_fill - 2)
    anchor_before = p["source_sig"] or p["entry_sig"]
    pre, oldest, reached = window_before(mint, anchor_before, t_src)
    post = window_after(mint, p["entry_sig"], (fill_tx or {}).get("blockTime") or t_fill)
    ours = dfx.trade_of(fill_tx, mint, sol_usd) if fill_tx else None
    curve = {}
    if fill_tx:
        from sniper.execution.pump_curve_buy import parse_trade_event
        from sniper.research.memecoin_opportunity.decoders import anchor_event_bytes
        from sniper.services.memecoin_opportunity_live import program_data_logs
        for _i, line in program_data_logs(fill_tx["meta"].get("logMessages") or [], fet.PUMP):
            try:
                if anchor_event_bytes(line)[:8] == fet.TRADE_DISC:
                    e = parse_trade_event(line)
                    if e.get("mint") == mint:
                        curve = {"virtual_sol": round(e["virtual_sol_reserves"] / 1e9, 2),
                                 "real_sol": round(e["real_sol_reserves"] / 1e9, 2)}
            except Exception:
                continue
    a_pre, a_post = activity(pre, mint, sol_usd), activity(post, mint, sol_usd)
    our_sol = ours["sol"] if ours else float(p["entry_notional"]) / sol_usd
    return {"id": p["id"], "handle": p["handle"] or p["source_handle"], "mint": mint[:8],
            "exit": p["exit_reason"], "pnl": round(float(p["realized_pnl"] or 0), 2),
            "pre_5m": a_pre, "post_5m": a_post, "curve_at_fill": curve or None,
            "coin_age_s": round(t_fill - oldest) if (oldest and reached) else None,
            "coin_age_at_least_s": round(t_fill - oldest) if (oldest and not reached) else None,
            "our_sol": round(our_sol, 3),
            "our_share_of_pre_5m_volume_pct": (round(our_sol / a_pre["sol_volume"] * 100)
                                               if a_pre.get("sol_volume") else None)}


def selftest():
    assert activity(None, "M", 1.0) == {"txs": None}
    assert activity([], "M", 1.0)["txs"] == 0
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*", type=int)
    ap.add_argument("--sol-usd", type=float, default=121.0)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return
    for p in dfx.load(args.ids):
        try:
            print(json.dumps(analyse(p, args.sol_usd)), flush=True)
        except fet.Stop as exc:
            print(json.dumps({"id": p["id"], "STOPPED": str(exc)}), flush=True)
            return


if __name__ == "__main__":
    main()
