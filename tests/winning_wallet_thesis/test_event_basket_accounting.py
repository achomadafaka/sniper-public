from sniper.analysis.winning_wallet_thesis.event_basket import build_event_basket_accounting
from sniper.analysis.winning_wallet_thesis.types import LedgerRow, WalletEventLedger


def test_split_merge_basket_counts_event_once_not_per_leg():
    ledger = WalletEventLedger("0xw", rows=[
        LedgerRow("buy", "0xw", "c_yes", 100, outcome_index=0, price=.45, size=100, usdc=45, event_slug="e"),
        LedgerRow("buy", "0xw", "c_no", 101, outcome_index=1, price=.45, size=100, usdc=45, event_slug="e"),
        LedgerRow("redeem", "0xw", "c_yes", 200, outcome_index=0, size=100, usdc=100, event_slug="e"),
    ])
    baskets = build_event_basket_accounting(ledger)
    assert len(baskets) == 1
    assert baskets["e"].condition_ids == {"c_yes", "c_no"}


def test_negrisk_basket_final_pnl_identity():
    ledger = WalletEventLedger("0xw", rows=[
        LedgerRow("buy", "0xw", "c1", 1, usdc=45, size=100, event_slug="ev"),
        LedgerRow("buy", "0xw", "c2", 2, usdc=45, size=100, event_slug="ev"),
        LedgerRow("redeem", "0xw", "c1", 3, usdc=100, size=100, event_slug="ev"),
        LedgerRow("reward", "0xw", "c1", 4, usdc=2, event_slug="ev"),
    ])
    baskets = build_event_basket_accounting(ledger)
    b = baskets["ev"]
    assert len(baskets) == 1
    assert b.final_pnl == b.net_proceeds + b.redeem_payout + b.rewards - b.net_cost
    assert b.final_pnl == 100 + 2 - 90      # +12
