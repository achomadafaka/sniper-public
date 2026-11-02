"""Forward-tracking job (D4) acceptance tests.

Verdict logic is unit-tested via injected fake producers (controlled n / effect /
evidence_state) so the rules are exercised precisely, decoupled from producer internals.
Orchestration + idempotency use the in-memory store + a fake provider.
"""
from __future__ import annotations

import pytest

from sniper.analysis.thesis_cards.contract import producer_card
from sniper.analysis.thesis_cards.enrollment import InMemoryPaperEnrollmentStore, PaperEnrollment
from sniper.analysis.thesis_cards.forward_tracking import evaluate, run_forward_tracking

_DAY = 86400
_EA = 1_000_000                       # enrolled_at
_FTD = {"metric": "calibration_edge", "horizon": "to_resolution", "success_threshold": 0.0,
        "kill_threshold": -0.05, "min_forward_n": 20, "stake_convention": "uniform_paper_unit"}
_LIVE_FIELDS = {"price", "size", "order_id", "pnl", "size_usdc", "shares"}


def fake(*, n, effect, state="supported", p=0.01):
    def _producer(window):
        return producer_card(card_id="x", lens="x", n=n, score=max(0.0, effect or 0.0),
                             effect=effect, p_value=p, placebo_passed=(state == "supported"),
                             evidence_state=state)
    return {"calibration_edge": _producer}


def row(*, state="active", pass_count=0, last_end=None, enrolled_effect=0.10,
        metric="calibration_edge"):
    fm = {}
    if pass_count or last_end is not None:
        fm = {"pass_count": pass_count, "last_pass_window_end": last_end}
    ftd = {**_FTD, "metric": metric}
    return {"wallet": "0xw", "card_id": "sizing_card", "card_version": "1",
            "profile_run_id": "r1", "state": state, "enrolled_at": _EA,
            "enrolled_effect": enrolled_effect, "forward_test_design": ftd,
            "forward_metric": fm, "verdict": None}


WIN = [{}]                            # truthy "data present" window (fake ignores content)
NOT_H = _EA + 1 * _DAY
H = _EA + 91 * _DAY


# --- verdict rules (pure evaluate) ---

def test_queued_goes_active_on_first_pass():
    d = evaluate(row(state="queued"), WIN, now=NOT_H, producers=fake(n=25, effect=0.10))
    assert d.new_state == "active" and d.forward_metric["pass_count"] == 1


def test_single_pass_stays_confirming():
    d = evaluate(row(pass_count=0), WIN, now=NOT_H, producers=fake(n=25, effect=0.10))
    assert d.new_state == "active" and d.verdict is None
    assert d.forward_metric["pass_count"] == 1 and d.forward_metric["status"] == "confirming"


def test_promote_on_two_sustained_passes():
    d = evaluate(row(pass_count=1, last_end=_EA + 10 * _DAY), WIN, now=NOT_H,
                 producers=fake(n=25, effect=0.10))
    assert d.new_state == "promoted" and d.verdict == "promoted_sustained"
    assert d.forward_metric["pass_count"] == 2


def test_no_promote_on_decayed_effect():
    # supported but effect 0.02 < enrolled_effect(0.10)*0.5 -> NOT a pass; streak resets
    d = evaluate(row(pass_count=1, enrolled_effect=0.10), WIN, now=NOT_H,
                 producers=fake(n=25, effect=0.02))
    assert d.new_state == "active" and d.verdict is None
    assert d.forward_metric["pass_count"] == 0


def test_kill_requires_n_and_negative_threshold():
    killed = evaluate(row(), WIN, now=NOT_H, producers=fake(n=25, effect=-0.06))
    assert killed.new_state == "killed" and killed.verdict == "killed_below_kill_threshold"
    # tiny-n negative does NOT kill
    tiny = evaluate(row(), WIN, now=NOT_H, producers=fake(n=5, effect=-0.06, state="underpowered"))
    assert tiny.new_state == "active" and tiny.verdict is None
    # above the negative bar does NOT kill
    mild = evaluate(row(), WIN, now=NOT_H, producers=fake(n=25, effect=-0.02, state="exploratory"))
    assert mild.new_state == "active" and mild.verdict is None


def test_failed_batch_resets_pass_count():
    d = evaluate(row(pass_count=1), WIN, now=NOT_H, producers=fake(n=25, effect=0.02))
    assert d.forward_metric["pass_count"] == 0


def test_completed_no_edge_at_horizon():
    d = evaluate(row(), WIN, now=H, producers=fake(n=25, effect=0.02, state="exploratory"))
    assert d.new_state == "completed" and d.verdict == "completed_no_edge"


def test_inconclusive_when_insufficient_data_at_horizon():
    d = evaluate(row(), WIN, now=H, producers=fake(n=5, effect=None, state="underpowered"))
    assert d.new_state == "completed" and d.verdict == "completed_inconclusive_insufficient_data"


def test_stays_active_while_awaiting_data():
    d = evaluate(row(), WIN, now=NOT_H, producers=fake(n=5, effect=None, state="underpowered"))
    assert d.new_state == "active" and d.verdict is None
    assert d.forward_metric["status"] == "awaiting_data"
    # also: no window at all -> awaiting_data
    d2 = evaluate(row(), None, now=NOT_H, producers=fake(n=25, effect=0.1))
    assert d2.new_state == "active" and d2.forward_metric["status"] == "awaiting_data"


def test_untrackable_metric_does_not_substitute():
    r = row(state="active", metric="totally_unknown_metric")
    d = evaluate(r, WIN, now=NOT_H, producers=fake(n=25, effect=0.10))  # registry lacks the metric
    assert d.new_state == "active" and d.verdict is None
    assert d.forward_metric["status"] == "untrackable_metric"


def test_no_live_or_sizing_fields_in_forward_metric():
    d = evaluate(row(), WIN, now=NOT_H, producers=fake(n=25, effect=0.10))
    assert not (_LIVE_FIELDS & set(d.forward_metric))


# --- store + orchestration ---

def _seed(store, **over):
    base = dict(wallet="0xw", card_id="sizing_card", card_version="1", profile_run_id="r1",
                rank=1, q_value=0.01, evidence_state="supported", data_tier=None,
                forward_test_design=dict(_FTD), state="queued", enrolled_effect=0.10,
                enrolled_at=_EA)
    base.update(over)
    pe = PaperEnrollment(**base)
    store.records[pe.idempotency_key] = pe
    return pe


def test_orchestrator_advances_queued_and_skips_terminals():
    store = InMemoryPaperEnrollmentStore()
    _seed(store, profile_run_id="r1", state="queued")
    _seed(store, profile_run_id="rT", state="promoted", verdict="promoted_sustained")  # terminal
    out = run_forward_tracking(store, lambda w, s, m, **k: WIN, now=NOT_H,
                               producers=fake(n=25, effect=0.10))
    # terminal row not iterated; queued row advanced to active (1 pass, confirming)
    assert store.records["0xw|sizing_card|1|r1"].state == "active"
    assert store.records["0xw|sizing_card|1|rT"].state == "promoted"  # untouched
    assert all(k != "0xw|sizing_card|1|rT" for k, _ in out)


def test_rerun_unchanged_when_no_new_data_is_idempotent():
    store = InMemoryPaperEnrollmentStore()
    _seed(store, state="active")
    prov = lambda w, s, m, **k: None                       # provider can't answer
    first = run_forward_tracking(store, prov, now=NOT_H, producers=fake(n=25, effect=0.1))
    fm1 = dict(store.records["0xw|sizing_card|1|r1"].forward_metric)
    second = run_forward_tracking(store, prov, now=NOT_H, producers=fake(n=25, effect=0.1))
    assert second == [("0xw|sizing_card|1|r1", "unchanged")]
    assert store.records["0xw|sizing_card|1|r1"].forward_metric == fm1  # no churn


def test_conditional_update_prevents_double_advance_and_blocks_illegal():
    store = InMemoryPaperEnrollmentStore()
    _seed(store, state="active")
    key = {"wallet": "0xw", "card_id": "sizing_card", "card_version": "1", "profile_run_id": "r1"}
    # wrong expected_state -> no-op
    assert store.update_tracking(key=key, expected_state="queued", new_state="killed",
                                 forward_metric={}, verdict="x") is False
    # illegal (unreachable) transition -> raises
    with pytest.raises(ValueError):
        store.update_tracking(key=key, expected_state="active", new_state="queued",
                              forward_metric={}, verdict=None)


def test_provider_receives_metric_for_exclusion():
    # the provider gets the metric so it can apply metric-specific exclusions (e.g.
    # drop negativeRisk from the calibration_edge window).
    seen = []
    store = InMemoryPaperEnrollmentStore()
    _seed(store, state="active")
    run_forward_tracking(store, lambda w, s, m, **k: (seen.append((w, m)) or WIN),
                         now=NOT_H, producers=fake(n=25, effect=0.10))
    assert seen and seen[0] == ("0xw", "calibration_edge")


def test_two_run_promotion_through_orchestrator():
    # run 1: pass #1 (active, confirming). run 2 over a non-overlapping window: pass #2 -> promoted.
    store = InMemoryPaperEnrollmentStore()
    _seed(store, state="active")
    run_forward_tracking(store, lambda w, s, m, **k: WIN, now=NOT_H, producers=fake(n=25, effect=0.10))
    assert store.records["0xw|sizing_card|1|r1"].state == "active"
    run_forward_tracking(store, lambda w, s, m, **k: WIN, now=NOT_H + _DAY,
                         producers=fake(n=25, effect=0.10))
    assert store.records["0xw|sizing_card|1|r1"].state == "promoted"
