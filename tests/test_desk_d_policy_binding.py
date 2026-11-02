"""D is one complete identity; an incomplete evidence bundle is never provisionable."""
import copy
import hashlib
import json

import pytest

from scripts import memecoin_desk_provision_cohort as P
from sniper.services import memecoin_desk_contract as K
from sniper.services import memecoin_desk_ps_trending as T
from test_desk_v28_policy_binding import BOUNDS, DIGEST, FEEDS, RULE


COLLECTED = ['chart', 'cluster', 'dump_group', 'holders']
VERSIONS = {'buyers': 'desk-buyers-v1', 'history': 'history-facts-v1',
            'heat': 'desk-heat-v2', 'list_claims': 'desk-list-claims-v1',
            'd_sheet': 'desk-d-sheet-v1', 'chart': 'pumpswap-facts-v3',
            'ps_trending': 'desk-ps-trending-v1'}
PREREG = {'path': 'docs/audits/2026-10-28-desk-cohort-d-all-sheet-gaps-preregistration.md',
          'commit': '87c0f666f8a67e4a29e5eacb564ea798f29a9aff'}


def definitions():
    # Unit identities only. The CLI must obtain real definitions from the actual release.
    return {name: {'version': version, 'definition_sha256': format(i, '064x')}
            for i, (name, version) in enumerate(VERSIONS.items(), 1)}


def d_policy(**changes):
    args = dict(judge_p_admit=1, round_trip_loss_pct_max=8, hook_allowlist=[],
                rail_bounds=BOUNDS, venue='pumpswap', cluster=True,
                intake={**RULE, 'feeds': FEEDS, 'require_collected': COLLECTED},
                ps_inputs=True, ps_inputs_version='pumpswap-facts-v3',
                judge_playbook_sha256=DIGEST, preregistration=PREREG,
                d_definitions=definitions())
    args.update(changes)
    return P.frozen_policy(**args)


def identity(policy):
    return P.cohort_id(policy, prompt_version='desk-judge-v29')


def test_d_policy_binds_the_whole_bundle_and_existing_rails():
    tags = definitions()
    policy = d_policy(d_definitions=tags)
    assert policy['ps_inputs'] == 'pumpswap-facts-v3'
    assert policy['judge_playbook_sha256'] == DIGEST
    assert policy['preregistration'] == PREREG
    assert policy['hard_filter']['intake']['require_collected'] == COLLECTED
    assert policy['context']['ps_trending'] == tags['ps_trending']
    assert {name: policy[name] for name in tags if name != 'ps_trending'} == {
        name: tag for name, tag in tags.items() if name != 'ps_trending'}
    assert (policy['clip_usd'], policy['max_open'], policy['judge_calls_per_hour']) == (25, 5, 40)
    assert policy['judge_model'] == 'claude-opus-5-5'
    assert 'judge_provider' not in policy and 'judge_effort' not in policy
    assert 'dex_token' not in policy
    assert 'timing' not in policy


@pytest.mark.parametrize('component', VERSIONS)
def test_each_definition_changes_the_d_identity(component):
    tags = definitions()
    original = identity(d_policy(d_definitions=tags))
    tags[component]['definition_sha256'] = 'f' * 64
    assert identity(d_policy(d_definitions=tags)) != original


@pytest.mark.parametrize('component', VERSIONS)
def test_missing_component_cannot_create_a_partial_d(component):
    tags = definitions()
    del tags[component]
    with pytest.raises(ValueError, match='ps_inputs'):
        d_policy(d_definitions=tags)


@pytest.mark.parametrize('tag', [None, {}, {'version': 'history-facts-v1'},
                              {'version': '', 'definition_sha256': 'f' * 64},
                              {'version': True, 'definition_sha256': 'f' * 64},
                              {'version': 'history-facts-v1', 'definition_sha256': 'A' * 64},
                              {'version': 'history-facts-v1', 'definition_sha256': 'f' * 63},
                              {'version': 'history-facts-v1', 'definition_sha256': False},
                              {'version': 'history-facts-v1', 'definition_sha256': 'f' * 64, 'extra': 1}])
def test_malformed_definition_is_refused(tag):
    tags = definitions()
    tags['history'] = tag
    with pytest.raises(ValueError, match='ps_inputs'):
        d_policy(d_definitions=tags)


@pytest.mark.parametrize('changes', [dict(d_definitions=None), dict(d_definitions={}),
                                   dict(cluster=False), dict(preregistration=None),
                                   dict(intake={**RULE, 'feeds': FEEDS}),
                                   dict(intake={**RULE, 'feeds': FEEDS, 'require_collected': COLLECTED[:-1]})])
def test_d_requires_collection_cluster_prereg_and_all_definitions(changes):
    with pytest.raises(ValueError, match='ps_inputs'):
        d_policy(**changes)


def test_policy_owns_its_tags_without_mutating_shared_definition_objects():
    tags = definitions()
    before = copy.deepcopy(tags)
    policy = d_policy(d_definitions=tags)
    assert tags == before
    policy['history']['definition_sha256'] = 'f' * 64
    policy['context']['ps_trending']['version'] = 'changed'
    assert tags == before


def test_legacy_policy_cannot_silently_ignore_d_definitions():
    with pytest.raises(ValueError, match='ps_inputs'):
        d_policy(ps_inputs_version='pumpswap-facts-v2')


@pytest.fixture
def d_cli(tmp_path, monkeypatch):
    """CLI identity unit seam; the real release validator is tested after component integration."""
    monkeypatch.setenv('SNIPER_STATE_ROOT', str(tmp_path))
    monkeypatch.setenv('MEMECOIN_DESK_RAIL_BOUNDS', 'tp_pct:15-150,sl_pct:10-40,trail_pct:8-30')
    monkeypatch.delenv('MEMECOIN_DESK_JUDGE_MAX_OUTPUT_TOKENS', raising=False)
    tags = definitions()
    tags['ps_trending'] = T.definition()
    monkeypatch.setattr(K, 'd_components', lambda: {
        name: tag for name, tag in tags.items() if name != 'ps_trending'}, raising=False)

    def validate(policy, prompt):
        assert prompt == 'desk-judge-v29' and policy['ps_inputs'] == 'pumpswap-facts-v3'
        assert policy['context']['ps_trending'] == tags['ps_trending']
        return True

    monkeypatch.setattr(K, 'validate_ps_inputs', validate)
    playbook = tmp_path / 'v29.md'
    playbook.write_text('# Human header\nprompt_version: `desk-judge-v29`\n\n---\nD system body.\n')
    args = ['--judge-p-admit', '1', '--round-trip-loss-pct-max', '8', '--intake', '--venue',
            'pumpswap', '--ps-inputs', '--intake-require-collected', '--intake-feeds', *FEEDS,
            '--playbook', str(playbook), '--intake-prereg', PREREG['path'] + '@' + PREREG['commit'],
            '--desk-db', str(tmp_path / 'desk.db'), '--ledger-db', str(tmp_path / 'ledger.db')]
    return args, playbook, tags


def test_v29_cli_freezes_the_sent_body_and_actual_definition_loader_without_writes(d_cli, capsys):
    args, playbook, tags = d_cli

    def preview():
        assert P.main(args) == 0
        output = capsys.readouterr().out
        assert 'dry run: nothing written' in output
        body, cid = output.split('\nMEMECOIN_DESK_U_COHORT_ID=', 1)
        assert not (playbook.parent / 'desk.db').exists()
        assert not (playbook.parent / 'ledger.db').exists()
        return json.loads(body), cid.splitlines()[0]

    body, cid = preview()
    policy = body['policy']
    assert body['prompt_version'] == 'desk-judge-v29'
    assert policy['judge_playbook_sha256'] == hashlib.sha256(b'D system body.\n').hexdigest()
    assert {name: policy[name] for name in tags if name != 'ps_trending'} == {
        name: tag for name, tag in tags.items() if name != 'ps_trending'}
    playbook.write_text('# Cosmetic note\n' + playbook.read_text())
    assert preview()[1] == cid
    playbook.write_text(playbook.read_text() + 'Changed system text.\n')
    changed_system_cid = preview()[1]
    assert changed_system_cid != cid
    tags['heat']['definition_sha256'] = 'f' * 64
    assert preview()[1] != changed_system_cid


@pytest.mark.parametrize('missing_flag', ['--ps-inputs', '--intake-require-collected'])
def test_v29_cli_rejects_incomplete_mode_before_registry_access(d_cli, capsys, missing_flag):
    args, playbook, _ = d_cli
    args.remove(missing_flag)
    with pytest.raises((ValueError, SystemExit)):
        P.main(args)
    assert 'MEMECOIN_DESK_U_COHORT_ID=' not in capsys.readouterr().out
    assert not (playbook.parent / 'desk.db').exists() and not (playbook.parent / 'ledger.db').exists()


def test_v29_cli_refuses_a_partial_definition_loader_before_preview(d_cli, monkeypatch, capsys):
    args, playbook, tags = d_cli
    tags.pop('d_sheet')
    with pytest.raises(ValueError, match='ps_inputs'):
        P.main(args)
    assert 'MEMECOIN_DESK_U_COHORT_ID=' not in capsys.readouterr().out
    assert not (playbook.parent / 'desk.db').exists() and not (playbook.parent / 'ledger.db').exists()


def test_d_release_definitions_pass_the_actual_validator():
    policy = d_policy(d_definitions=P._load_d_definitions())
    assert K.validate_ps_inputs(policy, 'desk-judge-v29') is True


@pytest.mark.parametrize('model', ['gpt-6-luna', 'claude-haiku-5-5'])
def test_d_refuses_non_opus_override(model):
    with pytest.raises(ValueError, match='d_judge_model'):
        d_policy(judge_model=model)


@pytest.mark.parametrize('model', ['claude-opus-5-5', 'gpt-6-luna'])
@pytest.mark.parametrize('error', ['provider_error', 'auth_expired', 'rate_limited', 'timeout'])
def test_error_without_receipt_keeps_declared_subscription_proof(monkeypatch, model, error):
    from contextlib import nullcontext
    from datetime import datetime, timezone
    from sniper.services import memecoin_desk_judge as J
    policy = {'judge_model': model, 'model_billing_basis': 'subscription_flat', 'rail_bounds': {}}
    if model == 'gpt-6-luna': policy.update(judge_provider='codex_cli', judge_effort='low')
    monkeypatch.setattr(J.store, 'get_ticket', lambda *a: {'ticket_id':'t', 'cohort_id':'c'})
    monkeypatch.setattr(J.store, 'get_cohort', lambda *a: {'policy':policy})
    got = {}
    at = '2026-10-28T12:00:00+00:00'
    result = {'call_error':error, 'billing_basis':'subscription_flat', 'result_id':None, 'session_id':None}
    J._record(None, lambda:nullcontext(None), {'ticket_id':'t','call_id':'c','worker':'judge-123',
        'claimed_at':at, 'call_started_at':at,'response_at':at,'elapsed_ms':1,'result':result},
        lambda:datetime(2026,10,28,12,tzinfo=timezone.utc), record_fn=lambda *a,**kw:got.update(kw) or {})
    assert got['model_cost_usd'] == 0
    assert got['cost_provenance'].startswith('model:'+J.judge_provider(model)+':subscription_flat:')


def test_v29_missing_both_source_bindings_refuses_without_store_writes(databases):
    from sniper.services import memecoin_desk as desk
    policy = d_policy(d_definitions=P._load_d_definitions())
    del policy['ps_inputs']
    del policy['hard_filter']['intake_source_version']
    before = [db.total_changes for db in databases]
    with pytest.raises(ValueError, match='ps_inputs'):
        desk.provision_cohort(*databases, producer='U', policy=policy,
                              prompt_version='desk-judge-v29', created_at='2026-10-28T12:00:00+00:00')
    assert [db.total_changes for db in databases] == before


@pytest.mark.parametrize('correct_body', [False, True])
def test_v29_frozen_system_body_is_checked_before_model_claim(databases, tmp_path, monkeypatch, correct_body):
    from sniper.services import memecoin_desk_judge as J
    from sniper.persistence import db_memecoin_desk as store
    from test_desk_ps_inputs_integration import ready, Clock, forbidden
    ticket, cid = ready(databases, tmp_path/'tape')
    policy = d_policy(d_definitions=P._load_d_definitions())
    policy['judge_playbook_sha256'] = hashlib.sha256(b'approved-body').hexdigest()
    get_cohort = store.get_cohort
    monkeypatch.setattr(store, 'get_cohort', lambda conn,key: {**get_cohort(conn,key),
                       'policy':policy,'prompt_version':'desk-judge-v29'})
    kwargs = dict(call_model=forbidden, system_prompt='approved-body' if correct_body else 'tampered-body',
                  worker='judge-test', now_fn=Clock(), calls_per_hour=40)
    if correct_body:
        result = J.run_once(databases[0], forbidden, **kwargs)
        assert result[0]['reason'] == 'ps_inputs_required'
    else:
        with pytest.raises(ValueError, match='judge_playbook_mismatch'):
            J.run_once(databases[0], forbidden, **kwargs)
        assert store.get_ticket(databases[0],ticket)['state'] != 'skipped'
    assert databases[0].execute('SELECT COUNT(*) FROM desk_judge_calls').fetchone()[0] == 0


from test_memecoin_desk_judge import databases  # noqa: E402,F401
