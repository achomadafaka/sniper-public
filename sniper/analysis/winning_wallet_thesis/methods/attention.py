from __future__ import annotations

from sniper.analysis.winning_wallet_thesis.data.fill_identity import FillIdentityProvider
from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.methods.types import MethodFinding


def _norm(wallet: str | None) -> str:
    return (wallet or "").lower()


def _fill_involves(fill, wallet: str) -> bool:
    return _norm(getattr(fill, "maker", "")) == wallet or _norm(getattr(fill, "taker", "")) == wallet


def _entrant_wallet(fill) -> str:
    """Conservative proxy: count maker as the active entrant. Full same-side inference needs
    asset-side classification, so this stays supported_proxy rather than supported_direct."""
    return _norm(getattr(fill, "maker", ""))


def first_attention_discovery(ctx: ClusterContext, *,
                              fill_identity: FillIdentityProvider,
                              first_n: int = 2,
                              follower_window_seconds: int = 7200,
                              min_followers: int = 2) -> MethodFinding:
    """First-attention / follower-flow (#18/#19/#25). Needs per-fill wallet identity, which
    the Data API does not expose — BLOCKED until an identity-bearing fill feed exists."""
    if not fill_identity.available:
        return MethodFinding("first_attention_discovery", "blocked_missing_feed", 0, None, 0,
                             evidence={}, controls={}, blockers=["missing_wallet_identified_fills"],
                             runnability="data_blocked")
    evaluable = first_attention_events = follower_total = 0
    for ep in ctx.episodes:
        if ep.first_buy_ts is None:
            continue
        wallet = _norm(ep.wallet)
        fills = sorted(
            fill_identity.fills_for_market(
                ep.condition_id,
                start_ts=0,
                end_ts=ep.first_buy_ts + follower_window_seconds,
            ),
            key=lambda f: f.ts,
        )
        own = next((f for f in fills if f.ts <= ep.first_buy_ts and _fill_involves(f, wallet)), None)
        if own is None:
            continue
        evaluable += 1
        prior_wallets = {
            entrant for entrant in (_entrant_wallet(f) for f in fills if f.ts < own.ts)
            if entrant and entrant != wallet
        }
        follower_wallets = {
            entrant for entrant in (_entrant_wallet(f) for f in fills if own.ts < f.ts <= own.ts + follower_window_seconds)
            if entrant and entrant != wallet and entrant not in prior_wallets
        }
        follower_total += len(follower_wallets)
        if len(prior_wallets) < first_n and len(follower_wallets) >= min_followers:
            first_attention_events += 1
    if evaluable == 0:
        return MethodFinding("first_attention_discovery", "blocked_missing_feed", 0, None, 0,
                             evidence={}, controls={}, blockers=["missing_wallet_identified_fill_window"],
                             runnability="data_blocked")
    effect = first_attention_events / evaluable
    state = "supported_proxy" if first_attention_events else "descriptive"
    return MethodFinding("first_attention_discovery", state, effect * 100, effect, evaluable,
                         evidence={
                             "first_attention_events": first_attention_events,
                             "independent_followers": follower_total,
                             "first_n": first_n,
                             "follower_window_seconds": follower_window_seconds,
                         },
                         controls={"guard": "maker_wallet_proxy_no_same_side_proof"},
                         blockers=[],
                         runnability="replication_required" if state == "supported_proxy" else "descriptive_only")
