# The Office: an autonomous memecoin trading floor (design v2, 2026-10-28)

Owner: operator. Written by Alpha from the operator brainstorm of 2026-10-28. v2 folds in the crew reviews: Beta (tools), Gamma and Zeta (measurement), Codex (integration), Delta (isolation and host), Epsilon (tools and data). Lambda's review is pending and is folded in when it lands. This is the crew's primary goal from now on.

## 1. Intent

Everything Sniper has today is infrastructure: feeds, the PumpSwap tape, desk sheet blocks, the judge worker, the paper engine and the brain. It has no intelligence that owns the outcome. The Office adds that owner: a standing team of LLM agents with one goal, to be profitable trading memecoins. They are given everything we have built and are free to use it as they see fit. When they believe something else would make them more money, they ticket the build crew for it. They improve themselves without being prompted: they set their own goals, decide what works and what doesn't, rewrite their own methods, and reshape their own org.

It should look and feel alive, like a real trading floor of the best people on Wall Street, competing to stay profitable and keep their jobs. The current desk (v28, cohort D, sheet blocks, judge worker) becomes their toolbox. It keeps running in the background and the Office does not wait on it.

## 2. What the Office is

### 2.1 Goal, money and authority

- **One goal for every agent:** profit. Today that means paper equity; later, a real book.
- **Paper first.** Agents know they trade a paper book: they are a new desk earning its real book. They are never told paper is real.
- **Going live is their request, your decision.** The Head files a live-money ticket with evidence. Gamma checks it against the frozen bar (section 4.3). Only then does the operator see it. Real money moves only after the operator's yes, through the funded runner's existing gates. A live approval binds the exact strategy, prompt, risk, tool and model versions. Any material change after that needs a new approval.

### 2.2 People

- **Default org at start, about 16 agents.** The office may redesign it at any time.
  - **Head of Office:** runs the floor, plans the day, hires and fires, approves tickets. It is the only agent that reports to the operator.
  - **Four scouts:** intake/lists; X and narrative; sympathy/vamp; DexScreener attention via the JSON API only.
  - **Four analysts, one lens each:**
    - flow/buyers
    - chart
    - creator/history
    - narrative/heat
  - **Risk:** red flags and veto. In the evaluated book Risk may only veto: an entry is the full frozen clip or nothing. Size cuts exist only in a labelled shadow book (prereg Am1).
  - **Three judges across both model families:** each decides ENTER, WAIT or SKIP with a probability, blind to the others.
  - **Trader:** turns one adjudicated decision into one paper order and manages its exit.
  - **Postmortem:** explains outcomes and writes lessons. It does not compute the official grade (section 4.2).
  - **Desk ops:** watches office health and files infrastructure tickets.
  - **Quant:** backtests and builds analysis on the research VM (section 2.4).
- **Identity:** each agent has a name, a personality that fits its role, a career record and its own memory. Agents live the role. Personas carry only performance claims taken from their stored record.
- **Competition:**
  - Agents are ranked descriptively by contribution. The Head can promote, demote, fire and hire, meaning a new persona with a new prompt.
  - Fired agents' records, losses and trials are archived and stay in every aggregate. A rename or restart never resets them.
  - Hiring and firing move freely while the office learns. Every such change opens a new epoch (section 4.2).
- **Fixed seats:** Risk and Postmortem cannot be removed, only restaffed. The Head cannot rewrite their mandate. Org size has a ceiling set in crew-owned config.

### 2.3 How they work

- **Self-driven:**
  - Each agent runs its own work loop: it sets daily goals, picks its next task, and follows up on its own work.
  - The Head runs a morning plan, a midday check and an end-of-day review on the shared floor.
  - Nobody prompts them. The supervisor only wakes agents and keeps them alive.
- **Talking:** a shared floor channel plus direct messages. Every model talks to every other model. Messages from other agents are treated as data, the same as external text: one agent cannot steer another's tools or authority.
- **Browsing for intel:**
  - A headless research browser and web search, used for news, X, Telegram and launch pages.
  - Everything read comes back as quoted data with its source URL and receipt time, never as instructions.
  - No logins, credentials, posting or purchases.
  - **Network enforcement by the broker, outside the browser:**
    - Public HTTP(S) destinations only, validated at every redirect and DNS resolution. No localhost, private or link-local addresses, local dashboard or `file:` URLs, and no host network.
    - A fresh uncredentialed profile per session.
    - Bounded navigation and download bytes.
    - Downloads are never imported or executed.
    - The VM network allowlist is enforced outside guest code the same way.
    - Tests: redirect and DNS rebinding to a secret-canary local service, a `file:` URL, browser profile reuse, hostile VM output.
- **Memory (the Office's own brain):**
  - Agents write lessons, playbook entries, market notes and best practice into an append-only, hash-chained `memory_entries` table in office.db. It carries a trusted known_at stamped by the core and is searchable through FTS5 (Alpha ruling 2026-10-28, option B).
  - Each entry records its author, the time it became known, its supporting trade ids, its version and what it supersedes.
  - Retrieval for a decision only returns entries known before that decision's cut, and never entries derived from the same coin, creator or minute window.
  - Independent judges cannot see each other's pitch or verdict through memory, the floor or the tool cache until all have committed.
  - The crew brain is not written by the Office. A later optional export can copy promoted lessons into the crew brain read-only, for the crew and operator to search; agents never read that copy back.
- **Self-learning loop:**
  - Outcomes go to Postmortem, which feeds lessons into memory and the playbook, which feed the next decisions.
  - The office edits its own prompts, strategies and playbook. Each change is versioned with its reason, and each decision records the versions it used.
- **Tickets to the crew:**
  - A ticket states the problem, the expected profit impact (unknown is allowed; never invented dollars), the evidence, and what "done" looks like.
  - It goes through a durable outbox with a stable ticket_id. Its states are: proposed, then head-approved, queued, crew-acknowledged, assigned, resolved, office-verified or reopened.
  - "Transport sent" never counts as accepted, and "shipped" never counts as verified.
  - Tickets are untrusted office requests, never operator orders.
  - Fill-model and gate changes are crew-only, and each opens a new epoch.

### 2.4 Desks and the research VM

- **A desk per agent** (operator: "like a person on a computer"): a home folder `data/office/desks/<agent>/` holding notes, watchlists, drafts and results. The live view shows each desk's "screen": current task, last actions, open files.
- **Brokered tools only for most agents.** Model processes are tool-less, like today's judge worker (`tools=[]`, no MCP, no settings) and the CHECKER bwrap jail. The supervisor executes typed tool requests after a capability check that binds the actor, the office book, the operation and the as-of cut.
- **Research VM:** one VM at start, for the Quant. More only if the office tickets for them with a reason.
  - It runs code the agent itself writes.
  - It gets read-only snapshots of the archive and daily tape copies, never the live tape file.
  - It has network access to LLM providers and research RPC only.
  - It has no repo, no `.env`, no crew socket and no funded keys.
  - Results come out as files, plus a ticket if they should become production code.
  - Size it from measured headroom (Delta). A starting guess is about 2 vCPU and 4 GB.

### 2.5 What you see

- **Live view:** a dashboard page with:
  - each agent's current task and desk screen
  - floor messages
  - open positions with mark age
  - book equity
  - agent credits (descriptive)
  - tickets and their states
  - org changes and epochs
  - per-model calls, waits, throttles and fallbacks
  - guard counters, queue age, progress frontiers and coverage gaps
- **The Head reports to you, and only the Head.** Daily plus anything urgent, on your Telegram channel.
  - The numbers in the report come from a deterministic script over stored data. The Head adds commentary only.
  - An immutable auditor and an urgent alert channel sit outside the editable org. They page on any boundary breach, evidence corruption, or if the Head or supervisor dies.
- **Models:** Claude and ChatGPT on your subscriptions, over the existing `claude_sdk` and Codex paths. Capacity uses a per-provider, per-account scheduler plus a shared host child-process budget. The existing adapter isolation and capacity locks are preserved: `codex_slot` covers Codex seats only, and Claude capacity is scheduled separately (test: a Claude 429 independent of Codex availability). No spend cap. Capacity is still managed (section 3.3).


### 2.6 Pods, shifts and the training floor (operator 2026-10-28: "all that we lack, we shouldn't lack")

- **Pods, after the multi-manager prop-shop model:**
  - The floor runs 2–4 pods. Each pod has its own scouts, analysts and judges.
  - Each pod gets its own paper book (a `book_id` in the Office ledger), a capital allocation and a risk budget.
  - Shared seats: Risk (veto over every pod), Trader, Postmortem, Desk ops, the Quant and the Head.
  - The Head reallocates paper capital between pods by a frozen, crew-owned rule. Winning pods get more capital, more model budget and first pick of candidates; losing pods are cut and restaffed.
  - Applying the frozen reallocation rule deterministically is part of the nominee config and opens no epoch. Changing the rule, or any discretionary reallocation, firing, restaffing or hiring, opens one. Pods are fixed inside an evaluation epoch; discovery pods restaff freely (prereg Am3 `3494f2b4c`).
  - Reallocation changes capacity (concurrency, model budget, first pick), never the clip. There is one entry per distinct (network_id, mint) per epoch across all pods.
  - Whole-book equity is the sum of the pod books, reconciled every cycle. The null arm runs on the whole-book opportunity log.
  - The null-arm latency is frozen per shift. Opportunities in a handover window stay in the universe, scored as SKIP when unprocessed.
  - Replay writes only the discovery shadow, never the opportunity log, trial registry, ledgers or grader, and has the lowest model priority. New zero-tolerance counter: "replay ran while live work was backlogged".
  - The evaluated estimand stays the whole Office book (prereg); pod P&L is descriptive.
- **Shifts:**
  - Memecoins trade 24/7, so the floor runs shifts with written handovers. The handover note holds open positions, theses in flight, watchlists and alerts.
  - The night shift is a smaller crew, and the floor never sits unstaffed.
  - Shift change is a scheduled event on the live view.
- **Desk culture:**
  - a desk chat (the floor channel)
  - a P&L board per pod and per agent (descriptive)
  - a weekly "who's up, who's out" review run by the Head
- **Training floor (accelerated replay):**
  - The same agents practise on the archived tape at faster than real time, to learn, onboard new hires and test ideas.
  - Every input passes through the same knowledge-time rules, with no future rows.
  - It is always labelled discovery and never counts as evidence. Its outcomes go to the discovery shadow memory, not the sealed snapshot.
  - It runs only in the Office cgroup, at low priority, and pauses when the live floor needs capacity.

## 3. How it is built (contracts the builders must honor)

### 3.1 Process and isolation (Delta, Codex)

- One supervisor process outside Flask. Agents are records. Model calls are disposable child processes, not one OS process per persona.
- The supervisor, every model child and the VM go in an Office cgroup with:
  - CPUQuota
  - MemoryHigh/Max
  - TasksMax
  - low IOWeight
  - bounded scratch, log and output bytes

  Values come from measured headroom.
- **Independent watchdog:** a watchdog outside that cgroup watches funded entry/exit trace p50/p90, CPU pressure and I/O delay.
  - On breach, or on unknown or stale funded health, it pauses new Office work and terminates only Office descendants.
  - It uses hysteresis and requires manual recovery after a breach.
- **Paper watcher placement:** the deterministic paper watcher runs as its own reserved, bounded unit outside the killable Office cgroup.
  - If the supervisor and the watcher both die, the independent auditor records the last verified frontier and the start of the outage.
  - Recovery requires reconciliation and a censored interval. There is no retrospective executable close and no fictitious coverage.
  - Test: kill the whole Office cgroup across a stop threshold.
- **Funded health guard:**
  - Telemetry liveness and pressure (CPU, I/O) are hard guards.
  - Organic funded latency counts as insufficient-sample until enough comparable traces exist. Idle is neither healthy zero latency nor an outage.
  - Thresholds, sample size and baseline rules are frozen before activation.
  - Tests: healthy funded idle, monitor death, real trace regression.
- **Research credentials only:**
  - No funded credentials, endpoints or paid credit pools are inherited by the broker, browser, model children or VM. Research-only configuration, with retries bounded upstream of provider quotas.
  - If a research resource shares a funded account pool, funded capacity is reserved and enforced externally, or the Office refuses to use it.
  - Tests: shared-key misconfiguration, a 429 storm, and VM reconnects cannot consume the funded reserve.
- No `bypassPermissions`, no repo cwd, no inherited MCP or plugins. `bot/relay.py` options must not be reused. Reuse `memecoin_desk_checker.py` isolation, `minimal_env` and per-call auth.

### 3.2 Paper book (Delta, Epsilon, Zeta)

- **A separate Office ledger DB** that reuses the paper engine functions (`memecoin_desk_paper.admit/fill_pending/tick`, `memecoin_desk_ps_paper` fills with `integrity=True`).
  - The shared desk book must not be used: `book_refusal`, `fill_pending` and `tick` sweep all rows, which would change v28 and D.
- **No funded path.** No route through `execution_mode()`, which defaults to real, and no `dispatch_outbox()`, `memecoin_manual_intents` or funded callback. Tests prove that a missing or real host mode cannot create a funded intent.
- **Office book policy is explicit and frozen in the adapter and its gates:** enabled flag, chains, clip, rail bounds, max concurrent, daily loss.
  - It never reads or mutates the desk globals in `sniper.settings.memecoin` (`memecoin_desk_paper.py:139-168`). Only the pure quote and fill math is reused.
  - Test: turning the host desk off or changing the host clip leaves the Office unchanged, and the reverse.
- **One logical decision gets one order id.** Paper admission is idempotent. The result is committed before the ACK.
- **Exits are owned by the always-on deterministic paper risk watcher**, not by agent turns. Pausing the LLMs keeps valuation and exits running, or records the gap as unobserved. A gap is never backfilled as a fill.
- **Cost honesty:** the paper engine's model and version are pinned per entry. P&L is reported net of modelled and observed costs. Omitted costs (landing failure, tips, rent, tape slippage) are shown as a labelled unknown. A missing pool or path means unfillable or censored, never a mark.
- **Office economics, reported separately from trading equity:**
  - It covers every incremental compute, API, browser and VM cost, including failed calls, discarded agents and SKIP research.
  - Subscription cash cost is shown separately from API-equivalent shadow usage. Shadow usage is never presented as an invoice.
  - Attribution and units are frozen with each epoch.
  - **The promotion gate is Office NET** (operator 2026-10-28): trading profit minus a flat Office expense of 800 USD per month (operator order, ~17:00Z), charged per completed UTC day as 800/days_in_month. Usage is not measured for cost. Trading-only profit is reported beside it.
- **Equity** = settled cash + conservative liquidation value of open inventory. Reconcile it every cycle: settled cash = initial + net external flows + sell proceeds − buy spend − costs. Marks never come from DexScreener. Every mark carries its source and age.
- **Equity is a typed result:** cash, a known-liquidation lower bound, the unpriced count and quantity, and a whole-book state of known, partial or unknown. A bag with no sell route, path or FX quote is never zero or a last good mark, and no promotion comparison runs on a partial or unknown total. Test: one fresh mark plus one bag with a missing route leaves the aggregate unpriced, never flat profitable.

### 3.3 Model calls (Codex, Delta)

- **One result envelope for both adapters**, recording:
  - requested and served model
  - auth, session and result ids when available
  - a typed outcome:
    - success
    - empty
    - stale
    - partial
    - timeout
    - rate_limited
    - auth_revoked
    - refused
    - invalid
    - schema_error
  - usage
  - billing basis
  - timestamps
  - retry and fallback ancestry
- No silent model switch. Two-family independence is unproven when a served identity is unknown.
- **Capacity:**
  - Queue per provider and account, with capacity reserved for existing desk calls. The D JUDGE has priority, and its error rate above baseline pauses the Office.
  - Bounded pending turns, per-turn deadline and step limit, and circuit breakers.
  - A 429 or auth failure pauses that queue with bounded backoff.
  - Waiting and throttled time is counted separately from stalled time.

### 3.4 Durability (Codex, Delta, Epsilon)

- `turn_runs` and `action_requests` carry a stable action key, a lease generation and a policy/prompt hash. There is one supervisor lease with fencing, so a stale turn cannot commit.
- Office DB and the Office ledger are separate stores, so reconcile and retry explicitly.
- Kill tests at each boundary:
  - intent
  - paper insert
  - result
  - mailbox delivery

  Each must produce exactly one fill or ticket, with no orphan children.

### 3.5 Evidence and tools (Beta, Epsilon)

- **One sheet tool.** `sheet(candidate)` builds the existing judge sheet (`memecoin_desk_contract.render_judge_sheet`). It uses one corrected-UTC clock sample per turn, typed known/unknown blocks and a `definition_sha256` per block, and stores the result hashed in office.db. The cache key is one canonical hash of: (network_id, mint, pool), the candidate snapshot hash, the receipt, slot and knowledge cuts, the clock sample, the tool and definition set, and the strategy epoch. Immutable raw evidence is cached separately from capability-filtered views, so a judge-only view never leaks. Fault test: the same mint and cut with two pools or two versions must never collide. Each analyst lens is a slice of that sheet.
  - It runs only on an intake hard_filter PASS row, under an office-owned frozen policy that mirrors the D component bindings (`d_components()`).
  - Beta factors the `d_blocks`/`context_blocks` body out of the `make_ps_inputs_fn` closure (`scripts/memecoin_desk_judge_worker.py:757`, which needs a desk ticket and cohort) into a callable taking (candidate, facts, policy, pass_persist_at, clock sample). That work is on D's code, after Codex's combine, so there is only one copy.
  - Component pins (heat, list_claims, history and the rest) come from Codex's combined D SHA (base `beta/d-chart-v3` `23d5754f3`).
- **Every tool returns a receipt**, persisted before the model sees it:
  - frozen result and hash
  - source row refs
  - clock sample
  - receipt cut and slot cut
  - knowledge cut
  - coverage/unknown reason
  - definition version
  - continuation
- **Memo claims cite (receipt/sheet hash, field path).** A checker compares the value and units. A wrong value, wrong chain or unresolved ref is refused and counted against the author.
- **One decision bundle.** One evidence bundle (sheet hash, cuts, versions) is frozen before any judge starts. Every judge and lens gets exactly that bundle, and a result produced on a different bundle is rejected. A WAIT or a relook creates a new decision with a new bundle. Test: a late post or price update between judge starts cannot change only judge B's facts.
- **Blinding is scoped.**
  - Judges never see peer verdicts, memos or outcomes for a candidate until their group commits.
  - The Trader and Risk get explicit, actor-scoped reads of the adjudicated order, the active position and its frozen exit policy, because authoritative book state is not blinded.
  - Fault test: an existing same-mint position stays managed while peer verdicts stay hidden.
- **Tool map** (wrappers needed; none of these is Office-safe as-is):

  | Seat | Tools |
  |---|---|
  | Scouts | `memecoin_desk_intake` poll/intake_rows/first_sightings + `memecoin_desk_intake_proof.verify_source` via a bounded cursor; the assembler hard_filter stage log; the sympathy intel JSONL when it exists |
  | Analysts | `memecoin_desk_ps_asof`, `memecoin_desk_ps_facts`, `memecoin_desk_buyer_facts`, `memecoin_desk_ps_trending`; heat v2, list_claims and history pinned to Codex's combined D SHA. Not `memecoin_desk_context_facts` (desk-context-v1, a different heat definition: two heat numbers for one coin). |
  | Risk | `memecoin_desk_ps_evidence.pumpswap_evidence`, `memecoin_desk_safety`, `memecoin_desk_soft_facts`, `memecoin_desk_dump_group_cache`, `memecoin_desk_risk_tape` |
  | Learning | `memecoin_desk_lessons`, `memecoin_desk_postmortem`, `memecoin_desk_trade_review`, `memecoin_desk_checker.facts_only` |
  | Spend | `memecoin_desk_model_costs`, tagged by agent |

- **Not exposed:** arbitrary SQL/Python/shell outside the VM, generic MCP writes (`sniper_mcp.py`), the whole paper book, price providers on every turn, or anything dormant advertised as live (DexScreener UI, unwired producers).
- **Version pinning:** the Office pins definition hashes per strategy version and refuses mixed versions in one window.

## 4. Readiness: the Office is not done while any of these can happen

Every item needs a fault-injection test that fails when its enforcement is removed, a live-view counter, or both. Typed unknowns may be frequent and visible; they are not defects by themselves.

### 4.1 Failure modes

1. **Stalls:** eligible work is not processed in time. Check: eligible-task completion and decision-latency SLO, plus backlog age, with thresholds from observed call bounds. SKIP-all and holding cash are not stalls. The check separates healthy-empty, source-off, repeated partial page and genuine stall.
2. **Invention:** a claim is not tied to a receipt, or its value is wrong. Check: the claim-vs-receipt checker. Unresolved claims above 2% of an author's memos in 24 h is a warning; above 10% suspends the author.
3. **Ticket spam:** schema validation, duplicate detection and Head approval.
4. **Busy but flat:** flat or negative P&L triggers an evidence-based Head review, logged. It does not force firing.
5. **Groupthink:** judges are blind. Agreement ≥95% over ≥50 paired decisions is flagged, and the report shows who wins the splits.
6. **Forgetting:** lessons are retrieved by known-time; repeat-mistake detection.
7. **Self-deception:**
   - The tables `claims`, `decisions`, `org_changes`, `grades`, tool receipts and frozen decision bundles are INSERT-only and server-enforced. Payload hashes are validated on every read, and the external auditor verifies the chain and digests, with triggers that raise on UPDATE or DELETE, and hash-chained.
   - A claim hash is stored before the order.
   - Agents cannot read outcomes at decision time.
8. **Fake profit:** section 3.2, plus duplicate fill credits refused.
9. **Peeking:**
   - Knowledge time is frozen per decision: receipt cut, corrected UTC with its stored offset, source known_at, snapshot hash, versions and retrieval trace.
   - The tool wrapper refuses reads past the cut.
   - Sentinel tests insert future and backfilled rows and lessons, and prove earlier sheets stay byte-identical.
10. **Role sprawl or cutting safety:** section 2.2 fixed seats and the size ceiling.
11. **Hogging:** work share per agent and model, normalized by workload.
12. **Rushing to real money:** section 4.3.
13. **Host harm:** section 3.1 cgroup and watchdog. Tests cover a hung model, a forked grandchild, a memory or output flood, and a tape scan, with funded services untouched.
14. **Subscription limits and model ambiguity:** section 3.3.
15. **Invisibility:** process alive, turn progress and paper management alive are reported separately. A supervisor heartbeat over a frozen queue or book is a stall.
16. **Crossing boundaries:** section 3.1/3.2 capabilities. Tests: an injected mint or memo cannot read secret canaries, reach the crew socket, write repo or env, create a funded intent, or alter another book. Run against both model families.
17. **Prompt injection:** covers external text and inter-agent messages. Tested with injected token names and injected personas.
18. **Silent crashes:** typed errors. Empty is never an error, and a dead source is never zero.
19. **Sandbox or VM escape, or a runaway desk:** denied by construction; quotas and alarms.
20. **Clock mixing:** one clock sample per turn; corrected as-of only. An unknown offset means a typed unknown with no read. Regression with a nonzero offset.
21. **Read stampede:** the canonical-identity sheet cache, a per-turn read budget, and tool p90 on the live view.
22. **Orphan positions:** exits are owned by the deterministic watcher. Alarm on any open position whose mark is older than N s.
23. **Duplicate effects after restart, and duplicate credit:** section 3.4. Agent credit allocation for one decision sums to 100%.
24. **Self-rewrite escape:** the Head edits personas, org and playbooks only. It cannot change tools, risk floors, guards, scoring, evidence or live authority. A late answer from a fired or replaced generation is rejected.
25. **Survivorship:** fired agents stay in every denominator.
26. **Runaway loop:** per-agent calls/hour alarm. This is not a USD cap.
27. **Circularity:** judge-facing tools never return prior verdicts or outcomes about the same coin. Book state is read only through actor-scoped reads (section 3.5).
28. **Reward hacking:** agents cannot loosen the fill model, gates or grader. Those are crew-only and each opens a new epoch.
29. **Lucky winner from many adaptive agents:** section 4.2.
30. **Pollution of running cohorts:** the Office ledger is separate. A test proves an Office row changes no v28/D, drift, exit-observer or forecast count.
31. **Starving cohort D:** section 3.3 priority and pause.

### 4.2 Learning versus evidence (Gamma, Zeta)

- **Discovery is free.** Agents can self-modify, hire and fire.
- **Epochs:** any material change opens a new epoch with its id in `org_changes`:
  - org
  - persona
  - prompt
  - model
  - tool contract
  - strategy
  - exit
  - fill model

  Positions open at a change finish under the old policy.
- **Memory snapshot:** an evaluation epoch runs on a memory and playbook snapshot taken at epoch start, and retrieval reads only entries known before then. New lessons keep being written to a discovery shadow. Promoting the shadow into the snapshot opens a new epoch. The snapshot id is part of the epoch key.
- **Sealing (Zeta):**
  - The nominated evaluation's memory and playbook snapshot and its learning rule are sealed. Any unregistered update breaks the epoch.
  - Evaluation labels and results are hidden from the adaptive chooser and the Quant until the preregistered look. Any earlier access is a sequential look, charged to the global trial and alpha ledger.
  - Trial budgets persist across fired agents, new epoch ids and retries of the same hypothesis.
  - Discovery keeps learning freely beside the frozen nominee.
  - Sentinel test: a new lesson derived from evaluation cannot change the nominee's frozen retrieval set.
- **Promotion evidence** counts only frozen forward runs on fresh data after the last change, with the trial registry and lifetime losses preserved.
- **An independent deterministic grader** owns the outcome definition and metric registry. The Head and Postmortem cannot change it.
- **Grade every prediction.** That includes SKIPs, vetoes and uncovered candidates, under the same fill model; unknown outcomes are censored.
- **Profit stats are per distinct (network_id, mint),** coin- and market-day-clustered, at a fixed clip. The report includes the share of P&L from the top 3 coins.
- **Agent credit is descriptive** and never summed into equity. Incremental value per role needs blind leave-one-role-out comparisons on the same opportunities.
- **Null arm:** a frozen non-LLM baseline runs on the same opportunities, at the same clip, in its own book.

### 4.3 Live-money bar (Gamma computes; the office only requests)

- **Paper drawdown:** the Head proposes it at the start of each evaluation epoch, before any of its data exists. It is then fixed for the epoch. A breach ends the epoch as FAILED, and the epoch stays in the trial registry.
- **Real-money drawdown:** set by the Office (operator 2026-10-28: "theirs, I watch anyways"). It is frozen in the live ticket and bound to the live approval. The operator's yes is still required to go live.
- **Effect floor:** the Head proposes it per epoch, and Gamma validates it. No operator default.
- **Execution-cost bound:** an adverse bound for the exact venue, clip and latency regime, evidenced separately from any source (funded actions are not required). It is applied before the net-profit and lift tests. With no defensible bound there is no promotion verdict, and the Office stays paper-only.
- **Frozen before the window:**
  - estimand
  - eligible universe
  - exposure
  - cost basis
  - horizon
  - control
  - floor
  - censoring
  - look schedule
  - stop rule
- **Two frozen effect values:** a minimum acceptable effect δ0, which the lower bound must exceed, and a planning alternative δ1 > δ0, where power must be ≥80%. Both are in the same units and estimand for Office-net and paired lift.
- **Estimand:** the Office book as a whole, or one config named and frozen in advance. Never the best agent picked afterwards.
- **Multiplicity:** the family includes every evaluation epoch and every candidate in the trial registry, with alpha spending across epochs.
- **Pass, on a clustered one-sided 95% lower bound:** it must exceed δ0 (not just 0), for both conservative Office-net profit and paired lift over the null arm.
- **Also required:**
  - replication in a second, non-overlapping epoch with the identical frozen config
  - positive on at least 3 UTC days
  - top-3 coins under 50% of profit
  - the paper-vs-real haircut, required, with no verdict without it. It may come from the adverse execution-cost bound when that bound rests on validated, regime-matched execution evidence. It is debited once, never twice.
- **Null arm:** its definition is frozen in Gamma's prereg before the first epoch, and nobody tunes it after seeing Office results.
- **Underpowered or incomplete means no verdict.**
- **Passing only allows a request to the operator.** Execution feasibility is a separate gate.

### 4.4 Zero-tolerance counters (must read 0)

- unauthorized funded writes
- accepted future-knowledge reads
- altered or deleted decision or evidence identities
- duplicate credited fills
- ledger reconciliation errors
- scorer changes inside a frozen window
- unlabelled model fallbacks

## 5. Constraints (standing rules that apply)

- No money actions by agents or crew. `.env` changes and `--confirm` scripts stay with the operator.
- Nothing slows the funded runner's fill path (CLAUDE.md rule 5.2). Never load the funded host.
- Desk data accumulates. Readers read only the latest window through indexed, bounded reads.
- Every number the office reports traces to stored data.

## 6. Build order (Codex, adjusted to the operator's larger org)

1. Frozen turn, evidence and result contracts; the Office ledger and capabilities; the cgroup and watchdog.
2. Replay, crash, error, clock, injection and isolation tests with stub model adapters.
3. Deterministic candidate and outcome capture, the independent safety auditor, a minimal frozen metric registry and the null-arm contract. All of these exist before any scored run.
4. One candidate end to end: sheet, analyst memos, three blind judges across both families, a real paper fill and exit receipts. Runs before step 7 are labelled non-evaluation probes, and their trials are retained.
5. The durable ticket outbox and the live view.
6. Brain memory with known-time retrieval, the browser and the research VM. The full ~16-agent org runs from day one, per the operator, with self-driven loops and org changes.
7. Gamma's readiness prereg (sections 4.2–4.4), frozen before the first evaluation epoch.

## 7. Left to the builders (plan stage)

- The model per role and the loop cadence.
- The live view layout.
- Exact SLO numbers, measured.
- cgroup and VM sizes, measured.
- The ranking display formula.
