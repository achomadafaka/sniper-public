from sniper.analysis.winning_wallet_thesis import assembler as A
from sniper.analysis.winning_wallet_thesis.types import (
    EdgeCluster, PositionBook, ShuffleBaseline,
)


def _cluster(label, pnl, loss=False):
    return EdgeCluster(cluster_id=label, label=label, scope={}, axis="tag",
                       n_positions=30, n_events=20, pnl=pnl, edge_pp=5.0,
                       is_loss_center=loss)


def test_profile_splits_edge_and_loss_and_is_deterministic():
    clusters = [_cluster("UFC", 50000), _cluster("Iran", -300000, loss=True)]
    book = PositionBook(wallet="0xw")
    base = ShuffleBaseline(20, 1.0, 5, 5.0, 0.5)
    p1 = A.assemble_profile("0xw", book, {}, base, clusters, reward_share=7.3)
    p2 = A.assemble_profile("0xw", book, {}, base, clusters, reward_share=7.3)
    assert [c.label for c in p1.edge_clusters] == ["UFC"]
    assert [c.label for c in p1.loss_centers] == ["Iran"]
    assert p1.reward_share_pct == 7.3
    assert p1.run_id == p2.run_id
    assert "UNVALIDATED" in p1.headline


def test_high_reward_share_emits_gap():
    p = A.assemble_profile("0xw", PositionBook(wallet="0xw"), {}, None,
                           [_cluster("X", 1000)], reward_share=40.0)
    assert any("reward_share high" in g for g in p.data_gaps)


def test_weak_baseline_emits_gap():
    base = ShuffleBaseline(20, 4.0, 4, 1.0, 0.4)   # obs/exp=1.0 (<2x)
    p = A.assemble_profile("0xw", PositionBook(wallet="0xw"), {}, base, [_cluster("X", 1000)])
    assert any("shuffle baseline" in g for g in p.data_gaps)
