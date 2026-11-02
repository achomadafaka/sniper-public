/* Office floor: host observations only. Untrusted prose is rendered with DOM text nodes. */
(function (root) {
  'use strict';
  const text = v => v === null || v === undefined ? 'Unknown' : String(v);
  const rows = p => Array.isArray(p?.items) ? p.items.slice(0, 200) : [];
  const validTime = v => typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= 253402300799;
  const time = v => validTime(v) ? new Date(v * 1000).toISOString().slice(11, 19) : 'Time unknown';
  const roleOrder=['head','judge','judge_gpt','scout','researcher','forensics','narrative'];
  const role = r => ({head:'Head', judge:'Judge',judge_gpt:'Judge GPT', scout:'Scout',researcher:'Researcher',forensics:'Forensics',narrative:'Narrative'}[String(r).toLowerCase()] || 'Seat');
  const verbs = {sheet_read:'Read a sheet', book_view:'Checked the book', feed_list:'Read the feed', db_read:'Read desk facts', brain_read:'Read research notes', brain_note:'Saved a note', web_search:'Searched the web', browse:'Visited a page', mail_read:'Read mail', mail_send:'Sent mail', paper_order:'Paper order', shell:'Used workspace', sleep:'Set next wake', request:'Asked the operator'};
  const css = `
#office-v2-work{--floor-ink:#eef2f6;--floor-muted:#97a6b5;--floor-edge:#2b3949;color:var(--floor-ink);margin:0 0 28px;font:14px/1.5 Inter,system-ui,sans-serif}
#office-v2-work *{box-sizing:border-box}#office-v2-work h2,#office-v2-work h3,#office-v2-work p{margin:0}
#office-v2-work button,#office-v2-work select,#office-v2-work textarea{font:inherit;color:inherit;background:#182333;border:1px solid #3b4c61;border-radius:5px}
#office-v2-work button,#office-v2-work select{padding:7px 11px}#office-v2-work button{cursor:pointer}#office-v2-work button:disabled{opacity:.45;cursor:default}
#office-v2-work .order-prices{display:block;font-size:14px;line-height:1.2;white-space:normal;overflow-wrap:anywhere}
#office-v2-work .coin-links a{color:#223c43;text-decoration:underline;font-weight:bold;overflow-wrap:anywhere}\n#office-v2-work :focus-visible{outline:2px solid #96c9f3;outline-offset:3px}
#office-v2-work .floor-heading{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:14px}
#office-v2-work .floor-heading h2{font-size:21px;letter-spacing:-.4px}#office-v2-work .floor-status{font-size:12px;color:var(--floor-muted)}
#office-v2-work .seat-overview{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:6px;margin:8px 12px 12px}#office-v2-work .seat-overview button{text-align:left;font-size:13px;padding:8px;overflow-wrap:anywhere}
#office-v2-work .floor-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
#office-v2-work [data-role=head]{--seat-color:#70a9df}#office-v2-work [data-role=judge]{--seat-color:#e2b76a}#office-v2-work [data-role=scout]{--seat-color:#67c4ae}#office-v2-work [data-role=researcher]{--seat-color:#bb9edc}#office-v2-work [data-role=forensics]{--seat-color:#dd8e72}#office-v2-work [data-role=narrative]{--seat-color:#8fbf8a}
#office-v2-work .floor-seat{border:1px solid var(--floor-edge);border-top:3px solid var(--seat-color,#97a6b5);background:#141e2b;border-radius:6px;padding:15px;min-width:0;display:flex;flex-direction:column;gap:9px}
#office-v2-work .seat-top{display:flex;align-items:center;justify-content:space-between;gap:8px}#office-v2-work .seat-top h3{font-size:18px;color:var(--seat-color)}
#office-v2-work .seat-reply{font-size:12px;padding:4px 9px}#office-v2-work .seat-state{font-size:14px;font-weight:600}#office-v2-work .seat-activity{font-size:12px;color:var(--floor-muted)}
#office-v2-work .seat-thought{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:42px;color:#dce4ed;overflow-wrap:anywhere}
#office-v2-work .seat-thought p,#office-v2-work .seat-thought ul{margin:0}#office-v2-work .seat-stats{display:flex;gap:22px;border-top:1px solid var(--floor-edge);padding-top:9px;margin-top:auto;font-size:12px;color:var(--floor-muted)}
#office-v2-work .seat-stats b{display:block;color:var(--floor-ink);font-size:16px;font-variant-numeric:tabular-nums;font-weight:600}
#office-v2-work .tokens-note{display:block;color:var(--floor-muted);font-size:11px}#office-v2-work .tokens-note:empty{display:none}
#office-v2-work .floor-toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:24px 0 10px}#office-v2-work .floor-toolbar h3{font-size:17px}#office-v2-work .floor-filters{display:flex;gap:8px;flex-wrap:wrap}
#office-v2-work .floor-timeline{border:1px solid var(--floor-edge);border-radius:6px;background:#111a26;max-height:65vh;min-height:180px;overflow:auto;overscroll-behavior:contain;padding:0 18px}
#office-v2-work .floor-note,#office-v2-work .floor-chip{border-bottom:1px solid #243141;padding:13px 0;border-left:3px solid var(--seat-color,#526273);padding-left:12px;margin:0}
#office-v2-work .note-top{display:flex;align-items:center;gap:12px;margin-bottom:7px;font-size:12px}#office-v2-work .seat-name{color:var(--seat-color);font-weight:600}
#office-v2-work time{font:11px/1.5 ui-monospace,monospace;color:var(--floor-muted);white-space:nowrap}#office-v2-work .floor-prose{max-width:78ch;font-size:15px;overflow-wrap:anywhere}
#office-v2-work .floor-prose p+ p{margin-top:8px}#office-v2-work .floor-prose ul{margin:6px 0;padding-left:20px}#office-v2-work .floor-prose li+li{margin-top:3px}
#office-v2-work .floor-chip{padding-top:8px;padding-bottom:8px;font-size:12px}#office-v2-work .floor-chip summary{cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}#office-v2-work .floor-chip time{margin-right:10px}#office-v2-work .floor-chip .seat-name{margin-right:8px}
#office-v2-work .floor-chip pre{font:12px/1.55 ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere;max-height:320px;overflow:auto;background:#0e1520;padding:12px;margin:10px 0 0;border-radius:4px}
#office-v2-work .floor-error{color:#f0a18f}#office-v2-work .floor-empty{padding:24px 0;color:var(--floor-muted)}
#office-v2-work .floor-dm,#office-v2-work .floor-coverage{margin-top:14px;font-size:12px;color:var(--floor-muted)}#office-v2-work details>summary{cursor:pointer}#office-v2-work .floor-dm form{padding:12px 0;display:grid;gap:8px;max-width:680px}
#office-v2-work textarea{width:100%;padding:10px;min-height:90px;resize:vertical}#office-v2-work .floor-dm button{justify-self:start}#office-v2-work .coverage-body{padding-top:10px}#office-v2-work .coverage-body pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:260px;overflow:auto}
@media(max-width:650px){#office-v2-work .floor-cards{grid-template-columns:1fr;gap:8px}#office-v2-work .floor-seat{padding:11px 13px;gap:5px}#office-v2-work .seat-stats{padding-top:5px;gap:25px}#office-v2-work .seat-stats b{display:inline;margin-right:5px;font-size:14px}#office-v2-work .floor-heading{align-items:flex-start}#office-v2-work .floor-toolbar{align-items:flex-start;flex-direction:column;margin-top:18px}#office-v2-work .floor-timeline{padding:0 10px;max-height:60vh}#office-v2-work .floor-filters select{max-width:170px}#office-v2-work .floor-note{padding-left:8px}#office-v2-work .floor-prose{font-size:14px}}

#office-v2-work .floor-work{display:grid;grid-template-columns:minmax(0,1fr) 360px;gap:20px;align-items:start}
#office-v2-work .floor-work>*{min-width:0}
#office-v2-work .floor-dm{margin-top:24px;padding:14px;border:1px solid var(--floor-edge);border-radius:6px;background:#141e2b;color:var(--floor-ink);min-width:0}
#office-v2-work .floor-dm h3{font-size:17px;margin-bottom:12px}#office-v2-work .floor-dm h4{margin:0 0 7px;font-size:13px}
#office-v2-work .chat-controls{display:grid;grid-template-columns:1fr;gap:7px;margin:12px 0 8px}#office-v2-work .chat-controls select{width:100%}
#office-v2-work .chat-thread{max-height:300px;min-height:90px;overflow:auto;overscroll-behavior:contain;border-top:1px solid var(--floor-edge);border-bottom:1px solid var(--floor-edge);padding:5px 0}
#office-v2-work .chat-message{padding:9px 0;border-bottom:1px solid #273546}#office-v2-work .chat-message:last-child{border:0}
#office-v2-work .chat-message[data-sender=operator]{padding-left:12px;border-left:2px solid #70a9df}#office-v2-work .chat-message[data-sender=crew]{border-left:2px solid #97a6b5;padding-left:12px}#office-v2-work .chat-message[data-sender=watch]{border-left:2px solid #e2b76a;padding-left:12px}
#office-v2-work .chat-message header{display:flex;justify-content:space-between;gap:8px;padding:0;margin-bottom:5px;border:0;font-size:11px;color:var(--floor-muted)}
#office-v2-work .chat-message .floor-prose{font-size:13px}#office-v2-work .chat-inbox{max-height:220px;overflow:auto;overscroll-behavior:contain}
#office-v2-work .chat-question{padding:8px 0;border-bottom:1px solid var(--floor-edge)}#office-v2-work .chat-question[data-notification=true]{border-left:3px solid #e2b76a;padding-left:8px}#office-v2-work .chat-question p{font-size:12px;margin:5px 0;overflow-wrap:anywhere}#office-v2-work .chat-question button{font-size:11px;padding:4px 7px}
#office-v2-work .chat-hint{font-size:11px;color:var(--floor-muted)}#office-v2-work .chat-outcome{white-space:pre-wrap;font-size:12px;overflow-wrap:anywhere}
@media(max-width:1050px){#office-v2-work .floor-work{grid-template-columns:1fr}#office-v2-work .floor-dm{margin-top:0}#office-v2-work .chat-controls{grid-template-columns:1fr 1fr}}
#office-v2-work .floor-book,#office-v2-work .floor-decisions{margin-top:14px;border:1px solid var(--floor-edge);border-radius:6px;padding:12px;min-width:0;overflow-wrap:anywhere;font-size:12px}
#office-v2-work .floor-book h3,#office-v2-work .floor-decisions h3{font-size:16px;margin-bottom:5px}#office-v2-work .floor-book h4{margin:8px 0 3px}#office-v2-work .book-warning{color:#d4ba88;font-size:11px}#office-v2-work .book-metrics{display:flex;flex-wrap:wrap;gap:8px 24px;margin:8px 0;font-size:14px}
#office-v2-work .book-snapshot[data-stale=true]{border-left:3px solid #493722;padding-left:8px}#office-v2-work .book-report-status{font-size:12px;color:#d4ba88}#office-v2-work .book-segments{max-height:95px;overflow:auto}#office-v2-work .book-order{padding:8px 0;border-bottom:1px solid var(--floor-edge)}#office-v2-work .decision-list{max-height:130px;overflow:auto}#office-v2-work .decision-list p{padding:5px 0;border-bottom:1px solid var(--floor-edge)}
#office-v2-work .floor-stage{display:block;margin-bottom:18px}#office-v2-work .floor-stage>*{min-width:0}#office-v2-work .floor-cards[hidden],#office-v2-work .floor-seat[hidden]{display:none!important}#office-v2-work .seat-inspector{background:#111a26;color:#eef2f6;border:4px ridge #a87e59;padding:18px;width:min(560px,92vw);max-height:85vh;overflow:auto}#office-v2-work .seat-inspector::backdrop{background:#071018bb}#office-v2-work .seat-inspector .floor-cards{display:block;margin:14px 0 0}

@media(max-width:480px){#office-v2-work .chat-controls{grid-template-columns:1fr}}

.pixel-office .pixel-world[data-chart-state=empty] .wall-screens{background:transparent}
#office-v2-work .book-orders-scroll{max-width:100%;max-height:360px;overflow:auto;scrollbar-gutter:stable;margin:6px 0}#office-v2-work .order-scroll-hint{display:none;color:inherit}#office-v2-work .ledger-enlarged .book-report-status{color:#804a32}@media(max-width:850px){#office-v2-work .order-scroll-hint{display:block;margin:5px 0;font-size:16px}}#office-v2-work .paper-orders{width:100%;min-width:800px;table-layout:fixed;border-collapse:collapse;font:16px/1.1 'Office Pixel',monospace;color:inherit}#office-v2-work .paper-orders th,#office-v2-work .paper-orders td{padding:7px 6px;text-align:left;vertical-align:top;border-bottom:1px solid #719b76;overflow-wrap:anywhere;font:inherit}#office-v2-work .paper-orders td{color:#223c43}#office-v2-work .paper-orders th{color:#44645b}#office-v2-work .paper-orders th:nth-child(1){width:14%}#office-v2-work .paper-orders th:nth-child(2){width:7%}#office-v2-work .paper-orders th:nth-child(3),#office-v2-work .paper-orders th:nth-child(4){width:17%}#office-v2-work .paper-orders th:nth-child(5){width:15%}#office-v2-work .paper-orders th:nth-child(6){width:7%}#office-v2-work .paper-orders th:nth-child(7){width:23%}#office-v2-work .paper-orders p{margin:3px 0!important;font:inherit!important}#office-v2-work .paper-orders .order-note{display:block;font-size:14px;margin:3px 0}#office-v2-work .paper-orders details{margin-top:5px;font-size:14px}#office-v2-work .paper-orders details p{font-size:14px!important}#office-v2-work .paper-orders .order-upper-bias{display:block;color:#804a32;font-size:15px}
#office-v2-work :is(.pixel-ledger,.ledger-enlarged){color:#223c43;background:#f1e8bd;--floor-ink:#223c43;--floor-muted:#223c43}
#office-v2-work :is(.pixel-ledger,.ledger-enlarged) *{color:#223c43}
#office-v2-work :is(.pixel-ledger,.ledger-enlarged) button{background:#e3d9aa;color:#223c43}
#office-v2-work .book-status-line{font-size:14px;margin-bottom:5px}#office-v2-work .book-evidence{margin-top:8px;font-size:14px}

`;
  function mount(doc, parent, options) {
    const node = (tag, value, cls) => { const e = doc.createElement(tag); if (value !== undefined) e.textContent = text(value); if (cls) e.className = cls; return e; };
    const coinName=coin=>{const name=[coin.display_name,coin.name,coin.display_ticker,coin.ticker,coin.mint].find(v=>typeof v==='string'&&v.trim());return name?name.trim().slice(0,160):'Name unavailable';};
    function recordedPlan(order){
      const recorded=value=>typeof value==='string'&&value.trim()?value.trim():null,reason=recorded(order.entry_reason),plan=order.plan,fields=[['Thesis',recorded(plan?.thesis)],['Disproof',recorded(plan?.disproof)],['Exit plan',recorded(plan?.exit_plan)]];
      const label=node('span',(reason?'Entry reason: '+reason:'Entry reason not recorded')+'\n'+(fields.some(([,value])=>value)?fields.map(([name,value])=>name+': '+(value||'not recorded')).join('\n'):'Plan not recorded'),'order-plan');label.style.display='block';label.style.whiteSpace='pre-wrap';return label;
    }
    function notFilled(order){const state=order.state??order.status;if(!['censored','pending'].includes(state)||order.filled_at!=null)return null;const reasons={entry_timeout:'entry wait timed out',sheet_expired:'decision sheet expired before entry',watcher_outage:'paper watcher missed the entry window',insufficient_capital:'not enough paper cash',frontier_not_committed:'market data was not ready',frontier_retry_expired:'market data retry window expired',pool_tip_unproven:'latest pool state was not proven'};const reason=typeof order.reason==='string'&&order.reason.trim()?reasons[order.reason]||order.reason.replace(/_/g,' '):'reason not recorded';return (state==='censored'?'Not filled: ':'Not filled yet: ')+reason;}
    function coinLinks(coin){const links=node('span',undefined,'coin-links');if(coin.network_id!=='solana'||typeof coin.mint!=='string'||!/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(coin.mint))return links;for(const [label,url] of [['GMGN','https://gmgn.ai/sol/token/'],['Dexscreener','https://dexscreener.com/solana/']]){const a=node('a',label);a.href=url+coin.mint;a.target='_blank';a.rel='noopener noreferrer';links.append(doc.createTextNode(' · '),a);}return links;}
    if (doc.head) { const style = node('style', css); doc.head.append(style); }
    function inline(parent, value) {
      for (const part of String(value).split(/(\*\*[^*]+\*\*)/g)) parent.append(node(part.startsWith('**') && part.endsWith('**') && part.length > 4 ? 'strong' : 'span', part.startsWith('**') && part.endsWith('**') && part.length > 4 ? part.slice(2,-2) : part));
    }
    function markdown(parent, value) {
      let list = null;
      for (const line of String(value).split(/\r?\n/)) {
        if (!line.trim()) { list = null; continue; }
        const bullet = /^\s*[-*]\s+/.test(line);
        if (bullet) { if (!list) { list = node('ul'); parent.append(list); } const li = node('li'); inline(li, line.replace(/^\s*[-*]\s+/,'')); list.append(li); }
        else { list = null; const p = node('p'); inline(p, line); parent.append(p); }
      }
    }
    const section = node('section'), heading = node('div',undefined,'floor-heading'), status = node('p','Connecting to the floor…','floor-status');
    section.id='office-v2-work'; heading.append(node('h2','Office floor'), status);
    const overview=node('div',undefined,'seat-overview');overview.setAttribute('role','region');overview.setAttribute('aria-label','Seat activity');
    const gameHost=node('div'),chartsHost=node('div'),stage=node('div',undefined,'floor-stage');stage.dataset.chartState='unknown';stage.append(gameHost);
    const cardsBox=node('div',undefined,'floor-cards'), toolbar=node('div',undefined,'floor-toolbar'), filters=node('div',undefined,'floor-filters');
    const seatFilter=node('select'), kindFilter=node('select'); seatFilter.setAttribute('aria-label','Timeline seat'); kindFilter.setAttribute('aria-label','Timeline view');
    for (const [value,label] of [['all','Everything'],['thoughts','Thoughts only'],['decisions','Decisions only']]) { const o=node('option',label);o.value=value;kindFilter.append(o); } kindFilter.value='all';
    filters.append(seatFilter,kindFilter);toolbar.append(node('h3','Floor timeline'),filters);
    const timeline=node('div',undefined,'floor-timeline');timeline.setAttribute('role','region');timeline.setAttribute('aria-label','Floor timeline');timeline.tabIndex=0;
    const dm=node('section',undefined,'floor-dm'), dmSummary=node('h3','CEO chat'), form=node('form'), dmTarget=node('select'), input=node('textarea'), button=node('button','Send operator DM'), result=node('p');
    dmTarget.setAttribute('aria-label','Message recipient');input.setAttribute('aria-label','Operator message');input.maxLength=4096;input.required=true;input.rows=3;button.type='submit';result.setAttribute('role','status');result.className='chat-outcome';
    const chatMode=node('select');chatMode.setAttribute('aria-label','Conversation type');for(const [value,label] of [['operator','With operator'],['seats','Between seats'],['crew','Crew notes'],['watch','Watch alerts']]){const o=node('option',label);o.value=value;chatMode.append(o);}chatMode.value='operator';
    const inbox=node('div',undefined,'chat-inbox');inbox.setAttribute('role','region');inbox.setAttribute('aria-label','Questions for the operator');
    const thread=node('div',undefined,'chat-thread');thread.setAttribute('role','log');thread.setAttribute('aria-label','Conversation');thread.tabIndex=0;
    const chatControls=node('div',undefined,'chat-controls');chatControls.append(dmTarget,chatMode);form.append(input,button,result);
    dm.append(dmSummary,node('h4','For you'),inbox,node('p','Recorded requests and replies; not automatically marked resolved.','chat-hint'),chatControls,thread,form);
    const coverage=node('details',undefined,'floor-coverage'), coverageBody=node('div',undefined,'coverage-body');coverage.append(node('summary','Sources and seat desks'),coverageBody);
    const bookBox=node('section',undefined,'floor-book');bookBox.setAttribute('role','region');bookBox.setAttribute('aria-label','Exploratory paper book');
    const reportNotice=node('p','Waiting for report · no reading yet','book-report-status'),reportSnapshot=node('div',undefined,'book-snapshot'),bookStatus=node('p','Paper book · waiting for reading','book-status-line'),bookEvidence=node('details',undefined,'book-evidence'),evidenceSnapshot=node('div');bookStatus.setAttribute('aria-live','polite');bookEvidence.append(node('summary','Sources, status and mark basis'),node('p','Exploratory epoch. Modelled paper fills, no landing proof (b_unmeasured). Not evidence of skill. No inference.','book-warning'),reportNotice,evidenceSnapshot);bookBox.append(node('h3','Exploratory paper book'),bookStatus,reportSnapshot,bookEvidence);let reportFailures=0,lastGoodAt=null,hasGoodReport=false;section.dataset.reportFailures='0';
    const decisions=node('section',undefined,'floor-decisions');decisions.setAttribute('role','region');decisions.setAttribute('aria-label','Recent decisions');
    const work=node('div',undefined,'floor-work'), floor=node('div');floor.append(toolbar,timeline);work.append(floor,dm);section.append(heading,overview,stage,bookBox,decisions,work,coverage);parent.prepend(section);
    let latestReport=null, latest=null, available=false, pending=false, retry=null, rosterKey='', openEvents=new Set(), mailKey='', questionKey='';
    let communications=null,openOperatorDesk=()=>{};const game=root.OfficeFloorV2?.mountV2?.(doc,gameHost,actor=>inspectSeat(actor),(actor,origin)=>communications?.openSeat(actor,origin));
    if(game?.scene)game.scene.prepend(overview);
    if(game?.scene)communications=root.OfficeCommunications?.mount(doc,game.scene,{reply:(actor,message)=>replyTo(actor,message),motion:value=>game.reduceMotion?.(value)});
    function expandBoard(board,label,dialogName){
      const launch=node('button',label,'room-expand'),dialog=node('dialog',undefined,'office-room-expanded'),close=node('button','Close '+dialogName);let slot=null;
      launch.type=close.type='button';dialog.setAttribute('aria-label',dialogName);board.dataset.expanded='false';board.prepend(launch);dialog.append(close);game.scene.append(dialog);
      function restore(){if(slot){slot.replaceWith(board);slot=null;board.dataset.expanded='false';launch.disabled=false;launch.focus();}if(dialog.open)dialog.close();}
      function open(){if(dialog.open)return;slot=node('div',undefined,'board-placeholder');slot.style.gridArea=root.getComputedStyle(board).gridArea;slot.style.height=board.getBoundingClientRect().height+'px';board.replaceWith(slot);dialog.append(board);board.dataset.expanded='true';launch.disabled=true;dialog.showModal();}
      launch.addEventListener('click',open);close.addEventListener('click',()=>dialog.close());dialog.addEventListener('close',restore);return open;
    }
    if(game?.ledger){game.ledger.append(bookBox);expandBoard(game.ledger,'Enlarge ledger','Enlarged paper ledger');game.ledger.append(decisions);}
    if(game?.scene){
      const operatorRoom=communications?.element||node('section',undefined,'post-desk');if(!operatorRoom.isConnected)game.scene.append(operatorRoom);
      operatorRoom.append(work,coverage);const dialogue=game.element.querySelector('.pixel-dialogue');if(dialogue)operatorRoom.append(dialogue);
      openOperatorDesk=expandBoard(operatorRoom,'Open operator inbox','Operator inbox');
      expandBoard(game.screens,'Expand chart wall','Expanded chart wall');
      root.OfficeNetwork?.mount(doc,game.scene);const network=game.scene.querySelector('.office-network-room');if(network)expandBoard(network,'Expand wallet universe','Expanded wallet universe');
    }
    if(doc.head)doc.head.append(node('style',`
#office-v2-work .pixel-scene{display:grid;grid-template-columns:330px minmax(0,1fr) minmax(0,1fr);grid-template-areas:"status status status" "seats charts charts" "ledger inbox universe";gap:12px;padding:12px;background-color:#c6bf96;background-image:linear-gradient(#b9b58d55 1px,transparent 1px),linear-gradient(90deg,#b9b58d55 1px,transparent 1px);background-size:24px 24px;align-items:start}
#office-v2-work .seat-overview{grid-area:status;margin:0;display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:5px}#office-v2-work .seat-overview button{min-width:0;text-align:left;font-size:14px!important;line-height:1.1;padding:5px!important}#office-v2-work .seat-overview :is(strong,span){display:block;overflow-wrap:anywhere}#office-v2-work .seat-overview strong{font-size:16px}#office-v2-work .seat-event-age,#office-v2-work .seat-tool-fact{font-size:14px}
#office-v2-work .pixel-seat[data-pulsing=true]{outline:3px solid #e3b85d!important;box-shadow:0 0 0 5px #223c4355}#office-v2-work .desk-room{grid-area:seats;width:100%;max-width:330px}#office-v2-work .wall-screens{grid-area:charts;align-self:stretch;padding:30px 12px 12px;position:relative;min-width:0;background:#b9c69b;border:6px ridge #6b8870}#office-v2-work .wall-screens .chart-gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,260px));gap:7px;max-height:360px;overflow:auto;padding:2px 7px 6px 0}
#office-v2-work .wall-screens .position-chart{border-width:4px;margin:0;padding:4px;min-width:0}#office-v2-work .wall-screens .position-chart[data-expanded=false] .chart-plot{height:160px}#office-v2-work .wall-screens .position-chart[data-expanded=false] h4{font-size:17px}#office-v2-work .wall-screens .position-chart[data-expanded=false] .monitor-top button{font-size:12px!important}#office-v2-work .wall-screens .office-position-charts>.chart-caption{display:none!important}#office-v2-work .wall-screens .office-position-charts>h3{position:static;width:auto;height:auto;clip-path:none;overflow:visible;font-size:20px}#office-v2-work .wall-screens .chart-status{font-size:15px}#office-v2-work .wall-screens .position-chart .chart-meta{font-size:13px}

#office-v2-work :is(.pixel-ledger,.post-desk,.office-network-room){position:relative;min-width:0;margin:0;padding:34px 8px 8px;border:6px ridge #a87e59;background:#f1e8bd;color:#223c43}#office-v2-work .pixel-ledger{grid-area:ledger}#office-v2-work .post-desk{grid-area:inbox}#office-v2-work .office-network-room{grid-area:universe}
#office-v2-work :is(.pixel-ledger,.post-desk,.office-network-room)[data-expanded=false]{height:265px;overflow:hidden;display:block}#office-v2-work .room-expand{position:absolute;top:4px;right:6px;z-index:2;font-size:15px!important}#office-v2-work [data-expanded=true]>.room-expand{display:none}
#office-v2-work .pixel-ledger[data-expanded=false] .floor-book{padding:0}#office-v2-work .pixel-ledger[data-expanded=false] h3{font-size:18px;margin:0}#office-v2-work .pixel-ledger[data-expanded=false] .book-metrics{display:grid;grid-template-columns:1fr 1fr;font-size:16px;gap:2px}#office-v2-work .pixel-ledger[data-expanded=false] .book-metrics span:nth-child(2),#office-v2-work .pixel-ledger[data-expanded=false] .book-metrics span:nth-child(5){display:none}#office-v2-work .pixel-ledger[data-expanded=false] :is(.book-evidence,.floor-decisions,.book-snapshot>details,.gpt-ledger>details,.gpt-ledger>small){display:none}#office-v2-work .pixel-ledger[data-expanded=false] .gpt-ledger{padding:6px 0;border-top:2px solid #a87e59;margin-top:6px}#office-v2-work .pixel-ledger[data-expanded=false] .gpt-ledger p{font-size:15px!important;margin:2px 0}#office-v2-work .pixel-ledger[data-expanded=false] .book-status-line{font-size:14px}#office-v2-work .pixel-ledger[data-expanded=false] .book-metrics span:nth-child(n+6){grid-column:1/-1}#office-v2-work .pixel-ledger[data-expanded=false] .book-metrics span{white-space:nowrap;text-overflow:ellipsis;overflow:hidden}
#office-v2-work .post-desk[data-expanded=false] :is(.post-tools,.floor-work,.floor-coverage,.pixel-dialogue){display:none}#office-v2-work .post-desk[data-expanded=false] .inbox-items{max-height:150px}#office-v2-work .post-desk[data-expanded=false] .post-note:not(.mail-status){display:none}#office-v2-work .post-desk h3{font-size:22px}
#office-v2-work .office-network-room[data-expanded=false] :is(form,.network-selection,.network-zoom,.network-help,.network-legend,.network-evidence,header>p){display:none}#office-v2-work .office-network-room[data-expanded=false] .network-layout{display:block}#office-v2-work .office-network-room[data-expanded=false] .network-svg{height:155px}#office-v2-work .office-network-room[data-expanded=false] .network-status{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:14px}#office-v2-work .office-network-room h3{font-size:22px}
#office-v2-work .office-room-expanded{background:#f1e8bd;color:#223c43;--floor-ink:#223c43;--floor-muted:#44645b;--floor-edge:#a87e59;border:8px ridge #a87e59;width:min(1250px,96vw);max-height:94vh;overflow:auto;padding:12px}#office-v2-work .office-room-expanded::backdrop{background:#16282bdd}#office-v2-work .office-room-expanded>[data-expanded=true]{height:auto;overflow:visible;padding:8px;border:0;box-shadow:none}#office-v2-work .office-room-expanded .floor-work{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr)}#office-v2-work .office-room-expanded :is(.floor-dm,.floor-decisions,.chat-message,.chat-question){background:#f1e8bd;color:#223c43}#office-v2-work .office-room-expanded .wall-screens .chart-gallery{max-height:none;grid-template-columns:repeat(auto-fill,minmax(200px,1fr))}#office-v2-work .office-room-expanded .floor-book{background:#f1e8bd}#office-v2-work .office-room-expanded .pixel-ledger{display:grid;grid-template-columns:minmax(0,2fr) minmax(220px,1fr)}#office-v2-work .office-room-expanded .pixel-ledger>.floor-decisions{grid-column:1/-1}
@media(max-width:1100px){#office-v2-work .pixel-scene{grid-template-columns:280px minmax(0,1fr);grid-template-areas:"status status" "seats charts" "ledger inbox" "universe universe"}#office-v2-work .seat-overview{grid-template-columns:repeat(4,minmax(0,1fr))}}
@media(max-width:650px){#office-v2-work .pixel-scene{grid-template-columns:minmax(0,1fr);grid-template-areas:"status" "charts" "seats" "ledger" "inbox" "universe";gap:10px;padding:8px}#office-v2-work .seat-overview{grid-template-columns:repeat(2,minmax(0,1fr))}#office-v2-work .wall-screens .chart-gallery{grid-template-columns:minmax(0,1fr);max-height:600px}#office-v2-work .wall-screens .position-chart[data-expanded=false] .chart-plot{height:150px}#office-v2-work .office-room-expanded .floor-work,#office-v2-work .office-room-expanded .pixel-ledger{display:block}#office-v2-work .office-room-expanded .wall-screens .chart-gallery{grid-template-columns:repeat(2,minmax(0,1fr))}}
`));
    const positionCharts=root.OfficePositionCharts?.mount?.(doc,game?.screens||chartsHost,state=>{stage.dataset.chartState=state;if(game?.world)game.world.dataset.chartState=state;game?.redraw?.();});if(!game)gameHost.append(chartsHost);
    const cards=new Map(), actorLabels=new Map();let feedCutAt=null,feedReceiptAt=root.performance.now(),feedStale=false;let inspectedActor=null;const inspector=node('dialog',undefined,'seat-inspector'),inspectorTitle=node('h3','Seat details'),closeInspector=node('button','Close seat details');inspector.setAttribute('aria-label','Seat details');closeInspector.type='button';closeInspector.addEventListener('click',()=>inspector.close());inspector.append(inspectorTitle,closeInspector,cardsBox);(game?.scene||gameHost).append(inspector);cardsBox.hidden=true;inspector.addEventListener('close',()=>{cardsBox.hidden=true;});
    function inspectSeat(actor){if(pending||!cards.has(actor))return;inspectedActor=actor;for(const [id,c] of cards)c.card.hidden=id!==actor;cardsBox.hidden=false;inspectorTitle.textContent=(actorLabels.get(actor)||'Seat')+' · recorded details';replyTo(actor,'',false);if(!inspector.open)inspector.showModal();}
    const gptRegion=communications?node('section',undefined,'gpt-ledger'):null;
    if(gptRegion){gptRegion.setAttribute('role','region');gptRegion.setAttribute('aria-label','Judge GPT paper ledger');game.ledger.append(gptRegion);}
    function watcherLabel(beat){return beat?.state==='known'?'Watcher: '+text(beat.watcher_state)+' · FX '+text(beat.fx_state)+' · age '+(finite(beat.age_s)?beat.age_s.toFixed(1)+'s':'not recorded')+' · last good '+(finite(beat.last_good_utc)?time(beat.last_good_utc)+' UTC':'not recorded'):'Watcher status unavailable · heartbeat could not be read';}
    function drawGpt(report){
      communications?.setReport(report);if(!gptRegion)return;gptRegion.replaceChildren();gptRegion.dataset.stale='false';
      const book=Array.isArray(report?.separate_books)?report.separate_books.find(b=>b.book_id==='office-v2-explore-gpt-1'&&b.epoch_id==='office-v2-explore-gpt-1'):null;
      const money=v=>typeof v==='number'&&Number.isFinite(v)?'$'+v.toFixed(2):'Unknown';
      gptRegion.append(node('h3','Judge GPT · own paper ledger'),node('small','office-v2-explore-gpt-1 · separate capital and orders; never pooled. Modelled, no landing proof.'));
      gptRegion.append(node('p','Cash: '+money(book?.cash_usd)+' · Equity: '+money(book?.state==='known'&&book?.unpriced_count===0?book.equity_usd:null)));
      if(book?.marked_state==='known'&&book?.marked_unpriced_count===0&&typeof book.marked_equity_usd==='number'&&Number.isFinite(book.marked_equity_usd))gptRegion.append(node('p','Marked equity: '+money(book.marked_equity_usd)+' · latest paper marks; may be carried or estimated.'));
      gptRegion.append(node('small','As recorded '+time(book?.as_of)+' UTC · '+text(book?.state)+(book?.reason?' · '+book.reason:'')));
      gptRegion.append(node('small',watcherLabel(book?.watcher_heartbeat)));
      if(book?.limited===true)gptRegion.append(node('small','Order window limited: latest 200 recorded orders; older rows not examined.'));
      const detail=node('details'),summary=node('summary','Judge GPT recorded orders'),orders=node('div',undefined,'gpt-orders');
      for(const order of (book?.orders||[]).slice(0,200)){const label=coinName(order);const unfilled=notFilled(order);const item=node('p',unfilled?label+' · '+unfilled:label+' · '+text(order.state)+' · bought '+money(order.spend_usd)+' · sold '+(order.state==='open'?'not sold':money(order.exit_usd))+' · net '+(order.state==='open'&&!finite(order.modelled_net_usd)?book.state==='known'&&book.unpriced_count===0&&finite(book.equity_usd)?'not realized':'needs proven marks; marked estimate '+(book.marked_state==='known'&&book.marked_unpriced_count===0&&finite(book.marked_equity_usd)?'shown':'unavailable'):money(order.modelled_net_usd))+' modelled'+(['estimated_terminal','estimated_at_request'].includes(order.exit_fill_kind)?' · estimated · upper-biased':''));if(!unfilled)item.append(node('span',root.OfficeOrderDisplay?.prices(order)||'Price reading unavailable','order-prices'));item.append(recordedPlan(order),coinLinks(order),node('small',order.display_ticker_source==='mint_short'?' · ticker not found · exact mint shortened':' · Symbol source: '+text(order.display_ticker_source)));orders.append(item);}
      if(!book?.orders?.length)orders.append(node('p',book&&book.state!=='unknown'?'No recorded orders in this book window.':'Orders unknown.'));
      detail.append(summary,orders);gptRegion.append(detail);
    }
    const names=()=>new Map(rows(latest?.roster).map(a=>[a.actor_id,role(a.role)]));
    function remember(details,id) { details.dataset.eventId=id;details.open=openEvents.has(id);details.addEventListener('toggle',()=>{if(details.open)openEvents.add(id);else openEvents.delete(id);}); }
    function payload(e) { try { const p=JSON.parse(e.text);return p && typeof p==='object' && !Array.isArray(p)?p:{}; } catch { return {}; } }
    function decision(e) { const p=payload(e);return e.kind==='tool_result' && e.refs?.tool==='paper_order' || ['ENTER','WAIT','SKIP'].includes(p.decision) || /^(?:decision\s*:\s*)?(ENTER|WAIT|SKIP)\b/i.test(String(e.text||'').trim()); }
    const finite=v=>typeof v==='number'&&Number.isFinite(v), money=v=>finite(v)?'$'+v.toFixed(2):'Unknown';
    function drawReport(report) {
      const content=doc.createDocumentFragment(),evidence=doc.createDocumentFragment();
      const book=report?.book||{}, now=book.now||{}, segments=report?.segments||{}, known=book.state==='known'&&now.state==='known'&&finite(now.equity_usd)&&now.unpriced_count===0;
      const open=Array.isArray(book.open_positions)&&book.open_positions.length>0,marked=now.marked_state==='known'&&now.marked_unpriced_count===0&&finite(now.marked_equity_usd),needsMarks='needs proven marks; marked estimate '+(marked?'shown':'unavailable');
      const oldOrders=reportSnapshot.querySelector?.('details'),ordersOpen=oldOrders?.open,ordersFocused=!!oldOrders&&oldOrders.querySelector?.('summary')===doc.activeElement;
      const metrics=node('div',undefined,'book-metrics');
      metrics.append(node('span','Cash: '+money(now.cash_usd)),node('span','Closed: '+text(book.closed_count)),node('span','Open: '+(Array.isArray(book.open_positions)?book.open_positions.length:'Unknown')),node('span','Equity: '+(known?money(now.equity_usd):'Unknown')+' · '+text(now.unpriced_count)+' unpriced'),node('span','Daily loss room: '+(finite(book.daily_loss_limit_usd)&&finite(book.daily_realized_usd)?money(Math.max(0,book.daily_loss_limit_usd+Math.min(0,book.daily_realized_usd))):'Unknown')));
      if(now.marked_state==='known'&&now.marked_unpriced_count===0&&finite(now.marked_equity_usd))metrics.append(node('span','Marked equity: '+money(now.marked_equity_usd)+' · latest paper marks; may be carried or estimated.'));
      if(book.state==='unknown'){const reasons=Array.isArray(now.legs)?[...new Set(now.legs.slice(0,16).filter(leg=>leg&&leg.cause!=='known'&&typeof leg.reason==='string').map(leg=>leg.reason))]:[];evidence.append(node('p','Book unknown: '+text(book.reason||reasons.join(', ')||'reason not supplied'),'book-report-status'));}
      const bindings=now.binding_kind_counts,bindingsKnown=bindings&&typeof bindings==='object'&&!Array.isArray(bindings)&&Object.values(bindings).every(n=>Number.isInteger(n)&&n>=0),bindingRows=bindingsKnown?Object.entries(bindings).slice(0,16).filter(([,n])=>Number.isInteger(n)&&n>=0):[];
      evidence.append(node('p','FX '+text(now.fx_source)+' · age '+(finite(now.fx_age_s)?now.fx_age_s.toFixed(1)+'s':'Unknown'),'chat-hint'),node('p','Priced open marks: '+(bindingsKnown?(bindingRows.map(([kind,n])=>String(kind).slice(0,80)+' '+n).join(', ')||'none'):'Unknown'),'chat-hint'));
      content.append(metrics,node('p',known&&finite(book.office_net)?'NET (all exits, modelled) '+money(book.office_net):open&&!known?'NET: '+needsMarks:'NET withheld · equity or costs unavailable'));evidence.append(node('p','Report '+time(report?.now)+' UTC · '+text(report?.state)+' · refreshes every minute','chat-hint'),node('h4','Segments'));
      evidence.append(node('p',watcherLabel(book.watcher_heartbeat),'chat-hint'));
      if(open&&!known)content.append(node('p','Statistics: '+needsMarks,'chat-hint'));
      const segmentList=node('div',undefined,'book-segments');
      for(const row of rows(segments)){segmentList.append(node('p','Segment '+text(row.segment)+' · '+time(row.at)+' · '+text(row.kind)+' · '+text(row.release_sha)));}
      if(!rows(segments).length)segmentList.append(node('p','Segments '+text(segments.state)+(segments.items_omitted?' · rows omitted by report limit':'')));
      evidence.append(segmentList,node('p','NET uses proven open marks; recorded modelled cashflows may include entry fills priced on a carried quote and exit fills priced on an estimate.','chat-hint'),node('p','Estimated exit fills are upper-biased modelled cashflows, not outcome evidence.','book-warning'),node('p','NET without rail_gap_exit: unavailable · this report does not calculate that counterfactual book.','chat-hint'),node('p','Null comparison: '+(report?.null_arm?.state==='known'?'known':'unavailable · complete feed and paper-fill inputs have not been recorded')+' · source: explore_report','chat-hint'));
      const orders=node('details'),summary=node('summary','Recorded paper orders · '+text(report?.orders?.state));orders.open=!!ordersOpen;orders.append(summary);
      const scroll=node('div',undefined,'book-orders-scroll'),table=node('table',undefined,'paper-orders'),head=node('thead'),headRow=node('tr'),body=node('tbody');
      table.setAttribute('aria-label','Recorded paper orders');scroll.tabIndex=0;scroll.setAttribute('role','region');scroll.setAttribute('aria-label','Paper order columns; scroll horizontally');orders.append(node('p','Scroll sideways for Sold, Net and Exit.','order-scroll-hint'));orders.append(node('p',rows(report?.orders).length+' shown of '+text(report?.orders?.count)+' recorded · newest first','chat-hint'));
      const omitted=report?.orders?.items_omitted;if(finite(omitted)&&Number.isInteger(omitted)&&omitted>=0)orders.append(node('p',omitted+' older rows omitted','chat-hint'));else if(omitted===true&&rows(report?.orders).length)orders.append(node('p','Older rows omitted; count not supplied','chat-hint'));
      for(const label of ['Coin','State','Bought','Sold','Net · modelled','Held','Exit']){const th=node('th',label);th.scope='col';headRow.append(th);}head.append(headRow);table.append(head,body);scroll.append(table);orders.append(scroll);
      const instant=v=>validTime(v)?new Date(v*1000).toISOString().slice(0,19).replace('T',' ')+' UTC':'Time unknown';
      const signed=v=>finite(v)?(v<0?'−$':v>0?'+$':'$')+Math.abs(v).toFixed(2):'Unknown';
      const duration=v=>!finite(v)||v<0?'Unknown':v>=3600?Math.floor(v/3600)+'h '+Math.floor(v%3600/60)+'m':v>=60?Math.floor(v/60)+'m '+Math.floor(v%60)+'s':Math.floor(v)+'s';
      const openMetadata=new Set(Array.from(oldOrders?.querySelectorAll?.('details[open][data-order-id]')||[]).map(e=>e.dataset.orderId)),focusedOrder=doc.activeElement?.closest?.('details[data-order-id]')?.dataset.orderId;let orderFocus=null;
      for(const row of rows(report?.orders)){
        const state=row.state??row.status,exitKind=row.exit_fill_kind,exitLabel=exitKind==null?(state==='open'?'none (open)':state==='closed'?'strict (closed before label)':'unlabelled'):['strict','estimated_terminal','estimated_at_request'].includes(exitKind)?exitKind:'unlabelled';
        const item=node('tr',undefined,'book-order'),coin=node('td'),mint=typeof row.mint==='string'?row.mint:'Mint unknown',name=coinName(row);
        const detail=node('details'),detailTitle=node('summary','Details');detail.dataset.orderId=String(row.order_id);detail.open=openMetadata.has(detail.dataset.orderId);detail.append(detailTitle,node('p','Full mint: '+mint),node('p','Display symbol source: '+(row.display_ticker_source==='mint_short'?'ticker not found · exact mint shortened':text(row.display_ticker_source))+' · source time '+time(row.display_ticker_at)),node('p','Segment '+text(row.segment_id)+' · policy '+text(row.policy_identity_state)+' · '+text(row.book_policy_sha256)),node('p','operator_influenced '+text(row.operator_influenced)+' · source '+text(row.source)+' · '+text(row.label_state)),node('p','Quote source '+text(row.virtual_quote_source)));
        detail.append(recordedPlan(row));
        if(report?.orders?.recorded_fields_omitted===true)detail.append(node('p','Recorded detail fields omitted by report limit; coin and row values remain available.'));
        if(focusedOrder===detail.dataset.orderId)orderFocus=detailTitle;coin.append(node('strong',name),coinLinks(row),detail);item.append(coin,node('td',text(state)));
        if(notFilled(row)){const pending=node('td');pending.colSpan=5;pending.append(node('strong',notFilled(row)),node('small','Requested '+instant(row.requested_at),'order-note'));item.append(pending);}
        else{
          const buy=node('td'),sell=node('td'),net=node('td'),exit=node('td');
          buy.append(node('p',root.OfficeOrderDisplay?.prices(row)||'Price reading unavailable','order-prices'),node('p','Bought '+money(row.spend_usd)),node('small',instant(row.filled_at),'order-note'),node('small','Binding '+text(row.entry_binding_kind),'order-note'));
          if(state==='closed'){sell.append(node('p','Sold '+money(row.exit_usd)),node('small',instant(row.closed_at),'order-note'),node('small','Binding '+text(row.exit_binding_kind),'order-note'));}else sell.append(node('p','Not sold'));
          // Keep row-only: future totals/win counts must follow exit-fill strata; never blend strict means with estimated exits.
          net.append(node('strong','Net '+(state==='open'&&!finite(row.modelled_net_usd)?known?'not realized':needsMarks:signed(row.modelled_net_usd))+(['estimated_terminal','estimated_at_request'].includes(exitKind)?' (upper-biased)':'')),node('small',(finite(row.modelled_net_pct)?(row.modelled_net_pct>0?'+':'')+row.modelled_net_pct.toFixed(1)+'%':state==='open'?'not realized':'% not recorded')+' · modelled','order-note'),node('small','Costs '+money(row.costs_usd),'order-note'));
          exit.append(node('p','Exit reason: '+text(row.exit_reason)),node('small','Exit fill: '+exitLabel+' · trigger: '+text(row.exit_trigger_kind),'order-note'));
          if(row.exit_requested_at!=null)exit.append(node('small','exit pending '+duration(row.exit_pending_s),'order-note'));
          if(['estimated_terminal','estimated_at_request'].includes(exitKind))exit.append(node('strong','estimated · upper-biased','order-upper-bias'));
          item.append(buy,sell,net,node('td',duration(row.hold_s)),exit);
        }
        body.append(item);
      }
      if(!rows(report?.orders).length){const tr=node('tr'),td=node('td',report?.orders?.items_omitted||finite(report?.orders?.count)&&report.orders.count>0?'No row details included in this report; older rows were omitted.':report?.orders?.state==='known'?'No orders in this report window.':'Orders unknown.');td.colSpan=7;tr.append(td);body.append(tr);}
      orders.append(node('p','Net = sold − bought − recorded costs; % of bought amount. Modelled.','chat-hint'));
      content.append(orders);reportSnapshot.replaceChildren(content);evidenceSnapshot.replaceChildren(evidence);if(orderFocus)orderFocus.focus({preventScroll:true});else if(ordersFocused)summary.focus({preventScroll:true});
    }
    function sourceUnknown(reason){bookStatus.textContent='Paper book · reading unavailable'+(hasGoodReport?' · previous reading stale':'');if(!hasGoodReport)drawReport(null);reportSnapshot.dataset.stale=String(hasGoodReport);reportNotice.dataset.state='source_unknown';reportNotice.textContent='Source state unknown: '+String(reason||'reason not provided').slice(0,160)+(hasGoodReport?' · last good reading '+time(lastGoodAt)+' UTC · stale snapshot':' · no good reading yet')+' · failures '+reportFailures;}
    function renderReport(report){
      drawGpt(report);
      if(report==null){bookStatus.textContent='Paper book · waiting for reading';drawReport(null);hasGoodReport=false;lastGoodAt=null;reportSnapshot.dataset.stale='false';reportNotice.dataset.state='waiting';reportNotice.textContent='Waiting for report · no reading yet';return;}
      if(report.state==='unknown'){sourceUnknown(report.reason);return;}
      if(report.epoch_id!=='office-v2-explore-1'){hasGoodReport=false;lastGoodAt=null;sourceUnknown('epoch_mismatch');return;}
      drawReport(report);latestReport=report;drawDecisions();hasGoodReport=true;lastGoodAt=finite(report.now)?report.now:null;reportSnapshot.dataset.stale='false';reportNotice.dataset.state='loaded';bookStatus.textContent='Paper book · '+time(report.now)+' UTC · modelled';reportNotice.textContent='Report loaded · source '+text(report.state)+' · failures '+reportFailures;
    }
    function reportFailed(kind,error){bookStatus.textContent='Paper book · refresh failed'+(hasGoodReport?' · previous reading stale':'');communications?.reportUnavailable?.();if(gptRegion){gptRegion.dataset.stale='true';gptRegion.append(node('small','Report refresh failed · previous GPT book reading is stale.'));}reportFailures++;section.dataset.reportFailures=String(reportFailures);reportNotice.dataset.state=kind==='render'?'render_error':'failed';reportSnapshot.dataset.stale=String(hasGoodReport);reportNotice.textContent=(kind==='render'?'Report render error: ':'Report failed: ')+kind+' · '+(hasGoodReport?'last good reading '+time(lastGoodAt)+' UTC · stale snapshot':'no good reading yet')+' · failures '+reportFailures;root.console?.error('[office-report] '+kind,{failures:reportFailures,name:String(error?.name||'Error'),message:String(error?.message||'').slice(0,200)});}
    function drawDecisions() {
      decisions.replaceChildren();decisions.append(node('h3','Recent decisions'),node('p','Explicit statements and paper-order responses in the current feed window; statements are not admissions.','chat-hint'));
      const list=node('div',undefined,'decision-list');
      for(const e of rows(latest?.events).filter(decision).sort((a,b)=>(b.corrected_at||0)-(a.corrected_at||0))){const p=payload(e),matches=typeof p.order_id==='string'?[...rows(latestReport?.orders),...(latestReport?.separate_books||[]).flatMap(b=>b.orders||[])].filter(o=>o.order_id===p.order_id):[],order=matches.length===1?matches[0]:null,label=e.refs?.tool==='paper_order'?'Paper order '+text(p.state):p.decision||String(e.text||'').trim().match(/(?:ENTER|WAIT|SKIP)/i)?.[0]?.toUpperCase()||'Decision';list.append(node('p',time(e.corrected_at)+' · '+(actorLabels.get(e.actor_id)||'Seat')+' · '+label+' · coin '+text(p.mint??p.coin??order?.ticker??order?.display_ticker??order?.mint??'not recorded in this response or ledger window')+' · '+text(p.reason??e.text)));}
      if(!list.childNodes?.length)list.append(node('p','No explicit decision observed.'));decisions.append(list);
    }
    function drawTimeline(reset=false) {
      const top=reset?0:timeline.scrollTop||0, focused=doc.activeElement?.closest?.('[data-event-id]')?.dataset.eventId, focusedRegion=doc.activeElement===timeline;
      timeline.replaceChildren();let focusTarget=null;
      const entries=rows(latest?.events).filter(e=>!(e.kind==='thought' && !String(e.text||'').trim()) && e.kind!=='tool_call').filter(e=>!seatFilter.value || seatFilter.value==='all' || e.actor_id===seatFilter.value).filter(e=>kindFilter.value==='thoughts'?e.kind==='thought':kindFilter.value==='decisions'?decision(e):true).sort((a,b)=>(validTime(b.corrected_at)?b.corrected_at:-1)-(validTime(a.corrected_at)?a.corrected_at:-1));
      const labels=names();
      for (const e of entries) {
        const id=text(e.event_id??e.seq), who=labels.get(e.actor_id)||'Seat', raw=String(e.text??''), name=rows(latest?.roster).find(a=>a.actor_id===e.actor_id)?.role?.toLowerCase();
        if (e.kind==='thought') {
          const item=node('article',undefined,'floor-note');item.dataset.role=name;const head=node('div',undefined,'note-top');head.append(node('time',time(e.corrected_at)),node('span',who,'seat-name'));const body=node('div',undefined,'floor-prose');markdown(body,raw.length>650?raw.slice(0,650)+'…':raw);item.append(head,body);
          if(raw.length>650){const more=node('details');remember(more,id);const summary=node('summary','Read full thought');const full=node('div',undefined,'floor-prose');markdown(full,raw);more.append(summary,full);item.append(more);if(focused===id)focusTarget=summary;}
          timeline.append(item);
        } else {
          const p=payload(e), tool=e.refs?.tool, error=e.refs?.is_error===true;
          let action=e.kind==='tool_result'?(verbs[tool]||'Tool result'):e.kind==='lifecycle'?({wake_start:'Started a wake',wake_end:'Finished a wake',sleep:'Set next wake',parked:'Parked'}[raw]||raw):e.kind==='workspace'?'Updated the desk':e.kind==='standup'?'Standup':e.kind==='request'?'Question for operator':e.kind;
          if(tool==='paper_order')action='Paper order · '+text(p.state).toLowerCase();
          const reason=p.reason||e.refs?.reason;
          if(reason)action+=' · '+String(reason).replace(/_/g,' ');else if(e.kind==='tool_result')action+=' · '+(error?'error':e.refs?.is_error===false?'returned':'outcome unknown');
          const item=node('details',undefined,'floor-chip');item.dataset.role=name;remember(item,id);const summary=node('summary');summary.append(node('time',time(e.corrected_at)),node('span',who,'seat-name'),node('span',action,error?'floor-error':''));
          item.append(summary,node('pre',raw||JSON.stringify(e.refs||{})));timeline.append(item);if(focused===id)focusTarget=summary;
        }
      }
      if(!entries.length)timeline.append(node('p',kindFilter.value==='decisions'?'No explicit decisions recorded in this window.':'No matching activity observed in this window.','floor-empty'));
      timeline.scrollTop=top;if(focusTarget)focusTarget.focus({preventScroll:true});if(focusedRegion)timeline.focus({preventScroll:true});
    }
    function targetAvailable(value) { return available && (value==='all' ? rows(latest?.roster).length>0 : rows(latest?.roster).some(a=>a.actor_id===value)); }
    function replyTo(actor,question='',focus=true) { if(pending)return;dmTarget.value=actor;chatMode.value='operator';if(question&&!input.value.trim())input.value='Re: '+String(question).replace(/\s+/g,' ').slice(0,160)+'\n\n';drawChat();button.disabled=!targetAvailable(dmTarget.value);if(focus){openOperatorDesk();input.focus();} }
    function drawChat() {
      const isCrew=id=>id==='crew'||String(id).startsWith('crew:');
      const who=id=>id==='operator'?'You':isCrew(id)?'Crew note':id==='watch'?'Watch alert':actorLabels.get(id)||'Seat';
      const selected=dmTarget.value, mode=chatMode.value||'operator';
      const values=rows(latest?.mail).filter(m=>selected==='all'||m.from_actor_id===selected||m.to_actor_id===selected).filter(m=>mode==='crew'?isCrew(m.from_actor_id):mode==='watch'?m.from_actor_id==='watch':mode==='seats'?!['operator','watch'].includes(m.from_actor_id)&&!isCrew(m.from_actor_id)&&m.to_actor_id!=='operator':m.from_actor_id==='operator'||m.to_actor_id==='operator').sort((a,b)=>a.created_at-b.created_at);
      const key=[selected,mode,rosterKey,...values.map(m=>m.message_id)].join('|');
      if(key!==mailKey){mailKey=key;const top=thread.scrollTop||0,atBottom=(thread.scrollHeight||0)-(thread.clientHeight||0)-top<24;thread.replaceChildren();
        for(const m of values){const item=node('article',undefined,'chat-message');item.dataset.sender=m.from_actor_id==='operator'?'operator':isCrew(m.from_actor_id)?'crew':m.from_actor_id==='watch'?'watch':'seat';const header=node('header');header.append(node('span',who(m.from_actor_id)+' → '+who(m.to_actor_id)),node('time',time(m.created_at)));const body=node('div',undefined,'floor-prose');markdown(body,m.text);item.append(header,body);thread.append(item);}
        if(!values.length)thread.append(node('p',latest?.mail?.state==='unknown'?'Mail state unknown.':'No messages observed in this conversation window.','floor-empty'));
        thread.scrollTop=atBottom?thread.scrollHeight:top;
      }
      const questions=[...rows(latest?.questions).map(q=>({...q,id:'q:'+q.event_id,at:q.corrected_at,kind:'Request'})),...rows(latest?.mail).filter(m=>m.to_actor_id==='operator').map(m=>({actor_id:m.from_actor_id,text:m.text,id:'m:'+m.message_id,at:m.created_at,kind:m.notify_operator===true?'Seat alert · unverified':'Seat reply',notify:m.notify_operator===true}))].sort((a,b)=>b.at-a.at);
      const qkey=[rosterKey,pending,...questions.map(q=>q.id)].join('|');
      if(qkey!==questionKey){questionKey=qkey;const focused=doc.activeElement?.dataset?.questionId;let focus=null;inbox.replaceChildren();
        for(const q of questions.slice(0,7)){const item=node('article',undefined,'chat-question');item.dataset.notification=String(q.notify===true);item.append(node('span',who(q.actor_id)+' · '+q.kind+' · '+time(q.at),'chat-hint'),node('p',String(q.text).slice(0,240)));const reply=node('button','Reply to '+who(q.actor_id));reply.type='button';reply.dataset.questionId=q.id;reply.disabled=pending||!targetAvailable(q.actor_id);reply.addEventListener('click',()=>replyTo(q.actor_id,q.text));item.append(reply);inbox.append(item);if(focused===q.id)focus=reply;}
        if(!questions.length)inbox.append(node('p',latest?.questions?.state==='unknown'?'Request state unknown.':'No requests or replies observed in this window.','chat-hint'));
        if(questions.length>7)inbox.append(node('p','Showing the 7 newest of '+questions.length+' recorded items.','chat-hint'));
        if(focus)focus.focus({preventScroll:true});
      }
    }
    function render(feed) {
      if(feed?.as_of!==feedCutAt){feedCutAt=feed?.as_of;feedReceiptAt=root.performance.now();}feedStale=false;game?.render(feed);communications?.render(feed);latest=feed;available=feed?.roster?.state==='known';for(const a of rows(feed?.roster))actorLabels.set(a.actor_id,role(a.role));const actors=rows(feed?.roster).sort((a,b)=>roleOrder.indexOf(a.role?.toLowerCase())-roleOrder.indexOf(b.role?.toLowerCase()));
      const key=actors.map(a=>[a.actor_id,a.role,a.generation,a.incarnation].join(':')).join('|');
      if(key!==rosterKey){rosterKey=key;const selected=seatFilter.value,target=dmTarget.value;seatFilter.replaceChildren();if(!pending)dmTarget.replaceChildren();const all=node('option','All seats');all.value='all';seatFilter.append(all);
        for(const a of actors){for(const select of pending?[seatFilter]:[seatFilter,dmTarget]){const o=node('option',role(a.role));o.value=a.actor_id;select.append(o);}}
        if(!pending){const broadcast=node('option','All seats (broadcast)');broadcast.value='all';dmTarget.append(broadcast);}
        seatFilter.value=actors.some(a=>a.actor_id===selected)?selected:'all';if(!pending){if(target&&target!=='all'&&!actors.some(a=>a.actor_id===target)&&input.value.trim()){const o=node('option',(actorLabels.get(target)||'Seat')+' (unavailable)');o.value=target;dmTarget.append(o);dmTarget.value=target;}else dmTarget.value=target==='all'||actors.some(a=>a.actor_id===target)?target:actors[0]?.actor_id||'';}
        cardsBox.replaceChildren();overview.replaceChildren();cards.clear();
        for(const a of actors){const card=node('article',undefined,'floor-seat');card.dataset.role=a.role?.toLowerCase();card.hidden=a.actor_id!==inspectedActor;const top=node('div',undefined,'seat-top'),reply=node('button','Reply','seat-reply');reply.type='button';reply.addEventListener('click',()=>{if(inspector.open)inspector.close();replyTo(a.actor_id);});top.append(node('h3',role(a.role)),reply);const state=node('p',undefined,'seat-state'),activity=node('p',undefined,'seat-activity'),thought=node('div',undefined,'seat-thought'),stats=node('div',undefined,'seat-stats'),wakes=node('b'),tokens=node('b'),tokensNote=node('small',undefined,'tokens-note'),w=node('span'),t=node('span');w.append(wakes,node('span','Wakes today'));t.append(tokens,node('span','Tokens today'),tokensNote);stats.append(w,t);card.append(top,state,activity,thought,stats);const activityButton=node('button');activityButton.type='button';activityButton.setAttribute('aria-haspopup','dialog');activityButton.addEventListener('click',()=>inspectSeat(a.actor_id));overview.append(activityButton);cards.set(a.actor_id,{card,state,activity,thought,wakes,tokens,tokensNote,reply,activityButton});cardsBox.append(card);}
      }
      const observedSeats=root.OfficeFloorV2?.v2Scene?.(feed);
      for(const a of actors){const c=cards.get(a.actor_id),s=rows(feed?.seats).find(s=>s.actor_id===a.actor_id)||{},observed=observedSeats?.find(v=>v.actor===a.actor_id),activity=observedSeats?{kind:observed?.mode||'unknown',until:observed?.until,reason:observed?.reason}:s.activity||{};
        c.state.textContent=activity.kind==='working'?'Working now':activity.kind==='sleeping'?'Sleeping until '+time(activity.until)+' UTC':activity.kind==='idle'?'Idle · waiting for work':activity.kind==='parked'?'Parked: '+text(activity.reason).replace(/_/g,' '):'State unknown';
        c.activityButton.textContent=role(a.role)+' · '+c.state.textContent;
        c.activity.textContent=activity.kind==='working'?(s.current_tool?'Last action: '+(verbs[s.current_tool]||text(s.current_tool)):'Working on the current wake'):['sleeping','idle'].includes(activity.kind)?'Waiting for mail or new work':activity.kind==='parked'?'Waiting for the host guard to clear':'Awaiting a host observation';
        c.thought.replaceChildren();markdown(c.thought,s.last_thought||'No thought observed in this window.');
        c.wakes.textContent=Number.isInteger(s.wakes_today)&&s.wakes_today>=0?s.wakes_today.toLocaleString():'Unknown';const knownTokens=Number.isInteger(s.tokens_today)&&s.tokens_today>=0?s.tokens_today:s.tokens_known;
        c.tokens.textContent=Number.isInteger(knownTokens)&&knownTokens>=0?knownTokens.toLocaleString():a.role?.toLowerCase()==='judge_gpt'?'not captured for GPT':'usage not captured';
        c.tokensNote.textContent=s.tokens_today===null&&a.role?.toLowerCase()==='judge_gpt'?'not captured for GPT':s.tokens_today===null&&Number.isInteger(s.tokens_unknown_wakes)&&s.tokens_unknown_wakes>0?'Usage not captured for '+s.tokens_unknown_wakes+' '+(s.tokens_unknown_wakes===1?'wake':'wakes'):'';c.reply.disabled=pending||!available;
      }
      if(inspector.open&&!cards.has(inspectedActor)){inspector.close();inspectedActor=null;status.tabIndex=-1;status.focus({preventScroll:true});}
      status.textContent=available?actors.length+' seats · '+(feed.as_of?time(feed.as_of)+' UTC':'UTC'):'Floor connection uncertain';
      drawTimeline();coverageBody.replaceChildren();coverageBody.append(node('p','Activity window: '+text(feed?.events?.state)+(feed?.events?.limited?' · bounded tail':'')+'. '+(feed?.tasks?.state==='unknown'||!feed?.tasks?feed?.roster?.state==='known'&&!rows(feed?.tasks).length?'no task lists published':'Task lists unavailable in this observation window':('Task lists: '+text(feed.tasks.state)))+'. Missing lists do not mean no work.'));
      coverageBody.append(node('p',feed?.seats?.coverage||'Seat state and counters unavailable.'));
      coverageBody.append(node('p',rows(feed?.mail).length ? 'Recorded messages: '+rows(feed.mail).length : feed?.mail?.state==='known' ? 'No messages in this window.' : 'Messages unknown in this window.'));
      for(const a of actors){const desk=rows(feed?.events).find(e=>e.actor_id===a.actor_id&&e.kind==='workspace');coverageBody.append(node('h3',role(a.role)),node('p','Identity '+a.actor_id+' · generation '+text(a.generation)+' · incarnation '+text(a.incarnation)),node('p','Desk '+text(desk?.state)),node('pre',desk?.refs?.notebook_text||'Notebook unknown.'));
      }
      updateSeatFacts();drawChat();drawDecisions();button.disabled=pending||!targetAvailable(dmTarget.value);
    }
    function updateSeatFacts(){
      if(doc.hidden||!section.isConnected||!section.getClientRects().length)return;
      const now=finite(feedCutAt)?feedCutAt+Math.max(0,(root.performance.now()-feedReceiptAt)/1000):null;
      for(const [actor,c] of cards){const seat=rows(latest?.seats).find(s=>s.actor_id===actor)||{},at=seat.last_event_at;
        const age=finite(at)&&at>=0&&finite(feedCutAt)&&at<=feedCutAt?Math.max(0,Math.floor(now-at)):null;
        const ageText=age===null?'Last event time unavailable':('Last event '+(age<60?age+'s':Math.floor(age/60)+'m '+age%60+'s')+' ago'+(seat.last_event_kind?' · '+String(seat.last_event_kind).replaceAll('_',' '):''));
        const tool=seat.last_tool||seat.current_tool,toolText=tool?((seat.tool_event_state==='called'&&seat.activity?.kind==='working'?'Tool: ':'Last tool: ')+String(tool).replaceAll('_',' ')+(seat.tool_event_state?' · '+seat.tool_event_state:'')):'Tool not reported';
        c.activityButton.replaceChildren(node('strong',(actorLabels.get(actor)||'Seat')+' · '+c.state.textContent),node('span',ageText+(feedStale?' · stale':''),'seat-event-age'),node('span',toolText,'seat-tool-fact'));
        if(c.card){let event=c.card.querySelector('.seat-last-event');if(!event){event=node('p',undefined,'seat-last-event');c.activity.after(event);}event.textContent=ageText+(feedStale?' · stale':'');c.activity.textContent=toolText;}
      }
    }
    const seatFactsTimer=root.setInterval(updateSeatFacts,1000);seatFactsTimer.unref?.();
    function unavailable(){feedStale=true;game?.unavailable();communications?.unavailable();available=false;status.textContent='Connection lost · previous observations may be stale';button.disabled=true;for(const [actor,c] of cards){c.state.textContent='State unknown · connection lost';c.activityButton.textContent=(actorLabels.get(actor)||'Seat')+' · '+c.state.textContent;}}
    async function submit(event){event.preventDefault();if(pending||!targetAvailable(dmTarget.value)||!input.value.trim())return;const body={to_actor_id:dmTarget.value,text:input.value},recipient=body.to_actor_id==='all'?'all seats':actorLabels.get(body.to_actor_id)||'Seat';if(!retry||retry.to_actor_id!==body.to_actor_id||retry.text!==body.text)retry={...body,client_id:options.id()};pending=true;button.disabled=true;input.disabled=true;dmTarget.disabled=true;drawChat();
      try{const response=await options.send(retry),valid=r=>['sent','duplicate'].includes(r?.state)&&typeof r.message_id==='string'&&r.message_id;
        if(body.to_actor_id==='all'){const receipts=response?.seats&&typeof response.seats==='object'&&!Array.isArray(response.seats)?Object.entries(response.seats):[];if(!receipts.length||receipts.some(([,r])=>!valid(r)))throw Error('Broadcast outcome unknown');result.textContent=receipts.map(([actor,r])=>(actorLabels.get(actor)||'Seat')+': stored '+r.state+' · '+(typeof r.delivery==='string'?r.delivery:'delivery state unknown')).join('\n');}
        else{if(!valid(response))throw Error('DM outcome unknown');result.textContent='Stored '+response.state+' for '+recipient+' · '+(typeof response.delivery==='string'?response.delivery:'delivery state unknown');}
        result.textContent+='\nDelivery is a snapshot at send; a seat read is unconfirmed.';input.value='';retry=null;
      }catch(error){result.textContent=text(error.message)+'. Send unconfirmed; retry keeps the same message identity.';}
      finally{pending=false;input.disabled=false;dmTarget.disabled=false;rosterKey='';if(latest)render(latest);button.disabled=!targetAvailable(dmTarget.value);}
    }
    dmTarget.addEventListener('change',()=>{drawChat();button.disabled=pending||!targetAvailable(dmTarget.value);});chatMode.addEventListener('change',drawChat);
    seatFilter.addEventListener('change',()=>drawTimeline(true));kindFilter.addEventListener('change',()=>drawTimeline(true));form.addEventListener('submit',submit);
    renderReport(null);return {render,renderReport,reportFailed,unavailable,input,submit,renderCharts:data=>positionCharts?.render(data),chartsUnavailable:()=>positionCharts?.unavailable(),chartsVisible:()=>positionCharts?.isVisible()||false};
  }
  if(typeof module!=='undefined'&&module.exports)module.exports={mount};
  if(typeof document==='undefined')return;
  const panel=document.getElementById('tab-desk'),parent=panel||document.querySelector('main');if(!parent)return;
  const ui=mount(document,parent,{id:()=>root.crypto.randomUUID(),send:async body=>{const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),5000);try{const r=await fetch('/api/office/dm',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal:controller.signal});const value=await r.json();if(!r.ok)throw Error('DM refused: '+text(value.error));return value;}finally{clearTimeout(timer);}}});
  let running=false;async function refresh(){if(running||document.hidden||panel&&!panel.classList.contains('active'))return;running=true;const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),2500);try{const r=await fetch('/api/office/feed?limit=200',{cache:'no-store',credentials:'same-origin',signal:controller.signal});if(!r.ok)throw Error('Floor unavailable');ui.render(await r.json());}catch{ui.unavailable();}finally{clearTimeout(timer);running=false;}}
  let reportRunning=false;async function refreshReport(){
    if(reportRunning||document.hidden||panel&&!panel.classList.contains('active'))return;reportRunning=true;const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),2500);
    try{
      let response;try{response=await fetch('/api/office/explore',{cache:'no-store',credentials:'same-origin',signal:controller.signal});}catch(error){ui.reportFailed(error?.name==='AbortError'?'timeout':'network',error);return;}
      if(!response.ok){ui.reportFailed('http_'+response.status,new Error('HTTP '+response.status));return;}
      let data;try{data=await response.json();if(!data||typeof data!=='object'||Array.isArray(data)||!['known','partial','unknown'].includes(data.state))throw new TypeError('Invalid report object');}catch(error){ui.reportFailed(error?.name==='AbortError'?'timeout':'parse',error);return;}
      try{ui.renderReport(data);}catch(error){ui.reportFailed('render',error);}
    }finally{clearTimeout(timer);reportRunning=false;}
  }
  if(panel){let wasActive=panel.classList.contains('active');new root.MutationObserver(()=>{const active=panel.classList.contains('active');if(active&&!wasActive)refreshReport();wasActive=active;}).observe(panel,{attributes:true,attributeFilter:['class']});}
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshReport();});
  let chartsRunning=false;async function refreshCharts(){if(chartsRunning||document.hidden||panel&&!panel.classList.contains('active')||!ui.chartsVisible())return;chartsRunning=true;const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),2500);try{const response=await fetch('/api/office/positions/charts',{cache:'no-store',credentials:'same-origin',signal:controller.signal});if(!response.ok)throw Error('Chart read unavailable');ui.renderCharts(await response.json());}catch{ui.chartsUnavailable();}finally{clearTimeout(timer);chartsRunning=false;}}
  refreshCharts();setInterval(refreshCharts,5000);
  document.getElementById('refresh')?.addEventListener('click',()=>{refresh();refreshReport();refreshCharts();});
  refresh();refreshReport();setInterval(refresh,5000);setInterval(refreshReport,60000);
})(typeof window!=='undefined'?window:globalThis);
