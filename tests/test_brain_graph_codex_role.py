"""Codex uses the same named crew paths and refusal gates, with no external writes."""
import copy

import pytest

from bot.brain_graph import api
from bot.brain_graph.store import BrainRefused
from test_brain_graph_api import REPO, draft, store
from test_brain_graph_cli import _cli


def test_codex_cli_task_episode_handover_peer_verification_and_own_retraction(tmp_path, monkeypatch, capsys):
    cli = _cli()
    monkeypatch.setattr(cli, 'STATE', tmp_path)
    monkeypatch.setattr(cli.views.live, 'derive', lambda *_a, **_k: {'errors': []})
    s = store()
    assert cli.main(['write', '--role', 'codex', '--kind', 'task', '--entity', 'actor:codex',
                     '--section', 'crew', '--evidence', 'path:CLAUDE.md',
                     'Codex OPEN: verify the merged desk flow.'], store=s, outbox=tmp_path / 'outbox') == 0
    fact = s.rows['brain_facts'][-1]
    assert fact['author'] == 'codex' and fact['status'] == 'unverified'
    assert cli.main(['episode', '--role', 'codex', '--kind', 'crew_report',
                     'Offline focused checks passed.'], store=s) == 0
    episode = s.rows['brain_episodes'][-1]
    assert episode['author'] == 'codex' and episode['occurred_at'] is None
    assert cli.main(['handover', 'codex'], store=s) == 0
    assert s.rows['brain_episodes'][-1]['kind'] == 'handover'
    assert s.rows['brain_episodes'][-1]['author'] == 'codex'
    assert 'Codex OPEN: verify the merged desk flow.' in capsys.readouterr().out

    before = copy.deepcopy(fact)
    assert cli.main(['verify', fact['id'], 'verified', '--role', 'codex'], store=s) == 2
    assert s.get('brain_facts', fact['id']) == before
    output = tmp_path / 'proof.txt'
    output.write_text('Focused offline checks passed.')
    assert cli.main(['verify', fact['id'], 'verified', '--role', 'beta',
                     '--output-file', str(output)], store=s) == 0
    reviewed = s.get('brain_facts', fact['id'])
    assert reviewed['verified_by'] == 'beta' and reviewed['status'] == 'verified'
    assert cli.main(['retract', fact['id'], '--role', 'codex', '--reason-kind', 'misfiled', '--reason', '  '], store=s) == 2
    assert s.get('brain_facts', fact['id']) == reviewed
    assert cli.main(['retract', fact['id'], '--role', 'codex', '--reason-kind', 'misfiled', '--reason',
                     'Mistaken task entry; duplicate scope.'], store=s) == 0
    closed = s.get('brain_facts', fact['id'])
    assert closed['status'] == 'retracted' and closed['verified_by'] == 'beta'
    assert 'retracted by codex [misfiled]:' in closed['verify_note']


def test_codex_peer_verifies_with_output_and_never_its_own_fact():
    s = store()
    fact = api.write_fact(s, draft('Focused checks cover the graph write path.'), repo=REPO)['fact']
    reviewed = api.verify_fact(s, fact['id'], 'verified', role='codex', output='Offline checks passed.')
    assert reviewed['status'] == 'verified' and reviewed['verified_by'] == 'codex'
    own = api.write_fact(s, draft('Record the offline graph checks.', kind='task', author='codex',
                                  entity=('actor', 'codex')), repo=REPO)['fact']
    with pytest.raises(BrainRefused, match='author'):
        api.verify_fact(s, own['id'], 'verified', role='codex', output='Offline checks passed.')
    assert s.get('brain_facts', own['id']) == own


@pytest.mark.parametrize('bad', [
    {'evidence': []},
    {'evidence': [{'type': 'path', 'ref': 'no/such/codex-proof.py'}]},
    {'statement': 'The configured credential is named-secret-for-test.'},
])
def test_codex_writes_still_require_evidence_and_refuse_secrets(bad):
    s = store()
    proposed = draft('Record focused offline graph checks.', author='codex')
    proposed.update(bad)
    result = api.write_fact(s, proposed, repo=REPO, secret_values={'named-secret-for-test'})
    assert result['status'] == 'refused'
    assert s.rows['brain_facts'] == s.rows['brain_entities'] == []
    assert 'named-secret-for-test' not in str(result)


@pytest.mark.parametrize('author', ['unknown-role', '', None])
def test_episode_requires_a_named_author_before_any_store_write(author):
    class NoWrites:
        def insert(self, *_a, **_k):
            pytest.fail('invalid author reached the store')
    with pytest.raises(BrainRefused, match='author'):
        api.add_episode(NoWrites(), 'crew_report', 'Offline checks passed.', author=author)


def test_codex_episode_verification_and_retraction_keep_secret_and_reason_gates():
    s = store()
    with pytest.raises(BrainRefused, match='secret'):
        api.add_episode(s, 'crew_report', 'private-value-for-test', author='codex',
                        secret_values={'private-value-for-test'})
    assert s.rows['brain_episodes'] == []
    fact = api.write_fact(s, draft('Record focused offline graph checks.'), repo=REPO)['fact']
    with pytest.raises(BrainRefused, match='reason'):
        api.verify_fact(s, fact['id'], 'refuted', role='codex', output='')
    with pytest.raises(BrainRefused, match='secret'):
        api.verify_fact(s, fact['id'], 'verified', role='codex', output='private-value-for-test',
                        secret_values={'private-value-for-test'})
    with pytest.raises(BrainRefused, match='secret'):
        api.retract_fact(s, fact['id'], role='codex', reason='private-value-for-test', reason_kind='misfiled',
                         secret_values={'private-value-for-test'})
    assert s.get('brain_facts', fact['id']) == fact
