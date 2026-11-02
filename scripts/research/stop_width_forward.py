#!/usr/bin/env python3
"""Funded stop width, forward replication (docs/audits/2026-10-21-stop-width-forward-preregistration.md).

Read-only. Reuses the sealed 2026-10-16 Solana exit-grid study (scripts/research/solana_exit_grid_study.py)
unchanged: its load(), coverage and exclusion rules, simulate(), price_arms(), validity() and analyse().
Only the window and the arms differ, as pre-registered:
  window  opened in [2026-10-16T06:00Z, 2026-10-22T00:00Z)
  arms    SL 15/20/25/30 against the control SL 35, TP 25, trail off, catastrophe 80

  python3 scripts/research/stop_width_forward.py --cohort              # identity and exclusion counts only
  python3 scripts/research/stop_width_forward.py --read --out FILE     # the one read, after the cut
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("exit_grid", REPO / "scripts/research/solana_exit_grid_study.py")
G = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(G)

G.START = "2026-10-16T06:00:00+00:00"
G.SEAL_AT = "2026-10-22T00:00:00+00:00"
G.ARMS = tuple((25, sl, None) for sl in (15, 20, 25, 30, 35))
SEED = 20261021
MIN_READ_SET = 60
MIN_CONTROL_STOPS = 15


def classify(control_label, control_value, arm_label, arm_value, arm_fired):
    """'winner_cut' | 'loss_saved' | None, on the paired quote basis (pre-reg: Alpha's ask)."""
    if not arm_fired or arm_label != "stop":
        return None
    if control_label == "tp":
        return "winner_cut"
    if control_label == "stop" and arm_value > control_value:
        return "loss_saved"
    return None


def per_arm_counts(read_set):
    out = {}
    for arm in G.ARMS:
        if arm == G.CONTROL:
            continue
        c = {"winner_cut": [0, 0.0], "loss_saved": [0, 0.0]}
        for p in read_set:
            n = int(p["policy"].get("stop_debounce_sweeps") or 1)
            ctrl = G.simulate(p["series"], p["notional"], **G.contract_rules(p["policy"], **G.mechanics(p)))
            got = G.simulate(p["series"], p["notional"],
                             **G.grid_rules(arm, G.CONTROL_CATASTROPHE, debounce=n, **G.mechanics(p)))
            cv = p["arms"][G.CONTROL][0]
            kind = classify(ctrl[2] if ctrl else None, cv, got[2] if got else None,
                            got[1] if got else None, got is not None)
            if kind:
                c[kind][0] += 1
                c[kind][1] += (got[1] - cv) / p["notional"]
        out[G.arm_label(arm)] = {k: {"n": v[0], "sum_diff": round(v[1], 6)} for k, v in c.items()}
    return out


def funding_of(conn, ids):
    rows = conn.execute("SELECT position_id, funding_asset FROM memecoin_fills WHERE kind='entry'").fetchall()
    return {r[0]: ("native_sol" if r[1] == "sol" else "usdc") for r in rows if r[0] in ids}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--cohort", action="store_true")
    mode.add_argument("--read", action="store_true")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    conn = G._connect()
    positions = G.load(conn)
    included = [p for p in positions if p["cause"] is None]
    read_set = [p for p in included if p["control_contract"]]
    causes = {}
    for p in positions:
        causes[p["cause"] or ("read_set" if p.get("control_contract") else "other_contract")] = \
            causes.get(p["cause"] or ("read_set" if p.get("control_contract") else "other_contract"), 0) + 1
    evm = dict(conn.execute("SELECT network_id, COUNT(*) FROM memecoin_positions WHERE execution_mode='real' "
                            "AND network_id!='solana' AND opened_at >= ? AND opened_at < ? GROUP BY 1",
                            (G.START, G.SEAL_AT)).fetchall())
    head = {"window": [G.START, G.SEAL_AT], "arms": [G.arm_label(x) for x in G.ARMS], "causes": causes,
            "read_set": len(read_set), "evm_not_read": evm}
    if a.cohort:
        print(json.dumps(head, indent=1, sort_keys=True))
        return 0
    if time.time() < G.ts(G.SEAL_AT):
        print("refused: the window closes at %s" % G.SEAL_AT)
        return 2
    if not a.out or Path(a.out).exists():
        print("refused: --out must name a new file")
        return 2
    out = dict(head)
    val = G.validity(read_set)
    out["validity"] = val
    if not val["stop_passed"]:
        out["stopped"] = "stop rail validity failed; no arm priced"
        Path(a.out).write_text(json.dumps(out, indent=1, sort_keys=True, default=str))
        return 0
    for p in read_set:
        p["arms"] = G.price_arms(p)
    control_stops = sum(1 for p in read_set if G.RAIL_REASONS.get(p["exit_reason"]) == "stop")
    out["control_stop_closes"] = control_stops
    out["too_thin"] = len(read_set) < MIN_READ_SET or control_stops < MIN_CONTROL_STOPS
    rng = np.random.default_rng(SEED)
    out["primary_pooled"] = G.analyse(read_set, rng, clusters_by_wallet=True, read_arms=G.ARMS)
    out["winners_cut_losses_saved"] = per_arm_counts(read_set)
    fund = funding_of(conn, {p["id"] for p in read_set})
    out["by_funding"] = {}
    for k in ("native_sol", "usdc"):
        grp = [p for p in read_set if fund.get(p["id"]) == k]
        out["by_funding"][k] = {"n": len(grp), "counts": per_arm_counts(grp) if grp else None,
                                "mean_diff": {G.arm_label(arm): (statistics.mean(
                                    (p["arms"][arm][0] - p["arms"][G.CONTROL][0]) / p["notional"] for p in grp)
                                    if grp else None) for arm in G.ARMS if arm != G.CONTROL}}
    wallets = {}
    for p in read_set:
        wallets.setdefault(p["wallet"], []).append(p)
    out["per_wallet_descriptive"] = {w: G.analyse(g, np.random.default_rng(SEED), clusters_by_wallet=False,
                                                  read_arms=G.ARMS)["arms"]
                                     for w, g in wallets.items() if len(g) >= 10}
    Path(a.out).write_text(json.dumps(out, indent=1, sort_keys=True, default=str))
    print("written %s" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
