"""event_level_position_builder — folds the enriched ledger into episodes + baskets.

Directional episodes are keyed (condition, outcome, episode); a flat-then-rebuy on the
same outcome opens a NEW episode. SPLIT/MERGE are attributed to per-leg episodes for
wallet-total PnL, but remain non-directional unless the wallet also has BUY exposure on
that leg. Rows are processed in (ts, seq) order so same-timestamp legs are deterministic."""
from __future__ import annotations

from collections import defaultdict

from sniper.analysis.winning_wallet_thesis.enrichment import parent_category, specific_tag
from sniper.analysis.winning_wallet_thesis.position_ledger import (
    allocate_merge_pair, allocate_split_pair,
)
from sniper.analysis.winning_wallet_thesis.types import (
    EnrichedLedger, EventBasket, PositionBook, PositionEpisode,
)


def build_position_book(enriched: EnrichedLedger) -> PositionBook:
    led = enriched.ledger
    episodes: dict[tuple, PositionEpisode] = {}
    baskets: dict[str, EventBasket] = {}
    rewards: dict[str, float] = defaultdict(float)
    current_episode: dict[tuple[str, int], int] = defaultdict(int)

    def basket(slug, cond) -> EventBasket:
        b = baskets.get(slug)
        if b is None:
            b = EventBasket(event_slug=slug)
            baskets[slug] = b
        if cond:
            b.condition_ids.add(cond)
        return b

    def get(cond, oidx, outcome, title, slug, episode) -> PositionEpisode:
        key = (cond, oidx, episode)
        e = episodes.get(key)
        if e is None:
            e = PositionEpisode(wallet=led.wallet, condition_id=cond, event_slug=slug,
                                outcome_index=oidx, episode_index=episode,
                                outcome=outcome or "", title=title or "")
            episodes[key] = e
        return e

    def leg_vwap(cond, oidx) -> float | None:
        """The leg's revealed buy price (running VWAP of the active episode) at split time."""
        e = episodes.get((cond, oidx, current_episode[(cond, oidx)]))
        return (e.buy_cost / e.buy_shares) if (e and e.buy_shares > 0) else None

    def apply_conversion(eff, ts) -> None:
        """negRisk convertPositions: burn NO legs (release running cost basis) and mint YES legs
        (inherit released-cost minus collateral, split equally). Time-ordered so the burned NO's
        cost reflects holdings at conversion time and chained conversions compose. Conversion legs
        are structural (no buy_shares) -> never enter the selection population."""
        n_yes = len(eff.yes_minted)
        released = 0.0
        burned = []
        for leg in eff.no_burned:
            e = get(leg.condition_id, leg.outcome_index, leg.outcome, "", eff.event_slug,
                    current_episode[(leg.condition_id, leg.outcome_index)])
            held = (e.buy_shares + e.split_shares + e.convert_in_shares
                    - e.sell_shares - e.merge_shares - e.convert_out_shares)
            cost = e.buy_cost + e.split_cost + e.convert_in_cost - e.convert_out_cost
            amt = abs(leg.share_delta)
            per_share = (cost / held) if held > 1e-9 else 0.0
            rel = min(amt, held) * per_share if held > 1e-9 else 0.0
            e.convert_out_shares += amt
            e.last_ts = max(e.last_ts, ts)
            burned.append((e, rel)); released += rel
        if n_yes > 0:
            # transfer model: burned-NO cost basis (minus collateral) moves to the minted YES legs.
            for e, rel in burned:
                e.convert_out_cost += rel
            per_yes = (released - eff.collateral_usdc) / n_yes  # collateral captured as reduced YES cost
            for leg in eff.yes_minted:
                e = get(leg.condition_id, leg.outcome_index, leg.outcome, "", eff.event_slug,
                        current_episode[(leg.condition_id, leg.outcome_index)])
                e.convert_in_shares += abs(leg.share_delta)
                e.convert_in_cost += per_yes
                e.last_ts = max(e.last_ts, ts)
        else:
            # zero-YES (NO burned on every outcome): the set was SOLD for collateral. Do NOT release
            # the cost (no YES recipient); book the collateral as cash so realized = collateral − cost.
            share = eff.collateral_usdc / len(burned) if burned else 0.0
            for e, _rel in burned:
                e.convert_cash += share

    # Merge ledger rows and conversions into one (ts, seq) stream. Conversions get a high seq so
    # they fold AFTER same-ts trades (the burned NO must already reflect that timestamp's buys).
    stream = [(r.ts, r.seq, "row", r) for r in led.rows]
    stream += [(ts, 1_000_000_000, "conv", eff) for ts, eff in (enriched.conversions or [])]
    for _ts, _seq, _kind, item in sorted(stream, key=lambda x: (x[0], x[1])):
        if _kind == "conv":
            apply_conversion(item, _ts)
            continue
        r = item
        slug, cond = r.event_slug, r.condition_id
        if r.kind in ("buy", "sell"):
            oidx = r.outcome_index if r.outcome_index is not None else 0
            base = (cond, oidx)
            ep = current_episode[base]
            if r.kind == "buy":
                prior = episodes.get((cond, oidx, ep))
                if prior is not None and prior.buy_shares > 0 and prior.sell_shares > 0:
                    # episode boundaries are decided by DIRECTIONAL flow only: a re-buy after the leg
                    # has gone flat (buy==sell) starts a new episode. Split/merge shares are kept out
                    # of this net so structural complete-set flow can't reshape directional episodes;
                    # they're still attributed to the leg's current episode below (split_cost/merge_cash).
                    net = prior.buy_shares - prior.sell_shares
                    if net <= 1e-9:
                        ep += 1
                        current_episode[base] = ep
            r.episode_index = ep                  # stamp so per-position activity drill-down can separate re-entries
            e = get(cond, oidx, r.outcome, r.title, slug, ep)
            e.last_ts = max(e.last_ts, r.ts)
            b = basket(slug, cond)
            if r.kind == "buy":
                e.buy_shares += r.size
                e.buy_cost += r.usdc
                e.first_buy_ts = r.ts if e.first_buy_ts is None else min(e.first_buy_ts, r.ts)
                b.net_cost += r.usdc
            else:
                e.sell_shares += r.size
                e.sell_proceeds += r.usdc
                b.net_proceeds += r.usdc
        elif r.kind == "redeem":
            for (c, oi, ep), e in episodes.items():
                if c == cond:
                    e.redeemed = True
                    e.last_ts = max(e.last_ts, r.ts)
        elif r.kind in ("split", "merge"):
            # condition-pair complete-set events (no outcome index). Allocate the row's ACTUAL
            # cash across both binary legs so the pair sums to it; legs are NOT selection bets
            # (is_directional stays buy-driven) but they carry split/merge cash for the total.
            b = basket(slug, cond)
            legs = (0, 1)
            if r.kind == "split":
                b.split_shares += r.size
                allocs = allocate_split_pair(r.usdc, leg_vwap(cond, 0), leg_vwap(cond, 1))
                for oidx, alloc in zip(legs, allocs):
                    e = get(cond, oidx, "", "", slug, current_episode[(cond, oidx)])
                    e.split_shares += r.size
                    e.split_cost += alloc
                    e.last_ts = max(e.last_ts, r.ts)
            else:
                b.merge_shares += r.size
                for oidx, cash in zip(legs, allocate_merge_pair(r.usdc)):
                    e = get(cond, oidx, "", "", slug, current_episode[(cond, oidx)])
                    e.merge_shares += r.size
                    e.merge_cash += cash
                    e.last_ts = max(e.last_ts, r.ts)
        elif r.kind == "reward":
            rewards[slug or cond or "wallet"] += r.usdc   # wallet-level rewards have no slug/cond
            if slug or cond:                               # don't mint an empty-slug basket
                basket(slug, cond).rewards += r.usdc

    for e in episodes.values():
        e.tags = list(enriched.tags.get(e.event_slug, []))
        e.category = parent_category(e.tags)
        e.subcat = specific_tag(e.tags)
        e.template = enriched.templates.get(e.event_slug, "")
        res = enriched.resolutions.get(e.condition_id) or {}
        if res.get("resolved") and e.outcome_index >= 0:
            e.resolved = True
            e.resolution_ts = res.get("resolved_ts")
            e.is_push = bool(res.get("push"))
            winner = res.get("winner")
            if res.get("push"):
                e.won = None
            elif winner is not None:
                e.won = 1 if winner == e.outcome_index else 0

    for b in baskets.values():
        b.final_pnl = b.net_proceeds - b.net_cost + b.redeem_payout

    # split/merge cash is now attributed per leg (split_cost/merge_cash on episodes); the old
    # wallet-level inventory_net_cash band-aid is superseded — see capital_risk reconciliation.
    return PositionBook(wallet=led.wallet, episodes=list(episodes.values()),
                        baskets=baskets, reward_income=dict(rewards))
