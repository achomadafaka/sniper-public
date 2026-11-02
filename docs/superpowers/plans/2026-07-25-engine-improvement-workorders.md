# Engine improvement work orders — agent-ready prompts

**Date:** 2026-07-25 · **Baseline:** main @ 4df67be4 (engine stack complete, 4 adversarial rounds,
flags OFF). Each section below is a self-contained prompt an agent can execute cold.

**Rules that apply to EVERY work order (paste into every prompt):**
> Work in /home/bigpapi/Desktop/Sniper. Read CLAUDE.md first. VERIFY EVERY ASSUMPTION against the
> current code before editing — file paths, function signatures, column names (brain_index schema),
> constants, and whether the thing you're about to build already half-exists (grep first; this
> session found three "new" features already built). TDD: every behavior RED first. Any decoder,
> adapter, or external-data assumption must be verified against LIVE data before its fixtures are
> trusted (fixtures-encode-the-bug lesson: 3 confirmed cases). Money-path changes: monotonic
> risk-reduction doctrine, existing exit reasons only, preregs govern constants (a new constant
> needs a dated prereg entry, [inherited]/[mechanical]/[bound] tagged). Work in an isolated
> worktree; explicit-path staging; one scoped commit; no push, no flag flips, no .env edits, no
> service restarts, no deploy. Report: files, test counts RED→GREEN, deviations, and the exact
> operator activation steps you did NOT take. Host clock is +7h vs chain truth — never compare
> DB/chain timestamps to the raw system clock; use sniper/util/clock.real_utc_now.

---

## WO-1 · Fastlane Phase B — sub-second hot tape + event-driven decisions [HIGHEST VALUE]

**Prompt:** Build the Solana WS hot lane per docs/superpowers/specs/2026-07-24-memecoin-quant-exit-fastlane-design.md
§2.1-2.2, updated for everything that changed since: verify current tape architecture first
(memecoin_solana_tape.py pull lane + own DB data/memecoin_sol_tape.db; frontier enum:/decode:
modes; db_exit_state own DB). Deliver: (1) `memecoin_solana_ws.py` — asyncio `accountSubscribe`
on BOTH resolved pool vaults per open real position over `wss://api.mainnet-beta.solana.com`
(MEASURED 2026-07-24: keyless, notification per slot, median 404ms — re-verify with a 30s probe
before building; do not assume the measurement still holds). Per-slot vault deltas → in-memory
ring + rows compatible with the existing sol tape schema. The PULL lane stays as verifier: log
divergence counts, never disable it. (2) An event-driven decision loop: on each new tick for a
held pool, incremental feature update (ring buffers; O(1) per tick — reuse exit_features pure
functions, one-code-path refusal) → propose_v3 → the EXISTING adapter path in
memecoin_real_runner (verify its current call signature). Decision latency target ≤1s from
notification; measure and report actual. (3) Pre-warmed exits: refresh the exact whole-bag
preview every N s (new [bound] setting) per engine position so a CUT is sign+broadcast only.
Fail-closed everywhere: WS gap/reconnect → fall back to the 30s pull cadence and say so in the
decision log. NEW regression musts: WS outage mid-position degrades to pull without a decision
gap longer than 60s; a fabricated notification (wrong vault/mint) is rejected by identity checks;
the pull-lane verifier catches an artificially dropped WS event.

## WO-2 · Experiential memory — the outcome feedback loop

**Prompt:** The engine records every decision with state_id and execution outcome
(memecoin_exit_candidate_decisions in data/memecoin_exit_state.db — verify current schema with
PRAGMA, not from this doc). Nothing reads it back. Build the grading loop, READ-ONLY first:
(1) `scripts/research/engine_decision_report.py` — per action (RIDE/DERISK/TAKE_PROFIT/CUT/DEFER),
per coin band, per chain: counts, outcome distribution, PnL-at-decision vs PnL-if-held-N-more-min
(counterfactual from the recorded tape — verify tape coverage per position before claiming a
counterfactual; report censored separately). (2) A pre-registered read protocol (dated doc):
n≥30 per cell before any verdict; the ONLY permitted output is a proposal document for threshold
changes, each with its own prereg entry — no auto-tuning, no constant edits from this pipeline,
ever (that firewall is the difference between learning and curve-fitting). (3) Wire the report
into the daily knowledge compile if one exists (verify scripts/compile_knowledge.py handles it)
so the operator sees decision quality without asking. Do NOT touch any live path.

## WO-3 · Episodic memory — the coin remembers its own history

**Prompt:** Today compute_state sees only the last ≤5000 ticks; a coin's earlier LP behavior,
prior shakeouts, and pre-position pivots are invisible. Build per-coin episode context,
display/shadow first: (1) verify what memecoin_token_episodes / memecoin_episode_signals already
contain (brain_index schema + actual row samples) — extend, don't duplicate. (2) A pure
`episode_context(pool_history) -> dict` producing: count of prior confirmed shakeouts (break
nominated + flow-rejected), prior LP add/remove events, prior peak drawdown, age of structure.
(3) Thread it into the decision LOG as context fields only (no rule may consume it without a new
prereg — this WO produces the evidence that would justify one). (4) Report: on recorded history,
how often did a coin's 2nd+ shakeout resolve upward vs first? That number decides whether a
"shakeout-count" term ever enters v4+.

## WO-4 · Regime context — honest reuse of the failed cross-pool factor

**Prompt:** The robustness gate (docs/research/assets/memecoin_tape_purged_robustness_2026-07-24.csv,
commit 6128a5fe — read its memo first) proved a strong SHARED market factor that fails as
per-pool timing but is real cross-sectionally. Build it as a CONTEXT input, shadow-only:
(1) a market-breadth series from the whole-chain 4663 tape + Solana positions' pools (fraction of
active pools with positive 5m flow; verify what memecoin_market_state._regime already computes —
extend rather than duplicate); (2) log it on every decision row (schema addition to the exit-state
DB, registered); (3) a pre-registered shadow rule proposal — regime-off ⇒ tighten reference trail
within the existing [0.5T, T] bound (this stays inside monotonic doctrine — verify against
exit-brain prereg §2.3 before claiming so); (4) the read: n≥30 regime-off decisions, did
tightening beat not-tightening on the recorded counterfactual? No live wiring in this WO.

## WO-5 · Flexibility — conviction-scaled fractions + per-band profiles + horizon windows [v5 prereg]

**Prompt:** Three registered-constant upgrades, ONE dated v5 prereg document, code behind it:
(1) DERISK/TAKE_PROFIT fraction scaled by signal strength instead of hard 0.5 — bounded
[0.25, 0.75] [bound], strength = the same vol-vs-reference ratio already computed (parameter-free,
verify scaled_trail_pct's current form first); (2) per-coin-band decision profiles — nano coins
get a shorter edge-duration derisk than mid caps; every band constant [inherited] from measured
hold-time distributions (query memecoin_positions for the actual per-band time-to-peak first —
do not assume the 3h prior applies uniformly; show the query output in the prereg); (3) windows
declared in HORIZON units (multiples of the target signal horizon) with a cadence-measured
conversion + drift assertion at startup (assert_cadence_matches_db pattern from the study —
verify it exists and reuse). All shadow-logged (would-have-decided vs v3 baseline) before any
authority; the diff report at n≥30 is the promotion gate.

## WO-6 · Predictive rug — wallet-graph port from the Polymarket engine

**Prompt:** sniper/analysis/winning_wallet_thesis/funding_graph.py implements deployer/funder
linkage + synchronized-entry detection for Polymarket. Port the CONCEPT (not the code — verify
how much actually transfers; the data model differs completely) to memecoin chains: (1) for each
held coin, resolve deployer + LP-provider wallets from the pool's creation/mint history (Solana:
getSignaturesForAddress on the vault paginated to genesis where retention allows — VERIFY
retention limits live before designing around genesis access; document what fraction of pools
are resolvable); (2) maintain a small watch-set per position: deployer, top-3 LP providers;
(3) a `deployer_activity` flag on the decision row when a watched wallet's vault-relevant
activity appears in the tape window; (4) shadow read: across recorded rugs (positions with
sol_burn/LP-pull or −80% closes), how often did watched-wallet activity precede the event, at
what lead time? That number is the go/no-go for a CUT-precedence rule (new prereg). Read-only
RPC budget: bounded, throttled, never the funded runner's lane.

## WO-7 · Execution tiering — priority fees + depth-aware slippage

**Prompt:** Per project-memecoin-execution-speed-roadmap (brain): each execution upgrade gates on
MEASURED fill-delta, never vibes. (1) Verify current submission paths (solana_exit_executor /
evm_exit_executor — read them; do not assume ladder shapes). (2) Add priority-fee support on
Solana exits behind a default-OFF setting with a per-exit fee cap [bound]; (3) depth-aware
slippage: the tape's reserve levels (sol_sync/v2_sync) are already recorded — size the ladder's
first rung from actual pool depth vs bag size instead of a static schedule; (4) the measurement
harness: every exit records (quote, submitted, filled, slippage_realized, fee_paid, latency_ms)
— verify what memecoin_exec_latency already captures and extend, don't duplicate; (5) the read:
20 fills per variant before any claim. No default-ON anywhere.

## WO-8 · v4 EV term — the surviving 5-minute signal [BLOCKED on OOS confirmation]

**Prompt:** Two stages, strictly ordered. STAGE A (research, unblocks B): confirm the
5m/no-span-floor signal (per-pool 0.15, commit 6128a5fe) on genuinely out-of-sample pools —
pools whose ENTIRE history postdates the study's data window (the tape accrues daily; verify
sufficient new-pool count first, report it; if <100 new qualifying pools, STOP and say so).
Same features, same walk-forward+purge discipline, same per-pool/per-sub-window read. Survives ⇒
STAGE B: dated v4 prereg registering the EV rule — act only when shrunk predicted 5m move
(shrinkage factor [bound], sized to per-pool 0.15 NOT the pooled number) crosses the exact-quoted
exit cost; predicted_move absent/None ⇒ exactly v3 (test: byte-identical decisions). Frozen ridge
coefficients from the confirmation's train window, no online learning. Shadow first: EV term
logs would-act alongside live v3 for n≥30 divergent decisions before any authority proposal.

## WO-9 · Crash-injection test harness [NEW — the session's meta-lesson]

**Prompt:** Every P0 this session was a crash/timing-window bug that green tests missed and
adversarial review caught. Systematize that: a pytest fixture family that kills the process (or
raises) at EVERY yield point of a money-path operation — after claim, after sign, after
broadcast, before stamp, between commit pairs — and asserts the recovery invariants: no
false-confirmed state, no double-submit, no orphaned reservation/claim, recovery queue sees
everything it must. Start with the derisk claim lifecycle and claim_close (verify their CURRENT
shapes at HEAD — round 4 changed them), then reservations. Deliverable: `tests/test_crash_windows.py`
with a documented pattern other agents extend. Every invariant that fails is a real bug: file it,
fix it RED-first in the same branch or report it as a blocker.

## WO-10 · Ops root-cause — WAL-pinning readers

**Prompt:** Three times on 2026-07-24 the main DB WAL grew to 400-830MB because long-lived
processes (web server, discovery worker, the funded runner itself) pinned read snapshots for
hours; each time the fix was a bounce. Find the mechanism, don't guess: for each service,
instrument or inspect where sqlite connections are created and whether any sits inside an
implicit transaction between requests (python sqlite3 default isolation opens a txn on first
read and holds it until commit/rollback). Reproduce locally: open a connection the same way the
service does, run its typical query pattern, watch `PRAGMA wal_checkpoint(PASSIVE)` page counts
stall. Fix per-service (autocommit mode / explicit rollback after reads / short-lived
connections) with a regression: a service-shaped reader must not block TRUNCATE for >2s.
The deliverable includes a one-line health check for the ops cron (WAL size + pinner identity).

## WO-11 · Deployment smoke harness [THE remaining gate]

**Prompt:** Codex's final gate: "activation/deployment smoke testing, intentionally out of
scope." Build the harness, run nothing live without the operator: a `scripts/engine_smoke.py`
that, given a deployed release dir, verifies in DRY mode: worker starts and connects to all
three DBs with the right modes (ro on ticks — assert from /proc fd flags), tape advances on both
chains within 120s, exit-state snapshots appear for a synthetic open position (injected into a
COPY of the ledger, never production), decision rows land with state_id+outcome fields, flag OFF
means zero engine-eligible ids, flag ON in the state-root .env is picked up next sweep, kill
switch blocks entries. Output: PASS/FAIL per check, one page. The operator runs this after
`deploy_memecoin_runtime.sh` and before deleting the kill switch.

## WO-12 · Canary observability — the operator's cockpit

**Prompt:** When the engine drives its first 5 positions the operator must SEE it think. Add a
dashboard surface (verify how the existing web tabs are built — sniper/routes/api_memecoin.py +
static/; follow the existing pattern exactly): per open engine position: current state label,
last 10 decisions with reason codes, the state snapshot values behind the latest one
(price/prior_low/flow/vol/depth_pull), outcome stamps, and the envelope's distance-to-catastrophe.
Read-only from the exit-state DB. Plus a Telegram line on every ACTED engine decision through
the existing alert path (verify memecoin_telegram's current interface). No new auth surfaces.

## WO-13 · Entry-side improvement program [DEFERRED by operator — do not start until told]

Placeholder so it isn't lost: wallet provenance (early-vs-connected arming prior), entry
conditioner shadow wiring, radar-as-synthetic-wallet, honest-negative-alpha demotion. Specified
in docs/superpowers/plans/2026-07-24-memecoin-quant-engine-brief.md P2/P3/P5/P6. The measured
fact motivating it: the engine can only lose-less on a bad-entry week; entries were the weak link.

---

**Sequencing recommendation:** WO-11 first (it gates the switch), WO-9 alongside (cheap, catches
the next round-5 before a reviewer does), then WO-1 (speed), WO-2 (memory), WO-8A (v4 unblock),
with WO-10 whenever ops has an hour. WO-4/5/6/7 after first live canary results — let real fills
set their priority. Each WO is independent; none requires another except WO-8B on WO-8A.
