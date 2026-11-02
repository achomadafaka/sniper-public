#!/usr/bin/env python3
"""Memecoin signal-conditioning study — reproducible artifact (2026-07-11).

Re-runnable version of the exploratory cuts first run inline on 2026-07-11 and audited by
Codex the same day. Incorporates the audit's corrections:
  - per-COIN dedup (first sighting per mint) alongside per-sighting numbers — sightings
    cluster heavily per coin (e.g. 1,120 large-cap sightings were only 39 coins);
  - excludes status='bad_quote' rows (malformed prints, e.g. the 4,916x HYPE mark) and
    cleanup/unquotable position closes;
  - TP counterfactual on ONE arm only (alpha_b) — cross-arm rows duplicate the same entry;
  - prints a manifest (as-of, row counts, exclusions) so results are auditable.

EXPLORATORY: ~4 days of forward capture, no multiple-testing control. Nothing here is a
verdict; pre-register before acting on any single cut. Read-only on the DB.

Usage: python3 scripts/research/memecoin_conditioning_study.py [--db sniper_tracker.db]
       [--capture-version 2|legacy]
Writes: docs/research/assets/memecoin_conditioning_<version>_<asof>.md
"""

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)
import argparse
import os
import sqlite3
import statistics as st

BANDS_MCAP = [("<1M", 0, 1e6), ("1-5M", 1e6, 5e6), ("5-30M", 5e6, 30e6), (">30M", 30e6, float("inf"))]
BANDS_SIZE = [("1-5k", 1e3, 5e3), ("5-20k", 5e3, 20e3), ("20k+", 20e3, float("inf"))]
TP_SWEEP = (20, 40, 60, 100, 150)
TP_HAIRCUT = 0.9


def fetch(conn, capture_version=2):
    if capture_version == 2:
        from scripts.research.memecoin_capture_v2_manifest import assert_capture_version
        assert_capture_version(conn, "memecoin_signal_outcomes_v2", 2)
        sightings = [dict(r) for r in conn.execute("""
            SELECT o.network_id, o.mint, o.signal_observed_at buy_ts,
                   MAX((h.price/o.ref_price-1)*100) peak,
                   (SELECT (last.price/o.ref_price-1)*100
                      FROM memecoin_horizon_marks_v2 last
                     WHERE last.outcome_id=o.outcome_id AND last.status='observed'
                     ORDER BY last.target_s DESC LIMIT 1) fin,
                   f.their_size_usd sz, f.their_market_cap mc
              FROM memecoin_signal_outcomes_v2 o
              JOIN memecoin_horizon_marks_v2 h ON h.outcome_id=o.outcome_id
              LEFT JOIN memecoin_feed_observations_v2 f
                ON f.upstream_event_id=o.upstream_event_id AND f.capture_version=2
             WHERE o.capture_version=2 AND o.status='complete' AND o.ref_price>0
               AND h.status='observed' AND h.price>0
             GROUP BY o.outcome_id
            HAVING COUNT(*)=o.expected_marks""")]
    elif capture_version == "legacy":
        sightings = [dict(r) for r in conn.execute("""
            SELECT COALESCE(o.network_id,'solana') network_id, o.mint,
                   o.buy_ts_normalized buy_ts,
                   o.max_gain_pct peak, (o.last_price/o.ref_price-1)*100 fin,
                   o.their_size_usd sz, e.their_market_cap mc
              FROM memecoin_signal_outcomes o
              LEFT JOIN memecoin_events e ON e.fomo_event_id=o.fomo_event_id
             WHERE o.capture_version=1 AND o.clock_domain!='legacy_unknown'
               AND o.buy_ts_normalized IS NOT NULL AND o.status='matured' AND o.ref_price>0
               AND o.last_price>0 AND o.max_gain_pct IS NOT NULL""")]
    else:
        raise ValueError("capture_version must be 2 or 'legacy'")
    positions = []
    if capture_version == "legacy":
        positions = [dict(r) for r in conn.execute("""
            SELECT source_handle h, realized_pnl pnl, max_gain_pct peak, entry_notional noti
              FROM memecoin_positions
             WHERE is_open=0 AND execution_mode='paper' AND strategy='alpha_b'
               AND exit_reason NOT LIKE 'cleanup%' AND exit_reason != 'unquotable'""")]
    return sightings, positions


def per_coin(sightings):
    """First sighting per chain/mint (chronological) kills repeated-sighting clustering."""
    first = {}
    for s in sorted(sightings, key=lambda x: x["buy_ts"] or ""):
        first.setdefault((s.get("network_id"), s["mint"]), s)
    return list(first.values())


def band_table(rows, keyf, bands, min_n=5):
    out = []
    for name, lo, hi in bands:
        sel = [r for r in rows if r[keyf] is not None and lo <= r[keyf] < hi]
        if len(sel) < min_n:
            out.append((name, len(sel), None, None, None))
            continue
        peaks = [r["peak"] for r in sel]
        out.append((name, len(sel), st.median(peaks),
                    sum(p >= 20 for p in peaks), sum(p >= 60 for p in peaks)))
    return out


def fmt_band(rows_tbl):
    lines = ["| band | n | med peak | hit>=20 | hit>=60 |", "|---|---|---|---|---|"]
    for name, n, med, h20, h60 in rows_tbl:
        if med is None:
            lines.append(f"| {name} | {n} | (n<5) | | |")
        else:
            lines.append(f"| {name} | {n} | {med:+.1f}% | {h20}/{n} | {h60}/{n} |")
    return "\n".join(lines)


def tp_sweep_by_wallet(positions, min_n=8):
    lines = ["| wallet | n(independent) | actual | " + " | ".join(f"T{t}" for t in TP_SWEEP) + " |",
             "|---|---|---|" + "---|" * len(TP_SWEEP)]
    byw = {}
    for p in positions:
        byw.setdefault(p["h"], []).append(p)
    for h, rs in sorted(byw.items(), key=lambda kv: -len(kv[1])):
        if len(rs) < min_n:
            continue
        cells = []
        for T in TP_SWEEP:
            v = sum((r["noti"] * T / 100 * TP_HAIRCUT if (r["peak"] or 0) >= T else r["pnl"]) for r in rs)
            cells.append(f"{v:+.0f}")
        lines.append(f"| {h} | {len(rs)} | {sum(r['pnl'] for r in rs):+.1f} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="sniper_tracker.db")
    ap.add_argument("--capture-version", choices=("2", "legacy"), default="2")
    args = ap.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    version = 2 if args.capture_version == "2" else "legacy"
    sightings, positions = fetch(conn, capture_version=version)
    coins = per_coin(sightings)
    if version == 2:
        asof = conn.execute(
            "SELECT MAX(completed_at) FROM memecoin_signal_outcomes_v2 WHERE capture_version=2",
        ).fetchone()[0]
        span = conn.execute(
            "SELECT MIN(signal_observed_at), MAX(signal_observed_at) "
            "FROM memecoin_signal_outcomes_v2 WHERE capture_version=2 AND status='complete'",
        ).fetchone()
        exclusion = "partial/open outcomes, non-ok quotes, and all legacy capture"
    else:
        asof = conn.execute("SELECT MAX(updated_at) FROM memecoin_signal_outcomes").fetchone()[0]
        span = conn.execute(
            "SELECT MIN(buy_ts), MAX(buy_ts) FROM memecoin_signal_outcomes "
            "WHERE capture_version=1 AND status='matured'",
        ).fetchone()
        excluded_bad = conn.execute(
            "SELECT COUNT(*) FROM memecoin_signal_outcomes "
            "WHERE capture_version=1 AND status='bad_quote'",
        ).fetchone()[0]
        exclusion = f"{excluded_bad} bad_quote row(s), cleanup/unquotable closes"

    md = [f"# Memecoin signal-conditioning study — as of {asof}",
          "",
          f"Manifest: {len(sightings)} eligible sightings / {len(coins)} distinct coins / "
          f"{len(positions)} independent alpha_b closes. Capture span {span[0]} .. {span[1]}. "
          f"Capture version: {args.capture_version}. Excluded: {exclusion}. "
          f"EXPLORATORY — no multiple-testing control; pre-register before acting.",
          "",
          "## Peak-gain by MARKET CAP — per SIGHTING (clustered; inflated n)",
          fmt_band(band_table(sightings, "mc", BANDS_MCAP)),
          "",
          "## Peak-gain by MARKET CAP — per COIN (first sighting; honest n)",
          fmt_band(band_table(coins, "mc", BANDS_MCAP)),
          "",
          "## Peak-gain by BUY SIZE — per COIN",
          fmt_band(band_table(coins, "sz", BANDS_SIZE)),
          "",
          ""]
    if version == "legacy":
        md.extend([
            "## TP counterfactual per wallet (legacy alpha_b entries only; exploratory)",
            tp_sweep_by_wallet(positions),
            "",
        ])
    else:
        md.extend([
            "## Exit-policy optimization",
            "Not run from horizon marks. TP/SL ordering requires mature dense quote sweeps; "
            "optimizing six scheduled marks would invent path order.",
            "",
        ])
    out_dir = os.path.join("docs", "research", "assets")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(
        out_dir, f"memecoin_conditioning_{args.capture_version}_{(asof or 'unknown')[:10]}.md",
    )
    with open(out, "w") as f:
        f.write("\n".join(md))
    print("\n".join(md))
    print("\nwritten:", out)



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('main',))

if __name__ == "__main__":
    main()
