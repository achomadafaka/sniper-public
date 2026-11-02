/* Original pixel Office: published observations only, no action authority. */
(function (root) {
  'use strict';
  const finite = v => typeof v === 'number' && Number.isFinite(v);
  const text = v => v === null || v === undefined ? 'Unknown' : String(v);
  const money = v => finite(v) ? (v < 0 ? '-$' : '$') + Math.abs(v).toFixed(2) : 'Unknown';
  const items = p => Array.isArray(p?.items) ? p.items.slice(0, 200).filter(r => r && typeof r === 'object' && !Array.isArray(r)) : [];
  function seatState(a, paused, known) {
    if (paused || ['paused', 'retired'].includes(a.state)) return 'paused';
    if (!known) return 'unknown';
    if (a.heartbeat_state === 'stale') return 'stale';
    if (a.heartbeat_state !== 'fresh') return 'unknown';
    if (a.turn_state === 'running') return 'typing';
    if (['waiting', 'waiting_model', 'waiting_provider'].includes(a.turn_state)) return 'waiting';
    if (['completed', 'idle'].includes(a.turn_state)) return 'idle';
    return 'unknown';
  }
  function guardState(s) {
    const g = s.health?.guard;
    if (g?.state === 'paused') return 'paused';
    return g?.read_state === 'known' && g.freshness === 'fresh' && ['running', 'healthy', 'clear'].includes(g.state) ? 'clear' : 'unknown';
  }
  function newHandoffs(s, seen, initialized, paused) {
    if (!initialized || paused || guardState(s) !== 'clear' || !['known', 'partial'].includes(s.runtime_handoffs?.state) || s.clock?.state !== 'known' || !finite(s.clock.now)) return [];
    const active = new Set(items(s.agents).filter(a => ['typing', 'waiting', 'idle'].includes(seatState(a, false, true))).map(a => a.agent_id));
    return items(s.runtime_handoffs).filter(e => typeof e.event_id === 'string' && !seen.has(e.event_id) &&
      finite(e.at) && s.clock.now - e.at >= 0 && s.clock.now - e.at <= 10 &&
      active.has(e.from_actor_id) && active.has(e.to_actor_id) && e.from_actor_id !== e.to_actor_id &&
      ['from_record_kind', 'from_record_id', 'to_record_kind', 'to_record_id'].every(k => typeof e[k] === 'string' && e[k].length > 0)).slice(0, 8);
  }
  function bubbleLines(value, width = 24) {
    const chars = Array.from(String(value).replace(/\s+/g, ' ').trim()), lines = [];
    while (chars.length && lines.length < 3) {
      let n = Math.min(width, chars.length);
      if (chars.length > width) { const space = chars.slice(0, width + 1).lastIndexOf(' '); if (space > 0) n = space; }
      lines.push(chars.splice(0, n).join('')); while (chars[0] === ' ') chars.shift();
    }
    if (chars.length) lines[lines.length - 1] = lines.at(-1).slice(0, width - 1) + '…';
    return lines;
  }
  const age = v => !finite(v) ? 'Unknown' : v < 3600 ? (v / 60).toFixed(0) + 'm' : (v / 3600).toFixed(1) + 'h';
  const count = v => Number.isInteger(v) && v >= 0 ? String(v) : 'Unknown';
  function bagRows(s) {
    // Per-book unsold open positions, from s.open_bags only (report.open_bags); never derived from equity/positions.
    const ob = s.open_bags;
    if (!ob || ob.state !== 'known' || !Array.isArray(ob.items)) return [{ title: 'Open bags', detail: 'Unknown · not readable in this read.' }];
    const window = ob.limited === true ? 'loaded window truncated, at least ' : 'loaded window, ';
    const rows = items(ob).slice(0, 12).map(b => ({ title: 'Open bags · ' + text(b.book_id),
      detail: window + count(b.open_n) + ' open · oldest ' + age(b.oldest_age_s) + ' · ' + count(b.unpriced_n) + ' not priced' + (ob.items.length > 12 ? ' · first 12 books shown; more omitted' : '') }));
    return rows.length ? rows : [{ title: 'Open bags', detail: ob.limited === true ? 'None in loaded window (truncated; older rows unread).' : 'None in loaded window.' }];
  }
  function requestRows(s) {
    const a = s.request_admission, n = s.near_pass_receipts;
    const rows = items(a).slice(0, 12).map(r => ({ title: text(r.actor_id) + ' / generation ' + text(r.generation) + ' / ' + text(r.reason) + ' / accounting ' + text(r.accounting_state), detail: 'requested ' + text(r.requested_n) + ' · refused ' + text(r.refused_n) + ' · truncated ' + text(r.truncated_n) }));
    if (!rows.length) rows.push({ title: 'Request admission', detail: a?.state === 'known' ? 'No turn snapshots in loaded window.' : 'Unknown · admission not published.' });
    rows.push(...items(n).slice(0, 12).map(r => ({ title: text(r.actor_id) + ' / generation ' + text(r.generation) + ' / ' + text(r.tool), detail: 'requests ' + text(r.requests_n) + ' · list items ' + text(r.list_items_n) + ' · ' + text(r.outcome) + ' / ' + text(r.reason) + (r.result_limited === true ? ' · result truncated' : r.result_limited === null ? ' · result coverage Unknown' : '') })));
    if (!items(n).length) rows.push({ title: 'Near-pass receipts', detail: n?.state === 'known' ? 'No broker receipts in loaded window.' : 'Unknown · receipt source unavailable.' });
    return rows;
  }
  const V2_ROLES=['head','judge','judge_gpt','scout','researcher','forensics','narrative'];
  function v2Scene(feed) {
    return items(feed?.roster).filter(a=>V2_ROLES.includes(a.role?.toLowerCase())).sort((a,b)=>V2_ROLES.indexOf(a.role.toLowerCase())-V2_ROLES.indexOf(b.role.toLowerCase())).slice(0,7).map(a=>{
      const seat=items(feed?.seats).find(s=>s.actor_id===a.actor_id)||{},activity=seat.activity||{},valid=feed?.roster?.state==='known'&&feed?.seats?.state==='known'&&finite(feed.as_of)&&finite(activity.at)&&activity.at<=feed.as_of&&activity.at>=0;
      const mode=valid&&['working','sleeping','parked','idle'].includes(activity.kind)&&!(activity.kind==='working'&&feed.as_of-activity.at>660)?activity.kind:'unknown',tool=mode==='working'?String(seat.current_tool||''):'';
      const event=items(feed?.events).filter(e=>e.actor_id===a.actor_id&&e.refs?.tool===tool&&['tool_call','tool_result'].includes(e.kind)&&finite(e.corrected_at)&&e.corrected_at>=activity.at&&e.corrected_at<=feed.as_of).sort((a,b)=>b.corrected_at-a.corrected_at)[0];
      return {actor:a.actor_id,identity:[a.actor_id,a.epoch_id,a.generation,a.incarnation].join(':'),role:a.role.toLowerCase(),name:({judge_gpt:'Judge GPT'}[a.role.toLowerCase()]||a.role[0].toUpperCase()+a.role.slice(1).toLowerCase()),mode,tool,station:tool.startsWith('chain_')?'map':['browse','web_search'].includes(tool)?'browser':tool==='paper_order'?'trade':'desk',at:event?.corrected_at??activity.at,thought:typeof seat.last_thought==='string'?seat.last_thought:'No thought observed.',reason:activity.reason,until:activity.until,lastEventId:seat.last_event_id??event?.event_id??null,lastEventAt:Object.hasOwn(seat,'last_event_at')?seat.last_event_at:event?.corrected_at??null,lastEventKind:seat.last_event_kind??event?.kind??null};
    });
  }
  function mountV2(doc,parent,reply,speak) {
    const node=(tag,value,cls)=>{const e=doc.createElement(tag);if(value!==undefined)e.textContent=String(value);if(cls)e.className=cls;return e;};
    const box=node('section',undefined,'pixel-office'),title=node('div',undefined,'pixel-heading'),scene=node('div',undefined,'pixel-scene'),desks=node('div',undefined,'desk-room'),canvas=node('canvas'),dialogue=node('div',undefined,'pixel-dialogue'),world=node('div',undefined,'pixel-world'),screens=node('div',undefined,'wall-screens'),ledger=node('div',undefined,'pixel-ledger'),speaker=node('strong','Office'),words=node('p','Waiting for recorded observations.');
    box.setAttribute('role','region');box.setAttribute('aria-label','Recorded pixel office');title.append(node('h3','The office'),node('span','Live recorded paper positions · animation replays recorded events'));canvas.setAttribute('role','img');canvas.setAttribute('aria-label','Original pixel office with current seat desks, browser terminal, chain map and paper trade desk');dialogue.setAttribute('role','log');dialogue.setAttribute('aria-label','Office dialogue');dialogue.setAttribute('aria-live','polite');dialogue.append(speaker,words);desks.append(canvas);scene.append(desks,screens,ledger);world.append(scene);box.append(title,world,dialogue);parent.append(box);
    const style=node('style',`.pixel-office{margin:0 0 18px;border:3px solid #52665f;background:#182f36;color:#f4edd1;box-shadow:0 5px 0 #080f17;font:13px/1.45 ui-monospace,monospace}.pixel-heading{display:flex;align-items:baseline;justify-content:space-between;gap:12px;padding:10px 14px;background:#203b3f}.pixel-heading h3{font:600 17px/1.3 ui-monospace,monospace!important;margin:0!important}.pixel-heading span{font-size:10px;color:#b6c9bb}.pixel-world{display:grid;grid-template-columns:minmax(320px,.9fr) minmax(0,1.2fr);align-items:start;background:#7e9a8c;gap:12px;padding:8px}.pixel-world[data-chart-state=empty]{grid-template-columns:1fr}.wall-screens{min-width:0;background:#223c43;border:5px ridge #a87e59;padding:10px}.pixel-world[data-chart-state=empty] .wall-screens{border:0;background:#203b3f;grid-row:1}.pixel-world[data-chart-state=empty] .wall-screens .chart-caption{display:none}.pixel-scene{position:relative;width:100%;margin:auto}.desk-room>canvas{display:block;width:100%;image-rendering:pixelated}.pixel-scene .pixel-seat{position:absolute;transform:translate(-50%,-65%);width:44px;height:44px;padding:0!important;border:0!important;background:transparent!important;border-radius:0!important;cursor:pointer}.pixel-seat-status{display:none;position:absolute;top:calc(100% + 13px);left:50%;transform:translateX(-50%);min-width:85px;max-width:135px;background:#203b3f;color:#f4edd1;padding:3px 5px;font:10px/1.25 ui-monospace,monospace;overflow-wrap:anywhere;box-shadow:2px 2px #080f17}.pixel-seat[data-mode=parked] .pixel-seat-status{display:block;background:#a42e28;color:#fff;border:2px solid #f1c7a9;font-weight:bold}.pixel-seat[data-mode=unknown] .pixel-seat-status{background:#626b65}.pixel-seat:focus-visible{outline:3px solid #f7d578!important;outline-offset:1px}.pixel-dialogue{min-height:74px;background:#eee7c4;color:#233c42;margin:0 9px 9px;padding:9px 13px;border:3px double #52665f;overflow-wrap:anywhere}.pixel-dialogue strong{font-size:11px;color:#52665f}.pixel-dialogue p{font:13px/1.5 ui-monospace,monospace!important;margin:3px 0 0!important;max-height:60px;overflow:auto}@media(max-width:850px){.pixel-world{grid-template-columns:1fr}.pixel-world .wall-screens{grid-row:1}.pixel-world .pixel-scene{grid-row:2}}@media(max-width:650px){.pixel-heading{padding:8px 10px;align-items:flex-start}.pixel-heading span{max-width:155px;text-align:right}.pixel-dialogue{min-height:88px}.pixel-dialogue p{font-size:12px!important}}`);doc.head?.append(style);
    const wingStyle=node('style',`@font-face{font-family:'Office Pixel';src:url('/static/vendor/fonts/VT323-Regular.ttf') format('truetype');font-display:swap}.pixel-office,.pixel-office button,.pixel-office select,.pixel-office textarea{font-family:'Office Pixel',monospace!important}.pixel-office .pixel-world{display:block;padding:0;background:#c6bf96}.pixel-office .pixel-scene{display:grid;grid-template-columns:minmax(0,1fr);gap:12px;padding:12px;position:relative}.pixel-office .desk-room{position:relative;isolation:isolate;width:100%;max-width:430px;min-width:0;margin:0 auto;align-self:start}.pixel-office .desk-room::before{content:'';display:block;padding-top:var(--desk-height,85%)}.pixel-office .desk-room>canvas{position:absolute;inset:0;width:100%;height:100%;z-index:-1;pointer-events:none}.pixel-office .pixel-heading h3{font-family:'Office Pixel',monospace!important;font-size:24px!important}.pixel-office .pixel-heading span{font-size:16px}.pixel-office .wall-screens{position:relative;margin:0;background:transparent;border:0;padding:0;min-width:0}.pixel-office .wall-screens .office-position-charts{font-family:'Office Pixel',monospace!important;color:#223c43}.pixel-office .wall-screens .office-position-charts>h3{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)}.pixel-office .wall-screens .office-position-charts>div{display:grid;grid-template-columns:1fr;gap:28px}.pixel-office .wall-screens .position-chart{margin:0;border:10px solid #44645b;border-radius:0;box-shadow:inset 3px 3px #719b76,4px 5px #223c43;padding:8px;background:#a8c58d;color:#223c43;font:16px/1.05 'Office Pixel',monospace}.pixel-office .wall-screens .chart-caption{display:block!important;color:#223c43;background:transparent;margin:0 0 5px!important;padding:0;font:15px/1.05 'Office Pixel',monospace}.pixel-office .wall-screens .chart-caption+.chart-caption{display:none!important}.pixel-office .wall-screens .chart-status{color:#223c43;font:16px/1 'Office Pixel',monospace;margin:4px 0 8px!important}.pixel-office .wall-screens .position-chart h4{font:22px/1 'Office Pixel',monospace}.pixel-office .wall-screens .chart-badge{font:16px/1 'Office Pixel',monospace;color:#223c43;border-color:#44645b;margin:4px 0!important}.pixel-office .wall-screens .chart-meta,.pixel-office .wall-screens .chart-note,.pixel-office .wall-screens .chart-levels,.pixel-office .wall-screens .chart-exit{font:14px/1.05 'Office Pixel',monospace;color:#223c43;margin:4px 0!important}.pixel-office .wall-screens .chart-plot{height:180px}.pixel-office .wall-screens .chart-levels{gap:3px 10px}.pixel-office button{border:2px solid #44645b!important;border-radius:0!important;background:#c6bf96!important;color:#223c43!important;box-shadow:2px 2px #223c43;font-size:16px!important;padding:3px 6px!important}.pixel-office button:disabled{opacity:.6}.pixel-office .monitor-top{align-items:center;flex-wrap:wrap;gap:5px}.pixel-office .monitor-top button{font-size:15px!important}.pixel-office .pixel-ledger{position:relative;display:grid;grid-template-columns:minmax(0,3fr) minmax(0,1fr);gap:8px;margin:0;border:10px solid #a87e59;background:#f1e8bd;box-shadow:4px 6px #223c43;padding:2px;min-width:0}#office-v2-work .pixel-ledger .floor-book{margin:0;border:0;border-radius:0;padding:5px 8px;background:#f1e8bd;color:#223c43;font:16px/1.05 'Office Pixel',monospace}#office-v2-work .pixel-ledger .floor-book h3{font:22px/1 'Office Pixel',monospace;margin:3px 0}#office-v2-work .pixel-ledger .floor-book h4{font:18px/1 'Office Pixel',monospace;margin:4px 0}#office-v2-work .pixel-ledger .book-warning,#office-v2-work .pixel-ledger .chat-hint{color:#44645b;font:14px/1.05 'Office Pixel',monospace;margin:3px 0}#office-v2-work .pixel-ledger .book-metrics{font:18px/1.1 'Office Pixel',monospace;gap:4px 16px;margin:4px 0}#office-v2-work .pixel-ledger .book-snapshot{max-height:none;overflow:visible}#office-v2-work .pixel-ledger .book-report-status{font:15px/1.05 'Office Pixel',monospace;color:#804a32;margin:4px 0}.pixel-office .pixel-ledger>button{grid-column:1/-1;justify-self:start}.pixel-office .ledger-enlarged{background:#f1e8bd;color:#223c43;border:10px solid #a87e59;width:min(1100px,95vw);max-height:92vh;overflow:auto;padding:14px;font:18px/1.2 'Office Pixel',monospace}.pixel-office .ledger-enlarged::backdrop{background:#223c43dd}.pixel-office .ledger-enlarged .floor-book{background:#f1e8bd!important;color:#223c43!important}.pixel-office .ledger-enlarged .book-snapshot{max-height:none}.pixel-office .chart-enlarged .chart-plot{height:min(52vh,500px)!important}#office-v2-work .ledger-enlarged .floor-book{font:18px/1.2 'Office Pixel',monospace}#office-v2-work .ledger-enlarged .book-warning,#office-v2-work .ledger-enlarged .chat-hint{color:#44645b}.pixel-office .pixel-dialogue{font-family:'Office Pixel',monospace;min-height:60px}.pixel-office .pixel-dialogue p{font:16px/1.1 'Office Pixel',monospace!important}.pixel-office .pixel-dialogue strong{font:16px/1 'Office Pixel',monospace}@media(min-width:1000px){.pixel-office .pixel-scene{grid-template-columns:minmax(280px,.9fr) minmax(0,1.5fr) minmax(280px,1fr);align-items:start}.pixel-office .desk-room{grid-column:1;grid-row:1}.pixel-office .wall-screens{grid-column:2;grid-row:1;margin:0}.pixel-office .pixel-ledger{grid-column:1/-1;grid-row:2;margin:0}.pixel-office .pixel-heading{padding:6px 14px}.pixel-office .pixel-dialogue{margin-bottom:8px}.pixel-office .wall-screens .chart-plot{height:170px}}@media(max-width:650px){.pixel-office .wall-screens,.pixel-office .pixel-ledger{margin:0}.pixel-office .pixel-ledger{grid-template-columns:minmax(0,1fr)}.pixel-office .wall-screens .position-chart{padding:5px;border-width:7px}.pixel-office .pixel-heading span{font-size:14px}.pixel-office .wall-screens .chart-plot{height:210px}}
`);doc.head?.append(wingStyle);
    const ctx=canvas.getContext('2d');if(!ctx){desks.style.setProperty('--desk-height','0%');words.textContent='Pixel canvas unavailable; select a seat for its recorded details.';return {element:box,screens,ledger,world,scene,render(value){desks.replaceChildren();for(const s of v2Scene(value)){const b=node('button',s.name+' · '+s.mode);b.type='button';b.setAttribute('aria-label','Chat with '+s.name);b.addEventListener('click',()=>reply(s.actor));desks.append(b);}},unavailable(){words.textContent='Connection lost; previous observations are stale.';}};}
    const reduced=root.matchMedia('(prefers-reduced-motion: reduce)'),P={ink:'#223c43',wall:'#7e9a8c',floor:'#c6bf96',tile:'#b9b58d',wood:'#a87e59',paper:'#f1e8bd',green:'#719b76',blue:'#86b8ce',gold:'#e3b85d',teal:'#68b7a3',plum:'#bb9edc',coral:'#dd8e72',red:'#d76259'};
    let seats=[],feed=null,frame=0,lastDraw=0,selected=null,seenMail=new Set(),ready=false,dialogueKey='';const figures=new Map();
    let motionPaused=false;const isReduced=()=>reduced.matches||motionPaused;
    const active=()=>!doc.hidden&&canvas.isConnected&&canvas.getClientRects().length>0;
    const rect=(x,y,w,h,c)=>{ctx.fillStyle=c;ctx.fillRect(Math.round(x),Math.round(y),w,h);};
    const label=(v,x,y,c=P.ink,size=8)=>{ctx.fillStyle=c;ctx.font=Math.max(12,size*1.6)+'px Office Pixel, monospace';ctx.fillText(String(v),Math.round(x),Math.round(y));};
    const narrow=()=>desks.clientWidth<1000;
    const fourColumns=()=>root.innerWidth>=1000&&seats.length>6;
    const dimensions=()=>{const w=narrow()?400:1200;return {w,h:Math.max(fourColumns()?460:narrow()&&seats.length>6?600:narrow()&&seats.length>3?460:narrow()?340:300,desks.clientHeight*w/(desks.clientWidth||w))};};
    const seatColor=role=>({head:P.blue,judge:P.gold,judge_gpt:P.plum,scout:P.teal,researcher:P.plum,forensics:P.coral,narrative:P.teal}[role]||'#9b9f92');
    const stationShift=()=>fourColumns()?120:seats.length>6?(narrow()?260:310):!narrow()?180:seats.length>3?120:0;
    function deskPosition(index,count=seats.length||3){if(!narrow()){const columns=count>4?3:2;return {x:30+(index%columns+.5)*440/columns,y:153+Math.floor(index/columns)*130};}const {w}=dimensions(),cols=fourColumns()?4:3,row=Math.floor(index/cols),columns=Math.min(cols,count-row*cols),column=index%cols;return {x:10+(column+.5)*(w-20)/columns,y:153+row*120};}

    function position(seat,index){const {w:sceneW}=dimensions(),w=narrow()?sceneW:500,lane=(index-(seats.length-1)/2)*52,shift=stationShift();return seat.station==='browser'?{x:35+index*40,y:235+shift+(index%2)*24}:seat.station==='map'?{x:w/2+lane,y:245+shift}:seat.station==='trade'?{x:w-35-index*40,y:235+shift+(index%2)*24}:deskPosition(index);}

    function current(f,t){if(!f.walk||isReduced())return f.target;const p=Math.min(1,Math.max(0,(t-f.walk.at)/2800)),a=f.walk.from,b=f.walk.to,mid=narrow()&&seats.length>3&&Math.max(a.y,b.y)>220?315:185,l1=Math.abs(a.y-mid),l2=Math.abs(b.x-a.x),l3=Math.abs(b.y-mid),distance=(l1+l2+l3)*p;let pos;
      if(distance<l1)pos={x:a.x,y:a.y+(mid-a.y)*(distance/(l1||1))};else if(distance<l1+l2)pos={x:a.x+(b.x-a.x)*((distance-l1)/(l2||1)),y:mid};else pos={x:b.x,y:mid+(b.y-mid)*((distance-l1-l2)/(l3||1))};
      if(p===1){if(f.mail){f.mail=null;f.walk={from:b,to:f.target,at:t};return b;}f.walk=null;return f.target;}return pos;
    }
    function room(w,h){const full=w;rect(0,0,full,h,P.ink);if(!narrow())w=500;rect(0,0,w,h,P.ink);rect(6,6,w-12,53,P.wall);rect(6,59,w-12,h-65,P.floor);for(let y=60;y<h-6;y+=16)for(let x=7;x<w-6;x+=24){rect(x,y,23,1,P.tile);rect(x+(y%32?0:12),y,1,15,P.tile);}rect(6,55,w-12,6,P.ink);
      for(const x of [28,w-94]){rect(x,14,66,35,P.ink);rect(x+3,17,60,29,P.blue);rect(x+31,17,3,29,P.paper);rect(x+3,30,60,3,P.paper);rect(x+8,21,14,3,'#c9e6e3');}rect(w/2-62,14,124,31,P.ink);rect(w/2-59,17,118,25,P.paper);label('SNIPER FIELD OFFICE',w/2-53,33,P.ink,9);
      for(let i=0;i<seats.length;i++){const seat=seats[i]||{name:['Head','Judge','Scout'][i],role:V2_ROLES[i]},pos=deskPosition(i),x=pos.x,c=seatColor(seat.role);ctx.save();ctx.translate(0,pos.y-153);rect(x-44,103,88,37,P.ink);rect(x-41,106,82,28,P.wood);rect(x-16,87,32,26,P.ink);rect(x-13,90,26,18,c);rect(x-4,113,9,4,P.ink);rect(x-17,123,34,6,P.paper);rect(x+24,115,8,10,P.paper);rect(x-34,137,5,12,P.ink);rect(x+29,137,5,12,P.ink);rect(x-12,154,24,16,'#738b7c');label(seat.name,x-seat.name.length*2.4,182,P.ink,8);ctx.restore();}
      ctx.save();ctx.translate(0,stationShift());rect(17,204,69,38,P.ink);rect(20,207,63,30,P.wood);rect(27,188,48,29,P.ink);rect(30,191,42,21,P.blue);label('www',41,205,P.ink,9);label('Browser',25,258,P.ink,8);
      rect(w/2-45,215,90,38,P.ink);rect(w/2-42,218,84,32,P.green);for(const [x,y] of [[-24,7],[0,18],[24,4]]){rect(w/2+x,222+y,7,6,P.paper);}rect(w/2-22,232,47,2,P.paper);label('Chain map',w/2-27,269,P.ink,8);
      rect(w-88,204,69,38,P.ink);rect(w-85,207,63,30,P.wood);rect(w-78,188,49,29,P.ink);rect(w-75,191,43,21,'#344e55');for(let i=0;i<6;i++)rect(w-70+i*6,207-i*2,4,3,P.gold);label('Paper trade',w-88,258,P.ink,8);ctx.restore();
      for(const x of [16,w-29]){rect(x,h-27,13,14,P.wood);rect(x-4,h-43,20,14,P.green);rect(x+2,h-49,8,14,'#527c65');}rect(w/2-18,h-25,36,19,P.ink);rect(w/2-14,h-22,28,16,P.wood);if(!narrow()){const edge=Math.min(h-110,h*.68);rect(506,6,full-512,edge,P.wall);rect(506,edge+6,full-512,h-edge-12,P.floor);rect(506,edge,full-512,6,P.ink);for(let y=edge+14;y<h-6;y+=24)rect(506,y,full-512,1,P.tile);label('PAPER TRADING WING',528,27,P.paper,10);rect(498,6,8,h-12,P.ink);rect(498,h*.55,8,65,P.floor);}
    }
    function person(x,y,c,mode,t,walking,pulse){ctx.save();ctx.translate(Math.round(x),Math.round(y));ctx.scale(1.25,1.25);x=0;y=0;const beat=Math.floor(t/180)%2,bob=pulse&&mode==='working'&&!isReduced()?Math.floor(t/800)%2:0;y+=bob;rect(x-9,y+15,18,3,'#9e9f7c');rect(x-5,y+10,4,7+(walking?beat*2:0),P.ink);rect(x+2,y+10,4,7+(walking?(1-beat)*2:0),P.ink);rect(x-7,y,15,12,c);rect(x-5,y-10,11,12,'#e1b58b');rect(x-6,y-13,13,6,P.ink);rect(x-7,y-9,3,5,P.ink);rect(x+1,y-5,2,2,P.ink);rect(x-10,y+2,4,7,'#e1b58b');rect(x+8,y+2,4,7,'#e1b58b');if(mode==='sleeping')label('z Z',x+11,y-10,P.ink,9);else if(mode==='parked'){rect(x+8,y-24,13,18,P.red);label('!',x+11,y-11,P.paper,13);}else if(mode==='unknown')label('?',x+12,y-12,P.ink,12);ctx.restore();}
    function draw(t){desks.style.setProperty('--desk-height',fourColumns()?'115%':narrow()&&seats.length>6?'150%':narrow()&&seats.length>3?'115%':narrow()?'85%':'0%');const {w,h}=dimensions();const layout=w+'x'+h+':'+seats.length;if(canvas.dataset.layout!==layout){canvas.dataset.layout=layout;seats.forEach((s,i)=>{const f=figures.get(s.actor);if(f){f.target=position(s,i);f.walk=null;}});}const s=Math.min(4,Math.max(1,Math.ceil(canvas.getBoundingClientRect().width*(root.devicePixelRatio||1)/w)));if(canvas.width!==w*s||canvas.height!==h*s){canvas.width=w*s;canvas.height=h*s;}ctx.setTransform(s,0,0,s,0,0);ctx.imageSmoothingEnabled=false;room(w,h);
      seats.forEach((s,i)=>{const f=figures.get(s.actor);if(!f)return;const pos=current(f,t),color=s.mode==='unknown'?'#9b9f92':seatColor(s.role);const pulse=!isReduced()&&t<(f.pulseUntil||0);person(pos.x,pos.y,color,s.mode,t,!!f.walk,pulse);f.button.dataset.pulsing=String(pulse);f.button.style.left=(pos.x/w*100)+'%';f.button.style.top=(pos.y/h*100)+'%';f.button.dataset.moving=String(!!f.walk);if(f.speech){f.speech.style.left=(pos.x/w*100)+'%';f.speech.style.top=((pos.y-40)/h*100)+'%';f.speech.querySelector('.speech-preview').textContent=shortIds(s.thought);f.speech.hidden=!s.thought?.trim()||s.thought==='No thought observed.';}if(f.mail){label('Recorded mail',pos.x-32,pos.y+29,P.ink,7);if(!f.walk)f.mail=null;}
        const bubble=f.mail||(!f.walk&&s.mode!=='unknown'?s.thought:null);if(bubble&&!f.speech){const lines=bubbleLines(bubble,narrow()?17:23).slice(0,2),bw=narrow()?112:148,left=Math.max(8,Math.min(w-bw-8,pos.x-bw/2)),top=Math.max(64,pos.y-78);rect(left,top,bw,33,P.ink);rect(left+2,top+2,bw-4,29,P.paper);rect(pos.x-2,top+32,5,5,P.paper);lines.forEach((v,j)=>label(v,left+6,top+13+j*10,P.ink,7));}
      });
    }
    function tick(t){frame=0;if(!active())return;if(t-lastDraw>=83){draw(t);lastDraw=t;}if(!isReduced()&&[...figures.values()].some(f=>f.walk||t<(f.pulseUntil||0)))frame=root.requestAnimationFrame(tick);}
    function start(){if(isReduced())for(const f of figures.values()){f.walk=null;f.mail=null;f.pulseUntil=0;}if(frame)root.cancelAnimationFrame(frame);frame=0;draw(root.performance.now());if(active()&&!isReduced()&&[...figures.values()].some(f=>f.walk||root.performance.now()<(f.pulseUntil||0)))frame=root.requestAnimationFrame(tick);}
    const shortIds=value=>String(value).replace(/\b(?:0x[a-fA-F0-9]{40,64}|[1-9A-HJ-NP-Za-km-z]{32,88})\b/g,id=>id.slice(0,6)+'…'+id.slice(-4));
    function updateDialogue(){const selectedSeat=seats.find(s=>s.actor===selected),events=items(feed?.events).filter(e=>e.kind==='thought'&&typeof e.text==='string'&&finite(e.corrected_at)&&e.corrected_at<=feed.as_of&&seats.some(s=>s.actor===e.actor_id)).map(e=>({id:e.event_id,actor:e.actor_id,text:e.text,at:e.corrected_at})),mail=items(feed?.mail).filter(m=>typeof m.text==='string'&&finite(m.created_at)&&m.created_at<=feed.as_of).map(m=>({id:m.message_id,actor:m.from_actor_id,text:m.text,at:m.created_at}));const newest=[...events,...mail].sort((a,b)=>b.at-a.at)[0],who=selectedSeat?.name||seats.find(s=>s.actor===newest?.actor)?.name||(newest?.actor==='operator'?'Operator':newest?.actor==='crew'?'Crew note':newest?.actor==='watch'?'Watch alert':'Office'),body=selectedSeat?.thought||newest?.text||'No thought or mail observed in this window.',key=who+'|'+body;if(key!==dialogueKey){dialogueKey=key;speaker.textContent=who+' · last recorded words · IDs shortened; full text in feed/chat';words.textContent=shortIds(body).slice(0,1200);}}
    function render(value){feed=value;const next=v2Scene(value),now=root.performance.now();for(const [id,f] of figures)if(!next.some(s=>s.actor===id&&s.identity===f.identity)){f.button.remove();f.speech?.remove();figures.delete(id);}next.forEach((s,i)=>{let f=figures.get(s.actor),target=position(s,i);if(!f){const button=node('button');button.type='button';button.className='pixel-seat';button.setAttribute('aria-label','Chat with '+s.name);button.addEventListener('click',()=>{selected=s.actor;updateDialogue();reply(s.actor);});desks.append(button);button.append(node('span',undefined,'pixel-seat-status'));f={button,target,identity:s.identity,key:null,eventKey:undefined,pulseUntil:0,walk:null};button.dataset.actor=s.actor;if(root.OfficeCommunications){const speech=node('button',undefined,'pixel-speech');speech.type='button';speech.setAttribute('aria-haspopup','dialog');speech.append(node('span',undefined,'speech-preview'),node('span','Expand','speech-hint'));speech.setAttribute('aria-label','Expand '+s.name+' message');speech.addEventListener('click',()=>speak?.(s.actor,speech));desks.append(speech);f.speech=speech;}figures.set(s.actor,f);}const fresh=s.lastEventId&&f.eventKey!==undefined&&f.eventKey!==s.lastEventId&&finite(s.lastEventAt)&&finite(value.as_of)&&value.as_of-s.lastEventAt>=0&&value.as_of-s.lastEventAt<=30;if(fresh&&!isReduced())f.pulseUntil=now+1800;f.eventKey=s.lastEventId;f.button.dataset.lastEventId=s.lastEventId||'';const key=[s.mode,s.station,s.at].join(':');if(fresh&&f.key!==null&&f.key!==key&&s.mode!=='unknown'&&finite(s.at)&&value.as_of-s.at>=0&&value.as_of-s.at<=30&&!isReduced()){const from=current(f,now);if(Math.abs(from.x-target.x)+Math.abs(from.y-target.y)>2)f.walk={from,to:target,at:now};}else if(s.mode==='unknown'||isReduced())f.walk=null;f.target=target;f.key=key;f.button.dataset.station=s.station;f.button.dataset.mode=s.mode;const state=s.mode==='working'?'Working now':s.mode==='sleeping'?'Sleeping'+(finite(s.until)?' until '+new Date(s.until*1000).toISOString().slice(11,16)+' UTC':' · time unknown'):s.mode==='idle'?'Idle · waiting for work':s.mode==='parked'?'Parked: '+String(s.reason||'reason unknown').replace(/_/g,' '):'State unknown';f.button.querySelector('.pixel-seat-status').textContent=s.mode==='parked'?s.name+' · '+state:state;f.button.title=s.name+' · '+state+(s.tool?' · '+s.tool:'');});seats=next;
      for(const m of items(value?.mail)){if(!root.OfficeCommunications&&ready&&!seenMail.has(m.message_id)&&finite(m.created_at)&&finite(value.as_of)&&value.as_of-m.created_at>=0&&value.as_of-m.created_at<=30&&!isReduced()){const a=seats.find(s=>s.actor===m.from_actor_id&&s.mode==='working'),b=seats.find(s=>s.actor===m.to_actor_id&&s.mode!=='unknown');if(a&&b&&a.actor!==b.actor){const f=figures.get(a.actor),to=figures.get(b.actor).target;f.walk={from:current(f,now),to,at:now};f.mail=m.text;}}seenMail.add(m.message_id);}if(seenMail.size>1000)seenMail=new Set(items(value?.mail).map(m=>m.message_id));if(['known','partial'].includes(value?.mail?.state))ready=true;updateDialogue();start();}
    function unavailable(){dialogueKey='';if(frame)root.cancelAnimationFrame(frame);frame=0;seats=seats.map(s=>({...s,mode:'unknown',station:'desk'}));for(const f of figures.values()){f.walk=null;f.mail=null;f.pulseUntil=0;f.button.dataset.pulsing='false';f.button.dataset.mode='unknown';f.button.querySelector('.pixel-seat-status').textContent='State unknown';}speaker.textContent='Connection lost';words.textContent='Previous observations are stale. Movement paused.';draw(0);}
    doc.addEventListener('visibilitychange',()=>{if(doc.hidden){if(frame)root.cancelAnimationFrame(frame);frame=0;}else start();});reduced.addEventListener('change',start);root.addEventListener('resize',start);if(root.ResizeObserver)new root.ResizeObserver(start).observe(desks);start();return {render,unavailable,redraw:start,reduceMotion(value){motionPaused=!!value;start();},element:box,screens,ledger,world,scene};
  }
  const api = { v2Scene, mountV2, seatState, guardState, newHandoffs, money, bubbleLines, bagRows, requestRows };
  root.OfficeFloorV2=api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof document === 'undefined') return;
  const $ = id => document.getElementById(id);
  const panel = $('tab-desk'), canvas = $('office-scene');
  if (!panel || !canvas) return;
  const ctx = canvas.getContext('2d'), reduced = root.matchMedia('(prefers-reduced-motion: reduce)');
  const P = { ink: '#182f36', slate: '#44645b', sage: '#a8c58d', paper: '#e6e8b2', amber: '#d5a76a', plum: '#7b6d8d' };
  const colors = [P.amber, '#8eb8b0', '#bc9fa9', P.sage, '#a8a4ca', '#d7bf90'];
  let state = null, known = false, heldPause = false, initialized = false, frame = 0, drawnAt = 0;
  let legacyFlight = null;
  let flight = null, lastRequest = -Infinity, walks = [], seen = new Set(), seats = [], bubbles = new Map();
  const active = () => !document.hidden && panel.classList.contains('active');
  const node = (tag, value, cls) => { const e = document.createElement(tag); if (value !== undefined) e.textContent = text(value); if (cls) e.className = cls; return e; };
  const instant = v => { if (!finite(v)) return 'Unknown'; try { return new Date(v * 1000).toISOString().replace('T', ' ').replace('.000Z', ' UTC'); } catch { return 'Unknown'; } };
  function statusLabel(a) { const s = seatState(a, heldPause, known && state?.clock?.state === 'known'); return { typing: 'Running · typing', waiting: 'Waiting for model', idle: 'Idle · last turn completed', stale: 'Stale heartbeat', paused: 'Paused', unknown: 'State unknown' }[s]; }
  function stop() {
    if (flight) flight.abort();
    if (legacyFlight) legacyFlight.abort();
    cancelAnimationFrame(frame); frame = 0; walks = [];
    if (state) draw(0);
  }
  function rect(x, y, w, h, color) { ctx.fillStyle = color; ctx.fillRect(Math.round(x), Math.round(y), w, h); }
  function label(value, x, y, color = P.ink, size = 9) { ctx.fillStyle = color; ctx.font = size + 'px monospace'; ctx.fillText(String(value).slice(0, 30), x, y); }
  function plant(x, y) {
    rect(x + 4, y + 17, 12, 11, P.amber); rect(x + 2, y + 15, 16, 4, P.ink);
    rect(x + 8, y + 2, 4, 17, P.slate); rect(x, y + 5, 10, 6, P.sage); rect(x + 12, y, 10, 7, P.sage); rect(x + 13, y + 9, 9, 5, P.slate);
  }
  function person(x, y, color, typing, t, walking = false) {
    const beat = Math.floor(t / 190) % 2;
    rect(x - 8, y + 17, 17, 3, '#63715c');
    rect(x - 5, y + 12, 4, 7 + (walking && beat ? 2 : 0), P.ink); rect(x + 2, y + 12, 4, 7 + (walking && !beat ? 2 : 0), P.ink);
    rect(x - 7, y + 2, 15, 12, color); rect(x - 5, y - 9, 11, 12, P.amber);
    rect(x - 6, y - 12, 13, 6, P.ink); rect(x - 7, y - 8, 3, 5, P.ink); rect(x + 1, y - 4, 2, 2, P.ink);
    rect(x - 10, y + 4 + (typing ? beat * 2 : 0), 4, 6, P.amber); rect(x + 8, y + 4 + (typing ? (1 - beat) * 2 : 0), 4, 6, P.amber);
  }
  function desk(x, y, a, index, t) {
    const mode = seatState(a, heldPause, known && state?.clock?.state === 'known'), typing = mode === 'typing' && !reduced.matches && active() && guardState(state || {}) === 'clear';
    rect(x - 51, y + 2, 105, 46, P.ink); rect(x - 48, y - 2, 99, 38, '#b8aa7b'); rect(x - 48, y + 33, 99, 5, '#8b8563');
    rect(x - 39, y + 38, 6, 12, P.ink); rect(x + 37, y + 38, 6, 12, P.ink);
    rect(x - 19, y - 20, 40, 29, P.ink); rect(x - 15, y - 17, 32, 20, ['typing', 'waiting', 'idle'].includes(mode) ? '#8eb8b0' : '#65746a');
    for (let k = 0; k < 3; k++) rect(x - 11, y - 13 + k * 5, typing && (Math.floor(t / 240) + k) % 2 ? 21 : 13, 2, P.slate);
    rect(x - 3, y + 8, 9, 4, P.ink); rect(x - 18, y + 17, 39, 7, P.paper);
    for (let k = 0; k < 7; k++) rect(x - 16 + k * 5, y + 19, 3, 1, P.slate);
    rect(x + 31, y + 8, 10, 10, P.paper); rect(x + 40, y + 10, 4, 5, P.paper); rect(x - 40, y + 6, 13, 19, colors[index % colors.length]);
    rect(x - 11, y + 43, 24, 22, P.slate); rect(x - 13, y + 61, 28, 5, P.ink);
    const walking = walks.some(w => w.from_actor_id === a.agent_id);
    if (!walking) { person(x, y + 39, colors[index % colors.length], typing, t); if (mode === 'paused') { rect(x - 4, y + 35, 8, 2, P.ink); label('z', x + 16, y + 24, P.ink, 9); } if (mode === 'waiting') rect(x + 7, y + 37, 5, 5, P.amber); }
    rect(x - 63, y + 74, 128, 31, P.ink); rect(x - 61, y + 76, 124, 27, P.paper);
    label(a.name || a.agent_id, x - 56, y + 87); label(statusLabel(a), x - 56, y + 99, P.ink, 7);
    const bubble = bubbles.get(a.agent_id);
    if (bubble) {
      const lines = bubbleLines(bubble.text, 24), h = lines.length * 10 + 12, top = Math.max(90, y - 28 - h);
      rect(x - 65, top, 132, h, P.ink); rect(x - 63, top + 2, 128, h - 4, P.paper); rect(x + 7, top + h - 2, 5, 6, P.paper);
      lines.forEach((line, i) => label(line, x - 58, top + 12 + i * 10, P.ink, 8));
    }
  }
  function draw(t) {
    if (!ctx) return;
    const columns = seats.length <= 4 ? 2 : 4, rows = Math.max(2, Math.ceil(seats.length / columns));
    const height = 120 + rows * 155;
    if (canvas.height !== height) canvas.height = height;
    ctx.imageSmoothingEnabled = false;
    rect(0, 0, 640, height, P.ink); rect(10, 10, 620, 75, P.slate); rect(10, 85, 620, height - 95, '#b3b68c');
    for (let y = 88; y < height - 10; y += 16) for (let x = 12; x < 628; x += 32) { rect(x + (y % 32 ? 0 : 8), y, 1, 15, '#9ca17e'); rect(x, y + 15, Math.min(31, 628 - x), 1, '#a3a984'); }
    rect(10, 79, 620, 7, P.ink);
    for (const x of [48, 480]) { rect(x, 19, 110, 49, P.ink); rect(x + 4, 23, 102, 41, '#8eb8b0'); rect(x + 51, 23, 4, 41, P.paper); rect(x + 4, 42, 102, 3, P.paper); rect(x + 8, 28, 16, 3, P.paper); }
    rect(238, 19, 165, 46, P.ink); rect(242, 23, 157, 38, P.paper); label('SNIPER  /  OFFICE', 255, 39, P.ink, 10); label('PAPER RESEARCH FLOOR', 257, 53, P.slate, 8);
    plant(20, height - 45); plant(595, height - 45);
    // Static shared furniture fills the aisle without implying any activity.
    if (columns === 2) {
      rect(280, 216, 82, 42, P.ink); rect(283, 219, 76, 36, '#b8aa7b');
      for (const x of [270, 363]) { rect(x, 224, 8, 24, P.slate); rect(x, 247, 8, 5, P.ink); }
      rect(303, 225, 20, 13, P.paper); rect(327, 234, 10, 10, P.plum);
      rect(286, 105, 68, 43, P.ink); rect(290, 109, 60, 34, P.paper); label('FIELD NOTES', 296, 121, P.ink, 7);
      rect(296, 128, 31, 2, P.slate); rect(296, 134, 43, 2, P.slate);
    }
    rect(302, height - 40, 40, 30, P.ink); rect(307, height - 37, 30, 27, '#8b8563'); rect(330, height - 24, 3, 3, P.amber);
    rect(582, 99, 22, 30, P.paper); rect(585, 87, 16, 17, '#8eb8b0'); rect(590, 112, 5, 3, P.slate);
    const lamp = heldPause ? P.amber : guardState(state || {}) === 'clear' && known ? P.sage : P.plum;
    rect(176, 17, 48, 48, P.ink); rect(194, 23, 12, 12, lamp); label(heldPause ? 'PAUSE' : guardState(state || {}) === 'clear' && known ? 'CLEAR' : '?', 182, 54, P.paper, 8);
    const positions = new Map();
    seats.forEach((a, i) => { const x = (i % columns + .5) * (610 / columns) + 15, y = 151 + Math.floor(i / columns) * 155; positions.set(a.agent_id, { x, y: y + 39 }); desk(x, y, a, i, t); });
    walks = walks.filter(w => t - w.start < 2400);
    for (const w of walks) {
      const a = positions.get(w.from_actor_id), b = positions.get(w.to_actor_id); if (!a || !b) continue;
      const progress = Math.min(1, Math.max(0, (t - w.start) / 2400));
      const x = a.x + (b.x - a.x) * progress, y = a.y + (b.y - a.y) * progress + 20 * Math.sin(progress * Math.PI);
      person(x, y, P.plum, false, t, true); label('recorded handoff', x - 40, y + 32, P.ink, 7);
    }
    if (heldPause || !known || state?.clock?.state !== 'known') { rect(0, 0, 640, height, heldPause ? 'rgba(12,25,29,.52)' : 'rgba(12,25,29,.32)'); }
    if (!seats.length) label('No published seat state', 235, 220, P.ink, 11);
  }
  function animate(t) {
    frame = 0;
    if (!active() || reduced.matches || !known || heldPause || guardState(state || {}) !== 'clear' || state?.clock?.state !== 'known') { draw(0); return; }
    if (t - drawnAt >= 1000 / 12) { draw(t); drawnAt = t; }
    if (walks.length || seats.some(a => seatState(a, false, true) === 'typing')) frame = requestAnimationFrame(animate);
  }
  function startAnimation() { cancelAnimationFrame(frame); frame = 0; draw(performance.now()); if (!reduced.matches && active() && known && !heldPause && guardState(state || {}) === 'clear') frame = requestAnimationFrame(animate); }
  function sectionRows(id, rows, empty) { const host = $(id); host.replaceChildren(); if (!rows.length) host.append(node('p', empty, 'office-muted')); else rows.forEach(r => host.append(r)); }
  function evidenceRow(title, detail) { const r = node('div', undefined, 'office-evidence-row'); r.append(node('strong', title), node('span', detail)); return r; }
  function render() {
    const s = state, guard = guardState(s);
    if (guard === 'paused') heldPause = true; else if (guard === 'clear') heldPause = false;
    seats = items(s.agents).slice(0, 32); bubbles = new Map();
    const cut = s.clock?.now, messages = [];
    for (const m of items(s.direct_messages)) {
      if (typeof m.text !== 'string' || !finite(cut) || !finite(m.created_at) || m.created_at > cut || cut - m.created_at > 60) continue;
      if (!seats.some(a => a.agent_id === m.from_actor_id && a.generation === m.from_generation) || !seats.some(a => a.agent_id === m.to_actor_id && a.generation === m.to_generation)) continue;
      messages.push({ actor: m.from_actor_id, text: m.text, id: text(m.message_id) + ' / receipt ' + text(m.receipt_id), at: m.created_at });
    }
    for (const m of items(s.messages)) {
      if (typeof m.text !== 'string' || !finite(cut) || !finite(m.known_at) || m.known_at > cut || cut - m.known_at > 60 || !seats.some(a => a.agent_id === m.author)) continue;
      messages.push({ actor: m.author, text: m.text, id: text(m.row_id), at: m.known_at });
    }
    messages.sort((a, b) => b.at - a.at);
    for (const m of messages) if (!bubbles.has(m.actor)) bubbles.set(m.actor, m);
    const room = $('office-room'); room.dataset.paused = String(heldPause);
    $('office-status').textContent = (known ? 'Read ' + instant(cut) : 'Read unavailable; prior data is stale') + ' · Guard ' + (heldPause ? 'paused' : guard) + ' · ' + seats.length + ' / ' + items(s.agents).length + ' loaded seats drawn';
    $('office-pause').hidden = !heldPause;
    $('office-pause').textContent = 'Office paused — ' + text(s.health?.guard?.reason) + '. Motion stopped.';
    sectionRows('office-seats', seats.map(a => {
      const card = node('article', undefined, 'office-seat'); card.dataset.state = seatState(a, heldPause, known && s.clock?.state === 'known');
      card.append(node('h3', a.name || a.agent_id), node('p', text(a.role) + ' / generation ' + text(a.generation), 'office-muted'), node('strong', statusLabel(a)));
      const call = items(s.runtime_calls).find(c => c.actor_id === a.agent_id && c.generation === a.generation);
      if (call) card.append(node('p', 'Last model: ' + text(call.served_model) + ' / ' + (call.served_model_basis === 'requested_pinned' ? 'requested, pin-verified; not echoed' : call.served_model_basis === 'echoed' ? 'provider echoed' : 'identity unknown'), 'office-muted'));
      const m = bubbles.get(a.agent_id); if (m) { const quote = node('blockquote', String(m.text).slice(0, 4096)); quote.append(node('small', 'Recorded message ' + m.id)); card.append(quote); }
      return card;
    }), 'Seat state unknown.');
    const history = items(s.equity_history), books = new Map();
    history.sort((a, b) => (b.at || 0) - (a.at || 0)).forEach(b => { if (!books.has(b.book_id)) books.set(b.book_id, b); });
    const equityRows = [...books.values()].slice(0, 12).map(b => evidenceRow(b.book_id, money(b.equity_usd) + ' · ' + text(b.state) + ' · ' + instant(b.at)));
    sectionRows('office-equity', [...(equityRows.length ? equityRows : [node('p', 'Equity unknown · no stored valuation in this read.', 'office-muted')]),
      ...bagRows(s).map(r => evidenceRow(r.title, r.detail))], 'Equity unknown · no stored valuation in this read.');
    sectionRows('office-positions', items(s.positions).filter(p => !['closed', 'refused', 'cancelled'].includes(p.state)).slice(0, 12).map(p => evidenceRow(text(p.mint).slice(0, 26), text(p.state) + ' · book ' + text(p.book_id) + ' · mark ' + text(p.mark_state))), s.positions?.state === 'known' ? 'No open positions in loaded window.' : 'Positions unknown.');
    sectionRows('office-tickets', items(s.tickets).slice(0, 12).map(t => evidenceRow(t.row_id, text(t.problem).slice(0, 180) + ' · ' + text(t.state))), s.tickets?.state === 'known' ? 'No tickets in loaded window.' : 'Tickets unknown.');
    sectionRows('office-requests', requestRows(s).map(r => evidenceRow(r.title, r.detail)), 'Request observations unknown.');
    $('office-request-note').textContent = 'Latest stored turn snapshots, cumulative once per turn; first 12 seat/reason groups. Known and unknown accounting shown separately. Admission ' + text(s.request_admission?.state) + (s.request_admission?.limited || items(s.request_admission).length > 12 ? ' (truncated)' : '') + '. Latest 32 receipts per tool, first 12 groups; receipts ' + text(s.near_pass_receipts?.state) + (s.near_pass_receipts?.limited || items(s.near_pass_receipts).length > 12 ? ' (truncated)' : '') + '. List item counts are delivered rows, not distinct coins; broker refusals are separate from admission refusals. Unknown stays unknown.';
    $('office-coverage').textContent = 'Loaded-window display, at most 32 drawn seats and 12 rows per collection (the equity board shows up to 12 equity rows and 12 open-bag rows). Equity ' + text(s.equity_history?.state) + '; positions ' + text(s.positions?.state) + '; tickets ' + text(s.tickets?.state) + '. ' + (['equity_history', 'positions', 'tickets', 'agents'].some(k => s[k]?.limited) ? 'Source window truncated. ' : '') + 'Separate paper books; no totals inferred. Animation illustrates recorded work, not physical location.';
    startAnimation();
  }
  async function load(signal) {
    if (!active()) { stop(); return; }
    if (flight || performance.now() - lastRequest < 5000) return;
    lastRequest = performance.now();
    const controller = new AbortController(); flight = controller;
    const cancel = () => controller.abort();
    signal?.addEventListener('abort', cancel, { once: true });
    const timeout = setTimeout(cancel, 2500);
    try {
      if (signal?.aborted) controller.abort();
      const response = await fetch('/api/office/state', { signal: controller.signal, credentials: 'same-origin', cache: 'no-store' });
      if (!response.ok) throw new Error('Office state unavailable');
      const body = await response.json();
      if (!body || !Array.isArray(body.agents?.items) || body.agents.items.some(a => !a || typeof a.agent_id !== 'string')) throw new Error('Office state malformed');
      if (controller.signal.aborted || !active()) return;
      known = true; state = body;
      const paused = guardState(body) === 'paused' || heldPause && guardState(body) !== 'clear';
      walks = reduced.matches ? [] : newHandoffs(body, seen, initialized, paused).map(e => ({ ...e, start: performance.now() }));
      items(body.runtime_handoffs).forEach(e => seen.add(e.event_id));
      seen = new Set([...seen].slice(-2048)); initialized = true;
      render();
      if ($('office-legacy').open) loadLegacy();
    } catch (error) {
      known = false; walks = []; cancelAnimationFrame(frame); frame = 0;
      if (state) render(); else { $('office-status').textContent = 'Office state unavailable · unknown'; draw(0); }
    } finally {
      clearTimeout(timeout); signal?.removeEventListener('abort', cancel);
      if (flight === controller) flight = null;
    }
  }
  function loadLegacy() {
    if (!active() || legacyFlight) return;
    const controller = new AbortController(); legacyFlight = controller;
    root.deskView?.load(controller.signal);
    setTimeout(() => { controller.abort(); if (legacyFlight === controller) legacyFlight = null; }, 2500);
  }
  $('office-legacy').addEventListener('toggle', () => { if ($('office-legacy').open) loadLegacy(); else if (legacyFlight) legacyFlight.abort(); });
  document.addEventListener('visibilitychange', () => { if (document.hidden) stop(); else if (active()) load(); });
  new MutationObserver(() => { if (!active()) stop(); }).observe(panel, { attributes: true, attributeFilter: ['class'] });
  reduced.addEventListener('change', () => { walks = []; startAnimation(); });
  root.officeView = { load, stop };
  draw(0);
})(typeof window !== 'undefined' ? window : globalThis);
