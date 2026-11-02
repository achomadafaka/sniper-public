# RISK evidence gating implementation plan

> For agentic workers: use TDD and independent review. Root owns the gate, validator, watcher, evidence reader/helper and source-coverage repairs per the Alpha/Lambda ownership ACKs. Beta retains universe reentry/emission sections; Lambda retains unrelated PumpSwap capture recovery.

Goal: refuse unsupported model EXIT/TIGHTEN proposals and avoid repeated model calls without a measured change.

Architecture: a pure gate consumes code-built measurements and provisional thresholds. The watcher owns local reads and audit rows; existing engine rails, fresh execution checks, claims and signing retain authority. No clock-based minimum hold and no default death-exit policy.

Tech stack: existing Python, math, JSON and observer SQLite database. No dependency or provider change.

Source contracts: Alpha crew messages5fa2ee09/9343bd8b describe the proposed predicates; Lambda76c622a9 describes measurement fields. These reports are design input, not operator authority for activation. This work remains offline and reviewable.

Constraints:
- 5 open / $25 clips / $200 daily halt / -$200 cumulative kill.
- No push, deployment, restart, provider/model calls, trading-ledger or env writes.
- Focused pytest only, explicit -n2; never change services to make room.
- Main's stop/catastrophe/TP and existing exact tightening checks remain outside these gates.
- Unknown input is unbacked, never zero. Only fresh elapsed evidence from the correct venue qualifies; a heartbeat alone is insufficient.
- Attention/fresh confirmed breaks wake the model; ordinary calls require a material move or known exact rail-proximity change. First observation establishes a baseline.
- Existing pending decisions retain their original TTL; no revocation or TTL renewal.

Review focus:
1. Current-second or stale pre-fill evidence, source stalls/gaps and graduation price steps.
2. Missing/zero/nonfinite/bool values, SOL versus USD, maker counts versus distinct sellers.
3. Mixed-position batches, unchanged cycles, budgets/backoff, restart and lost attention.
4. Per-reason action validation and traceable thresholds; alternate valid OR evidence can suffice.
5. No source freshness or profitability claim from shaped tests or the flawed death replay.

Task1 (complete, d111689ef): enforce supplied sb=1 for code10 EXIT, persist HOLD reason/counter. Three RED assertions, then282 focused tests passed; independent source review found no defect.

Task2 (complete as a standalone offline component): pure predicate and material-change module.
- Files: sniper/services/memecoin_desk_risk_gate.py; tests/test_desk_risk_evidence_gate.py.
- Run new tests RED before implementation; implement finite-data predicates without I/O/global mutation; rerun GREEN and independent review.
- EXIT10 sb;11 sell/buy1.5 plus3 sellers;12 nohigh60 plus1n5 drop from120s high;13 plan horizon or300 plus nohigh120;14 drop3n5 over30s plus sellshare.6;15 activity<=.25 positive prior baseline;16 unsupported.
- TIGHTEN6 gain1n5;7 sellshare.55 orcode12;8 targetwithin1n5 orbuyflow<.5prior;9 known trail off. Other action/code pairs are unbacked; LOOSEN keeps its separate validator.
- Thresholds are named settings, validated and echoed when integrated; tests cover boundaries and unknowns. Material move1n5; known exact rail proximity1n5.

Task3 (implemented offline): measurement/source repairs and watcher integration.
- Files: scripts/memecoin_desk_risk_watcher.py and sniper/services/memecoin_desk_risk_live.py; focused integration tests.
- Bind complete/fresh actor-bearing non-own flow, post-fill windows, and exact rail proximity. Revalidate backing at decision time; do not relabel a model-start snapshot as fresh decision evidence.
- Runtime rollback knobs resolve after state.env loads. Record gate facts/settings/reasons and distinguish gated positions from model calls actually avoided.
- Preserve failure/backoff/hour cap behavior and pending-order identity/TTL. No new synchronous call on entry/signing paths.
- RED/GREEN mixed batches, attention, changed/unchanged evidence, missing sources, restart and rollback; bounded offline p50/p90 for watcher work.

Task4: frozen-tree independent review and handoff to owners. No merge or runtime activation implied by passing tests.

Component verification: 545 focused tests passed in 19.75s (263 pure gate cases plus 282 RISK/guard checks). Ten price-scale checks first showed six failures; the log-ratio repair greens them. A deliberate collapse-share mutation was rejected. Independent source review found no remaining component defects. Offline microbenchmark (1,000 samples): backing p50/p90 6.395/6.510 us; material 5.412/5.526 us. These are function timings, not live execution latency. Those component timings exclude local reads. The integrated source contracts and validation are described below.


Integrated v12 behavior:
- `MEMECOIN_DESK_RISK_EVIDENCE_GATES=0` is the default and the rollback. `1` enables evidence-based call selection and reason backing. Resolve after loading state env; invalid values refuse before a model call. `MEMECOIN_DESK_RISK_GATE_<UPPERCASE_PARAMETER>` overrides only the named provisional numeric thresholds. Existing unconditional code10 confirmation and exact-quote tightening apply independently.
- Curve and PS publication requires successfully processed block metadata with original host receipt, capped by corrected block time, followed by durable commit and drained queues. Idle commits cannot move the cut. Heartbeats expose continuous capture start separately from committed-through. Drops, process/write failures and global disconnect/stop blind the source; failed gap writes remain pending.
- The bounded read-only reader uses explicit native SOL identity or the canonical PS pool, the complete verified own-fill signatures, at most20,000 rows and one hour of retained history. Unknown denomination, graduation without PS, malformed rows and missing source proof remain unknown. Latest unpriceable states cannot reuse an older price. Mint/unknown-pool losses clip usable evidence history; structure breaks are recomputed inside that same coverage.
- Rail proximity binds a fresh exact residual quantity and stored USDC/token unit quote. Unknown partial quantities, stale/mismatched quotes and unknown executable peak/arming never become proximity facts. Bag PnL is separate.
- Attention wakes persist in the observer DB with original signal/time identity across budgets, backoff and restart, until one successful current-evidence call consumes them atomically with its decision. Old pending wakes are not presented as fresh attention facts. Closed positions and expired consumed receipts are pruned. Response-based failure backoff restores on restart.
- First usable unselected observation is a baseline. Selected failed calls do not consume the reference. Only selected positions enter model batches; avoided-call counts use actual batch/hour-cap capacity. Model-start inputs remain immutable; current position identity, policy, capture, code facts and exact quote are re-read after the response. Unknown or unbacked proposals produce audited HOLDs without an executable TTL. Existing orders retain their original TTL.
- All new reads and observer writes stay off entry, approval, signing and funded exit execution. The funded ledger is read-only. No rails, sizing, limit, exit fee clamp or execution claim changes.

Independent regressions first failed for stale source watermarks, lost/unwritten gaps, incomplete own-fill proof, partial closed buckets, old pre-fill anchors, nonfinite noise, residual quote binding, lost attention, initial rail-wake retry and structure breaks spanning mint-local losses. Focused checks use the shared observer-test lock and explicit `-n2`; no full suite or live/provider call is part of this validation.

Limits: the provisional thresholds are not an edge claim. Rows beyond one hour/20,000 are censored, not inferred. Aggregate maker bars remain unknown. PumpSwap stop/recovery policy (PS7) remains Lambda's separate correction/review. This tree adds no default code death-exit policy or plan-checkpoint scheduler, and does not activate the optional evidence gates. Runtime requires compatible source publication; no runtime proof is claimed here.

Offline integration timing: 50 measured samples after5 warmups, five sequential position evidence reads of2,000 curve rows each plus quote/proximity and material checks: p50=56.375ms, p90=59.610ms. A temporary fixture and fake committed proof were used; this excludes other watcher/ledger/chart reads and proves no live latency. Entry/signing paths do not call this work.

Integrated verification:852 focused cases passed in29.97s with `-n2`, covering the RISK watcher/validator/apply/fill/quote/SB/regime/evidence contracts and nearest universe/PS producer consumers. Independent source/behavior reviews found and repaired the documented blockers. Gamma exact-commit review remains the next gate; none of this is runtime or trading proof.


Exact-tree review round after12c27c9bb:
- eCat's two source RCs were reproduced RED: explicit canonical-pool mint/quote contradictions could be filtered into known zero activity, and a repeated individual decode loss could hide behind gaps_ps's earliest coalesced timestamp. The reader now validates raw identities before normalization and reads bounded individual losses; missing/wrong loss schema is unknown. Coalesced and individual losses share a row budget. The source adds a received_at index; a query-plan regression first failed with SCAN and now proves an indexed range. No runtime database was opened or migrated by this work.
- Gamma reviewed risk_evidence/sb_closes only, no reader/watcher/live wiring or tests, and confirmed the measurement contract with four findings. Proven no-new-high history now reports elapsed hold time as secs_since_new_high; unknown history remains None. SB_K is explicitly provisional/unvalidated. Marginal reserve prices can stay flat through an executable-value fee step; range5 is not the death study's close-to-close noise_5. Code10 uses all-market bars including our impact; the own-excluded ev population differs. These are now explicit source/docstring boundaries, and no death-study k is imported.
- The separate death-study executable-V repair belongs to Gamma (f732935a4); root supplied a standalone RED witness but did not edit or rerun research outcomes. It is not part of this RISK tree.

RC-round combined verification:864 focused cases passed in30.51s with `-n2` and the shared lock. No runtime/source-effectiveness claim.

Index provisioning bound (Alpha review): the first CREATE INDEX proposal would have built on retained source history at worker start, holding SQLite's write lock and delaying that observer writer. No live timing was measured because no runtime database was touched. Final code creates the index only with a NEW EMPTY ps_decode_losses table; existing source files never auto-build it. The reader requires INDEXED BY ps_decode_losses_received and returns unknown if absent, with no SCAN fallback. Provisioning an existing source index needs a separate operator-controlled maintenance action and is outside this offline tree. Two RED/GREEN regressions prove no legacy startup build and no unindexed reader authority. This adds no funded entry/signing lock or I/O.

Final index-guard verification:866 combined focused cases passed in29.39s with `-n2` and the shared lock; no full suite or runtime checks.

## 2026-10-26: unknown evidence calls instead of claiming no change

Fresh Alpha instruction under the operator's reciprocal crew delegation: only proven unchanged
evidence may suppress an ordinary model call. Missing/nonfresh evidence, unknown noise
(including unusable zero noise) or unknown price selects a call, audited as
`gate_json.selection = "evidence_gated:unknown_called"`. Attention/confirmed-break selection
keeps its existing precedence. Only the first usable baseline (fresh and positively priced,
with a stored reference) is suppressed. A cold start that cannot build a reference calls the
model, and repeated unknown cycles continue selecting calls under the existing cap/backoff.

The existing hourly cap and failure backoff still govern actual calls: the selection label alone
does not prove a call occurred; inspect `call_id`/`call_error` and actual call counters. Unknown
evidence does not grant EXIT/TIGHTEN backing. Decision-time rechecks can still produce an audited
HOLD with no executable TTL. Existing pending decisions keep their original TTL.

Edge cases: this changes only the off-path RISK watcher, with no entry/signing/execution calls,
locks or retries. State is per watcher; no shared/global mutation. Missing, stale, zero-noise
and invalid prices are not proof of no change. Model failures, budgets and backoff retain their
existing behavior. Source venues, clocks, quantities and thresholds are unchanged. Call counts
describe the watcher, not market quality. Knob0 remains rollback; no environment, observer-schema
or runtime mutation is included. Live latency is not measured or claimed.

Six new focused regressions first failed: five unknown-evidence selections and one hourly-cap
case. Delta review then identified cold-start unknowns repeating the baseline state forever;
three further regressions first failed and now cover cold/repeated unknown calls, the cap,
recovery to the first usable baseline and later unchanged suppression. The minimal watcher
selection fix greens them; existing baseline, material-change,
backing, TTL, backoff and knob0 regressions remain required. Delta reviews the exact commit
before Alpha activates the optional knob. Source/test proof is not a post-activation live read.
