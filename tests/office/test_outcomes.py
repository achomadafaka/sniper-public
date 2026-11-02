"""Office outcome table rules and store, on synthetic prints. Never reads a tape."""
import random
import sqlite3

import pytest

from sniper.office import outcomes as O

T0 = 1_000_000.
NOW = T0 + 10_000.
KW = dict(now=NOW, tape_end=NOW, gap_check='checked')       # a window the loss tables cover; unchecked windows get ok_unchecked (see the dedicated test)


def row(prints, h=900, **kw): return O.horizon_row(O.clean(prints), T0, h, **{**KW, **kw})


def test_immature_until_the_horizon_plus_settle():
    assert O.horizon_row([], T0, 900, now=T0 + 900 + O.SETTLE_S - 1, tape_end=NOW) is None
    assert O.horizon_row(O.clean([(T0 - 1, 1.)]), T0, 900, now=T0 + 900 + O.SETTLE_S, tape_end=NOW)['status'] == 'no_tape'
    assert len(O.rows_for_coin(O.clean([(T0 - 1, 1.)]), T0, now=T0 + 900 + O.SETTLE_S, tape_end=NOW)) == 3      # 60, 300, 900 only


def test_values():
    r = row([(T0 - 5, 1.0), (T0 + 10, 2.0), (T0 + 20, 0.4), (T0 + 2000, 9.0)], h=60)
    assert r['status'] == 'ok' and (r['p0'], r['p0_age_s'], r['p_end'], r['n_prints']) == (1.0, 5, 0.4, 2)
    assert r['ret'] == pytest.approx(-0.6) and r['peak_ret'] == pytest.approx(1.0) and r['trough_ret'] == pytest.approx(-0.6)
    assert r['max_drawdown'] == pytest.approx(-0.8) and r['collapsed'] == 1 and r['collapse_s'] == 20 and r['last_print_age_s'] == 40


def test_reference_is_the_last_print_at_or_before_the_sheet_never_after():
    assert row([(T0 + 1, 1.0), (T0 + 30, 2.0)])['status'] == 'no_ref_price'                   # first print after the sheet is not a reference
    assert row([(T0 - O.REF_MAX_AGE_S - 1, 1.0), (T0 + 30, 2.0)])['status'] == 'no_ref_price'  # too old
    assert row([(T0, 1.0), (T0 + 30, 2.0)])['p0'] == 1.0                                       # a print exactly at the sheet counts


def test_unpriced_prints_are_dropped_not_zero():
    r = row([(T0 - 1, 1.0), (T0 + 5, None), (T0 + 6, 0.0), (T0 + 7, float('nan')), (T0 + 8, -1.)])
    assert r['status'] == 'no_tape' and r['ret'] is None and r['collapsed'] is None and r['n_prints'] == 0


def test_typed_unknowns():
    base = [(T0 - 1, 1.0), (T0 + 30, 0.9)]
    assert row(base, h=60, tape_end=T0 + 40)['status'] == 'tape_ended_capture'
    assert row(base, h=60, tape_end=None)['status'] == 'tape_ended_capture'
    assert row(base, h=60, gaps=[T0 + 50], gap_check='checked')['status'] == 'censored_gap'
    r = row(base, h=60, gaps=[T0 + 50], gap_check='unavailable')          # an unchecked source does not claim a gap, and says so
    assert r['status'] == 'ok_unchecked' and r['gap_check'] == 'unavailable'      # a return where no loss check was possible is never plain ok
    assert row(base, h=60, gaps=[T0 - 5, T0 + 5000], gap_check='checked')['status'] == 'ok'          # gaps outside the window do not censor
    for bad in (row(base, h=60, tape_end=T0 + 40), row([(T0 + 30, 1.)], h=60)): assert bad['ret'] is None and bad['max_drawdown'] is None


def test_no_look_ahead_a_row_equals_the_row_from_a_tape_cut_at_its_horizon():
    rng = random.Random(7)
    prints = O.clean([(T0 - 100 + i * 7., rng.uniform(.2, 3.)) for i in range(600)])
    for h in O.HORIZONS:
        full = O.horizon_row(prints, T0, h, now=NOW, tape_end=NOW)
        cut = O.horizon_row([p for p in prints if p[0] <= T0 + h], T0, h, now=NOW, tape_end=NOW)
        assert full == cut


def test_invalid_input_is_refused():
    for kw in (dict(h=7), dict(gap_check='maybe')):
        with pytest.raises(ValueError, match='outcome_input_invalid'): row([(T0, 1.)], **kw)


@pytest.fixture
def db():
    c = sqlite3.connect(':memory:'); O.ensure_schema(c); return c


COIN = dict(network_id='solana', mint='m1', sheet_at=T0, creator='c1', creator_source='tape', price_source='ps')


def test_store_is_append_only_first_write_wins_and_immature_reads_null(db):
    early = O.rows_for_coin(O.clean([(T0 - 1, 1.), (T0 + 10, .4), (T0 + 295, .4)]), T0, now=T0 + 400, tape_end=T0 + 400)      # horizons 60 and 300 matured
    O.write(db, asof=T0 + 400, coins=[COIN], horizons=[('solana', 'm1', r) for r in early])
    for sql in ('UPDATE outcome_coins SET creator="x"', 'DELETE FROM outcome_horizons', 'UPDATE outcome_horizons SET ret=1'):
        with pytest.raises(sqlite3.DatabaseError, match='outcome_append_only'): db.execute(sql)
    seen = db.execute('SELECT horizon_s FROM outcome_horizons ORDER BY 1').fetchall()
    assert seen == [(60,), (300,)]
    got = dict(db.execute('SELECT h.horizon_s,o.ret FROM (SELECT 60 horizon_s UNION SELECT 300 UNION SELECT 900) h '
                          'LEFT JOIN outcome_horizons o ON o.horizon_s=h.horizon_s AND o.mint="m1"').fetchall())
    assert got[900] is None and got[300] == pytest.approx(-0.6)                    # immature = NULL, matured = number
    again = O.rows_for_coin(O.clean([(T0 - 1, 1.), (T0 + 10, 9.)]), T0, now=T0 + 400, tape_end=T0 + 400)
    O.write(db, asof=T0 + 500, coins=[dict(COIN, creator='other')], horizons=[('solana', 'm1', r) for r in again])
    assert db.execute('SELECT ret FROM outcome_horizons WHERE horizon_s=300').fetchone()[0] == pytest.approx(-0.6)       # first write wins
    assert db.execute('SELECT creator FROM outcome_coins').fetchone()[0] == 'c1'


def test_asof_only_moves_forward_and_numbers_need_ok_status(db):
    O.write(db, asof=500., coins=[], horizons=[]); O.write(db, asof=400., coins=[], horizons=[])
    assert db.execute('SELECT asof,version FROM outcome_meta').fetchone() == (500., O.VERSION)
    bad = O.horizon_row(O.clean([(T0 - 1, 1.), (T0 + 5, 2.)]), T0, 60, **KW)
    bad = dict(bad, status='no_tape')                                              # numbers with an unknown status are refused by the table
    with pytest.raises(sqlite3.IntegrityError): O.write(db, asof=600., coins=[], horizons=[('solana', 'm2', bad)])


def test_decoder_version_stamps_the_side_of_the_boundary_and_mixed_windows():
    v = [(0, 'v1'), (T0 + 500, 'v2')]
    assert O.decoder_version(v, T0 - 100, T0 + 400) == 'v1'
    assert O.decoder_version(v, T0 + 500, T0 + 900) == 'v2'
    assert O.decoder_version(v, T0 - 100, T0 + 900) == 'mixed:v1>v2'
    with pytest.raises(ValueError, match='outcome_decoder_unknown'): O.decoder_version([(T0, 'v1')], T0 - 1, T0 + 5)


def test_every_row_carries_its_decoder_version_and_the_table_requires_it(db):
    r = O.horizon_row(O.clean([(T0 - 1, 1.), (T0 + 10, .4)]), T0, 60, decoder='v7', **KW)
    assert r['tape_decoder_version'] == 'v7' and O.horizon_row([], T0, 60, **KW)['tape_decoder_version'] == O.DEFAULT_DECODER
    O.write(db, asof=T0 + 400, coins=[COIN], horizons=[('solana', 'm1', r)])
    assert db.execute('SELECT tape_decoder_version FROM outcome_horizons').fetchone() == ('v7',)
    with pytest.raises(sqlite3.IntegrityError): O.write(db, asof=T0 + 500, coins=[], horizons=[('solana', 'm2', dict(r, tape_decoder_version=None))])


def test_an_old_last_print_is_never_ok_it_is_the_coin_going_quiet_and_capture_end_is_separate():
    # 8fc3eaa5 shape: prints stop long before the horizon end. Covered tape = coin quiet; tape that ends first = capture/retention.
    stale = [(T0 - 1, 1.0), (T0 + 5, 0.9)]
    q = row(stale, tape_end=T0 + 5000)
    assert q['status'] == 'tape_ended_quiet' and q['ret'] is None and q['last_print_age_s'] > O.QUIET_S
    c = row(stale, tape_end=T0 + 30)
    assert c['status'] == 'tape_ended_capture' and c['ret'] is None
    assert row([(T0 - 1, 1.0), (T0 + 55, 0.9)], h=60, tape_end=T0 + 5000)['status'] == 'ok'      # within the bound


def test_loss_counts_and_shares_are_recorded_only_where_the_window_was_checked():
    base = [(T0 - 1, 1.0), (T0 + 30, 0.9)]
    r = row(base, h=60, loss=(2, 5))
    assert (r['loss_complete_n'], r['loss_unknown_n']) == (2, 5) and r['loss_share_complete'] == pytest.approx(2 / 3) and r['loss_share_all'] == pytest.approx(7 / 8)
    u = row(base, h=60, gap_check='unavailable', loss=(2, 5))
    assert u['loss_complete_n'] is None and u['loss_share_all'] is None                  # outside the loss tables' retention nothing is claimed
    assert row(base, h=60)['loss_complete_n'] is None                                    # no counts given: unknown, not zero
