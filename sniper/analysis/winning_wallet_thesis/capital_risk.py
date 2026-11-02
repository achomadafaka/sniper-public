"""Capital & risk decomposition (Phase 6). The selection slices answer WHERE she is right;
this answers HOW she runs her money: per-slice budget share, bet-size distribution /
concentration, turnover ROI, and exit value added (loser-rescue vs winner-upside-lost).

peak_deployed is an interval-sweep approximation, and max_drawdown is realized-equity
only; mark-to-market drawdown still needs a price-path feed."""
from __future__ import annotations

import math
from statistics import mean, median

from sniper.analysis.winning_wallet_thesis.slicer import _entry_hour, _price_regime
from sniper.analysis.winning_wallet_thesis.types import (
    CapitalRiskProfile, CapitalRiskSlice, EdgeCluster, ExitValueSummary, PositionBook,
    PositionEpisode,
)


def _pct(num: float, den: float) -> float:
    return num / den * 100.0 if den else 0.0


def _p90(values: list[float]) -> float:
    if not values:
        return 0.0
    vals = sorted(values)
    idx = min(len(vals) - 1, math.ceil(0.9 * len(vals)) - 1)
    return vals[idx]


def _cv(values: list[float]) -> float:
    if not values:
        return 0.0
    m = mean(values)
    if m == 0:
        return 0.0
    var = sum((v - m) ** 2 for v in values) / len(values)
    return math.sqrt(var) / m


def _close_ts(ep: PositionEpisode) -> int | None:
    """Time the position's capital is freed: held -> market RESOLUTION (Gamma closedTime);
    sold-early -> the sell time (last_ts before resolution). Falls back to last activity
    only when no resolution time is available. This fixes the last_ts bias where resolved-
    but-not-redeemed positions looked never-closing and inflated peak."""
    if ep.resolution_ts:
        if ep.sell_shares > 0 and ep.last_ts and ep.last_ts < ep.resolution_ts:
            return ep.last_ts
        return ep.resolution_ts
    if ep.last_ts and ep.last_ts > (ep.first_buy_ts or 0):
        return ep.last_ts
    return ep.first_buy_ts


def _peak_deployed(episodes: list[PositionEpisode]) -> float | None:
    """Max capital simultaneously at risk: sweep each position's open interval
    [first_buy_ts, close_ts] and track running concurrent buy_cost. close_ts uses the real
    resolution time (see _close_ts). Approximation: full buy_cost over the life (partial
    sells not pro-rated). None if no timestamps."""
    events: list[tuple[int, int, float]] = []
    for ep in episodes:
        if ep.first_buy_ts is None or ep.buy_cost <= 0:
            continue
        events.append((ep.first_buy_ts, 0, ep.buy_cost))          # open (0 sorts before close)
        close = _close_ts(ep)
        if close and close > ep.first_buy_ts:
            events.append((close, 1, -ep.buy_cost))                # close
    if not events:
        return None
    events.sort(key=lambda e: (e[0], e[1]))
    running = peak = 0.0
    for _, _, delta in events:
        running += delta
        peak = max(peak, running)
    return round(peak, 2)


def _realized_max_drawdown(episodes: list[PositionEpisode]) -> float | None:
    """Largest peak-to-trough give-back in the REALIZED cumulative-PnL curve (PnL booked at
    each position's resolution/close ts). Honest limit: realized-only — not mark-to-market,
    since we have no price path to value open positions intra-life (the deferred feed)."""
    closed = [(_close_ts(ep) or 0, ep.realized_pnl or 0.0)
              for ep in episodes if ep.resolved and ep.won is not None]
    if not closed:
        return None
    closed.sort(key=lambda x: x[0])
    cum = peak = max_dd = 0.0
    for _, pnl in closed:
        cum += pnl
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    return round(max_dd, 2)


def capital_row(slice_key: str, axis: str, episodes: list[PositionEpisode], *,
                total_buy_cost: float) -> CapitalRiskSlice:
    costs = [ep.buy_cost for ep in episodes]
    buy_cost = sum(costs)
    pnl = sum(ep.realized_pnl or 0.0 for ep in episodes)
    by_event: dict[str, float] = {}
    for ep in episodes:
        by_event[ep.event_slug] = by_event.get(ep.event_slug, 0.0) + ep.buy_cost
    exits = exit_value_summary(episodes)
    peak = _peak_deployed(episodes)
    return CapitalRiskSlice(
        slice_key=slice_key,
        axis=axis,
        n_positions=len(episodes),
        n_events=len({ep.event_slug for ep in episodes if ep.event_slug}),
        buy_cost=round(buy_cost, 2),
        budget_share_pct=round(_pct(buy_cost, total_buy_cost), 2),
        realized_pnl=round(pnl, 2),
        turnover_roi_pct=round(_pct(pnl, buy_cost), 2),
        avg_bet=round(mean(costs), 2) if costs else 0.0,
        median_bet=round(median(costs), 2) if costs else 0.0,
        p90_bet=round(_p90(costs), 2),
        max_bet=round(max(costs), 2) if costs else 0.0,
        bet_cv=round(_cv(costs), 3),
        top_event_budget_share_pct=round(_pct(max(by_event.values(), default=0.0), buy_cost), 2),
        peak_deployed=peak,
        roi_on_peak_deployed_pct=round(_pct(pnl, peak), 2) if peak else None,
        max_drawdown=_realized_max_drawdown(episodes),
        exit_value_added=exits.sell_vs_hold_value,
        loser_rescue_value=exits.loser_rescue_value,
        winner_upside_lost=exits.winner_upside_lost,
    )


def exit_value_summary(episodes: list[PositionEpisode]) -> ExitValueSummary:
    """Did selling beat holding the SAME positions to resolution? Per sold position,
    value = sell_proceeds - sell_shares*won. Loser (won=0) -> rescued from ~$0 (positive);
    winner (won=1) -> upside given up (negative)."""
    n_sold = n_losers = n_winners = 0
    rescue = upside = total = 0.0
    for ep in episodes:
        if ep.sell_shares <= 0 or not ep.resolved or ep.won is None:
            continue
        n_sold += 1
        payout_if_held = ep.sell_shares * (0.5 if ep.is_push else (1.0 if ep.won else 0.0))
        value = ep.sell_proceeds - payout_if_held
        total += value
        if ep.won:
            n_winners += 1
            upside += value
        else:
            n_losers += 1
            rescue += value
    return ExitValueSummary(
        n_sold=n_sold,
        n_sold_losers=n_losers,
        n_sold_winners=n_winners,
        sell_vs_hold_value=round(total, 2),
        loser_rescue_value=round(rescue, 2),
        winner_upside_lost=round(upside, 2),
    )


def _axis_groups(episodes: list[PositionEpisode]) -> dict[str, dict[str, list[PositionEpisode]]]:
    groups: dict[str, dict[str, list[PositionEpisode]]] = {
        "category": {}, "tag": {}, "subcat": {}, "template": {}, "price": {},
        "mechanism": {}, "hour": {}, "tag_price": {},
    }
    for ep in episodes:
        for axis, key in {
            "category": ep.category or "unknown",
            "subcat": ep.subcat or "untagged",
            "template": ep.template or "untagged",
            "price": _price_regime(ep.entry_vwap),
            "mechanism": ep.exit_mechanism,
            "hour": _entry_hour(ep),
            "tag_price": f"{ep.subcat or 'untagged'} | {_price_regime(ep.entry_vwap)}",
        }.items():
            groups[axis].setdefault(key, []).append(ep)
        for tag in (ep.tags or ["untagged"]):
            groups["tag"].setdefault(tag, []).append(ep)
    return groups


def _ep_key(ep: PositionEpisode) -> tuple:
    return (ep.condition_id, ep.outcome_index, ep.episode_index)


def build_capital_risk_profile(wallet: str, book: PositionBook,
                               clusters: list[EdgeCluster], *, ledger=None) -> CapitalRiskProfile:
    episodes = book.resolved_directional()
    total_buy = sum(ep.buy_cost for ep in episodes)
    total_pnl = sum(ep.realized_pnl or 0.0 for ep in episodes)
    slices = {
        axis: [capital_row(key, axis, eps, total_buy_cost=total_buy)
               for key, eps in table.items()]
        for axis, table in _axis_groups(episodes).items()
    }
    cluster_risk: dict[str, CapitalRiskSlice] = {}
    by_key = {_ep_key(ep): ep for ep in episodes}
    warnings: list[str] = []
    for cluster in clusters:
        eps = [by_key[k] for k in cluster.member_position_keys if k in by_key]
        row = capital_row(cluster.label, f"cluster:{cluster.axis}", eps, total_buy_cost=total_buy)
        cluster_risk[cluster.cluster_id] = row
        if cluster.is_loss_center and row.budget_share_pct >= 20:
            warnings.append("capital_overallocated_to_loss_center")
        if row.bet_cv >= 1.5:
            warnings.append("high_bet_concentration")
    exits = exit_value_summary(episodes)
    peak = _peak_deployed(episodes)
    drawdown = _realized_max_drawdown(episodes)
    if exits.loser_rescue_value > 0:
        warnings.append("loss_cutting_adds_value")
    if exits.winner_upside_lost < 0:
        warnings.append("winner_selling_leaks_upside")
    # realized give-back larger than the final net result => a volatile equity path
    if drawdown is not None and drawdown > abs(total_pnl):
        warnings.append("realized_drawdown_exceeds_net_pnl")
    # full wallet realized PnL includes structural split/merge legs (e.g. a complete-set book),
    # which the selection population (resolved_directional) deliberately excludes.
    full_pnl = round(sum(ep.realized_pnl or 0.0 for ep in book.resolved_with_pnl()), 2)
    # reconciliation guard: raw net inventory cash (band-aid quantity) vs the cash attributed onto
    # legs. ~0 confirms the pair allocation neither dropped nor double-counted split/merge cash.
    # raw_inv from the ledger rows when supplied, else from the baskets (usdc == size for sets).
    if ledger is not None:
        raw_inv = (sum(r.usdc for r in ledger.rows if r.kind == "merge")
                   - sum(r.usdc for r in ledger.rows if r.kind == "split"))
    else:
        raw_inv = sum(b.merge_shares - b.split_shares for b in book.baskets.values())
    attr_inv = sum((ep.merge_cash - ep.split_cost) for ep in book.episodes)
    inventory_reconciliation = round(raw_inv - attr_inv, 2)
    if abs(inventory_reconciliation) > max(1.0, 0.01 * abs(full_pnl)):
        warnings.append("inventory_unreconciled")
    return CapitalRiskProfile(wallet, round(total_buy, 2), round(total_pnl, 2),
                              slices=slices, cluster_risk=cluster_risk,
                              exit_summary=exits, warnings=sorted(set(warnings)),
                              peak_deployed=peak, max_drawdown=drawdown,
                              roi_on_peak_deployed_pct=round(_pct(total_pnl, peak), 2) if peak else None,
                              total_realized_pnl_with_inventory=full_pnl,
                              inventory_reconciliation=inventory_reconciliation)
