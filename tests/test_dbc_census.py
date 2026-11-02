import base64
import struct

import base58
import pytest

from sniper.feeds.yellowstone import geyser_pb2 as pb
from sniper.research import dbc_census as C
from sniper.research import dbc_census_tape as T

POOL = base58.b58encode(bytes([7]) * 32).decode()
CONFIG = base58.b58encode(bytes([8]) * 32).decode()
BASE = base58.b58encode(bytes([9]) * 32).decode()


def payload(direction=1, actual_in=2_000_000_000, output=5_000_000):
    return (bytes.fromhex(C.EVENT_CPI.hex()) + C.EVT_SWAP + bytes([7]) * 32 + bytes([8]) * 32 +
            bytes([direction, 0]) + struct.pack('<QQQQ', actual_in, 0, actual_in, output) + (123).to_bytes(16, 'little') +
            struct.pack('<QQQ', 10, 2, 0) + struct.pack('<QQ', actual_in, 1))


def frame(data=None, err=False, sig=1, disc=None):
    u = pb.SubscribeUpdate()
    u.transaction.slot = 50
    info = u.transaction.transaction
    info.index = 3
    info.signature = bytes([sig]) * 64
    info.transaction.message.account_keys.extend([bytes([1]) * 32, base58.b58decode(C.PROGRAM)])
    if err:
        info.meta.err.err = b'\x01\x02'
    if data is not None:
        g = info.meta.inner_instructions.add()
        ix = g.instructions.add()
        ix.program_id_index = 1
        ix.data = data
    return u.SerializeToString()


def census():
    c = T.connect(':memory:')
    T.start_run(c, 'r', 1000.0, 6 * 3600, C.SOURCE_FILTER)
    return C.Census(c, 'r', clock=lambda: 1100.0), c


def test_swap_row_unknown_pool_facts_are_null_then_resolved():
    k, c = census()
    k.process(frame(payload()))
    row = c.execute('SELECT direction,amount_in,amount_out,mint,quote_mint,quote_decimals,err,pool_create_slot,'
                    'is_first_swap_for_pool,tx_index FROM swaps').fetchone()
    assert row == ('buy', '2000000000', '5000000', None, None, None, '', None, None, 3)
    seen = []

    def rpc(method, params):
        seen.append(method)
        if params[1]['encoding'] == 'base64':
            return {'value': [{'data': [base64.b64encode(pool_bytes()).decode()]},
                              {'data': [base64.b64encode(config_bytes()).decode()]}]}
        return {'value': [{'data': {'parsed': {'info': {'decimals': 6}}}}]}
    import sniper.execution.meteora_dbc_buy as D
    pool_bytes = _pool
    config_bytes = lambda: _config()
    k.resolve_pending(rpc)
    r = c.execute('SELECT mint,quote_mint,quote_decimals,base_decimals FROM swaps').fetchone()
    assert r == (BASE, T.WSOL, 9, 6) and seen == ['getMultipleAccounts'] * 2


def _pool():
    import sniper.execution.meteora_dbc_buy as D
    b = bytearray(D.POOL_LEN)
    b[:8] = D.POOL_DISCRIMINATOR
    b[D.OFF_BASE_MINT:D.OFF_BASE_MINT + 32] = bytes([9]) * 32
    return bytes(b)


def _config():
    import sniper.execution.meteora_dbc_buy as D
    b = bytearray(D.CONFIG_LEN)
    b[:8] = D.CONFIG_DISCRIMINATOR
    b[D.OFF_QUOTE_MINT:D.OFF_QUOTE_MINT + 32] = base58.b58decode(T.WSOL)
    b[D.OFF_THRESHOLD:D.OFF_THRESHOLD + 8] = struct.pack('<Q', 1)
    return bytes(b)


def test_failed_tx_kept_with_err_and_swap_fields_null_and_counted():
    k, c = census()
    k.process(frame(err=True))
    assert c.execute('SELECT err,direction,pool FROM swaps').fetchone() == ('0102', None, None)
    assert c.execute('SELECT txs,failed FROM tx_minutes').fetchone() == (1, 1)


def test_evtswap2_only_is_skipped_visibly_and_garbage_event_is_not_a_swap():
    k, c = census()
    k.process(frame(C.EVENT_CPI + C.EVT_SWAP2 + bytes(40), sig=2))
    k.process(frame(C.EVENT_CPI + C.EVT_SWAP + bytes(5), sig=3))
    assert c.execute('SELECT COUNT(*) FROM swaps').fetchone()[0] == 0
    assert sorted(r[0] for r in c.execute('SELECT reason FROM skips')) == ['evtswap2_only', 'swap_decode']


def test_ring_overflow_counts_drops():
    k, c = census()
    k.QUEUE_MAX = 2
    for i in range(5):
        k.push(b'x', 1.0)
    k.flush_drops()
    assert c.execute('SELECT SUM(n) FROM drops').fetchone()[0] == 3


def test_coverage_pass_fail_and_429_partial():
    k, c = census()
    for i in range(20):
        T.count_tx(c, 'r', 1100.0, False)
    T.insert_swap(c, 'r', 'anchor', 0, 1400.0, err='')
    page = [{'signature': 's%d' % i, 'blockTime': 1100 + i} for i in range(20)] + [{'signature': 'old', 'blockTime': 900}]
    ok = C.coverage_check(c, 'r', lambda m, p: page, [(1000.0, 1300.0)])
    assert ok['verdict'] == 'pass' and ok['windows'][0]['rpc'] == 20
    short = [{'signature': 's%d' % i, 'blockTime': 1100 + i} for i in range(30)] + [{'signature': 'old', 'blockTime': 900}]
    assert C.coverage_check(c, 'r', lambda m, p: short, [(1000.0, 1300.0)])['verdict'] == 'partial'

    def boom(m, p):
        raise OSError('HTTP 429')
    r = C.coverage_check(c, 'r', boom, [(1000.0, 1300.0)])
    assert r['verdict'] == 'partial' and r['windows'][0]['reason'] == 'OSError' and r['calls'] == 1


def test_refused_subscribe_is_fatal_and_plain_disconnect_is_not():
    class Code:
        name = 'RESOURCE_EXHAUSTED'

    class Rpc(Exception):
        def code(self):
            return Code()
    assert C.stream_error_is_fatal(Rpc())
    assert C.stream_error_is_fatal(RuntimeError('max streams reached'))
    assert not C.stream_error_is_fatal(OSError('connection reset'))
    assert C.MAX_RECONNECTS == 3 and min(C.RECONNECT_BACKOFF_S) >= 30
