/* Wallet Map: live wallet authority, supporting evidence, and the orbital Wallet universe.
   Navigation contract: docs/superpowers/specs/2026-10-03-wallet-universe-navigation.md.
   WalletMapCore exports pure helpers; actions retain the existing confirmation and authority checks. */
(function (root) {
  'use strict';

  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function short(id) {
    const s = String(id || '');
    return s.length > 14 ? s.slice(0, 4) + '…' + s.slice(-4) : s;
  }

  function who(id, handle) {
    return handle ? handle : short(id);
  }

  function money(v) {
    return v == null ? '' : '$' + Math.round(v).toLocaleString('en-US');
  }

  function ageFmt(s) {
    if (s == null) return '';
    return s < 120 ? Math.round(s) + ' s' : s < 7200 ? Math.round(s / 60) + ' min' : Math.round(s / 3600) + ' h';
  }

  // ---------------------------------------------------------------- the follow pipeline (read-only)
  // A percent with its sample, never a bare number: "+6.3% on 119", "none yet" when there are no closes.
  function pctText(ret, n) {
    if (!n || ret == null) return 'none yet';
    return (ret >= 0 ? '+' : '') + (Math.round(1000 * ret) / 10).toFixed(1) + '% on ' + n;
  }

  const STAGES = ['curated', 'tracked', 'paper', 'trial', 'armed'];

  function stageCounts(rows) {
    const out = {};
    STAGES.forEach(k => { out[k] = 0; });
    (rows || []).forEach(r => { out[r.stage] = (out[r.stage] || 0) + 1; });
    return out;
  }

  function funnelStampText(st) {
    if (st.authority_state === 'unavailable') return 'Live authority unavailable — money controls disabled. Measurements may be stale.';
    return 'Live roster authority · ' + String(st.live_at || 'time unavailable') + ' · independent of research results';
  }

  // Censored share of the notional (no exit price came back): beside every paper number (Gamma, 2026-10-02).
  function censText(share) {
    return share == null ? '' : ` · ${Math.round(100 * share)}% cens`;
  }

  function feasibleText(byChain) {
    return Object.entries(byChain || {}).map(([chain, n]) => `${chain} ${n}`).join(', ');
  }

  // The 14-day gated cell names only chains with closes ("56 0, 4663 0, solana 2" read as noise).
  function feasibleNonzero(byChain) {
    const nz = {};
    Object.entries(byChain || {}).forEach(([chain, n]) => { if (n) nz[chain] = n; });
    return feasibleText(nz) || '0';
  }

  // A return in one cell: coloured, with its sample and censored share beside it, and a sort value.
  function retCell(ret, n, cens) {
    if (!n || ret == null) return '<td data-v=""><span class="herd-dim">none yet</span></td>';
    const cls = ret > 0 ? 'herd-pos' : ret < 0 ? 'herd-neg' : '';
    const pct = pctText(ret, n);
    return `<td data-v="${esc(ret)}" title="${esc(pct)}"><span class="${cls}">${esc(pct.split(' on ')[0])}</span>` +
      ` <span class="herd-dim">on ${n}</span>${cens == null ? '' : `<span class="herd-cens">${esc(censText(cens))}</span>`}</td>`;
  }

  // ---------------------------------------------------------------- one view at a time
  const CHAIN_NAME = { solana: 'Solana', '56': 'BSC (56)', '4663': 'Robinhood (4663)', '8453': 'Base (8453)',
                       '5042': 'Arc (5042)' };
  const chainLabel = ch => CHAIN_NAME[ch] || ('chain ' + ch);
  // The universe payload names every chain the server knows (body.chains); the constant above missed
  // 1 and 57073, so a chain switch asked "Switch to chain 1?" (Gamma LOW, 2026-10-07).
  function learnChainLabels(chains) {
    (chains || []).forEach(c => { if (c && c.id && c.label) CHAIN_NAME[c.id] = c.id === 'solana' ? c.label : `${c.label} (${c.id})`; });
  }

  // The Live roster tab bar: every view that has rows, in pipeline order. The herder rows lead the
  // section as its relationship table (Wallet Map W2), so they are not a tab here.
  function views(funnel) {
    const out = [];
    if (funnel) {
      const c = stageCounts(funnel.funnel);
      [['armed', 'Armed', c.armed], ['trial', 'On trial', c.trial], ['paper', 'Watch only', c.paper],
       ['tracked', 'Roster', c.tracked + c.curated]].forEach(([key, label, n]) => out.push({ key, label, n, group: 'stage' }));

    }
    return out.filter(v => v.n > 0);
  }

  // A sortable header: data-sort marks it, and the label is a button, so Tab and Enter/Space sort.
  function sortTh(label, title) {
    return `<th scope="col" data-sort${title ? ` title="${esc(title)}"` : ''}><button type="button" class="herd-sort">${esc(label)}</button></th>`;
  }

  // Sort order with unknown values (null) last in both directions.
  function cmpVal(a, b, dir) {
    if (a === b) return 0;
    if (a == null) return 1;
    if (b == null) return -1;
    return (a < b ? -1 : 1) * dir;
  }

  // ---------------------------------------------------------------- the map's actions
  // Operator 2026-10-02: "arm it directly and see it in Armed". What each button sends, pure (node-
  // tested): the EXISTING roster, arm, pause and retire endpoints and the one trial route (the same
  // service the trial script runs). A money action (trial, arm full, keep armed) carries a confirm
  // naming the wallet, the chain and the clip; a trial is dry-run first so a refusal shows before
  // any confirm. `disabled` is the reason a button cannot run.
  const CLIP_CHAINS = ['solana', '4663', '56', '8453'];

  function actionsFor(kind, row, clips) {
    const enc = encodeURIComponent;
    const usd = v => '$' + Math.round(Number(v));
    const uid = row.uid, handle = row.handle || short(uid), chain = row.chain;
    const clip = (clips || {})[chain] || {};
    const wallets = '/api/memecoin/wallets', one = wallets + '/' + enc(uid);
    const add = { method: 'POST', url: wallets, body: { source_user_id: uid, handle, label: 'follow funnel' } };
    const arm = ch => ({ method: 'POST', url: one + '/real', body: { chain: ch, armed: true, reason: 'Wallet Map' } });
    const full = ch => {
      const cap = ((clips || {})[ch] || {}).full_max;
      return cap ? 'bankroll-sized, at most ' + usd(cap) : 'bankroll-sized, no ceiling set';
    };
    const noTrial = clip.trial ? null : 'no trial clip on ' + chainLabel(chain);
    const trialLabel = clip.trial ? 'Trial ' + usd(clip.trial) : 'Trial';
    const trialAsk = extra => `Start a ${clip.trial ? usd(clip.trial) : '?'} trial of ${handle} on ${chainLabel(chain)}?${extra}`;
    // Arm full meets the promote gate, which refuses a wallet with no funded closes (operator,
    // 2026-10-02: SQHK48QT and 55msLm12 refused at 0 closes, $0 copy PnL): Trial is the way in.
    const gateNote = ' The promote gate refuses a wallet with no funded closes; Trial is the way in. ' +
      'A gate refusal offers the operator override, behind a second confirm.';
    const override = { handle, chain, clip: full(chain) };
    const retire = { key: 'retire', label: 'Retire', money: false, disabled: null,
                     confirm: `Retire ${handle}? Paper and any arming stop; reobserve restores it.`,
                     steps: [{ method: 'DELETE', url: one, body: null }] };
    if (kind === 'cand') {
      const blocked = row.no_commands || null;       // no proven address: our detection cannot see it
      return [
        { key: 'trial', label: trialLabel, money: true, primary: true, disabled: blocked || noTrial,
          confirm: trialAsk(' It is added to the roster first.'),
          steps: [{ method: 'POST', url: one + '/trial', body: { action: 'start', chain, add: true, handle }, dry_run: true }] },
        { key: 'arm', label: 'Arm full', money: true, disabled: blocked,
          confirm: `Arm ${handle} on ${chainLabel(chain)} at the FULL funded clip (${full(chain)})? It is added to the roster first.${gateNote}`,
          steps: [add, arm(chain)], override },
        { key: 'watch', label: 'Watch on paper', money: false, disabled: blocked, confirm: null, steps: [add] }];
    }
    if (kind === 'paper') {
      return [
        { key: 'trial', label: trialLabel, money: true, primary: true, disabled: noTrial, confirm: trialAsk(''),
          steps: [{ method: 'POST', url: one + '/trial', body: { action: 'start', chain }, dry_run: true }] },
        { key: 'arm', label: 'Arm full', money: true, disabled: null,
          confirm: `Arm ${handle} on ${chainLabel(chain)} at the FULL funded clip (${full(chain)})?${gateNote}`, steps: [arm(chain)], override },
        retire];
    }
    if (kind === 'trial') {
      // A trial the grader stopped (Gamma L1): the confirm names the stop, and only an override of a
      // non-PROMOTE stop sends ack_stop, which the route requires for it (rule 3a0b141ee: the
      // verdict is a recommendation; the operator decides, knowingly).
      return (row.trial_chains || []).flatMap(ch => {
        const stop = (row.trial_stops || {})[ch];
        const over = !!stop && stop.verdict !== 'PROMOTE';
        const keep = { action: 'end', chain: ch, keep_armed: true, confirm: true };
        if (over) keep.ack_stop = stop.verdict;
        const said = stop ? `The grader STOPPED this trial: ${stopText(stop)}. ` + (over ? `Override ${stop.verdict}: ` : '') : '';
        return [
          { key: 'keep:' + ch, label: `Keep armed (${chainLabel(ch)})` + (over ? ' — override ' + stop.verdict : ''), money: true, disabled: null,
            confirm: said + `End ${handle}'s trial on ${chainLabel(ch)} and KEEP it armed at the FULL clip (${full(ch)})?`,
            steps: [{ method: 'POST', url: one + '/trial', body: keep }] },
          { key: 'end:' + ch, label: `End + disarm (${chainLabel(ch)})`, money: false, disabled: null, confirm: null,
            steps: [{ method: 'POST', url: one + '/trial', body: { action: 'end', chain: ch, disarm: true, confirm: true } }] }];
      });
    }
    if (kind === 'armed') {
      return (row.armed || []).map(ch => ({
        key: 'disarm:' + ch, label: 'Disarm ' + chainLabel(ch), money: false, disabled: null, confirm: null,
        steps: [{ method: 'POST', url: one + '/real', body: { chain: ch, armed: false, reason: 'Wallet Map' } }] }))
        .concat([{ key: 'pause', label: 'Pause', money: false, disabled: null,
                   confirm: `Pause ${handle}? It disarms every chain.`,
                   steps: [{ method: 'POST', url: one + '/pause', body: { paused: true } }] }]);
    }
    if (kind === 'rotation') return [retire];
    return [];
  }

  // What stopped a trial, for its tag and its Keep-armed confirm.
  function stopText(stop) {
    const at = String(stop.stopped_at || '').slice(0, 16).replace('T', ' ');
    return `${stop.verdict}, mean r ${stop.closes ? pctText(stop.mean_r, stop.closes) + ' closes' : 'not read'}` +
      (at ? `, stopped ${at}Z` : '');
  }

  // A paper row's chain picker changes what its buttons can do (Gamma L4: Trial kept the default
  // chain's disabled state). Relabel and re-enable each button from the picked chain's actions.
  function syncButtons(buttons, acts) {
    for (const b of buttons) {
      const a = acts.find(x => x.key === b.dataset.key);
      if (!a) continue;
      b.textContent = a.label;
      b.disabled = !!a.disabled;
      b.title = a.disabled || '';
      if (a.disabled) b.dataset.blocked = '1'; else delete b.dataset.blocked;
    }
  }

  const refusal = out => out.reason || out.detail || out.error || ('HTTP ' + out._status);

  function stepDone(step, out) {
    if (step.url.endsWith('/trial')) return 'trial ' + (out.reason || 'done') + (out.added ? ' (added to the roster)' : '');
    if (step.url.endsWith('/real')) return (step.body.armed ? 'armed on ' : 'disarmed on ') + chainLabel(step.body.chain);
    if (step.url.endsWith('/pause')) return 'paused';
    if (step.method === 'DELETE') return 'retired';
    return 'added to the roster';
  }

  // THE OPERATOR'S OVERRIDE OF THE PROMOTE GATE (operator yes, 2026-10-02). Only an arm the operator
  // already confirmed, refused by the promote gate itself (approve_refused: no funded closes), gets a
  // SECOND confirm naming the gate's reason, the wallet, the chain and the full clip; only a yes
  // there sends the same arm again with force=true as operator:dashboard. An identity or exclusion
  // refusal is never offered it, and force never bypasses those.
  function overrideOf(act, step, out) {
    if (!act.override || out.ok || out.action !== 'approve_refused' || !step.url.endsWith('/real')) return null;
    const o = act.override;
    return {
      confirm: `The promote gate refused: ${out.reason || 'no reason given'}.\n\nArm ${o.handle} on ${chainLabel(o.chain)} ` +
        `at the FULL funded clip (${o.clip}) anyway (operator override)?`,
      step: Object.assign({}, step, { body: Object.assign({}, step.body, { force: true, actor: 'operator:dashboard' }) }) };
  }

  // The steps in order; the endpoint's own refusal is the message, and a step that already wrote
  // is named before a later refusal (added to the roster, then the arm refused). null = cancelled.
  // `send(method, url, body)` and `ask(text) -> bool` are the page's (fetch, window.confirm).
  async function runSteps(act, send, ask) {
    if (act.confirm && !act.steps[0].dry_run && !ask(act.confirm)) return null;
    const done = [];
    for (const step of act.steps) {
      let out;
      if (step.dry_run) {
        const dry = await send(step.method, step.url, step.body);
        if (!dry.ok) return 'refused: ' + refusal(dry);
        if (!ask(act.confirm)) return null;
        out = await send(step.method, step.url, Object.assign({}, step.body, { confirm: true }));
      } else {
        out = await send(step.method, step.url, step.body);
      }
      const over = overrideOf(act, step, out);
      if (over && ask(over.confirm)) {
        out = await send(over.step.method, over.step.url, over.step.body);
        if (out.ok) { done.push(stepDone(over.step, out) + ' (operator override)'); continue; }
      }
      if (!out.ok) return (done.length ? done.join('; ') + '; then ' : '') + 'refused: ' + refusal(out);
      done.push(stepDone(step, out));
    }
    return 'done: ' + done.join('; ');
  }

  function armedUrl(chain) { return '/?tab=memecoin&mc_view=wallets&mc_chain=' + encodeURIComponent(chain); }
  function authorityAfterReadback(uid, chain, body) {
    if (body?.stamp?.authority_state !== 'ok') return 'unavailable';
    const row = (body.funnel || []).find(r => r.uid === uid);
    if (!row) return 'unavailable';
    return (row.effective_chains || []).includes(chain) ? 'armed' : 'watch';
  }
  function sharedCoinRows(edges, ids) {
    const out = [];
    for (let i = 0; i < ids.length; i++) for (let j = i + 1; j < ids.length; j++) {
      const coins = [...new Set(edges.filter(e =>
        (e.src_uid === ids[i] && e.dst_uid === ids[j]) ||
        (e.src_uid === ids[j] && e.dst_uid === ids[i])).flatMap(e => e.coins || []))];
      out.push({a: ids[i], b: ids[j], coins, n: coins.length});
    }
    return out;
  }

  function dossierUrl(chain, wallet) {
    return '/?tab=wallet-map&wm_chain='+encodeURIComponent(chain)+'&wm_wallet='+encodeURIComponent(wallet);
  }

  function dossierHTML(view) {
    const count = value => value == null ? 'unknown' : esc(value);
    const coverage = view.coverage || {}, funnel = view.funnel || {};
    const gain = value => value == null ? 'unknown' : Number(value).toFixed(2)+'%';
    return `<section class="wu-execution-research wu-dossier" aria-label="Wallet research dossier">
      <div><h4>30-day dossier</h4><a href="${dossierUrl(view.chain,view.wallet)}">Open wallet link</a></div>
      <p>No strategy supported</p>
      <span>${view.state === 'ok' ? count(Number(coverage.days_observed || 0).toFixed(1))+' days since first recorded collection · '+(coverage.stale?'collection stale or absent':'collection current') : 'Research ledger unavailable'}</span>
      <dl><div><dt>Observed buys · ${esc(view.window_days || 30)} days</dt><dd>${count(funnel.observed_buys)}</dd></div><div><dt>Structural receipts pass · of ${count(funnel.observed_buys)} observed</dt><dd>${count(funnel.valid_entries)}</dd></div><div><dt>Excluded / unknown · of ${count(funnel.observed_buys)} observed</dt><dd>${count(funnel.excluded_or_unknown)}</dd></div><div><dt>Distinct coins · among ${count(funnel.observed_buys)} observed</dt><dd>${count(funnel.distinct_coins)}</dd></div></dl>
      <ul><li>Observed buys: this wallet's buys our collection recorded in the window.</li><li>Structural receipts pass: buys whose receipt decodes to the swap we expected.</li><li>Excluded / unknown: buys that failed a check or could not be checked.</li><li>Distinct coins: different coins among the observed buys.</li></ul>
      <p>Modeled outcomes: ${count(funnel.modeled_outcomes)} · Pending / censored: ${count(funnel.pending_or_censored)}</p>
      <p>Producer provenance and the correctness of gate evaluations require independent review. These records are model inputs.</p>
      <p>${view.baseline?'Frozen paper defaults · '+esc(view.baseline.sealed_at):'Defaults not sealed. Existing paper history cannot qualify.'}</p>
      <details><summary>Paper rules and coverage</summary><ul>${(view.rules || []).map(rule=>'<li>'+esc(rule)+'</li>').join('')}</ul><p>Recorded buys are a partial census. A quiet period or a positive peak does not establish an exploitable strategy. Coin flow is not measured by this paper ledger.</p><pre>${esc(JSON.stringify({baseline:view.baseline || null,coverage:view.coverage || null,reasons:view.reasons || {},truncated:view.truncated},null,2))}</pre></details>
      <details><summary>Recent entry evidence${view.truncated?' · bounded page':''}</summary>${(view.entries || []).map(entry=>`<details><summary>${esc(short(entry.mint))} · ${esc(entry.state === 'valid' ? 'structural receipt passes · '+entry.measurement_state : 'excluded / unknown')}</summary><p>${esc(entry.observed_at)} · ${esc((entry.reasons || []).join(', '))}</p>${entry.fast_move?'<p>Exact exit quote floors after modeled entry, including costs. Peak is descriptive.</p><div class="wu-dossier-table"><table><thead><tr><th>Seconds</th><th>Peak</th><th>End</th><th>Worst</th></tr></thead><tbody>'+Object.entries(entry.fast_move).map(([h,v])=>'<tr><th>'+esc(h)+'</th><td>'+gain(v.peak_pct)+'</td><td>'+gain(v.endpoint_pct)+'</td><td>'+gain(v.worst_pct)+'</td></tr>').join('')+'</tbody></table></div>':''}<pre>${esc(JSON.stringify({origin:entry.origin_id,admission:entry.admission || null,paper:entry.paper || null},null,2))}</pre></details>`).join('') || '<p>No recorded entry evidence in this wallet and chain window.</p>'}</details>
      <details class="wu-notes"><summary>Research notes</summary><ol>${(view.notes || []).map(note=>`<li><small>${esc(note.at)}</small><p>${esc(note.text)}</p></li>`).join('') || '<li>No saved notes.</li>'}</ol><label>Research note<textarea maxlength="4000" rows="3" aria-label="Research note"></textarea></label><button type="button" data-save-note>Save note</button><p data-note-status role="status"></p></details>
    </section>`;
  }

  function researchCoverageHTML(body, chain) {
    const count = n => Number.isInteger(n) && n >= 0 ? esc(n.toLocaleString()) : 'unknown';
    const status = body?.state === 'ok' ? 'Collection current' : body?.state === 'stale' ? 'Collection stale · last recorded counts' : 'Collection unavailable';
    const cells = (Array.isArray(body?.cells) ? body.cells : []).filter(c => c && c.chain === chain);
    const sizing = (Array.isArray(body?.studies) ? body.studies : []).filter(s => s?.chain === chain
      && s.analysis_scope === 'fixed_nominal_size_diagnostic_not_funded_policy');
    const sizes = [...new Set(sizing.flatMap(s => (Array.isArray(s.policies) ? s.policies : [])
      .map(p => p?.clip_usd).filter(v => typeof v === 'number' && Number.isFinite(v) && v > 0)))].sort((a,b) => a-b);
    cells.sort((a,b) => String(a.handle || a.wallet).localeCompare(String(b.handle || b.wallet)));
    return `<h3>Research coverage · ${esc(chainLabel(chain))}</h3><p class="herd-stamp">${esc(status)} · ${esc(body?.generated_at || 'update time unknown')}</p>
      <p>No strategy supported. Archive counts are since enrollment, not a frozen validation cohort. Missing measurements stay unknown.</p>
      ${sizing.length ? `<p>Fixed size research: ${sizes.map(v => '$'+v.toFixed(2)).join(' vs ') || 'sizes unknown'} · ${sizing.length} frozen wallet round${sizing.length === 1 ? '' : 's'} · awaiting paired execution evidence. Live sizing unchanged.</p>` : ''}
      <div class="herd-scroll herd-pane-scroll"><table class="herd-table"><thead><tr><th scope="col">Wallet</th><th scope="col">Archived buys</th><th scope="col">Structural receipts</th><th scope="col">Usable paths</th><th scope="col">Distinct coins</th><th scope="col">Observed UTC days</th><th scope="col">Evidence</th></tr></thead><tbody>
      ${cells.map(c => `<tr><th scope="row" title="${esc(c.wallet)}">${esc(who(c.wallet,c.handle))}</th><td>${count(c.signals)}</td><td>unknown · see dossier</td><td>${count(c.complete_episodes)}</td><td>${count(c.distinct_coins)}</td><td>${count(c.observed_days)}</td><td><a class="herd-act" href="${dossierUrl(chain,c.wallet)}">Open 30-day dossier</a><details><summary>Blocked · missing evidence</summary><ul>${(Array.isArray(c.missing) ? c.missing : ['Evidence unavailable']).map(v=>`<li>${esc(v)}</li>`).join('')}</ul></details></td></tr>`).join('') || '<tr><td colspan="7">No coverage cell available; this does not establish zero buys.</td></tr>'}
      </tbody></table></div><p class="herd-stamp">UTC day counts exclude buys with unknown clocks. Structural receipt counts, distinct coins and days may be absent from this artifact. Inspect the dossier for checked admission counts, pending outcomes and collection duration. Usable paths alone do not establish strategy validity.</p>`;
  }

  function legacyDiagnosticsHTML(body) {
    if (body?.stamp?.error || !Array.isArray(body?.funnel)) return '<p>Forensic archive unavailable.</p>';
    const percent = value => typeof value === 'number' && Number.isFinite(value) ? esc((100*value).toFixed(1))+'%' : 'unknown';
    const rows=body.funnel.filter(r=>r && r.uid).slice().sort((a,b)=>String(a.handle || a.uid).localeCompare(String(b.handle || b.uid)));
    return `<p>Historical sampled diagnostics only. Mixed chains, policies and cohorts; no supported ranking, strategy or arming evidence.</p>
      <p class="herd-stamp">Recorded generator stamp: ${esc(body.stamp?.generated_at || 'unknown')} · file age: ${esc(ageFmt(body.stamp?.age_s) || 'unknown')}. Generator stamps may lack a timezone.</p>
      <p>Returns are priced-only. Paper weights per-chain ratios by priced-close count; the partial-filter return pools priced notional. Selected closes include unpriced outcomes; unpriced share is weighted by notional. Gated priced count not recorded. The old mixed-chain funded percentage is omitted; actual funded records remain in the wallet inspector.</p>
      <div class="herd-scroll herd-pane-scroll"><table class="herd-table"><thead><tr><th scope="col">Wallet</th><th scope="col">Paper priced closes</th><th scope="col">Paper priced-only return</th><th scope="col">Partial-filter selected closes (includes unpriced)</th><th scope="col">Partial-filter priced-only return</th><th scope="col">Unpriced notional share</th></tr></thead><tbody>
      ${rows.map(r=>`<tr><th scope="row">${esc(who(r.uid,r.handle))}</th><td>${esc(r.paper_n ?? 'unknown')}</td><td>${percent(r.paper_ret)}</td><td>${esc(r.gated_n ?? 'unknown')}</td><td>${percent(r.gated_ret)}</td><td>${typeof r.gated_cens_share==='number' ? esc(Math.round(100*r.gated_cens_share))+'%' : 'unknown'}</td></tr>`).join('')}
      </tbody></table></div>`;
  }

  const Core = { esc, short, who,
                 money, ageFmt, pctText, stageCounts, funnelStampText,
                 censText, feasibleText, feasibleNonzero, retCell, chainLabel, learnChainLabels, views, cmpVal, sortTh,
                 actionsFor, CLIP_CHAINS, stopText, syncButtons, runSteps, armedUrl, authorityAfterReadback, sharedCoinRows, dossierHTML, dossierUrl, researchCoverageHTML, legacyDiagnosticsHTML };
  if (typeof module !== 'undefined' && module.exports) module.exports = Core;
  root.WalletMapCore = Core;
  if (typeof document === 'undefined') return;

  // ---------------------------------------------------------------- rendering
  const th = sortTh;

  // The herders read feeds the Wallet Map's table, map and summary (W3 retired its own pane).
  function draw(body) {
    state.herders = body;
    state.loaded.herders = true;
    if (universeState.body) drawUniverse(universeState.body);
  }

  // ---------------------------------------------------------------- the view state (tabs, sort, finder)
  const VIEW_KEY = 'walletMap.view';
  // loaded: both payloads answered (data or error), so a remembered view that is gone can fall back.
  const state = { view: 'armed', sort: {}, funnel: null, herders: null, loaded: { funnel: false, herders: false } };
  try { state.view = localStorage.getItem(VIEW_KEY) || 'armed'; } catch (e) { /* private window: default view */ }

  const section = () => document.getElementById('tab-wallet-map');
  const activeScroller = () => {
    const pane = section() && section().querySelector(`[data-pane="${state.view}"]:not([hidden])`);
    return pane && (pane.matches('.herd-pane-scroll') ? pane : pane.querySelector('.herd-pane-scroll'));
  };

  // A redraw keeps the reader's place: the open folds, and the scroll of the view on screen.
  function keep(render) {
    const sec = section();
    const sc = activeScroller();
    const top = sc ? sc.scrollTop : 0;
    const open = sec ? [...sec.querySelectorAll('details[open][data-k]')].map(d => d.dataset.k) : [];
    render();
    if (sec) sec.querySelectorAll('details[data-k]').forEach(d => { if (open.includes(d.dataset.k)) d.open = true; });
    applyView();
    const after = activeScroller();
    if (after) after.scrollTop = top;
  }

  function applyView() {
    const sec = section();
    if (!sec) return;
    const avail = views(state.funnel);
    if (state.loaded.funnel && avail.length && !avail.some(v => v.key === state.view)) state.view = avail[0].key;
    const tabs = document.getElementById('wallet-map-tabs');
    if (tabs) {
      tabs.innerHTML = avail.map((v, i) => `<button type="button" class="herd-tab${i && v.group !== avail[i - 1].group ? ' herd-tab-sep' : ''}"
        data-view="${esc(v.key)}" aria-pressed="${v.key === state.view}">${esc(v.label)} <span class="mono">${v.n == null ? '…' : v.n}</span></button>`).join('');
    }
    sec.querySelectorAll('[data-pane]').forEach(p => { p.hidden = p.dataset.pane !== state.view; });
    applySort();
    applyFind();
  }

  function cellV(tr, col) {
    const v = tr.cells[col] && tr.cells[col].dataset.v;
    if (v == null || v === '') return null;
    return isNaN(Number(v)) ? v.toLowerCase() : Number(v);
  }

  function applySort() {
    const s = state.sort[state.view];
    const pane = section().querySelector(`[data-pane="${state.view}"]`);
    const table = pane && pane.querySelector('table.herd-table');
    if (!table) return;
    table.querySelectorAll('th[data-sort]').forEach(h => {
      h.setAttribute('aria-sort', s && h.cellIndex === s.col ? (s.dir > 0 ? 'ascending' : 'descending') : 'none');
    });
    if (!s) return;
    const body = table.tBodies[0];
    [...body.rows].sort((a, b) => cmpVal(cellV(a, s.col), cellV(b, s.col), s.dir)).forEach(r => body.appendChild(r));
  }

  function applyFind() {
    const input = document.getElementById('wallet-map-find');
    const q = (input ? input.value : '').trim().toLowerCase();
    // the row's own wallet only (its first cell and full id), never a name inside its folded lists
    section().querySelectorAll('[data-pane] table.herd-table:not(.herd-coins) tbody tr').forEach(tr => {
      tr.hidden = !!q && !(tr.cells[0].textContent + ' ' + (tr.dataset.q || '')).toLowerCase().includes(q);
    });
  }

  // ---------------------------------------------------------------- running an action
  let csrf = null;

  // JSON with the dashboard's CSRF token (the token rotates on a web restart: one refresh, one retry).
  async function send(method, url, body) {
    for (let attempt = 0; attempt < 2; attempt++) {
      if (!csrf) {
        try { csrf = (await (await fetch('/api/memecoin/csrf-token', { cache: 'no-store' })).json()).csrf_token; } catch (e) { csrf = null; }
      }
      const r = await fetch(url, { method, credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf || '' },
        body: body == null ? undefined : JSON.stringify(body) });
      let out = {};
      try { out = await r.json(); } catch (e) { out = {}; }
      if (r.status === 403 && out.error === 'csrf rejected' && attempt === 0) { csrf = null; continue; }
      out._status = r.status;
      if (out.ok === undefined) out.ok = r.ok;
      return out;
    }
    return { ok: false, error: 'csrf rejected' };
  }

  // The steps in order (runSteps, in the pure part above), with the page's own send and confirm.
  const execute = act => runSteps(act, send, msg => window.confirm(msg));

  const actionRows = new Map();          // a cell's id -> {kind, row}; rebuilt on every funnel draw
  let actionSeq = 0;

  async function runAction(btn) {
    const reg = actionRows.get(btn.dataset.act);
    if (!reg || btn.disabled) return;
    const cell = btn.closest('td');
    const pick = cell.querySelector('.herd-chain');
    const row = pick ? Object.assign({}, reg.row, { chain: pick.value }) : reg.row;
    const act = actionsFor(reg.kind, row, (state.funnel || {}).clips).find(a => a.key === btn.dataset.key);
    const status = document.getElementById('wallet-map-action-status');
    const label = (row.handle || short(row.uid)) + ': ' + (act ? act.label : btn.textContent);
    const say = (text, bad) => { if (status) { status.textContent = text; status.classList.toggle('herd-bad', !!bad); } };
    if (state.funnel?.stamp?.authority_state !== 'ok') { say('Live authority unavailable — action disabled', true); return; }
    if (!act || act.disabled) { say(label + ' — ' + ((act && act.disabled) || 'not available'), true); return; }
    const buttons = [...cell.querySelectorAll('button')];
    buttons.forEach(b => { b.disabled = true; });            // one request at a time per row: no double click
    say(label + '…');
    let msg;
    try {
      msg = await execute(act);
    } catch (err) {
      await loadFunnel();
      msg = 'response unavailable; readback: ' + authorityAfterReadback(row.uid, row.chain, state.funnel || {});
    }
    if (msg === null) {
      buttons.forEach(b => { b.disabled = !!b.dataset.blocked; });
      say(label + ' — cancelled');
      return;
    }
    say(label + ' — ' + msg, /refused|failed/.test(msg));
    if (status && row.chain) { const link = document.createElement('a'); link.href = armedUrl(row.chain); link.textContent = ' View Armed on ' + chainLabel(row.chain); status.append(link); }
    await loadFunnel();
    await loadUniverse();                                    // the route overlays live state: the row moves tab
  }

  function defaultChain(row) {
    return (row.armed || [])[0] || (row.address_chains || []).find(ch=>CLIP_CHAINS.includes(ch)) || 'solana';
  }

  function actionCell(kind, row, extra, fixedChain = false) {
    const id = String(++actionSeq);
    actionRows.set(id, { kind, row });
    const acts = actionsFor(kind, row, (state.funnel || {}).clips);
    const chains = kind === 'paper' && !fixedChain ? `<select class="herd-chain" aria-label="Chain for ${esc(row.handle || row.uid)}">` +
      CLIP_CHAINS.map(ch => `<option value="${esc(ch)}"${ch === row.chain ? ' selected' : ''}>${esc(chainLabel(ch))}</option>`).join('') +
      '</select>' : '';
    const btns = acts.map(a => `<button type="button" class="herd-act${a.money ? ' herd-act-money' : ''}${a.primary ? ' herd-act-primary' : ''}" data-act="${id}" data-key="${esc(a.key)}"` +
      (a.disabled ? ` disabled data-blocked="1" title="${esc(a.disabled)}"` : '') + `>${esc(a.label)}</button>`).join('');
    const reasons = [...new Set(acts.map(a => a.disabled).filter(Boolean))];
    const why = acts.length && reasons.length === 1 && acts.every(a => a.disabled) ? `<span class="herd-dim herd-act-why">${esc(reasons[0])}</span>` : '';
    return `<td class="herd-actcell">${chains}${btns}${why}${extra || ''}</td>`;
  }


  function bind() {
    const sec = section();
    if (!sec || sec.dataset.bound) return;
    sec.dataset.bound = '1';
    sec.addEventListener('click', ev => {
      const tab = ev.target.closest('.herd-tab');
      if (tab) {
        state.view = tab.dataset.view;
        try { localStorage.setItem(VIEW_KEY, state.view); } catch (e) { /* not kept: fine */ }
        applyView();
        return;
      }
      const head = ev.target.closest('th[data-sort]');
      if (head) {
        const prev = state.sort[state.view];
        const table = head.closest('table');
        const first = table ? [...table.tBodies[0].rows].map(r => cellV(r, head.cellIndex)).find(v => v != null) : null;
        const dir = prev && prev.col === head.cellIndex ? -prev.dir : (typeof first === 'number' ? -1 : 1);
        state.sort[state.view] = { col: head.cellIndex, dir };
        applySort();
        return;
      }
      const act = ev.target.closest('.herd-act');
      if (act) { runAction(act); return; }
    });
    sec.addEventListener('change', ev => {
      const pick = ev.target.closest('.herd-chain');
      const cell = pick && pick.closest('td');
      const first = cell && cell.querySelector('.herd-act');
      const reg = first && actionRows.get(first.dataset.act);
      if (reg) syncButtons([...cell.querySelectorAll('.herd-act')],
                           actionsFor(reg.kind, Object.assign({}, reg.row, { chain: pick.value }), (state.funnel || {}).clips));
    });
    const find = document.getElementById('wallet-map-find');
    if (find) find.addEventListener('input', applyFind);
    const archive = document.getElementById('wallet-legacy-diagnostics');
    if (archive) archive.addEventListener('toggle', async () => {
      const host = document.getElementById('wallet-legacy-body');
      if (!archive.open || !host || host.textContent) return;
      host.textContent = 'Loading forensic archive…';
      try {
        const response = await fetch('/api/memecoin/wallet-map/funnel?diagnostic_only=1');
        if (!response.ok) throw new Error('HTTP '+response.status);
        host.innerHTML = legacyDiagnosticsHTML(await response.json());
      } catch (err) { host.textContent = 'Forensic archive unavailable: '+err.message; }
    });
  }

  async function load(signal) {
    bind();
    applyView();
    try {
      const r = await fetch('/api/memecoin/wallet-map/herders?chain=solana', { signal });
      const body = await r.json();
      if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status));
      draw(body);
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      state.herders = {stamp: {error: 'request_failed'}, herders: [], shared: [], probes: []};
      if(universeState.body)drawUniverse(universeState.body);
      state.loaded.herders = true;
      applyView();
    }
  }

  // ---------------------------------------------------------------- the follow pipeline
  function pane(key, head, rows, compact) {
    return `<section data-pane="${esc(key)}" hidden><div class="herd-scroll herd-pane-scroll"><table class="herd-table${compact ? ' herd-compact' : ''}">
      <thead><tr>${head}</tr></thead><tbody>${rows.join('')}</tbody></table></div></section>`;
  }

  function drawFunnel(body) {
    const stampEl = document.getElementById('follow-funnel-stamp');
    const el = document.getElementById('follow-funnel-body');
    if (!el) return;
    const st = body.stamp || {};
    if (stampEl) stampEl.textContent = funnelStampText(st);
    state.loaded.funnel = true;
    if (st.authority_state !== 'ok') { el.innerHTML = ''; state.funnel = null; applyView(); return; }
    state.funnel = body;
    actionRows.clear();
    // Universe controls are rebuilt below after the funnel registry refresh.
    const byStage = (...ks) => (body.funnel || []).filter(r => ks.includes(r.stage));
    const stageHead = th('Wallet') + th('Configured chains') + th('Effective chains') + '<th scope="col">Operator action</th>';
    const kindOf = r => (r.stage === 'armed' || r.stage === 'trial' ? r.stage : 'paper');
    const stageRow = r => `<tr data-q="${esc(r.uid)}"><th scope="row" data-v="${esc(who(r.uid, r.handle))}" title="${esc(r.uid)}">${esc(who(r.uid, r.handle))}${r.stage === 'curated' ? ' <span class="herd-tag">curated</span>' : ''}${r.paused ? ' <span class="herd-tag herd-tag-warn">paused</span>' : ''}${r.from_candidate ? ' <span class="herd-tag">new</span>' : ''}${Object.entries(r.trial_stops || {}).map(([ch, st]) => ` <span class="herd-tag${st.verdict === 'PROMOTE' ? '' : ' herd-tag-warn'}" title="${esc(stopText(st))}">${esc(chainLabel(ch))} stopped: ${esc(st.verdict)}</span>`).join('')}</th>
        <td>${esc((r.armed || []).map(chainLabel).join(', ')) || 'watch only'}</td><td>${esc((r.effective_chains || []).map(chainLabel).join(', ')) || (r.paused ? 'paused' : 'watch only')}</td>
        ${actionCell(kindOf(r), kindOf(r) === 'paper' ? Object.assign({}, r, { chain: defaultChain(r) }) : r)}</tr>`;
    keep(() => {
      el.innerHTML = `<p class="herd-stamp" data-for="pipeline">The buttons write the roster, arming and trials directly (database authority only; the runner picks a change up at its next roster refresh, about 10 s). Money actions ask first; a trial refuses without our own on-chain buy in 7 days.</p>
      ${pane('armed', stageHead, byStage('armed').map(stageRow), true)}
      ${pane('trial', stageHead, byStage('trial').map(stageRow), true)}
      ${pane('paper', stageHead, byStage('paper').map(stageRow), true)}
      ${pane('tracked', stageHead, byStage('tracked', 'curated').map(stageRow), true)}
`;
    });
  }

  async function loadFunnel(signal) {
    const stampEl = document.getElementById('follow-funnel-stamp');
    try {
      const r = await fetch('/api/memecoin/wallet-map/funnel', { signal });
      const body = await r.json();
      if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status));
      drawFunnel(body);
      if(universeState.body) drawUniverse(universeState.body);
      return body;
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      state.funnel = null;
      if (stampEl) stampEl.textContent = 'Follow pipeline unavailable: ' + err.message;
      state.loaded.funnel = true;
      applyView();
    }
  }

  // W4b: the URL is read through WalletUniverse.readUrlState, so a bad chain, wallet or member reads as the default.
  const universeUrl = root.WalletUniverse?.readUrlState(root.location.search) || {compare: []};
  let savedUniverseChain='solana';
  try { savedUniverseChain=localStorage.getItem('walletMap.chain') || 'solana'; } catch (_) {}
  if (root.WalletUniverse && !root.WalletUniverse.chainId(savedUniverseChain)) savedUniverseChain='solana';
  const universeState = {chain: universeUrl.chain || savedUniverseChain, body: null,
    uid: universeUrl.wallet || '', request: 0, compare: new Set(universeUrl.compare)};
  let compareUnchecked = true;           // a restored tray keeps only wallets the next payload has
  // The roster check for a restored tray, against `body` (S4, Codex review 2026-10-07): run by
  // drawUniverse AND by a same-chain Back/Forward, which redraws without drawUniverse. A new chain's
  // ids wait for that chain's payload; until then they are unchecked, never a zero relationship.
  function validateCompare(body) {
    if (!compareUnchecked || !body) return;
    const known = new Set((body.wallets || []).map(w => w.uid));
    universeState.compare = new Set([...universeState.compare].filter(id => known.has(id)));
    compareUnchecked = false;
  }
  const dossierDrafts = new Map();
  let dossierFetch = null;
  let universeView = null;
  function drawUniverse(body) {
    const host=document.getElementById('wallet-universe');
    if (!host) return;
    bind();
    universeState.body=body; universeState.chain=body.chain;
    learnChainLabels(body.chains);
    validateCompare(body);
    const coverage=document.getElementById('wallet-research-coverage');
    if(coverage) coverage.innerHTML=researchCoverageHTML(body.execution_research,body.chain);
    if(!universeView)universeView=root.WalletUniverse.mount(host, {
      initialWallet: universeState.uid,
      beforeChain(chain) {
        const n = universeState.compare.size;
        return !n || root.confirm(`Switch to ${chainLabel(chain)}? The comparison is chain-scoped: its ${n} wallet${n === 1 ? '' : 's'} will be cleared.`);
      },
      onChain(chain) {
        universeState.chain=chain; universeState.uid=''; universeState.compare.clear();
        try {localStorage.setItem('walletMap.chain',chain);} catch(_) {}
        loadUniverse();
      },
      onRefresh() { load(); loadUniverse(); },      // W4a: same endpoints; the view keeps selection and folds
      compare: () => [...universeState.compare],     // W4b: the universe view writes the URL
      onHistory(s) {                                 // W4b: Back/Forward restore, never a new entry
        universeState.compare = new Set(s.compare); compareUnchecked = true;
        if (s.wallet) universeState.uid = s.wallet;
        if (s.chain && s.chain !== universeState.chain) { universeState.chain = s.chain; loadUniverse(); return; }
        validateCompare(universeState.body);         // same chain: the payload in hand, before the redraw
      },
      onInspect(uid,inspector) {
        universeState.uid=uid;
        const evidence=document.createElement('div');
        evidence.className='wu-wallet-evidence';
        drawUniverseEvidence(universeState.body,evidence);
        inspector.append(evidence);
        loadDossier(evidence.querySelector('[data-dossier]'),uid,universeState.chain);
        loadWalletView(inspector,uid,universeState.chain);
      },
      status(body, stamp) {
        const authority='Authority: '+(body?.stamp?.authority_state || 'unavailable');
        const graph=stamp.error ? 'Observations unavailable: '+stamp.error : stamp.computed_at ?
          'Observations '+String(stamp.computed_at).slice(0,16).replace('T',' ')+'Z'+(stamp.stale?' · stale':'') : 'Observations loading';
        return authority+' · '+(body?.chain==='solana'?graph:'Relationships not measured on this chain');
      }
    });
    universeView.update(body,state.herders);
  }

  // W5b: the comparison tray sits where Add was: its members, the 6-wallet cap, and the coins each
  // compared pair shares under a measured link. A chain change asks before it clears the tray.
  const COMPARE_CAP = 6;
  function trayHTML(pairs, name) {
    const ids = [...universeState.compare], has = universeState.compare.has(universeState.uid);
    const full = !has && ids.length >= COMPARE_CAP;
    return `<div class="wu-tray" role="group" aria-label="Comparison"><h4>Comparison · ${ids.length} of ${COMPARE_CAP} wallets</h4>
      ${ids.length ? `<ul>${ids.map(id => `<li><span title="${esc(id)}">${esc(name(id))}</span> <button type="button" class="wu-tray-remove" data-tray-remove="${esc(id)}" aria-label="Remove ${esc(name(id))} from the comparison">Remove</button></li>`).join('')}</ul>` : '<p class="note">Empty. Add wallets to compare the coins they share.</p>'}
      <button id="wu-compare" type="button" class="inspect-action"${full ? ` disabled title="The comparison holds ${COMPARE_CAP} wallets; remove one first"` : ''}>${has ? 'Remove this wallet from' : 'Add this wallet to'} the comparison</button>
      ${full ? `<p class="note">Full: ${COMPARE_CAP} of ${COMPARE_CAP}. Remove a wallet to add this one.</p>` : ''}
      ${ids.length > 1 ? '<h4>Shared coins</h4>' + pairs.map(p => `<details><summary>${esc(name(p.a))} ↔ ${esc(name(p.b))}: ${p.n} distinct supporting coins</summary><p>${p.coins.map(esc).join(', ') || 'No measured shared coin; coverage may be incomplete.'}</p></details>`).join('') : ''}</div>`;
  }

  function drawUniverseEvidence(body, host) {
    if (!host) return;
    bind();
    universeState.body = body;
    universeState.chain = body.chain;
    const wallets = body.wallets || [];
    if (!wallets.some(w => w.uid === universeState.uid)) universeState.uid = wallets[0]?.uid || '';
    const name = uid => wallets.find(w => w.uid === uid)?.handle || short(uid);
    const w = wallets.find(w => w.uid === universeState.uid);
    const badge = (label, evidence) => `<span class="wu-badge">${esc(label)}: ${esc(evidence?.state || 'unknown')}</span>`;
    const metric = (label, value) => `<div><small>${esc(label)}</small><strong>${value?.survivor_return == null ? 'unknown' : esc(pctText(value.survivor_return, value.priced))}</strong><small>${value ? esc(value.censored) + ' unpriced · survivor diagnostic' : 'no measured closes'}</small></div>`;
    const links = (body.edges || []).filter(e => e.src_uid === universeState.uid || e.dst_uid === universeState.uid).map(e => ({...e, coverage: e.coverage ?? body.coverage_by_ref?.[e.coverage_ref]}));  // D1 dedupe compat until W5
    const pairs = sharedCoinRows(body.edges || [], [...universeState.compare]);
    if (!w) return;
    const research=body.execution_research || {state:'unavailable',cells:[]};
    const cell=(research.cells || []).find(c=>c.wallet===w.uid && c.chain===body.chain);
    const researchText=research.state==='stale' ? 'Collection update is late' : research.state!=='ok' ? 'Collection unavailable' : 'Collecting source buys';
    const missing=cell?.missing || ['Verified source swap and address','Complete ordered one-second route state','Exact entry and whole-bag exits','Measured delay and full costs','Future comparison'];
    const researchCard=`<section class="wu-execution-research" aria-label="Execution research"><div><h4>Legacy collection coverage</h4><span>${esc(researchText)}</span></div><p>No strategy supported yet</p><dl><div><dt>Archived buys · all time</dt><dd>${cell ? Number(cell.signals || 0).toLocaleString() : '—'}</dd></div><div><dt>Usable paths</dt><dd>${cell ? Number(cell.complete_episodes || 0).toLocaleString() : '—'}</dd></div></dl><details><summary>What is missing?</summary><ul>${missing.map(v=>`<li>${esc(v)}</li>`).join('')}</ul><p>One-second paths, achievable fills and costs are required. Historical chart marks stay diagnostic only.</p>${cell?.nomination==='watch_only_not_nominated'?'<p>Watch only. This wallet has no nominated strategy study.</p>':''}</details></section>`;
    host.innerHTML = `<section class="wu-sec" id="wu-next" aria-label="Next step"><h3>Next step</h3><div class="wu-next-actions"><a class="inspect-action" href="#wu-coins">Open supporting coins</a></div>${trayHTML(pairs, name)}<div data-gaps><p class="note">Loading missing data…</p></div><button type="button" class="inspect-action" id="wu-save-question">Save as research question</button><p class="note" id="wu-save-status" role="status"></p></section>
      <section class="wu-sec" aria-label="Execution research"><h3>Execution research</h3><div data-dossier>${researchCard}</div></section>
      <section class="wu-sec wu-authority" aria-label="Live authority · changes real money"><h3>Live authority · changes real money</h3><table class="wu-actions"><tbody><tr>${actionCell(w.armed ? 'armed' : w.from_roster ? 'paper' : 'cand', {uid:w.uid,handle:w.handle,chain:body.chain,armed:w.armed?[body.chain]:[],no_commands:w.coverage?.address?.state==='ok'?null:'proven address unavailable'}, '', true)}</tr></tbody></table>
      <a class="wu-armed-link" href="${armedUrl(body.chain)}">View Armed on ${esc(chainLabel(body.chain))}</a>
      <details class="wu-proof"><summary>Evidence and settings</summary><div><div class="wu-badges"><span class="wu-badge">configured: ${w.armed?'armed':'watch'}</span><span class="wu-badge">effective: ${w.effective?'armed':w.paused?'paused':'watch'}</span>${badge('address',w.coverage?.address)}${badge('subscription',w.coverage?.subscription)}</div>
      <div class="wu-metrics">${metric('Paper',w.paper)}${metric('Funded',w.funded)}<div><small>Strict policy</small><strong>${esc(w.strict_policy?.arm || 'band fallback / insufficient evidence')}</strong><small>${esc(w.strict_policy?.blocked_reason || 'Frozen funded contracts remain authoritative')}</small></div></div>
      <details><summary>Last on-chain buy and measured position origins</summary><pre>${esc(JSON.stringify({last_buy:w.last_buy || null,positions:w.supporting_positions || {},latest_funded_contract:w.latest_funded_contract || null},null,2))}</pre></details>
      <details><summary>Isolated research coverage</summary><pre>${esc(JSON.stringify(w.research || {state:'not_collected'},null,2))}</pre></details>
      <h4>Observed neighborhood · ${links.length} links</h4>${links.length ? links.map(e => `<details><summary>${esc(name(e.src_uid))} ${e.direction==='association'?'↔':'→'} ${esc(name(e.dst_uid))} · ${esc(e.kind)} · ${(e.coins||[]).length} supporting coins</summary><p>Timing does not establish causation. Coverage: ${esc(typeof e.coverage==='string'?e.coverage:JSON.stringify(e.coverage || 'unknown'))}</p><pre>${esc(JSON.stringify(e.provenance,null,2))}</pre><ul>${(e.coins||[]).map(c=>`<li class="mono">${esc(c)}</li>`).join('') || '<li>Coin provenance not stored</li>'}</ul></details>`).join('') : `<p>Relationships ${esc(body.graph_state === 'ok' ? 'not observed in the measured window' : body.graph_state)}.</p>`}
      <div id="wu-setup-evidence"></div></div></details></section>
      <section class="wu-sec wu-more" aria-label="Notes and raw records"><h3>Notes and raw records</h3><div data-notes></div></section>`;
    const evidenceHost = host.querySelector('#wu-setup-evidence');
    if (evidenceHost && w) {
      evidenceHost.innerHTML = `<h4>Coin setups and strict policy comparison</h4>
        <p>Partial feasibility covers mcap/source-buy only. Volume, turnover and route gates are not implied. Frozen funded contracts remain authoritative.</p>
        <details><summary>Current chain override and selection exemptions</summary><pre>${esc(JSON.stringify({override:w.current_override || null,exemptions:w.gate_exemptions || []},null,2))}</pre></details>
        ${(w.setups || []).map(v => `<details><summary>${esc(v.setup)} · ${v.complete_origins} complete / ${v.origins} origins · ${v.distinct_coins} coins</summary><p>${esc(v.price_kind || v.state)} · ${esc(v.cohort_state)}. Training evidence; no funding verdict.</p>
        <pre>${esc(JSON.stringify({policies:v.policies,full_book:v.full_book,policy_ids:v.policy_ids,latency:v.latency_state,route:v.route_state,open:v.open_origins,censored:v.censored_origins,missing_arm:v.missing_arm_origins,late_setup:v.late_setup_origins},null,2))}</pre><details><summary>Supporting origins</summary><pre>${esc(JSON.stringify(v.evidence,null,2))}</pre></details></details>`).join('') || '<p>Setup-matched evidence not collected. Missing costs/entry setup stay unknown.</p>'}
        <h4>Actual blockers</h4>${(w.blockers || []).map(v=>`<details><summary>${esc((v.actual_gate||[]).join(', '))} · ${esc(v.origin)}</summary><p>Inputs: ${esc(v.inputs_state)} · Capture: ${esc(v.capture_state)} · Outcome: ${esc(v.outcome_kind)}</p><pre>${esc(JSON.stringify({attempts:v.attempts,captures:v.captures},null,2))}</pre></details>`).join('') || '<p>No classified blocker in the bounded stored window; not proof every signal was fillable.</p>'}
        <details><summary>Freeze one paper adjustment for review</summary><p>This exports a hypothesis. It changes no funded settings, wallets or positions.</p>
        <label>Setup <select id="wu-hypothesis-setup">${(w.setups||[]).map((s,i)=>`<option value="${i}">${esc(s.setup)}</option>`).join('')}</select></label>
        <label>Selection family <select id="wu-hypothesis">${(body.selection_gates||[]).map(g=>`<option>${esc(g)}</option>`).join('')}</select></label>
        <label>Test value <input id="wu-hypothesis-value" value="false" aria-label="Boolean or numeric test value"></label><button id="wu-export" type="button">Download paper hypothesis</button><p id="wu-export-status" role="status"></p></details>`;
      evidenceHost.querySelector('#wu-export').onclick=()=>{
        const status=evidenceHost.querySelector('#wu-export-status');
        const field=evidenceHost.querySelector('#wu-hypothesis').value;
        let value; try { value=JSON.parse(evidenceHost.querySelector('#wu-hypothesis-value').value); } catch(e) { status.textContent='Use a boolean or finite number.';return; }
        if (!field || !(typeof value==='boolean' || (typeof value==='number' && Number.isFinite(value)))) {status.textContent='Choose one documented selection family and a boolean or finite number.';return;}
        const setup=(w.setups||[])[Number(evidenceHost.querySelector('#wu-hypothesis-setup').value)];
        const arms=Object.keys(setup?.policies || {});
        if (!setup || !arms.length || !setup.exportable) {status.textContent='Collect setup and policy evidence before exporting.';return;}
        const manifest={version:1,chain:body.chain,uid:w.uid,cutoff:new Date().toISOString(),setup:setup.setup,arms,frozen_policies:setup.frozen_policies || [],change:{category:'selection',fields:{[field]:value}},state:'unvalidated',authority:'none'};
        const link=document.createElement('a');const url=URL.createObjectURL(new Blob([JSON.stringify(manifest,null,2)],{type:'application/json'}));
        link.href=url;link.download='wallet-paper-hypothesis.json';link.click();URL.revokeObjectURL(url);status.textContent='Exported unvalidated hypothesis. No authority or policy changed.';
      };
    }
    host.querySelector('#wu-save-question').onclick = () => saveQuestion(host.closest('.inspector') || host);
    host.querySelector('#wu-compare').onclick = () => { if (universeState.compare.has(universeState.uid)) universeState.compare.delete(universeState.uid); else if(universeState.compare.size<COMPARE_CAP) universeState.compare.add(universeState.uid); drawUniverse(body); };
    host.querySelectorAll('[data-tray-remove]').forEach(b => b.onclick = () => { universeState.compare.delete(b.dataset.trayRemove); drawUniverse(body); });
  }
  async function loadDossier(host, wallet, chain) {
    if (!host) return;
    dossierFetch?.abort();
    const controller = new AbortController(); dossierFetch = controller;
    const key=chain+':'+wallet;
    const url='/api/memecoin/wallet-map/dossier?chain='+encodeURIComponent(chain)+'&wallet='+encodeURIComponent(wallet);
    try {
      const response=await fetch(url,{signal:controller.signal,cache:'no-store'});
      if(!response.ok) throw new Error('HTTP '+response.status);
      const view=await response.json();
      if (!host.isConnected || wallet!==universeState.uid || chain!==universeState.chain) return;
      if(view.wallet!==wallet || view.chain!==chain) throw new Error('Wallet identity mismatch');
      host.innerHTML=dossierHTML(view);
      universeState.dossier={key,view};
      renderGaps(host.closest('.inspector'));
      const notes=host.querySelector('.wu-notes'), notesHost=host.closest('.wu-wallet-evidence')?.querySelector('[data-notes]');
      if(notesHost) notesHost.replaceChildren(notes);
      const input=notes.querySelector('textarea'), button=notes.querySelector('[data-save-note]'), status=notes.querySelector('[data-note-status]');
      input.value=dossierDrafts.get(key) || '';
      input.oninput=()=>dossierDrafts.set(key,input.value);
      button.onclick=async()=>{
        if(!input.value.trim()) {status.textContent='Write a research note first.';return;}
        const submitted=input.value;
        button.disabled=true;status.textContent='Saving…';
        try {
          const result=await send('POST',url,{text:submitted});
          if(!result.ok) throw new Error(result.error || 'Save failed');
          if(dossierDrafts.get(key)===submitted) dossierDrafts.delete(key);
          if(notes.isConnected) {status.textContent='Saved.';loadDossier(host,wallet,chain);}
        } catch(err) {status.textContent=err.message;button.disabled=false;}
      };
    } catch(err) {
      if(err.name!=='AbortError' && host.isConnected) host.insertAdjacentHTML('beforeend','<p role="status">30-day dossier unavailable: '+esc(err.message)+'</p>');
    }
  }


  // ---------------------------------------------------------------- W3 supporting coins, gaps, a question
  // The wallet view (D2 coins, D3 funded results, D4 gaps) fills the summary's supporting evidence
  // and next step. Unknown stays unknown: no coin, gap or funded figure is ever drawn as a zero.
  let walletFetch = null;
  const pct1 = v => v == null || !Number.isFinite(Number(v)) ? 'unknown' : (v >= 0 ? '+' : '−') + Math.abs(v).toFixed(1) + '%';
  const usdSigned = v => v == null ? 'unknown' : (v < 0 ? '−$' : '$') + Math.abs(Math.round(v)).toLocaleString('en-US');
  const utc = t => t ? String(t).slice(0, 16).replace('T', ' ') + 'Z' : 'unknown';

  function coinFundedText(f) {
    if (!f) return 'unknown';
    if (f.state === 'unavailable') return 'unavailable';
    const others = (f.other_positions_same_coin || []).length;
    const extra = others ? ` · +${others} other row${others === 1 ? '' : 's'} on this coin, not counted` : '';
    if (f.state === 'entered') return (f.positions || []).map(p => p.is_open ? 'open' :
      pct1(p.return_pct) + (p.exit_reason ? ' · ' + p.exit_reason : '')).join('; ') + extra;
    if (f.state === 'not_entered') return 'not entered · ' + (f.reason || 'no attempt recorded') + extra;
    return 'unknown';
  }

  function coinRows(coins) {
    return coins.map(c => {
      const after = c.after == null ? 'order unknown'
        : c.followers ? `${c.followers} buyer${c.followers === 1 ? '' : 's'} · ${(c.regulars || []).length} repeat` : 'none';
      return `<tr><th scope="row" title="${esc(c.mint)}">${esc(short(c.mint))}</th><td title="${esc((c.source || {}).signature || '')}">${esc(utc(c.at))}</td><td>${esc(after)}</td><td>${esc(coinFundedText(c.funded))}</td></tr>`;
    }).join('');
  }

  function supportingCoinsHTML(view) {
    if (view.graph_state === 'not_measured') return '<p class="note">Supporting coins are not measured on this chain; the link graph covers Solana only.</p>';
    if ((view.stamp || {}).error) return `<p class="note">Supporting coins unavailable: ${esc(view.stamp.error)}.</p>`;
    const f = view.funded || {};
    const record = f.state === 'ok'
      ? `${pct1(f.roi_pct)} all time over ${f.closed_count} closes · 7d ${pct1(((f.windows || {})['7d'] || {}).roi_pct)} over ${((f.windows || {})['7d'] || {}).closed ?? 'unknown'} closes · ${f.open_count} open · ${usdSigned(f.realized_pnl)} realized`
      : f.state === 'none' ? 'no funded rows for this wallet on this chain' : f.state === 'unavailable' ? 'unavailable: the ledger read failed' : 'unknown';
    const coins = view.coins || [];
    const head = '<thead><tr><th scope="col">Coin</th><th scope="col">Source first buy</th><th scope="col">After it in its slot</th><th scope="col">Our funded entry</th></tr></thead>';
    const regs = ((view.herd || {}).regulars || []).filter(r => r.other_herds > 0);
    const lane = (rows, mine) => rows.length ? '<ul>' + rows.map(x => `<li>${esc(who(x.id, x.handle))} · ${x.n_coins} coins · sells ~${Math.round(x.lag_p50_s || 0)} s after ${mine ? 'their' : 'its'} buy</li>`).join('') + '</ul>' : '<p class="note">none seen</p>';
    const pt = view.patterns || {};
    return `<h4 id="wu-coins">Supporting coins · ${coins.length} of ${esc(view.coins_total ?? coins.length)}${view.coins_omitted ? ` · ${esc(view.coins_omitted)} older not listed` : ''}</h4>
      <p class="note">Our funded record: ${esc(record)}.${view.funded_scope ? ' Scope: ' + esc(view.funded_scope) + '.' : ''}</p>
      ${coins.length ? `<div class="wu-dossier-table"><table class="wu-coins">${head}<tbody>${coinRows(coins.slice(0, 10))}</tbody></table></div>
      ${coins.length > 10 ? `<details><summary>All ${coins.length} listed coins</summary><div class="wu-dossier-table"><table class="wu-coins">${head}<tbody>${coinRows(coins.slice(10))}</tbody></table></div></details>` : ''}
      <p class="note">${esc((view.notes || {}).after_signature ? 'Later buyers’ signatures: ' + view.notes.after_signature + '.' : '')}</p>` : '<p class="note">No read first-buy coin for this wallet.</p>'}
      ${regs.length ? `<h4>Unverified high-overlap addresses</h4><p class="note">Repeat buyers of this wallet that also repeat-buy after other wallets. Unverified: not proof of a bot or a shared owner.</p><ul>${regs.map(r => `<li title="${esc(r.id)}">${esc(who(r.id, r.handle))} · here on ${r.k} of ${r.n} · after ${r.other_herds} other wallet${r.other_herds === 1 ? '' : 's'}</li>`).join('')}</ul>` : ''}
      <details><summary>Sell-side links</summary><h4>Sells into it</h4>${lane(view.sells_into_it || [], true)}<h4>It sells into</h4>${lane(view.it_sells_into || [], false)}</details>
      ${pt.hours_utc || pt.lanes ? `<details><summary>Trading hours and lanes</summary><p class="note">${esc(Object.entries(pt.hours_utc || {}).map(([b, n]) => b + ' ' + n).join(' · '))}</p><p class="note">${esc(Object.entries(pt.lanes || {}).map(([l, n]) => l + ' ' + n).join(' · '))}</p></details>` : ''}`;
  }

  // The gaps list: the wallet view's D4 gaps, plus the dossier's admission gap (D4 reads it from the
  // dossier, never from the wallet view). Either may arrive first; both redraw the same list.
  function renderGaps(inspector) {
    const host = inspector && inspector.querySelector('[data-gaps]');
    if (!host) return;
    const key = universeState.chain + ':' + universeState.uid;
    const wallet = universeState.walletGaps && universeState.walletGaps.key === key ? universeState.walletGaps.gaps : null;
    const dossier = universeState.dossier && universeState.dossier.key === key ? universeState.dossier.view : null;
    const gaps = (wallet || []).slice();
    const fun = dossier && dossier.funnel;
    if (dossier && dossier.state === 'ok' && fun && fun.observed_buys > 0 && !fun.valid_entries)
      gaps.push({ key: 'dossier_admission_missing', what: 'No recorded buy in the 30-day dossier has passed its admission checks.',
                  fill: 'Buys whose receipts decode and whose gates evaluate; see Execution research.' });
    universeState.gaps = gaps;
    if (wallet === null) { host.innerHTML = '<p class="note">Missing data: loading the wallet view…</p>'; return; }
    host.innerHTML = '<h4>Missing data</h4>' + (gaps.map((g, i) => `<label class="wu-gap"><input type="radio" name="wu-gap" value="${i}"${i ? '' : ' checked'}> <span><b>${esc(g.what)}</b><br><span class="note">Would fill it: ${esc(g.fill)}${g.n != null ? ' · n = ' + esc(g.n) : ''}${g.since ? ' · since ' + esc(utc(g.since)) : ''}</span></span></label>`).join('')
      || '<p class="note">No named gap.</p>');
  }

  async function loadWalletView(inspector, uid, chain) {
    const coins = inspector.querySelector('[data-coins]');
    walletFetch?.abort();
    const controller = new AbortController(); walletFetch = controller;
    universeState.walletGaps = null;
    if (coins) coins.innerHTML = '<p class="note">Loading supporting coins…</p>';
    renderGaps(inspector);
    try {
      const response = await fetch('/api/memecoin/wallet-map/wallet?id=' + encodeURIComponent(uid) + '&chain=' + encodeURIComponent(chain), { signal: controller.signal, cache: 'no-store' });
      const view = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(view.error || ('HTTP ' + response.status));
      if (uid !== universeState.uid || chain !== universeState.chain || !inspector.isConnected) return;
      if (coins) coins.innerHTML = supportingCoinsHTML(view);
      universeState.walletGaps = { key: chain + ':' + uid, gaps: Array.isArray(view.gaps) ? view.gaps : [] };
      renderGaps(inspector);
    } catch (err) {
      if (err.name === 'AbortError' || !inspector.isConnected) return;
      if (coins) coins.innerHTML = '<p class="note">Supporting coins unavailable: ' + esc(err.message) + '</p>';
      const host = inspector.querySelector('[data-gaps]');
      if (host) host.innerHTML = '<p class="note">Missing data unknown: the wallet view did not load.</p>';
    }
  }

  // "Save as research question" (D4): the observation sentence plus the selected gap, posted to the
  // existing dossier note endpoint with the dashboard's CSRF token. No new workflow, no new table.
  async function saveQuestion(inspector) {
    const status = inspector.querySelector('#wu-save-status');
    const picked = inspector.querySelector('input[name="wu-gap"]:checked');
    const gap = picked ? (universeState.gaps || [])[Number(picked.value)] : null;
    const observation = (inspector.querySelector('.wu-observation') || {}).textContent || '';
    const text = ('Research question. Observation: ' + observation.trim() +
      (gap ? '\nGap: ' + gap.what + '\nWould fill it: ' + gap.fill : '\nGap: none named')).slice(0, 4000);
    const button = inspector.querySelector('#wu-save-question');
    button.disabled = true; status.textContent = 'Saving…';
    const url = '/api/memecoin/wallet-map/dossier?chain=' + encodeURIComponent(universeState.chain) + '&wallet=' + encodeURIComponent(universeState.uid);
    try {
      const result = await send('POST', url, { text });
      if (!result.ok) throw new Error(result.error || 'Save failed');
      status.textContent = 'Saved to research notes.';
      const dossierHost = inspector.querySelector('[data-dossier]');
      if (dossierHost) loadDossier(dossierHost, universeState.uid, universeState.chain);
    } catch (err) { status.textContent = err.message; }
    button.disabled = false;
  }

  async function loadUniverse() {
    const request=++universeState.request, chain=universeState.chain;
    universeView?.loading(chain);
    const coverage=document.getElementById('wallet-research-coverage');
    if(coverage) coverage.textContent='Loading research coverage for '+chainLabel(chain)+'…';
    try {
      const response=await fetch('/api/memecoin/wallet-map/universe?chain='+encodeURIComponent(chain));
      if(response.status===400 && chain!=='solana' && (await response.clone().json().catch(()=>({}))).error==='unknown_chain') {
        if(request===universeState.request){universeState.chain='solana';loadUniverse();}   // W4b: the server names the chains
        return;
      }
      if(!response.ok) throw new Error('HTTP '+response.status);
      const body=await response.json();
      if(request===universeState.request) drawUniverse(body);      // body.chain is canonical: 'eth' draws as '1'
    } catch(err) { if(request===universeState.request) { const coverage=document.getElementById('wallet-research-coverage'); if(coverage) coverage.innerHTML=researchCoverageHTML(null,chain); if(universeView)universeView.error('Universe unavailable: '+err.message); else {const host=document.getElementById('wallet-universe');if(host)host.textContent='Universe unavailable: '+err.message;} } }
  }

  root.walletMap = { load: signal => { loadFunnel(signal); loadUniverse(); return load(signal); }, draw, drawFunnel, drawUniverse };
})(typeof window !== 'undefined' ? window : globalThis);
