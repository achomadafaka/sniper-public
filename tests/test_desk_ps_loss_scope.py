"""Immutable loss attribution and actual selected-pool coverage; temporary tapes only."""
import json
import sqlite3

import pytest

from sniper.services import memecoin_desk_pumpswap as PS
from sniper.services import memecoin_desk_ps_losses as L
from test_memecoin_desk_pumpswap import logs, buy, POOL, ACTOR
from sniper.execution.pumpswap_buy import AMM_PROGRAM, BUY_EXACT_QUOTE_IN_DISCRIMINATOR
from test_memecoin_desk_pumpswap_worker import W, worker, frame


def db(tmp_path):
    files = W.DayFiles(tmp_path)
    c = files.conn('ps_decode_losses', 100.)
    return files, c


def receipt(c, sig='one', at=100., pools=None, scope='complete'):
    c.execute('INSERT INTO ps_decode_losses VALUES(?,?,?,?)', (sig, 'decode_loss', 10, at))
    c.execute('INSERT INTO ps_loss_attribution VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
              (sig, 'decode_loss', 'pumpswap_event_malformed', scope, json.dumps(pools or []),
               1, '67f4521f2cf57777', 20, PS.PUMP_AMM_IDL_SHA256, at, 10, 1))


def batch(c, sigs=('one',), at=100.):
    cur = c.execute("INSERT INTO gaps_ps VALUES(?,'decode_loss',10)", (at,))
    gid = cur.lastrowid
    c.execute('INSERT INTO ps_loss_batches VALUES(?,?,?,?,?,?)', (gid, at, 'decode_loss', len(sigs), 1, at + 1.))
    c.executemany('INSERT INTO ps_loss_batch_members VALUES(?,?,?)', [(gid, s, 'decode_loss') for s in sigs])
    c.commit()
    return gid


@pytest.mark.parametrize('pools,expected', [([POOL], [100.]), ([ACTOR], []), ([POOL, ACTOR], [100.])])
def test_complete_scope_checks_every_affected_pool(tmp_path, pools, expected):
    files, c = db(tmp_path); receipt(c, pools=pools); batch(c)
    assert L.losses(tmp_path, POOL, 99., 102.) == expected
    c.close()


def test_unknown_scope_and_legacy_rows_stay_global(tmp_path):
    files, c = db(tmp_path); receipt(c, pools=[ACTOR], scope='unknown'); batch(c)
    assert L.losses(tmp_path, POOL, 99., 102.) == [100.]
    c.execute('DELETE FROM ps_loss_attribution'); c.commit()
    assert L.losses(tmp_path, POOL, 99., 102.) == [100.]
    c.close()


def test_unattested_or_partial_batch_never_weakens_legacy_gap(tmp_path):
    files, c = db(tmp_path); receipt(c, pools=[ACTOR]); gid = batch(c)
    c.execute('DELETE FROM ps_loss_batch_members'); c.commit()
    assert L.losses(tmp_path, POOL, 99., 102.) == [100.]
    c.execute('DELETE FROM ps_loss_batches'); c.commit()
    assert L.losses(tmp_path, POOL, 99., 102.) == [100.]
    c.close()


def test_second_loss_in_coalesced_batch_is_read_inside_later_window(tmp_path):
    files, c = db(tmp_path); receipt(c, pools=[ACTOR]); receipt(c, 'two', 100.8, [POOL]); batch(c, ('one', 'two'))
    assert L.losses(tmp_path, POOL, 100.5, 102.) == [100.8]
    c.close()


@pytest.mark.parametrize('mutation,reason', [
    ('DROP INDEX ps_decode_losses_received', 'index'),
    ('DROP TABLE ps_decode_losses', 'schema'),
    ("UPDATE ps_loss_attribution SET pools_json='broken'", 'malformed')])
def test_bad_schema_index_or_attribution_refuses(tmp_path, mutation, reason):
    files, c = db(tmp_path); receipt(c, pools=[POOL]); batch(c); c.execute(mutation); c.commit()
    with pytest.raises(L.LossUnreadable, match=reason): L.losses(tmp_path, POOL, 99., 102.)
    c.close()


def test_overflow_and_malformed_pending_refuse_but_future_pending_does_not(tmp_path):
    files, c = db(tmp_path); receipt(c, pools=[POOL]); receipt(c, 'two', 100.8, [POOL]); batch(c, ('one', 'two'))
    with pytest.raises(L.LossUnreadable, match='row_limit'): L.losses(tmp_path, POOL, 99., 102., max_rows=1)
    assert L.losses(tmp_path, POOL, 99., 100., pending={'decode_loss': 101.}) == []
    assert L.losses(tmp_path, POOL, 99., 100., pending={'decode_loss': 99.5}) == [99.5]
    with pytest.raises(L.LossUnreadable, match='pending'): L.losses(tmp_path, POOL, 99., 102., pending={'decode_loss': None})
    c.close()


def ix(pool_index=1):
    return {'program_id_index': 0, 'accounts': [pool_index] + [2] * 24,
            'data': BUY_EXACT_QUOTE_IN_DISCRIMINATOR + bytes(17)}


def test_full_transaction_scope_cannot_stop_at_the_first_event():
    detail = PS.loss_details(logs(buy()) + logs(buy()), reason='pumpswap_event_malformed',
                             account_keys=[AMM_PROGRAM, POOL, ACTOR], instructions=[ix(), ix(2)], inner_complete=True)
    assert detail['scope'] == 'complete' and detail['pools'] == sorted([POOL, ACTOR])
    assert detail['invocations'] == 2


@pytest.mark.parametrize('change', ['truncated', 'missing_alt', 'unknown_instruction', 'missing_inner', 'overflow'])
def test_incomplete_transaction_scope_is_unknown(change):
    ls, keys, instructions, complete = logs(buy()), [AMM_PROGRAM, POOL, ACTOR], [ix()], True
    if change == 'truncated': ls = ls[:-1]
    if change == 'missing_alt': instructions[0]['accounts'][0] = 99
    if change == 'unknown_instruction': instructions[0]['data'] = bytes(8)
    if change == 'missing_inner': complete = False
    if change == 'overflow': instructions = [ix()] * 129
    assert PS.loss_details(ls, reason='pumpswap_event_malformed', account_keys=keys,
                           instructions=instructions, inner_complete=complete)['scope'] == 'unknown'


def test_writer_preserves_reason_and_conservative_legacy_gap(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    monkeypatch.setattr(PS, 'events', lambda _: (_ for _ in ()).throw(PS.DecodeLoss('pumpswap_logs_incomplete')))
    w.process(frame(), received_at=100.); w.flush_ps_gaps(102.)
    c = w.files.conn('ps_decode_losses', 102.)
    assert c.execute('SELECT reason,scope FROM ps_loss_attribution').fetchone() == (
        'pumpswap_logs_incomplete:scope_invocation_count_n0_l1', 'unknown')
    assert c.execute('SELECT cause FROM gaps_ps').fetchall() == [('decode_loss',)]
    assert L.losses(tmp_path, POOL, 99., 103.) == [100.]


def test_coverage_requires_actual_inclusion_and_capture_intersection(tmp_path):
    files, c = db(tmp_path)
    c.execute('INSERT INTO ps_pool_selection VALUES(?,?,?,?,?)', (POOL, 'session', 90., None, 1))
    c.execute('INSERT INTO ps_capture_intervals VALUES(?,?,?,?)', ('session', 95., 105., 1)); c.commit()
    assert L.pool_coverage(tmp_path, POOL, 80., 110.) == [(95., 105.)]
    assert L.pool_coverage(tmp_path, ACTOR, 80., 110.) == []
    c.execute('DROP TABLE ps_pool_selection'); c.commit()
    with pytest.raises(L.LossUnreadable, match='coverage'): L.pool_coverage(tmp_path, POOL, 80., 110.)
    c.close()


def test_selection_records_union_deltas_without_backdating_or_reloads(tmp_path, monkeypatch):
    from types import SimpleNamespace
    w = worker(tmp_path, monkeypatch)
    book = SimpleNamespace(pools={})
    book.apply_listed = lambda read, now: setattr(book, 'pools', read)
    w.intake_book = book
    w.intake_apply({POOL: 'mint'}, 100.)
    w.intake_apply({POOL: 'mint'}, 105.)
    w.intake_apply({}, 110.)
    c = w.files.conn('ps_pool_selection', 110.)
    assert c.execute('SELECT pool,start_at,end_at,protocol FROM ps_pool_selection').fetchall() == [(POOL, 100., 110., 1)]


def test_writer_reconnect_leaves_a_real_capture_hole(tmp_path, monkeypatch):
    from test_desk_ps_commit_watermark import source_block
    from sniper.util import clock
    monkeypatch.setattr(clock, 'offset_s', lambda: 0.)
    now = [100.]; monkeypatch.setattr(W.time, 'time', lambda: now[0])
    w = worker(tmp_path, monkeypatch)
    source_block(w, 100.); w.commit_files()
    now[0] = 102.; source_block(w, 102.); w.commit_files()
    w.ps_gap('reconnect', 103.)
    now[0] = 105.; source_block(w, 105.); w.commit_files()
    c = w.files.conn('ps_capture_intervals', 105.)
    assert c.execute('SELECT start_at,through_at FROM ps_capture_intervals ORDER BY start_at').fetchall() == [(100., 102.), (105., 105.)]


def test_pool_count_overflow_and_global_instruction_remain_global():
    from solders.pubkey import Pubkey
    keys = [AMM_PROGRAM] + [str(Pubkey.from_bytes(bytes([i]) * 32)) for i in range(1, 34)]
    instructions = [ix(i) for i in range(1, 34)]
    assert PS.loss_details(logs(buy()) * 33, reason='malformed', account_keys=keys,
                           instructions=instructions, inner_complete=True)['scope'] == 'unknown'
    instructions = [ix()]; instructions[0]['data'] = bytes.fromhex('2d3da597680031bd') + bytes(42)
    assert PS.loss_details(logs(buy()), reason='malformed', account_keys=keys,
                           instructions=instructions, inner_complete=True)['scope'] == 'unknown'


def test_decoder_diagnostic_retains_typed_detail_and_bounded_event_identity():
    import base64
    raw = base64.b64decode(buy().split(': ', 1)[1]) + b'\x01'
    ls = logs('Program data: ' + base64.b64encode(raw).decode())
    with pytest.raises(PS.DecodeLoss) as e: PS.events(ls)
    assert str(e.value) == 'pumpswap_event_malformed'
    assert e.value.detail_reason == 'pumpswap_event_malformed:truncated_virtual_quote'
    detail = PS.loss_details(ls, reason=e.value.detail_reason, account_keys=[AMM_PROGRAM, POOL, ACTOR],
                             instructions=[ix()], inner_complete=True)
    assert detail['discriminator'] == '67f4521f2cf57777' and detail['event_length'] == len(raw)


def test_storage_rollback_requires_fresh_selection_start(tmp_path, monkeypatch):
    from types import SimpleNamespace
    w = worker(tmp_path, monkeypatch)
    book = SimpleNamespace(pools={}); book.apply_listed = lambda read, now: setattr(book, 'pools', read)
    w.intake_book = book
    w.intake_apply({POOL:'mint'}, 100.)
    c = w.files.conn('ps_pool_selection', 100.); c.commit()
    w.ps_storage_error(sqlite3.OperationalError('disk'))
    w.intake_apply({}, 110.)
    assert c.execute('SELECT end_at FROM ps_pool_selection').fetchall() == [(110.,)]


def test_diagnostic_names_the_failed_event_after_an_earlier_valid_event():
    import base64
    disc = bytes.fromhex('78f83d531f8e6b90')
    ls = logs(buy(), 'Program data: ' + base64.b64encode(disc).decode())
    detail = PS.loss_details(ls, reason='pumpswap_virtual_quote_unproven',
                             account_keys=[AMM_PROGRAM, POOL, ACTOR], instructions=[ix()], inner_complete=True)
    assert detail['discriminator'] == disc.hex() and detail['event_length'] == 8


def test_partial_batch_before_lo_is_still_global_during_its_flush_span(tmp_path):
    files, c = db(tmp_path); receipt(c, pools=[ACTOR]); receipt(c, 'two', 100.8, [POOL]); batch(c, ('one','two'))
    c.execute("DELETE FROM ps_decode_losses WHERE signature='two'"); c.commit()
    assert L.losses(tmp_path, POOL, 100.5, 102.) == [100.5]
    c.close()


def test_writer_scope_requires_resolved_loaded_addresses(tmp_path, monkeypatch):
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    from solders.pubkey import Pubkey
    w = worker(tmp_path, monkeypatch)
    u = pb.SubscribeUpdate.FromString(frame())
    info = u.transaction.transaction
    info.transaction.message.account_keys.extend([bytes(Pubkey.from_string(x)) for x in (AMM_PROGRAM, ACTOR, POOL)])
    instruction = info.transaction.message.instructions.add(program_id_index=0)
    instruction.accounts = bytes([1] + [2] * 24)
    instruction.data = BUY_EXACT_QUOTE_IN_DISCRIMINATOR + bytes(17)
    monkeypatch.setattr(PS, 'events', lambda _: (_ for _ in ()).throw(PS.DecodeLoss('pumpswap_event_malformed')))
    w.process(u.SerializeToString(), received_at=100.); w.flush_ps_gaps(102.)
    assert L.losses(tmp_path, POOL, 99., 103.) == []
    info.signature = bytes([7])*64
    info.transaction.message.address_table_lookups.add(account_key=bytes([9])*32, writable_indexes=b'\x00')
    w.process(u.SerializeToString(), received_at=104.); w.flush_ps_gaps(106.)
    assert L.losses(tmp_path, POOL, 103., 107.) == [104.]


def test_failed_receipt_commit_cannot_attest_a_pending_legacy_gap(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    monkeypatch.setattr(PS, 'events', lambda _: (_ for _ in ()).throw(PS.DecodeLoss('pumpswap_logs_incomplete')))
    w.process(frame(), received_at=100.)
    w.ps_storage_error(sqlite3.OperationalError('disk'))
    w.flush_ps_gaps(102.)
    c = w.files.conn('ps_loss_batches', 102.)
    assert c.execute('SELECT * FROM ps_loss_batches').fetchall() == []
    assert c.execute('SELECT * FROM ps_loss_attribution').fetchall() == []
    assert L.losses(tmp_path, POOL, 99., 103.) == [100.]


def test_pending_first_receipt_before_lo_can_hide_a_later_coalesced_loss(tmp_path):
    files,c=db(tmp_path);c.commit()
    assert L.losses(tmp_path,POOL,100.,102.,pending={'decode_loss':99.}) == [100.]
    c.close()


def test_giant_pending_integer_is_a_typed_refusal(tmp_path):
    files,c=db(tmp_path);c.commit()
    with pytest.raises(L.LossUnreadable,match='pending'):
        L.losses(tmp_path,POOL,100.,102.,pending={'decode_loss':10**1000})
    c.close()
