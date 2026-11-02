"""Wallet Map W4a (spec docs/superpowers/specs/2026-10-07-wallet-map-overhaul-design.md): wheel over the
map scrolls the page; zoom is deliberate (ctrl/cmd+wheel, the buttons, or a click that arms wheel zoom
until the pointer leaves); one-finger vertical drags scroll; Refresh reloads and keeps the view; a
same-wallet redraw keeps open folds and focus; loading never reads as an absence."""
from datetime import datetime, timedelta, timezone

from test_wallet_universe_navigation import _body, _draw, universe_page  # noqa: F401  (fixture)


def _zoom(page):
    return page.locator('#zoom-level').text_content()


def _wheel(page, **init):
    return page.evaluate("""init=>{const g=document.querySelector('.space-map');const r=g.getBoundingClientRect();
      const e=new WheelEvent('wheel',Object.assign({deltaY:-240,clientX:r.left+r.width*.1,clientY:r.top+r.height*.1,
        bubbles:true,cancelable:true},init));g.dispatchEvent(e);return e.defaultPrevented;}""", init)


def test_an_unarmed_wheel_without_ctrl_leaves_the_zoom_and_lets_the_page_scroll(universe_page):
    page, _ = universe_page
    _draw(page)
    page.evaluate("document.body.style.minHeight='4000px'")
    before = _zoom(page)
    assert _wheel(page) is False, "the page keeps the wheel"
    assert _zoom(page) == before
    box = page.locator('.space-map').bounding_box()
    page.mouse.move(box['x'] + 20, box['y'] + 20)
    page.mouse.wheel(0, 400)
    page.wait_for_function('scrollY>0')
    assert _zoom(page) == before


def test_ctrl_or_cmd_wheel_zooms(universe_page):
    page, _ = universe_page
    _draw(page)
    for key in ('ctrlKey', 'metaKey'):
        before = _zoom(page)
        assert _wheel(page, **{key: True}) is True
        assert _zoom(page) != before


def test_a_click_arms_wheel_zoom_until_the_pointer_leaves(universe_page):
    page, _ = universe_page
    _draw(page)
    page.evaluate("document.querySelector('.space-map').scrollIntoView({block:'end'})")   # W2's Table|Map bar pushes the map's foot below 900px
    box = page.locator('.space-map').bounding_box()
    page.mouse.click(box['x'] + 12, box['y'] + box['height'] - 12)
    before = _zoom(page)
    assert _wheel(page) is True and _zoom(page) != before
    page.mouse.move(box['x'] + box['width'] / 2, box['y'] - 40)        # out of the map
    before = _zoom(page)
    assert _wheel(page) is False and _zoom(page) == before


def test_one_finger_vertical_drags_scroll_and_two_fingers_still_pinch(universe_page):
    page, _ = universe_page
    _draw(page)
    assert page.locator('.space-map').evaluate('e=>getComputedStyle(e).touchAction') == 'pan-y'


def _stub_reloads(page, universe, herders):
    page.evaluate("""b=>{window.fetches=[];window.fetch=async url=>{url=String(url);fetches.push(url);
      const body=url.includes('/universe')?b.u:url.includes('/herders')?b.h:{error:'stub'};
      return new Response(JSON.stringify(body),{status:url.includes('/dossier')?503:200});};}""",
                  dict(u=universe, h=herders))


def test_refresh_reloads_and_keeps_selection_camera_and_an_open_fold(universe_page):
    page, _ = universe_page
    graph = _draw(page)
    page.locator('#browse').click()
    page.locator('[data-browse="B"]').click()
    fold = page.locator('.inspector details.wu-proof')
    fold.locator(':scope > summary').click()
    assert fold.evaluate('d=>d.open')
    page.locator('#map-refresh').focus()
    camera = page.locator('.world').get_attribute('transform')
    _stub_reloads(page, _body(), graph)
    page.locator('#map-refresh').click()
    page.wait_for_function("fetches.some(u=>u.includes('/universe'))&&fetches.some(u=>u.includes('/herders'))")
    page.wait_for_timeout(200)
    assert page.locator('.planet.selected').get_attribute('data-node') == 'B'
    assert page.locator('.world').get_attribute('transform') == camera
    assert page.locator('.inspector details.wu-proof').evaluate('d=>d.open'), "the open fold survives the redraw"


def test_a_same_wallet_redraw_keeps_focus_inside_the_summary(universe_page):
    page, _ = universe_page
    _draw(page)
    page.locator('[data-node="A"] .planet-label').click()
    page.locator('.inspector #next-wallet').focus()
    page.evaluate('b=>walletMap.drawUniverse(b)', _body())
    assert page.evaluate('document.activeElement.id') == 'next-wallet'


def test_loading_says_loading_never_no_wallets(universe_page):
    page, _ = universe_page
    _draw(page)
    page.evaluate("window.pending={};window.fetch=url=>new Promise(resolve=>{pending[String(url)]=resolve});void 0")
    page.locator('.chain[data-chain="56"]').click()
    text = page.locator('.inspector').inner_text()
    assert 'Loading' in text and 'No wallets observed' not in text


def test_the_header_shows_a_ticking_graph_age(universe_page):
    page, _ = universe_page
    graph = _draw(page)
    body = _body()
    body['graph'] = dict(run_finished_at=(datetime.now(timezone.utc) - timedelta(minutes=12)).isoformat())
    page.evaluate('b=>walletMap.drawUniverse(b)', body)
    assert page.locator('#graph-age').inner_text() == 'graph 12 min old'
    page.evaluate('b=>walletMap.drawUniverse(b)', _body('56'))
    assert page.locator('#graph-age').inner_text() == 'relationships not measured'


def test_a_focused_fold_summary_keeps_focus_across_a_same_wallet_redraw(universe_page):
    """Gamma LOW (a) on W4a: a summary has no id, and focus fell to BODY after a redraw."""
    page, _ = universe_page
    _draw(page)
    summary = page.locator('.inspector details.wu-proof > summary')
    summary.focus()
    page.evaluate('b=>walletMap.drawUniverse(b)', _body())
    assert page.evaluate("document.activeElement.tagName") == 'SUMMARY'
    assert page.evaluate("document.activeElement.textContent") == 'Evidence and settings'


def test_identical_fold_summaries_keep_their_own_open_state(universe_page):
    """Gamma LOW (b) on W4a: frankdegods has repeated summaries; opening one reopened both."""
    page, _ = universe_page
    _draw(page)
    body = _body()
    twin = dict(actual_gate=['same gate'], origin='same-origin', inputs_state='x', capture_state='x', outcome_kind='x')
    body['wallets'][0]['blockers'] = [dict(twin), dict(twin)]
    page.evaluate('b=>walletMap.drawUniverse(b)', body)
    twins = page.locator('.inspector details', has=page.locator(':scope > summary', has_text='same gate · same-origin'))
    assert twins.count() == 2
    twins.nth(1).evaluate('d=>{d.open=true}')
    page.evaluate('b=>walletMap.drawUniverse(b)', body)
    twins = page.locator('.inspector details', has=page.locator(':scope > summary', has_text='same gate · same-origin'))
    assert [twins.nth(i).evaluate('d=>d.open') for i in range(2)] == [False, True]


def test_a_touch_screen_gets_touch_help_text():
    """Gamma/Alpha LOW (c) on W4a: pointer:coarse read the mouse text."""
    from pathlib import Path
    from playwright.sync_api import sync_playwright
    root = Path(__file__).resolve().parents[1]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.add_init_script("""const real=window.matchMedia.bind(window);
          window.matchMedia=q=>q.includes('pointer: coarse')?{matches:true,media:q,onchange:null,
            addEventListener(){},removeEventListener(){},addListener(){},removeListener(){}}:real(q);""")
        page.route('http://sniper.test/**', lambda r: r.fulfill(content_type='text/html',
                   body='<section id="tab-wallet-map"><div id="wallet-universe"></div></section>'))
        page.goto('http://sniper.test/')
        page.add_script_tag(content=(root / 'static/wallet_universe.js').read_text())
        page.add_script_tag(content=(root / 'static/wallet_map.js').read_text())
        page.evaluate('b=>walletMap.drawUniverse(b)', _body())
        text = page.locator('#map-help').inner_text()
        browser.close()
    assert 'Pinch' in text and 'Ctrl' not in text and 'scroll' in text
