"""ARC bridge validation and recovery checks use no network or custody key."""
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('arc_bridge', Path(__file__).parents[1] / 'scripts/memecoin_bridge_solana_to_arc.py')
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


def quote():
    return {'action': {'fromChainId': 1151111081099710, 'toChainId': 5042,
        'fromAddress': 'SOL', 'toAddress': '0xABC', 'fromAmount': '20000000',
        'fromToken': {'address': b.SOL_USDC, 'decimals': 6, 'chainId': 1151111081099710},
        'toToken': {'address': b.ARC_USDC, 'decimals': 6, 'chainId': 5042}},
        'estimate': {'toAmount': '19900000', 'toAmountMin': '19800000'},
        'transactionRequest': {'data': 'unsigned'}}


@pytest.mark.parametrize('value', ['0', '-1', 'nan', 'inf', '0.0000001', '1.0000001'])
def test_amount_requires_positive_exact_usdc(value):
    with pytest.raises(ValueError):
        b.usdc_atoms(value)


def test_route_uses_minimum_output_and_exact_identities():
    q = quote()
    assert b.validate_quote(q, 'SOL', '0xabc', 20_000_000, '98.5') == 19_800_000
    q['estimate']['toAmountMin'] = '10000000'
    with pytest.raises(ValueError):
        b.validate_quote(q, 'SOL', '0xabc', 20_000_000, '98.5')


@pytest.mark.parametrize('field,value', [('toChainId', 8453), ('fromChainId', 1),
    ('fromAmount', '20000001'), ('fromAddress', 'OTHER'), ('toAddress', '0xOTHER')])
def test_route_rejects_wrong_transfer(field, value):
    q = quote(); q['action'][field] = value
    with pytest.raises(ValueError):
        b.validate_quote(q, 'SOL', '0xabc', 20_000_000, '98.5')


def test_route_rejects_wrong_token_decimals():
    q = quote(); q['action']['toToken']['decimals'] = 18
    with pytest.raises(ValueError):
        b.validate_quote(q, 'SOL', '0xabc', 20_000_000, '98.5')


def test_signed_identity_precedes_send_and_ambiguous_send_stays_pending(tmp_path):
    path = tmp_path / 'receipt.json'; sent = []
    def send(data, rpc):
        saved = json.loads(path.read_text())
        assert saved['sig'] == 'SIG' and saved['state'] == 'submitted_unknown'
        assert 'BYTES' not in path.read_text()
        sent.append(data)
        raise TimeoutError('response lost')
    b.broadcast_once(path, {'request_id': 'r'}, ('BYTES', 'SIG'), 'rpc', send)
    assert sent == ['BYTES']
    assert json.loads(path.read_text())['state'] == 'submitted_unknown'
    with pytest.raises(ValueError):
        b.broadcast_once(path, {'request_id': 'r'}, ('NEW', 'NEW_SIG'), 'rpc', send)
    assert sent == ['BYTES']


def test_pause_preserves_existing_and_later_operator_kill(tmp_path, monkeypatch):
    monkeypatch.setenv('SNIPER_STATE_ROOT', str(tmp_path))
    path = tmp_path / 'kill'; path.write_text('operator')
    with pytest.raises(FileExistsError):
        b.pause_entries(path, 'r')
    assert path.read_text() == 'operator'
    path.unlink()
    token = b.pause_entries(path, 'r')
    from sniper.services import memecoin_arming as arming
    assert token and token == arming.kill_switch_status(str(path))['kill_token']
    path.write_text('new operator stop')
    assert not b.resume_entries(path, 'r', token)
    assert path.exists()


def test_settlement_needs_source_identity_bridge_success_and_balance_credit():
    receipt = {'sig': 'SIG', 'arc_before_atoms': 100, 'minimum_atoms': 1000}
    result = {'status': 'DONE', 'substatus': 'COMPLETED',
        'sending': {'txHash': 'SIG'}, 'receiving': {'chainId': 5042, 'txHash': '0xreceipt'}}
    assert b.settlement_complete(receipt, result, 'confirmed', 1100)
    assert not b.settlement_complete(receipt, result, 'confirmed', 1099)
    assert not b.settlement_complete(receipt, result, 'pending', 1100)
    result['substatus'] = 'REFUNDED'
    assert not b.settlement_complete(receipt, result, 'confirmed', 1100)
    result['substatus'] = 'COMPLETED'; result['sending']['txHash'] = 'OTHER'
    assert not b.settlement_complete(receipt, result, 'confirmed', 1100)


@pytest.mark.parametrize('send_raises', [False, True])
def test_cli_reads_destination_before_send_and_retains_pause_until_credit(tmp_path, monkeypatch, send_raises):
    import sqlite3
    from types import SimpleNamespace
    from sniper.execution import evm_wallet as ew, solana_wallet as sw, solana_real_seams as seams
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_rent_sweep as sweep
    from sniper.settings import memecoin as cfg
    from sniper.util import envfiles
    conn = sqlite3.connect(tmp_path / 'sniper_tracker.db'); conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn); conn.close()
    monkeypatch.setenv('SNIPER_STATE_ROOT', str(tmp_path))
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'sniper_tracker.db'))
    monkeypatch.setattr(envfiles, 'load_env_files', lambda root: None)
    monkeypatch.setattr(cfg, 'evm_rpc_for_chain', lambda chain: 'arc-rpc')
    monkeypatch.setattr(ew, 'load_keypair', lambda path: None)
    monkeypatch.setattr(ew, 'address_str', lambda key: '0xABC')
    monkeypatch.setattr(sw, 'load_keypair', lambda path: None)
    monkeypatch.setattr(sw, 'pubkey_str', lambda key: 'SOL')
    monkeypatch.setattr(sw, 'token_balance_checked', lambda *a: 1000)
    monkeypatch.setattr(sw, 'sol_balance_read', lambda *a: SimpleNamespace(ok=True))
    monkeypatch.setattr(sw, 'native_sol_balance_value', lambda read: 0.05)
    sent = []
    def rpc(url, method, params):
        if method == 'eth_chainId': return {'result': hex(5042)}
        return {'result': hex(19_900_100 if sent else 100)}
    monkeypatch.setattr(ew, '_rpc', rpc)
    monkeypatch.setattr(seams, 'sign_swap_tx', lambda *a: ('BYTES', 'SIG'))
    monkeypatch.setattr(seams, 'rpc_confirm', lambda *a, **kw: 'pending' if send_raises else 'confirmed')
    def simulate(url, method, params):
        assert method == 'simulateTransaction'
        assert params[1]['sigVerify'] is True
        error = None if params[1].get('commitment') == 'confirmed' else 'BlockhashNotFound'
        return {'result': {'value': {'err': error}}}
    monkeypatch.setattr(sweep, 'rpc_call', simulate)
    kill = tmp_path / 'kill'
    monkeypatch.setenv('MEMECOIN_KILL_SWITCH_FILE', str(kill))
    def send(*args, **kwargs):
        assert kwargs['preflight_commitment'] == 'confirmed'
        receipt = json.loads(next((tmp_path / 'data').glob('arc-funding-*.json')).read_text())
        from sniper.services import memecoin_arming as arming
        assert receipt['kill_token'] == arming.kill_switch_status(str(kill))['kill_token']
        assert receipt['arc_before_atoms'] == 100
        assert receipt['sig'] == 'SIG' and kill.exists()
        sent.append(args)
        if send_raises: raise TimeoutError('response lost')
    monkeypatch.setattr(seams, 'rpc_send', send)
    result = {'status': 'DONE', 'substatus': 'COMPLETED', 'sending': {'txHash': 'SIG'},
              'receiving': {'chainId': 5042, 'txHash': '0xreceipt'}}
    monkeypatch.setattr(b, '_get', lambda url: result if '/status?' in url else quote())
    monkeypatch.setattr(b.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(b.sys, 'argv', ['bridge', '--amount-usdc', '20', '--confirm', '--state-root', str(tmp_path)])
    b.main()
    assert len(sent) == 1
    assert kill.exists() == send_raises
    saved = json.loads(next((tmp_path / 'data').glob('arc-funding-*.json')).read_text())
    assert saved['state'] == ('submitted_unknown' if send_raises else 'credited')


def test_completed_receipt_clears_its_pause_after_restart(tmp_path, monkeypatch):
    from sniper.execution import evm_wallet as ew, solana_wallet as sw
    from sniper.settings import memecoin as cfg
    from sniper.util import envfiles
    monkeypatch.setenv('SNIPER_STATE_ROOT', str(tmp_path))
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'sniper_tracker.db'))
    monkeypatch.setattr(envfiles, 'load_env_files', lambda root: None)
    monkeypatch.setattr(cfg, 'evm_rpc_for_chain', lambda chain: 'arc-rpc')
    monkeypatch.setattr(ew, 'load_keypair', lambda path: None)
    monkeypatch.setattr(ew, 'address_str', lambda key: '0xABC')
    monkeypatch.setattr(ew, '_rpc', lambda *args: {'result': hex(5042)})
    monkeypatch.setattr(sw, 'load_keypair', lambda path: None)
    monkeypatch.setattr(sw, 'pubkey_str', lambda key: 'SOL')
    kill = tmp_path / 'kill'; token = b.pause_entries(kill, 'r')
    monkeypatch.setenv('MEMECOIN_KILL_SWITCH_FILE', str(kill))
    receipt = tmp_path / 'receipt.json'
    receipt.write_text(json.dumps({'request_id': 'r', 'source': 'SOL', 'destination': '0xABC',
                                  'state': 'credited', 'sig': 'SIG', 'kill_token': token}))
    monkeypatch.setattr(b.sys, 'argv', ['bridge', '--resume', str(receipt), '--state-root', str(tmp_path)])
    b.main()
    assert not kill.exists()


@pytest.mark.parametrize('replacement_source', ['operator', 'runtime-deploy', 'arc-funding:r'])
def test_bridge_clear_preserves_replacement_hold(tmp_path, monkeypatch, replacement_source):
    from sniper.services import memecoin_arming as arming
    monkeypatch.setenv('SNIPER_STATE_ROOT', str(tmp_path))
    path = tmp_path / 'kill'
    token = b.pause_entries(path, 'r')
    replacement = arming.trip_kill_switch(str(path), source=replacement_source)
    assert not b.resume_entries(path, 'r', token)
    assert arming.kill_switch_status(str(path))['kill_token'] == replacement


def test_legacy_bridge_receipt_clears_only_exact_legacy_content(tmp_path, monkeypatch):
    from sniper.services import memecoin_arming as arming
    monkeypatch.setenv('SNIPER_STATE_ROOT', str(tmp_path))
    path = tmp_path / 'kill'
    path.write_text('tripped by arc-funding:r')
    assert b.resume_entries(path, 'r')
    token = b.pause_entries(path, 'r')
    assert not b.resume_entries(path, 'r')
    assert arming.kill_switch_status(str(path))['kill_token'] == token


@pytest.mark.parametrize('operation', ['acquire', 'clear'])
def test_bridge_waits_for_shared_pause_lock(tmp_path, monkeypatch, operation):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event
    from sniper.services import memecoin_arming as arming
    monkeypatch.setenv('SNIPER_STATE_ROOT', str(tmp_path))
    path = tmp_path / 'kill'
    token = b.pause_entries(path, 'r') if operation == 'clear' else None
    started = Event()

    def bridge_operation():
        started.set()
        if operation == 'clear':
            return b.resume_entries(path, 'r', token)
        return b.pause_entries(path, 'r')

    with ThreadPoolExecutor(max_workers=1) as pool:
        with arming._kill_lock(str(path)):
            future = pool.submit(bridge_operation)
            assert started.wait(5)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.05)
            # The other owner replaces the hold while it owns the shared lock.
            path.write_text('tripped by operator\nhold later-owner\n')
        if operation == 'clear':
            assert future.result(timeout=5) is False
        else:
            with pytest.raises(FileExistsError):
                future.result(timeout=5)
    assert arming.kill_switch_source(str(path)) == 'operator'
