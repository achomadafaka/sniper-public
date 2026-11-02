"""Liquidity change on feed tickets: dated, one basis, unknown stays unknown."""
import json
import sqlite3

from sniper.office import feed_liquidity as F

NOW = 1000.0


def ticket(liq=1000., pool='P'):
    return dict(candidate=dict(pool=pool, snapshot=dict(fields=dict(liq_usd=dict(value=liq, slot=5, known_at=900.), pool_state=dict(value=dict(quote_reserve_atomic='10000000000'))))))


def desk(tmp_path, rows=((b'', 0),), sol=100., age=1.):
    (tmp_path / 'pumpswap').mkdir(exist_ok=True)
    c = sqlite3.connect(tmp_path / 'pumpswap/tape.db')
    c.execute('CREATE TABLE swaps_ps(pool TEXT,post_quote_reserve_atomic TEXT,slot INTEGER,received_at REAL)')
    for row in rows:
        if row[0] != b'': c.execute('INSERT INTO swaps_ps VALUES(?,?,?,?)', row)
    c.commit(); c.close()
    (tmp_path / 'universe.heartbeat').write_text(json.dumps(dict(sol_usd=sol, sol_age_s=age, ts=NOW - 1)))
    return tmp_path


def run(d, t):
    items = [dict(ticket_id='t')]
    F.annotate(items, get_ticket=lambda i: t, desk_dir=d, now=NOW)
    return items[0]['liquidity_change']


def test_a_fall_past_the_threshold_is_material_with_both_sides_dated(tmp_path):
    out = run(desk(tmp_path, [('P', '4000000000', 9, 990.), ('P', '9000000000', 8, 950.), ('other', '1', 10, 995.)]), ticket())
    assert out['state'] == 'known' and out['material'] is True and out['current_liq_usd'] == 400. and out['change_pct'] == -60.
    assert (out['previous_slot'], out['current_slot'], out['previous_age_s'], out['current_age_s'], out['sol_age_s']) == (5, 9, 100., 10., 2.)


def test_a_smaller_fall_and_a_rise_are_not_material_and_a_later_row_is_not_read(tmp_path):
    assert run(desk(tmp_path, [('P', '6000000000', 9, 990.), ('P', '1', 11, 1001.)]), ticket())['material'] is False


def test_each_missing_side_is_unknown_never_no_change(tmp_path):
    d = desk(tmp_path)
    assert run(d, ticket())['reason'] == 'no_tape_row' and run(d, ticket())['material'] is None
    assert run(d, ticket(pool=None))['reason'] == 'no_pool'
    assert run(tmp_path / 'absent', ticket())['reason'] == 'tape_unreadable'
    e = desk(tmp_path / 'e' if (tmp_path / 'e').mkdir() is None else tmp_path, [('P', '6000000000', 9, 990.)])
    assert run(e, ticket(liq=None))['reason'] == 'no_previous_liquidity'
    (e / 'universe.heartbeat').unlink()
    got = run(e, ticket())
    assert got['reason'] == 'sol_unknown' and got['current_slot'] == 9 and got['material'] is None


def test_a_bad_ticket_stays_on_its_own_item(tmp_path):
    items = [dict(ticket_id='a'), dict(ticket_id='b')]
    def get(i):
        if i == 'a': raise sqlite3.OperationalError('x')
        return ticket()
    F.annotate(items, get_ticket=get, desk_dir=desk(tmp_path, [('P', '6000000000', 9, 990.)]), now=NOW)
    assert items[0]['liquidity_change']['reason'] == 'ticket_unreadable' and items[1]['liquidity_change']['state'] == 'known'


def test_report_puts_material_first_and_leaves_the_feed_list_unchanged(tmp_path):
    d = desk(tmp_path, [('A', '4000000000', 9, 990.), ('B', '9900000000', 9, 990.)])
    tickets = dict(a=ticket(pool='B'), b=ticket(pool='A'), c=ticket(pool='absent'))
    feed = dict(state='known', items=[dict(ticket_id=k, feed='pass') for k in tickets])
    out = F.report(feed, get_ticket=tickets.get, desk_dir=d, now=NOW)
    assert [r['ticket_id'] for r in out['items']] == ['b', 'c', 'a'] and (out['material'], out['unknown'], out['not_material']) == (1, 1, 1)
    assert 'material' not in out['items'][1] and all('liquidity_change' not in i for i in feed['items'])
    assert F.report(dict(state='unknown', reason='clock_unknown'), get_ticket=tickets.get, desk_dir=d, now=NOW) == dict(state='unknown', reason='clock_unknown', items=[])


def test_a_proven_zero_reserve_is_a_known_total_fall(tmp_path):
    out = run(desk(tmp_path, [('P', '0', 9, 990.)]), ticket())
    assert out['state'] == 'known' and out['material'] is True and out['change_pct'] == -100.


def test_a_newest_row_without_a_reserve_is_unknown_never_an_older_reserve(tmp_path):
    out = run(desk(tmp_path, [('P', '9000000000', 8, 950.), ('P', None, 9, 990.)]), ticket())
    assert out['state'] == 'unknown' and out['reason'] == 'newest_reserve_missing' and out['current_slot'] == 9 and out['material'] is None


def test_a_failed_feed_stays_unknown_even_with_an_empty_list():
    out = F.report(dict(state='unknown', reason='clock_unknown', items=[]), get_ticket=None, desk_dir='/x', now=NOW)
    assert out['state'] == 'unknown' and out['reason'] == 'clock_unknown'
