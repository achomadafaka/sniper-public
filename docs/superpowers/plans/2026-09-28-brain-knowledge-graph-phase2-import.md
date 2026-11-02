# Brain Knowledge Graph, Phase 2 (import) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. On this project a "subagent" is a crew session (Beta or Lambda) working in a private worktree; the operator's standing rule forbids spawning worker fleets.

**Goal:** Import the old brain (HANDOFF, the memory files and Supabase `memories` rows, the handover skill's traps and standing rules, and the CLAUDE.md / AGENTS.md doctrine) into `brain_episodes` and `brain_facts` through the phase-1 gate: idempotently, with exact identifier resolution, author != verifier, and stale or retracted sources imported WITH a status instead of dropped.

**Architecture:** Three pure layers and one writer. `bot/brain_graph/import_sources.py` parses the sources (no I/O beyond the text it is given). `bot/brain_graph/import_entities.py` turns text into exact identifier entities with sections. `bot/brain_graph/import_manifest.py` builds a deterministic manifest (episodes, facts, judge-queue items, links) from the parsed sources. `bot/brain_graph/importer.py` writes the manifest through `api.add_episode` / `api.write_fact(interactive=False)` behind `CachedStore`, so the run makes O(n) Supabase calls, and reports every count. `scripts/brain_import.py` is the CLI; `--dry-run` runs the same code on `FakeStore`.

**Tech Stack:** Python 3.13 stdlib; the phase-1 package `bot/brain_graph/`; Supabase PostgREST via `RestStore`; `git` (one `cat-file --batch-check`, one `blame --porcelain`); pytest with `FakeStore`.

**Spec:** `docs/superpowers/specs/2026-09-28-brain-knowledge-graph-design.md` (approved, including the [Rn] amendments), sections 1.3, 2.1, 2.3 and 4.3. The phase-1 plan and its review amendments: `docs/superpowers/plans/2026-09-28-brain-knowledge-graph-phase1.md`. Inputs: Beta's value cut (`docs/audits/2026-09-28-brain-surface-inventory.md` section 7, 8b59a6fd) and the gate baseline (`docs/audits/2026-09-28-brain-baseline-gate.md`). Plans 3 and 4 (78d03b38) consume this plan's names; see "Interfaces other plans use".

<!-- schema:external=brain_episodes -->
<!-- schema:external=brain_entities -->
<!-- schema:external=brain_facts -->
<!-- schema:external=brain_links -->
<!-- schema:external=brain_sections -->
<!-- schema:external=memories -->

## Global Constraints

- Everything in the phase-1 plan's Global Constraints holds: sizes, kinds, statuses, evidence results, `verify_by` by kind, server-stamped `recorded_at`, no `.env` values printed, 4 embedding threads at nice 19, offline tests (`FakeStore`), `-n 2` for one file and `-n 6` at most for a set (`pytest.ini` sets `-n auto`; `-p no:xdist` exits 4), explicit-pathspec commits.
- The importer writes as author `import`, except doctrine facts (`operator`, spec 2.1.3) and traps whose adding commit names a crew role (D7). It never sets `verified_by`, and no role may verify a fact whose author it is (DB CHECK).
- Every imported fact carries `author_session = "import:v1"` (`import_sources.IMPORT_SESSION`). That marker, not the author, is what the idempotency index, the import status permission and the rollback select on (Beta review B1, B2).
- Nothing is deleted. A stale, superseded or retracted source is imported with that status (D3).
- Identifier entities resolve by exact name or alias only (R1): commits by a full SHA that `git cat-file` resolves, knobs only when `evidence.check` says `ok`, tables from the registry. Descriptive entities come only from the curated alias tables in `import_entities.py`.
- Supabase is never written while building. The live run (Task 8) is Alpha's step, after Beta and Alpha read the dry run's counts (Task 7).
- Paging: 500 rows per page, keyset on `id`, under PostgREST's max-rows of 1000, so a short page is the end (the Plan 3 convention).

## Decisions

### D1. Sources and what each becomes

| source | episodes | facts |
|---|---|---|
| `HANDOFF.md` + `HANDOFF-archive-2026-08.md`, one top-level block each | `handoff_line` | none automatically: decisions (last 7 days) and rule candidates go to the judge queue (D11) |
| local memory files `~/.claude/projects/-home-bigpapi-Desktop-Sniper/memory/*.md` (canonical for the `session/` mirrors) | `legacy_memory`, one per file | headline (the frontmatter `description`) + each "How to apply" bullet |
| Supabase `memories` rows whose name does not start with `session/` (the direct writers, all statuses) | `legacy_memory`, one per row | as local files; a direct row whose name suffix equals a local memory's name is a duplicate and is skipped (the dry run's rule) |
| `.claude/skills/handover/SKILL.md`, NAMING TRAPS items | `doc`, one per item | kind `trap` |
| the same file, STANDING RULES items | `doc`, one per item | kind `rule`, section `brain/doctrine` |
| `CLAUDE.md`, `AGENTS.md` rule sentences (`doctrine_rules`) | none (git holds the file) | kind `rule`, author `operator`, evidence `path CLAUDE.md:<line>`, section `brain/doctrine` |

- The SKILL.md SETTLED block is a template placeholder (rebuilt per handover), so it is not a source. SETTLED lines live in HANDOFF and reach facts through the judge queue.
- `MEMORY.md` itself is not imported (its lines point at the files). `topic-*` archive indexes and frontmatter type `user` are episodes only. The `session/HANDOFF*` mirror rows are skipped (the files are the source).
- Kinds for memory headlines: `feedback` -> `rule`; `finding`, `project`, `reference`, no type, and every Supabase direct-row type -> `finding`. How-to-apply bullets -> `rule`.

### D2. Idempotency (a re-run writes nothing new; a resumed run finishes)

- **Episode key** = `source_ref` = `<anchor>#<sha256(original body)[:12]>`. Anchors: `memory/<file>.md` (Plan 4's key, unchanged), `supabase:memories/<name>`, `HANDOFF.md`, `HANDOFF-archive-2026-08.md`, `SKILL.md`. The run reads `episode_index("import", prefix)` first and skips a key it holds. A changed body is a NEW episode under the same anchor: episodes are history. HANDOFF line numbers are not in the key, so an appended HANDOFF re-imports nothing.
- **Partial episode**: a key whose stored parts are fewer than its re-derived parts gets the missing parts inserted under the existing parent. If stored part 1 differs from the re-derived part 1 (the redaction set changed), the key is reported `redaction_changed` and left alone.
- **Fact key** = `import_sources.normalize(statement)` (whitespace collapsed, stripped). The run reads `fact_index(IMPORT_SESSION)` (every fact with `author_session = "import:v1"`, whatever its author) over ALL statuses and skips a key it holds, so a superseded, retracted or refuted import is never revived, and a crew-authored trap (D7) is found like any other.
- **Changed source**: an anchor that produced a fact and now yields a different statement for the same slot (headline, bullet n) gets the new fact through the gate plus a `candidate` link to the old one, and the new fact is `judge_pending`. A session judges it (usually `supersedes`). The importer never supersedes on its own.
- Links are unique on `(from_id, to_id, rel)`; a duplicate refusal on a re-run counts as present.

### D3. Status on import ("stale or retracted get status, never deletion")

| source state | status | valid_to |
|---|---|---|
| Supabase `status=superseded` | `superseded`, plus a `supersedes` link from its `superseded_by` row's fact when that is imported (second pass) | the row's `updated_at` |
| Supabase `status=retired` | `superseded` (replaced, not proven false; no successor link) | the row's `updated_at` |
| active; name or description matches a RETRACTED / WRONG / withdrawn / SUPERSEDED / CORRECTED / STALE / "no longer" marker | `unverified`, `judge_pending`, plus a judge-queue item | none |
| an evidence item the gate calls `broken` | `evidence_broken` | none |

A text marker NEVER closes a fact (Beta review): on the 529 local descriptions the retraction regex matched 2, and both were live rules (`onchain-detections-is-a-funded-lane`: "RETRACTED FIX, do not move ..."; `radar-creator-prior-arm`: "live but its evidence was retracted"). Only the Supabase row status closes, and a session judges every marker.

`api.write_fact(..., import_status=..., valid_to=...)` (Task 1) sets the status on the single INSERT, never a patch after it, so a crash cannot leave a closed source live. It is accepted only when `draft["author_session"] == IMPORT_SESSION`, so a crew-authored trap with a broken reference imports as `evidence_broken` instead of being refused (B2); a closed status skips the candidate stage. Without it a `broken` item refuses the write and the source is lost.

### D4. Archive-tag SHAs are `unchecked`, not `broken` (a gate change, Task 1)

68 of the 122 broken SHAs in the gate baseline resolve only on `refs/tags/archive/...`: the commit exists and is retained, so the reference is not false. `evidence._sha` returns `unchecked` ("only on archive tag <tag>, never merged under this SHA") when `git tag --contains` lists an `archive/` tag. It applies to every writer and to the Plan 3 nightly. A commit on a non-archive tag only, or on no ref, stays `broken`.

### D5. Entities: exact identifier resolution only (R1)

| type | rule | section |
|---|---|---|
| commit | a hex token of 7-40 chars that `git cat-file --batch-check` resolves to a commit; name = the full SHA | the fact's section |
| path | a repo-relative path (the dry run's PATH regex), `:line` stripped | by path rule (below) |
| knob | only when `evidence.check` says `ok` (an invented or near-miss name is never an entity) | by knob rule (below) |
| table / column | a table in `index/schema_registry.json` or in `EXTERNAL_TABLES` (`pool_ticks`, `evm_position_pools`, `volume_feed`, `volume_feed_lag`, `block_times`, `pool_meta`, the `brain_*` tables); a column only as `table.column` whose table resolves | `memecoin_*` -> `memecoin`, `brain_*` -> `brain`, `hl_*` -> `hyperliquid`, else `infra/db` |
| chain | the alias table: `4663` (robin, robinhood), `8453` (base), `56` (bsc, bnb), `5042` (arc), `solana` (sol); word aliases match case-sensitively as written in the table | `memecoin/<id>` (`memecoin/solana` for solana) |
| position | keyword form only (`position 66413`, `pos 66413`) | `memecoin` |
| wallet | `memecoin_wallets` handles (4+ chars, not a dictionary word, as the dry run); name = handle | `memecoin` |
| venue, provider, lane, process, gate, exit_rail | `CURATED` in `import_entities.py`, exact alias match | per row |
| tool | the action keys `write_fact` adds | `brain/doctrine` |
| path (provenance) | every memory fact also carries `memory/<file>.md` (or `supabase:memories/<name>`); every trap and standing rule carries `.claude/skills/handover/SKILL.md`; every doctrine fact carries `CLAUDE.md` / `AGENTS.md` | the fact's section; SKILL.md and doctrine files in `brain/doctrine` |

The provenance entity guarantees at least one entity per fact. Path rule: `sniper/services/memecoin_*`, `sniper/execution/*`, `sniper/feeds/*` -> `memecoin`; `scripts/deploy*`, `*watchdog*` -> `infra/deploy`; `scripts/research/*`, `docs/audits/*` -> `research/studies`; `bot/brain_graph/*`, `scripts/brain*` -> `brain`; else `infra/runtime`. Knob rule, first match: a numeric chain suffix -> `memecoin/<chain>`; `STOP|TP|TRAIL|EXIT|VOLDEATH|MIRROR` -> `memecoin/exits`; `CHASE|MCAP|VOLUME|TURNOVER|FLOOR|CEILING|AGE` -> `memecoin/gates`; `CLIP|SIZE|EXPOSURE|CONCURRENT|BANKROLL` -> `memecoin/sizing`; `MEMECOIN_` -> `memecoin`; `RPC|HELIUS|ALCHEMY|_URL` -> `infra/rpc`; `HL_` -> `hyperliquid`; `CRYPTO5M` -> `crypto5m`; else `infra/runtime`.

### D6. Sections of a fact (resolved from the local corpus: 529 files)

Measured front of the local memory files (`front_of` on name + description): memecoin 271, other 159, brain/infra 43, wallet-analyzer 18, crypto5m 14, sports/markets 12, hyperliquid 12. The "other" files are mostly crew method lessons (`feedback-paste-counts-...`, `feedback-map-the-edge-cases-...`) and research findings the regex missed.

- Frontmatter type `feedback` -> `crew/methods` (Beta review: crew working methods are not CLAUDE.md doctrine). `crew/methods` is a new section; Task 5's `--seed-methods` inserts it and appends it to every `role/<name>` view's `sections` (the seed never rewrites an existing view, so the importer patches the five views once, idempotently).
- Otherwise by front: memecoin -> `memecoin`; crypto5m -> `crypto5m`; hyperliquid -> `hyperliquid`; wallet-analyzer and sports/markets -> `wallet-analyzer` (both Polymarket); brain/infra -> `infra/runtime`; other -> `research/studies`.
- Chain, knob, table and path entities carry their own sections (D5), so a fact also reaches every section its identifiers live in.

### D7. Authors, and author != verifier

- Imported facts are `author=import`; `import` is not a verifier value, so any crew role may verify them and the DB CHECK holds. The importer never sets `verified_by`.
- HANDOFF block authors: the first-named role after an optional date and time, including dateless blocks such as `- 20:24Z Lambda (...)` (Beta review).
- A trap whose adding commit (one `git blame --porcelain` over SKILL.md) has a subject naming a crew role ("traps Lambda paid for") is written with that role as `author`, so the DB CHECK stops that role from verifying its own trap. No role found -> `import`.
- Doctrine facts are `author=operator` (spec 2.1.3). Episode authors: a HANDOFF block's first-named role (`alpha`, `beta`, `gamma`, `lambda`, `operator`), else `import` (Codex blocks included: codex is not a writer).
- `author_session` = `IMPORT_SESSION` on every imported fact (B1). The memory's `originSessionId` stays in its episode body (the frontmatter is verbatim there).

### D8. Dates

- `valid_from`: memory frontmatter `metadata.modified`, else a `YYYY-MM-DD` in the file name, else the Supabase row's `updated_at`; a trap's or standing rule's blame author time; doctrine: the file's last commit time. A date-only value maps to T00:00:00Z of that day.
- Episode `occurred_at`: the same rules; a HANDOFF block's own ISO stamp, else its date; NULL when there is no date at all (never "now").
- `verify_by = valid_from + kind days + stagger`, stagger = `int(sha256(fact key)[:8], 16) % 14` days (R8): deterministic, so a re-run and the tests agree. Facts older than about 30-44 days land STALE and enter the queue; the dry run reports how many.

### D9. Statements over 600 chars (R12)

statement = the longest run of whole sentences <= 600 chars (a first sentence over 600 is cut at the last word boundary <= 597 plus "..."); detail = the FULL original text (<= 4,096; the largest known is 3,742). Nothing is lost; the detail is not embedded.

### D10. Same-source pairs are structure, not judgement

A how-to-apply bullet shares its memory's provenance entity, and often a key, with the memory's headline, so the candidate stage pairs them. The importer passes `judgements={<headline fact id>: "refines"}` for candidates whose `episode_ids` contain the SAME episode, giving a `derived_from` link (and, under Plan 3 N5, a recorded judgement). Every cross-source candidate stays unjudged and lands `judge_pending` with a `candidate` link.

### D11. The judge queue (R18; spec 4.3, about 1,500 items)

Queue items are not facts. They are the dry run's LLM-queue candidates (HANDOFF decisions of the last 7 days, HANDOFF rule candidates, memory-body retractions) plus the retraction candidates that name a target, and the D3 SUPERSEDED/CORRECTED/STALE memories. They become `task` facts, one per section per 25 items: statement "Judge import batch <section> #<n>: turn each item into a fact with brain_cli.py write, or drop it", detail = one line per item (episode id, then an excerpt of at most 120 chars), evidence = the episodes, `verify_by` = import day + batch index days (spec 2.2: one per-section batch a day), so the batches go stale one a day and never all at once. Worst case for the 4,096-char detail cap: 25 x (36-char uuid + 1 + 120) + 24 newlines = 3,949 chars (Beta review; a test asserts it).

### D12. Cost and concurrency

- `write_fact` calls `find_entity` per entity and `find_facts` (every fact on the entities). For about 1,500 facts on shared entities (chain `solana` alone carries hundreds) that is O(n^2) transfer. `CachedStore` reads every entity and every live fact once (keyset pages) and adds each insert to its cache; writes go through the real store.
- Episodes go in batches of 200 through `insert_many` (part 1 rows first, then later parts with their parents).
- Known gap: a crew write during the import is not in the cache, so it is not a candidate for later imported facts. Plan 3's nightly candidate sweep (N4) covers it.
- `RestStore(timeout=10)`; `BrainUnavailable` stops the run with its counts; a re-run resumes (D2). At most 5 requests a second.

### D13. What boot sees after the import

`boot` reads the 500 newest facts by `recorded_at`, which the server stamps at insert. The importer therefore inserts least important first: episodes, how-to-apply bullets, headlines, doctrine, standing rules, traps last, so the newest 500 are traps, standing rules and doctrine. Ranking inside the database (not after a newest-first cut) is Plan 4's view work; this plan only orders the inserts and reports the cut in the dry run.

### D14. Embeddings

After the rows exist, `scripts/brain_embed_backfill.py` embeds `brain_facts.statement`, `brain_entities.name` and `brain_episodes.body` where `embedding IS NULL`, through `brain_set_embedding`, 4 threads, nice 19, model loaded once, resumable. The dry run embedded 1,524 texts in 3.5 min; about 7,000 texts is about 16 min.

## Interfaces other plans use

- Plan 3 N7: `import_sources.doctrine_rules(text: str, source: str) -> list[dict]` with keys `statement`, `line`, `source`; and `import_sources.normalize(statement) -> str` (the D2 fact key).
- Plan 3: D4 lands in Task 1 (`evidence._sha`).
- Plan 4 Task 3: `store.episode_refs(author: str, prefix: str) -> set[str]` is defined HERE (Task 1), keyset-paged; Plan 4 consumes it instead of adding its own. The memory episode key is `memory/<file>.md#<sha256(body)[:12]>` in both plans.
- Plan 4 Task 5: `memories` rows are retired against the facts this import writes; `importer.run` returns `memory_facts: {memory name: [fact ids]}` in its report for that manifest.

## Edge cases (CLAUDE.md rule 5)

1. **Money path:** none. The importer is a script plus `bot/brain_graph` modules; the phase-1 isolation test refuses a `bot.brain_graph` import from `sniper/` and the funded entry points, and Task 5 adds `bot/brain_graph/importer.py` to nothing that test allows.
2. **Fill speed:** not touched. Research load: one process, one `git cat-file --batch-check` for every hex token, one `git blame`, one read-only query for wallet handles, one read-only Supabase snapshot of `memories`; embeddings at nice 19.
3. **Concurrency:** D12's cache gap; unique constraints on entity names, links and section slugs refuse a racing duplicate and the loser re-reads. No module global is mutated except the phase-1 key-free caches.
4. **Absent vs zero vs unknown:** no date -> NULL `occurred_at` (valid_from falls back to the import day and the report counts `valid_from_unknown`); a hex token that is no commit is not evidence; a tool error in a check is `unchecked`.
5. **Dependencies:** Supabase down stops the run with the counts so far; a short page ends paging only below the page size; a 429 or 5xx stops (no retry loop against the shared project); `git` failing makes every check `unchecked`, reported.
6. **Units and identity:** commits stored full; chain aliases canonical (`canonical_chain_id`); `normalize` is the only fact key; the Supabase and local copies of one memory are one source (D1 duplicate rule).
7. **The sample:** every count in the report names its denominator (sources parsed, not "the brain"). The judge-queue size depends on the dry run's regex typing (50% agreement in Beta's sample), which is why items are judged, not auto-imported.
8. **Rollback:** facts and episodes cannot be deleted (triggers). A bad import is closed by one UPDATE setting `status='retracted', valid_to=now(), expired_at=now()` on facts with `author_session = 'import:v1'` recorded inside the run's window (the report prints the window); that includes the crew-authored traps (B1). Episodes stay as history. This is why the dry run is mandatory.

## File Structure

| File | Responsibility |
|---|---|
| `bot/brain_graph/store.py`, `tests/brain_graph_fake_store.py` (modify) | `rows_page`, `episode_index`, `episode_refs`, `fact_index`, `insert_many` |
| `bot/brain_graph/api.py` (modify) | `write_fact(import_status=, valid_to=)` |
| `bot/brain_graph/evidence.py` (modify) | archive-tag SHAs `unchecked` (D4) |
| `bot/brain_graph/import_sources.py` | pure parsers, dates, splits, keys, `IMPORT_SESSION` (moved from the dry run, plus doctrine and memory frontmatter) |
| `bot/brain_graph/import_entities.py` | identifier extraction, curated alias tables, sections |
| `bot/brain_graph/import_manifest.py` | the deterministic manifest |
| `bot/brain_graph/importer.py` | `CachedStore`, `run`, the report, the judge-queue batches |
| `scripts/brain_import.py` | CLI: `--dry-run`, `--live`, `--report <path>` |
| `scripts/brain_embed_backfill.py` | D14 |
| `scripts/research/brain_migration_dryrun.py` (modify) | imports the moved parsers; its `--selftest` still passes |
| `tests/test_brain_graph_import_*.py` | tests |

---

### Task 1: Store paging and batches, `import_status`, archive-tag SHAs

**Files:**
- Modify: `bot/brain_graph/store.py`, `tests/brain_graph_fake_store.py`, `bot/brain_graph/api.py`, `bot/brain_graph/evidence.py`, `tests/test_brain_graph_evidence.py`
- Test: `tests/test_brain_graph_import_store.py`

**Interfaces:**
- Produces:
  - `store.rows_page(table: str, *, select: str = "*", filters: tuple = (), after_id: str | None = None, limit: int = 500) -> list[dict]`: keyset page in `id` order; `filters` are `(column, op, value)` with PostgREST ops (`eq`, `like`, `in`).
  - `store.episode_index(author: str, prefix: str) -> dict[str, dict]`: `{source_ref: {"parent": id, "parts": n}}`.
  - `store.episode_refs(author: str, prefix: str) -> set[str]` (Plan 4 consumes it).
  - `store.fact_index(session: str) -> dict[str, dict]`: `{normalize(statement): {"id", "status", "episode_ids"}}` over ALL statuses, for `author_session = session`.
  - `store.insert_many(table: str, rows: list[dict]) -> list[dict]`.
  - `api.write_fact(..., import_status: str | None = None, valid_to: str | None = None, judge_pending: bool = False)`: all three only with `author_session == IMPORT_SESSION`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_brain_graph_import_store.py
from pathlib import Path

import pytest

from brain_graph_fake_store import FakeStore
from bot.brain_graph import api
from bot.brain_graph.store import BrainRefused

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
```

Also edit `tests/test_brain_graph_evidence.py`: the tag-only fixture commit is tagged `archive/old`, so its assertion becomes `== "unchecked"`; add a second commit tagged `v-not-archive` only, asserted `== "broken"`.

- [ ] **Step 2: Run them and see them fail**

Run: `python3 -m pytest -q -n 2 tests/test_brain_graph_import_store.py tests/test_brain_graph_evidence.py`
Expected: FAIL (`AttributeError: 'FakeStore' object has no attribute 'rows_page'`; the archive assertion reads `broken`).

- [ ] **Step 3: Implement**

`bot/brain_graph/store.py`, in `RestStore`:

```python
    def rows_page(self, table, *, select="*", filters=(), after_id=None, limit=500):
        """One keyset page in id order. limit stays under PostgREST's max-rows (1000), so a page
        shorter than `limit` is the end."""
        path = f"{table}?select={_q(select)}&order=id.asc&limit={int(limit)}"
        for column, op, value in filters:
            path += f"&{column}={op}.{_q(value)}"
        if after_id:
            path += f"&id=gt.{_q(after_id)}"
        return self._call("GET", path) or []

    def _all(self, table, **kw):
        rows, after = [], None
        while True:
            page = self.rows_page(table, after_id=after, **kw)
            rows += page
            if len(page) < kw.get("limit", 500):
                return rows
            after = page[-1]["id"]

    def episode_index(self, author, prefix):
        out = {}
        for r in self._all("brain_episodes", select="id,source_ref,part_no,parent_id",
                           filters=(("author", "eq", author), ("source_ref", "like", prefix + "*"))):
            slot = out.setdefault(r["source_ref"], {"parent": None, "parts": 0})
            slot["parts"] += 1
            if r["part_no"] == 1:
                slot["parent"] = r["id"]
        return out

    def episode_refs(self, author, prefix):
        return set(self.episode_index(author, prefix))

    def fact_index(self, session):
        from bot.brain_graph.import_sources import normalize
        rows = self._all("brain_facts", select="id,statement,status,episode_ids",
                         filters=(("author_session", "eq", session),))
        return {normalize(r["statement"]): {"id": r["id"], "status": r["status"],
                                            "episode_ids": r.get("episode_ids") or []} for r in rows}

    def insert_many(self, table, rows):
        return self._call("POST", table, list(rows), prefer="return=representation") or []
```

`tests/brain_graph_fake_store.py` gets the same five methods over `self.rows` (keyset on `id` order of insertion; `like` = prefix match; `eq` = equality).

`bot/brain_graph/api.py`, `write_fact` signature gains `import_status=None, valid_to=None`. At the top:

```python
IMPORT_STATUSES = ("evidence_broken", "retracted", "superseded")
IMPORT_SESSION = "import:v1"            # import_sources.IMPORT_SESSION; the importer's only marker


def write_fact(store, draft, *, repo, judgements=None, interactive=True, secret_values=frozenset(),
               outbox=None, fetched=True, import_status=None, valid_to=None, judge_pending=False):
    if import_status is not None and import_status not in IMPORT_STATUSES:
        raise ValueError(f"import_status must be one of {IMPORT_STATUSES}")
    if (import_status is not None or judge_pending) and draft.get("author_session") != IMPORT_SESSION:
        return {"status": "refused", "errors": ["only the importer sets a status on write"]}
    errors = validate_draft(draft, secret_values=set(secret_values))
    evidence = check_all(draft.get("evidence"), repo, fetched=fetched)
    broken = [f"evidence {e.get('type')} {e.get('ref')}: {e['check_reason']}"
              for e in evidence if e["check"] == "broken"]
    if not import_status:
        errors += broken
    if errors:
        return {"status": "refused", "errors": errors}
    closed = import_status in ("retracted", "superseded")
```

In the body: skip `candidates(...)` when `closed` (`cands = []`); after building `row`, add

```python
        if import_status:
            row["status"] = import_status
        if judge_pending:
            row["judge_pending"] = True                 # a D3 marker: judged before any verify
        if closed:
            row["valid_to"] = valid_to or row.get("valid_from")
            row["expired_at"] = datetime.now(timezone.utc).isoformat()
```

`bot/brain_graph/evidence.py`, in `_sha`, before the final `broken` return:

```python
    tags = [t for t in _git(repo, "tag", "--contains", full, "--list", "archive/*").stdout.split() if t]
    if tags:
        return "unchecked", f"{sha} is only on archive tag {tags[0]} (never merged under this SHA)"
```

- [ ] **Step 4: Run them and see them pass**

Run: `python3 -m pytest -q -n 2 tests/test_brain_graph_import_store.py tests/test_brain_graph_evidence.py tests/test_brain_graph_api.py tests/test_brain_graph_store.py`
Expected: PASS. Remove the archive branch in `_sha` and see the evidence test fail; remove the `author_session` check and see `test_only_the_importer_may_set_a_status` fail.

- [ ] **Step 5: Commit**

```bash
git add -- tests/test_brain_graph_import_store.py
git commit -F msg.txt -- bot/brain_graph/store.py tests/brain_graph_fake_store.py bot/brain_graph/api.py bot/brain_graph/evidence.py tests/test_brain_graph_evidence.py tests/test_brain_graph_import_store.py
```

Message: `feat(brain): keyset paging, batch inserts, import statuses, archive-tag SHAs unchecked`.

---

### Task 2: `import_sources.py`, the pure parsers

**Files:**
- Create: `bot/brain_graph/import_sources.py`
- Modify: `scripts/research/brain_migration_dryrun.py` (import the moved functions; delete its copies)
- Test: `tests/test_brain_graph_import_sources.py`

**Interfaces:**
- Consumes: the dry run's `handoff_blocks`, `trap_items`, `strip_frontmatter`, `split_candidates`, `classify`, `RULES`, `block_dates`, `apply_rules`, `front_of`, `FRONT_RX`, `STALE_RX` (moved verbatim).
- Produces:
  - `IMPORT_SESSION = "import:v1"`: the marker every imported fact carries (B1).
  - `normalize(statement: str) -> str`: the D2 fact key.
  - `episode_key(anchor: str, body: str) -> str`: `f"{anchor}#{sha256(body)[:12]}"`.
  - `frontmatter(text: str) -> dict`: `name`, `description`, `type` (top-level or `metadata.type`), `session` (`originSessionId`), `modified`.
  - `memory_source(file_name: str, text: str) -> dict`: `headline`, `bullets`, `kind`, `type`, `session`, `date`, `marker` (`marked` | None, D3: any retraction or staleness word; it only routes the memory to the judge queue).
  - `standing_rules(skill_text: str) -> list[tuple[int, str]]` and `traps(skill_text: str) -> list[tuple[int, str]]`: (first line number, text).
  - `doctrine_rules(text: str, source: str) -> list[dict]` with `statement`, `line`, `source` (Plan 3 N7).
  - `split_statement(text: str) -> tuple[str, str | None]` (D9).
  - `source_date(fm: dict, file_name: str) -> str | None` (D8, ISO UTC).
  - `handoff_author(header: str) -> str | None` (D7).
  - `verify_by(kind: str, valid_from: str, key: str) -> str` (D8).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_brain_graph_import_sources.py
from bot.brain_graph import import_sources as S

MEM = """---
name: feedback-paste-counts-never-retype-them-2026-09-28
description: Two report errors in one night came from reading a truncated line and retyping a count; paste numbers from tool output
metadata:
  type: feedback
  originSessionId: d933c54a-0557-416a-a15a-834f35eef5a7
  modified: 2026-09-28T05:22:14.767Z
---
Body.
**How to apply:**
- Paste every count in a report from the tool output. Never retype one.
- Never make a claim from a line that shows a cut or truncation mark.
"""


def test_a_memory_file_yields_its_headline_bullets_and_provenance():
    m = S.memory_source("feedback-paste-counts-never-retype-them-2026-09-28.md", MEM)
    assert m["kind"] == "rule" and m["type"] == "feedback"
    assert m["headline"].startswith("Two report errors in one night")
    assert m["bullets"] == ["Paste every count in a report from the tool output. Never retype one.",
                            "Never make a claim from a line that shows a cut or truncation mark."]
    assert m["session"] == "d933c54a-0557-416a-a15a-834f35eef5a7"
    assert m["date"] == "2026-09-28T05:22:14.767000+00:00" and m["marker"] is None


def test_the_name_date_is_the_fallback_and_a_retraction_is_marked():
    text = MEM.replace("  modified: 2026-09-28T05:22:14.767Z\n", "").replace(
        "description: Two", "description: RETRACTED 2026-09-29. Two")
    m = S.memory_source("finding-x-2026-09-20.md", text)
    assert m["date"] == "2026-09-20T00:00:00+00:00" and m["marker"] == "marked"
    assert S.memory_source("finding-y.md", MEM.replace("description: Two", "description: STALE: two"))["marker"] == "marked"
    live_rule = MEM.replace("description: Two", "description: RETRACTED FIX, do not move the lane. Two")
    assert S.memory_source("onchain-detections.md", live_rule)["marker"] == "marked"   # judged, never closed


def test_normalize_and_the_episode_key_are_stable():
    assert S.normalize("  Never   `git stash`\n the fix. ") == "Never `git stash` the fix."
    k = S.episode_key("memory/a.md", "body")
    assert k == S.episode_key("memory/a.md", "body") and k.startswith("memory/a.md#") and len(k) == len("memory/a.md#") + 12


def test_split_keeps_whole_sentences_and_the_full_text():
    long = " ".join(["Sentence number %03d is here." % i for i in range(40)])
    st, detail = S.split_statement(long)
    assert len(st) <= 600 and st.endswith(".") and detail == long
    assert S.split_statement("Short rule.") == ("Short rule.", None)
    st, _ = S.split_statement("x" * 700)
    assert len(st) <= 600 and st.endswith("...")


def test_doctrine_rules_carry_line_and_source():
    text = "# T\n\n- Never `git add -A` in this repo.\n- The sky is blue today.\n"
    rules = S.doctrine_rules(text, "CLAUDE.md")
    assert rules == [{"statement": "Never `git add -A` in this repo.", "line": 3, "source": "CLAUDE.md"}]


def test_verify_by_is_staggered_but_deterministic():
    a = S.verify_by("trap", "2026-09-01T00:00:00+00:00", "k1")
    assert a == S.verify_by("trap", "2026-09-01T00:00:00+00:00", "k1")
    assert "2026-10-01" <= a[:10] <= "2026-10-15"
    assert S.verify_by("settled", "2026-09-01T00:00:00+00:00", "k1")[:10] >= "2026-10-31"


def test_handoff_author_is_the_first_named_role():
    assert S.handoff_author("- 2026-09-28T13:02Z Beta: DARK WALLETS WRITTEN") == "beta"
    assert S.handoff_author("- 2026-09-28T13:50Z OPERATOR: (1) brain shape") == "operator"
    assert S.handoff_author("- 2026-09-28T13:50Z Codex: pushed") is None
    assert S.handoff_author("- 20:24Z Lambda (operator flagged the GRAMR block):") == "lambda"
```

- [ ] **Step 2: Run it and see it fail**

Run: `python3 -m pytest -q -n 2 tests/test_brain_graph_import_sources.py`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

Move the listed dry-run functions and regexes into `bot/brain_graph/import_sources.py` unchanged, then add:

```python
import hashlib
import re
from datetime import datetime, timedelta, timezone

_WS = re.compile(r"\s+")
_ISO = re.compile(r"(20\d\d-\d\d-\d\d)(?:T(\d\d:\d\d(?::\d\d(?:\.\d+)?)?)Z?)?")
IMPORT_SESSION = "import:v1"
_MARKER = re.compile(r"\b(RETRACTED|retracted|WRONG|withdrawn|STALE|SUPERSEDED|superseded|CORRECTED|"
                     r"no longer (?:true|holds|applies))\b")
_ROLE = re.compile(r"^\W*(?:(?:\d{4}-\d\d-\d\d)?T?[\d:]+Z\s+)?(Alpha|Beta|Gamma|Lambda|OPERATOR|Operator|Codex)\b")
_DAYS = {"measurement": 7, "settled": 60, "task": 3}


def normalize(statement):
    return _WS.sub(" ", str(statement or "")).strip()


def episode_key(anchor, body):
    return "%s#%s" % (anchor, hashlib.sha256(body.encode()).hexdigest()[:12])


def _iso(text):
    m = _ISO.search(str(text or ""))
    if not m:
        return None
    when = m.group(1) + "T" + (m.group(2) or "00:00:00")
    if len(when) == 16:
        when += ":00"
    return datetime.fromisoformat(when).replace(tzinfo=timezone.utc).isoformat()


def frontmatter(text):
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    out = {}
    for line in text[3:end].splitlines():
        key, sep, value = line.strip().partition(":")
        if sep and key in ("name", "description", "type", "originSessionId", "modified"):
            out.setdefault(key, value.strip().strip('"').strip("'"))
    out["session"] = out.pop("originSessionId", None)
    return out


def source_date(fm, file_name):
    return _iso(fm.get("modified")) or _iso(file_name)


def memory_source(file_name, text):
    fm = frontmatter(text)
    head = normalize(fm.get("description") or fm.get("name") or file_name)
    mtype = fm.get("type")
    marker = "marked" if _MARKER.search(head) else None      # routes to the judge queue; never closes
    return {"headline": head, "bullets": [normalize(b) for b in apply_rules(strip_frontmatter(text))],
            "kind": "rule" if mtype == "feedback" else "finding", "type": mtype,
            "session": fm.get("session"), "date": source_date(fm, file_name), "marker": marker}


def split_statement(text):
    text = normalize(text)
    if len(text) <= 600:
        return text, None
    out = ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if len(out) + len(sentence) + 1 > 600:
            break
        out = (out + " " + sentence).strip()
    if not out:
        out = text[:597].rsplit(" ", 1)[0] + "..."
    return out, text[:4096]


def _items(lines, lo, hi):
    items, cur = [], None
    for n in range(lo, hi):
        line = lines[n]
        if line.startswith("- "):
            cur = [n + 1, [line[2:].strip()]]
            items.append(cur)
        elif cur is not None and line.strip():
            cur[1].append(line.strip())
    return [(first, normalize(" ".join(parts))) for first, parts in items]


def traps(skill_text):
    lines = skill_text.splitlines()
    lo = next(i for i, l in enumerate(lines) if "NAMING TRAPS" in l)
    hi = next(i for i, l in enumerate(lines) if i > lo and l.startswith("SETTLED"))
    return _items(lines, lo + 1, hi)


def standing_rules(skill_text):
    lines = skill_text.splitlines()
    lo = next(i for i, l in enumerate(lines) if "STANDING RULES" in l)
    hi = next(i for i, l in enumerate(lines) if "NAMING TRAPS" in l)
    return _items(lines, lo + 1, hi)


def doctrine_rules(text, source):
    out = []
    for line_no, header, body in handoff_blocks(text):
        for candidate in split_candidates(header, body):
            if classify(candidate) == "rule":
                out.append({"statement": normalize(candidate), "line": line_no, "source": source})
    return out


def handoff_author(header):
    m = _ROLE.search(header or "")
    if not m or m.group(1) == "Codex":
        return None
    return "operator" if m.group(1).lower() == "operator" else m.group(1).lower()


def verify_by(kind, valid_from, key):
    start = datetime.fromisoformat(valid_from)
    stagger = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % 14
    return (start + timedelta(days=_DAYS.get(kind, 30) + stagger)).isoformat()
```

`doctrine_rules` reports the BLOCK's first line (the dry run's granularity); Plan 3 N7 compares by `normalize(statement)`, never by line. In the dry run, replace the moved definitions with `from bot.brain_graph.import_sources import (...)` (it already inserts the repo root on `sys.path`).

- [ ] **Step 4: Run and see it pass**

Run: `python3 -m pytest -q -n 2 tests/test_brain_graph_import_sources.py && python3 scripts/research/brain_migration_dryrun.py --selftest`
Expected: PASS and the dry run's selftest OK.

- [ ] **Step 5: Commit** the two files and the test. Message: `feat(brain): import_sources, the pure parsers the import and the nightly share`.

---

### Task 3: `import_entities.py`, exact identifiers and sections

**Files:**
- Create: `bot/brain_graph/import_entities.py`
- Test: `tests/test_brain_graph_import_entities.py`

**Interfaces:**
- Consumes: `evidence.check` (knobs), `index/schema_registry.json`, `sniper.services.memecoin_chains.canonical_chain_id`.
- Produces:
  - `resolve_commits(tokens: set[str], repo) -> dict[str, str]`: token -> full SHA, one `git cat-file --batch-check` for every token; a non-commit or ambiguous token is absent.
  - `extract(text: str, *, commits: dict, tables: set, handles: set, knob_ok) -> list[dict]`: entity specs `{"type", "name", "section", "aliases"}`, deduplicated, provenance NOT included.
  - `fact_section(front: str, mtype: str | None) -> str` (D6).
  - `CURATED: dict[str, list[tuple[name, aliases, section]]]` for venue, provider, lane, process, gate, exit_rail.
  - `evidence_items(entities: list[dict]) -> list[dict]`: `sha` / `path` / `knob` / `column` evidence for the identifier entities, at most 8.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_brain_graph_import_entities.py
import subprocess
from pathlib import Path

from bot.brain_graph import import_entities as E

REPO = Path(__file__).resolve().parents[1]


def _sha_on_main():
    return subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD~1"], capture_output=True,
                          text=True, check=True).stdout.strip()


def test_only_real_commits_become_commit_entities():
    full = _sha_on_main()
    got = E.resolve_commits({full[:8], "deadbee1", "e1f95763"}, REPO)
    assert got == {full[:8]: full}          # a key-id hash and a made-up SHA are not commits


def test_extract_is_exact_and_sectioned():
    text = ("Fixed in `memecoin_positions.opened_recorded_at` on 4663 via MEMECOIN_REAL_CHASE_MAX_PCT, "
            "scripts/research/j7_shadow_read.py:12 and position 66413; the base rate is low.")
    ents = E.extract(text, commits={}, tables={"memecoin_positions"}, handles=set(),
                     knob_ok=lambda name: name == "MEMECOIN_REAL_CHASE_MAX_PCT")
    got = {(e["type"], e["name"], e["section"]) for e in ents}
    assert ("chain", "4663", "memecoin/4663") in got
    assert ("knob", "MEMECOIN_REAL_CHASE_MAX_PCT", "memecoin/gates") in got
    assert ("table", "memecoin_positions", "memecoin") in got
    assert ("column", "memecoin_positions.opened_recorded_at", "memecoin") in got
    assert ("path", "scripts/research/j7_shadow_read.py", "research/studies") in got
    assert ("position", "66413", "memecoin") in got
    assert not any(e["type"] == "chain" and e["name"] == "8453" for e in ents), "'base rate' is not Base"


def test_an_invented_knob_is_not_an_entity():
    ents = E.extract("MEMECOIN_MAX_MCAP_USD caps it", commits={}, tables=set(), handles=set(),
                     knob_ok=lambda name: False)
    assert not [e for e in ents if e["type"] == "knob"]


def test_fact_sections_follow_d6():
    assert E.fact_section("memecoin", "feedback") == "crew/methods"
    assert E.fact_section("other", "project") == "research/studies"
    assert E.fact_section("sports/markets", "finding") == "wallet-analyzer"
    assert E.fact_section("brain/infra", "project") == "infra/runtime"
```

- [ ] **Step 2: Run it and see it fail** (`ModuleNotFoundError`).

- [ ] **Step 3: Implement** `import_entities.py`: the D5 path and knob rule tables as ordered `(regex, section)` lists; `CHAINS = [("4663", ("robin", "robinhood", "Robinhood")), ("8453", ("Base",)), ("56", ("BSC", "bnb")), ("5042", ("Arc",)), ("solana", ("Solana", "sol"))]` matched as whole words, case-sensitive except the numeric ids, and a numeric id only when not part of a larger number or a `$` amount; the dry run's `HEX`, `KNOB`, `PATH`, `POSITION` (keyword form only) patterns; `resolve_commits` feeding tokens on stdin to `git cat-file --batch-check='%(objectname) %(objecttype)'` and keeping `commit` answers; `CURATED` seeded from the dry run's taxonomy (22 venues, 15 providers, 10 lane aliases, 10 processes, 22 gates, 16 exit rails), each row `(canonical name, aliases, section)`; `evidence_items` mapping commit -> `sha`, path -> `path`, knob -> `knob`, column -> `column`.

- [ ] **Step 4: Run and see it pass**; delete the `(?<![\d$])` guard on numeric chain ids and see the "base rate" / amount assertions fail.

- [ ] **Step 5: Commit.** Message: `feat(brain): exact identifier entities and sections for the import`.

---

### Task 4: `import_manifest.py`, the deterministic manifest

**Files:**
- Create: `bot/brain_graph/import_manifest.py`
- Test: `tests/test_brain_graph_import_manifest.py`

**Interfaces:**
- Consumes: Tasks 2 and 3; `gate.redact`.
- Produces:
  - `Sources` (dataclass): `handoff: str`, `handoff_archive: str`, `memory_files: dict[str, str]`, `supabase_rows: list[dict]`, `skill: str`, `skill_blame: dict[int, tuple[str, str]]` (line -> (author time ISO, commit subject)), `doctrine: dict[str, tuple[str, str]]` (file -> (text, last commit ISO)).
  - `build(sources, *, commits, tables, handles, knob_ok, broken_refs_fn, secret_values) -> Manifest` with lists `episodes`, `facts`, `queue`, `links` of plain dicts, in the D13 order, and `report` counts. `broken_refs_fn(text) -> list[dict]` returns the body's evidence items that check `broken` (injected so the build stays pure in tests).
  - Episode dict: `{"key", "kind", "body" (redacted), "author", "occurred_at", "anchor"}`.
  - Fact dict: `{"key", "slot", "draft" (a write_fact draft), "import_status", "valid_to", "judge_pending": bool, "episode_key", "same_source": bool}`. `judge_pending` marks a D3 text marker: the importer passes `judge_pending=True` to `write_fact` (Task 1; import session only), so no role can verify the fact before a session judges it, and the memory is also a judge-queue item.
  - Queue item: `{"section", "episode_key", "excerpt"}`. Link dict: `{"from_key", "to_key", "rel"}` (by fact key).

- [ ] **Step 1: Write the failing tests**

```python
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
    assert trap["draft"]["kind"] == "trap" and trap["draft"]["author"] == "lambda"
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
```

- [ ] **Step 2: Run it and see it fail** (`ModuleNotFoundError`).

- [ ] **Step 3: Implement** `build` exactly in this order, so the lists come out in D13 order:
  1. Episodes: HANDOFF blocks (kind `handoff_line`, author `handoff_author`, `occurred_at` from the block header), archive blocks, memory files (`legacy_memory`, anchor `memory/<file>`), Supabase direct rows (anchor `supabase:memories/<name>`, skipping the D1 duplicates and `session/*`), SKILL.md standing rules and traps (`doc`, anchor `SKILL.md`). Body = `gate.redact(original, secret_values)[0]`; key = `episode_key(anchor, original)`.
  2. Queue items: the dry run's HANDOFF `decision` candidates dated within 7 days of the newest block, its `rule` candidates, memory-body `retraction` candidates, retraction candidates that name a target, and every memory with `marker == "stale"`; section = `fact_section(front_of(text), None)`; excerpt = the first 120 chars.
  3. Facts, in D13 order: how-to-apply bullets, headlines (D3: `import_status` only from the Supabase status, `superseded` for `superseded` and `retired`; a text marker sets `judge_pending` and adds a queue item; `evidence_broken` when `broken_refs_fn` returns items), doctrine rules (`doctrine_rules` over each file, author `operator`, evidence `path <file>:<line>`), standing rules, traps (author from `skill_blame` subject `traps? (Alpha|Beta|Gamma|Lambda) paid for`). Every draft: statement/detail from `split_statement`, entities = provenance entity + `extract(...)` (+ `section` from `fact_section`), evidence = `[{"type": "episode", "ref": <episode key>}]` + `evidence_items(...)` + the broken body refs, `valid_from` per D8, `verify_by` per D8, `author_session = IMPORT_SESSION` on every draft. A duplicate `normalize` key within the build keeps the first and counts `duplicate_statement`.
  4. Links: Supabase `superseded_by` pairs as `{"from_key": <new fact key>, "to_key": <old fact key>, "rel": "supersedes"}` when both are in the manifest.
  The `episode` evidence ref is the episode KEY at build time; the importer swaps in the episode id when it writes (Task 5).

- [ ] **Step 4: Run and see it pass**; flip the D13 order (traps first) and see the order assertion fail.

- [ ] **Step 5: Commit.** Message: `feat(brain): the deterministic import manifest`.

---

### Task 5: `importer.py` and `scripts/brain_import.py`

**Files:**
- Create: `bot/brain_graph/importer.py`, `scripts/brain_import.py`
- Test: `tests/test_brain_graph_importer.py`

**Interfaces:**
- Consumes: Tasks 1-4; `api.add_episode`, `api.write_fact`.
- Produces:
  - `CachedStore(store)`: the `BrainStore` methods, with `find_entity`, `find_facts` served from a cache loaded by `rows_page` at construction and updated on every `insert`, `patch_fact`, `supersede`.
  - `run(store, manifest, *, repo, secret_values, now_fn, sleep_fn=time.sleep, max_rps=5.0) -> dict`: the report `{episodes_written, episodes_skipped, parts_completed, redaction_changed, facts_written, facts_skipped, facts_refused: {reason: n}, statuses: {status: n}, judge_pending, candidate_links, queue_tasks, links_written, links_present, stale_at_import, valid_from_unknown, window: [first, last], memory_facts: {memory name: [fact ids]}}`.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run it and see it fail** (`ModuleNotFoundError`).

- [ ] **Step 3: Implement** `importer.py`:
  - `CachedStore.__init__` pages `brain_entities` (all) and `brain_facts` (live statuses, `valid_to` null) with `rows_page`, indexing entities by `(type, name_lc)` and every alias, and facts by entity id. `find_facts(entity_ids, kinds, live_only)` returns the union, oldest first, the same contract as the RPC. Every other method delegates; `insert`, `patch_fact`, `supersede` update the cache from the returned row.
  - `run`: (0) with `--seed-methods` (idempotent): insert section `crew/methods` under `crew` if missing and append it to each `role/<name>` view's `query_spec.sections` if missing (one PATCH per view, `brain_views` is service-role UPDATE). (1) `episode_index` per anchor prefix, then write missing episodes in batches of 200 via `insert_many` (part 1 rows, then later parts with `parent_id`), completing partial keys per D2; build `episode_key -> id`. (2) `fact_index(IMPORT_SESSION)`; for each manifest fact in order, skip a held key; swap the episode key in `evidence` and `episode_ids` for the id; judgements = `{id: "refines"}` for cached candidate facts whose `episode_ids` share the episode (D10); `write_fact(cached, draft, repo=repo, interactive=False, judgements=..., import_status=..., valid_to=..., secret_values=...)`; count by status and refusal reason. (3) links by fact key; a duplicate refusal counts `links_present`. (4) queue tasks (Task 6). Throttle every server call to `max_rps`. `window` = the first and last `recorded_at` written.
  - `scripts/brain_import.py`: loads `.env` (`load_env_files(str(state_root()))`), reads the sources (HANDOFF files, the memory dir, `git blame --porcelain .claude/skills/handover/SKILL.md`, CLAUDE.md/AGENTS.md with `git log -1 --format=%cI`, Supabase `memories` via `bot.supabase_helper` in pages of 200 read-only, wallet handles via `get_readonly_connection`), resolves commits and knobs once, builds the manifest, then `--dry-run` runs `importer.run` on a seeded `FakeStore` (`brain_seed.seed`) and prints the report; `--live` requires `--i-am-alpha` and uses `CachedStore(RestStore(timeout=10))`; `--report <path>` writes the JSON report.

- [ ] **Step 4: Run and see it pass**; make `CachedStore.find_facts` delegate to the real store and see the linear-cost test fail; drop the D2 key check and see the second-run test fail.

- [ ] **Step 5: Commit.** Message: `feat(brain): the importer, idempotent, cached, with a dry run on FakeStore`.

---

### Task 6: Judge-queue batches (D11)

**Files:**
- Modify: `bot/brain_graph/importer.py`
- Test: `tests/test_brain_graph_importer.py` (append)

**Interfaces:**
- Produces: `importer.queue_tasks(queue: list[dict], *, import_day: str) -> list[dict]` (write_fact drafts, kind `task`, author `import`, one per section per 25 items, `verify_by = import_day + batch index days`), written by `run` after the facts.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2-4:** see it fail (`AttributeError`), implement (the detail line is `<episode key> <excerpt>`; the importer swaps keys for ids), see it pass.
- [ ] **Step 5: Commit.** Message: `feat(brain): judge-queue batches, one per section per day`.

---

### Task 7: The dry run on the real sources (Beta and Alpha read it)

No new code. On a flat book (research-load rule; the run is one process at nice 19):

- [ ] Run `nice -n 19 python3 scripts/brain_import.py --dry-run --report data/brain_import_dryrun.json` and paste the report.
- [ ] Write `docs/audits/2026-09-29-brain-import-dryrun.md` with: episodes and parts by kind; facts by kind and by status; refusals by reason (a secret refusal names the source, never the value); entities by type and section; the `judge_pending` share and candidate links per section; queue tasks per section; `stale_at_import`; `valid_from_unknown`; the boot cut (`views.boot` on the dry-run store for each role: facts cut at 500, and which kinds made the cut); duplicate statements; the Supabase/local duplicate count.
- [ ] Beta reviews the counts. Stop conditions (any one holds the live run for a decision): `judge_pending` over 60% of imported facts; a refusal reason other than `secret` above 1%; any role's boot showing no traps.

---

### Task 8: The live run, the zero re-run, and the embeddings (Alpha)

- [ ] A live probe first: `rows_page` with an `in.(...)` filter and `insert_many` of two `selftest` episodes against production (the phase-1 probe style), then the run: `nice -n 19 python3 scripts/brain_import.py --live --i-am-alpha --report data/brain_import_live.json`.
- [ ] Re-run the same command. Its report must print zero episodes, facts and links written.
- [ ] Create `scripts/brain_embed_backfill.py` (D14) with a `--selftest` that embeds three texts through a fake store and proves a second pass embeds nothing; run it live at nice 19.
- [ ] Record the report window, counts and the zero re-run in HANDOFF and as a `crew_report` episode.

---

## Self-review against the spec

- 4.3 import: facts from traps, headlines and how-to-apply bullets (Tasks 2, 4, 5); splits [R12] (D9, Task 2); judge queue [R18] (D11, Task 6); episodes in 1,800-char parts [R10, R11] (`add_episode`, Task 5); doctrine as read-only rules [R4] (D1, Task 2 `doctrine_rules`, author `operator`).
- 1.3 taxonomy and R1 exact resolution: Task 3. 2.1.2 checks: Task 1 (D4, `import_status`). 2.1.3 non-interactive judge_pending [R7]: `write_fact(interactive=False)`; same-source structure D10. 2.1.4 secrets: `redact` for episodes, refusal for facts, counted. 2.3 imported expiry stagger [R8]: D8, Task 2. 2.5 read filters: statuses per D3 are the spec's.
- 5.3 author is not verifier: D7. 5.5 short pages: keyset paging below max-rows. 5.8 rollback: the edge-case list, point 8.
- Beta's review of 1b8467a5 (B1 marker-keyed index and rollback, B2 import statuses for crew-authored traps, D3 markers never close and retired -> superseded, D6 `crew/methods`, D11 detail cap, dateless HANDOFF authors) is folded in. Still open: D4 (a gate change for every writer), D7 (trap authorship from blame), D12 (the cache's concurrency gap left to Plan 3).
