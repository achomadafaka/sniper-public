# The brain as a verified knowledge graph (Supabase-primary)

Status: APPROVED. Sections 1 and 2 approved by the operator on 2026-09-28 (~13:45Z and ~13:58Z);
the whole spec and the [Rn] review amendments inside sections 1-2 approved ~14:15Z (section 7).
section 3 onward written for review. Author: Alpha. Inputs: Beta's surface inventory, value cut
and contradiction dry run (`docs/audits/2026-09-28-brain-surface-inventory.md`,
`scripts/research/brain_migration_dryrun.py`, commits 57719d33, 8b59a6fd, c8d909bf).

Review amendments (Beta's adversarial review, 2026-09-28 ~14:00Z, ruled on by Alpha ~14:10Z) are
folded in and tagged [R1] to [R21]; the map is in section 8. A tag inside sections 1-2 reads
"review amendment, needs operator re-approval", because it changes text the operator approved.

## 0. Why

The operator's order (2026-09-28): stop handing sessions a long copy-paste handover. Move live
context and knowledge into the Supabase brain, keep local files to a bare minimum, let every
session write and every session verify, and above all keep the brain from going stale or piling
up bad information.

What exists today, measured (Beta, 13:14-13:34Z):

- `memories` holds 975 active rows. 523 (54%) are nightly one-way MIRRORS of local markdown
  (`scripts/sync_session_findings.py`, cron 04:15 local). The mirror rewrites a row whenever
  content or status differs from the file, and the file's status defaults to active, so any
  hygiene change made in Supabase is reverted overnight.
- Direct Supabase writes stopped on 2026-09-22. Everything since arrives up to 24 h late.
- `session/HANDOFF` is one 1.82 MB row (plus a 628 KB archive). Embeddings cover the first
  ~512 tokens of a row, so 68% of active content characters are invisible to semantic search.
- 15 active rows declare themselves SUPERSEDED/RETRACTED in their own text and are still served
  by the prompt-time RAG hook.
- Nothing has been retired or superseded since 2026-07-08. `superseded_by` is set on 0 active rows
  (61 of the 112 non-active rows carry it). [R21]
- Boot costs ~220 KB of unqueried text per session (CLAUDE.md, MEMORY.md, handover template,
  HANDOFF tail). The handover SKILL.md is 81 KB, mostly 178 traps.
- Pointer rot: 57 of 625 checkable memories (9.1%) name a SHA not on origin/main, a missing path,
  or an unknown knob. This is a LOWER bound for the new gate: it was measured with looser checks
  than 2.1.2 (a knob anywhere in code, a path in the tree or on disk, no column check). Phase 1
  re-measures the baseline with the gate's own checks before it is used as a target. [R5]
- The dominant failure is CONTRADICTION, not rot: every load-bearing wrong row in Beta's graded
  samples was an older rule reversed by a newer one ("git stash the fix" vs never-stash). No
  pointer check can see that.

## 1. Shape (approved)

Designs read: Zep/Graphiti (arXiv 2501.13956), Mem0 (2504.19413), A-MEM (2502.12110), Letta
memory blocks, the MCP knowledge-graph memory server. What we take from each:

| Source | Taken | Not taken |
|---|---|---|
| Zep/Graphiti | three tiers (episode, entity+fact, community); bi-temporal facts; a contradiction closes the old fact's validity instead of deleting it; entity resolution before insert; hybrid retrieval (BM25 + cosine + graph walk) with rank fusion | Neo4j; LLM extraction on every message |
| Mem0 | the update decision ADD / UPDATE / INVALIDATE / NOOP against the top-k similar existing facts; invalid, never deleted | DELETE |
| A-MEM | every note carries keywords, tags and links; a new note may refresh its neighbours' summaries | automatic rewriting of neighbours' content |
| Letta | small, labelled, size-capped context blocks shared by every agent | hand-edited blocks (last write wins) |
| MCP memory | atomic observations, one fact each | untyped strings |

### 1.1 Tables (Supabase Postgres, schema `public`, migration `migrations/supabase_032_brain_graph.sql`)

<!-- schema:external=brain_episodes -->
<!-- schema:external=brain_entities -->
<!-- schema:external=brain_facts -->
<!-- schema:external=brain_links -->
<!-- schema:external=brain_sections -->
<!-- schema:external=brain_views -->
<!-- schema:external=brain_health -->

**brain_episodes**: immutable provenance. One row per raw event.

| column | type | meaning |
|---|---|---|
| id | uuid pk | |
| kind | text | handoff_line, operator_order, crew_report, command_output, commit, doc, transcript_excerpt, legacy_memory |
| body | text | verbatim, at most 1,800 chars per part, so the embedding covers the whole part (bge-small reads 512 tokens) [R11, review amendment, needs operator re-approval] |
| parent_id | uuid | the first part of a multi-part episode; NULL on a single-part episode [R11, review amendment, needs operator re-approval] |
| part_no | int | order within the parent, from 1 [R11, review amendment, needs operator re-approval] |
| author | text | session role (alpha/beta/gamma/lambda/codex/operator/cron/import) [R17, review amendment, needs operator re-approval] |
| session_id | text | the Claude session that wrote it, when known |
| occurred_at | timestamptz | when it happened (world time) |
| recorded_at | timestamptz | server default now() |
| source_ref | text | file:line, SHA, or message id |
| embedding | vector(384) | bge-small, as `memories` |

Immutable: a BEFORE UPDATE OR DELETE trigger raises. A grant cannot do this, because every session
writes with the service_role key, which bypasses RLS and holds every grant. [R15, review amendment, needs operator re-approval]

**brain_entities**: the things facts are about.

| column | type | meaning |
|---|---|---|
| id | uuid pk | |
| type | text | from the seed taxonomy (1.3) |
| name | text | canonical name |
| aliases | text[] | e.g. chain `4663` / `robin` / `Robinhood` |
| summary | text | <= 400 chars, regenerated from current facts, never hand-written |
| section_id | uuid | primary section |
| embedding | vector(384) | |
| created_at, updated_at | timestamptz | |

Unique `(type, lower(name))`. Resolution before insert [R1, review amendment, needs operator re-approval]:

- Identifier types (knob, table, column, path, commit, chain, address, position, mint, wallet)
  resolve by exact name or alias only. A cosine threshold would merge different things. Measured
  with bge-small:
  - `MEMECOIN_REAL_STOP_PCT` vs `_TP_PCT`: 0.977
  - `..._GRACE_AFTER_HIT_S_4663` vs `_8453`: 0.991
  - `memecoin_positions` vs `memecoin_position_marks`: 0.968
  - `opened_at` vs `opened_recorded_at`: 0.948
  - a real knob vs an invented name for it: 0.993
- Descriptive types (venue, provider, process, lane, gate, spec): full-text and cosine only
  SUGGEST an existing entity. A merge is a judged `same_as` link, never automatic.

**brain_facts**: one claim per row. The unit of verification and expiry.

| column | type | meaning |
|---|---|---|
| id | uuid pk | |
| kind | text | rule, trap, decision, settled, finding, measurement, task, observation. There is no `config` kind: a config DECISION (who set a knob and why) is kind `decision`, and the knob's value itself is derived (1.2), never stored [R14, review amendment, needs operator re-approval] |
| statement | text | one sentence, <= 600 chars (fits the embedding window) |
| detail | text | optional, <= 4 KB |
| entity_ids | uuid[] | at least one |
| polarity | smallint | +1 do/is, -1 do-not/is-not, 0 neutral; used by the contradiction candidate stage |
| status | text | unverified, verified, verified_partial, disputed, superseded, retracted, refuted, evidence_broken |
| valid_from, valid_to | timestamptz | world time: when the claim became / stopped being true |
| recorded_at, expired_at | timestamptz | brain time: when the brain learned / retired it |
| verify_by | timestamptz | expiry; set from kind (2.3) |
| evidence | jsonb | list of {type: sha/path/knob/column/query/episode/doc, ref, note, check: ok/unchecked/pending_push/broken} [R5, review amendment, needs operator re-approval] |
| judge_pending | bool | a contradiction candidate that no session has judged yet (2.1.3) [R7, review amendment, needs operator re-approval] |
| author | text | role; a self-asserted string (see 5.3) |
| author_session | text | |
| verified_by | text | role; CHECK verified_by <> author |
| verified_at | timestamptz | |
| episode_ids | uuid[] | provenance |
| version | int | optimistic concurrency |
| embedding | vector(384) | on statement only |
| search_vector | tsvector | on statement + detail |

**brain_links**: typed edges. `(from_id, from_kind, to_id, to_kind, rel, author, created_at)`
with rel in supports, contradicts, supersedes, depends_on, derived_from, same_as, part_of.

**brain_sections**: a hand-seeded tree. `(id, parent_id, slug, title, summary, summary_at)`.
Seed: memecoin/{solana/{slot-lane,j7,onchain},4663,56,8453,5042,exits,gates,sizing},
infra/{deploy,runtime,rpc,db,clock}, research/{methods,studies}, crew/{alpha,beta,gamma,lambda},
brain/{doctrine} [R4, review amendment, needs operator re-approval], hyperliquid, crypto5m,
wallet-analyzer. Section summaries are regenerated from member facts
(map-reduce), never typed.

**brain_views**: definitions, not content. `(name, description, char_limit, query_spec jsonb)`.
A view is assembled on read. Seed views: `role/<name>`, `live-state`, `open-work`, `settled`,
`traps/<section>`, `operator-pending`.

Overflow rule [R20, review amendment, needs operator re-approval]. The 178 traps alone are 72 KB against a ~12 KB boot, so a view cuts. It keeps,
in order:
1. disputed and STALE facts;
2. by kind: settled > trap > rule > decision > finding > measurement;
3. facts whose entities appear in the session's last 20 episodes;
4. the most recently recorded.

The last line says how many facts were cut and which `brain entity` or `brain ask` command shows
them.

**brain_health**: one row per nightly run (2.4).

**Access** [R15, review amendment, needs operator re-approval]:
- RLS on every `brain_*` table, with a SELECT policy for reads and no anon or authenticated write
  policy (the 2026-06-20 hardening pattern).
- Every new RPC gets `REVOKE EXECUTE ... FROM PUBLIC, anon, authenticated`. Supabase auto-grants
  EXECUTE on new public functions; that is the supabase_031 lesson.
- The migration ships as a pair: `supabase_032_brain_graph.sql` and
  `supabase_032_rollback_brain_graph.sql` (repo convention; 031 is the latest today).

### 1.2 Derived, never stored

Live state (what is deployed, what runs, what `.env` sets, which SHAs are pushed, open positions,
heartbeat) is computed at read time by `brain live` from git, `.runtime` symlinks, `ps`, the
heartbeat, the settings and the ledger. It cannot go stale because it is never written. A snapshot
of it becomes an episode at each handover, for history only.

Knob VALUES [R13, review amendment, needs operator re-approval]:
- `.env` names alone cannot answer what handovers carry (J7 Solana mode = shadow, 4663 grace = 0).
- Non-secret values are resolved through `sniper.settings` after `load_env_files(state_root)`. A
  bare settings read returns the CODE DEFAULT, which is narrower than production.
- Names matching URL, KEY, TOKEN, SECRET, PRIVATE, PASS or SEED are shown by name only.
- A value whose `.env` line changed after the runner started is flagged `pending restart`, because
  the running process loaded `.env` at its start.

### 1.3 Entity taxonomy seed (Beta's dry run, unique / mentions)

actor 6/6,891; chain 5 (+10 aliases)/4,963; commit 1,939/4,364; process 10/2,556; exit rail
16/2,071; venue 22/1,874; path 930/1,616 (code/test/doc/runtime); provider 15/1,466; wallet
169 handles + 189 addresses/1,670 (one entity, addresses as aliases); knob 463/1,161; lane
10 aliases/1,064; table 114/782; position 163/461; gate 22/430. Added by hand: spec, audit, task,
release, mint, doctrine [R4, review amendment, needs operator re-approval]. Raw credentials are never stored (2.1.4). Key-id hashes from
`solana_rpc_pool.key_id` (e.g. e1f95763) are NOT secrets: operator orders name keys by them, so they
are allowed as `credential_id` entities. [R21, review amendment, needs operator re-approval]

## 2. Verification and staleness (approved)

### 2.1 The write gate: one tool, `scripts/brain.py write` (and a Postgres RPC behind it)

1. **Evidence required.** At least one evidence item, except kind `observation`, which must carry
   an `episode_id`. Missing evidence: refused, with the reason.
2. **Machine checks on write** [R5, review amendment, needs operator re-approval]. Each evidence item gets `ok`, `unchecked`, `pending_push` or
   `broken`.
   - **sha**: `ok` when reachable from origin/main after a `git fetch`. Reachable only from local
     HEAD is `pending_push` (builders commit locally and Alpha pushes later); the nightly check
     re-runs it. An unknown object is `broken`.
   - **path**: a tracked path is checked at origin/main. An untracked runtime path (`data/`,
     `logs/`, `.runtime/`) is checked on disk, and absent is `unchecked`: a `.pid` exists only
     while its process runs.
   - **knob**: `ok` when any source file at origin/main names it, with a per-chain numeric suffix
     stripped. `sniper/settings` alone is too narrow: 70 of the 430 knob names in today's corpus
     live elsewhere (`SNIPER_STATE_ROOT`, `MEMECOIN_RESEARCH_HELIUS_RPC_URL`, per-chain URL knobs).
   - **table.column**: `ok` when `index/schema_registry.json` has it. The registry does not cover
     `pool_ticks`, `evm_position_pools`, `volume_feed`, `block_times` or any Supabase table, and
     traps cite them, so a table outside the registry is `unchecked` until phase 1 extends it.

   A write is refused only for a `broken` item, which is named. A tool, git or network error is
   `unchecked`, never `broken`.
3. **Contradiction pass (Beta, c8d909bf).** Candidate stage [R3, review amendment, needs operator re-approval]: facts that share an entity AND
   a key.
   - Kinds compare within a GROUP, not only the same kind. Normative is rule, trap, decision and
     settled; descriptive is finding and measurement. Both known reversals cross kinds: a
     How-to-apply rule vs a standing rule, and the alt-short finding vs a doctrine decision.
   - Keys are backticked code, knob names, and a curated ACTION list: `git stash`, `git add -A`,
     `git commit -a`, `--amend`, `rebase`, `pkill -f`, `pgrep -f`, the Agent tool and subagents,
     `pytest -n`, GeckoTerminal, mainnet-beta, `--with-backup`, `:memory:` and `utcnow`. The list
     lives in the tool and grows by a judged write. Backticks and knobs alone lose the known agents
     reversal, because "Reserve general-purpose Agent" carries neither.
   - Ambiguous words ("agents" meaning the crew, ".env", "restart") are not keys.

   Cosine alone does not separate the candidates: 574 of 588 same-key pairs sit at cos >= 0.75,
   and 0 of 10 graded pairs at >= 0.80 were real reversals. So the candidates go to a JUDGE. The
   judge is the writing session itself: the tool prints the candidates and the session must answer
   each with one of duplicate / refines / supersedes / contradicts-unresolved / unrelated. There is no API key for a separate model on this host (`.env` names checked), and
   the writer has the context. Outcomes: duplicate = NOOP; refines = ADD + `derived_from` link;
   supersedes = ADD, old fact gets `valid_to` = new `valid_from`, status superseded, `supersedes`
   link; contradicts-unresolved = both `disputed` + `contradicts` link.

   **Non-interactive writers** [R7, review amendment, needs operator re-approval] cannot answer the judge: the import, the nightly job and an
   outbox replay. Their writes land `unverified` with `judge_pending = true`, and the candidate
   pairs are stored. The next interactive session in that section resolves them from `brain queue`.

   **Doctrine** [R4, review amendment, needs operator re-approval]. CLAUDE.md and AGENTS.md rules are mirrored in as read-only `rule` facts
   (author operator, evidence `CLAUDE.md:<line>`, section `brain/doctrine`) so the pass can see
   them. In the dry run, 186 of the 587 candidate pairs had a doctrine side, and the alt-short
   reversal came from CLAUDE.md. The files stay the source. The nightly job supersedes a doctrine
   fact whose line changed.
4. **Secret scan.** Refuse statements, details, evidence AND episode bodies that match key/token
   patterns or an RPC URL with a key in its path or query (the funded-pool URLs are the known
   trap). The imported HANDOFF and memory episodes pass the same scan. Key-id hashes from
   `solana_rpc_pool.key_id` are allowed. [R21, review amendment, needs operator re-approval]
5. **Concurrency.** Updates pass the `version` they read; a mismatch is refused and re-read.
6. **Size.** statement <= 600 chars, detail <= 4 KB. Longer material is an episode or a doc.
7. **Clock.** The server stamps `recorded_at`; the tool fills `valid_from` from the evidence or
   the episode, never from a typed time.

### 2.2 Peer verification

- New facts are `unverified`. Only a DIFFERENT role may set `verified`, `verified_partial` or
  `refuted` (DB CHECK plus RPC guard). A refutation carries a reason and an evidence item.
- Every `brain boot` hands the session a quota: re-check up to 3 unverified facts in its own
  sections, oldest first. The session re-runs the evidence (the tool prints the commands) and
  records the verdict. During the migration the quota is 10 per boot, plus one per-section batch
  a day. [R8, review amendment, needs operator re-approval]
- Traps and rules are verified by MECHANISM, not by replay [R9, review amendment, needs operator re-approval]. The verifier shows that the
  function, column, file or code line the fact names still resolves at origin/main and still
  behaves as stated, and pastes the grep or schema lookup as the output. The past incident ("Cost
  on 2026-09-23 ...") is evidence of why, not something to re-run. Without this rule every trap
  stays `verified_partial` forever.
- An author may not verify their own fact in another session's name.

### 2.3 Expiry by kind (`verify_by`)

| kind | verify_by |
|---|---|
| measurement | 7 d |
| rule, trap, decision, finding | 30 d |
| settled | 60 d |
| task | until closed; stale after 3 d without an update |
| observation | never expires; never shown in views, search only |

The clock starts at `recorded_at` for a new write. For an IMPORTED fact it starts at its
`valid_from` (the source date), staggered by up to 14 days [R8, review amendment, needs operator re-approval]. Otherwise the import stamps 1,410
facts on one day and every trap and rule expires together. At 3 per boot and roughly 10-20
restarts a day (unsure), clearing them takes 24-47 days, longer than the 30-day expiry.

A fact past `verify_by` still loads, marked STALE, and enters the verification queue. Nothing
expires into deletion.

### 2.4 Nightly check (`scripts/brain_nightly.py`, cron, flag-only)

Re-runs the machine evidence on every active fact. A check that FAILS sets `evidence_broken`; a
check that cannot run is `unchecked`, never broken [R6, review amendment, needs operator re-approval]. Cannot run means a tool, git, Supabase or
network error, or an absent runtime file.

It also:
- re-checks `pending_push` SHAs;
- re-runs the contradiction candidate stage across each entity and queues new pairs for the next
  session's judge;
- lists disputed facts older than 24 h and unverified facts older than 72 h;
- regenerates entity and section summaries;
- writes one `brain_health` row.

It never deletes and never changes a status except to `evidence_broken`. SQL evidence runs
read-only through `get_readonly_connection`, one query at a time, with a per-query timeout, and
never as a correlated subquery on the tick or marks tables (the research-load rule).

### 2.5 Read filters

Default reads return `verified`, `verified_partial`, `unverified`, `disputed` and
`evidence_broken` facts with `valid_to IS NULL` and `expired_at IS NULL`, each tagged with its
status. `evidence_broken` and STALE carry a warning tag and are never hidden: one false nightly
failure must not remove a trap from boot [R6, review amendment, needs operator re-approval]. `superseded`,
`retracted`, `refuted` only in `--history`. Status is a column; text inside a statement is never
used as a filter.

### 2.6 Brain health is measured like any other system

Share verified; age of the unverified queue (p50/p90); disputed count and age; evidence failure
rate (baseline re-measured in phase 1 with the gate's checks; the 9.1% used looser ones [R5, review amendment, needs operator re-approval]);
facts per section; facts past `verify_by`. One dashboard tile reads the
latest `brain_health` row.

## 3. Tool surface, boot and handover (for review)

One CLI, `scripts/brain.py`, over the Supabase REST API (the existing `bot/supabase_helper.py`
transport) with the service key from `.env`. Subcommands:

- `brain boot <role>`: prints, within ~12 KB: the role block, `brain live` (derived), open work
  for the role, operator-pending decisions, settled items and traps for the role's sections,
  disputed and STALE items in those sections, and the verification quota. Every fact line shows
  its status and short id.
- `brain live`: the derived state only.
- `brain ask "<question>" [--section s]`: BM25 + cosine + one-hop graph walk, reciprocal-rank
  fusion, top 15 facts with status, plus the 3 best episodes.
- `brain entity <name>`: every current fact on it, its links, its section.
- `brain write --kind ... --entity ... --evidence ... "<statement>"`: the gate in 2.1, including
  the judge prompt for contradiction candidates.
- `brain verify <id> verified|partial|refuted --evidence ...` and `brain queue`.
- `brain episode --kind ... "<body>"`: append provenance (HANDOFF lines go here).
- `brain task open|update|close`: tasks as entities with facts.
- `brain handover <role>`: re-derives live state; lists what this session wrote that is still
  unverified; asks the session to close or update its tasks; records a `handover` episode; prints
  the short restart note (3.2).

Also exposed as MCP tools later (not in phase 1).

Embeddings are pinned to 4 threads and run at nice 19 in the CLI, the hooks and the nightly job.
fastembed defaults to every core, which is 32 on this funded host. The dry run took 3.5 min for
1,524 texts at those settings. The CLI loads the model lazily, only when a command embeds, because
a cold load costs about 1-2 s. [R19]

### 3.1 Hooks

- SessionStart cannot know the role [R2]. It fires before the operator pastes the note, and its
  input is only session_id and source. So it prints the ROLE-FREE part of boot: `brain live`,
  disputed facts, operator-pending decisions and the queue size. It is fail-open (3 s timeout,
  exit 0) and prints the local snapshot banner if Supabase is unreachable.
- The role part loads when the session runs `brain boot <role>` from the note, or from a
  UserPromptSubmit match on "You are <Role>" in the first prompt.
- SessionStart also fires with source=compact, so the role-free boot is re-injected after every
  compaction.
- UserPromptSubmit: the existing `brain_ambient.py rag` is re-pointed to `brain_facts` with the
  2.5 filters; it stops reading `memories` once the migration is done.

### 3.2 The onboarding note (what the operator pastes)

Under 15 lines: the role name, the crew roster, the three standing rules that must hold before
anything loads (one shared checkout, no money actions, `.env` only on the operator's yes), and
`python3 scripts/brain.py boot <role>`. Nothing volatile. It changes only when the crew changes.

### 3.3 What stays local (bare minimum)

- `CLAUDE.md`: rules and pointers; the "read HANDOFF.md" instruction is replaced by `brain boot`.
- The onboarding note (in chat, not a file).
- `data/brain_snapshot.json`: written by every successful `brain boot`/`brain handover`; read-only
  fallback when Supabase is down, loudly labelled with its age.
- `data/brain_outbox.jsonl`: writes made while Supabase is down; replayed by the next successful
  call, each through the full gate (a replayed write can still be refused). A replay is
  non-interactive, so its contradiction candidates land `judge_pending` (2.1.3).
- Claude Code's auto-memory `MEMORY.md` shrinks to a pointer to `brain boot` (the harness loads it
  whatever we do).
- The harness's own system prompt tells every session to write local memory files, and CLAUDE.md
  cannot switch that off [R16]. After cut-over, NEW local memory files are imported one-way as
  `legacy_memory` episodes, never facts and never a status, so nothing written by habit is lost.
  A session that wants a fact runs `brain write`.

## 4. Migration (phased; each phase reversible)

1. **Schema + tool + gate** (`supabase_032` plus its rollback), with tests against a Supabase
   branch. The old `memories` table and the nightly mirror keep running untouched. Also in this
   phase:
   - extend `index/schema_registry.json` to the tick and tape DBs [R5];
   - re-measure the staleness baseline with the gate's own checks [R5].
2. **Seed**: sections, entity taxonomy, views.
3. **Import** (Beta's cut, 8b59a6fd).
   - **Facts, 1,410:** 178 traps, ~875 memory headlines and 357 How-to-apply bullets. All enter as
     `unverified`; their contradiction candidates are stored with `judge_pending` and judged in
     batches by crew sessions.
   - **Splits** [R12]: statements over 600 chars (7 traps, 3 headlines and up to 40 How-to-apply
     bullets, max 3,742) are split into a statement that keeps the rule and a detail. The detail is not
     embedded.
   - **Judge queue, 1,515:** the 1,402 LLM-queue rows (recent HANDOFF decisions and rules,
     memory-body retractions, the 70 REVIEW rows) plus the 113 retraction candidates [R18]. Most of
     those 113 name an entity (a commit, path or position), not a fact, and a `supersedes` link
     needs a fact at both ends.
   - **Episodes** [R10]: the 2,977 HANDOFF blocks, the 973 memory bodies and the archive's blocks,
     about 4,000+, split into parts of at most 1,800 chars. The 28,661 figure counts sentence
     fragments INSIDE those episodes; a fragment is never an episode.
   - **Doctrine** [R4]: CLAUDE.md and AGENTS.md rules enter as read-only rule facts.
4. **Cut-over**:
   - The ROLE-FREE boot goes in SessionStart; the note carries `brain boot <role>` (3.1) [R2].
   - The handover skill is rewritten to `brain handover`.
   - HANDOFF.md becomes append-only history, with new entries written as episodes.
   - The `sync_session_findings.py` mirror is STOPPED, otherwise it reverts statuses nightly. Its
     replacement is the one-way import of new local memory files as episodes (3.3) [R16].
   - CLAUDE.md is edited in the same step [R16]: step 0 "read HANDOFF.md", "keep HANDOFF.md
     current" and "read knowledge/ FIRST" (dead since 2026-06-20) become `brain boot` /
     `brain episode`.
   - The untracked, diverged `.agents/skills/handover/SKILL.md` (44 KB) is retired [R17].
   - `strategy_agent_observations` (a live 4-hourly writer) stays a separate table [R21].
5. **Retire**: the `memories` rows that were imported get `superseded_by` pointing at their fact;
   the RAG hook reads `brain_facts` only.

Rollback at every phase: the old path keeps working until phase 4, and phase 4 is one hook line
plus one cron line to restore.

## 5. Edge cases (CLAUDE.md rule 5)

1. **Money path**: none. The brain is never read or written on the entry, exit, sizing or signing
   path, and no funded process imports `scripts/brain.py`. A test asserts that no module under
   `sniper/` imports it.
2. **Fill speed**: not touched (1). Session hooks have a 3 s cap and fail open.
3. **Concurrency**: four sessions write at once. Row versions (2.1.5); inserts are independent;
   the contradiction judge runs against a snapshot, and a supersede re-checks the old row's
   version at commit. Two sessions superseding the same fact: the second is refused and re-reads.
   Identity [R15]: `author` is a self-asserted role string, and every session holds the same
   service key. So `verified_by <> author` is honor-system plus audit (author_session is
   recorded), not an access control.
4. **Absent vs zero vs unknown**: an absent `verify_by` is invalid (NOT NULL); `valid_to` NULL
   means still valid; an unchecked evidence item is `unchecked`, never `ok`; a Supabase read
   error at boot shows "BRAIN UNREACHABLE", never an empty brain.
5. **Dependency failures**: Supabase down, slow, 429, partial page. Reads: snapshot fallback with
   its age. Writes: outbox; a replayed write is non-interactive, so it lands `judge_pending` (2.1.3).
   A short page (PostgREST range cut) is detected by the Content-Range header, which PostgREST
   sends only with `Prefer: count=exact`; `bot/supabase_helper.py` does not send it today [R21].
   A short page is never read as "no more facts" (the batch-returns-short trap).
6. **Units and identity**: chain aliases resolve to one entity (`4663`, `robin`, `Robinhood`);
   commit SHAs are stored full, matched by prefix >= 8; times are UTC timestamptz only.
   Identifier entities resolve by exact name or alias, never by cosine (1.1) [R1].
7. **The sample**: brain-health shares describe what sessions chose to write. The staleness
   baseline counts only checkable rows (625 of 975); unmeasurable rows are reported as their own
   count, never folded in.
8. **Rollback**: 4 above. A half-applied migration leaves both brains readable; the tool refuses
   to write facts until the phase-1 schema check passes.
9. **Bad information, the operator's named risk**: (a) wrong but plausible facts: peer
   verification + evidence re-run; (b) reversed rules: contradiction pass + judge; (c) rot: nightly
   evidence re-run + `verify_by`; (d) pile-up: dedup on write, 600-char statements, supersede
   instead of add, observations out of views; (e) injected instructions: brain content is printed
   as data inside a labelled block, never as instructions; (f) secrets: 2.1.4.
10. **Self-verification collusion**: the same model in two roles can agree by habit. The verifier
   must re-run the evidence command and paste its output into the verdict; a verdict with no
   output is `verified_partial` at most. For traps and rules, the pasted output is the MECHANISM
   check (2.2) [R9].

The runnable check left behind: `tests/test_brain_gate.py` (refuses no-evidence, a SHA not on
main, a secret, a self-verify; a supersede closes `valid_to`), plus the "no `sniper/` import"
test. Each is proven to fail with its guard removed.

## 6. Out of scope (phase 1)

Automatic LLM extraction from transcripts (979 MB, unread); MCP server exposure; a web UI for the
graph; community detection by clustering (sections are hand-seeded).

## 7. Operator decisions (2026-09-28 ~14:15Z, "yes all")

1. The judge is the writing session; a peer verifies later. No separate judge model.
2. The import backlog is judged by the crew in idle time, at the migration quota (2.2) [R8].
3. Codex is READ-ONLY until it has a role note; it does not write facts or verify. [R17]
4. The amendments tagged inside sections 1-2 ([R1], [R4], [R5], [R6], [R7], [R8], [R9], [R11],
   [R13], [R14], [R15], [R17], [R21]) are re-approved.
5. Execution: Beta and Lambda build from private branches task by task; Alpha reviews, merges,
   applies the migration and deploys nothing without a flat book.

## 8. Review map (Beta, 2026-09-28)

| tag | finding | where |
|---|---|---|
| R1 | cosine entity resolution merges distinct identifiers | 1.1 brain_entities, 5.6 |
| R2 | SessionStart cannot know the role | 3.1 |
| R3 | same-kind and backtick-only keys miss known reversals | 2.1.3 |
| R4 | the pass is blind to CLAUDE.md/AGENTS.md rules | 1.3, 2.1.3, 4.3 |
| R5 | machine checks refuse true facts; baseline not comparable | 0, 1.1 evidence, 2.1.2, 2.6, 4.1 |
| R6 | evidence_broken hid facts; errors must be unchecked | 2.4, 2.5 |
| R7 | non-interactive writers cannot judge | 1.1 judge_pending, 2.1.3, 5.5 |
| R8 | import expiry wave vs verification throughput | 2.2, 2.3, 7 |
| R9 | traps verified by mechanism, not replay | 2.2, 5.10 |
| R10 | 28,661 are fragments, not episodes | 4.3 |
| R11 | episode parts: columns and embedding-sized parts | 1.1 brain_episodes |
| R12 | statements over 600 chars at import | 4.3 |
| R13 | knob values, not names only | 1.2 |
| R14 | kind `config` dropped; a config decision is kind `decision` with a derived value | 1.1, 2.3 |
| R15 | service key bypasses grants; author self-asserted; RLS/REVOKE/rollback | 1.1, 5.3 |
| R16 | harness auto-memory and CLAUDE.md edits at cut-over | 3.3, 4.4 |
| R17 | Codex as an actor; the diverged .agents handover skill | 1.1, 4.4, 7 |
| R18 | the 113 retraction links go to the judge queue | 4.3 |
| R19 | 4 threads, nice 19, lazy model load | 3 |
| R20 | view overflow ranking | 1.1 brain_views |
| R21 | small corrections (superseded_by, Content-Range, key-id hashes, observations table) | 0, 1.3, 2.1.4, 4.4, 5.5 |
