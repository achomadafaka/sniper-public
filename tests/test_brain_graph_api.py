"""The write, verify, episode and outbox API against the FakeStore (which carries migration 032's rules)."""
import json
from pathlib import Path

import pytest

from brain_graph_fake_store import FakeStore
from bot.brain_graph import api
from bot.brain_graph.store import BrainRefused, BrainUnavailable, VersionConflict

REPO = Path(__file__).resolve().parents[1]
DOC = [{"type": "path", "ref": "CLAUDE.md"}]
OLD_AGENTS = ("For lookups: use the dedicated tool (Read for known paths, Grep via Bash for exact symbols). "
              "Reserve general-purpose Agent for open-ended cross-codebase questions.")
NO_AGENTS = "Do not use the Agent tool or spawn parallel subagents in this project."


def store():
    s = FakeStore()
    for slug in ("crew", "infra/deploy"):
        s.insert("brain_sections", {"slug": slug, "title": slug})
    return s


def draft(statement, kind="rule", author="alpha", evidence=DOC, entity=("tool", "git"), section="crew", **kw):
    d = {"kind": kind, "statement": statement, "author": author, "evidence": list(evidence),
         "entities": [{"type": entity[0], "name": entity[1]}], "section": section}
    d.update(kw)
    return d


def test_needs_judgement_then_duplicate_is_noop():
    s = store()
    assert api.write_fact(s, draft("Never `git stash` in the shared checkout."), repo=REPO)["status"] == "written"
    r = api.write_fact(s, draft("Do not `git stash`; park WIP on a branch."), repo=REPO)
    assert r["status"] == "needs_judgement" and len(r["candidates"]) == 1
    n_entities = len(s.rows["brain_entities"])
    cid = r["candidates"][0]["id"]
    r2 = api.write_fact(s, draft("Do not `git stash`; park WIP on a branch."), repo=REPO, judgements={cid: "duplicate"})
    assert r2["status"] == "noop" and len(s.rows["brain_entities"]) == n_entities


def test_supersede_and_contradict():
    s = store()
    old = api.write_fact(s, draft("Use `git stash` to park the fix."), repo=REPO)["fact"]
    r = api.write_fact(s, draft("Never `git stash` in the shared checkout."), repo=REPO,
                       judgements={old["id"]: "supersedes"})
    assert r["status"] == "written" and s.get("brain_facts", old["id"])["status"] == "superseded"
    s2 = store()
    old2 = api.write_fact(s2, draft("Use `git stash` to park the fix."), repo=REPO)["fact"]
    r2 = api.write_fact(s2, draft("Never `git stash` in the shared checkout."), repo=REPO,
                        judgements={old2["id"]: "contradicts"})
    assert r2["fact"]["status"] == "disputed" and s2.get("brain_facts", old2["id"])["status"] == "disputed"
    assert any(l["rel"] == "contradicts" for l in s2.rows["brain_links"])


def test_contradict_retries_a_moved_candidate_and_never_half_applies():
    class Racy(FakeStore):
        conflicts = 1

        def write_fact(self, row, actions):
            moving = next((a for a in actions if a["verdict"] == "contradicts"), None)
            if moving and self.conflicts:
                self.conflicts -= 1
                old = self.get("brain_facts", moving["id"])
                self.patch_fact(old["id"], old["version"], {"verify_note": "moved by someone else"})
                raise VersionConflict(old["id"])
            return super().write_fact(row, actions)
    s = Racy()
    s.insert("brain_sections", {"slug": "crew", "title": "crew"})
    old = api.write_fact(s, draft("Use `git stash` to park the fix."), repo=REPO)["fact"]
    r = api.write_fact(s, draft("Never `git stash` here."), repo=REPO, judgements={old["id"]: "contradicts"})
    assert r["status"] == "written" and s.get("brain_facts", old["id"])["status"] == "disputed"
    s.conflicts = 5
    old3 = api.write_fact(s, draft("Use `git stash` before a checkout."), repo=REPO,
                          judgements={f["id"]: "unrelated" for f in s.rows["brain_facts"]})["fact"]
    r3 = api.write_fact(s, draft("Never `git stash` at all."), repo=REPO,
                        judgements={old3["id"]: "contradicts", old["id"]: "unrelated", r["fact"]["id"]: "unrelated"})
    assert r3["status"] == "written" and r3["fact"]["judge_pending"] is True and r3["warnings"]
    assert any(l["rel"] == "candidate" and l["to_id"] == old3["id"] for l in s.rows["brain_links"])


def test_non_interactive_writes_land_judge_pending():
    s = store()
    old = api.write_fact(s, draft("Use `git stash` to park the fix."), repo=REPO)["fact"]
    r = api.write_fact(s, draft("Never `git stash` in the shared checkout.", author="import"), repo=REPO,
                       interactive=False)
    assert r["status"] == "written" and r["fact"]["judge_pending"] is True
    assert any(l["rel"] == "candidate" and l["to_id"] == old["id"] for l in s.rows["brain_links"])


def test_new_entities_need_a_section_and_action_keys_share_a_tool_entity():
    s = store()
    r = api.write_fact(s, draft("Commit with explicit pathspecs only.", entity=("tool", "git"), section=None),
                       repo=REPO)
    assert r["status"] == "refused" and any("section" in e for e in r["errors"])
    assert s.rows["brain_entities"] == []
    old = api.write_fact(s, draft(OLD_AGENTS, entity=("process", "research"), section="crew"), repo=REPO)["fact"]
    r = api.write_fact(s, draft(NO_AGENTS, entity=("process", "pytest"), section="infra/deploy"), repo=REPO)
    assert r["status"] == "needs_judgement" and [c["id"] for c in r["candidates"]] == [old["id"]]
    tool = s.find_entity("tool", "agent tool")
    assert tool and tool["id"] in old["entity_ids"]
    ent = s.find_entity("process", "research")
    assert ent["section_id"] == s.sections(["crew"])[0]["id"]
    assert s.find_entity("process", "pytest") is None                  # needs_judgement created nothing
    api.write_fact(s, draft(NO_AGENTS, entity=("process", "pytest"), section="infra/deploy"), repo=REPO,
                   judgements={old["id"]: "supersedes"})
    assert s.find_entity("process", "pytest")["section_id"] == s.sections(["infra/deploy"])[0]["id"]


def test_a_non_interactive_supersede_keeps_the_unjudged_candidates_pending():
    s = store()
    a = api.write_fact(s, draft("Use `git stash` to park the fix."), repo=REPO)["fact"]
    b = api.write_fact(s, draft("Use `git stash` before a checkout.", author="beta"), repo=REPO,
                       judgements={a["id"]: "unrelated"})["fact"]
    r = api.write_fact(s, draft("Never `git stash` in the shared checkout.", author="import"), repo=REPO,
                       interactive=False, judgements={a["id"]: "supersedes"})
    assert r["status"] == "written" and s.get("brain_facts", a["id"])["status"] == "superseded"
    assert r["fact"]["judge_pending"] is True
    assert any(l["rel"] == "candidate" and l["to_id"] == b["id"] for l in s.rows["brain_links"])


def test_broken_evidence_refuses_unchecked_does_not():
    s = store()
    r = api.write_fact(s, draft("A rule about a ghost file.", evidence=[{"type": "path", "ref": "no/such.py"}]),
                       repo=REPO)
    assert r["status"] == "refused" and any("no/such.py" in e for e in r["errors"])
    ok = api.write_fact(s, draft("A rule citing a runtime file.",
                                 evidence=[{"type": "path", "ref": "data/absent_now.pid"}]), repo=REPO)
    assert ok["status"] == "written" and ok["fact"]["evidence"][0]["check"] == "unchecked"
    offline = api.write_fact(s, draft("A rule about a maybe-new file.", evidence=[{"type": "path", "ref": "no/such.py"}]),
                             repo=REPO, fetched=False)
    assert offline["status"] == "written" and offline["fact"]["evidence"][0]["check"] == "unchecked"


def test_codex_writes_through_the_normal_fact_gate():
    r = api.write_fact(store(), draft("Commit with explicit pathspecs only.", author="codex"), repo=REPO)
    assert r["status"] == "written" and r["fact"]["author"] == "codex"


def test_verify_rules():
    s = store()
    f = api.write_fact(s, draft("Commit with explicit pathspecs only."), repo=REPO)["fact"]
    with pytest.raises(BrainRefused, match="author"):
        api.verify_fact(s, f["id"], "verified", role="alpha", output="ok")
    with pytest.raises(BrainRefused, match="verifier"):
        api.verify_fact(s, f["id"], "verified", role="unknown-role", output="ok")
    with pytest.raises(BrainRefused, match="reason"):
        api.verify_fact(s, f["id"], "refuted", role="beta", output="")
    v = api.verify_fact(s, f["id"], "verified", role="beta", output="")
    assert v["status"] == "verified_partial" and v["verified_by"] == "beta"
    g = api.write_fact(s, draft("Never `git stash` in the shared checkout."), repo=REPO)["fact"]
    s.patch_fact(g["id"], g["version"], {"status": "disputed"})
    with pytest.raises(BrainRefused, match="dispute"):
        api.verify_fact(s, g["id"], "verified", role="beta", output="ran it")
    h = api.write_fact(s, draft("Never `git stash` in any checkout.", author="import"), repo=REPO,
                       interactive=False)["fact"]
    with pytest.raises(BrainRefused, match="judge"):
        api.verify_fact(s, h["id"], "verified", role="beta", output="ran it")


def test_verify_refuses_a_closed_fact():
    """Gamma 2026-09-30: a `duplicate` judgement retracted 505f017e, 217ad664 and dc71d33f (valid_to set);
    verify_fact then rewrote their status to verified_partial, leaving each closed but reading as live."""
    s = store()
    for patch in ({"valid_to": "2026-09-30T11:17:28+00:00"}, {"status": "retracted"}, {"status": "superseded"}):
        f = api.write_fact(s, draft("Commit with explicit pathspecs only, case %d." % len(patch)), repo=REPO)["fact"]
        f = s.patch_fact(f["id"], f["version"], patch)
        with pytest.raises(BrainRefused, match="closed"):
            api.verify_fact(s, f["id"], "verified", role="beta", output="ran it")
        assert s.get("brain_facts", f["id"])["verified_by"] is None


def test_verify_refuses_secrets_in_note_and_output():
    s = store()
    f = api.write_fact(s, draft("Commit with explicit pathspecs only."), repo=REPO)["fact"]
    for note, output, secrets in (("", "https://example.invalid/v2/FAKESECRET1234567890", ()),
                                  ("configured-secret-value", "ok", ("configured-secret-value",))):
        with pytest.raises(BrainRefused, match="secret"):
            api.verify_fact(s, f["id"], "verified", role="beta", output=output,
                            note=note, secret_values=secrets)
    assert s.get("brain_facts", f["id"])["verify_note"] is None


def test_supersede_and_contradict_insert_a_disputed_fact():
    s = store()
    a = api.write_fact(s, draft("Use `git stash` for temporary work."), repo=REPO)["fact"]
    b = api.write_fact(s, draft("Use `git stash` for urgent work."), repo=REPO,
                       judgements={a["id"]: "unrelated"})["fact"]
    new = api.write_fact(s, draft("Never `git stash` in this checkout."), repo=REPO,
                         judgements={a["id"]: "supersedes", b["id"]: "contradicts"})["fact"]
    assert new["status"] == "disputed"
    assert s.get("brain_facts", b["id"])["status"] == "disputed"


def test_outbox_queue_flush_and_a_concurrent_append_is_kept(tmp_path):
    class Down(FakeStore):
        def find_entity(self, *a, **k):
            raise BrainUnavailable("down")
    out = tmp_path / "outbox.jsonl"
    d = draft("Commit with explicit pathspecs only.")
    assert api.write_fact(Down(), d, repo=REPO, outbox=out)["status"] == "queued"
    assert api.write_fact(Down(), draft("Never `git stash` in the shared checkout."), repo=REPO,
                          outbox=out)["status"] == "queued"

    class AppendsDuringFlush(FakeStore):
        done = False

        def insert(self, table, row):
            if table == "brain_facts" and not self.done:
                self.done = True
                api._queue(out, draft("Queued by another session mid-flush."), {})
            return super().insert(table, row)
    s = AppendsDuringFlush()
    s.insert("brain_sections", {"slug": "crew", "title": "crew"})
    res = api.flush_outbox(s, out, repo=REPO, secret_values=set())
    assert res["written"] == 2 and res["refused"] == 0
    left = [json.loads(l) for l in out.read_text().splitlines() if l.strip()]
    assert [x["draft"]["statement"] for x in left] == ["Queued by another session mid-flush."]
    assert not out.with_name(out.name + ".inflight").exists()


def test_a_crashed_flush_is_replayed(tmp_path):
    out = tmp_path / "outbox.jsonl"
    out.with_name(out.name + ".inflight").write_text(
        json.dumps({"draft": draft("Commit with explicit pathspecs only."), "judgements": {}}) + "\n")
    s = store()
    assert api.flush_outbox(s, out, repo=REPO, secret_values=set())["written"] == 1


def test_episode_parts_occurred_at_secret_and_redaction():
    s = store()
    parts = api.add_episode(s, "handoff_line", "x" * 4000, author="alpha", occurred_at="2026-09-27T10:00:00+00:00")
    assert [p["part_no"] for p in parts] == [1, 2, 3] and all(len(p["body"]) <= 1800 for p in parts)
    assert parts[1]["parent_id"] == parts[0]["id"] and parts[0].get("parent_id") is None
    assert all(p["occurred_at"] == "2026-09-27T10:00:00+00:00" for p in parts)
    assert api.add_episode(s, "handoff_line", "undated", author="alpha")[0]["occurred_at"] is None
    with pytest.raises(BrainRefused):
        api.add_episode(s, "handoff_line", "token=abc123def456ghi789jkl0", author="alpha")
    red = api.add_episode(s, "legacy_memory", "key AbCdEf12GhIj34KlMn56 here", author="import",
                          secret_values={"AbCdEf12GhIj34KlMn56"}, redact_secrets=True)
    assert "AbCdEf12" not in red[0]["body"] and "[REDACTED]" in red[0]["body"]
    assert api.add_episode(s, "crew_report", "hello", author="codex")[0]["author"] == "codex"


def test_lost_insert_reply_outbox_replay_does_not_duplicate_fact(tmp_path):
    class LostReply(FakeStore):
        drop = True

        def write_fact(self, row, actions):
            result = super().write_fact(row, actions)
            if self.drop:
                self.drop = False
                raise BrainUnavailable('simulated response lost after commit')
            return result

    s = LostReply()
    s.insert('brain_sections', {'slug': 'crew', 'title': 'crew'})
    outbox = tmp_path / 'outbox.jsonl'
    statement = 'Commit with explicit pathspecs only.'
    assert api.write_fact(s, draft(statement), repo=REPO, outbox=outbox)['status'] == 'queued'
    api.flush_outbox(s, outbox, repo=REPO, secret_values=set())
    assert len([f for f in s.rows['brain_facts'] if f['statement'] == statement]) == 1


def test_lost_atomic_reply_preserves_all_judgements_and_links(tmp_path):
    class LostReply(FakeStore):
        drop = False

        def write_fact(self, row, actions):
            result = super().write_fact(row, actions)
            if self.drop:
                self.drop = False
                raise BrainUnavailable('reply lost after atomic commit')
            return result

    s = LostReply()
    s.insert('brain_sections', {'slug': 'crew', 'title': 'crew'})
    a = api.write_fact(s, draft('Use `git stash` for temporary work.'), repo=REPO)['fact']
    b = api.write_fact(s, draft('Use `git stash` for urgent work.'), repo=REPO,
                       judgements={a['id']: 'unrelated'})['fact']
    s.drop = True
    out = tmp_path / 'outbox'
    result = api.write_fact(s, draft('Never `git stash` in this checkout.'), repo=REPO, outbox=out,
                           judgements={a['id']: 'supersedes', b['id']: 'contradicts'})
    assert result['status'] == 'queued'
    before = [(f['id'], f['status'], f['version']) for f in s.rows['brain_facts']]
    api.flush_outbox(s, out, repo=REPO, secret_values=set())
    assert [(f['id'], f['status'], f['version']) for f in s.rows['brain_facts']] == before
    assert sorted(l['rel'] for l in s.rows['brain_links']) == ['contradicts', 'supersedes']


def test_crashed_flush_checkpoints_identity_before_commit(tmp_path):
    class CrashAfterCommit(BaseException):
        pass

    class Crashes(FakeStore):
        crash = True

        def write_fact(self, row, actions):
            result = super().write_fact(row, actions)
            if self.crash:
                self.crash = False
                raise CrashAfterCommit()
            return result

    s = Crashes()
    s.insert('brain_sections', {'slug': 'crew', 'title': 'crew'})
    out = tmp_path / 'outbox'
    # Pre-upgrade inflight entries have no UUID. They must get one before any write.
    inflight = out.with_name(out.name + '.inflight')
    inflight.write_text(json.dumps({'draft': draft('Commit with explicit pathspecs only.'), 'judgements': {}}) + '\n')
    with pytest.raises(CrashAfterCommit):
        api.flush_outbox(s, out, repo=REPO, secret_values=set())
    committed_id = s.rows['brain_facts'][0]['id']
    assert json.loads(inflight.read_text())['write_id'] == committed_id
    assert api.flush_outbox(s, out, repo=REPO, secret_values=set())['written'] == 1
    assert len(s.rows['brain_facts']) == 1


def test_replay_preserves_a_judgement_whose_target_closed_while_offline(tmp_path):
    s = store()
    old = api.write_fact(s, draft('Use `git stash` for temporary work.'), repo=REPO)['fact']
    out = tmp_path / 'outbox'
    api._queue(out, draft('Never `git stash` in this checkout.'), {old['id']: 'supersedes'})
    s.patch_fact(old['id'], old['version'], {'status': 'superseded', 'valid_to': s._now()})
    assert api.flush_outbox(s, out, repo=REPO, secret_values=set())['written'] == 1
    new = s.rows['brain_facts'][-1]
    assert new['judge_pending']
    assert any(l['from_id'] == new['id'] and l['to_id'] == old['id'] and l['rel'] == 'candidate'
               for l in s.rows['brain_links'])


def test_handoff_syncs_new_inflight_directory_before_clearing_source(tmp_path, monkeypatch):
    import builtins
    import os
    s = store()
    out = tmp_path / 'outbox'
    api._queue(out, draft('Commit with explicit pathspecs only.'), {})
    inflight = out.with_name(out.name + '.inflight')
    events = []
    real_sync, real_open = os.fsync, builtins.open

    def sync(fd):
        events.append(os.readlink(f'/proc/self/fd/{fd}'))
        return real_sync(fd)

    def opening(path, mode='r', *args, **kwargs):
        if Path(path) == out and mode == 'w':
            appended = max(i for i, name in enumerate(events) if name == str(inflight))
            assert str(tmp_path) in events[appended + 1:], 'source cleared before new inflight directory entry was durable'
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, 'fsync', sync)
    monkeypatch.setattr(builtins, 'open', opening)
    assert api.flush_outbox(s, out, repo=REPO, secret_values=set())['written'] == 1
