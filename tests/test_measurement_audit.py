from sniper.research.leadlag import measurement_audit as M


def test_microprice_leans_toward_ask_when_bid_size_dominates():
    # bid 100 (sz 3), ask 101 (sz 1): more bid size -> price likely up -> microprice near ask.
    assert M.microprice(100.0, 101.0, 3.0, 1.0) == 100.75
    assert M.microprice(100.0, 101.0, 1.0, 1.0) == 100.5  # balanced -> mid
    assert M.microprice(100.0, 101.0, 0.0, 0.0) is None


def test_ofi_step_positive_on_bid_growth_and_ask_depletion():
    prev = (100.0, 5.0, 101.0, 5.0)
    cur = (100.0, 8.0, 101.0, 3.0)   # bid +3 (same px), ask -2 (same px)
    assert M.ofi_step(prev, cur) == 5.0
    # bid price up -> +new bid size; ask price up -> +old ask size (both buy pressure)
    assert M.ofi_step((100.0, 5.0, 101.0, 5.0), (100.5, 4.0, 101.5, 6.0)) == 4.0 + 5.0


def test_executable_return_marks_buy_ask_sell_bid_with_fee():
    # long: buy ask 100, sell bid 101 -> 1% gross - 2*fee
    r = M.executable_return("long", entry_bid=99.9, entry_ask=100.0, exit_bid=101.0, exit_ask=101.1, fee_rate=0.00045)
    assert round(r, 6) == round(0.01 - 0.0009, 6)
    # short: sell bid 100, buy ask 99 -> 1% gross - 2*fee
    s = M.executable_return("short", entry_bid=100.0, entry_ask=100.1, exit_bid=98.9, exit_ask=99.0, fee_rate=0.00045)
    assert round(s, 6) == round(0.01 - 0.0009, 6)


def test_executable_return_is_never_mid_marked():
    # A flat mid (bid/ask move symmetrically) still costs the spread crossing -> negative net.
    r = M.executable_return("long", entry_bid=99.95, entry_ask=100.05, exit_bid=99.95, exit_ask=100.05, fee_rate=0.0)
    assert r < 0  # crossing the spread both ways loses even at zero fee and zero mid move


def test_signed_microprice_return_side_aware():
    assert round(M.signed_microprice_return(100.0, 101.0, "long"), 6) == 0.01
    assert round(M.signed_microprice_return(100.0, 101.0, "short"), 6) == -0.01
