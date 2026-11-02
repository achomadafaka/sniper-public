# Memecoin Cockpit — PR3 (Per-Coin Chart + Buy Consensus Drawer) Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Give each real-position card a "▸ timeline & chart" affordance that opens a persistent per-coin drawer showing the **mini price graph**, **buy consensus** (which wallets bought this coin and when, from real feed signals), our **paper arms**, and our **real decisions** — reusing the existing radar timeline drawer, targeted to a real-cockpit element that survives the 4s poll.

**Honest-scope note (important):** the spec's §6 "holder consensus" (who still *holds*, remaining size) is **not backed by data**. `memecoin_holder_evidence` is a deliberately fail-closed lane: of 2237 rows, only 4 are `positive` with `remaining_usd` and 12 have a handle; 2233 are `unknown`+invalidated (verified on `sniper_tracker.db` 2026-07-16). Live per-wallet holdings of external wallets aren't tracked. PR3 therefore delivers **buy** consensus (real, from `memecoin_feed_observations_v2` signals) instead of **hold** consensus, and **names the gap in-UI** — it does not render a fabricated holder table.

**Architecture:** Add `#mc-real-drawer` (sibling to `#mc-real`, so `_loadReal`'s 4s innerHTML rebuild never touches it). Parametrize the existing `_openRadarTimeline(coinKey, keepOpen, targetElId)` with a target element (default unchanged = `mc-radar-drawer`). A card affordance calls it with `mc-real-drawer`. Add a buy-consensus summary line + a holdings-not-tracked honesty note to the shared drawer (accurate on both surfaces).

**Tech Stack:** Vanilla JS (`static/app.js`). Verify: `node --check`, authed curl, funded-path guard.

**Spec:** `docs/superpowers/specs/2026-07-16-memecoin-operator-cockpit-design.md` §6 (chart + consensus blocks) with the honest-scope amendment above.

## Global Constraints

- **Funded path untouched.** Only `static/app.js` + `templates/index.html` (cache-bust). Guard: `git diff --name-only` lists only those two; funded runner healthy on pinned release, `auth_ok=true` (PID may differ — it self-restarts on its watchdog cadence).
- **No extra network on the 4s poll.** The drawer fetches `coin_timeline` **once per click** (on demand), into an element outside `#mc-real`; the poll never re-fetches it. This satisfies the no-poll-network constraint.
- **No fabricated data.** No holder table. Buy consensus is labeled as buy observations; the holdings gap is stated in-UI.
- **Radar board unaffected.** `_openRadarTimeline`'s default target stays `mc-radar-drawer`; the added summary + note are accurate for the radar board too.
- Cache-bust `app.js`.

---

### Task 1: Parametrize `_openRadarTimeline` + add buy-consensus summary + honesty note

**Files:** Modify `static/app.js` — `_openRadarTimeline` (signature + the `mc-radar-drawer` lookup + the signals section).

- [ ] **Step 1: Parametrize the target element.** Change the signature and the element lookup:

Signature `async _openRadarTimeline(coinKey, keepOpen) {` → `async _openRadarTimeline(coinKey, keepOpen, targetElId) {`
Lookup `const el = document.getElementById('mc-radar-drawer');` → `const el = document.getElementById(targetElId || 'mc-radar-drawer');`

- [ ] **Step 2: Add a buy-consensus summary + holdings honesty note.** In `_openRadarTimeline`, replace the signals section header line:

```javascript
        <div style="font-weight:600;font-size:12px;margin:8px 0 4px">Signals — claimed vs observed</div>
```

with a version that prepends the consensus summary (computed from `t.signals`):

```javascript
        ${(() => { const buys = (t.signals || []).filter(s => s.side === 'buy'); const wallets = new Set(buys.map(s => s.user).filter(Boolean)).size; const last = buys.map(s => s.observed_at).filter(Boolean).sort().slice(-1)[0]; return `<div style="margin:6px 0"><span class="pill" style="color:var(--info)" title="distinct wallets observed BUYING this coin (feed observations — not current holdings)">buy consensus: ${wallets} wallet${wallets === 1 ? '' : 's'}</span>${last ? ` <span class="text-muted" style="font-size:11px">last buy ${esc(this._ageRealIso(last))}</span>` : ''}</div>`; })()}
        <div class="text-muted" style="font-size:11px;margin-bottom:4px" title="external wallets' live holdings are not tracked (holder-evidence lane is fail-closed); the signals below are buy observations, not current positions">live per-wallet holdings not tracked — buy observations only</div>
        <div style="font-weight:600;font-size:12px;margin:8px 0 4px">Signals — claimed vs observed</div>
```

- [ ] **Step 3:** `node --check static/app.js` → OK.

---

### Task 2: Add the real-cockpit drawer element

**Files:** Modify `static/app.js` — the real section template.

- [ ] **Step 1:** In the `<div class="mc-section" data-section="real">…</div>` line, insert `#mc-real-drawer` between `#mc-real` and `#mc-chart-real`:

```javascript
          <div class="mc-section" data-section="real"><div id="mc-real"></div><div id="mc-real-drawer" style="margin-top:12px"></div><div id="mc-chart-real" style="margin-top:12px"></div></div>
```

- [ ] **Step 2:** `node --check static/app.js` → OK.

---

### Task 3: Card affordance + click wiring

**Files:** Modify `static/app.js` — `_realCard` (add footer affordance) and the click delegation.

- [ ] **Step 1: Add the affordance** to `_realCard`, as a new final section before the closing `</div>` of the card (immediately after the exit-controls `</div>`):

```javascript
        <div class="mc-pos-sec"><span data-action="rtimeline" data-coin="${esc((p.network_id || '') + '|' + p.mint)}" style="cursor:pointer;color:var(--info);font-size:var(--fs-xs)">▸ timeline &amp; chart</span></div>
```

- [ ] **Step 2: Wire the handler.** In the click delegation (after the `else if (a === 'rclose') …` line), add:

```javascript
          else if (a === 'rtimeline') { this._openRadarTimeline(t.dataset.coin, false, 'mc-real-drawer'); const d = document.getElementById('mc-real-drawer'); if (d) d.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); }
          else if (a === 'rtimeline-close') { const d = document.getElementById('mc-real-drawer'); if (d) d.innerHTML = ''; }
```

- [ ] **Step 3:** `node --check static/app.js` → OK.

---

### Task 4: Cache-bust + verification gate

**Files:** Modify `templates/index.html` (`app.js?v=166` → `?v=167`).

- [ ] **Step 1:** Bump `app.js?v=166` → `?v=167`; `grep -c 'app.js?v=167' templates/index.html` → 1.
- [ ] **Step 2:** `node --check static/app.js` → OK.
- [ ] **Step 3: Funded-path guard.** `git diff --name-only origin/main...HEAD` lists ONLY `static/app.js`, `templates/index.html` (+ this plan doc).
- [ ] **Step 4: Runner healthy.** Funded runner alive on pinned release `f7cb9463`, heartbeat `auth_ok=true`, single instance (`ps -eo … | grep memecoin_runner_loop`).
- [ ] **Step 5: Restart web (by PID, never `pkill -f`), authed-curl verify** served `app.js?v=167` contains `_openRadarTimeline(coinKey, keepOpen, targetElId)`, `mc-real-drawer`, `data-action="rtimeline"`, `buy consensus:`, and `holdings not tracked`. Confirm `coin_timeline` for a live real coin returns `price_path` length > 2 and `signals` non-empty (buy consensus has data).
- [ ] **Step 6: Visual (desktop + mobile).** Playwright Live › Cockpit: each card shows "▸ timeline & chart"; clicking opens the drawer below with a sparkline + "buy consensus: N wallets" + the honesty note + paper-arm table; drawer survives a 4s poll (doesn't vanish); ✕/re-click works.

---

## Self-Review

- **Spec coverage (amended):** §6 mini price graph — delivered (real `price_path`). §6 holder consensus — **replaced** with buy consensus from real feed signals + explicit in-UI naming of the missing hold-tracking data (fabrication avoided per doctrine). Deferred still: real manual partial-sell; Accounting/Venues as separate Live views.
- **Placeholder scan:** all steps have concrete code; the consensus summary IIFE is complete.
- **Reuse/consistency:** reuses `_openRadarTimeline` + `_sparkline` + `_ageRealIso` (all present). Drawer element pattern mirrors `#mc-chart-real`/`#mc-radar-drawer`. `coinKey` = `nid|mint` matches `_openRadarTimeline`'s existing `.split('|')`.
- **Poll safety:** `#mc-real-drawer` is a sibling of `#mc-real`; `_loadReal` only rewrites `#mc-real`, so the drawer persists and is never re-fetched on the 4s cadence.
- **Risk:** low — additive (new element + affordance + one param); the shared drawer's default target is unchanged so the radar board is unaffected.

## Post-merge follow-up (not code)

- Report the holder-evidence stub finding to the operator and save a brain memory: `memecoin_holder_evidence` is fail-closed and ~0.5% populated, so live external-wallet holdings aren't available for the cockpit; buy consensus (feed signals) is the honest proxy.
