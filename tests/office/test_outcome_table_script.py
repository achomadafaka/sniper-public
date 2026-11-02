"""Outcome table writer on synthetic sources: universe, incremental passes, source labels. No tape is read."""
import importlib.util
from pathlib import Path
import sqlite3
from datetime import datetime, timezone

import pytest

from sniper.office import outcomes as O

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('otw', ROOT / 'scripts/office_outcome_table.py')
W = importlib.util.module_from_spec(spec); spec.loader.exec_module(W)

T0 = 1_000_000.
NOW = T0 + 10_000.


class Curve:
    def __init__(self, prints=(), end=None, creator=None): self.p, self.e, self.c = list(prints), end, creator
    def prints(self, mint, lo, hi): return [x for x in self.p if lo <= x[0] < hi], self.e
    def creator(self, mint, lo, hi): return self.c


class PS:
    def __init__(self, prints=(), end=None, creator=None, gaps=(), check='checked'): self.a = (list(prints), end, creator, list(gaps), check)
    def prints(self, mint, lo, hi, start=None): return ([x for x in self.a[0] if lo <= x[0] < hi],) + self.a[1:]


def test_universe_is_the_first_emission_per_mint(tmp_path):
    p = tmp_path / 'd.db'; c = sqlite3.connect(p)
    c.execute('CREATE TABLE desk_candidate_emissions(mint TEXT,network_id TEXT,emitted_at TEXT)')
    c.executemany('INSERT INTO desk_candidate_emissions VALUES(?,?,?)', [('m1', 'solana', '2026-10-22T10:00:10+00:00'), ('m1', 'solana', '2026-10-22T10:00:05+00:00'),
                                                                     ('m2', 'solana', '2026-10-22T11:00:00+00:00'), ('x', 'base', '2026-10-22T11:00:00+00:00')])
    c.commit(); c.close()
    uni = W.universe(p)
    assert set(uni) == {'m1', 'm2'} and uni['m1'] == 1792663205.0


def test_priced_coin_gets_matured_rows_only_and_skips_existing():
    prints = [(T0 - 1, 1.), (T0 + 10, .4)]
    coins, rows = W.process({'m1': T0}, Curve(), PS(prints, NOW, 'cr'), set(), now=T0 + 400)
    assert [r['horizon_s'] for _, _, r in rows] == [60, 300] and coins[0]['creator'] == 'cr' and coins[0]['creator_source'] == 'ps_tape'
    again = W.process({'m1': T0}, Curve(), PS(prints, NOW, 'cr'), {('m1', 60), ('m1', 300)}, now=T0 + 400)
    assert again == ([], [])
    later = W.process({'m1': T0}, Curve(), PS(prints, NOW, 'cr'), {('m1', 60), ('m1', 300)}, now=NOW)
    assert [r['horizon_s'] for _, _, r in later[1]] == [900, 1800, 3600]


def test_no_tape_at_all_is_typed_unknown_not_zero():
    coins, rows = W.process({'m1': T0}, Curve(), PS(), set(), now=NOW)
    assert {r['status'] for _, _, r in rows} == {'no_ref_price'} and all(r['ret'] is None for _, _, r in rows)
    assert coins[0]['price_source'] is None and coins[0]['creator'] is None


def test_creator_falls_back_to_launches_and_says_so():
    coins, _ = W.process({'m1': T0}, Curve(creator='lc'), PS(), set(), now=NOW)
    assert (coins[0]['creator'], coins[0]['creator_source']) == ('lc', 'launches')


def test_gap_check_is_unavailable_for_a_curve_priced_coin():
    prints = [(T0 - 1, 1.), (T0 + 10, .9)]
    _, rows = W.process({'m1': T0}, Curve(prints, NOW), PS([], NOW, None, [T0 + 5], 'checked'), set(), now=NOW)
    assert {r['gap_check'] for _, _, r in rows} == {'unavailable'} and {r['status'] for _, _, r in rows} <= {'ok', 'ok_unchecked', 'tape_ended_quiet'}       # a gap is not claimed from a tape that has none (and no row is censored by it)
    _, rows = W.process({'m1': T0}, Curve(), PS(prints, NOW, None, [T0 + 5], 'checked'), set(), now=NOW)
    assert {r['gap_check'] for _, _, r in rows} == {'checked'} and {r['status'] for _, _, r in rows} == {'censored_gap'}


def test_limit_bounds_the_pass_and_oldest_coins_go_first():
    coins, _ = W.process({'a': T0 + 5, 'b': T0, 'c': T0 + 9}, Curve(), PS(), set(), now=NOW, limit=2)
    assert [c['mint'] for c in coins] == ['b', 'a']


def test_an_unreadable_tape_is_typed_not_dropped_and_not_zero():
    class Bad:
        def prints(self, mint, lo, hi, start=None): raise W.TapeUnreadable('ps_swaps:row_limit')
    coins, rows = W.process({'m1': T0, 'm2': T0}, Curve(), Bad(), set(), now=NOW)
    assert {r['status'] for _, _, r in rows} == {'tape_unreadable'} and len(rows) == 10 and all(r['ret'] is None for _, _, r in rows)
    assert len(coins) == 2                                         # the coin is still in the table


def test_a_busy_window_is_read_in_halves():
    calls = []
    class T:
        class TapeUnreadable(Exception): pass
        def load_ps_swaps(self, d, mint, lo, hi, usd, **k):
            calls.append((lo, hi))
            if hi - lo > 10: raise self.TapeUnreadable('ps_swaps:row_limit')
            return [dict(received_at=lo)]
    ps = W.PumpSwap.__new__(W.PumpSwap); ps.T, ps.dir = T(), None
    got = ps._sliced('m', 0., 40.)
    assert [r['received_at'] for r in got] == [0., 10., 20., 30.] and (0., 40.) in calls


def test_creators_are_found_for_any_mint_from_every_source_and_disagreement_is_kept(tmp_path):
    desk = tmp_path / 'desk'
    (desk / 'launches').mkdir(parents=True); (desk / 'pumpswap').mkdir(); (tmp_path / 'arch').mkdir()
    c = sqlite3.connect(desk / 'launches/2026-10-21.db'); c.execute('CREATE TABLE launches(mint TEXT,creator TEXT,block_time INTEGER)')
    c.execute("INSERT INTO launches VALUES('mA','cL',100)"); c.execute("INSERT INTO launches VALUES('mB',NULL,100)"); c.commit(); c.close()
    r = sqlite3.connect(tmp_path / 'r.db'); r.execute('CREATE TABLE runner_signals(mint TEXT,creator TEXT,created_at TEXT)')
    r.execute("INSERT INTO runner_signals VALUES('mA','cX','2026-10-22T00:00:00+00:00')"); r.commit(); r.close()
    for path in (desk / 'pumpswap/tape.db', tmp_path / 'arch/keep-2026-10-23.db'):
        p = sqlite3.connect(path); p.execute('CREATE TABLE swaps_ps(mint TEXT,coin_creator TEXT,received_at REAL)')
        p.execute("INSERT INTO swaps_ps VALUES('mC','cP',50.)"); p.execute("INSERT INTO swaps_ps VALUES('mC','cP',40.)"); p.execute("INSERT INTO swaps_ps VALUES('mD',NULL,1.)"); p.commit(); p.close()
    rows = W.creator_rows(desk, tmp_path / 'arch', tmp_path / 'r.db')
    got = {(m, c, s) for _, m, c, s, _ in rows}
    assert got == {('mA', 'cL', 'launches'), ('mA', 'cX', 'runner_signals'), ('mC', 'cP', 'ps_live'), ('mC', 'cP', 'ps_archive')}      # null creators are not rows
    assert [t for _, m, c, s, t in rows if s == 'ps_live'] == [40.]                                                                 # earliest sighting
    db = sqlite3.connect(':memory:'); O.ensure_schema(db); O.write_creators(db, rows); O.write_creators(db, [(*rows[0][:4], 999.)])
    assert db.execute("SELECT COUNT(*) FROM mint_creators WHERE mint='mA'").fetchone()[0] == 2        # the two sources disagree: both stay
    assert db.execute("SELECT source_at FROM mint_creators WHERE source='launches'").fetchone()[0] == 100.     # first write wins
    with pytest.raises(sqlite3.DatabaseError, match='outcome_append_only'): db.execute('DELETE FROM mint_creators')


def test_rows_are_stamped_with_the_decoder_version_of_their_window_and_the_file_is_checked(tmp_path):
    prints = [(T0 - 1, 1.), (T0 + 10, .9)]
    _, rows = W.process({'m1': T0}, Curve(), PS(prints, NOW, None), set(), now=NOW, versions=[(0, 'v1'), (T0 + 400, 'v2')])
    got = {r['horizon_s']: r['tape_decoder_version'] for _, _, r in rows}
    assert got[60] == got[300] == 'v1' and got[900] == got[1800] == got[3600] == 'mixed:v1>v2'
    f = tmp_path / 'v.json'
    for text in ('', '{}', '{"versions": []}', '{"versions": [{"version":"a","effective_from":5},{"version":"b","effective_from":1}]}'):
        f.write_text(text)
        with pytest.raises(ValueError, match='outcome_decoder_file'): W.load_versions(f)
    assert W.load_versions(ROOT / 'config/office/tape_decoder_versions.json') == [(0, 'ps-decode-v1'), (1793400056.05985, 'ps-decode-cutover-v1v2'), (1793400064.1327717, 'ps-decode-v2')]


def test_a_creator_source_that_cannot_be_opened_is_named_not_skipped_silently(tmp_path):
    desk = tmp_path / 'desk'
    (desk / 'launches').mkdir(parents=True); (desk / 'pumpswap').mkdir(); (tmp_path / 'arch').mkdir()
    gone = []
    rows = W.creator_rows(desk, tmp_path / 'arch', tmp_path / 'no-runner.db', unavailable=gone)
    assert rows == [] and 'runner_signals' in gone and 'ps_live' in gone      # a pinned checkout without data/ read as an empty source for a whole run


def _pumpswap(now, keep, live_end, selection=None, archive=None, ret=7200.):
    ps = W.PumpSwap.__new__(W.PumpSwap)
    ps.live_keep, ps.selection, ps.live_end, ps.now, ps.ret_s = keep, selection or {}, live_end, now, ret
    ps.archive = archive or []
    return ps


def test_pool_coverage_ends_with_its_keep_window_not_with_the_live_tape():
    # 8fc3eaa5: keep window ends sheet+1198, the live tape runs on for other pools, the pool's later rows were pruned (3 h later)
    ps = _pumpswap(now=T0 + 3 * 3600, keep={'p': [(T0 - 60, T0 + 1198)]}, live_end=T0 + 3 * 3600)
    assert ps.coverage_end('p', T0, T0 + 3600) == T0 + 1198
    assert ps.coverage_end('p', T0, T0 + 900) == T0 + 1198
    # a keep window that runs into the unpruned tail is continuous to the live end
    young = _pumpswap(now=T0 + 1500, keep={'p': [(T0 - 60, T0 + 1198)]}, live_end=T0 + 1500)
    assert young.coverage_end('p', T0, T0 + 3600) == T0 + 1500
    # no keep window and older than retention: no coverage at all
    assert _pumpswap(now=T0 + 3 * 3600, keep={}, live_end=T0 + 3 * 3600).coverage_end('q', T0, T0 + 900) is None
    # a graduation pool is stored while in M whatever the feed listing says: selection does not cap it
    kept = _pumpswap(now=T0 + 1500, keep={'p': [(T0 - 60, T0 + 1198)]}, live_end=T0 + 1500, selection={'p': [(T0 - 148, T0 + 1000)]})
    assert kept.coverage_end('p', T0, T0 + 3600) == T0 + 1500
    # a pool with no keep window is stored only while a feed lists it: selection caps it
    listed = _pumpswap(now=T0 + 1500, keep={}, live_end=T0 + 1500, selection={'p': [(T0 - 148, T0 + 1000)]})
    assert listed.coverage_end('p', T0, T0 + 3600) == T0 + 1000
    assert _pumpswap(now=T0, keep=None, live_end=T0).coverage_end('p', T0, T0 + 1) is None      # unreadable keep table: no claim


def test_a_selection_row_still_open_covers_to_the_live_end_and_does_not_crash():
    listed = _pumpswap(now=T0 + 1500, keep={}, live_end=T0 + 1500, selection={'p': [(T0 - 148, None)]})
    assert listed.coverage_end('p', T0, T0 + 3600) == T0 + 1500


def test_the_archive_scratch_is_built_only_when_a_coin_needs_it(tmp_path):
    desk = tmp_path / 'desk'; (desk / 'pumpswap').mkdir(parents=True)
    arch = tmp_path / 'arch'; arch.mkdir()
    c = sqlite3.connect(arch / 'keep-2026-10-23.db'); c.execute('CREATE TABLE swaps_ps(pool TEXT, slot INTEGER, received_at REAL)'); c.execute('CREATE TABLE ps_keep(pool TEXT PRIMARY KEY, keep_from REAL, keep_until REAL)'); c.commit(); c.close()
    scratch = tmp_path / 'scratch'; scratch.mkdir()
    ps = W.PumpSwap(desk, arch, scratch, {})
    assert list(scratch.iterdir()) == [] and ps._archive is None                  # nothing pending: nothing copied
    _ = ps.archive
    assert ps._archive is not None and list(scratch.iterdir())                    # first use builds it


def _loss_ps(loss_from, gaps_from, by_pool=None, unknown=()):
    class T:                                                                   # capture gaps: none
        @staticmethod
        def ps_gaps(*a): return []
    ps = W.PumpSwap.__new__(W.PumpSwap)
    ps.T, ps.dir, ps.pools, ps.live_end, ps.now, ps.ret_s = T, '.', {'m': 'p'}, T0 + 100, T0 + 100, 7200.
    ps.live_keep, ps.selection, ps._archive, ps._archive_dir = {}, {}, [], None
    ps._loss = dict(by_pool=by_pool or {}, unknown=list(unknown), loss_from=loss_from, gaps_from=gaps_from)
    ps._sliced = lambda mint, lo, hi, depth=0: []          # no priced rows at all: the check must not depend on which tape priced the coin
    return ps


def test_the_loss_check_covers_every_window_the_loss_tables_cover_not_only_live_priced_coins():
    ps = _loss_ps(T0 - 500, T0 - 500, by_pool={'p': [T0 + 20]}, unknown=[T0 + 10, T0 + 30])
    _, _, _, gaps, check = ps.prints('m', T0 - 100, T0 + 60)
    assert check == 'checked' and gaps == [T0 + 20]                               # the pool's own complete-scope loss, no rows needed
    assert ps.loss_counts('p', T0, T0 + 60) == (1, 2)                             # both counts from one pass
    _, _, _, gaps, check = ps.prints('m', T0 - 900, T0 + 60)                      # starts before the loss tables do
    assert check == 'unavailable' and gaps == [] and ps.loss_counts('p', T0 - 900, T0 + 60) is None


def test_an_unreadable_loss_model_is_unknown_not_clean(tmp_path):
    from sniper.services import memecoin_desk_ps_tape as T
    ps = W.PumpSwap.__new__(W.PumpSwap); ps.T = T; ps.dir = tmp_path; ps._loss = None
    with pytest.raises(T.TapeUnreadable): ps.checked(T0)


def test_a_capture_stop_spanning_the_whole_window_censors_it_through_the_reader(tmp_path):
    # Zeta's [0, 2000] stop over (1000, 1060]: a clipped point at the read edge let it pass as checked ok
    from sniper.services import memecoin_desk_ps_tape as T
    p = T.tape_path(tmp_path); p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p)
    c.execute('CREATE TABLE gaps_ps(at REAL, cause TEXT, last_slot INTEGER)')
    c.execute('CREATE TABLE ps_gap_spans(start_at REAL, end_at REAL, cause TEXT, ended_by TEXT)')
    c.execute('CREATE TABLE ps_loss_attribution(received_at REAL, scope TEXT, pools_json TEXT, slot INTEGER, discriminator TEXT)')
    c.execute('INSERT INTO gaps_ps VALUES(500, "worker_start", 1)'); c.execute('INSERT INTO ps_loss_attribution VALUES(600, "complete", "[]", NULL, NULL)')
    c.execute('INSERT INTO ps_gap_spans VALUES(0, 2000, "stop", "restart")'); c.commit(); c.close()
    ps = W.PumpSwap.__new__(W.PumpSwap); ps.T, ps.dir, ps.now, ps._loss = T, tmp_path, 3000., None
    pts, spans = ps.window_gaps('p', 1000., 1060.)
    assert pts == [] and spans == [(0.0, 2000.0)]
    row = O.horizon_row([(1000., 1.), (1060., 2.)], 1000., 60, now=3000., tape_end=3000., gaps=pts, gap_spans=spans, gap_check='checked', loss=(0, 0))
    assert row['status'] == 'censored_gap'


def test_a_row_less_window_keeps_its_checked_state_and_a_curve_only_window_does_not():
    _, rows = W.process({'m1': T0}, Curve(), PS([], NOW, None, [], 'checked'), set(), now=NOW)
    assert {r['gap_check'] for _, _, r in rows} == {'checked'}                    # no PumpSwap rows, but the loss tables cover the window: it is in the checked denominator
    _, rows = W.process({'m1': T0}, Curve([(T0 - 1, 1.), (T0 + 10, .9)], NOW), PS([], NOW, None, [], 'checked'), set(), now=NOW)
    assert {r['gap_check'] for _, _, r in rows} == {'unavailable'}                # priced by the curve tape: PumpSwap losses say nothing about it


def test_the_hole_between_two_capture_sessions_censors_a_window_that_crosses_it(tmp_path):
    # the 2026-10-30 repin left 8.07 s between session cbd0b41e (through_at) and 432be482 (start_at) with no coverage and no ps_gap_spans row
    from sniper.services import memecoin_desk_ps_tape as T
    p = T.tape_path(tmp_path); p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p)
    c.execute('CREATE TABLE gaps_ps(at REAL, cause TEXT, last_slot INTEGER)')
    c.execute('CREATE TABLE ps_gap_spans(start_at REAL, end_at REAL, cause TEXT, ended_by TEXT)')
    c.execute('CREATE TABLE ps_loss_attribution(received_at REAL, scope TEXT, pools_json TEXT, slot INTEGER, discriminator TEXT)')
    c.execute('CREATE TABLE ps_capture_intervals(session TEXT, start_at REAL, through_at REAL, protocol INTEGER)')
    c.execute('INSERT INTO gaps_ps VALUES(500, "worker_start", 1)'); c.execute('INSERT INTO ps_loss_attribution VALUES(600, "complete", "[]", NULL, NULL)')
    c.execute('INSERT INTO ps_capture_intervals VALUES("old", 100, 1056.06, 1)'); c.execute('INSERT INTO ps_capture_intervals VALUES("new", 1064.13, NULL, 1)')
    c.commit(); c.close()
    ps = W.PumpSwap.__new__(W.PumpSwap); ps.T, ps.dir, ps.now, ps._loss = T, tmp_path, 3000., None
    pts, spans = ps.window_gaps('p', 1000., 1100.)
    assert spans == [(1056.06, 1064.13)]
    row = O.horizon_row([(1000., 1.), (1099., 1.)], 1000., 60, now=3000., tape_end=3000., gaps=pts, gap_spans=spans, gap_check='checked', loss=(0, 0))
    assert row['status'] == 'censored_gap'
    assert ps.window_gaps('p', 1070., 1100.)[1] == []            # a window after the hole is untouched


def _cand(mcap=None, liq=1000., reserves=True):
    state = dict(base_reserve_atomic='1000000', quote_reserve_atomic='3000000000', virtual_quote_atomic='1000000000', sol_usd=100.) if reserves else None
    return dict(snapshot=dict(fields=dict(mcap_usd=dict(completeness='complete' if mcap else 'absent', value=mcap), liq_usd=dict(completeness='complete', value=liq), pool_state=dict(value=state))))


TICKET = dict(stages=[dict(stage='hard_filter', inputs=dict(evidence=dict(known_at='2026-10-31T00:00:00+00:00', checks=dict(token_safety=dict(supply_atomic='10000000')))))])


def test_market_at_emission_prices_on_virtual_inclusive_reserves_and_supply():
    assert W.market_at_emission(_cand(), TICKET) == (4000.0, 1000., 'emission_reserves_x_hard_filter_supply', datetime(2026, 10, 31, tzinfo=timezone.utc).timestamp())   # 4 SOL x 100 USD x supply/base 10
    assert W.market_at_emission(_cand(mcap=7.), TICKET) == (7., 1000., 'emission_field', None)


def test_market_at_emission_unknown_parts_stay_null():
    assert W.market_at_emission(_cand(), dict(stages=[])) == (None, 1000., 'liq_only', None)
    assert W.market_at_emission(_cand(reserves=False), TICKET) == (None, 1000., 'liq_only', None)
    assert W.market_at_emission({}, None) == (None, None, None, None)
    untimed = dict(stages=[dict(stage='hard_filter', inputs=dict(evidence=dict(checks=dict(token_safety=dict(supply_atomic='10000000')))))])
    assert W.market_at_emission(_cand(), untimed)[0] is None      # no read time, no mcap


def test_old_coin_rows_stay_null_and_a_new_coin_records_its_market(tmp_path):
    db = sqlite3.connect(tmp_path / 'o.db')
    db.execute('CREATE TABLE outcome_coins(network_id TEXT NOT NULL,mint TEXT NOT NULL,sheet_at REAL NOT NULL,first_seen_at REAL NOT NULL,creator TEXT,creator_source TEXT,price_source TEXT,PRIMARY KEY(network_id,mint))')
    db.execute("INSERT INTO outcome_coins VALUES('solana','old',1.,1.,NULL,NULL,NULL)"); db.commit()
    O.ensure_schema(db); O.ensure_schema(db)
    O.write(db, asof=10., coins=[dict(network_id='solana', mint='new', sheet_at=5., mcap_usd=9., liq_usd=3., market_source='emission_field')], horizons=[])
    assert db.execute("SELECT mint,mcap_usd,liq_usd,market_source FROM outcome_coins ORDER BY mint").fetchall() == [('new', 9., 3., 'emission_field'), ('old', None, None, None)]


def test_a_half_applied_market_migration_resumes(tmp_path):
    db = sqlite3.connect(tmp_path / 'h.db')
    db.execute('CREATE TABLE outcome_coins(network_id TEXT NOT NULL,mint TEXT NOT NULL,sheet_at REAL NOT NULL,first_seen_at REAL NOT NULL,creator TEXT,creator_source TEXT,price_source TEXT,mcap_usd REAL,PRIMARY KEY(network_id,mint))')
    O.ensure_schema(db)
    assert {'mcap_usd', 'liq_usd', 'market_source', 'supply_known_at'} <= {r[1] for r in db.execute('PRAGMA table_info(outcome_coins)')}


def test_seats_do_not_get_market_fields_that_postdate_the_row_time():
    from sniper.office import seat_db
    assert not {'mcap_usd', 'liq_usd', 'market_source', 'supply_known_at'} & set(seat_db.EXTERNAL['outcome_coins'][3])
