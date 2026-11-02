"""A partial-flow cohort binds its actual input semantics and model prompt."""
import copy
import hashlib
import json

import pytest

from scripts import memecoin_desk_provision_cohort as P
from sniper.services import memecoin_desk_contract as K
from test_desk_ps_inputs_contract import BOUNDS, RULE, FEEDS, enabled

SYSTEM = 'Offline v28 system prompt.\n'
DIGEST = hashlib.sha256(SYSTEM.encode()).hexdigest()
PREREG = {'path': 'docs/audits/2026-10-27-three-feed-desk-inputs-v28-preregistration.md',
          'commit': '45895eb569bece68c527cb6438944f5530b1013e'}


def v28_policy(**extra):
    args = dict(judge_p_admit=1, round_trip_loss_pct_max=8, hook_allowlist=[], rail_bounds=BOUNDS,
                venue='pumpswap', intake={**RULE, 'feeds': FEEDS}, ps_inputs=True,
                ps_inputs_version='pumpswap-facts-v2', judge_playbook_sha256=DIGEST,
                preregistration=PREREG)
    args.update(extra)
    return P.frozen_policy(**args)


def test_existing_v27_policy_and_identity_are_byte_compatible(monkeypatch):
    monkeypatch.setenv('MEMECOIN_DESK_JUDGE_DEFER_MAX_S', '120')
    policy = enabled()
    assert P.cohort_id(policy, prompt_version='desk-judge-v27') == (
        'desk-cohort:U:550705a8c8d2cde9cfdb7325ab72b1b8ae7f8e7af5c3a4ffa2fa584dfba5c026')
    assert 'judge_playbook_sha256' not in policy


def test_v28_policy_binds_facts_prompt_and_executed_preregistration():
    policy = v28_policy()
    assert K.validate_ps_inputs(policy, 'desk-judge-v28') is True
    assert policy['ps_inputs'] == 'pumpswap-facts-v2'
    assert policy['judge_playbook_sha256'] == DIGEST
    assert policy['preregistration'] == PREREG
    assert (policy['judge_model'], policy['judge_provider'], policy['judge_effort']) == (
        'gpt-6-luna', 'codex_cli', 'low')
    assert P.cohort_id(policy, prompt_version='desk-judge-v28') != P.cohort_id(enabled(), prompt_version='desk-judge-v27')


@pytest.mark.parametrize('version,prompt', [('pumpswap-facts-v2', 'desk-judge-v27'),
                                         ('pumpswap-facts-v1', 'desk-judge-v28'),
                                         ('pumpswap-facts-v3', 'desk-judge-v28')])
def test_crossed_input_and_prompt_versions_refuse(version, prompt):
    policy = {**enabled(), 'ps_inputs': version, 'judge_playbook_sha256': DIGEST}
    with pytest.raises(ValueError, match='ps_inputs'):
        K.validate_ps_inputs(policy, prompt)


@pytest.mark.parametrize('digest', [None, '', 'f'*63, 'g'*64, 'A'*64, 'f'*64+'\n', False])
def test_v28_requires_a_valid_frozen_prompt_digest(digest):
    policy = {**enabled(), 'ps_inputs': 'pumpswap-facts-v2', 'judge_playbook_sha256': digest}
    with pytest.raises(ValueError, match='ps_inputs'):
        K.validate_ps_inputs(policy, 'desk-judge-v28')


def test_v28_cannot_use_a_legacy_policy_without_ps_inputs():
    policy = copy.deepcopy(enabled())
    policy.pop('ps_inputs')
    policy['hard_filter'].pop('intake_source_version')
    with pytest.raises(ValueError, match='ps_inputs'):
        K.validate_ps_inputs(policy, 'desk-judge-v28')


@pytest.mark.parametrize('changes', [dict(ps_inputs=False), dict(ps_inputs_version='pumpswap-facts-v3'),
                                  dict(judge_playbook_sha256=None), dict(judge_playbook_sha256='bad')])
def test_policy_builder_refuses_unbound_v2(changes):
    with pytest.raises(ValueError, match='ps_inputs|playbook'):
        v28_policy(**changes)


@pytest.mark.parametrize("collected", [False, True])
def test_v28_preview_hashes_sent_system_text_and_writes_neither_store(tmp_path, monkeypatch, capsys, collected):
    monkeypatch.setenv('SNIPER_STATE_ROOT', str(tmp_path))
    monkeypatch.setenv('MEMECOIN_DESK_RAIL_BOUNDS', 'tp_pct:15-150,sl_pct:10-40,trail_pct:8-30')
    monkeypatch.delenv('MEMECOIN_DESK_JUDGE_MAX_OUTPUT_TOKENS', raising=False)
    playbook = tmp_path/'v28.md'
    header = '# Human header\nprompt_version: `desk-judge-v28`\n\n---\n'
    args = ['--judge-p-admit','1','--round-trip-loss-pct-max','8','--intake','--venue','pumpswap',
            '--ps-inputs','--intake-feeds',*FEEDS,'--playbook',str(playbook),'--intake-prereg',
            PREREG['path']+'@'+PREREG['commit'],'--desk-db',str(tmp_path/'desk.db'),
            '--ledger-db',str(tmp_path/'ledger.db')]

    if collected:
        args.append('--intake-require-collected')

    def preview(text):
        playbook.write_text(text)
        assert P.main(args) == 0
        raw = capsys.readouterr().out
        body = json.loads(raw.split('\nMEMECOIN_DESK_U_COHORT_ID=',1)[0])
        cid = raw.split('MEMECOIN_DESK_U_COHORT_ID=',1)[1].splitlines()[0]
        assert not (tmp_path/'desk.db').exists() and not (tmp_path/'ledger.db').exists()
        return body, cid

    body, cid = preview(header+SYSTEM)
    assert body['prompt_version'] == 'desk-judge-v28'
    assert body['policy']['ps_inputs'] == 'pumpswap-facts-v2'
    assert body['policy']['judge_playbook_sha256'] == DIGEST
    assert body['policy']['preregistration'] == PREREG
    assert body['policy']['hard_filter']['intake'].get('require_collected') == (
        ['chart', 'cluster', 'dump_group', 'holders'] if collected else None)
    assert preview('# Another human note\n'+header+SYSTEM)[1] == cid
    changed, changed_cid = preview(header+SYSTEM+'Changed execution text.\n')
    assert changed_cid != cid
    assert changed['policy']['judge_playbook_sha256'] != DIGEST
