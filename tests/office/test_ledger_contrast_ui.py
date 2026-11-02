"""WCAG AA contrast over every rendered ledger text node, including stale/detail/dialog states."""
from playwright.sync_api import sync_playwright
from office.test_communication_ui import start
from office.test_embedded_office_ui import dashboard_css

CONTRAST=r'''root=>{
 const rgba=s=>{const m=s.match(/[\d.]+/g);return m?m.map(Number):[0,0,0,0]},blend=(a,b)=>a.slice(0,3).map((v,i)=>v*(a[3]??1)+b[i]*(1-(a[3]??1))),lum=c=>c.map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4).reduce((a,v,i)=>a+v*[.2126,.7152,.0722][i],0),out=[];
 const walk=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);let t;
 while(t=walk.nextNode()){
  if(!t.textContent.trim()||['STYLE','SCRIPT'].includes(t.parentElement.tagName))continue;
  const r=document.createRange();r.selectNodeContents(t);if(![...r.getClientRects()].some(r=>r.width&&r.height))continue;
  const e=t.parentElement,chain=[];for(let p=e;p;p=p.parentElement)chain.unshift(p);
  let bg=[255,255,255],opacity=1;for(const p of chain){const s=getComputedStyle(p);bg=blend(rgba(s.backgroundColor),bg);opacity*=Number(s.opacity)}
  const raw=rgba(getComputedStyle(e).color);raw[3]=(raw[3]??1)*opacity;const fg=blend(raw,bg),a=lum(fg),b=lum(bg),ratio=(Math.max(a,b)+.05)/(Math.min(a,b)+.05);
  out.push({text:t.textContent.trim().slice(0,100),ratio,fg,bg});
 }return out;
}'''


def test_all_ledger_text_meets_aa_in_room_details_stale_and_enlarged():
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        try:
            page=b.new_page(viewport={'width':390,'height':844});start(page);dashboard_css(page)
            page.evaluate("ui.renderReport({state:'known',epoch_id:'office-v2-explore-1',now:100,book:{state:'known',now:{state:'known',equity_usd:1000,unpriced_count:0},office_net:0},orders:{state:'known',items:[{order_id:'one',state:'closed',ticker:'FUN',exit_fill_kind:'estimated_terminal',modelled_net_usd:0}]},segments:{state:'known',items:[]},separate_books:[{book_id:'office-v2-explore-gpt-1',epoch_id:'office-v2-explore-gpt-1',state:'known',orders:[{order_id:'g',state:'closed',ticker:'FUN',exit_fill_kind:'estimated_terminal'}]}]})")
            compact=page.locator('.pixel-ledger').evaluate(CONTRAST)
            assert len(compact)>5 and all(s['ratio']>=4.5 for s in compact)
            page.get_by_role('button',name='Enlarge ledger').click()
            for stale in (False,True):
                page.evaluate("stale=>{document.querySelector('.book-snapshot').dataset.stale=String(stale);document.querySelectorAll('.pixel-ledger details').forEach(e=>e.open=true)}",stale)
                samples=page.locator('.pixel-ledger').evaluate(CONTRAST)
                assert len(samples)>25
                assert not [s for s in samples if s['ratio']<4.5]
            samples=page.get_by_role('dialog',name='Enlarged paper ledger').evaluate(CONTRAST)
            assert not [s for s in samples if s['ratio']<4.5]
        finally:b.close()


def test_report_caption_details_start_collapsed_with_one_short_status():
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        try:
            page=b.new_page();start(page)
            book=page.get_by_role('region',name='Exploratory paper book')
            details=book.locator('.book-evidence')
            assert details.count()==1 and not details.evaluate('e=>e.open')
            status=book.locator('.book-report-status').first
            assert len(status.inner_text())<110
            assert book.get_by_text('Exploratory epoch. Modelled paper fills, no landing proof (b_unmeasured). Not evidence of skill. No inference.',exact=True).is_hidden()
        finally:b.close()
