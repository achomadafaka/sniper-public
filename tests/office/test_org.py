"""Task 10 org core (Lambda): pure seat/limit validation and change classification. Persistence (hire/fire/epochs in the store) is wired when Task 1 is merged."""
import hashlib
from pathlib import Path

import pytest

from sniper.office import org as O

C = Path(__file__).resolve().parents[2] / "config" / "office"


def sha(name):
    return hashlib.sha256((C / name).read_bytes()).hexdigest()


def loaded():
    return O.load_org(C / "default_org.json", sha("default_org.json")), O.load_limits(C / "limits.json", sha("limits.json"))


def test_configs_are_pinned_by_hash(tmp_path):
    other = tmp_path / "l.json"
    other.write_text((C / "limits.json").read_text().replace('"org_size_ceiling": 24', '"org_size_ceiling": 999'))
    with pytest.raises(O.ConfigMismatch):
        O.load_limits(other, sha("limits.json"))


def test_default_org_expands_to_seats_and_passes_validation():
    org, lim = loaded()
    seats = O.expand(org)
    assert len(seats) == 20 and sum(1 for s in seats if s["role"] == "Judge") == 6
    assert O.validate(seats, lim) == []
    for pod in ("pod1", "pod2"):
        fam = {s["family"] for s in seats if s["role"] == "Judge" and s["pod"] == pod}
        assert fam == {"claude", "gpt"}                                        # every pod has judges from both families
        assert sum(1 for s in seats if s["role"] == "Judge" and s["pod"] == pod) == 3          # odd: no tie on ENTER/SKIP (Alpha ruling 2026-10-28)


def test_risk_and_postmortem_cannot_be_removed_and_the_ceiling_holds():
    org, lim = loaded()
    seats = O.expand(org)
    no_risk = [s for s in seats if s["role"] != "Risk"]
    assert "missing_fixed_seat:Risk" in O.validate(no_risk, lim)
    too_big = seats + [{"role": "Scout", "pod": "pod1", "family": "gpt", "lane": "x"}] * 7
    assert "org_size_over_ceiling" in O.validate(too_big, lim)


def test_judges_must_cover_both_families_in_total():
    org, lim = loaded()
    claude_only = [dict(s, family="claude") if s["role"] == "Judge" else s for s in O.expand(org)]
    assert "judges_missing_family:gpt" in O.validate(claude_only, lim)


def test_the_head_cannot_change_tools_guards_scoring_evidence_or_live_authority():
    _, lim = loaded()
    for what in ("tools", "risk_floors", "guards", "scoring", "evidence", "live_authority", "fill_model"):
        assert O.head_may_apply(lim, {"kind": what}) is False
    assert O.head_may_apply(lim, {"kind": "persona"}) is True and O.head_may_apply(lim, {"kind": "prompt"}) is True


def test_every_material_change_opens_an_epoch_and_unknown_kinds_are_refused_not_ignored():
    _, lim = loaded()
    for k in lim["material_changes"]:
        assert O.opens_epoch(lim, {"kind": k}) is True
    assert O.opens_epoch(lim, {"kind": "desk_chat_message"}) is False
    with pytest.raises(O.UnknownChange):
        O.opens_epoch(lim, {"kind": ""})


def test_pod_count_outside_limits_is_refused():
    org, lim = loaded()
    with pytest.raises(O.BadOrg):
        O.expand(dict(org, pods=5), lim)


def test_crew_only_changes_also_open_an_epoch():
    _, lim = loaded()
    for k in ("guards", "scoring", "live_authority", "risk_floors"):
        assert O.opens_epoch(lim, {"kind": k}) is True and O.head_may_apply(lim, {"kind": k}) is False


def test_gamma_p1c_the_missing_material_kinds_open_an_epoch_and_the_crew_only_ones_are_head_forbidden():
    _, lim = loaded()
    for k in ("learning_rule", "pod_reallocation_rule", "pod_reallocation_discretionary", "clip", "hard_filter", "eligible_universe", "null_arm", "l_null", "execution_bound_evidence",
              "cost_rule", "shift_schedule", "restaff", "hire", "fire"):
        assert O.opens_epoch(lim, {"kind": k}) is True, k
    for k in ("pod_reallocation_rule", "clip", "hard_filter", "eligible_universe", "null_arm", "l_null", "execution_bound_evidence", "cost_rule", "shift_schedule"):
        assert O.head_may_apply(lim, {"kind": k}) is False, k
    for k in ("learning_rule", "pod_reallocation_discretionary", "restaff", "hire", "fire"):
        assert O.head_may_apply(lim, {"kind": k}) is True, k


def pilot():
    return O.load_org(C / "pilot_org.json", sha("pilot_org.json")), O.load_limits(C / "limits_pilot.json", sha("limits_pilot.json"))


def test_pilot_org_is_exactly_head_one_scout_and_two_judges_of_different_families_with_deterministic_risk_trader_postmortem():
    org, lim = pilot()
    seats = O.expand(org, lim)
    assert sorted((s["role"], s["family"]) for s in seats) == [("Head", "claude"), ("Judge", "claude"), ("Judge", "gpt"), ("Scout", "claude")]
    assert org["deterministic_seats"] == ["Risk", "Trader", "Postmortem"] and not any(s["role"] in org["deterministic_seats"] for s in seats)       # never agents
    assert O.validate(seats, lim, deterministic=org["deterministic_seats"]) == [] and org["pilot"] is True and lim["pilot"] is True


def test_pilot_limits_cap_the_org_and_require_both_judge_families_and_declare_the_tie_rule():
    org, lim = pilot()
    seats = O.expand(org, lim)
    assert "org_size_over_ceiling" in O.validate(seats + [{"role": "Scout", "pod": "pod1", "family": "gpt"}] * 3, lim, deterministic=org["deterministic_seats"])
    one_family = [dict(s, family="claude") if s["role"] == "Judge" else s for s in seats]
    assert "judges_missing_family:gpt" in O.validate(one_family, lim, deterministic=org["deterministic_seats"])
    assert lim["judge_tie_rule"] == "SKIP"                                                           # two judges can tie: a tie is a SKIP, never an ENTER


def test_a_fixed_seat_that_is_neither_an_agent_nor_declared_deterministic_is_still_missing():
    org, lim = loaded()                                                                                # full limits: Risk and Postmortem must exist
    seats = [s for s in O.expand(org, lim) if s["role"] != "Risk"]
    assert "missing_fixed_seat:Risk" in O.validate(seats, lim)
    assert "missing_fixed_seat:Risk" not in O.validate(seats, lim, deterministic=["Risk"])             # declared deterministic = satisfied
