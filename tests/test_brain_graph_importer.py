# tests/test_brain_graph_importer.py
from pathlib import Path

from brain_graph_fake_store import FakeStore
from bot.brain_graph import import_manifest as MF, importer
from test_brain_graph_import_manifest import _src

REPO = Path(__file__).resolve().parents[1]
SLUGS = ("crew", "crew/methods", "brain", "brain/doctrine", "research/studies", "memecoin",
         "infra/runtime", "wallet-analyzer", "memecoin/4663")


def _store():
    s = FakeStore()
    for slug in SLUGS:
        s.insert("brain_sections", {"slug": slug, "title": slug})
    return s


def _manifest():
    return MF.build(_src(), commits={}, tables=set(), handles=set(), knob_ok=lambda n: False,
                    broken_refs_fn=lambda t: [], secret_values=set())


def _run(s):
    return importer.run(s, _manifest(), repo=REPO, secret_values=set(),
                        now_fn=lambda: "2026-09-29T00:00:00+00:00", sleep_fn=lambda _s: None)


def test_a_second_run_writes_nothing():
    s = _store()
    first, second = _run(s), _run(s)
    assert first["facts_written"] > 0 and first["episodes_written"] > 0
    assert second["facts_written"] == 0 and second["episodes_written"] == 0 and second["links_written"] == 0


def test_imports_are_unverified_and_never_self_verified():
    s = _store()
    _run(s)
    facts = s.rows["brain_facts"]
    assert all(f["verified_by"] is None for f in facts)
    assert {f["status"] for f in facts} <= {"unverified", "retracted", "evidence_broken", "superseded"}
    assert all(f["author"] in ("import", "operator", "alpha", "beta", "gamma", "lambda") for f in facts)
    assert all(f["author_session"] == "import:v1" for f in facts)


def test_a_retired_row_is_closed_on_insert_and_never_revived():
    s = _store()
    _run(s)
    closed = [f for f in s.rows["brain_facts"] if f["status"] == "superseded"]
    assert closed and all(f["valid_to"] and f["expired_at"] for f in closed)
    _run(s)
    assert len([f for f in s.rows["brain_facts"] if f["statement"] == closed[0]["statement"]]) == 1


def test_same_source_bullets_refine_their_headline_without_a_judge():
    s = _store()
    report = _run(s)
    assert any(l["rel"] == "derived_from" for l in s.rows["brain_links"])
    bullet = next(f for f in s.rows["brain_facts"] if f["statement"] == "Paste counts from tool output.")
    assert not bullet["judge_pending"]
    assert report["memory_facts"]["feedback-x"]


def test_an_interrupted_episode_is_completed_not_duplicated():
    s = _store()
    m = _manifest()
    long = next(e for e in m.episodes if e["kind"] == "handoff_line")
    long["body"] = "x" * 4000
    first = importer.run(s, m, repo=REPO, secret_values=set(), now_fn=lambda: "2026-09-29T00:00:00+00:00",
                         sleep_fn=lambda _s: None)
    parts = [e for e in s.rows["brain_episodes"] if e["source_ref"] == long["key"]]
    s.rows["brain_episodes"].remove(max(parts, key=lambda e: e["part_no"]))     # a crash before part 3
    again = importer.run(s, m, repo=REPO, secret_values=set(), now_fn=lambda: "2026-09-29T00:00:00+00:00",
                         sleep_fn=lambda _s: None)
    assert again["parts_completed"] == 1 and again["episodes_written"] == 0
    assert len([e for e in s.rows["brain_episodes"] if e["source_ref"] == long["key"]]) == 3


def test_the_cache_makes_the_run_linear():
    s = _store()
    calls = {"find_facts": 0}
    real = s.find_facts

    def counted(*a, **k):
        calls["find_facts"] += 1
        return real(*a, **k)
    s.find_facts = counted
    _run(s)
    assert calls["find_facts"] == 0, "candidates come from CachedStore, not one server read per fact"


def test_the_queue_is_batched_per_section_and_staggered():
    items = [{"section": "memecoin", "episode_key": "HANDOFF.md#%012d" % i, "excerpt": "item %d" % i}
             for i in range(60)]
    drafts = importer.queue_tasks(items, import_day="2026-09-29")
    assert [d["kind"] for d in drafts] == ["task"] * 3
    assert [d["verify_by"][:10] for d in drafts] == ["2026-09-30", "2026-10-01", "2026-10-02"]
    assert drafts[0]["statement"].startswith("Judge import batch memecoin #1")
    assert drafts[0]["detail"].count("\n") == 24 and len(drafts[-1]["detail"].splitlines()) == 10
    worst = [{"section": "memecoin", "episode_key": "x" * 36, "excerpt": "y" * 500} for _ in range(25)]
    assert len(importer.queue_tasks(worst, import_day="2026-09-29")[0]["detail"]) <= 4096


def test_changed_redaction_on_merged_provenance_refuses_new_fact():
    s = _store()
    src = MF.Sources(supabase_rows=[dict(name='finding/'+name,
        description='The same durable claim appears here.', content='body '+name,
        type='finding', status='active') for name in ('one', 'two')])
    m = MF.build(src, commits={}, tables=set(), handles=set(), knob_ok=lambda n: False,
                 broken_refs_fn=lambda t: [], secret_values=set())
    importer.write_episodes(s, [m.episodes[1]], {})
    s.rows['brain_episodes'][0]['body'] = 'older redaction policy body'
    report = importer.run(s, m, repo=REPO, secret_values=set(), sleep_fn=lambda _: None)
    assert report['redaction_changed'] == 1
    assert not s.rows['brain_facts']
    assert report['facts_refused']['episode redaction changed'] == 1


def test_cached_store_reads_sections_once():
    s = _store()
    original = s.sections
    calls = []
    def sections(slugs=None):
        calls.append(slugs)
        return original(slugs)
    s.sections = sections
    cached = importer.CachedStore(s)
    assert cached.sections(['crew'])[0]['slug'] == 'crew'
    assert cached.sections(['brain'])[0]['slug'] == 'brain'
    assert len(cached.sections()) == len(SLUGS)
    assert calls == [None]


def test_bullets_refine_only_headline_not_siblings():
    s = _store()
    text = '---\nname: feedback-many\ndescription: Never publish a claim without its evidence.\ntype: feedback\n---\n**How to apply:**\n- Record the command output.\n- Keep every source timestamp.\n- Read the exact repository revision.\n'
    m = MF.build(MF.Sources(memory_files={'feedback-many.md': text}), commits={}, tables=set(),
        handles=set(), knob_ok=lambda n: False, broken_refs_fn=lambda t: [], secret_values=set())
    importer.run(s, m, repo=REPO, secret_values=set())
    head = next(f['id'] for f in s.rows['brain_facts'] if f['statement'].startswith('Never publish'))
    derived = [l for l in s.rows['brain_links'] if l['rel'] == 'derived_from']
    assert len(derived) == 3 and {l['to_id'] for l in derived} == {head}
