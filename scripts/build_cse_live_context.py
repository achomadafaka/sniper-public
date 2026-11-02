#!/usr/bin/env python3
"""Build a CSE point-in-time MarketContext from the LIVE Polymarket API.

The Becker archive froze at 2026-02-03; for an active wallet most decisions are
newer than that, so the counterfactual cannot be reconstructed from Becker. This
builds the same MarketContext { market_meta, price_index } from live endpoints,
current to today and with REAL trade timestamps (no block→epoch approximation):

  Gamma  /markets?condition_ids=…   → her picks' metadata (created/end/tokens/res)
  Gamma  /markets (paginated)       → the candidate universe alive in her window
  CLOB   /prices-history            → per-token price series → price_index

Scope (tractability + a valid counterfactual): candidates are binary markets whose
[start,end] overlaps her engagement window, in the categories she actually traded,
above a volume floor, capped at --max-candidates by recency. Every cap is logged
(F0/G1: no silent truncation).

Output loads directly via MarketContext.from_json_file.

Usage:
  python3 scripts/build_cse_live_context.py \
      --wallet-activity runs/swissmiss_cycle/swissmiss_snapshot.json \
      --out runs/swissmiss_cycle/cse_ctx_live.json \
      --max-candidates 800 --volume-floor 5000
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import httpx

sys.path.insert(0, ".")
from sniper.markets.polymarket_api import (  # noqa: E402
    build_session, _request_json, _parse_token_ids, _parse_iso_ts, GAMMA_BASE_URL,
)
from sniper.analysis.counterfactual_selection.entity_layers import build_engagements  # noqa: E402

CLOB_PRICES_HISTORY = "https://clob.polymarket.com/prices-history"

_CATEGORY_KEYWORDS = {
    "sports": ("nba", "nfl", "mlb", "nhl", "soccer", "ufc", "tennis", "epl",
               "premier-league", "champions-league", "game", "match", "-vs-"),
    "politics": ("election", "president", "senate", "congress", "governor",
                 "trump", "biden", "primary", "parliament", "vote", "act becomes law"),
    "crypto": ("bitcoin", "btc", "ethereum", "eth", "solana", "crypto", "coin"),
    "finance": ("fed", "rate", "cpi", "gdp", "inflation", "stock", "nasdaq", "s&p"),
}


def _category(slug: str | None, question: str | None) -> str:
    blob = f"{slug or ''} {question or ''}".lower()
    for cat, kws in _CATEGORY_KEYWORDS.items():
        if any(k in blob for k in kws):
            return cat
    return "unknown"


def _market_meta_from_gamma(m: dict) -> dict | None:
    """Gamma market dict → CSE meta. Binary only. resolved_yes from outcomePrices."""
    tokens = _parse_token_ids(m.get("clobTokenIds"))
    if len(tokens) != 2:
        return None
    created = _parse_iso_ts(m.get("createdAt") or m.get("startDate"))
    end = _parse_iso_ts(m.get("endDate") or m.get("endDateIso"))
    if not created:
        return None
    outcomes = m.get("outcomes")
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except Exception:
            outcomes = []
    yes_idx = 0  # index-0 (YES) is the recorded side; the other is 1 - it.
    prices = m.get("outcomePrices")
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except Exception:
            prices = []
    resolved_yes = None
    winning_index = None
    if m.get("closed") and prices:
        try:
            fp = [float(x) for x in prices]
            winning_index = max(range(len(fp)), key=lambda i: fp[i]) if max(fp) >= 0.5 else None
            resolved_yes = 1 if fp[yes_idx] >= 0.5 else 0
        except Exception:
            resolved_yes = winning_index = None
    try:
        volume = float(m.get("volume") or m.get("volumeNum") or 0.0) or None
    except (TypeError, ValueError):
        volume = None
    event_family = (str(m.get("eventSlug") or "").strip()
                    or str(m.get("groupItemTitle") or "").strip() or None)
    return {
        "created_at": created, "end_date": end or None,
        "outcomes": [str(o) for o in (outcomes or [])],
        "winning_index": winning_index, "volume": volume, "event_family": event_family,
        "category": _category(m.get("slug"), m.get("question")),
        "market_type": "binary", "yes_token": tokens[yes_idx],
        "resolved_yes": resolved_yes,
    }


def fetch_market_closed_aware(session, cid: str) -> dict:
    """Gamma /markets?condition_ids — the default query omits resolved markets, so
    most of an active wallet's (closed) picks return empty. Try default, then
    closed=true."""
    for params in ({"condition_ids": cid, "limit": 1},
                   {"condition_ids": cid, "closed": "true", "limit": 1}):
        rows = _request_json(session, f"{GAMMA_BASE_URL}/markets", params=params)
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return rows[0]
    return {}


def fetch_price_series(token: str, *, fidelity: int = 180, timeout: float = 20.0,
                       session: httpx.Client | None = None,
                       start: int | None = None, end: int | None = None
                       ) -> list[tuple[int, float]]:
    """Per-token price series via CLOB prices-history. Real epoch timestamps,
    sorted [(ts, yes_price)]. When the market's active window (start,end) is
    given, query it explicitly — `interval=max` returns EMPTY for resolved
    markets, but startTs/endTs over the active window recovers the history."""
    cl = session or httpx
    if start and end:
        params = {"market": token, "startTs": int(start), "endTs": int(end), "fidelity": fidelity}
    else:
        params = {"market": token, "interval": "max", "fidelity": fidelity}
    try:
        r = cl.get(CLOB_PRICES_HISTORY, params=params, timeout=timeout)
        if r.status_code >= 400:
            return []
        hist = (r.json() or {}).get("history") or []
    except Exception:
        return []
    out = []
    for bar in hist:
        try:
            out.append((int(bar["t"]), float(bar["p"])))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out)


def enumerate_candidates(session, *, lo: int, hi: int, categories: set[str],
                         volume_floor: float, max_candidates: int,
                         max_pages: int) -> list[dict]:
    """Gamma markets whose [start,end] overlaps [lo,hi], binary, in `categories`,
    volume >= floor. The server pre-filters to the window via end_date_min +
    start_date_max, so the closed pass yields RESOLVED in-window markets (the
    counterfactual the edge test needs), not just currently-open high-volume ones.
    Paginate both closed states; cap at max_candidates."""
    lo_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(lo))
    hi_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(hi))
    seen: dict[str, dict] = {}
    for closed in ("true", "false"):
        offset, page = 0, 0
        while page < max_pages and len(seen) < max_candidates:
            page += 1
            rows = _request_json(session, f"{GAMMA_BASE_URL}/markets",
                                 params={"closed": closed, "limit": 500,
                                         "offset": offset, "order": "volumeNum",
                                         "ascending": "false",
                                         "end_date_min": lo_iso, "start_date_max": hi_iso})
            if not isinstance(rows, list) or not rows:
                break
            for m in rows:
                cid = str(m.get("conditionId") or "")
                if not cid or cid in seen:
                    continue
                try:
                    vol = float(m.get("volume") or m.get("volumeNum") or 0.0)
                except (TypeError, ValueError):
                    vol = 0.0
                if vol < volume_floor:
                    continue
                meta = _market_meta_from_gamma(m)
                if not meta:
                    continue
                if categories and meta["category"] not in categories:
                    continue
                ca, ed = meta["created_at"], meta["end_date"]
                if ca > hi or (ed is not None and ed < lo):  # no overlap with her window
                    continue
                seen[cid] = meta
                if len(seen) >= max_candidates:
                    break
            offset += 500
            if len(rows) < 500:
                break
    return [{"cid": c, **mt} for c, mt in seen.items()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wallet-activity", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-candidates", type=int, default=800)
    ap.add_argument("--volume-floor", type=float, default=5000.0)
    ap.add_argument("--max-pages", type=int, default=60)
    ap.add_argument("--fidelity", type=int, default=180)
    args = ap.parse_args()

    snap = json.load(open(args.wallet_activity))
    rows = snap.get("activity_rows") or []
    engagements = build_engagements(rows)
    if not engagements:
        raise SystemExit("no engagements parsed from activity_rows")
    her_cids = [str(e["conditionId"]) for e in engagements]
    times = [int(e["first_entry_ts"]) for e in engagements]
    lo, hi = min(times), max(times)
    print(f"[1/4] {len(engagements)} engagements, window "
          f"{time.strftime('%Y-%m-%d', time.gmtime(lo))} → "
          f"{time.strftime('%Y-%m-%d', time.gmtime(hi))}", file=sys.stderr)

    session = build_session()
    # her picks' meta
    meta: dict[str, dict] = {}
    her_cats: set[str] = set()
    for cid in her_cids:
        m = fetch_market_closed_aware(session, cid)
        mt = _market_meta_from_gamma(m) if m else None
        if mt:
            meta[cid] = mt
            her_cats.add(mt["category"])
    print(f"[2/4] resolved {len(meta)}/{len(her_cids)} of her picks; "
          f"categories={sorted(her_cats)}", file=sys.stderr)

    # candidate universe
    cands = enumerate_candidates(session, lo=lo, hi=hi, categories=her_cats,
                                 volume_floor=args.volume_floor,
                                 max_candidates=args.max_candidates,
                                 max_pages=args.max_pages)
    capped = len(cands) >= args.max_candidates
    for c in cands:
        meta.setdefault(c["cid"], {k: c[k] for k in
                                   ("created_at", "end_date", "category",
                                    "market_type", "yes_token", "resolved_yes",
                                    "outcomes", "winning_index", "volume", "event_family")})
    print(f"[3/4] {len(cands)} candidate markets "
          f"({'CAPPED at ' + str(args.max_candidates) if capped else 'no cap hit'}, "
          f"volume_floor={args.volume_floor})", file=sys.stderr)

    # price series for every market (picks + candidates)
    price_index: dict[str, list] = {}
    n = 0
    for cid, mt in meta.items():
        tok = mt.get("yes_token")
        if not tok:
            continue
        series = fetch_price_series(tok, fidelity=args.fidelity, session=session,
                                    start=mt.get("created_at"), end=mt.get("end_date"))
        if series:
            price_index[cid] = series
        n += 1
        if n % 100 == 0:
            print(f"      priced {n}/{len(meta)} …", file=sys.stderr)
    print(f"[4/4] priced {len(price_index)}/{len(meta)} markets", file=sys.stderr)

    out_meta = {c: {k: m.get(k) for k in
                    ("created_at", "end_date", "category", "market_type", "resolved_yes",
                     "outcomes", "winning_index", "volume", "event_family")}
                for c, m in meta.items()}
    with open(args.out, "w") as f:
        json.dump({"market_meta": out_meta, "price_index": price_index,
                   "_source": "live_polymarket_api",
                   "_window": [lo, hi], "_candidates_capped": capped}, f)
    print(f"wrote {args.out}: {len(out_meta)} markets, {len(price_index)} priced",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
