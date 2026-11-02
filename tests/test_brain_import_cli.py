import json
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location('brain_import_cli', ROOT/'scripts/brain_import.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_snapshot_accepts_raw_rows_and_report_is_private(tmp_path):
    cli = load()
    p = tmp_path/'rows.json'
    p.write_text('[{"name":"test","description":"x"}]')
    assert cli.read_snapshot(p)[0]['name'] == 'test'
    report = tmp_path/'report.json'
    report.write_text('old')
    report.chmod(0o644)
    cli.write_report(report, {'facts_written': 3})
    assert report.stat().st_mode & 0o777 == 0o600
    assert json.loads(report.read_text())['facts_written'] == 3


def test_seed_methods_is_idempotent_and_updates_role_views():
    from brain_graph_fake_store import FakeStore
    cli = load()
    store = FakeStore()
    cli.seed_import(store)
    cli.seed_import(store)
    assert [s['slug'] for s in store.sections()].count('crew/methods') == 1
    assert all('crew/methods' in store.view('role/'+r)['query_spec']['sections']
               for r in ('alpha','beta','gamma','lambda','codex'))


def test_live_requires_snapshot_and_report(tmp_path):
    import pytest
    cli = load()
    with pytest.raises(SystemExit):
        cli.main(['--live', '--snapshot', str(tmp_path/'input.json')])


def test_seed_real_view_shape_uses_name_primary_key():
    from brain_graph_fake_store import FakeStore
    from scripts.brain_seed import seed
    cli = load()
    store = FakeStore()
    seed(store)
    for row in store.rows['brain_views']:
        row.pop('id', None)
    cli.seed_import(store)
    assert 'crew/methods' in store.view('role/alpha')['query_spec']['sections']


def test_failure_report_has_stage_and_redacts_error_detail(tmp_path, monkeypatch):
    cli = load()
    fake_secret = 'test-secret-abcdefgh-12345678'
    monkeypatch.setenv('FAKE_API_TOKEN', fake_secret)
    from bot.brain_graph import live
    monkeypatch.setattr(live, 'state_root', lambda repo: tmp_path)
    def fail(*args):
        raise ValueError('source failed: ' + fake_secret)
    monkeypatch.setattr(cli, 'read_sources', fail)
    snapshot = tmp_path/'rows.json'
    snapshot.write_text('[]')
    report = tmp_path/'report.json'
    assert cli.main(['--dry-run','--snapshot',str(snapshot),'--report',str(report)]) == 4
    data = json.loads(report.read_text())
    assert data['error_stage'] == 'load_sources' and data['error_type'] == 'ValueError'
    assert fake_secret not in report.read_text() and '[REDACTED]' in data['error_detail']


def test_unresolved_numeric_count_is_not_broken_commit_evidence(monkeypatch):
    cli = load()
    from bot.brain_graph import import_entities as entities, import_manifest as manifest, evidence
    monkeypatch.setattr(entities, 'resolve_commits', lambda tokens, repo: {})
    monkeypatch.setattr(evidence, 'check', lambda item, *a, **kw:
                        ('broken', 'missing') if item['type'] == 'sha' else ('ok', 'exists'))
    sources = manifest.Sources(supabase_rows=[
        dict(name='finding/count', description='The sample contained 1000000 observations.', content=''),
        dict(name='finding/commit', description='Commit deadbee no longer exists.', content='')])
    m = cli.build_manifest(ROOT, sources, set(), fetched=True)
    by = {f['draft']['statement']: f for f in m.facts}
    assert by['The sample contained 1000000 observations.']['import_status'] is None
    assert by['Commit deadbee no longer exists.']['import_status'] == 'evidence_broken'


def test_generated_broken_doctrine_refs_are_preserved_as_broken_facts(monkeypatch):
    cli = load()
    from bot.brain_graph import import_entities as entities, import_manifest as manifest, evidence
    monkeypatch.setattr(entities, 'resolve_commits', lambda tokens, repo: {})
    monkeypatch.setattr(evidence, 'check', lambda item, *a, **kw:
        ('broken', 'missing') if item['ref'] == 'positions.network_id' else ('ok', 'exists'))
    sources = manifest.Sources(doctrine={'AGENTS.md':
        ('- Never substitute positions.network_id for a verified chain identifier.', '2026-09-01')})
    m = cli.build_manifest(ROOT, sources, set(), fetched=True)
    assert m.facts[0]['import_status'] == 'evidence_broken'
    assert any(e['ref'] == 'positions.network_id' for e in m.facts[0]['draft']['evidence'])
