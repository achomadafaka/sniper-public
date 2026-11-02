#!/usr/bin/env python3
"""Build a CSE C4 (event-sibling) MarketContext from the live Gamma API.

For each EVENT the wallet engaged (shared eventSlug), fetch the FULL market
roster — every sibling market she could have picked within that event — so the
counterfactual becomes "why THIS market/threshold/fighter, not the OTHERS of the
same event?". That controls for category, timing, and context by construction:
e.g. within "US strikes Iran by <date>" (65 date variants) which thresholds did
she pick; within a UFC card (8-12 fights) which fighters.

Engagements come from activity_rows (real entry timestamps); her exact side
(outcome_index) + event_family (eventSlug) are enriched from closed_positions.
Output loads via MarketContext.from_json_file and runs with
build_decision_matrix(..., real_scope_axes=("event_family",)).

Usage:
  python3 scripts/build_cse_event_context.py \
      --wallet-activity runs/swissmiss_cycle/swissmiss_full.json \
      --out runs/swissmiss_cycle/cse_ctx_events.json \
      --min-event-picks 2 --max-events 150
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter

sys.path.insert(0, ".")
from sniper.markets.polymarket_api import build_session, _request_json, GAMMA_BASE_URL  # noqa: E402
from sniper.analysis.counterfactual_selection.entity_layers import build_engagements  # noqa: E402
from scripts.build_cse_live_context import _market_meta_from_gamma, fetch_price_series  # noqa: E402


def enrich_engagements(engagements, closed_positions):
    """Attach her exact side (outcome_index) + event_family (eventSlug) from the
    positions snapshot, joined on the held token (asset)."""
    by_asset = {str(p.get("asset")): p for p in (closed_positions or [])}
    for e in engagements:
        p = by_asset.get(str(e.get("asset")))
        if not p:
            continue
        if p.get("outcomeIndex") is not None:
            try:
                e["outcome_index"] = int(p["outcomeIndex"])
            except (TypeError, ValueError):
                pass
        e["event_family"] = str(p.get("eventSlug") or "").strip() or None
    return engagements


def fetch_event_roster(session, slug: str) -> list[dict]:
    rows = _request_json(session, f"{GAMMA_BASE_URL}/events", params={"slug": slug})
    if not isinstance(rows, list) or not rows:
        return []
    return rows[0].get("markets") or []


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wallet-activity", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--min-event-picks", type=int, default=2,
                    help="only events where she picked >= this many markets (so siblings exist)")
    ap.add_argument("--max-events", type=int, default=150)
    ap.add_argument("--fidelity", type=int, default=180)
    args = ap.parse_args()

    snap = json.load(open(args.wallet_activity))
    eng = build_engagements(snap.get("activity_rows") or [])
    eng = enrich_engagements(eng, snap.get("closed_positions") or [])
    ev_counts = Counter(e.get("event_family") for e in eng if e.get("event_family"))
    events = [s for s, _ in ev_counts.most_common(args.max_events)
              if s and ev_counts[s] >= args.min_event_picks]
    print(f"[1/3] {len(eng)} engagements across {len(ev_counts)} events; "
          f"{len(events)} events with >={args.min_event_picks} picks (cap {args.max_events})",
          file=sys.stderr)

    session = build_session()
    meta: dict[str, dict] = {}
    for i, slug in enumerate(events):
        for m in fetch_event_roster(session, slug):
            cid = str(m.get("conditionId") or "")
            if not cid:
                continue
            mt = _market_meta_from_gamma(m)
            if not mt:
                continue
            mt["event_family"] = slug
            meta[cid] = mt
        if (i + 1) % 40 == 0:
            print(f"      {i + 1}/{len(events)} rosters → {len(meta)} markets", file=sys.stderr)
    print(f"[2/3] {len(meta)} roster markets across {len(events)} events", file=sys.stderr)

    price_index: dict[str, list] = {}
    for j, (cid, mt) in enumerate(meta.items()):
        tok = mt.get("yes_token")
        if not tok:
            continue
        ser = fetch_price_series(tok, fidelity=args.fidelity, session=session,
                                 start=mt.get("created_at"), end=mt.get("end_date"))
        if ser:
            price_index[cid] = ser
        if (j + 1) % 250 == 0:
            print(f"      priced {j + 1}/{len(meta)} …", file=sys.stderr)
    print(f"[3/3] priced {len(price_index)}/{len(meta)} markets", file=sys.stderr)

    out_meta = {c: {k: m.get(k) for k in
                    ("created_at", "end_date", "category", "market_type", "resolved_yes",
                     "outcomes", "winning_index", "volume", "event_family")}
                for c, m in meta.items()}
    with open(args.out, "w") as f:
        json.dump({"market_meta": out_meta, "price_index": price_index,
                   "_source": "live_event_rosters", "_n_events": len(events)}, f)
    print(f"wrote {args.out}: {len(out_meta)} markets, {len(price_index)} priced",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
