"""Opaque J7 IDs wake independent confirmed discovery; they never identify a source buy."""
import copy
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.research import onchain_wallet_watch as W
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_onchain_lane as O
from sniper.settings import memecoin as cfg

OWNER = '6xmMW5JPSEfeRuNdkxixHWsm4Sf57SdXybA3BdwZzrM1'
UID = 'verified-wallet'


@pytest.fixture
def scan(tmp_path, monkeypatch):
    path = tmp_path / 'ledger.db'
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    conn.executescript(W.SCHEMA)
    tx = json.loads((Path(__file__).parent / 'fixtures/memecoin/j7_sobdog_confirmed_20261011.json').read_text())['result']
    now = [float(tx['blockTime'] + 1)]
    mono = [100.]
    owners = {OWNER: {'uid': UID, 'handle': 'verified', 'status': 'registry_verified'}}
    monkeypatch.setattr(W, 'registry_watchlist', lambda conn: dict(owners))
    monkeypatch.setattr(W, 'time', SimpleNamespace(time=lambda: now[0], monotonic=lambda: mono[0]))
    monkeypatch.setattr(W, '_stage_write', lambda *a: None)
    monkeypatch.setattr(W, 'send_wake', lambda *a: True)
    monkeypatch.setattr(W, '_cooldown_until', lambda: 0.)
    cache = {'snapshot': (160, now[0])}
    scanner = W.J7HintScanner(path, conn, cache)
    scanner.tick(conn)  # Start at the current event tail, never replay a startup backlog.
    calls = []
    def rpc(method, params, stats=None):
        calls.append((method, params))
        if method == 'getSignaturesForAddress':
            return [dict(signature=tx['transaction']['signatures'][0], slot=tx['slot'],
                         blockTime=tx['blockTime'], err=None, confirmationStatus='confirmed')]
        assert method == 'getTransaction'
        return copy.deepcopy(tx)
    monkeypatch.setattr(W, '_http_rpc', rpc)
    return conn, scanner, tx, now, mono, owners, calls


def hint(conn, now, *, uid=UID, lane='j7_wallets', age=0, claimed_age=0, origin=None):
    iso = lambda epoch: datetime.fromtimestamp(epoch, timezone.utc).isoformat()
    conn.execute('INSERT INTO memecoin_events(fomo_event_id,source_user_id,mint,side,network_id,'
                 'signal_source,delivery_lane,created_at,received_at,received_at_normalized) '
                 'VALUES(?,?,?,?,?,?,?,?,?,?)',
                 (origin or 'swap:opaque-' + str(conn.total_changes), uid, 'unrelated-hint-mint', 'buy',
                  'solana', 'follow_activity', lane, iso(now - claimed_age), iso(now - age), iso(now - age)))
    conn.commit()


@pytest.mark.parametrize('uid,lane', [('account-one-wallet', 'j7_wallets'),
                                     ('account-two-wallet', 'j7_wallets'),
                                     ('pump:' + OWNER, 'j7_pump')])
def test_uuid_from_either_account_wakes_independent_confirmed_source_proof(scan, uid, lane):
    conn, scanner, tx, now, _, owners, calls = scan
    owners[OWNER]['uid'] = uid
    hint(conn, now[0], uid=uid, lane=lane)
    scanner.tick(conn)
    row = conn.execute('SELECT * FROM onchain_wallet_detections').fetchone()
    assert row is not None
    assert row['signature'] == tx['transaction']['signatures'][0] and row['source_user_id'] == uid
    assert row['feed_event_id'] is None and row['feed_lag_vs_onchain_s'] is None
    valuation = json.loads(row['source_valuation_json'])
    assert valuation['backfill'] is True and valuation['j7_native_failure_backup'] is True
    assert valuation['source_initiation']['payer'] == OWNER
    assert valuation['source_initiation']['beneficiary'] == OWNER
    assert valuation['source_initiation']['tx_hash'] == row['signature']
    ev = O.OnchainEv(row)
    assert ev.j7_native_failure_backup and ev.created_at == datetime.fromtimestamp(tx['blockTime'], timezone.utc).isoformat()
    assert calls[0] == ('getSignaturesForAddress', [OWNER, {'limit': 5, 'commitment': 'confirmed'}])
    assert scanner.stats['discovered_new'] == 1


@pytest.mark.parametrize('change', ['unverified', 'stale_received', 'stale_claimed', 'future', 'wrong_lane'])
def test_untrusted_or_stale_hint_cannot_start_rpc(scan, change):
    conn, scanner, _, now, _, owners, calls = scan
    kwargs = {}
    if change == 'unverified': owners.clear()
    if change == 'stale_received': kwargs['age'] = 46
    if change == 'stale_claimed': kwargs['claimed_age'] = 46
    if change == 'future': kwargs['claimed_age'] = -1
    if change == 'wrong_lane': kwargs['lane'] = 'websocket'
    hint(conn, now[0], **kwargs)
    scanner.tick(conn)
    assert calls == []
    assert conn.execute('SELECT COUNT(*) FROM onchain_wallet_detections').fetchone()[0] == 0


@pytest.mark.parametrize('change', ['stale_tx', 'missing_blocktime', 'wrong_signature', 'unsigned', 'failed_tx'])
def test_independent_receipt_still_requires_fresh_signer_payment_proof(scan, change):
    conn, scanner, tx, now, _, _, _ = scan
    if change == 'stale_tx': tx['blockTime'] -= 50
    if change == 'missing_blocktime': tx['blockTime'] = None
    if change == 'wrong_signature':
        original = tx['transaction']['signatures'][0]
        def rpc(method, params, stats=None):
            if method == 'getSignaturesForAddress':
                return [dict(signature=original, slot=tx['slot'], blockTime=now[0] - 1,
                             err=None, confirmationStatus='confirmed')]
            bad = copy.deepcopy(tx); bad['transaction']['signatures'] = ['wrong']; return bad
        # A page identity and its returned receipt disagree.
        from _pytest.monkeypatch import MonkeyPatch
        with MonkeyPatch.context() as mp:
            mp.setattr(W, '_http_rpc', rpc)
            hint(conn, now[0]); scanner.tick(conn)
        assert conn.execute('SELECT COUNT(*) FROM onchain_wallet_detections').fetchone()[0] == 0
        return
    if change == 'unsigned':
        for key in tx['transaction']['message']['accountKeys']:
            if isinstance(key, dict) and key.get('pubkey') == OWNER: key['signer'] = False
    if change == 'failed_tx': tx['meta']['err'] = {'InstructionError': [1, 'error']}
    hint(conn, now[0]); scanner.tick(conn)
    assert conn.execute('SELECT COUNT(*) FROM onchain_wallet_detections').fetchone()[0] == 0


def test_roster_withdrawal_while_rpc_is_out_cannot_publish(scan, monkeypatch):
    conn, scanner, tx, now, _, owners, _ = scan
    original = W._http_rpc
    def rpc(method, params, stats=None):
        result = original(method, params, stats)
        if method == 'getTransaction': owners.clear()
        return result
    monkeypatch.setattr(W, '_http_rpc', rpc)
    hint(conn, now[0]); scanner.tick(conn)
    assert conn.execute('SELECT COUNT(*) FROM onchain_wallet_detections').fetchone()[0] == 0


def test_existing_receipt_is_not_relabelled_or_reaged(scan):
    conn, scanner, tx, now, mono, _, calls = scan
    hint(conn, now[0]); scanner.tick(conn)
    original = tuple(conn.execute('SELECT * FROM onchain_wallet_detections').fetchone())
    now[0] += 6; mono[0] += 6
    hint(conn, now[0]); scanner.tick(conn)
    assert tuple(conn.execute('SELECT * FROM onchain_wallet_detections').fetchone()) == original
    assert scanner.stats['discovered_new'] == 1 and scanner.stats['already_present'] == 1
    assert [method for method, _ in calls].count('getTransaction') == 1


def test_debounce_queue_and_transaction_reads_are_bounded(scan, monkeypatch):
    conn, scanner, tx, now, mono, owners, calls = scan
    sigs = []
    import base58
    for x in range(1, 10): sigs.append(base58.b58encode(bytes([x]) * 64).decode())
    def rpc(method, params, stats=None):
        calls.append((method, params))
        if method == 'getSignaturesForAddress':
            return [dict(signature=sig, slot=tx['slot'], blockTime=tx['blockTime'], err=None,
                         confirmationStatus='confirmed') for sig in sigs]
        return None
    monkeypatch.setattr(W, '_http_rpc', rpc)
    for _ in range(8): hint(conn, now[0])
    scanner.tick(conn)
    assert len(calls) == 4  # One page and at most three receipt reads, one owner at a time.
    hint(conn, now[0]); scanner.tick(conn)
    assert len(calls) == 4
    for i in range(20):
        owner = f'owner-{i}'; owners[owner] = dict(uid=str(i), handle='owner', status='registry_verified')
        hint(conn, now[0], uid=str(i))
    scanner.tick(conn)
    assert scanner.snapshot()['pending'] <= 16 and scanner.stats['overflow'] > 0


@pytest.mark.parametrize('pure_sell', [False, True])
def test_hint_scan_never_expands_sell_mirror_delivery(scan, monkeypatch, pure_sell):
    conn, scanner, tx, now, _, _, _ = scan
    monkeypatch.setattr(W, 'classify_sell', lambda *a: pytest.fail('scanner classified an exit'))
    if pure_sell: monkeypatch.setattr(W, 'classify_buy', lambda *a: None)
    hint(conn, now[0]); scanner.tick(conn)
    assert conn.execute('SELECT COUNT(*) FROM onchain_evm_sell_shadow').fetchone()[0] == 0


@pytest.mark.parametrize('funded,backup,provider,source,enabled', [(True, True, 'shyft', 'j7', True),
    (False, True, 'shyft', 'j7', False), (True, False, 'shyft', 'j7', False),
    (True, True, 'helius', 'j7', False), (True, True, 'shyft', 'fomo', False)])
def test_scanner_starts_only_for_requested_funded_shyft_backup(tmp_path, monkeypatch, funded, backup, provider, source, enabled):
    monkeypatch.setenv('MEMECOIN_SIGNAL_SOURCE', source)
    monkeypatch.setenv('MEMECOIN_FUNDED_ONLY', str(int(funded)))
    monkeypatch.setenv('MEMECOIN_J7_SOLANA_VERIFIED_BACKUP', str(int(backup)))
    monkeypatch.setenv('MEMECOIN_SOLANA_INFRA_PROVIDER', provider)
    # An old imported config must not override a later disable in the saved environment.
    monkeypatch.setattr(cfg, 'MEMECOIN_SIGNAL_SOURCE', 'j7')
    monkeypatch.setattr(cfg, 'MEMECOIN_FUNDED_ONLY', True)
    monkeypatch.setattr(cfg, 'MEMECOIN_J7_SOLANA_VERIFIED_BACKUP', True)
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_INFRA_PROVIDER', 'shyft')
    started = []
    monkeypatch.setattr(W.J7HintScanner, 'start', lambda self: started.append(self))
    scanner = W.start_j7_hint_scanner(object(), {})
    assert bool(started) is enabled and (scanner is not None) is enabled


@pytest.mark.parametrize('stop', [False, True])
def test_expired_hint_or_shutdown_during_rpc_cannot_publish_late(scan, monkeypatch, stop):
    conn, scanner, tx, now, _, _, _ = scan
    original = W._http_rpc
    def rpc(method, params, stats=None):
        result = original(method, params, stats)
        if method == 'getTransaction':
            if stop: scanner._stop.set()
            else:
                now[0] += 46
                result['blockTime'] = int(now[0]) - 1
        return result
    monkeypatch.setattr(W, '_http_rpc', rpc)
    hint(conn, now[0]); scanner.tick(conn)
    assert conn.execute('SELECT COUNT(*) FROM onchain_wallet_detections').fetchone()[0] == 0


def test_live_writer_distinguishes_new_insert_from_durable_duplicate(tmp_path, monkeypatch):
    from test_onchain_wallet_watch_writer import _proven_params, _wait
    path = tmp_path / 'ledger.db'
    conn = sqlite3.connect(path)
    conn.executescript(W.SCHEMA)
    monkeypatch.setattr(W, '_stage_write', lambda *a: None)
    monkeypatch.setattr(W, 'send_wake', lambda *a: True)
    writer = W.DetectionWriter(path)
    scanner = W.J7HintScanner(path, writer, {})
    durable = []
    try:
        for _ in range(2):
            assert writer.offer(_proven_params('independent-source'), (), {'signal_feed': 'j7_hint_scan'},
                                on_persisted=durable.append, on_stored=scanner._stored)
        _wait(lambda: len(durable) == 2 and scanner.stats['already_present'] == 1)
        assert durable == [True, True]
        assert scanner.stats['discovered_new'] == 1 and scanner.stats['already_present'] == 1
        assert writer.stats['written'] == 1 and writer.stats['duplicates'] == 1
    finally:
        writer.close()
        conn.close()


def test_preexisting_independent_receipt_does_not_gain_j7_authority(scan):
    conn, scanner, tx, now, _, owners, calls = scan
    sig = tx['transaction']['signatures'][0]
    W.process_transaction(sig, OWNER, owners[OWNER], conn, tx=tx,
        sol_usd=(160, now[0]),
        detected_at=datetime.fromtimestamp(now[0], timezone.utc).isoformat(), detected_epoch=now[0],
        require_initiation=True)
    original = tuple(conn.execute('SELECT * FROM onchain_wallet_detections').fetchone())
    hint(conn, now[0]); scanner.tick(conn)
    row = conn.execute('SELECT * FROM onchain_wallet_detections').fetchone()
    assert tuple(row) == original and not O.OnchainEv(row).j7_native_failure_backup
    assert scanner.stats['discovered_new'] == 0 and scanner.stats['already_present'] == 1
    assert [method for method, _ in calls] == ['getSignaturesForAddress']


@pytest.mark.parametrize('failed_method,reason', [('getSignaturesForAddress', 'signature_page_unavailable'),
                                               ('getTransaction', 'receipt_unavailable')])
def test_unavailable_rpc_is_counted_and_visible_without_retry(scan, monkeypatch, failed_method, reason):
    conn, scanner, _, now, _, _, calls = scan
    original = W._http_rpc
    def rpc(method, params, stats=None):
        if method == failed_method:
            calls.append((method, params))
            return None
        return original(method, params, stats)
    monkeypatch.setattr(W, '_http_rpc', rpc)
    hint(conn, now[0]); scanner.tick(conn)
    assert scanner.stats['rpc_unavailable'] == 1 and scanner.stats['last_error'] == reason
    assert len(calls) == (1 if failed_method == 'getSignaturesForAddress' else 2)
    assert conn.execute('SELECT COUNT(*) FROM onchain_wallet_detections').fetchone()[0] == 0



def test_scanner_gate_reads_saved_environment_loaded_after_settings_import(tmp_path, monkeypatch):
    import importlib.util
    from sniper.util.envfiles import load_env_files
    settings = {'MEMECOIN_FUNDED_ONLY': ('1', False),
                'MEMECOIN_J7_SOLANA_VERIFIED_BACKUP': ('1', False),
                'MEMECOIN_SOLANA_INFRA_PROVIDER': ('shyft', 'legacy'),
                'MEMECOIN_SIGNAL_SOURCE': ('j7', 'fomo')}
    for key, (_, stale) in settings.items():
        monkeypatch.delenv(key, raising=False)
        monkeypatch.setattr(cfg, key, stale)
    spec = importlib.util.spec_from_file_location('watcher_before_env', W.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / '.env').write_text(''.join(key + '=' + value + '\n'
                                         for key, (value, _) in settings.items()))
    assert load_env_files(tmp_path) == ['.env']
    started = []
    monkeypatch.setattr(module.J7HintScanner, 'start', lambda self: started.append(self))
    scanner = module.start_j7_hint_scanner(object(), {})
    assert scanner is not None and started == [scanner]
    assert all(getattr(cfg, key) == stale for key, (_, stale) in settings.items())
