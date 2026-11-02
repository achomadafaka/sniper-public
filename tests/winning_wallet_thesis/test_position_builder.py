from sniper.analysis.winning_wallet_thesis import ledger as L
from sniper.analysis.winning_wallet_thesis import position_builder as PB
from sniper.analysis.winning_wallet_thesis.types import WalletEventLedger, EnrichedLedger


def _trade(ts, side, size, price, cond="0xc", slug="ufc-x"):
    return {"type": "TRADE", "conditionId": cond, "outcomeIndex": 0, "outcome": "Y",
            "title": "t", "eventSlug": slug, "timestamp": ts, "side": side,
            "size": size, "usdcSize": size * price}


def _enriched(acts):
    led = WalletEventLedger(wallet="0xw", rows=L.normalize(acts), raw_activity=acts)
    return EnrichedLedger(ledger=led,
                          resolutions={"0xc": {"resolved": True, "winner": 0, "push": False}},
                          tags={"ufc-x": ["Sports", "UFC"]},
                          templates={"ufc-x": "ufc"})


def test_flat_then_rebuy_splits_into_two_episodes():
    book = PB.build_position_book(_enriched([
        _trade(1, "BUY", 10, 0.2), _trade(2, "SELL", 10, 0.5), _trade(3, "BUY", 5, 0.7),
    ]))
    dir_eps = [e for e in book.episodes if e.is_directional]
    assert len(dir_eps) == 2
    assert sorted(e.buy_shares for e in dir_eps) == [5.0, 10.0]
    assert all(e.category == "sports" and e.template == "ufc" for e in dir_eps)


def test_redeem_marks_all_episodes_and_resolution_applies():
    book = PB.build_position_book(_enriched([
        _trade(1, "BUY", 10, 0.2), {"type": "REDEEM", "conditionId": "0xc", "timestamp": 2},
    ]))
    e = [e for e in book.episodes if e.is_directional][0]
    assert e.redeemed and e.resolved and e.won == 1


def test_split_appears_in_event_basket_not_directional():
    book = PB.build_position_book(_enriched([
        {"type": "SPLIT", "conditionId": "0xc", "eventSlug": "ufc-x", "timestamp": 1,
         "size": 100, "usdcSize": 100},
    ]))
    assert all(not e.is_directional for e in book.episodes) or not book.episodes
    assert book.baskets["ufc-x"].split_shares == 100


def test_conditionless_reward_kept_under_wallet_key():
    book = PB.build_position_book(EnrichedLedger(
        ledger=WalletEventLedger(wallet="0xw",
            rows=L.normalize([{"type": "REWARD", "usdcSize": 12}]), raw_activity=[]),
        resolutions={}, tags={}, templates={}))
    assert book.reward_income.get("wallet") == 12
    assert "" not in book.reward_income            # no empty-string key
    assert "" not in book.baskets                  # no empty-slug basket minted


def test_split_then_sell_one_leg_captured_in_total_not_selection():
    # SPLIT mints YES+NO; SELL the NO leg -> net-long YES via the split. The YES leg carries
    # split_shares (counted in the wallet TOTAL) but is NOT a selection bet (no buy) -> excluded
    # from the selection edge population.
    acts = [
        {"type": "SPLIT", "conditionId": "c", "eventSlug": "e", "timestamp": 1, "size": 100, "usdcSize": 100},
        {"type": "TRADE", "conditionId": "c", "outcomeIndex": 1, "outcome": "No", "title": "t",
         "eventSlug": "e", "timestamp": 2, "side": "SELL", "size": 100, "usdcSize": 30},
    ]
    book = PB.build_position_book(EnrichedLedger(
        ledger=WalletEventLedger("0xw", rows=L.normalize(acts), raw_activity=acts),
        resolutions={"c": {"resolved": True, "winner": 0, "push": False}},
        tags={"e": ["Sports"]}, templates={"e": "x"}))
    yes = next(e for e in book.episodes if e.outcome_index == 0)
    assert yes.split_shares == 100
    assert not yes.is_directional                       # split-only leg is NOT a selection bet
    assert yes not in book.resolved_directional()       # excluded from selection edge
    assert yes in book.resolved_with_pnl()              # but counted in the wallet total
    # cash truth: split -100, sell NO +30, redeem YES +100 = +30
    assert round(sum(e.realized_pnl or 0 for e in book.resolved_with_pnl()), 2) == 30.0


def test_buy_both_then_merge_realizes_arbitrage_in_total():
    # buy YES@0.55 ($55) + NO@0.40 ($40) = $95/pair, MERGE 100 -> $100 -> +$5, captured in total
    acts = [
        {"type": "TRADE", "conditionId": "c", "outcomeIndex": 0, "outcome": "Y", "title": "t",
         "eventSlug": "e", "timestamp": 1, "side": "BUY", "size": 100, "usdcSize": 55},
        {"type": "TRADE", "conditionId": "c", "outcomeIndex": 1, "outcome": "N", "title": "t",
         "eventSlug": "e", "timestamp": 2, "side": "BUY", "size": 100, "usdcSize": 40},
        {"type": "MERGE", "conditionId": "c", "eventSlug": "e", "timestamp": 3, "size": 100, "usdcSize": 100},
    ]
    book = PB.build_position_book(EnrichedLedger(
        ledger=WalletEventLedger("0xw", rows=L.normalize(acts), raw_activity=acts),
        resolutions={"c": {"resolved": True, "winner": 0, "push": False}},
        tags={"e": ["Sports"]}, templates={"e": "x"}))
    total = sum(e.realized_pnl or 0 for e in book.resolved_with_pnl())
    assert round(total, 2) == 5.0


def test_loser_episode_won_zero_and_resolved_directional_filter():
    book = PB.build_position_book(EnrichedLedger(
        ledger=WalletEventLedger(wallet="0xw",
            rows=L.normalize([_trade(1, "BUY", 10, 0.2)]), raw_activity=[]),
        resolutions={"0xc": {"resolved": True, "winner": 1, "push": False}},  # she held idx0, idx1 won
        tags={"ufc-x": ["Sports"]}, templates={"ufc-x": "ufc"}))
    rd = book.resolved_directional()
    assert len(rd) == 1 and rd[0].won == 0
