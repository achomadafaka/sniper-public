from sniper.analysis.winning_wallet_thesis.position_ledger import (
    LegEvent, account_leg, allocate_split_pair, allocate_merge_pair,
)


def test_allocate_split_pair_uses_revealed_price_and_sums_to_cash():
    a0, a1 = allocate_split_pair(cash=100.0, vwap0=0.80, vwap1=None)
    assert round(a0, 2) == 80.0 and round(a1, 2) == 20.0       # leg0 revealed 0.80 -> leg1 0.20
    b0, b1 = allocate_split_pair(cash=100.0, vwap0=None, vwap1=0.30)
    assert round(b0, 2) == 70.0 and round(b1, 2) == 30.0       # leg1 revealed 0.30 -> leg0 0.70
    c0, c1 = allocate_split_pair(cash=100.0, vwap0=None, vwap1=None)
    assert c0 == 50.0 and c1 == 50.0                            # no revealed price -> 50/50
    assert round(sum(allocate_split_pair(215000.0, None, 0.30)), 2) == 215000.0


def test_allocate_merge_pair_is_5050():
    assert allocate_merge_pair(100.0) == (50.0, 50.0)


def test_plain_held_winner():
    # buy 100 @0.40 ($40), hold to a WIN -> redeem 100*$1 - $40 = +$60
    r = account_leg([LegEvent("buy", 100, 40.0)], resolved=True, won=1, is_push=False)
    assert round(r.realized_pnl, 2) == 60.0
    assert r.split_shares == 0.0


def test_split_held_to_win_uses_allocated_cost():
    # split mints 100 shares of this leg; position_builder allocated $50 of the pair cost here.
    # Hold to WIN -> 100 payout - 50 cost = +50.
    r = account_leg([LegEvent("split", 100, 50.0)], resolved=True, won=1, is_push=False)
    assert r.split_shares == 100.0
    assert round(r.split_cost, 2) == 50.0
    assert round(r.realized_pnl, 2) == 50.0


def test_split_plus_buy_cost_accumulates():
    # buy 100 @0.80 ($80); split 100 allocated $80 to this leg (builder priced at the 0.80 leg).
    # Lose -> 0 payout - (80 buy + 80 split) = -160.
    r = account_leg([LegEvent("buy", 100, 80.0), LegEvent("split", 100, 80.0)],
                    resolved=True, won=0, is_push=False)
    assert round(r.split_cost, 2) == 80.0
    assert round(r.realized_pnl, 2) == -160.0


def test_sell_realizes_against_avg_cost():
    # buy 100 @0.40, sell 50 @0.60 -> realized 50*(0.60-0.40)=+10 ; hold 50 to WIN -> +50-20=+30
    r = account_leg([LegEvent("buy", 100, 40.0), LegEvent("sell", 50, 30.0)],
                    resolved=True, won=1, is_push=False)
    assert round(r.realized_pnl, 2) == 40.0               # sell +10, held 50: 50 - 20 = +30


def test_merge_realizes_pair_pnl():
    # buy 100 @0.40 ($40); MERGE 100 with $50 cash allocated to this leg -> +50-40 = +10.
    # (merge is NOT cost-neutral in general: cheap basis + face-value cash = real gain.)
    r = account_leg([LegEvent("buy", 100, 40.0), LegEvent("merge", 100, 50.0)],
                    resolved=True, won=1, is_push=False)
    assert r.merge_shares == 100.0
    assert round(r.merge_cash, 2) == 50.0
    assert round(r.realized_pnl, 2) == 10.0
    assert r.held_shares == 0.0


def test_split_then_merge_roundtrip_is_neutral():
    # split alloc $50 in, merge alloc $50 out on the same shares -> cost-neutral round trip.
    r = account_leg([LegEvent("split", 100, 50.0), LegEvent("merge", 100, 50.0)],
                    resolved=True, won=1, is_push=False)
    assert round(r.realized_pnl, 2) == 0.0
    assert r.held_shares == 0.0
