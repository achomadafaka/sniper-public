import pytest
from office.test_sheet_near_pass import near
from sniper.office.tools.sheet import near_pass_blockers

@pytest.mark.parametrize('check,reason',[
 ('holders','provenance_unreadable'),
 ('holders','facts_unavailable:key_error:opportunity_id'),
 ('holders','kill:concentration'),
 ('cluster','cluster_unknown_dependency:pool_tape_gap'),
 ('chart','age_unknown_provenance_unreadable'),
])
def test_global_refusal_wins_over_near_pass_allowlist(check,reason):
 assert near_pass_blockers(near({check:{'status':'unknown','reason':reason}})) is None


@pytest.mark.parametrize('reason',[
 'facts_unavailable', 'facts_unavailable:key_error', 'source_unreadable',
 'provenance_unreadable', 'binding', 'pool_binding_unknown', 'pool_tip_unproven',
 'pool_snapshot_mismatch', 'pool_block_cut_unknown', 'pool_tape_gap', 'sol_usd_future',
])
@pytest.mark.parametrize('check,prefix',[
 ('holders',''), ('cluster','cluster_unknown_dependency:'), ('chart','age_unknown_'),
])
def test_every_preregistered_refusal_overrides_allowed_reason_prefix(check,prefix,reason):
 assert near_pass_blockers(near({check:{'status':'unknown','reason':prefix+reason}})) is None


def test_kill_reason_is_refused_even_with_inconsistent_pass_status():
 row=near()
 row['evidence']['checks']['token_safety']={'status':'pass','reason':'kill:unsafe'}
 assert near_pass_blockers(row) is None


def test_refused_top_level_unknown_reason_cannot_be_hidden_by_check_allowlist():
 assert near_pass_blockers(near(reason='unknown:chart:provenance_unreadable')) is None
