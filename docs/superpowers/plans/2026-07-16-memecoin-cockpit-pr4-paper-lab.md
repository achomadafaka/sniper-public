# Memecoin Cockpit — PR4 (Paper Strategy Lab) Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Replace the paper "Strategy Lab" per-arm-row table with **one card per copy-episode** `(source_handle, network_id, mint)` showing every arm side-by-side (open + exited) and an explicit **divergence line** — so the operator reads "same entry, different exit policy → different outcome" without scanning duplicated rows.

**Architecture:** `_loadOpen` fetches `/positions/open` (open arms + marks) **and** `/positions/closed` (exited arms) once each, groups both by the episode key, renders one `_paperLabCard` per episode with an `_armDivergence` summary. Cards = episodes that have ≥1 open arm (live episodes); their exited sibling arms are pulled from the recent-closed set. Reuses PR2 card CSS + existing helpers + existing `sell`/`close`/`chart` actions.

**Tech Stack:** Vanilla JS (`static/app.js`). Verify: `node --check`, authed curl, funded-path guard.

**Spec:** `docs/superpowers/specs/2026-07-16-memecoin-operator-cockpit-design.md` §8. **Note:** the system has **7** paper arms (`control, real_shadow, scalp_c2, scalp_c, alpha_b, momentum_a, trail_d`), not the 3 the spec named — the Lab is arm-agnostic (shows whichever arms the episode has).

## Global Constraints

- **Funded path untouched.** Only `static/app.js` + `templates/index.html`. Guard: `git diff --name-only` lists only those two; funded runner healthy on pinned release, `auth_ok=true`.
- **Reuse endpoints as-is.** `/positions/open` (`{positions, marks}`) + `/positions/closed` (`{positions, stats}`). No backend change. Closed set is recent-200 — if an episode's exited arm isn't in it, it simply isn't shown (no completeness claim).
- **Existing controls preserved.** Open arms keep `sell` (50%) + `close`; ticker keeps `chart`. Trader/arm/chain filters keep working.
- **No fabricated data.** Aligned episodes say "arms aligned"; divergence is computed from real open %PnL / closed realized PnL.
- Cache-bust `app.js`.

**Data contract (both from `memecoin_positions`, `execution_mode='paper'`):** open row → `id, mint, network_id, ticker, source_handle, source_user_id, strategy, entry_price, opened_at` + mark `{position_id, unrealized_pnl, current_price, liquidity, stale}`; closed row → `mint, network_id, ticker, source_handle, strategy, entry_price, realized_pnl, exit_reason, opened_at, closed_at`.

---

### Task 1: Replace `_loadOpen` + add `_paperLabCard` / `_armDivergence`

**Files:** Modify `static/app.js` — replace the entire `_loadOpen` method (from `async _loadOpen() {` through its closing `},`, before `async _loadTraders()`), inserting the two helpers after it.

- [ ] **Step 1: Apply the replacement** (via script `scripts/tmp` or an Edit). New `_loadOpen`:

```javascript
    async _loadOpen() {
      const el = document.getElementById('mc-open');
      if (!el) return;
      let d, cd;
      try {
        [d, cd] = await Promise.all([
          fetchJson('/api/memecoin/positions/open', { suppressErrorToast: true }),
          fetchJson('/api/memecoin/positions/closed', { suppressErrorToast: true }).catch(() => ({ positions: [] })),
        ]);
      } catch (e) { el.innerHTML = `<div class="text-muted">Open positions unavailable: ${esc(String(e && e.message || e))}</div>`; return; }
      const marks = {};
      (d.marks || []).forEach(mk => { marks[mk.position_id] = mk; });
      let open = (d.positions || []).slice();
      this._syncTraderFilter(open);
      this._syncChainFilter('mc-chain-filter', open);
      if (this._traderFilter && this._traderFilter !== 'all')
        open = open.filter(p => (p.source_handle || p.source_user_id) === this._traderFilter);
      if (this._armFilter && this._armFilter !== 'all')
        open = open.filter(p => (p.strategy || 'control') === this._armFilter);
      open = filterByChain(open, this._chainFilter);
      const closed = filterByChain((cd.positions || []), this._chainFilter);
      const key = p => (p.source_handle || p.source_user_id || '?') + '|' + (p.network_id || '') + '|' + p.mint;
      const groups = {};
      open.forEach(p => { const k = key(p); (groups[k] = groups[k] || { open: [], closed: [], p }).open.push(p); });
      closed.forEach(p => { const k = key(p); if (groups[k]) groups[k].closed.push(p); });
      const bestU = g => Math.max(-Infinity, ...g.open.map(p => (marks[p.id] && marks[p.id].unrealized_pnl != null) ? marks[p.id].unrealized_pnl : -Infinity));
      const order = Object.values(groups).sort((a, b) => bestU(b) - bestU(a));
      el.innerHTML = order.length
        ? `<div style="display:flex;flex-wrap:wrap;gap:12px">` + order.map(g => this._paperLabCard(g, marks)).join('') + `</div>`
        : `<div class="text-muted" style="font-size:13px;padding:8px 0">No open paper positions.</div>`;
      if (this._chartMint) this._renderChart(this._chartMint, 'mc-chart', this._chartNetwork);
    },
    _armRank(s) { return ({ control: 0, real_shadow: 1, scalp_c2: 2, scalp_c: 3, alpha_b: 4, momentum_a: 5, trail_d: 6 })[s] ?? 9; },
    _paperLabCard(g, marks) {
      const p0 = g.p;
      const tick = p0.ticker || (p0.mint || '').slice(0, 6);
      const chn = chainOf(p0.mint, p0.network_id);
      const openArms = g.open.slice().sort((a, b) => this._armRank(a.strategy) - this._armRank(b.strategy));
      const closedArms = g.closed.slice().sort((a, b) => this._armRank(a.strategy) - this._armRank(b.strategy));
      const openRow = (p) => {
        const mk = marks[p.id] || {};
        const pct = (p.entry_price > 0 && mk.current_price != null) ? (mk.current_price / p.entry_price - 1) * 100 : null;
        const up = mk.unrealized_pnl;
        return `<tr>
          <td class="mc-pos-lbl">${esc(p.strategy || 'control')}</td>
          <td><span class="pill" style="color:var(--info)">open</span></td>
          <td class="num" style="color:${this._col(pct)}${mk.stale ? ';opacity:.55' : ''}">${pct == null ? '—' : (pct >= 0 ? '+' : '') + pct.toFixed(0) + '%'}</td>
          <td class="num" style="color:${this._col(up)}${mk.stale ? ';opacity:.55' : ''}">${this._money(up)}</td>
          <td style="white-space:nowrap"><button class="btn btn-sm" data-action="sell" data-pid="${p.id}" data-frac="0.5" title="sell 50% of the ${esc(p.strategy || 'control')} arm (paper)">50%</button> <button class="btn btn-sm btn-danger" data-action="close" data-pid="${p.id}" title="close the ${esc(p.strategy || 'control')} arm (paper)">close</button></td>
        </tr>`;
      };
      const closedRow = (p) => `<tr style="opacity:.85">
          <td class="mc-pos-lbl">${esc(p.strategy || 'control')}</td>
          <td><span class="pill" style="color:var(--text-3)" title="exit reason">exited</span></td>
          <td class="num text-muted" style="font-size:11px">${esc(p.exit_reason || '—')}</td>
          <td class="num" style="color:${this._col(p.realized_pnl)}">${this._money(p.realized_pnl)}</td>
          <td></td>
        </tr>`;
      return `<div class="mc-pos">
        <div class="mc-pos-h">
          <div><a href="#" class="mc-pos-tk" data-action="chart" data-mint="${esc(p0.mint)}" data-network="${esc(p0.network_id || '')}" title="${esc(p0.mint)}">${esc(tick)}</a> ${chainPill(chn)}</div>
          <div class="mc-pos-lbl">copied ${esc(p0.source_handle || '—')} · ${esc(this._ageRealIso(p0.opened_at))}</div>
        </div>
        <table class="data-table compact" style="width:100%;margin-top:4px"><thead><tr><th>arm</th><th>state</th><th class="num">result</th><th class="num">PnL</th><th></th></tr></thead>
          <tbody>${openArms.map(openRow).join('')}${closedArms.map(closedRow).join('')}</tbody></table>
        ${this._armDivergence(openArms, closedArms, marks)}
      </div>`;
    },
    _armDivergence(openArms, closedArms, marks) {
      const openPcts = openArms.map(p => { const mk = marks[p.id] || {}; return (p.entry_price > 0 && mk.current_price != null) ? (mk.current_price / p.entry_price - 1) * 100 : null; }).filter(v => v != null);
      if (openArms.length && closedArms.length) {
        const realized = closedArms.map(p => p.realized_pnl).filter(v => v != null);
        const bestC = realized.length ? Math.max(...realized) : null, worstC = realized.length ? Math.min(...realized) : null;
        const bestOpen = openPcts.length ? Math.max(...openPcts) : null;
        const cTxt = bestC == null ? '' : (worstC != null && worstC !== bestC ? `${this._money(worstC)}…${this._money(bestC)}` : this._money(bestC));
        return `<div class="mc-pos-lbl" style="color:var(--warn);margin-top:6px">⚔ diverged: ${closedArms.length} arm${closedArms.length === 1 ? '' : 's'} exited (${cTxt}) while ${openArms.length} still open${bestOpen != null ? ` (best ${bestOpen >= 0 ? '+' : ''}${bestOpen.toFixed(0)}%)` : ''}</div>`;
      }
      if (openPcts.length >= 2) {
        const spread = Math.max(...openPcts) - Math.min(...openPcts);
        if (spread >= 20) return `<div class="mc-pos-lbl" style="color:var(--warn);margin-top:6px">⚔ open arms spread ${spread.toFixed(0)}pp (${Math.min(...openPcts).toFixed(0)}% … +${Math.max(...openPcts).toFixed(0)}%)</div>`;
      }
      return `<div class="mc-pos-lbl" style="margin-top:6px">arms aligned</div>`;
    },
```

- [ ] **Step 2:** `node --check static/app.js` → OK.

---

### Task 2: Cache-bust + verification gate

**Files:** Modify `templates/index.html` (`app.js?v=167` → `?v=168`).

- [ ] **Step 1:** Bump `app.js?v=167` → `?v=168`; `grep -c 'app.js?v=168' templates/index.html` → 1.
- [ ] **Step 2:** `node --check static/app.js` → OK. Confirm old table gone: `grep -c 'th>uPnL</th' static/app.js` → 0.
- [ ] **Step 3: Funded-path guard.** `git diff --name-only origin/main...HEAD` lists ONLY `static/app.js`, `templates/index.html` (+ plan doc).
- [ ] **Step 4: Runner healthy.** Single runner instance on pinned release `f7cb9463`, `auth_ok=true`.
- [ ] **Step 5: Restart web (by PID, never `pkill -f`), authed-curl verify** served `app.js?v=168` contains `_paperLabCard`, `_armDivergence`, `arms aligned`; index links v168. Confirm `/positions/open` returns paper positions and `/positions/closed` returns rows (Lab has data to group).
- [ ] **Step 6: Visual (desktop + mobile).** Playwright Paper › Strategy Lab: one card per episode; arms listed (open + any exited); divergence line reads "diverged…" or "arms aligned"; sell/close buttons work; no clipped table on 390px.

---

## Self-Review

- **Spec coverage (§8):** one coin-episode card, arms side-by-side, outcome divergence, shared source wallet — delivered. Arm-agnostic (7 arms). Wallet × Arm matrix is PR5 (separate).
- **Placeholder scan:** `_loadOpen` + both helpers are complete verbatim code.
- **Control preservation:** open arms keep `sell`/`close`; ticker keeps `chart`; trader/arm/chain filters still applied to `open`. (Note: PR4 drops the 25% quick-sell button to declutter — 50% + close remain; the exit editor isn't used on paper.)
- **Consistency:** helpers use only existing functions (`chainOf`, `chainPill`, `esc`, `filterByChain`, `_col`, `_money`, `_ageRealIso`, `_syncTraderFilter`, `_syncChainFilter`, `_renderChart`) + PR2 `.mc-pos*` CSS. Episode key matches the arm-replication unit.
- **Risk:** medium (rewrites the paper renderer) but bounded — read-only, reuses endpoints, verified served + visually. Closed-set-recent-200 limitation stated (no completeness claim).
