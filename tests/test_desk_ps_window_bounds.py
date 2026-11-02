"""Bounded observations retain receipt membership and fail closed on unknown slots/proof."""
import json
import sqlite3

import pytest

from sniper.services import memecoin_desk_ps_tape as PT

NOW = 1_800_000_000.0


@pytest.fixture(params=[False, True], ids=['rowid', 'without-rowid'])
def tape(request):
    c = sqlite3.connect(':memory:')
    c.execute('CREATE TABLE swaps_ps(pool TEXT,slot INTEGER,received_at REAL,signature TEXT,ordinal INTEGER,'
              'payload_json TEXT,is_buy INTEGER,tx_index INTEGER,PRIMARY KEY(signature,ordinal))'
              + (' WITHOUT ROWID' if request.param else ''))
    c.executescript('CREATE INDEX swaps_ps_received ON swaps_ps(received_at);'
                    'CREATE INDEX swaps_ps_pool_slot ON swaps_ps(pool,slot);'
                    'CREATE TABLE ps_keep(pool TEXT PRIMARY KEY,keep_from REAL,keep_until REAL);')
    yield c
    c.close()


def put(c, sig, slot, received, *, payload='{}'):
    c.execute('INSERT INTO swaps_ps VALUES(?,?,?,?,0,?,1,1)', ('P',slot,received,sig,payload))


def reader():
    call = getattr(PT, 'window_rows', None)
    assert callable(call), 'shared bounded reader is missing'
    return call


def test_receipt_window_keeps_delayed_old_slots_and_has_two_index_bounds(tape):
    put(tape, 'ancient', 999999, NOW-100000)
    put(tape, 'late', 2, NOW-5)
    put(tape, 'current', 100, NOW-4)
    put(tape, 'future', 101, NOW+1)
    statements = []
    tape.set_trace_callback(statements.append)
    rows = reader()(tape, 'slot,signature', 'P', NOW-10, NOW)
    tape.set_trace_callback(None)
    assert rows == [(2,'late'), (100,'current')]
    query = next(s for s in statements if 'WITH' in s.upper() and 'swaps_ps' in s)
    plan = ' '.join(r[3] for r in tape.execute('EXPLAIN QUERY PLAN '+query))
    assert 'swaps_ps_received (received_at>? AND received_at<?)' in plan
    assert 'swaps_ps_pool_slot (pool=? AND slot>? AND slot<?)' in plan


def test_cutoff_row_limit_and_empty_window(tape):
    put(tape, 'a', 5, NOW-3)
    put(tape, 'b', 6, NOW-2)
    assert reader()(tape, 'slot', 'P', NOW-10, NOW, cut_slot=6, limit=1) == [(5,)]
    assert reader()(tape, 'slot', 'P', NOW+10, NOW+20) == []


@pytest.mark.parametrize("cut_slot", [None, 50])
def test_null_slot_is_unknown_not_silently_filtered_by_minmax(tape, cut_slot):
    put(tape, 'bad', None, NOW-2)
    with pytest.raises(PT.TapeUnreadable, match='slot'):
        reader()(tape, 'slot', 'P', NOW-10, NOW, cut_slot=cut_slot)


@pytest.mark.parametrize('index', ['swaps_ps_received','swaps_ps_pool_slot'])
def test_missing_index_is_typed_unknown_not_a_full_scan(tape, index):
    put(tape, 'a', 5, NOW-3)
    tape.execute('DROP INDEX '+index)
    with pytest.raises(PT.TapeUnreadable):
        reader()(tape, 'slot', 'P', NOW-10, NOW)


def test_proof_seed_outside_fallback_bound_is_unknown_but_keep_window_is_authoritative(tape):
    proof=json.dumps({'ix_name':'buy','virtual_quote_source':PT.VQ_SOURCE,
                      'virtual_quote_atomic':'0','virtual_quote_reserves':0})
    put(tape, 'old-proof', 2, NOW-3*86400, payload=proof)
    assert PT._proof_before(tape, 'P', NOW) is None
    tape.execute('INSERT INTO ps_keep VALUES(?,?,?)', ('P',NOW-4*86400,NOW+60))
    assert PT._proof_before(tape, 'P', NOW) == 0


def test_bad_keep_bound_is_unknown_not_a_default_or_unbounded_seed(tape):
    tape.execute('INSERT INTO ps_keep VALUES(?,?,?)', ('P','bad',NOW+60))
    with pytest.raises(PT.TapeUnreadable):
        PT._proof_before(tape, 'P', NOW)


@pytest.mark.parametrize('index', ['swaps_ps_received','swaps_ps_pool_slot'])
def test_wrong_index_definition_is_unknown_not_an_index_named_full_scan(tape, index):
    put(tape, 'a', 5, NOW-3)
    tape.execute('DROP INDEX '+index)
    tape.execute('CREATE INDEX '+index+' ON swaps_ps(signature)')
    with pytest.raises(PT.TapeUnreadable):
        reader()(tape, 'slot', 'P', NOW-10, NOW)


def _bounds_plan(c):
    statements = []
    c.set_trace_callback(statements.append)
    reader()(c, 'slot,signature', 'P', NOW-10, NOW)
    c.set_trace_callback(None)
    query = next(s for s in statements if 'WITH' in s.upper() and 'swaps_ps' in s)
    return ' '.join(r[3] for r in c.execute('EXPLAIN QUERY PLAN '+query))


def test_pool_received_index_bounds_one_pool_not_the_whole_tape(tape):
    """With swaps_ps_pool_received present the bounds walk only this pool's receipts (6 h window was 3.5 s on the
    live tape through swaps_ps_received, which reads every pool's rows in the window)."""
    tape.execute('CREATE INDEX swaps_ps_pool_received ON swaps_ps(pool,received_at)')
    put(tape, 'current', 100, NOW-4)
    plan = _bounds_plan(tape)
    assert 'swaps_ps_pool_received (pool=? AND received_at>? AND received_at<?)' in plan
    assert 'swaps_ps_received' not in plan.replace('swaps_ps_pool_received', '')


def test_wrong_shape_pool_received_index_fails_closed(tape):
    tape.execute('CREATE INDEX swaps_ps_pool_received ON swaps_ps(received_at,pool)')
    with pytest.raises(PT.TapeUnreadable, match='index_unknown'):
        reader()(tape, 'slot', 'P', NOW-10, NOW)
