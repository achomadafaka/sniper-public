"""Real refresh controller with local fetch outcomes; no live report or service."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[2]
REPORT=dict(state='known',epoch_id='office-v2-explore-1',now=100.,book=dict(state='known',now=dict(state='known',equity_usd=100.,unpriced_count=0),open_positions=[],daily_loss_limit_usd=100.,daily_realized_usd=-10.,office_net=5.),segments=dict(state='known',complete=True,items=[dict(segment=1,at=90.,kind='release',release_sha='abc')]),orders=dict(state='known',items=[]),null_arm=dict(state='unknown'))


def start(page,*,active):
    page.set_default_timeout(2500)
    page.add_init_script('window.goodReport='+json.dumps(REPORT)+';'+r'''
window.reportKind='ok';window.reportCalls=0;window.intervals=[];window.setInterval=(fn,ms)=>{intervals.push({fn,ms});return intervals.length};
window.fetch=async url=>{if(!url.includes('/api/office/explore'))return {ok:true,json:async()=>({roster:{state:'known',items:[]},events:{items:[]},mail:{items:[]}})};reportCalls++;
if(reportKind==='network')throw new TypeError('offline');if(reportKind==='timeout')throw new DOMException('timed out','AbortError');
if(reportKind==='http')return {ok:false,status:503};if(reportKind==='parse')return {ok:true,json:async()=>{throw new SyntaxError('bad JSON')}};
if(reportKind==='unknown')return {ok:true,json:async()=>({state:'unknown',epoch_id:'office-v2-explore-1',reason:'<img src=x> source unavailable'})};
const data=JSON.parse(JSON.stringify(goodReport));if(reportKind==='render'){data.book.office_net=9999;data.orders.items=[null]};return {ok:true,json:async()=>data};};
''')
    cls='tab-panel active' if active else 'tab-panel'
    html='<style>.tab-panel{display:none}.tab-panel.active{display:block}</style><button id="refresh">Refresh observations</button><section id="tab-desk" class="'+cls+'"></section><script defer src="/static/office-v2.js"></script>'
    page.route('**/*',lambda r:r.fulfill(content_type='text/javascript',body=(ROOT/'static/office-v2.js').read_text()) if '/static/' in r.request.url else r.fulfill(content_type='text/html',body=html))
    page.goto('http://office.test/view')


def test_opening_office_fetches_report_without_waiting_for_minute_timer():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--disable-gpu','--renderer-process-limit=2'])
        try:
            page=browser.new_page();start(page,active=False)
            assert page.evaluate('reportCalls')==0
            page.evaluate("document.getElementById('tab-desk').classList.add('active')")
            page.wait_for_function('reportCalls===1')
            assert '$100.00' in page.get_by_role('region',name='Exploratory paper book').inner_text()
        finally:browser.close()


def test_failures_keep_last_good_snapshot_and_explain_fetch_render_and_source_states():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--disable-gpu','--renderer-process-limit=2'])
        try:
            page=browser.new_page();logs=[];page.on('console',lambda m:logs.append(m.text) if m.type=='error' else None);start(page,active=True)
            book=page.get_by_role('region',name='Exploratory paper book');notice=book.locator('.book-report-status')
            page.wait_for_function('reportCalls===1')
            book.locator('.book-evidence summary').click()
            for count,(kind,label) in enumerate([('network','network'),('timeout','timeout'),('http','http_503'),('parse','parse'),('render','render')],1):
                page.evaluate('v=>reportKind=v',kind);page.get_by_role('button',name='Refresh observations').click()
                page.wait_for_function('n=>document.querySelector("#office-v2-work").dataset.reportFailures===String(n)',arg=count)
                assert label in notice.inner_text() and 'last good reading 00:01:40 UTC' in notice.inner_text()
                assert '$100.00' in book.inner_text() and '9999' not in book.inner_text()
                assert book.locator('.book-snapshot').get_attribute('data-stale')=='true'
            assert len(logs)==5
            page.evaluate("reportKind='unknown'");page.get_by_role('button',name='Refresh observations').click()
            page.wait_for_function("document.querySelector('.book-report-status').innerText.includes('source unavailable')")
            assert 'Source state unknown' in notice.inner_text() and book.locator('img').count()==0
            assert page.locator('#office-v2-work').get_attribute('data-report-failures')=='5'
            page.evaluate("reportKind='ok';goodReport.now=200");page.get_by_role('button',name='Refresh observations').click()
            page.wait_for_function("document.querySelector('.book-snapshot').dataset.stale==='false'")
            assert '00:03:20' in book.inner_text() and '5' in notice.inner_text()
        finally:browser.close()


def test_complete_net_survives_omitted_segment_detail_rows():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--disable-gpu','--renderer-process-limit=2'])
        try:
            page=browser.new_page();start(page,active=True)
            page.wait_for_function('reportCalls===1')
            page.evaluate("goodReport.segments={state:'known',complete:true,items_omitted:true};goodReport.book.office_net=0")
            page.get_by_role('button',name='Refresh observations').click()
            page.wait_for_function('reportCalls===2')
            assert 'NET (all exits, modelled) $0.00' in page.get_by_role('region',name='Exploratory paper book').inner_text()
            page.evaluate("goodReport.book.office_net=null")
            page.get_by_role('button',name='Refresh observations').click()
            page.wait_for_function('reportCalls===3')
            assert 'NET withheld' in page.get_by_role('region',name='Exploratory paper book').inner_text()
        finally:browser.close()


def test_marked_equity_is_displayed_separately_from_missing_proven_equity():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--disable-gpu','--renderer-process-limit=2'])
        try:
            page=browser.new_page();start(page,active=True)
            page.wait_for_function('reportCalls===1')
            page.evaluate("goodReport.book.open_positions=[{}];goodReport.book.watcher_heartbeat={state:'known',watcher_state:'managed',fx_state:'known',age_s:0,last_good_utc:100};goodReport.book.now={state:'unknown',equity_usd:null,unpriced_count:1,marked_state:'known',marked_unpriced_count:0,marked_equity_usd:1009.32};goodReport.separate_books=[{book_id:'office-v2-explore-gpt-1',epoch_id:'office-v2-explore-gpt-1',state:'unknown',unpriced_count:1,marked_state:'known',marked_unpriced_count:0,marked_equity_usd:1009.32}]")
            page.get_by_role('button',name='Refresh observations').click();page.wait_for_function('reportCalls===2')
            assert 'Marked equity: $1009.32' in page.get_by_role('region',name='Exploratory paper book').inner_text()
            assert 'NET: needs proven marks; marked estimate shown' in page.get_by_role('region',name='Exploratory paper book').inner_text()
            assert 'Statistics: needs proven marks; marked estimate shown' in page.get_by_role('region',name='Exploratory paper book').inner_text()
            page.get_by_role('region',name='Exploratory paper book').locator('.book-evidence summary').click()
            assert all(label in page.get_by_role('region',name='Exploratory paper book').inner_text() for label in ('Watcher: managed','FX known','age 0.0s','last good 00:01:40 UTC'))
        finally:browser.close()
