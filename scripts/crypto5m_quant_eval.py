#!/usr/bin/env python3
"""Crypto-5m performance evaluator (read-only). Runs on a cron; writes a dated report of what the algos do
well/badly and concrete recommendations. Covers: process health + data freshness, the forward C0-R3
benchmark, and the unified quant cohort (overall + residual split + per-component Brier + paired vs C1/C0 +
regime breakdown). No orders, no mutation — pure analysis over the cohort DBs.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.algos.polymarket_crypto5m import forward_report as FR   # noqa: E402
from sniper.algos.polymarket_crypto5m import quant_eval as QEV      # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FORWARD_DB = os.path.join(ROOT, "data", "crypto5m", "btc5m_forward_v0.sqlite")
QUANT_DB = os.path.join(ROOT, "data", "crypto5m", "btc5m_quant_shadow_v4.sqlite")
CAPTURE_DB = os.path.join(ROOT, "data", "crypto5m_capture.sqlite")
REPORT_DIR = os.path.join(ROOT, "reports", "crypto5m")
PIDS = {"forward": "data/crypto5m/btc5m_forward_v0.pid", "capture": "data/crypto5m_capture.pid",
        "quant-v4": "data/crypto5m/btc5m_quant_shadow_v4.pid"}


def _ro(path):
    if not os.path.exists(path):
        return None
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _alive(pidfile):
    try:
        pid = int(open(os.path.join(ROOT, pidfile)).read().strip())
        os.kill(pid, 0)
        return pid
    except Exception:  # noqa: BLE001
        return None


def health(now):
    lines = []
    for name, pf in PIDS.items():
        pid = _alive(pf)
        lines.append(f"- **{name}**: {'alive PID ' + str(pid) if pid else 'DOWN'}")
    cap = _ro(CAPTURE_DB)
    if cap:
        for t in ("crypto5m_markets", "crypto5m_chainlink_ticks", "crypto5m_book_snapshots",
                  "crypto5m_binance_observations"):
            try:
                n = cap.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.Error:
                n = "n/a"
            lines.append(f"- capture `{t}`: {n}")
        cap.close()
    return lines


def extract_quant_records(conn):
    settled = {r["condition_id"]: r["winning_outcome"] for r in conn.execute(
        "SELECT condition_id, winning_outcome FROM crypto5m_quant_settlements "
        "WHERE winning_outcome IN ('Up','Down')")}
    matrices = {}
    for r in conn.execute("SELECT condition_id, checkpoint, matrix_json FROM crypto5m_quant_matrix"):
        try:
            matrices[(r["condition_id"], r["checkpoint"])] = json.loads(r["matrix_json"] or "{}")
        except (TypeError, ValueError):
            matrices[(r["condition_id"], r["checkpoint"])] = {}
    out = []
    for d in conn.execute("SELECT condition_id, checkpoint, actionable_side, q_trade, net_edge "
                          "FROM crypto5m_quant_decisions"):
        winner = settled.get(d["condition_id"])
        if winner is None:
            continue
        m = matrices.get((d["condition_id"], d["checkpoint"]), {})
        panel = m.get("panel", {})
        out.append({
            "actionable_side": d["actionable_side"], "winner": winner,
            "q_market": m.get("q_market", panel.get("q_market")),
            "q_trade": d["q_trade"], "components": panel.get("components", {}),
            "c1_side": m.get("c1_side", panel.get("c1_side")),
            "c0_side": m.get("c0_side", panel.get("c0_side")),
            "vol_regime": panel.get("vol_regime"), "session": panel.get("session"),
            "disagreement_regime": panel.get("disagreement_regime"),
            "net_edge_f1": d["net_edge"],
        })
    return out


def extract_leadlag_pairs(conn):
    """Per window, sort checkpoint snapshots and pair (model−market signal at the early checkpoint,
    market q move to the next checkpoint). Tests whether our signal leads the market's repricing."""
    from collections import defaultdict
    snaps = defaultdict(list)
    for r in conn.execute("SELECT condition_id, checkpoint, matrix_json FROM crypto5m_quant_matrix"):
        try:
            m = json.loads(r["matrix_json"] or "{}")
        except (TypeError, ValueError):
            m = {}
        panel = m.get("panel", {}) or {}
        qm = m.get("q_market", panel.get("q_market"))
        if qm is None:
            continue
        snaps[r["condition_id"]].append((r["checkpoint"], qm, panel.get("structural_minus_market")))
    pairs = []
    for lst in snaps.values():
        lst.sort(key=lambda x: x[0])
        for i in range(len(lst) - 1):
            _, qm_e, sig_e = lst[i]
            _, qm_l, _ = lst[i + 1]
            if sig_e is not None:
                pairs.append((sig_e, qm_l - qm_e))
    return pairs


def main():
    os.makedirs(REPORT_DIR, exist_ok=True)
    now = int(time.time())
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    md = [f"# Crypto-5m eval — {datetime.now(timezone.utc).isoformat()}", "", "## Health"]
    md += health(now)

    # forward benchmark
    md += ["", "## Forward benchmark (C0-R3)"]
    fc = _ro(FORWARD_DB)
    if fc:
        rep = FR.build_forward_report(fc)
        fc.close()
        md.append(f"- resolved windows: {rep['settlements']['resolved']}, decisions: {rep['totals']['n_decisions']}")
        for ru in rep["rules"]:
            s1 = (ru["scenarios"] or {}).get("F1_documented", {})
            md.append(f"  - {ru['rule_id']}: resolved {ru['n_resolved']}, WR(F1) {s1.get('win_rate')}, PnL F1 {s1.get('total_pnl')}")
    else:
        md.append("- forward DB not found")

    # quant cohort
    md += ["", "## Quant unified cohort (btc5m-quant-shadow-v4)"]
    qc = _ro(QUANT_DB)
    summary_row = {"stamp": stamp, "quant_n": 0, "quant_hit": None, "c1_hit": None, "top_rec": ""}
    if qc:
        recs = extract_quant_records(qc)
        ll = QEV.leadlag(extract_leadlag_pairs(qc))
        qc.close()
        ev = QEV.evaluate_cohort(recs)
        md.append(f"- resolved actionable: **{ev['n']}**, hit rate **{ev['hit_rate']}** CI {ev['ci']}")
        md.append(f"- vs C1 favorite: {ev['c1_hit_rate']} ({ev['c1_hits']}/{ev['c1_n']}) · vs C0 random: {ev['c0_hit_rate']}")
        md.append(f"- residual split — agree: {ev['agree_market']['hit_rate']} (n={ev['agree_market']['n']}) · "
                  f"**differ: {ev['differ_market']['hit_rate']} (n={ev['differ_market']['n']}, CI {ev['differ_market']['ci']})**")
        md.append(f"- shadow net edge (F1) mean: {ev['shadow_net_edge_mean']}")
        md.append(f"- **lead-lag** (does our signal predict the market's next move?): "
                  f"agreement {ll['agreement_rate']} (n={ll['n']}, CI {ll['ci']}) — >0.5 clear of 0.5 = market lags us")
        md.append("- per-component (Brier ↓ better; 0.25 = chance):")
        for name, c in sorted(ev["components"].items(), key=lambda kv: (kv[1]["brier"] is None, kv[1]["brier"])):
            md.append(f"  - {name}: Brier {c['brier']}, dir-acc {c['dir_acc']}, n {c['n']}")
        md.append(f"- by vol regime: { {k: v['hit_rate'] for k, v in ev['by_vol_regime'].items()} }")
        md.append(f"- by session: { {k: v['hit_rate'] for k, v in ev['by_session'].items()} }")
        md += ["", "### Recommendations / what to fix"]
        md += [f"- {r}" for r in ev["recommendations"]]
        summary_row.update(quant_n=ev["n"], quant_hit=ev["hit_rate"], c1_hit=ev["c1_hit_rate"],
                           top_rec=(ev["recommendations"][0] if ev["recommendations"] else ""))
    else:
        md.append("- quant DB not found")

    report_path = os.path.join(REPORT_DIR, f"eval-{stamp}.md")
    with open(report_path, "w") as f:
        f.write("\n".join(md) + "\n")
    # append one-line history
    log_path = os.path.join(REPORT_DIR, "eval_log.csv")
    new = not os.path.exists(log_path)
    with open(log_path, "a") as f:
        if new:
            f.write("stamp,quant_n,quant_hit,c1_hit,top_rec\n")
        f.write(f'{stamp},{summary_row["quant_n"]},{summary_row["quant_hit"]},{summary_row["c1_hit"]},'
                f'"{summary_row["top_rec"]}"\n')
    print(f"wrote {report_path}")
    print("\n".join(md[-12:]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
