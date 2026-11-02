"""Missed Pons launches and ERC-20 counters: SPORE chain evidence, 2026-10-03."""
import json
import sqlite3
from pathlib import Path

import pytest

from sniper.services import memecoin_volume_writer as w
from sniper.settings import memecoin as cfg

CHAIN = '4663'
CURVE = '0xbdb19132df796efc36f7f893b8231d113e68ca6a'
MINT = '0xa5127fae2d0986a4cb6619b9c4ec53461726454b'
FACTORY = '0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e'
COUNTER = '0xaa07a0e9209e16ac99708c3ec70159c6ef3128a3'
EVIDENCE = json.loads((Path(__file__).parent / 'fixtures/pons/spore_metadata.json').read_text())


@pytest.fixture
def tape(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_V4_LAUNCHPADS', {CHAIN: ({
        'address': FACTORY, 'info_selector': '3cf28b5a', 'coin_word': 0, 'counter_word': 4},)})
    c = w.connect(tmp_path / 'volume.db')
    t = w.VolumeTape(c, CHAIN, 1690)
    for n, ts in [(1, 1690), (2, 1800), (3, 1900), (4, 2000)]:
        t.head({'number': hex(n), 'timestamp': hex(ts), 'hash': hex(n)}, ts)
    t.log_pons(dict(EVIDENCE['buy_log'], blockNumber='0x4', blockHash='0x4'), 2000)
    yield t
    c.close()


def rpc(*, changes=None, calls=None, counter=COUNTER):
    info = EVIDENCE['info']
    info = info[:2+4*64] + '0'*24 + counter[2:] + info[2+5*64:]
    values = {(CURVE, '0xfc0c546a'): EVIDENCE['token'],
              (CURVE, '0xc45a0155'): EVIDENCE['factory'],
              (FACTORY, '0x3cf28b5a' + '0'*24 + MINT[2:]): info,
              (MINT, '0x313ce567'): EVIDENCE['decimals'],
              (MINT, '0x18160ddd'): EVIDENCE['supply'],
              (COUNTER, '0x313ce567'): '0x' + f'{18:064x}'}
    values.update(changes or {})

    def call(reqs):
        if calls is not None:
            calls.extend(reqs)
        out = []
        for method, params in reqs:
            assert method == 'eth_call' and params[1] == 'latest'
            out.append(values[(params[0]['to'], params[0]['data'])])
        return out
    return call


def resolve(tape, call=None, now=2000):
    from sniper.services.memecoin_pons_metadata import resolve_pons_curves
    return resolve_pons_curves(tape.conn, CHAIN, call or rpc(), now)


def read(tape, now=2001, chain=CHAIN):
    from sniper.services.memecoin_pons_metadata import read_cached_supply
    path = tape.conn.execute('PRAGMA database_list').fetchone()[2]
    return read_cached_supply(chain, MINT, path=path, now=now)


def test_launch_event_does_not_prove_a_native_counter(tape):
    tape.register_pons_curve(CURVE, MINT, 2000)
    row = tape.conn.execute('SELECT token0,token1,quote_side FROM pool_meta WHERE pool=?', (CURVE,)).fetchone()
    assert tuple(row) == (None, MINT, None)
    assert {r[0] for r in tape.conn.execute('SELECT mint FROM onchain_pool_protocols WHERE pool=?', (CURVE,))} == {MINT}


def test_new_launch_is_eligible_for_verification_immediately(tape):
    tape.register_pons_curve(CURVE, MINT, 2000)
    assert resolve(tape, now=2000) == 1


def test_launch_warms_metadata_and_supply_before_the_first_swap(tape):
    tape.conn.execute('DELETE FROM pool_ticks'); tape.conn.commit()
    tape.register_pons_curve(CURVE,MINT,2000)
    assert resolve(tape) == 1
    assert read(tape)['supply'] == 1e9


def test_missed_launch_recovers_verified_counter_and_atomic_supply(tape):
    assert resolve(tape) == 1
    meta = tape.conn.execute('SELECT token0,token1,quote_side,token0_decimals,token1_decimals,resolve_state FROM pool_meta WHERE pool=?', (CURVE,)).fetchone()
    assert tuple(meta) == (COUNTER, MINT, 0, 18, 18, 'pons_verified')
    supply = tape.conn.execute('SELECT total_supply,typeof(total_supply) FROM volume_token_supply WHERE token=?', (MINT,)).fetchone()
    assert tuple(supply) == ('1000000000000000000000000000', 'text')
    assert read(tape)['supply'] == 1e9
    assert read(tape, chain='56')['outcome'] == 'missing'


def test_erc20_quote_uses_its_own_price_and_source_exclusion(tape):
    assert resolve(tape) == 1
    tape.conn.execute('INSERT OR REPLACE INTO volume_quote_prices VALUES (?,?,?,?)', (CHAIN,COUNTER,0.09806167215112026,2000))
    tape.conn.execute('INSERT OR REPLACE INTO volume_quote_price_evidence VALUES (?,?,?,?)', (CHAIN,COUNTER,2000,'tape'))
    tape.conn.commit()
    tape.set_quote_price(2700, 2000)
    out = w.measure(tape.conn, CHAIN, MINT, 2001)
    amount = int(EVIDENCE['buy_log']['data'][2:66], 16) / 1e18
    assert out['usd'] == pytest.approx(amount * 0.09806167215112026)
    excluded = w.measure(tape.conn, CHAIN, MINT, 2001, exclude_tx=EVIDENCE['buy_log']['transactionHash'])
    assert excluded['swaps'] == 0


def test_native_counter_is_supported_without_calling_zero_address(tape):
    calls = []
    assert resolve(tape, rpc(counter=w.NATIVE, calls=calls)) == 1
    assert tape.conn.execute('SELECT token0,token0_decimals FROM pool_meta WHERE pool=?', (CURVE,)).fetchone()[:] == (w.NATIVE,18)
    assert not any(params[0]['to'] == w.NATIVE for _, params in calls)


def test_unpriced_counter_remains_unknown_volume(tape):
    assert resolve(tape) == 1
    tape.set_quote_price(2700,2000)
    out = w.measure(tape.conn,CHAIN,MINT,2001)
    assert out['unpriced_pools'] and out['swaps'] == 0


def test_legacy_native_assumption_is_corrected(tape):
    tape.register_pons_curve(CURVE,MINT,1800,counter=w.NATIVE)
    tape.conn.execute('UPDATE pool_meta SET resolve_state=NULL WHERE pool=?',(CURVE,)); tape.conn.commit()
    assert resolve(tape) == 1
    assert not tape.conn.execute('SELECT 1 FROM onchain_pool_protocols WHERE pool=? AND mint=?',(CURVE,w.NATIVE)).fetchone()
    assert tape.conn.execute('SELECT token0 FROM pool_meta WHERE pool=?',(CURVE,)).fetchone()[0] == COUNTER


def test_launch_redelivery_preserves_verified_counter(tape):
    assert resolve(tape) == 1
    tape.register_pons_curve(CURVE, MINT, 2001)
    assert tape.conn.execute('SELECT token0,resolve_state FROM pool_meta WHERE pool=?', (CURVE,)).fetchone()[:] == (COUNTER,'pons_verified')


def test_launch_cannot_erase_a_concurrent_resolver_commit(tape):
    tape.register_pons_curve(CURVE,MINT,1900)
    path = tape.conn.execute('PRAGMA database_list').fetchone()[2]
    other = w.connect(path); resolved = w.VolumeTape.__new__(w.VolumeTape)
    resolved.conn, resolved.chain = other, CHAIN
    actual = tape.conn
    class Interleaved:
        fired = False
        def execute(self, sql, args=()):
            if not self.fired and sql.startswith(('SELECT token1,resolve_state','INSERT')):
                self.fired = True
                if sql.startswith('SELECT'):
                    stale = actual.execute(sql,args).fetchone()
                    resolved.register_pons_curve(CURVE,MINT,2000,counter=COUNTER)
                    return SimpleCursor(stale)
                resolved.register_pons_curve(CURVE,MINT,2000,counter=COUNTER)
            return actual.execute(sql,args)
        def __getattr__(self,name): return getattr(actual,name)
        def __enter__(self): actual.__enter__(); return self
        def __exit__(self,*args): return actual.__exit__(*args)
    class SimpleCursor:
        def __init__(self,row): self.row=row
        def fetchone(self): return self.row
    tape.conn = Interleaved()
    try:
        tape.register_pons_curve(CURVE,MINT,2001)
        assert actual.execute('SELECT token0,resolve_state FROM pool_meta WHERE pool=?',(CURVE,)).fetchone()[:] == (COUNTER,'pons_verified')
        assert actual.execute('SELECT 1 FROM onchain_pool_protocols WHERE pool=? AND mint=?',(CURVE,COUNTER)).fetchone()
    finally:
        tape.conn=actual; other.close()


@pytest.mark.parametrize('response', [None, '0x', '0x'+'ff'*32, w.RpcErrorReply({'code':429,'message':'rate limit'})])
def test_bad_or_missing_factory_never_establishes_identity(tape, response):
    assert resolve(tape, rpc(changes={(CURVE,'0xc45a0155'):response})) == 0
    assert read(tape)['outcome'] == 'missing'
    assert not tape.conn.execute("SELECT 1 FROM pool_meta WHERE pool=? AND resolve_state='pons_verified'", (CURVE,)).fetchone()
    assert resolve(tape, now=2031) == 1, 'transient failure must not poison the venue permanently'


@pytest.mark.parametrize('word', [0,1])
def test_registered_factory_info_must_agree_on_token_and_curve(tape, word):
    bad = EVIDENCE['info']
    bad = bad[:2+word*64] + '0'*24 + 'ba'*20 + bad[2+(word+1)*64:]
    assert resolve(tape, rpc(changes={(FACTORY,'0x3cf28b5a'+'0'*24+MINT[2:]):bad})) == 0
    assert read(tape)['outcome'] == 'missing'


def test_partial_rpc_batch_never_produces_a_verified_curve(tape):
    assert resolve(tape, lambda reqs: []) == 0
    assert read(tape)['outcome'] == 'missing'


def test_failed_supply_does_not_hide_verified_venue(tape):
    assert resolve(tape, rpc(changes={(MINT,'0x18160ddd'):None})) == 1
    assert read(tape)['outcome'] == 'missing'
    assert tape.conn.execute('SELECT token0 FROM pool_meta WHERE pool=?', (CURVE,)).fetchone()[0] == COUNTER
    assert resolve(tape, now=2031) == 1
    assert read(tape, now=2032)['supply'] == 1e9


def test_supply_refresh_is_bounded_and_expiry_does_not_become_zero(tape):
    assert resolve(tape) == 1
    calls = []
    assert resolve(tape, rpc(calls=calls), now=2100) == 0 and calls == []
    assert read(tape, now=2601)['outcome'] == 'stale'
    assert read(tape, now=2601)['supply'] is None
    assert resolve(tape, now=2301) == 1
    assert read(tape, now=2601)['supply'] == 1e9


@pytest.mark.parametrize('protocol', ['v3','v4'])
@pytest.mark.parametrize('failure', [None,'factory','partial'])
def test_migrated_coin_refreshes_supply_from_recent_registered_activity(tape, protocol, failure):
    assert resolve(tape) == 1
    tape.conn.execute('DELETE FROM pool_ticks')
    if protocol == 'v3':
        pool = '0x' + 'ab' * 20
        tape.register_v3_pool(pool, w.NATIVE, MINT, 2000)
    else:
        key = dict(currency0=w.NATIVE,currency1=MINT,fee=10000,tick_spacing=200,hooks=w.NATIVE)
        pool = w.v4_pool_id(key)
        tape.register_pool(pool, key, 2000)
    tape.conn.execute(
        'INSERT INTO pool_ticks (chain_id,pool,kind,block_no,tx_sig,log_index,observed_at) '
        'VALUES (?,?,?,?,?,?,?)', (CHAIN,pool,'v3_swap',5,'0xmigrated',1,w.iso(7000)))
    tape.conn.commit()
    assert read(tape, now=7001)['outcome'] == 'stale'
    calls = []
    call = (lambda reqs: []) if failure == 'partial' else rpc(
        calls=calls, changes={(CURVE,'0xc45a0155'):None} if failure == 'factory' else None)
    assert resolve(tape, call, now=7001) == (0 if failure else 1)
    if failure:
        assert read(tape, now=7002)['outcome'] == 'stale'
        return
    assert read(tape, now=7002)['supply'] == 1e9
    assert any(params[0]['to'] == FACTORY for _,params in calls), 'migration must not skip factory proof'


@pytest.mark.parametrize('kind', ['unregistered','wrong_chain','wrong_mint','old_activity'])
def test_migrated_refresh_ignores_unbound_or_stale_activity(tape, kind):
    assert resolve(tape) == 1
    tape.conn.execute('DELETE FROM pool_ticks')
    pool = '0x' + 'ab' * 20
    token = '0x' + 'cd' * 20 if kind == 'wrong_mint' else MINT
    if kind != 'unregistered':
        tape.register_v3_pool(pool, w.NATIVE, token, 2000)
    tape.conn.execute(
        'INSERT INTO pool_ticks (chain_id,pool,kind,block_no,tx_sig,log_index,observed_at) '
        'VALUES (?,?,?,?,?,?,?)', ('56' if kind == 'wrong_chain' else CHAIN,pool,
                                  'v3_swap',5,'0xmigrated',1,w.iso(2000 if kind == 'old_activity' else 7000)))
    tape.conn.commit()
    calls = []
    assert resolve(tape, rpc(calls=calls), now=7001) == 0 and calls == []
    assert read(tape, now=7001)['outcome'] == 'stale'


def test_migrated_refresh_keeps_original_priority_and_sixteen_pool_bound(tape):
    from sniper.services.memecoin_pons_metadata import resolve_pons_curves
    assert resolve(tape) == 1
    tape.conn.execute('DELETE FROM pool_ticks')
    pool = '0x' + 'ab' * 20
    tape.register_v3_pool(pool, w.NATIVE, MINT, 2000)
    tape.conn.execute(
        'INSERT INTO pool_ticks (chain_id,pool,kind,block_no,tx_sig,log_index,observed_at) '
        'VALUES (?,?,?,?,?,?,?)', (CHAIN,pool,'v3_swap',5,'0xmigrated',1,w.iso(7000)))
    for n in range(17):
        tape.register_pons_curve('0x' + f'{n+1:040x}', MINT, 7000)
    calls = []
    def missing(reqs):
        calls.extend(reqs)
        return [None] * len(reqs)
    assert resolve_pons_curves(tape.conn, CHAIN, missing, 7001, limit=999) == 0
    assert len({params[0]['to'] for _,params in calls}) == 16
    assert not any(params[0]['to'] == CURVE for _,params in calls)
    assert read(tape, now=7002)['outcome'] == 'stale'


@pytest.mark.parametrize('column,value', [('total_supply','0'),('total_supply','1e27'),('decimals',256),('observed',3000),('observed',None)])
def test_invalid_cache_rows_remain_unknown(tape, column, value):
    assert resolve(tape) == 1
    tape.conn.execute(f'UPDATE volume_token_supply SET {column}=?', (value,)); tape.conn.commit()
    assert read(tape)['supply'] is None


def test_cache_reader_does_not_create_a_missing_store(tmp_path):
    from sniper.services.memecoin_pons_metadata import read_cached_supply
    path = tmp_path / 'absent.db'
    assert read_cached_supply(CHAIN,MINT,path=path,now=2000)['outcome'] == 'unavailable'
    assert not path.exists()


def test_rpc_has_no_open_write_transaction(tape):
    wrapped = rpc()
    def call(reqs):
        assert not tape.conn.in_transaction
        return wrapped(reqs)
    assert resolve(tape, call) == 1
