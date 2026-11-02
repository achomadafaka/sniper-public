"""The embedded Office must work with the real dashboard styles, not only standalone fixtures."""
from pathlib import Path
from playwright.sync_api import sync_playwright
from office.test_chart_rendering import mount, populated
from office.test_communication_ui import start
ROOT=Path(__file__).resolve().parents[2]


def dashboard_css(page):
    page.add_style_tag(path=str(ROOT/'static/styles.css'))
    page.add_style_tag(path=str(ROOT/'static/office-floor.css'))
    page.locator('article').first.evaluate("e=>{const tab=document.createElement('section');tab.id='tab-desk';tab.className='tab-panel active';e.replaceWith(tab);tab.append(e)}")


def test_embedded_chart_overlay_stays_transparent_and_keeps_renderer_size():
    with sync_playwright() as p:
        browser=p.chromium.launch()
        try:
            page=browser.new_page(viewport={'width':1440,'height':1000});mount(page);dashboard_css(page);populated(page)
            top=page.locator('.chart-plot canvas').nth(1)
            assert top.evaluate('e=>getComputedStyle(e).backgroundColor')=='rgba(0, 0, 0, 0)'
            assert top.evaluate('e=>getComputedStyle(e).imageRendering')=='auto'
            assert page.locator('.chart-plot canvas').first.evaluate('e=>Math.abs(e.getBoundingClientRect().height-e.height/devicePixelRatio)<2')
        finally:browser.close()


def test_embedded_ledger_cells_keep_dark_ink_in_room_and_dialog():
    with sync_playwright() as p:
        browser=p.chromium.launch()
        try:
            page=browser.new_page();start(page);dashboard_css(page)
            page.evaluate("ui.renderReport({state:'partial',epoch_id:'office-v2-explore-1',now:100,book:{},segments:{},orders:{state:'known',count:1,items:[{order_id:'one',ticker:'FUN',state:'open',filled_at:90,spend_usd:25}]}})")
            page.get_by_role('button',name='Enlarge ledger').click()
            page.get_by_text('Recorded paper orders · known',exact=True).click()
            cell=page.locator('.paper-orders td').first
            assert cell.evaluate('e=>getComputedStyle(e).color')=='rgb(34, 60, 67)'
            page.keyboard.press('Escape')
            page.get_by_role('button',name='Enlarge ledger').click()
            assert cell.evaluate('e=>getComputedStyle(e).color')=='rgb(34, 60, 67)'
        finally:browser.close()


def test_chart_display_symbol_uses_same_exact_mint_source_without_rewriting_order(tmp_path, monkeypatch):
    from sniper.office import position_charts as C
    from office.test_position_charts import row
    from office.test_communication_followup import symbols
    monkeypatch.delenv('SNIPER_CLOCK_OFFSET_FILE', raising=False)
    root=tmp_path/'office';root.mkdir();symbols(root)
    projected=C.project_order(row(ticker=None,mint='exact'),[],[],now=200.)
    reader=C.OfficeCharts(root,monotonic=lambda:0.)
    monkeypatch.setattr(reader,'_load',lambda **kw:dict(state='partial',epoch_id=C.EPOCH,as_of=200.,open_count=1,items=[projected]))
    result=reader.read(now=200.)['items'][0]
    assert result['ticker'] is None
    assert result['display_ticker']=='CAT'
    assert result['display_ticker_source']=='narrative_guess.launches'


def test_empty_chart_names_the_empty_read_and_clears_when_points_arrive():
    with sync_playwright() as p:
        browser=p.chromium.launch()
        try:
            page=browser.new_page();mount(page)
            expect_text='No recorded OHLC candles in this read.'
            assert page.get_by_text(expect_text,exact=True).is_visible()
            populated(page)
            assert not page.get_by_text(expect_text,exact=True).is_visible()
        finally:browser.close()
