# Memecoin Live UI Render and Trader Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair two Memecoin render errors and keep each trader chain action visible without horizontal scroll.

**Architecture:** A Playwright pytest fixture runs the actual `static/app.js` methods in Chromium. The Traders surface uses a semantic grid with native `details` rows.

**Tech Stack:** JavaScript, HTML, scoped CSS, Python, pytest, and Playwright Chromium.

## Global Constraints

- Keep each API payload unchanged.
- Keep each database table unchanged.
- Keep funded authority and selector logic unchanged.
- Keep research chains without a funded action.
- Keep the survivor diagnostic separate from the funded selector.
- Use an isolated worktree from `origin/main` at `df8d3d9c`.
- Use the exact merged SHA for the web deployment.

---

### Task 1: Repair the Armed renderer

**Files:**

- Create: `tests/test_memecoin_ui_render_runtime.py`
- Modify: `static/app.js:6407-6408`

**Interfaces:**

- Consumes: `GET /api/memecoin/armed?chain=<chain>`.
- Produces: A complete `#mc-armed` table without a `ReferenceError`.

- [ ] **Step 1: Add the browser test fixture**

The fixture must load `static/styles.css` and `static/app.js` in Chromium.
Expose the private Memecoin object only inside the test script.

```python
source = APP_JS.read_text().replace(
    "  return {\n    state,\n",
    "  return {\n    memecoin,\n    state,\n",
    1,
)
page.add_script_tag(content=source + "\nwindow.__memecoin = Sniper.memecoin;")
```

The page must contain `#tab-memecoin`, `#memecoin-board`, and `.content-area`.
The fetch double must return a real `Response` for each requested API path.

- [ ] **Step 2: Add the Armed regression test**

Use one relevant Solana row.
The row must include `funded_arm_available=true` and a strict-cohort `funded_arm`.
Call `await window.__memecoin._loadArmed()`.
Assert that the table contains the handle and `trail_d` explanation.
Assert that the browser records no page error.

- [ ] **Step 3: Run the Armed test and confirm RED**

Run:

```bash
python3 -m pytest tests/test_memecoin_ui_render_runtime.py::test_armed_renderer_uses_the_active_api_row -q
```

Expected: FAIL with `ReferenceError: w is not defined`.

- [ ] **Step 4: Apply the smallest Armed fix**

```javascript
const fundedArm = r.funded_arm;
const fundedUnavailable = r.funded_arm_available === false;
```

- [ ] **Step 5: Run the Armed test and confirm GREEN**

Run the command from Step 3.
Expected: one passed test and no browser error.

- [ ] **Step 6: Commit the Armed fix**

```bash
git add static/app.js tests/test_memecoin_ui_render_runtime.py
git commit -m "fix(memecoin): repair Armed row render"
```

---

### Task 2: Repair the Paper results renderer

**Files:**

- Modify: `tests/test_memecoin_ui_render_runtime.py`
- Modify: `static/app.js:7032-7057`

**Interfaces:**

- Consumes: A chain-scoped paper report with closed rows and open episode arms.
- Produces: A complete `#mc-closed` table with an honest censored count.

- [ ] **Step 1: Add the Paper results regression test**

Use one event with three closed arms and one open arm.
Give two closed arms a numeric `full_book_pnl`.
Give one closed arm a null `full_book_pnl`.
Set the episode arm count to four.

Replace `_fetchPaperReport` with an async function that returns this report.
Replace `_renderPaperSummary` with a function that returns an empty string.
Call `await window.__memecoin._loadClosed()`.

Assert that the Known cell contains `2/4` and `1 cens`.
Assert that the browser records no page error.

- [ ] **Step 2: Run the Paper results test and confirm RED**

Run:

```bash
python3 -m pytest tests/test_memecoin_ui_render_runtime.py::test_closed_renderer_counts_only_closed_censored_arms -q
```

Expected: FAIL with `ReferenceError: censored is not defined`.

- [ ] **Step 3: Apply the smallest Paper results fix**

Add this value after `known`:

```javascript
const censored = grp.length - known.length;
```

Do not include `stillOpen` in this value.

- [ ] **Step 4: Run the Paper results test and confirm GREEN**

Run the command from Step 2.
Expected: one passed test and the text `2/4 1 cens`.

- [ ] **Step 5: Commit the Paper results fix**

```bash
git add static/app.js tests/test_memecoin_ui_render_runtime.py
git commit -m "fix(memecoin): define the censored arm count"
```

---

### Task 3: Replace the wide trader matrix with compact detail rows

**Files:**

- Modify: `tests/test_memecoin_ui_render_runtime.py`
- Modify: `static/app.js:2999-3380`
- Modify: `static/app.js:6876-7000`

**Interfaces:**

- Consumes: `d.arms`, `d.wallets[].by_arm`, the open exposure fields, and the combined roster.
- Produces: `.mc-trader-board`, `.mc-trader-row`, and `.mc-trader-arm-grid`.

- [ ] **Step 1: Add the desktop layout test**

Use eight arm names and one funded Solana wallet.
Set the viewport to 1,365 by 768 pixels.
Call `await window.__memecoin._loadTraders()`.

Measure `#mc-traders`, `.mc-trader-board`, and the chain action button.
Assert that the board scroll width does not exceed its client width by more than two pixels.
Assert that the button right edge does not exceed the wrapper right edge.

- [ ] **Step 2: Add the detail and narrow layout test**

Click the first `.mc-trader-row > summary`.
Assert that `.mc-trader-arm-grid` shows all eight configured arms.

Set the viewport to 390 by 844 pixels.
Assert that the Signals and Best arm header cells are hidden.
Assert that the mobile secondary text is visible.
Assert that the chain action button remains inside the wrapper.

- [ ] **Step 3: Run both layout tests and confirm RED**

Run:

```bash
python3 -m pytest tests/test_memecoin_ui_render_runtime.py -k "trader" -q
```

Expected: FAIL because the compact row selectors do not exist.

- [ ] **Step 4: Add the scoped trader styles**

Add styles to the existing Memecoin style block.
Use this layout model:

```css
.mc-trader-board{width:100%;min-width:0}
.mc-trader-head,.mc-trader-row>summary{display:grid;grid-template-columns:minmax(180px,1.35fr) 76px minmax(170px,.9fr) 96px 90px 104px;gap:8px;align-items:center}
.mc-trader-row>summary{cursor:pointer;padding:9px 10px;list-style:none;border-top:1px solid var(--border-subtle)}
.mc-trader-row>summary::-webkit-details-marker{display:none}
.mc-trader-arm-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:7px;padding:9px 10px 12px 28px;background:var(--surface-0)}
.mc-trader-arm-cell{min-width:0;border:1px solid var(--border-subtle);border-radius:8px;padding:7px 9px;background:var(--surface-2)}
.mc-trader-mobile-meta{display:none}
@media(max-width:720px){
  .mc-trader-head,.mc-trader-row>summary{grid-template-columns:minmax(0,1fr) 72px 64px 88px;gap:4px}
  .mc-trader-signals,.mc-trader-best{display:none}
  .mc-trader-mobile-meta{display:block}
  .mc-trader-arm-grid{grid-template-columns:repeat(auto-fit,minmax(115px,1fr));padding-left:10px}
}
```

- [ ] **Step 5: Build the semantic trader rows**

Change `armCell` to `armControl` and return only its inner markup.
Keep the current chain and research-only checks.

For each wallet, select the highest-PnL arm with at least one close.
Keep the API arm order as the tie break.
Label this value `survivor diagnostic` in its title.

Render this structure:

```html
<div class="mc-trader-board" role="table">
  <div class="mc-trader-head" role="row">
    <span role="columnheader">Trader</span>
    <span class="mc-trader-signals" role="columnheader">Signals</span>
    <span class="mc-trader-best" role="columnheader">Best arm</span>
    <span role="columnheader">Total</span>
    <span role="columnheader">Open</span>
    <span role="columnheader">Action</span>
  </div>
  <div class="mc-trader-row" role="row">
    <details class="mc-trader-details">
      <summary>Trader | Signals | Best arm | Total | Open</summary>
      <div class="mc-trader-arm-grid" role="row">
        ${arms.map(arm => armDetail(w.by_arm[arm], arm)).join('')}
      </div>
    </details>
    <div class="mc-trader-action" role="cell">${armControl(w.source_user_id)}</div>
  </div>
</div>
```

The mobile Trader cell must include Signals and Best arm as secondary text.
Each arm cell must show `n/a`, no closes, or PnL with close and win counts.

- [ ] **Step 6: Run both layout tests and confirm GREEN**

Run the command from Step 3.
Expected: both tests pass at desktop and narrow widths.

- [ ] **Step 7: Run all render tests**

```bash
python3 -m pytest tests/test_memecoin_ui_render_runtime.py -q
```

Expected: all tests pass without page errors.

- [ ] **Step 8: Commit the compact trader layout**

```bash
git add static/app.js tests/test_memecoin_ui_render_runtime.py
git commit -m "fix(memecoin): keep trader actions visible"
```

---

### Task 4: Verify, merge, deploy, and probe the live release

**Files:**

- Inspect: `scripts/deploy_memecoin_runtime.sh`
- Inspect: the web service unit and `.runtime/web/current`

**Interfaces:**

- Consumes: the final branch commits and `origin/main`.
- Produces: an exact merged SHA in the pinned web runtime.

- [ ] **Step 1: Run focused verification**

```bash
python3 -m pytest tests/test_memecoin_ui_render_runtime.py tests/test_memecoin_api_armed_and_screen_targets.py tests/test_memecoin_ui.py tests/test_memecoin_ui_poll_stability.py -q
python3 scripts/validate_schema_refs.py
git diff --check origin/main...HEAD
```

- [ ] **Step 2: Run the applicable Memecoin UI suite**

```bash
python3 -m pytest tests/test_memecoin_ui*.py tests/test_memecoin_api_armed_and_screen_targets.py -q
```

- [ ] **Step 3: Merge into current `main` without a pull request**

Fetch `origin/main` again.
Stop if the remote branch moved into a conflicting change.
Merge the feature branch into a clean local `main` worktree.
Push the tested merged SHA to `origin/main`.

- [ ] **Step 4: Deploy the exact merged SHA**

Use the repository web deployment path found in the current scripts.
Do not deploy from the mutable checkout.
Restart only the web process unless the deployment script requires a shared restart.

- [ ] **Step 5: Verify the runtime and browser**

Verify `.runtime/web/current` resolves to the merged SHA.
Verify one web process runs from that release.
Verify the authenticated page returns success.

Open the Armed, Paper results, and Traders surfaces.
Assert that the browser console has no `w` or `censored` error.
Assert that all three surfaces contain data.
Assert that the desktop and narrow Traders actions are visible.

## Knowledge Sync

Knowledge delta: none.
Reason: this work changes only bugfix and display behavior.
