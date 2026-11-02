"""Fetch a wallet's historical CLOB fills from the Goldsky order-book subgraph and expose them
through `OnchainFillIdentityProvider` (Track 2).

Reuses the existing, tested `sniper.analysis.wallet_protocol.subgraph_reader` (public Goldsky
`orderbook-subgraph` endpoint, pagination, HTTP-200-errors handling) rather than building a new
GraphQL client — see docs/audits/2026-05-31-goldsky-fill-endpoint-gate.md (verdict: PROCEED).
Operator-triggered, cached per wallet, no live loop. The row-fetch fn is injectable for offline
tests. Cache discipline: a transient subgraph failure raises and is NOT cached; only a successful
response (including a genuine empty list) is written."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, replace
from typing import Callable, Iterable

from sniper.analysis.wallet_protocol import subgraph_reader as SR
from sniper.analysis.winning_wallet_thesis.data.onchain_fill_identity import OnchainFillIdentityProvider
from sniper.feeds.fill_normalizer import fill_event_from_subgraph_row
from sniper.feeds.thesis_feed_types import FillEvent

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "runs", "wallet_anatomy")


def fetch_wallet_fills(wallet: str, *, refresh: bool = False, cache_dir: str | None = None,
                       fetch_fn: Callable[[str], list[dict]] | None = None) -> list[FillEvent]:
    """Return the wallet's fills as `FillEvent`s, cached to `<cache>/<wallet>_fills.json`.
    `fetch_fn(wallet) -> list[dict]` defaults to the Goldsky reader (maker ∪ taker, deduped)."""
    root = cache_dir or CACHE_DIR
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, f"{wallet.lower()}_fills.json")
    if os.path.exists(path) and not refresh:
        with open(path) as f:
            return [FillEvent(**r) for r in json.load(f)]
    fetch = fetch_fn or (lambda w: SR.fetch_order_filled_events(w, side="both"))
    rows = fetch(wallet)                                      # raises on transient failure -> no cache write
    fills = [fill_event_from_subgraph_row(r) for r in rows]
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump([asdict(fe) for fe in fills], f)
    os.replace(tmp, path)
    return fills


def attach_condition_ids(fills: list[FillEvent], asset_to_condition: dict[str, str]) -> list[FillEvent]:
    """Tag each fill with its condition_id. The non-USDC side of a fill is the outcome token
    (USDC asset id is '0'); map that token to its condition. FillEvent is frozen -> replace.
    A token missing from the map leaves condition_id=None (simply ungrouped, not an error)."""
    out = []
    for f in fills:
        token = f.maker_asset_id if f.maker_asset_id != "0" else f.taker_asset_id
        out.append(replace(f, condition_id=asset_to_condition.get(token)))
    return out


def asset_to_condition_from_activity(raw_activity: Iterable[dict]) -> dict[str, str]:
    """Build outcome-token-id -> condition_id from the wallet's OWN activity rows — free, no API.
    TRADE rows carry both `asset` (the token id) and `conditionId`, and the wallet's fills are on
    exactly these assets, so this covers every fill. Preferred over the live builder below."""
    out: dict[str, str] = {}
    for a in raw_activity:
        asset = str(a.get("asset") or "")
        cond = str(a.get("conditionId") or a.get("condition_id") or "")
        if asset and cond:
            out[asset] = cond
    return out


def build_asset_to_condition(condition_ids: Iterable[str], *, session=None) -> dict[str, str]:
    """Live fallback: outcome-token-id -> condition_id via Gamma clobTokenIds (one request per
    condition). Uses fetch_market_by_condition (resolved-INCLUSIVE), NOT the active-only
    fetch_market_index. Prefer asset_to_condition_from_activity when cached activity is available."""
    from sniper.markets import polymarket_api as P
    sess = session or P.build_session()
    out: dict[str, str] = {}
    for cond in {c for c in condition_ids if c}:
        market = P.fetch_market_by_condition(sess, cond)
        for token in P._parse_token_ids(market.get("clobTokenIds")):
            out[str(token)] = cond
    return out


def build_onchain_fill_provider(wallet: str, *, asset_to_condition: dict[str, str] | None = None,
                                condition_ids: Iterable[str] | None = None,
                                refresh: bool = False) -> OnchainFillIdentityProvider:
    """Fetch the wallet's fills and (optionally) tag them with condition_id so `fills_for_market`
    works. Pass `asset_to_condition` (build it free via asset_to_condition_from_activity) to enable
    first-attention/adverse-selection; or `condition_ids` to build the map live. Omit both for
    maker-fraction (which only needs fills_for_wallet)."""
    fills = fetch_wallet_fills(wallet, refresh=refresh)
    amap = asset_to_condition
    if amap is None and condition_ids:
        amap = build_asset_to_condition(condition_ids)
    if amap:
        fills = attach_condition_ids(fills, amap)
    return OnchainFillIdentityProvider(fills=fills)
