# tests/test_brain_graph_import_manifest.py
from bot.brain_graph import import_manifest as MF

SKILL = """STANDING RULES (they override anything you would do by default):
- ONE SHARED CHECKOUT with three other sessions. Never `git add -A`.

NAMING TRAPS -- do not rediscover these:
- A SLICED IDENTIFIER IS A DIFFERENT IDENTIFIER. Never reuse a printed, sliced id.

SETTLED -- do not re-propose without new evidence:
<<< REBUILD >>>
"""
MEM = {"feedback-x-2026-09-20.md": "---\nname: feedback-x\ndescription: Never retype a count.\nmetadata:\n  type: feedback\n---\n**How to apply:**\n- Paste counts from tool output.\n",
       "finding-y.md": "---\nname: finding-y\ndescription: RETRACTED: the pocket was noise.\nmetadata:\n  type: finding\n  modified: 2026-09-01T00:00:00Z\n---\nbody\n"}
RETIRED = [{"name": "finding/old-pocket", "description": "An old pocket that was replaced.", "content": "body",
            "status": "retired", "type": "finding", "updated_at": "2026-08-01T00:00:00+00:00"}]


def _src():
    return MF.Sources(handoff="- 2026-09-28T13:02Z Beta: OPERATOR DECISION r=0.02 is live.\n",
                      handoff_archive="", memory_files=MEM, supabase_rows=RETIRED, skill=SKILL,
                      skill_blame={5: ("2026-09-26T10:00:00+00:00", "docs(handover): three traps Lambda paid for")},
                      doctrine={"CLAUDE.md": ("- Never `git stash` here.\n", "2026-09-20T00:00:00+00:00")})


def _build(**kw):
    args = dict(commits={}, tables=set(), handles=set(), knob_ok=lambda n: False,
                broken_refs_fn=lambda text: [], secret_values=set())
    args.update(kw)
    return MF.build(_src(), **args)


def test_the_build_is_deterministic():
    assert _build().facts == _build().facts and _build().episodes == _build().episodes


def test_kinds_statuses_authors_and_order():
    m = _build()
    by = {f["draft"]["statement"]: f for f in m.facts}
    assert by["Never retype a count."]["draft"]["kind"] == "rule"
    assert by["Paste counts from tool output."]["same_source"] is True
    marked = by["RETRACTED: the pocket was noise."]
    assert marked["import_status"] is None and marked["judge_pending"] is True        # never closed
    assert by["An old pocket that was replaced."]["import_status"] == "superseded"     # retired row
    trap = by["A SLICED IDENTIFIER IS A DIFFERENT IDENTIFIER. Never reuse a printed, sliced id."]
    assert trap["draft"]["kind"] == "trap" and trap["draft"]["author"] == "import"
    doc = by["Never `git stash` here."]
    assert doc["draft"]["author"] == "operator" and doc["draft"]["evidence"][0]["ref"] == "CLAUDE.md:1"
    kinds = [f["draft"]["kind"] for f in m.facts]
    assert kinds.index("trap") > max(i for i, k in enumerate(kinds) if k == "finding"), "traps go last (D13)"


def test_handoff_is_episodes_and_queue_only():
    m = _build()
    assert any(e["kind"] == "handoff_line" and e["author"] == "beta" for e in m.episodes)
    assert not any("r=0.02" in f["draft"]["statement"] for f in m.facts)
    assert any("r=0.02" in q["excerpt"] for q in m.queue)


def test_a_broken_body_ref_imports_the_headline_as_evidence_broken():
    m = _build(broken_refs_fn=lambda text: [{"type": "path", "ref": "scripts/gone.py"}] if "retype" in text else [])
    head = next(f for f in m.facts if f["draft"]["statement"] == "Never retype a count.")
    assert head["import_status"] == "evidence_broken"
    assert {"type": "path", "ref": "scripts/gone.py"} in head["draft"]["evidence"]


def test_a_secret_in_a_body_is_redacted_in_the_episode():
    src = _src()
    src.memory_files = {"finding-z.md": MEM["finding-y.md"].replace("body", "key=abcdef1234567890abcdef")}
    m = MF.build(src, commits={}, tables=set(), handles=set(), knob_ok=lambda n: False,
                 broken_refs_fn=lambda t: [], secret_values={"abcdef1234567890abcdef"})
    body = next(e for e in m.episodes if e["anchor"] == "memory/finding-z.md")["body"]
    assert "abcdef1234567890abcdef" not in body and "[REDACTED]" in body


def test_unmatched_session_row_and_user_type_are_not_lost_or_promoted():
    src = MF.Sources(supabase_rows=[
        dict(name='session/missing-file', description='Retained knowledge from an absent local file.', content='body', type='finding', status='active'),
        dict(name='person/operator', description='Personal detail stays provenance only.', content='body', type='user', status='active')])
    m = MF.build(src, commits={}, tables=set(), handles=set(), knob_ok=lambda n: False,
                 broken_refs_fn=lambda t: [], secret_values=set())
    assert any(f['draft']['statement'].startswith('Retained knowledge') for f in m.facts)
    assert len(m.episodes) == 2 and len(m.facts) == 1


def test_identical_claims_keep_closed_lifecycle_and_all_provenance():
    rows = [dict(name='finding/'+name, description='The same durable claim appears here.', content=name,
                 type='finding', status=status, updated_at='2026-09-01T00:00:00Z')
            for name,status in [('one','active'),('two','active'),('old','retired')]]
    m = MF.build(MF.Sources(supabase_rows=rows), commits={}, tables=set(), handles=set(),
                 knob_ok=lambda n: False, broken_refs_fn=lambda t: [], secret_values=set())
    assert len(m.facts) == 2
    assert {f['import_status'] for f in m.facts} == {None, 'superseded'}
    active = next(f for f in m.facts if not f['import_status'])
    assert len([e for e in active['draft']['evidence'] if e['type'] == 'episode']) == 2


def test_short_source_is_episode_only_with_counted_reason():
    src = MF.Sources(supabase_rows=[dict(name='brain_backend', description='backend', content='body')])
    m = MF.build(src, commits={}, tables=set(), handles=set(), knob_ok=lambda n: False,
                 broken_refs_fn=lambda t: [], secret_values=set())
    assert len(m.episodes) == 1 and not m.facts
    assert m.report['short_statement_episode_only'] == 1


def test_undated_local_mirror_uses_source_row_timestamp_before_closure():
    local = '---\nname: finding-example\ndescription: This source was retired long ago.\n---\nbody\n'
    src = MF.Sources(memory_files={'finding-example.md': local}, supabase_rows=[
        dict(name='session/finding-example', status='retired', updated_at='2026-08-02T02:00:00Z')])
    m = MF.build(src, commits={}, tables=set(), handles=set(), knob_ok=lambda n: False,
                 broken_refs_fn=lambda t: [], secret_values=set())
    fact = m.facts[0]
    assert fact['draft']['valid_from'] == fact['valid_to'] == '2026-08-02T02:00:00+00:00'
