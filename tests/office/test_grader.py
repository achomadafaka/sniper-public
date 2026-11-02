"""Task 8 grader: frozen claims against outcomes. Pure scoring; the store wiring waits for Task 1."""
import hashlib
import json
from pathlib import Path

import pytest

from sniper.office import grader as G

REG = Path(__file__).resolve().parents[2] / "config" / "office" / "metric_registry.json"


def claim(decision="ENTER", p=0.7, horizon=900):
    return {"decision": decision, "p_win": p, "horizon_s": horizon}


def test_registry_is_pinned_by_hash_and_a_changed_file_is_refused(tmp_path):
    sha = hashlib.sha256(REG.read_bytes()).hexdigest()
    assert G.load_registry(REG, sha)["version"] == "metric-registry-1"
    other = tmp_path / "r.json"
    changed = json.loads(REG.read_text())
    changed["censored_share_ceiling"] = 0.90
    other.write_text(json.dumps(changed))
    with pytest.raises(G.RegistryMismatch):
        G.load_registry(other, sha)                                                 # the Office cannot move the goalposts: the pinned hash does not match


def test_enter_skip_and_wait_are_graded_on_the_same_event_with_brier():
    win, loss = {"state": "observed", "net_pnl_usd": 3.0}, {"state": "observed", "net_pnl_usd": -2.0}
    assert G.grade(claim(p=0.8), win) == {"state": "graded", "y": 1, "p": 0.8, "brier": pytest.approx(0.04)}
    assert G.grade(claim("SKIP", p=0.2), loss)["brier"] == pytest.approx(0.04)           # a SKIP carries a probability of winning too
    assert G.grade(claim("WAIT", p=0.5), win)["y"] == 1


def test_unknown_outcomes_are_censored_with_the_reason_never_scored_as_loss_or_zero():
    assert G.grade(claim(), {"state": "path_gap"}) == {"state": "censored", "reason": "path_gap"}
    assert G.grade(claim(), {"state": "pending"}) == {"state": "pending"}
    assert G.grade(claim(), {"state": "observed", "net_pnl_usd": None, "reason": "no_swaps"}) == {"state": "censored", "reason": "no_swaps"}


def test_a_probability_outside_0_1_or_missing_is_refused_not_clipped():
    for p in (-0.1, 1.1, None, float("nan")):
        with pytest.raises(G.BadClaim):
            G.grade(claim(p=p), {"state": "observed", "net_pnl_usd": 1.0})


def test_skill_score_needs_100_graded_and_uses_climatology():
    g = [{"state": "graded", "y": i % 2, "p": 0.5, "brier": 0.25} for i in range(99)]
    assert G.skill(g, min_graded=100) is None                                         # too few: unknown, not 0
    g.append({"state": "graded", "y": 1, "p": 0.5, "brier": 0.25})
    s = G.skill(g, min_graded=100)
    assert s["n"] == 100 and s["base_rate"] == 0.5 and s["bss"] == pytest.approx(0.0)         # p = 0.5 on a 50% coin: no better than climatology


def test_censored_and_pending_do_not_enter_the_skill_denominator():
    g = [{"state": "graded", "y": 1, "p": 0.9, "brier": 0.01}] * 100 + [{"state": "censored", "reason": "x"}, {"state": "pending"}]
    assert G.skill(g, min_graded=100)["n"] == 100 and json.dumps(G.skill(g, min_graded=100))
