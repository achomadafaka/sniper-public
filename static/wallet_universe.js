/* Interactive Wallet universe. Stored observations only; authority actions stay in wallet_map.js. */
(function(root) {
'use strict';
// Camera coordinates are world units; viewport and pointer coordinates are SVG pixels.
function worldAt(camera, point, viewport) {
  return {
    x: camera.x + (point.x - viewport.width / 2) / camera.k,
    y: camera.y + (point.y - viewport.height / 2) / camera.k,
  };
}

function zoomAt(camera, factor, point, viewport, limits) {
  if (!Number.isFinite(factor) || factor <= 0) throw new RangeError('Zoom factor must be positive');
  const anchor = worldAt(camera, point, viewport);
  const k = Math.max(limits.min, Math.min(limits.max, camera.k * factor));
  return {
    x: anchor.x - (point.x - viewport.width / 2) / k,
    y: anchor.y - (point.y - viewport.height / 2) / k,
    k,
  };
}

function panBy(camera, dx, dy) {
  return { x: camera.x - dx / camera.k, y: camera.y - dy / camera.k, k: camera.k };
}

function pinchCamera(camera, start, current, viewport, limits) {
  const midpoint = pair => ({ x: (pair[0].x + pair[1].x) / 2, y: (pair[0].y + pair[1].y) / 2 });
  const distance = pair => Math.max(1, Math.hypot(pair[0].x - pair[1].x, pair[0].y - pair[1].y));
  const before = midpoint(start), after = midpoint(current);
  const zoomed = zoomAt(camera, distance(current) / distance(start), before, viewport, limits);
  return panBy(zoomed, after.x - before.x, after.y - before.y);
}

function fitPoints(points, viewport, padding = 40) {
  const usable = points.filter(p => Number.isFinite(p.x) && Number.isFinite(p.y));
  if (!usable.length) return { x: 0, y: 0, k: 1 };
  const left = Math.min(...usable.map(p => p.x - Math.max(0, p.r || 0)));
  const right = Math.max(...usable.map(p => p.x + Math.max(0, p.r || 0)));
  const top = Math.min(...usable.map(p => p.y - Math.max(0, p.r || 0)));
  const bottom = Math.max(...usable.map(p => p.y + Math.max(0, p.r || 0)));
  return {
    x: (left + right) / 2,
    y: (top + bottom) / 2,
    k: Math.min(Math.max(1, viewport.width - 2 * padding) / Math.max(1, right - left),
                Math.max(1, viewport.height - 2 * padding) / Math.max(1, bottom - top)),
  };
}

// W4b URL STATE (spec 2026-10-07-wallet-map-overhaul-design.md W4). One writer, inside the mount;
// these three are pure. Keys: wm_chain, wm_view (table|map), wm_wallet, wm_sat, wm_size, wm_sort
// (key:dir), wm_f (min,ranked,measured,armed,funded), wm_cmp (<= 6 ids), wm_folds (JSON {uid: [fold
// ids]}, the W4a per-wallet keys: summary + nth occurrence).
const URL_FOLDS_MAX = 1500;
const URL_ENUMS = {ranked: ['', 'yes', 'no'], measured: ['', 'yes', 'no'], armed: ['', 'armed', 'watch'],
                   size: ['following', 'capital', 'impact']};
const URL_ID = /^[\w.:-]{1,128}$/;
// Syntax only: which chains exist is the server's answer (body.chains, or 400 unknown_chain), never a constant.
const chainId = v => typeof v === 'string' && /^[\w-]{1,32}$/.test(v) ? v : null;

// Syntax only: a value that cannot be one of ours reads as the default. What needs the data (is the
// wallet, satellite, sort column or comparison member on this chain?) is checked once it arrives.
function readUrlState(search) {
  const p = new URLSearchParams(search || '');
  const id = v => typeof v === 'string' && URL_ID.test(v) && v !== '__proto__' ? v : '';
  const pick = (name, value) => URL_ENUMS[name].includes(value) ? value : '';
  const sort = /^(\w{1,32})(?::(-?1))?$/.exec(p.get('wm_sort') || '');
  const f = String(p.get('wm_f') || '').split(','), min = Number(f[0]);
  const folds = {};
  try {
    const raw = JSON.parse(p.get('wm_folds') || '{}');
    if (raw && typeof raw === 'object' && !Array.isArray(raw))
      for (const [uid, keys] of Object.entries(raw)) {
        const keep = Array.isArray(keys) ? keys.filter(k => typeof k === 'string' && k && k.length <= 200) : [];
        if (id(uid) && keep.length) folds[uid] = keep.slice(-40);
      }
  } catch (_) { /* malformed: no folds */ }
  return {chain: chainId(p.get('wm_chain')), view: p.get('wm_view') === 'map' ? 'map' : 'table',
          wallet: id(p.get('wm_wallet')), sat: id(p.get('wm_sat')) || null, size: pick('size', p.get('wm_size')) || null,
          sort: sort ? {key: sort[1], dir: sort[2] === '-1' ? -1 : 1} : null,
          filters: p.has('wm_f') ? {min: Number.isInteger(min) && min >= 0 && min <= 1e6 ? min : 0, ranked: pick('ranked', f[1] || ''),
                                    measured: pick('measured', f[2] || ''), armed: pick('armed', f[3] || ''),
                                    funded: f[4] === '1'} : null,
          compare: [...new Set(String(p.get('wm_cmp') || '').split(',').filter(id))].slice(0, 6), folds};
}

function writeUrlParams(params, s) {
  const put = (k, v) => v ? params.set(k, v) : params.delete(k);
  const f = s.filters || {};
  const defaults = !f.min && !f.ranked && !f.measured && !f.armed && !f.funded;
  put('wm_chain', s.chain); put('wm_view', s.view); put('wm_wallet', s.wallet); put('wm_sat', s.sat);
  put('wm_size', s.size); put('wm_sort', s.sort ? s.sort.key + ':' + s.sort.dir : '');
  put('wm_f', defaults ? '' : [f.min || 0, f.ranked || '', f.measured || '', f.armed || '', f.funded ? 1 : ''].join(','));
  put('wm_cmp', (s.compare || []).join(',')); put('wm_folds', s.folds);
}

// Every wallet's open folds when they fit the bound, else the current wallet's only, its oldest
// openings dropped first until that fits (Alpha: ~1500 chars).
function encodeFolds(folds, current, max = URL_FOLDS_MAX) {
  const pack = entries => entries.length ? JSON.stringify(Object.fromEntries(entries)) : '';
  const fits = text => encodeURIComponent(text).length <= max;
  const every = pack([...folds].filter(([, ids]) => ids.size).map(([uid, ids]) => [uid, [...ids]]));
  if (fits(every)) return every;
  let mine = [...(folds.get(current) || [])];
  while (mine.length && !fits(pack([[current, mine]]))) mine = mine.slice(1);
  return pack(mine.length ? [[current, mine]] : []);
}

function mount(host,options) {
  host.innerHTML = '\n<header><div><h3>Wallet universe</h3><p>Repeat buyers and same-slot order from our sampled first buys. Not ownership, not profit.</p></div></header>\n<div class="toolbar"><nav class="chains" aria-label="Chain universe"></nav><div class="search"><input id="find" type="search" placeholder="Find a wallet or repeat buyer" aria-label="Find wallet or repeat buyer" autocomplete="off"><div id="results" hidden></div></div></div>\n<p class="wu-live-status" role="status"></p><p class="wu-availability" role="status" aria-label="What this chain measures"></p><div class="main"><div class="wu-left"><div class="wu-pane-bar" role="group" aria-label="Wallet Map view"><button id="pane-table" aria-pressed="true">Table</button><button id="pane-map" aria-pressed="false">Map</button></div><section class="wu-table" aria-label="Wallet relationship table"><div class="wu-filters"><label>Min read coins <input id="wu-f-min" type="number" min="0" step="1" value="0"></label><label>Timing <select id="wu-f-ranked"><option value="">all</option><option value="yes">ranked</option><option value="no">unranked</option></select></label><label>Evidence <select id="wu-f-measured"><option value="">all</option><option value="yes">measured</option><option value="no">unmeasured</option></select></label><label>Authority <select id="wu-f-armed"><option value="">all</option><option value="armed">armed</option><option value="watch">watch-only</option></select></label><label><input id="wu-f-funded" type="checkbox"> Has funded closes</label><span id="wu-f-count" role="status"></span></div><div class="wu-table-scroll"><table><caption class="wu-caption"></caption><thead></thead><tbody></tbody></table></div></section><section class="sky" aria-label="Orbital wallet map" hidden><div class="sky-top"><strong id="sky-title"></strong><span id="sky-caption"></span></div><label class="size">Planet size <select aria-label="Planet size metric"><option value="following">Repeat buyers</option><option value="capital" disabled>Liquid capital · collecting</option><option value="impact" disabled>Coin flow · collecting</option></select></label><svg tabindex="0" aria-describedby="map-help" class="space-map" viewBox="0 0 940 680" role="group" aria-label="Wallet planets and their repeat buyers"></svg><div class="map-breadcrumb" id="breadcrumb"></div><button id="browse" class="browse" aria-expanded="false">Browse wallets</button><div id="wallet-drawer" hidden></div><div class="map-foot"><div class="map-notes"><div id="hover-info" hidden></div><div class="map-help" id="map-help">Drag to move · Scroll or pinch to zoom · Double-click to focus</div><div class="legend"><span><i></i>Wallet</span><span><i class="moon-key"></i>Repeat buyer</span><span><i class="line-key"></i>Shared buyer</span><span><i class="unknown-key"></i>Not measured</span><span><i class="zero-key"></i>Measured zero</span></div></div><div class="zoom"><button id="zoom-out" aria-label="Zoom out">−</button><output id="zoom-level" aria-label="Zoom level">100%</output><button id="zoom-in" aria-label="Zoom in">+</button><button id="zoom-reset" aria-label="Reset view">Universe</button><button id="focus-wallet" aria-label="Focus selected wallet">Focus</button><button id="full-screen" aria-label="Expand map">↗</button><button id="details" aria-expanded="false">Details</button></div></div></section></div><aside class="inspector" tabindex="-1" aria-label="Selected wallet details" aria-live="polite"></aside><button id="details-close">Close details</button></div>\n<footer class="foot"><p id="scope"></p><span>Capital ≠ repeat buyers ≠ coin impact</span></footer>\n';
  const data = {herders:[],shared:[]};
let chains = [['solana','Solana','#8dd8ce'],['56','BSC','#e8c18b'],['4663','Robinhood','#b6b5ea'],['8453','Base','#82b6e9'],['5042','Arc','#cda6ce']];
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const short = id => String(id).length > 14 ? String(id).slice(0,5)+'…'+String(id).slice(-4) : id;
const state = { metric:'following', chain:'solana', uid:'', moon:null, camera:null, view:'universe', expanded:false,
  pane:'table', sort:null, filters:{min:0,ranked:'',measured:'',armed:'',funded:false} };
const graph = host.querySelector('.space-map');
state.loading = false;                 // W4a; its own line, so W2's state fields do not collide with it
// W4a (spec 2026-10-07-wallet-map-overhaul-design.md). Injected here, not in the one-line template above,
// so the readability/table/summary slices that edit the template do not collide with these controls.
graph.style.touchAction = 'pan-y';     // one-finger vertical drags scroll the page; pinch and sideways drags stay ours
host.querySelector('#map-help').textContent = window.matchMedia('(pointer: coarse)').matches   // a touch screen has no wheel
  ? 'Drag sideways to move · Pinch or the buttons to zoom · Drag up or down to scroll the page'
  : 'Drag to move · Ctrl/⌘+scroll, pinch or the buttons to zoom · Click the map, then scroll to zoom';
host.querySelector('header').insertAdjacentHTML('beforeend',
  '<div class="map-refresh"><span id="graph-age" role="status"></span> <button id="map-refresh" type="button">Refresh</button></div>');
let wheelArmed = false, lastInspected = null, holdTimer = null;
const openFolds = new Map();           // wallet -> fold keys the reader opened; a same-wallet redraw keeps them
const foldKey = d => (d.querySelector(':scope > summary')?.textContent || '').replace(/[\d.,$%+−-]+/g,'#').trim();
// [details, (fold key, nth occurrence)] in document order: two folds with the same summary keep their
// own state (Gamma LOW on W4a: frankdegods repeats summaries, and opening one reopened both).
function foldIds(root) {
  const seen = new Map();
  return [...root.querySelectorAll('details')].map(d => {
    const base = foldKey(d), n = seen.get(base) || 0;
    seen.set(base, n + 1);
    return [d, base + '#' + n];
  });
}
const mobile = window.matchMedia('(max-width:700px)');
let nodes = [], source = [], positions = new Map(), viewport = {width:940,height:680}, fit = 1;
let world, pointerGesture = null, suppressClickUntil = 0, downTarget = null;
const pointers = new Map();
const chainName = () => chains.find(c => c[0] === state.chain)[1];
const label = w => w?.handle || short(w?.id || '');
const ranked = w => w.entry_order.n >= 10 && Number.isFinite(w.entry_order.share_before) && w.entry_order.share_before >= 0 && w.entry_order.share_before <= 1;
const money = v => '$'+Math.abs(v).toLocaleString('en-US',{maximumFractionDigits:0});
const quoteUnits = q => {
  const raw=String(q.net_after_atomic??'');
  if(!/^-?\d+$/.test(raw) || !Number.isInteger(q.decimals) || q.decimals<0 || q.decimals>38)return 'Unreadable quote units';
  const negative=raw.startsWith('-');
  const digits=raw.replace(/^-/,'').padStart(q.decimals+1,'0');
  const whole=digits.slice(0,digits.length-q.decimals).replace(/^0+(?=\d)/,'');
  const fraction=q.decimals?digits.slice(-q.decimals).replace(/0+$/,''):'';
  return (negative?'−':'+')+whole+(fraction?'.'+fraction:'')+' '+short(q.asset);
};
const flowWindows = r => {
  const windows=r.horizons?.windows;
  if(!Array.isArray(windows))return '';
  return `<p class="note">${r.source_position?.state==='add_on'?'Add-on: prior holdings visible.':'New wallet position: unknown.'}</p><div class="wu-dossier-table"><table class="wu-flow-windows"><thead><tr><th scope="col">After buy</th><th scope="col">Net quote flow</th><th scope="col">Verified buyers</th></tr></thead><tbody>${windows.map(w=>`<tr><th scope="row">${esc(w.seconds)}s</th><td>${w.state==='complete'?esc((w.quotes||[]).map(quoteUnits).join(' · ')||'0'):w.state==='not_captured'?'Not captured':'Unknown'}</td><td>${Number.isInteger(w.verified_buyer_count)?esc(w.verified_buyer_count)+(w.buyers_complete?'':'+'):'Unknown'}</td></tr>`).join('')}</tbody></table></div>`;
};
const metricValue = w => {
  if(state.metric==='following')return w.regulars==null?null:w.regulars;
  const m=w.authority?.metrics?.[state.metric];
  const v=state.metric==='capital'?m?.usd:m?.value_usd;
  return m?.state==='ok' && typeof v==='number' && Number.isFinite(v)?Math.abs(v):null;
};
const metricText = w => {
  const v=metricValue(w);
  if(state.metric==='following')return v==null?'Repeat buyers unknown':v+' repeat buyers';
  const m=w.authority?.metrics?.[state.metric];
  if(state.metric==='capital')return v==null?'Liquid capital unknown':money(v)+' liquid funds';
  return v==null?'Coin flow unknown':(m.value_usd>=0?'+':'−')+money(m.value_usd)+' post-buy flow';
};
const limits = () => ({min:fit*.55,max:Math.max(2.5,fit*20)});
const center = () => ({x:viewport.width/2,y:viewport.height/2});
const svgPoint = event => {
  const matrix = graph.getScreenCTM();
  return new DOMPoint(event.clientX,event.clientY).matrixTransform(matrix.inverse());
};

// W5a: what this chain can measure, from the universe payload (D1) alone. A count of reads is a
// measured number (0 means none yet); a graph this chain does not have is "not measured", never 0.
function availabilityText(body, chainLabel) {
  if (!body || state.loading) return ['Loading what this chain measures…'];
  const wallets = body.wallets || [], n = wallets.length;
  const proven = wallets.filter(w => w.coverage?.address?.state === 'ok').length;
  const fresh = wallets.filter(w => w.metrics?.capital?.state === 'ok').length;
  const g = body.graph_state;
  const graph = g === 'ok' || g === 'stale' ? 'measured' + (g === 'stale' ? ' (stale)' : '')
    : g === 'not_measured' ? 'not measured on this chain'
    : g === 'unavailable' ? 'unavailable' + (body.graph?.reason ? ' (' + body.graph.reason + ')' : '')
    : state.chain === 'solana' ? 'unknown' : 'not measured on this chain';
  return ['Roster ' + n + ' wallets', 'Addresses proven on ' + chainLabel + ': ' + proven + ' of ' + n,
          'Relationship graph: ' + graph, 'Fresh capital: ' + fresh + ' of ' + n];
}

function rows() {
  const observations = new Map((data.herders || []).map(w => [w.id,w]));
  const wallets = data.body?.wallets || [];
  return wallets.map(w => ({id:w.uid,handle:w.handle,regulars:null,regular_list:[],n:0,
    entry_order:{share_before:null,n:0}, ...(state.chain==='solana' && observations.get(w.uid)),
    handle:w.handle || observations.get(w.uid)?.handle, authority:w})).map(w=>{
      w.regular_list=[...w.regular_list];
      if(w.authority.identity?.links_method) for(const r of w.authority.identity.related_wallets||[]) {
        if(r.n_coins<2 && !r.funding_verified)continue;
        const peer={id:r.address,k:r.n_coins,eligible_coins:r.eligible_coins,identity:r};
        const at=w.regular_list.findIndex(m=>m.id===r.address);
        if(at<0)w.regular_list.push(peer);else w.regular_list[at]=peer;
      }
      return w;
    });
}

function select(uid, push = true) {
  pendingPush = pendingPush || (push && uid !== state.uid);   // a wallet step is a history entry (W4b)
  restoreSat = null;
  state.uid = uid;
  state.moon = null;
  render();
  focusWallet(true);
  host.querySelector('.inspector').scrollTop = 0;
}

// "Next in list" steps through the rows the table shows, filtered and sorted (review SF04): one list,
// visibleRows(), for the table and the steps. A hidden selection steps to the first (Next) or last
// (Previous) visible row; with one visible row (or none) the steps are disabled in the inspector.
function stepWallet(step) {
  const list = visibleRows().shown.map(r => r.w);
  if (!list.length) return;
  const index = list.findIndex(w => w.id === state.uid);
  const next = index < 0 ? (step > 0 ? 0 : list.length - 1) : (index + step + list.length) % list.length;
  if (list[next].id !== state.uid) select(list[next].id, false);   // "Next in list" never pushes
}

function resize() {
  viewport = {width:graph.clientWidth || 940,height:graph.clientHeight || 680};
  graph.setAttribute('viewBox',`0 0 ${viewport.width} ${viewport.height}`);
  fit = fitPoints([...positions.values()],viewport,mobile.matches?65:90).k;
  if (!state.camera || state.view === 'universe') fitUniverse();
  else if (state.view === 'system') focusWallet();
  else applyCamera();
}

function fitUniverse() {
  state.camera = fitPoints([...positions.values()],viewport,mobile.matches?65:90);
  state.view = 'universe';
  applyCamera();
}

function revealMap() {
  if (mobile.matches) graph.scrollIntoView({block:'center'});
}

function focusWallet(reveal=false) {
  const p = positions.get(state.uid);
  if (!p) return;
  state.camera = fitPoints([{...p,r:p.r+115}],viewport,mobile.matches?35:95);
  state.camera.k = Math.min(1.65,state.camera.k);
  state.view = 'system';
  applyCamera();
  if (reveal) revealMap();
}

function fitConnections() {
  const related = source.filter(w => w.id === state.uid || w.regular_list.some(m => m.id === state.moon));
  state.camera = fitPoints(related.map(w => positions.get(w.id)),viewport,mobile.matches?70:95);
  state.view = 'connections';
  applyCamera();
  revealMap();
}

function changeZoom(factor, anchor=center()) {
  state.camera = zoomAt(state.camera,factor,anchor,viewport,limits());
  if (state.view === 'universe' || (state.view === 'system' && state.camera.k < .55)) state.view = 'custom';
  applyCamera();
}

function applyCamera() {
  if (!world || !state.camera) return;
  const c = state.camera;
  world.setAttribute('transform',`translate(${viewport.width/2} ${viewport.height/2}) scale(${c.k}) translate(${-c.x} ${-c.y})`);
  world.setAttribute('class',`world ${c.k>=.55?'detail':''} ${state.view}`);
  world.querySelectorAll('[data-fixed-label]').forEach(el => {
    el.setAttribute('transform',`translate(${el.dataset.x} ${el.dataset.y}) scale(${Math.min(2,Math.max(1,c.k/fit))/c.k})`);
  });
  world.querySelectorAll('.moon.selected .moon-body').forEach(el => el.setAttribute('r',9/c.k));
  world.querySelectorAll('.moon .hit').forEach(el => el.setAttribute('r',Math.max(12,22/c.k)));
  host.querySelector('#zoom-level').value = Math.round(c.k/fit*100)+'%';
  host.querySelector('#zoom-out').disabled = c.k <= limits().min*1.001;
  host.querySelector('#zoom-in').disabled = c.k >= limits().max*.999;
  const owner = source.find(w => w.id === state.uid);
  host.querySelector('#breadcrumb').classList.toggle('has-moon',!!state.moon);
  host.querySelector('#breadcrumb').innerHTML = `<button id="crumb-universe">${esc(chainName())}</button><span>/</span>${state.view==='universe'?'<span>All wallets</span>':`<button id="crumb-wallet">${esc(label(owner))}</button>${state.moon?`<span>/</span><span>${esc(short(state.moon))}</span>`:''}`}`;
  host.querySelector('#crumb-universe').onclick = fitUniverse;
  const walletCrumb = host.querySelector('#crumb-wallet');
  if (walletCrumb) walletCrumb.onclick = () => select(state.uid);
}

// The relationship table (Wallet Map W2): the same rows as the map, one wallet per line. A missing
// field is unknown, a named state (not measured, unavailable) says why, and a measured zero is 0.
const LINK_KINDS=[['co_buy_slot','co-buy'],['slot_follower','after'],['follows','follows'],['dumps_on','dumps on'],['shared_funder','funder']];
const dim = (text,title='') => `<span class="wu-dim"${title?` title="${esc(title)}"`:''}>${esc(text)}</span>`;
const ago = ms => { const m=Math.max(0,Math.round((Date.now()-ms)/60000)); return m<60?m+' min':m<2880?Math.round(m/60)+' h':Math.round(m/1440)+' d'; };
function tableColumns(rankOf,newest) {
  const seen=w=>state.chain==='solana' && w.units!=null;        // a herders observation exists
  // A chain-wide state (not measured off Solana, the herders read failed) is named once in the
  // caption; its cells stay a dash with the reason as a tooltip. A wallet with no read is per row.
  const not=()=>state.chain!=='solana'?{v:null,h:dim('—','Relationships are not measured on this chain')}
    :data.stamp?.error?{v:null,h:dim('—','Relationship reads unavailable: '+data.stamp.error)}:{v:null,h:dim('not read')};
  // Seven decision columns fit the 1366px summary layout without a horizontal scroll (Alpha
  // 2026-10-07): the first-buy coverage, the same-slot share and the link breakdown ride along as a
  // second line or a tooltip.
  const two=(top,sub)=>`${top}<div>${dim(sub)}</div>`;
  const links=w=>{
    const l=w.authority?.links;
    if(!l)return {v:null,h:dim('unknown')};
    if(!['ok','stale'].includes(l.state) || !l.by_type)return {v:null,h:l.state==='not_measured'?dim('—','Relationships are not measured on this chain'):dim(l.state==='unavailable'?'unavailable':'unknown')};
    const total=LINK_KINDS.reduce((n,[k])=>n+(l.by_type[k]||0),0);
    const parts=LINK_KINDS.map(([k,t])=>t+' '+(l.by_type[k]||0)).join(' · ');
    return {v:total,h:l.state==='stale'?two(total,'stale'):String(total),t:parts};
  };
  const funded=w=>{
    const f=w.authority?.funded;
    if(!f)return {v:null,h:dim('unknown')};
    if(f.state==='none')return {v:null,h:'',t:'No funded rows on this chain'};
    if(f.state!=='ok')return {v:null,h:dim(f.state==='unavailable'?'unavailable':'unknown')};
    const closes=f.closed_count===1?'1 close':f.closed_count+' closes';
    return f.roi_pct==null?{v:null,h:two(dim('unknown'),closes)}:{v:f.roi_pct,h:two((f.roi_pct>=0?'+':'−')+Math.abs(f.roi_pct).toFixed(1)+'%',closes)};
  };
  return [
    ['wallet','Wallet',w=>({v:label(w).toLowerCase(),h:`<button type="button" class="wu-row-open" title="${esc(w.id)}">${esc(label(w))}</button>`})],
    ['n','Read coins',w=>seen(w)?{v:w.n,h:String(w.n),t:`${w.read} of ${w.units} first buys read`}:not(),'Sampled coins (n) behind the repeat-buyer and same-slot reads'],
    ['regulars','Repeat buyers',w=>!seen(w)?not():{v:w.regulars,h:two(w.regulars==null?dim('few reads','Too few read coins to name repeat buyers (10 needed)'):w.regulars,w.n?`${w.drew}/${w.n}`:'0/0'),t:`${w.drew} of ${w.n} read coins had another decoded buyer land after it in the same slot`},'Repeat buyers; second line: read coins where another decoded buyer landed after it in the same slot, of n'],
    ['timing','Timing rank',w=>!seen(w)?not():ranked(w)?{v:rankOf.get(w.id),h:two('#'+rankOf.get(w.id),'of '+rankOf.size),t:`Median ${Math.round(w.entry_order.share_before*100)}% of decoded same-slot peers bought earlier, over ${w.entry_order.n} sampled coins`}
      :{v:null,h:two(dim('unranked'),w.entry_order.n<10?`n ${w.entry_order.n} < 10`:'no order'),t:w.entry_order.n<10?`Timing sample n = ${w.entry_order.n} sampled coins with a decoded peer order; ranking needs 10`:'The timing sample has no usable order'},'Rank by the median share of decoded same-slot peers that bought earlier; 1 = earliest'],
    ['links','Links',links,'Relationship-graph links on this chain; hover a count for co-buy (same slot), after (landed after it in its slot), follows, dumps on, funder (shared funder)'],
    ['funded','Funded return',funded,'Realized PnL ÷ entry notional, all time, robot entries only; only wallets we armed have rows'],
    ['fresh','Newest link',w=>newest.has(w.id)?{v:newest.get(w.id),h:ago(newest.get(w.id)),t:new Date(newest.get(w.id)).toISOString().slice(0,16).replace('T',' ')+'Z'}:{v:null,h:dim('—')},'Age of the newest link the graph saw for this wallet'],
  ];
}
function visibleRows() {
  const f=state.filters;
  const rankOf=new Map(source.filter(ranked).sort((a,b)=>a.entry_order.share_before-b.entry_order.share_before).map((w,i)=>[w.id,i+1]));
  const newest=new Map();
  for(const e of data.body?.edges||[])for(const id of [e.src_uid,e.dst_uid]){const t=Date.parse(e.last_seen);if(Number.isFinite(t)&&t>(newest.get(id)??-Infinity))newest.set(id,t);}
  const cols=tableColumns(rankOf,newest);
  let shown=source.filter(w=>{
    const seen=state.chain==='solana' && w.units!=null, fd=w.authority?.funded;
    return (!f.min || (seen && w.n>=f.min)) && (!f.ranked || ranked(w)===(f.ranked==='yes')) && (!f.measured || seen===(f.measured==='yes'))
      && (!f.armed || !!w.authority?.armed===(f.armed==='armed')) && (!f.funded || (fd?.state==='ok' && fd.closed_count>0));
  }).map(w=>({w,cells:cols.map(c=>c[2](w))}));
  if(state.sort){
    const i=cols.findIndex(c=>c[0]===state.sort.key), dir=state.sort.dir;
    if(i<0)state.sort=null;                 // W4b: a sort key this table has no column for (a stale or edited link)
    else shown.sort((a,b)=>{const x=a.cells[i].v,y=b.cells[i].v;return x===y?0:x==null?1:y==null?-1:(x<y?-1:1)*dir;});
  }
  return {cols,shown};
}
function renderTable() {
  const table=host.querySelector('.wu-table table');
  const keep=host.querySelector('.wu-table tbody')?.contains(document.activeElement)?document.activeElement.closest('tr')?.dataset.uid:null;
  const {cols,shown}=visibleRows();
  table.querySelector('.wu-caption').textContent=state.chain!=='solana' && source.some(w=>w.authority?.identity?.links_method)?`${chainName()} wallets: saved funding and before/after receipt links in wallet details. Same-slot ranking covers Solana only.`
    :state.chain!=='solana'?`${chainName()} wallets: relationships are not measured on this chain (the link graph covers Solana only). Funded results are our own trades.`
    :data.stamp?.error?`${chainName()} wallets: relationship reads unavailable (${data.stamp.error}).`
    :`${chainName()} wallets: repeat buyers and same-slot order from our sampled first buys. Not ownership, not profit.`;
  table.tHead.innerHTML='<tr>'+cols.map(([key,title,,tip])=>`<th scope="col" aria-sort="${state.sort?.key===key?(state.sort.dir>0?'ascending':'descending'):'none'}"${tip?` title="${esc(tip)}"`:''}><button type="button" data-sort-key="${key}">${esc(title)}</button></th>`).join('')+'</tr>';
  table.tBodies[0].innerHTML=shown.map(({w,cells})=>`<tr data-uid="${esc(w.id)}" aria-selected="${w.id===state.uid}">${cells.map((c,i)=>
    i===0?`<th scope="row" data-col="wallet">${c.h}</th>`:`<td data-col="${cols[i][0]}"${c.t?` title="${esc(c.t)}"`:''}>${c.h}</td>`).join('')}</tr>`).join('')
    ||`<tr><td colspan="${cols.length}">${dim(source.length?'No wallet matches these filters.':'No wallets on this chain.')}</td></tr>`;
  host.querySelector('#wu-f-count').textContent=`${shown.length} of ${source.length} wallets`;
  if(keep)table.tBodies[0].querySelector(`tr[data-uid="${CSS.escape(keep)}"] button`)?.focus();
}

function walletLink(address, chain=state.chain, base=null) {
  const code=`<code>${esc(address)}</code>`;
  base=base || (chain==='solana'?'https://solscan.io/':source.find(w=>w.authority?.identity?.explorer_base)?.authority.identity.explorer_base);
  const valid=typeof address==='string' && (chain==='solana'?/^[1-9A-HJ-NP-Za-km-z]{32,44}$/:/^0x[0-9a-fA-F]{40}$/).test(address);
  return valid && base ? `<a href="${esc(base)}${chain==='solana'?'account':'address'}/${encodeURIComponent(address)}" target="_blank" rel="noopener noreferrer">${code}</a>` : code;
}

function identitySection(row) {
  if (!row) return '';
  const labels={verified_account_address:'Verified account address',suspected_alternate:'Suspected alternate',funding_verified_link:'Funding verified link',related_trader:'Related trader',ownership_conflict:'Ownership conflict'};
  const base=row.explorer_base || (state.chain==='solana'?'https://solscan.io/':null);
  const receipt=(sig,label)=>typeof sig==='string' && (state.chain==='solana'?/^[1-9A-HJ-NP-Za-km-z]{64,88}$/:/^0x[0-9a-fA-F]{64}$/).test(sig)
    ? base?`<a href="${esc(base)}tx/${encodeURIComponent(sig)}" target="_blank" rel="noopener noreferrer">${label}</a>`:`${label}: <code>${esc(sig)}</code>` : 'Receipt unavailable';
  const addresses=(row.verified_addresses||[]).map(a=>`<p><b>Verified account address</b><br>${walletLink(a.address,a.chain,base)}<br><span class="note">${esc(a.evidence_kind?.replaceAll('_',' ')||'Current verified registry')}</span></p>`).join('');
  const conflicts=(row.address_conflicts||[]).map(a=>`<p><b>Ownership conflict</b><br>${walletLink(a.address,a.chain)}<br>${esc(a.owner_uids.join(', '))}</p>`).join('');
  const relations=(row.related_wallets||[]).map(r=>{
    const funding=(r.funding_clues||[]).filter(c=>c.kind==='verified_funding' && c.verified===true);
    const directional=Number.isInteger(r.before_coins);
    const timing=r.n_coins>0?`${directional?`Before main: ${esc(r.before_coins)} coins · After main: ${esc(r.after_coins)} coin${r.after_coins===1?'':'s'}<br>`:''}${esc(r.n_coins)} of ${esc(r.eligible_coins)} read coins · ${esc(r.unknown_coins)} unread · ${r.missed_coins==null?'Absences unknown':esc(r.missed_coins)+' without this address'}<br>${esc(r.days)} UTC days${r.lead_p50_s==null?'':` · lead p50 ${esc(r.lead_p50_s)}s / p90 ${esc(r.lead_p90_s)}s`}`:'No measured timing sample.';
    return `<article><h4>${esc(labels[r.status]||'Related trader')}</h4>${walletLink(r.address,r.chain,base)}<p class="note">Ownership unproven<br>${timing}${r.known_owner_uids?.length?'<br>Known accounts: '+esc(r.known_owner_uids.map(uid=>source.find(w=>w.id===uid)?.handle||uid).join(', ')):''}</p>${funding.map(c=>`<p><b>Funding verified · ${esc(c.amount??c.amount_sol)} ${esc(c.symbol||'SOL')}${c.hub?' · hub':''}</b><br>${walletLink(c.funder,r.chain,base)} → ${walletLink(c.recipient,r.chain,base)}<br>${receipt(c.funding_signature,'Funding receipt')}<br>Confirmed signed transfer; common control unproved.</p>`).join('')}<details><summary>Trade evidence</summary>${(r.evidence||[]).map(e=>`<p><code>${esc(e.mint)}</code><br>${esc(new Date(e.t0*1000).toISOString())} · ${e.direction==='after'?esc(Math.abs(e.lead_s))+'s after':esc(e.lead_s)+'s earlier'} · ${e.order?`buyer ${esc(e.order.join(' / '))} · main ${esc(e.source_order.join(' / '))}`:`slot ${esc(e.slot??'unknown')} / index ${esc(e.tx_index??'unknown')}`}<br>${receipt(e.buy_signature,'Candidate receipt')} · ${receipt(e.source_signature,'Source receipt')}</p>`).join('')}${r.evidence_omitted?'<p>'+esc(r.evidence_omitted)+' more supporting observations omitted.</p>':''}${(r.funding_clues||[]).filter(c=>c.kind!=='verified_funding').map(c=>`<p><b>Funding clue · ${c.kind==='shared_funder'?'shared funder':'initial credit'}</b>${c.hub?' · hub':''}<br>${walletLink(c.funder,r.chain,base)} → ${walletLink(c.recipient,r.chain,base)}<br>${receipt(c.funding_signature,'Funding receipt')}<br>Cached balance attribution; common control unproved.</p>`).join('')||(!funding.length?'<p>Funding evidence unavailable.</p>':'')}</details></article>`;
  }).join('');
  const coverage=row.links_method?`Stored 14-day sample, 60 seconds before and after exact main buys. Known roster peers included. ${esc(row.coverage?.sampled_coins??0)} coins sampled; ${esc(row.coverage?.unknown_coins??0)} unknown. Funding history ${esc(row.coverage?.funding_state||'unavailable')}. ${esc(row.coverage?.reason?.replaceAll('_',' ')||'')} Wider history remains unknown.`:'Stored 14-day Solana sample, 60 seconds before first buys. Known roster addresses are excluded from this intake; wider history remains unknown.';
  return `<section class="wu-sec wu-identity" aria-label="Account addresses and related wallets"><h3>Account addresses and related wallets</h3><p class="note">Research ${esc(row.state||'unavailable')} · ${esc(row.generated_at||'No artifact')}<br>Account attribution and possible common control are separate.</p>${addresses||'<p class="note">No verified account address on this chain.</p>'}${conflicts}${relations||`<p class="note">${row.related_wallets==null?'Related wallets unavailable.':row.state==='not_measured'?'Alternate wallets not measured on this chain.':'No related buyers in the stored sample.'}</p>`}${row.omitted?'<p class="note">'+esc(row.omitted)+' related addresses omitted.</p>':''}<p class="note">${coverage} ${row.coverage?.ordering_unknown_coins?esc(row.coverage.ordering_unknown_coins)+' coins have unknown same-second ordering.':''}${row.coverage?.capped?.length?' Intake capped: '+esc(row.coverage.capped.join(', '))+'.':''}</p></section>`;
}

function render() {
  const chain = chains.find(c => c[0] === state.chain);
  host.style.setProperty('--accent',chain[2]);
  host.querySelector('.chains').innerHTML = chains.map(c => `<button class="chain" data-chain="${c[0]}" aria-pressed="${state.chain===c[0]}" style="--c:${c[2]}"><i></i>${c[1]}</button>`).join('');
  host.querySelectorAll('[data-chain]').forEach(button => button.onclick = () => {
    if(state.chain===button.dataset.chain)return;
    if(options.beforeChain && !options.beforeChain(button.dataset.chain))return;   // W5b: a full comparison tray asks first
    state.chain = button.dataset.chain;
    pendingPush = true;                                    // a chain switch is a history entry (W4b)
    state.uid = ''; state.moon = null; state.camera = null; state.view = 'universe';
    pointers.clear(); pointerGesture = null; downTarget = null;
    data.body = {chain:state.chain,wallets:[]};
    state.loading = true;
    host.querySelector('#find').value = '';
    host.querySelector('#results').hidden = true;
    host.querySelector('#wallet-drawer').hidden = true;
    host.querySelector('#browse').setAttribute('aria-expanded','false');
    render(); options.onChain(state.chain);
  });
  source = rows();
  const sizeSelect=host.querySelector('[aria-label="Planet size metric"]');
  const valid={};
  for(const [key,title] of [['capital','Liquid capital'],['impact','Coin flow']]) {
    valid[key]=source.filter(w=>{const m=w.authority?.metrics?.[key];return m?.state==='ok' && Number.isFinite(key==='capital'?m.usd:m.value_usd);}).length;
    const option=sizeSelect.querySelector('[value="'+key+'"]');
    option.disabled=!valid[key];option.textContent=title+' · '+valid[key]+'/'+source.length+' measured';
    option.title=valid[key]?'':key==='capital'?'No fresh liquid-capital read on this chain yet (it needs a proven address)':'No complete coin-flow read on this chain yet';
  }
  // W5a: repeat buyers come from the Solana link graph; elsewhere the mode cannot measure anything.
  const following=sizeSelect.querySelector('[value="following"]');
  following.disabled=state.chain!=='solana' || !!data.stamp?.error;
  following.title=state.chain!=='solana'?'Repeat buyers are measured on Solana only (the link graph covers Solana)':data.stamp?.error?'Relationship graph unavailable: '+data.stamp.error:'';
  if(sizeSelect.querySelector('[value="'+state.metric+'"]')?.disabled)
    state.metric=['following','capital','impact'].find(k=>!sizeSelect.querySelector('[value="'+k+'"]').disabled)||'following';
  sizeSelect.value=state.metric;
  sizeSelect.onchange=()=>{state.metric=sizeSelect.value;render();};
  source.sort((a,b)=>(metricValue(b)??-1)-(metricValue(a)??-1)||b.n-a.n);
  nodes = source;
  if (!source.some(w => w.id === state.uid)) state.uid = source[0]?.id || '';
  const owner = source.find(w => w.id === state.uid);
  const peersObserved=data.observed || !!owner?.authority?.identity?.links_method;
  if (state.moon && !peersObserved) { restoreSat = state.moon; state.moon = null; }   // W4b: unavailable keeps it pending
  if (restoreSat && owner && peersObserved) {           // W4b: judged once, when the observations exist
    if (owner.regular_list.some(m => m.id === restoreSat)) state.moon = restoreSat;
    restoreSat = null;
  }
  if (state.moon && !owner?.regular_list.some(m => m.id === state.moon)) state.moon = null;
  host.querySelector('#sky-title').textContent = chain[1]+' universe';
  host.querySelector('#sky-caption').textContent = state.loading && !source.length ? 'Loading wallets…' : state.chain==='solana' ? `${source.length} wallets · click a planet to enter` : `${source.length} tracked wallets · ${source.some(w=>w.authority?.identity?.links_method)?'saved receipt links':'relationships not measured'}`;
  host.querySelector('#scope').textContent = state.chain==='solana' ? 'Nearer the center = earlier among decoded buyers in the same sampled first-buy slot. Size = measured repeat buyers. The outer belt has insufficient timing evidence. These observations do not prove ownership or causation.' : source.some(w=>w.authority?.identity?.links_method)?'Saved funding and exact before/after receipt links appear in wallet details. Timing ranks stay unmeasured; ownership is unproven.':'Entry order and relationships are not measured on this chain. Planets sit in an unranked discovery belt; size stays neutral.';
  const stamp=data.stamp || {};
  host.querySelector('.wu-live-status').textContent = options.status(data.body,stamp);
  // Each item stays whole; a narrow screen wraps between items (Gamma LOW on W5a).
  host.querySelector('.wu-availability').innerHTML = availabilityText(data.body, chain[1]).map(t => '<span>'+esc(t)+'</span>').join(' · ');
  const metricScope={following:'Size = measured repeat buyers.',capital:'Size = measured liquid funds on proven addresses, including native, wrapped native and trusted stables. Stablecoins valued at par; other holdings excluded.',impact:'Size = magnitude of median net quote flow in the 60 seconds after a source buy, valued at current snapshot prices. Ten complete distinct coins and 75% coverage required. Observed venues only; no causation or profit claim.'};
  host.querySelector('#scope').textContent=host.querySelector('#scope').textContent.replace(' Size = measured repeat buyers.','').replace('size stays neutral.','timing stays unranked.')+' '+metricScope[state.metric];
  host.querySelector('#focus-wallet').disabled = !owner;
  renderTable();
  const metricMax=Math.max(1,...source.map(w=>metricValue(w)??0));
  positions = new Map();
  for (const known of [true,false]) {
    const group = source.filter(w => ranked(w) === known);
    group.forEach((w,i) => {
      const radius = known ? 820 + w.entry_order.share_before*600 : 1640;
      const angle = (-90+i*360/group.length)*Math.PI/180;
      positions.set(w.id,{x:radius*Math.cos(angle),y:radius*Math.sin(angle),r:metricValue(w)==null?20:state.metric==='following'?20+Math.sqrt(metricValue(w))*9:20+65*Math.sqrt(metricValue(w)/metricMax),known});
    });
  }
  const related = state.chain !== 'solana' ? [] : state.moon ? source.filter(w => w.id!==state.uid && w.regular_list.some(m => m.id===state.moon)).map(w=>w.id) : (data.shared||[]).filter(e=>e.a===state.uid||e.b===state.uid).map(e=>e.a===state.uid?e.b:e.a);
  const shared = new Set(related);
  const colors = ['#9ddfd4','#a9bddc','#b8afdc','#d8c391','#9bbec8'];
  let svg = `<defs><radialGradient id="core-glow"><stop stop-color="${chain[2]}" stop-opacity=".13"/><stop offset="1" stop-color="${chain[2]}" stop-opacity="0"/></radialGradient>${colors.map((color,i)=>`<radialGradient id="planet-${i}" cx="30%" cy="25%" r="80%"><stop stop-color="${color}"/><stop offset=".35" stop-color="${color}" stop-opacity=".85"/><stop offset="1" stop-color="#1a3346"/></radialGradient>`).join('')}</defs><g class="world">`;
  for (let i=0;i<180;i++) svg += `<circle cx="${(i*511+37)%3600-1800}" cy="${(i*439+61)%3600-1800}" r="${i%7===0?2:1}" fill="#bfd6e5" opacity="${i%7===0?.2:.1}"/>`;
  svg += `<g class="universe-grid"><path class="cross" d="M0 -1850V1850M-1850 0H1850"/>${[360,820,1120,1420,1640].map((r,i)=>`<circle class="orbit ${i===4?'orbit-dash':''}" cx="0" cy="0" r="${r}"/>`).join('')}<circle r="560" fill="url(#core-glow)"/><g class="core" data-fixed-label data-x="0" data-y="0"><text class="title" y="17">${state.chain==='solana'?'Earlier entries':'Unranked'}</text><text class="sub" y="36">${state.chain==='solana'?'within sampled slots':'timing not measured'}</text></g></g>`;
  const origin = positions.get(state.uid);
  const moons = owner?.regular_list || [];
  const moonPosition = (m,i) => {
    const angle = (-130+i*360/moons.length)*Math.PI/180;
    return {x:origin.x+(origin.r+90)*Math.cos(angle),y:origin.y+(origin.r+90)*Math.sin(angle)};
  };
  if (origin) {
    const anchor = state.moon ? moonPosition(moons.find(m=>m.id===state.moon),moons.findIndex(m=>m.id===state.moon)) : origin;
    for (const uid of shared) {
      const p = positions.get(uid);
      if (p) svg += `<path class="link" d="M${anchor.x} ${anchor.y} Q0 0 ${p.x} ${p.y}"/>`;
    }
  }
  nodes.forEach((w,i)=>{
    const p = positions.get(w.id), active = w.id===state.uid;
    svg += `<g class="planet ${active?'selected':''} ${shared.has(w.id)?'related':''}" data-node="${esc(w.id)}" tabindex="0" role="button" aria-label="${esc(label(w))}, ${esc(metricText(w))}"><title>${esc(label(w))} · click to explore</title><circle class="hit" cx="${p.x}" cy="${p.y}" r="${p.r+7}"/><circle class="focus-ring" cx="${p.x}" cy="${p.y}" r="${p.r+8}" fill="none" stroke="${chain[2]}" stroke-width="2" stroke-opacity="${active?.8:0}"/>${metricValue(w)==null?`<circle class="planet-body unknown" cx="${p.x}" cy="${p.y}" r="${p.r}" fill="none" stroke="${colors[i%5]}" stroke-opacity=".75" stroke-dasharray="5 5"/>`:`<circle class="planet-body" cx="${p.x}" cy="${p.y}" r="${p.r}" fill="url(#planet-${i%5})" stroke="${colors[i%5]}" stroke-opacity=".3"/>`}<ellipse cx="${p.x}" cy="${p.y}" rx="${p.r*1.05}" ry="${p.r*.23}" fill="none" stroke="${colors[i%5]}" stroke-opacity=".17" transform="rotate(-25 ${p.x} ${p.y})"/><g class="planet-label-block" data-fixed-label data-x="${p.x}" data-y="${p.y+p.r}"><rect class="label-hit" x="${-Math.min(180,label(w).length*7+18)/2}" y="7" width="${Math.min(180,label(w).length*7+18)}" height="${active?38:24}" fill="transparent"/><text class="planet-label" text-anchor="middle" y="22">${esc(label(w))}</text>${active?`<text class="selection-tag" text-anchor="middle" y="39">${esc(metricText(w))}</text>`:''}</g></g>`;
  });
  if (origin && moons.length) {
    svg += `<circle class="satellite-orbit orbit orbit-dash" cx="${origin.x}" cy="${origin.y}" r="${origin.r+90}"/>`;
    moons.forEach((m,i)=>{
      const p = moonPosition(m,i);
      const systems = source.filter(w=>w.regular_list.some(v=>v.id===m.id)).length;
      svg += `<g class="moon ${state.moon===m.id?'selected':''}" data-moon="${esc(m.id)}" tabindex="0" role="button" aria-label="${esc(short(m.id))}, repeat buyer on ${m.k} of ${m.eligible_coins??owner.n} sampled coins"><title>${esc(m.id)} · ${m.k}/${m.eligible_coins??owner.n} coins · repeat buyer of ${systems} wallet${systems===1?'':'s'}</title><circle class="hit" cx="${p.x}" cy="${p.y}" r="22"/><circle class="moon-body" cx="${p.x}" cy="${p.y}" r="${state.moon===m.id?9:systems>1?7:5}" fill="${state.moon===m.id?'#e5edf6':chain[2]}"/><circle cx="${p.x}" cy="${p.y}" r="${systems>1?12:9}" fill="none" stroke="${chain[2]}" stroke-opacity=".25"/><g data-fixed-label data-x="${p.x}" data-y="${p.y}"><text class="moon-label" x="15" y="4">${esc(short(m.id))}</text></g></g>`;
    });
  }
  graph.innerHTML = svg+'</g>';
  world = graph.querySelector('.world');
  const detail = host.querySelector('.inspector'), same = !!state.uid && state.uid === lastInspected;
  const active = same && detail.contains(document.activeElement) ? document.activeElement : null;
  const focusSel = active?.id ? '#'+CSS.escape(active.id) : null;
  const activeSummary = !focusSel && active ? active.closest('summary') : null;     // a summary has no id
  const focusFold = activeSummary ? foldIds(detail).find(([d]) => d === activeSummary.parentElement)?.[1] : null;
  const scrollTop = detail.scrollTop;
  clearTimeout(holdTimer);
  detail.style.minHeight = same ? detail.offsetHeight+'px' : '';
  if (same) holdTimer = setTimeout(() => { detail.style.minHeight = ''; }, 3000);   // the dossier arrives async
  inspector(owner,shared);
  if(owner && !state.moon)options.onInspect(owner.id,host.querySelector('.inspector'));
  if (same) {
    detail.scrollTop = scrollTop;
    const target = focusSel ? detail.querySelector(focusSel)
      : focusFold ? foldIds(detail).find(([, id]) => id === focusFold)?.[0].querySelector(':scope > summary') : null;
    target?.focus({preventScroll: true});
  }
  lastInspected = state.uid;
  graphAge();
  host.querySelector('#wallet-drawer').innerHTML = source.map(w=>`<button data-browse="${esc(w.id)}" aria-current="${w.id===state.uid}"><span>${esc(label(w))}</span><small>${w.regulars??'—'} repeat buyers</small></button>`).join('');
  host.querySelectorAll('[data-browse]').forEach(button=>button.onclick=()=>{
    host.querySelector('#wallet-drawer').hidden=true;
    host.querySelector('#browse').setAttribute('aria-expanded','false');
    select(button.dataset.browse);
  });
  host.querySelector('#previous-wallet')?.setAttribute('aria-label','Previous in list');
  host.querySelector('#next-wallet')?.setAttribute('aria-label','Next in list');
  if (syncUrl(pendingPush ? 'push' : 'replace')) pendingPush = false;
  resize();
}
function inspector(w,shared){const detail=host.querySelector('.inspector');if(!w){detail.innerHTML=state.loading?'<h2>Loading wallets…</h2>':'<h2>No wallets observed</h2>';return;}if(state.moon){const m=w.regular_list.find(m=>m.id===state.moon);const systems=source.filter(n=>n.regular_list.some(v=>v.id===state.moon));detail.innerHTML=`<button class="back" id="back">Back to ${esc(label(w))}</button><div class="kind" style="margin-top:23px"><i></i>Repeat buyer</div><h2>${esc(short(m.id))}</h2><div class="identity">${walletLink(m.id)}</div><p class="description">${m.identity?`Saved receipt link to ${esc(label(w))}. Before main: ${m.identity.before_coins??m.identity.n_coins} coins · After main: ${m.identity.after_coins??'unknown'} coins · ${m.k} of ${m.eligible_coins} sampled coins.`:`Buys after ${esc(label(w))} in its first-buy slot on <b>${m.k} of ${w.n} sampled coins</b>.`}</p><div class="facts"><div><strong>${systems.length}</strong><span>${m.identity?'Wallets with this saved link':'Wallets it repeat-buys after'}</span></div><div><strong class="unmeasured">Unproven</strong><span>Owner / bot identity</span></div></div><button class="inspect-action" id="show-connections">See all connections on the map</button>${m.identity?identitySection({...w.authority.identity,related_wallets:[m.identity]}):''}<h3>Linked wallets</h3><div class="rel-list">${systems.map(n=>`<button class="rel" data-system="${esc(n.id)}"><span class="rel-dot"></span><span class="rel-name">${esc(label(n))}</span><span class="rel-n">${n.regular_list.find(v=>v.id===m.id).k}/${n.regular_list.find(v=>v.id===m.id).eligible_coins??n.n} coins</span></button>`).join('')}</div><p class="note">A shared trading pattern is evidence of a link, not proof of who controls this address.</p>`;detail.querySelector('#back').onclick=()=>select(state.uid);detail.querySelectorAll('[data-system]').forEach(b=>b.onclick=()=>select(b.dataset.system));detail.querySelector('#show-connections').onclick=fitConnections;return;}
const cap=w.authority?.metrics?.capital||{};const flow=w.authority?.metrics?.impact||{};
const capText=cap.state==='ok'?money(cap.usd):cap.state==='stale'?'Stale read':cap.state==='address_unproven'?'Address unproven':'Collecting';
const flowText=flow.state==='ok'?(flow.value_usd>=0?'+':'−')+money(flow.value_usd):`${flow.complete||0}/${flow.eligible||0} complete coins`;
const reads=['ok','insufficient','incomplete','unpriced'].includes(flow.state)?(flow.recent||[]).filter(r=>r.status==='complete'):[];
const flowReads=reads.length?`<details class="wu-flow-reads"><summary>Measured coin reads</summary>${reads.map(r=>`<p class="note"><b>${esc(short(r.mint))}</b> · ${typeof r.value_usd==='number' && Number.isFinite(r.value_usd)?(r.value_usd>=0?'+':'−')+money(r.value_usd):esc((r.quotes||[]).map(quoteUnits).join(' · ')||'No other swaps observed')+' · USD unavailable'}<br>${esc(Number.isFinite(r.t0)?new Date(r.t0*1000).toISOString().slice(0,19).replace('T',' ')+'Z':'')} · 60 seconds after buy${r.source_attribution==='beneficiary_only'?'<br>Relayed purchase received; initiator unproven. Third-party payment possible.':r.source_attribution==='routed_payment'?'<br>Wallet payment verified through router; router ownership unproven.':''}</p>${flowWindows(r)}`).join('')}<p class="note">14-day source-flow census; first observed buy per wallet/coin. Source and known aliases excluded on Solana; source and own transactions excluded on EVM. Observed venues only. Buyer counts with + are lower bounds. Default-qualified entries appear in the 30-day dossier.</p></details>`:'';
const measured=state.chain==='solana' && w.read != null;const near=ranked(w);const satellites=w.regular_list||[];const links=[...shared].map(id=>source.find(n=>n.id===id)).filter(Boolean);
// W3 summary order (spec 2026-10-07): observation, sample and age, limitation, supporting evidence;
// wallet_map.js appends the next step, execution research, live authority and the collapsed rest.
const graphInfo=data.body?.graph||{}, stamp=data.stamp||{};
const stale=graphInfo.state==='stale' || stamp.stale===true;
const observation=state.chain!=='solana'?`Relationships are not measured on ${chainName()}; the link graph covers Solana only.`
  :stamp.error?'Relationship observations are unavailable: '+stamp.error+'.'
  :!measured?'No read first buys for this wallet yet.'
  :w.regulars==null?`${w.n} read coins: too few to name repeat buyers (10 needed); another decoded buyer landed after it in the same slot on ${w.drew}/${w.n}.`
  :`${w.regulars} repeat buyer${w.regulars===1?'':'s'} across ${w.n} read coins; another decoded buyer landed after it in the same slot on ${w.drew}/${w.n}.`;
const runAt=graphInfo.run_finished_at||stamp.computed_at;
const sample=state.chain!=='solana'?'No relationship sample on this chain.'
  :`Roster wallets' first buys we read on Solana · n = ${measured?w.n:0} read coins${measured?` (${w.read} of ${w.units} first buys read)`:''} · window ${stamp.window_from?esc(String(stamp.window_from).slice(0,10))+' to '+esc(String(stamp.window_to).slice(0,10)):'unknown'} · graph run ${runAt?esc(String(runAt).slice(0,16).replace('T',' '))+'Z':'unknown'}${stale?' · <b>stale: older than 3 h</b>':''}`;
const limitation=state.chain!=='solana'?'Without a relationship graph nothing here says who this wallet trades with.'
  :'Sampled same-slot order among decoded buyers only, a lower bound; ownership and causation unproved.';
const steps=visibleRows().shown.map(r=>r.w.id),at=steps.indexOf(w.id),still=steps.length<2&&(at>=0||!steps.length)?' disabled':'';
detail.innerHTML=`<div class="wallet-steps"><button id="previous-wallet" aria-label="Previous wallet"${still}>‹</button><span>${at<0?'–':at+1} / ${steps.length} wallets</span><button id="next-wallet" aria-label="Next wallet"${still}>›</button></div><div class="kind"><i></i>${w.authority?.effective?'Armed':w.authority?.paused?'Paused':'Observed wallet'} · ${esc(chains.find(c=>c[0]===state.chain)[1])}</div><h2>${esc(label(w))}</h2><div class="identity">${esc(w.id)}</div>
<section class="wu-sec" aria-label="Observation"><h3>Observation</h3><p class="wu-observation">${esc(observation)}</p></section>
<section class="wu-sec" aria-label="Sample and age"><h3>Sample and age</h3><p class="note">${sample}</p></section>
<section class="wu-sec" aria-label="Limitation"><h3>Limitation</h3><p class="note">${esc(limitation)}</p></section>
${identitySection(w.authority?.identity)}
<section class="wu-sec" id="wu-supporting" aria-label="Supporting evidence"><h3>Supporting evidence</h3><div class="facts"><div><strong>${w.regulars??'—'}</strong><span>Repeat buyers${measured?` · of ${w.n} read coins`:''}</span></div><div><strong>${measured?w.members:'—'}</strong><span>Buyers seen after it${measured?` · in ${w.n} read coins`:''}</span></div><div><strong class="${cap.state==='ok'?'':'unmeasured'}">${esc(capText)}</strong><span>Liquid capital${cap.state==='ok'&&Number.isInteger(cap.addresses)?` · ${cap.addresses} proven address${cap.addresses===1?'':'es'}`:''}</span></div><div><strong class="${flow.state==='ok'?'':'unmeasured'}">${esc(flowText)}</strong><span>Coin post-buy flow${flow.state==='ok'?` · ${flow.complete||0} of ${flow.eligible||0} first buys`:''}</span></div></div><p class="note">Liquid funds read ${esc(cap.observed_at?String(cap.observed_at).slice(0,16).replace('T',' ')+'Z':'pending')} · Native, wrapped native and trusted stables; excludes other holdings. ${cap.samples?.some(s=>s.last_failure)?'Latest refresh failed; previous timestamp retained.':''}</p><p class="note">Coin flow: ${flow.complete||0} complete of ${flow.eligible||0} eligible first buys · ${esc(flow.state||'collecting')}${flow.details?' · '+esc(Object.entries(flow.details).map(([k,n])=>n+' '+k.replaceAll('_',' ')).join(', ')):''}. Quote flow is descriptive; USD uses current snapshot prices.</p>${flowReads}<h4>${near?'Entry order':'Entry timing not ranked'}</h4><p class="note">${near?`Median ${Math.round(w.entry_order.share_before*100)}% of decoded peers bought earlier, across ${w.entry_order.n} sampled coins. Same-slot ordering only.`:'At least ten usable sampled coins are needed to place this wallet by entry order.'}</p><button class="inspect-action" id="inspect-focus">Show on the map</button><h4>Repeat buyers${satellites.length?' · '+satellites.length:''}</h4><div class="rel-list">${satellites.map(m=>`<button class="rel" data-detail-moon="${esc(m.id)}"><span class="rel-dot"></span><span class="rel-name">${esc(short(m.id))}</span><span class="rel-n">${m.k}/${m.eligible_coins??w.n} coins</span></button>`).join('')||`<p class="note">${w.regulars==null?'Repeat buyers not measured.':'No repeat buyers in the sampled coins.'}</p>`}</div>${links.length?`<h4>Shared repeat buyers with · ${links.length} wallet${links.length===1?'':'s'}</h4><div class="rel-list">${links.map(n=>`<button class="rel" data-link="${esc(n.id)}"><span class="rel-name">${esc(label(n))}</span><span class="rel-n">${data.shared.find(e=>(e.a===w.id&&e.b===n.id)||(e.b===w.id&&e.a===n.id)).count} shared</span></button>`).join('')}</div>`:''}<div data-coins></div></section>`;
detail.querySelector('#inspect-focus').onclick=()=>{setPane('map');focusWallet(true);};detail.querySelector('#previous-wallet').onclick=()=>stepWallet(-1);detail.querySelector('#next-wallet').onclick=()=>stepWallet(1);detail.querySelectorAll('[data-detail-moon]').forEach(b=>b.onclick=()=>{state.moon=b.dataset.detailMoon;render();focusWallet(true);});detail.querySelectorAll('[data-link]').forEach(b=>b.onclick=()=>select(b.dataset.link));}

// Pointer capture keeps drags and two-finger gestures alive outside the SVG.
// A moved or multi-pointer gesture suppresses the click that follows pointerup.
graph.addEventListener('pointerdown', event => {
  if (event.button !== 0 && event.pointerType !== 'touch') return;
  const point = svgPoint(event);
  downTarget = event.target.closest('[data-node],[data-moon]');
  pointers.set(event.pointerId,point);
  graph.setPointerCapture(event.pointerId);
  host.querySelector('#hover-info').hidden=true;
  if (pointers.size === 1) pointerGesture = {startCamera:{...state.camera},startPoint:point,moved:false,multi:false};
  if (pointers.size === 2) {
    const [a,b] = [...pointers.values()];
    pointerGesture = {startCamera:{...state.camera},startMid:{x:(a.x+b.x)/2,y:(a.y+b.y)/2},startPair:[a,b],moved:true,multi:true};
  }
});

graph.addEventListener('pointermove', event => {
  if (!pointers.has(event.pointerId)) {
    const target = event.target.closest('[data-node],[data-moon]');
    const hint = host.querySelector('#hover-info');
    if (!target) { hint.hidden=true; return; }
    const w = source.find(w=>w.id===(target.dataset.node||state.uid));
    const moon = target.dataset.moon && w.regular_list.find(m=>m.id===target.dataset.moon);
    hint.innerHTML = moon ? `${esc(short(moon.id))}<span>${moon.k}/${moon.eligible_coins??w.n} sampled coins · click to see connections</span>` : `${esc(label(w))}<span>${w.regulars??'Unknown'} repeat buyers · click to enter this system</span>`;
    hint.hidden=false;
    return;
  }
  event.preventDefault();
  const point = svgPoint(event);
  pointers.set(event.pointerId,point);
  if (pointers.size === 2 && pointerGesture.startMid) {
    const [a,b] = [...pointers.values()];
    state.camera = pinchCamera(pointerGesture.startCamera,pointerGesture.startPair,[a,b],viewport,limits());
    pointerGesture.moved=true;
  } else if (pointers.size === 1 && pointerGesture.startPoint) {
    const dx = point.x-pointerGesture.startPoint.x, dy = point.y-pointerGesture.startPoint.y;
    if (Math.hypot(dx,dy)>5) pointerGesture.moved=true;
    if (!pointerGesture.moved) return;
    state.camera = panBy(pointerGesture.startCamera,dx,dy);
  } else return;
  graph.classList.add('dragging');
  if (state.view==='universe') state.view='custom';
  const origin=positions.get(state.uid);
  if(state.view==='system'&&origin&&Math.hypot(state.camera.x-origin.x,state.camera.y-origin.y)>origin.r+210)state.view='custom';
  applyCamera();
},{passive:false});

function endPointer(event) {
  if (pointerGesture?.moved || pointerGesture?.multi || event.type==='pointercancel') suppressClickUntil=performance.now()+350;
  pointers.delete(event.pointerId);
  if (graph.hasPointerCapture(event.pointerId)) graph.releasePointerCapture(event.pointerId);
  if (pointers.size === 2) {
    const [a,b]=[...pointers.values()];
    pointerGesture={startCamera:{...state.camera},startMid:{x:(a.x+b.x)/2,y:(a.y+b.y)/2},startPair:[a,b],moved:true,multi:true};
  } else if (pointers.size === 1) {
    const point = [...pointers.values()][0];
    pointerGesture = {startCamera:{...state.camera},startPoint:point,moved:true,multi:true};
  } else if (!pointers.size) {
    pointerGesture=null;
    graph.classList.remove('dragging');
  }
  if (event.type==='pointercancel') downTarget=null;
}
graph.addEventListener('pointerup',endPointer);
graph.addEventListener('pointercancel',endPointer);
graph.addEventListener('lostpointercapture',event=>{
  // Normal pointerup already removed this pointer. An unexpected loss is a cancellation.
  if (pointers.has(event.pointerId)) endPointer({pointerId:event.pointerId,type:'pointercancel'});
});
graph.addEventListener('pointerleave',()=>{host.querySelector('#hover-info').hidden=true;wheelArmed=false;});

graph.addEventListener('click',event=>{
  wheelArmed = true;                    // a click on the map arms wheel zoom until the pointer leaves
  const target = event.target.closest('[data-node],[data-moon]') || downTarget;
  downTarget=null;
  if (performance.now()<suppressClickUntil) return;
  if (target?.dataset.node) { select(target.dataset.node); return; }
  if (target?.dataset.moon) { state.moon=target.dataset.moon;render();return; }
  const point=worldAt(state.camera,svgPoint(event),viewport);
  const nearest=source.filter(w=>state.view!=='system'||w.id===state.uid).map(w=>({w,p:positions.get(w.id)})).sort((a,b)=>Math.hypot(a.p.x-point.x,a.p.y-point.y)-Math.hypot(b.p.x-point.x,b.p.y-point.y))[0];
  if (nearest && Math.hypot(nearest.p.x-point.x,nearest.p.y-point.y)*state.camera.k<=18) select(nearest.w.id);
});

graph.addEventListener('dblclick',event=>{
  event.preventDefault();
  const target=event.target.closest('[data-node]');
  if (target) select(target.dataset.node);
  else changeZoom(1.8,svgPoint(event));
});

graph.addEventListener('wheel',event=>{
  if (!(event.ctrlKey || event.metaKey || wheelArmed)) return;      // an ordinary wheel scrolls the page
  event.preventDefault();
  const delta = event.deltaY*(event.deltaMode===1?16:event.deltaMode===2?viewport.height:1);
  changeZoom(Math.exp(-Math.max(-120,Math.min(120,delta))*.0035),svgPoint(event));
},{passive:false});

graph.addEventListener('keydown',event=>{
  if(event.key==='Escape'&&state.expanded){event.preventDefault();event.stopPropagation();toggleExpanded();return;}
  const target=event.target.closest('[data-node],[data-moon]');
  if (target && (event.key==='Enter'||event.key===' ')) {
    event.preventDefault();
    if (target.dataset.node) select(target.dataset.node);
    else { state.moon=target.dataset.moon;render(); }
    return;
  }
  if (event.key==='+'||event.key==='=') {event.preventDefault();changeZoom(1.25);}
  else if (event.key==='-') {event.preventDefault();changeZoom(.8);}
  else if (event.key==='Home'||event.key==='Escape') {event.preventDefault();fitUniverse();}
  else if (event.key.startsWith('Arrow')) {
    const delta={ArrowLeft:[80,0],ArrowRight:[-80,0],ArrowUp:[0,80],ArrowDown:[0,-80]}[event.key];
    if (delta) {event.preventDefault();state.camera=panBy(state.camera,...delta);if(state.view==='universe')state.view='custom';applyCamera();}
  }
});

host.querySelector('#zoom-in').onclick=()=>changeZoom(1.3);
host.querySelector('#zoom-out').onclick=()=>changeZoom(1/1.3);
host.querySelector('#zoom-reset').onclick=fitUniverse;
host.querySelector('#focus-wallet').onclick=()=>focusWallet(true);
host.querySelector('#browse').onclick=()=>{
  const drawer=host.querySelector('#wallet-drawer');
  drawer.hidden=!drawer.hidden;
  host.querySelector('#browse').setAttribute('aria-expanded',String(!drawer.hidden));
};
function toggleExpanded() {
  state.expanded=!state.expanded;
  if (!state.expanded) host.querySelector('.main').classList.remove('details-open');
  host.querySelector('.main').classList.toggle('expanded',state.expanded);
  document.body.classList.toggle('wallet-universe-expanded',state.expanded);
  const button=host.querySelector('#full-screen');
  button.setAttribute('aria-label',state.expanded?'Exit expanded map':'Expand map');
  button.textContent=state.expanded?'↙':'↗';
}
host.querySelector('#full-screen').onclick=toggleExpanded;

// Table | Map (W2): the table is the landing view; the map keeps its own camera while hidden.
function setPane(pane) {
  state.pane=pane;
  host.querySelector('.wu-table').hidden=pane!=='table';
  host.querySelector('.sky').hidden=pane!=='map';
  host.querySelector('#pane-table').setAttribute('aria-pressed',String(pane==='table'));
  host.querySelector('#pane-map').setAttribute('aria-pressed',String(pane==='map'));
  syncUrl('replace');
}
host.querySelector('#pane-table').onclick=()=>setPane('table');
host.querySelector('#pane-map').onclick=()=>setPane('map');
const narrow=window.matchMedia('(max-width:1099px)');
host.querySelector('.wu-table').addEventListener('click',event=>{
  const head=event.target.closest('[data-sort-key]');
  if(head){const key=head.dataset.sortKey;state.sort={key,dir:state.sort?.key===key?-state.sort.dir:key==='wallet'?1:-1};renderTable();syncUrl('replace');return;}
  const row=event.target.closest('tr[data-uid]');
  if(!row)return;
  select(row.dataset.uid);
  if(narrow.matches)host.querySelector('.inspector').scrollIntoView({block:'start'});   // stacked: the summary sits below
});
const readFilters=()=>{
  const v=id=>host.querySelector(id);
  const next={min:Math.max(0,Number(v('#wu-f-min').value)||0),ranked:v('#wu-f-ranked').value,measured:v('#wu-f-measured').value,
              armed:v('#wu-f-armed').value,funded:v('#wu-f-funded').checked};
  // An input's `change` on blur repeats the `input` it follows. Rebuilding the table then replaced
  // the header under the click that caused the blur, so the first sort click after typing was lost.
  if(JSON.stringify(next)===JSON.stringify(state.filters))return;
  state.filters=next;
  renderTable();
  syncUrl('replace');
};
host.querySelector('.wu-filters').addEventListener('input',readFilters);
host.querySelector('.wu-filters').addEventListener('change',readFilters);
// Narrow expanded mode: the summary opens as an overlay; closing it returns focus to the map node.
function setDetails(open) {
  host.querySelector('.main').classList.toggle('details-open',open);
  host.querySelector('#details').setAttribute('aria-expanded',String(open));
  if (open) { host.querySelector('.inspector').focus(); return; }
  const key=state.moon?`[data-moon="${CSS.escape(state.moon)}"]`:`[data-node="${CSS.escape(state.uid)}"]`;
  (graph.querySelector(key)||graph).focus();
}
host.querySelector('#details').onclick=()=>setDetails(true);
host.querySelector('#details-close').onclick=()=>setDetails(false);
document.addEventListener('keydown',event=>{
  if(event.key!=='Escape')return;
  if(host.querySelector('.main.details-open')){event.preventDefault();setDetails(false);}
  else if(state.expanded){event.preventDefault();toggleExpanded();}
});

host.querySelector('#find').oninput=event=>{
  const q=event.target.value.trim().toLowerCase(),results=host.querySelector('#results');
  if (!q) {results.hidden=true;return;}
  const matches=source.filter(w=>([label(w),w.id,...(w.authority?.identity?.verified_addresses||[]).map(a=>a.address),...(w.authority?.identity?.related_wallets||[]).map(a=>a.address)].join(' ')).toLowerCase().includes(q)).slice(0,6);
  const foundMoons=new Map();
  source.forEach(w=>w.regular_list.forEach(m=>{if(m.id.toLowerCase().includes(q)&&!foundMoons.has(m.id))foundMoons.set(m.id,{...m,parent:w.id});}));
  results.innerHTML=matches.map(w=>`<button data-result="${esc(w.id)}">${esc(label(w))}</button>`).join('')+[...foundMoons.values()].slice(0,3).map(m=>`<button data-parent="${esc(m.parent)}" data-result-moon="${esc(m.id)}">${esc(short(m.id))} · repeat buyer</button>`).join('')||'<button disabled>No matching wallet on this chain</button>';
  results.hidden=false;
  results.querySelectorAll('button:not(:disabled)').forEach(button=>button.onclick=()=>{
    select(button.dataset.result||button.dataset.parent);
    if (button.dataset.resultMoon) {state.moon=button.dataset.resultMoon;render();focusWallet();}
    results.hidden=true;
    event.target.value='';
  });
};
host.querySelector('#find').onkeydown=event=>{
  if (event.key==='Escape') host.querySelector('#results').hidden=true;
  if (event.key==='ArrowDown') {event.preventDefault();host.querySelector('#results button:not(:disabled)')?.focus();}
};
document.addEventListener('click',event=>{
  if (!event.target.closest('.search')) host.querySelector('#results').hidden=true;
  if (!event.target.closest('#wallet-drawer,#browse')) {
    host.querySelector('#wallet-drawer').hidden=true;
    host.querySelector('#browse').setAttribute('aria-expanded','false');
  }
});

const inspectorEl = host.querySelector('.inspector');
// Folds the redraw reopened from openFolds (the MutationObserver below): their toggle is not the
// reader's, so it never writes the URL after a restore (S3).
const autoOpened = new WeakSet();
inspectorEl.addEventListener('toggle', event => {
  if (event.target.tagName !== 'DETAILS' || !state.uid) return;
  if (autoOpened.has(event.target)) { autoOpened.delete(event.target); return; }
  const id = foldIds(inspectorEl).find(([d]) => d === event.target)?.[1];
  if (!id) return;
  const keys = openFolds.get(state.uid) || new Set();
  if (event.target.open) keys.add(id); else keys.delete(id);
  openFolds.set(state.uid, keys);
  syncUrl('replace');
}, true);
// The fold summary the reader focused, as (summary, nth). Content that lands after a redraw (the
// dossier, the coins) replaces it; once a fold with the same id exists, focus returns to it, but only
// while focus sits on <body>, i.e. it was lost to the redraw rather than moved by the reader (W3).
let focusedFold = null;
inspectorEl.addEventListener('focusin', event => {
  const summary = event.target.closest?.('details > summary');
  const id = summary && foldIds(inspectorEl).find(([d]) => d === summary.parentElement)?.[1];
  focusedFold = id ? {uid: state.uid, id, el: summary} : null;
});
inspectorEl.addEventListener('focusout', event => {
  const el = event.target;
  queueMicrotask(() => { if (focusedFold?.el === el && el.isConnected && document.activeElement !== el) focusedFold = null; });
});
new MutationObserver(() => {
  const keys = openFolds.get(state.uid);
  if (keys) foldIds(inspectorEl).forEach(([d, id]) => { if (!d.open && keys.has(id)) { autoOpened.add(d); d.open = true; } });
  const f = focusedFold;
  if (!f || f.el.isConnected || f.uid !== state.uid || (document.activeElement && document.activeElement !== document.body)) return;
  const summary = foldIds(inspectorEl).find(([, id]) => id === f.id)?.[0].querySelector(':scope > summary');
  if (summary) { f.el = summary; summary.focus({preventScroll: true}); }
}).observe(inspectorEl, {childList: true, subtree: true});

function graphAge() {
  const el = host.querySelector('#graph-age');
  if (state.chain !== 'solana') { el.textContent = 'relationships not measured'; return; }
  const at = data.body?.graph?.run_finished_at || data.stamp?.computed_at;
  const t = at ? Date.parse(at) : NaN;
  if (!Number.isFinite(t)) { el.textContent = 'graph age unknown'; return; }
  const min = Math.max(0, Math.floor((Date.now() - t) / 60000));
  el.textContent = 'graph ' + (min < 1 ? 'under 1 min' : min < 120 ? min + ' min' : Math.floor(min / 60) + ' h') + ' old';
}
setInterval(graphAge, 30000);
host.querySelector('#map-refresh').onclick = () => options.onRefresh?.();

// W4b: the URL is restored at mount and on Back/Forward, and written by syncUrl only.
let pendingPush = false, restoring = false, restoreSat = null, restoreMetric = null;
function setFilterInputs() {
  const f = state.filters;
  host.querySelector('#wu-f-min').value = String(f.min || 0);
  host.querySelector('#wu-f-ranked').value = f.ranked; host.querySelector('#wu-f-measured').value = f.measured;
  host.querySelector('#wu-f-armed').value = f.armed; host.querySelector('#wu-f-funded').checked = !!f.funded;
}
function applyUrlState(s) {
  state.pane = s.view;
  state.sort = s.sort;
  state.filters = s.filters || {min:0,ranked:'',measured:'',armed:'',funded:false};
  setFilterInputs();
  restoreSat = s.sat;
  restoreMetric = s.size;
  // A restored entry REPLACES the open folds (S3, Codex review 2026-10-07): merged, a fold absent
  // from the entry stayed open from before, and its toggle wrote it back into the restored URL.
  openFolds.clear();
  for (const [uid, ids] of Object.entries(s.folds)) openFolds.set(uid, new Set(ids));
}
// Skipped before data, while loading and after a failed read: a half-loaded or empty view must never
// overwrite a deep link.
function syncUrl(mode) {
  if (restoring || !data.body || state.loading || data.failed) return false;
  const url = new URL(location.href);
  writeUrlParams(url.searchParams, {chain: state.chain, view: state.pane, wallet: state.uid,
    sat: state.moon || restoreSat, size: state.metric, sort: state.sort, filters: state.filters,
    compare: options.compare ? options.compare() : [],
    folds: encodeFolds(new Map([...openFolds].filter(([uid]) => source.some(w => w.id === uid))), state.uid)});
  if (url.href !== location.href) history[mode === 'push' ? 'pushState' : 'replaceState'](history.state, '', url);
  return true;
}
applyUrlState(readUrlState(location.search));
setPane(state.pane);
window.addEventListener('popstate', () => {
  const s = readUrlState(location.search);
  restoring = true;
  try {
    applyUrlState(s);
    setPane(s.view);
    options.onHistory?.(s);
    if (s.chain && s.chain !== state.chain) {            // the chain's data loads first; update() restores
      options.initialWallet = s.wallet;
      return;
    }
    if (s.size) state.metric = s.size;
    if (s.wallet) state.uid = s.wallet;
    state.moon = null;
    render();
  } finally { restoring = false; }
});

const observer = new ResizeObserver(resize);
observer.observe(graph);
return {update(body,observations) {
  const changed = state.chain !== body.chain || !data.body;
  const colors=new Map(chains.map(c=>[c[0],c[2]]));
  if(body.chains?.length)chains=body.chains.map(c=>[c.id,c.label,colors.get(c.id)||'#9bbec8']);
  if(!chains.some(c=>c[0]===body.chain))chains.push([body.chain,body.chain,'#9bbec8']);
  data.body = body;
  data.herders = observations?.stamp?.error ? [] : observations?.herders || [];
  data.observed = observations != null && !observations.stamp?.error;   // unavailable cannot judge a satellite
  data.failed = false;
  data.shared = observations?.shared || [];
  data.stamp = observations?.stamp || {};
  state.loading = false;
  state.chain = body.chain;
  if(changed){state.uid='';state.moon=null;state.camera=null;state.view='universe';}
  if(options.initialWallet){state.uid=options.initialWallet;options.initialWallet=null;}
  if(restoreMetric){state.metric=restoreMetric;restoreMetric=null;}      // W4b: render keeps it only if measurable
  render();
  if(changed && mobile.matches)focusWallet();
}, loading(chain) {
  state.loading = true;
  if (!source.length) render();
  host.querySelector('.wu-live-status').textContent='Loading '+(chains.find(c=>c[0]===chain)?.[1]||chain)+' wallets…';
}, error(message) {
  state.loading = false;
  host.querySelector('.wu-live-status').textContent=message;
  if (state.uid) options.initialWallet = options.initialWallet || state.uid;   // W4b: the next good draw restores them
  if (state.moon) restoreSat = restoreSat || state.moon;
  data.body={chain:state.chain,wallets:[]}; data.failed=true;
  state.uid='';state.moon=null;state.camera=null;render();
  host.querySelector('.wu-live-status').textContent=message;
}};

}
const api={worldAt,zoomAt,panBy,pinchCamera,fitPoints,mount,readUrlState,encodeFolds,chainId};
if(typeof module!=='undefined'&&module.exports)module.exports=api;
root.WalletUniverse=api;
})(typeof window!=='undefined'?window:globalThis);
