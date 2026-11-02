"""Phase 1B Deliverable #7 — executable-price features from the taker engine.

For Up and Down INDEPENDENTLY, at 5/10/20 USDC: executable_price, fee_inclusive_cost,
slippage_vs_best_ask, fillability, levels_consumed. These are observations (not trade decisions).
Fee-inclusive cost is null when the fee is unresolved; the ask walk (executable price/slippage/levels)
is fee-exclusive and ask-ordering independent.
"""
from sniper.algos.polymarket_crypto5m import executable_features as EX


def test_up_and_down_independent():
    up = {"asks": [[0.50, 1000]]}
    down = {"asks": [[0.30, 1000]]}
    f = EX.build_executable_features(up, down, fee_rate=None, fee_known=False)
    assert f["up_executable_price_5_usdc"] == 0.50
    assert f["down_executable_price_5_usdc"] == 0.30        # not derived from up


def test_fillability_and_levels():
    up = {"asks": [[0.50, 1000]]}
    f = EX.build_executable_features(up, {"asks": []}, fee_rate=None, fee_known=False)
    assert f["up_fillability_10_usdc"] == 1.0
    assert f["up_levels_consumed_10_usdc"] == 1
    assert f["down_fillability_10_usdc"] == 0.0             # empty book -> not fillable
    assert f["down_executable_price_10_usdc"] is None


def test_insufficient_depth_is_not_fillable():
    thin = {"asks": [[0.50, 20]]}                           # 20 shares * 0.50 = $10 depth
    f = EX.build_executable_features(thin, thin, fee_rate=None, fee_known=False)
    assert f["up_fillability_20_usdc"] == 0.0               # can't deploy $20 against $10 depth
    assert f["up_fillability_5_usdc"] == 1.0                # $5 -> 10 shares, fits


def test_fee_inclusive_null_when_fee_unknown_present_when_known():
    up = {"asks": [[0.50, 1000]]}
    unknown = EX.build_executable_features(up, up, fee_rate=None, fee_known=False)
    assert unknown["up_fee_inclusive_cost_10_usdc"] is None       # fee unresolved -> null
    assert unknown["up_executable_price_10_usdc"] == 0.50         # but exec price still observable

    known = EX.build_executable_features(up, up, fee_rate=0.001, fee_known=True)
    fic = known["up_fee_inclusive_cost_10_usdc"]
    assert fic is not None and fic >= 0.50                        # includes the fee


def test_ask_ordering_is_irrelevant():
    sorted_book = {"asks": [[0.50, 100], [0.60, 100]]}
    shuffled = {"asks": [[0.60, 100], [0.50, 100]]}
    a = EX.build_executable_features(sorted_book, sorted_book, fee_rate=None, fee_known=False)
    b = EX.build_executable_features(shuffled, shuffled, fee_rate=None, fee_known=False)
    assert a["up_executable_price_20_usdc"] == b["up_executable_price_20_usdc"]
    assert a["up_slippage_vs_best_ask_20_usdc"] == b["up_slippage_vs_best_ask_20_usdc"]
