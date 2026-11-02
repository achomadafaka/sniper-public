"""Wallet Map W4b (spec docs/superpowers/specs/2026-10-07-wallet-map-overhaul-design.md): the URL carries
chain, view, wallet, satellite, size metric, table filters + sort, the comparison set and open folds;
reload restores it; a wallet step pushes history so Back returns to the prior wallet with its folds;
filters, sort, metric and "Next in list" replace instead of push; the fold list is bounded."""
import json
import subprocess
from pathlib import Path
from urllib.parse import quote

import pytest

from test_wallet_universe_navigation import _body, _draw, universe_page  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]


def _state(page):
    return page.evaluate('WalletUniverse.readUrlState(location.search)')


def _open_page(browser_page, url, errors=None):
    """A second page at `url`, the same scripts as the fixture: a reload that keeps the URL."""
    page = browser_page.context.browser.new_page(viewport={"width": 1280, "height": 900})
    if errors is not None:
        page.on('pageerror', lambda e: errors.append(str(e)))
    page.route('http://sniper.test/**', lambda r: r.fulfill(content_type='text/html',
               body='<section id="tab-wallet-map"><div id="wallet-universe"></div><div id="wallet-map-action-status"></div></section>'))
    page.goto(url)
    page.add_style_tag(content=(ROOT / 'static/styles.css').read_text())
    page.add_style_tag(content=(ROOT / 'static/wallet_universe.css').read_text())
    page.add_script_tag(content=(ROOT / 'static/wallet_universe.js').read_text())
    page.add_script_tag(content=(ROOT / 'static/wallet_map.js').read_text())
    return page


def _pick(page, uid):
    if page.locator('.wu-table').is_visible():
        page.locator('.wu-table tr[data-uid="%s"]' % uid).click()
    else:
        page.locator('#browse').click()
        page.locator('[data-browse="%s"]' % uid).click()


def _fold(page):
    return page.locator('.inspector details.wu-proof')


def test_a_reload_restores_the_whole_state_object(universe_page):
    page, _ = universe_page
    graph = _draw(page, view='table')
    page.locator('#wu-f-min').fill('3')
    page.locator('#wu-f-ranked').select_option('yes')
    page.locator('.wu-table [data-sort-key]').first.click()
    _pick(page, 'B')
    page.locator('#pane-map').click()
    _fold(page).locator(':scope > summary').click()          # the comparison button sits inside it
    page.locator('#wu-compare').click()
    before = _state(page)
    assert before['wallet'] == 'B' and before['view'] == 'map' and before['filters']['min'] == 3
    assert before['chain'] == 'solana' and before['size'] == 'following'
    assert before['filters']['ranked'] == 'yes' and before['sort'] and before['compare'] == ['B']
    assert before['folds'].get('B'), "the open fold rides the URL"
    again = _open_page(page, page.url)
    again.evaluate('b=>{walletMap.draw(b.g);walletMap.drawUniverse(b.u)}', dict(g=graph, u=_body()))
    assert _state(again) == before
    assert again.locator('.planet.selected').get_attribute('data-node') == 'B'
    assert again.locator('#pane-map').get_attribute('aria-pressed') == 'true'
    assert again.locator('#wu-f-min').input_value() == '3' and again.locator('#wu-f-ranked').input_value() == 'yes'
    assert again.locator('.wu-table th[aria-sort]:not([aria-sort="none"])').count() == 1
    assert 'Remove' in again.locator('#wu-compare').inner_text()      # W5b: "Remove this wallet from the comparison"
    assert again.locator('.inspector details.wu-proof').evaluate('d=>d.open')
    again.close()


def test_back_returns_to_the_prior_wallet_with_its_folds(universe_page):
    page, _ = universe_page
    _draw(page)
    _pick(page, 'B')
    _fold(page).locator(':scope > summary').click()
    _pick(page, 'C')
    assert not _fold(page).evaluate('d=>d.open')
    page.evaluate('history.back()')
    page.wait_for_function("document.querySelector('.planet.selected')?.dataset.node==='B'")
    assert _fold(page).evaluate('d=>d.open'), "B comes back with its fold open"
    page.evaluate('history.back()')
    page.wait_for_function("document.querySelector('.planet.selected')?.dataset.node==='A'")
    page.evaluate('history.forward()')
    page.wait_for_function("document.querySelector('.planet.selected')?.dataset.node==='B'")


def test_filters_sort_pane_metric_and_next_in_list_replace_instead_of_push(universe_page):
    page, _ = universe_page
    _draw(page, view='table')
    length = page.evaluate('history.length')
    # Two wallets stay visible: "Next in list" steps through the visible rows only (review SF04),
    # so a filter set that hides every wallet would leave nothing to step to.
    page.locator('#wu-f-min').fill('2')
    page.locator('#wu-f-measured').select_option('yes')
    page.locator('#wu-f-ranked').select_option('yes')
    page.locator('.wu-table [data-sort-key]').nth(1).click()
    page.locator('#pane-map').click()
    page.locator('[aria-label="Planet size metric"]').select_option('following')
    nxt = page.locator('.inspector #next-wallet')
    assert nxt.get_attribute('aria-label') == 'Next in list'
    selected = page.locator('.planet.selected').get_attribute('data-node')
    nxt.click()
    assert page.locator('.planet.selected').get_attribute('data-node') != selected
    assert page.evaluate('history.length') == length, "none of these may flood Back"
    s = _state(page)
    assert s['filters'] == {'min': 2, 'ranked': 'yes', 'measured': 'yes', 'armed': '', 'funded': False}
    assert s['view'] == 'map' and s['sort'] and s['wallet'] != selected


def test_a_long_fold_list_keeps_the_newest_wallets_folds_only():
    js = """const U=require('./static/wallet_universe.js');
      const big=new Map([['A',new Set(Array.from({length:40},(_,i)=>'Fold number '+i+' with a long summary#0'))],
                         ['B',new Set(['Evidence and settings#0'])]]);
      const small=new Map([['A',new Set(['x#0'])],['B',new Set(['y#0'])]]);
      const huge=new Map([['B',new Set(Array.from({length:80},(_,i)=>'Fold '+i+' summary text#0'))]]);
      const out={big:U.encodeFolds(big,'B',1500),small:U.encodeFolds(small,'B',1500),huge:U.encodeFolds(huge,'B',1500)};
      process.stdout.write(JSON.stringify(out));"""
    out = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True, cwd=ROOT).stdout)
    assert json.loads(out['small']) == {'A': ['x#0'], 'B': ['y#0']}, "everything fits: every wallet kept"
    assert json.loads(out['big']) == {'B': ['Evidence and settings#0']}, "over the bound: the current wallet only"
    huge = json.loads(out['huge'])['B']
    assert huge[-1] == 'Fold 79 summary text#0' and len(huge) < 80, "still over: the oldest folds go first"
    from urllib.parse import quote
    assert all(len(quote(out[k], safe='')) <= 1500 for k in out)


def test_the_first_sort_click_after_typing_a_filter_is_not_lost(universe_page):
    """Found by W4b: the blur `change` rebuilt the table header under the click (W2 handler)."""
    page, _ = universe_page
    _draw(page, view='table')
    page.locator('#wu-f-min').fill('3')
    page.locator('.wu-table [data-sort-key]').first.click()
    assert page.locator('.wu-table th[aria-sort="ascending"]').count() == 1


# W4b HOLD (Gamma MEDIUM, Alpha): every restored param is validated. Unknown or malformed values fall
# back to the default, are dropped from the URL on the next replaceState, and never throw.

def _read(search):
    js = ("const U=require('./static/wallet_universe.js');"
          "process.stdout.write(JSON.stringify(U.readUrlState(process.argv[1])));")
    return json.loads(subprocess.run(["node", "-e", js, search], capture_output=True, text=True,
                                     check=True, cwd=ROOT).stdout)


@pytest.mark.parametrize("search, field, want", [
    ("wm_chain=%3Cx%3E", "chain", None), ("wm_chain=" + "9" * 40, "chain", None),
    ("wm_chain=56", "chain", "56"), ("wm_chain=57073", "chain", "57073"),
    ("wm_view=grid", "view", "table"),
    ("wm_wallet=%3Cimg%3E", "wallet", ""), ("wm_wallet=" + "a" * 200, "wallet", ""), ("wm_wallet=B-1_x", "wallet", "B-1_x"),
    ("wm_sat=%3Cx%3E", "sat", None), ("wm_sat=__proto__", "sat", None),
    ("wm_size=bogus", "size", None),
    ("wm_sort=n:sideways", "sort", None), ("wm_sort=a%20b:1", "sort", None), ("wm_sort=:1", "sort", None),
    ("wm_sort=n:-1", "sort", {"key": "n", "dir": -1}), ("wm_sort=n", "sort", {"key": "n", "dir": 1}),
    ("wm_f=Infinity,maybe,yes,armed,1", "filters", {"min": 0, "ranked": "", "measured": "yes", "armed": "armed", "funded": True}),
    ("wm_f=-5", "filters", {"min": 0, "ranked": "", "measured": "", "armed": "", "funded": False}),
    ("wm_f=2.5", "filters", {"min": 0, "ranked": "", "measured": "", "armed": "", "funded": False}),
    ("wm_cmp=A,A,%3Cx%3E,B,C,D,E,F,G", "compare", ["A", "B", "C", "D", "E", "F"]),
    ("wm_folds=%5B1%2C2%5D", "folds", {}), ("wm_folds=%7B", "folds", {}),
    ("wm_folds=" + quote(json.dumps({"A": "x", "B": ["ok#0", 5, "z" * 300], "<x>": ["a"], "__proto__": ["p"]})),
     "folds", {"B": ["ok#0"]}),
], ids=lambda v: v if isinstance(v, str) and len(v) < 40 else None)
def test_a_malformed_param_reads_as_its_default(search, field, want):
    assert _read(search)[field] == want


def test_garbage_never_throws():
    for search in ("%E0%A4%A", "wm_folds=null", "wm_folds=%22s%22", "wm_sort=%00", "wm_f=,,,,,,,", "wm_cmp=,,,"):
        assert _read(search)["view"] == "table"


def _load(again, body=None):
    from test_wallet_universe_navigation import _draw as draw
    return draw(again, view='table') if body is None else again.evaluate('b=>walletMap.drawUniverse(b)', body)


def test_gammas_bogus_sort_renders_the_table_unsorted_and_keeps_the_wallet(universe_page):
    page, _ = universe_page
    errors = []
    again = _open_page(page, 'http://sniper.test/?tab=wallet-map&wm_chain=solana&wm_sort=bogus:1&wm_wallet=B', errors)
    _load(again)
    assert errors == [], errors
    assert again.locator('.wu-table tbody tr').count() == 3
    assert again.locator('.planet.selected').get_attribute('data-node') == 'B'
    s = _state(again)
    assert s['wallet'] == 'B' and s['sort'] is None, "the bad sort leaves the URL, the wallet stays"
    again.close()


def test_an_unknown_wallet_satellite_view_and_metric_fall_back_and_leave_the_url(universe_page):
    page, _ = universe_page
    errors = []
    again = _open_page(page, 'http://sniper.test/?wm_wallet=ZZ&wm_sat=Nope&wm_size=capital&wm_view=grid', errors)
    _load(again)
    assert errors == [], errors
    s = _state(again)
    assert s['wallet'] == again.locator('.planet.selected').get_attribute('data-node') != 'ZZ'
    assert s['sat'] is None and s['size'] == 'following' and s['view'] == 'table'
    again.close()


def test_a_valid_satellite_is_restored_once_the_observations_arrive(universe_page):
    page, _ = universe_page
    again = _open_page(page, 'http://sniper.test/?wm_wallet=A&wm_sat=Moon')
    _load(again)
    assert _state(again)['sat'] == 'Moon'
    again.close()


def test_unknown_comparison_ids_and_their_folds_are_dropped(universe_page):
    page, _ = universe_page
    errors = []
    folds = quote(json.dumps({"ZZ": ["x#0"], "B": ["Evidence and settings#0"]}))
    again = _open_page(page, 'http://sniper.test/?wm_wallet=B&wm_cmp=B,ZZ&wm_folds=' + folds, errors)
    _load(again)
    assert errors == [], errors
    s = _state(again)
    assert s['compare'] == ['B'] and list(s['folds']) == ['B']
    assert 'ZZ' not in again.locator('.inspector').inner_text()
    again.close()


def test_malformed_filters_show_every_row_and_leave_the_url(universe_page):
    page, _ = universe_page
    again = _open_page(page, 'http://sniper.test/?wm_f=Infinity,maybe')
    _load(again)
    assert again.locator('.wu-table tbody tr').count() == 3
    assert again.locator('#wu-f-min').input_value() == '0'
    assert 'wm_f' not in again.url
    again.close()


_CHAINS = [dict(id='solana', label='Solana'), dict(id='56', label='BSC'), dict(id='1', label='Ethereum'),
           dict(id='8453', label='Base'), dict(id='57073', label='Ink')]


def _serve(page):
    """fetch stub: the universe answers per requested chain like the route (aliases canonical, an
    unknown chain 400 unknown_chain); every other endpoint fails."""
    page.evaluate("""chains=>{window.fetches=[];window.fetch=async url=>{url=String(url);fetches.push(url);
      if(!url.includes('/universe'))return new Response('{"error":"stub"}',{status:503});
      const asked=new URL(url,location.href).searchParams.get('chain'), chain={eth:'1'}[asked]||asked;
      if(!chains.some(c=>c.id===chain))return new Response('{"error":"unknown_chain"}',{status:400});
      return new Response(JSON.stringify({chain,chains,stamp:{authority_state:'ok'},graph_state:'not_measured',
        edges:[],wallets:[{uid:'E1',handle:'Eve',from_roster:true,coverage:{}}]}),{status:200});};}""", _CHAINS)


def _chain_now(page):
    return page.locator('.chain[aria-pressed="true"]').get_attribute('data-chain')


def test_a_bogus_chain_or_an_alias_resolves_through_the_server(universe_page):
    """Gamma LOW: ?wm_chain=zzz drew 'HTTP 400' with no chain buttons. The server names the chains."""
    page, _ = universe_page
    for asked, lands in (('zzz', 'solana'), ('eth', '1')):
        errors = []
        again = _open_page(page, 'http://sniper.test/?wm_chain=' + asked, errors)
        _serve(again)
        again.evaluate('walletMap.load()')
        again.wait_for_function("u=>new URL(location.href).searchParams.get('wm_chain')===u", arg=lands)
        assert _chain_now(again) == lands and again.locator('.chain[data-chain]').count() == 5
        assert errors == [], errors
        again.close()


def test_an_ethereum_deep_link_and_its_reload_stay_on_ethereum(universe_page):
    """Gamma HOLD 2: a five-chain constant rewrote ?wm_chain=1 to solana."""
    page, _ = universe_page
    url = 'http://sniper.test/?wm_chain=1&wm_wallet=E1'
    for _ in range(2):
        again = _open_page(page, url)
        _serve(again)
        again.evaluate('walletMap.load()')
        again.wait_for_function("document.querySelector('.chain[aria-pressed=\"true\"]')?.dataset.chain==='1'")
        assert _state(again)['chain'] == '1' and _state(again)['wallet'] == 'E1'
        url = again.url
        again.close()


def test_ethereum_then_base_then_back_returns_to_ethereum(universe_page):
    page, _ = universe_page
    again = _open_page(page, 'http://sniper.test/?wm_chain=solana')
    _serve(again)
    again.evaluate('walletMap.load()')
    again.wait_for_function("document.querySelectorAll('.chain[data-chain=\"1\"]').length===1")
    for chain in ('1', '8453'):
        again.locator('.chain[data-chain="%s"]' % chain).click()
        again.wait_for_function("c=>new URL(location.href).searchParams.get('wm_chain')===c", arg=chain)
    again.evaluate('history.back()')
    again.wait_for_function("document.querySelector('.chain[aria-pressed=\"true\"]')?.dataset.chain==='1'")
    assert _state(again)['chain'] == '1'
    again.close()


def test_a_failed_reload_keeps_the_link(universe_page):
    """A refresh whose universe read fails draws an empty view; it must not rewrite the URL from it."""
    page, _ = universe_page
    errors = []
    again = _open_page(page, 'http://sniper.test/?wm_wallet=B&wm_sat=Moon', errors)
    _load(again)
    before = again.url
    again.evaluate("""()=>{window.fetches=[];window.fetch=async url=>{fetches.push(String(url));
      return new Response(JSON.stringify({error:'stub'}),{status:503});};}""")
    again.locator('#map-refresh').click()
    again.wait_for_function("fetches.some(u=>u.includes('/universe'))")
    again.wait_for_timeout(200)
    assert again.url == before and errors == [], errors
    again.close()


def test_a_satellite_is_kept_while_the_observations_are_unavailable(universe_page):
    page, _ = universe_page
    again = _open_page(page, 'http://sniper.test/?wm_wallet=A&wm_sat=Moon')
    again.evaluate("b=>{walletMap.draw({stamp:{error:'request_failed'},herders:[],shared:[],probes:[]});walletMap.drawUniverse(b)}", _body())
    assert _state(again)['sat'] == 'Moon', "unavailable observations cannot judge the satellite"
    again.close()


# --- S3/S4 (Codex two-day review 2026-10-07): a restored entry replaces, never merges -------------

def _push_then_back(page, url):
    """Push `url`, push the current one again, then Back: a popstate onto `url`."""
    page.evaluate("u=>{const now=location.href;history.pushState(null,'',u);history.pushState(null,'',now)}", url)
    page.evaluate('history.back()')
    page.wait_for_function("u=>location.href===u", arg=url)


def test_back_to_a_same_wallet_entry_without_folds_closes_them_and_keeps_its_url(universe_page):
    """S3: the fold map of a history entry was merged into the open folds, so an absent fold stayed
    open, and the render's own toggle rewrote the restored URL with it."""
    page, _ = universe_page
    _draw(page)
    _pick(page, 'B')
    _fold(page).locator(':scope > summary').click()
    # The click's toggle event is asynchronous; its handler writes the fold into the URL. Wait for
    # that write: read early under load, a late handler re-opened the fold after Back (the flake).
    page.wait_for_function("()=>new URL(location.href).searchParams.has('wm_folds')")
    assert _fold(page).evaluate('d=>d.open') and _state(page)['folds'].get('B')
    bare = page.evaluate("()=>{const u=new URL(location.href);u.searchParams.delete('wm_folds');return u.href}")
    _push_then_back(page, bare)
    # The redraw is the popstate's; wait for its result, never a fixed time (a 300 ms sleep raced it
    # under load). Merged folds reopen inside the redraw's own microtask, so with the bug this times out.
    page.wait_for_function("()=>document.querySelector('.inspector details.wu-proof')?.open===false")
    assert page.url == bare and _state(page)['folds'] == {}


def test_a_same_chain_back_drops_an_unknown_comparison_id_before_it_draws(universe_page):
    """S4: the same-chain popstate path skipped drawUniverse's roster check, so an unknown ZZ joined
    the comparison with a fabricated zero relationship."""
    page, _ = universe_page
    _draw(page)
    _pick(page, 'B')
    _fold(page).locator(':scope > summary').click()
    page.locator('#wu-compare').click()
    page.wait_for_function("()=>new URL(location.href).searchParams.get('wm_cmp')==='B'")
    bogus = page.evaluate("()=>{const u=new URL(location.href);u.searchParams.set('wm_cmp','B,ZZ');return u.href}")
    _push_then_back(page, bogus)
    page.wait_for_function("()=>document.querySelectorAll('.inspector .wu-tray li').length===1")
    tray = page.locator('.inspector .wu-tray')
    assert 'ZZ' not in page.locator('.inspector').inner_text()
    assert tray.locator('li').count() == 1 and '1 of 6' in tray.inner_text()
