from sniper.analysis.winning_wallet_thesis.methods.runner import run_detectors
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook, PositionEpisode


def test_runner_returns_multiple_findings_per_cluster():
    ep = PositionEpisode(
        "0xw", "c1", "e1", 0, 0, buy_shares=100, buy_cost=40,
        resolved=True, won=1, tags=["UFC"], template="ufc-fight-night",
    )
    cluster = EdgeCluster("c", "UFC", {"tag": "UFC"}, "tag", 1, 1, 60, 60,
                          [(ep.condition_id, ep.outcome_index, ep.episode_index)])
    findings = run_detectors(PositionBook("0xw", [ep]), [cluster])
    methods = {f.method for f in findings["c"]}
    assert "specialist_domain_edge" in methods
    assert "reward_attribution" in methods
    assert "capital_velocity" in methods
    assert "stale_liquidity_sniping" in methods
    assert "first_attention_discovery" in methods
    assert "market_making_spread_capture" in methods
    assert len(findings["c"]) == 12


def test_runner_surfaces_feed_blocked_methods_by_default():
    ep = PositionEpisode(
        "0xw", "c1", "e1", 0, 0, buy_shares=100, buy_cost=40,
        first_buy_ts=100, resolved=True, won=1,
    )
    cluster = EdgeCluster("c", "test", {}, "tag", 1, 1, 60, 60,
                          [(ep.condition_id, ep.outcome_index, ep.episode_index)])
    findings = run_detectors(PositionBook("0xw", [ep]), [cluster])["c"]
    by_method = {f.method: f for f in findings}
    assert by_method["stale_liquidity_sniping"].state == "blocked_missing_feed"
    assert by_method["first_attention_discovery"].state == "blocked_missing_feed"
    assert by_method["market_making_spread_capture"].state == "blocked_missing_feed"


def test_assembler_attaches_method_findings():
    from sniper.analysis.winning_wallet_thesis import assembler as A
    ep = PositionEpisode("0xw", "c1", "e1", 0, 0, buy_shares=100, buy_cost=40,
                         resolved=True, won=1, tags=["UFC"])
    cluster = EdgeCluster("c", "UFC", {"tag": "UFC"}, "tag", 1, 1, 60, 60,
                          [(ep.condition_id, ep.outcome_index, ep.episode_index)])
    profile = A.assemble_profile("0xw", PositionBook("0xw", [ep]), {}, None, [cluster])
    assert "c" in profile.method_findings
    assert len(profile.method_findings["c"]) == 12


def test_runner_feeds_provider_to_microstructure_detectors():
    from sniper.analysis.winning_wallet_thesis.data.onchain_fill_identity import OnchainFillIdentityProvider
    from sniper.feeds.thesis_feed_types import FillEvent
    ep = PositionEpisode("0xw", "c1", "e1", 0, 0, buy_shares=10, buy_cost=4,
                         first_buy_ts=1, resolved=True, won=1)
    cluster = EdgeCluster("c", "UFC", {"tag": "UFC"}, "tag", 1, 1, 1, 1, [("c1", 0, 0)])
    fills = [FillEvent("t", f"l{i}", i + 1, "0xw", "0xo", "A", "0", 1, 1, condition_id="c1")
             for i in range(5)]
    out = run_detectors(PositionBook("0xw", [ep]), [cluster],
                        fill_identity=OnchainFillIdentityProvider(fills))
    methods = [f.method for f in out["c"]]
    assert methods.count("market_making_spread_capture") == 1   # NOT double-run
    assert len(out["c"]) == 12
    mm = next(f for f in out["c"] if f.method == "market_making_spread_capture")
    assert mm.state != "blocked_missing_feed"                   # fed the provider
