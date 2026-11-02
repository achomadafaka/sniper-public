# Memecoin Manual Buy V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add immediate percent-based manual buys and measured refusal overrides without changing funded safety gates.

**Architecture:** The web writes an intent only. The funded runner converts a percent to selected-chain dollars at claim. A separate refusal-slot table stores one operator decision per funded refusal.

**Tech Stack:** Python, Flask, SQLite, browser JavaScript, CSS, pytest, and Playwright.

**Spec:** `docs/design/2026-08-29-memecoin-manual-buy-review.md`

## Global Constraints

- Keep funded execution in the runner.
- Keep all safety, route, balance, exposure, and kill-switch gates active.
- Use coin-band exit rails for a refusal override.
- Do not pass the source wallet into the manual policy input.
- Keep manual concurrency uncapped.
- Use selected-chain sizing equity. Exclude gas and unrealized marks.
- Use `13%` and `26%` as the only percent presets.
- Keep custom dollar orders.
- Use no confirmation modal.
- Require a phone press-and-hold action on Activity and refusal rows.
- Run focused tests only. Do not run the full suite.

---

### Task 1: Percent sizing and the manual intent contract

**Files:**
- Modify: `sniper/services/memecoin_risk.py`
- Modify: `sniper/services/memecoin_evm_risk.py`
- Modify: `sniper/services/memecoin_evm_leg.py`
- Modify: `sniper/services/memecoin_real_runner.py`
- Modify: `sniper/services/memecoin_manual_intents.py`
- Modify: `sniper/persistence/db_memecoin.py`
- Test: `tests/test_memecoin_fraction_sizing.py`
- Test: `tests/test_memecoin_manual_buy.py`
- Test: `tests/test_memecoin_manual_intents.py`

**Interfaces:**
- Produces: `manual_percent_size(...) -> float` in `memecoin_risk.py`.
- Produces: `size_pct_equity` and `resolved_size_usd` intent fields.
- Consumes: selected-chain quote balance, open cost basis, reserved cost basis, and global exposure.

- [ ] **Step 1: Write the failing sizing tests**

```python
def test_manual_percent_size_uses_selected_chain_cost_basis():
    assert manual_percent_size(
        quote_balance=340.0, open_notional_usd=50.0,
        pending_notional_usd=0.0, global_exposure_usd=50.0,
        max_total_exposure_usd=0.0, pct=0.13,
    ) == 50.70


def test_manual_percent_size_clamps_to_exposure_headroom():
    assert manual_percent_size(
        quote_balance=600.0, open_notional_usd=100.0,
        pending_notional_usd=0.0, global_exposure_usd=680.0,
        max_total_exposure_usd=700.0, pct=0.26,
    ) == 20.0
```

- [ ] **Step 2: Run the sizing tests and verify the expected failures**

Run:

```bash
python3 -m pytest tests/test_memecoin_fraction_sizing.py -q
```

Expected: the tests fail because `manual_percent_size` does not exist.

- [ ] **Step 3: Add the pure sizing calculation**

The function validates `0 < pct <= 1`. It floors the result to cents. It clamps to free quote funds and enabled exposure headroom.

- [ ] **Step 4: Write the failing intent tests**

```python
def test_percent_intent_keeps_percent_and_dollars_separate():
    iid = mi.enqueue(conn, mint="MINT", now_iso=T0, size_pct_equity=0.13)
    row = mi.read_status(conn, iid)
    assert row["size_pct_equity"] == 0.13
    assert row["size_usd"] is None
    assert row["resolved_size_usd"] is None


def test_drain_persists_the_runner_resolved_dollars():
    iid = mi.enqueue(conn, mint="MINT", now_iso=T0, size_pct_equity=0.26)
    mi.drain(conn, buy_fn, now_fn=lambda: T0)
    assert mi.read_status(conn, iid)["resolved_size_usd"] == 52.0
```

- [ ] **Step 5: Run the intent tests and verify the expected failures**

Run:

```bash
python3 -m pytest tests/test_memecoin_manual_intents.py -q
```

Expected: the new column and keyword tests fail.

- [ ] **Step 6: Add the intent fields and runner resolution**

Add `size_pct_equity REAL`, `resolved_size_usd REAL`, and `origin_event_id TEXT`. Reject an intent that has both dollar and percent input.

Make `make_real_buy_fn` accept `size_pct_equity`. Resolve the percent with the selected venue context. Return `resolved_size_usd` with all outcomes.

- [ ] **Step 7: Fix the existing EVM manual size seam**

Pass `size_override` through `build_evm_risk_context` and `reserve_entry_evm`. This keeps direct dollar and percent orders exact on EVM chains.

- [ ] **Step 8: Run the focused runner tests**

Run:

```bash
python3 -m pytest tests/test_memecoin_fraction_sizing.py tests/test_memecoin_manual_buy.py tests/test_memecoin_manual_intents.py -q
```

- [ ] **Step 9: Commit the task**

```bash
git add sniper/services/memecoin_risk.py sniper/services/memecoin_evm_risk.py \
  sniper/services/memecoin_evm_leg.py sniper/services/memecoin_real_runner.py \
  sniper/services/memecoin_manual_intents.py sniper/persistence/db_memecoin.py \
  tests/test_memecoin_fraction_sizing.py tests/test_memecoin_manual_buy.py \
  tests/test_memecoin_manual_intents.py
git commit -m "feat(memecoin): resolve manual percent size in runner"
```

### Task 2: Money-route validation and CSRF

**Files:**
- Modify: `sniper/routes/api_memecoin.py`
- Test: `tests/test_memecoin_manual_buy_api.py`

**Interfaces:**
- Consumes: `real_buy_fn(..., size_pct_equity=..., origin_event_id=...)`.
- Produces: a generic CSRF token and same-origin check for writable memecoin routes.

- [ ] **Step 1: Write route tests for percent validation and idempotency**

Test these cases with a Flask test client:

```python
assert post({"mint": "M", "size_pct_equity": 0.13}).status_code == 200
assert post({"mint": "M", "size_usd": 20, "size_pct_equity": 0.13}).status_code == 400
assert post({"mint": "M", "size_pct_equity": 1.1}).status_code == 400
assert post({"mint": "M", "size_pct_equity": 0.13}, csrf=False).status_code == 403
```

Use the same `intent_id` twice. Assert that both responses name the same queued intent.

- [ ] **Step 2: Run the route tests and verify the expected failures**

Run:

```bash
python3 -m pytest tests/test_memecoin_manual_buy_api.py -q
```

- [ ] **Step 3: Generalize the existing V6 CSRF helper**

Rename `_v6_csrf_token` and `_v6_csrf_ok` to generic local helpers. Keep V6 behavior unchanged.

- [ ] **Step 4: Validate the manual buy request**

Require exactly one size input. Pass `intent_id`, `origin_event_id`, and the selected size to the enqueue function. Never pass `source_user_id` for refusal overrides.

- [ ] **Step 5: Run the focused route tests**

Run:

```bash
python3 -m pytest tests/test_memecoin_manual_buy_api.py tests/test_memecoin_v6_paper_api.py -q
```

- [ ] **Step 6: Commit the task**

```bash
git add sniper/routes/api_memecoin.py tests/test_memecoin_manual_buy_api.py
git commit -m "feat(memecoin): protect manual money intents"
```

### Task 3: Refusal-slot measurement and API

**Files:**
- Modify: `sniper/persistence/db_memecoin_captures.py`
- Modify: `sniper/routes/api_memecoin.py`
- Create: `tests/test_memecoin_refusal_slots.py`

**Interfaces:**
- Produces: `memecoin_operator_refusal_slots`, keyed by `attempt_id`.
- Produces: `GET /api/memecoin/real/refusal-slots`.
- Produces: `POST /api/memecoin/real/refusal-slots/<attempt_id>/shown`.
- Produces: `POST /api/memecoin/real/refusal-slots/<attempt_id>/decision`.

- [ ] **Step 1: Write the failing store tests**

Test the following behavior:

```python
assert show_slot(conn, refusal, now_iso=T0)["slot_shown_at"] == T0
assert show_slot(conn, refusal, now_iso=T1)["slot_shown_at"] == T0
assert mark_visible(conn, attempt_id, now_iso=T1)["slot_visible_at"] == T1
assert decide_slot(conn, attempt_id, decision="dismiss", now_iso=T2)["operator_decision"] == "dismiss"
assert decide_slot(conn, attempt_id, decision="enter", now_iso=T3)["operator_decision"] == "dismiss"
```

- [ ] **Step 2: Run the store tests and verify the expected failures**

Run:

```bash
python3 -m pytest tests/test_memecoin_refusal_slots.py -q
```

- [ ] **Step 3: Add the one-row refusal table and store functions**

Use a primary key on `attempt_id`. Use `COALESCE` and conditional updates for every write-once field.

- [ ] **Step 4: Write the failing API tests**

Insert recent entry attempts for one selection gate and one safety gate. Assert that only the selection gate appears.

Assert that the list uses `EXEMPTIBLE_GATE_FAMILIES`. Assert that it returns exact refusal text and a 120-second expiry.

- [ ] **Step 5: Add the refusal routes**

Read recent attempts from the funded ledger. Open the refusal database only for slot state and quote-path data. Keep a refusal-database failure out of the funded decision path.

- [ ] **Step 6: Run the focused refusal tests**

Run:

```bash
python3 -m pytest tests/test_memecoin_refusal_slots.py tests/test_memecoin_refusal_capture.py -q
```

- [ ] **Step 7: Commit the task**

```bash
git add sniper/persistence/db_memecoin_captures.py sniper/routes/api_memecoin.py \
  tests/test_memecoin_refusal_slots.py
git commit -m "feat(memecoin): record operator refusal decisions"
```

### Task 4: Direct order dock and row actions

**Files:**
- Modify: `static/app.js`
- Modify: `static/styles.css`
- Modify: `tests/test_memecoin_ui_render_runtime.py`

**Interfaces:**
- Consumes: accounting `manual_presets`, the CSRF token, intent status, and refusal-slot routes.
- Produces: direct dock, Activity actions, refusal actions, and same-button states.

- [ ] **Step 1: Write the failing dock tests**

Test that the dock renders `BUY 13%` and `BUY 26%`. Click `BUY 13%`. Assert one POST with `size_pct_equity: 0.13` and no modal call.

Press Enter inside the dock. Assert that it sends the selected preset once. Press Enter outside the dock. Assert no request.

- [ ] **Step 2: Run the dock tests and verify the expected failures**

Run:

```bash
python3 -m pytest tests/test_memecoin_ui_render_runtime.py -q
```

- [ ] **Step 3: Replace the fixed-dollar chips**

Keep one custom dollar input. Add the two percent buttons. Use the server preview and mark it with `≈`.

Track each submitted intent by `intent_id` and button key. Render `QUEUED`, `CHECKING`, `FILLED`, `REFUSED`, and `EXPIRED` on that button.

- [ ] **Step 4: Write the failing Activity and refusal tests**

Assert that each buy event has two direct percent actions. Assert that an Activity click sends immediately on desktop.

Assert that each refusal shows the exact reason, drift, countdown, `ENTER`, and `DISMISS`. Assert that a safety refusal never appears.

- [ ] **Step 5: Add row actions and the phone hold guard**

Use one shared direct-buy function. On a coarse pointer, require a 450 ms hold before the request. Do not show a confirmation modal.

When a refusal enters, send `origin_event_id` and no source wallet. This selects coin-band rails.

- [ ] **Step 6: Run the focused UI tests**

Run:

```bash
python3 -m pytest tests/test_memecoin_ui_render_runtime.py tests/test_memecoin_ui.py -q
```

- [ ] **Step 7: Commit the task**

```bash
git add static/app.js static/styles.css tests/test_memecoin_ui_render_runtime.py
git commit -m "feat(memecoin): add direct percent buy controls"
```

### Task 5: Correct the review artifacts and verify the feature

**Files:**
- Modify: `docs/design/2026-08-29-memecoin-manual-buy-review.md`
- Modify: `docs/design/2026-08-29-memecoin-manual-buy-review.html`

- [ ] **Step 1: Update the prototype text**

Remove the false one-second live claim. Remove the three-slot exposure claim. State selected-chain sizing equity and coin-band override rails.

- [ ] **Step 2: Run schema validation**

Run:

```bash
python3 scripts/validate_schema_refs.py
```

- [ ] **Step 3: Run the focused verification set**

Run:

```bash
python3 -m pytest \
  tests/test_memecoin_fraction_sizing.py \
  tests/test_memecoin_manual_buy.py \
  tests/test_memecoin_manual_intents.py \
  tests/test_memecoin_manual_buy_api.py \
  tests/test_memecoin_refusal_slots.py \
  tests/test_memecoin_refusal_capture.py \
  tests/test_memecoin_ui_render_runtime.py \
  tests/test_memecoin_ui.py -q
```

- [ ] **Step 4: Inspect the final diff**

Run:

```bash
git diff --check
git status --short
git log --oneline --decorate -8
```

- [ ] **Step 5: Commit the artifacts**

```bash
git add docs/design/2026-08-29-memecoin-manual-buy-review.md \
  docs/design/2026-08-29-memecoin-manual-buy-review.html \
  docs/superpowers/plans/2026-08-29-memecoin-manual-buy-v2.md
git commit -m "docs(memecoin): finalize manual buy v2 contract"
```
