"""Maker/microstructure detectors (#11/#13/#14). The Data API exposes no maker/taker or
order detail, so these stay BLOCKED until an identity-bearing fill feed is supplied. With a
fill provider (Track 2 Goldsky fills) they compute real proxies: maker fraction, and post-fill
price drift reconstructed from SUBSEQUENT fills on the same asset (a sparse path, NOT an order
book — explicitly a proxy). The wallet is derived from ctx.book.wallet (no wallet= param).

Both detectors are CLUSTER-SCOPED (spec §4.6 — per cluster, not whole-wallet): they only count
fills on the conditions this cluster actually covers, so a cluster with no fills of its own stays
blocked instead of inheriting a wallet-level number. Adverse-selection drift is sign-adjusted by
the wallet's maker side (a maker buyer profits from up-drift; a maker seller is picked off by it)."""
from __future__ import annotations

from sniper.analysis.winning_wallet_thesis.data.fill_identity import FillIdentityProvider
from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.methods.types import MethodFinding

_BIG_TS = 1 << 62


def _wallet(ctx: ClusterContext) -> str:
    return (ctx.book.wallet or "").lower()


def _cluster_conditions(ctx: ClusterContext) -> set[str]:
    """Condition ids this cluster covers. Empty => no condition we can attribute fills to, so
    microstructure evidence must NOT fall back to the whole wallet."""
    return {ep.condition_id for ep in ctx.episodes if ep.condition_id}


def _token_id(f) -> str:
    """Outcome-token id a fill trades (the USDC side is asset '0')."""
    return f.maker_asset_id if f.maker_asset_id != "0" else f.taker_asset_id


def _maker_bought_token(f, w: str) -> bool:
    """Wallet is the maker; it BOUGHT the token iff it paid USDC (gave asset '0'); else it SOLD.
    Determines the sign of adverse drift: up-drift is favorable to a buyer, adverse to a seller."""
    return (f.maker or "").lower() == w and f.maker_asset_id == "0"


def _blocked(method: str, blocker: str) -> MethodFinding:
    return MethodFinding(method, "blocked_missing_feed", 0, None, 0,
                         evidence={}, controls={}, blockers=[blocker], runnability="data_blocked")


def market_making_spread_capture(ctx: ClusterContext, *,
                                 fill_identity: FillIdentityProvider | None = None) -> MethodFinding:
    if fill_identity is None or not fill_identity.available:
        return _blocked("market_making_spread_capture", "missing_maker_taker_or_order_detail")
    w = _wallet(ctx)
    conds = _cluster_conditions(ctx)
    if not conds:
        return _blocked("market_making_spread_capture", "no_cluster_conditions")
    wf = [f for f in fill_identity.fills_for_wallet(w, start_ts=0, end_ts=_BIG_TS)
          if f.condition_id in conds]
    if not wf:
        return _blocked("market_making_spread_capture", "missing_wallet_identified_fills")
    maker = sum(1 for f in wf if (f.maker or "").lower() == w)
    frac = maker / len(wf)
    return MethodFinding("market_making_spread_capture", "descriptive", frac * 100, frac, len(wf),
                         evidence={"maker_fraction": round(frac, 4), "n_fills": len(wf),
                                   "n_conditions": len(conds)},
                         controls={"note": "maker_fraction_is_attribution_not_a_promotable_edge",
                                   "scope": "cluster_conditions_only"},
                         blockers=[], runnability="descriptive_only")


def adverse_selection_aware_making(ctx: ClusterContext, *,
                                   fill_identity: FillIdentityProvider | None = None) -> MethodFinding:
    if fill_identity is None or not fill_identity.available:
        return _blocked("adverse_selection_aware_making", "missing_passive_fill_baseline")
    w = _wallet(ctx)
    conds = _cluster_conditions(ctx)
    if not conds:
        return _blocked("adverse_selection_aware_making", "no_cluster_conditions")
    maker_fills = [f for f in fill_identity.fills_for_wallet(w, start_ts=0, end_ts=_BIG_TS)
                   if (f.maker or "").lower() == w and f.price is not None
                   and f.condition_id in conds]
    if not maker_fills:
        return _blocked("adverse_selection_aware_making", "missing_wallet_identified_fills")
    drifts: list[float] = []
    for mf in maker_fills:
        token = _token_id(mf)
        bought = _maker_bought_token(mf, w)
        later = sorted((x for x in fill_identity.fills_for_market(mf.condition_id, start_ts=mf.ts + 1,
                                                                  end_ts=_BIG_TS)
                        if x.price is not None and _token_id(x) == token), key=lambda x: x.ts)
        if later:
            raw = later[-1].price - mf.price                # sparse path: last subsequent same-token fill
            drifts.append(raw if bought else -raw)          # favorable-to-the-maker-side drift
    if not drifts:
        return _blocked("adverse_selection_aware_making", "no_subsequent_fills_for_drift")
    avg_drift = sum(drifts) / len(drifts)
    state = "supported_proxy" if avg_drift >= 0 else "contradicted"   # not picked off vs adverse
    return MethodFinding("adverse_selection_aware_making", state, max(avg_drift, 0.0) * 100, avg_drift,
                         len(drifts),
                         evidence={"avg_favorable_drift": round(avg_drift, 4), "n_maker_fills": len(drifts)},
                         controls={"proxy": "drift_from_subsequent_same_token_fills_not_orderbook",
                                   "sign": "positive=favorable_to_maker_side(buyer:+drift,seller:-drift)",
                                   "scope": "cluster_conditions_only"},
                         blockers=[],
                         runnability="replication_required" if state == "supported_proxy" else "descriptive_only")
