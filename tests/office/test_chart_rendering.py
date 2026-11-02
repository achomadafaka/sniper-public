"""Client known-state and live-range proof with the actual vendored canvas renderer."""
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[2]


def mount(page):
    page.set_default_timeout(2500);page.set_content('<article></article>')
    page.add_script_tag(path=str(ROOT/'static/vendor/lightweight-charts.standalone.production.js'))
    page.evaluate('''()=>{const lib=LightweightCharts;window.realCharts=[];window.realSeries=[];window.LightweightCharts={...lib,createChart:(...args)=>{const chart=lib.createChart(...args);realCharts.push(chart);for(const [method,key] of [['addLineSeries','realSeries'],['addCandlestickSeries','realCandles'],['addHistogramSeries','realVolumes']]){window[key]||=[];const add=chart[method].bind(chart);chart[method]=o=>{const series=add(o);window[key].push(series);return series}}return chart}}}''')
    page.add_script_tag(path=str(ROOT/'static/office-order-display.js'))
    page.add_script_tag(path=str(ROOT/'static/office-charts.js'))
    page.evaluate('''()=>{window.view=OfficePositionCharts.mount(document,document.querySelector('article'),()=>{});window.snapshot={state:'known',epoch_id:'office-v2-explore-1',as_of:100.1,open_count:1,items:[{order_id:'one',state:'partial',status:'open',ticker:'COIN',filled_at:100,age_s:0.1,levels:{entry:0},candles:{state:'known','5s':[],'1m':[]},tape_segments:[],mark_segments:[],stats:{}}]};view.render(snapshot)}''')


def populated(page):
    page.evaluate("snapshot.as_of=130;snapshot.items[0].state='known';snapshot.items[0].entry_unit_price_usd=.000001;snapshot.items[0].candles={state:'known','5s':[[95,.000001,.0000011,.0000009,.000001,1,0,1,0],[110,.000001,.0000012,.000001,.0000011,1,0,1,0],[120,.0000011,.0000013,.0000011,.0000012,1,0,1,0]],'1m':[]};snapshot.items[0].tape_segments=[[{time:99,value:0},{time:110,value:10},{time:120,value:20}]];snapshot.items[0].mark_segments=[[{time:110,value:5}]];view.render(snapshot)")


def test_data_arriving_after_empty_first_read_becomes_visible_and_preserves_manual_zoom():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--disable-gpu','--renderer-process-limit=2'])
        try:
            page=browser.new_page();mount(page);populated(page)
            assert page.evaluate('realCharts[0].options().layout.fontFamily').startswith('Office Pixel')
            assert page.evaluate('realCharts[0].options().layout.background.color')=='#a8c58d'
            visible=page.evaluate('realCharts[0].timeScale().getVisibleRange()')
            assert visible['from']<=99 and visible['to']>=120
            page.wait_for_function("[...document.querySelectorAll('.chart-plot canvas')].some(c=>{const a=c.getContext('2d').getImageData(0,0,c.width,c.height).data;for(let i=0;i<a.length;i+=4)if(a[i]===49&&a[i+1]===94&&a[i+2]===80)return true;return false})")
            assert ' · known' in page.locator('.chart-badge').inner_text()
            assert 'Stored modelled fill prices remain historical' in page.locator('.chart-meta').last.text_content()
            page.locator('.chart-plot').dispatch_event('pointerdown')
            page.evaluate('realCharts[0].timeScale().setVisibleRange({from:105,to:115})')
            page.wait_for_timeout(50)
            before=page.evaluate('realCharts[0].timeScale().getVisibleRange()')
            page.evaluate('snapshot.as_of=140;view.render(snapshot)')
            visible=page.evaluate('realCharts[0].timeScale().getVisibleRange()')
            assert visible==before
            page.get_by_role('button',name='Enlarge chart').click()
            page.get_by_role('button',name='Follow live',exact=True).click()
            page.wait_for_function('realCharts[0].timeScale().getVisibleRange().to>=120')
        finally:browser.close()


def test_server_known_with_no_points_or_zero_size_is_partial_and_logs_cause():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--disable-gpu','--renderer-process-limit=2'])
        try:
            page=browser.new_page();logs=[];page.on('console',lambda m:logs.append(m.text) if m.type in ('error','warning') else None);mount(page)
            page.evaluate("snapshot.items[0].state='known';view.render(snapshot)")
            assert 'partial: no points rendered' in page.locator('.chart-badge').inner_text()
            assert 'series_empty' in page.locator('.chart-badge').inner_text()
            assert 'sent 0' in page.locator('.chart-badge').inner_text() and any('series_empty' in m for m in logs)
            page.locator('.chart-plot').evaluate("e=>e.style.display='none'");populated(page)
            assert 'partial: no points rendered' in page.locator('.chart-badge').text_content()
            assert any('size_zero' in m for m in logs)
            page.locator('.chart-plot').evaluate("e=>e.style.display='block'");page.evaluate('view.render(snapshot)')
            page.wait_for_function("document.querySelector('.chart-badge').innerText.endsWith(' · known')")
        finally:browser.close()
