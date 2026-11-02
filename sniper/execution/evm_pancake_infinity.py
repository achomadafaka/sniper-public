"""BSC native/coin Infinity CL routes, discovered from source receipts off the entry path.

Deployments/ABI: pancakeswap/infinity-periphery script/config/bsc-mainnet.json and
ICLQuoter; infinity-universal-router Commands.INFI_SWAP. Infinity's six-field PoolKey
and custom Permit2 are incompatible with Uniswap V4. No Bin or multi-hop routing.
"""
from __future__ import annotations

import json
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from eth_abi import decode, encode
from eth_utils import keccak

from sniper.execution.metadata_cache import BoundedLru

MANAGER = '0xa0ffb9c1ce1fe56963b0321b32e7a0302114058b'
QUOTER = '0xd0737c9762912dd34c3271197e362aa736df0926'
ROUTER = '0xd9c500dff816a1da21a48a732d3498bf09dc9aeb'
PERMIT2 = '0x31c2f6fcff4f8759b3bd5bf0e1084a055615c768'
WBNB = '0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c'
NATIVE = '0x' + '00' * 20
KEY_TYPE = '(address,address,address,address,uint24,bytes32)'
SWAP_TOPIC = '0x' + keccak(text='Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24,uint16)').hex()
_KEY_FIELDS = ('currency0', 'currency1', 'hooks', 'pool_manager', 'fee', 'parameters')
_KEYS = BoundedLru(256)
_WARM_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="infinity-source-key")
_WARM_SLOTS = threading.BoundedSemaphore(32)
_WARM_LOCK = threading.Lock()
_WARM_PENDING = set()
logger = logging.getLogger(__name__)


class InfinityReadUnavailable(RuntimeError):
    def __init__(self, failure_type):
        super().__init__(failure_type)
        self.failure_type = failure_type


def key_tuple(key):
    return tuple(key[f] for f in _KEY_FIELDS[:-1]) + (bytes.fromhex(key['parameters'][2:]),)


def verified_key(pid, key, mint):
    try:
        return (set(key) == set(_KEY_FIELDS) and key['pool_manager'] == MANAGER
                and key['currency0'] == NATIVE and key['currency1'] == str(mint).lower()
                and 0 <= int(key['fee']) <= 100000
                and '0x' + keccak(encode([KEY_TYPE], [key_tuple(key)])).hex() == str(pid).lower())
    except (ValueError, TypeError, KeyError):
        return False


def _store_key(pid, key):
    from sniper.persistence.db_core import get_connection
    from sniper.persistence.db_memecoin import put_evm_v4_pool_key
    conn = get_connection()
    try:
        put_evm_v4_pool_key(conn, chain_id='56', pool_id=pid, pool_manager=MANAGER, key=key)
    finally:
        conn.close()


def warm_receipt(chain, receipt, mint, *, read_fn, store_fn=None):
    """Receipt worker only: read the manager's immutable key, hash-bind it, persist it.

    No wide scans, retries, funded RPC or negative cache. A failed read leaves the key unknown.
    """
    if str(chain) != '56' or str((receipt or {}).get('status')) not in ('0x1', '1'):
        return 0
    seen, stored = set(), 0
    for log in receipt.get('logs') or ():
        topics = log.get('topics') or ()
        if (str(log.get('address')).lower() != MANAGER or len(topics) != 3
                or str(topics[0]).lower() != SWAP_TOPIC):
            continue
        pid = str(topics[1]).lower()
        if pid in seen:
            continue
        seen.add(pid)
        try:
            def load():
                data = '0x' + keccak(text='poolIdToPoolKey(bytes32)')[:4].hex() + pid[2:]
                raw = read_fn('eth_call', [{'to': MANAGER, 'data': data}, 'latest'])
                values = decode([KEY_TYPE], bytes.fromhex(raw[2:]))[0]
                key = dict(zip(_KEY_FIELDS, values))
                key['parameters'] = '0x' + key['parameters'].hex()
                if not verified_key(pid, key, mint):
                    return None
                (store_fn or _store_key)(pid, key)
                return key
            if (load() if store_fn else _KEYS.get_or_load((pid, str(mint).lower()), load)):
                stored += 1
        except Exception as exc:
            logger.warning("Infinity key warm failed pool=%s kind=%s", pid, type(exc).__name__)
            continue
    return stored


def stored_keys(mint, *, require_complete=False):
    """Indexed immutable-key lookup only; never discover a pool on the order thread."""
    from sniper.persistence.db_core import get_readonly_connection
    conn = None
    try:
        conn = get_readonly_connection()
        rows = list(conn.execute(
            "SELECT pool_id,key_json FROM memecoin_evm_v4_pool_keys "
            "WHERE network_id='56' AND pool_manager=? "
            "AND lower(json_extract(key_json,'$.currency1'))=? LIMIT " + ("9" if require_complete else "8"),
            (MANAGER, str(mint).lower())))
        keys = [(pid, json.loads(raw)) for pid, raw in rows]
        # A bounded quote shortlist is not a complete silence witness.
        if require_complete and (len(keys) > 8 or any(not verified_key(pid, key, mint) for pid, key in keys)):
            raise InfinityReadUnavailable('cache_incomplete')
        return tuple((pid, key) for pid, key in keys if verified_key(pid, key, mint))
    except InfinityReadUnavailable:
        raise
    except Exception as exc:
        raise InfinityReadUnavailable('cache_read_failed') from exc
    finally:
        if conn is not None:
            conn.close()


def warm_receipt_offpath(chain, receipt, mint, *, read_fn):
    """Bounded, deduplicated key read; never delay the source-price receipt worker."""
    if str(chain) != '56' or not any(
            str(log.get('address')).lower() == MANAGER for log in (receipt or {}).get('logs', ())):
        return None
    identity = (str((receipt or {}).get('transactionHash')), str(mint).lower())
    with _WARM_LOCK:
        if identity in _WARM_PENDING or not _WARM_SLOTS.acquire(blocking=False):
            return None
        _WARM_PENDING.add(identity)
    def work():
        try:
            return warm_receipt(chain, receipt, mint, read_fn=read_fn)
        finally:
            with _WARM_LOCK:
                _WARM_PENDING.discard(identity)
            _WARM_SLOTS.release()
    try:
        return _WARM_POOL.submit(work)
    except Exception:
        with _WARM_LOCK:
            _WARM_PENDING.discard(identity)
        _WARM_SLOTS.release()
        return None


def quote_amount(key, buy, amount, *, rpc_url, http=None, rpc_fn=None):
    from sniper.execution.evm_read_rpc import _rpc, _freshest_read_member
    params_type = '(' + KEY_TYPE + ',bool,uint128,bytes)'
    data = ('0x' + keccak(text='quoteExactInputSingle(' + params_type + ')')[:4].hex()
            + encode([params_type], [(key_tuple(key), buy, int(amount), b'')]).hex())
    try:
        response = (rpc_fn or _rpc)(_freshest_read_member(rpc_url), 'eth_call',
                                   [{'to': QUOTER, 'data': data}, 'latest'], http=http)
    except Exception as exc:
        raise InfinityReadUnavailable('rpc_transport') from exc
    error = (response or {}).get('error')
    if error:
        if 'revert' in str(error).lower():
            return None
        raise InfinityReadUnavailable('rpc_transport')
    try:
        amount_out, _gas = decode(['uint256', 'uint256'], bytes.fromhex(response['result'][2:]))
    except Exception as exc:
        raise InfinityReadUnavailable('malformed_response') from exc
    return amount_out or None


def build_quote(key, pid, buy, amount, out, taker, *, slippage_bps=None,
                rpc_url=None, http=None, acct=None, now_fn=None):
    from sniper.settings import memecoin as cfg
    from sniper.execution.evm_v4_quote import _permit2_input
    from sniper.services.memecoin_evm_gas_topup import WETH_CALL_GAS_LIMIT
    bps = int(slippage_bps if slippage_bps is not None else cfg.MEMECOIN_ENTRY_SLIPPAGE_PCT * 100)
    minimum = int(out) * (10000 - bps) // 10000
    if not 0 <= bps < 10000 or minimum <= 0:
        return None
    now = now_fn or time.time
    ptype = '(' + KEY_TYPE + ',bool,uint128,uint128,bytes)'
    swap = encode([ptype], [(key_tuple(key), buy, int(amount), minimum, b'')])
    cin, cout = (NATIVE, key['currency1']) if buy else (key['currency1'], NATIVE)
    # A native exit is taken by the router and wrapped to the sender, never left as native dust.
    take = encode(['address', 'uint256'], [cout, minimum]) if buy else encode(
        ['address', 'address', 'uint256'], [cout, '0x' + '00' * 19 + '02', 0])
    actions = bytes.fromhex('060c0f' if buy else '060c0e')
    infi = encode(['bytes', 'bytes[]'], [actions, [swap, encode(['address', 'uint256'], [cin, int(amount)]), take]])
    if buy:
        commands, inputs = b'\x10', [infi]
    else:
        if acct is None:
            return None
        permit = _permit2_input('56', acct, cin, ROUTER, amount, rpc_url, http=http,
                                now_fn=now, permit2=PERMIT2)
        if permit is None:
            raise InfinityReadUnavailable('permit_unavailable')
        commands = bytes.fromhex('0a100b')
        inputs = [bytes.fromhex(permit), infi, encode(['address', 'uint256'],
                  ['0x' + '00' * 19 + '01', 1 << 255])]
    data = ('0x' + keccak(text='execute(bytes,bytes[],uint256)')[:4].hex()
            + encode(['bytes', 'bytes[]', 'uint256'], [commands, inputs,
                  int(now()) + int(cfg.memecoin_evm_swap_deadline_s('56', 120))]).hex())
    gas = int(cfg.MEMECOIN_EVM_DEFAULT_ROUTE_GAS)
    return {'sellAmount': str(amount), 'buyAmount': str(out), 'minBuyAmount': str(minimum),
            'liquidityAvailable': True,
            'pre_transactions': ([{'to': WBNB, 'data': '0x2e1a7d4d' + format(amount, '064x'),
                                  'value': '0x0', 'gas': str(WETH_CALL_GAS_LIMIT)}] if buy else []),
            'transaction': {'to': ROUTER, 'data': data, 'value': hex(amount) if buy else '0x0',
                            'gas': str(gas if gas > 0 else 900000)},
            'issues': {'allowance': None if buy else {'spender': PERMIT2}},
            'route': {'protocol': 'pancake_infinity', 'pool_id': pid, 'pool_key': key,
                      'quote_currency': NATIVE, 'pool_fee': key['fee'], 'hooked': key['hooks'] != NATIVE}}


def dex_infinity_quote(chain, sell, buy, amount, taker, *, rpc_url=None, http=None,
                       slippage_bps=None, acct=None):
    if str(chain) != '56' or not rpc_url or not 0 < int(amount) < (1 << 128):
        return None
    is_buy = str(sell).lower() == WBNB
    if str(buy if not is_buy else sell).lower() != WBNB:
        return None
    mint = str(buy if is_buy else sell).lower()
    best = None
    for pid, key in stored_keys(mint):
        out = quote_amount(key, is_buy, amount, rpc_url=rpc_url, http=http)
        if out and (best is None or out > best[0]):
            best = (out, pid, key)
    if best is None:
        return None
    out, pid, key = best
    q = build_quote(key, pid, is_buy, int(amount), out, taker, rpc_url=rpc_url,
                    http=http, slippage_bps=slippage_bps, acct=acct)
    if q is not None:
        q['route']['pool_key'] = key
    return q
