from sniper.analysis.winning_wallet_thesis.data.onchain_fill_identity import OnchainFillIdentityProvider
from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.methods.microstructure import (
    adverse_selection_aware_making,
    market_making_spread_capture,
)
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook, PositionEpisode
from sniper.feeds.thesis_feed_types import FillEvent


def _ctx(conds=("c1",)):
    """Cluster context whose episodes (and member keys) cover `conds`, so microstructure
    detectors have conditions to scope their fills to."""
    eps = [PositionEpisode("0xw", c, f"ev-{c}", 0, 0) for c in conds]
    keys = [(c, 0, 0) for c in conds]
    cluster = EdgeCluster("c", "x", {}, "tag", 0, 0, 0, 0, member_position_keys=keys)
    return ClusterContext(cluster, PositionBook("0xw", episodes=eps))


def test_market_making_blocks_without_maker_taker_feed():
    finding = market_making_spread_capture(_ctx())
    assert finding.state == "blocked_missing_feed"
    assert "missing_maker_taker_or_order_detail" in finding.blockers


def test_adverse_selection_blocks_without_passive_fill_baseline():
    finding = adverse_selection_aware_making(_ctx())
    assert finding.state == "blocked_missing_feed"
    assert "missing_passive_fill_baseline" in finding.blockers


def test_market_making_uses_maker_fraction_when_fills_present():
    fills = [FillEvent("t", f"l{i}", i + 1, "0xw", "0xo", "A", "0", 1, 1, condition_id="c1")
             for i in range(8)]                                       # all maker == wallet, on c1
    f = market_making_spread_capture(_ctx(), fill_identity=OnchainFillIdentityProvider(fills))
    assert f.state != "blocked_missing_feed"
    assert f.evidence["maker_fraction"] == 1.0 and f.evidence["n_fills"] == 8


def test_market_making_blocks_when_provider_has_no_wallet_fills():
    other = [FillEvent("t", "l", 1, "0xother", "0xx", "A", "0", 1, 1, condition_id="c1")]
    f = market_making_spread_capture(_ctx(), fill_identity=OnchainFillIdentityProvider(other))
    assert f.state == "blocked_missing_feed"                          # no fills involving the wallet


def test_market_making_blocks_when_fills_are_on_another_cluster():
    # Codex repro: cluster c1 has no fills of its own; wallet fills exist only on cOTHER.
    fills = [FillEvent("t", f"l{i}", i + 1, "0xw", "0xo", "A", "0", 1, 1, condition_id="cOTHER")
             for i in range(8)]
    f = market_making_spread_capture(_ctx(conds=("c1",)), fill_identity=OnchainFillIdentityProvider(fills))
    assert f.state == "blocked_missing_feed"                          # not scoped to this cluster


def test_adverse_selection_supported_when_maker_buyer_and_up_drift():
    # wallet BUYS as maker at 0.50 (pays USDC, maker_asset='0'); price drifts UP -> favorable to a
    # buyer -> supported_proxy.
    fills = [FillEvent("t", "l0", 1, "0xw", "0xo", "0", "A", 1, 1, condition_id="c1", price=0.50),
             FillEvent("t", "l1", 5, "0xa", "0xb", "A", "0", 1, 1, condition_id="c1", price=0.55),
             FillEvent("t", "l2", 9, "0xc", "0xd", "A", "0", 1, 1, condition_id="c1", price=0.58)]
    f = adverse_selection_aware_making(_ctx(), fill_identity=OnchainFillIdentityProvider(fills))
    assert f.state == "supported_proxy"
    assert f.controls.get("proxy")
    assert round(f.evidence["avg_favorable_drift"], 2) == 0.08


def test_adverse_selection_contradicted_when_maker_seller_and_up_drift():
    # wallet SELLS as maker at 0.50 (gives token A); SAME up-drift now means it was picked off
    # -> adverse -> contradicted. This is the case the old code mislabeled as supported.
    fills = [FillEvent("t", "l0", 1, "0xw", "0xo", "A", "0", 1, 1, condition_id="c1", price=0.50),
             FillEvent("t", "l1", 5, "0xa", "0xb", "A", "0", 1, 1, condition_id="c1", price=0.55),
             FillEvent("t", "l2", 9, "0xc", "0xd", "A", "0", 1, 1, condition_id="c1", price=0.58)]
    f = adverse_selection_aware_making(_ctx(), fill_identity=OnchainFillIdentityProvider(fills))
    assert f.state == "contradicted"
    assert round(f.evidence["avg_favorable_drift"], 2) == -0.08


def test_adverse_selection_blocks_when_fills_are_on_another_cluster():
    fills = [FillEvent("t", "l0", 1, "0xw", "0xo", "0", "A", 1, 1, condition_id="cOTHER", price=0.50),
             FillEvent("t", "l1", 5, "0xa", "0xb", "A", "0", 1, 1, condition_id="cOTHER", price=0.58)]
    f = adverse_selection_aware_making(_ctx(conds=("c1",)), fill_identity=OnchainFillIdentityProvider(fills))
    assert f.state == "blocked_missing_feed"                          # cluster c1 has no fills of its own
