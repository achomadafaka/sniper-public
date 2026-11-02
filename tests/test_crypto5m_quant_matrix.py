"""Phase 2A — per-decision price-comparison matrix: assemble q_* vs executable token prices, derive the
only economically actionable edge (uncertainty-adjusted prob - fee-inclusive executable cost - margin)."""
from sniper.algos.polymarket_crypto5m import quant_matrix as QM

UP_BOOK = {"best_bid": 0.58, "best_ask": 0.60, "bids": [[0.58, 500]], "asks": [[0.60, 500], [0.62, 500]]}
DOWN_BOOK = {"best_bid": 0.38, "best_ask": 0.40, "bids": [[0.38, 500]], "asks": [[0.40, 500]]}


def test_matrix_assembles_core_fields():
    m = QM.build_comparison_matrix(
        q_components={"q_market": 0.60, "q_structural": 0.66, "q_residual": 0.68},
        q_ensemble=0.67, q_trade=0.64, up_book=UP_BOOK, down_book=DOWN_BOOK,
        fee_rate=0.0, fee_known=True, latent_btc_price=63850.0,
        source_prices={"binance": 63850.0, "coinbase": 63848.0}, gross_usdc=10.0,
        min_order_size=5, safety_margin=0.01)
    assert m["q_ensemble"] == 0.67
    assert abs(m["up_best_ask"] - 0.60) < 1e-9 and abs(m["down_best_ask"] - 0.40) < 1e-9
    assert m["market_residual"] == round(0.67 - 0.60, 6)
    assert m["cross_source_basis_abs"] == 2.0
    # net edge on Up = q_trade_up - fee-inclusive executable cost per share (fee 0 -> just the price)
    assert m["up_net_edge_vs_executable"] == round(0.64 - m["up_fee_inclusive_cost_per_share"] - 0.01, 6)


def test_matrix_actionable_side_is_the_one_with_positive_net_edge():
    # q_trade Up = 0.64 vs Up ask ~0.60 -> Up has positive edge; Down side (0.36 vs 0.40 ask) negative
    m = QM.build_comparison_matrix(
        q_components={"q_market": 0.60}, q_ensemble=0.64, q_trade=0.64,
        up_book=UP_BOOK, down_book=DOWN_BOOK, fee_rate=0.0, fee_known=True,
        latent_btc_price=None, source_prices={}, gross_usdc=10.0, min_order_size=5, safety_margin=0.0)
    assert m["actionable_side"] == "Up"
    assert m["up_net_edge_vs_executable"] > 0 >= m["down_net_edge_vs_executable"]


def test_matrix_fee_unknown_blocks_net_edge():
    m = QM.build_comparison_matrix(
        q_components={"q_market": 0.60}, q_ensemble=0.64, q_trade=0.64,
        up_book=UP_BOOK, down_book=DOWN_BOOK, fee_rate=None, fee_known=False,
        latent_btc_price=None, source_prices={}, gross_usdc=10.0, min_order_size=5, safety_margin=0.0)
    assert m["up_net_edge_vs_executable"] is None and m["actionable_side"] is None
    assert m["fee_known"] is False
