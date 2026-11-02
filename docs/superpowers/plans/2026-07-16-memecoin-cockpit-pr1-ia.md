# Memecoin Cockpit — PR1 (Information Architecture) Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Re-parent the 11 flat memecoin subtabs into 4 workspaces (Live / Paper / Radar / Wallets) with the existing subtabs as secondary views — a two-level nav that reuses the current `mc-section` + `_SECTION_LOADERS` machinery unchanged.

**Architecture:** Add a workspace nav row above the subtab row. Tag each subtab button with `data-workspace`. A new `_showWorkspace(ws)` shows only that workspace's subtabs and activates its last-used (or first) secondary view. `_showSubtab` is unchanged except it records the last-used subtab per workspace. Renderers, sections, endpoints, and polling are untouched.

**Tech Stack:** Vanilla JS (`static/app.js` IIFE), CSS tokens from PR0. Verification: `node --check`, authed curl of served bytes, Playwright visual, funded-path guard.

**Spec:** `docs/superpowers/specs/2026-07-16-memecoin-operator-cockpit-design.md` §5.

## Global Constraints

- **Funded path untouched.** Only `static/app.js` and `templates/index.html` (cache-bust) change. Guard in Task 6: `git diff --name-only` lists only those two; funded runner heartbeat `auth_ok=true` & PID unchanged before/after.
- **No renderer/section/endpoint changes.** `data-subtab` keys stay identical so `_SECTION_LOADERS` and the `mc-section[data-section=…]` divs keep working. This PR only re-groups and relabels the nav.
- **Nothing deleted.** All 11 sections remain reachable; they're grouped, not removed.
- **Default landing = Live › Cockpit.**
- **Cache-bust** `app.js` on change.

**Workspace → secondary-view map (the contract):**
| Workspace | Subtabs (data-subtab, in order) | Relabel |
|---|---|---|
| `live` | `real` | Real-Money → **Cockpit** |
| `paper` | `positions`, `closed` | Positions → **Strategy Lab** |
| `radar` | `radar`, `activity`, `decisions` | Radar → **Board** |
| `wallets` | `performance`, `discovery`, `traders`, `theses`, `x-sources` | Performance → **Wallet × Arm** |

---

### Task 1: Workspace nav styling + row

**Files:** Modify `static/app.js` — `<style>` block (`:2503`) and after `#mc-status` / before `#mc-subtabs` (`:2523`).

- [ ] **Step 1: Add `.mc-ws` styles** to the `<style>` block (after the `.mc-subtab.active` rule):

```javascript
          .mc-ws{background:none;border:0;color:var(--text-3);padding:8px 16px;cursor:pointer;font-size:var(--fs-md);font-weight:700;border-radius:8px 8px 0 0;white-space:nowrap}
          .mc-ws:hover{color:var(--text-2)}
          .mc-ws.active{color:var(--text-1);background:var(--surface-1)}
```

- [ ] **Step 2: Insert the workspace nav row** between `<div id="mc-status" …>` and `<div id="mc-subtabs" …>` (at `app.js:2523`):

```javascript
          <div id="mc-workspaces" style="display:flex;gap:4px;margin-bottom:2px;overflow-x:auto;-webkit-overflow-scrolling:touch">
            <button class="mc-ws" data-workspace-nav="live">💰 Live</button>
            <button class="mc-ws" data-workspace-nav="paper">Paper</button>
            <button class="mc-ws" data-workspace-nav="radar">Radar</button>
            <button class="mc-ws" data-workspace-nav="wallets">Wallets</button>
          </div>
```

- [ ] **Step 3:** `node --check static/app.js` → OK.
- [ ] **Step 4: Commit.** `git add static/app.js && git commit -m "feat(memecoin-ui): workspace nav row + styles (PR1/1)"`

---

### Task 2: Tag + regroup + relabel the subtab buttons

**Files:** Modify `static/app.js` — the 11-button block (`:2524`–`:2534`).

- [ ] **Step 1: Replace the whole button block** with the regrouped, `data-workspace`-tagged, relabeled version (keys unchanged):

```javascript
            <!-- Live -->
            <button class="mc-subtab" data-workspace="live" data-subtab="real" title="live real-money control + observability">Cockpit</button>
            <!-- Paper -->
            <button class="mc-subtab" data-workspace="paper" data-subtab="positions" title="paper A/B copy-mirror">Strategy Lab <span class="pill" style="font-size:9px">paper</span></button>
            <button class="mc-subtab" data-workspace="paper" data-subtab="closed">Closed <span class="pill" style="font-size:9px">paper</span></button>
            <!-- Radar -->
            <button class="mc-subtab" data-workspace="radar" data-subtab="radar" title="live per-coin entry-radar telemetry (read-only)">Board</button>
            <button class="mc-subtab" data-workspace="radar" data-subtab="activity">Activity</button>
            <button class="mc-subtab" data-workspace="radar" data-subtab="decisions" title="what the REAL algo actually did with each curated buy">Decisions</button>
            <!-- Wallets -->
            <button class="mc-subtab" data-workspace="wallets" data-subtab="performance">Wallet × Arm</button>
            <button class="mc-subtab" data-workspace="wallets" data-subtab="discovery">Discovery</button>
            <button class="mc-subtab" data-workspace="wallets" data-subtab="traders">Traders</button>
            <button class="mc-subtab" data-workspace="wallets" data-subtab="theses">Theses</button>
            <button class="mc-subtab" data-workspace="wallets" data-subtab="x-sources">X Sources <span class="pill" style="font-size:9px">display-only</span></button>
```

- [ ] **Step 2:** `node --check static/app.js` → OK.
- [ ] **Step 3: Commit.** `git add static/app.js && git commit -m "feat(memecoin-ui): tag subtabs by workspace, regroup + relabel (PR1/2)"`

---

### Task 3: `_showWorkspace` + state + last-used memory

**Files:** Modify `static/app.js` — add state/method near `_showSubtab` (`:2701`–`:2722`).

- [ ] **Step 1: Add workspace state + method.** Immediately before `_subtab: 'real',` (`app.js:2701`) insert:

```javascript
    _workspace: 'live',
    _WORKSPACES: { live: ['real'], paper: ['positions', 'closed'], radar: ['radar', 'activity', 'decisions'], wallets: ['performance', 'discovery', 'traders', 'theses', 'x-sources'] },
    _wsLast: { live: 'real', paper: 'positions', radar: 'radar', wallets: 'performance' },
    _showWorkspace(ws) {
      if (!this._WORKSPACES[ws]) return;
      this._workspace = ws;
      const el = document.getElementById('memecoin-board');
      if (!el) return;
      el.querySelectorAll('[data-workspace-nav]').forEach(b => b.classList.toggle('active', b.dataset.workspaceNav === ws));
      el.querySelectorAll('.mc-subtab').forEach(b => { b.style.display = (b.dataset.workspace === ws) ? '' : 'none'; });
      const subs = this._WORKSPACES[ws];
      const target = (subs.includes(this._wsLast[ws]) ? this._wsLast[ws] : subs[0]);
      this._showSubtab(target);
    },
```

- [ ] **Step 2: Record last-used subtab per workspace.** In `_showSubtab` (`:2709`), after `this._subtab = name;` add:

```javascript
      if (this._WORKSPACES[this._workspace] && this._WORKSPACES[this._workspace].includes(name)) this._wsLast[this._workspace] = name;
```

- [ ] **Step 3:** `node --check static/app.js` → OK.
- [ ] **Step 4: Commit.** `git add static/app.js && git commit -m "feat(memecoin-ui): _showWorkspace + per-workspace last-used subtab (PR1/3)"`

---

### Task 4: Wire the workspace click + init on Live

**Files:** Modify `static/app.js` — click delegation (`:2639`) and the init call (`:2692`).

- [ ] **Step 1: Handle workspace-nav clicks.** In the `el.addEventListener('click', …)` block, immediately before the `const stb = e.target.closest('[data-subtab]');` line (`app.js:2639`), add:

```javascript
          const wsb = e.target.closest('[data-workspace-nav]');
          if (wsb) { e.preventDefault(); this._showWorkspace(wsb.dataset.workspaceNav); return; }
```

- [ ] **Step 2: Initialize via workspace.** Replace the init call at `app.js:2692`:

```javascript
      this._showWorkspace(this._workspace || 'live');
```

(was `this._showSubtab(this._subtab || 'positions');` — `_showWorkspace` now drives the first render and correctly hides non-Live subtabs.)

- [ ] **Step 3:** `node --check static/app.js` → OK.
- [ ] **Step 4: Commit.** `git add static/app.js && git commit -m "feat(memecoin-ui): wire workspace switch + default to Live (PR1/4)"`

---

### Task 5: Cache-bust

**Files:** Modify `templates/index.html:845` (`app.js?v=164` → `?v=165`).

- [ ] **Step 1:** Bump `app.js?v=164` → `?v=165`.
- [ ] **Step 2:** `grep -c 'app.js?v=165' templates/index.html` → 1.
- [ ] **Step 3: Commit.** `git add templates/index.html && git commit -m "chore(memecoin-ui): cache-bust app.js v165 (PR1/5)"`

---

### Task 6: Verification gate

- [ ] **Step 1:** `node --check static/app.js` → OK.
- [ ] **Step 2: Funded-path guard.** `git diff --name-only origin/main...HEAD` lists ONLY `static/app.js`, `templates/index.html`.
- [ ] **Step 3: Runner untouched.** `cat data/memecoin_runner.heartbeat` → `auth_ok` true, age < 30s; funded runner PID unchanged.
- [ ] **Step 4: Restart web (web-only), verify served bytes.** Restart via `bash scripts/sniper_web_watchdog.sh` after killing the web PID **by PID** (never `pkill -f "sniper.web.server"` — it self-matches and kills the shell). Authed curl: served `app.js?v=165` contains `_showWorkspace`, `data-workspace-nav`, all four `data-workspace-nav="live|paper|radar|wallets"`, and `Wallet × Arm`.
- [ ] **Step 5: Visual (desktop + mobile).** Playwright: default lands on Live › Cockpit; clicking Paper shows only Strategy Lab + Closed; Radar shows Board/Activity/Decisions; Wallets shows the 5 wallet subtabs; each remembers its last subtab on return. No clipped nav on 390px.

---

## Self-Review

- **Spec coverage:** implements §5's 4-workspace map exactly (table above). Live's Accounting/Venues remain panels inside Cockpit for now (they're rendered by `_loadReal`); splitting them into separate secondary views is deferred to the cockpit rebuild (PR2+) — stated, not silent.
- **Placeholder scan:** every step has the concrete edit and exact anchor line.
- **Consistency:** `data-workspace` (subtab buttons) vs `data-workspace-nav` (workspace buttons) are deliberately distinct so click delegation disambiguates. `data-subtab` keys unchanged → `_SECTION_LOADERS`/sections untouched. `_wsLast` keys match `_WORKSPACES` keys.
- **Risk:** low — pure nav layer; no renderer/endpoint/poll change; every section still reachable.
