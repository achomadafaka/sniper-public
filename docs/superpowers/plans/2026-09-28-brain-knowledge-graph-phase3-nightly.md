# Brain Knowledge Graph, Phase 3 (nightly check and health) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. On this project a "subagent" is a crew session (Beta or Lambda) working in a private worktree; the operator's standing rule forbids spawning worker fleets.

**Goal:** A flag-only nightly job, `scripts/brain_nightly.py`. It re-runs every live fact's machine evidence, queues new contradiction pairs, lists the stale queue, regenerates entity and section summaries, keeps the doctrine mirror in step with CLAUDE.md / AGENTS.md, and writes one `brain_health` row plus a local copy that the web health block reads.

**Architecture:**
- The logic lives in `bot/brain_graph/nightly.py` and is pure except for the store and a `check_fn`, so every rule is tested offline on `FakeStore`.
- One additive migration, 033, adds `brain_judgements`. That table is the record that a candidate pair was judged, so the nightly never re-queues a pair a session has already answered.
- A thin CLI verb (`brain_cli.py judge`) answers the pairs the nightly queues.
- The web reads a JSON file the nightly writes. It never talks to Supabase and never imports `bot.brain_graph`.

**Tech Stack:** Python 3.13 stdlib; the phase-1 package `bot/brain_graph/`; Supabase PostgREST through `RestStore`; `git`; pytest with `FakeStore`.

**Spec:** `docs/superpowers/specs/2026-09-28-brain-knowledge-graph-design.md` (approved), sections 2.4, 2.5 and 2.6, plus 2.1.3 for doctrine. The phase-1 plan and its review amendments: `docs/superpowers/plans/2026-09-28-brain-knowledge-graph-phase1.md`. The Plan 2 draft this depends on is 1b282155 on `lambda/brain-plan2-import`: D4 (archive-tag SHAs are `unchecked`), D10/D11 (judgements and the judge queue), and Task 2's doctrine parser.

<!-- schema:external=brain_facts -->
<!-- schema:external=brain_links -->
<!-- schema:external=brain_entities -->
<!-- schema:external=brain_sections -->
<!-- schema:external=brain_health -->
<!-- schema:external=brain_judgements -->

## Global Constraints

- Everything in the phase-1 plan's Global Constraints holds: sizes, kinds, statuses, evidence results, `verify_by` by kind, server-stamped `recorded_at`, no `.env` values printed, 4 embedding threads at nice 19, offline tests, `-n 2` (plain `-p no:xdist` exits 4 under `pytest.ini -n auto`), explicit-pathspec commits.
- The nightly never deletes. It changes a fact's status only to `evidence_broken`, except for the doctrine supersede that spec 2.1.3 requires. It never sets `verified`, `verified_partial` or `refuted`.
- A check that cannot run is `unchecked`, never `broken` [R6]. A fact goes `evidence_broken` only when the same item is `broken` on TWO consecutive runs (N1).
- Its author string is `cron` (migration 032's AUTHORS list). Doctrine facts it writes use `operator` (spec 2.1.3).
- It pages live facts 500 at a time, under PostgREST's max-rows of 1000, so a short page means the end and never a server cut (phase-1 amendment 3).
- It never runs SQL evidence (N3). The research-load rule therefore holds trivially: no ledger query, no tick or marks table.
- One run at a time: `fcntl.flock` on `data/brain_nightly.lock`, non-blocking. A second run exits 0 with one line.
- Supabase unreachable (`BrainUnavailable`) aborts the run. It still writes `data/brain_health_latest.json` with `"error"` and the partial counts, and exits 4. The web shows a stale or errored brain, never a silent one.
- No module under `sniper/` imports `bot.brain_graph` (the phase-1 isolation test). The web reads the JSON file only.

## Decisions

- **N1. Debounce.** Each evidence item carries `broken_runs`, which counts consecutive `broken` results.
  - `ok` and `pending_push` reset it to 0. `unchecked` leaves it unchanged, because a tool error is not evidence either way.
  - The fact flips to `evidence_broken` when any item reaches 2.
  - These are annotations. `brain_evidence_refs` ignores them, so a nightly patch never voids a verification (migration 032, `brain_facts_before`).
- **N2. No automatic recovery.** Spec 2.4 forbids status changes other than to `evidence_broken`.
  - A broken fact whose items all read `ok` again is listed as `recovered_candidates` in the health row, for a session to re-verify.
  - Every flip records `{id, prior_status}` in the health row. `restore_flips` (Task 2) undoes one run by hand.
- **N3. Query evidence is not executed.** Phase-1 `evidence.check` returns `unchecked` for a `query` item ("checked by a person"). The nightly keeps it that way: re-running a measurement is a person's job, and a nightly SQL job on the funded ledger is research load the operator sees.
- **N4. Candidate pairs.**
  - Pairs are facts that share an entity, the kind GROUP (normative or descriptive) and a key (`contradict.keys`).
  - A pair is skipped if a link of any rel joins its two facts, or if a `brain_judgements` row names it.
  - A new pair gets a `candidate` link from the newer fact to the older one (author `cron`), and the newer fact gets `judge_pending = true`.
  - A version conflict re-reads and retries once, else it is counted and left for the next night.
- **N5. `brain_judgements` (migration 033).**
  - Columns: `(pair_lo uuid, pair_hi uuid, verdict text, author text, created_at timestamptz)`, unique on the ordered pair, RLS deny-anon, service-role INSERT/SELECT only, no UPDATE/DELETE.
  - `api.write_fact` records every judgement it takes at write time, and `brain_cli.py judge` records the rest.
  - Without it, a pair judged `unrelated` leaves no trace and comes back every night.
- **N6. Summaries are extractive.** There is no LLM on this host (spec 2.1.3).
  - An entity summary is `"<n> facts (<v> verified, <u> unverified, <d> disputed). "` followed by the top-ranked statements (`views.rank`), cut at 400 chars.
  - A section summary is the same built over its entities' facts.
  - A summary is patched only when its text changed.
- **N7. Doctrine drift** (needs Plan 2 Task 2's doctrine parser).
  - Current doctrine sentences and live doctrine facts are compared by the normalized statement (the Plan 2 D2 fact key).
  - A fact whose statement is gone is superseded by the best new sentence with word-Jaccard >= 0.6 (`store.supersede`, author `operator`, `judge_pending = false`: the file is the source). With no match it is listed as `doctrine_removed` for a session.
  - A new sentence with no fact is written with `write_fact(interactive=False)`, author `operator`, section `brain/doctrine`.
- **N8. The web health block.**
  - The nightly writes `data/brain_health_latest.json` with tmp plus `os.replace`.
  - `ops_health()` in `sniper/web/server.py` adds `result["brain"]` from it through the pure `sniper/services/brain_health_tile.summary`.
  - It raises NO issue: the header's WARN/DELAYED is for money health.
  - The dashboard line that renders it is Plan 4, Task 8.

## File Structure

| File | Responsibility |
|---|---|
| `migrations/supabase_033_brain_judgements.sql` / `_rollback_brain_judgements.sql` | the judged-pair record (N5) |
| `bot/brain_graph/store.py` (modify) | `live_facts_page`, `links_among`, `patch_row`, `insert_health`, `judged_pairs`, `record_judgement` |
| `tests/brain_graph_fake_store.py` (modify) | the same six methods, mirroring 033 |
| `bot/brain_graph/nightly.py` | `all_live_facts`, `recheck`, `evidence_pass`, `restore_flips`, `sweep_candidates`, `queue_report`, `summaries`, `doctrine_drift`, `health_metrics` |
| `bot/brain_graph/api.py` (modify) | `judge_pair`; `write_fact` records its judgements |
| `scripts/brain_cli.py` (modify) | `judge` verb |
| `scripts/brain_nightly.py` | orchestration, lock, local JSON, `--dry-run` |
| `sniper/services/brain_health_tile.py` | pure reader of the local JSON for the web |
| `sniper/web/server.py` (modify) | `result["brain"]` in `ops_health()` |
| `tests/test_brain_graph_nightly.py`, `tests/test_brain_graph_judgements.py`, `tests/test_brain_health_tile.py` | tests |

---

### Task 1: Migration 033 (`brain_judgements`) and the six store methods

**Files:**
- Create: `migrations/supabase_033_brain_judgements.sql`, `migrations/supabase_033_rollback_brain_judgements.sql`
- Modify: `bot/brain_graph/store.py`, `tests/brain_graph_fake_store.py`
- Test: `tests/test_brain_graph_judgements.py`

**Interfaces:**
- Consumes: `RestStore._call`, `RestStore.insert`, `_q`, `_pg_in` (phase 1); `READ_STATUSES`.
- Produces:
  - `store.live_facts_page(after_id: str | None, limit: int = 500) -> list[dict]`: live, non-observation facts in `id` order.
  - `store.links_among(fact_ids: list[str]) -> list[dict]`: every link touching any of the ids, chunked 100 ids per request.
  - `store.patch_row(table: str, id: str, patch: dict) -> dict`: `brain_entities` and `brain_sections` only.
  - `store.insert_health(metrics: dict) -> dict`.
  - `store.judged_pairs(fact_ids: list[str]) -> set[tuple[str, str]]`: ordered (lo, hi) pairs.
  - `store.record_judgement(a: str, b: str, verdict: str, author: str) -> None`: idempotent; a duplicate counts as present.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_brain_graph_judgements.py
import pytest
from brain_graph_fake_store import FakeStore
from bot.brain_graph.store import BrainRefused


def _fact(s, statement, kind="rule", ents=("e1",)):
    return s.insert("brain_facts", {"kind": kind, "statement": statement, "entity_ids": list(ents),
                                    "evidence": [{"type": "path", "ref": "CLAUDE.md"}], "author": "beta"})


def test_live_facts_page_pages_in_id_order_and_skips_observations():
    s = FakeStore()
    ids = sorted(_fact(s, "rule number %02d is long enough" % i)["id"] for i in range(5))
    s.insert("brain_facts", {"kind": "observation", "statement": "an observation row", "entity_ids": ["e1"],
                             "evidence": [], "episode_ids": ["ep"], "author": "beta"})
    first = s.live_facts_page(None, 3)
    rest = s.live_facts_page(first[-1]["id"], 3)
    assert [f["id"] for f in first + rest] == ids and len(rest) == 2


def test_record_judgement_is_idempotent_and_ordered():
    s = FakeStore()
    s.record_judgement("b", "a", "unrelated", "gamma")
    s.record_judgement("a", "b", "unrelated", "gamma")          # the same pair, other order
    assert s.judged_pairs(["a"]) == {("a", "b")}
    with pytest.raises(BrainRefused):
        s.record_judgement("a", "c", "maybe", "gamma")           # verdict CHECK


def test_links_among_returns_links_touching_any_id():
    s = FakeStore()
    s.insert("brain_links", {"from_id": "x", "from_kind": "fact", "to_id": "y", "to_kind": "fact",
                             "rel": "candidate", "author": "cron"})
    assert len(s.links_among(["y"])) == 1 and s.links_among(["z"]) == []


def test_insert_health_and_patch_row():
    s = FakeStore()
    assert s.insert_health({"facts_live": 3})["metrics"] == {"facts_live": 3}
    sec = s.insert("brain_sections", {"slug": "memecoin", "title": "memecoin"})
    ent = s.insert("brain_entities", {"type": "knob", "name": "MEMECOIN_X", "aliases": [], "section_id": sec["id"]})
    assert s.patch_row("brain_entities", ent["id"], {"summary": "one fact"})["summary"] == "one fact"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_brain_graph_judgements.py -q -n 2`
Expected: FAIL, `AttributeError: 'FakeStore' object has no attribute 'live_facts_page'`.

- [ ] **Step 3: Write the migration pair**

```sql
-- migrations/supabase_033_brain_judgements.sql
-- The record that a contradiction candidate pair was JUDGED (spec 2.1.3; Plan 3 N5). A verdict of
-- 'unrelated' or 'duplicate' leaves no link, so without this row the nightly re-queues the pair forever.
CREATE TABLE IF NOT EXISTS public.brain_judgements (
    pair_lo uuid NOT NULL,
    pair_hi uuid NOT NULL,
    verdict text NOT NULL CHECK (verdict IN ('duplicate','refines','supersedes','contradicts','unrelated')),
    author text NOT NULL CHECK (author IN ('alpha','beta','gamma','lambda','operator','cron','import')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (pair_lo, pair_hi),
    CHECK (pair_lo < pair_hi)
);
ALTER TABLE public.brain_judgements ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS brain_judgements_no_anon ON public.brain_judgements;
CREATE POLICY brain_judgements_no_anon ON public.brain_judgements FOR ALL TO anon, authenticated USING (false);
GRANT SELECT, INSERT ON public.brain_judgements TO service_role;
REVOKE UPDATE, DELETE, TRUNCATE ON public.brain_judgements FROM service_role;
```

```sql
-- migrations/supabase_033_rollback_brain_judgements.sql
DROP TABLE IF EXISTS public.brain_judgements;
```

- [ ] **Step 4: Implement the store methods**

In `bot/brain_graph/store.py`, add to `RestStore`:

```python
    PAGE_MAX = 500          # under PostgREST max-rows (1000): a short page is the end, never a cut

    def live_facts_page(self, after_id=None, limit=500):
        from bot.brain_graph import READ_STATUSES
        limit = min(int(limit), self.PAGE_MAX)
        path = (f"brain_facts?valid_to=is.null&kind=neq.observation&status=in.({','.join(READ_STATUSES)})"
                f"&order=id.asc&limit={limit}")
        if after_id:
            path += f"&id=gt.{_q(after_id)}"
        return self._call("GET", path) or []

    def links_among(self, fact_ids):
        out, ids = {}, list(dict.fromkeys(fact_ids))
        for i in range(0, len(ids), 100):
            chunk = _pg_in(ids[i:i + 100])
            for col in ("from_id", "to_id"):
                for l in self._call("GET", f"brain_links?{col}=in.{chunk}") or []:
                    out[l["id"]] = l
        return list(out.values())

    def patch_row(self, table, id, patch):
        if table not in ("brain_entities", "brain_sections"):
            raise BrainRefused(f"patch_row does not write {table}")
        rows = self._call("PATCH", f"{table}?id=eq.{_q(id)}", patch, prefer="return=representation") or []
        if not rows:
            raise BrainRefused(f"no {table} row {id}")
        return rows[0]

    def insert_health(self, metrics):
        return self.insert("brain_health", {"metrics": metrics})

    def judged_pairs(self, fact_ids):
        out, ids = set(), list(dict.fromkeys(fact_ids))
        for i in range(0, len(ids), 100):
            chunk = _pg_in(ids[i:i + 100])
            for col in ("pair_lo", "pair_hi"):
                for r in self._call("GET", f"brain_judgements?{col}=in.{chunk}&select=pair_lo,pair_hi") or []:
                    out.add((r["pair_lo"], r["pair_hi"]))
        return out

    def record_judgement(self, a, b, verdict, author):
        lo, hi = sorted((a, b))
        try:
            self._call("POST", "brain_judgements", {"pair_lo": lo, "pair_hi": hi, "verdict": verdict,
                                                    "author": author}, prefer="return=minimal")
        except BrainRefused as exc:
            if "duplicate key" not in str(exc) and "23505" not in str(exc):
                raise
```

In `tests/brain_graph_fake_store.py`, add `"brain_judgements"` to the `self.rows` tables and these methods to `FakeStore`:

```python
    def live_facts_page(self, after_id=None, limit=500):
        rows = sorted((f for f in self.rows["brain_facts"] if self._live(f) and f["kind"] != "observation"),
                      key=lambda f: f["id"])
        if after_id:
            rows = [f for f in rows if f["id"] > after_id]
        return [dict(f) for f in rows[:min(limit, 500)]]

    def links_among(self, fact_ids):
        ids = set(fact_ids)
        return [dict(l) for l in self.rows["brain_links"] if l["from_id"] in ids or l["to_id"] in ids]

    def patch_row(self, table, id, patch):
        if table not in ("brain_entities", "brain_sections"):
            raise BrainRefused(f"patch_row does not write {table}")
        for r in self.rows[table]:
            if r["id"] == id:
                r.update(patch)
                return dict(r)
        raise BrainRefused(f"no {table} row {id}")

    def insert_health(self, metrics):
        return self.insert("brain_health", {"metrics": metrics, "run_at": self._now()})

    def judged_pairs(self, fact_ids):
        ids = set(fact_ids)
        return {(j["pair_lo"], j["pair_hi"]) for j in self.rows["brain_judgements"]
                if j["pair_lo"] in ids or j["pair_hi"] in ids}

    def record_judgement(self, a, b, verdict, author):
        from bot.brain_graph import AUTHORS, VERDICTS
        if verdict not in VERDICTS or author not in AUTHORS:
            raise BrainRefused("judgement CHECK")
        lo, hi = sorted((a, b))
        if not any((j["pair_lo"], j["pair_hi"]) == (lo, hi) for j in self.rows["brain_judgements"]):
            self.rows["brain_judgements"].append({"pair_lo": lo, "pair_hi": hi, "verdict": verdict,
                                                  "author": author, "created_at": self._now()})
```

- [ ] **Step 5: Run to verify it passes, then prove one test can fail**

Run: `python3 -m pytest tests/test_brain_graph_judgements.py tests/test_brain_graph_store.py -q -n 2`
Expected: PASS. Then delete the `sorted` in `FakeStore.record_judgement`, re-run, see `test_record_judgement_is_idempotent_and_ordered` FAIL, and restore it.

- [ ] **Step 6: Commit**

```bash
git add -- migrations/supabase_033_brain_judgements.sql migrations/supabase_033_rollback_brain_judgements.sql tests/test_brain_graph_judgements.py
git commit -m "feat(brain): brain_judgements (migration 033) and the store methods the nightly needs" -- migrations/supabase_033_brain_judgements.sql migrations/supabase_033_rollback_brain_judgements.sql bot/brain_graph/store.py tests/brain_graph_fake_store.py tests/test_brain_graph_judgements.py
```

---

### Task 2: The evidence pass with the two-run debounce, and `restore_flips`

**Files:**
- Create: `bot/brain_graph/nightly.py`
- Test: `tests/test_brain_graph_nightly.py`

**Interfaces:**
- Consumes: `store.live_facts_page`, `store.patch_fact`, `store.get`, `VersionConflict`, `BrainRefused`.
- Produces:
  - `nightly.BREAK_AFTER = 2`.
  - `nightly.all_live_facts(store, page=500) -> list[dict]`.
  - `nightly.recheck(fact: dict, checked: list[dict], now_iso: str) -> tuple[list[dict], str]`: the action is `"none"`, `"annotate"` or `"break"`.
  - `nightly.evidence_pass(store, facts, check_fn, now_iso) -> dict`: `{"checks": {"<type>:<result>": n}, "flips": [{"id", "prior_status"}], "patched": n, "version_conflict": n, "recovered_candidates": [id]}`.
  - `nightly.restore_flips(store, flips) -> dict`: `{"restored": n, "skipped": n, "refused": n}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_brain_graph_nightly.py
from brain_graph_fake_store import FakeStore
from bot.brain_graph import nightly

NOW = "2026-09-29T08:40:00+00:00"


def _fact(s, status="unverified", evidence=None, author="beta"):
    f = s.insert("brain_facts", {"kind": "rule", "statement": "a rule long enough to pass", "entity_ids": ["e1"],
                                 "evidence": evidence or [{"type": "path", "ref": "gone.py"}], "author": author})
    if status != "unverified":
        f = s.patch_fact(f["id"], f["version"], {"status": status, "verified_by": "gamma"})
    return f


def _checker(result):
    return lambda items: [{**i, "check": result, "check_reason": result} for i in items]


def test_one_broken_night_only_annotates():
    s = FakeStore()
    f = _fact(s)
    out = nightly.evidence_pass(s, [f], _checker("broken"), NOW)
    g = s.get("brain_facts", f["id"])
    assert g["status"] == "unverified" and g["evidence"][0]["broken_runs"] == 1 and out["flips"] == []


def test_two_consecutive_broken_nights_flip_and_keep_the_verifier():
    s = FakeStore()
    f = _fact(s, status="verified")
    nightly.evidence_pass(s, [s.get("brain_facts", f["id"])], _checker("broken"), NOW)
    out = nightly.evidence_pass(s, [s.get("brain_facts", f["id"])], _checker("broken"), NOW)
    g = s.get("brain_facts", f["id"])
    assert g["status"] == "evidence_broken" and g["verified_by"] == "gamma"      # annotations kept refs
    assert out["flips"] == [{"id": f["id"], "prior_status": "verified"}]


def test_ok_resets_and_unchecked_keeps_the_count():
    s = FakeStore()
    f = _fact(s)
    nightly.evidence_pass(s, [s.get("brain_facts", f["id"])], _checker("broken"), NOW)
    nightly.evidence_pass(s, [s.get("brain_facts", f["id"])], _checker("unchecked"), NOW)
    assert s.get("brain_facts", f["id"])["evidence"][0]["broken_runs"] == 1          # tool error: no change
    nightly.evidence_pass(s, [s.get("brain_facts", f["id"])], _checker("ok"), NOW)
    assert s.get("brain_facts", f["id"])["evidence"][0]["broken_runs"] == 0


def test_unchanged_annotations_write_nothing():
    s = FakeStore()
    f = _fact(s)
    nightly.evidence_pass(s, [s.get("brain_facts", f["id"])], _checker("ok"), NOW)
    v = s.get("brain_facts", f["id"])["version"]
    out = nightly.evidence_pass(s, [s.get("brain_facts", f["id"])], _checker("ok"), NOW)
    assert s.get("brain_facts", f["id"])["version"] == v and out["patched"] == 0


def test_a_broken_fact_reading_ok_again_is_listed_not_healed():
    s = FakeStore()
    f = _fact(s)
    for _ in range(2):
        nightly.evidence_pass(s, [s.get("brain_facts", f["id"])], _checker("broken"), NOW)
    out = nightly.evidence_pass(s, [s.get("brain_facts", f["id"])], _checker("ok"), NOW)
    assert s.get("brain_facts", f["id"])["status"] == "evidence_broken"
    assert out["recovered_candidates"] == [f["id"]]


def test_restore_flips_puts_the_prior_status_back():
    s = FakeStore()
    f = _fact(s, status="verified")
    for _ in range(2):
        out = nightly.evidence_pass(s, [s.get("brain_facts", f["id"])], _checker("broken"), NOW)
    assert nightly.restore_flips(s, out["flips"]) == {"restored": 1, "skipped": 0, "refused": 0}
    assert s.get("brain_facts", f["id"])["status"] == "verified"


def test_all_live_facts_reads_every_page():
    s = FakeStore()
    for _ in range(7):
        _fact(s)
    assert len(nightly.all_live_facts(s, page=3)) == 7
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_brain_graph_nightly.py -q -n 2`
Expected: FAIL, `ImportError: cannot import name 'nightly'`.

- [ ] **Step 3: Implement**

```python
# bot/brain_graph/nightly.py
"""The nightly brain check (spec 2.4; Plan 3). Flag-only: it annotates evidence, flips a fact to
evidence_broken after two consecutive broken runs, queues contradiction pairs, and reports. It never
deletes and never sets a verified status."""
import collections

from bot.brain_graph.store import BrainRefused, VersionConflict

BREAK_AFTER = 2
LIVE_BREAKABLE = ("unverified", "verified", "verified_partial", "disputed")


def all_live_facts(store, page=500):
    out, after = [], None
    while True:
        rows = store.live_facts_page(after, page)
        out += rows
        if len(rows) < page:
            return out
        after = rows[-1]["id"]


def _sig(items):
    return [(e.get("type"), e.get("ref"), e.get("check"), int(e.get("broken_runs") or 0)) for e in items or []]


def recheck(fact, checked, now_iso):
    prev = {(e.get("type"), e.get("ref")): e for e in fact.get("evidence") or []}
    out, hard = [], False
    for e in checked:
        runs = int(prev.get((e.get("type"), e.get("ref")), {}).get("broken_runs") or 0)
        if e["check"] == "broken":
            runs += 1
        elif e["check"] in ("ok", "pending_push"):
            runs = 0
        out.append({**e, "broken_runs": runs, "checked_at": now_iso})
        hard = hard or runs >= BREAK_AFTER
    if hard and fact["status"] in LIVE_BREAKABLE:
        return out, "break"
    return out, ("annotate" if _sig(out) != _sig(fact.get("evidence")) else "none")


def evidence_pass(store, facts, check_fn, now_iso):
    stats = collections.Counter()
    flips, recovered, patched, conflicts = [], [], 0, 0
    for f in facts:
        if f["kind"] == "observation" or not f.get("evidence"):
            continue
        new, action = recheck(f, check_fn(f["evidence"]), now_iso)
        for e in new:
            stats[f"{e.get('type')}:{e['check']}"] += 1
        if f["status"] == "evidence_broken" and all(e["check"] == "ok" for e in new):
            recovered.append(f["id"])
        if action == "none":
            continue
        patch = {"evidence": new}
        if action == "break":
            patch["status"] = "evidence_broken"
        try:
            store.patch_fact(f["id"], f["version"], patch)
        except VersionConflict:
            conflicts += 1
            continue
        patched += 1
        if action == "break":
            flips.append({"id": f["id"], "prior_status": f["status"]})
    return {"checks": dict(stats), "flips": flips, "patched": patched, "version_conflict": conflicts,
            "recovered_candidates": recovered}


def restore_flips(store, flips):
    out = {"restored": 0, "skipped": 0, "refused": 0}
    for fl in flips:
        f = store.get("brain_facts", fl["id"])
        if not f or f["status"] != "evidence_broken":
            out["skipped"] += 1
            continue
        try:
            store.patch_fact(f["id"], f["version"], {"status": fl["prior_status"]})
            out["restored"] += 1
        except (BrainRefused, VersionConflict):
            out["refused"] += 1
    return out
```

- [ ] **Step 4: Run to verify it passes, then prove the debounce can fail**

Run: `python3 -m pytest tests/test_brain_graph_nightly.py -q -n 2`
Expected: PASS. Then set `BREAK_AFTER = 1`, re-run, see `test_one_broken_night_only_annotates` FAIL, and restore it.

- [ ] **Step 5: Commit**

```bash
git add -- bot/brain_graph/nightly.py tests/test_brain_graph_nightly.py
git commit -m "feat(brain): nightly evidence pass, two-run debounce, restore_flips" -- bot/brain_graph/nightly.py tests/test_brain_graph_nightly.py
```

---

### Task 3: Candidate sweep, `judge_pair`, and write-time judgements recorded

**Files:**
- Modify: `bot/brain_graph/nightly.py`, `bot/brain_graph/api.py`, `scripts/brain_cli.py`
- Test: `tests/test_brain_graph_nightly.py`, `tests/test_brain_graph_judgements.py`, `tests/test_brain_graph_cli.py`

**Interfaces:**
- Consumes: `contradict.keys`, `bot.brain_graph.group_of`, `store.links_among`, `store.judged_pairs`, `store.record_judgement`, `store.patch_fact`, `store.insert`.
- Produces:
  - `nightly.sweep_candidates(store, facts, author="cron") -> dict`: `{"pairs_new": n, "pairs_seen": n, "facts_flagged": n, "version_conflict": n}`.
  - `api.judge_pair(store, newer_id, older_id, verdict, *, role) -> dict`.
  - `write_fact` calls `store.record_judgement(new_id, cand_id, verdict, author)` for every judged candidate.
  - CLI: `brain_cli.py judge <newer_id> <older_id> <verdict> --role <role>`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_brain_graph_nightly.py`:

```python
def _keyed(s, statement, kind="rule", ents=("e1",)):
    return s.insert("brain_facts", {"kind": kind, "statement": statement, "entity_ids": list(ents),
                                    "evidence": [{"type": "path", "ref": "CLAUDE.md"}], "author": "beta"})


def test_sweep_links_a_new_pair_once_and_flags_the_newer_fact():
    s = FakeStore()
    old = _keyed(s, "Never run `git stash` in the shared checkout.")
    new = _keyed(s, "Use `git stash` before a checkout.")
    out = nightly.sweep_candidates(s, nightly.all_live_facts(s))
    assert out["pairs_new"] == 1
    link = s.rows["brain_links"][0]
    assert (link["from_id"], link["to_id"], link["rel"], link["author"]) == (new["id"], old["id"], "candidate", "cron")
    assert s.get("brain_facts", new["id"])["judge_pending"] is True
    assert nightly.sweep_candidates(s, nightly.all_live_facts(s))["pairs_new"] == 0      # idempotent


def test_sweep_skips_judged_pairs_and_other_groups():
    s = FakeStore()
    a = _keyed(s, "Never run `git stash` in the shared checkout.")
    b = _keyed(s, "Use `git stash` before a checkout.")
    s.record_judgement(a["id"], b["id"], "unrelated", "gamma")
    _keyed(s, "Measured `git stash` loss on 3 files.", kind="measurement")
    assert nightly.sweep_candidates(s, nightly.all_live_facts(s))["pairs_new"] == 0
```

Append to `tests/test_brain_graph_judgements.py`:

```python
from bot.brain_graph import api


def test_judge_pair_contradicts_disputes_both_and_records_the_verdict():
    s = FakeStore()
    a = _fact(s, "Never run `git stash` in the shared checkout.")
    b = _fact(s, "Use `git stash` before a checkout.")
    s.patch_fact(b["id"], b["version"], {"judge_pending": True})
    out = api.judge_pair(s, b["id"], a["id"], "contradicts", role="gamma")
    assert out["verdict"] == "contradicts"
    assert {s.get("brain_facts", x)["status"] for x in (a["id"], b["id"])} == {"disputed"}
    assert s.judged_pairs([a["id"]]) == {tuple(sorted((a["id"], b["id"])))}
    assert s.get("brain_facts", b["id"])["judge_pending"] is False


def test_judge_pair_supersedes_closes_the_older_fact():
    s = FakeStore()
    a = _fact(s, "The chase cap is 30 percent on Solana.")
    b = _fact(s, "The chase cap is 20 percent on Solana.")
    api.judge_pair(s, b["id"], a["id"], "supersedes", role="gamma")
    old = s.get("brain_facts", a["id"])
    assert old["status"] == "superseded" and old["valid_to"] is not None


def test_judge_pair_refuses_an_unknown_verdict():
    s = FakeStore()
    a, b = _fact(s, "first rule long enough"), _fact(s, "second rule long enough")
    with pytest.raises(ValueError):
        api.judge_pair(s, b["id"], a["id"], "maybe", role="gamma")
```

Append to `tests/test_brain_graph_cli.py`, following that file's existing `main(argv, store=...)` pattern:

```python
def test_cli_judge_records_a_verdict(tmp_path):
    cli = _cli()                                   # the file's own loader (importlib, scripts/brain_cli.py)
    s = FakeStore()
    ev = [{"type": "path", "ref": "CLAUDE.md"}]
    a = s.insert("brain_facts", {"kind": "rule", "statement": "first rule long enough", "entity_ids": ["e"],
                                 "evidence": ev, "author": "beta"})
    b = s.insert("brain_facts", {"kind": "rule", "statement": "second rule long enough", "entity_ids": ["e"],
                                 "evidence": ev, "author": "beta"})
    rc = cli.main(["judge", b["id"], a["id"], "unrelated", "--role", "gamma"], store=s,
                  outbox=tmp_path / "o.jsonl", snapshot=tmp_path / "s.json")
    assert rc == 0 and s.judged_pairs([a["id"]])
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_brain_graph_nightly.py tests/test_brain_graph_judgements.py tests/test_brain_graph_cli.py -q -n 2`
Expected: FAIL, `AttributeError: module 'bot.brain_graph.nightly' has no attribute 'sweep_candidates'`.

- [ ] **Step 3: Implement the sweep**

Append to `bot/brain_graph/nightly.py`:

```python
from bot.brain_graph import group_of
from bot.brain_graph.contradict import keys


def sweep_candidates(store, facts, author="cron"):
    """Spec 2.4: re-run the candidate stage across each entity; queue NEW pairs for a session."""
    ids = [f["id"] for f in facts]
    linked = {(l["from_id"], l["to_id"]) for l in store.links_among(ids)}
    linked |= {(b, a) for a, b in linked}
    judged = store.judged_pairs(ids)
    by_group_key = collections.defaultdict(list)
    for f in facts:
        grp = group_of(f["kind"])
        if grp not in ("normative", "descriptive"):
            continue
        for ent in f.get("entity_ids") or []:
            for k in keys(f["statement"]):
                by_group_key[(ent, grp, k)].append(f)
    new, seen = [], set()
    for members in by_group_key.values():
        members = sorted({m["id"]: m for m in members}.values(), key=lambda m: m.get("recorded_at") or "")
        for i, older in enumerate(members):
            for newer in members[i + 1:]:
                pair = tuple(sorted((older["id"], newer["id"])))
                if pair in seen:
                    continue
                seen.add(pair)
                if (older["id"], newer["id"]) in linked or pair in judged:
                    continue
                new.append((older, newer))
    flagged, conflicts = set(), 0
    for older, newer in new:
        store.insert("brain_links", {"from_id": newer["id"], "from_kind": "fact", "to_id": older["id"],
                                     "to_kind": "fact", "rel": "candidate", "author": author})
        if newer["id"] in flagged:
            continue
        for attempt in range(2):
            cur = store.get("brain_facts", newer["id"])
            try:
                store.patch_fact(cur["id"], cur["version"], {"judge_pending": True})
                flagged.add(newer["id"])
                break
            except VersionConflict:
                if attempt:
                    conflicts += 1
    return {"pairs_new": len(new), "pairs_seen": len(seen), "facts_flagged": len(flagged),
            "version_conflict": conflicts}
```

- [ ] **Step 4: Implement `judge_pair` and record write-time judgements**

Append to `bot/brain_graph/api.py`:

```python
def judge_pair(store, newer_id, older_id, verdict, *, role):
    """A session's verdict on one candidate pair (spec 2.1.3 outcomes; Plan 3 N5)."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}")
    newer, older = store.get("brain_facts", newer_id), store.get("brain_facts", older_id)
    if not newer or not older:
        raise BrainRefused("no such fact")
    now = datetime.now(timezone.utc).isoformat()
    if verdict == "duplicate":
        store.patch_fact(newer["id"], newer["version"], {"status": "retracted", "valid_to": now, "expired_at": now})
        _link(store, newer["id"], older["id"], "same_as", role)
    elif verdict == "refines":
        _link(store, newer["id"], older["id"], "derived_from", role)
    elif verdict == "supersedes":
        store.patch_fact(older["id"], older["version"],
                         {"status": "superseded", "valid_to": newer.get("valid_from") or now, "expired_at": now})
        _link(store, newer["id"], older["id"], "supersedes", role)
    elif verdict == "contradicts":
        for f in (older, newer):
            cur = store.get("brain_facts", f["id"])
            store.patch_fact(cur["id"], cur["version"], {"status": "disputed"})
        _link(store, newer["id"], older["id"], "contradicts", role)
    store.record_judgement(newer["id"], older["id"], verdict, role)
    cur = store.get("brain_facts", newer["id"])
    open_pairs = [l for l in store.links_among([newer["id"]])
                  if l["rel"] == "candidate" and l["from_id"] == newer["id"]
                  and tuple(sorted((l["from_id"], l["to_id"]))) not in store.judged_pairs([newer["id"]])]
    if cur["judge_pending"] and not open_pairs and cur["valid_to"] is None:
        store.patch_fact(cur["id"], cur["version"], {"judge_pending": False})
    return {"verdict": verdict, "newer": newer_id, "older": older_id}
```

Add `from datetime import datetime, timezone` at the top of `api.py` if it is not already imported. Inside `write_fact`, wherever an interactive judgement is applied to candidate `c` for the new fact `new_id`, add the record right after the outcome is applied:

```python
            store.record_judgement(new_id, c["id"], judgements[c["id"]], author)
```

A `duplicate` verdict at write time writes no new fact, so there is no pair to record; skip it there.

- [ ] **Step 5: Add the CLI verb**

In `scripts/brain_cli.py`, next to the `verify` parser:

```python
    j = sub.add_parser("judge", help="judge one candidate pair")
    j.add_argument("newer")
    j.add_argument("older")
    j.add_argument("verdict", choices=["duplicate", "refines", "supersedes", "contradicts", "unrelated"])
    j.add_argument("--role", required=True)
```

and in `main`'s dispatch, beside `verify`:

```python
        if ns.cmd == "judge":
            print(json.dumps(api.judge_pair(store, ns.newer, ns.older, ns.verdict, role=ns.role)))
            return 0
```

- [ ] **Step 6: Run to verify it passes, then prove the idempotency guard can fail**

Run: `python3 -m pytest tests/test_brain_graph_nightly.py tests/test_brain_graph_judgements.py tests/test_brain_graph_cli.py tests/test_brain_graph_api.py -q -n 2`
Expected: PASS. Then remove `or pair in judged` from `sweep_candidates`, re-run, see `test_sweep_skips_judged_pairs_and_other_groups` FAIL, and restore it.

- [ ] **Step 7: Commit**

```bash
git add -- bot/brain_graph/nightly.py bot/brain_graph/api.py scripts/brain_cli.py tests/test_brain_graph_nightly.py tests/test_brain_graph_judgements.py tests/test_brain_graph_cli.py
git commit -m "feat(brain): nightly candidate sweep, judge_pair, judgements recorded at write time" -- bot/brain_graph/nightly.py bot/brain_graph/api.py scripts/brain_cli.py tests/test_brain_graph_nightly.py tests/test_brain_graph_judgements.py tests/test_brain_graph_cli.py
```

---

### Task 4: Queue report, extractive summaries, and the health metrics

**Files:**
- Modify: `bot/brain_graph/nightly.py`
- Test: `tests/test_brain_graph_nightly.py`

**Interfaces:**
- Consumes: `views.rank`, `store.sections`, `store.patch_row`, the entity rows (`store.get("brain_entities", id)`).
- Produces:
  - `nightly.queue_report(facts, now_iso) -> dict`: `{"disputed_over_24h": [id], "unverified_over_72h": n, "judge_pending": n}`.
  - `nightly.summary_text(facts, now_iso) -> str`, at most 400 chars.
  - `nightly.summaries(store, facts, now_iso) -> dict`: `{"entities_patched": n, "sections_patched": n}`.
  - `nightly.health_metrics(facts, evidence_out, sweep_out, queue_out, now_iso) -> dict`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_brain_graph_nightly.py`:

```python
def _row(i, status="unverified", kind="rule", recorded="2026-09-28T00:00:00+00:00", jp=False):
    return {"id": "f%d" % i, "kind": kind, "status": status, "recorded_at": recorded, "judge_pending": jp,
            "statement": "statement number %d is long" % i, "entity_ids": ["e1"],
            "verify_by": "2026-10-30T00:00:00+00:00", "evidence": []}


def test_queue_report_ages():
    facts = [_row(1, "disputed", recorded="2026-09-27T00:00:00+00:00"), _row(2, "disputed"),
             _row(3, recorded="2026-09-25T00:00:00+00:00"), _row(4, jp=True)]
    out = nightly.queue_report(facts, "2026-09-29T08:00:00+00:00")
    assert out == {"disputed_over_24h": ["f1", "f2"], "unverified_over_72h": 1, "judge_pending": 1}


def test_summary_text_is_capped_and_counts_status():
    facts = [_row(i, "verified" if i < 2 else "unverified") for i in range(20)]
    text = nightly.summary_text(facts, "2026-09-29T08:00:00+00:00")
    assert text.startswith("20 facts (2 verified, 18 unverified, 0 disputed).") and len(text) <= 400


def test_health_metrics_shares_and_ages():
    facts = [_row(1, "verified"), _row(2), _row(3, "evidence_broken"), _row(4, "disputed")]
    m = nightly.health_metrics(facts, {"checks": {"path:broken": 1, "path:ok": 3}, "flips": [],
                                       "recovered_candidates": []},
                               {"pairs_new": 0}, {"disputed_over_24h": []}, "2026-09-29T08:00:00+00:00")
    assert m["facts_live"] == 4 and m["share_verified"] == 0.25
    assert m["evidence_failure_rate"] == {"path": 0.25} and m["disputed"] == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_brain_graph_nightly.py -q -n 2`
Expected: FAIL, `AttributeError: ... no attribute 'queue_report'`.

- [ ] **Step 3: Implement**

Append to `bot/brain_graph/nightly.py`:

```python
from datetime import datetime


def _age_h(iso, now_iso):
    return (datetime.fromisoformat(now_iso) - datetime.fromisoformat(iso)).total_seconds() / 3600.0


def _pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * (len(xs) - 1) + 0.5))] if xs else None


def queue_report(facts, now_iso):
    return {"disputed_over_24h": sorted(f["id"] for f in facts
                                        if f["status"] == "disputed" and _age_h(f["recorded_at"], now_iso) > 24),
            "unverified_over_72h": sum(1 for f in facts
                                       if f["status"] == "unverified" and _age_h(f["recorded_at"], now_iso) > 72),
            "judge_pending": sum(1 for f in facts if f.get("judge_pending"))}


def summary_text(facts, now_iso):
    from bot.brain_graph.views import rank
    c = collections.Counter(f["status"] for f in facts)
    head = "%d facts (%d verified, %d unverified, %d disputed)." % (
        len(facts), c["verified"] + c["verified_partial"], c["unverified"], c["disputed"])
    out = head
    for f in rank(facts, set(), now_iso):
        piece = " " + f["statement"]
        if len(out) + len(piece) > 400:
            break
        out += piece
    return out[:400]


def summaries(store, facts, now_iso):
    by_ent = collections.defaultdict(list)
    for f in facts:
        for e in f.get("entity_ids") or []:
            by_ent[e].append(f)
    ents_patched, by_section = 0, collections.defaultdict(list)
    for eid, fs in by_ent.items():
        ent = store.get("brain_entities", eid)
        if not ent:
            continue
        by_section[ent.get("section_id")].extend(fs)
        text = summary_text(fs, now_iso)
        if ent.get("summary") != text:
            store.patch_row("brain_entities", eid, {"summary": text})
            ents_patched += 1
    secs_patched = 0
    for sec in store.sections():
        fs = list({f["id"]: f for f in by_section.get(sec["id"], [])}.values())
        text = summary_text(fs, now_iso) if fs else None
        if fs and sec.get("summary") != text:
            store.patch_row("brain_sections", sec["id"], {"summary": text, "summary_at": now_iso})
            secs_patched += 1
    return {"entities_patched": ents_patched, "sections_patched": secs_patched}


def health_metrics(facts, evidence_out, sweep_out, queue_out, now_iso):
    c = collections.Counter(f["status"] for f in facts)
    unv_age = [_age_h(f["recorded_at"], now_iso) for f in facts if f["status"] == "unverified"]
    disp_age = [_age_h(f["recorded_at"], now_iso) for f in facts if f["status"] == "disputed"]
    by_type = collections.defaultdict(lambda: [0, 0])
    for key, n in (evidence_out.get("checks") or {}).items():
        typ, res = key.split(":", 1)
        if res in ("ok", "broken"):
            by_type[typ][1] += n
            by_type[typ][0] += n if res == "broken" else 0
    return {"run_at": now_iso, "facts_live": len(facts), "by_status": dict(c),
            "share_verified": round((c["verified"] + c["verified_partial"]) / len(facts), 4) if facts else None,
            "unverified_age_h_p50": _pct(unv_age, .5), "unverified_age_h_p90": _pct(unv_age, .9),
            "disputed": c["disputed"], "disputed_age_h_max": max(disp_age) if disp_age else None,
            "past_verify_by": sum(1 for f in facts if f.get("verify_by") and f["verify_by"] < now_iso),
            "evidence_failure_rate": {t: round(b / n, 4) for t, (b, n) in by_type.items() if n},
            "evidence_checks": evidence_out.get("checks"), "flips": evidence_out.get("flips"),
            "recovered_candidates": evidence_out.get("recovered_candidates"),
            "candidates": sweep_out, "queue": queue_out}
```

- [ ] **Step 4: Run to verify it passes, then prove the cap can fail**

Run: `python3 -m pytest tests/test_brain_graph_nightly.py -q -n 2`
Expected: PASS. Then change `> 400` to `> 4000` in `summary_text`, re-run, see `test_summary_text_is_capped_and_counts_status` FAIL, and restore it.

- [ ] **Step 5: Commit**

```bash
git add -- bot/brain_graph/nightly.py tests/test_brain_graph_nightly.py
git commit -m "feat(brain): nightly queue report, extractive summaries, health metrics" -- bot/brain_graph/nightly.py tests/test_brain_graph_nightly.py
```

---

### Task 5: Doctrine drift (after Plan 2 Task 2 merges)

**Files:**
- Modify: `bot/brain_graph/nightly.py`
- Test: `tests/test_brain_graph_nightly.py`

**Interfaces:**
- Consumes:
  - Plan 2 Task 2's doctrine parser, `bot.brain_graph.import_sources.doctrine_rules(text: str, source: str) -> list[dict]` with keys `statement`, `line`, `source`. If Plan 2 lands it under another name, use that name and update this block before building.
  - `store.supersede`, `api.write_fact(..., interactive=False)`.
- Produces: `nightly.doctrine_drift(store, facts, current_rules, *, repo, secret_values) -> dict`: `{"superseded": n, "added": n, "doctrine_removed": [id], "refused": n}`.

- [ ] **Step 1: Write the failing test**

```python
def test_doctrine_drift_supersedes_a_changed_line_and_lists_a_removed_one():
    s = FakeStore()
    sec = s.insert("brain_sections", {"slug": "brain/doctrine", "title": "doctrine"})
    ent = s.insert("brain_entities", {"type": "doctrine", "name": "CLAUDE.md", "aliases": [], "section_id": sec["id"]})

    def doc(stmt):
        return s.insert("brain_facts", {"kind": "rule", "statement": stmt, "entity_ids": [ent["id"]],
                                        "evidence": [{"type": "path", "ref": "CLAUDE.md:10"}], "author": "operator"})
    changed = doc("Never use git stash in the shared checkout ever.")
    gone = doc("Read knowledge first before grepping the DB.")
    now_rules = [{"statement": "Never use git stash in the shared checkout at all.", "line": 11, "source": "CLAUDE.md"}]
    out = nightly.doctrine_drift(s, nightly.all_live_facts(s), now_rules, repo=".", secret_values=set())
    assert out["superseded"] == 1 and out["doctrine_removed"] == [gone["id"]]
    assert s.get("brain_facts", changed["id"])["status"] == "superseded"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_brain_graph_nightly.py -k doctrine -q -n 2`
Expected: FAIL, `AttributeError: ... no attribute 'doctrine_drift'`.

- [ ] **Step 3: Implement**

```python
import re

_W = re.compile(r"[a-z0-9_]+")


def _norm(s):
    return " ".join(str(s).split())


def _jac(a, b):
    wa, wb = set(_W.findall(a.lower())), set(_W.findall(b.lower()))
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def doctrine_drift(store, facts, current_rules, *, repo, secret_values):
    """Spec 2.1.3: the files are the source; a doctrine fact whose line changed is superseded."""
    from bot.brain_graph import api
    docs = [f for f in facts if f["author"] == "operator" and f["kind"] == "rule"
            and any(str(e.get("ref", "")).split(":")[0] in ("CLAUDE.md", "AGENTS.md") for e in f.get("evidence") or [])]
    have = {_norm(f["statement"]): f for f in docs}
    now = {_norm(r["statement"]): r for r in current_rules}
    gone = [f for k, f in have.items() if k not in now]
    fresh = [r for k, r in now.items() if k not in have]
    out = {"superseded": 0, "added": 0, "doctrine_removed": [], "refused": 0}
    used = set()
    for f in gone:
        best = max(((_jac(f["statement"], r["statement"]), i) for i, r in enumerate(fresh) if i not in used),
                   default=(0.0, None))
        if best[0] < 0.6:
            out["doctrine_removed"].append(f["id"])
            continue
        r = fresh[best[1]]
        used.add(best[1])
        store.supersede(f["id"], f["version"], {
            "kind": "rule", "statement": r["statement"], "entity_ids": f["entity_ids"], "polarity": f.get("polarity", 0),
            "evidence": [{"type": "path", "ref": "%s:%d" % (r["source"], r["line"])}], "author": "operator",
            "judge_pending": False})
        out["superseded"] += 1
    for i, r in enumerate(fresh):
        if i in used:
            continue
        res = api.write_fact(store, {"kind": "rule", "statement": r["statement"], "author": "operator",
                                     "section": "brain/doctrine",
                                     "entities": [{"type": "doctrine", "name": r["source"]}],
                                     "evidence": [{"type": "path", "ref": "%s:%d" % (r["source"], r["line"])}]},
                             repo=repo, interactive=False, secret_values=secret_values)
        out["added" if res.get("status") != "refused" else "refused"] += 1
    return out
```

- [ ] **Step 4: Run to verify it passes, then prove the threshold can fail**

Run: `python3 -m pytest tests/test_brain_graph_nightly.py -q -n 2`
Expected: PASS. Then change `< 0.6` to `< 0.99`, re-run, see the doctrine test FAIL (the changed line becomes "removed"), and restore it.

- [ ] **Step 5: Commit**

```bash
git add -- bot/brain_graph/nightly.py tests/test_brain_graph_nightly.py
git commit -m "feat(brain): nightly doctrine drift, supersede a changed CLAUDE.md/AGENTS.md rule" -- bot/brain_graph/nightly.py tests/test_brain_graph_nightly.py
```

---

### Task 6: `scripts/brain_nightly.py`, the local health file and the web block

**Files:**
- Create: `scripts/brain_nightly.py`, `sniper/services/brain_health_tile.py`, `tests/test_brain_health_tile.py`
- Modify: `sniper/web/server.py` (`ops_health()`, before its `if result["issues"]` line)
- Test: `tests/test_brain_graph_nightly.py` (`run` against `FakeStore`), `tests/test_brain_health_tile.py`

**Interfaces:**
- Consumes: every `nightly.*` function above; `evidence.check_all`, `evidence.fetch_once`; `bot.brain_graph.gate.secret_values_from_env`; `live.state_root`; `RestStore(timeout=10)`.
- Produces:
  - `brain_nightly.run(store, repo, *, check_fn, now_iso, rules=None, dry_run=False, secret_values=frozenset()) -> dict`: the metrics.
  - `brain_nightly.write_local(path, payload) -> None`: tmp plus `os.replace`.
  - CLI: `brain_nightly.py [--dry-run] [--restore <health_json_path>]`. Exit 0 ok, 3 lock held, 4 brain unavailable.
  - `brain_health_tile.summary(path, now_ts) -> dict`: `{"state": "ok"|"stale"|"error"|"missing", "age_h", "facts_live", "share_verified", "evidence_broken", "disputed", "queue"}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_brain_health_tile.py
import json

from sniper.services import brain_health_tile as tile


def test_missing_file_reads_missing(tmp_path):
    assert tile.summary(tmp_path / "none.json", 0)["state"] == "missing"


def test_fresh_ok_and_stale_after_36h(tmp_path):
    p = tmp_path / "b.json"
    p.write_text(json.dumps({"written_ts": 1000.0, "metrics": {"facts_live": 5, "share_verified": 0.2,
                                                               "by_status": {"evidence_broken": 1}, "disputed": 0,
                                                               "queue": {"judge_pending": 2}}}))
    assert tile.summary(p, 1000.0 + 3600)["state"] == "ok"
    assert tile.summary(p, 1000.0 + 37 * 3600)["state"] == "stale"


def test_error_run_reads_error(tmp_path):
    p = tmp_path / "b.json"
    p.write_text(json.dumps({"written_ts": 1000.0, "error": "BrainUnavailable", "metrics": {}}))
    assert tile.summary(p, 1001.0)["state"] == "error"
```

Append to `tests/test_brain_graph_nightly.py`:

```python
def test_run_dry_writes_nothing_and_live_run_writes_health():
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location("brain_nightly", pathlib.Path("scripts/brain_nightly.py"))
    bn = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bn)
    s = FakeStore()
    f = _fact(s)
    m = bn.run(s, ".", check_fn=_checker("broken"), now_iso=NOW, dry_run=True)
    assert s.rows["brain_health"] == [] and s.get("brain_facts", f["id"])["version"] == f["version"]
    assert m["facts_live"] == 1
    bn.run(s, ".", check_fn=_checker("broken"), now_iso=NOW)
    assert len(s.rows["brain_health"]) == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_brain_health_tile.py tests/test_brain_graph_nightly.py -q -n 2`
Expected: FAIL, `ModuleNotFoundError: No module named 'sniper.services.brain_health_tile'`.

- [ ] **Step 3: Implement the tile reader**

```python
# sniper/services/brain_health_tile.py
"""The brain's nightly health for the web health block. Reads data/brain_health_latest.json only:
no Supabase call and no bot.brain_graph import (the brain never touches sniper/, spec 5.1)."""
import json

STALE_H = 36.0


def summary(path, now_ts):
    try:
        d = json.loads(open(path).read())
    except FileNotFoundError:
        return {"state": "missing"}
    except (OSError, ValueError):
        return {"state": "error", "reason": "unreadable"}
    age_h = (float(now_ts) - float(d.get("written_ts") or 0)) / 3600.0
    m = d.get("metrics") or {}
    state = "error" if d.get("error") else ("stale" if age_h > STALE_H else "ok")
    return {"state": state, "age_h": round(age_h, 1), "facts_live": m.get("facts_live"),
            "share_verified": m.get("share_verified"),
            "evidence_broken": (m.get("by_status") or {}).get("evidence_broken", 0),
            "disputed": m.get("disputed"), "queue": m.get("queue"), "error": d.get("error")}
```

In `sniper/web/server.py` `ops_health()`, right before `if result["issues"]:` (it raises NO issue, N8):

```python
    try:
        from sniper.services import brain_health_tile
        from sniper.util.state_paths import state_path
        result["brain"] = brain_health_tile.summary(state_path("data", "brain_health_latest.json"), time.time())
    except Exception:  # noqa: BLE001 - the brain block must never break ops health
        result["brain"] = {"state": "error", "reason": "tile_failed"}
```

- [ ] **Step 4: Implement the job**

```python
#!/usr/bin/env python3
# scripts/brain_nightly.py
"""The nightly brain check (spec 2.4; Plan 3). Flag-only. Cron, nice 19, one run at a time.

    brain_nightly.py              run and write the brain_health row + data/brain_health_latest.json
    brain_nightly.py --dry-run    compute and print, write nothing
    brain_nightly.py --restore data/brain_health_latest.json   undo that run's evidence_broken flips
Exit codes: 0 ok, 3 another run holds the lock, 4 brain unavailable (local file still written)."""
import argparse
import fcntl
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.brain_graph import live, nightly  # noqa: E402

STATE = live.state_root(ROOT)
LOCAL = STATE / "data" / "brain_health_latest.json"
LOCK = STATE / "data" / "brain_nightly.lock"


def write_local(path, payload):
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, default=str))
    os.replace(tmp, path)


def run(store, repo, *, check_fn, now_iso, rules=None, dry_run=False, secret_values=frozenset()):
    facts = nightly.all_live_facts(store)
    if dry_run:
        ev = {"checks": {}, "flips": [], "recovered_candidates": []}
        for f in facts:
            new, action = nightly.recheck(f, check_fn(f.get("evidence") or []), now_iso)
            ev["checks"][action] = ev["checks"].get(action, 0) + 1
        return nightly.health_metrics(facts, ev, {"dry_run": True}, nightly.queue_report(facts, now_iso), now_iso)
    ev = nightly.evidence_pass(store, facts, check_fn, now_iso)
    facts = nightly.all_live_facts(store)
    sweep = nightly.sweep_candidates(store, facts)
    doctrine = (nightly.doctrine_drift(store, facts, rules, repo=repo, secret_values=secret_values)
                if rules is not None else {"skipped": "no doctrine parser yet"})
    facts = nightly.all_live_facts(store)
    queue = nightly.queue_report(facts, now_iso)
    summ = nightly.summaries(store, facts, now_iso)
    m = nightly.health_metrics(facts, ev, sweep, queue, now_iso)
    m.update({"doctrine": doctrine, "summaries": summ})
    store.insert_health(m)
    return m


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--restore")
    ns = ap.parse_args(argv)
    from sniper.util.envfiles import load_env_files
    load_env_files(str(STATE))
    from bot.brain_graph.evidence import check_all, fetch_once
    from bot.brain_graph.gate import secret_values_from_env
    from bot.brain_graph.store import BrainUnavailable, RestStore
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("brain nightly: another run holds the lock")
            return 3
        store = RestStore(timeout=10)
        if ns.restore:
            flips = json.loads(Path(ns.restore).read_text())["metrics"]["flips"]
            print(json.dumps(nightly.restore_flips(store, flips)))
            return 0
        fetched = fetch_once(ROOT)
        now_iso = datetime.now(timezone.utc).isoformat()
        rules = None
        try:
            from bot.brain_graph.import_sources import doctrine_rules
            rules = [r for src in ("CLAUDE.md", "AGENTS.md") if (ROOT / src).exists()
                     for r in doctrine_rules((ROOT / src).read_text(), src)]
        except ImportError:
            pass
        started = time.monotonic()
        try:
            m = run(store, ROOT, check_fn=lambda items: check_all(items, ROOT, fetched=fetched), now_iso=now_iso,
                    rules=rules, dry_run=ns.dry_run, secret_values=secret_values_from_env(STATE / ".env"))
        except BrainUnavailable as exc:
            write_local(LOCAL, {"written_ts": time.time(), "error": f"BrainUnavailable: {exc}", "metrics": {}})
            print(f"brain nightly: BRAIN UNREACHABLE ({exc})")
            return 4
        m["duration_s"] = round(time.monotonic() - started, 1)
        if not ns.dry_run:
            write_local(LOCAL, {"written_ts": time.time(), "metrics": m})
        print(json.dumps({k: m.get(k) for k in ("facts_live", "share_verified", "disputed", "past_verify_by",
                                                "evidence_failure_rate", "duration_s")}))
        print("flips: %d, new candidate pairs: %s" % (len(m.get("flips") or []),
                                                      (m.get("candidates") or {}).get("pairs_new")))
        return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run to verify it passes, plus the isolation and web tests**

Run: `python3 -m pytest tests/test_brain_health_tile.py tests/test_brain_graph_nightly.py tests/test_brain_graph_isolation.py -q -n 2`
Also run every test file that imports `sniper/web/server.py`: `grep -rl --include='test_*.py' "sniper.web.server\|from sniper.web import server" tests/ | xargs python3 -m pytest -q -n 2`.
Expected: PASS. Then change `STALE_H = 36.0` to `STALE_H = 99999.0`, re-run, see `test_fresh_ok_and_stale_after_36h` FAIL, and restore it.

- [ ] **Step 6: Commit**

```bash
git add -- scripts/brain_nightly.py sniper/services/brain_health_tile.py tests/test_brain_health_tile.py
git commit -m "feat(brain): brain_nightly.py, local health file, brain block in ops health" -- scripts/brain_nightly.py sniper/services/brain_health_tile.py sniper/web/server.py tests/test_brain_health_tile.py tests/test_brain_graph_nightly.py
```

---

### Task 7: Apply migration 033, the first runs, and the cron line (Alpha)

No new code.

- [ ] Apply `migrations/supabase_033_brain_judgements.sql` the way Task 10 of the phase-1 plan applied 032, byte-exact. Extend `scripts/research/brain_graph_live_probe.py` with one check: an anon SELECT on `brain_judgements` returns `[]`, and a service DELETE is refused.
- [ ] `python3 scripts/brain_nightly.py --dry-run` against production. Read `facts_live`, the would-break count and the duration. Nothing is written.
- [ ] First live run by hand: `nice -n 19 python3 scripts/brain_nightly.py`. Read the `brain_health` row and `data/brain_health_latest.json`. The first run can flip nothing (debounce), so count only `annotate`.
- [ ] Install the cron line, local time, AFTER the 04:15 mirror while the mirror still runs (Plan 4 stops it). Back up the crontab to `data/crontab.bak.<UTC stamp>` first:
  `40 4 * * * cd /home/bigpapi/Desktop/Sniper && nice -n 19 /home/bigpapi/.pyenv/versions/3.13.0/bin/python3 scripts/brain_nightly.py >> /home/bigpapi/logs/brain_nightly.log 2>&1`
- [ ] After three nights, record in HANDOFF (and as a `crew_report` episode):
  - the flips per night, and any `recovered_candidates`;
  - the `evidence_failure_rate` against the Task 12 baseline;
  - the duration;
  - the `ops_health` brain block state.

## Edge cases (CLAUDE.md rule 5)

1. **Money path:** none. It is a cron script; the isolation test covers `bot.brain_graph`. The web reads a local JSON through a module that imports nothing from the brain, raises no header issue, and swallows its own failure.
2. **Fill speed:** untouched. It runs at nice 19. Git calls come from `evidence.check`, one `fetch_once` per run.
3. **Concurrency:** four sessions write while it runs.
   - Every patch passes the version it read. A conflict is counted and retried the next night, never forced.
   - The lock stops two nightly runs.
   - A session's `write_fact` racing the sweep can create a pair the sweep misses; the next night catches it.
4. **Absent / zero / unknown:**
   - `unchecked` never moves the debounce count.
   - A missing `recorded_at` sorts oldest in `rank`.
   - A missing local file reads `missing`, never `ok`.
   - A short page is the end only because the page size (500) is under max-rows (1000).
5. **Dependency failure:**
   - Supabase down: exit 4, and the local file carries the error, so the web shows `error`.
   - Git fetch failure: `fetch_once` returns False, SHAs check `unchecked` rather than `broken`.
   - A 429 is `BrainUnavailable` (phase-1 amendment 5).
6. **Units and identity:**
   - Pairs are ordered `(lo, hi)` uuids.
   - Times are ISO UTC, and ages are hours from `recorded_at` (the server clock).
   - `verify_by` is compared as ISO strings in the same `+00:00` form, as the server writes them.
7. **The sample:** health shares describe what sessions wrote, not the world. `share_verified` is over live non-observation facts only. `evidence_failure_rate` counts `ok` and `broken` only, and `unchecked` is reported apart in `evidence_checks`.
8. **Rollback:**
   - Remove the cron line.
   - `brain_nightly.py --restore <health file>` puts one run's flips back.
   - Migration 033 has a rollback file.
   - Half-applied (033 without the code, or the reverse) leaves `write_fact` and the sweep failing loudly on the missing table, never silently skipping. The phase-1 tables are untouched.

The runnable checks left behind are the Task 1-6 tests. Each was proven to fail with its guard removed (Step 5 of each task).

## Self-review against the spec

- 2.4 "re-runs the machine evidence on every active fact": Task 2 (`all_live_facts` pages everything; observations are skipped because they carry no evidence).
- 2.4 "cannot run is unchecked": Task 2 (unchecked leaves the count) and `evidence.check`'s own contract. [R6]
- 2.4 pending_push re-check: Task 2 (`pending_push` resets the count, and `fetch_once` runs first).
- 2.4 contradiction candidates across each entity, queued for the next session: Task 3 (candidate link plus `judge_pending`); judged pairs are never re-queued (N5).
- 2.4 disputed > 24 h, unverified > 72 h: Task 4 `queue_report`.
- 2.4 summaries: Task 4 (extractive, N6).
- 2.4 one `brain_health` row: Task 6 `run`.
- 2.4 never deletes, only `evidence_broken`: Tasks 2 and 6. The doctrine supersede is the one exception spec 2.1.3 names (Task 5).
- 2.4 SQL evidence read-only and bounded: not executed at all (N3). Stated so a reviewer can object.
- 2.5 `evidence_broken` visible, not hidden: unchanged (the phase-1 read filters), and the web block counts it.
- 2.6 share verified, queue age p50/p90, disputed count and age, failure rate, facts per section (via summaries), past `verify_by`: Task 4 `health_metrics`; the tile is Task 6.
- Plan 2 D4 (archive-tag SHAs `unchecked`) lives in `evidence._sha` (Plan 2 Task 1), so the nightly inherits it with no change here.
- Facts per section: carried in the section summaries' counts. A separate `per_section` map in `health_metrics` is omitted; add it only if the tile needs it.
