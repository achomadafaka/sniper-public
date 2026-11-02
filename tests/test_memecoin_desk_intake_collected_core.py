"""Operator 2026-10-29 (f7a006dd): dump_group and cluster are facts the JUDGE weighs, not vetoes. A cohort may freeze
require_collected = ["chart","holders"]; the full four-list keeps meaning order B; anything else stays malformed."""
import pytest

from test_memecoin_desk_intake_collected import collected, decide, intake_candidate
from sniper.services import memecoin_desk_contract as K

CORE = ["chart", "holders"]
UNK = {"status": "unknown", "reason": "dump_group_unknown"}


def test_core_list_passes_with_unknown_dump_group_and_cluster():
    c = intake_candidate()
    e = collected(c, dump_group=UNK, cluster={"status": "unknown", "reason": "cluster_unknown_dependency"})
    assert decide(c, e, require_collected=CORE) == "pass"


@pytest.mark.parametrize("name", CORE)
def test_core_list_still_refuses_unknown_chart_or_holders(name):
    c = intake_candidate()
    e = collected(c, **{name: {"status": "unknown", "reason": "not_collected"}})
    assert decide(c, e, require_collected=CORE).startswith("unknown:%s:" % name)


def test_full_list_still_refuses_unknown_dump_group():
    c = intake_candidate()
    assert decide(c, collected(c, dump_group=UNK), require_collected=list(K.INTAKE_COLLECTED)).startswith("unknown:dump_group:")


@pytest.mark.parametrize("bad", [["holders"], ["chart"], ["holders", "chart"], ["chart", "holders", "cluster"], []])
def test_other_subsets_stay_malformed(bad):
    c = intake_candidate()
    assert decide(c, collected(c), require_collected=bad) == "unknown:policy:intake"


def test_provisioner_freezes_the_core_list_into_a_distinct_cohort_identity():
    from test_desk_d_policy_binding import COLLECTED, d_policy, identity
    core = d_policy(intake={**d_policy()['hard_filter']['intake'], 'require_collected': ["chart", "holders"]})
    assert core['hard_filter']['intake']['require_collected'] == ["chart", "holders"]
    assert identity(core) != identity(d_policy())
    assert d_policy()['hard_filter']['intake']['require_collected'] == COLLECTED
