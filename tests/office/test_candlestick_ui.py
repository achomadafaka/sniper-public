"""Actual pinned renderer: recorded candles, volume, crosshair, rails and priced markers."""
from playwright.sync_api import sync_playwright
from office.test_chart_rendering import mount


def populate(page):
    page.evaluate("""snapshot.as_of=200;Object.assign(snapshot.items[0],{state:'known',status:'closed',closed_at:145,entry_unit_price_usd:1e-8,exit_unit_price_usd:1.2e-8,levels:{entry:0,stop:-20,take_profit:50},exit:{time:145,reason:'trail'},candles:{state:'known',basis:'recorded tape',unknown_volume_rows:0,'5s':[[100,1e-8,1.3e-8,1e-8,1.2e-8,1,2,1,1],[105,1.2e-8,1.4e-8,1.1e-8,1.3e-8,2,0,1,0],[115,1.3e-8,1.3e-8,1.1e-8,1.2e-8,0,3,0,1]],'1m':[[60,1e-8,1.4e-8,1e-8,1.3e-8,3,2,2,1],[120,1.3e-8,1.4e-8,1.1e-8,1.2e-8,0,3,0,1]]}});view.render(snapshot)""")


def test_candles_volume_axis_toggle_crosshair_and_frozen_price_rails():
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        try:
            page=b.new_page(viewport={'width':1440,'height':1000});mount(page);populate(page)
            assert page.evaluate('realCandles.length')==1 and page.evaluate('realVolumes.length')==1
            assert page.evaluate('realCandles[0].data().every(x=>x.time!==110)&&realCharts[0].timeScale().timeToCoordinate(110)!==null')
            assert page.evaluate('realVolumes[0].data()[0].value')==3
            assert page.evaluate('realCandles[0].options().priceFormat.formatter(1.2e-8)')=='$1.2e-8'
            page.get_by_role('button',name='Enlarge chart').click()
            page.get_by_role('button',name='1 m',exact=True).click()
            assert page.evaluate('realCandles[0].data().filter(x=>x.open!==undefined).length')==2
            assert page.get_by_role('button',name='1 m',exact=True).get_attribute('aria-pressed')=='true'
            assert 'Stop: $0.000000008' in page.locator('.chart-levels').inner_text()
            assert 'Take profit: $0.000000015' in page.locator('.chart-levels').inner_text()
            page.get_by_role('button',name='Show stop/TP',exact=True).click()
            for price in (8e-9,1.5e-8):
                y=page.evaluate('v=>realCandles[0].priceToCoordinate(v)',price);assert y is not None and 0<=y<page.locator('.chart-plot').bounding_box()['height']
            page.evaluate('async()=>{await new Promise(requestAnimationFrame);await new Promise(requestAnimationFrame)}')
            xy=page.evaluate('({x:realCharts[0].timeScale().timeToCoordinate(120),y:realCandles[0].priceToCoordinate(1.2e-8)})');box=page.locator('.chart-plot').bounding_box();page.mouse.move(box['x']+xy['x'],box['y']+xy['y'])
            page.wait_for_function("document.querySelector('.chart-ohlc').textContent.includes('O $0.000000013')")
            assert all(x in page.locator('.chart-ohlc').inner_text() for x in ('H $0.000000014','L $0.000000011','C $0.000000012','V 3 SOL'))
            assert 'Modelled entry $0.00000001' in page.locator('.chart-markers').inner_text()
            assert 'Modelled exit $0.000000012' in page.locator('.chart-markers').inner_text()
            page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        finally:b.close()


def test_interval_survives_refresh_partial_volume_and_malformed_buckets_stay_explicit():
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        try:
            page=b.new_page();mount(page);populate(page)
            page.get_by_role('button',name='1 m',exact=True).click()
            page.evaluate("snapshot.items[0].candles.state='partial';snapshot.items[0].candles.unknown_volume_rows=1;view.render(snapshot)")
            assert page.get_by_role('button',name='1 m',exact=True).get_attribute('aria-pressed')=='true'
            assert 'lower bound' in page.locator('.chart-ohlc').inner_text() and 'partial' in page.locator('.chart-badge').inner_text()
            page.evaluate("snapshot.items[0].candles['1m']=[];view.render(snapshot)")
            assert 'No recorded OHLC candles' in page.locator('.chart-empty').inner_text()
            assert page.evaluate('realCandles[0].data().length')==0
            page.evaluate("snapshot.items[0].candles['1m']=[[240,1,2,1,2,0,0,0,0]];view.render(snapshot)")
            assert 'invalid OHLC bucket' in page.locator('.chart-empty').inner_text()
            page.evaluate("snapshot.items[0].levels={entry:0};snapshot.items[0].policy_state='known';for(let i=0;i<4;i++)view.render(snapshot)")
            assert page.locator('.chart-levels').text_content().count('Take profit: not configured')==1
            assert page.evaluate('realCandles[0].data().length')==0
        finally:b.close()


def test_chart_library_is_the_existing_pinned_vendor():
    import hashlib
    from pathlib import Path
    root=Path(__file__).resolve().parents[2];vendor=root/'static/vendor/lightweight-charts.standalone.production.js'
    assert b'TradingView Lightweight Charts' in vendor.read_bytes() and b'v4.1.1' in vendor.read_bytes()[:200]
    assert hashlib.sha256(vendor.read_bytes()).hexdigest() in (vendor.parent/'lightweight-charts.README.md').read_text()


def test_fill_between_bar_starts_uses_containing_ohlc_bucket_and_bad_fields_are_refused():
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        try:
            page=b.new_page();mount(page);populate(page)
            page.evaluate('snapshot.items[0].filled_at=102;view.render(snapshot)')
            page.get_by_role('button',name='Enlarge chart').click()
            page.wait_for_function("Math.abs(realCharts[0].options().width-document.querySelector('.chart-plot').clientWidth)<2")
            page.evaluate('async()=>{await new Promise(requestAnimationFrame);await new Promise(requestAnimationFrame)}')
            xy=page.evaluate('({x:realCharts[0].timeScale().timeToCoordinate(102),y:realCandles[0].priceToCoordinate(1e-8)})');box=page.locator('.chart-plot').bounding_box();page.mouse.move(box['x']+xy['x'],box['y']+xy['y'])
            page.wait_for_function("document.querySelector('.chart-ohlc').textContent.includes('Crosshair 00:01:40 UTC · O $0.00000001')")
            page.evaluate("snapshot.items[0].candles.fields=['t','c','h','l','o','buy_sol','sell_sol','buy_n','sell_n'];view.render(snapshot)")
            assert page.evaluate('realCandles[0].data().length')==0
            assert 'unsupported OHLC fields' in page.locator('.chart-empty').inner_text()
        finally:b.close()
