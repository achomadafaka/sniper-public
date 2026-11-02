"""Tests for §6.2 eventSlug-only independence clustering and pool_and_correct."""

import pytest

from sniper.analysis.wallet_protocol.independence import (
    CLUSTER_SIZE_FLAG_MIN,
    CONCENTRATED_FRACTION_THRESHOLD,
    cluster_by_eventslug,
    cluster_reduced_n,
)
from sniper.analysis.wallet_protocol.statistics import pool_and_correct


# ----------------------------------------------------------------------------
# cluster_by_eventslug
# ----------------------------------------------------------------------------


def test_cluster_distinct_event_slugs_no_concentration():
    positions = [{"eventSlug": f"event-{i}"} for i in range(20)]
    result = cluster_by_eventslug(positions)
    assert result.n_total == 20
    assert result.n_clusters == 20
    assert result.flag == "normal"
    assert result.fraction_in_concentrated == 0.0


def test_cluster_concentrated_when_one_event_dominates():
    # 8 positions in event-A (cluster size 8 >= 3), 2 standalone
    positions = [{"eventSlug": "event-A"} for _ in range(8)]
    positions += [{"eventSlug": "event-B"}, {"eventSlug": "event-C"}]
    result = cluster_by_eventslug(positions)
    assert result.n_clusters == 3
    assert result.n_in_concentrated_clusters == 8  # event-A
    assert result.fraction_in_concentrated == 0.8
    assert result.flag == "concentrated"


def test_cluster_pair_size_two_does_not_concentrate():
    """Cluster size 2 is below CLUSTER_SIZE_FLAG_MIN (3). Not concentrated."""
    assert CLUSTER_SIZE_FLAG_MIN == 3
    positions = [{"eventSlug": "A"}, {"eventSlug": "A"}]
    positions += [{"eventSlug": f"x{i}"} for i in range(10)]
    result = cluster_by_eventslug(positions)
    assert result.n_in_concentrated_clusters == 0
    assert result.flag == "normal"


def test_cluster_threshold_at_20_percent():
    """Spec rule: >20% of positions in clusters >=3 → concentrated."""
    assert CONCENTRATED_FRACTION_THRESHOLD == 0.20
    # 3-position cluster A + 7 distinct singletons = 30% concentrated > 20%
    positions = [{"eventSlug": "A"} for _ in range(3)]
    positions += [{"eventSlug": f"x{i}"} for i in range(7)]
    result = cluster_by_eventslug(positions)
    assert result.fraction_in_concentrated == 0.30
    assert result.flag == "concentrated"


def test_cluster_missing_eventslug_treated_as_orphan():
    """Positions without eventSlug must not silently merge."""
    positions = [{}, {}, {}]
    result = cluster_by_eventslug(positions)
    assert result.n_clusters == 3
    assert result.flag == "normal"


def test_cluster_empty_input():
    result = cluster_by_eventslug([])
    assert result.n_total == 0
    assert result.n_clusters == 0
    assert result.flag == "normal"


def test_cluster_indices_are_sorted_per_cluster():
    """Indices within a cluster preserve input order."""
    positions = [
        {"eventSlug": "X"},   # idx 0
        {"eventSlug": "Y"},   # idx 1
        {"eventSlug": "X"},   # idx 2
        {"eventSlug": "X"},   # idx 3
    ]
    result = cluster_by_eventslug(positions)
    assert result.cluster_to_indices["X"] == [0, 2, 3]


def test_cluster_reduced_n_is_distinct_event_count():
    """Cluster-reduced n = number of distinct events (most pessimistic adjustment)."""
    positions = [
        {"eventSlug": "A"}, {"eventSlug": "A"}, {"eventSlug": "A"},  # cluster A
        {"eventSlug": "B"}, {"eventSlug": "B"},                        # cluster B
        {"eventSlug": "C"},                                            # singleton C
    ]
    assert cluster_reduced_n(positions) == 3


# ----------------------------------------------------------------------------
# pool_and_correct
# ----------------------------------------------------------------------------


def test_pool_and_correct_preserves_names_in_output():
    named = [("H1", 0.001), ("H2", 0.5), ("H5:leaf:iran", 0.04)]
    result = pool_and_correct(named, q=0.10)
    assert set(result.keys()) == {"H1", "H2", "H5:leaf:iran"}
    assert isinstance(result["H1"], tuple)
    assert len(result["H1"]) == 2  # (adjusted_p, rejected)


def test_pool_and_correct_rejects_significant():
    named = [("H2", 0.001), ("H3", 0.5)]
    result = pool_and_correct(named, q=0.10)
    assert result["H2"][1] is True
    assert result["H3"][1] is False


def test_pool_and_correct_handles_mixed_named_and_per_grain():
    """Realistic pool: branch-level + per-grain tests for H5/FH4."""
    named = [
        ("H2", 0.04),
        ("H3", 0.3),
        ("H5:leaf:iran", 0.001),
        ("H5:leaf:nuclear-weapons", 0.002),
        ("H5:nav:geopolitics", 0.005),
    ]
    result = pool_and_correct(named, q=0.10)
    assert len(result) == 5
    # The strong tag-grain p-values should survive correction
    assert result["H5:leaf:iran"][1] is True


def test_pool_and_correct_rejects_duplicate_names():
    """Silent overwrite would corrupt the report."""
    named = [("H2", 0.001), ("H2", 0.5)]
    with pytest.raises(ValueError, match="duplicate"):
        pool_and_correct(named)


def test_pool_and_correct_empty_input():
    assert pool_and_correct([]) == {}
