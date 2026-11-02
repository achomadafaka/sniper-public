"""G6 — Acceptance tests for `category_clusters` on `outcome_side_breakdown`.

Spec: docs/superpowers/specs/2026-05-17-g6-category-clusters-producer.md §3
Origin: docs/audits/2026-05-10-wallet-analyzer-live-sampling.md G6

Adds a top-level `category_clusters` list to outcome_side_breakdown
produced by `_compute_outcome_side_breakdown` when both leafs_by_cid
and nav_lookup are available. Shape matches what
`category_specialist_v1` already consumes.

10 tests covering:
  §3.1  Field presence under tag availability     — 4 tests
  §3.2  Shape correctness                          — 2 tests
  §3.3  Concentration math                         — 3 tests
  §3.4  Integration with the rule                  — 1 test

All tests must be RED until `_compute_outcome_side_breakdown` emits the
new key.
"""
from __future__ import annotations

import time

from sniper.analysis.wallet_protocol.analyze_wallet import (
    _compute_outcome_side_breakdown,
    analyze_wallet,
)
from sniper.analysis.wallet_protocol.types import WalletData


def _now() -> int:
    return int(time.time())


def _pos(
    cid: str,
    *,
    outcome: str = "Yes",
    realizedPnl: float = 100.0,
    curPrice: float = 1.0,
    avgPrice: float = 0.50,
    size: int = 100,
    ts: int | None = None,
) -> dict:
    return {
        "conditionId": cid,
        "outcome": outcome,
        "curPrice": curPrice,
        "avgPrice": avgPrice,
        "size": size,
        "realizedPnl": realizedPnl,
        "timestamp": ts if ts is not None else _now(),
        "eventSlug": f"slug-{cid}",
    }


# ===========================================================================
# §3.1  Field presence under tag availability
# ===========================================================================

def test_category_clusters_emitted_when_tag_data_available():
    """5 positions in 'politics', 2 in 'sports' — both clusters present."""
    closed = [_pos(f"cid_p{i}", realizedPnl=100.0) for i in range(5)]
    closed += [_pos(f"cid_s{i}", realizedPnl=50.0) for i in range(2)]

    leafs_by_cid = {**{f"cid_p{i}": ["election"] for i in range(5)},
                    **{f"cid_s{i}": ["nba"] for i in range(2)}}
    nav_lookup = {"election": "politics", "nba": "sports"}

    osb = _compute_outcome_side_breakdown(
        closed, leafs_by_cid=leafs_by_cid, nav_lookup=nav_lookup,
    )
    assert osb is not None
    assert "category_clusters" in osb, (
        f"category_clusters missing. Keys: {sorted(osb.keys())}"
    )
    clusters = osb["category_clusters"]
    categories = [c["category"] for c in clusters]
    assert set(categories) == {"politics", "sports"}, (
        f"Expected both 'politics' and 'sports', got {categories}"
    )


def test_category_clusters_absent_when_leafs_by_cid_is_none():
    closed = [_pos(f"cid_p{i}") for i in range(5)]
    osb = _compute_outcome_side_breakdown(
        closed, leafs_by_cid=None, nav_lookup={"election": "politics"},
    )
    assert osb is not None
    assert "category_clusters" not in osb


def test_category_clusters_absent_when_nav_lookup_is_none():
    closed = [_pos(f"cid_p{i}") for i in range(5)]
    leafs_by_cid = {f"cid_p{i}": ["election"] for i in range(5)}
    osb = _compute_outcome_side_breakdown(
        closed, leafs_by_cid=leafs_by_cid, nav_lookup=None,
    )
    assert osb is not None
    assert "category_clusters" not in osb


def test_category_clusters_absent_when_no_positions_map_to_nav():
    """leafs are present but nav_lookup has no mapping for them → omit key."""
    closed = [_pos(f"cid_p{i}") for i in range(5)]
    leafs_by_cid = {f"cid_p{i}": ["obscure_tag"] for i in range(5)}
    nav_lookup = {"election": "politics"}  # doesn't include obscure_tag
    osb = _compute_outcome_side_breakdown(
        closed, leafs_by_cid=leafs_by_cid, nav_lookup=nav_lookup,
    )
    assert osb is not None
    assert "category_clusters" not in osb


# ===========================================================================
# §3.2  Shape correctness
# ===========================================================================

def test_category_clusters_shape_matches_rule_consumer():
    closed = [_pos(f"cid_p{i}", realizedPnl=100.0) for i in range(3)]
    leafs_by_cid = {f"cid_p{i}": ["election"] for i in range(3)}
    nav_lookup = {"election": "politics"}

    osb = _compute_outcome_side_breakdown(
        closed, leafs_by_cid=leafs_by_cid, nav_lookup=nav_lookup,
    )
    clusters = osb["category_clusters"]
    assert len(clusters) == 1
    cluster = clusters[0]
    assert set(cluster.keys()) == {"category", "n", "pnl", "total_pnl"}, (
        f"Cluster keys must be {{category,n,pnl,total_pnl}}, got "
        f"{sorted(cluster.keys())}"
    )
    assert isinstance(cluster["category"], str)
    assert isinstance(cluster["n"], int)
    assert isinstance(cluster["pnl"], float)
    assert isinstance(cluster["total_pnl"], float)


def test_category_clusters_sorted_by_n_then_pnl_then_name():
    """3 categories: politics (n=5, pnl=300), sports (n=5, pnl=200),
    geo (n=3). Expected order: politics, sports, geo."""
    closed = []
    leafs_by_cid = {}
    # politics: 5 positions, pnl 60 each → 300 total
    for i in range(5):
        cid = f"cid_p{i}"
        closed.append(_pos(cid, realizedPnl=60.0))
        leafs_by_cid[cid] = ["election"]
    # sports: 5 positions, pnl 40 each → 200 total
    for i in range(5):
        cid = f"cid_s{i}"
        closed.append(_pos(cid, realizedPnl=40.0))
        leafs_by_cid[cid] = ["nba"]
    # geo: 3 positions
    for i in range(3):
        cid = f"cid_g{i}"
        closed.append(_pos(cid, realizedPnl=20.0))
        leafs_by_cid[cid] = ["sanctions"]

    nav_lookup = {
        "election": "politics", "nba": "sports", "sanctions": "geo",
    }

    osb = _compute_outcome_side_breakdown(
        closed, leafs_by_cid=leafs_by_cid, nav_lookup=nav_lookup,
    )
    cats = [c["category"] for c in osb["category_clusters"]]
    assert cats == ["politics", "sports", "geo"], (
        f"Expected [politics, sports, geo] (n-desc, then |pnl|-desc, then "
        f"name-asc), got {cats}"
    )


# ===========================================================================
# §3.3  Concentration math
# ===========================================================================

def test_cluster_total_pnl_is_wallet_wide_abs_pnl_sum():
    """5 positions with pnl [100, -50, 200, -30, 80] → total_pnl = 460
    (sum of abs values). Repeated on every cluster row."""
    pnls = [100.0, -50.0, 200.0, -30.0, 80.0]
    closed = [_pos(f"cid_{i}", realizedPnl=pnl) for i, pnl in enumerate(pnls)]
    leafs_by_cid = {f"cid_{i}": ["election"] for i in range(len(pnls))}
    nav_lookup = {"election": "politics"}

    osb = _compute_outcome_side_breakdown(
        closed, leafs_by_cid=leafs_by_cid, nav_lookup=nav_lookup,
    )
    expected_total = sum(abs(p) for p in pnls)
    for cluster in osb["category_clusters"]:
        assert cluster["total_pnl"] == expected_total, (
            f"total_pnl should be wallet-wide sum(|pnl|) = {expected_total}, "
            f"got {cluster['total_pnl']}"
        )


def test_cluster_pnl_is_in_category_sum():
    """3 positions in 'politics' with pnl [100, -50, 200] →
    cluster.pnl = 250."""
    closed = [
        _pos("cid_1", realizedPnl=100.0),
        _pos("cid_2", realizedPnl=-50.0),
        _pos("cid_3", realizedPnl=200.0),
    ]
    leafs_by_cid = {
        "cid_1": ["election"],
        "cid_2": ["election"],
        "cid_3": ["election"],
    }
    nav_lookup = {"election": "politics"}

    osb = _compute_outcome_side_breakdown(
        closed, leafs_by_cid=leafs_by_cid, nav_lookup=nav_lookup,
    )
    cluster = osb["category_clusters"][0]
    assert cluster["category"] == "politics"
    assert cluster["pnl"] == 250.0


def test_multi_category_position_counts_in_both_clusters():
    """1 position tagged as both 'politics' and 'sports'. Both clusters
    have n=1, both clusters' pnl includes its pnl. total_pnl only counts
    |pnl| once."""
    closed = [_pos("cid_dual", realizedPnl=100.0)]
    leafs_by_cid = {"cid_dual": ["election", "nba"]}
    nav_lookup = {"election": "politics", "nba": "sports"}

    osb = _compute_outcome_side_breakdown(
        closed, leafs_by_cid=leafs_by_cid, nav_lookup=nav_lookup,
    )
    clusters = {c["category"]: c for c in osb["category_clusters"]}
    assert clusters["politics"]["n"] == 1
    assert clusters["sports"]["n"] == 1
    assert clusters["politics"]["pnl"] == 100.0
    assert clusters["sports"]["pnl"] == 100.0
    # total_pnl is wallet-wide |pnl| sum: just one position with |100| = 100
    assert clusters["politics"]["total_pnl"] == 100.0
    assert clusters["sports"]["total_pnl"] == 100.0


# ===========================================================================
# §3.4  Integration with the rule — the failing-real-wallet case
# ===========================================================================

def test_category_specialist_v1_now_matches_real_report():
    """Build a real ProtocolReport via analyze_wallet with 6 of 8 positions
    in 'politics'. Run the rule; assert pattern_predicate True. Before
    this slice, the rule was always INCONCLUSIVE because category_clusters
    never existed on real reports."""
    from sniper.analysis.strategy_pattern_audit.seed_rules import (
        default_seed_rules,
    )

    closed = []
    leafs_by_cid = {}
    # 6 'politics' positions, mostly profitable
    for i in range(6):
        cid = f"cid_p{i}"
        closed.append(_pos(cid, realizedPnl=100.0))
        leafs_by_cid[cid] = ["election"]
    # 2 'sports' positions, small loss to dilute
    for i in range(2):
        cid = f"cid_s{i}"
        closed.append(_pos(cid, realizedPnl=-20.0))
        leafs_by_cid[cid] = ["nba"]

    data = WalletData(
        wallet="0xabc",
        closed_positions=closed,
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        archetype_layer2=None,
        execution_profile=None,
        fetched_at=_now(),
        subgraph_fallback_used=False,
        walk_full_history=False,
        n_unmapped_fills=0,
    )
    nav_lookup = {"election": "politics", "nba": "sports"}

    report = analyze_wallet(
        "0xabc",
        pre_fetched_data=data,
        leafs_by_cid=leafs_by_cid,
        nav_lookup=nav_lookup,
        n_iter=50,
        seed=1,
    )

    rule = {r.rule_id: r for r in default_seed_rules()}["category_specialist_v1"]
    assert rule.pattern_predicate(report, {}) is True, (
        "category_specialist_v1 must now match a real wallet with strong "
        "category concentration. Before G6, category_clusters was never "
        "produced and the rule always returned False."
    )
