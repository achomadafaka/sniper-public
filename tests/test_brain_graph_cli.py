"""The brain CLI (scripts/brain_cli.py; not brain.py, which the legacy root module already is) and
hybrid search. The CLI is loaded by PATH so a module named `brain` elsewhere can never shadow it."""
import importlib.util
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from brain_graph_fake_store import FakeStore
from bot.brain_graph import api
from bot.brain_graph.search import ask

ROOT = Path(__file__).resolve().parents[1]


def _cli():
    spec = importlib.util.spec_from_file_location("brain_cli_under_test", ROOT / "scripts" / "brain_cli.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _store():
    s = FakeStore()
    s.insert("brain_sections", {"slug": "crew", "title": "crew"})
    return s


def test_help_lists_every_command():
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "brain_cli.py"), "--help"],
                         capture_output=True, text=True, timeout=60).stdout
    for cmd in ("boot", "live", "ask", "entity", "write", "verify", "queue", "episode", "handover", "flush"):
        assert cmd in out


def test_write_args_parse_with_section_and_judgements():
    cli = _cli()
    d = cli.draft_from_args(cli.build_parser().parse_args(
        ["write", "--role", "alpha", "--kind", "rule", "--entity", "tool:git", "--section", "crew",
         "--evidence", "path:CLAUDE.md", "--evidence", "doc:https://x/y", "--judge", "abc=duplicate",
         "Never `git stash`."]))
    assert d["entities"] == [{"type": "tool", "name": "git"}] and d["section"] == "crew"
    assert d["evidence"] == [{"type": "path", "ref": "CLAUDE.md"}, {"type": "doc", "ref": "https://x/y"}]
    assert d["judgements"] == {"abc": "duplicate"}


def test_an_operational_decision_naming_a_memecoin_knob_passes_the_claim_gate():
    # 2026-10-07: operator decisions that named a MEMECOIN_ knob were refused as performance findings
    # because the CLI could not say the claim was operational. A decision without the scope still is.
    from bot.brain_graph.gate import validate_draft
    cli = _cli()
    argv = ["write", "--role", "alpha", "--kind", "decision", "--entity", "actor:alpha",
            "--evidence", "knob:MEMECOIN_SLOT_LANE_SENDER_ALL",
            "OPERATOR CHOICE: MEMECOIN_SLOT_LANE_SENDER_ALL=all on every slot-lane send."]
    plain = cli.draft_from_args(cli.build_parser().parse_args(argv))
    scoped = cli.draft_from_args(cli.build_parser().parse_args(argv[:1] + ["--claim-scope", "operational"] + argv[1:]))
    gate = "unsupported memecoin performance finding"
    assert scoped["claim_scope"] == "operational" and plain["claim_scope"] is None
    assert any(gate in e for e in validate_draft(plain, secret_values=()))
    assert not any(gate in e for e in validate_draft(scoped, secret_values=()))


def test_exit_codes_through_main(tmp_path, capsys):
    cli = _cli()
    s = _store()
    ok = ["write", "--role", "alpha", "--kind", "rule", "--entity", "tool:git", "--section", "crew",
          "--evidence", "path:CLAUDE.md"]
    assert cli.main(ok + ["Never `git stash` in the shared checkout."], store=s, outbox=tmp_path / "o") == 0
    assert cli.main(ok + ["Do not `git stash`; park it on a branch."], store=s, outbox=tmp_path / "o") == 3
    assert cli.main(ok[:-2] + ["--evidence", "path:no/such.py", "A rule about a ghost file."], store=s,
                    outbox=tmp_path / "o") == 2
    assert cli.main(["write", "--role", "unknown-role", "--kind", "rule", "--entity", "tool:git", "--section", "crew",
                     "--evidence", "path:CLAUDE.md", "Unknown role tries to write."], store=s, outbox=tmp_path / "o") == 2
    fid = s.rows["brain_facts"][0]["id"]
    assert cli.main(["verify", fid, "verified", "--role", "beta"], store=s) == 0
    assert "verified_partial" in capsys.readouterr().out                       # no output file: partial
    assert cli.main(["verify", "no-such-id", "verified", "--role", "beta"], store=s) == 2
    assert cli.main(["verify", fid, "verified", "--role", "alpha"], store=s) == 2     # the author: refused
    assert "REFUSED" in capsys.readouterr().out
    assert cli.main(["entity", "tool", "git"], store=s) == 0
    assert "[ok]" in capsys.readouterr().out                                   # evidence checks are shown


def test_ask_fuses_and_expands():
    s = _store()
    for st in ("Never `git stash` in the shared checkout.", "Commit with explicit pathspecs only."):
        api.write_fact(s, {"kind": "rule", "statement": st, "author": "alpha", "section": "crew",
                           "evidence": [{"type": "path", "ref": "CLAUDE.md"}],
                           "entities": [{"type": "tool", "name": "git"}]}, repo=ROOT, interactive=False)
    got = ask(s, "stash", embed=False)
    assert got[0]["statement"].startswith("Never") and len(got) == 2


def test_embedding_threads_are_pinned(monkeypatch):
    import bot.embeddings as emb
    seen = {}

    class FakeTE:
        def __init__(self, **kw):
            seen.update(kw)
    monkeypatch.setattr(emb, "_model", None)
    monkeypatch.setitem(sys.modules, "fastembed", type(sys)("fastembed"))
    sys.modules["fastembed"].TextEmbedding = FakeTE
    monkeypatch.delenv("BRAIN_EMBED_THREADS", raising=False)
    emb._get_model()
    assert seen["threads"] == 4                                                # not every core (32 here)
    monkeypatch.setattr(emb, "_model", None)


def test_importing_the_cli_does_not_touch_the_environment():
    # Lambda's review C1: the CLI loaded the REAL .env into os.environ at import, so a test worker
    # that loaded it saw production knob values the conftest does not pin. The import runs in a
    # fresh interpreter: in-process, an earlier test's import had already set the variable, which
    # hid a setdefault at import (Alpha, 2026-09-28). A minimal environment, so a variable the
    # caller's shell already holds cannot hide a setdefault either.
    import os
    env = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
    code = ("import importlib.util, os, sys\n"
            "before = dict(os.environ)\n"
            f"spec = importlib.util.spec_from_file_location('c', {str(ROOT / 'scripts' / 'brain_cli.py')!r})\n"
            "spec.loader.exec_module(importlib.util.module_from_spec(spec))\n"
            "print(sorted(set(os.environ.items()) ^ set(before.items())))\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, cwd=ROOT,
                       env=env)
    assert r.returncode == 0, r.stderr[-400:]
    assert r.stdout.strip() == "[]"


def test_a_real_run_loads_env_files_before_the_store(monkeypatch):
    cli = _cli()
    order = []
    monkeypatch.setattr(cli, "_load_env", lambda: order.append("env"))
    monkeypatch.setattr(cli, "RestStore", lambda: (order.append("store"), _store())[1])
    assert cli.main(["ask", "anything"], fetch=False) == 0
    assert order == ["env", "store"]


def test_the_boot_budget_holds_when_the_alarm_lands_inside_a_broad_except(tmp_path, monkeypatch, capsys):
    # Lambda's review V2: live.guard catches Exception, so an alarm raising an Exception inside
    # live.derive was swallowed and boot ran past its budget.
    import time
    cli = _cli()
    monkeypatch.setattr(cli, "BOOT_BUDGET_S", 1)

    def slow_derive(repo, knobs=()):
        try:
            time.sleep(5)
        except Exception:
            pass
        return {"errors": []}
    monkeypatch.setattr(cli.views.live, "derive", slow_derive)
    s = _store()
    s.insert("brain_views", {"name": "role/alpha", "description": "a", "char_limit": 12000,
                             "query_spec": {"sections": ["crew"], "kinds": None, "role_text": "Alpha."}})
    t = time.monotonic()
    assert cli.main(["boot", "alpha"], store=s, snapshot=tmp_path / "snap.json") == 0
    assert time.monotonic() - t < 3
    assert "BRAIN UNREACHABLE" in capsys.readouterr().out


def test_the_cli_boot_rides_out_a_slow_supabase_spell(tmp_path, capsys):
    # 2026-10-02 08:03:47Z: every brain call answered 200 but took 0.5-1.9 s (normally ~40 ms), and
    # Lambda's CLI boot fell back to the snapshot at its 3 s budget. A role boot reads view, sections,
    # then actor entity and its facts, four calls in series; at 0.8 s each that is 3.2 s.
    import time
    from datetime import datetime, timezone
    cli = _cli()
    s = _store()
    s.clock = datetime.now(timezone.utc)          # the CLI boots at real now; a 09-28 task reads stale
    s.insert("brain_views", {"name": "role/alpha", "description": "a", "char_limit": 12000,
                             "query_spec": {"sections": ["crew"], "kinds": None, "role_text": "Alpha."}})
    api.write_fact(s, {"kind": "task", "statement": "Alpha OPEN: read the slow-spell boot.", "author": "alpha",
                       "section": "crew", "evidence": [{"type": "path", "ref": "CLAUDE.md"}],
                       "entities": [{"type": "actor", "name": "alpha"}]}, repo=ROOT, interactive=False)

    class Slow:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            attr = getattr(self._inner, name)

            def call(*a, **kw):
                time.sleep(0.8)
                return attr(*a, **kw)
            return call if callable(attr) else attr
    assert cli.main(["boot", "alpha"], store=Slow(s), snapshot=tmp_path / "snap.json") == 0
    out = capsys.readouterr().out
    assert "BRAIN UNREACHABLE" not in out and "read the slow-spell boot" in out


def test_search_keeps_knob_path_and_slug_tokens():
    # Lambda's review C3: only isalnum tokens survived, so "MEMECOIN_REAL_CHASE_MAX_PCT" (the commonest
    # brain query, an exact key) became an empty query and returned nothing.
    s = _store()
    api.write_fact(s, {"kind": "rule", "statement": "MEMECOIN_REAL_CHASE_MAX_PCT is shared with J7; never widen it.",
                       "author": "alpha", "section": "crew", "evidence": [{"type": "path", "ref": "CLAUDE.md"}],
                       "entities": [{"type": "knob", "name": "MEMECOIN_REAL_CHASE_MAX_PCT"}]},
                   repo=ROOT, interactive=False)
    assert [f["statement"][:27] for f in ask(s, "MEMECOIN_REAL_CHASE_MAX_PCT", embed=False)] == \
        ["MEMECOIN_REAL_CHASE_MAX_PCT"]
    from bot.brain_graph.store import RestStore

    class Rec:
        def __init__(self):
            self.paths = []

        def __call__(self, method, path, body, headers, timeout):
            self.paths.append(path)
            return []
    rec = Rec()
    RestStore(request=rec).search_facts("MEMECOIN_REAL_CHASE_MAX_PCT scripts/brain_cli.py")
    assert "MEMECOIN%20REAL%20CHASE%20MAX%20PCT%20scripts%20brain%20cli%20py" in rec.paths[0]


def test_verify_refuses_fomo_and_injected_credentials(tmp_path, monkeypatch, capsys):
    cli = _cli()
    monkeypatch.setattr(cli, 'STATE', tmp_path)
    (tmp_path / '.env').write_text('PUBLIC_FLAG=1\n')
    (tmp_path / '.fomo.env').write_text('FOMO_API_KEY=UnmistakablyFakeFileCredential\n')
    monkeypatch.setenv('INJECTED_API_KEY', 'UnmistakablyFakeShellCredential')
    s = _store()
    fact = api.write_fact(s, {'kind': 'rule', 'statement': 'Commit with explicit paths only.',
                             'author': 'alpha', 'section': 'crew',
                             'evidence': [{'type': 'path', 'ref': 'CLAUDE.md'}],
                             'entities': [{'type': 'tool', 'name': 'git'}]}, repo=ROOT)['fact']
    for fake in ('UnmistakablyFakeFileCredential', 'UnmistakablyFakeShellCredential'):
        output = tmp_path / 'output.txt'
        output.write_text('upstream rejected bearer ' + fake)
        assert cli.main(['verify', fact['id'], 'verified', '--role', 'beta',
                         '--output-file', str(output)], store=s) == 2
        assert fake not in capsys.readouterr().out
        assert s.get('brain_facts', fact['id'])['verify_note'] is None


def test_the_cli_snapshot_fallback_uses_the_boot_limit(tmp_path, monkeypatch, capsys):
    """Gamma INFO 2026-09-30: the CLI's own fallback still cut the snapshot at 12,000."""
    import json
    from bot.brain_graph.store import BrainUnavailable
    cli = _cli()
    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps({"at": "2026-09-30T09:00:00Z", "role": "alpha", "text": "X" * 20000}))
    monkeypatch.setattr(cli, "_load_env", lambda: None)
    def down():
        raise BrainUnavailable("down")
    monkeypatch.setattr(cli, "RestStore", down)
    assert cli.main(["boot", "alpha"], snapshot=snap) == 0
    out = capsys.readouterr().out
    assert "BRAIN UNREACHABLE" in out and len(out.rstrip("\n")) <= cli.views.BOOT_LIMIT


class _HubStore(FakeStore):
    """Vector side answers every query with the same old, generic rows (live: 30 hub rules at similarity 0.84-0.87 for any query)."""
    hubs = []

    def match_facts(self, embedding, limit=20):
        return [dict(f) for f in self.hubs]


def _ranked_store(monkeypatch):
    monkeypatch.setattr('bot.brain_graph.search._embed', lambda q: [0.0])      # never load the real model in a test
    s = _HubStore()
    s.insert("brain_sections", {"slug": "crew", "title": "crew"})
    for i in range(5):
        api.write_fact(s, {"kind": "rule", "statement": f"Generic hub rule number {i} about shells.", "author": "alpha", "section": "crew",
                           "evidence": [{"type": "path", "ref": "CLAUDE.md"}], "entities": [{"type": "tool", "name": f"hub{i}"}]},
                       repo=ROOT, interactive=False)
    s.hubs = list(s.rows["brain_facts"])
    return s


def _fact(s, kind, text, recorded_at):
    api.write_fact(s, {"kind": kind, "statement": text, "author": "alpha", "section": "crew",
                       "evidence": [{"type": "path", "ref": "CLAUDE.md"}], "entities": [{"type": "tool", "name": text[:20]}]},
                   repo=ROOT, interactive=False)
    s.rows["brain_facts"][-1]["recorded_at"] = s.rows["brain_facts"][-1]["valid_from"] = recorded_at


def test_ask_keyword_hits_outrank_vector_only_hub_rows(monkeypatch):
    s = _ranked_store(monkeypatch)
    for i in range(3):
        _fact(s, "finding", f"Octopus plan {i} was measured.", "2026-10-01T00:00:00+00:00")
    got = [f["statement"] for f in ask(s, "octopus", embed=True)]
    assert [g.startswith("Octopus") for g in got[:3]] == [True, True, True], got


def test_ask_recent_decision_beats_older_fact_one_keyword_rank_behind(monkeypatch):
    s = _ranked_store(monkeypatch)
    _fact(s, "finding", "Octopus eval-1 old finding from March.", "2026-03-01T00:00:00+00:00")
    _fact(s, "decision", "Octopus eval-1 operator decision today.", "2026-10-30T09:00:00+00:00")
    got = [f["statement"] for f in ask(s, "octopus", embed=True, now=datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc))]
    assert got[0].startswith("Octopus eval-1 operator decision"), got


def test_ask_unknown_age_gets_no_recency_boost(monkeypatch):
    s = _ranked_store(monkeypatch)
    _fact(s, "finding", "Octopus first keyword hit.", None)
    _fact(s, "finding", "Octopus second keyword hit.", "not-a-time")
    got = [f["statement"] for f in ask(s, "octopus", embed=True, now=datetime(2026, 10, 30, tzinfo=timezone.utc))]
    assert got[:2] == ["Octopus first keyword hit.", "Octopus second keyword hit."]


FULL = "5fac49b8d" + "0123456789abcdef0123456789abcde"       # 40 hex


def _sha_store():
    s = _store()
    api.write_fact(s, {"kind": "finding", "statement": "Merged the coverage sample with no keyword in common.", "author": "alpha", "section": "crew",
                       "evidence": [{"type": "sha", "ref": FULL[:9]}], "entities": [{"type": "tool", "name": "sampler"}]},
                   repo=ROOT, interactive=False)
    api.write_fact(s, {"kind": "decision", "statement": "Entity linked change without sha evidence.", "author": "alpha", "section": "crew",
                       "evidence": [{"type": "path", "ref": "CLAUDE.md"}], "entities": [{"type": "commit", "name": "ab12cd34" + "e" * 32}]},
                   repo=ROOT, interactive=False)
    return s


def test_ask_sha_is_a_retrieval_key_short_and_full():
    s = _sha_store()
    for q in (FULL[:9], FULL, "see commit " + FULL[:9]):
        got = ask(s, q, embed=False)
        assert got and got[0]["statement"].startswith("Merged the coverage sample"), (q, got)


def test_ask_sha_resolves_through_the_commit_entity_prefix():
    s = _sha_store()
    got = ask(s, "ab12cd34", embed=False)
    assert got and got[0]["statement"].startswith("Entity linked change")


def test_ask_does_not_treat_words_or_timestamps_as_sha():
    s = _sha_store()
    calls = []
    real = s.facts_by_sha
    s.facts_by_sha = lambda t, limit=20: (calls.append(t), real(t, limit))[1]
    ask(s, "defaced 1793353688 facade deadbeef", embed=False)       # hex-only word, digits-only, hex-only word
    assert calls == []


def test_rest_store_facts_by_sha_issues_exact_ref_and_commit_prefix_reads():
    from bot.brain_graph.store import RestStore

    class Rec:
        def __init__(self):
            self.paths = []

        def __call__(self, method, path, body, headers, timeout):
            self.paths.append((method, path))
            return []
    rec = Rec()
    RestStore(request=rec).facts_by_sha(FULL)
    refs = [p for m, p in rec.paths if "evidence=cs." in p]
    assert len(refs) == 6 and any(FULL[:9] in p for p in refs) and all("valid_to=is.null" in p for p in refs)
    assert any("brain_entities" in p and "type=eq.commit" in p and "name_lc=like." + FULL + "%2A" in p for m, p in rec.paths)


def test_fake_and_rest_facts_by_shas_report_completeness():
    s = _sha_store()
    got = s.facts_by_shas([FULL[:9], "000000a1b2c", "ab12cd34"])
    assert got[FULL[:9]]["complete"] and len(got[FULL[:9]]["facts"]) == 1
    assert got["000000a1b2c"] == {"facts": [], "complete": True}        # absent: empty AND complete, not unknown
    assert got["ab12cd34"]["facts"] and got["ab12cd34"]["complete"]
    api.write_fact(s, {"kind": "finding", "statement": "Second record naming the same commit.", "author": "alpha", "section": "crew",
                       "evidence": [{"type": "sha", "ref": FULL[:9]}], "entities": [{"type": "tool", "name": "sampler2"}]}, repo=ROOT, interactive=False)
    assert s.facts_by_shas([FULL[:9]], per_limit=1)[FULL[:9]]["complete"] is False        # capped: never proof of absence of more


def test_rest_store_facts_by_shas_is_bounded_batch_not_per_token_fanout():
    from bot.brain_graph.store import RestStore

    class Rec:
        def __init__(self):
            self.paths = []

        def __call__(self, method, path, body, headers, timeout):
            self.paths.append(path)
            return []
    rec = Rec()
    toks = [("%02x" % i) * 20 for i in range(1, 41)]                  # 40 full ids
    res = RestStore(request=rec).facts_by_shas(toks)
    assert all(res[t] == {"facts": [], "complete": True} for t in toks)
    assert len(rec.paths) <= 6, rec.paths                              # not ~7 per token
    assert any("or=" in p and "evidence.cs." in p for p in rec.paths)

    class Full(Rec):
        def __call__(self, method, path, body, headers, timeout):
            self.paths.append(path)
            return [{"id": "f%03d" % i, "evidence": [], "entity_ids": []} for i in range(500)] if "evidence" in path else []
    res = RestStore(request=Full()).facts_by_shas(toks[:3])
    assert all(v["complete"] is False for v in res.values())          # a full page may hide more: unknown, not empty


def test_ask_recency_uses_event_date_not_import_day(monkeypatch):
    s = _ranked_store(monkeypatch)
    _fact(s, "finding", "Octopus eval-1 old finding imported late.", "2026-10-29T00:00:00+00:00")
    s.rows["brain_facts"][-1]["valid_from"] = "2026-03-01T00:00:00+00:00"                 # March fact, recorded yesterday
    _fact(s, "decision", "Octopus eval-1 operator decision today.", "2026-10-30T09:00:00+00:00")
    got = [f["statement"] for f in ask(s, "octopus", embed=True, now=datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc))]
    assert got[0].startswith("Octopus eval-1 operator decision"), got


def test_ask_caps_vector_only_hits_at_five_and_reports_the_cut(monkeypatch):
    s = _ranked_store(monkeypatch)
    for i in range(7):                                                    # 12 hub rows in all, none matching the keyword
        api.write_fact(s, {"kind": "rule", "statement": f"Extra hub rule {i} about shells.", "author": "alpha", "section": "crew",
                           "evidence": [{"type": "path", "ref": "CLAUDE.md"}], "entities": [{"type": "tool", "name": f"xhub{i}"}]},
                       repo=ROOT, interactive=False)
    s.hubs = [f for f in s.rows["brain_facts"] if "hub rule" in f["statement"].lower()]
    _fact(s, "finding", "Octopus keyword hit one.", "2026-10-01T00:00:00+00:00")
    stats = {}
    got = ask(s, "octopus", embed=True, stats=stats, limit=50)
    hubs = [f for f in got if "hub rule" in f["statement"].lower()]
    assert len(hubs) == 5 and got[0]["statement"].startswith("Octopus"), [g["statement"] for g in got]
    assert stats == {"vector_only_seen": len(s.hubs), "vector_only_cut": len(s.hubs) - 5, "vector_only_cap": 5}


def test_ask_cap_never_cuts_a_hit_that_also_matched_by_keyword_or_sha(monkeypatch):
    s = _ranked_store(monkeypatch)
    for i in range(8):
        _fact(s, "finding", f"Octopus shared hit {i}.", "2026-10-01T00:00:00+00:00")
    s.hubs = list(s.rows["brain_facts"])                                   # every fact is ALSO a vector hit
    stats = {}
    got = ask(s, "octopus", embed=True, stats=stats, limit=50)
    assert sum(g["statement"].startswith("Octopus shared") for g in got) == 8 and stats["vector_only_seen"] == len(s.hubs) - 8
