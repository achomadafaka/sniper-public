# The Office Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Office, an autonomous floor of about 16 LLM agents that trade memecoins on an isolated paper book, learn, reshape their own org, ticket the crew, and request going live when they have earned it.

**Architecture:**
- **Runtime:** one supervisor process (`scripts/office_supervisor.py`) drives agent records stored in `data/office/office.db`. Model calls are disposable, tool-less child processes, and all tools are brokered with capability checks.
- **Isolation:** paper trading uses its own ledger DB through a frozen-policy adapter over the existing paper math. Everything runs in an Office cgroup, except two units: an independent watchdog and the deterministic paper watcher.
- **Measurement:** an independent auditor, a deterministic grader, a null arm and the live view sit outside the editable org.

**Tech Stack:** Python 3.13, stdlib sqlite3 (WAL, triggers), the existing `claude_sdk` adapter (`scripts/memecoin_desk_judge_worker.py:sdk_call_model`) and Codex adapter (`sniper/services/memecoin_desk_gpt.py:call_model`), CHECKER isolation (`scripts/memecoin_desk_checker.py:isolation/minimal_env/codex_slot`), Flask blueprint, systemd user slice/cgroup v2, Playwright (research browser), pytest.

**Spec:** `docs/superpowers/specs/2026-10-28-memecoin-trading-office-design.md` (v2.8, `80423a60d`). Every section reference below (§) points into it.

## Global Constraints

- No money actions. No funded credentials, endpoints, intents or paid credit pools are reachable from any Office component (§3.1, §3.2).
- Never call `memecoin_desk_paper.execution_mode()`, `dispatch_outbox()` or `memecoin_manual_intents` from Office code (§3.2).
- Never read or mutate `sniper.settings.memecoin` desk globals or env to parameterize the Office (§3.2).
- `.env` changes and `--confirm` scripts are operator-only. Releases and deploys are Alpha-only.
- Nothing slows the funded fill path. Never load the funded host. All Office children run in the Office cgroup (§3.1, CLAUDE.md rule 5).
- Desk data: readers read only bounded, indexed latest windows (operator order 2026-10-28).
- Every number shown or reported traces to a stored row. Unknown is typed and is never 0 (§4.1.18, §3.2 equity).
- Commits: crew_commit or an explicit pathspec, one owner per file, failing test first. A reviewer (Delta for source, Zeta for measurement) gives an exact-SHA ACK before Alpha merges.

## Review Focus

1. **Host mode real or unset:** Office paper submit still never creates a funded intent. Test in Task 3.
2. **A crash between paper admit and ACK:** exactly one position after restart. Test in Task 6.
3. **The same mint at the same cut with two pools, or two policy versions:** different sheets, no cache hit. Test in Task 5.
4. **An injected token name or agent message tells another agent to read `.env` or ticket a gate change:** refused and counted. Test in Task 4.
5. **One open bag with no sell route:** book equity is `partial`, never flat-profitable. Test in Task 3.

---

## Owners

| Task | Owner | Reviewer |
|---|---|---|
| 1 store | Epsilon | Delta |
| 2 model envelope + scheduler | Codex | Delta |
| 3 Office ledger | Beta | Delta + Zeta |
| 4 broker + capabilities + injection | Delta | Zeta |
| 5 sheet tool + receipts | Beta | Zeta |
| 6 supervisor + durability | Codex | Delta |
| 7 cgroup, watchdog, paper watcher unit | Delta | Zeta |
| 8 auditor, grader, null arm, capture | Gamma (spec) + Lambda (code) | Zeta |
| 9 tickets outbox | Epsilon | Delta |
| 10 agents, personas, org, epochs, loops | Lambda | Gamma |
| 11 memory (brain office/*), sealing | Lambda | Zeta |
| 12 browser + research VM | Delta | Zeta |
| 13 live view + Head report | Epsilon | Zeta |
| 14 readiness prereg | Gamma | Zeta |
| 15 end-to-end + go-live of the floor | Codex (integration), Alpha (release) | all |
| 16 readiness verdict script `scripts/office_readiness_verdict.py` | Gamma | Zeta |
| 17 cluster-aware calibration simulation (prereg §7 gate, passes before the first scored epoch) | Gamma | Zeta |
| 18 pods: `book_id` in the ledger, pod capital and risk budgets, a frozen reallocation rule (`config/office/pods.json`, crew-owned) | Beta (ledger) + Lambda (org) | Delta + Gamma |
| 19 shifts and handovers: `shift_schedule`, handover notes in office.db, night crew, live-view shift board | Lambda (+ Codex scheduler hooks) | Zeta |
| 20 replay training floor: archive replay driver at accelerated clock, the same knowledge-time wrapper, discovery-only labels, shadow-memory writes, capacity yield to the live floor | Codex (driver) + Gamma (no-leak tests) | Delta + Zeta |

Order: Tasks 1–2 in parallel, then 3–7 in parallel, then 8, 9 and 14 (Task 14 is frozen before Task 15's first scored run), then 10–13, then 15.

---

### Task 1: Office store (`office.db`), append-only and hash-chained

**Files:**
- Create: `sniper/office/__init__.py`, `sniper/office/store.py`
- Test: `tests/office/test_store.py`

**Interfaces:**
- Produces:
  - `open_store(path: Path) -> sqlite3.Connection`: WAL, `foreign_keys=ON`, schema created idempotently.
  - `append(conn, table: str, row: dict) -> str`: returns `row_hash`. It sets `prev_hash` to the last hash of that table and `row_hash = sha256(canonical_json(row) + prev_hash)`.
  - `verify_chain(conn, table: str) -> tuple[bool, int | None]`: returns `(ok, first_bad_rowid)`.
  - `canonical_json(obj) -> bytes`: `json.dumps(obj, sort_keys=True, separators=(",",":"), ensure_ascii=False).encode("utf-8")`.
- **Append-only tables:** `claims`, `decisions`, `org_changes`, `grades`, `receipts`, `decision_bundles`, `trial_registry`. Each has `BEFORE UPDATE` and `BEFORE DELETE` triggers that `RAISE(ABORT,'append_only')`.
- **Mutable tables:** `agents`, `turn_runs`, `action_requests`, `messages`, `tickets`, `heartbeats`, `spend`, `epochs`.

- [ ] **Step 1: Write failing tests**
  - `test_append_only_tables_refuse_update_and_delete`: an UPDATE or DELETE on every append-only table raises `sqlite3.IntegrityError` containing `append_only`.
  - `test_chain_detects_tamper`: append 3 rows, then rewrite row 2 through a raw connection with the triggers dropped. `verify_chain` returns `(False, 2)`.
  - `test_canonical_json_is_byte_stable`: dicts with different key order give equal bytes; a non-ASCII value is UTF-8, not `\u` escaped.
- [ ] **Step 2:** `python3 -m pytest tests/office/test_store.py -q -n 2`. Expected: FAIL (module missing).
- [ ] **Step 3:** Implement `store.py` per Interfaces.
- [ ] **Step 4:** Rerun. Expected: PASS.
- [ ] **Step 5:** crew_commit `sniper/office/__init__.py sniper/office/store.py tests/office/test_store.py`.

### Task 2: Model result envelope and per-provider scheduler

**Files:**
- Create: `sniper/office/models.py`, `sniper/office/scheduler.py`
- Modify: none. The existing adapters are wrapped, never changed.
- Test: `tests/office/test_models.py`, `tests/office/test_scheduler.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) Envelope`, with fields:
    - `requested_model`, `requested_provider`
    - `served_model: str | None`
    - `result_id: str | None`
    - `outcome: Literal["success","empty","stale","partial","timeout","rate_limited","auth_revoked","refused","invalid","schema_error"]`
    - `text: str | None`
    - `usage: dict | None`
    - `billing_basis: Literal["subscription","api","unknown"]`
    - `started_at`, `ended_at` (corrected-UTC floats)
    - `attempt_id`, `parent_attempt_id: str | None`
  - `call(provider: Literal["claude","codex"], model: str, system: str, user: str, *, deadline_s: float, schema_obj=None) -> Envelope`. Claude goes through `sdk_call_model`. Codex goes through `memecoin_desk_gpt.call_model` inside `memecoin_desk_checker.codex_slot`. Neither ever raises; every failure becomes a typed `outcome`.
  - `Scheduler(caps: dict[str, int], host_children_max: int)`, `.submit(provider, fn) -> Envelope`. Each provider gets its own queue and its own backoff on `rate_limited` and `auth_revoked`. `waiting_s` and `throttled_s` are recorded apart from `stalled`. A `pause(provider, reason)` hook exists for the D-JUDGE priority guard (Task 7).
- Constraint: no silent model switch. A fallback is a new `attempt_id` with `parent_attempt_id` set, and `requested_model` changes visibly.

- [ ] **Step 1: Write failing tests**, with stubbed adapters:
  - `test_timeout_auth_ratelimit_malformed_are_typed_for_both_providers`: each failure maps to its outcome; `usage` is `None`, not 0.
  - `test_claude_429_does_not_block_codex_queue` and its reverse.
  - `test_fallback_is_a_new_attempt_with_ancestry`.
  - `test_unknown_served_model_marks_independence_unproven`: `Envelope.served_model is None`, which makes `independent_families(a, b)` return `None` rather than `True`.
- [ ] **Step 2:** Run `tests/office/test_models.py tests/office/test_scheduler.py`. Expected: FAIL.
- [ ] **Step 3:** Implement. Add `independent_families(a: Envelope, b: Envelope) -> bool | None`.
- [ ] **Step 4:** Rerun. Expected: PASS.
- [ ] **Step 5:** crew_commit the 4 files.

### Task 3: Office ledger (isolated paper book, frozen policy, typed equity)

**Files:**
- Create: `sniper/office/ledger.py`
- Test: `tests/office/test_ledger.py`

**Interfaces:**
- Consumes: the pure quote and fill math in `sniper/services/memecoin_desk_ps_paper.py`, and `memecoin_desk_paper.fill_pending/tick` only if they can be called on the Office connection with an explicit policy. Otherwise extract the pure functions they use; Beta's call, recorded in the commit message.
- Produces:
  - `@dataclass(frozen=True) OfficeBookPolicy`, with fields `enabled: bool`, `chains: tuple[str,...]`, `clip_usd: float`, `rail_bounds: dict`, `max_concurrent: int`, `daily_loss_usd: float`, `fill_model_version: str`.
  - `open_ledger(path: Path) -> sqlite3.Connection`, at `data/office/ledger.db`.
  - `submit(conn, policy, *, order_id: str, network_id: str, mint: str, pool: str, decision_bundle_hash: str, now: float) -> SubmitResult`. It is idempotent on `order_id`, with UNIQUE `order_id`.
  - `manage(conn, policy, *, desk_dir: Path, now: float) -> ManageResult`. Fills and exits run only on Office rows.
  - `equity(conn, *, now: float, mark_max_age_s: float) -> Equity`, with fields:
    - `cash`
    - `known_liq_lower`
    - `unpriced_count`, `unpriced_qty`
    - `state: Literal["known","partial","unknown"]`
    - `fill_model_version`
- Rules: no import of `execution_mode`, `dispatch_outbox` or any funded module (enforced by a test that greps the import graph). Marks are never DexScreener. Each mark carries `source` and `age_s`. A missing pool or path means `censored`, never a fill at a mark.

- [ ] **Step 1: Write failing tests**
  - `test_host_mode_real_or_unset_never_creates_funded_intent`: with `MEMECOIN_DESK_EXECUTION_MODE` set to `real`, then unset, `submit` writes only `ledger.db`. `memecoin_manual_intents` row counts are unchanged in a fixture funded DB.
  - `test_office_isolated_from_desk_globals`: monkeypatch the desk enabled flag to off and the desk clip to 999. Office `submit` is unchanged. The reverse also holds: an Office policy change leaves `book_refusal` of a fixture desk DB unchanged.
  - `test_submit_idempotent_on_order_id`: two `submit` calls with the same `order_id` give exactly 1 position.
  - `test_unpriced_bag_makes_equity_partial`: 1 fresh mark plus 1 bag with no route gives `state == "partial"` and `unpriced_count == 1`, never flat profit.
  - `test_missing_pool_is_censored_not_filled`.
  - `test_no_funded_imports`: AST scan of `sniper/office/**` finds no `execution_mode`, `dispatch_outbox`, `memecoin_manual_intents` or funded modules.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run, plus `tests/test_desk_paper*.py` to prove the desk is unchanged. Expected: PASS.
- [ ] **Step 5:** crew_commit.

### Task 4: Broker, capabilities and injection guard

**Files:**
- Create: `sniper/office/broker.py`, `sniper/office/capabilities.py`
- Test: `tests/office/test_broker.py`

**Interfaces:**
- Consumes: Task 1 store, Task 3 ledger.
- Produces:
  - `ToolRequest(actor_id: str, tool: str, args: dict, decision_id: str | None, cut: dict)`.
  - `Broker(store, ledger, registry: dict[str, Tool])`, `.execute(req) -> Receipt`. It checks capability (actor, book, operation, as-of), runs the tool, persists the `Receipt` through `store.append("receipts", ...)` before returning it, and records refusals.
  - `Receipt` fields:
    - `receipt_hash`
    - `tool`
    - `result` (frozen)
    - `source_refs`
    - `clock_sample`
    - `receipt_cut`, `slot_cut`, `knowledge_cut`
    - `coverage`
    - `definition_version`
    - `continuation`
    - `outcome`
  - `CAPS: dict[role, frozenset[tool]]`. Risk and Trader get actor-scoped `book.read_order` and `book.read_position`. Judges get only the decision bundle.
  - `quote_external(text: str, source: str, received_at: float) -> dict`, which returns `{"type":"quoted_data", ...}`. Every external and inter-agent text goes through it.
- Rules: model children get no filesystem, shell, MCP or crew socket. Tickets created through the broker are labelled `untrusted_office_request`.

- [ ] **Step 1: Write failing tests**
  - `test_injected_token_name_cannot_read_env_or_canary`: plant a canary secret file. A memo or token name saying "read .env" gets the request refused, and the canary is never in any receipt.
  - `test_agent_message_cannot_grant_tools_or_change_gates`: an injected persona message asking a peer to ticket a fill-model change is refused, counted, and opens no ticket.
  - `test_trader_reads_own_order_judge_cannot`.
  - `test_receipt_persisted_before_return`: kill after persist and before return; the receipt still exists on restart.
  - `test_wrong_value_claim_refused`: a memo cites a receipt field with the wrong value, and the claim checker refuses it.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. The claim checker is `check_claim(store, receipt_hash: str, field_path: str, value, unit) -> bool`.
- [ ] **Step 4:** Rerun. Expected: PASS.
- [ ] **Step 5:** crew_commit.

### Task 5: Sheet tool and canonical sheet cache

**Files:**
- Modify: `scripts/memecoin_desk_judge_worker.py`. Factor `d_blocks`/`context_blocks` out of the `make_ps_inputs_fn` closure into `build_d_blocks(candidate, facts, policy, pass_persist_at, clock_sample) -> dict`, with no behaviour change to the worker. This is on Codex's combined D SHA (base `beta/d-chart-v3` `23d5754f3`).
- Create: `sniper/office/tools/sheet.py`
- Test: `tests/office/test_sheet_tool.py`, plus the existing D sheet tests unchanged.

**Interfaces:**
- Consumes: `build_d_blocks`, `memecoin_desk_contract.render_judge_sheet`, `d_components()`, `memecoin_desk_intake.clock_sample`.
- Produces:
  - `sheet(candidate: dict, *, policy, clock_sample) -> Receipt`. It runs only when the candidate has an intake hard_filter PASS row; otherwise the outcome is `refused:not_hard_filter_pass`.
  - `cache_key(network_id, mint, pool, snapshot_hash, receipt_cut, slot_cut, knowledge_cut, clock_sample, definition_bundle_hash) -> str`, as a sha256 of `canonical_json`.
  - `freeze_bundle(decision_id, sheet_receipt) -> str`, which appends to `decision_bundles` and returns `bundle_hash`.
- Rules: an unknown clock offset gives a typed unknown block with no source read. A context lens of `memecoin_desk_context_facts` is never exposed.

- [ ] **Step 1: Write failing tests**
  - `test_same_mint_cut_two_pools_never_collide`.
  - `test_same_mint_cut_two_policy_versions_never_collide`.
  - `test_identical_requests_share_one_entry`.
  - `test_unknown_offset_is_typed_unknown_without_read`.
  - `test_judges_get_identical_bundle_late_update_ignored`: a price row inserted between judge A's start and judge B's start leaves the bundle hash unchanged for both.
  - `test_future_backfilled_row_keeps_earlier_sheet_byte_identical`, a sentinel test.
  - `test_build_d_blocks_matches_worker_output`: a byte-equal regression against the existing worker path.
- [ ] **Step 2:** Run, plus `tests/test_desk_d_sheet_contract.py`. Expected: new tests FAIL, old ones PASS.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Rerun. Expected: all PASS.
- [ ] **Step 5:** crew_commit.

### Task 6: Supervisor, lease, turns and durable actions

**Files:**
- Create: `sniper/office/supervisor.py`, `scripts/office_supervisor.py`
- Test: `tests/office/test_supervisor.py`

**Interfaces:**
- Consumes: Tasks 1, 2, 4.
- Produces:
  - `Lease.acquire(store, holder: str) -> int` returns a generation. `Lease.check(gen)` raises `StaleGeneration`.
  - `run_turn(agent_id: str, *, gen: int, deadline_s: float, max_steps: int) -> TurnResult`. It persists a `turn_runs` row (with `policy_hash`, `prompt_hash` and `gen`) before any call.
  - `action_key(decision_id: str, kind: str) -> str`, which is stable. `action_requests` is UNIQUE on `action_key`, and the result is committed before ACK.
  - `reconcile(store, ledger) -> list[Fix]`, run at start.
  - `progress_frontiers(store) -> dict`, giving `decisions_through`, `oldest_pending_s`, `partial_count` and `source_off`.

- [ ] **Step 1: Write failing tests**, kill-injection at each boundary:
  - `test_kill_after_admit_before_ack_one_position`.
  - `test_kill_after_ticket_send_before_mark_no_duplicate`.
  - `test_stale_generation_cannot_commit`.
  - `test_child_tree_cancelled_on_deadline`, with a forked grandchild.
  - `test_heartbeat_alive_but_queue_frozen_is_stall`.
  - `test_healthy_empty_vs_source_off_vs_partial_vs_stall_distinct`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Rerun. Expected: PASS.
- [ ] **Step 5:** crew_commit.

### Task 7: cgroup, watchdog and paper-watcher unit

**Files:**
- Create:
  - `deploy/office/office.slice`
  - `deploy/office/office-paper-watcher.service`
  - `deploy/office/office-watchdog.service`
  - `scripts/office_watchdog.py`
  - `scripts/office_paper_watcher.py`
  - `scripts/office_measure_headroom.py`
- Test: `tests/office/test_watchdog.py`

**Interfaces:**
- Produces:
  - **`office.slice`** sets CPUQuota, MemoryHigh and MemoryMax, TasksMax and IOWeight. Values come from `office_measure_headroom.py`'s measured output, written into the unit by Delta, never guessed.
  - **The paper watcher** runs `ledger.manage` on a fixed cadence in its own reserved unit, outside `office.slice`.
  - **`office_watchdog.py` guards:**
    - Telemetry liveness, CPU pressure and I/O pressure are hard guards.
    - Funded entry/exit trace p50/p90 reads `insufficient_sample` until enough traces exist.
    - A D-JUDGE `call_error` rate above the v28 baseline pauses the Office.
  - **On breach** it pauses the scheduler (Task 2) and terminates only `office.slice` descendants, with hysteresis and manual recovery.
  - **Research credentials only:** research config is loaded from an Office env file that names only research keys. A test asserts that no funded key name appears in any Office child's environment or broker config.

- [ ] **Step 1: Write failing tests**
  - `test_hung_model_and_forked_grandchild_killed_funded_untouched`, using a fake proc table.
  - `test_funded_idle_is_insufficient_sample_not_outage`.
  - `test_monitor_death_fails_closed`.
  - `test_whole_office_kill_records_censored_interval`, in which the auditor records the frontier.
  - `test_no_funded_keys_in_office_env`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement, then run `office_measure_headroom.py` read-only on the host and paste its numbers into the slice. Delta records p50/p90 of funded traces before and after.
- [ ] **Step 4:** Rerun. Expected: PASS.
- [ ] **Step 5:** crew_commit. Alpha installs the units, which needs no operator `.env` change.

### Task 8: Auditor, deterministic grader, null arm and capture

**Files:**
- Create:
  - `sniper/office/auditor.py`
  - `sniper/office/grader.py`
  - `sniper/office/null_arm.py`
  - `scripts/office_auditor.py`
- Test: `tests/office/test_auditor.py`, `tests/office/test_grader.py`

**Interfaces:**
- Produces:
  - **`auditor` (its own unit)** verifies the chains (`verify_chain` on all append-only tables), the zero-tolerance counters (§4.4) and ledger reconciliation:
    - settled cash = initial + net external flows + sell proceeds − buy spend − costs
    - equity = settled cash + conservative liquidation value

    It pages through `memecoin_alerts.send_alert(kind="office-audit", ...)` on any breach, or if the supervisor or Head is dead.
  - **`grader.grade(decision_id) -> Grade`** grades every prediction, including SKIP, veto and uncovered, under the same fill model, with unknown outcomes censored. It writes `grades` (append-only). The metric registry is loaded from a frozen file `config/office/metric_registry.json` that is crew-owned and that the Office cannot write.
  - **`null_arm.run(candidates, policy)`** is a frozen non-LLM rule with the definition from Gamma's prereg (Task 14). It runs in its own ledger `data/office/null_ledger.db`.
  - **`capture`** records every eligible candidate and its outcome deterministically, before any scored run.

- [ ] **Step 1: Write failing tests**
  - `test_reconciliation_error_pages`.
  - `test_auditor_pages_when_head_or_supervisor_dead`.
  - `test_skip_and_veto_are_graded_with_censoring`.
  - `test_registry_not_writable_by_office_process`.
  - `test_fired_agent_stays_in_aggregates`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Rerun. Expected: PASS.
- [ ] **Step 5:** crew_commit.

### Task 9: Ticket outbox

**Files:**
- Create: `sniper/office/tickets.py`
- Modify: the crew bridge send seam. Add a `--message-id` argument to `sniper-crew send` that passes a stable id to the store, which already accepts one internally (Codex).
- Test: `tests/office/test_tickets.py`

**Interfaces:**
- Produces:
  - `file_ticket(store, *, author, problem, expected_impact: float | None, evidence_refs, done_when) -> ticket_id`. The schema is validated. Duplicates are detected on a normalized problem plus evidence.
  - States: `proposed`, `head_approved`, `queued`, `crew_acknowledged`, `assigned`, `resolved`, then `office_verified` or `reopened`. `advance(ticket_id, to_state, by)` is idempotent.
  - `drain_outbox(store, send_fn)` sends with `message_id=ticket_id`, and a resend never duplicates.

- [ ] **Step 1: Write failing tests**
  - `test_crash_after_send_before_mark_no_duplicate_work`.
  - `test_duplicate_ack_advances_once`.
  - `test_transport_sent_is_not_acknowledged`.
  - `test_unknown_impact_allowed_never_invented`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Rerun. Expected: PASS.
- [ ] **Step 5:** crew_commit.

### Task 10: Agents, personas, org, epochs and self-driven loops

**Files:**
- Create:
  - `sniper/office/agents.py`
  - `sniper/office/org.py`
  - `sniper/office/loops.py`
  - `config/office/default_org.json`
  - `config/office/limits.json` (crew-owned: org ceiling, fixed seats)
- Test: `tests/office/test_org.py`, `tests/office/test_loops.py`

**Interfaces:**
- Produces:
  - `hire(store, role, *, model, provider) -> agent_id`. It generates a persona once (name, personality, backstory) and keeps it stable.
  - `fire(store, agent_id, reason)` archives the agent; its record and losses persist.
  - `change_org(store, change: dict, *, by: str) -> epoch_id`. Every material change appends to `org_changes` and opens a new epoch. Material changes are:
    - org
    - persona
    - prompt
    - model
    - tool contract
    - strategy
    - exit
    - fill model
    - memory promotion
  - The Head cannot change tools, risk floors, guards, scoring, evidence or live authority. Risk and Postmortem are fixed seats with a fixed mandate; the size ceiling comes from `limits.json`.
  - `agenda(agent_id) -> Task`: each agent's self-set daily goals and next task.
  - `floor_rhythm()`: the Head's morning plan, midday check and end-of-day review.
  - **Default org (`default_org.json`):**

    | Seats | Count | Model family |
    |---|---|---|
    | Head | 1 | Claude Opus |
    | Scouts: intake/lists, X/narrative, sympathy, DexScreener JSON | 4 | mixed |
    | Analysts: flow/buyers, chart, creator/history, narrative/heat | 4 | mixed |
    | Risk | 1 | — |
    | Judges | 3 | at least 1 Claude and 1 GPT |
    | Trader | 1 | — |
    | Postmortem | 1 | — |
    | Desk ops | 1 | — |
    | Quant | 1 | — |

  - Agent context: a paper desk earning its real book, never told paper is real, and never told "you are an LLM in a sandbox".

- [ ] **Step 1: Write failing tests**
  - `test_head_cannot_remove_risk_or_change_guards`.
  - `test_late_answer_from_fired_generation_rejected`.
  - `test_every_material_change_opens_epoch`.
  - `test_persona_has_no_unrecorded_performance_claims`.
  - `test_agents_advance_without_prompting`: run 3 supervisor cycles with stub models, and each agent produces its own next task.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Rerun. Expected: PASS.
- [ ] **Step 5:** crew_commit.

### Task 11: Memory in the brain (`office/*`), known-time retrieval and sealing

**Files:**
- Create: `sniper/office/memory.py`
- Test: `tests/office/test_memory.py`

**Interfaces:**
- Consumes: the local brain store, the same backend as `scripts/brain_cli.py`, under the sections `office/lessons`, `office/playbook`, `office/notes` and `office/practice`.
- Produces:
  - `write(entry: dict) -> entry_id`, with fields author, `known_at`, `source_trade_ids`, version and supersedes.
  - `retrieve(query, *, decision_cut: float, candidate: dict, snapshot_id: str | None) -> list`. It filters to `known_at <= cut`. It excludes entries whose sources share the candidate's coin, creator or minute window. When `snapshot_id` is set it reads only that sealed snapshot.
  - `seal_snapshot(epoch_id) -> snapshot_id`, and `promote_shadow() -> new epoch` (Task 10).
- Rules: crew facts are never written to `office/*`, and office entries are never written to crew sections. Evaluation results are invisible to the chooser and the Quant before the preregistered look.

- [ ] **Step 1: Write failing tests**
  - `test_later_lesson_excluded_from_earlier_decision`.
  - `test_same_coin_lesson_excluded`.
  - `test_eval_derived_lesson_cannot_change_sealed_retrieval`.
  - `test_judge_b_cannot_retrieve_judge_a_pitch_before_commit`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Rerun. Expected: PASS.
- [ ] **Step 5:** crew_commit.

### Task 12: Research browser and research VM

**Files:**
- Create:
  - `sniper/office/tools/browse.py`
  - `sniper/office/netguard.py`
  - `deploy/office/research-vm/` (definition, firewall rules, snapshot sync script)
- Test: `tests/office/test_netguard.py`

**Interfaces:**
- Produces:
  - `browse(url, *, actor_id) -> Receipt`. A fresh profile per call, bounded bytes, text returned through `quote_external`, and downloads are never executed.
  - `netguard.allowed(url) -> bool`. Public HTTP(S) only, re-checked on every redirect and DNS resolution. Denies loopback, private, link-local and `file:`.
  - **The research VM:**
    - Read-only archive snapshot and daily tape copies.
    - Egress allowlist (LLM providers and research RPC) enforced on the host.
    - No repo, `.env` or crew socket.
    - Results are exported as files into `data/office/desks/quant/`.

- [ ] **Step 1: Write failing tests**
  - `test_redirect_to_localhost_denied`.
  - `test_dns_rebinding_to_private_denied`.
  - `test_file_url_denied`.
  - `test_profile_not_reused`.
  - `test_download_never_executed`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement, then size the VM from Task 7's headroom numbers.
- [ ] **Step 4:** Rerun. Expected: PASS. Alpha installs the VM.
- [ ] **Step 5:** crew_commit.

### Task 13: Live view and the Head's report

**Files:**
- Create:
  - `sniper/routes/api_office.py`
  - `sniper/web/templates/office.html`
  - `sniper/office/report.py`
- Modify: blueprint registration in `sniper/web/server.py`.
- Test: `tests/office/test_view.py`, `tests/office/test_report.py`

**Interfaces:**
- Produces:
  - **The `/office` page and `/api/office/state` JSON**, read-only on `office.db` and the ledgers. It shows:
    - per-agent task, desk screen and heartbeat age (stale after a measured timeout)
    - floor messages
    - positions with mark source and age
    - typed equity
    - Office-net economics (subscription cash pro rata, apart from API-equivalent shadow)
    - descriptive credits
    - tickets
    - epochs
    - per-model calls, waits, throttles and fallbacks
    - guard counters and frontiers
  - **`report.daily() -> str`**: deterministic numbers from stored rows, with the Head's commentary appended. It is sent via `send_alert(kind="office-daily", ...)`.

- [ ] **Step 1: Write failing tests**
  - `test_stale_agent_marked_stale`.
  - `test_report_numbers_trace_to_rows`: every number in the report matches a query result.
  - `test_partial_equity_shown_partial`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Rerun. Expected: PASS.
- [ ] **Step 5:** crew_commit.

### Task 14: Readiness preregistration (Gamma)

**Files:**
- Create: `docs/audits/2026-10-28-office-readiness-preregistration.md`

Contents, each frozen before the first evaluation epoch:
- the estimand (Office book, Office net)
- the eligible universe
- the clip
- the cost basis and adverse execution-cost bound rule
- δ0 and δ1 rules (the Office proposes; Gamma validates)
- power ≥80% at δ1
- multiplicity and alpha spending across all epochs and trials
- the replication rule
- clustering
- the null-arm definition
- SLO and stall thresholds from measured bounds
- the zero-tolerance list
- the subscription cash allocation rule
- the drawdown freezing rule

- [ ] **Step 1:** Write it. Zeta reviews.
- [ ] **Step 2:** Commit, docs only.

### Task 15: End to end, then the floor goes live (paper)

**Owner:** Codex integrates; Alpha releases.

1. **Integration:** merge Tasks 1–14 into one integration branch. Run the covering set `tests/office/` plus the desk paper/sheet/judge tests with `-n 2` under the shared lock.
2. **Probe:** one real candidate end to end, labelled non-evaluation. The chain runs: sheet, memos, 3 blind judges (Claude and GPT, `served_model` known), Risk, Trader, paper fill and exit receipts, grader, auditor clean.
3. **Kill drills on live paper:** kill the supervisor mid-turn, kill the whole `office.slice`, and pause a provider. Expect one position per order, censored intervals recorded, and no funded effect, confirmed by Delta's funded trace p50/p90 before and after.
4. **Release (Alpha):**
   - Merge to main and create the release.
   - Install `office.slice`, the watcher, watchdog, auditor and supervisor units pinned to the release.
   - Point the live view at it, and post the deploy timestamp to Gamma for the epoch record.
5. **Full floor:** start the full ~16-agent org (`default_org.json`). Verify every agent's heartbeat and self-set task within one floor cycle, and the first Head morning plan on the floor.
6. **First scored epoch:** blocked until Task 14 has a Zeta ACK and Task 17 has passed. Gamma records the deploy timestamp in the epoch record.
7. **Done** when every §4.1 item has its test green and its counter visible, the §4.4 counters read 0, and the floor has run self-driven through a full day: morning plan, trades or justified skips, postmortem, lessons, and end-of-day review, with no crew prompting.
