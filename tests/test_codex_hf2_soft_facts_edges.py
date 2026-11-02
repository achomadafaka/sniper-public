"""Offline soft evidence: exact denominators, causal rows, and venue isolation."""
import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_desk_cluster as C
from sniper.services import memecoin_desk_soft_facts as SF

NOW = 1_800_000_000.0


def test_exact_early_bound_is_sound_but_partial_percentages_are_unknown():
    from sniper.services.memecoin_cluster_admission import decide_cluster

    owners = [(f"owner_{i}", n) for i, n in enumerate((20, 20, 20, 20, 19, 1))]
    cached = {owner: f"funder_{i}" for i, (owner, _) in enumerate(owners[:-1])}

    def no_trace(*args, **kwargs):
        pytest.fail("cached bound must not trace")

    kwargs = dict(trace_fn=no_trace, thin_owner_pass=True, early_allow=True,
                  workers=1, now_fn=lambda: "2027-01-15T08:00:00+00:00")
    decision = decide_cluster(owners, cached, **kwargs)
    assert decision.allowed and decision.reason == "allowed_bound"
    assert decision.metrics["attempted_supply"] == 100
    assert decision.metrics["resolved_supply"] == 99
    assert decision.metrics["clustered_supply"] == 0
    check = C.check_from_decision(decision)
    assert check == {"status": "pass", "reason": "allowed_bound"}
    facts = SF.check_from_row({"check": check, "metrics": decision.metrics})
    assert facts["status"] == "pass" and facts["reason"] == "allowed_bound"
    assert facts.get("top_funder_pct") is None
    assert facts.get("clustered_pct") is None

    worst = decide_cluster(owners, {**cached, owners[-1][0]: cached[owners[0][0]]}, **kwargs)
    assert worst.allowed and worst.metrics["resolved_supply"] == 100
    assert worst.metrics["clustered_supply"] == worst.metrics["largest_funder_supply"] == 21


def metrics(**changes):
    return {"attempted_supply": 100, "resolved_supply": 100, "eligible": 5,
            "coverage": 1.0, "top_funder_of_attempted": .2,
            "clustered_of_attempted": .3, **changes}


@pytest.mark.parametrize("reason", ["allowed_bound", "pass_thin_bound"])
def test_bound_reason_with_fully_resolved_metrics_still_has_no_exact_pct(reason):
    facts = SF.check_from_row({"check": {"status": "pass", "reason": reason}, "metrics": metrics()})
    assert facts == {"status": "pass", "reason": reason}


def check(*, status="pass", reason="cluster_clear", stats=None):
    return SF.check_from_row({"check": {"status": status, "reason": reason},
                              "metrics": metrics() if stats is None else stats})


def ticket(*, ps=False):
    candidate = {"ticket_id": "fixture", "network_id": "solana", "mint": "mint", "pool": "pool",
                 "snapshot": {"hash": "a" * 64, "emission_at": NOW - 20}}
    if ps:
        candidate.update(schema="desk-candidate/pumpswap-v1", venue="pumpswap")
    return {"candidate": candidate, "stages": [{"stage": "hard_filter", "inputs": {"evidence": {
        "checks": {"holders": {"status": "pass", "reason": "largest_accounts", "top10_pct": 20,
                               "creator_pct": 3, "creator_basis": "top20", "exclusions_verified": True},
                   "cluster": {"status": "pass", "reason": "cluster_clear"}}}}}]}


def write_rows(tmp_path, t, entries):
    path = tmp_path / "cluster_checks"
    path.mkdir()
    c = t["candidate"]
    rows = [{"schema": SF.SCHEMA, **{k: c[k] for k in ("ticket_id", "network_id", "mint", "pool")},
             "snapshot_hash": c["snapshot"]["hash"], "metrics": metrics(), **entry} for entry in entries]
    day = datetime.fromtimestamp(NOW, timezone.utc).date().isoformat()
    (path / (day + ".jsonl")).write_text("".join(json.dumps(r) + "\n" for r in rows))


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


def test_complete_percentages_are_scaled_once_without_editing_the_row():
    row = {"check": {"status": "kill", "reason": "cluster_share_high"}, "metrics": metrics()}
    before = copy.deepcopy(row)
    result = SF.check_from_row(row)
    assert result["top_funder_pct"] == 20 and result["clustered_pct"] == 30
    assert row == before


@pytest.mark.parametrize("stats", [
    metrics(attempted_supply=0, resolved_supply=0),
    metrics(attempted_supply=None), metrics(attempted_supply=True),
    metrics(attempted_supply=10**1000, resolved_supply=10**1000),
    metrics(resolved_supply=None), metrics(resolved_supply=99, coverage=1.0),
    {"top_funder_of_attempted": 0, "clustered_of_attempted": 0},
])
def test_unproven_or_partial_denominators_never_become_exact_percentages(stats):
    for status in ("pass", "kill", "unknown"):
        result = check(status=status, stats=stats)
        assert result["status"] == status
        assert result.get("top_funder_pct") is None and result.get("clustered_pct") is None


@pytest.mark.parametrize("reason", ["pass_thin_owners", "cluster_share_high"])
def test_thin_book_never_calls_a_filtered_group_the_largest_funder(reason):
    result = check(reason=reason, stats=metrics(eligible=2))
    assert result.get("top_funder_pct") is None
    assert result["clustered_pct"] == 30


def test_giant_metric_is_absent_without_raising():
    result = check(stats=metrics(top_funder_of_attempted=10**1000))
    assert result.get("top_funder_pct") is None and result["clustered_pct"] == 30


@pytest.mark.parametrize("reason", ["allowed_bound", "pass_thin_bound"])
def test_bound_pass_reason_survives_the_worker_adapter(reason):
    assert C.check_from_decision(SimpleNamespace(allowed=True, reason=reason)) == {
        "status": "pass", "reason": reason}


def test_latest_known_cluster_row_wins_over_append_order(tmp_path):
    t = ticket()
    write_rows(tmp_path, t, [
        {"known_at": iso(NOW - 1), "check": {"status": "kill", "reason": "cluster_share_high"}},
        {"known_at": iso(NOW - 2), "check": {"status": "pass", "reason": "cluster_clear"}},
    ])
    assert SF.bound_cluster_check(t["candidate"], desk_dir=tmp_path, now=NOW)["status"] == "kill"


@pytest.mark.parametrize("known", [datetime.fromtimestamp(NOW - 1).isoformat(), iso(NOW + 1), "broken", iso(NOW - 21)])
def test_naive_future_malformed_or_preemission_row_never_binds(tmp_path, known):
    t = ticket()
    write_rows(tmp_path, t, [{"known_at": known, "check": {"status": "pass", "reason": "cluster_clear"}}])
    assert SF.bound_cluster_check(t["candidate"], desk_dir=tmp_path, now=NOW) is None


def test_pumpswap_cluster_is_not_applicable_without_curve_io(tmp_path, monkeypatch):
    t = ticket(ps=True)
    before = copy.deepcopy(t)
    def forbidden(*args, **kwargs):
        pytest.fail("PumpSwap must not read curve cluster files")
    monkeypatch.setattr(SF, "bound_cluster_check", forbidden)
    out = SF.for_ticket(t, desk_dir=tmp_path, now=NOW)
    assert out["cluster"]["state"] == "unknown" and out["cluster"]["reason"] == "not_applicable"
    assert out["holders"]["top10_pct"] == 20
    assert t == before


@pytest.mark.parametrize("raw", [None, [], {"check": None}, {"check": {"status": []}},
                                  {"check": {"status": "pass", "reason": {}}}])
def test_malformed_cluster_body_becomes_typed_unknown(raw):
    result = SF.check_from_row(raw)
    assert result["status"] == "unknown"


def test_unreadable_file_is_unknown_while_stored_holder_facts_survive(tmp_path):
    path = tmp_path / "cluster_checks"
    path.mkdir()
    (path / (datetime.fromtimestamp(NOW, timezone.utc).date().isoformat() + ".jsonl")).mkdir()
    out = SF.for_ticket(ticket(), desk_dir=tmp_path, now=NOW)
    assert out["cluster"]["state"] == "unknown" and out["cluster"]["reason"] == "unreadable"
    assert out["holders"]["top10_pct"] == 20
