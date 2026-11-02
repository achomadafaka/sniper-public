# Office pods: frozen reallocation rule (Task 18, ledger side)

Spec §2.6, readiness prereg §16 (Am3). Owner Beta (rule + ledger), Lambda (org wiring). Reviewers Delta + Gamma.

## What exists

`sniper/office/ledger.py` (main, 6d18d31ac..6f82914e6): `open_epoch(rule_version=)`, `open_book` (one book per pod,
`book_id = f"{epoch_id}:{pod}"`), `fund`, `transfer` (paired legs, epoch and rule_version checked, free cash at causal FX,
idempotent on `transfer_id`), `lookup_transfer`. One entry per `(epoch, kind, network_id, mint)` across all pods.

## What this adds

- `config/office/pods.json`: crew-owned, sha256-pinned by the nominee (`pods_rule_sha256`). `version` is the
  `rule_version` the epoch freezes.
- `sniper/office/pods.py`, pure (no clock, no DB, no IO except `load_rule`):
  - `load_rule(path) -> dict`: validated rule, raises `ValueError` on anything else.
  - `initial_capital(rule, pods, total_usd) -> {pod: usd}`: equal split, cent-rounded, remainder to the first pod by name.
  - `plan_reallocation(rule, *, epoch_id, period_key, scores, capital, free_cash) -> [transfer]`: at most one
    transfer per period, worst pod to best pod.
  - `pod_caps(rule, capital) -> {pod: {max_concurrent, daily_loss_usd, model_share, pick_rank}}`.

## The rule (pods-rule-v1)

Once per `period_key` (a UTC-day string the caller derives from corrected UTC; this module never reads a clock):
1. Score per pod = realized net USD of closed orders over the trailing `score.window_periods` periods, with its closed
   count. A pod with fewer than `score.min_closed` closes is unknown.
2. Any pod unknown, fewer than 2 pods, or best score == worst score: no transfer.
3. Best = highest score, worst = lowest; ties broken by pod name (ascending) so the result is deterministic.
4. Amount = min(`step_pct` of total pod capital, worst capital minus `floor_pct` of total, worst free cash), floored to
   cents. Below `min_transfer_usd`: no transfer.
5. `transfer_id = sha256(rule_version|epoch_id|period_key)[:32]`: one effect key per period. It does not include the
   amount, so a retry whose recomputed amount differs raises `transfer_id_conflict` in the ledger instead of booking a
   second effect. The caller checks `ledger.lookup_transfer` first after a crash.

Capacity follows capital share, never the clip (prereg). The rule's totals are hard ceilings (Gamma ruling
2026-10-29): `max_concurrent` is a largest-remainder split of `max_concurrent_total` (one slot first to each pod by
rank while slots last, so a pod can get 0 and then cannot enter), and `daily_loss_usd` is a largest-remainder cent
split of `daily_loss_usd_total`; each sums exactly to its total. `model_share = share`, `pick_rank` 1 = largest
capital (ties by name).

## Ledger binding

`open_epoch(..., pods_rule=)` freezes the rule (table `epoch_pods_rules`, version must equal `rule_version`).
`submit` on an evaluated book: no frozen rule = `pods_rule_unknown`; then the pod's caps from current book capital
(`pod_max_concurrent`, `pod_daily_loss_halt`) and the office totals across the epoch's evaluated books on their own
(`office_max_concurrent`, `office_daily_loss_halt`), all inside the submit write transaction. The caller's policy
caps still apply too (the tighter wins). Discovery books keep the policy caps only.

## Edge cases

1. Money path: none. Paper ledger only; no funded intent, credential or endpoint. Not on the fill path.
2. Speed: pure arithmetic, no I/O on any entry path.
3. Concurrency: pure functions, no module state. The ledger write lock and `transfer_id` cover concurrent hooks.
4. Absent vs zero vs unknown: pod sets of scores, capital and free cash differ, score `None`, a missing pod, a non-finite or bool number, or closes below the minimum
   = unknown = no transfer. A score of 0.0 is a real score. Missing capital or free cash for a pod = no transfer.
5. Dependency failure: the ledger refuses (fx unknown, insufficient capital, wrong rule) raise in the caller; the
   plan is not retried with another amount inside the same period.
6. Units: USD floats, cents. `period_key` is an opaque string; the module never turns it into time.
7. Sample: the score counts only the pod's own closed orders in that epoch; open marks are excluded (descriptive
   P&L stays in the report).
8. Rollback: a new rule is a new `version`, so a new epoch. Transfers are append-only ledger rows; a bad period is
   corrected only by the next period's rule output.

Not handled: discretionary reallocation, restaffing, firing (Lambda's org guard ends an evaluated epoch BROKEN);
model budget enforcement (scheduler consumes `model_share`); first-pick ordering (org consumes `pick_rank`).
