# Memecoin Custom TP UI Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore custom TP and SL edits in the funded dense table and preserve an mcap TP across later edits.

**Architecture:** Reuse the existing card editor, modal, API, and funded exit path. Add only the missing dense-table cells and correct the shared editor value precedence.

**Tech Stack:** JavaScript, Flask, pytest, Git worktrees, systemd pinned web runtime.

## Global Constraints

- Use ASD-STE100 Simplified Technical English.
- Do not change funded policy, exit nomination, quote confirmation, signing, broadcast, or database schema.
- Do not restart the funded memecoin runner.
- Do not run a full test suite.
- Use the exact merged SHA for the pinned web release.

---

### Task 1: Restore custom TP controls in the dense table

**Files:**

- Create: `tests/test_memecoin_custom_tp_ui.py`
- Modify: `static/app.js:3872-3934`

**Interfaces:**

- Consumes: `_tpDisplay(p)`, `_exitLegTxt(eff, leg, dflt, floor)`, and the existing `edit-exit-leg` click action.
- Produces: editable `TP` and `SL` table cells that open the existing position exit modal.

- [ ] **Step 1: Write the failing table contract test**

```python
from pathlib import Path


APP_JS = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text()


def _block(start, end):
    value = APP_JS.split(start, 1)[1]
    return value.split(end, 1)[0]


def test_dense_table_has_editable_tp_and_sl_columns():
    table = _block("    _realTable(positions) {", "    _entryContractDetails(p) {")
    assert "<th>TP</th><th>SL</th><th></th>" in table
    assert 'data-action="edit-exit-leg" data-pid="${p.id}" data-leg="tp"' in table
    assert 'data-action="edit-exit-leg" data-pid="${p.id}" data-leg="sl"' in table
    assert "this._tpDisplay(p)" in table
    assert "this._exitLegTxt(p.effective_exit, 'sl', p.chain_stop_pct, p.catastrophe_pct)" in table
    assert table.count('colspan="${cols.length + 3}"') == 2
```

- [ ] **Step 2: Run the table test and verify RED**

Run:

```bash
python3 -m pytest tests/test_memecoin_custom_tp_ui.py::test_dense_table_has_editable_tp_and_sl_columns -q
```

Expected: FAIL because `_realTable` has no TP or SL edit cell.

- [ ] **Step 3: Restore the table cells**

Change the table header suffix to:

```javascript
+ '<th>TP</th><th>SL</th><th></th>';
```

Insert these cells before the sell controls:

```javascript
<td data-action="edit-exit-leg" data-pid="${p.id}" data-leg="tp" style="white-space:nowrap;cursor:pointer" title="set take-profit">${this._tpDisplay(p)} <span style="opacity:.5">✎</span></td>
<td data-action="edit-exit-leg" data-pid="${p.id}" data-leg="sl" style="white-space:nowrap;cursor:pointer" title="set stop-loss">${this._exitLegTxt(p.effective_exit, 'sl', p.chain_stop_pct, p.catastrophe_pct)} <span style="opacity:.5">✎</span>${p.effective_exit ? ` <button class="btn btn-sm" data-action="clear-exit" data-pid="${p.id}" title="revert both legs to default rails" style="padding:0 4px">✕</button>` : ''}</td>
```

Change both chart-row column spans from `cols.length + 1` to `cols.length + 3`.

- [ ] **Step 4: Run the table test and verify GREEN**

Run:

```bash
python3 -m pytest tests/test_memecoin_custom_tp_ui.py::test_dense_table_has_editable_tp_and_sl_columns -q
```

Expected: PASS.

---

### Task 2: Preserve an mcap TP in the shared editor

**Files:**

- Modify: `tests/test_memecoin_custom_tp_ui.py`
- Modify: `static/app.js:4860-4872`

**Interfaces:**

- Consumes: an effective override with `tp_pct=0` and `tp_target_mcap=<absolute USD>`.
- Produces: `_exitLegRaw(eff, "tp")` editor state `{mode: "mcap", value: <short mcap>}`.

- [ ] **Step 1: Write the failing precedence test**

```python
def test_exit_editor_reads_mcap_before_zero_percent():
    editor = _block("    _exitLegRaw(eff, leg) {", "    _legFields(leg, mode, value) {")
    assert editor.index("if (mcap != null)") < editor.index("if (pct != null)")
```

- [ ] **Step 2: Run the precedence test and verify RED**

Run:

```bash
python3 -m pytest tests/test_memecoin_custom_tp_ui.py::test_exit_editor_reads_mcap_before_zero_percent -q
```

Expected: FAIL because `_exitLegRaw` checks `pct` first.

- [ ] **Step 3: Apply the minimal precedence fix**

Use this order in `_exitLegRaw`:

```javascript
if (mcap != null) return { mode: 'mcap', value: this._mcapShort(mcap) };
if (pct != null) {
  if (Number(pct) === 0) return { mode: 'none', value: '' };
  return { mode: 'pct', value: String(this._n(Math.abs(pct), 0)) };
}
return { mode: 'default', value: '' };
```

- [ ] **Step 4: Run both new tests and verify GREEN**

Run:

```bash
python3 -m pytest tests/test_memecoin_custom_tp_ui.py -q
```

Expected: `2 passed`.

- [ ] **Step 5: Run focused existing contracts**

Run:

```bash
python3 -m pytest tests/test_web_server_asset_paths.py tests/test_memecoin_exit_override_api.py tests/test_memecoin_real_exit_override.py -q
python3 scripts/validate_schema_refs.py
git diff --check
```

Expected: all focused tests pass, no schema violations, and no whitespace errors.

- [ ] **Step 6: Commit the implementation**

```bash
git add static/app.js tests/test_memecoin_custom_tp_ui.py
git commit -m "fix(memecoin): restore custom TP table controls"
```

---

### Task 3: Merge and deploy only the pinned web runtime

**Files:**

- Inspect: `.runtime/web/current`
- Inspect: `systemd/sniper-web.service`
- Do not change source files.

**Interfaces:**

- Consumes: the verified branch and current `origin/main`.
- Produces: `origin/main`, `.runtime/web/current`, and the live web process at one exact SHA.

- [ ] **Step 1: Merge through a clean local main worktree**

Fetch `origin/main`. Merge it into the feature branch if the remote advanced.

Run the Task 2 focused tests again after any merge.

Fast-forward local `main` to the verified feature branch. Push the exact SHA to `origin/main`.

Record the merged SHA:

```bash
MERGED_SHA="$(git rev-parse HEAD)"
git merge-base --is-ancestor "$MERGED_SHA" origin/main
```

- [ ] **Step 2: Create and activate the exact web release**

Create the detached release from `$MERGED_SHA`:

```bash
WEB_ROOT=/home/bigpapi/Desktop/Sniper/.runtime/web
OLD_RELEASE="$(readlink -f "$WEB_ROOT/current")"
git worktree add --detach "$WEB_ROOT/releases/$MERGED_SHA" "$MERGED_SHA"
```

Update `previous` and `current` with atomic symlink changes:

```bash
ln -sfn "$OLD_RELEASE" "$WEB_ROOT/previous.next"
mv -Tf "$WEB_ROOT/previous.next" "$WEB_ROOT/previous"
ln -sfn "$WEB_ROOT/releases/$MERGED_SHA" "$WEB_ROOT/current.next"
mv -Tf "$WEB_ROOT/current.next" "$WEB_ROOT/current"
sudo systemctl restart sniper-web.service
```

Restart only `sniper-web.service`. Do not restart the funded memecoin runner.

- [ ] **Step 3: Verify live ownership and served code**

Verify these conditions:

```text
origin/main SHA = web current SHA = web process release SHA
one web process owns the service
authenticated /api/memecoin/real/status returns HTTP 200
served app.js contains the table TP and SL edit cells
served app.js reads mcap before zero percent
```

Verify the funded runner PID did not change during the web deployment.

Use these read-only checks:

```bash
readlink -f .runtime/web/current
systemctl show sniper-web.service -p ActiveState -p MainPID -p ExecMainStartTimestamp
ps -eo pid,ppid,args | rg '[s]niper.web.server'
curl -fsS -u "$SNIPER_DASH_USER:$SNIPER_DASH_PASS" \
  http://127.0.0.1:5000/api/memecoin/real/status >/dev/null
curl -fsS -u "$SNIPER_DASH_USER:$SNIPER_DASH_PASS" \
  http://127.0.0.1:5000/static/app.js | rg 'edit-exit-leg|if \(mcap != null\)'
```

## Knowledge Sync

Knowledge delta: none. Reason: bugfix-only operator UI repair.
