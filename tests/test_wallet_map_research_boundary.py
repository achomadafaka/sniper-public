"""Legacy performance must not enter live controls or research coverage."""
import json
import subprocess

from test_wallet_map_actions import client, _write_funnel


def test_live_roster_ignores_cached_rankings_and_archive_requires_opt_in(client):
    from sniper.persistence import db_memecoin as m
    cl, conn, _, path = client
    _write_funnel(path)
    m.add_wallet(conn, 'current', 'Current name', '')
    body = cl.get('/api/memecoin/wallet-map/funnel').get_json()
    assert {r['uid'] for r in body['funnel']} == {'current'}
    assert body['stamp']['mode'] == 'live_authority'
    assert body['solana_candidates'] == [] and body['evm_candidates'] == {} and body['rotation'] == []
    assert all('paper_ret' not in r and 'funded_ret' not in r for r in body['funnel'])
    universe = cl.get('/api/memecoin/wallet-map/universe?chain=solana&diagnostic_only=1').get_json()
    assert {r['uid'] for r in universe['wallets']} == {'current'}
    assert universe['stamp']['mode'] == 'live_authority'
    archive = cl.get('/api/memecoin/wallet-map/funnel?diagnostic_only=1').get_json()
    assert archive['research_eligibility']['eligible'] is False
    assert archive['research_eligibility']['codes'] == ['protocol_missing_or_legacy']
    assert archive['funnel'][0]['paper_ret'] == .05
    assert 'authority_state' not in archive['stamp'] and 'clips' not in archive
    (path / 'data/wallet_follow_funnel.json').write_text('{broken')
    assert cl.get('/api/memecoin/wallet-map/funnel').get_json()['stamp']['authority_state'] == 'ok'
    assert cl.get('/api/memecoin/wallet-map/funnel?diagnostic_only=1').get_json()['stamp']['error'] == 'unreadable'


def _render(function, body, chain='solana'):
    js = 'const C=require("./static/wallet_map.js"); process.stdout.write(C.' + function + '(' + json.dumps(body) + ',' + json.dumps(chain) + '));'
    return subprocess.run(['node', '-e', js], check=True, capture_output=True, text=True).stdout


def test_coverage_is_chain_scoped_and_unknown_is_not_zero_or_strategy_support():
    body = {'state': 'stale', 'generated_at': '2026-10-05T21:00:00+00:00', 'cells': [
        {'wallet': 'w', 'handle': '<script>Wallet</script>', 'chain': 'solana', 'signals': 50,
         'complete_episodes': 0, 'missing': ['source proof'], 'strategy': 'forged winner'},
        {'wallet': 'w', 'handle': 'Other chain', 'chain': '56', 'signals': 900, 'complete_episodes': 900}]}
    html = _render('researchCoverageHTML', body)
    assert 'stale' in html.lower() and 'No strategy supported' in html
    assert '>50<' in html and '>0<' in html and 'unknown' in html
    assert 'Other chain' not in html and '900' not in html and 'forged winner' not in html
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert '<button' not in html and '/?tab=wallet-map' in html
    assert 'Collection unavailable' in _render('researchCoverageHTML', {'state': 'unavailable', 'cells': []})


def test_forensic_returns_have_explicit_denominators_and_no_money_actions():
    body = {'stamp': {'generated_at': '2026-10-04T15:42:08', 'age_s': 100000}, 'funnel': [
        {'uid': 'w', 'handle': '<b>Wallet</b>', 'paper_ret': .07, 'paper_n': 76,
         'gated_ret': .176, 'gated_n': 37, 'gated_cens_share': 7/37, 'funded_ret': -.236}]}
    html = _render('legacyDiagnosticsHTML', body)
    assert '37' in html and 'includes unpriced' in html and 'priced-only' in html
    assert 'notional' in html and '19%' in html
    assert 'priced count not recorded' in html.lower()
    assert '-23.6%' not in html and '<button' not in html and 'data-act' not in html
    assert '<b>Wallet</b>' not in html and '&lt;b&gt;Wallet&lt;/b&gt;' in html
