#!/usr/bin/env python3
"""Market-cohort calibration study — EXTRACTION + CHECKPOINT (no battery).

Implements §Data + §Unit + §Observation-point + §Cohort of
docs/superpowers/specs/2026-05-30-market-cohort-calibration-study.md (rev 2):
one leg per market, leakage + fidelity filters, frozen volume terciles + OOS
split. Outputs ONLY the pre-battery checkpoint counts — does NOT run the
calibration/FDR/placebo battery. The operator reviews the counts and gates the
battery (catch an underpowered/broken universe before a long run).

T = market_created_at + 24h. Chosen leg = the underdog (price_at_T <= 0.5), ties
-> YES. Becker has NO negativeRisk field; binary-2-token filtering excludes most
(multi-outcome) negativeRisk markets — reported as a coverage gap.

Usage:
  python3 scripts/cohort_extract.py \
    --markets-glob '.../markets/*.parquet' --trades-glob '.../trades/*.parquet' \
    --out runs/cohort/checkpoint.json [--trade-files-sample 0]
"""
from __future__ import annotations

import argparse
import glob
import json
import sys

import duckdb

USDC = "0"
W = 1714  # ±1h in blocks (~2.1s/block); active-fidelity window
ACTIVE_MIN = 5

_CATS = {
    "sports": ("nba", "nfl", "mlb", "nhl", "soccer", "ufc", "tennis", "epl",
               "premier-league", "champions-league", "-vs-", " vs "),
    "politics": ("election", "president", "senate", "congress", "governor",
                 "trump", "biden", "primary", "parliament", "vote", "becomes law"),
    "crypto": ("bitcoin", "btc", "ethereum", "eth", "solana", "crypto", "coin"),
    "finance": ("fed", "rate", "cpi", "gdp", "inflation", "stock", "nasdaq", "s&p"),
}


def _category_sql() -> str:
    # SQL CASE deriving a coarse category from slug+question (keyword heuristic).
    parts = []
    for cat, kws in _CATS.items():
        conds = " OR ".join(f"lower(blob) LIKE '%{k}%'" for k in kws)
        parts.append(f"WHEN {conds} THEN '{cat}'")
    return "CASE " + " ".join(parts) + " ELSE 'unknown' END"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets-glob", required=True)
    ap.add_argument("--trades-glob", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--trade-files-sample", type=int, default=0, help="0=all; >0=first N trade files (smoke)")
    ap.add_argument("--calib-files", type=int, default=2000, help="trade files sampled for block->epoch calibration (median residual is robust)")
    ap.add_argument("--block-seconds", type=float, default=2.1)
    args = ap.parse_args()

    con = duckdb.connect()
    con.execute("PRAGMA memory_limit='8GB'")
    con.execute("PRAGMA temp_directory='/home/bigpapi/.claude/jobs/3c91c9bf/tmp/duck'")

    mfiles = sorted(glob.glob(args.markets_glob))
    tfiles = sorted(glob.glob(args.trades_glob))
    if args.trade_files_sample > 0:
        tfiles = tfiles[: args.trade_files_sample]
    mglob = "[" + ",".join(repr(f) for f in mfiles) + "]"
    tglob = "[" + ",".join(repr(f) for f in tfiles) + "]"
    cfiles = tfiles[: args.calib_files] if args.calib_files > 0 else tfiles
    cglob = "[" + ",".join(repr(f) for f in cfiles) + "]"
    print(f"markets files={len(mfiles)} trades files={len(tfiles)} calib files={len(cfiles)}", file=sys.stderr)

    # ---- 1. block->epoch calibration (anchor: token first-trade block vs market created_at) — sampled
    con.execute(f"""
      CREATE TEMP TABLE first_block AS
      SELECT asset, MIN(block_number) mn FROM (
        SELECT maker_asset_id asset, block_number FROM read_parquet({cglob})
        UNION ALL SELECT taker_asset_id, block_number FROM read_parquet({cglob})
      ) WHERE asset <> '{USDC}' GROUP BY asset
    """)
    intercept = con.execute(f"""
      WITH m AS (
        SELECT condition_id, created_at,
               json_extract_string(clob_token_ids,'$[0]') t0,
               json_extract_string(clob_token_ids,'$[1]') t1
        FROM read_parquet({mglob})
        WHERE clob_token_ids IS NOT NULL AND created_at IS NOT NULL
      ), anch AS (
        SELECT epoch(m.created_at) - {args.block_seconds}*fb.mn AS resid
        FROM m JOIN first_block fb ON fb.asset = m.t0
        WHERE m.t0 IS NOT NULL
      )
      SELECT median(resid) FROM anch
    """).fetchone()[0]
    slope = args.block_seconds
    print(f"calibration: epoch = {slope}*block + {intercept:.0f}", file=sys.stderr)

    # ---- 2. markets pass: binary + resolved + life>=48h; T, T_block, yes_token, winner, volume, cat, ttr
    con.execute(f"""
      CREATE TEMP TABLE mkt AS
      WITH base AS (
        SELECT condition_id AS cid,
          json_extract_string(clob_token_ids,'$[0]') AS yes_token,
          clob_token_ids, outcomes, outcome_prices, closed, volume,
          epoch(created_at) AS created_e, epoch(end_date) AS end_e,
          lower(coalesce(slug,'') || ' ' || coalesce(question,'')) AS blob,
          json_array_length(clob_token_ids) AS n_tok
        FROM read_parquet({mglob})
        WHERE condition_id IS NOT NULL AND clob_token_ids IS NOT NULL
          AND created_at IS NOT NULL AND end_date IS NOT NULL
      )
      SELECT cid, yes_token, closed, volume, created_e, end_e, n_tok,
        (created_e + 86400) AS t_e,
        (( (created_e + 86400) - {intercept}) / {slope}) AS t_block,
        outcome_prices,
        {_category_sql()} AS category,
        ((end_e - (created_e + 86400))) AS ttr_s
      FROM base
    """)

    total = con.execute("SELECT count(*) FROM mkt").fetchone()[0]
    n_binary = con.execute("SELECT count(*) FROM mkt WHERE n_tok = 2").fetchone()[0]
    # resolved: outcome_prices has a clear winner (>=0.9)
    con.execute("""
      CREATE TEMP TABLE mkt2 AS
      SELECT *,
        CASE WHEN closed AND TRY_CAST(json_extract_string(outcome_prices,'$[0]') AS DOUBLE) >= 0.9 THEN 0
             WHEN closed AND TRY_CAST(json_extract_string(outcome_prices,'$[1]') AS DOUBLE) >= 0.9 THEN 1
             ELSE NULL END AS winning_index
      FROM mkt WHERE n_tok = 2
    """)
    n_resolved = con.execute("SELECT count(*) FROM mkt2 WHERE winning_index IS NOT NULL").fetchone()[0]
    # life >= 48h (== end_date > T+24h given T=created+24h)
    con.execute("""
      CREATE TEMP TABLE mkt3 AS
      SELECT * FROM mkt2 WHERE winning_index IS NOT NULL AND (end_e - created_e) >= 172800
    """)
    n_life = con.execute("SELECT count(*) FROM mkt3").fetchone()[0]

    # ---- 3. price-at-T + fidelity from trades on the YES token near T_block
    con.execute(f"""
      CREATE TEMP TABLE tp AS
      SELECT block_number AS block,
        CASE WHEN maker_asset_id='{USDC}' THEN taker_asset_id ELSE maker_asset_id END AS asset,
        CASE WHEN maker_asset_id='{USDC}' THEN maker_amount::DOUBLE/NULLIF(taker_amount,0)
             ELSE taker_amount::DOUBLE/NULLIF(maker_amount,0) END AS price
      FROM read_parquet({tglob})
      WHERE (maker_asset_id='{USDC}' OR taker_asset_id='{USDC}')
        AND maker_asset_id <> taker_asset_id
    """)
    con.execute(f"""
      CREATE TEMP TABLE priced AS
      SELECT m.cid,
        arg_max(t.price, t.block) FILTER (WHERE t.block <= m.t_block AND t.price BETWEEN 0 AND 1) AS yes_price_at_t,
        count(*) FILTER (WHERE t.block BETWEEN m.t_block - {W} AND m.t_block + {W}) AS fidelity_n
      FROM mkt3 m JOIN tp t
        ON t.asset = m.yes_token AND t.block BETWEEN m.t_block - {W} AND m.t_block + {W}
      GROUP BY m.cid
    """)
    n_haveprice = con.execute("SELECT count(*) FROM priced WHERE yes_price_at_t IS NOT NULL").fetchone()[0]
    n_active = con.execute(f"SELECT count(*) FROM priced WHERE yes_price_at_t IS NOT NULL AND fidelity_n >= {ACTIVE_MIN}").fetchone()[0]

    # ---- 4. chosen leg (underdog) + bands + frozen terciles + OOS split
    con.execute(f"""
      CREATE TEMP TABLE legs AS
      SELECT m.cid, m.category, m.volume, m.created_e, m.ttr_s, m.winning_index,
        p.yes_price_at_t, p.fidelity_n,
        -- chosen leg: underdog (price<=0.5). idx 0 if yes<=0.5 else 1.
        CASE WHEN p.yes_price_at_t <= 0.5 THEN 0 ELSE 1 END AS chosen_idx,
        CASE WHEN p.yes_price_at_t <= 0.5 THEN p.yes_price_at_t ELSE 1 - p.yes_price_at_t END AS chosen_price,
        CASE WHEN (CASE WHEN p.yes_price_at_t <= 0.5 THEN 0 ELSE 1 END) = m.winning_index THEN 1 ELSE 0 END AS chosen_won
      FROM mkt3 m JOIN priced p ON p.cid = m.cid
      WHERE p.yes_price_at_t IS NOT NULL AND p.fidelity_n >= {ACTIVE_MIN}
    """)
    # frozen volume terciles on the FULL filtered universe
    vt = con.execute("SELECT quantile_cont(volume,[0.3333,0.6667]) FROM legs").fetchone()[0]
    v_lo, v_hi = float(vt[0]), float(vt[1])
    # OOS split at 70th pct of created_e
    split_e = con.execute("SELECT quantile_cont(created_e, 0.70) FROM legs").fetchone()[0]
    con.execute(f"""
      CREATE TEMP TABLE cohort AS
      SELECT cid, category, chosen_price, chosen_won, chosen_idx, ttr_s, volume,
        CASE WHEN created_e <= {split_e} THEN 'train' ELSE 'oos' END AS split,
        CASE WHEN volume < {v_lo} THEN 'v_lo' WHEN volume < {v_hi} THEN 'v_mid' ELSE 'v_hi' END AS volume_tier,
        CASE
          WHEN chosen_price < 0.1 THEN '[0.0,0.1)' WHEN chosen_price < 0.2 THEN '[0.1,0.2)'
          WHEN chosen_price < 0.35 THEN '[0.2,0.35)' WHEN chosen_price < 0.5 THEN '[0.35,0.5)'
          WHEN chosen_price < 0.65 THEN '[0.5,0.65)' WHEN chosen_price < 0.8 THEN '[0.65,0.8)'
          WHEN chosen_price < 0.9 THEN '[0.8,0.9)' WHEN chosen_price < 0.97 THEN '[0.9,0.97)'
          ELSE '[0.97,1.0]' END AS price_band,
        CASE WHEN ttr_s < 86400 THEN '<24h' WHEN ttr_s < 604800 THEN '1-7d'
             WHEN ttr_s < 2592000 THEN '7-30d' WHEN ttr_s < 7776000 THEN '30-90d'
             ELSE '>90d' END AS ttr_band
      FROM legs
    """)

    # ---- 5. checkpoint aggregates
    n_legs = con.execute("SELECT count(*) FROM cohort").fetchone()[0]
    n_dup = con.execute("SELECT count(*) - count(DISTINCT cid) FROM cohort").fetchone()[0]
    n_underdog_only = con.execute("SELECT count(*) FROM cohort WHERE chosen_price > 0.5").fetchone()[0]
    cohort_key = "category, market_type:='binary', price_band, ttr_band, volume_tier"
    powered = con.execute("""
      WITH c AS (
        SELECT category, price_band, ttr_band, volume_tier,
               count(*) FILTER (WHERE split='train') n_train,
               count(*) FILTER (WHERE split='oos') n_oos
        FROM cohort GROUP BY 1,2,3,4)
      SELECT count(*) FILTER (WHERE n_train>=200) powered_train,
             count(*) FILTER (WHERE n_oos>=200) powered_oos,
             count(*) FILTER (WHERE n_train>=200 AND n_oos>=200) powered_both,
             count(*) total_cohorts FROM c
    """).fetchone()
    cat_dist = con.execute("SELECT category, count(*) n FROM cohort GROUP BY 1 ORDER BY n DESC").fetchall()
    band_dist = con.execute("SELECT price_band, count(*) n FROM cohort GROUP BY 1 ORDER BY price_band").fetchall()
    pb_ttr = con.execute("""
      SELECT price_band, ttr_band, count(*) n FROM cohort GROUP BY 1,2 ORDER BY 1,2
    """).fetchall()
    top_powered = con.execute("""
      SELECT category, price_band, ttr_band, volume_tier,
             count(*) FILTER (WHERE split='train') n_train,
             count(*) FILTER (WHERE split='oos') n_oos
      FROM cohort GROUP BY 1,2,3,4
      HAVING count(*) FILTER (WHERE split='train')>=200 AND count(*) FILTER (WHERE split='oos')>=200
      ORDER BY n_train DESC LIMIT 25
    """).fetchall()

    report = {
      "calibration": {"slope": slope, "intercept": intercept},
      "funnel": {
        "total_markets_scanned": total,
        "binary_2token": n_binary,
        "resolved": n_resolved,
        "life_ge_48h_(== end_date>T+24h)": n_life,
        "have_yes_price_at_T": n_haveprice,
        "active_fidelity_ge_5": n_active,
        "final_legs": n_legs,
      },
      "drops": {
        "non_binary": total - n_binary,
        "unresolved": n_binary - n_resolved,
        "life_lt_48h": n_resolved - n_life,
        "no_price_at_T": n_life - n_haveprice,
        "sparse_fidelity_lt_5": n_haveprice - n_active,
      },
      "negativeRisk_gap": "Becker markets carry NO negativeRisk field; cannot filter explicitly. Binary-2-token filter excludes multi-outcome negRisk; residual binary negRisk markets may remain (unquantified).",
      "frozen_volume_terciles": {"v_lo<": v_lo, "v_hi<": v_hi},
      "oos_split_created_epoch_p70": split_e,
      "sanity": {"final_legs": n_legs, "duplicate_market_ids": n_dup,
                 "rows_with_chosen_price_gt_0.5_should_be_0": n_underdog_only},
      "cohorts": {"total": powered[3], "powered_train_ge200": powered[0],
                  "powered_oos_ge200": powered[1], "powered_both_ge200": powered[2]},
      "category_distribution": [{"category": c, "n": n} for c, n in cat_dist],
      "price_band_distribution": [{"price_band": b, "n": n} for b, n in band_dist],
      "price_band_x_ttr": [{"price_band": pb, "ttr_band": tb, "n": n} for pb, tb, n in pb_ttr],
      "top_powered_cohorts": [
        {"category": a, "price_band": b, "ttr_band": c2, "volume_tier": d, "n_train": e, "n_oos": f}
        for a, b, c2, d, e, f in top_powered],
    }
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=2)
    print(json.dumps({k: report[k] for k in ("funnel", "drops", "sanity", "cohorts")}, indent=2))
    print(f"\nwrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
