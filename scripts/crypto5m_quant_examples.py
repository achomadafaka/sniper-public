#!/usr/bin/env python3
"""Phase 2A worked examples — exercises the quant spine on an ILLUSTRATIVE synthetic mid-window state.

NOT validated results: there is no captured dataset yet and the boundary rule is unlocked, so the
structural inputs here are hand-set to show the mechanism end to end (fusion -> probability decomposition
-> price-comparison matrix -> dynamic timing -> uncertainty-adjusted paper sizing). No DB, network, or
orders.
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.algos.polymarket_crypto5m import quant_fusion as QF  # noqa: E402
from sniper.algos.polymarket_crypto5m import quant_matrix as QM
from sniper.algos.polymarket_crypto5m import quant_probability as QP
from sniper.algos.polymarket_crypto5m import quant_sizing as QS
from sniper.algos.polymarket_crypto5m import quant_timing as QT


def _phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def main():
    # ---- 1. source fusion (latent BTC state) ----
    sources = [
        {"source": "binance", "price": 63850.0, "source_age_ms": 480, "stale": False, "gap": False},
        {"source": "coinbase", "price": 63846.0, "source_age_ms": 200, "stale": False, "gap": False},
        {"source": "kraken", "price": 63849.0, "source_age_ms": 240, "stale": False, "gap": False},
        {"source": "deribit", "price": 63852.0, "source_age_ms": 260, "stale": False, "gap": False},
    ]
    fused = QF.fuse_latent_state(sources)
    print("=== 1. FUSION (latent state) ===")
    print(f"  latent={fused['latent_btc_price']}  uncertainty={fused['latent_price_uncertainty']}  "
          f"disagreement={fused['source_disagreement_bps']}bps  quality={fused['state_quality']}")

    # ---- 2. probability decomposition ----
    cur, p2b, vol, secs = fused["latent_btc_price"], 63800.0, 0.00015, 60
    z = math.log(cur / p2b) / (vol * math.sqrt(secs))
    q1 = _phi(z)
    q2 = QP.monte_carlo_up_probability(current_price=cur, price_to_beat=p2b, vol_per_s=vol,
                                       seconds_remaining=secs, n_paths=60000, seed=11)
    q2j = QP.monte_carlo_up_probability(current_price=cur, price_to_beat=p2b, vol_per_s=vol,
                                        seconds_remaining=secs, n_paths=60000, seed=11,
                                        jump_intensity_per_s=0.03, jump_sigma=0.004)
    q_market = 0.70                                   # Polymarket Up midpoint (prior)
    # illustrative residual fit: feature = (latent-p2b)/p2b normalized momentum; here just show predict
    resid_fit = {"intercept": 0.0, "weights": {"norm_dist": 4.0}, "feature_names": ["norm_dist"]}
    q3 = QP.residual_predict(resid_fit, {"norm_dist": (cur - p2b) / p2b}, q_market=q_market)
    comps = {"q_market": q_market, "q_structural_diffusion": round(q1, 4),
             "q_structural_mc": round(q2["q_structural_up"], 4),
             "q_structural_mc_jumps": round(q2j["q_structural_up"], 4),
             "q_residual": round(q3, 4)}
    q_ens = round(sum([q_market, q1, q2["q_structural_up"], q3]) / 4, 4)
    ens_unc = round(max(abs(q1 - q_market), abs(q3 - q_market), q2["simulation_standard_error"]), 4)
    q_trade = round(q_ens - ens_unc, 4)               # uncertainty-adjusted lower bound
    print("\n=== 2. PROBABILITY DECOMPOSITION ===")
    for k, v in comps.items():
        print(f"  {k:24s} {v}")
    print(f"  {'q_ensemble':24s} {q_ens}   (uncertainty {ens_unc})")
    print(f"  {'q_trade (lower bound)':24s} {q_trade}")

    # ---- 3. price-comparison matrix ----
    up_book = {"best_bid": 0.64, "best_ask": 0.66, "bids": [[0.64, 400]], "asks": [[0.66, 600], [0.68, 600]]}
    down_book = {"best_bid": 0.34, "best_ask": 0.36, "bids": [[0.34, 400]], "asks": [[0.36, 600]]}
    for label, fee in (("fee F0 gross", 0.0), ("fee F1 documented 0.07", 0.07)):
        m = QM.build_comparison_matrix(
            q_components=comps, q_ensemble=q_ens, q_trade=q_trade, up_book=up_book, down_book=down_book,
            fee_rate=fee, fee_known=True, latent_btc_price=cur,
            source_prices={s["source"]: s["price"] for s in sources}, gross_usdc=10.0, min_order_size=5,
            safety_margin=0.01)
        print(f"\n=== 3. PRICE-COMPARISON MATRIX ({label}) ===")
        print(f"  Up:   exec={m['up_executable_price']}  fee_inc_cost={m['up_fee_inclusive_cost_per_share']}"
              f"  net_edge={m['up_net_edge_vs_executable']}  microprice={m['up_microprice']}  OFI={m['up_order_flow_imbalance']}")
        print(f"  Down: exec={m['down_executable_price']}  fee_inc_cost={m['down_fee_inclusive_cost_per_share']}"
              f"  net_edge={m['down_net_edge_vs_executable']}")
        print(f"  forecast_edge_vs_midpoint={m['forecast_edge_vs_midpoint']}  market_residual={m['market_residual']}"
              f"  cross_source_basis={m['cross_source_basis_abs']}  -> actionable_side={m['actionable_side']}")
        last = m

    # ---- 4. dynamic timing (T1 edge persistence) ----
    edge_hist = [0.005, 0.018, 0.026, 0.031, 0.033]   # edge climbing as the window matures
    t1 = QT.edge_persistence(edge_hist, threshold=0.02, persist_n=3, gates_ok=True)
    print("\n=== 4. DYNAMIC TIMING (T1, persist_n=3, threshold=0.02) ===")
    print(f"  edge history {edge_hist} -> action={t1['action']} ({t1['reason']})")

    # ---- 5. uncertainty-adjusted sizing (S2 fractional Kelly, capped) ----
    cost = last["up_fee_inclusive_cost_per_share"]
    s2 = QS.fractional_kelly_stake(q_conservative=q_trade, cost_per_share=cost, bankroll=400.0,
                                   fraction=0.25, max_fraction_per_market=0.05,
                                   max_open_exposure=120.0, current_open_exposure=40.0)
    print("\n=== 5. UNCERTAINTY-ADJUSTED SIZING (S2, 0.25 Kelly, caps) ===")
    print(f"  q_conservative={q_trade}  cost/share={cost}  full_kelly={s2['kelly_full']}  "
          f"stake=${s2['stake']}  capped_by={s2['capped_by']}")


if __name__ == "__main__":
    main()
