"""Delta HOLD 7e04cc470 witnesses (verbatim from ~/.cache/codex/delta-7e04cc470-ageclock-review/test_witness.py)."""
from sniper.services import memecoin_desk_ps_facts as F


def metadata(known):
    return {'source':'pumpfun','known_at':known,'host_received_at':known-.2,
            'observed_at':known-.2,'clock_offset_s':0.,'created_timestamp':400000}


def test_nonfinite_decision_clock_does_not_grant_unbounded_knowledge():
    now,reason,_=F._clock(metadata(1015.),1000.,decision_as_of=float('inf'))
    assert now is None and reason is not None


def test_metadata_after_backwards_decision_never_becomes_known():
    now,reason,_=F._clock(metadata(999.5),1000.,decision_as_of=999.)
    assert now is None and reason is not None
