from __future__ import annotations

import time


def _policy():
    from sniper.services import memecoin_cluster_admission

    return memecoin_cluster_admission


def _resolved(owner: str) -> tuple[str, str]:
    return f"FUNDER-{owner}", "resolved"


def test_exact_25_percent_clustered_attempted_supply_blocks() -> None:
    policy = _policy()
    decision = policy.decide_cluster(
        [("A", 15), ("B", 10), ("C", 25), ("D", 25), ("E", 25)],
        {"A": "WEB", "B": "WEB", "C": "FC", "D": "FD", "E": "FE"},
        trace_fn=_resolved,
    )
    assert decision.allowed is False
    assert decision.reason == "cluster_share_high"
    assert decision.metrics["clustered_supply"] == 25
    assert decision.metrics["attempted_supply"] == 100


def test_exact_50_percent_top_funder_attempted_supply_blocks() -> None:
    policy = _policy()
    decision = policy.decide_cluster(
        [("A", 50), ("B", 20), ("C", 15), ("D", 15)],
        {"A": "WEB", "B": "FB", "C": "FC", "D": "FD"},
        trace_fn=_resolved,
    )
    assert decision.allowed is False
    assert decision.reason == "cluster_top_funder_high"
    assert decision.metrics["largest_funder_supply"] == 50


def test_exact_50_percent_coverage_cannot_allow() -> None:
    policy = _policy()
    decision = policy.decide_cluster(
        [("A", 25), ("B", 25), ("C", 25), ("D", 25)],
        {"A": "FA", "B": "FB"},
        trace_fn=lambda _owner: (None, "unresolved"),
    )
    assert decision.allowed is False
    assert decision.reason == "cluster_unknown_unresolved"
    assert decision.metrics["coverage"] == 0.5


def test_one_unresolved_owner_blocks_an_allow() -> None:
    policy = _policy()
    decision = policy.decide_cluster(
        [("A", 40), ("B", 30), ("C", 20), ("D", 10)],
        {"A": "FA", "B": "FB", "C": "FC"},
        trace_fn=lambda _owner: (None, "unresolved"),
    )
    assert decision.allowed is False
    assert decision.reason == "cluster_unknown_unresolved"


def test_fewer_than_four_eligible_owners_blocks() -> None:
    policy = _policy()
    decision = policy.decide_cluster(
        [("A", 40), ("B", 30), ("C", 30)],
        {"A": "FA", "B": "FB", "C": "FC"},
        trace_fn=lambda _owner: (_ for _ in ()).throw(AssertionError("unexpected trace")),
    )
    assert decision.allowed is False
    assert decision.reason == "cluster_too_few_owners"


def test_clean_distributed_book_allows_after_full_resolution() -> None:
    policy = _policy()
    decision = policy.decide_cluster(
        [("A", 40), ("B", 30), ("C", 20), ("D", 10)],
        {},
        trace_fn=_resolved,
    )
    assert decision.allowed is True
    assert decision.reason == "allowed"
    assert decision.metrics["coverage"] == 1.0


def test_aas_supply_fixture_blocks_from_linkage_not_concentration() -> None:
    policy = _policy()
    decision = policy.decide_cluster(
        [("A", 14), ("B", 11), ("C", 10), ("D", 10), ("E", 55)],
        {"A": "WEB", "B": "WEB", "C": "WEB", "D": "WEB", "E": "OTHER"},
        trace_fn=_resolved,
    )
    assert decision.allowed is False
    assert decision.reason == "cluster_share_high"
    assert decision.metrics["clustered_of_attempted"] == 0.45


def test_eigenslur_lower_bound_blocks_before_full_resolution() -> None:
    policy = _policy()
    trace_calls: list[str] = []
    decision = policy.decide_cluster(
        [("A", 22), ("B", 22), ("C", 20), ("D", 18), ("E", 18)],
        {"A": "WEB", "B": "WEB"},
        trace_fn=lambda owner: (trace_calls.append(owner), _resolved(owner))[1],
    )
    assert decision.allowed is False
    assert decision.reason == "cluster_share_high"
    assert decision.metrics["clustered_of_attempted"] == 0.44
    assert trace_calls == []


def test_unknown_rank_one_stays_eligible_and_blocks_allow() -> None:
    policy = _policy()
    decision = policy.decide_cluster(
        [("UNKNOWN-RANK-ONE", 60), ("B", 15), ("C", 15), ("D", 10)],
        {"B": "FB", "C": "FC", "D": "FD"},
        trace_fn=lambda owner: (None, "unresolved") if owner == "UNKNOWN-RANK-ONE" else _resolved(owner),
    )
    assert decision.allowed is False
    assert decision.reason == "cluster_unknown_unresolved"
    assert decision.metrics["attempted_supply"] == 100


def test_buyer_count_cannot_satisfy_the_supply_policy() -> None:
    policy = _policy()
    assert "buyer_count" not in policy.decide_cluster.__code__.co_varnames
    decision = policy.decide_cluster(
        [("A", 60), ("B", 15), ("C", 15), ("D", 10)],
        {"B": "FB", "C": "FC", "D": "FD"},
        trace_fn=lambda _owner: (None, "unresolved"),
    )
    assert decision.allowed is False


def test_deadline_cancels_pending_work_and_blocks() -> None:
    policy = _policy()

    def slow_trace(_owner: str) -> tuple[str, str]:
        time.sleep(0.05)
        return "FUNDER", "resolved"

    started = time.monotonic()
    decision = policy.decide_cluster(
        [("A", 25), ("B", 25), ("C", 25), ("D", 25)],
        {},
        trace_fn=slow_trace,
        deadline_s=0.001,
    )
    assert decision.allowed is False
    assert decision.reason == "cluster_unknown_deadline"
    assert time.monotonic() - started < 0.04


def test_trace_error_is_visible_and_blocks() -> None:
    policy = _policy()

    def broken_trace(_owner: str) -> tuple[str, str]:
        raise RuntimeError("trace failed")

    decision = policy.decide_cluster(
        [("A", 25), ("B", 25), ("C", 25), ("D", 25)],
        {},
        trace_fn=broken_trace,
    )
    assert decision.allowed is False
    assert decision.reason == "cluster_unknown_trace_error"


def test_each_new_trace_result_reaches_the_durable_cache_callback() -> None:
    policy = _policy()
    persisted: list[tuple[str, str | None, str]] = []
    decision = policy.decide_cluster(
        [("A", 40), ("B", 30), ("C", 20), ("D", 10)],
        {},
        trace_fn=_resolved,
        persist_fn=lambda owner, funder, status: persisted.append((owner, funder, status)),
    )
    assert decision.allowed is True
    assert sorted(persisted) == [
        ("A", "FUNDER-A", "resolved"),
        ("B", "FUNDER-B", "resolved"),
        ("C", "FUNDER-C", "resolved"),
        ("D", "FUNDER-D", "resolved"),
    ]
