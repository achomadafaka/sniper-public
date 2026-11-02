"""Tests for M4 cross-event correlation clustering."""

from sniper.analysis.wallet_protocol.independence import (
    cluster_by_event_or_news_window,
)
from sniper.analysis.wallet_protocol.held_analyst import run_held_branch
from sniper.analysis.wallet_protocol.types import WalletData


def _pos(i, *, slug=None, cid=None, ts=1_700_000_000):
    return {
        "eventSlug": slug,
        "conditionId": cid or f"cid_{i}",
        "timestamp": ts,
    }


def test_same_eventslug_clusters_together():
    positions = [_pos(0, slug="event-a"), _pos(1, slug="event-a")]
    result = cluster_by_event_or_news_window(positions, {}, {})
    assert result.n_clusters == 1
    assert result.cluster_to_indices["event:event-a"] == [0, 1]


def test_same_nav_within_24h_clusters_together():
    positions = [
        _pos(0, cid="cid_a", ts=1_700_000_000),
        _pos(1, cid="cid_b", ts=1_700_000_000 + 3600),
    ]
    result = cluster_by_event_or_news_window(
        positions,
        {"cid_a": ["iran"], "cid_b": ["nuclear"]},
        {"iran": "geopolitics", "nuclear": "geopolitics"},
    )
    assert result.n_clusters == 1
    assert list(result.cluster_to_indices.values()) == [[0, 1]]


def test_same_nav_outside_24h_does_not_cluster():
    positions = [
        _pos(0, cid="cid_a", ts=1_700_000_000),
        _pos(1, cid="cid_b", ts=1_700_000_000 + 90_000),
    ]
    result = cluster_by_event_or_news_window(
        positions,
        {"cid_a": ["iran"], "cid_b": ["nuclear"]},
        {"iran": "geopolitics", "nuclear": "geopolitics"},
    )
    assert result.n_clusters == 2


def test_different_nav_inside_24h_does_not_cluster():
    positions = [
        _pos(0, cid="cid_a", ts=1_700_000_000),
        _pos(1, cid="cid_b", ts=1_700_000_000 + 3600),
    ]
    result = cluster_by_event_or_news_window(
        positions,
        {"cid_a": ["iran"], "cid_b": ["nba"]},
        {"iran": "geopolitics", "nba": "sports"},
    )
    assert result.n_clusters == 2


def test_iran_crisis_chain_does_not_transitively_merge_all_geopolitics():
    base = 1_700_000_000
    positions = [
        _pos(0, cid="iran_nuclear", ts=base),
        _pos(1, cid="iran_sanctions", ts=base + 23 * 3600),
        _pos(2, cid="russia_sanctions", ts=base + 46 * 3600),
        _pos(3, cid="venezuela_election", ts=base + 69 * 3600),
        _pos(4, cid="nba", ts=base + 3600),
    ]
    leafs = {
        "iran_nuclear": ["iran"],
        "iran_sanctions": ["iran"],
        "russia_sanctions": ["russia"],
        "venezuela_election": ["venezuela"],
        "nba": ["nba"],
    }
    nav = {
        "iran": "geopolitics",
        "russia": "geopolitics",
        "venezuela": "geopolitics",
        "nba": "sports",
    }
    result = cluster_by_event_or_news_window(positions, leafs, nav)
    assert result.n_clusters == 3
    assert max(len(v) for v in result.cluster_to_indices.values()) == 2


def test_missing_eventslug_and_nav_becomes_singleton():
    result = cluster_by_event_or_news_window([{}, {}], {}, {})
    assert result.n_clusters == 2
    assert result.flag == "normal"


def test_run_held_branch_uses_cross_event_clustering_when_nav_lookup_present():
    base = 1_700_000_000
    closed = [
        {
            "conditionId": f"cid_{i}",
            "outcome": "Yes",
            "curPrice": "1",
            "avgPrice": "0.5",
            "size": "10",
            "totalBought": "5",
            "realizedPnl": "5",
            "timestamp": base + i * 3600,
        }
        for i in range(3)
    ]
    closed += [
        {
            "conditionId": f"sports_{i}",
            "outcome": "Yes",
            "curPrice": "1",
            "avgPrice": "0.5",
            "size": "10",
            "totalBought": "5",
            "realizedPnl": "5",
            "timestamp": base + i * 3600,
        }
        for i in range(2)
    ]
    data = WalletData(
        wallet="0xtest",
        closed_positions=closed,
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=base,
    )
    leafs = {f"cid_{i}": ["iran"] for i in range(3)}
    leafs.update({f"sports_{i}": ["nba"] for i in range(2)})
    nav = {"iran": "geopolitics", "nba": "sports"}
    branch = run_held_branch(
        data,
        leafs_by_cid=leafs,
        nav_lookup=nav,
        n_iter=100,
    )
    assert branch.independence_check is not None
    assert branch.independence_check.n_clusters == 2
    assert branch.independence_check.flag == "concentrated"
