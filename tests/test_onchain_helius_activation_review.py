"""Offline activation regressions for confirmed feed workers and durable observation writes."""
import asyncio
import copy
import threading

import pytest

from sniper.services import memecoin_shyft_confirmed as confirmed
from test_memecoin_shyft_confirmed import FIX, OWNER, WATCH, wire_receipt
from test_memecoin_helius_evidence import notification
from test_onchain_wallet_watch_load import _load

SIGNATURE = FIX['transaction']['signatures'][0]


def _receipt(**changes):
    return dict(head_slot=FIX['slot'], head_monotonic=1., recv_monotonic=1.,
                recv_epoch=105., signal_feed='test', **changes)


def _tx():
    return dict(slot=FIX['slot'], transaction=dict(signatures=[SIGNATURE],
        message=dict(accountKeys=[OWNER], header=dict(numRequiredSignatures=1))))


def test_worker_bounds_queue_and_slow_decode_age_without_refreshing_receipt():
    now, entered, release, decoded, consumed = [10.], threading.Event(), threading.Event(), [], []
    released = []
    def decode(update, owners, **receipt):
        decoded.append(update)
        entered.set()
        assert release.wait(1.)
        return _tx()
    worker = confirmed.ReceiptWorker(lambda *args: consumed.append(args) or True, decoder=decode)
    worker.clock = lambda: now[0]
    try:
        assert worker.offer('running', WATCH, **_receipt(), on_released=lambda: released.append('running'))
        assert entered.wait(1.)
        assert worker.offer('queued', WATCH, **_receipt(), on_released=lambda: released.append('queued'))
        now[0] = 12.001
        release.set()
        worker._jobs.join()
        assert not consumed, 'expired running/queued receipts must not reach persistence'
        assert decoded == ['running'], 'expired queued job must not spend decode work'
        assert worker.stats['queue_expired'] == worker.stats['decode_expired'] == 1
        assert worker.stats['decoded'] == 0
        assert not worker.has_receipt(SIGNATURE, OWNER)
        assert sorted(released) == ['queued', 'running'], 'both expiry paths must return byte credits'
        # Expiry cannot poison signature dedupe; a new original receipt may still be processed.
        assert worker.offer('fresh', WATCH, **_receipt())
        worker._jobs.join()
        assert len(consumed) == 1 and worker.has_receipt(SIGNATURE, OWNER)
        assert consumed[0][3]['recv_monotonic'] == 1.
        assert consumed[0][3]['recv_epoch'] == 105.
    finally:
        release.set()
        worker.close()


@pytest.mark.parametrize('elapsed, accepted', [(2., True), (2.0001, False)])
def test_worker_decode_age_boundary_and_original_clock(elapsed, accepted):
    now, consumed = [10.], []
    def decode(*args, **kwargs):
        now[0] += elapsed
        return _tx()
    worker = confirmed.ReceiptWorker(lambda *args: consumed.append(args) or True, decoder=decode)
    worker.clock = lambda: now[0]
    try:
        assert worker.offer(object(), WATCH, **_receipt())
        worker._jobs.join()
        assert bool(consumed) is accepted
        if accepted:
            assert consumed[0][3]['recv_monotonic'] == 1.
            assert consumed[0][3]['recv_epoch'] == 105.
    finally:
        worker.close()


@pytest.mark.parametrize('feed', ['helius', 'shyft'])
@pytest.mark.parametrize('roster_change', ['withdrawn', 'reassigned', 'reassigned_in_place'])
def test_every_confirmed_feed_rechecks_current_owner_after_decode(tmp_path, monkeypatch, feed, roster_change):
    from sniper.services import memecoin_helius_evidence as helius, memecoin_shyft_signal as shyft
    watcher = _load(tmp_path)
    watch, processed, workers = copy.deepcopy(WATCH), [], []
    monkeypatch.setattr(shyft, 'live_config', lambda: ('fake', ('https://offline.invalid',)) if feed == 'shyft' else None)
    monkeypatch.setattr(helius, 'live_config', lambda: 'wss://offline.invalid' if feed == 'helius' else None)
    monkeypatch.setattr(watcher, '_receipt_utc', lambda _: 'original-receipt-clock')
    monkeypatch.setattr(watcher, 'process_transaction', lambda *args, **kwargs: processed.append(kwargs) or True)
    original_worker = confirmed.ReceiptWorker
    def worker_factory(consume, **kwargs):
        original_decoder = kwargs.pop('decoder', confirmed.confirmed_transaction)
        def decode(*args, **kw):
            result = original_decoder(*args, **kw)
            assert result is not None
            if roster_change == 'withdrawn':
                watch.clear()
            elif roster_change == 'reassigned_in_place':
                watch[OWNER]['uid'] = 'reassigned-user'
            else:
                watch[OWNER] = {**watch[OWNER], 'uid': 'reassigned-user'}
            return result
        worker = original_worker(consume, decoder=decode, **kwargs)
        workers.append(worker)
        return worker
    monkeypatch.setattr(confirmed, 'ReceiptWorker', worker_factory)
    async def offer(worker, feed_name, update):
        assert worker.offer(update, watch, head_slot=FIX['slot'], head_monotonic=1.,
            recv_monotonic=1., recv_epoch=105., signal_feed=feed_name)
        await asyncio.to_thread(worker._jobs.join)
    async def enhanced(url, owners_fn, worker, status, stop):
        await offer(worker, 'helius_enhanced_confirmed', notification())
    async def yellowstone(url, token, owners_fn, worker, status, stop):
        await offer(worker, 'shyft_confirmed:offline.invalid', wire_receipt())
    monkeypatch.setattr(helius, 'run_feed', enhanced)
    monkeypatch.setattr(confirmed, 'run_feed', yellowstone)
    async def check():
        group = watcher.start_confirmed_feed(lambda: watch, object(), {})
        try:
            await asyncio.gather(*group['tasks'])
            assert not processed, 'withdrawn/reassigned queued owner cannot reach writer'
            assert not any(worker.has_receipt(FIX['transaction']['signatures'][0], OWNER) for worker in workers)
        finally:
            group['stop'].set()
            for worker in workers:
                worker.close()
    asyncio.run(check())


@pytest.mark.parametrize('durable_identity', ['matching', 'wrong_uid'])
def test_separate_feeds_share_one_durable_row_and_only_matching_proof_suppresses_http(
        tmp_path, monkeypatch, durable_identity):
    import json
    import sqlite3
    from sniper.services import memecoin_helius_evidence as helius, memecoin_shyft_signal as shyft
    watcher = _load(tmp_path)
    watcher.DB = tmp_path / 'observations.db'
    conn = sqlite3.connect(watcher.DB)
    conn.executescript(watcher.SCHEMA)
    watcher.ensure_onchain_sells_table(conn)
    sig = FIX['transaction']['signatures'][0]
    if durable_identity == 'wrong_uid':
        conn.execute('INSERT INTO onchain_wallet_detections '
            '(signature,source_user_id,address,mint,detected_at,created_at,source_slot,source_valuation_json) '
            'VALUES (?,?,?,?,?,?,?,?)', (sig, 'other-user', OWNER, 'wrong-mint',
                                       '2026-10-10T11:00:00+00:00', '2026-10-10T11:00:00+00:00',
                                       FIX['slot'], '{}'))
    conn.commit()
    conn.close()
    monkeypatch.setattr(shyft, 'live_config', lambda: ('fake', ('https://offline.invalid',)))
    monkeypatch.setattr(helius, 'live_config', lambda: 'wss://offline.invalid')
    monkeypatch.setattr(watcher, 'match_feed', lambda *args: (None, None))
    arrivals = {100.: '2026-10-10T12:00:00+00:00', 100.1: '2026-10-10T12:00:00.100000+00:00'}
    monkeypatch.setattr(watcher, '_receipt_utc', arrivals.get)
    wakes, http = [], []
    monkeypatch.setattr(watcher, 'send_wake', wakes.append)
    monkeypatch.setattr(watcher, '_http_rpc', lambda *args: http.append(args) or None)
    entered, release = threading.Event(), threading.Event()
    original_persist = watcher._persist_observation
    def persist(*args, **kwargs):
        entered.set()
        assert release.wait(2.)
        return original_persist(*args, **kwargs)
    monkeypatch.setattr(watcher, '_persist_observation', persist)
    async def yellowstone(url, token, owners_fn, worker, status, stop):
        assert worker.offer(wire_receipt(), owners_fn(), head_slot=FIX['slot'], head_monotonic=100.,
            recv_monotonic=100., recv_epoch=105., signal_feed='shyft_confirmed:offline.invalid')
        await asyncio.to_thread(worker._jobs.join)
    async def enhanced(url, owners_fn, worker, status, stop):
        assert await asyncio.to_thread(entered.wait, 1.)
        assert worker.offer(notification(), owners_fn(), head_slot=FIX['slot'], head_monotonic=100.,
            recv_monotonic=100.1, recv_epoch=105.1, signal_feed='helius_enhanced_confirmed')
        await asyncio.to_thread(worker._jobs.join)
    monkeypatch.setattr(confirmed, 'run_feed', yellowstone)
    monkeypatch.setattr(helius, 'run_feed', enhanced)
    writer = watcher.DetectionWriter(watcher.DB)
    async def check():
        group = watcher.start_confirmed_feed(lambda: WATCH, writer, {})
        try:
            await asyncio.gather(*group['tasks'])
            assert len(group['workers']) == 2 and len(set(group['workers'])) == 2
            assert not group['has_receipt'](sig, OWNER, WATCH[OWNER]['uid']), 'queue acceptance is not durability'
            release.set()
            await asyncio.to_thread(writer._jobs.join)
            matching = durable_identity == 'matching'
            assert group['has_receipt'](sig, OWNER, WATCH[OWNER]['uid']) is matching
            assert not group['has_receipt'](sig, OWNER, 'reassigned-user')
            assert all(worker.stats['decoded'] == int(matching) for worker in group['workers'])
            await watcher.process_signature(sig, OWNER, WATCH[OWNER], writer,
                frame_mono=100.1, confirmed_seen=group['has_receipt'])
            assert len(http) == int(not matching), 'unverified duplicate must retain HTTP fallback'
            check_conn = sqlite3.connect(watcher.DB)
            try:
                rows = check_conn.execute('SELECT detected_at,source_valuation_json FROM onchain_wallet_detections').fetchall()
                assert len(rows) == 1
                if matching:
                    assert rows[0][0] == arrivals[100.], 'later duplicate must not refresh original arrival'
                    proof = json.loads(rows[0][1])['source_initiation']
                    assert proof['tx_hash'] == sig and proof['block_number'] == FIX['slot']
                    assert proof['beneficiary'] == OWNER and proof['source_user_id'] == WATCH[OWNER]['uid']
                    assert len(wakes) == 1
                else:
                    assert not wakes
            finally:
                check_conn.close()
        finally:
            release.set()
            group['stop'].set()
            for worker in group['workers']:
                worker.close()
            writer.close(drain_s=1.)
    asyncio.run(check())


@pytest.mark.parametrize('first_proof_age, resumed', [(0.49, True), (0.501, False)])
def test_confirmed_feed_duplicate_never_refreshes_original_native_deadline(monkeypatch, first_proof_age, resumed):
    from test_slot_lane_receipt_resume import setup
    buy, event, queued, handoff, clock = setup(monkeypatch)
    handoff.remember(buy)
    clock.now = buy.recv_monotonic + first_proof_age
    event.provenance = 'confirmed_shyft_grpc'
    assert handoff.offer(event) is resumed
    clock.now = buy.recv_monotonic + .6
    event.provenance = 'confirmed_helius_websocket'
    assert not handoff.offer(event), 'second feed cannot extend or recreate the original native window'
    assert len(queued) == int(resumed)
    if resumed:
        assert queued[0].recv_monotonic == buy.recv_monotonic
        assert queued[0].recv_utc == buy.recv_utc
        assert queued[0].proof_ready_monotonic == buy.recv_monotonic + first_proof_age
        handoff.finish(queued[0])


def test_disabled_confirmed_features_create_no_decoder_or_socket(tmp_path, monkeypatch):
    watcher = _load(tmp_path)
    monkeypatch.setenv('MEMECOIN_SHYFT_LIVE_ENABLED', '0')
    monkeypatch.setenv('MEMECOIN_HELIUS_EVIDENCE_ENABLED', '0')
    # Invalid inactive URLs/tokens cannot make a disabled feature fail startup.
    monkeypatch.setenv('MEMECOIN_RESEARCH_HELIUS_RPC_URL', 'invalid-disabled-url')
    monkeypatch.setenv('MEMECOIN_SHYFT_GRPC_URLS', 'invalid-disabled-url')
    monkeypatch.setattr(confirmed, 'ReceiptWorker', lambda *args, **kwargs: pytest.fail('disabled decoder started'))
    group = watcher.start_confirmed_feed(lambda: WATCH, object(), {})
    assert group['worker'] is group['stop'] is None
    assert group['tasks'] == [] and group['status']['last_error'] is None


@pytest.mark.parametrize('outcome', ['success', 'invalid', 'decoder_error', 'consume_error', 'async', 'duplicate'])
def test_byte_credits_return_after_every_decode_or_consume_terminal_path(outcome):
    released, callbacks = [], []
    def decode(*args, **kwargs):
        if outcome == 'decoder_error':
            raise ValueError('fake-parser-error')
        return None if outcome == 'invalid' else _tx()
    def consume(tx, owner, info, receipt):
        if outcome == 'consume_error':
            raise ValueError('fake-consumer-error')
        callbacks.append(receipt['on_persisted'])
        return None if outcome == 'async' else True
    worker = confirmed.ReceiptWorker(consume, decoder=decode)
    try:
        assert worker.offer(object(), WATCH, **_receipt(), on_released=lambda: released.append(4))
        worker._jobs.join()
        if outcome == 'duplicate':
            assert worker.offer(object(), WATCH, **_receipt(), on_released=lambda: released.append(4))
            worker._jobs.join()
            assert worker.stats['duplicates'] == 1 and len(callbacks) == 1
        assert released == [4] * (2 if outcome == 'duplicate' else 1), 'each raw-frame ownership releases once'
        if outcome == 'async':
            assert not worker.has_receipt(SIGNATURE, OWNER)
            callbacks[0](True)
            assert worker.has_receipt(SIGNATURE, OWNER)
            assert released == [4], 'durable completion must not release raw-frame credit twice'
    finally:
        worker.close()


def test_byte_budget_recovers_from_full_queue_close_drop_and_shutdown_refusal():
    entered, unblock = threading.Event(), threading.Event()
    held, releases, consumed = [0], [], []
    def decode(*args, **kwargs):
        entered.set()
        assert unblock.wait(2.)
        return _tx()
    worker = confirmed.ReceiptWorker(lambda *args: consumed.append(args) or True, maxsize=1, decoder=decode)
    def offer(label):
        held[0] += 4
        def release_credit():
            releases.append(label)
            held[0] -= 4
        return worker.offer(object(), WATCH, **_receipt(), on_released=release_credit)
    try:
        assert offer('active') and entered.wait(1.)
        assert offer('queued')
        assert not offer('full')
        assert held[0] == 8 and releases == ['full'], 'full queue must immediately free its rejected bytes'
        worker.close()
        assert held[0] == 4 and sorted(releases) == ['full', 'queued']
        assert not offer('stopped')
        assert held[0] == 4 and releases.count('stopped') == 1
        unblock.set()
        worker._thread.join(1.)
        assert not worker._thread.is_alive()
        assert held[0] == 0 and releases.count('active') == 1
        assert not consumed, 'decoder finishing after close cannot offer persistence'
    finally:
        unblock.set()
        worker.close()


def test_release_callback_failure_cannot_kill_decoder_or_block_next_job():
    consumed = []
    worker = confirmed.ReceiptWorker(lambda *args: consumed.append(args) or False,
                                     decoder=lambda *args, **kwargs: _tx())
    def bad_release():
        raise RuntimeError('fake-release-error')
    try:
        assert worker.offer(object(), WATCH, **_receipt(), on_released=bad_release)
        worker._jobs.join()
        assert worker.stats['release_errors'] == 1 and worker._thread.is_alive()
        assert worker.offer(object(), WATCH, **_receipt())
        worker._jobs.join()
        assert len(consumed) == 2
    finally:
        worker.close()


def test_offer_exception_returns_raw_frame_credit_before_reraising():
    released = []
    worker = confirmed.ReceiptWorker(lambda *args: True)
    try:
        with pytest.raises(TypeError):
            worker.offer(object(), {OWNER: None}, **_receipt(), on_released=lambda: released.append(True))
        assert released == [True] and worker._jobs.unfinished_tasks == 0
    finally:
        worker.close()


def test_offer_crossing_close_cannot_strand_a_job_or_byte_credit():
    entered, unblock = threading.Event(), threading.Event()
    released, answers = [], []
    class SlowRoster:
        def items(self):
            entered.set()
            assert unblock.wait(2.)
            return WATCH.items()
    worker = confirmed.ReceiptWorker(lambda *args: pytest.fail('closed worker consumed receipt'))
    thread = threading.Thread(target=lambda: answers.append(worker.offer(object(), SlowRoster(),
        **_receipt(), on_released=lambda: released.append(True))))
    try:
        thread.start()
        assert entered.wait(1.)
        worker.close()
        assert not worker._thread.is_alive()
        unblock.set()
        thread.join(1.)
        assert not thread.is_alive()
        assert answers == [False] and released == [True]
        assert worker._jobs.unfinished_tasks == 0
        assert worker.stats['shutdown_dropped'] == 1
    finally:
        unblock.set()
        thread.join(1.)
        worker.close()


@pytest.mark.parametrize('submission', ['connection', 'writer'])
@pytest.mark.parametrize('failure', ['open', 'locked', 'missing_table'])
def test_optional_feed_read_failure_preserves_receipt_proof_and_durable_acceptance(
        tmp_path, monkeypatch, submission, failure):
    import json
    import sqlite3
    from test_memecoin_shyft_confirmed import decode
    watcher = _load(tmp_path)
    ledger = tmp_path / 'ledger.db'
    conn = sqlite3.connect(ledger)
    conn.executescript(watcher.SCHEMA)
    watcher.ensure_onchain_sells_table(conn)
    conn.commit()
    watcher.DB = tmp_path / 'unavailable-global.db'
    stages, acks, wakes, paths = [], [], [], []
    monkeypatch.setattr(watcher, '_stage_write', stages.append)
    monkeypatch.setattr(watcher, 'send_wake', wakes.append)
    reader = locker = None
    if failure == 'locked':
        optional = tmp_path / 'optional-feed.db'
        locker = sqlite3.connect(optional)
        locker.execute('CREATE TABLE memecoin_events (id INTEGER, source_user_id TEXT, mint TEXT, '
            'side TEXT, fomo_event_id TEXT, received_at_normalized TEXT, received_at TEXT)')
        locker.commit()
        reader = watcher.get_readonly_connection(str(optional))
        reader.execute('PRAGMA busy_timeout=0')
        locker.execute('BEGIN EXCLUSIVE')
    original_open = watcher.get_readonly_connection
    def open_reader(path):
        paths.append(path)
        if failure == 'open':
            raise sqlite3.OperationalError('unable to open database file')
        return reader if failure == 'locked' else original_open(path)
    monkeypatch.setattr(watcher, 'get_readonly_connection', open_reader)
    writer = watcher.DetectionWriter(ledger, retry_s=.1) if submission == 'writer' else conn
    stamp = '2026-10-10T12:00:00+00:00'
    tx = decode(wire_receipt())
    try:
        assert watcher.process_transaction(tx['transaction']['signatures'][0], OWNER, WATCH[OWNER],
            writer, tx=tx, frame_mono=1., detected_at=stamp, detected_epoch=105.,
            sol_usd=(120., 100.), require_initiation=True, on_persisted=acks.append)
        if submission == 'writer':
            writer._jobs.join()
            assert writer.stats['written'] == 1 and writer.stats['errors'] == 0
            assert writer.stats['retries'] == writer.stats['retry_exhausted'] == 0
            assert writer.stats['feed_match_errors'] == 1
        assert acks == [True] and len(wakes) == 1
        row = conn.execute('SELECT detected_at,source_valuation_json,feed_event_id,feed_lag_vs_onchain_s '
                           'FROM onchain_wallet_detections').fetchone()
        assert row[0] == stamp and row[2:] == (None, None)
        proof = json.loads(row[1])['source_initiation']
        assert proof['tx_hash'] == tx['transaction']['signatures'][0]
        assert proof['beneficiary'] == OWNER and proof['source_user_id'] == WATCH[OWNER]['uid']
        assert stages[-1]['detected_epoch'] == 1791633600.
        assert stages[-1]['feed_match_state'] == 'unknown'
        assert stages[-1]['feed_match_error'] == ('feed_match_locked' if failure == 'locked'
                                                 else 'feed_match_unavailable')
        assert paths == [str(ledger)], 'supplied connection must never look up the unrelated global DB'
        assert not watcher.DB.exists(), 'optional observation read cannot create a writable database'
    finally:
        if submission == 'writer':
            writer.close(drain_s=1.)
        if locker is not None:
            locker.rollback()
            locker.close()
        if reader is not None:
            reader.close()
        conn.close()


@pytest.mark.parametrize('matched', [True, False])
def test_feed_lookup_uses_actual_readonly_ledger_without_waits_and_distinguishes_empty(
        tmp_path, monkeypatch, matched):
    import sqlite3
    from test_memecoin_shyft_confirmed import decode
    watcher = _load(tmp_path)
    ledger = tmp_path / 'ledger.db'
    conn = sqlite3.connect(ledger)
    conn.executescript(watcher.SCHEMA)
    watcher.ensure_onchain_sells_table(conn)
    conn.execute('CREATE TABLE memecoin_events (id INTEGER, source_user_id TEXT, mint TEXT, side TEXT, '
                 'fomo_event_id TEXT, received_at_normalized TEXT, received_at TEXT)')
    tx = decode(wire_receipt())
    stamp = '2026-10-10T12:00:00+00:00'
    mint = watcher.value_source_buy(tx, OWNER, sol_usd=(120., 100.), now=105.)['mint']
    if matched:
        conn.execute('INSERT INTO memecoin_events VALUES (1,?,?,\'buy\',\'feed-id\',?,?)',
                     (WATCH[OWNER]['uid'], mint, stamp, stamp))
    conn.commit()
    watcher.DB = tmp_path / 'unavailable-global.db'
    stages, handles = [], []
    monkeypatch.setattr(watcher, '_stage_write', stages.append)
    monkeypatch.setattr(watcher, 'send_wake', lambda _: None)
    original_match = watcher.match_feed
    def match(reader, *args):
        handles.append(reader)
        assert reader is not conn
        assert reader.execute('PRAGMA query_only').fetchone()[0] == 1
        assert reader.execute('PRAGMA busy_timeout').fetchone()[0] == 0
        assert not reader.in_transaction
        return original_match(reader, *args)
    monkeypatch.setattr(watcher, 'match_feed', match)
    try:
        assert watcher.process_transaction(tx['transaction']['signatures'][0], OWNER, WATCH[OWNER], conn,
            tx=tx, frame_mono=1., detected_at=stamp, detected_epoch=105.,
            sol_usd=(120., 100.), require_initiation=True)
        assert len(handles) == 1
        row = conn.execute('SELECT feed_event_id,feed_lag_vs_onchain_s FROM onchain_wallet_detections').fetchone()
        assert row == (('feed-id', 0.) if matched else (None, None))
        assert stages[-1]['feed_match_state'] == ('matched' if matched else 'unmatched')
        assert 'feed_match_error' not in stages[-1]
        assert not watcher.DB.exists()
    finally:
        conn.close()
