#!/usr/bin/env python3
"""Where 4663's funded loss comes from: the wallets, the route cost, or the exits (Beta, 2026-09-30).

Same population as scripts/research/entry_side_money_map.py (funded fills opened on or after
2026-09-13, closed, manual entries excluded); 4663 is compared against Solana. Read-only on the
ledger, indexed by position id, no network call.

Costs, per fill:
  entry route cost  fill price above the exact entry quote (execution_slippage_pct), in USD as
                    notional * x / (1 + x): the tokens the fill did not buy at the quote.
  exit route cost   realised exit proceeds (entry_notional + realized_pnl) minus the last exact
                    whole-balance quote the exit rail took (memecoin_route_series, seam
                    'exit_decision') within 10 s before the close.
  gas               NOT in realized_pnl on EVM (realized_pnl = exit notional - entry notional) and
                    not measured here.

    python3 scripts/research/chain_4663_loss_split.py
"""
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SINCE = "2026-09-13T00:00:00+00:00"
UNTIL = "2026-09-29T22:00:00+00:00"          # pinned so the numbers in the doc reproduce
EXIT_QUOTE_WINDOW_S = 10.0


def _ts(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def load(conn):
    rows = [dict(r) for r in conn.execute(
        "SELECT id, network_id, signal_source, source_user_id, source_handle, entry_notional, "
        "opened_at, closed_at, realized_pnl, exit_reason, evm_route_json, attempt_id "
        "FROM memecoin_positions WHERE execution_mode='real' AND opened_at >= ? AND opened_at < ? "
        "AND closed_at IS NOT NULL AND realized_pnl IS NOT NULL "
        "AND COALESCE(signal_source,'') != 'manual' ORDER BY opened_at", (SINCE, UNTIL))
        if r["network_id"] in ("4663", "solana")]
    for r in rows:
        a = conn.execute("SELECT execution_slippage_pct FROM memecoin_entry_attempts "
                         "WHERE attempt_id=?", (r["attempt_id"],)).fetchone()
        x = a[0] if a else None
        n = float(r["entry_notional"])
        r["pnl"] = float(r["realized_pnl"])
        r["roi"] = r["pnl"] / n if n > 0 else None
        r["entry_cost_usd"] = (n * (x / 100.0) / (1 + x / 100.0)
                               if x is not None and x > -100 else None)
        closed = _ts(r["closed_at"])
        lo = datetime.fromtimestamp(closed.timestamp() - EXIT_QUOTE_WINDOW_S, closed.tzinfo)
        q = conn.execute("SELECT expected_usdc FROM memecoin_route_series WHERE position_id=? "
                         "AND seam='exit_decision' AND observed_at >= ? AND observed_at <= ? "
                         "ORDER BY observed_at DESC LIMIT 1",
                         (r["id"], lo.isoformat(), r["closed_at"])).fetchone()
        r["exit_cost_usd"] = (float(q[0]) - (n + r["pnl"])) if q and q[0] else None
        route = json.loads(r["evm_route_json"] or "{}")
        r["route"] = route.get("protocol") or ("none" if r["network_id"] == "4663" else "solana")
    return rows


def agg(rs):
    pnl = [r["pnl"] for r in rs]
    ec = [r["entry_cost_usd"] for r in rs if r["entry_cost_usd"] is not None]
    xc = [r["exit_cost_usd"] for r in rs if r["exit_cost_usd"] is not None]
    notional = sum(float(r["entry_notional"]) for r in rs)
    return {"n": len(rs), "pnl": round(sum(pnl), 2), "notional": round(notional, 2),
            "pnl_pct_of_notional": round(100 * sum(pnl) / notional, 2) if notional else None,
            "win_pct": round(100 * sum(p > 0 for p in pnl) / len(pnl), 1) if pnl else None,
            "entry_cost_usd": [round(sum(ec), 2), len(ec)],
            "entry_cost_pct_of_notional": round(100 * sum(ec) / notional, 2) if notional else None,
            "exit_cost_usd": [round(sum(xc), 2), len(xc)],
            "exit_cost_median_usd": round(statistics.median(xc), 3) if xc else None}


def main():
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_path, state_root
    load_env_files(str(state_root()))
    from sniper.persistence.db_core import get_readonly_connection
    conn = get_readonly_connection(str(state_path("sniper_tracker.db")))
    rows = load(conn)
    armed = {r[0] for r in conn.execute(
        "SELECT source_user_id FROM memecoin_wallets WHERE real_approved=1 "
        "AND (',' || COALESCE(real_chains,'') || ',') LIKE '%,4663,%'")}
    evm = [r for r in rows if r["network_id"] == "4663"]
    sol = [r for r in rows if r["network_id"] == "solana"]
    out = {"chain": {"4663": agg(evm), "solana": agg(sol)},
           "4663_by_armed_now": {"armed": agg([r for r in evm if r["source_user_id"] in armed]),
                                 "not_armed": agg([r for r in evm
                                                   if r["source_user_id"] not in armed])}}
    by_route = defaultdict(list)
    for r in evm:
        by_route[r["route"]].append(r)
    out["4663_by_route"] = {k: agg(v) for k, v in sorted(by_route.items(), key=lambda kv: -len(kv[1]))}
    out["4663_by_route_and_armed"] = {
        f"{k}|{'armed' if a else 'not_armed'}": agg([r for r in v if (r["source_user_id"] in armed) == a])
        for k, v in by_route.items() for a in (True, False)
        if any((r["source_user_id"] in armed) == a for r in v)}
    by_wallet = defaultdict(list)
    for r in evm:
        by_wallet[r["source_handle"]].append(r)
    out["4663_by_wallet"] = {k: {**agg(v), "armed_now": v[0]["source_user_id"] in armed}
                             for k, v in sorted(by_wallet.items(),
                                                key=lambda kv: sum(x["pnl"] for x in kv[1]))}
    rails = {}
    for chain, rs in (("4663", evm), ("solana", sol)):
        d = defaultdict(list)
        for r in rs:
            d[r["exit_reason"]].append(r)
        rails[chain] = {k: {"n": len(v), "share": round(len(v) / len(rs), 3),
                            "pnl": round(sum(x["pnl"] for x in v), 2),
                            "mean_roi_pct": round(100 * statistics.fmean(x["roi"] for x in v), 1)}
                        for k, v in sorted(d.items(), key=lambda kv: -len(kv[1]))}
    out["rails"] = rails
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
