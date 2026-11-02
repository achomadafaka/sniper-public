"""The wallet network: proof, unknowns, caps, expiry, and that nothing here can arm a wallet."""
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from sniper.office import wallet_network as N

A, B, C = '1' * 40, '2' * 40, '3' * 40
AT = 1793450000.0


class Chain:
    def __init__(self, db, out): self.db, self.out, self.calls = db, out, []
    def _conn(self): return sqlite3.connect(self.db)
    def funding_trace(self, wallet, hops=1):
        self.calls.append(wallet)
        c = self._conn(); c.execute('INSERT INTO calls VALUES(?,?)', (AT, 100)); c.commit(); c.close()
        return self.out
    token_holders = lambda self, mint: self.out


@pytest.fixture
def chain_db(tmp_path):
    p = tmp_path / 'chain.db'; c = sqlite3.connect(p)
    c.execute('CREATE TABLE fetches(fetch_id TEXT,state TEXT,result TEXT)'); c.execute('CREATE TABLE calls(at REAL,credits_charged INTEGER)')
    c.execute('INSERT INTO fetches VALUES(?,?,?)', ('f1', 'partial', json.dumps([A, B])))
    c.execute('INSERT INTO fetches VALUES(?,?,?)', ('f2', 'unknown', json.dumps([A, B])))
    c.commit(); c.close()
    return p


def net(tmp_path, chain_db, **kw):
    return N.WalletNetwork(tmp_path / 'n.db', chain_db=chain_db, clock=lambda: AT, **kw)


def test_verified_needs_a_stored_answered_fetch_naming_both_addresses(tmp_path, chain_db):
    n = net(tmp_path, chain_db)
    assert n.add_edge(src=A, dst=B, edge_type='funded_by', actor='s', fetch_id='f1')['proof_state'] == 'verified'
    for fetch, src, dst in (('f2', A, B), ('nope', A, B), ('f1', A, C)):
        assert n.add_edge(src=src, dst=dst, edge_type='follows', actor='s', fetch_id=fetch)['proof_state'] == 'claimed'
    assert n.add_edge(src=A, dst=C, edge_type='co_buy_slot', actor='s')['proof_state'] == 'none'


def test_bad_inputs_are_refused_and_a_repeat_is_duplicate(tmp_path, chain_db):
    n = net(tmp_path, chain_db)
    for kw in (dict(src='x', dst=B, edge_type='follows'), dict(src=A, dst=A, edge_type='follows'), dict(src=A, dst=B, edge_type='owns'), dict(src=A, dst=B, edge_type='follows', observed_at=AT + 1)):
        assert n.add_edge(actor='s', **kw)['reason'] == 'bad_arguments'
    assert n.add_edge(src=A, dst=B, edge_type='follows', actor='s', note_ref='r')['state'] == 'added'
    assert n.add_edge(src=A, dst=B, edge_type='follows', actor='s', note_ref='r')['state'] == 'duplicate'
    assert n.add_edge(src=A, dst=C, edge_type='follows', actor='s')['state'] == 'added' and n.add_edge(src=A, dst=C, edge_type='follows', actor='s')['state'] == 'duplicate'   # no proof ref still dedupes


def test_a_missing_time_stays_null_and_read_walks_both_directions_and_two_hops(tmp_path, chain_db):
    n = net(tmp_path, chain_db)
    n.add_edge(src=A, dst=B, edge_type='follows', actor='s', note_ref='a'); n.add_edge(src=C, dst=B, edge_type='follows', actor='s', note_ref='b')
    one, two = n.read(seed=A, hops=1), n.read(seed=A, hops=2)
    assert one['edges'][0]['observed_at'] is None and one['edges'][0]['source'] == 'seat' and len(one['edges']) == 1 and len(two['edges']) == 2 and {x['address'] for x in two['nodes']} == {A, B, C}
    assert n.read(seed=A, limit=1, hops=2)['truncated'] is True


def test_an_absent_store_is_unknown_not_empty(tmp_path, chain_db):
    assert net(tmp_path, chain_db).read(seed=A)['state'] == 'unknown'


def test_caps_refuse_typed(tmp_path, chain_db, monkeypatch):
    n = net(tmp_path, chain_db, max_bytes=1)
    n._conn().close()
    assert n.add_edge(src=A, dst=B, edge_type='follows', actor='s')['reason'] == 'disk_cap'
    m = N.WalletNetwork(tmp_path / 'm.db', clock=lambda: AT)
    monkeypatch.setattr(N, 'MAX_ROWS_PER_DAY', 1)
    assert m.add_edge(src=A, dst=B, edge_type='follows', actor='s', note_ref='1')['state'] == 'added'
    assert m.add_edge(src=A, dst=C, edge_type='follows', actor='s', note_ref='2')['reason'] == 'daily_row_cap'
    assert N.CREDITS_PER_DAY is None and N.MAX_BYTES == float('inf')


def test_expand_records_a_funder_with_proof_and_an_unknown_as_coverage_then_stops_at_the_credit_cap(tmp_path, chain_db, monkeypatch):
    out = dict(state='partial', reason='trace_stopped', fetch_id='f1', steps=[dict(wallet=A, state='known', funder=B, block_time_s=5), dict(wallet=B, state='unknown', reason='no_history')])
    chain = Chain(chain_db, out)
    n = net(tmp_path, chain_db, chain=chain)
    got = n.expand(seed=A, hops=2, actor='s')
    assert got['edges_added'] == 1 and got['credits_estimated'] == 100
    r = n.read(seed=A)
    assert r['edges'][0]['proof_state'] == 'verified' and r['edges'][0]['observed_at'] == 5 and r['edges'][0]['source'] == 'host'
    assert {(c['address'], c['state']) for c in n.read(seed=A, hops=2)['coverage']} == {(A, 'found'), (B, 'unknown')}
    monkeypatch.setattr(N, 'CREDITS_PER_DAY', 100)
    assert n.expand(seed=A, actor='s')['reason'] == 'credit_cap' and len(chain.calls) == 1       # refused before the call


def test_an_address_inside_a_longer_string_proves_nothing(tmp_path, chain_db):
    c = sqlite3.connect(chain_db); c.execute('INSERT INTO fetches VALUES(?,?,?)', ('f3', 'known', json.dumps([A + 'x', B + 'x']))); c.commit(); c.close()
    assert net(tmp_path, chain_db).add_edge(src=A, dst=B, edge_type='follows', actor='s', fetch_id='f3')['proof_state'] == 'claimed'


def test_expand_counts_real_rows_and_real_credits_after_its_reservation(tmp_path, chain_db):
    out = dict(state='partial', fetch_id='f1', steps=[dict(wallet=A, state='known', funder=B, block_time_s=5)])
    n = net(tmp_path, chain_db, chain=Chain(chain_db, out))
    n.expand(seed=A, actor='s')
    c = n._conn()
    assert tuple(c.execute('SELECT rows_added,credits FROM daily').fetchone()) == (2, 100)     # one edge, one coverage row, one call


def test_explicit_expiry_drops_old_claimed_rows_first(tmp_path, chain_db, monkeypatch):
    monkeypatch.setattr(N,'CLAIMED_DAYS',90)
    monkeypatch.setattr(N,'OTHER_DAYS',365)
    clock = [AT]
    n = N.WalletNetwork(tmp_path / 'n.db', chain_db=chain_db, clock=lambda: clock[0])
    n.add_edge(src=A, dst=B, edge_type='follows', actor='s', note_ref='old'); n.add_edge(src=A, dst=B, edge_type='funded_by', actor='s', fetch_id='f1')
    c = n._conn(); n.prune(c, AT + 91 * 86400)
    assert [r[0] for r in c.execute('SELECT proof_state FROM edges')] == ['verified']
    n.prune(c, AT + 366 * 86400)
    assert c.execute('SELECT count(*) FROM edges').fetchone()[0] == 0


def test_seed_is_read_only_labelled_and_idempotent(tmp_path):
    import scripts.office_wallet_network_seed as Seed
    (tmp_path / 'data/desk').mkdir(parents=True); (tmp_path / 'data/office').mkdir()
    g = sqlite3.connect(tmp_path / 'data/memecoin_wallet_graph.db')
    g.execute('CREATE TABLE memecoin_wallet_edges(network_id,edge_type,src,dst,first_seen,method_version)'); g.execute('CREATE TABLE memecoin_wallet_funders(wallet,network_id,funder,funding_sig,funded_at)')
    g.execute("INSERT INTO memecoin_wallet_edges VALUES('solana','follows',?,?,'2026-10-01T00:00:00+00:00','v1')", (A, B)); g.execute("INSERT INTO memecoin_wallet_funders VALUES(?,'solana',NULL,NULL,NULL)", (C,)); g.commit(); g.close()
    c = sqlite3.connect(tmp_path / 'data/desk/cluster.db'); c.execute('CREATE TABLE cluster_funder_cache(owner,funder,source_signature,resolved_at)'); c.execute("INSERT INTO cluster_funder_cache VALUES(?,?,NULL,'2026-10-01T00:00:00+00:00')", (B, C)); c.commit(); c.close()
    o = sqlite3.connect(tmp_path / 'data/office/outcomes.db'); o.execute('CREATE TABLE mint_creators(network_id,mint,creator,source,source_at)'); o.execute("INSERT INTO mint_creators VALUES('solana',?,?,'launches',5.)", (A, C)); o.commit(); o.close()
    n = N.WalletNetwork(tmp_path / 'data/office/wallet_network.db', clock=lambda: AT)
    assert Seed.seed(tmp_path, n) == dict(state='done', added=3, malformed=0)
    assert Seed.seed(tmp_path, n)['added'] == 0
    rows = n.read(seed=A, hops=1)['edges']
    assert all(r['source'] == 'import' and r['proof_state'] == 'none' for r in rows) and any(r['stale'] == 1 for r in rows)


def test_only_a_forensics_host_exposes_the_tools():
    from sniper.office import seat_tools as T
    source = Path(T.__file__).read_text()
    assert 'names-NETWORK_TOOLS' in source and T.NETWORK_TOOLS <= set(T.TOOLS)
    assert "self.cfg.get('role')=='forensics'" in Path('sniper/office/seat_host.py').read_text()


def test_nothing_that_can_arm_or_fund_can_reach_the_network():
    """Fails if a funded, arming or Wallet Map path names the store, or the module names an arming surface."""
    got = subprocess.run(['git', 'grep', '-lE', 'wallet_network|WalletNetwork', '--', 'sniper', 'scripts'], capture_output=True, text=True).stdout.split()
    # Research collectors read this graph only; quotes use the isolated capture client and cannot arm or fund.
    allowed = {'sniper/office/wallet_network.py', 'sniper/office/seat_tools.py', 'sniper/office/seat_host.py', 'scripts/office_seat_host.py', 'scripts/office_wallet_network_seed.py', 'scripts/office_wallet_episodes.py', 'sniper/office/wallet_watch.py', 'scripts/office_wallet_watch_collect.py', 'sniper/office/wallet_watch_collect.py', 'sniper/office/wallet_family.py', 'scripts/research/dex_boost_collector.py'}
    allowed |= {'sniper/office/wallet_campaign.py', 'sniper/office/wallet_campaign_report.py','sniper/office/wallet_group_leads.py','scripts/office_wallet_group_leads.py'}
    assert set(got) - {p for p in got if p.startswith('sniper/routes/api_office') or p.startswith('sniper/office/operator_view')} <= allowed, got
    source = Path(N.__file__).read_text()
    code = source.split('EDGE_TYPES =', 1)[1]
    for word in ('memecoin_wallets', 'real_approved', 'real_chains', 'sniper_tracker', 'memecoin_trial', 'sniper.execution', 'sniper.services'):
        assert word not in code, word


def test_holder_rows_are_clamped_to_the_reserved_bound(tmp_path, chain_db):
    from sniper.office import chain_data as CD
    items = [dict(owner='4' * 37 + chr(65 + i % 26) + chr(65 + i // 26) + 'B') for i in range(CD.MAX_ROWS + 30)]
    n = net(tmp_path, chain_db, chain=Chain(chain_db, dict(state='partial', fetch_id='f1', items=items)))
    n.expand(seed=A, kind='mint', actor='s')
    c = n._conn()
    assert c.execute('SELECT rows_added FROM daily').fetchone()[0] <= N.EXPAND_MAX_ROWS and c.execute('SELECT count(*) FROM edges').fetchone()[0] == CD.MAX_ROWS


def test_a_trace_cannot_read_past_its_limit_when_the_provider_ignores_it():
    from sniper.office import chain_data as CD
    calls = []
    d = CD.ChainData.__new__(CD.ChainData)
    d.monotonic = lambda: 0.0
    def paced(method, params, deadline, dials, **kw):
        calls.append(method)
        return (dict(data=[dict(signature='5' * 88) for _ in range(40)]), False, 1.) if method == 'getTransactionsForAddress' else (None, False, 1.)
    d._paced_call = paced
    d._funder(A, 1., [])
    assert calls.count('getTransaction') <= CD.TRACE_TXS


def test_the_room_route_reads_and_refuses_bad_seeds(tmp_path):
    from flask import Flask
    from sniper.routes.api_office import create_api_office_blueprint
    n = N.WalletNetwork(tmp_path / 'wallet_network.db', clock=lambda: AT)      # state_root in the app is already data/office
    n.add_edge(src=A, dst=B, edge_type='follows', actor='s', note_ref='r')
    app = Flask(__name__); app.register_blueprint(create_api_office_blueprint(dict(state_root=tmp_path, clock=lambda: AT)))
    c = app.test_client()
    got = c.get('/api/office/network?seed=' + A).get_json()
    assert got['state'] == 'known' and got['edges'][0]['proof_state'] == 'claimed'
    assert c.get('/api/office/network').get_json()['state'] == 'known'
    assert c.get('/api/office/network?seed=bad').status_code == 400 and c.get('/api/office/network?seed=' + A + '&hops=x').status_code == 400
    assert c.post('/api/office/network?seed=' + A).status_code == 405


def test_the_forensics_prompt_tells_the_seat_to_map_proactively():
    text = Path('config/office/prompts/office-v2/forensics.md').read_text()
    assert 'creators, related wallets and snipers' in text and 'network_expand' in text


def test_no_seed_gives_a_bounded_overview_with_seat_edges_before_import(tmp_path, chain_db):
    n = net(tmp_path, chain_db)
    n.add_edge(src=A, dst=B, edge_type='follows', actor='s', note_ref='a')
    c = n._conn()
    for i in range(3):
        n._insert(c, src=C, dst='4' * 39 + str(i), edge_type='holds', source='import', actor=None, observed_at=None, known_at=AT, proof_kind=None, proof_ref=None, proof_state='none', method='m')
    c.commit(); c.close()
    got = n.read(limit=2)
    assert got['truncated'] is True and got['state'] == 'partial' and len(got['edges']) == 2 and got['edges'][0]['source'] == 'seat' and A in {x['address'] for x in got['nodes']}
    assert n.read(limit=10)['truncated'] is False and n.read(limit=10, types=['follows'])['edges'][0]['type'] == 'follows' and len(n.read(limit=10, types=['follows'])['edges']) == 1
    assert n.read(limit=501)['state'] == 'refused' and n.read(seed='bad')['state'] == 'refused'


def test_the_overview_uses_its_index_and_a_read_past_its_budget_is_typed(tmp_path, chain_db, monkeypatch):
    n = net(tmp_path, chain_db)
    n.add_edge(src=A, dst=B, edge_type='follows', actor='s', note_ref='a')
    c = n._conn()
    plan = ' '.join(str(r[3]) for r in c.execute("EXPLAIN QUERY PLAN SELECT * FROM edges WHERE network_id='solana' ORDER BY source='import',edge_id DESC LIMIT 5"))
    c.close()
    assert 'edges_overview' in plan and 'TEMP B-TREE' not in plan
    monkeypatch.setattr(N, 'READ_MAX_VM_OPS', 0); monkeypatch.setattr(N, 'READ_STEP', 1)
    got = n.read()
    assert got['state'] == 'unknown' and got['reason'] == 'store_read_budget'
