"""Fixture-only room geometry, live board updates and keyboard enlargement."""
from pathlib import Path
import pytest
from playwright.sync_api import sync_playwright, expect
ROOT=Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('width',[1440,390])
def test_charts_and_ledger_live_inside_full_width_room(width):
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--disable-gpu','--renderer-process-limit=2'])
        try:
            page=browser.new_page(viewport={'width':width,'height':1100},reduced_motion='reduce');page.set_default_timeout(3000)
            page.route('**/*',lambda r:r.fulfill(path=str(ROOT/'static/vendor/fonts/VT323-Regular.ttf')) if r.request.url.endswith('/VT323-Regular.ttf') else r.fulfill(content_type='text/html',body='<article></article>'));page.goto('http://office.test');page.set_content('<article></article>');page.evaluate('window.module={exports:{}}')
            for name in ('vendor/lightweight-charts.standalone.production.js','office-charts.js','office-floor.js','office-v2.js'):page.add_script_tag(path=str(ROOT/'static'/name))
            page.evaluate('''()=>{window.ui=module.exports.mount(document,document.querySelector('article'),{});ui.render({as_of:100,roster:{state:'known',items:[{actor_id:'h',role:'head'}]},seats:{state:'known',items:[{actor_id:'h',activity:{kind:'working',at:99},current_tool:'chain_transaction',last_thought:'Reviewing paper'}]},events:{items:[]},mail:{state:'known',items:[]}});ui.renderCharts({state:'known',epoch_id:'office-v2-explore-1',as_of:100,open_count:1,items:[{order_id:'one',state:'known',status:'open',ticker:'FIXTURE COIN',filled_at:90,levels:{entry:0},tape_segments:[[{time:90,value:0},{time:100,value:10}]],mark_segments:[],stats:{}}]});window.report={state:'partial',epoch_id:'office-v2-explore-1',now:100,book:{state:'known',now:{state:'known',equity_usd:100,unpriced_count:0,cash_usd:75},open_positions:[{}],daily_loss_limit_usd:100,daily_realized_usd:0,office_net:0},segments:{state:'known',items:[{segment:1,at:90,kind:'release',release_sha:'fixture'}]},orders:{state:'known',items:[]},null_arm:{state:'unknown'}};ui.renderReport(report)}''')
            scene=page.locator('.pixel-scene');board=page.get_by_role('region',name='Exploratory paper book')
            expect(scene.locator('.floor-book')).to_have_count(1)
            assert scene.locator('.wall-screens').count()==1
            page.wait_for_function("document.querySelector('.pixel-scene').getBoundingClientRect().width/document.querySelector('.pixel-world').getBoundingClientRect().width>.95")
            plot=page.locator('.chart-plot').bounding_box();room=scene.bounding_box()
            assert 80<=plot['width']<700 and plot['height']<=180 and plot['y']>room['y']
            assert board.bounding_box()['y']>plot['y']+plot['height']
            assert 'Office Pixel' in page.locator('.position-chart').evaluate('e=>getComputedStyle(e).fontFamily')
            if width>1000:
                assert scene.locator('.pixel-seat').first.bounding_box()['x']+44<plot['x']
                assert board.bounding_box()['y']+board.bounding_box()['height']<1100
            assert 'Cash: $75.00' in board.inner_text()
            assert 'NET uses proven open marks; recorded modelled cashflows may include entry fills priced on a carried quote and exit fills priced on an estimate.' in board.text_content()
            assert '$100.00' in board.inner_text() and 'NET (all exits, modelled) $0.00' in board.inner_text()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.get_by_role('button',name='Enlarge ledger').click()
            board.locator('.book-evidence summary').click()
            dialog=page.get_by_role('dialog',name='Enlarged paper ledger');assert dialog.is_visible()
            page.evaluate('report.book.now.equity_usd=125;ui.renderReport(report)')
            assert '$125.00' in dialog.inner_text() and 'Segment 1' in dialog.inner_text()
            page.keyboard.press('Escape');assert not dialog.is_visible()
            expect(scene.locator('.floor-book')).to_have_count(1)
            assert page.get_by_role('button',name='Enlarge ledger').evaluate('e=>e===document.activeElement')
            page.screenshot(path=f'/tmp/epsilon-trading-wing-fixture-{width}.png',full_page=True)
            page.evaluate("ui.renderCharts({state:'known',epoch_id:'office-v2-explore-1',as_of:100,open_count:6,items:Array.from({length:6},(_,i)=>({order_id:'coin'+i,state:'known',status:'open',ticker:'FIXTURE '+i,filled_at:90,levels:{entry:0},tape_segments:[[{time:90,value:0},{time:100,value:10}]],mark_segments:[],stats:{}}))})")
            assert scene.locator('.position-chart').count()==6
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            gallery=scene.locator('.chart-gallery').bounding_box()
            assert gallery['y']+gallery['height']<=board.bounding_box()['y']  # Overflow stays within the chart wall.
            scene.get_by_role('button',name='Enlarge chart').last.click()
            assert page.get_by_role('dialog',name='Enlarged paper position chart').is_visible()
            page.keyboard.press('Escape')
            page.evaluate("report.book={state:'unknown',reason:'source_unavailable_or_invalid:OperationalError'};ui.renderReport(report)")
            page.get_by_role('button',name='Enlarge ledger').click()
            if not board.locator('.book-evidence').evaluate('e=>e.open'):board.locator('.book-evidence summary').click()
            assert 'Book unknown: source_unavailable_or_invalid:OperationalError' in board.inner_text()
        finally:browser.close()
