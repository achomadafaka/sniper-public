#!/usr/bin/env python3
"""Build a CSE point-in-time MarketContext from the Becker Polymarket archive.

The Counterfactual Selection Engine (sniper/analysis/counterfactual_selection/)
needs, for every decision time T, the universe of markets that EXISTED at T and
their price at T. The Becker archive supplies this offline:

  markets/*.parquet  -> condition_id, clob_token_ids, outcomes, outcome_prices,
                        created_at, end_date  (real datetimes)
  trades/*.parquet   -> block_number, maker/taker asset_id + amount  (NO timestamp:
                        the `timestamp` column is 100% null, so block_number is the
                        only clock — calibrated to epoch here)

Output is a JSON MarketContext { market_meta, price_index } directly loadable by
pit_market_context.MarketContext.

Cost control: a full price index over all markets is a multi-GB scan. Pass
--wallet-activity to scope the price index to only the candidate universe a given
wallet's engagements actually touch (the in-scope markets alive at her decision
times). market_meta is always built in full (cheap — 41 market files).

Block→time: trades carry no usable timestamp. We calibrate block_number→epoch
from market created_at: a market's tokens get their first trade shortly after
creation, so (min_trade_block_of_token, created_at) is an approximate anchor.
Slope is fixed to Polygon's block time (~2.1s); the intercept is the median
anchor residual. Absolute error is ~minutes (creation→first-trade lag), which is
fine for hour-scale selection cadence — NOT for sub-minute execution claims.

Usage:
  python3 scripts/build_cse_pit_index.py \
      --markets-glob 'data/polymarket/markets/*.parquet' \
      --trades-glob  'data/polymarket/trades/*.parquet' \
      --wallet-activity snapshots/swissmiss_activity.json \
      --out artifacts/cse_context_swissmiss.json \
      --max-trade-files 0            # 0 = all; >0 = sample for a smoke run
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from typing import Any

try:
    import duckdb
except ImportError:  # pragma: no cover - env guard
    print("duckdb required: pip install duckdb", file=sys.stderr)
    raise

# Repo import (script lives in scripts/)
sys.path.insert(0, ".")
from sniper.analysis.counterfactual_selection.entity_layers import build_engagements  # noqa: E402

USDC_ASSET = "0"
POLYGON_BLOCK_SECONDS = 2.1
ACTIVE_PRICE_MIN_TRADES = 5  # mirrors pit_market_context.ACTIVE_MIN_TRADES


# --------------------------------------------------------------------------
# markets → market_meta + token map
# --------------------------------------------------------------------------

_CATEGORY_KEYWORDS = {
    "sports": ("nba", "nfl", "mlb", "nhl", "soccer", "ufc", "tennis", "epl",
               "premier-league", "champions-league", "game", "match", "vs-", "-vs-"),
    "politics": ("election", "president", "senate", "congress", "governor",
                 "trump", "biden", "primary", "parliament", "vote"),
    "crypto": ("bitcoin", "btc", "ethereum", "eth", "solana", "crypto", "coin"),
    "finance": ("fed", "rate", "cpi", "gdp", "inflation", "stock", "s-p", "nasdaq"),
}


def _category(slug: str | None, question: str | None) -> str:
    blob = f"{slug or ''} {question or ''}".lower()
    for cat, kws in _CATEGORY_KEYWORDS.items():
        if any(k in blob for k in kws):
            return cat
    return "unknown"


def _epoch(dt: Any) -> int | None:
    if dt is None:
        return None
    try:
        return int(dt.timestamp())
    except Exception:
        return None


def _parse_json_list(raw: Any) -> list:
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def _resolution_fields(outcomes: list[Any], prices: list[Any], *, closed: bool) -> dict[str, Any]:
    """Per-side metadata needed by the real CSE matrix path."""
    clean_outcomes = [str(o) for o in (outcomes or [])]
    winning_index = None
    resolved_yes = None
    if closed and prices:
        try:
            numeric = [float(p) for p in prices]
        except (TypeError, ValueError):
            numeric = []
        if numeric:
            win = max(range(len(numeric)), key=lambda i: numeric[i])
            if numeric[win] >= 0.5:
                winning_index = win
                resolved_yes = 1 if win == 0 else 0
    return {
        "outcomes": clean_outcomes,
        "winning_index": winning_index,
        "resolved_yes": resolved_yes,
    }


def load_markets(markets_glob: str) -> tuple[dict[str, dict], dict[str, str]]:
    """market_meta keyed by condition_id, and token_id -> condition_id map.

    outcome_role / favorite_rank are price-derived per token; here we record the
    YES token and an early-price-derived favorite flag from outcome_prices if the
    market is already resolved (kept conservative — the engine re-derives role
    from point-in-time price when available)."""
    con = duckdb.connect()
    files = sorted(glob.glob(markets_glob))
    if not files:
        raise SystemExit(f"no market files matched {markets_glob}")
    glob_lit = "[" + ",".join(repr(f) for f in files) + "]"
    q = f"""
        SELECT condition_id, clob_token_ids, outcomes, outcome_prices,
               slug, question, created_at, end_date, closed
        FROM read_parquet({glob_lit})
        WHERE condition_id IS NOT NULL AND clob_token_ids IS NOT NULL
    """
    meta: dict[str, dict] = {}
    token_to_cid: dict[str, str] = {}
    for row in con.execute(q).fetchall():
        cid, tok_raw, out_raw, price_raw, slug, question, created, end, closed = row
        tokens = [str(t) for t in _parse_json_list(tok_raw)]
        if len(tokens) != 2:
            continue  # binary only for now
        outcomes = _parse_json_list(out_raw)
        prices = _parse_json_list(price_raw)
        yes_idx = 0
        if outcomes and str(outcomes[0]).lower() not in ("yes",):
            # outcomes[0] not "Yes" — keep index 0 as the recorded side anyway
            yes_idx = 0
        yes_token = tokens[yes_idx]
        resolution = _resolution_fields(outcomes, prices, closed=bool(closed))
        meta[str(cid)] = {
            "created_at": _epoch(created),
            "end_date": _epoch(end),
            "category": _category(slug, question),
            "market_type": "binary",
            "yes_token": yes_token,
            "tokens": tokens,
            "outcomes": resolution["outcomes"],
            "winning_index": resolution["winning_index"],
            "resolved_yes": resolution["resolved_yes"],
        }
        for t in tokens:
            token_to_cid[t] = str(cid)
    con.close()
    return meta, token_to_cid


# --------------------------------------------------------------------------
# block → epoch calibration
# --------------------------------------------------------------------------

def calibrate_block_epoch(trades_glob: str, token_to_cid: dict[str, str],
                          meta: dict[str, dict], *, sample_files: int = 40,
                          block_seconds: float = POLYGON_BLOCK_SECONDS) -> tuple[float, float]:
    """Fit epoch = block_seconds * block + intercept. Anchors come from each
    token's earliest trade block vs its market's created_at; intercept is the
    median residual (robust). Returns (slope, intercept)."""
    files = sorted(glob.glob(trades_glob))[: max(1, sample_files)]
    glob_lit = "[" + ",".join(repr(f) for f in files) + "]"
    con = duckdb.connect()
    q = f"""
        SELECT asset, MIN(block_number) AS min_block FROM (
            SELECT maker_asset_id AS asset, block_number FROM read_parquet({glob_lit})
            UNION ALL
            SELECT taker_asset_id AS asset, block_number FROM read_parquet({glob_lit})
        ) WHERE asset <> '{USDC_ASSET}'
        GROUP BY asset
    """
    residuals: list[float] = []
    for asset, min_block in con.execute(q).fetchall():
        cid = token_to_cid.get(str(asset))
        if not cid:
            continue
        created = meta.get(cid, {}).get("created_at")
        if created is None or min_block is None:
            continue
        residuals.append(created - block_seconds * float(min_block))
    con.close()
    if not residuals:
        raise SystemExit("block→epoch calibration found no anchors; widen --calib-files")
    residuals.sort()
    intercept = residuals[len(residuals) // 2]  # median
    return block_seconds, intercept


# --------------------------------------------------------------------------
# trades → price_index (YES-token price per condition)
# --------------------------------------------------------------------------

def build_price_index(trades_glob: str, target_yes_tokens: set[str],
                      token_to_cid: dict[str, str], slope: float, intercept: float,
                      *, max_trade_files: int = 0) -> dict[str, list]:
    """price_index[cid] = sorted [(epoch, yes_price)]. Scans only trades whose
    maker/taker asset is one of target_yes_tokens. price = usdc_amount /
    token_amount (the shared 1e6 scale cancels)."""
    files = sorted(glob.glob(trades_glob))
    if max_trade_files > 0:
        files = files[:max_trade_files]
    if not target_yes_tokens:
        return {}
    tok_lit = "(" + ",".join("'" + t.replace("'", "") + "'" for t in target_yes_tokens) + ")"
    con = duckdb.connect()
    by_cid: dict[str, list[tuple[float, float]]] = {}
    # chunk the file glob to bound memory on the full archive
    CHUNK = 2000
    for i in range(0, len(files), CHUNK):
        chunk = files[i:i + CHUNK]
        glob_lit = "[" + ",".join(repr(f) for f in chunk) + "]"
        q = f"""
            SELECT block_number, maker_asset_id, taker_asset_id, maker_amount, taker_amount
            FROM read_parquet({glob_lit})
            WHERE (maker_asset_id IN {tok_lit} AND taker_asset_id = '{USDC_ASSET}')
               OR (taker_asset_id IN {tok_lit} AND maker_asset_id = '{USDC_ASSET}')
        """
        for block, m_asset, t_asset, m_amt, t_amt in con.execute(q).fetchall():
            if str(m_asset) == USDC_ASSET:
                token, usdc, tok_amt = str(t_asset), float(m_amt), float(t_amt)
            else:
                token, usdc, tok_amt = str(m_asset), float(t_amt), float(m_amt)
            if tok_amt <= 0:
                continue
            price = usdc / tok_amt
            if not (0.0 <= price <= 1.0):
                continue
            cid = token_to_cid.get(token)
            if not cid:
                continue
            epoch = slope * float(block) + intercept
            by_cid.setdefault(cid, []).append((epoch, price))
    con.close()
    return {cid: sorted(pts) for cid, pts in by_cid.items()}


# --------------------------------------------------------------------------
# scope: which conditions does this wallet's counterfactual need?
# --------------------------------------------------------------------------

def scope_target_cids(activity_path: str, meta: dict[str, dict]) -> set[str]:
    """Candidate universe for a wallet: every market alive during the wallet's
    engagement window in the same coarse scopes she touched. Keeps the price-index
    build tractable vs. indexing the whole archive."""
    with open(activity_path) as f:
        activity = json.load(f)
    # record_fetch_snapshot shape uses `activity_rows`; tolerate a few aliases.
    rows = (activity.get("activity_rows") or activity.get("activity")
            or activity.get("positions") or (activity if isinstance(activity, list) else []))
    engagements = build_engagements(rows if isinstance(rows, list) else [])
    if not engagements:
        raise SystemExit(
            f"no engagements parsed from {activity_path} (looked for activity_rows). "
            "Refusing to fall back to a full-archive index — pass a valid wallet snapshot."
        )
    times = [int(e["first_entry_ts"]) for e in engagements]
    lo, hi = min(times), max(times)
    her_cats = set()
    for e in engagements:
        m = meta.get(str(e["conditionId"]), {})
        if m.get("category"):
            her_cats.add(m["category"])
    targets: set[str] = set()
    for cid, m in meta.items():
        ca, ed = m.get("created_at"), m.get("end_date")
        if ca is None:
            continue
        alive = ca <= hi and (ed is None or ed >= lo)
        if alive and (not her_cats or m.get("category") in her_cats):
            targets.add(cid)
    # always include her own picks
    targets |= {str(e["conditionId"]) for e in engagements}
    return targets


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--markets-glob", required=True)
    ap.add_argument("--trades-glob", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--wallet-activity", default=None,
                    help="scope the price index to one wallet's candidate universe")
    ap.add_argument("--calib-files", type=int, default=40,
                    help="trade files sampled for block→epoch calibration")
    ap.add_argument("--max-trade-files", type=int, default=0,
                    help="0 = all trade files; >0 = sample (smoke run)")
    ap.add_argument("--block-seconds", type=float, default=POLYGON_BLOCK_SECONDS)
    args = ap.parse_args()

    print("[1/4] loading markets …", file=sys.stderr)
    meta, token_to_cid = load_markets(args.markets_glob)
    print(f"      {len(meta)} binary markets, {len(token_to_cid)} tokens", file=sys.stderr)

    print("[2/4] calibrating block→epoch …", file=sys.stderr)
    slope, intercept = calibrate_block_epoch(
        args.trades_glob, token_to_cid, meta,
        sample_files=args.calib_files, block_seconds=args.block_seconds)
    print(f"      epoch = {slope:.3f}*block + {intercept:.0f}", file=sys.stderr)

    if args.wallet_activity:
        target_cids = scope_target_cids(args.wallet_activity, meta)
        print(f"[3/4] scoped to {len(target_cids)} candidate conditions", file=sys.stderr)
    else:
        target_cids = set(meta)
        print(f"[3/4] full build: {len(target_cids)} conditions", file=sys.stderr)
    target_yes = {meta[c]["yes_token"] for c in target_cids if c in meta}

    print("[4/4] building price index …", file=sys.stderr)
    price_index = build_price_index(
        args.trades_glob, target_yes, token_to_cid, slope, intercept,
        max_trade_files=args.max_trade_files)
    print(f"      priced {len(price_index)} conditions", file=sys.stderr)

    # MarketContext-shaped meta: epoch fields + resolved_yes for the edge label.
    out_meta = {c: {
        "created_at": meta[c]["created_at"],
        "end_date": meta[c]["end_date"],
        "category": meta[c]["category"],
        "market_type": meta[c]["market_type"],
        "outcomes": meta[c]["outcomes"],
        "winning_index": meta[c]["winning_index"],
        "resolved_yes": meta[c]["resolved_yes"],
    } for c in target_cids if c in meta}

    with open(args.out, "w") as f:
        json.dump({
            "market_meta": out_meta,
            "price_index": price_index,
            "_calibration": {"slope": slope, "intercept": intercept,
                             "block_seconds": args.block_seconds},
        }, f)
    print(f"wrote {args.out}: {len(out_meta)} markets, {len(price_index)} priced",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
