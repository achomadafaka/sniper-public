"""Watcher persistence cannot block reception or suppress a rotation's sell leg. Offline SQLite."""
import asyncio
import json
import sqlite3
import threading
import time

import pytest

from scripts.research import onchain_wallet_watch as w
from sniper.persistence import db_memecoin as m
from solders.pubkey import Pubkey


def _params(signature, stamp='2026-10-02T12:00:00Z'):
    return (signature, 'u', 'h', 'owner', 'B', 100.0, 1.0, 1, stamp, 0.5,
            None, None, stamp, 100.0, 1.0, '{}', 100)


def _proven_params(signature):
    params = _params(signature)
    proof = dict(schema_version=1, proof_version='direct-wallet-paid-v1', network_id='solana',
                 tx_hash=signature, block_number=100, beneficiary='owner', payer='owner',
                 mint='B', source_user_id='u', acquired_atomic='100000000', payment_atomic='1000000')
    return (*params[:15], json.dumps({'source_initiation': proof}), params[16])


def _ledger(tmp_path):
    path = tmp_path / 'ledger.db'
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return path, conn


def _wait(predicate, timeout=3):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(.005)
    assert predicate(), 'writer did not settle'


def test_a_lock_does_not_block_reception_and_retry_preserves_the_original_clock(tmp_path, monkeypatch):
    path, conn = _ledger(tmp_path)
    monkeypatch.setattr(w, 'DB', path)
    wakes = []
    def wake(_):
        # Visibility from a separate connection proves wake follows commit.
        with sqlite3.connect(path) as read:
            wakes.append(read.execute('SELECT signature FROM onchain_wallet_detections').fetchall())
    monkeypatch.setattr(w, 'send_wake', wake)
    writer = w.DetectionWriter(path, retry_s=1)
    conn.execute('BEGIN IMMEDIATE')
    try:
        async def reception():
            assert writer.offer(_params('first'), (), {})
            await asyncio.sleep(.02)
            return True
        started = time.monotonic()
        assert asyncio.run(reception())
        assert time.monotonic() - started < .15
        _wait(lambda: writer.stats['retries'] > 0)
        assert wakes == []
        conn.rollback()
        _wait(lambda: writer.stats['written'] == 1)
        row = conn.execute('SELECT * FROM onchain_wallet_detections').fetchone()
        assert (row['signature'], row['detected_at'], row['source_slot']) == ('first', '2026-10-02T12:00:00Z', 100)
        assert wakes == [[('first',)]]
        assert writer.offer(_params('first'), (), {})
        _wait(lambda: writer.stats['duplicates'] == 1)
        assert len(wakes) == 1
    finally:
        conn.rollback()
        writer.close()
        conn.close()


def test_overflow_and_exhausted_retry_are_counted_without_waking(tmp_path, monkeypatch):
    path, conn = _ledger(tmp_path)
    monkeypatch.setattr(w, 'DB', path)
    wakes = []
    monkeypatch.setattr(w, 'send_wake', lambda _: wakes.append(1))
    writer = w.DetectionWriter(path, maxsize=1, retry_s=.2)
    conn.execute('BEGIN IMMEDIATE')
    try:
        assert writer.offer(_params('one'), (), {})
        _wait(lambda: writer.stats['retries'] > 0)
        assert writer.offer(_params('two'), (), {})
        assert not writer.offer(_params('overflow'), (), {})
        assert writer.stats['overflow'] == 1
        _wait(lambda: writer.stats['retry_exhausted'] == 2)
        assert wakes == [] and conn.execute('SELECT COUNT(*) FROM onchain_wallet_detections').fetchone()[0] == 0
    finally:
        conn.rollback()
        writer.close()
        conn.close()


def test_a_rotation_records_its_buy_and_sell_with_the_same_verified_identity(tmp_path, monkeypatch):
    path, conn = _ledger(tmp_path)
    monkeypatch.setattr(w, 'DB', path)
    owner, pool = str(Pubkey.new_unique()), str(Pubkey.new_unique())
    def row(who, mint, amount):
        return {'owner': who, 'mint': mint, 'accountIndex': 0,
                'uiTokenAmount': {'amount': str(amount * 10**6), 'uiAmount': amount, 'decimals': 6}}
    tx = {'slot': 100, 'blockTime': int(time.time()),
          'transaction': {'signatures': ['rotation'], 'message': {'accountKeys': [owner, pool]}},
          'meta': {'err': None, 'fee': 5000, 'preBalances': [2_000_000_000, 2_000_000_000],
                   'postBalances': [999_995_000, 2_000_000_000],
                   'preTokenBalances': [row(owner, 'A', 100), row(owner, w.USDC, 0), row(pool, w.USDC, 50)],
                   'postTokenBalances': [row(owner, 'A', 0), row(owner, 'B', 100), row(owner, w.USDC, 50),
                                         row(pool, 'A', 100), row(pool, w.USDC, 0)]}}
    monkeypatch.setattr(w, '_http_rpc', lambda *a: tx)
    monkeypatch.setattr(w, '_stage_write', lambda _: None)
    monkeypatch.setattr(w, 'send_wake', lambda _: None)
    asyncio.run(w.process_signature('rotation', owner, {'uid': 'u', 'handle': 'h', 'status': 'armed'}, conn))
    buy = conn.execute('SELECT signature,mint FROM onchain_wallet_detections').fetchone()
    sell = conn.execute('SELECT signature,mint,sold_atomic FROM onchain_wallet_sells_v2').fetchone()
    assert tuple(buy) == ('rotation', 'B')
    assert sell is not None and tuple(sell) == ('rotation', 'A', 100000000)
    conn.close()


def test_writer_failure_is_visible_and_shutdown_counts_queued_loss(tmp_path, monkeypatch):
    path, conn = _ledger(tmp_path)
    monkeypatch.setattr(w, 'DB', path)
    monkeypatch.setattr(w, 'send_wake', lambda _: None)
    writer = w.DetectionWriter(path, maxsize=2, retry_s=1)
    # Wrong dependency data is counted, never a silently dead worker.
    assert writer.offer(('wrong tuple',), (), {})
    _wait(lambda: writer.stats['errors'] == 1)
    assert writer.stats['alive']
    conn.execute('BEGIN IMMEDIATE')
    assert writer.offer(_params('blocked'), (), {})
    _wait(lambda: writer.stats['retries'] > 0)
    assert writer.offer(_params('queued'), (), {})
    writer.close()
    conn.rollback(); conn.close()
    assert not writer.stats['alive'] and writer.stats['shutdown_dropped'] == 2


def test_a_failing_log_line_never_kills_the_writer(tmp_path, monkeypatch):
    """Gamma INFO on SOL-3: the error handlers' print raised on a broken stdout, ended _run and left
    the writer dead while the watcher kept receiving."""
    path, conn = _ledger(tmp_path)
    monkeypatch.setattr(w, 'DB', path)
    monkeypatch.setattr(w, 'send_wake', lambda _: None)

    def broken(*_a, **_k):
        raise BrokenPipeError(32, 'Broken pipe')
    monkeypatch.setattr(w, 'print', broken, raising=False)
    writer = w.DetectionWriter(path, retry_s=1)
    try:
        assert writer.offer(('wrong tuple',), (), {})
        _wait(lambda: writer.stats['errors'] == 1)
        assert writer.offer(_params('after'), (), {})
        _wait(lambda: writer.stats['written'] == 1)
        assert writer.stats['alive']
    finally:
        writer.close()
        conn.close()


def test_a_connection_that_cannot_roll_back_is_replaced_not_fatal(tmp_path, monkeypatch):
    """Gamma INFO on 5d5c00024: a rollback that raises inside the error handler ended _run."""
    path, conn = _ledger(tmp_path)
    monkeypatch.setattr(w, 'DB', path)
    monkeypatch.setattr(w, 'send_wake', lambda _: None)
    real_connect, opened = w.sqlite3.connect, []

    class Broken:
        def __init__(self, inner):
            self.inner = inner

        def rollback(self):
            raise w.sqlite3.ProgrammingError('Cannot operate on a closed database.')

        def __getattr__(self, name):
            return getattr(self.inner, name)

    def connect(*a, **k):
        c = real_connect(*a, **k)
        opened.append(c)
        return Broken(c) if len(opened) == 1 else c
    monkeypatch.setattr(w.sqlite3, 'connect', connect)
    calls = []

    def persist(c, params, sells, stage, readonly=None):
        calls.append(c)
        if len(calls) == 1:
            raise RuntimeError('write failed')
        return True
    monkeypatch.setattr(w, '_persist_observation', persist)
    writer = w.DetectionWriter(path, retry_s=1)
    try:
        assert writer.offer(_params('one'), (), {})
        _wait(lambda: writer.stats['errors'] == 1)
        assert writer.offer(_params('two'), (), {})
        _wait(lambda: writer.stats['written'] == 1)
        assert writer.stats['alive'] and calls[1] is not calls[0]      # a fresh connection
    finally:
        writer.close()
        conn.close()


def test_every_offered_job_lands_in_exactly_one_counter(tmp_path, monkeypatch):
    """Gamma, live SOL-3 (2026-10-06 22:57Z): writer_shutdown read offered 13, written 10 and
    nothing lost. The 3 were sell-only jobs, committed but counted nowhere."""
    path, conn = _ledger(tmp_path)
    monkeypatch.setattr(w, 'DB', path)
    monkeypatch.setattr(w, 'send_wake', lambda _: None)
    monkeypatch.setattr(w, 'record_onchain_sell', lambda c, **sale: None)
    writer = w.DetectionWriter(path, retry_s=1)
    try:
        assert writer.offer(_params('buy'), (), {})
        assert writer.offer(None, ({'signature': 'sell'},), {})
        assert writer.offer(_params('buy'), (), {})                # a duplicate
        _wait(lambda: writer.stats['written'] + writer.stats['duplicates'] + writer.stats['sells_only'] == 3)
    finally:
        writer.close()
        conn.close()
    s = writer.stats
    outcomes = ('written', 'duplicates', 'sells_only', 'errors', 'retry_exhausted', 'shutdown_dropped')
    assert s['offered'] == sum(s[k] for k in outcomes) and s['sells_only'] == 1


def test_persist_ack_waits_for_commit_and_accepts_exact_durable_duplicate(tmp_path, monkeypatch):
    path, conn = _ledger(tmp_path)
    monkeypatch.setattr(w, 'DB', path)
    monkeypatch.setattr(w, 'send_wake', lambda _: None)
    acknowledgements = []
    def acknowledge(durable):
        with sqlite3.connect(path) as read:
            rows = read.execute('SELECT signature,address,source_user_id,mint,source_slot '
                                'FROM onchain_wallet_detections').fetchall()
        acknowledgements.append((durable, rows, threading.get_ident()))
    writer = w.DetectionWriter(path, retry_s=1)
    conn.execute('BEGIN IMMEDIATE')
    try:
        assert writer.offer(_proven_params('proved'), (), {}, on_persisted=acknowledge)
        _wait(lambda: writer.stats['retries'] > 0)
        assert acknowledgements == []
        conn.rollback()
        _wait(lambda: len(acknowledgements) == 1)
        assert acknowledgements[0][:2] == (True, [('proved', 'owner', 'u', 'B', 100)])
        assert acknowledgements[0][2] != threading.get_ident()
        assert writer.offer(_proven_params('proved'), (), {}, on_persisted=acknowledge)
        _wait(lambda: len(acknowledgements) == 2)
        assert acknowledgements[1][:2] == (True, [('proved', 'owner', 'u', 'B', 100)])
        assert writer.stats['duplicates'] == 1
    finally:
        conn.rollback(); writer.close(); conn.close()


@pytest.mark.parametrize('conflict', ['owner', 'uid', 'mint', 'slot', 'proof', 'unproved'])
def test_duplicate_ack_requires_same_source_identity_and_proof(tmp_path, monkeypatch, conflict):
    path, conn = _ledger(tmp_path)
    monkeypatch.setattr(w, 'DB', path)
    monkeypatch.setattr(w, 'send_wake', lambda _: None)
    original = list(_proven_params('duplicate'))
    if conflict in ('owner', 'uid', 'mint', 'slot'):
        index = {'owner': 3, 'uid': 1, 'mint': 4, 'slot': 16}[conflict]
        original[index] = 101 if conflict == 'slot' else 'different'
    elif conflict == 'unproved':
        original[15] = '{}'
    else:
        facts = json.loads(original[15]); facts['source_initiation']['payment_atomic'] = '2'
        original[15] = json.dumps(facts)
    conn.execute(w._DETECTION_INSERT, original); conn.commit()
    acknowledgements = []
    writer = w.DetectionWriter(path)
    try:
        assert writer.offer(_proven_params('duplicate'), (), {}, on_persisted=acknowledgements.append)
        _wait(lambda: acknowledgements)
        assert acknowledgements == [False]
        row = conn.execute('SELECT address,source_user_id,mint,source_slot,source_valuation_json '
                           'FROM onchain_wallet_detections').fetchone()
        assert tuple(row) == (original[3], original[1], original[4], original[16], original[15])
    finally:
        writer.close(); conn.close()


@pytest.mark.parametrize('failure', ['error', 'retry_exhausted'])
def test_terminal_write_failure_acknowledges_false_once(tmp_path, monkeypatch, failure):
    path, conn = _ledger(tmp_path)
    monkeypatch.setattr(w, 'DB', path)
    monkeypatch.setattr(w, 'send_wake', lambda _: None)
    acknowledgements = []
    writer = w.DetectionWriter(path, retry_s=.1)
    if failure == 'retry_exhausted':
        conn.execute('BEGIN IMMEDIATE')
    try:
        params = ('wrong tuple',) if failure == 'error' else _proven_params('failed')
        assert writer.offer(params, (), {}, on_persisted=acknowledgements.append)
        _wait(lambda: acknowledgements)
        assert acknowledgements == [False]
        assert writer.stats['errors' if failure == 'error' else failure] == 1
        assert conn.execute('SELECT COUNT(*) FROM onchain_wallet_detections').fetchone()[0] == 0
    finally:
        conn.rollback(); writer.close(); conn.close()


def test_shutdown_and_refused_offers_acknowledge_false_without_loss(tmp_path, monkeypatch):
    path, conn = _ledger(tmp_path)
    monkeypatch.setattr(w, 'DB', path)
    monkeypatch.setattr(w, 'send_wake', lambda _: None)
    acknowledgements = []
    writer = w.DetectionWriter(path, maxsize=1, retry_s=1)
    conn.execute('BEGIN IMMEDIATE')
    try:
        assert writer.offer(_proven_params('inflight'), (), {}, on_persisted=acknowledgements.append)
        _wait(lambda: writer.stats['retries'] > 0)
        assert writer.offer(_proven_params('queued'), (), {}, on_persisted=acknowledgements.append)
        assert not writer.offer(_proven_params('overflow'), (), {}, on_persisted=acknowledgements.append)
        assert acknowledgements == [False]
        writer.close()
        assert acknowledgements == [False, False, False]
        assert not writer.offer(_proven_params('closed'), (), {}, on_persisted=acknowledgements.append)
        assert acknowledgements == [False, False, False, False]
        assert writer.stats['shutdown_dropped'] == 2
    finally:
        conn.rollback(); writer.close(); conn.close()


def test_ack_callback_failure_never_retries_a_committed_write_or_kills_writer(tmp_path, monkeypatch):
    path, conn = _ledger(tmp_path)
    monkeypatch.setattr(w, 'DB', path)
    monkeypatch.setattr(w, 'send_wake', lambda _: None)
    acknowledgements = []
    def failed_ack(durable):
        acknowledgements.append(durable)
        raise RuntimeError('ack failed')
    writer = w.DetectionWriter(path)
    try:
        assert writer.offer(_proven_params('committed'), (), {}, on_persisted=failed_ack)
        _wait(lambda: acknowledgements)
        assert writer.offer(_proven_params('after'), (), {})
        _wait(lambda: writer.stats['written'] == 2)
        assert acknowledgements == [True]
        assert writer.stats['alive'] and writer.stats['retries'] == 0 and writer.stats['errors'] == 0
        assert writer.stats['ack_errors'] == 1
    finally:
        writer.close(); conn.close()


def test_sync_caller_ack_failure_cannot_turn_a_committed_detection_into_failure(tmp_path, monkeypatch):
    path, conn = _ledger(tmp_path)
    monkeypatch.setattr(w, 'DB', path)
    monkeypatch.setattr(w, 'send_wake', lambda _: None)
    def failed_ack(durable):
        assert durable is True
        raise RuntimeError('ack failed')
    try:
        assert w._submit_observation(conn, _proven_params('sync'), (), {}, on_persisted=failed_ack) is True
        assert conn.execute('SELECT signature FROM onchain_wallet_detections').fetchone()[0] == 'sync'
    finally:
        conn.close()


@pytest.mark.parametrize('frame, explicit, expected', [
    (80.0, None, '1970-01-01T00:16:20+00:00'),
    (None, None, '1970-01-01T00:16:40+00:00'),
    (80.0, '1970-01-01T00:01:45+00:00', '1970-01-01T00:01:45+00:00')])
def test_detection_arrival_is_corrected_without_resetting_elapsed_receipt_or_cache_age(
        tmp_path, monkeypatch, frame, explicit, expected):
    from test_memecoin_shyft_confirmed import decode, wire_receipt, OWNER, WATCH
    path, conn = _ledger(tmp_path)
    monkeypatch.setattr(w, 'DB', path)
    monkeypatch.setattr(w, 'send_wake', lambda _: None)
    monkeypatch.setattr(w, '_stage_write', lambda _: None)
    monkeypatch.setattr(w, 'real_utc_now', lambda: 1000.0, raising=False)
    monkeypatch.setattr(w.time, 'time', lambda: 30000.0)
    monkeypatch.setattr(w.time, 'monotonic', lambda: 100.0)
    captured_epochs = []
    original = w.value_source_buy
    def value(*args, **kwargs):
        captured_epochs.append(kwargs['now'])
        return original(*args, **kwargs)
    monkeypatch.setattr(w, 'value_source_buy', value)
    tx = decode(wire_receipt())
    try:
        assert w.process_transaction(tx['transaction']['signatures'][0], OWNER, WATCH[OWNER], conn,
            tx=tx, frame_mono=frame, detected_at=explicit, detected_epoch=30000.0,
            sol_usd=(120.0, 29995.0), require_initiation=True) is True
        row = conn.execute('SELECT detected_at FROM onchain_wallet_detections').fetchone()
        assert row[0] == expected
        assert captured_epochs == [30000.0]
    finally:
        conn.close()


@pytest.mark.parametrize('block_time, latency', [(900, 80.0), (0, 980.0), (None, None)])
def test_arrival_metrics_and_feed_match_use_same_corrected_receipt_epoch(
        tmp_path, monkeypatch, block_time, latency):
    from test_memecoin_shyft_confirmed import decode, wire_receipt, OWNER, WATCH
    path, conn = _ledger(tmp_path)
    monkeypatch.setattr(w, 'DB', path)
    monkeypatch.setattr(w, 'send_wake', lambda _: None)
    monkeypatch.setattr(w, 'real_utc_now', lambda: 1000.0, raising=False)
    monkeypatch.setattr(w.time, 'time', lambda: 30000.0)
    monkeypatch.setattr(w.time, 'monotonic', lambda: 100.0)
    matched, stages, valued = [], [], []
    monkeypatch.setattr(w, '_stage_write', stages.append)
    def match(read, uid, mint, detected_epoch):
        matched.append(detected_epoch)
        return None, None
    monkeypatch.setattr(w, 'match_feed', match)
    original = w.value_source_buy
    def value(*args, **kwargs):
        valued.append(kwargs['now'])
        return original(*args, **kwargs)
    monkeypatch.setattr(w, 'value_source_buy', value)
    tx = decode(wire_receipt()); tx['blockTime'] = block_time
    try:
        assert w.process_transaction(tx['transaction']['signatures'][0], OWNER, WATCH[OWNER], conn,
            tx=tx, frame_mono=80.0, detected_epoch=30000.0, sol_usd=(120.0, 29995.0),
            require_initiation=True) is True
        row = conn.execute('SELECT detected_at,detect_latency_s FROM onchain_wallet_detections').fetchone()
        assert tuple(row) == ('1970-01-01T00:16:20+00:00', latency)
        assert matched == [980.0] and stages[-1]['detected_epoch'] == 980.0
        assert valued == [30000.0]
    finally:
        conn.close()


@pytest.mark.parametrize('frame', [-1.0, float('nan'), float('inf'), 101.0])
def test_invalid_receipt_clock_cannot_be_replaced_with_a_fresh_arrival(tmp_path, monkeypatch, frame):
    from test_memecoin_shyft_confirmed import decode, wire_receipt, OWNER, WATCH
    path, conn = _ledger(tmp_path)
    monkeypatch.setattr(w, 'DB', path)
    monkeypatch.setattr(w, 'send_wake', lambda _: None)
    monkeypatch.setattr(w, 'real_utc_now', lambda: 1000.0, raising=False)
    monkeypatch.setattr(w.time, 'monotonic', lambda: 100.0)
    stages = []; monkeypatch.setattr(w, '_stage_write', stages.append)
    tx = decode(wire_receipt())
    try:
        assert w.process_transaction(tx['transaction']['signatures'][0], OWNER, WATCH[OWNER], conn,
            tx=tx, frame_mono=frame, require_initiation=True) is False
        assert conn.execute('SELECT COUNT(*) FROM onchain_wallet_detections').fetchone()[0] == 0
        assert stages[-1]['outcome'] == 'receipt_clock_unknown'
    finally:
        conn.close()


def test_confirmed_receipt_failed_write_retains_http_fallback_and_duplicate_can_ack_after_commit(
        tmp_path, monkeypatch):
    from test_memecoin_shyft_confirmed import decode, wire_receipt, OWNER, WATCH, FIX
    from sniper.services import memecoin_shyft_confirmed as g, memecoin_shyft_signal as live
    path, conn = _ledger(tmp_path)
    monkeypatch.setattr(w, 'DB', path)
    monkeypatch.setattr(w, 'send_wake', lambda _: None)
    monkeypatch.setattr(w, '_stage_write', lambda _: None)
    monkeypatch.setattr(live, 'live_config', lambda: ('test-token', ['https://beta-grpc.fra.shyft.to']))
    calls = []
    tx = decode(wire_receipt())
    monkeypatch.setattr(w, '_http_rpc', lambda *args: calls.append(args) or tx)
    received = time.monotonic()
    receipt = dict(head_slot=FIX['slot'], head_monotonic=received, recv_monotonic=received,
                   recv_utc='unused-host-clock', recv_epoch=time.time(), signal_feed='shyft_confirmed:test')
    async def feed(url, token, owners_fn, worker, status, stop):
        assert worker.offer(wire_receipt(), owners_fn(), **receipt)
        await asyncio.to_thread(worker._jobs.join)
    monkeypatch.setattr(g, 'run_feed', feed)
    writer = w.DetectionWriter(path, retry_s=.1)
    conn.execute('BEGIN IMMEDIATE')
    async def exercise():
        group = w.start_confirmed_feed(lambda: WATCH, writer, {})
        try:
            await asyncio.gather(*group['tasks'])
            worker = group['worker']
            signature = tx['transaction']['signatures'][0]
            assert not worker.has_receipt(signature, OWNER, WATCH[OWNER]['uid'])
            await asyncio.to_thread(_wait, lambda: writer.stats['retry_exhausted'] == 1)
            assert not worker.has_receipt(signature, OWNER, WATCH[OWNER]['uid'])
            conn.rollback()
            await w.process_signature(signature, OWNER, WATCH[OWNER], writer,
                                      confirmed_seen=worker.has_receipt)
            assert len(calls) == 1
            await asyncio.to_thread(_wait, lambda: writer.stats['written'] == 1)
            assert worker.offer(wire_receipt(), WATCH, **receipt)
            await asyncio.to_thread(worker._jobs.join)
            await asyncio.to_thread(_wait, lambda: worker.has_receipt(signature, OWNER, WATCH[OWNER]['uid']))
            await w.process_signature(signature, OWNER, WATCH[OWNER], writer,
                                      confirmed_seen=worker.has_receipt)
            assert len(calls) == 1  # suppression begins only after exact durable proof acknowledgement
            assert conn.execute('SELECT COUNT(*) FROM onchain_wallet_detections').fetchone()[0] == 1
        finally:
            group['stop'].set(); group['worker'].close()
    try:
        asyncio.run(exercise())
    finally:
        conn.rollback(); writer.close(); conn.close()
