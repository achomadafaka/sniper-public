import sqlite3

from sniper.office import wallet_network as N, wallet_watch as W

A, B, C, D = ('1' * 40, '2' * 40, '3' * 40, '4' * 40)


def graph(tmp_path):
    n = N.WalletNetwork(tmp_path / 'network.db', clock=lambda: 100.)
    c = n._conn()
    for src, dst, known, observed, proof in ((A, B, 20, 10, 'verified'), (C, B, 40, 10, 'verified'), (D, B, 15, 10, 'claimed')):
        c.execute('INSERT INTO edges(network_id,src,dst,type,source,known_at,observed_at,proof_state,method_version) VALUES(?,?,?,?,?,?,?,?,?)',
                  ('solana', src, dst, 'funded_by', 'host', known, observed, proof, 'test'))
    c.commit(); c.close()
    return n


def test_family_uses_discovery_time_not_backdated_chain_time(tmp_path, monkeypatch):
    n = graph(tmp_path)
    monkeypatch.setattr(W, '_known_infrastructure', lambda: frozenset())
    before = W.funding_links(n, address=A, as_of=30., verified_only=True)
    after = W.funding_links(n, address=A, as_of=50., verified_only=True)
    assert before['siblings'] == []
    assert {r['wallet'] for r in after['siblings']} == {C}
    assert before['funding_chain'][0]['known_at'] == 20
    assert before['as_of'] == 30.


def test_future_fanout_does_not_hide_a_prior_funder(tmp_path, monkeypatch):
    n = graph(tmp_path)
    monkeypatch.setattr(W, '_known_infrastructure', lambda: frozenset())
    monkeypatch.setattr(W, 'FANOUT_HUB', 2)
    assert W.funding_links(n, address=A, as_of=30., verified_only=True)['funding_chain']
    assert W.funding_links(n, address=A, as_of=50., verified_only=True)['excluded_funders'][0]['excluded'] == 'high_fanout_funder'


def test_family_does_not_create_an_absent_store(tmp_path):
    n = N.WalletNetwork(tmp_path / 'absent.db', clock=lambda: 100.)
    assert W.funding_links(n, address=A, as_of=30., verified_only=True)['state'] == 'unknown'
    assert not n.path.exists()


def test_future_observation_and_invalid_cutoff_are_refused(tmp_path, monkeypatch):
    n = graph(tmp_path)
    monkeypatch.setattr(W, '_known_infrastructure', lambda: frozenset())
    with sqlite3.connect(n.path) as c:
        c.execute('UPDATE edges SET observed_at=90 WHERE src=?', (A,))
    assert not W.funding_links(n, address=A, as_of=30., verified_only=True)['funding_chain']
    for at in (float('nan'), float('inf'), True, -1, 101):
        assert W.funding_links(n, address=A, as_of=at, verified_only=True)['state'] == 'refused'


def test_network_cutoff_filters_future_edges_labels_and_coverage(tmp_path):
    n = graph(tmp_path)
    with sqlite3.connect(n.path) as c:
        c.execute('INSERT INTO labels VALUES(?,?,?,?,?,?)', ('solana', A, 'later-label', 'host', 'test', 40))
        c.execute('INSERT INTO coverage VALUES(?,?,?,?,?,?,?)', ('solana', A, 'funder', 'found', None, 40, None))
    out = n.read(seed=A, hops=2, as_of=30., verified_only=True)
    assert {(r['src'], r['dst']) for r in out['edges']} == {(A, B)}
    assert out['coverage'] == []
    assert next(r for r in out['nodes'] if r['address'] == A)['labels'] == []
    assert out['as_of'] == 30.
    overview = n.read(as_of=30., verified_only=True)
    assert {(r['src'], r['dst']) for r in overview['edges']} == {(A, B)}


def test_network_cutoff_validation(tmp_path):
    n = graph(tmp_path)
    for at in (float('nan'), float('inf'), True, -1, 101):
        assert n.read(seed=A, as_of=at)['state'] == 'refused'
