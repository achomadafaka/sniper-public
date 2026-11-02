from sniper.analysis.winning_wallet_thesis import clusters as C
from sniper.analysis.winning_wallet_thesis.types import EdgeSlice, PositionEpisode


def _slice(axis, key, n, n_events, pnl, wins, entry):
    s = EdgeSlice(slice_key=key, axis=axis, n_positions=n, n_events=n_events,
                  wins=wins, entry_sum=entry * n, pnl=pnl, cost=abs(pnl) + 100)
    return s


def test_green_slice_above_guards_becomes_cluster():
    tables = {"tag": {"UFC": _slice("tag", "UFC", 30, 20, 50000, 20, 0.5)}}
    clusters = C.build_clusters(tables, min_events=5, min_positions=15, max_depth=2)
    assert len(clusters) == 1
    assert clusters[0].label == "UFC" and clusters[0].pnl == 50000
    assert not clusters[0].is_loss_center


def test_slice_below_guards_is_dropped():
    tables = {"tag": {"Tiny": _slice("tag", "Tiny", 8, 3, 9000, 6, 0.5)}}   # n<15, events<5
    assert C.build_clusters(tables, min_events=5, min_positions=15, max_depth=2) == []


def test_loss_center_is_emitted():
    tables = {"tag": {"Iran": _slice("tag", "Iran", 40, 20, -300000, 20, 0.7)}}
    clusters = C.build_clusters(tables, min_events=5, min_positions=15, max_depth=2)
    assert clusters and clusters[0].is_loss_center


def test_cluster_id_deterministic_and_non_informative_dropped():
    tables = {"tag": {"UFC": _slice("tag", "UFC", 30, 20, 50000, 20, 0.5),
                      "untagged": _slice("tag", "untagged", 99, 50, 80000, 60, 0.5)}}
    out = C.build_clusters(tables, min_events=5, min_positions=15, max_depth=2)
    assert [c.label for c in out] == ["UFC"]      # untagged dropped
    again = C.build_clusters(tables, min_events=5, min_positions=15, max_depth=2)
    assert out[0].cluster_id == again[0].cluster_id


def test_depth2_axis_kept_depth3_axis_excluded():
    # tag_price (depth 2) kept; a depth-3 axis (tag_price_hour) structurally rejected
    tables = {
        "tag_price": {"UFC | underdog": _slice("tag_price", "UFC | underdog", 30, 20, 9000, 18, 0.4)},
        "tag_price_hour": {"UFC | underdog | 22h":
                           _slice("tag_price_hour", "UFC | underdog | 22h", 30, 20, 9000, 18, 0.4)},
    }
    out = C.build_clusters(tables, min_events=5, min_positions=15, max_depth=2)
    assert [c.axis for c in out] == ["tag_price"]        # depth-3 axis dropped


def test_overlap_reported_between_clusters():
    shared = [PositionEpisode(wallet="w", condition_id=f"c{i}", event_slug=f"e{i}",
                              outcome_index=0, episode_index=0) for i in range(20)]
    a = _slice("tag", "UFC", 20, 20, 50000, 14, 0.5); a.members = shared
    b = _slice("subcat", "UFC", 20, 20, 40000, 14, 0.5); b.members = shared
    out = C.build_clusters({"tag": {"UFC": a}, "subcat": {"UFC": b}},
                           min_events=5, min_positions=15, max_depth=2)
    # the lower-pnl cluster records overlap with the higher-pnl one
    assert any(c.overlap and c.overlap[0]["share"] == 1.0 for c in out)
