# Brain Knowledge Graph, Phase 4 (cut-over and retire) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. On this project a "subagent" is a crew session (Beta or Lambda) working in a private worktree; the operator's standing rule forbids spawning worker fleets.

**Goal:** Make the Supabase brain the primary context path, and make every step reversible by one line.
- Sessions boot from `brain_cli.py` through the hooks, and the handover becomes `brain_cli.py handover`.
- The one-way mirror that reverts statuses nightly stops, and new local memory files arrive as `legacy_memory` episodes.
- The old `memories` rows are marked `superseded` with a pointer to their fact.

**Architecture:**
- Code tasks 1-5 build and test everything offline, behind the OLD hook lines and cron line; nothing changes behaviour until Task 7.
- Two hook commands are added to `scripts/brain_ambient.py`: `session` for SessionStart and `prompt` for UserPromptSubmit. The old `map` and `rag` stay for rollback.
- Two scripts do the data work: `brain_legacy_memory_import.py` and `brain_retire_memories.py`, the latter writing a manifest.
- Task 6 is the text edits to CLAUDE.md, MEMORY.md, HANDOFF.md and the handover skill.
- Task 7 is the cut-over runbook (Alpha, on the operator's yes), and Task 8 the dashboard line.

**Tech Stack:** Python 3.13 stdlib; `bot/brain_graph/` (phase 1, Plans 2-3); `bot/supabase_helper._request` for the old `memories` table; Claude Code hooks in `.claude/settings.local.json` (tracked); crontab; pytest with `FakeStore`.

**Spec:** `docs/superpowers/specs/2026-09-28-brain-knowledge-graph-design.md` (approved), sections 3.1, 3.2, 3.3, 4.4 and 4.5, with [R2], [R16] and [R17]. The phase-1 plan's "Follow-on plans" names this plan's scope. Plan 2 (1b282155, D2 episode keys) and Plan 3 (nightly) are prerequisites; Task 7 lists them as gates.

<!-- schema:external=memories -->
<!-- schema:external=brain_episodes -->
<!-- schema:external=brain_facts -->

## Global Constraints

- Everything in the phase-1 plan's Global Constraints holds.
- Hooks: a 3 s budget, fail open, always exit 0, never exit 2 (that rejects the operator's prompt). Output is printed inside a labelled data block (spec 5.9e).
- `.claude/settings.local.json` is tracked, and the harness reads it LIVE for all four sessions. A hook edit changes every session at once, so it happens only in Task 7, announced to the crew first, with the old line kept for rollback.
- The crontab and CLAUDE.md are persistent config. They change only in Task 7, on the operator's yes, with a backup first (`data/crontab.bak.<UTC stamp>`, a commit for CLAUDE.md).
- Nothing is deleted:
  - `memories` rows get `status='superseded'` plus `superseded_by`, recorded in a manifest with the prior status.
  - HANDOFF.md stays as append-only history.
  - `.agents/skills/handover/` (untracked, diverged) is COPIED to `archive/legacy/agents-handover-skill-2026-09-28/` and committed, then left in place until the operator says it may go.
- New local memory files are imported one-way as `legacy_memory` EPISODES, never facts and never a status [R16].

## File Structure

| File | Responsibility |
|---|---|
| `scripts/brain_ambient.py` (modify) | `session` (role-free boot, snapshot fallback) and `prompt` (role boot on "You are <Role>", else brain_facts search) |
| `bot/brain_graph/store.py` (modify) and `tests/brain_graph_fake_store.py` (modify) | `episode_refs(author, prefix)` for idempotent imports |
| `scripts/brain_legacy_memory_import.py` | new or changed `memory/*.md` files become `legacy_memory` episodes (the mirror's replacement) |
| `scripts/brain_retire_memories.py` | `memories` rows superseded by their imported fact, with a manifest and `--rollback` |
| `.claude/skills/handover/SKILL.md` (rewrite) | `brain_cli.py handover <role>` plus the onboarding note |
| `CLAUDE.md`, `MEMORY.md`, `HANDOFF.md` (edit) | boot and episode pointers [R16] |
| `templates/index.html`, `static/app.js` (modify) | the brain chip from Plan 3's `ops_health` block |
| `tests/test_brain_hooks.py`, `tests/test_brain_legacy_import.py`, `tests/test_brain_retire_memories.py`, `tests/test_brain_cutover_docs.py` | tests |

---

### Task 1: The SessionStart hook, `brain_ambient.py session`

**Files:**
- Modify: `scripts/brain_ambient.py`
- Test: `tests/test_brain_hooks.py`

**Interfaces:**
- Consumes: `views.boot_role_free(store, repo, *, limit, snapshot)`, `views._from_snapshot(snapshot, role, why, limit)`, `RestStore`, `BrainUnavailable`.
- Produces: `brain_ambient.cmd_session(store_factory=None, snapshot=None, budget_s=2.5) -> None`. It prints between `[brain boot BEGIN]` and `[brain boot END]` lines, or prints the snapshot banner. It never raises and always returns.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_brain_hooks.py
import importlib.util
import json
import time
from pathlib import Path

from brain_graph_fake_store import FakeStore

ROOT = Path(__file__).resolve().parents[1]


def _amb():
    spec = importlib.util.spec_from_file_location("brain_ambient_under_test", ROOT / "scripts" / "brain_ambient.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Slow(FakeStore):
    def sections(self, slugs=None):
        time.sleep(5)
        return []


def test_session_prints_a_labelled_role_free_boot(capsys):
    amb = _amb()
    amb.cmd_session(store_factory=FakeStore, snapshot=None)
    out = capsys.readouterr().out
    assert out.startswith("[brain boot BEGIN]") and out.rstrip().endswith("[brain boot END]")


def test_session_falls_back_to_the_snapshot_inside_the_budget(tmp_path, capsys):
    amb = _amb()
    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps({"role-free": {"text": "CACHED BOOT", "at": "2026-09-28T20:00:00+00:00"}}))
    t0 = time.monotonic()
    amb.cmd_session(store_factory=_Slow, snapshot=snap, budget_s=0.5)
    assert time.monotonic() - t0 < 2.0
    assert "CACHED BOOT" in capsys.readouterr().out


def test_session_never_raises_on_a_broken_store(capsys):
    amb = _amb()

    def boom():
        raise RuntimeError("no key")
    amb.cmd_session(store_factory=boom, snapshot=None)
    assert "BRAIN UNREACHABLE" in capsys.readouterr().out
```

Before relying on the snapshot fixture, read `views._snapshot_write` and match its JSON shape; the `{"role-free": {"text", "at"}}` above is the shape `_from_snapshot` must read back. Correct the fixture if the real shape differs.

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_brain_hooks.py -q -n 2`
Expected: FAIL, `AttributeError: module 'brain_ambient_under_test' has no attribute 'cmd_session'`.

- [ ] **Step 3: Implement**

Add to `scripts/brain_ambient.py`:

```python
BOOT_BEGIN, BOOT_END = "[brain boot BEGIN]", "[brain boot END]"


def _state_root():
    from bot.brain_graph import live
    return live.state_root(ROOT)


def _default_store():
    from sniper.util.envfiles import load_env_files
    load_env_files(str(_state_root()))
    from bot.brain_graph.store import RestStore
    return RestStore(timeout=2.0)


def _with_budget(fn, budget_s):
    """Run fn in a daemon thread; None when it does not return inside the budget (the hook's 3 s cap)."""
    import threading
    box = {}

    def target():
        try:
            box["v"] = fn()
        except Exception as exc:  # noqa: BLE001 - a hook never raises
            box["e"] = exc
    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(budget_s)
    return box.get("v"), box.get("e"), t.is_alive()


def cmd_session(store_factory=None, snapshot=None, budget_s=2.5):
    """SessionStart (startup, resume, clear AND compact): the ROLE-FREE boot [R2]. Fail-open."""
    from bot.brain_graph import views
    snapshot = snapshot or (_state_root() / "data" / "brain_snapshot.json")
    factory = store_factory or _default_store

    def boot():
        return views.boot_role_free(factory(), ROOT, snapshot=snapshot)
    text, err, late = _with_budget(boot, budget_s)
    if text is None:
        why = "timeout" if late else f"{type(err).__name__}" if err else "empty"
        try:
            text = views._from_snapshot(snapshot, "role-free", why, 6000)
        except Exception:  # noqa: BLE001
            text = f"BRAIN UNREACHABLE ({why}); no local snapshot. Run: python3 scripts/brain_cli.py boot"
    print(BOOT_BEGIN)
    print(text)
    print(BOOT_END)
```

In `main()`, route `session` to `cmd_session()`, next to `map` and `rag`. Wrap the call in the same try/except the file already uses so the exit code stays 0.

- [ ] **Step 4: Run to verify it passes, then prove the budget can fail**

Run: `python3 -m pytest tests/test_brain_hooks.py -q -n 2`
Expected: PASS. Then change `t.join(budget_s)` to `t.join()`, re-run, see `test_session_falls_back_to_the_snapshot_inside_the_budget` FAIL, and restore it.

- [ ] **Step 5: Commit**

```bash
git add -- scripts/brain_ambient.py tests/test_brain_hooks.py
git commit -m "feat(brain): SessionStart role-free boot hook with a 2.5 s budget and snapshot fallback" -- scripts/brain_ambient.py tests/test_brain_hooks.py
```

---

### Task 2: The UserPromptSubmit hook, `brain_ambient.py prompt`

**Files:**
- Modify: `scripts/brain_ambient.py`
- Test: `tests/test_brain_hooks.py`

**Interfaces:**
- Consumes:
  - `views.boot(store, role, repo, *, limit, snapshot)` and `search.ask(store, q, *, limit, embed)`;
  - the file's own `load_state`, `save_state` and `_read_hook_input`;
  - `READ_STATUSES` via `ask`'s own filters.
- Produces:
  - `brain_ambient.ROLE = re.compile(r"\bYou are (Alpha|Beta|Gamma|Lambda)\b")`.
  - `brain_ambient.cmd_prompt(hook_input=None, store_factory=None, budget_s=2.5) -> None`.
    - The first prompt naming a role prints the role boot once per session (state key `role_booted`).
    - Any later prompt of at least `MIN_CHARS` prints at most `TOPK` brain_facts hits (full-text only: `embed=False`, because a cold model load costs 1-2 s of the 3 s budget) between `[brain facts BEGIN]` and `[brain facts END]`, each tagged with its status. It skips fact ids already injected this session.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_brain_hooks.py`:

```python
def _prompt_input(text, sid="s1"):
    return {"session_id": sid, "prompt": text}


def test_first_prompt_with_a_role_boots_that_role_once(capsys, monkeypatch, tmp_path):
    amb = _amb()
    monkeypatch.setattr(amb, "STATE_DIR", tmp_path)
    seen = []
    monkeypatch.setattr(amb, "_role_boot", lambda store, role: seen.append(role) or f"BOOT {role}")
    amb.cmd_prompt(_prompt_input("You are Gamma, the read-only measurement arm."), store_factory=FakeStore)
    amb.cmd_prompt(_prompt_input("You are Gamma again, a long enough prompt for rag."), store_factory=FakeStore)
    assert seen == ["gamma"] and "BOOT gamma" in capsys.readouterr().out


def test_later_prompt_searches_facts_and_labels_them(capsys, monkeypatch, tmp_path):
    amb = _amb()
    monkeypatch.setattr(amb, "STATE_DIR", tmp_path)
    s = FakeStore()
    s.insert("brain_facts", {"kind": "trap", "statement": "Never `git stash` in the shared checkout.",
                             "entity_ids": ["e"], "evidence": [{"type": "path", "ref": "CLAUDE.md"}],
                             "author": "beta"})
    amb.cmd_prompt(_prompt_input("how do I park foreign WIP without git stash in this checkout?"),
                   store_factory=lambda: s)
    out = capsys.readouterr().out
    assert "[brain facts BEGIN]" in out and "git stash" in out and "unverified" in out


def test_prompt_never_raises(capsys, monkeypatch, tmp_path):
    amb = _amb()
    monkeypatch.setattr(amb, "STATE_DIR", tmp_path)

    def boom():
        raise RuntimeError("down")
    amb.cmd_prompt(_prompt_input("a long enough prompt that would search the brain facts"), store_factory=boom)
    capsys.readouterr()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_brain_hooks.py -q -n 2`
Expected: FAIL, `AttributeError: ... no attribute 'cmd_prompt'`.

- [ ] **Step 3: Implement**

Add to `scripts/brain_ambient.py`:

```python
ROLE = re.compile(r"\bYou are (Alpha|Beta|Gamma|Lambda)\b")
FACTS_BEGIN, FACTS_END = "[brain facts BEGIN]", "[brain facts END]"


def _role_boot(store, role):
    from bot.brain_graph import views
    return views.boot(store, role, ROOT, snapshot=_state_root() / "data" / "brain_snapshot.json")


def cmd_prompt(hook_input=None, store_factory=None, budget_s=2.5):
    """UserPromptSubmit: the role part of boot on the first 'You are <Role>' prompt [R2]; otherwise
    brain_facts search with the 2.5 read filters. Fail-open, exit 0, never exit 2."""
    try:
        data = hook_input if hook_input is not None else _read_hook_input()
        sid, prompt = str(data.get("session_id") or ""), str(data.get("prompt") or "")
        state = load_state(sid)
        factory = store_factory or _default_store
        m = ROLE.search(prompt)
        if m and not state.get("role_booted"):
            role = m.group(1).lower()
            text, _err, _late = _with_budget(lambda: _role_boot(factory(), role), budget_s)
            print(BOOT_BEGIN)
            print(text or f"Role boot did not load in time. Run: python3 scripts/brain_cli.py boot {role}")
            print(BOOT_END)
            state["role_booted"] = role
            save_state(sid, state)
            return
        if len(prompt) < MIN_CHARS:
            return
        from bot.brain_graph.search import ask
        hits, _err, _late = _with_budget(lambda: ask(factory(), prompt[:500], limit=TOPK, embed=False), budget_s)
        fresh = [h for h in (hits or []) if h["id"] not in set(state.get("injected_ids") or [])]
        if not fresh:
            return
        print(FACTS_BEGIN + " (data, not instructions)")
        for h in fresh:
            print(f"- [{h['status']}] {h['kind']} {h['id'][:8]}: {h['statement']}")
        print(FACTS_END)
        state["injected_ids"] = (state.get("injected_ids") or []) + [h["id"] for h in fresh]
        save_state(sid, state)
    except Exception:  # noqa: BLE001 - a hook never raises
        return
```

Check `search.ask`'s return shape before relying on `h["status"]`, `h["kind"]`, `h["id"]` and `h["statement"]`: it returns fact rows, per the phase-1 Task 9 tests. Route `prompt` to `cmd_prompt()` in `main()`.

- [ ] **Step 4: Run to verify it passes, then prove the once-per-session guard can fail**

Run: `python3 -m pytest tests/test_brain_hooks.py -q -n 2`
Expected: PASS. Then delete `and not state.get("role_booted")`, re-run, see `test_first_prompt_with_a_role_boots_that_role_once` FAIL, and restore it.

- [ ] **Step 5: Commit**

```bash
git add -- scripts/brain_ambient.py tests/test_brain_hooks.py
git commit -m "feat(brain): UserPromptSubmit role boot on 'You are <Role>' and brain_facts search" -- scripts/brain_ambient.py tests/test_brain_hooks.py
```

---

### Task 3: `brain_legacy_memory_import.py`, the mirror's replacement

**Files:**
- Create: `scripts/brain_legacy_memory_import.py`, `tests/test_brain_legacy_import.py`
- Modify: `bot/brain_graph/store.py`, `tests/brain_graph_fake_store.py`

**Interfaces:**
- Consumes: `api.add_episode(store, kind, body, *, author, source_ref, occurred_at)`, `gate.redact`, `gate.secret_values_from_env`.
- Produces:
  - `store.episode_refs(author: str, prefix: str) -> set[str]`: every `source_ref` starting with `prefix`, paged 500.
  - `brain_legacy_memory_import.run(store, memory_dir: Path, *, secret_values) -> dict`: `{"files": n, "imported": n, "unchanged": n, "skipped_index": n}`.
  - Key: `source_ref = "memory/<file>.md#<sha256(body)[:12]>"`, the Plan 2 D2 episode key. An unchanged file is skipped; a changed file is a NEW episode (history).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_brain_legacy_import.py
import importlib.util
from pathlib import Path

from brain_graph_fake_store import FakeStore

ROOT = Path(__file__).resolve().parents[1]


def _mod():
    spec = importlib.util.spec_from_file_location("bl_import", ROOT / "scripts" / "brain_legacy_memory_import.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_imports_new_files_once_and_changed_files_again(tmp_path):
    m, s = _mod(), FakeStore()
    (tmp_path / "MEMORY.md").write_text("- index line")
    (tmp_path / "finding-a.md").write_text("---\nname: finding-a\n---\nbody one")
    assert m.run(s, tmp_path, secret_values=set()) == {"files": 1, "imported": 1, "unchanged": 0, "skipped_index": 1}
    assert m.run(s, tmp_path, secret_values=set())["imported"] == 0
    (tmp_path / "finding-a.md").write_text("---\nname: finding-a\n---\nbody two")
    assert m.run(s, tmp_path, secret_values=set())["imported"] == 1
    kinds = {e["kind"] for e in s.rows["brain_episodes"]}
    assert kinds == {"legacy_memory"} and all(e["author"] == "import" for e in s.rows["brain_episodes"])


def test_a_secret_is_redacted_not_dropped(tmp_path):
    m, s = _mod(), FakeStore()
    (tmp_path / "x.md").write_text("key AKIAZZZZZZZZZZZZZZZZ here")
    m.run(s, tmp_path, secret_values={"AKIAZZZZZZZZZZZZZZZZ"})
    body = "".join(e["body"] for e in s.rows["brain_episodes"])
    assert "AKIAZZZZZZZZZZZZZZZZ" not in body and "[REDACTED" in body
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_brain_legacy_import.py -q -n 2`
Expected: FAIL, `FileNotFoundError` on the script path.

- [ ] **Step 3: Implement**

Add to `RestStore`:

```python
    def episode_refs(self, author, prefix):
        out, offset = set(), 0
        while True:
            rows = self._call("GET", f"brain_episodes?author=eq.{_q(author)}&source_ref=like.{_q(prefix)}*"
                                     f"&select=source_ref&order=id.asc&limit=500&offset={offset}") or []
            out |= {r["source_ref"] for r in rows}
            if len(rows) < 500:
                return out
            offset += 500
```

Add to `FakeStore`:

```python
    def episode_refs(self, author, prefix):
        return {e["source_ref"] for e in self.rows["brain_episodes"]
                if e["author"] == author and str(e.get("source_ref") or "").startswith(prefix)}
```

```python
#!/usr/bin/env python3
# scripts/brain_legacy_memory_import.py
"""One-way import of local memory files as legacy_memory EPISODES (spec 3.3 [R16]; Plan 4). It replaces
the nightly sync_session_findings.py mirror, which rewrote Supabase rows back from the files and so
reverted every status. Never a fact, never a status. Idempotent on memory/<file>.md#<sha12>."""
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MEMORY_DIR = Path.home() / ".claude" / "projects" / "-home-bigpapi-Desktop-Sniper" / "memory"


def run(store, memory_dir, *, secret_values):
    from bot.brain_graph import api
    from bot.brain_graph.gate import redact
    have = store.episode_refs("import", "memory/")
    out = {"files": 0, "imported": 0, "unchanged": 0, "skipped_index": 0}
    for p in sorted(Path(memory_dir).glob("*.md")):
        if p.name in ("MEMORY.md", "HANDOFF.md") or p.name.startswith("HANDOFF-archive"):
            out["skipped_index"] += 1
            continue
        out["files"] += 1
        body = p.read_text(errors="replace")
        ref = "memory/%s#%s" % (p.name, hashlib.sha256(body.encode()).hexdigest()[:12])
        if ref in have:
            out["unchanged"] += 1
            continue
        clean, _n = redact(body, secret_values)
        api.add_episode(store, "legacy_memory", clean, author="import", source_ref=ref, occurred_at=None)
        out["imported"] += 1
    return out


def main():
    from bot.brain_graph import live
    from sniper.util.envfiles import load_env_files
    state = live.state_root(ROOT)
    load_env_files(str(state))
    from bot.brain_graph.gate import secret_values_from_env
    from bot.brain_graph.store import RestStore
    print(run(RestStore(timeout=10), MEMORY_DIR, secret_values=secret_values_from_env(state / ".env")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

HANDOFF.md is skipped on purpose. After Task 7 it is append-only history, its blocks entered through Plan 2's import, and new lines go through `brain_cli.py episode`. Check `api.add_episode`: it must split a body over 1,800 chars into parts [R11]. If it does not, split here with Plan 2's part rule.

- [ ] **Step 4: Run to verify it passes, then prove idempotency can fail**

Run: `python3 -m pytest tests/test_brain_legacy_import.py -q -n 2`
Expected: PASS. Then replace `if ref in have:` with `if False:`, re-run, see the first test FAIL, and restore it.

- [ ] **Step 5: Commit**

```bash
git add -- scripts/brain_legacy_memory_import.py tests/test_brain_legacy_import.py
git commit -m "feat(brain): one-way legacy_memory episode import, the mirror's replacement" -- scripts/brain_legacy_memory_import.py tests/test_brain_legacy_import.py bot/brain_graph/store.py tests/brain_graph_fake_store.py
```

---

### Task 4: `brain_retire_memories.py`, supersede the imported `memories` rows

**Files:**
- Create: `scripts/brain_retire_memories.py`, `tests/test_brain_retire_memories.py`

**Interfaces:**
- Consumes: the Plan 2 import's episode keys, `supabase:memories/<name>#...` for direct rows and `memory/<file>.md#...` for mirrored `session/<stem>` rows; `brain_facts.episode_ids`; `bot.supabase_helper._request(method, path, body, headers, timeout)`.
- Produces:
  - `plan_retire(memories: list[dict], episodes: list[dict], facts: list[dict]) -> list[dict]`: `{"memory_id", "name", "prior_status", "fact_id"}` per active memory that has an imported fact.
  - `apply(request, rows, manifest_path) -> dict`, which writes the manifest BEFORE the first PATCH.
  - `rollback(request, manifest_path) -> dict`.
  - CLI: `brain_retire_memories.py --dry-run | --apply | --rollback <manifest>`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_brain_retire_memories.py
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _mod():
    spec = importlib.util.spec_from_file_location("brm", ROOT / "scripts" / "brain_retire_memories.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


MEMS = [{"id": 1, "name": "session/finding-a", "status": "active"},
        {"id": 2, "name": "direct-note", "status": "active"},
        {"id": 3, "name": "session/never-imported", "status": "active"},
        {"id": 4, "name": "session/finding-old", "status": "superseded"}]
EPS = [{"id": "ep1", "source_ref": "memory/finding-a.md#abc"},
       {"id": "ep2", "source_ref": "supabase:memories/direct-note#def"},
       {"id": "ep4", "source_ref": "memory/finding-old.md#123"}]
FACTS = [{"id": "fa", "episode_ids": ["ep1"], "kind": "rule", "recorded_at": "2"},
         {"id": "fa0", "episode_ids": ["ep1"], "kind": "finding", "recorded_at": "1"},
         {"id": "fb", "episode_ids": ["ep2"], "kind": "finding", "recorded_at": "1"},
         {"id": "fd", "episode_ids": ["ep4"], "kind": "finding", "recorded_at": "1"}]


def test_plan_maps_active_rows_to_their_first_fact_and_skips_the_rest():
    rows = _mod().plan_retire(MEMS, EPS, FACTS)
    assert [(r["memory_id"], r["fact_id"]) for r in rows] == [(1, "fa0"), (2, "fb")]


def test_apply_writes_the_manifest_first_and_rollback_restores(tmp_path):
    m, calls = _mod(), []

    def req(method, path, body=None, headers=None, timeout=10):
        calls.append((method, path, body))
        return []
    man = tmp_path / "manifest.json"
    rows = m.plan_retire(MEMS, EPS, FACTS)
    assert m.apply(req, rows, man) == {"patched": 2}
    assert json.loads(man.read_text())["rows"] == rows
    assert calls[0] == ("PATCH", "memories?id=eq.1", {"status": "superseded", "superseded_by": "fa0"})
    calls.clear()
    assert m.rollback(req, man) == {"restored": 2}
    assert calls[0] == ("PATCH", "memories?id=eq.1", {"status": "active", "superseded_by": None})
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_brain_retire_memories.py -q -n 2`
Expected: FAIL, `FileNotFoundError` on the script path.

- [ ] **Step 3: Implement**

```python
#!/usr/bin/env python3
# scripts/brain_retire_memories.py
"""Spec 4.5: imported memories rows get superseded_by pointing at their fact. Reversible: the manifest
(written BEFORE the first PATCH) holds each row's prior status; --rollback restores it.

    brain_retire_memories.py --dry-run
    brain_retire_memories.py --apply            writes data/brain_retire_manifest_<UTC>.json first
    brain_retire_memories.py --rollback <manifest>"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _key(ref):
    ref = str(ref).split("#", 1)[0]
    if ref.startswith("memory/") and ref.endswith(".md"):
        return "session/" + ref[len("memory/"):-3]
    if ref.startswith("supabase:memories/"):
        return ref[len("supabase:memories/"):]
    return None


def plan_retire(memories, episodes, facts):
    ep_name = {e["id"]: _key(e.get("source_ref")) for e in episodes}
    first = {}
    for f in sorted(facts, key=lambda f: f.get("recorded_at") or ""):
        for ep in f.get("episode_ids") or []:
            first.setdefault(ep_name.get(ep), f["id"])
    return [{"memory_id": m["id"], "name": m["name"], "prior_status": m["status"], "fact_id": first[m["name"]]}
            for m in memories if m.get("status") == "active" and m["name"] in first]


def apply(request, rows, manifest_path):
    Path(manifest_path).write_text(json.dumps({"written_at": datetime.now(timezone.utc).isoformat(), "rows": rows}))
    for r in rows:
        request("PATCH", f"memories?id=eq.{r['memory_id']}", {"status": "superseded", "superseded_by": r["fact_id"]})
    return {"patched": len(rows)}


def rollback(request, manifest_path):
    rows = json.loads(Path(manifest_path).read_text())["rows"]
    for r in rows:
        request("PATCH", f"memories?id=eq.{r['memory_id']}", {"status": r["prior_status"], "superseded_by": None})
    return {"restored": len(rows)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    g.add_argument("--rollback")
    ns = ap.parse_args(argv)
    from bot.brain_graph import live
    from sniper.util.envfiles import load_env_files
    state = live.state_root(ROOT)
    load_env_files(str(state))
    import bot.supabase_helper as sb
    req = sb._request
    if ns.rollback:
        print(rollback(req, ns.rollback))
        return 0

    def pages(path):
        out, off = [], 0
        while True:
            rows = req("GET", f"{path}&limit=500&offset={off}") or []
            out += rows
            if len(rows) < 500:
                return out
            off += 500
    mems = pages("memories?select=id,name,status&order=id.asc")
    eps = pages("brain_episodes?author=eq.import&select=id,source_ref&order=id.asc")
    facts = pages("brain_facts?author=in.(import,operator)&select=id,episode_ids,kind,recorded_at&order=id.asc")
    rows = plan_retire(mems, eps, facts)
    print("memories %d, episodes %d, facts %d -> retire %d" % (len(mems), len(eps), len(facts), len(rows)))
    if ns.apply:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        print(apply(req, rows, state / "data" / f"brain_retire_manifest_{stamp}.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run to verify it passes, then prove the manifest-first order can fail**

Run: `python3 -m pytest tests/test_brain_retire_memories.py -q -n 2`
Expected: PASS. Then move the `write_text` line below the loop, re-run with a `req` that raises on the second call (add that case to the test as `test_manifest_survives_a_failed_patch`), see it FAIL, and restore the order.

- [ ] **Step 5: Commit**

```bash
git add -- scripts/brain_retire_memories.py tests/test_brain_retire_memories.py
git commit -m "feat(brain): retire imported memories rows with superseded_by, manifest-first, rollback" -- scripts/brain_retire_memories.py tests/test_brain_retire_memories.py
```

---

### Task 5: The handover skill becomes `brain_cli.py handover`, and the `.agents` copy is archived

**Files:**
- Rewrite: `.claude/skills/handover/SKILL.md`
- Create: `archive/legacy/agents-handover-skill-2026-09-28/` (a copy of `.agents/skills/handover/`)
- Test: `tests/test_brain_cutover_docs.py`

**Interfaces:**
- Consumes: `views.ONBOARDING`, `brain_cli.py handover <role>`.
- Produces: a skill under 4 KB. Its traps live in the brain (Plan 2 imported them as `trap` facts), not in the file.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_brain_cutover_docs.py
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_handover_skill_runs_the_brain_and_is_small():
    text = (ROOT / ".claude" / "skills" / "handover" / "SKILL.md").read_text()
    assert "scripts/brain_cli.py handover" in text and len(text.encode()) < 4096
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_brain_cutover_docs.py -q -n 0`
Expected: FAIL. Today's file is about 81 KB and does not name the command.

- [ ] **Step 3: Rewrite the skill**

Keep the existing frontmatter (`name: handover` and its `description`) exactly, and replace the body with:

```markdown
# Handover

1. Run `python3 scripts/brain_cli.py handover <role>` (your role in lower case). It re-derives live
   state, records a `handover` episode, lists what you wrote that is still unverified, and prints the
   restart note.
2. Ask a peer to verify what the handover lists as still unverified (a `task` fact is written with
   `brain_cli.py write --kind task`; phase 1 has no separate `task` verb).
3. Print the restart note it gave you, in chat, as the prompt to paste. Never write it to a file.

The note is short on purpose. Traps, SETTLED items and standing rules live in the brain and load
with `brain_cli.py boot <role>`. Do not paste them into the note.
```

Copy the untracked `.agents/skills/handover/` directory to `archive/legacy/agents-handover-skill-2026-09-28/` with `cp -r` and commit the copy. Leave the original in place; removing it is the operator's call [R17].

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/test_brain_cutover_docs.py -q -n 0`
Expected: PASS.

- [ ] **Step 5: Commit (it lands with Task 7; do not merge before the cut-over)**

```bash
git add -- .claude/skills/handover/SKILL.md archive/legacy/agents-handover-skill-2026-09-28 tests/test_brain_cutover_docs.py
git commit -m "docs(brain): handover skill runs brain_cli.py handover; archive the diverged .agents copy" -- .claude/skills/handover/SKILL.md archive/legacy/agents-handover-skill-2026-09-28 tests/test_brain_cutover_docs.py
```

---

### Task 6: CLAUDE.md, MEMORY.md and HANDOFF.md edits [R16]

**Files:**
- Modify: `CLAUDE.md` (tracked), `~/.claude/projects/-home-bigpapi-Desktop-Sniper/memory/MEMORY.md` and `HANDOFF.md` (local, untracked)
- Test: `tests/test_brain_cutover_docs.py`

**Interfaces:**
- Consumes: nothing.
- Produces: pointers only. No rule changes meaning.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_brain_cutover_docs.py`:

```python
def test_claude_md_boots_from_the_brain():
    text = (ROOT / "CLAUDE.md").read_text()
    assert "python3 scripts/brain_cli.py boot <role>" in text
    assert "python3 scripts/brain_cli.py episode" in text
    assert "read `knowledge/` FIRST" not in text
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_brain_cutover_docs.py -q -n 0`
Expected: FAIL on the first assert.

- [ ] **Step 3: Edit CLAUDE.md**

- In "FIRST THING EVERY SESSION", replace "Step 0 — Read live session state" and its code block with:

````markdown
### Step 0 — Boot from the brain (takes seconds)
```bash
python3 scripts/brain_cli.py boot <role>
```
It prints live state (derived, never stored), open work, operator-pending decisions, settled items
and traps for your sections, and your verification quota. HANDOFF.md is append-only history since
the cut-over; do not read it for current state.
````

- Replace "During a session — keep HANDOFF.md current" and its bullets with:

```markdown
### During a session — record what happened
Every significant change is an episode: `python3 scripts/brain_cli.py episode --kind handoff_line "<what, where, SHA>"`.
A durable rule, trap or finding is a fact: `python3 scripts/brain_cli.py write ...` (the gate checks evidence).
```

- In "Knowledge layer", delete the paragraph that begins with the words "For strategy or wallet context, read" and its layout bullets (the directory has been dead since 2026-06-20). Keep the "Source-of-truth docs" list.
- In "MEMORY — Read this every session", replace the HANDOFF read instruction with the `brain_cli.py boot <role>` line above.

- [ ] **Step 4: Edit the two local files (no test; they are untracked)**

- Put these lines at the TOP of `MEMORY.md` and keep the existing index below them (the harness loads the whole file):

```markdown
# Sniper memory: the brain is primary (cut-over 2026-09-2x)
Boot: `python3 scripts/brain_cli.py boot <role>`. Facts: `brain_cli.py write`. History: `brain_cli.py episode`.
New memory files written here are imported nightly as legacy_memory episodes (never facts). The index
below is frozen history.
```

- Append to `HANDOFF.md`, using the `date -u` stamp from the same command:

```markdown
- <UTC stamp> CUT-OVER: this file is append-only history from here. New entries are
  `python3 scripts/brain_cli.py episode --kind handoff_line "..."`; current state is `brain_cli.py boot <role>`.
```

- [ ] **Step 5: Run to verify it passes, and commit CLAUDE.md with Task 7**

Run: `python3 -m pytest tests/test_brain_cutover_docs.py -q -n 0`
Expected: PASS.

```bash
git commit -m "docs(brain): CLAUDE.md boots from brain_cli.py; HANDOFF becomes history" -- CLAUDE.md tests/test_brain_cutover_docs.py
```

---

### Task 7: The cut-over runbook (Alpha, on the operator's yes)

No new code. Gates, each checked and pasted into the HANDOFF line before step 1:

- Plan 2's live import has run, and a second run printed all zeros.
- Plan 3's nightly has written at least 3 `brain_health` rows. The latest `evidence_failure_rate` is within 2 points of the Task 12 baseline, and flips per night are under 20.
- Phase-1 Task 13 (first real use) is reported for all four roles, with nothing missing that the old handover carried.
- `python3 scripts/brain_cli.py boot gamma` returns inside 3 s warm (amendment 23), measured 3 times.
- The book is flat (no reason to wait on it, but the cut-over restarts nothing in the runner).

Steps, in order. Each has its own rollback, so a stop between steps is safe.

- [ ] **1. Merge Tasks 1-6 and announce.** Tell Beta, Lambda and Gamma the hook swap time, because every open session changes at once.
- [ ] **2. Hooks.** Back up `.claude/settings.local.json` as the previous commit. Change the SessionStart command from `python3 scripts/brain_ambient.py map` to `python3 scripts/brain_ambient.py session`, and the UserPromptSubmit command from `python3 scripts/brain_ambient.py rag` to `python3 scripts/brain_ambient.py prompt`. Verify with a fresh session and read the first lines it shows. **Rollback:** revert the two command strings.
- [ ] **3. Cron.** Back up the crontab to `data/crontab.bak.<UTC stamp>`. Replace the 04:15 line
  `sync_session_findings.py ... && embed_memories.py` with
  `15 4 * * * cd /home/bigpapi/Desktop/Sniper && nice -n 19 /home/bigpapi/.pyenv/versions/3.13.0/bin/python3 scripts/brain_legacy_memory_import.py >> /home/bigpapi/logs/brain_sync.log 2>&1`
  (Plan 2's embedding backfill script is appended with `&&` if it exists by then). **Rollback:** restore the backup line.
- [ ] **4. Docs.** Commit Task 6's CLAUDE.md edit and Task 5's skill. Edit the local MEMORY.md and HANDOFF.md. **Rollback:** `git revert` the commit; delete the added MEMORY.md header lines.
- [ ] **5. Retire.** `python3 scripts/brain_retire_memories.py --dry-run`, then read the count against Plan 2's imported memory count. Then `--apply`; the manifest path goes into HANDOFF. **Rollback:** `--rollback <manifest>`.
- [ ] **6. Verify.** Each role boots from the onboarding note only. It reports the boot size, anything it could not find, and whether `prompt` injected facts on a normal question. Record one `crew_report` episode and one HANDOFF line (the last one written the old way).

Half-applied states:
- Hooks swapped but mirror running: the mirror still reverts `memories` statuses. That is harmless to the new brain, which never reads `memories` for facts.
- Mirror stopped but hooks old: sessions boot the old way and new files still import as episodes.
- Retire applied but hooks old: the old `rag` hook reads active memories only, so it shows fewer rows, which is the rollback signal for step 5.

---

### Task 8: The brain chip on the dashboard (Plan 3 N8)

**Files:**
- Modify: `templates/index.html` (after the `shell-health` chip, line ~51), `static/app.js` (after `state.opsHealthWarn = hasOpsWarn || opsRes.status === 'rejected';`, line ~14613)
- Test: `tests/test_brain_cutover_docs.py` (static presence)

- [ ] **Step 1: Write the failing test**

```python
def test_dashboard_has_a_brain_chip_fed_by_ops_health():
    html = (ROOT / "templates" / "index.html").read_text()
    js = (ROOT / "static" / "app.js").read_text()
    assert 'id="shell-brain"' in html and "opsData && opsData.brain" in js
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_brain_cutover_docs.py -q -n 0`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `templates/index.html`, right after the `shell-health` span closes:

```html
                        <span class="shell-chip" id="shell-brain" title="Brain nightly">
                            <span class="shell-chip-icon">B</span>
                            <span class="shell-chip-val">--</span>
                        </span>
```

In `static/app.js`, right after `state.opsHealthWarn = hasOpsWarn || opsRes.status === 'rejected';`:

```javascript
          const brainEl = document.getElementById('shell-brain');
          const brain = opsData && opsData.brain;
          if (brainEl && brain) {
            const val = brainEl.querySelector('.shell-chip-val');
            if (val) val.textContent = brain.state === 'ok' ? `${Math.round(100 * (brain.share_verified || 0))}%` : brain.state;
            brainEl.title = `Brain nightly ${brain.state}` + (brain.age_h != null ? `, ${brain.age_h} h ago` : '')
              + (brain.facts_live != null ? `; ${brain.facts_live} live facts, ${brain.evidence_broken || 0} evidence broken, ${brain.disputed || 0} disputed` : '');
            brainEl.className = 'shell-chip' + (brain.state === 'ok' ? ' ok' : ' warn');
          }
```

The chip never feeds `state.opsHealthWarn`, so it cannot turn the header WARN (money health).

- [ ] **Step 4: Run it, plus the UI harness tests that slice app.js**

Run: `python3 -m pytest tests/test_brain_cutover_docs.py -q -n 0`, then every test that reads `static/app.js`: `grep -rl --include='test_*.py' "static/app.js" tests/ | xargs python3 -m pytest -q -n 6`.
Expected: PASS. Deploy runner and web together (the split-release trap).

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(web): brain nightly chip beside system health, never part of the WARN state" -- templates/index.html static/app.js tests/test_brain_cutover_docs.py
```

## Edge cases (CLAUDE.md rule 5)

1. **Money path:** none. The hooks, scripts and chip never touch entry, exit, sizing or signing. The chip only reads a field and cannot raise the header's WARN.
2. **Fill speed:** untouched. The hooks run in the Claude session process tree, not the runner.
3. **Concurrency:** four sessions get the new hooks at the same instant (Task 7 step 2, announced). The per-session state files are keyed by session id. Retire runs once, manifest first.
4. **Absent / zero / unknown:** Supabase down at boot prints the snapshot with its age, or "BRAIN UNREACHABLE", never an empty brain. A memory with no imported fact is not retired. A `superseded_by` NULL on rollback restores the pre-retire state.
5. **Dependency failure:** the hooks have a 2.5 s budget inside the harness's 3 s. A hung socket is a daemon thread that dies with the hook process. The legacy import on Supabase down exits non-zero in the cron log, and the next night re-runs it idempotently.
6. **Units and identity:** episode keys are `memory/<file>.md#<sha12>`. `session/<stem>` maps to `memory/<stem>.md`; a stem that is not a file name maps to nothing and is not retired.
7. **The sample:** the retire count is over active rows with an imported fact. Rows Plan 2 skipped (the `session/HANDOFF*` mirrors and `user`-type memories) stay active and are listed by the dry run.
8. **Rollback:** per step in Task 7; every step is one line or one manifest. The old `map`/`rag` hooks, `sync_session_findings.py` and `memories` stay in the tree.

## Self-review against the spec

- 3.1 role-free SessionStart, also on compact [R2]: Task 1 (the hook fires on every SessionStart source).
- 3.1 role from the first "You are <Role>" prompt: Task 2.
- 3.1 UserPromptSubmit reads brain_facts with the 2.5 filters: Task 2, via `search.ask`, which applies READ_STATUSES.
- 3.2 onboarding note: `views.ONBOARDING` (phase 1), printed by `brain_cli.py handover` (Task 5).
- 3.3 snapshot fallback: Task 1. Outbox: phase 1. Harness auto-memory [R16]: Tasks 3 and 6.
- 4.4 hooks, handover skill, HANDOFF append-only, mirror stopped, CLAUDE.md edited, `.agents` skill retired [R16, R17]: Tasks 1-7. `strategy_agent_observations` is untouched (spec [R21]).
- 4.5 `superseded_by`, and RAG reads brain_facts only: Tasks 4 and 2.
- 2.6 dashboard tile: Task 8 (data from Plan 3 Task 6).
- Rollback at every phase: Task 7, one line per step.
