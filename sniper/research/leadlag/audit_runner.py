"""Measurement Audit v1 — DB driver. Reconstructs the bbo event stream from hlm_raw_events
and horse-races predictors under DUAL marking: forward microprice (lead structure) and
forward EXECUTABLE return (cost-clearing, buy-ask/sell-bid + fee). Free lead-lag reads:
mark-oracle basis and BTC->alt. All stats clustered by (asset, 60s block). Read-only.
"""
from __future__ import annotations

import json
import sqlite3
from bisect import bisect_right
from collections import defaultdict, deque

from sniper.research.leadlag.measurement_audit import (
    executable_return, microprice, ofi_step, signed_microprice_return,
)

HORIZON_EVENTS = 10
TRADE_WINDOW_MS = 5_000
OFI_WINDOW = 5
FEE_RATE = 0.00045
CLUSTER_MS = 60_000
BTC_LEAD_MS = 500


def build_measurement_audit(conn: sqlite3.Connection, *, max_events_per_asset: int | None = None) -> dict:
    bbo = _bbo_by_asset(conn, max_events_per_asset)
    trades = _trades_by_asset(conn)
    ctx = _ctx_by_asset(conn)
    # Precompute BTC microprice series for O(log n) lead-lag lookups.
    btc = bbo.get("BTC") or []
    btc_ts = [e["ex"] for e in btc]
    btc_mp = [microprice(e["bid"], e["ask"], e["bsz"], e["asz"]) for e in btc]

    horse = defaultdict(lambda: _Acc())          # (predictor) -> Acc  (pooled)
    horse_by_asset = defaultdict(lambda: _Acc())  # (predictor, asset)
    basis_acc = _Acc()
    btc_lead_acc = _Acc()
    control_acc = _Acc()
    sample = {"assets": 0, "bbo_events": 0, "labeled": 0}

    for asset, events in bbo.items():
        if len(events) <= HORIZON_EVENTS + OFI_WINDOW:
            continue
        sample["assets"] += 1
        sample["bbo_events"] += len(events)
        tdq = deque()
        tlist = trades.get(asset) or []
        ti = 0
        ofi_hist = deque(maxlen=OFI_WINDOW)
        cl = ctx.get(asset) or []
        cl_ts = [c["rx"] for c in cl]
        cl_basis = [c["basis"] for c in cl]
        for i in range(OFI_WINDOW, len(events) - HORIZON_EVENTS):
            e = events[i]
            fut = events[i + HORIZON_EVENTS]
            # trailing trade-flow imbalance (by exchange ts)
            t_now = e["ex"]
            while ti < len(tlist) and tlist[ti]["ex"] <= t_now:
                tdq.append(tlist[ti]); ti += 1
            while tdq and tdq[0]["ex"] < t_now - TRADE_WINDOW_MS:
                tdq.popleft()
            buy = sum(t["notional"] for t in tdq if t["buy"])
            sell = sum(t["notional"] for t in tdq if not t["buy"])
            tot = buy + sell
            trade_imb = ((buy - sell) / tot) if tot > 0 else 0.0
            # book / microprice / ofi predictors
            mp = microprice(e["bid"], e["ask"], e["bsz"], e["asz"])
            mid = (e["bid"] + e["ask"]) / 2.0
            book_imb = ((e["bsz"] - e["asz"]) / (e["bsz"] + e["asz"])) if (e["bsz"] + e["asz"]) > 0 else 0.0
            ofi_hist.append(ofi_step(events[i - 1]["snap"], e["snap"]))
            ofi_roll = sum(x for x in ofi_hist if x is not None)
            micro_dev = (mp - mid) if mp is not None else 0.0
            fut_mp = microprice(fut["bid"], fut["ask"], fut["bsz"], fut["asz"])
            preds = {
                "trade_imbalance": trade_imb,
                "static_book_imbalance": book_imb,
                "true_ofi": ofi_roll,
                "microprice_deviation": micro_dev,
            }
            labeled = False
            block = (asset, e["ex"] // CLUSTER_MS)
            for name, val in preds.items():
                side = "long" if val > 0 else ("short" if val < 0 else None)
                if side is None:
                    continue
                lead = signed_microprice_return(mp, fut_mp, side) if (mp and fut_mp) else None
                cost = executable_return(side, e["bid"], e["ask"], fut["bid"], fut["ask"], FEE_RATE)
                if lead is None or cost is None:
                    continue
                horse[name].add(block, lead, cost)
                horse_by_asset[(name, asset)].add(block, lead, cost)
                labeled = True
                # shuffled-sign negative control (deterministic by index parity)
                if name == "true_ofi":
                    cside = side if (i % 2) else ("short" if side == "long" else "long")
                    clead = signed_microprice_return(mp, fut_mp, cside)
                    ccost = executable_return(cside, e["bid"], e["ask"], fut["bid"], fut["ask"], FEE_RATE)
                    if clead is not None and ccost is not None:
                        control_acc.add(block, clead, ccost)
            # free read: mark-oracle basis (ctx is receive-time only -> align on event receive ts)
            basis = _basis_at(cl_ts, cl_basis, e["rx"])
            if basis is not None and basis != 0.0:
                bside = "short" if basis > 0 else "long"  # mark above oracle -> expect revert down
                lead = signed_microprice_return(mp, fut_mp, bside) if (mp and fut_mp) else None
                cost = executable_return(bside, e["bid"], e["ask"], fut["bid"], fut["ask"], FEE_RATE)
                if lead is not None and cost is not None:
                    basis_acc.add(block, lead, cost)
            # free read: BTC -> alt lead-lag (both on exchange clock)
            if asset != "BTC" and btc_ts:
                bret = _btc_lead_return(btc_ts, btc_mp, t_now, BTC_LEAD_MS)
                if bret is not None and bret != 0.0:
                    aside = "long" if bret > 0 else "short"
                    lead = signed_microprice_return(mp, fut_mp, aside) if (mp and fut_mp) else None
                    cost = executable_return(aside, e["bid"], e["ask"], fut["bid"], fut["ask"], FEE_RATE)
                    if lead is not None and cost is not None:
                        btc_lead_acc.add(block, lead, cost)
            if labeled:
                sample["labeled"] += 1

    return {
        "sample": sample,
        "horizon_events": HORIZON_EVENTS,
        "fee_rate": FEE_RATE,
        "horse_race": {k: horse[k].summary() for k in horse},
        "horse_race_by_asset": {f"{n}|{a}": acc.summary() for (n, a), acc in horse_by_asset.items()},
        "free_leadlag": {
            "mark_oracle_basis": basis_acc.summary(),
            "btc_to_alt": btc_lead_acc.summary(),
        },
        "negative_control_shuffled_ofi": control_acc.summary(),
    }


class _Acc:
    def __init__(self):
        self.n = 0
        self.lead_correct = 0
        self.cost_sum = 0.0
        self.cost_wins = 0
        self.clusters = defaultdict(lambda: [0.0, 0])  # block -> [cost_sum, count]

    def add(self, block, lead, cost):
        self.n += 1
        self.lead_correct += 1 if lead > 0 else 0
        self.cost_sum += cost
        self.cost_wins += 1 if cost > 0 else 0
        c = self.clusters[block]
        c[0] += cost; c[1] += 1

    def summary(self):
        if self.n == 0:
            return {"n": 0}
        cluster_means = [s / c for s, c in self.clusters.values() if c]
        return {
            "n": self.n,
            "effective_clusters": len(cluster_means),
            "lead_dir_accuracy": round(self.lead_correct / self.n, 4),
            "cost_net_bps": round(10000 * self.cost_sum / self.n, 3),
            "cost_win_rate": round(self.cost_wins / self.n, 4),
            "cost_cluster_mean_bps": round(10000 * sum(cluster_means) / len(cluster_means), 3) if cluster_means else None,
        }


def _bbo_by_asset(conn, cap):
    rows = conn.execute(
        "SELECT asset, exchange_ts_ms, receive_ts_ms, payload_json FROM hlm_raw_events WHERE channel='bbo' ORDER BY asset, exchange_ts_ms, id"
    ).fetchall()
    out = defaultdict(list)
    for r in rows:
        if cap and len(out[r["asset"]]) >= cap:
            continue
        p = json.loads(r["payload_json"])
        bb = p.get("bbo") or []
        if len(bb) < 2:
            continue
        bid, ask = bb[0], bb[1]
        bpx, bsz, apx, asz = _f(bid.get("px")), _f(bid.get("sz")), _f(ask.get("px")), _f(ask.get("sz"))
        if None in (bpx, bsz, apx, asz) or bpx <= 0 or apx <= 0:
            continue
        out[r["asset"]].append({"ex": int(r["exchange_ts_ms"] or p.get("time") or 0),
                                "rx": int(r["receive_ts_ms"] or 0),
                                "bid": bpx, "ask": apx, "bsz": bsz, "asz": asz,
                                "snap": (bpx, bsz, apx, asz)})
    return out


def _trades_by_asset(conn):
    rows = conn.execute(
        "SELECT asset, exchange_ts_ms, payload_json FROM hlm_raw_events WHERE channel='trades' ORDER BY asset, exchange_ts_ms, id"
    ).fetchall()
    out = defaultdict(list)
    for r in rows:
        p = json.loads(r["payload_json"])
        px, sz = _f(p.get("px")), _f(p.get("sz"))
        if px is None or sz is None:
            continue
        out[r["asset"]].append({"ex": int(r["exchange_ts_ms"] or p.get("time") or 0),
                                "buy": str(p.get("side", "")).upper().startswith("B"),
                                "notional": px * sz})
    return out


def _ctx_by_asset(conn):
    rows = conn.execute(
        "SELECT asset, receive_ts_ms, payload_json FROM hlm_raw_events WHERE channel='activeAssetCtx' ORDER BY asset, receive_ts_ms, id"
    ).fetchall()
    out = defaultdict(list)
    for r in rows:
        c = (json.loads(r["payload_json"]) or {}).get("ctx") or {}
        mark, oracle = _f(c.get("markPx")), _f(c.get("oraclePx"))
        if mark is None or oracle is None or oracle <= 0:
            continue
        out[r["asset"]].append({"rx": int(r["receive_ts_ms"]), "basis": (mark - oracle) / oracle})
    return out


def _basis_at(cl_ts, cl_basis, ts):
    # most recent ctx at-or-before ts (both on the receive clock). O(log n).
    i = bisect_right(cl_ts, ts) - 1
    return cl_basis[i] if i >= 0 else None


def _btc_lead_return(btc_ts, btc_mp, ts, lead_ms):
    # BTC microprice return over [ts-lead_ms, ts] (exchange clock). O(log n).
    i1 = bisect_right(btc_ts, ts) - 1
    i0 = bisect_right(btc_ts, ts - lead_ms) - 1
    if i0 < 0 or i1 < 0 or i0 == i1:
        return None
    mp0, mp1 = btc_mp[i0], btc_mp[i1]
    if mp0 is None or mp1 is None or mp0 <= 0:
        return None
    return (mp1 - mp0) / mp0


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
