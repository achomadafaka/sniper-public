from sniper.analysis.winning_wallet_thesis.data.fill_identity import UnavailableFillIdentityProvider
from sniper.analysis.winning_wallet_thesis.data.onchain_fill_identity import OnchainFillIdentityProvider
from sniper.analysis.winning_wallet_thesis.methods.attention import first_attention_discovery
from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook, PositionEpisode
from sniper.feeds.thesis_feed_types import FillEvent


def test_first_attention_blocks_without_identity_feed():
    ctx = ClusterContext(EdgeCluster("c", "x", {}, "tag", 0, 0, 0, 0), PositionBook("0xw"))
    finding = first_attention_discovery(ctx, fill_identity=UnavailableFillIdentityProvider())
    assert finding.state == "blocked_missing_feed"
    assert "missing_wallet_identified_fills" in finding.blockers


def test_first_attention_uses_fill_identity_to_find_followers():
    ep = PositionEpisode("0xlead", "cond1", "event1", 0, 0, buy_shares=10, buy_cost=4,
                         first_buy_ts=100, resolved=True, won=1)
    ctx = ClusterContext(
        EdgeCluster("c", "x", {}, "tag", 1, 1, 1, 1, [(ep.condition_id, ep.outcome_index, ep.episode_index)]),
        PositionBook("0xlead", [ep]),
    )
    fills = [
        FillEvent("tx0", "0", 90, "0xother", "0xmm", "a", "b", 1, 1, condition_id="cond1"),
        FillEvent("tx1", "1", 100, "0xlead", "0xmm", "a", "b", 1, 1, condition_id="cond1"),
        FillEvent("tx2", "2", 130, "0xfollower1", "0xmm", "a", "b", 1, 1, condition_id="cond1"),
        FillEvent("tx3", "3", 160, "0xfollower2", "0xmm", "a", "b", 1, 1, condition_id="cond1"),
    ]
    finding = first_attention_discovery(ctx, fill_identity=OnchainFillIdentityProvider(fills))
    assert finding.state == "supported_proxy"
    assert finding.n == 1
    assert finding.evidence["first_attention_events"] == 1
    assert finding.evidence["independent_followers"] == 2


def test_first_attention_blocks_when_fills_lack_condition_id():
    # fills present but untagged (condition_id=None) -> fills_for_market finds nothing -> blocked.
    # Proves condition tagging (Track 3 Task 1) is the actual unblocker, not just having fills.
    ep = PositionEpisode("0xlead", "cond1", "event1", 0, 0, buy_shares=10, buy_cost=4,
                         first_buy_ts=100, resolved=True, won=1)
    ctx = ClusterContext(
        EdgeCluster("c", "x", {}, "tag", 1, 1, 1, 1, [(ep.condition_id, ep.outcome_index, ep.episode_index)]),
        PositionBook("0xlead", [ep]),
    )
    fills = [FillEvent("tx1", "1", 100, "0xlead", "0xmm", "a", "b", 1, 1),    # condition_id=None
             FillEvent("tx2", "2", 130, "0xfollower1", "0xmm", "a", "b", 1, 1)]
    finding = first_attention_discovery(ctx, fill_identity=OnchainFillIdentityProvider(fills))
    assert finding.state == "blocked_missing_feed"
