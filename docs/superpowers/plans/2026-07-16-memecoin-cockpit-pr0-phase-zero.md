# Memecoin Cockpit — PR0 (Phase-Zero Cleanup) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the safe, no-new-cards first slice of the operator-cockpit redesign: introduce a design-token sheet, kill the duplicate arm-status render, fix mobile subtab navigation, and get closed history out of the live view.

**Architecture:** Pure `static/` front-end change on the memecoin surface. A new `static/css/mc-tokens.css` defines CSS custom properties; the memecoin renderers in `static/app.js` migrate their shared component classes and status-color constants to those tokens. Only *surface/border* token values are re-themed (navy/slate); semantic gain/loss/warn keep their current values. No data, no routes, no funded path.

**Tech Stack:** Vanilla JS (`static/app.js` IIFE), plain CSS, Flask-served static assets, Playwright for visual verification, `node --check` for JS sanity.

**Spec:** `docs/superpowers/specs/2026-07-16-memecoin-operator-cockpit-design.md` (§4 tokens, §10 phase-zero).

## Global Constraints

- **Funded path untouched.** No file under the funded runner / rails / exit sweep is modified. This PR changes only `static/app.js`, `static/css/mc-tokens.css` (new), and `templates/index.html`. Verification in Task 7: `git diff --name-only` lists only those three paths; runner heartbeat `data/memecoin_runner.heartbeat` shows `auth_ok=true` and age < 30s both before and after.
- **Identity `(network_id, mint)`; Robinhood 4663 explicit.** Not touched by PR0 but must not regress.
- **No visual change except the three intended ones** (dup-status removal, mobile nav, base-surface re-theme). Token values for gain/loss/warn/info/neutral are defined to the CURRENT hex so class migration is pure indirection.
- **No inline hex in code this PR touches** — migrated classes reference tokens.
- **Cache-bust** every asset version on change (`styles.css?v=`, `app.js?v=`) so the operator's browser loads new code.

---

### Task 1: Design-token sheet + wired into the page

**Files:**
- Create: `static/css/mc-tokens.css`
- Modify: `templates/index.html:11` (add `<link>` after the `styles.css` line)

**Interfaces:**
- Produces: CSS custom properties on `:root` — surfaces `--surface-0/1/2`, borders `--border-subtle/-strong`, status `--gain/-bg`, `--loss/-bg`, `--warn/-bg`, `--info/-bg`, `--neutral/-bg`, text `--text-1/2/3`, freshness `--fresh/--stale/--unknown`, type `--fs-lg/md/sm/xs`, spacing `--sp-1..5`. Consumed by Tasks 2–5.

- [ ] **Step 1: Write `static/css/mc-tokens.css`.** Status/text tokens use the CURRENT app hex (no visual change); surface tokens use the new navy/slate values (the one intended re-theme).

```css
/* Memecoin operator-cockpit design tokens. Status/text = current values (no visual
   change on migration); surfaces re-themed navy/slate to kill the Matrix-green base. */
:root {
  /* surfaces — re-themed (navy/slate) */
  --surface-0:#0b1220; --surface-1:#0f172a; --surface-2:#131c31;
  --border-subtle:#243044; --border-strong:#33405a;
  /* status — CURRENT values, so class migration is pure indirection */
  --gain:#3fb950; --gain-bg:rgba(63,185,80,.14);
  --loss:#f85149; --loss-bg:rgba(248,81,73,.14);
  --warn:#d29922; --warn-bg:rgba(210,153,34,.14);
  --info:#58a6ff; --info-bg:rgba(88,166,255,.14);
  --neutral:#8b949e; --neutral-bg:rgba(139,148,158,.12);
  /* text */
  --text-1:#e6edf3; --text-2:#c9d1d9; --text-3:#8b949e;
  /* freshness (honesty rail) */
  --fresh:#3fb950; --stale:#d29922; --unknown:#6e7681;
  /* type + spacing */
  --fs-lg:20px; --fs-md:14px; --fs-sm:12px; --fs-xs:11px;
  --sp-1:4px; --sp-2:8px; --sp-3:12px; --sp-4:16px; --sp-5:24px;
}
```

- [ ] **Step 2: Link it before `app.js` runs.** In `templates/index.html`, add after line 11:

```html
    <link rel="stylesheet" href="/static/css/mc-tokens.css?v=1">
```

- [ ] **Step 3: Verify it loads.** Run: `python3 -c "import pathlib,sys; sys.exit(0 if pathlib.Path('static/css/mc-tokens.css').exists() else 1)"` → exit 0. Confirm the `<link>` is present: `grep -c 'mc-tokens.css' templates/index.html` → `1`.

- [ ] **Step 4: Commit.**

```bash
git add static/css/mc-tokens.css templates/index.html
git commit -m "feat(memecoin-ui): add design-token sheet, wire into page (PR0/1)"
```

---

### Task 2: Remove the duplicate arm-status render

**Files:**
- Modify: `static/app.js` — the `_loadReal` banner block (`:2976`–`:2983`)

**Interfaces:**
- Consumes: `_armState(rs)` (`:2760`), `_armPill(rs)` (`:2774`) — unchanged. The persistent `#mc-status` pill (`:2746`) remains the single arm-state source.

- [ ] **Step 1: Capture the baseline.** With the web server running, Playwright-screenshot the Real tab (desktop) so the "status shown twice" is on record. Save under `.playwright-cli/`.

- [ ] **Step 2: Edit the banner to drop the duplicated label + dot, keep the config line.** Replace the banner block at `app.js:2976`–`2983` so it no longer renders `s.label`/the status dot (those live in the `#mc-status` pill); it shows only the policy/clips/slots config it uniquely owns:

```javascript
      // --- config banner (arm STATE lives in the persistent #mc-status pill; not duplicated here) ---
      let html = `<div class="card" style="padding:10px 18px;margin-bottom:14px;border:1px solid var(--border-subtle)">
        <div style="font-size:var(--fs-sm);color:var(--text-3)">policy <b style="color:var(--text-2)">${esc(env.policy || '—')}</b> · ${this._clipTxt(env)} · <b style="color:var(--text-2)">${env.max_concurrent}</b> slots${env.max_exposure > 0 ? ` / $${this._n(env.max_exposure, 0)} cap` : ''}</div>
      </div>`;
```

- [ ] **Step 3: Verify — arm state appears exactly once.** Reload the Real tab, Playwright-screenshot it, read it back. Acceptance: the arm label (e.g. "LIVE" / "DISARMED") appears in the top `#mc-status` pill only, not a second time in the Real-tab banner; the policy/clips/slots line is still present.

- [ ] **Step 4: `node --check static/app.js`** → no error.

- [ ] **Step 5: Commit.**

```bash
git add static/app.js
git commit -m "fix(memecoin-ui): render real-money arm state once (drop banner dup) (PR0/2)"
```

---

### Task 3: Migrate shared `.mc-*` classes + status constants to tokens

**Files:**
- Modify: `static/app.js` — the `<style>` block at `:2502`–`:2519` and `_armState` color constants at `:2761`

**Interfaces:**
- Consumes: tokens from Task 1.
- Note: this migrates only the shared component classes and the arm-status constants — NOT the ~116 long-tail inline hex in per-position renderers. Those renderers are rewritten in PR2+; migrating them now would be a large untested sweep. This scoping is deliberate and stated in the spec (§10/§11).

- [ ] **Step 1: Migrate the shared component classes.** In the `<style>` block (`app.js:2503`–`2519`), replace hardcoded chrome hex with tokens (values equal, except surfaces which now resolve to the re-themed navy tokens):

```javascript
          .mc-subtab{background:none;border:0;border-bottom:2px solid transparent;color:var(--text-3);padding:8px 14px;cursor:pointer;font-size:var(--fs-md);font-weight:600;white-space:nowrap}
          .mc-subtab:hover{color:var(--text-2)}
          .mc-subtab.active{color:var(--info);border-bottom-color:var(--info)}
          @keyframes mcpulse{0%,100%{opacity:1}50%{opacity:.3}}
          .mc-kpi{background:var(--surface-1);border:1px solid var(--border-subtle);border-radius:8px;padding:12px 14px;min-width:150px;flex:1}
          .mc-kpi .lbl{font-size:var(--fs-xs);color:var(--text-3);text-transform:uppercase;letter-spacing:.4px}
          .mc-kpi .val{font-size:var(--fs-lg);font-weight:700;margin-top:3px}
          .mc-ven{background:var(--surface-1);border:1px solid var(--border-subtle);border-radius:8px;padding:12px 14px;flex:1;min-width:240px}
          .mc-chk{font-size:var(--fs-sm);display:flex;align-items:center;gap:6px;margin-top:3px}
          .mc-addr{font-family:ui-monospace,monospace;font-size:var(--fs-xs);color:var(--text-3);cursor:pointer}
```

- [ ] **Step 2: Migrate `_armState` color constants** (`app.js:2761`) to the status tokens so the arm banner/pill draw from the semantic palette:

```javascript
      const RED = 'var(--loss-bg)', AMBER = 'var(--warn-bg)', GREEN = 'var(--gain-bg)', GREY = 'var(--neutral-bg)';
```

Leave the `color:` hex in `_armState`'s returned objects for this PR (they feed inline `style` strings that also compose `${s.color}55` alpha suffixes — token migration of those is a PR2 concern noted in the spec). Only the `bg` constants move now.

- [ ] **Step 3: Verify no regression + surfaces re-themed.** Playwright-screenshot the Real and Radar tabs; read back. Acceptance: KPI/venue cards and subtab bar render on the navy surface (not the old near-black Matrix panel), text is legible (AA), no layout shift, arm pill/banner colors unchanged (gain green / warn amber / loss red intact).

- [ ] **Step 4: `node --check static/app.js`** → no error.

- [ ] **Step 5: Commit.**

```bash
git add static/app.js
git commit -m "refactor(memecoin-ui): shared classes + arm-status bg to design tokens (PR0/3)"
```

---

### Task 4: Fix mobile subtab navigation

**Files:**
- Modify: `static/app.js` — the `#mc-subtabs` container (`:2523`) and the subtab click handler (`:2716`)

**Interfaces:**
- Consumes: the existing `.mc-subtab` click wiring at `app.js:2716`.

- [ ] **Step 1: Add scroll affordance + snap to the subtab bar.** Update the `#mc-subtabs` inline style (`app.js:2523`) to add scroll-snap and touch momentum, and a bottom fade is unnecessary — snapping + scroll-into-view (Step 2) is enough:

```javascript
          <div id="mc-subtabs" style="display:flex;gap:2px;flex-wrap:nowrap;margin-bottom:14px;border-bottom:1px solid var(--border-subtle);overflow-x:auto;scroll-snap-type:x proximity;-webkit-overflow-scrolling:touch">
```

- [ ] **Step 2: Scroll the activated subtab into view.** In the `.mc-subtab` click handler (around `app.js:2716`, where `b.classList.add('active')` runs), add after the active class is set:

```javascript
        b.scrollIntoView({ inline: 'center', block: 'nearest', behavior: 'smooth' });
```

Add `scroll-snap-align:center` to `.mc-subtab` in the `<style>` block (Task 3 Step 1 edit region):

```javascript
          .mc-subtab{background:none;border:0;border-bottom:2px solid transparent;color:var(--text-3);padding:8px 14px;cursor:pointer;font-size:var(--fs-md);font-weight:600;white-space:nowrap;scroll-snap-align:center}
```

- [ ] **Step 3: Verify on a narrow viewport.** Playwright at 390×844: screenshot the memecoin tab, tap a right-side subtab (e.g. `Performance`), screenshot again, read back. Acceptance: the tapped subtab scrolls into view and is reachable; no content clipped off-screen; the bar scrolls rather than wrapping into a broken second row.

- [ ] **Step 4: `node --check static/app.js`** → no error.

- [ ] **Step 5: Commit.**

```bash
git add static/app.js
git commit -m "fix(memecoin-ui): reachable mobile subtab nav (snap + scroll-into-view) (PR0/4)"
```

---

### Task 5: Get "Real closed" out of the default live view

**Files:**
- Modify: `static/app.js` — the "Real closed" render inside `_loadReal` (`:3116`)

**Interfaces:**
- Consumes: the existing closed-render markup at `app.js:3116`. Full re-parent into a Live › History secondary view is PR1 (IA); PR0 only collapses it.

- [ ] **Step 1: Wrap the Real-closed block in a collapsed `<details>`.** At `app.js:3116`, change the header line so the closed section is collapsed by default (out of the live scan) but one click away:

```javascript
          <details style="margin-top:14px"><summary style="cursor:pointer;font-weight:600;font-size:var(--fs-md);color:var(--text-2)">Real closed <span style="color:var(--text-3);font-size:var(--fs-xs)">last ${cls.length}${this._chainFilter !== 'all' ? ' · ' + chainLabel(this._chainFilter) + ' only' : ''}</span></summary>
```

Ensure the matching closing tag for the block becomes `</details>` (locate the block's current closing `</div>` for this section and update it — verify by reading `_loadReal` around the closed render before editing).

- [ ] **Step 2: Verify.** Reload the Real tab, screenshot. Acceptance: the live view opens with NO closed rows expanded; a "Real closed" disclosure is present and expands to the table on click.

- [ ] **Step 3: `node --check static/app.js`** → no error.

- [ ] **Step 4: Commit.**

```bash
git add static/app.js
git commit -m "fix(memecoin-ui): collapse real-closed history out of live view (PR0/5)"
```

---

### Task 6: Cache-bust the assets

**Files:**
- Modify: `templates/index.html:11` (`styles.css?v=`), `templates/index.html:845` (`app.js?v=163`)

- [ ] **Step 1: Bump versions.** `styles.css?v=94` → `?v=95`; `app.js?v=163` → `?v=164`. (mc-tokens.css stays `?v=1` — first release.)

- [ ] **Step 2: Verify.** `grep -n 'app.js?v=164\|styles.css?v=95' templates/index.html` → two hits.

- [ ] **Step 3: Commit.**

```bash
git add templates/index.html
git commit -m "chore(memecoin-ui): cache-bust app.js/styles for PR0 (PR0/6)"
```

---

### Task 7: Final verification gate (funded path + visual + JS)

**Files:** none (verification only).

- [ ] **Step 1: JS sanity.** `node --check static/app.js` → no error.

- [ ] **Step 2: Funded-path guard.** `git diff --name-only origin/main...HEAD` lists ONLY: `static/app.js`, `static/css/mc-tokens.css`, `templates/index.html`. Any other path = STOP, revert it.

- [ ] **Step 3: Runner untouched.** `cat data/memecoin_runner.heartbeat` → `auth_ok` is `true` and timestamp age < 30s; the funded runner PID is unchanged from before this PR (compare `ps aux | grep [m]emecoin_runner_loop`).

- [ ] **Step 4: Visual acceptance (desktop + mobile).** Playwright before/after pairs for Real, Paper, Radar at 1280×900 and 390×844, read back. Acceptance checklist: (a) arm state shown once; (b) navy surfaces, no Matrix-green base panels; (c) subtab bar reachable on mobile; (d) live view opens with no closed rows; (e) no clipped content, no horizontal body scroll.

- [ ] **Step 5: Restart the web server so it serves the bumped assets, then re-verify one live endpoint renders.** (Web-only restart; do NOT touch the funded runner.)

```bash
pkill -f "[s]niper.web.server"; sleep 1
nohup python3 -m sniper.web.server >/tmp/sniper_web.log 2>&1 &
sleep 3
curl -s -u "$SNIPER_DASH_USER:$SNIPER_DASH_PASS" http://127.0.0.1:5000/static/css/mc-tokens.css | head -1
```

Acceptance: the token sheet is served (200, CSS body), and `curl -sI .../static/app.js?v=164` is 200.

---

## Self-Review

- **Spec coverage:** PR0 implements spec §10 items 1–5 (dup status, tokens, mobile nav, label cleanup deferred to per-card PRs where labels get rewritten, Closed out of live). IA re-parent (§5) and card rebuilds (§6, §8) are explicitly later PRs — not in scope here.
- **Placeholder scan:** every step has the actual edit. Task 5 Step 1 requires reading the block's closing tag before editing (called out) — that's a real instruction, not a placeholder.
- **Type/name consistency:** token names in Task 1 match their uses in Tasks 2–5. `_armState`/`_armPill`/`_loadReal` line refs verified against current `static/app.js`.
- **Risk:** contained. Only surface tokens re-theme; status/text tokens equal current values; long-tail inline-hex migration deferred with a stated reason (no silent cap).
