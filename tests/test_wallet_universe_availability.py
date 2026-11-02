"""Wallet Map W5a (spec docs/superpowers/specs/2026-10-07-wallet-map-overhaul-design.md): a per-chain
availability strip, size modes a chain cannot measure disabled with the reason, unknown drawn as a
dashed outline and a measured zero as a solid minimum planet, and the W3 header wording."""
from test_wallet_universe_navigation import _body, _draw, universe_page  # noqa: F401  (fixture)


def _wallets(proven=(), capital=()):
    out = []
    for uid, handle in (('A', 'Alpha'), ('B', 'Beta'), ('C', 'Gamma')):
        out.append(dict(uid=uid, handle=handle, from_roster=True,
                        coverage={'address': {'state': 'ok' if uid in proven else 'not_proven'}},
                        metrics={'capital': {'state': 'ok', 'usd': 100.0} if uid in capital
                                 else {'state': 'address_unproven', 'usd': None},
                                 'impact': {'state': 'address_unproven', 'value_usd': None}}))
    return out


def test_base_says_not_measured_and_never_draws_or_writes_a_zero(universe_page):
    page, _ = universe_page
    _draw(page)
    body = dict(_body('8453'), chains=[dict(id='solana', label='Solana'), dict(id='8453', label='Base')],
                graph_state='not_measured', graph={'state': 'not_measured', 'reason': 'chain_not_measured'},
                wallets=_wallets(proven=('A',)))
    page.evaluate('b=>walletMap.drawUniverse(b)', body)
    strip = page.locator('.wu-availability').inner_text()
    assert 'Roster 3 wallets' in strip
    assert 'Addresses proven on Base: 1 of 3' in strip
    assert 'Relationship graph: not measured on this chain' in strip
    assert 'Fresh capital: 0 of 3' in strip, "a count of capital reads is a measured zero"
    assert page.locator('.planet-body.unknown').count() == 3, "Base has no repeat-buyer measurement"
    assert '0 repeat buyers' not in page.locator('#wallet-universe').text_content()


def test_an_unknown_planet_is_dashed_and_a_measured_zero_is_a_solid_minimum(universe_page):
    page, _ = universe_page
    graph = _draw(page)
    graph['herders'].append(dict(id='B2', n=12, regulars=0, regular_list=[], drew=0, members=0, read=12,
                                 units=12, entry_order=dict(n=0, share_before=None)))
    body = _body()
    body['wallets'].append(dict(uid='B2', handle='Zero', from_roster=True, coverage={}))
    page.evaluate('b=>{walletMap.draw(b.g);walletMap.drawUniverse(b.u)}', dict(g=graph, u=body))
    unknown = page.locator('[data-node="C"] circle.planet-body')
    zero = page.locator('[data-node="B2"] circle.planet-body')
    assert 'unknown' in unknown.get_attribute('class')
    assert unknown.get_attribute('fill') == 'none' and unknown.get_attribute('stroke-dasharray')
    assert 'unknown' not in zero.get_attribute('class') and zero.get_attribute('fill') != 'none'
    assert not zero.get_attribute('stroke-dasharray')
    assert float(zero.get_attribute('r')) == float(unknown.get_attribute('r')), "zero is the minimum size"
    legend = page.locator('.legend').inner_text()
    assert 'Not measured' in legend and 'Measured zero' in legend


def test_a_size_mode_the_chain_cannot_measure_is_disabled_with_its_reason(universe_page):
    page, _ = universe_page
    _draw(page)
    body = dict(_body('8453'), chains=[dict(id='solana', label='Solana'), dict(id='8453', label='Base')],
                graph_state='not_measured', wallets=_wallets(proven=('A',)))
    page.evaluate('b=>walletMap.drawUniverse(b)', body)
    following = page.locator('[aria-label="Planet size metric"] option[value="following"]')
    capital = page.locator('[aria-label="Planet size metric"] option[value="capital"]')
    assert following.evaluate('o=>o.disabled') and 'Solana' in following.get_attribute('title')
    assert capital.evaluate('o=>o.disabled') and 'capital' in capital.get_attribute('title').lower()
    body['wallets'] = _wallets(proven=('A', 'B'), capital=('A',))
    page.evaluate('b=>walletMap.drawUniverse(b)', body)
    assert not capital.evaluate('o=>o.disabled')
    assert page.locator('[aria-label="Planet size metric"]').input_value() == 'capital', \
        "an unmeasurable mode is not left selected when a measured one exists"


def test_the_header_states_what_the_map_is_and_is_not(universe_page):
    page, _ = universe_page
    _draw(page)
    header = page.locator('#wallet-universe header').inner_text()
    assert 'Repeat buyers and same-slot order from our sampled first buys. Not ownership, not profit.' in header
    assert 'who leads, who follows' not in header


def test_each_strip_item_stays_whole_and_the_strip_wraps_between_items(universe_page):
    """Gamma LOW on W5a: at 664 px the break landed inside 'Fresh capital: 92 of 97'."""
    page, _ = universe_page
    _draw(page)
    items = page.locator('.wu-availability > span')
    assert items.count() == 4
    assert {items.nth(i).evaluate('e=>getComputedStyle(e).whiteSpace') for i in range(4)} == {'nowrap'}
