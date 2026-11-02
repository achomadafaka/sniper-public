"""Low-population tag grains must not promote to supported."""
from __future__ import annotations

from sniper.analysis.wallet_protocol.held_analyst import run_held_branch
from sniper.analysis.wallet_protocol.types import TagGrainResult, WalletData


def _closed_win(idx: int) -> dict:
    return {
        "conditionId": f"cid_{idx}",
        "outcome": "Yes",
        "curPrice": 1.0,
        "avgPrice": 0.40,
        "size": 100,
        "realizedPnl": 60.0,
        "timestamp": 1700000000 + idx,
        "eventSlug": f"event-{idx}",
    }


def _wallet_data(closed: list[dict]) -> WalletData:
    return WalletData(
        wallet="0xabc",
        closed_positions=closed,
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=1700000000,
    )


def _apply_pool_promotion(g: TagGrainResult, *, rejected: bool) -> str:
    from sniper.analysis.wallet_protocol.held_analyst import _verdict_after_pool

    new_verdict = _verdict_after_pool(
        "inconclusive", rejected, g.ci_lower, g.ci_upper,
    )
    if new_verdict == "supported" and g.confidence_flag != "ok":
        new_verdict = "inconclusive"
    return new_verdict


def test_low_population_grain_does_not_promote_via_gate():
    g = TagGrainResult(
        grain="leaf", tag="politics", peer_count=15,
        confidence_flag="low_population",
        n_at_grain=15, metric_value=1.0, pop_baseline=0.30,
        excess_pp=70.0, verdict="inconclusive",
        ci_lower=0.20, ci_upper=0.80,
    )

    assert _apply_pool_promotion(g, rejected=True) == "inconclusive"


def test_ok_population_grain_still_promotes_via_gate():
    g = TagGrainResult(
        grain="leaf", tag="politics", peer_count=50,
        confidence_flag="ok",
        n_at_grain=15, metric_value=1.0, pop_baseline=0.30,
        excess_pp=70.0, verdict="inconclusive",
        ci_lower=0.20, ci_upper=0.80,
    )

    assert _apply_pool_promotion(g, rejected=True) == "supported"


def test_held_branch_low_population_h5_grain_stays_inconclusive_end_to_end():
    closed = [_closed_win(i) for i in range(50)]
    leafs_by_cid = {f"cid_{i}": ["politics"] for i in range(50)}
    nav_lookup = {"politics": "current-events"}

    def peer_fn(grain: str, tag: str, wallet: str) -> tuple[float, int]:
        return (0.30, 15)

    branch = run_held_branch(
        _wallet_data(closed),
        peer_baseline_fn=peer_fn,
        leafs_by_cid=leafs_by_cid,
        nav_lookup=nav_lookup,
        n_iter=300,
        seed=1,
    )
    politics_grains = [g for g in branch.tag_grains if g.tag == "politics"]
    assert politics_grains
    assert any(g.confidence_flag == "low_population" for g in politics_grains)
    assert all(g.verdict != "supported" for g in politics_grains)
