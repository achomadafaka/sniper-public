"""Production orbital map: navigation, evidence identity, and chain isolation."""
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def universe_page():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.set_default_timeout(5000)
        page.route('http://sniper.test/**', lambda r: r.fulfill(content_type='text/html', body='<section id="tab-wallet-map"><div id="wallet-universe"></div><div id="wallet-map-action-status"></div></section>'))
        page.goto('http://sniper.test/')
        page.add_style_tag(content=(ROOT / 'static/styles.css').read_text())
        if (ROOT / 'static/wallet_universe.css').exists():
            page.add_style_tag(content=(ROOT / 'static/wallet_universe.css').read_text())
        if (ROOT / 'static/wallet_universe.js').exists():
            page.add_script_tag(content=(ROOT / 'static/wallet_universe.js').read_text())
        page.add_script_tag(content=(ROOT / 'static/wallet_map.js').read_text())
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        yield page, errors
        assert errors == []
        browser.close()


def _body(chain='solana'):
    return dict(chain=chain, chains=[dict(id='solana', label='Solana'), dict(id='56', label='BSC')],
                stamp={'authority_state': 'ok'}, graph_state='not_collected', wallets=[
                    dict(uid='A', handle='Alpha', from_roster=True, coverage={}),
                    dict(uid='B', handle='Beta', from_roster=True, coverage={}),
                    dict(uid='C', handle='<img src=x onerror=alert(1)>', from_roster=True, coverage={})], edges=[])


def _draw(page, view='map'):
    graph = dict(stamp={'error': None, 'stale': False, 'computed_at': '2026-10-03T10:00:00Z'}, shared=[dict(a='A', b='B', count=1)], probes=[], herders=[
        dict(id='A', n=10, regulars=1, regular_list=[dict(id='Moon', k=4)], drew=4, members=2, read=10, units=10,
             entry_order=dict(n=10, share_before=0)),
        dict(id='B', n=10, regulars=1, regular_list=[dict(id='Moon', k=3)], drew=3, members=1, read=10, units=10,
             entry_order=dict(n=10, share_before=.5))])
    page.evaluate('b=>{walletMap.draw(b.g);walletMap.drawUniverse(b.u)}', dict(g=graph, u=_body()))
    if view == 'map':                       # W2: Wallet Map lands on the table; these tests drive the map
        page.locator('#pane-map').click()
    return graph


def test_measured_capital_and_impact_size_selection_and_unknown(universe_page):
    page,_=universe_page
    _draw(page)
    body=_body()
    body['wallets'][0]['metrics']=dict(capital=dict(state='ok',usd=1000,observed_at='2026-10-04T16:00:00Z'),
                                     impact=dict(state='ok',value_usd=500,complete=20,eligible=20))
    body['wallets'][1]['metrics']=dict(capital=dict(state='ok',usd=10,observed_at='2026-10-04T16:00:00Z'),
                                     impact=dict(state='incomplete',value_usd=None,complete=10,eligible=20))
    page.evaluate('b=>walletMap.drawUniverse(b)',body)
    select=page.locator('[aria-label="Planet size metric"]')
    select.select_option('capital')
    assert float(page.locator('[data-node="A"] circle.planet-body').get_attribute('r')) > float(page.locator('[data-node="B"] circle.planet-body').get_attribute('r'))
    page.locator('[data-node="A"] .planet-label').click()
    assert '$1,000' in page.locator('.inspector').inner_text()
    select.select_option('impact')
    assert float(page.locator('[data-node="A"] circle.planet-body').get_attribute('r')) > float(page.locator('[data-node="B"] circle.planet-body').get_attribute('r'))
    assert '$500' in page.locator('.inspector').inner_text()
    assert 'snapshot prices' in page.locator('#scope').inner_text()
    page.locator('#browse').click();page.locator('[data-browse="C"]').click()
    assert 'Coin flow unknown' in page.locator('.planet.selected').text_content()
    assert 'NaN' not in page.locator('.world').get_attribute('transform')


def test_actual_flow_reads_are_inspectable_before_comparison_is_ready(universe_page):
    page,_=universe_page
    _draw(page);body=_body()
    body['wallets'][0]['metrics']=dict(impact=dict(state='insufficient',complete=1,eligible=3,value_usd=None,
        recent=[dict(mint='MeasuredCoin',t0=1791129600,status='complete',value_usd=-123.45,
                     source_attribution='beneficiary_only'),
                dict(mint='UnpricedCoin',t0=1791129600,status='complete',value_usd=None,source_attribution='routed_payment',
                     quotes=[dict(asset='QuoteMint',net_after_atomic='-1234567',decimals=6)])]))
    page.evaluate('b=>walletMap.drawUniverse(b)',body)
    summary=page.get_by_text('Measured coin reads',exact=True)
    assert summary.count()==1
    summary.click()
    assert 'MeasuredCoin' in page.locator('.wu-flow-reads').inner_text()
    assert '−$123' in page.locator('.wu-flow-reads').inner_text()
    assert '−1.234567 QuoteMint' in page.locator('.wu-flow-reads').inner_text()
    assert 'Relayed purchase received; initiator unproven' in page.locator('.wu-flow-reads').inner_text()
    assert 'Wallet payment verified through router; router ownership unproven' in page.locator('.wu-flow-reads').inner_text()
    assert page.locator('[aria-label="Planet size metric"] option[value="impact"]').get_attribute('disabled') is not None
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')


def test_account_and_suspected_addresses_show_trade_proofs_and_search(universe_page):
    from test_wallet_identity import A,B,project,identity
    page,_=universe_page;_draw(page);body=_body();row=identity(project())
    row['state']='ok';row['generated_at']='2026-10-10T12:00:00+00:00'
    relation=row['related_wallets'][0];relation['evidence'][0]['buy_signature']='1'*64
    relation['evidence'][1]['buy_signature']=None
    relation['evidence'][2]['mint']='<img src=x onerror=alert(1)>'
    body['wallets'][0]['identity']=row
    page.evaluate('b=>walletMap.drawUniverse(b)',body)
    section=page.locator('[aria-label="Account addresses and related wallets"]')
    assert A in section.inner_text() and B in section.inner_text()
    assert 'Suspected alternate' in section.inner_text() and '3 of 4' in section.inner_text()
    assert '1 unread' in section.inner_text() and '20s' in section.inner_text() and '30s' in section.inner_text()
    section.get_by_text('Trade evidence',exact=True).click()
    assert 'Receipt unavailable' in section.inner_text()
    assert section.locator('a[href="https://solscan.io/tx/'+('1'*64)+'"]').count()==1
    assert 'Funding clue' in section.inner_text() and section.locator('img').count()==0
    page.locator('#find').fill(B)
    assert page.locator('#results [data-result="A"]').count()==1
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')


def test_verified_funding_and_repeat_buyers_have_inspectable_wallet_links(universe_page):
    from test_wallet_identity import A, B, project, identity
    from test_wallet_identity_funding import SIG
    page, _ = universe_page; graph = _draw(page); body = _body(); row = identity(project())
    row.update(state='ok', links_method='armed-wallet-links-v1', coverage=dict(sampled_coins=4,unknown_coins=0))
    related, = row['related_wallets']
    related.update(status='funding_verified_link', funding_verified=True)
    related['funding_clues'] = [dict(kind='verified_funding', verified=True, funder=B, recipient=A,
                                   funding_signature=SIG, amount_sol='1', ownership_proven=False)]
    body['wallets'][0]['identity'] = row
    graph['herders'][0]['regular_list'] = [dict(id=B, k=4)]
    page.evaluate('b=>{walletMap.draw(b.g);walletMap.drawUniverse(b.u)}', dict(g=graph,u=body))
    page.locator('[data-node="A"] .planet-label').click()
    section = page.locator('.wu-identity')
    assert 'Funding verified link' in section.inner_text()
    assert '1 SOL' in section.inner_text() and 'Ownership unproven' in section.inner_text()
    assert section.locator(f'a[href="https://solscan.io/account/{B}"]').count() >= 1
    assert section.locator(f'a[href="https://solscan.io/tx/{SIG}"]').count() == 1
    assert section.locator('a[target="_blank"]').first.get_attribute('rel') == 'noopener noreferrer'
    page.locator('[data-detail-moon]').click()
    assert page.locator(f'.inspector a[href="https://solscan.io/account/{B}"]').count() >= 1
    assert 'Owner / bot identity' in page.locator('.inspector').inner_text()
    assert f"Before main: {related['n_coins']} coins" in page.locator('.inspector').inner_text()
    assert 'After main: unknown' in page.locator('.inspector').inner_text()
    page.locator('#back').click()
    related.update(n_coins=0,lead_p50_s=None,lead_p90_s=None,evidence=[])
    page.evaluate('b=>walletMap.drawUniverse(b)',body)
    assert 'No measured timing sample.' in page.locator('.wu-identity').inner_text()
    assert 'nulls' not in page.locator('.wu-identity').inner_text()
    page.locator('#find').fill(B)
    assert page.locator('#results [data-result="A"]').count()==1
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')


def test_fast_flow_windows_and_position_uncertainty_are_visible(universe_page):
    page,_=universe_page
    _draw(page);body=_body()
    body['wallets'][0]['metrics']=dict(impact=dict(state='insufficient',complete=1,eligible=3,
        recent=[dict(mint='FlowCoin',t0=1791129600,status='complete',value_usd=None,
            source_position=dict(state='add_on',visible_pre_balance_atomic='1000'),
            horizons=dict(windows=[dict(seconds=5,state='complete',verified_buyer_count=2,buyers_complete=False,
                quotes=[dict(asset='QuoteMint',symbol='<img src=x onerror=alert(1)>',net_after_atomic='1234567',decimals=6)]),
                dict(seconds=10,state='unknown',quotes=None,verified_buyer_count=None),
                dict(seconds=120,state='not_captured',quotes=None,verified_buyer_count=None)]))]))
    page.evaluate('b=>walletMap.drawUniverse(b)',body)
    page.get_by_text('Measured coin reads',exact=True).click()
    text=page.locator('.wu-flow-reads').inner_text()
    assert 'Add-on: prior holdings visible' in text
    assert 'New wallet position: unknown' not in text
    assert '5s' in text and '1.234567' in text and '2+' in text
    assert 'Unknown' in text and 'Not captured' in text
    assert '14-day source-flow census' in text
    assert page.locator('.wu-flow-reads img').count()==0
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    body['wallets'][0]['metrics']['impact']['recent'][0]['source_position']['state']='unknown'
    page.evaluate('b=>walletMap.drawUniverse(b)',body)
    # W4a: a same-wallet redraw keeps the fold the reader opened, so it is not clicked again.
    assert page.locator('.wu-flow-reads').evaluate('d=>d.open')
    assert 'New wallet position: unknown' in page.locator('.wu-flow-reads').inner_text()
    body['wallets'][0]['metrics']['impact']['state']='proof_changed'
    page.evaluate('b=>walletMap.drawUniverse(b)',body)
    assert page.locator('.wu-flow-reads').count()==0


def test_planet_label_moon_connections_and_refresh_keep_view(universe_page):
    page, _ = universe_page
    _draw(page)
    assert page.locator('.space-map .planet').count() == 3, page.locator('#wallet-universe').inner_html()[:2000]
    page.locator('[data-node="A"] .planet-label').click()
    assert 'system' in page.locator('.world').get_attribute('class')
    page.locator('[data-detail-moon="Moon"]').click()
    assert '4 of 10 sampled coins' in page.locator('.inspector').inner_text()
    page.locator('#show-connections').click()
    assert 'connections' in page.locator('.world').get_attribute('class')
    assert page.locator('.planet.related').count() == 1
    camera = page.locator('.world').get_attribute('transform')
    page.evaluate('b=>walletMap.drawUniverse(b)', _body())
    assert page.locator('.world').get_attribute('transform') == camera
    assert 'Repeat buyer' in page.locator('.inspector .kind').inner_text()
    assert page.locator('#wallet-universe img').count() == 0


def test_zoom_drag_and_expand_keep_wallet_selection(universe_page):
    page, _ = universe_page
    _draw(page)
    page.locator('#focus-wallet').click()
    before = page.locator('.world').get_attribute('transform')
    page.locator('#zoom-in').click()
    assert page.locator('.world').get_attribute('transform') != before
    box = page.locator('.space-map').bounding_box()
    x, y = box['x'] + box['width'] / 2, box['y'] + box['height'] / 2
    before = page.locator('.world').get_attribute('transform')
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x+90, y+30, steps=5)
    page.mouse.up()
    assert page.locator('.world').get_attribute('transform') != before
    assert page.locator('.planet.selected').get_attribute('data-node') == 'A'
    page.locator('#full-screen').click()
    assert page.locator('.main.expanded').count() == 1
    page.locator('#full-screen').press('Escape')
    assert page.locator('.main.expanded').count() == 0


def test_unknown_and_zero_following_and_chain_switch_clear_old_moons(universe_page):
    page, _ = universe_page
    graph = _draw(page)
    page.locator('#browse').click()
    page.locator('[data-browse="C"]').click()
    assert 'Repeat buyers unknown' in page.locator('.planet.selected').text_content()
    graph['herders'].append(dict(id='C', n=12, regulars=0, regular_list=[], drew=0, members=0, read=12, units=12, entry_order=dict(n=0, share_before=None)))
    page.evaluate('b=>walletMap.draw(b)', graph)
    assert '0 repeat buyers' in page.locator('.planet.selected').text_content()
    # Hold old and new chain responses so ordering can be exercised without a server.
    page.evaluate("window.pending={};window.fetch=url=>new Promise(resolve=>{pending[String(url)]=resolve});void 0")
    page.locator('.chain[data-chain="56"]').click()
    page.locator('.chain[data-chain="solana"]').click()
    page.evaluate('b=>pending["/api/memecoin/wallet-map/universe?chain=solana"](new Response(JSON.stringify(b)))', _body())
    page.wait_for_function("document.querySelector('#sky-title').textContent==='Solana universe'")
    page.evaluate('b=>pending["/api/memecoin/wallet-map/universe?chain=56"](new Response(JSON.stringify(b)))', _body('56'))
    assert page.locator('#sky-title').inner_text() == 'Solana universe'
    page.locator('.chain[data-chain="56"]').click()
    page.evaluate('b=>pending["/api/memecoin/wallet-map/universe?chain=56"](new Response(JSON.stringify(b)))', _body('56'))
    page.wait_for_function("document.querySelector('#sky-title').textContent==='BSC universe'")
    assert page.locator('.moon').count() == 0
    assert 'relationships not measured' in page.locator('#sky-caption').inner_text()

def test_phone_search_all_wallets_and_empty_chain(universe_page):
    page, _ = universe_page
    page.set_viewport_size({'width': 390, 'height': 844})
    _draw(page)
    page.locator('#find').fill('Beta')
    page.locator('[data-result="B"]').click()
    assert page.locator('.planet.selected').get_attribute('data-node') == 'B'
    page.locator('#browse').click()
    assert page.locator('[data-browse]').count() == 3
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.evaluate('b=>walletMap.drawUniverse(b)', dict(_body('56'), wallets=[]))
    assert 'No wallets observed' in page.locator('.inspector').inner_text()
    assert 'NaN' not in page.locator('.world').get_attribute('transform')


def test_failed_observation_refresh_does_not_show_cached_moons_as_current(universe_page):
    page, _ = universe_page
    _draw(page)
    page.evaluate("""async body=>{
      window.fetch=async url=>{
        if(String(url).includes('/universe?'))return new Response(JSON.stringify(body));
        if(String(url).includes('/herders?'))return new Response('{}',{status:503});
        return new Response(JSON.stringify({stamp:{authority_state:'ok'},funnel:[],clips:{},evm_candidates:{}}));
      };
      await walletMap.load();
    }""", _body())
    assert 'Observations unavailable' in page.locator('.wu-live-status').inner_text()
    assert page.locator('.moon').count()==0
    assert 'Repeat buyers unknown' in page.locator('.planet.selected').text_content()


def test_phone_explore_from_details_brings_the_map_back_into_view(universe_page):
    page, _ = universe_page
    page.set_viewport_size({'width':390,'height':844})
    _draw(page)
    page.locator('#inspect-focus').click()
    bounds=page.locator('.space-map').bounding_box()
    assert bounds['y']>=0 and bounds['y']+bounds['height']<=844, bounds
    page.locator('[data-detail-moon="Moon"]').click()
    page.locator('#show-connections').click()
    bounds=page.locator('.space-map').bounding_box()
    assert bounds['y']>=0 and bounds['y']+bounds['height']<=844, bounds


def test_execution_coverage_is_selected_wallet_scoped_and_readable_on_phone(universe_page):
    page,_=universe_page
    _draw(page)
    body=_body()
    body['execution_research']={'state':'ok','cells':[
        {'wallet':'A','chain':'solana','signals':7,'complete_episodes':0,'missing':['Exact whole-bag exits','Future comparison']},
        {'wallet':'B','chain':'solana','signals':12,'complete_episodes':0,'missing':['Verified source address']} ]}
    page.evaluate('b=>walletMap.drawUniverse(b)',body)
    assert page.locator('.wu-execution-research').get_by_text('7',exact=True).count()==1
    page.locator('#find').fill('Beta'); page.locator('[data-result="B"]').click()
    card=page.locator('.wu-execution-research')
    assert card.get_by_text('12',exact=True).count()==1
    card.locator('summary').click(); assert card.get_by_text('Verified source address',exact=True).is_visible()
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    assert 'No strategy supported yet' in card.inner_text()


def test_wallet_dossier_notes_and_link_follow_wallet_and_chain(universe_page):
    from playwright.sync_api import expect
    from urllib.parse import parse_qs, urlparse
    page, _ = universe_page
    saved = []
    def endpoint(route):
        req = route.request
        query = parse_qs(urlparse(req.url).query)
        wallet, chain = query['wallet'][0], query['chain'][0]
        if req.method == 'POST':
            saved.append((wallet, chain, req.post_data_json['text']))
            route.fulfill(json={'ok': True}); return
        route.fulfill(json={'state': 'ok', 'wallet': wallet, 'chain': chain,
            'funnel': {'observed_buys': 3, 'valid_entries': 0, 'excluded_or_unknown': 3, 'distinct_coins': 2},
            'coverage': {'days_observed': 1, 'stale': False}, 'entries': [], 'rules': [],
            'notes': [{'at': 'today', 'text': '<img src=x onerror=alert(1)>'}], 'baseline': None})
    page.route('**/api/memecoin/wallet-map/dossier?**', endpoint)
    page.route('**/api/memecoin/csrf-token', lambda route: route.fulfill(json={'csrf_token': 'test'}))
    _draw(page)
    dossier = page.locator('.wu-dossier')
    expect(dossier.get_by_text('Defaults not sealed. Existing paper history cannot qualify.')).to_be_visible()
    assert dossier.locator('img').count() == 0
    # W3: research notes sit collapsed in "Notes and raw records", after Live authority
    notes = page.locator('.wu-more .wu-notes')
    assert notes.get_attribute('open') is None
    notes.locator('summary').click()
    notes.get_by_role('textbox', name='Research note').fill('A first research note')
    page.locator('#next-wallet').click()
    expect(dossier.get_by_role('link', name='Open wallet link')).to_have_attribute('href', '/?tab=wallet-map&wm_chain=solana&wm_wallet=B')
    page.locator('#previous-wallet').click()
    expect(notes.get_by_role('textbox', name='Research note')).to_have_value('A first research note')
    if notes.get_attribute('open') is None:
        notes.locator('summary').click()
    notes.get_by_role('button', name='Save note').click()
    expect(notes.get_by_role('textbox', name='Research note')).to_have_value('')
    assert saved == [('A', 'solana', 'A first research note')]
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


def test_dossier_link_restores_selected_wallet_on_first_draw(universe_page):
    page, _ = universe_page
    page.goto('http://sniper.test/?tab=wallet-map&wm_chain=solana&wm_wallet=B')
    page.add_style_tag(content=(ROOT / 'static/wallet_universe.css').read_text())
    page.add_script_tag(content=(ROOT / 'static/wallet_universe.js').read_text())
    page.add_script_tag(content=(ROOT / 'static/wallet_map.js').read_text())
    _draw(page)
    assert page.locator('.inspector h2').inner_text() == 'Beta'
    assert 'wm_wallet=B' in page.url


def test_narrow_expanded_details_overlay_takes_focus_and_escape_returns_it(universe_page):
    page, _ = universe_page
    page.set_viewport_size({'width': 664, 'height': 652})
    _draw(page)
    page.locator('#full-screen').click()
    assert not page.locator('.inspector').is_visible()
    page.locator('#details').click()
    assert page.locator('.main.details-open').count() == 1
    assert page.locator('.inspector').is_visible()
    assert page.evaluate("document.activeElement.closest('.inspector') !== null")
    page.keyboard.press('Escape')
    assert page.locator('.main.details-open').count() == 0
    assert page.locator('.main.expanded').count() == 1, 'Escape closes the overlay first, not expanded mode'
    assert page.evaluate("document.activeElement.dataset.node") == 'A'
    page.locator('#details').click()
    page.locator('#details-close').click()
    assert page.locator('.main.details-open').count() == 0
    assert page.evaluate("document.activeElement.dataset.node") == 'A'


def test_wide_layout_puts_the_summary_beside_the_map_and_expanded_needs_no_details(universe_page):
    page, _ = universe_page
    page.set_viewport_size({'width': 1366, 'height': 768})
    _draw(page)
    sky, summary = page.locator('.sky').bounding_box(), page.locator('.inspector').bounding_box()
    assert summary['x'] >= sky['x'] + sky['width'] - 1 and 480 <= summary['width'] <= 720, (sky, summary)
    assert page.evaluate("getComputedStyle(document.querySelector('.inspector')).overflowY") == 'visible'
    page.locator('#full-screen').click()
    assert page.locator('.inspector').is_visible() and not page.locator('#details').is_visible()
    page.set_viewport_size({'width': 1000, 'height': 768})
    page.keyboard.press('Escape')
    sky, summary = page.locator('.sky').bounding_box(), page.locator('.inspector').bounding_box()
    assert summary['y'] >= sky['y'] + sky['height'] - 1, 'below 1100px the summary stacks under the map'


def test_map_labels_keep_a_12px_floor_and_grow_when_zoomed_in(universe_page):
    page, _ = universe_page
    _draw(page)
    size = lambda: page.evaluate("document.querySelector('.planet.selected .planet-label').getBoundingClientRect().height")
    page.locator('#zoom-reset').click()
    at_fit = size()
    page.locator('#zoom-out').click()
    assert abs(size() - at_fit) < 0.5, 'below 100% labels hold their floor'
    page.locator('#zoom-reset').click()
    page.locator('#zoom-in').click(); page.locator('#zoom-in').click()
    assert size() > at_fit * 1.5, 'above 100% labels grow with the map'


def test_expanded_mode_hides_the_app_bottom_nav_so_details_takes_the_click(universe_page):
    # Gamma HOLD on 9837b6030: at <= 767px the app shell's fixed bottom nav (z-index 9000) covered the
    # expanded zoom bar, so a real click on Details landed on the nav.
    page, _ = universe_page
    page.set_viewport_size({'width': 664, 'height': 652})
    page.evaluate("document.body.insertAdjacentHTML('beforeend','<nav class=\"mobile-bottom-nav\" id=\"mobile-bottom-nav\"><a>Home</a></nav>')")
    _draw(page)
    nav = page.locator('#mobile-bottom-nav')
    assert nav.is_visible()
    page.locator('#full-screen').click()
    assert not nav.is_visible()
    box = page.locator('#details').bounding_box()
    assert page.evaluate('([x,y])=>document.elementFromPoint(x,y).id', [box['x'] + box['width'] / 2, box['y'] + box['height'] / 2]) == 'details'
    page.mouse.click(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
    assert page.locator('.main.details-open').count() == 1
    page.keyboard.press('Escape'); page.keyboard.press('Escape')
    assert page.locator('.main.expanded').count() == 0 and nav.is_visible()


def _cell(page, uid, col):
    return page.locator(f'.wu-table tr[data-uid="{uid}"] [data-col="{col}"]').inner_text().strip()


def test_wallet_map_lands_on_the_table_and_the_toggle_reaches_the_map(universe_page):
    page, _ = universe_page
    _draw(page, view=None)
    assert page.locator('.wu-table').is_visible() and not page.locator('.sky').is_visible()
    assert page.locator('#pane-table').get_attribute('aria-pressed') == 'true'
    page.locator('#pane-map').click()
    assert page.locator('.sky').is_visible() and not page.locator('.wu-table').is_visible()
    page.locator('#pane-table').click()
    assert page.locator('.wu-table').is_visible()


def test_a_table_row_click_opens_that_wallet_in_the_summary_and_stays_on_the_table(universe_page):
    page, _ = universe_page
    _draw(page, view=None)
    page.locator('.wu-table tr[data-uid="B"] td[data-col="n"]').click()
    assert page.locator('.inspector h2').inner_text() == 'Beta'
    assert page.locator('.wu-table tr[data-uid="B"]').get_attribute('aria-selected') == 'true'
    assert page.locator('.wu-table').is_visible()


def test_the_table_keeps_unknown_apart_from_none_and_a_measured_zero(universe_page):
    page, _ = universe_page
    _draw(page, view=None)
    body = _body()
    body['wallets'][0].update(armed=True, funded=dict(state='ok', open_count=0, closed_count=4, roi_pct=12.5),
                              links=dict(state='ok', by_type=dict(follows=1, dumps_on=0, shared_funder=0,
                                                                  co_buy_slot=3, slot_follower=2)))
    body['wallets'][1].update(funded=dict(state='none', open_count=0, closed_count=0),
                              links=dict(state='ok', by_type=dict(follows=0, dumps_on=0, shared_funder=0,
                                                                  co_buy_slot=0, slot_follower=0)))
    page.evaluate('b=>walletMap.drawUniverse(b)', body)       # wallets[2] carries no funded or links field
    assert _cell(page, 'A', 'funded').split() == ['+12.5%', '4', 'closes']
    assert _cell(page, 'B', 'funded') == ''
    assert _cell(page, 'C', 'funded') == 'unknown'
    assert _cell(page, 'A', 'links') == '6' and _cell(page, 'B', 'links') == '0'
    assert 'co-buy 3' in page.locator('.wu-table tr[data-uid="A"] [data-col="links"]').get_attribute('title')
    assert _cell(page, 'C', 'links') == 'unknown'
    body['wallets'][2].update(funded=dict(state='unavailable'), links=dict(state='unavailable', by_type=None))
    page.evaluate('b=>walletMap.drawUniverse(b)', body)
    assert _cell(page, 'C', 'funded') == 'unavailable' and _cell(page, 'C', 'links') == 'unavailable'


def _in_app_shell(page):
    """The dashboard's content area (2rem padding, its own vertical scroller and scrollbar) around the tab,
    as in templates/index.html, so a width test measures the real column, not a bare 1366px page."""
    page.evaluate("""()=>{const main=document.createElement('main');main.className='main-content';
      main.innerHTML='<div class="content-area"></div>';document.body.prepend(main);
      const tab=document.getElementById('tab-wallet-map');tab.classList.add('tab-panel','active');
      main.firstChild.append(tab);}""")


def _widest_table(page):
    """Every column at the longest label the table code can emit (Gamma HOLD on 970080005, Alpha):
    Wallet a long unbroken handle; Read coins 'not read'; Repeat buyers 'few reads' over '999/999';
    Timing 'unranked' over 'n 9 < 10'; Links 'unavailable'; Funded '−1234.5%' over '1234 closes', and
    'unavailable'; Newest link '59 min'. A new, longer label must widen this fixture or fail the test."""
    from datetime import datetime, timedelta, timezone
    seen = (datetime.now(timezone.utc) - timedelta(minutes=59)).isoformat()
    graph = dict(stamp={'error': None, 'stale': False, 'computed_at': '2026-10-03T10:00:00Z'}, shared=[], probes=[], herders=[
        dict(id='A', n=999, regulars=None, regular_list=[], drew=999, members=0, read=999, units=999,
             entry_order=dict(n=9, share_before=.5)),
        dict(id='B', n=10, regulars=1, regular_list=[dict(id='Moon', k=4)], drew=4, members=2, read=10, units=10,
             entry_order=dict(n=10, share_before=0))])
    body = _body()
    body['wallets'][0].update(handle='DegenCapitalLLCwithaverylonghandle', funded=dict(state='ok', closed_count=1234, roi_pct=-1234.5),
                              links=dict(state='unavailable', by_type=None))
    body['wallets'][1].update(funded=dict(state='unavailable'), links=dict(state='stale', by_type=dict(
        follows=9999, dumps_on=0, shared_funder=0, co_buy_slot=0, slot_follower=0)))
    body['edges'] = [dict(src_uid='A', dst_uid='B', kind='follows', last_seen=seen)]
    page.evaluate('b=>{walletMap.draw(b.g);walletMap.drawUniverse(b.u)}', dict(g=graph, u=body))
    texts = page.locator('.wu-table tbody').inner_text()
    for label in ('not read', 'few reads', '999/999', 'unranked', 'n 9 < 10', 'unavailable', '−1234.5%', '1234 closes', '59 min'):
        assert label in texts, label


def test_the_seven_decision_columns_fit_beside_the_summary_at_1366_without_horizontal_scroll(universe_page):
    # Alpha 2026-10-07: laptop width is the operator's main screen; hidden links/funded defeat table-first.
    page, _ = universe_page
    page.set_viewport_size({'width': 1366, 'height': 768})
    _in_app_shell(page)
    _widest_table(page)
    heads = page.locator('.wu-table thead th').all_inner_texts()
    assert heads == ['Wallet', 'Read coins', 'Repeat buyers', 'Timing rank', 'Links', 'Funded return', 'Newest link']
    assert page.evaluate("(s=>s.scrollWidth<=s.clientWidth)(document.querySelector('.wu-table-scroll'))")
    assert page.locator('.inspector').is_visible()


def test_at_664_the_table_may_scroll_inside_its_box_but_the_page_never_scrolls_sideways(universe_page):
    page, _ = universe_page
    page.set_viewport_size({'width': 664, 'height': 652})
    _in_app_shell(page)
    _widest_table(page)
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    assert page.locator('.wu-table tbody th').first.evaluate('e=>getComputedStyle(e).position') == 'sticky'


# ---------------------------------------------------------------- W3 summary order and wording
SUMMARY_ORDER = ['Observation', 'Sample and age', 'Limitation', 'Supporting evidence', 'Next step',
                 'Execution research', 'Live authority · changes real money', 'Notes and raw records']


def _actions(kind, row, clips):
    import json
    import subprocess
    js = ("const C=require('./static/wallet_map.js');process.stdout.write(JSON.stringify(C.actionsFor(%s,%s,%s)))"
          % (json.dumps(kind), json.dumps(row), json.dumps(clips)))
    return json.loads(subprocess.run(['node', '-e', js], capture_output=True, text=True, check=True, cwd=ROOT).stdout)


def test_the_summary_reads_in_the_fixed_order_with_live_authority_last_before_the_collapsed_rest(universe_page):
    page, _ = universe_page
    _draw(page, view=None)
    labels = page.locator('.inspector section.wu-sec').evaluate_all('s=>s.map(e=>e.getAttribute("aria-label"))')
    assert labels == SUMMARY_ORDER
    assert page.locator('.wu-observation').inner_text() == (
        '1 repeat buyer across 10 read coins; another decoded buyer landed after it in the same slot on 4/10.')


@pytest.mark.parametrize('key', ['arm', 'retire', 'trial'])
def test_live_authority_controls_keep_their_endpoint_confirm_and_csrf_after_the_move(universe_page, key):
    # Spec W3 check: the controls only move in the DOM. Each one still sends the actionsFor step with
    # the dashboard's CSRF header and asks the same confirmation.
    import time
    from urllib.parse import urlparse
    page, _ = universe_page
    clips = {'solana': {'trial': 25, 'full_max': 175}}
    funnel = dict(stamp={'authority_state': 'ok'}, funnel=[], clips=clips, evm_candidates={}, solana_candidates=[], rotation=[])
    calls = []
    def wallets(route):
        req = route.request
        calls.append((req.method, urlparse(req.url).path, req.headers.get('x-csrf-token')))
        route.fulfill(json={'ok': True})
    page.route('**/api/memecoin/csrf-token', lambda r: r.fulfill(json={'csrf_token': 'tok'}))
    page.route('**/api/memecoin/wallets**', wallets)
    page.route('**/api/memecoin/wallet-map/funnel**', lambda r: r.fulfill(json=funnel))
    page.evaluate('window.asks=[];window.confirm=m=>{window.asks.push(m);return true;};void 0')   # a bare function would be called
    page.evaluate("document.getElementById('tab-wallet-map').insertAdjacentHTML('beforeend','<div id=\"follow-funnel-stamp\"></div><div id=\"follow-funnel-body\"></div>')")
    page.evaluate('f=>walletMap.drawFunnel(f)', funnel)
    _draw(page, view=None)
    act = {a['key']: a for a in _actions('paper', {'uid': 'A', 'handle': 'Alpha', 'chain': 'solana', 'armed': [],
                                                   'no_commands': 'proven address unavailable'}, clips)}[key]
    step = act['steps'][-1]
    page.locator('section[aria-label="Live authority · changes real money"]').locator(f'button[data-key="{key}"]').click()
    deadline = time.time() + 3
    while time.time() < deadline and not any(c[0] == step['method'] and c[1] == step['url'] for c in calls):
        page.wait_for_timeout(50)
    sent = [c for c in calls if c[1] == step['url']]
    assert sent and all(c[2] == 'tok' for c in sent), calls
    assert sent[-1][0] == step['method'], sent
    assert page.evaluate('window.asks') == [act['confirm']]


def test_supporting_coins_gaps_and_a_saved_question_from_the_wallet_view(universe_page):
    from urllib.parse import parse_qs, urlparse
    page, _ = universe_page
    view = {'graph_state': 'ok', 'stamp': {'error': None, 'graph_state': 'ok'}, 'coins_total': 3, 'coins_omitted': 0,
            'funded': {'state': 'ok', 'roi_pct': 12.5, 'windows': {'7d': {'roi_pct': -3.0, 'closed': 2}}, 'closed_count': 4,
                       'open_count': 1, 'realized_pnl': -42.0},
            'funded_scope': 'robot entries only; only armed wallets have rows',
            'herd': {'regulars': [{'id': 'Moon', 'k': 4, 'n': 10, 'other_herds': 2}]},
            'coins': [
                {'mint': 'MintEntered', 'at': '2026-10-07T09:00:00Z', 'followers': 3, 'regulars': ['Moon'], 'after': [],
                 'source': {'signature': 'S1'}, 'funded': {'state': 'entered', 'positions': [{'is_open': False, 'return_pct': 31.5, 'exit_reason': 'tp'}],
                                                         'other_positions_same_coin': [{}]}},
                {'mint': 'MintRefused', 'at': '2026-10-07T08:00:00Z', 'followers': None, 'regulars': None, 'after': None,
                 'source': {'signature': 'S2'}, 'funded': {'state': 'not_entered', 'attempts': 1, 'reason': 'mcap over ceiling', 'other_positions_same_coin': []}},
                {'mint': 'MintUnread', 'at': '2026-10-07T07:00:00Z', 'followers': 0, 'regulars': [], 'after': [],
                 'source': {'signature': 'S3'}, 'funded': {'state': 'unavailable'}}],
            'gaps': [{'key': 'small_sample', 'n': 7, 'what': 'Only 7 read first-buy coins; repeat buyers are named from 10.',
                      'fill': 'At least 10 read first-buy coins for this wallet.'},
                     {'key': 'no_funded_closes', 'what': 'No funded closes for this wallet on this chain.',
                      'fill': 'Funded closes; only wallets we armed have them.'}]}
    saved = []
    page.route('**/api/memecoin/wallet-map/wallet?**', lambda r: r.fulfill(json=view))
    page.route('**/api/memecoin/csrf-token', lambda r: r.fulfill(json={'csrf_token': 'tok'}))
    def dossier(route):
        req = route.request
        if req.method == 'POST':
            saved.append((req.headers.get('x-csrf-token'), parse_qs(urlparse(req.url).query)['wallet'][0], req.post_data_json['text']))
            route.fulfill(json={'ok': True}); return
        route.fulfill(status=503, json={})
    page.route('**/api/memecoin/wallet-map/dossier?**', dossier)
    _draw(page, view=None)
    coins = page.locator('#wu-supporting [data-coins]')
    coins.locator('.wu-coins').first.wait_for()
    text = coins.inner_text()
    assert '+12.5% all time over 4 closes · 7d −3.0% over 2 closes · 1 open · −$42 realized' in text   # W5b populations
    assert '+31.5% · tp · +1 other row on this coin, not counted' in text
    assert 'order unknown' in text and 'not entered · mcap over ceiling' in text and 'unavailable' in text
    assert 'Unverified high-overlap addresses' in text and 'after 2 other wallets' in text
    gaps = page.locator('#wu-next [data-gaps]')
    assert 'Only 7 read first-buy coins' in gaps.inner_text() and 'No funded closes' in gaps.inner_text()
    gaps.locator('input[name="wu-gap"]').nth(1).check()
    page.locator('#wu-save-question').click()
    page.locator('#wu-save-status', has_text='Saved to research notes.').wait_for()
    assert len(saved) == 1 and saved[0][0] == 'tok' and saved[0][1] == 'A'
    assert saved[0][2] == ('Research question. Observation: 1 repeat buyer across 10 read coins; another decoded buyer landed '
                           'after it in the same slot on 4/10.\nGap: No funded closes for this wallet on this chain.\n'
                           'Would fill it: Funded closes; only wallets we armed have them.')


def test_user_facing_words_are_repeat_buyers_not_moons_satellites_herders_or_following(universe_page):
    page, _ = universe_page
    _draw(page)
    page.locator('#browse').click()
    words = page.locator('#wallet-universe').inner_text().lower() + page.locator('#wallet-universe').evaluate(
        'h=>[...h.querySelectorAll("[aria-label],[placeholder],option")].map(e=>(e.getAttribute("aria-label")||"")+" "+(e.getAttribute("placeholder")||"")+" "+e.textContent).join(" ")').lower()
    import re
    for word in (r'\bsatellites?\b', r'\bmoons\b', r'\bherders?\b', r'\bfollowing\b', r'\bprobe bots?\b', r'\bsystems?\b'):
        assert not re.search(word, words), (word, re.search(word, words) and words[max(0, re.search(word, words).start()-40):re.search(word, words).end()+20])


def test_a_focused_fold_inside_the_async_dossier_keeps_focus_when_the_same_wallet_redraws(universe_page):
    # Alpha 2026-10-07 (Gamma's known limit on W4a): the dossier lands after the redraw, so W4a's
    # synchronous restore found no fold to focus; the fold is found by (summary, nth) once it lands.
    from playwright.sync_api import expect
    page, _ = universe_page
    page.route('**/api/memecoin/wallet-map/dossier?**', lambda r: r.fulfill(json={
        'state': 'ok', 'wallet': 'A', 'chain': 'solana', 'funnel': {'observed_buys': 1, 'valid_entries': 1},
        'coverage': {'days_observed': 1, 'stale': False}, 'entries': [], 'rules': ['r'], 'notes': [], 'baseline': None}))
    _draw(page, view=None)
    fold = page.locator('.wu-dossier details > summary', has_text='Paper rules and coverage')
    fold.focus()
    page.evaluate('b=>walletMap.drawUniverse(b)', _body())             # a same-wallet redraw (poll, Refresh)
    expect(page.locator('.wu-dossier details > summary', has_text='Paper rules and coverage')).to_be_focused()


# ---------------------------------------------------------------- W5b summary side
def _seven_wallets(page):
    body = _body()
    body['wallets'] = [dict(uid=u, handle='W' + u, from_roster=True, coverage={}) for u in 'ABCDEFG']
    page.evaluate('b=>walletMap.drawUniverse(b)', body)
    return body


def test_the_comparison_tray_lists_its_members_and_stops_at_six(universe_page):
    page, _ = universe_page
    _draw(page, view=None)
    _seven_wallets(page)
    for uid in 'ABCDEF':
        page.locator(f'.wu-table tr[data-uid="{uid}"] td[data-col="n"]').click()
        page.locator('#wu-compare').click()
    tray = page.locator('.wu-tray')
    assert 'Comparison · 6 of 6 wallets' in tray.inner_text()
    assert [' '.join(t.split()) for t in tray.locator('li').all_inner_texts()] == ['W' + u + ' Remove' for u in 'ABCDEF']
    page.locator('.wu-table tr[data-uid="G"] td[data-col="n"]').click()
    assert page.locator('#wu-compare').is_disabled() and 'Full: 6 of 6' in page.locator('.wu-tray').inner_text()
    page.locator('[data-tray-remove="A"]').click()
    assert 'Comparison · 5 of 6 wallets' in page.locator('.wu-tray').inner_text() and page.locator('#wu-compare').is_enabled()


def test_a_chain_change_asks_before_it_clears_the_comparison(universe_page):
    page, _ = universe_page
    _draw(page, view=None)
    page.locator('#wu-compare').click()
    page.evaluate('window.asks=[];window.answer=false;window.confirm=m=>{window.asks.push(m);return window.answer;};void 0')
    page.locator('.chain[data-chain="56"]').click()
    assert page.locator('.chain[data-chain="solana"]').get_attribute('aria-pressed') == 'true'
    assert 'Comparison · 1 of 6 wallets' in page.locator('.wu-tray').inner_text()
    assert len(page.evaluate('window.asks')) == 1 and 'will be cleared' in page.evaluate('window.asks')[0]
    page.evaluate('window.answer=true')
    page.locator('.chain[data-chain="56"]').click()
    assert page.locator('.chain[data-chain="56"]').get_attribute('aria-pressed') == 'true'


def test_shared_repeat_buyers_list_every_link_with_a_total_and_headline_numbers_name_their_population(universe_page):
    page, _ = universe_page
    graph = dict(stamp={'error': None, 'stale': False, 'computed_at': '2026-10-03T10:00:00Z'}, probes=[],
                 shared=[dict(a='A', b=u, count=2) for u in 'BCDE'],
                 herders=[dict(id=u, n=10, regulars=1, regular_list=[dict(id='Moon', k=4)], drew=4, members=2, read=10,
                               units=10, entry_order=dict(n=10, share_before=0)) for u in 'ABCDE'])
    body = _body()
    body['wallets'] = [dict(uid=u, handle='W' + u, from_roster=True, coverage={}) for u in 'ABCDE']
    page.evaluate('b=>{walletMap.draw(b.g);walletMap.drawUniverse(b.u)}', dict(g=graph, u=body))
    shared = page.locator('#wu-supporting h4', has_text='Shared repeat buyers with')
    assert shared.inner_text() == 'Shared repeat buyers with · 4 wallets'
    assert page.locator('#wu-supporting [data-link]').count() == 4
    facts = page.locator('#wu-supporting .facts').inner_text()
    assert 'Repeat buyers · of 10 read coins' in facts and 'Buyers seen after it · in 10 read coins' in facts


def _overlap(a, b):
    return a['x'] < b['x'] + b['width'] and b['x'] < a['x'] + a['width'] and a['y'] < b['y'] + b['height'] and b['y'] < a['y'] + a['height']


@pytest.mark.parametrize('width,height', [(1366, 768), (900, 700), (664, 652), (390, 800)])
def test_hover_help_legend_and_zoom_never_overlap(universe_page, width, height):
    """Gamma acceptance item 1 (2026-10-07): at 1366 Map view #map-help overlapped the two-row legend.
    Each had its own bottom offset; one bottom row (notes column + zoom) cannot overlap at any width."""
    page, _ = universe_page
    page.set_viewport_size({'width': width, 'height': height})
    _draw(page)
    page.evaluate("()=>{const h=document.querySelector('#hover-info');h.innerHTML='Alpha<span>1 repeat buyers · click to enter this system</span>';h.hidden=false}")
    names = ['#hover-info', '#map-help', '.legend', '.zoom']
    boxes = {n: page.locator(n).bounding_box() for n in names}
    sky = page.locator('.sky').bounding_box()
    for i, a in enumerate(names):
        assert sky['x'] <= boxes[a]['x'] and boxes[a]['x'] + boxes[a]['width'] <= sky['x'] + sky['width'] + 1, a
        for b in names[i + 1:]:
            assert not _overlap(boxes[a], boxes[b]), (a, b, boxes[a], boxes[b])


def test_scope_text_has_single_spaces_and_chain_names_come_from_the_payload(universe_page):
    page, _ = universe_page
    _draw(page)
    assert '  ' not in page.locator('#scope').inner_text()
    body = _body()
    body['chains'] += [dict(id='1', label='Ethereum'), dict(id='57073', label='Ink')]
    page.evaluate('b=>walletMap.drawUniverse(b)', body)
    assert page.evaluate("()=>['1','57073','56'].map(WalletMapCore.chainLabel)") == ['Ethereum (1)', 'Ink (57073)', 'BSC (56)']


@pytest.mark.parametrize('legacy_observed',[True,False])
def test_evm_links_show_both_sides_actual_order_and_funding_asset(universe_page,legacy_observed):
    page,_=universe_page
    if legacy_observed:_draw(page)
    body=_body('8453')
    address='0x'+'a'*40;peer='0x'+'b'*40;sig='0x'+'c'*64
    body['wallets'][0]['identity']=dict(state='partial',generated_at='2026-10-10T12:00:00Z',
        links_method='armed-wallet-links-v1',explorer_base='https://basescan.org/',verified_addresses=[dict(address=address,chain='8453')],
        coverage=dict(sampled_coins=5,unknown_coins=2,funding_state='partial',reason='history_capped',known_roster_peers_included=True),
        related_wallets=[dict(address=peer,chain='8453',status='funding_verified_link',n_coins=3,before_coins=2,after_coins=1,
            eligible_coins=5,unknown_coins=2,days=2,lead_p50_s=0,lead_p90_s=8,missed_coins=None,
            funding_clues=[dict(kind='verified_funding',verified=True,funder=peer,recipient=address,amount='0.25',symbol='ETH',funding_signature=sig,hub=True)],
            evidence=[dict(mint=address,t0=1791630000,direction='before',lead_s=0,order=[5,2],source_order=[5,4],
                buy_signature=sig,source_signature=sig)])])
    page.evaluate('b=>walletMap.drawUniverse(b)',body)
    section=page.locator('.wu-identity')
    assert 'Before main: 2 coins' in section.inner_text()
    assert 'After main: 1 coin' in section.inner_text()
    assert '0.25 ETH' in section.inner_text() and 'hub' in section.inner_text()
    assert section.locator(f'a[href="https://basescan.org/address/{peer}"]').count()>=1
    assert section.get_by_role('link',name='Funding receipt',exact=True).get_attribute('href')=='https://basescan.org/tx/'+sig
    section.get_by_text('Trade evidence',exact=True).click()
    assert '5 / 2' in section.inner_text() and '5 / 4' in section.inner_text()
    assert 'Known roster peers included' in section.inner_text()
    assert 'Solana sample' not in section.inner_text()
    assert 'not measured on this chain' not in page.locator('.wu-caption').inner_text()
    assert 'relationships are not measured' not in page.locator('#scope').inner_text()
    assert '3 of 5 sampled coins' in page.locator(f'[data-moon="{peer}"]').get_attribute('aria-label')
    page.locator('[data-detail-moon]').click()
    assert 'Before main: 2 coins' in page.locator('.inspector').inner_text()
    assert 'first-buy slot' not in page.locator('.inspector').inner_text()
    assert '3/5 coins' in page.locator('.inspector').inner_text()
    assert '/0 coins' not in page.locator('.inspector').inner_text()
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')


def test_revoking_saved_links_does_not_mutate_the_legacy_graph(universe_page):
    page,_=universe_page;_draw(page);body=_body()
    peer='B'*32
    body['wallets'][0]['identity']=dict(state='partial',links_method='armed-wallet-links-v1',related_wallets=[
        dict(address=peer,chain='solana',status='related_trader',n_coins=2,before_coins=2,after_coins=0,
             eligible_coins=3,unknown_coins=1,days=1,lead_p50_s=0,lead_p90_s=0,missed_coins=None,
             evidence=[],funding_clues=[])])
    page.evaluate('b=>walletMap.drawUniverse(b)',body)
    assert page.locator('[data-detail-moon]').count()==2
    page.evaluate('b=>walletMap.drawUniverse(b)',_body())
    assert page.locator('[data-detail-moon]').count()==1
