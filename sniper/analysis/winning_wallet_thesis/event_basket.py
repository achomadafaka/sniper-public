"""Full event-level exposure accounting for negRisk / split / merge baskets (spec §3.3,
§4.3 — Push 2). An event is ONE unit even when it spans many condition legs, so basket/arb
claims never get a fake per-leg n."""
from __future__ import annotations

from sniper.analysis.winning_wallet_thesis.types import EventBasket, WalletEventLedger


def build_event_basket_accounting(ledger: WalletEventLedger) -> dict[str, EventBasket]:
    baskets: dict[str, EventBasket] = {}
    for row in ledger.rows:
        event = row.event_slug or row.condition_id or "wallet"
        basket = baskets.setdefault(event, EventBasket(event_slug=event))
        if row.condition_id:
            basket.condition_ids.add(row.condition_id)
        if row.kind == "buy":
            basket.net_cost += row.usdc
        elif row.kind == "sell":
            basket.net_proceeds += row.usdc
        elif row.kind == "redeem":
            basket.redeem_payout += row.usdc
        elif row.kind == "split":
            basket.split_shares += row.size
        elif row.kind == "merge":
            basket.merge_shares += row.size
        elif row.kind == "reward":
            basket.rewards += row.usdc
    for basket in baskets.values():
        basket.final_pnl = (basket.net_proceeds + basket.redeem_payout + basket.rewards
                            - basket.net_cost)
    return baskets
