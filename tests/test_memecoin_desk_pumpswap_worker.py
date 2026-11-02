"""Pool tape boundary: offline synthetic frames and separate temporary SQLite files."""
import importlib.util
import json
import time
from pathlib import Path

from sniper.feeds.yellowstone import geyser_pb2 as pb
from sniper.services import memecoin_desk_pumpswap as P
from sniper.services import memecoin_desk_universe as U

spec = importlib.util.spec_from_file_location('ps_worker', Path(__file__).resolve().parents[1] / 'scripts/memecoin_desk_universe_worker.py')
W = importlib.util.module_from_spec(spec)
spec.loader.exec_module(W)


def worker(path, monkeypatch, enabled=True, filt=False):
    monkeypatch.setenv('MEMECOIN_DESK_PUMPSWAP_CAPTURE', '1' if enabled else '0')
    monkeypatch.setenv('MEMECOIN_DESK_PUMPSWAP_FILTER', '1' if filt else '0')    # the write filter has its own tests
    return W.Worker(W.knobs({}), base=path, sol_price_fn=lambda: None, alert_fn=lambda *a: None)


def event(kind='buy', mint='mint'):
    e = dict(kind=kind, pool='pool', actor='actor', event_time=1, payload={}, payload_b64='raw',
             base_atomic=str(2**64-1), quote_atomic='0', pre_base_reserve_atomic=str(2**64-1),
             pre_quote_reserve_atomic='0', post_base_reserve_atomic=None, post_quote_reserve_atomic=None,
             lp_fee_bps=20, protocol_fee_bps=5, creator_fee_bps=0, cashback_fee_bps=None, buyback_fee_bps=None,
             coin_creator='creator')
    if kind == 'create_pool':
        e.update(base_mint=mint, quote_mint=P.WSOL_MINT, pool_creator='authority', canonical=True)
    return e


def frame(*, failed=False, names=('pumpswap',)):
    u = pb.SubscribeUpdate()
    u.filters.extend(names)
    u.transaction.slot = 10
    u.transaction.transaction.signature = b'\x01'*64
    u.transaction.transaction.index = 3
    u.transaction.transaction.meta.log_messages.extend([f'Program {P.AMM_PROGRAM} invoke [1]',f'Program {P.AMM_PROGRAM} success'])
    if failed:
        u.transaction.transaction.meta.err.err = b'failed'
    return u.SerializeToString()


def test_capture_default_off_has_no_pool_stream(tmp_path, monkeypatch):
    monkeypatch.delenv('MEMECOIN_DESK_PUMPSWAP_CAPTURE', raising=False)
    w = W.Worker(W.knobs({}), base=tmp_path, sol_price_fn=lambda: None, alert_fn=lambda *a: None)
    monkeypatch.setattr(P, 'events', lambda _: [(1, event())])
    w.process(frame())
    assert not list(tmp_path.glob('swaps_ps/*.db'))


def test_unmapped_uint64_trade_retained_as_text_with_receipt_clock(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    monkeypatch.setattr(P, 'events', lambda _: [(1, event())])
    w.process(frame(), received_at=100.25)
    c = w.files.conn('swaps_ps', time.time())
    r = c.execute('SELECT mint,base_atomic,quote_atomic,received_at,processed_at,tx_index FROM swaps_ps').fetchone()
    assert r[:4] == (None, str(2**64-1), '0', 100.25)
    assert r[4] >= 100.25 and r[5] == 3
    assert c.execute('SELECT typeof(base_atomic),typeof(quote_atomic) FROM swaps_ps').fetchone() == ('text','text')
    assert w.stats['ps_unmapped'] == 1
    assert not list(tmp_path.glob('candidate_emissions_ps/*.jsonl'))


def test_map_causal_no_retroactive_patch_duplicate_and_conflict(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    events = [(1, event()), (2, event('create_pool')), (3, event())]
    monkeypatch.setattr(P, 'events', lambda _: events)
    raw = frame(names=('pump','pumpswap'))
    w.process(raw, received_at=100)
    w.process(raw, received_at=101)
    c = w.files.conn('swaps_ps', time.time())
    assert c.execute('SELECT ordinal,mint FROM swaps_ps ORDER BY ordinal').fetchall() == [(1,None),(3,'mint')]
    assert w.stats['ps_swaps'] == 2
    assert w.stats['ps_duplicate'] == 3
    monkeypatch.setattr(P, 'events', lambda _: [(4,event('create_pool','other')), (5,event())])
    w.process(raw, received_at=102)
    assert c.execute('SELECT mint,map_status FROM swaps_ps WHERE ordinal=5').fetchone() == (None,'conflict')


def test_failed_transaction_never_maps_or_captures(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    monkeypatch.setattr(P, 'events', lambda _: [(1,event('create_pool')),(2,event())])
    w.process(frame(failed=True), received_at=100)
    assert not list(tmp_path.glob('pools/*.db')) and not list(tmp_path.glob('swaps_ps/*.db'))
    assert w.stats['ps_failed_tx'] == 1


def test_map_reload_after_restart_and_day_rollover(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    monkeypatch.setattr(P, 'events', lambda _: [(1,event('create_pool'))])
    monkeypatch.setattr(W.time, 'time', lambda: 1700000000.)
    w.process(frame(), received_at=1700000000.)
    w.files.commit()
    w2 = worker(tmp_path, monkeypatch)
    monkeypatch.setattr(W.time, 'time', lambda: 1700086400.)
    monkeypatch.setattr(P, 'events', lambda _: [(2,event())])
    w2.process(frame(), received_at=1700086400.)
    r = w2.files.conn('swaps_ps',1700086400.).execute('SELECT mint,map_known_at FROM swaps_ps').fetchone()
    assert r == ('mint',1700000000.)


def test_ps_ring_is_separate_bounded_and_cannot_drop_curve_frames(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    w.ring_cap = 100
    curve = b'curve'
    w.push(curve, received_at=10)
    for n in range(5):
        w.push(b'x'*90, received_at=11+n, pumpswap=True)
    assert w.ring_bytes == len(curve) and len(w.ring) == 1
    assert w.ps_ring_bytes <= 100 and w.stats['ps_ring_dropped'] == 4
    assert w.stats['ring_dropped'] == 0
    assert not list(tmp_path.glob('gaps/*.db'))
    assert not (tmp_path/'pumpswap'/'tape.db').exists()
    w.flush_ps_gaps(time.time())
    assert (tmp_path/'pumpswap'/'tape.db').exists()


def test_later_source_map_cannot_identify_an_earlier_swap(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    monkeypatch.setattr(P, 'events', lambda _: [(1,event('create_pool'))])
    u = pb.SubscribeUpdate.FromString(frame())
    u.transaction.slot = 20
    w.process(u.SerializeToString(), received_at=100)
    monkeypatch.setattr(P, 'events', lambda _: [(2,event())])
    u.transaction.slot = 10
    u.transaction.transaction.signature = b'\x02'*64
    w.process(u.SerializeToString(), received_at=101)
    assert w.files.conn('swaps_ps',time.time()).execute('SELECT mint FROM swaps_ps').fetchone() == (None,)


def test_ps_control_heartbeat_and_restart_gaps_report_separate_tape(tmp_path, monkeypatch):
    w = worker(tmp_path,monkeypatch)
    w.gap('worker_start')
    w.commit_files()
    assert w.files.conn('gaps_ps',time.time()).execute('SELECT cause FROM gaps_ps').fetchall() == [('worker_start',)]
    w.heartbeat(True)
    hb = json.loads((tmp_path/'universe.heartbeat').read_text())
    assert hb['pumpswap_capture'] is True and hb['ps_ring_bytes'] == 0


def test_pool_tape_has_explicit_chain_venue_and_program(tmp_path,monkeypatch):
    w=worker(tmp_path,monkeypatch)
    monkeypatch.setattr(P,'events',lambda _: [(1,event('create_pool')),(2,event())])
    w.process(frame())
    for stream in ('pools','swaps_ps'):
        assert w.files.conn(stream,time.time()).execute(f'SELECT network_id,venue,program FROM {stream}').fetchone() == ('solana','pumpswap',P.AMM_PROGRAM)


def test_reader_with_both_ps_knobs_off_never_scans_filters(tmp_path,monkeypatch):
    import asyncio
    from types import SimpleNamespace
    monkeypatch.setenv('MEMECOIN_DESK_PUMPSWAP_CENSUS','0')
    w=worker(tmp_path,monkeypatch,enabled=False)
    monkeypatch.setattr(w,'census_frame',lambda _: (_ for _ in ()).throw(AssertionError('unnecessary filter parsing')))
    def push(raw,**kwargs):
        w.stop=True
    monkeypatch.setattr(w,'push',push)
    async def responses(*args,**kwargs):
        yield frame(names=('pump',))
    class Channel:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        def stream_stream(self,*args,**kwargs): return responses
    import sys
    monkeypatch.setitem(sys.modules,'grpc',SimpleNamespace(aio=SimpleNamespace(secure_channel=lambda *a,**k:Channel()),ssl_channel_credentials=lambda:None))
    asyncio.run(W.run(w,'offline.invalid','fake'))


def test_pool_failure_is_a_named_ps_gap_and_does_not_abort_curve_processing(tmp_path,monkeypatch):
    w=worker(tmp_path,monkeypatch)
    def failed(_): raise RuntimeError('offline decoder failure')
    monkeypatch.setattr(P,'events',failed)
    w.process(frame(names=('pump','pumpswap')))
    assert w.stats['ps_process_errors'] == 1
    w.commit_files()
    assert w.files.conn('gaps_ps',time.time()).execute('SELECT cause FROM gaps_ps').fetchall() == [('process_error',)]
    assert not list(tmp_path.glob('gaps/*.db'))


def test_ps_emission_requires_explicit_band_and_capture(tmp_path,monkeypatch):
    import pytest
    monkeypatch.setenv('MEMECOIN_DESK_PUMPSWAP_EMISSIONS','1')
    monkeypatch.setenv('MEMECOIN_DESK_PUMPSWAP_CAPTURE','0')
    with pytest.raises(ValueError,match='capture'):
        W.Worker(W.knobs({}),base=tmp_path,sol_price_fn=lambda:None,alert_fn=lambda *a:None)
    monkeypatch.setenv('MEMECOIN_DESK_PUMPSWAP_CAPTURE','1')
    monkeypatch.delenv('MEMECOIN_DESK_PUMPSWAP_BAND_LO_USD',raising=False)
    monkeypatch.delenv('MEMECOIN_DESK_PUMPSWAP_BAND_HI_USD',raising=False)
    with pytest.raises(ValueError,match='band'):
        W.Worker(W.knobs({}),base=tmp_path,sol_price_fn=lambda:None,alert_fn=lambda *a:None)


def test_offline_complete_pool_crossing_is_separate_and_unknown_metadata_stays_capture_only(tmp_path,monkeypatch):
    from sniper.services import memecoin_desk_contract as K, memecoin_desk_safety as S
    monkeypatch.setenv('MEMECOIN_DESK_PUMPSWAP_EMISSIONS','1')
    monkeypatch.setenv('MEMECOIN_DESK_PUMPSWAP_CAPTURE','1')
    monkeypatch.setenv('MEMECOIN_DESK_PUMPSWAP_BAND_LO_USD','30000')
    monkeypatch.setenv('MEMECOIN_DESK_PUMPSWAP_BAND_HI_USD','100000')
    monkeypatch.setattr(W.time,'time',lambda:1700000000.)
    mint=U.b58encode(b'\x01'*32)
    pool=S.pumpswap_pool_address(mint)
    facts=dict(pool=pool,mint=mint,quote_mint=P.WSOL_MINT,base_decimals=6,supply_atomic=str(10**15),virtual_quote_atomic='0',known_at=1699999999.,slot=1,source='offline_fixture')
    def play(w):
        w.sol_usd,w.sol_at=150.,1700000000.
        for n,cap in enumerate((None,20000,40000),1):
            e=event('create_pool',mint) if cap is None else event()
            e['pool']=pool
            if cap:
                e.update(post_base_reserve_atomic=str(10**15),post_quote_reserve_atomic=str(round(cap/150*1e9)),cashback_fee_bps=0,virtual_quote_atomic='0',virtual_quote_source='pumpswap_grpc:buy_event_virtual_quote')
            monkeypatch.setattr(P,'events',lambda _,e=e:[(n,e)])
            u=pb.SubscribeUpdate.FromString(frame())
            u.transaction.slot=n
            u.transaction.transaction.signature=bytes([n])*64
            w.process(u.SerializeToString(),received_at=1700000000.)
    w=W.Worker(W.knobs({}),base=tmp_path/'known',sol_price_fn=lambda:None,alert_fn=lambda *a:None,pool_metadata_fn=lambda _:facts)
    play(w)
    rows=list((tmp_path/'known'/'candidate_emissions_ps').glob('*.jsonl'))
    cand=json.loads(rows[0].read_text())
    assert cand['venue']=='pumpswap' and K.validate_candidate(cand)
    assert S.verify_u_emission(cand,now=1700000000.,max_age_s=2)
    assert not list((tmp_path/'known'/'candidate_emissions').glob('*.jsonl'))
    unknown=W.Worker(W.knobs({}),base=tmp_path/'unknown',sol_price_fn=lambda:None,alert_fn=lambda *a:None)
    play(unknown)
    assert not list((tmp_path/'unknown'/'candidate_emissions_ps').glob('*.jsonl'))
    assert unknown.stats['ps_pricing_unknown'] == 2
    rows=unknown.files.conn('pricing_ps',1700000000.).execute('SELECT reason,COUNT(*) FROM pricing_ps GROUP BY reason').fetchall()
    assert rows==[('metadata_not_yet_known',2)]
    w.gap('restart')
    assert not w.ps_book._inside


def test_day_rollover_commits_pending_rows_before_close(tmp_path):
    import sqlite3
    files=W.DayFiles(tmp_path)
    old=1700000000.
    files.conn('gaps',old).execute("INSERT INTO gaps VALUES(1,'pending',1)")
    files.conn('gaps',old+86400.)
    c=sqlite3.connect(tmp_path/'gaps'/f'{W.utc_day(old)}.db')
    assert c.execute('SELECT cause FROM gaps').fetchall() == [('pending',)]
    c.close()


def test_ps_global_identity_survives_midnight_replay_without_advancing_state(tmp_path,monkeypatch):
    w=worker(tmp_path,monkeypatch)
    monkeypatch.setattr(P,'events',lambda _: [(1,event())])
    monkeypatch.setattr(W.time,'time',lambda:1700000000.)
    w.process(frame(),received_at=1700000000.)
    w.files.commit()
    monkeypatch.setattr(W.time,'time',lambda:1700086400.)
    w.process(frame(),received_at=1700086400.)
    c=w.files.conn('swaps_ps',1700086400.)
    assert c.execute('SELECT COUNT(*),MIN(received_at) FROM swaps_ps').fetchone() == (1,1700000000.)
    assert w.stats['ps_swaps']==1 and w.stats['ps_duplicate']==1
    assert list((tmp_path/'pumpswap').glob('*.db')) == [tmp_path/'pumpswap'/'tape.db']


def test_old_unreleased_daily_ps_layout_refuses_when_enabled_only(tmp_path,monkeypatch):
    import pytest
    (tmp_path/'pools').mkdir()
    (tmp_path/'pools'/'2026-10-21.db').touch()
    with pytest.raises(ValueError,match='legacy PumpSwap'):
        worker(tmp_path,monkeypatch)
    worker(tmp_path,monkeypatch,enabled=False)


def test_ps_overflow_defers_and_coalesces_sqlite_off_reader(tmp_path,monkeypatch):
    w=worker(tmp_path,monkeypatch)
    w.ring_cap=100
    for n in range(100):
        w.push(b'x'*90,received_at=100+n/1000,pumpswap=True)
    assert w.stats['ps_ring_dropped']==99
    assert not (tmp_path/'pumpswap'/'tape.db').exists()
    w.flush_ps_gaps(101.)
    w.flush_ps_gaps(101.1)
    assert w.files.conn('gaps_ps',101.).execute('SELECT cause FROM gaps_ps').fetchall()==[('ring_overflow',)]


def test_decoder_loss_invalidates_pool_book_and_records_named_gap(tmp_path,monkeypatch):
    w=worker(tmp_path,monkeypatch)
    class Book:
        def mark_unknown(self,reason): self.reason=reason
    book=w.ps_book=Book()
    monkeypatch.setattr(P,'events',lambda _: (_ for _ in ()).throw(P.DecodeLoss('pumpswap_logs_incomplete')))
    w.process(frame())
    assert w.stats['ps_decode_loss']==1 and book.reason=='decode_loss'
    w.commit_files()
    assert w.files.conn('gaps_ps',time.time()).execute('SELECT cause FROM gaps_ps').fetchall()==[('decode_loss',)]


def test_disk_cap_names_and_commits_ps_gap_before_stopping(tmp_path,monkeypatch):
    import sqlite3
    from types import SimpleNamespace
    w=worker(tmp_path,monkeypatch)
    monkeypatch.setattr(W,'desk_bytes',lambda *a:21e9)
    monkeypatch.setattr(W.shutil,'disk_usage',lambda *a:SimpleNamespace(free=100e9))
    w.check_disk()
    assert w.stop
    c=sqlite3.connect(tmp_path/'pumpswap'/'tape.db')
    assert c.execute('SELECT cause FROM gaps_ps').fetchall()==[('disk_cap',)]
    c.close()


def test_locked_ps_writer_cannot_abort_curve_commit_or_disk_stop_and_gap_recovers(tmp_path,monkeypatch):
    import sqlite3
    from types import SimpleNamespace
    w=worker(tmp_path,monkeypatch)
    w.files.conn('pools',time.time())
    w.files.commit()
    lock=sqlite3.connect(tmp_path/'pumpswap'/'tape.db')
    lock.execute('BEGIN IMMEDIATE')
    monkeypatch.setattr(P,'events',lambda _: [(1,event())])
    try:
        w.process(frame())
        assert w.ps_pending_gaps and w.stats['ps_write_errors']>=1
        w.files.conn('gaps',time.time()).execute("INSERT INTO gaps VALUES(1,'curve_ok',1)")
        w.commit_files()
        c=sqlite3.connect(tmp_path/'gaps'/f'{W.utc_day(time.time())}.db')
        assert c.execute('SELECT cause FROM gaps').fetchall()==[('curve_ok',)]
        c.close()
        monkeypatch.setattr(W,'desk_bytes',lambda *a:21e9)
        monkeypatch.setattr(W.shutil,'disk_usage',lambda *a:SimpleNamespace(free=100e9))
        w.check_disk()
        assert w.stop and w.ps_pending_gaps
        w.heartbeat(False)
        assert json.loads((tmp_path/'universe.heartbeat').read_text())['ps_gap_pending'] is True
    finally:
        lock.rollback();lock.close()
    w.flush_ps_gaps(time.time()+2)
    assert not w.ps_pending_gaps
    assert w.files.conn('gaps_ps',time.time()).execute("SELECT COUNT(*) FROM gaps_ps WHERE cause='write_error'").fetchone()==(1,)


MINT = P.WSOL_MINT                      # any valid 32-byte key works as a mint for the PDA derivation


def _swap_event(mint=MINT):
    from sniper.services import memecoin_desk_safety as S
    e = event()
    e['pool'] = S.pumpswap_pool_address(mint)
    return e


def _frame_at(slot, sig_byte=1, index=3):
    u = pb.SubscribeUpdate.FromString(frame())
    u.transaction.slot = slot
    u.transaction.transaction.signature = bytes([sig_byte]) * 64
    u.transaction.transaction.index = index
    return u.SerializeToString()


def test_curve_side_graduation_maps_later_swaps_as_a_separate_derived_subgroup(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    ev = _swap_event()
    monkeypatch.setattr(P, 'events', lambda _: [(1, ev)])
    # the curve stream's migration event: slot 100, sig 0x09, tx_index 5, receipt 1000
    w.note_graduation(MINT, 'migration', 100, 'S' * 64, 0, 5, None, 1000.0, 1000.0)
    assert w.stats['ps_grads'] == 1
    for slot, sig_byte, index, at, want in ((99, 2, 1, 1001.0, 'unmapped'),            # earlier slot
                                           (100, 3, 4, 1001.0, 'unmapped'),            # same slot, EARLIER tx
                                           (100, 4, None, 1001.0, 'unmapped'),         # same slot, tx_index unproven
                                           (100, 5, 6, 1001.0, 'mapped_curve_derived'),  # same slot, later tx
                                           (101, 6, 0, 1001.0, 'mapped_curve_derived')):  # later slot
        w.process(_frame_at(slot, sig_byte, index or 0), received_at=at)
    rows = w.files.conn('swaps_ps', time.time()).execute(
        'SELECT slot, mint, map_status FROM swaps_ps ORDER BY slot, signature').fetchall()
    assert rows == [(99, None, 'unmapped'), (100, None, 'unmapped'), (100, None, 'unmapped'),
                    (100, MINT, 'mapped_curve_derived'), (101, MINT, 'mapped_curve_derived')]
    assert w.files.conn('pools', time.time()).execute('SELECT COUNT(*) FROM pools').fetchone() == (0,)   # not a captured pool
    pool = ev['pool']
    assert w.files.conn('ps_keep', time.time()).execute('SELECT COUNT(*) FROM ps_keep WHERE pool=?', (pool,)).fetchone() == (1,)


def test_a_captured_create_pool_wins_and_a_derived_row_for_another_mint_conflicts(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    ev = _swap_event()
    create = event('create_pool', MINT)
    create['pool'] = ev['pool']
    monkeypatch.setattr(P, 'events', lambda _: [(1, create)])
    w.process(_frame_at(50, 1, 1), received_at=900.0)                      # captured create_pool, slot 50
    w.note_graduation(MINT, 'complete', 50, 'T' * 64, 0, 2, None, 901.0, time.time() + 5)   # same identity, LATER fact (known_at)
    monkeypatch.setattr(P, 'events', lambda _: [(2, ev)])
    w.process(_frame_at(60, 2, 1), received_at=1000.0)
    c = w.files.conn('swaps_ps', time.time())
    assert c.execute('SELECT mint, map_status FROM swaps_ps').fetchone() == (MINT, 'mapped')   # earlier fact, captured source
    # a different mint claiming the same pool address is an identity conflict, never silently mapped
    w.ps_bindings.clear()
    w.files.conn('ps_grads', time.time()).execute(
        "INSERT INTO ps_grads(pool,mint,quote_mint,kind,slot,signature,ordinal,tx_index,block_time,received_at,known_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (ev['pool'], 'OtherMint', P.WSOL_MINT, 'complete', 55, 'U' * 64, 0, 1, None, 950.0, time.time() - 1))
    monkeypatch.setattr(P, 'events', lambda _: [(3, ev)])
    w.process(_frame_at(70, 3, 1), received_at=1100.0)
    assert c.execute("SELECT mint, map_status FROM swaps_ps WHERE slot=70").fetchone() == (None, 'conflict')


def test_a_decode_loss_names_the_lost_transaction(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    monkeypatch.setattr(P, 'events', lambda _: (_ for _ in ()).throw(P.DecodeLoss('pumpswap_virtual_quote_unproven')))
    w.process(_frame_at(77, 7, 2), received_at=500.0)
    w.process(_frame_at(77, 7, 2), received_at=501.0)                      # the same tx twice: one row
    rows = w.files.conn('ps_decode_losses', time.time()).execute(
        'SELECT cause, slot, received_at FROM ps_decode_losses').fetchall()
    assert rows == [('virtual_quote_unproven', 77, 500.0)]


def test_the_keep_window_only_widens_when_a_pool_gets_a_second_anchor(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    ev = _swap_event()
    w.note_graduation(MINT, 'complete', 100, 'S' * 64, 0, 5, None, 1000.0, 1000.0)           # curve side first
    create = event('create_pool', MINT)
    create['pool'] = ev['pool']
    monkeypatch.setattr(P, 'events', lambda _: [(1, create)])
    w.process(_frame_at(110, 2, 1), received_at=1900.0)                                      # create_pool 900 s later
    got = w.files.conn('ps_keep', time.time()).execute('SELECT keep_from, keep_until FROM ps_keep WHERE pool=?', (ev['pool'],)).fetchone()
    assert got == (1000.0 - W.PS_KEEP_BEFORE_S, 1900.0 + W.PS_KEEP_AFTER_S)                  # both anchors covered


def _curve_row(w, mint, quote_mint, real_quote, slot, now):
    c = w.files.conn('swaps', now)
    c.execute("INSERT INTO swaps(slot, signature, ordinal, mint, quote_mint, real_quote) VALUES(?,?,?,?,?,?)",
              (slot, 'sig-%s-%d' % (mint[:6], slot), 0, mint, quote_mint, real_quote))


def test_a_graduation_is_classed_from_the_mints_last_curve_print_before_the_event(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    now = time.time()
    SOL = W.SOL_QUOTE
    mints = {'a' * 1: P.WSOL_MINT}
    cases = {}
    from solders.pubkey import Pubkey
    keys = [str(Pubkey.from_bytes(bytes([i]) * 32)) for i in range(1, 6)]
    full, small, tok, none_q, nocurve = keys
    _curve_row(w, full, SOL, 70_000_000_000, 1, now)
    _curve_row(w, full, SOL, 85_005_359_925, 2, now)                  # the LAST print before the event decides
    _curve_row(w, small, SOL, 30_000_000, 1, now)
    _curve_row(w, tok, 'TTWofwUSD1', 0, 1, now)                       # another token's curve: legacy real_quote 0
    _curve_row(w, none_q, SOL, None, 1, now)                          # NULL real_quote: a row from before the column
    for i, m in enumerate((full, small, tok, none_q, nocurve)):
        w.note_graduation(m, 'migration', 100 + i, ('S%d' % i) * 40, 0, i + 1, None, now, now)
    _curve_row(w, small, SOL, 85_005_359_925, 200, now)               # a LATER print must never reclassify
    got = dict(w.files.conn('ps_grads', now).execute('SELECT mint, grad_class FROM ps_grads').fetchall())
    assert [got[m] for m in (full, small, tok, none_q, nocurve)] == ['sol85', 'sub_threshold', 'quote_token', 'unknown', 'unknown']
    assert w.stats['ps_grad_no_curve_row'] == 1 and w.stats['ps_grads:sol85'] == 1 and w.stats['ps_grads:unknown'] == 2
    assert W.Worker.grad_class(SOL, W.SOL_CURVE_FULL_LAMPORTS) == 'sol85' and W.Worker.grad_class(SOL, W.SOL_CURVE_FULL_LAMPORTS - 1) == 'sub_threshold'


def test_an_older_ps_grads_table_gains_the_class_columns_in_place(tmp_path):
    import sqlite3 as sq
    path = tmp_path / 'pumpswap' / 'tape.db'
    path.parent.mkdir(parents=True)
    c = sq.connect(path)
    c.execute("CREATE TABLE ps_grads(pool TEXT, mint TEXT, quote_mint TEXT, kind TEXT, slot INTEGER, signature TEXT, ordinal INTEGER, "
              "tx_index INTEGER, block_time INTEGER, received_at REAL, known_at REAL, PRIMARY KEY(signature,ordinal)) WITHOUT ROWID")
    c.execute("INSERT INTO ps_grads VALUES('p','m','q','migration',1,'s',0,1,NULL,1.0,1.0)")
    c.commit()
    c.close()
    files = W.DayFiles(tmp_path)
    got = files.conn('pools', time.time()).execute('SELECT grad_class, curve_real_quote FROM ps_grads').fetchone()
    assert got == (None, None)                                          # old rows: NULL = unknown, never a class


def _sweep(ordinal, bucket, amount='100'):
    return dict(ordinal=ordinal, pool='poolA', base_mint='base', quote_mint=P.WSOL_MINT, recipient='r', payer='p', amount=amount, bucket=bucket, event_time=5)


def test_a_sweep_loss_records_every_event_beside_the_unchanged_loss_rows(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    monkeypatch.setattr(P, 'events', lambda _: (_ for _ in ()).throw(P.DecodeLoss('pumpswap_virtual_quote_unproven')))
    monkeypatch.setattr(P, 'sweep_events', lambda _: [_sweep(7, 0, '11'), _sweep(9, 1, '100')])
    w.process(_frame_at(77, 7, 2), received_at=500.0)
    w.process(_frame_at(77, 7, 2), received_at=501.0)                      # the same tx twice: no duplicate rows
    conn = w.files.conn('ps_decode_losses', time.time())
    assert conn.execute('SELECT cause, slot, received_at FROM ps_decode_losses').fetchall() == [('virtual_quote_unproven', 77, 500.0)]      # the loss is still recorded
    assert conn.execute('SELECT count(*) FROM ps_loss_attribution').fetchone()[0] == 1
    got = conn.execute('SELECT ordinal, pool, amount, bucket, slot, tx_index, received_at, recorder_version FROM ps_sweep_events ORDER BY ordinal').fetchall()
    assert got == [(7, 'poolA', '11', 0, 77, 2, 500.0, P.SWEEP_RECORDER_VERSION), (9, 'poolA', '100', 1, 77, 2, 500.0, P.SWEEP_RECORDER_VERSION)]
    assert w.stats['ps_sweep_events'] == 2 and w.stats['ps_sweep_record_failed'] == 0


def test_a_failing_sweep_record_never_loses_the_loss_rows(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    monkeypatch.setattr(P, 'events', lambda _: (_ for _ in ()).throw(P.DecodeLoss('pumpswap_virtual_quote_unproven')))
    def boom(_): raise RuntimeError('parser down')
    monkeypatch.setattr(P, 'sweep_events', boom)
    w.process(_frame_at(78, 8, 1), received_at=600.0)
    conn = w.files.conn('ps_decode_losses', time.time())
    assert conn.execute('SELECT count(*) FROM ps_decode_losses').fetchone()[0] == 1 and conn.execute('SELECT count(*) FROM ps_loss_attribution').fetchone()[0] == 1
    assert conn.execute('SELECT count(*) FROM ps_sweep_events').fetchone()[0] == 0 and w.stats['ps_sweep_record_failed'] == 1


def test_an_extended_pool_outside_the_intake_book_does_not_fail_the_frame(tmp_path, monkeypatch):
    from types import SimpleNamespace
    w = worker(tmp_path, monkeypatch)
    hooked = []
    w.intake_book = SimpleNamespace(pools={})
    w.intake_hook = lambda *a: hooked.append(a)
    w.ps_extended['pool'] = time.time() + 100
    w.ps_pending_gaps = {}
    second = dict(event(), actor='second')
    monkeypatch.setattr(P, 'events', lambda _: [(1, event()), (2, second)])
    w.process(frame(), received_at=time.time())
    assert w.stats['ps_swaps'] == 2 and len(hooked) == 2 and hooked[0][1] is None      # no invented identity; the next event in the frame is not lost
