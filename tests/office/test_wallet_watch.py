"""Watch list, episodes and the real worker path; real sqlite, no fake seams."""
import importlib.util
import json
import sqlite3
import time
from pathlib import Path

import pytest

from sniper.office import seat_tools as T, wallet_network as N, wallet_watch as WW
from sniper.services import memecoin_desk_pumpswap as P

A, B = '1' * 40, '2' * 40
_s = importlib.util.spec_from_file_location('uw_watch', Path(__file__).resolve().parents[2] / 'scripts/memecoin_desk_universe_worker.py')
W = importlib.util.module_from_spec(_s); _s.loader.exec_module(W)


@pytest.fixture
def net(tmp_path):
    (tmp_path / 'pumpswap').mkdir()
    return N.WalletNetwork(tmp_path / 'n.db', clock=lambda: 100.)


def test_a_listing_needs_evidence_writes_the_file_and_removal_updates_it(net, tmp_path):
    assert WW.add(net, tmp_path, address=A, operator_id='op', evidence=' ', source='seat', actor='s', now=1.)['reason'] == 'bad_arguments'
    assert WW.add(net, tmp_path, address=A, operator_id='op', evidence='drained X', source='seat', actor='s', now=1.)['state'] == 'added'
    assert json.loads(WW.watch_file(tmp_path).read_text())['wallets'] == [A] and WW.read_list(net)[0]['operator_id'] == 'op'
    WW.remove(net, tmp_path, address=A, now=2.)
    assert json.loads(WW.watch_file(tmp_path).read_text())['wallets'] == [] and WW.read_list(net) == []


def test_the_list_is_capped(net, tmp_path, monkeypatch):
    monkeypatch.setattr(WW, 'MAX_LISTED', 1)
    WW.add(net, tmp_path, address=A, operator_id=None, evidence='e', source='seat', actor='s', now=1.)
    assert WW.add(net, tmp_path, address=B, operator_id=None, evidence='e', source='seat', actor='s', now=1.)['reason'] == 'watch_list_full'


def test_an_accumulate_then_dump_is_one_closed_episode_and_a_rebuy_is_another():
    rows = [(1., 'P', 'M', 1, '100', '10', '1000'), (2., 'P', 'M', 1, '100', '30', '1000'), (5., 'P', 'M', 0, '200', '90', '1000'), (9., 'P', 'M', 1, '10', '9', '1000')]
    first, second = sorted(WW.episodes_from_rows(rows), key=lambda e: e['first_buy_at'])
    assert (first['state'], first['buys'], first['sells'], first['first_sell_at'], first['last_buy_at']) == ('closed', 2, 1, 5., 2.) and first['bought_share_of_pool_pct'] == 1.
    assert second['state'] == 'open' and second['first_buy_at'] == 9.


def test_a_sell_with_no_seen_buy_is_entry_unseen_never_a_profit_read():
    e = WW.episodes_from_rows([(5., 'P', 'M', 0, '200', '90', '1000')])[0]
    assert e['state'] == 'entry_unseen' and e['first_buy_at'] is None


def test_episodes_round_trip_through_the_store(net):
    assert WW.store_episodes(net, {A: [(5., 'P', 'M', 0, '200', '90', '1000')]}, 7.) == 1
    assert WW.read_episodes(net, address=A)['items'][0]['state'] == 'entry_unseen' and WW.read_episodes(net, address=B)['items'] == []


def worker(path, monkeypatch):
    monkeypatch.setenv('MEMECOIN_DESK_PUMPSWAP_CAPTURE', '1')
    monkeypatch.setenv('MEMECOIN_DESK_PUMPSWAP_FILTER', '1')
    return W.Worker(W.knobs({}), base=path, sol_price_fn=lambda: None, alert_fn=lambda *a: None)


def event(actor):
    return dict(kind='buy', pool='pool', actor=actor, event_time=1, payload={}, payload_b64='raw', base_atomic='100', quote_atomic='50', pre_base_reserve_atomic='1000', pre_quote_reserve_atomic='1000',
                post_base_reserve_atomic='900', post_quote_reserve_atomic='1050', lp_fee_bps=20, protocol_fee_bps=5, creator_fee_bps=0, cashback_fee_bps=None, buyback_fee_bps=None, coin_creator='c')


def test_the_worker_record_method_stores_a_listed_wallets_swap(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    (tmp_path / 'pumpswap').mkdir(exist_ok=True)
    WW.watch_file(tmp_path).write_text(json.dumps(dict(version=1, wallets=[A])))
    now = time.time()
    w.watch_tick(now)
    assert w.ps_watch == {A}
    c = w.files.conn('swaps_ps', now)
    w.watch_record(c, event(A), None, 'm', 'q', 10, 'sig', 0, 1, now)
    c.commit()
    assert w.stats['ps_watch_events'] == 1
    tape = tmp_path / 'pumpswap' / 'tape.db'
    assert sqlite3.connect(tape).execute('SELECT actor,quote_atomic FROM ps_watch_events').fetchall() == [(A, '50')]
    got = WW.events(tmp_path, now=now + 1)
    assert got['state'] == 'known' and got['items'][0]['size_pct_of_pool_quote'] == 5.


def test_a_bad_list_keeps_the_old_one_and_a_missing_list_is_empty(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    (tmp_path / 'pumpswap').mkdir(exist_ok=True)
    f = WW.watch_file(tmp_path)
    f.write_text(json.dumps(dict(wallets=[A]))); w.watch_tick(time.time())
    f.write_text('broken'); w.watch_tick(time.time())
    assert w.ps_watch == {A} and w.stats['ps_watch_list_errors'] == 1
    f.unlink(); w.watch_tick(time.time())
    assert w.ps_watch == frozenset()


def test_events_without_a_table_are_unknown(tmp_path):
    (tmp_path / 'pumpswap').mkdir()
    sqlite3.connect(tmp_path / 'pumpswap/tape.db').close()
    assert WW.events(tmp_path, now=1.)['reason'] == 'no_watch_table'
    assert WW.events(tmp_path / 'absent', now=1.)['reason'] == 'tape_unreadable'


def test_forensics_tools_need_the_network_and_nothing_here_names_an_arming_surface():
    assert {'watch_add', 'watch_remove', 'wallet_episodes'} <= T.NETWORK_TOOLS
    code = Path(WW.__file__).read_text().split('SCHEMA =', 1)[1]
    for word in ('memecoin_wallets', 'real_approved', 'sniper_tracker', 'sniper.execution'):
        assert word not in code


def test_a_real_process_call_records_a_listed_wallet_on_an_unstored_pool_and_not_others(tmp_path, monkeypatch):
    from test_memecoin_desk_pumpswap_worker import frame
    w = worker(tmp_path, monkeypatch)
    (tmp_path / 'pumpswap').mkdir(exist_ok=True)
    WW.watch_file(tmp_path).write_text(json.dumps(dict(wallets=[A])))
    w.watch_tick(time.time())
    monkeypatch.setattr(P, 'events', lambda _: [(1, event(A)), (2, event(B))])
    w.process(frame(), received_at=time.time())
    w.commit_files()
    tape = sqlite3.connect(tmp_path / 'pumpswap/tape.db')
    assert tape.execute('SELECT actor FROM ps_watch_events').fetchall() == [(A,)]
    assert tape.execute('SELECT count(*) FROM swaps_ps').fetchone()[0] == 0          # the filter still holds the pool's rows back: the watch does not widen the tape


def test_a_sell_with_unseen_entry_then_a_buy_keeps_the_old_episode_unseen():
    rows = [(5., 'P', 'M', 0, '200', '90', '1000'), (9., 'P', 'M', 1, '10', '9', '1000')]
    states = sorted(e['state'] for e in WW.episodes_from_rows(rows))
    assert states == ['entry_unseen', 'open']


def test_scan_includes_unstored_watch_rows_and_dedupes_by_identity_only():
    c = sqlite3.connect(':memory:')
    cols = '(actor,received_at,pool,mint,is_buy,base_atomic,quote_atomic,pre_quote_reserve_atomic,signature,ordinal)'
    c.execute('CREATE TABLE swaps_ps' + cols); c.execute('CREATE TABLE ps_watch_events' + cols)
    same = (A, 1., 'P', 'M', 1, '1', '1', '9')
    c.execute('INSERT INTO swaps_ps VALUES(?,?,?,?,?,?,?,?,?,?)', (*same, 's1', 0))
    c.execute('INSERT INTO swaps_ps VALUES(?,?,?,?,?,?,?,?,?,?)', (*same, 's2', 0))
    c.execute('INSERT INTO ps_watch_events VALUES(?,?,?,?,?,?,?,?,?,?)', (*same, 's1', 0))
    c.execute('INSERT INTO ps_watch_events VALUES(?,?,?,?,?,?,?,?,?,?)', (A, 2., 'U', 'N', 1, '1', '1', '9', 's3', 0))
    rows = WW.scan(c, [A])[A]
    assert len(rows) == 4 and {(r[7], r[8]) for r in WW.unique_swaps(rows)} == {('s1', 0), ('s2', 0), ('s3', 0)}


def test_the_network_stores_disk_cap_covers_the_watch_and_episode_writers(tmp_path):
    (tmp_path / 'pumpswap').mkdir()
    n = N.WalletNetwork(tmp_path / 'n.db', clock=lambda: 100., max_bytes=1)
    assert WW.add(n, tmp_path, address=A, operator_id=None, evidence='e', source='seat', actor='s', now=1.)['reason'] == 'disk_cap'
    with pytest.raises(ValueError, match='disk_cap'):
        WW.store_episodes(n, {A: []}, 7.)


C, D, HUB = '3' * 40, '4' * 40, '5' * 40


def _funded(c, src, dst, proof='claimed'):
    c.execute("INSERT INTO edges(network_id,src,dst,type,source,known_at,proof_state,method_version,proof_ref) VALUES('solana',?,?,'funded_by','seat',1.,?,'m',?)", (src, dst, proof, src + dst))


def test_funding_walks_real_funders_and_excludes_hubs_and_known_exchanges(net, monkeypatch):
    c = net._conn()
    _funded(c, A, B, 'verified'); _funded(c, C, B); _funded(c, B, D)
    _funded(c, A, HUB)
    for i in range(3): _funded(c, '6' * 39 + '%d' % (i + 1), HUB)
    c.commit(); c.close()
    monkeypatch.setattr(WW, 'FANOUT_HUB', 4)
    monkeypatch.setattr(WW, '_known_infrastructure', lambda: frozenset({D}))
    got = WW.funding_links(net, address=A, hops=3)
    assert [e['dst'] for e in got['funding_chain']] == [B]
    assert {e['dst']: e['excluded'] for e in got['excluded_funders']} == {HUB: 'high_fanout_funder', D: 'known_infrastructure'}
    assert got['siblings'] == [dict(wallet=C, shared_funder=B, proof_state='claimed')] and got['funding_chain'][0]['proof_state'] == 'verified'


def test_the_real_known_funder_list_loads_and_an_unknown_list_is_partial(net, monkeypatch):
    net._conn().close()
    assert WW._known_infrastructure()
    monkeypatch.setattr(WW, '_known_infrastructure', lambda: None)
    assert WW.funding_links(net, address=A)['state'] == 'partial'
    assert WW.funding_links(net, address='bad')['state'] == 'refused' and WW.funding_links(net, address=A, hops=4)['state'] == 'refused'


def test_shortened_sibling_and_edge_lists_say_so(net):
    c = net._conn()
    _funded(c, A, B)
    for i in range(6): _funded(c, '7' * 39 + str(i + 1), B)
    for i in range(11): _funded(c, C, '8' * 38 + '%02d' % (i + 11))
    c.commit(); c.close()
    assert WW.funding_links(net, address=A, limit=5)['siblings_truncated'] is True and WW.funding_links(net, address=A, limit=6)['siblings_truncated'] is False
    got = WW.funding_links(net, address=C, hops=1)
    assert got['funding_edges_truncated'] is True and len(got['funding_chain']) == 10
