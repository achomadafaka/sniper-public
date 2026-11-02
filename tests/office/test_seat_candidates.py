"""Candidate lists on a real sqlite tape through the real query; no fakes."""
import json
import sqlite3

from sniper.office import seat_candidates as C

NOW = 100000.0
WSOL = 'So11111111111111111111111111111111111111112'


def desk(tmp_path, rows, sol=100.):
    (tmp_path / 'pumpswap').mkdir(exist_ok=True)
    c = sqlite3.connect(tmp_path / 'pumpswap/tape.db')
    c.execute('CREATE TABLE swaps_ps(pool TEXT,mint TEXT,quote_mint TEXT,received_at REAL,slot INTEGER,is_buy INTEGER,actor TEXT,post_quote_reserve_atomic TEXT,tx_index INTEGER DEFAULT 1,ordinal INTEGER DEFAULT 0)')
    c.execute('CREATE INDEX swaps_ps_received ON swaps_ps(received_at)')
    c.executemany('INSERT INTO swaps_ps(pool,mint,quote_mint,received_at,slot,is_buy,actor,post_quote_reserve_atomic) VALUES(?,?,?,?,?,?,?,?)', rows)
    c.commit(); c.close()
    (tmp_path / 'universe.heartbeat').write_text(json.dumps(dict(sol_usd=sol, sol_age_s=1., ts=NOW - 1)))
    return tmp_path


def row(pool, at, quote='1000000000', buy=1, actor='a', mint=None, qm=WSOL):
    return (pool, mint or 'M' + pool, qm, at, 5, buy, actor, quote)


def test_liquidity_uses_the_newest_row_and_a_fallen_pool_leaves_the_list(tmp_path):
    d = desk(tmp_path, [row('up', NOW - 10, '90000000000'), ('fell', 'Mf', WSOL, NOW - 10, 4, 1, 'a', '90000000000'), ('fell', 'Mf', WSOL, NOW - 20, 6, 0, 'a', '1000000000'), row('none', NOW - 10, None), row('later', NOW + 1, '90000000000'), row('usdc', NOW - 5, '90000000000', qm='U')])
    out = C.build(d, NOW)
    assert [i['pool'] for i in out['graduated_liquid']] == ['up'] and out['graduated_liquid'][0]['liq_usd'] == 9000. and out['newest_reserve_missing'] == 1


def test_buyer_growth_counts_distinct_buyers_in_both_windows(tmp_path):
    new = [row('g', NOW - 100 - i, actor='n%d' % i) for i in range(20)] + [row('g', NOW - 99, actor='n1'), row('g', NOW - 98, actor='seller', buy=0)]
    old = [row('g', NOW - 2000 - i, actor='o%d' % i) for i in range(13)]
    item = C.build(desk(tmp_path, new + old), NOW)['buyer_growth'][0]
    assert (item['buyers_30m'], item['buyers_prior_30m']) == (20, 13)


def test_below_the_ratio_or_count_is_out(tmp_path):
    new = [row('g', NOW - 100 - i, actor='n%d' % i) for i in range(20)]
    old = [row('g', NOW - 2000 - i, actor='o%d' % i) for i in range(14)]
    assert C.build(desk(tmp_path, new + old), NOW)['buyer_growth'] == []


def test_unknowns_are_typed(tmp_path):
    assert C.build(tmp_path / 'absent', NOW)['reason'] == 'tape_unreadable'
    d = desk(tmp_path, [row('up', NOW - 10, '90000000000')] * 1)
    (d / 'universe.heartbeat').unlink()
    got = C.build(d, NOW)
    assert got['sol_known'] is False and got['graduated_liquid'][0]['liq_usd'] is None


def test_the_written_list_is_read_with_its_age_and_old_or_missing_is_unknown(tmp_path):
    d = desk(tmp_path, [row('up', NOW - 10, '90000000000')])
    f = tmp_path / C.FILE_NAME
    assert C.read(f, NOW)['reason'] == 'lists_unavailable'
    C.write(d, f, NOW)
    got = C.read(f, NOW + 30)
    assert got['state'] == 'known' and got['age_s'] == 30. and got['graduated_liquid'][0]['pool'] == 'up'
    assert C.read(f, NOW + C.MAX_AGE_S + 1)['reason'] == 'lists_stale'
    f.write_text('broken')
    assert C.read(f, NOW)['reason'] == 'lists_unavailable'


def test_stale_future_or_malformed_fx_gives_no_usd_but_keeps_sol(tmp_path):
    d = desk(tmp_path, [row('up', NOW - 10, '90000000000')])
    for beat in (dict(sol_usd=100., sol_age_s=1000., ts=NOW - 1), dict(sol_usd=100., sol_age_s=1., ts=NOW + 5), []):
        (d / 'universe.heartbeat').write_text(json.dumps(beat))
        got = C.build(d, NOW)
        assert got['sol_known'] is False and got['graduated_liquid'][0]['liq_usd'] is None and got['graduated_liquid'][0]['real_quote_sol'] == 90.
