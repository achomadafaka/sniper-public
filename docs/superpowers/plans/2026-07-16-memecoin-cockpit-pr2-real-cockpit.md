# Memecoin Cockpit — PR2 (Real Position Cards) Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Replace the unreadable 18-column real-positions table with one readable **card per position** — same data, same controls, laid out so the operator reads it without hover text. Sort needs-action first.

**Architecture:** Extract the per-row logic into a `_realCard(p)` method that returns one card's HTML, reusing every existing helper (`_durTxt`, `_n`, `_usd`, `_money`, `_col`, `_quoteAge`, `_tpDisplay`, `_exitLegTxt`, `chainPill`, `chainOf`) and preserving every existing `data-action` (chart, toggle-mirror, edit-exit-leg, clear-exit, rclose). `_loadReal` sorts the positions and renders a card grid instead of a `<table>`. Card styling via PR0 tokens.

**Tech Stack:** Vanilla JS (`static/app.js`), CSS tokens. Verify: `node --check`, authed curl, funded-path guard.

**Spec:** `docs/superpowers/specs/2026-07-16-memecoin-operator-cockpit-design.md` §6. **Scope note:** holder consensus + mini price graph (§6 lower blocks) are **PR3** — they need a per-coin fetch and expand-state preserved across the 4s `_loadReal` re-render; deliberately out of PR2 (stated, not silent). Real manual **partial** sell is also PR3+ (needs a real partial-sell path; PR2 keeps the existing full **Close** + mirror toggle + TP/SL override).

## Global Constraints

- **Funded path untouched.** Only `static/app.js` + `templates/index.html` (cache-bust). Same data + same POST actions as today — this is a display swap, not a behavior change. Guard in Task 4: `git diff --name-only` lists only those two; funded runner healthy on pinned release, `auth_ok=true` before/after (note: the runner self-restarts on its own watchdog cadence — verify it's alive on release `f7cb9463`, not that the PID is identical).
- **Every existing control preserved.** All `data-action` hooks from the current row must appear on the card: `chart`, `toggle-mirror`, `edit-exit-leg` (tp+sl), `clear-exit`, `rclose`.
- **Honesty rails.** Unknown → "—"; Robinhood 4663 liq/vol → muted "n/a"; quote age shown; no fabricated zeros.
- **No new money action.** No new real partial-sell. Cache-bust `app.js`.

---

### Task 1: Card CSS

**Files:** Modify `static/app.js` — `<style>` block, after the `.mc-ven` rule.

- [ ] **Step 1: Add card classes** after the `.mc-ven{…}` line:

```javascript
          .mc-pos{background:var(--surface-1);border:1px solid var(--border-subtle);border-radius:10px;padding:12px 14px;flex:1 1 340px;min-width:300px;max-width:520px}
          .mc-pos.act{border-color:var(--warn)}
          .mc-pos-h{display:flex;justify-content:space-between;align-items:baseline;gap:8px;margin-bottom:6px}
          .mc-pos-tk{font-weight:700;font-size:var(--fs-md);color:var(--info);text-decoration:none}
          .mc-pos-row{display:flex;justify-content:space-between;gap:10px;font-size:var(--fs-sm);margin:3px 0}
          .mc-pos-sec{border-top:1px solid var(--border-subtle);margin-top:8px;padding-top:8px}
          .mc-pos-lbl{color:var(--text-3);font-size:var(--fs-xs)}
```

- [ ] **Step 2:** `node --check static/app.js` → OK.

---

### Task 2: `_realCard(p)` method

**Files:** Modify `static/app.js` — insert a method immediately before `async _loadReal(withBalances) {`.

- [ ] **Step 1: Insert the method** (verbatim; it ports the current row's exact expressions into a card):

```javascript
    _realCard(p) {
      const tick = p.ticker || (p.mint || '').slice(0, 8);
      const pnl = p.pnl_pct, notion = p.entry_notional;
      const valNow = (pnl != null && notion != null) ? notion * (1 + pnl / 100) : null;
      const pnlUsd = (pnl != null && notion != null) ? notion * (pnl / 100) : null;
      const peak = p.max_gain_pct;
      const giveback = (peak != null && pnl != null && peak > pnl) ? (peak - pnl) : null;
      const held = this._durTxt(p.held_s);
      const holdPct = p.timecap_s > 0 ? Math.min(100, (p.held_s || 0) / p.timecap_s * 100) : null;
      const qAge = this._quoteAge(p.last_quote_ts);
      const eff = p.effective_exit;
      const dexBlind = chainOf(p.mint, p.network_id) === 'robin';
      const naCell = (label) => `<span class="text-muted" title="no market-data feed for Robinhood 4663 — ${label} unavailable on this chain">n/a</span>`;
      const needsAct = p.stuck_bag || p.last_quote_price == null;
      const mirrorPill = p.mirror_disabled
        ? `<span class="pill" data-action="toggle-mirror" data-pid="${p.id}" data-disabled="1" data-ticker="${esc(tick)}" style="color:var(--warn);cursor:pointer" title="mirror-exit OFF — holding past the trader's sell (TP/stop/timecap still active). Click to re-enable the copy-exit.">mirror OFF · holding</span>`
        : `<span class="pill" data-action="toggle-mirror" data-pid="${p.id}" data-disabled="0" data-ticker="${esc(tick)}" style="color:var(--gain);cursor:pointer" title="mirror-exit ON — auto-sells when the copied trader sells. Click to HOLD past their sell.">mirror ON</span>`;
      const flags = [
        mirrorPill,
        p.stuck_bag ? '<span class="pill" style="color:var(--loss)" title="sell ladder exhausted — un-exited">STUCK BAG</span>' : '',
        p.derisked ? '<span class="pill" style="color:var(--gain)" title="entry recovered — remainder rides on house money">derisked</span>' : '',
        p.last_quote_price == null ? '<span class="pill" style="color:var(--warn)" title="no live quote — SL blind; unquotable-exit guard will force an exit attempt">no quote</span>' : '',
      ].filter(Boolean).join(' ');
      const srcTip = `their size $${this._n(p.their_size_usd || 0, 0)}${p.entry_lag_s != null ? ` · copied ${this._durTxt(p.entry_lag_s)} after` : ''}${p.fill_gap_pct != null ? ` · our fill ${p.fill_gap_pct >= 0 ? '+' : ''}${Number(p.fill_gap_pct).toFixed(1)}% vs their price` : ''}`;
      const src = p.source_handle ? esc(p.source_handle) : '—';
      return `<div class="mc-pos${needsAct ? ' act' : ''}">
        <div class="mc-pos-h">
          <div><a href="#" class="mc-pos-tk" data-action="chart" data-mint="${esc(p.mint)}" data-network="${esc(p.network_id || '')}" data-chart-el="mc-chart-real" title="${esc(p.mint)}">${esc(tick)}</a>
            ${chainPill(chainOf(p.mint, p.network_id))} <span class="pill">${p.venue}</span>${p.strategy ? ` <span class="mc-pos-lbl">${esc(p.strategy)}</span>` : ''}</div>
          <div class="mc-pos-lbl" title="hold time${holdPct != null ? ' · ' + holdPct.toFixed(0) + '% of timecap' : ''}">held ${held}${holdPct != null ? ` · ${holdPct.toFixed(0)}%` : ''}</div>
        </div>
        ${flags ? `<div style="margin-bottom:2px">${flags}</div>` : ''}
        <div class="mc-pos-sec">
          <div class="mc-pos-row"><span><span class="mc-pos-lbl">entry→now</span> ${this._n(p.entry_price, 6)} → ${p.last_quote_price == null ? '—' : this._n(p.last_quote_price, 6)}${qAge ? ` <span class="mc-pos-lbl" title="quote age">(${qAge})</span>` : ''}</span>
            <span><span class="mc-pos-lbl">value</span> ${valNow == null ? '—' : this._usd(valNow)}</span></div>
          <div class="mc-pos-row"><span style="color:${this._col(pnl)}"><span class="mc-pos-lbl">PnL</span> ${pnl == null ? '—' : `${this._money(pnlUsd)} (${pnl >= 0 ? '+' : ''}${this._n(pnl, 1)}%)`}</span>
            <span><span class="mc-pos-lbl">peak</span> ${peak == null ? '—' : `<span style="color:var(--gain)">+${this._n(peak, 0)}%</span>${giveback != null ? ` <span class="mc-pos-lbl" title="handed back from peak">▼${this._n(giveback, 0)}</span>` : ''}`}</span></div>
          <div class="mc-pos-row"><span class="mc-pos-lbl">mcap ${this._usd(p.market_cap)} · liq ${dexBlind && p.liquidity == null ? naCell('liquidity') : this._usd(p.liquidity)} · vol ${dexBlind && p.volume_24h == null ? naCell('24h volume') : this._usd(p.volume_24h)}</span></div>
        </div>
        <div class="mc-pos-sec mc-pos-lbl" title="${esc(srcTip)}">copied ${src}${p.their_size_usd != null ? ` · their $${this._n(p.their_size_usd, 0)}` : ''}${p.entry_lag_s != null ? ` · lag ${this._durTxt(p.entry_lag_s)}` : ''}${p.fill_gap_pct != null ? ` · fill ${p.fill_gap_pct >= 0 ? '+' : ''}${Number(p.fill_gap_pct).toFixed(1)}%` : ''}</div>
        <div class="mc-pos-sec" style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          <span data-action="edit-exit-leg" data-pid="${p.id}" data-leg="tp" style="cursor:pointer" title="set take-profit (% / target mcap / none)"><span class="mc-pos-lbl">TP</span> ${this._tpDisplay(p)} ✎</span>
          <span data-action="edit-exit-leg" data-pid="${p.id}" data-leg="sl" style="cursor:pointer" title="set stop-loss (% / floor mcap / none)"><span class="mc-pos-lbl">SL</span> ${this._exitLegTxt(eff, 'sl', p.stop_pct)} ✎</span>
          ${eff ? `<button class="btn btn-sm" data-action="clear-exit" data-pid="${p.id}" title="revert both legs to default rails" style="padding:0 6px">✕</button>` : ''}
          <button class="btn btn-sm" data-action="rclose" data-pid="${p.id}" data-ticker="${esc(tick)}" data-venue="${p.venue}" data-notional="${this._n(notion, 2)}" data-mint="${esc(p.mint)}" style="margin-left:auto" title="close on-chain via the exit executor (real money)">Close</button>
        </div>
      </div>`;
    },
```

- [ ] **Step 2:** `node --check static/app.js` → OK.

---

### Task 3: Render the card grid (replace the table)

**Files:** Modify `static/app.js` — the `} else { … }` table block inside `_loadReal` (the `realPositions` render, currently the `<table>` from `<div class="table-wrapper" style="overflow-x:auto"><table>` through `html += \`</tbody></table></div>\`;`).

- [ ] **Step 1: Replace the whole `else` body** (the table build + `realPositions.forEach` row loop) with a sort + card grid:

```javascript
      } else {
        // needs-action first (stuck bag / no quote), then worst PnL, then largest deployed
        realPositions.sort((a, b) => {
          const na = (a.stuck_bag || a.last_quote_price == null) ? 0 : 1;
          const nb = (b.stuck_bag || b.last_quote_price == null) ? 0 : 1;
          if (na !== nb) return na - nb;
          const pa = (a.pnl_pct == null) ? 0 : a.pnl_pct, pb = (b.pnl_pct == null) ? 0 : b.pnl_pct;
          if (pa !== pb) return pa - pb;
          return (b.entry_notional || 0) - (a.entry_notional || 0);
        });
        html += `<div style="display:flex;flex-wrap:wrap;gap:12px">` + realPositions.map(p => this._realCard(p)).join('') + `</div>`;
      }
```

- [ ] **Step 2:** `node --check static/app.js` → OK.

---

### Task 4: Cache-bust + verification gate

**Files:** Modify `templates/index.html` (`app.js?v=165` → `?v=166`).

- [ ] **Step 1:** Bump `app.js?v=165` → `?v=166`; `grep -c 'app.js?v=166' templates/index.html` → 1.
- [ ] **Step 2:** `node --check static/app.js` → OK.
- [ ] **Step 3: Funded-path guard.** `git diff --name-only origin/main...HEAD` lists ONLY `static/app.js`, `templates/index.html` (+ this plan doc).
- [ ] **Step 4: Runner healthy.** Funded runner alive on pinned release `f7cb9463`, heartbeat `auth_ok=true`, age < 30s.
- [ ] **Step 5: Restart web (web-only, by PID — never `pkill -f "sniper.web.server"`), authed-curl verify** served `app.js?v=166` contains `_realCard(p)`, `class="mc-pos`, and all five actions still present in the real render: `data-action="toggle-mirror"`, `data-action="edit-exit-leg"`, `data-action="clear-exit"`, `data-action="rclose"`, `data-action="chart" ... data-chart-el="mc-chart-real"`.
- [ ] **Step 6: Visual (desktop + mobile).** Playwright Live › Cockpit: cards render (no table), needs-action cards border amber and sort first, no clipped content on 390px, PnL colored, controls clickable.

---

## Self-Review

- **Spec coverage:** §6 header / MTM (entry→now, value, PnL, peak+giveback, mcap/liq/vol, quote age) / provenance (source, their size, copy lag, fill drift) / exit controls (mirror, TP, SL, target-mcap via `_tpDisplay`/`_exitLegTxt`, clear, Close) — all present. Holder consensus + mini graph + real partial-sell explicitly deferred to PR3 (stated).
- **Placeholder scan:** `_realCard` is complete verbatim code; no TODOs.
- **Control preservation:** the five `data-action` hooks are all carried onto the card (Task 4 Step 5 asserts each in served bytes) — this is the highest-risk item (a dropped action = a dead control), so it's an explicit verification gate.
- **Consistency:** `_realCard(p)` uses only `p` + existing helpers (`_tpDisplay`, `_exitLegTxt`, `_quoteAge`, `_durTxt`, `_usd`, `_money`, `_n`, `_col`, `chainPill`, `chainOf`, `esc`) — all confirmed present in current `app.js`.
- **Risk:** medium (rewrites the real-money renderer) but bounded — same data object, same actions, verified in served bytes + visually. No behavior/endpoint/money change.
