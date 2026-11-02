/* Desk tab: read-only view of the LLM desk (GET /api/memecoin/desk/status). Display only; nothing here
   writes. Outcomes are per cohort, never pooled, under the no-tuning line the API sends. */
(function (root) {
  'use strict';

  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function usd(v) { return v == null ? 'unknown' : (v < 0 ? '-$' : '$') + Math.abs(v).toFixed(2); }
  function age(s) { return s == null ? '' : s < 120 ? Math.round(s) + ' s' : Math.round(s / 60) + ' min'; }
  function table(head, rows) {
    return '<div class="table-wrapper"><table class="data-table compact"><thead><tr>' + head.map(h => '<th>' + esc(h) + '</th>').join('') +
      '</tr></thead><tbody>' + (rows.length ? rows.join('') :
      '<tr><td colspan="' + head.length + '" class="table-empty text-muted">Nothing yet.</td></tr>') + '</tbody></table></div>';
  }
  function tr(cells) { return '<tr>' + cells.map(c => '<td>' + c + '</td>').join('') + '</tr>'; }

  function capsHtml(c) {
    if (!c || !c.available) return '<p class="text-muted">Ledger: ' + esc((c && c.reason) || 'unavailable') + '.</p>';
    const d = c.desk;
    const desk = '<p><b>Desk-wide:</b> open ' + d.open + ' / ' + d.max_open +
      ' · today realized ' + usd(d.daily_realized_usd) + ' vs halt ' + usd(d.daily_halt_usd) +
      (d.daily_latched ? ' · <b>HALTED today</b>' : '') + '</p>';
    const rows = c.cohorts.map(k => tr([esc(k.cohort_id.slice(0, 26)) + '…', esc(k.producer), esc(k.prompt_version),
      String(k.open), usd(k.known_net_usd) + (k.net_usd == null ? ' <span class="text-muted">(costs pending)</span>' : ''),
      k.stop_mode === 'manual' ? 'manual stop' : usd(k.kill_limit_usd), k.killed ? '<b>KILLED</b>' :
        k.stop_mode === 'manual' ? (k.admission_enabled ? 'admission enabled' : 'admission stopped') : 'live']));
    return desk + table(['cohort', 'producer', 'prompt', 'open', 'cumulative net', 'kill at', 'state'], rows);
  }

  function funnelHtml(t, ranker) {
    if (!t || !t.available) return '<p class="text-muted">Desk DB: ' + esc((t && t.reason) || 'unavailable') + '.</p>';
    const line = '<p><b>Today:</b> emitted ' + t.emitted +
      (ranker ? ' · ranked ' + ranker.ranked + ' · shortlisted ' + ranker.shortlisted : ' · ranker: no data') +
      ' · model calls last hour ' + t.model_calls_last_hour + ' / ' + (t.seat_cap_per_hour || 'cap unset') + '</p>';
    const order = ['hard_filter', 'vet', 'size', 'fill', 'close'];
    const stages = Object.keys(t.stages).sort((a, b) => (order.indexOf(a) + 99) % 99 - (order.indexOf(b) + 99) % 99);
    const rows = [];
    stages.forEach(s => t.stages[s].slice(0, 8).forEach(r => rows.push(tr([esc(s), esc(r.verdict), esc(r.reason), String(r.n)]))));
    return line + table(['stage', 'verdict', 'reason', 'tickets'], rows);
  }

  function workersHtml(ws) {
    return table(['worker', 'state', 'heartbeat'], (ws || []).map(w =>
      tr([esc(w.worker), w.state === 'running' ? 'running' : '<b>' + esc(w.state) + '</b>', age(w.heartbeat_age_s)])));
  }

  function reportsHtml(r) {
    const pm = r && r.latest_postmortem;
    return '<p><b>Post-mortem:</b> ' + (pm ? '<a href="/api/memecoin/desk/postmortem/' + esc(pm) + '" target="_blank" rel="noopener">' +
      esc(pm) + '</a>' : 'none yet') + ' · <b>Checker:</b> ' +
      (r && r.checker ? r.checker.checks + ' checks, latest ' + esc(r.checker.latest_day) : 'none yet') + '</p>';
  }

  function short(m) { return m ? esc(m.slice(0, 6)) + '…' + esc(m.slice(-4)) : ''; }
  function num(v, d) { return v == null ? 'unknown' : Number(v).toFixed(d || 0); }

  function feedsHtml(f) {
    if (!f) return '<p class="text-muted">Feeds: unavailable.</p>';
    const beats = table(['feed', 'last data', 'connected', 'last error'], (f.heartbeats || []).map(h =>
      tr([esc(h.feed), h.age_s == null ? '<b>no data</b>' : age(h.age_s), h.connected == null ? '' : String(h.connected),
          esc(h.last_error || '')])));
    const t = f.trending || {};
    const trend = !t.available ? '<p class="text-muted">Trending: ' + esc(t.reason || 'unavailable') + '.</p>' :
      '<p><b>KOTH:</b> ' + short(t.koth.mint) + ' · mcap ' + usd(t.koth.mcap_usd) + ' · ' + esc(t.koth.curve_kind || 'curve unknown') +
      ' · coverage ' + esc(t.coverage) + '</p>' +
      table(['#', 'coin', '15 min buys', 'makers', 'swaps', '15 min change %', 'mcap', 'curve'], t.ranks.map(x =>
        tr([String(x.rank), short(x.mint), usd(x.buy_usd_15m), String(x.makers_15m), String(x.swaps_15m),
            num(x.change_15m_pct, 1), usd(x.mcap_usd), esc(x.curve_kind || '')])));
    const n = f.narrative || {};
    const narr = !n.available ? '<p class="text-muted">Narrative: ' + esc(n.reason || 'unavailable') + '.</p>' :
      (n.version === 'narrative-live-v2' ? '<p>' + esc(n.scope) + '</p><p><b>Top observed name tokens, last closed hour</b></p>' : '<p><b>Top name tokens, last closed hour</b> (launches and distinct creators side by side: one creator = a launch farm)</p>') +
      table(['token', 'launches', 'creators', 'prev hour'], (n.tokens.name_token || []).map(x =>
        tr([esc(x.token), num(x.launches), num(x.creators), num(x.prev_hour_launches)]))) +
      (n.version === 'narrative-live-v2' ? '<p><b>Observed themes, last 60 min</b> · retained flow lower bounds; missing source = unknown</p>' : '<p><b>Themes, last 60 min</b> (theme-rules-v1; only about a quarter of launches get a specific theme)</p>') +
      table(['theme', 'launches', 'band entries', 'buy USD', 'buyers'], (n.themes_60m || []).map(x =>
        tr([esc(x.theme), num(x.launches), num(x.band_entries), usd(x.buy_usd), num(x.buyers)])));
    return '<p class="text-muted">' + esc(f.collect_only) + '</p>' + beats + '<h4>Tape trending</h4>' + trend +
      '<h4>Narrative</h4>' + narr;
  }

  function copyLaneHtml(c) {
    if (!c || !c.available) return '<p class="text-muted">Ledger: ' + esc((c && c.reason) || 'unavailable') + '.</p>';
    const grp = (title, rows) => '<h4>' + esc(title) + '</h4>' + table(['', 'closes', 'wins', 'unpriced', 'realized'],
      rows.map(r => tr([esc(r.key), String(r.closes), String(r.wins), String(r.unpriced), usd(r.realized_usd)])));
    const noFee = c.fills_fee.reduce((n, f) => n + f.no_fee, 0);
    return '<p class="text-muted">Real closes, rolling last 7 days (since ' + esc(c.since) + ', not calendar days). ' +
      'Unpriced closes are not in realized. <b>Realized excludes network gas (fee unrecorded on ' + noFee +
      ' fills)</b>: it is not net.</p>' +
      grp('By exit reason', c.by_exit_reason) + grp('By chain', c.by_chain) + grp('By signal source', c.by_signal_source) +
      '<h4>Fills without a network fee</h4>' + table(['chain', 'side', 'fills', 'no fee'],
        c.fills_fee.map(f => tr([esc(f.chain), esc(f.kind), String(f.fills), f.no_fee ? '<b>' + f.no_fee + '</b>' : '0'])));
  }

  async function loadCopyLane(signal) {
    const host = document.getElementById('desk-copy-lane');
    if (!host) return;
    try {
      const resp = await fetch('/api/memecoin/desk/copy-lane', { signal, credentials: 'same-origin' });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      host.innerHTML = copyLaneHtml(await resp.json());
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      host.innerHTML = '<p class="text-muted">Copy lane unavailable: ' + esc(err && err.message || err) + '</p>';
    }
  }

  function paperHtml(p) {
    if (!p.available) return '<p class="text-muted">PAPER: ' + esc(p.reason) + '</p>';
    return '<p><b>PAPER — no real money.</b> Closed ' + p.closed.closes + ', paper PnL ' +
      Number(p.closed.pnl_usd).toFixed(2) + ' USD.</p>' +
      table(['PAPER', 'coin', 'state', 'opened', 'entry', 'mark', 'exit', 'PnL USD', 'note'],
        p.positions.map(r => tr(['PAPER', esc(r.mint.slice(0, 4) + '…' + r.mint.slice(-4)), esc(r.state),
          esc(r.filled_at || r.requested_at), r.entry_price_usd == null ? '' : Number(r.entry_price_usd).toPrecision(4),
          r.last_mark_usd == null ? '' : Number(r.last_mark_usd).toPrecision(4), esc(r.exit_reason || ''),
          r.pnl_usd == null ? '' : Number(r.pnl_usd).toFixed(2), esc(r.refusal || '')])));
  }

  async function loadPaper(signal) {
    const host = document.getElementById('desk-paper');
    if (!host) return;
    try {
      const resp = await fetch('/api/memecoin/desk/paper', { signal, credentials: 'same-origin' });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      host.innerHTML = paperHtml(await resp.json());
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      host.innerHTML = '<p class="text-muted">PAPER positions unavailable: ' + esc(err && err.message || err) + '</p>';
    }
  }

  async function loadFeeds(signal) {
    const host = document.getElementById('desk-feeds');
    if (!host) return;
    try {
      const resp = await fetch('/api/memecoin/desk/feeds', { signal, credentials: 'same-origin' });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      host.innerHTML = feedsHtml(await resp.json());
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      host.innerHTML = '<p class="text-muted">Feeds unavailable: ' + esc(err && err.message || err) + '</p>';
    }
  }

  function draw(body) {
    const host = document.getElementById('desk-body');
    const stamp = document.getElementById('desk-stamp');
    if (!host) return;
    if (stamp) stamp.textContent = 'As of ' + body.as_of + ' · display only';
    host.innerHTML = '<p class="text-muted">' + esc(body.no_tuning) + '</p>' +
      '<h3>PAPER positions</h3><div id="desk-paper"><p class="text-muted">Loading…</p></div>' +
      '<h3>Caps</h3>' + capsHtml(body.cohorts) +
      '<h3>Funnel</h3>' + funnelHtml(body.tickets, body.ranker) +
      '<h3>Workers</h3>' + workersHtml(body.workers) +
      '<h3>Reports</h3>' + reportsHtml(body.reports) +
      '<h3>Feeds (collect/report only)</h3><div id="desk-feeds"><p class="text-muted">Loading…</p></div>' +
      '<h3>Copy lane, last 7 days</h3><div id="desk-copy-lane"><p class="text-muted">Loading…</p></div>';
  }

  async function load(signal) {
    try {
      const resp = await fetch('/api/memecoin/desk/status', { signal, credentials: 'same-origin' });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      draw(await resp.json());
      loadFeeds(signal);
      loadCopyLane(signal);
      loadPaper(signal);
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      const stamp = document.getElementById('desk-stamp');
      if (stamp) stamp.textContent = 'Desk status unavailable: ' + (err && err.message || err);
    }
  }

  root.deskView = { load, draw, feedsHtml, copyLaneHtml, paperHtml };
})(window);
