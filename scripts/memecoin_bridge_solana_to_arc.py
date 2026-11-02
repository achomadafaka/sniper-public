#!/usr/bin/env python3
"""Fund the ARC custody wallet from Solana USDC. ARC uses the same USDC for gas.

Quote only by default. --confirm pauses new entries, then signs and sends once.
Both affected books must be flat. Exits on other chains remain active.
An uncertain transfer retains the entry pause. --resume RECEIPT checks it without another send.

  python3 scripts/memecoin_bridge_solana_to_arc.py --amount-usdc 80
  python3 scripts/memecoin_bridge_solana_to_arc.py --amount-usdc 80 --confirm
"""
import argparse
from contextlib import ExitStack
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import urllib.parse
import urllib.request
import uuid

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
ARC_CHAIN = '5042'
ARC_USDC = '0x3600000000000000000000000000000000000000'
SOL_USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
SOL_CHAIN = 1151111081099710  # LI.FI chain identifier.
LIFI = 'https://li.quest/v1'


def _get(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'Sniper/1.0', 'Accept': 'application/json'})
    with urllib.request.urlopen(request, timeout=40) as response:
        return json.load(response)


def usdc_atoms(value):
    """Reject rounding, nonfinite values, and nonpositive amounts."""
    try:
        amount = Decimal(str(value)) * 1_000_000
        if not amount.is_finite() or amount <= 0 or amount != amount.to_integral_value():
            raise ValueError('amount requires positive USDC with at most six decimals')
        return int(amount)
    except InvalidOperation as exc:
        raise ValueError('invalid USDC amount') from exc


def validate_quote(quote, source, destination, atoms, minimum_pct):
    """Bind the route to both custody wallets, chains, tokens, and the requested amount."""
    a, estimate = quote['action'], quote['estimate']
    for field, chain, token in [('fromToken', SOL_CHAIN, SOL_USDC), ('toToken', 5042, ARC_USDC)]:
        item = a[field]
        address = item['address'] if field == 'fromToken' else item['address'].lower()
        if address != token or int(item['decimals']) != 6 or int(item['chainId']) != chain:
            raise ValueError('route token identity mismatch')
    if (int(a['fromChainId']) != SOL_CHAIN or int(a['toChainId']) != 5042
            or a['fromAddress'] != source or a['toAddress'].lower() != destination.lower()
            or int(a['fromAmount']) != atoms):
        raise ValueError('route transfer identity mismatch')
    pct = Decimal(str(minimum_pct))
    if not pct.is_finite() or not 0 < pct <= 100:
        raise ValueError('minimum received percent must be above zero and at most 100')
    minimum = int(estimate['toAmountMin'])
    if minimum < Decimal(atoms) * pct / 100 or int(estimate['toAmount']) < minimum:
        raise ValueError('minimum route output is below the requested floor')
    if not quote.get('transactionRequest', {}).get('data'):
        raise ValueError('route has no transaction')
    return minimum


def _save(path, value):
    """Persist public transaction evidence before any broadcast."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    with temp.open('w') as handle:
        json.dump(value, handle, indent=2)
        handle.flush(); os.fsync(handle.fileno())
    temp.replace(path)
    fd = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def pause_entries(path, request_id):
    """Acquire an entry pause without replacing an existing hold."""
    from sniper.services.memecoin_arming import trip_kill_switch
    token = trip_kill_switch(str(path), source='arc-funding:' + request_id, only_if_clear=True)
    if token is None:
        raise FileExistsError('an entry pause already exists')
    return token


def resume_entries(path, request_id, token=None):
    """Clear only the exact hold stored for this transfer."""
    from sniper.services.memecoin_arming import clear_kill_switch
    if token is None:
        # Legacy receipts identify only the exact original file content.
        token = hashlib.sha256(('tripped by arc-funding:' + request_id).encode()).hexdigest()
    return clear_kill_switch(str(path), source='arc-funding:' + request_id, expected_token=token)


def broadcast_once(path, receipt, signed, rpc, send_fn):
    """Save the signature before send. Never repeat a stored request."""
    if path.exists() and json.loads(path.read_text()).get('sig'):
        raise ValueError('this request already has a signed transaction; use --resume')
    receipt.update(sig=signed[1], state='submitted_unknown')
    _save(path, receipt)
    try:
        send_fn(signed[0], rpc)
    except Exception as exc:
        print('Send response unavailable:', type(exc).__name__)
    return receipt


def settlement_complete(receipt, result, source_status, arc_atoms):
    """Require source confirmation, the exact bridge result, and observed destination credit."""
    return (source_status == 'confirmed' and result.get('status') == 'DONE'
            and result.get('substatus') == 'COMPLETED'
            and result.get('sending', {}).get('txHash') == receipt['sig']
            and str(result.get('receiving', {}).get('chainId')) == ARC_CHAIN
            and bool(result.get('receiving', {}).get('txHash'))
            and arc_atoms >= receipt['arc_before_atoms'] + receipt['minimum_atoms'])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--amount-usdc', help='exact USDC amount to debit on Solana')
    parser.add_argument('--reserve-usdc', default='50', help='USDC to retain on Solana (default: 50)')
    parser.add_argument('--min-received-pct', default='98.5')
    parser.add_argument('--confirm', action='store_true')
    parser.add_argument('--resume', type=Path, help='check a stored transfer; never send again')
    parser.add_argument('--state-root', default='/home/bigpapi/Desktop/Sniper')
    args = parser.parse_args()
    if args.resume and (args.confirm or args.amount_usdc):
        parser.error('--resume cannot be combined with --confirm or --amount-usdc')
    if not args.resume and not args.amount_usdc:
        parser.error('--amount-usdc is required')
    root = Path(args.state_root).resolve()
    os.environ['SNIPER_STATE_ROOT'] = str(root)
    os.environ['DB_PATH'] = str(root / 'sniper_tracker.db')
    from sniper.util.envfiles import load_env_files
    load_env_files(str(root))
    from sniper.settings import memecoin as cfg
    from sniper.execution import evm_wallet as ew, solana_wallet as sw, solana_real_seams as seams
    from sniper.execution.exec_lock import wallet_exec_lock
    from sniper.persistence.db_core import get_readonly_connection
    from sniper.services.memecoin_arming import _kill_path
    from sniper.services.memecoin_rent_sweep import rpc_call
    from sniper.util.clock import real_utc_now_iso

    key = sw.load_keypair(cfg.MEMECOIN_SOLANA_KEYPAIR)
    source = sw.pubkey_str(key)
    destination = ew.address_str(ew.load_keypair(cfg.MEMECOIN_EVM_KEYPAIR))
    rpc, arc_rpc = cfg.MEMECOIN_SOLANA_RPC_URL, cfg.evm_rpc_for_chain(ARC_CHAIN)
    if int(ew._rpc(arc_rpc, 'eth_chainId', [])['result'], 16) != 5042:
        raise ValueError('ARC RPC chain identity mismatch')
    kill = Path(_kill_path())

    def arc_balance():
        raw = ew._rpc(arc_rpc, 'eth_call', [{'to': ARC_USDC,
            'data': '0x70a08231' + destination[2:].lower().rjust(64, '0')}, 'latest'])
        return int(raw['result'], 16)

    def settle(path, receipt):
        for _ in range(30):
            try:
                source_status = seams.rpc_confirm(receipt['sig'], rpc, max_attempts=1)
                result = _get(LIFI + '/status?' + urllib.parse.urlencode({
                    'txHash': receipt['sig'], 'fromChain': SOL_CHAIN, 'toChain': 5042}))
                balance = arc_balance()
                if settlement_complete(receipt, result, source_status, balance):
                    receipt.update(state='credited', arc_after_atoms=balance,
                                   destination_tx=result['receiving']['txHash'], completed_at=real_utc_now_iso())
                    _save(path, receipt)
                    cleared = resume_entries(kill, receipt['request_id'], receipt.get('kill_token'))
                    print('ARC credit confirmed. USDC balance: %.6f. Entry pause cleared: %s' % (balance / 1e6, cleared))
                    return
            except Exception as exc:
                print('Settlement check unavailable:', type(exc).__name__)
            time.sleep(4)
        print('Settlement remains uncertain. The entry pause remains. No second transfer was sent.')
        print('Resume: python3 %s --resume %s' % (Path(__file__).resolve(), path))

    if args.resume:
        receipt = json.loads(args.resume.read_text())
        if receipt['source'] != source or receipt['destination'].lower() != destination.lower():
            raise ValueError('receipt custody identity mismatch')
        if not receipt.get('sig'):
            raise ValueError('receipt has no signed transfer; inspect it before clearing the entry pause')
        if receipt.get('state') == 'credited':
            resume_entries(kill, receipt['request_id'], receipt.get('kill_token'))
            print('Stored transfer already credited:', receipt['sig'])
            return
        settle(args.resume, receipt)
        return

    atoms, reserve = usdc_atoms(args.amount_usdc), usdc_atoms(args.reserve_usdc)
    quote = _get(LIFI + '/quote?' + urllib.parse.urlencode({'fromChain': 'SOL', 'toChain': 5042,
        'fromToken': SOL_USDC, 'toToken': ARC_USDC, 'fromAddress': source,
        'toAddress': destination, 'fromAmount': atoms}))
    minimum = validate_quote(quote, source, destination, atoms, args.min_received_pct)

    def balances():
        usdc = sw.token_balance_checked(source, SOL_USDC, rpc)
        sol_read = sw.sol_balance_read(source, rpc)
        if usdc is None or not getattr(sol_read, 'ok', False):
            raise ValueError('source balance unavailable')
        sol = float(sw.native_sol_balance_value(sol_read))
        if Decimal(str(usdc)) * 1_000_000 < atoms + reserve:
            raise ValueError('source USDC cannot cover the amount and retained reserve')
        if sol < 0.003:
            raise ValueError('source SOL is below 0.003; fee and rent funds are insufficient')
        return usdc, sol

    usdc, sol = balances()
    print('Solana: %.6f USDC, %.6f SOL. ARC custody: %s' % (usdc, sol, destination))
    print('Send %.6f USDC; estimated ARC receipt %.6f; minimum %.6f; route %s.' % (
        atoms / 1e6, int(quote['estimate']['toAmount']) / 1e6, minimum / 1e6, quote.get('tool')))
    print('ARC USDC pays both trades and gas. Source SOL pays the bridge transaction fee.')
    if not args.confirm:
        print('Quote only. Nothing signed or sent. Add --confirm to pause entries and transfer.')
        return

    request_id = str(uuid.uuid4())
    path = root / 'data' / ('arc-funding-' + request_id + '.json')
    kill_token = pause_entries(kill, request_id)
    receipt = {'request_id': request_id, 'kill_token': kill_token, 'source': source, 'destination': destination,
        'source_atoms': atoms, 'minimum_atoms': minimum, 'created_at': real_utc_now_iso(), 'state': 'prepared'}
    try:
        with ExitStack() as stack:
            for lock in ['sol-' + source, 'evm-5042-' + destination]:
                stack.enter_context(wallet_exec_lock(lock, lock_dir=str(root / 'data')))
            conn = get_readonly_connection(str(root / 'sniper_tracker.db'))
            try:
                positions = conn.execute("SELECT COUNT(*) FROM memecoin_positions WHERE execution_mode='real' "
                    "AND is_open=1 AND network_id IN ('solana','5042')").fetchone()[0]
                pending = conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries WHERE status='reserved'").fetchone()[0]
                from sniper.persistence import db_memecoin as m
                withdrawals = m.unresolved_withdraws(conn, 'solana')
                if positions or pending or withdrawals:
                    raise ValueError('affected book or transaction is unresolved; transfer refused')
            finally: conn.close()
            balances()
            receipt['arc_before_atoms'] = arc_balance()
            signed = seams.sign_swap_tx(quote['transactionRequest']['data'], key)
            if not signed:
                raise ValueError('bridge signing failed')
            simulation = rpc_call(rpc, 'simulateTransaction', [signed[0], {
                'encoding': 'base64', 'sigVerify': True, 'commitment': 'confirmed'}])
            value = simulation.get('result', {}).get('value', {})
            if 'err' not in value or value['err'] is not None:
                raise ValueError('bridge simulation failed')
            print('Receipt:', path, flush=True)
            broadcast_once(path, receipt, signed, rpc, lambda data, url:
                           seams.rpc_send(data, url, preflight_commitment='confirmed'))
            print('Source transaction:', signed[1], flush=True)
            settle(path, receipt)
    finally:
        # A signed identity retains the pause even when the send response is lost.
        if not path.exists() or not json.loads(path.read_text()).get('sig'):
            resume_entries(kill, request_id, kill_token)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError) as exc:
        raise SystemExit('REFUSE: ' + str(exc))
