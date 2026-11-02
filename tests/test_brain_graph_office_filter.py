"""Office namespace is untrusted even when linked to a crew entity."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from brain_graph_fake_store import FakeStore
from bot.brain_graph import search, views
from bot.brain_graph.store import BrainUnavailable

REPO = Path(__file__).resolve().parents[1]


def seeded():
    s = FakeStore()
    crew = s.insert('brain_sections', {'slug': 'crew', 'title': 'Crew'})
    office = s.insert('brain_sections', {'slug': 'office:head', 'title': 'Office'})
    me = s.insert('brain_entities', {'type': 'actor', 'name': 'alpha', 'section_id': crew['id']})
    lab = s.insert('brain_entities', {'type': 'actor', 'name': 'office:head', 'section_id': office['id']})
    s.insert('brain_views', {'name': 'role/alpha', 'query_spec': {
        'sections': ['crew', 'office:head'], 'role_text': 'crew role', 'kinds': None}})
    clean = s.insert('brain_facts', {'kind': 'trap', 'statement': 'clean retrieval witness',
        'entity_ids': [me['id']], 'author': 'beta', 'evidence': [{'type': 'path', 'ref': 'CLAUDE.md'}]})
    dirty = s.insert('brain_facts', {'kind': 'decision', 'statement': 'OFFICE_WEB_SENTINEL retrieval witness',
        'entity_ids': [me['id'], lab['id']], 'author': 'beta', 'status': 'disputed', 'evidence': [{'type': 'path', 'ref': 'CLAUDE.md'}]})
    return s, clean, dirty


@pytest.mark.parametrize('mode', ['boot', 'role-free', 'ask', 'expansion'])
def test_office_rows_never_reach_crew_even_through_a_shared_entity(mode, monkeypatch):
    s, clean, dirty = seeded()
    monkeypatch.setattr(views.live, 'derive', lambda *a, **k: {'errors': []})
    if mode == 'boot':
        out = views.boot(s, 'alpha', REPO, now=datetime(2026, 9, 28, 12, 1, tzinfo=timezone.utc))
    elif mode == 'role-free':
        out = views.boot_role_free(s, REPO)
    else:
        if mode == 'expansion':
            s.search_facts = lambda *a: [clean]
        out = json.dumps(search.ask(s, 'retrieval witness', embed=False))
    assert 'OFFICE_WEB_SENTINEL' not in out
    if mode != 'role-free':
        assert clean['statement'] in out


def test_office_episodes_are_not_crew_handoffs(monkeypatch):
    s, _, _ = seeded()
    s.insert('brain_episodes', {'kind': 'handoff_line', 'author': 'beta',
        'body': 'OFFICE_EPISODE_SENTINEL', 'source_ref': 'office:head:turn:1'})
    s.insert('brain_episodes', {'kind': 'handoff_line', 'author': 'beta', 'body': 'clean episode'})
    monkeypatch.setattr(views.live, 'derive', lambda *a, **k: {'errors': []})
    out = views.boot(s, 'alpha', REPO, now=datetime(2026, 9, 28, 12, 1, tzinfo=timezone.utc))
    assert 'OFFICE_EPISODE_SENTINEL' not in out
    assert 'clean episode' in out


def test_unfiltered_legacy_snapshot_is_refused(tmp_path):
    snap = tmp_path / 'snapshot.json'
    snap.write_text(json.dumps({'role': 'alpha', 'at': 'old', 'text': 'OFFICE_CACHED_SENTINEL'}))
    out = views._from_snapshot(snap, 'alpha', 'down', 6000)
    assert 'OFFICE_CACHED_SENTINEL' not in out
    assert 'unfiltered' in out.lower()


def test_namespace_lookup_failure_cannot_fall_back_to_unfiltered_rows(monkeypatch):
    s, _, _ = seeded()
    s.rows_page = lambda *a, **k: (_ for _ in ()).throw(BrainUnavailable('lookup failed'))
    with pytest.raises(BrainUnavailable):
        search.ask(s, 'retrieval', embed=False)


@pytest.mark.parametrize('slug', ['office', 'office:head', 'office/v2/judge'])
def test_namespace_does_not_depend_on_web_taint_field(slug):
    s, _, dirty = seeded()
    s.rows['brain_sections'][1]['slug'] = slug
    assert dirty['id'] not in {f['id'] for f in search.ask(s, 'retrieval', embed=False)}


def test_short_server_pages_are_followed_until_empty():
    from bot.brain_graph.crew_filter import CrewFilter
    s, _, dirty = seeded()
    read = s.rows_page
    s.rows_page = lambda *a, **k: read(*a, **dict(k, limit=1))
    assert CrewFilter(s).facts([dirty]) == []


def test_rest_episode_query_keeps_namespace_provenance():
    from bot.brain_graph.store import RestStore
    calls = []
    def request(method, path, body, headers, timeout):
        calls.append(path)
        return []
    RestStore(request).episodes_since(['handoff_line'], '2026-10-30T00:00:00Z', 40)
    assert 'recorded_at,source_ref' in calls[0]


def test_office_entity_created_after_filter_construction_is_still_excluded():
    from bot.brain_graph.crew_filter import CrewFilter
    s, _, dirty = seeded()
    guard = CrewFilter(s)
    section = s.insert('brain_sections', {'slug': 'office:new-seat', 'title': 'New seat'})
    entity = s.insert('brain_entities', {'type': 'actor', 'name': 'office:new-seat', 'section_id': section['id']})
    dirty = dict(dirty, entity_ids=[entity['id']])
    assert guard.facts([dirty]) == []
