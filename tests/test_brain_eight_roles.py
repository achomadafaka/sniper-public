"""Eight fixed crew identities retain independent evidence and provenance gates."""
import copy
from datetime import datetime, timezone

import pytest

from bot.brain_graph import api, views
from bot.brain_graph.store import BrainRefused
from brain_graph_fake_store import FakeStore
from scripts import brain_ambient, brain_import, brain_seed
from test_brain_graph_api import REPO, draft, store
from test_brain_graph_cli import _cli

ROLES = ('alpha', 'beta', 'lambda', 'gamma', 'codex', 'delta', 'epsilon', 'zeta')


@pytest.mark.parametrize('role', ('delta', 'epsilon', 'zeta'))
def test_new_role_task_episode_handover_peer_review_and_close(role, tmp_path, monkeypatch, capsys):
    cli = _cli()
    monkeypatch.setattr(cli, 'STATE', tmp_path)
    monkeypatch.setattr(views.live, 'derive', lambda *_a, **_k: {'errors': []})
    monkeypatch.delenv('CLAUDE_SESSION_ID', raising=False)
    monkeypatch.setenv('CODEX_THREAD_ID', 'gpt-thread-identity')
    s = FakeStore()
    brain_seed.seed(s)
    s.clock = datetime.now(timezone.utc)
    assert cli.main(['write', '--role', role, '--kind', 'task', '--entity', 'actor:'+role,
                     '--section', 'crew/'+role, '--evidence', 'path:CLAUDE.md',
                     role+' OPEN: verify the isolated crew role.'], store=s) == 0
    own = s.rows['brain_facts'][-1]
    assert own['author'] == role and own['author_session'] == 'gpt-thread-identity'
    assert cli.main(['episode', '--role', role, '--kind', 'handoff_line', 'Focused role checks.'], store=s) == 0
    assert s.rows['brain_episodes'][-1]['session_id'] == 'gpt-thread-identity'
    assert cli.main(['boot', role], store=s, snapshot=tmp_path/'role-snapshot.json') == 0
    assert role+' OPEN' in capsys.readouterr().out
    assert cli.main(['verify', own['id'], 'verified', '--role', role], store=s) == 2
    assert s.get('brain_facts', own['id']) == own
    proof = tmp_path/'proof.txt'
    proof.write_text('Isolated role checks passed.')
    assert cli.main(['verify', own['id'], 'verified', '--role', 'codex', '--output-file', str(proof)], store=s) == 0
    peer = api.write_fact(s, draft('Independent baseline evidence.', entity=('tool', 'baseline')), repo=REPO)['fact']
    assert api.verify_fact(s, peer['id'], 'verified', role=role, output='Peer review passed.')['verified_by'] == role
    assert cli.main(['handover', role], store=s) == 0
    assert s.rows['brain_episodes'][-1]['session_id'] == 'gpt-thread-identity'
    assert cli.main(['retract', own['id'], '--role', role, '--reason', 'Done: focused role checks passed.'], store=s) == 0
    assert s.get('brain_facts', own['id'])['status'] == 'retracted'


@pytest.mark.parametrize('role', ('delta', 'epsilon', 'zeta'))
def test_new_roles_preserve_evidence_secret_and_reason_guards(role):
    s = store()
    for patch in ({'evidence': []}, {'evidence': [{'type': 'path', 'ref': 'no/such/proof'}]},
                  {'statement': 'private-role-test-secret'}):
        d = draft('Named role requires checked evidence.', author=role)
        d.update(patch)
        assert api.write_fact(s, d, repo=REPO, secret_values={'private-role-test-secret'})['status'] == 'refused'
    assert s.rows['brain_facts'] == []
    with pytest.raises(BrainRefused, match='secret'):
        api.add_episode(s, 'handoff_line', 'private-role-test-secret', author=role,
                        secret_values={'private-role-test-secret'})


def test_seed_all_roles_methods_and_targeted_legacy_text_repair():
    s = FakeStore()
    brain_seed.seed(s)
    old_codex = 'Codex: an outside session in its own worktree. It reads the brain and never writes it until the operator says otherwise.'
    spec = copy.deepcopy(s.view('role/codex')['query_spec'])
    spec.update(role_text=old_codex, custom='preserve this')
    spec['sections'].append('custom/section')
    s.patch_view('role/codex', {'query_spec': spec})
    alpha = copy.deepcopy(s.view('role/alpha'))
    before = copy.deepcopy(s.rows)
    assert brain_seed.seed(brain_seed.DryRun(s))['views_updated'] == 1
    assert s.rows == before
    assert brain_seed.seed(s)['views_updated'] == 1
    repaired = s.view('role/codex')['query_spec']
    assert repaired['role_text'] != old_codex and 'shared checkout' in repaired['role_text']
    assert repaired['custom'] == 'preserve this' and 'custom/section' in repaired['sections']
    assert s.view('role/alpha') == alpha
    before = copy.deepcopy(s.rows)
    brain_seed.seed(brain_seed.DryRun(s))
    assert s.rows == before
    brain_import.seed_import(s)
    for role in ROLES:
        assert s.find_entity('actor', role)
        view = s.view('role/'+role)['query_spec']
        assert 'crew/'+role in view['sections'] and 'crew/methods' in view['sections']
    before = copy.deepcopy(s.rows)
    brain_import.seed_import(s)
    assert s.rows == before


@pytest.mark.parametrize('role', ROLES)
def test_all_roles_hook_detection_and_current_restart_authority(role):
    match = brain_ambient.ROLE.search('You are '+role.title())
    assert match and match.group(1).lower() == role
    text = views.onboarding(role)
    for name in ('Alpha', 'Beta', 'Lambda', 'Gamma', 'Codex', 'Delta', 'Epsilon', 'Zeta'):
        assert name in text
    assert 'delegated operator authority' in text
    assert 'historical inbox' in text and 'explicit yes' in text and '--confirm' in text


def test_claude_session_provenance_takes_precedence(monkeypatch):
    cli = _cli()
    monkeypatch.setenv('CLAUDE_SESSION_ID', 'claude-session')
    monkeypatch.setenv('CODEX_THREAD_ID', 'gpt-session')
    ns = cli.build_parser().parse_args(['write', '--role', 'alpha', '--kind', 'task', 'Record role provenance.'])
    assert cli.draft_from_args(ns)['author_session'] == 'claude-session'
