# tests/test_brain_graph_import_store.py
from pathlib import Path

import pytest

from brain_graph_fake_store import FakeStore
from bot.brain_graph import api
from bot.brain_graph.store import BrainRefused, FACT_COLUMNS, RestStore

REPO = Path(__file__).resolve().parents[1]


def _store():
    s = FakeStore()
    s.insert("brain_sections", {"slug": "crew", "title": "crew"})
    return s


def _draft(statement, **kw):
    d = {"kind": "rule", "statement": statement, "author": "import", "section": "crew",
         "author_session": "import:v1",
         "evidence": [{"type": "path", "ref": "CLAUDE.md"}],
         "entities": [{"type": "tool", "name": "git"}]}
    d.update(kw)
    return d


def test_rows_page_is_keyset_and_a_short_page_ends_it():
    s = _store()
    for i in range(7):
        api.add_episode(s, "doc", "body %d" % i, author="import", source_ref="SKILL.md#%012d" % i)
    seen, after = [], None
    while True:
        page = s.rows_page("brain_episodes", select="id,source_ref", after_id=after, limit=3)
        seen += page
        if len(page) < 3:
            break
        after = page[-1]["id"]
    assert len({r["id"] for r in seen}) == 7


def test_episode_index_counts_parts_under_one_key():
    s = _store()
    api.add_episode(s, "legacy_memory", "x" * 4000, author="import", source_ref="memory/a.md#abc")
    idx = s.episode_index("import", "memory/")
    assert idx["memory/a.md#abc"]["parts"] == 3 and idx["memory/a.md#abc"]["parent"]
    assert s.episode_refs("import", "memory/") == {"memory/a.md#abc"}
    assert s.episode_refs("import", "HANDOFF") == set()


def test_fact_index_sees_every_status():
    s = _store()
    live = api.write_fact(s, _draft("Never `git stash` in the shared checkout."), repo=REPO,
                          interactive=False)["fact"]
    closed = api.write_fact(s, _draft("Use   `git stash` to park the fix.", valid_from="2026-07-01T00:00:00+00:00"),
                            repo=REPO, interactive=False, import_status="retracted",
                            valid_to="2026-07-02T00:00:00+00:00")["fact"]
    idx = s.fact_index("import:v1")
    assert idx["Never `git stash` in the shared checkout."]["id"] == live["id"]
    assert idx["Use `git stash` to park the fix."]["status"] == "retracted"
    assert closed["valid_to"] == "2026-07-02T00:00:00+00:00" and closed["expired_at"]


def test_rest_fact_pages_keep_history_paging_and_full_metadata():
    from urllib.parse import parse_qs
    facts = [{"id": "a", "statement": "One historical fact.", "status": "retracted", "version": 2},
             {"id": "b", "statement": "Another historical fact.", "status": "superseded", "version": 4},
             {"id": "c", "statement": "The current replacement.", "status": "verified", "version": 1}]
    calls = []

    def request(method, path, body, headers, timeout):
        query = parse_qs(path.split('?', 1)[1])
        calls.append(query)
        assert set(query.get('select', ['*'])[0].split(',')) == set(FACT_COLUMNS.split(','))
        assert query['order'] == ['id.asc']
        assert query['author_session'] == ['eq.import:v1']
        assert 'status' not in query and 'valid_to' not in query
        after = query.get('id', ['gt.'])[0][3:]
        return [f for f in facts if f['id'] > after][:int(query['limit'][0])]

    result = RestStore(request=request)._all('brain_facts', limit=2,
        filters=(("author_session", "eq", "import:v1"),))
    assert result == facts
    assert len(calls) == 2 and calls[1]['id'] == ['gt.b']


def test_rest_entity_fact_rpc_continues_server_short_pages_and_keeps_oldest_order():
    from urllib.parse import parse_qs
    facts = [{"id": "a", "recorded_at": "2026-10-02", "status": "retracted"},
             {"id": "b", "recorded_at": "2026-10-01", "status": "verified"}]
    calls = []

    def request(method, path, body, headers, timeout):
        query = parse_qs(path.split('?', 1)[1])
        calls.append(query)
        assert method == 'POST'
        assert body == dict(p_entity_ids=['e'], p_kinds=['rule'], p_live_only=False)
        assert set(query.get('select', ['*'])[0].split(',')) == set(FACT_COLUMNS.split(','))
        after = query.get('id', ['gt.'])[0][3:]
        return [f for f in facts if f['id'] > after][:1]

    result = RestStore(request=request).find_facts(['e'], kinds=['rule'], live_only=False)
    assert result == [facts[1], facts[0]]
    assert len(calls) == 3 and calls[1]['id'] == ['gt.a'] and calls[2]['id'] == ['gt.b']


def test_a_broken_item_imports_as_evidence_broken_instead_of_refusing():
    s = _store()
    d = _draft("A script that no longer exists did the thing.",
               evidence=[{"type": "path", "ref": "scripts/does_not_exist_anywhere.py"}])
    assert api.write_fact(s, d, repo=REPO, interactive=False)["status"] == "refused"
    r = api.write_fact(s, d, repo=REPO, interactive=False, import_status="evidence_broken")
    assert r["status"] == "written" and r["fact"]["status"] == "evidence_broken"


def test_only_the_importer_may_set_a_status():
    s = _store()
    r = api.write_fact(s, _draft("Crew facts start unverified, always.", author="beta", author_session="s1"),
                       repo=REPO, interactive=False, import_status="retracted")
    assert r["status"] == "refused"
    trap = _draft("A crew-authored trap citing a gone script.", author="lambda",
                  evidence=[{"type": "path", "ref": "scripts/does_not_exist_anywhere.py"}])
    r = api.write_fact(s, trap, repo=REPO, interactive=False, import_status="evidence_broken")
    assert r["status"] == "written" and r["fact"]["author"] == "lambda"          # B2
    with pytest.raises(ValueError):
        api.write_fact(s, _draft("A status the importer may not set.", ), repo=REPO,
                       interactive=False, import_status="verified")


def test_insert_many_returns_rows_in_order():
    s = _store()
    rows = s.insert_many("brain_episodes", [
        {"kind": "doc", "body": "a", "author": "import", "source_ref": "SKILL.md#a"},
        {"kind": "doc", "body": "b", "author": "import", "source_ref": "SKILL.md#b"}])
    assert [r["body"] for r in rows] == ["a", "b"] and all(r["id"] for r in rows)


def test_closed_same_episode_refinement_stays_pending():
    s = _store()
    ep = api.add_episode(s, 'doc', 'source text', author='import')[0]['id']
    old = api.write_fact(s, _draft('Prior instruction from this document.', episode_ids=[ep],
                        valid_from='2026-08-01T00:00:00+00:00'), repo=REPO, interactive=False,
                        import_status='retracted', valid_to='2026-08-02T00:00:00+00:00')['fact']
    new = api.write_fact(s, _draft('Different instruction from this document.', episode_ids=[ep]),
                        repo=REPO, interactive=False, judgements={old['id']: 'refines'})
    assert new['status'] == 'written' and new['fact']['judge_pending']
    assert any(l['rel'] == 'candidate' and l['to_id'] == old['id'] for l in s.rows['brain_links'])


def test_equal_closed_timestamps_in_different_offsets_match_postgres():
    s = _store()
    result = api.write_fact(s, _draft('Historical statement with an exact terminal timestamp.',
        valid_from='2026-08-02T00:00:00+00:00'), repo=REPO, interactive=False,
        import_status='retracted', valid_to='2026-08-01T20:00:00-04:00')
    assert result['status'] == 'written'
