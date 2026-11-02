"""Cohort D Amendment 22: Pump.fun created_timestamp observed after the cut but by the decision passes (age at the cut,
age_clock 'decision'); after the decision it stays metadata_future; without decision_as_of nothing changes."""
from sniper.services import memecoin_desk_ps_facts as F

CUT = 1000.0


def meta(known, offset=0.5):
    return {"source": "pumpfun", "known_at": known, "observed_at": known - 0.2 - offset, "host_received_at": known - 0.2,
            "clock_offset_s": offset, "created_timestamp": (CUT - 600) * 1000}


def test_metadata_after_the_cut_but_by_the_decision_is_accepted_and_labelled():
    assert F._clock(meta(CUT + 5), CUT) == (None, "metadata_future", None)                       # cut clock: refused
    now, reason, clock = F._clock(meta(CUT + 5), CUT, decision_as_of=CUT + 10)
    assert (now, reason, clock) == (CUT - 0.5, None, "decision")                                 # age taken at the cut
    assert F._clock(meta(CUT + 15), CUT, decision_as_of=CUT + 10)[1] == "metadata_future"         # after the decision
    assert F._clock(meta(CUT - 5), CUT, decision_as_of=CUT + 10)[2] == "cut"


def test_ages_carry_the_age_clock_on_a_known_provider_age(tmp_path):
    ages = F._ages({"mint": "m", "pool": "p"}, tmp_path, CUT, CUT, 9, meta(CUT + 5), [], decision_as_of=CUT + 10)
    p = ages["provider_reported"]
    assert p["state"] == "known" and p["age_clock"] == "decision" and abs(p["age_s"] - 599.5) < 1e-9
    assert p["decision_as_of"] == CUT + 10
    ages = F._ages({"mint": "m", "pool": "p"}, tmp_path, CUT, CUT, 9, meta(CUT + 5), [])
    assert ages["provider_reported"]["reason"] == "metadata_future" and ages["provider_reported"]["age_clock"] is None


def test_facts_for_refuses_a_bad_decision_clock_at_its_boundary(tmp_path):
    import pytest
    from test_desk_ps_model_facts import tape, facts, NOW
    c, _ = tape(tmp_path)
    assert facts(c, tmp_path, decision_as_of=NOW + 5)["version"]                 # a valid decision clock works
    for bad in (float("inf"), float("nan"), True, NOW - 1):
        with pytest.raises(ValueError, match="ps_facts_candidate"):
            facts(c, tmp_path, decision_as_of=bad)
