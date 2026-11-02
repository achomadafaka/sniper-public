# Office paper evaluation 1 — epoch open and runtime binding (Beta)

Status: design for review (Gamma, Alpha). Binds Gamma prereg `5273d4f7c` (`docs/audits/2026-10-29-office-paper-eval-1-preregistration.md`). Order path is Delta's slice (`sniper/office/eval_orders.py`); this document covers the epoch, roster, runtime binding and operating view only. Paper ledger only; no funded DB, no `.env`, no `--confirm`.

## 1. Why a new transition
`sniper/office/epoch_transition.py` is single-purpose: `OLD`/`NEW` are discovery-1/discovery-2, `_actors` requires exactly 4, `office_epoch_control` holds one transition, and `runtime.work_enabled` is pinned to discovery-2. Eval-1 adds a seat and a book, so it gets its own module `sniper/office/eval_transition.py` (same shape: dry run, apply, rollback-while-untouched, startup `adopted`). The discovery transition stays byte-identical.

## 2. What apply does (one Office write transaction, plus one ledger stage)
1. Preconditions: active epoch is `office-pilot-discovery-2` (control row), open, discovery; supervisor lease expired; 4 active actors; org chain verifies; `tool_hash()` equals the approved record (it includes `grade.read`/`lesson.propose`).
2. Seal discovery-2 (`closed_at`), write org_changes event `pilot_epoch_transition` with old/new epoch, old/new actors, manifest, approved record, fingerprints.
3. Roster: the 4 existing actors get generation+1 and the Epsilon prompt hashes `17f673691` (`config/office/prompts/office-paper-eval-1/sha256.json`). One new actor `Postmortem`, family claude, model `claude-sonnet-5-5`, generation 1, id `ag_` + sha256('office-paper-eval-1:postmortem')[:16].
4. Epoch payload: `epoch_id=office-paper-eval-1`, `kind='paper_eval'`, `evaluation_allowed=True` (lessons are evaluation-derived), `lessons_live=True`, `operator_waived_gates=True`, `book_id='office-paper-eval-1'`, `ledger_epoch_id='office-paper-eval-1'`, prereg sha, initial memory snapshot id (sealed at open via `memory_store.seal`), frozen config hashes (book policy, fill model `office-ps-paper-v1`, prompts, models, tool contract, hard filter).
5. Control row: `office_epoch_control` gains `history` (list of receipt ids); `active_epoch=office-paper-eval-1`, `receipt_id` = this event. `epoch_transition.adopted` keeps working for discovery-2 rows; a new `eval_transition.adopted` handles eval-1.
6. Ledger stage (separate DB, not atomic with Office): `ledger.open_epoch(office-paper-eval-1, clip 25)`, `open_book(office-paper-eval-1, kind='evaluated')`, `fund(1000, 'initial')`. Idempotent by ids; the runtime refuses to trade until the ledger book exists with exactly one initial flow (typed `eval_book_unbound`). Pilot-floor discovery book is left untouched and never pooled.

## 3. Runtime binding
- `work_enabled` accepts discovery-2 or eval-1; `discovery_only` is false only when the active epoch is eval-1 and the runtime config allows it (`evaluated_epoch_allowed`, Alpha's activation knob).
- `slot_of` maps Postmortem (done, `fea8643e4`); `pilot_models.PROVIDERS`, `pilot_seed.SLOTS`, roster 4-seat checks become seat-set aware: 4 seats for pilot/discovery, 5 for eval-1. `config/office/pilot_models.json` gains `postmortem` (claude, claude-sonnet-5-5) under a new pinned file `pilot_models_eval1.json` so discovery pins do not move.
- Judge turns: frame carries `candidates` (`8480349cf`) and `lessons` (`fea8643e4`); the eval-1 Judge answer adds `decision` (ENTER|SKIP|WAIT|EXIT), `candidate` (receipt_row_hash) and `public_reason` (<=160 chars). The host maps a committed Judge answer to Delta's `eval_orders.enter/exit`; aggregate stays unanimous ENTER across both Judges.

## 4. Operating view (prereg section 4)
Head and Judge frames get `book`: own open positions (order_id, mint, filled_at, spend_usd, latest mark at/lamports, mark age), `ledger.equity(book_id, mark_max_age_s=60)` typed state known/partial/unknown with unpriced count, today's realized PnL against `daily_loss_usd`, and an `equity_snapshot_id` (sha256 of the canonical view). Every decision record stores that id and the injected lesson ids. Never in the view: grade rows, closed per-coin PnL, win rate, NET after C, other epochs.

## 5. Edge cases
- Money path: paper ledger only; no funded module imported. Speed: no new call on any fill path; view is one bounded read per turn (orders_book_state index, LIMIT 3 open by max_concurrent).
- Concurrency: apply under `write_transaction` + expired lease; runtime reads control row at start and refuses a mid-run change (`epoch_record_changed`).
- Absent/zero/unknown: missing ledger book = `eval_book_unbound`, never zero equity; unpriced position shown unpriced; stale mark (>60 s) makes equity partial.
- Half-applied: Office transitioned, ledger stage missing -> runtime refuses to trade, rerun of the ledger stage is idempotent; never a second initial fund.
- Rollback: only while eval-1 has written nothing (same fingerprint rule); restores discovery-2 actors/epoch/control. After the first order the epoch can only end FAILED/BROKEN per prereg 5/6.
- Kill rule (prereg 5): Delta's submit refusal; the view shows the halt state.

## 6. Not handled here
Order construction, fills, marks, kill rule mechanics (Delta). Prompts (Epsilon). Opening the epoch on the live Office and the activation knob (Alpha). Pod reallocation / multi-book evaluated floor (Codex `0ee9672b0`, not needed for one book).

## Open questions for Alpha
1. Initial capital 1000 USD in a new evaluated book `office-paper-eval-1` (recommended), or continue on `pilot-floor`?
2. `max_concurrent` 3 and `daily_loss_usd` 100 from `book_policy_pilot.json` stay as the book rails (prereg 5 says yes); confirm clip 25.
