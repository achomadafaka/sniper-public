# Desk prompt evidence review implementation plan

> **For agentic workers:** use the assigned existing owners; review exact commits before integration.

**Goal:** correct misleading JUDGE/RISK instructions without changing execution gates or adding critical-path work.

**Architecture:** Beta owns a prompt-only JUDGE candidate; Lambda owns a separately versioned RISK candidate. Codex independently reviews both. Retain the current schema and data producers; describe their actual semantics.

**Tech stack:** existing Python, pytest, Markdown prompts and immutable local replay records.

**Spec:** approved chat scope, 2026-10-23: improve prompts, peer review, reasoning and data use; keep price caps and safety intact.

## Constraints and edge cases

- Keep $15k–$100k eligibility, 6% verdict tolerance, 6% round-trip cap and existing sizing/loss controls unchanged.
- No synchronous calls, locks, retries, extra model calls or mutable global state on the fill path.
- Preserve preference for timely entries on supported active tape; entry frequency is not a per-sheet quota.
- Optional missing context is neutral; mandatory missing universe flow retains the existing refusal.
- Missing pivot support is unknown, not an observed break. Healthy monotone rises may lack pivots.
- Breakout holding=false can remain false after a past undercut and subsequent reclaim; inspect current evidence.
- Tape gaps, stale quotes, missing FX and graduation are not zero activity, executable losses or collapse.
- Current-price-relative chart bp, fill-relative RISK bp and executable whole-bag PnL remain distinct.
- Agent volume and wallet counts do not prove independent organic demand or distinct owners.
- Rollback restores the old pinned prompt/version/cohort; never relabel old decisions or mix prompt versions silently.
- Not handled: new WAIT grammar, new structure-v2 fields, provider repairs, model training, or proof of profitable trading.

## Task 1 — JUDGE, Beta

- [ ] Add a focused failing prompt-contract check; run it red.
- [ ] Create a separately versioned prompt from v20: explicit instruction precedence; remove unsupported second-reader quality claim; preserve active-entry intent without forcing frequency; clarify missingness, reclaim semantics and structure-compatible rails.
- [ ] Keep exact output schema, allowed values, bounds and parser behavior unchanged.
- [ ] Run focused prompt/parser tests with `python3 -m pytest -n 2 ...`; send exact commit and red/green evidence.
- [ ] Codex compares the full prompt diff and checks healthy rise/no pivot, reclaimed breakout, blind mandatory flow, optional nulls, agent-only activity and conflicting rail suggestions.

## Task 2 — RISK, Lambda

- [ ] Add a failing check for the misleading missing-support instruction; run it red.
- [ ] Replace missing-support-as-break with observed break evidence from available bars or existing sb; clarify historical breakout failure versus current reclaim.
- [ ] Bump prompt version; preserve validators, quote checks, cooldowns, rail precedence and apply-time freshness.
- [ ] Run focused RISK tests with two workers; send exact commit and red/green evidence.
- [ ] Codex reviews the exact branch and the connected apply guards; no activation before review passes.

## Task 3 — evaluation and future specialization, Codex

- [x] Independent Codex peer reviewed current prompts and producer semantics.
- [x] Read current official model-optimization guidance; training availability must be checked, not assumed.
- [x] Measured latest stored sheets read-only: 24 usable sheets in latest 40 calls; median structure 3568.5 bytes, context 810.5, evidence 397. Bytes are not tokenizer counts or a latency estimate.
- [x] Document final candidates, focused test results and remaining behavioral uncertainty.

### Review results

JUDGE d45249235 and RISK f1652430e passed independent Codex review. JUDGE connected tests: 276 passed. RISK connected set including three independent witnesses: 219 passed before the last wording follow-up; final follow-up prompt/witness set: 13 passed. The independent missing-support instruction check failed on v8 and passed on v9. Healthy rising/no-pivot and historical failure/reclaim witnesses establish the producer semantics; they do not establish how an LLM will act.

JUDGE output, mandatory refusal, rail bounds, WAIT grammar and reason-code sections compare byte-identically with v20. RISK executable AST compares identically with c41894622 after excluding only prompt/version assignments. No inference calls were made for this change. Static correctness passes; decision-quality and latency improvement remain unproven. Alpha received final reviewed pins for a controlled new-cohort cutover, separate from exit-policy, cadence and standing-order work.

Use the existing replay/grade tools for any model experiment. Baseline reruns estimate natural variation; record strict JSON void rate, call p50/p90, input/output tokens, decisions and reasons on the disagreement set. Run only bounded, non-overlapping subscription work after checking live-seat contention; no paid training or external upload is authorized here.

Outcome analysis uses immutable as-of inputs and separate quote/admission results, never retrospective data in prompts. Missing executable outcomes stay censored. Split future train/validation/test by time and mint/episode to prevent leakage. Include refusals and skips in the denominator; successful filled positions alone create selection bias. Model labels should reflect evidence/contract correctness and executable, cost-aware outcomes where observed, not blindly imitate past model answers or label every later rally a missed buy.

Prior local 40-sheet trials did not establish a speed gain from thinking-off or brief instructions. The faster alternative changed decisions and failed raw strict JSON. Neither fewer bytes nor more entries establishes better quality. Keep those claims separate from static contract/test success.

Potential future custom models: first compare a small task-specific predictor or distilled decision model against the existing baseline; no model family or hosting choice is selected yet. Preserve deterministic gates outside any learned model. Training becomes justified only after stable labels and untouched forward evaluation exist.

Official reference checked 2026-10-23: https://developers.openai.com/api/docs/guides/model-optimization . It recommends evaluation before optimization and currently states OpenAI fine-tuning is unavailable to new users; do not assume hosted training access.
