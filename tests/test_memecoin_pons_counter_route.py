"""Generic counter-token Pons routes: atomic funding, refunds and exit custody."""
from eth_abi import decode
import pytest

from sniper.execution.evm_pons_counter_quote import CounterCache, buy_quote, sell_quote
from sniper.execution.evm_relay_quote import RELAY_APPROVAL_PROXY, RELAY_ROUTER

WETH = '0x0bd7d308f8e1639fab988df18a8011f41eacad73'
MINT, CURVE, ROUTER, POOL, TAKER = ['0x' + ch * 40 for ch in 'abcde']
TYPES = ['address[]', 'uint256[]', '(address,bool,uint256,bytes)[]',
         'address', 'address', 'bytes']


def state(counter):
    return dict(mint=MINT, curve=CURVE, counter=counter, router=ROUTER, bridge_pool=POOL,
                bridge_fee=3000, max_input=10**18, max_counter=10**9,
                reverse_cash=97 * 10**16, quote_reserve=10**12,
                token_reserve=10**27, fee_bps=100, tax_bps=0,
                sellable=10**27, graduated=0, block_number=123)


@pytest.mark.parametrize('counter', ['0x' + ch * 40 for ch in '123'])
def test_any_counter_builds_one_atomic_buy_and_a_full_balance_exit(counter):
    s = state(counter)
    q = buy_quote(s, 10**17, TAKER, 1000)
    assert q['transaction']['to'] == RELAY_APPROVAL_PROXY
    assert q['transaction']['value'] == '0x0' and not q.get('pre_transactions')
    assert q['issues']['allowance']['spender'] == RELAY_APPROVAL_PROXY
    tokens, amounts, calls, refund, recipient, _ = decode(TYPES, bytes.fromhex(q['transaction']['data'][10:]))
    assert tokens == (WETH,) and amounts == (10**17,)
    assert refund == recipient == TAKER and all(not c[1] and c[2] == 0 for c in calls)
    bridge = decode(['(address,address,uint24,address,uint256,uint256,uint160)'], calls[1][3][4:])[0]
    assert bridge[:4] == (WETH, counter, 3000, RELAY_ROUTER)
    assert bridge[5] == 10**17  # Exact output, bounded spend; no loose counter inventory.
    curve_amount, floor, curve_recipient = decode(['uint256','uint256','address'], calls[3][3][4:])
    assert curve_amount == bridge[4] and curve_recipient == TAKER
    assert floor == int(q['minBuyAmount']) > 0
    assert int(q['buyAmount']) >= floor
    cleaned, destinations, _, _ = decode(['address[]','address[]','uint256[]','bytes'], calls[-1][3][4:])
    assert WETH in cleaned and all(a == TAKER for a in destinations)
    assert -10 < q['route']['round_trip_pct'] < 0
    exit_q = sell_quote(s, int(q['buyAmount']), TAKER, 10**8, 10**17, 1000)
    _, qty, exits, _, _, _ = decode(TYPES, bytes.fromhex(exit_q['transaction']['data'][10:]))
    assert qty == (int(q['buyAmount']),)
    _, _, receiver = decode(['uint256','uint256','address'], exits[1][3][4:])
    assert receiver == ROUTER
    final = decode(['(address,address,uint24,address,uint256,uint256,uint160)'], exits[2][3][4:])[0]
    assert final[:4] == (counter, WETH, 3000, TAKER)
    assert final[4] == 0  # Router consumes ALL counter proceeds in the same transaction.
    assert final[5] == int(exit_q['minBuyAmount']) > 0


def test_prepared_quote_never_waits_or_reads_rpc_and_expired_state_does_not_trade():
    cache = CounterCache(clock=lambda: 100)
    cache.publish({MINT: state('0x' + '1' * 40)}, started=99.5)
    assert cache.quote(WETH, MINT, 10**17, TAKER, 1000) is not None
    assert cache.quote(WETH, MINT, 2 * 10**18, TAKER, 1000) is None
    assert cache.quote(MINT, WETH, 10**17, TAKER, 1000) is None  # Exits require fresh reads.
    cache.publish({MINT: state('0x' + '1' * 40)}, started=97)
    assert cache.quote(WETH, MINT, 10**17, TAKER, 1000) is None
    cache.publish({}, started=100)
    assert cache.quote(WETH, MINT, 10**17, TAKER, 1000) is None


@pytest.mark.parametrize('changes', [dict(graduated=1), dict(quote_reserve=0),
    dict(sellable=1), dict(fee_bps=10000), dict(tax_bps=-1), dict(counter=MINT),
    dict(reverse_cash=0), dict(max_input=0)])
def test_unknown_wrong_or_closed_venue_does_not_build(changes):
    s = state('0x' + '1' * 40) | changes
    assert buy_quote(s, 10**17, TAKER, 1000) is None


def test_receipt_binds_the_pons_leg_without_inventing_a_post_trade_price():
    from eth_abi import encode
    from sniper.services.memecoin_evm_post_trade import source_post_trade, TRANSFER
    from sniper.services.memecoin_source_fill import _source_post
    from sniper.services.memecoin_tick_stream import PONS_V2_CURVE_BUY
    topic = lambda a: '0x' + a[2:].rjust(64, '0')
    receipt = dict(status='0x1', logs=[
        dict(address=MINT, topics=[TRANSFER, topic(CURVE), topic(TAKER)], data=hex(10**24)),
        dict(address=CURVE, topics=[PONS_V2_CURVE_BUY, topic(TAKER), topic(TAKER)],
             data='0x' + encode(['uint256'] * 4, [10**8, 10**24, 10**6, 0]).hex())])
    post = source_post_trade('4663', receipt, MINT, owner=TAKER)
    assert post['venue'] == 'pons_v2' and post['pool'] == CURVE
    assert post['reason'] == 'pons_post_state_unavailable'
    assert 'ratio' not in post and 'sqrt_price_x96' not in post
    assert _source_post(post) == post
    assert source_post_trade('4663', receipt, MINT, owner=POOL)['reason'] == 'owner_received_nothing'


def _tenet_receipt():
    import json
    from pathlib import Path
    return json.loads((Path(__file__).parent / 'fixtures/memecoin/tenet_pons_split_buy_20261006.json').read_text())


def test_tenet_five_buys_in_one_pool_reach_the_fast_shared_route():
    from sniper.services.memecoin_evm_post_trade import source_post_trade
    from sniper.services.memecoin_rh_fast_decide import TapeState, decide
    from test_memecoin_rh_source_binding import CTX
    frame = _tenet_receipt()
    proof = frame['source_fill']
    post = source_post_trade('4663', frame['receipt'], proof['mint'], owner=proof['wallet'],
                             pool_key_fn=lambda chain, pool: frame['bridge_keys'].get(pool))
    assert post == dict(reason='pons_post_state_unavailable', venue='pons_v2',
        pool='0x06d4507c0cc824b12ed77aa5fbd321e6f96894a7',
        pool_input_atomic='147084107674349385', pool_output_atomic=proof['acquired_atomic'],
        receipt_routes_complete=True)
    proof['source_post'] = post
    sig = dict(wallet=proof['wallet'], source_user_id=proof['source_user_id'], mint=proof['mint'],
               tx_hash=proof['tx_hash'], seq=proof['block_number'],
               payment_token=proof['payment_token'], paid=int(proof['payment_atomic']))
    tape = TapeState({}.get)
    tape.learn_source_fills([proof])
    out = decide(sig, tape=tape, **dict(CTX, decimals={proof['payment_token']: 6}))
    assert out['reason'] == 'would_send' and out['route_kind'] == 'pons_shared'
    assert out['src_out'] == int(proof['acquired_atomic']) and out['post_ratio'] is None


@pytest.mark.parametrize('damage,reason', [
    ('output_mismatch', 'no_venue_leg'), ('missing_buy', 'no_venue_leg'),
    ('malformed_buy', 'no_venue_leg'), ('nonpositive_buy', 'no_venue_leg'),
    ('second_pool', 'split_route'), ('foreign_owner', 'owner_received_nothing'),
    ('unknown_bridge', 'pons_post_state_unavailable'),
])
def test_tenet_same_pool_aggregation_keeps_receipt_binding(damage, reason):
    from eth_abi import encode
    from sniper.services.memecoin_evm_post_trade import source_post_trade, TRANSFER
    from sniper.services.memecoin_tick_stream import PONS_V2_CURVE_BUY
    from sniper.services.memecoin_source_fill import validate_source_fill
    frame = _tenet_receipt()
    proof, receipt = frame['source_fill'], frame['receipt']
    buy = next(log for log in receipt['logs'] if log['topics'][0] == PONS_V2_CURVE_BUY)
    if damage in ('output_mismatch', 'nonpositive_buy'):
        amounts = list(decode(['uint256'] * 4, bytes.fromhex(buy['data'][2:])))
        amounts[1] = amounts[1] + 1 if damage == 'output_mismatch' else 0
        buy['data'] = '0x' + encode(['uint256'] * 4, amounts).hex()
    elif damage == 'missing_buy':
        receipt['logs'].remove(buy)
    elif damage == 'malformed_buy':
        buy['data'] = buy['data'][:-1]
    elif damage == 'second_pool':
        transfer = next(log for log in receipt['logs'] if log['topics'][0] == TRANSFER)
        buy['address'] = '0x' + 'ab' * 20
        transfer['topics'][1] = '0x' + buy['address'][2:].rjust(64, '0')
    elif damage == 'foreign_owner':
        proof['wallet'] = '0x' + 'ab' * 20
    post = source_post_trade('4663', receipt, proof['mint'], owner=proof['wallet'],
        pool_key_fn=lambda chain, pool: None if damage == 'unknown_bridge' else frame['bridge_keys'].get(pool))
    assert post['reason'] == reason
    if damage == 'unknown_bridge':
        assert post['receipt_routes_complete'] is False
        proof['source_post'] = post
        assert validate_source_fill(proof, proof) is None


def test_shared_rung_takes_prepared_counter_quote_without_rpc(monkeypatch):
    from sniper.execution import evm_pons_counter_quote as counter
    from sniper.execution.evm_pons_v2_quote import dex_pons_v2_quote
    cache = CounterCache(clock=lambda: 100)
    cache.publish({MINT: state('0x' + '1' * 40)}, started=100)
    monkeypatch.setattr(counter, 'CACHE', cache)
    def forbidden(*args):
        raise AssertionError('prepared entry made a network call')
    q = dex_pons_v2_quote('4663', WETH, MINT, 10**17, TAKER,
                          slippage_bps=1000, call_fn=forbidden)
    assert q['route']['protocol'] == 'pons_v2_counter'


def test_exact_receipt_spend_subtracts_refund_in_the_same_read(monkeypatch):
    from sniper.execution import evm_real_seams as seams
    from test_memecoin_confirmed_entry_not_dropped import _log, _receipt
    calls = []
    receipt = _receipt([_log(WETH, TAKER, ROUTER, 100),
                        _log(WETH, ROUTER, TAKER, 7),
                        _log(WETH, TAKER, TAKER, 3)])
    def rpc(*args, **kwargs):
        calls.append(args)
        return {'result': receipt}
    monkeypatch.setattr(seams, '_rpc', rpc)
    assert seams.erc20_sent_in_receipt('0xabc', WETH, TAKER, 'unused') == 93
    assert len(calls) == 1


def test_pons_source_enters_shared_lane_without_a_fake_pool_price():
    import os
    from sniper.services import memecoin_rh_fast_decide as fast
    from sniper.services import memecoin_rh_fast_lane as lane
    from test_memecoin_rh_source_binding import CTX, SIG
    from rh_source_fill_fixture import proof_for
    sig = dict(SIG, mint=MINT, paid=10**18, permit_nonce=1, arrival_utc='2026-10-05T00:00:00Z')
    proof = proof_for(sig, got=10**24, pool=CURVE, venue='pons_v2')
    proof['source_post'] = dict(reason='pons_post_state_unavailable', venue='pons_v2',
        pool=CURVE, pool_input_atomic='100', pool_output_atomic=str(10**24),
        receipt_routes_complete=True)
    tape = fast.TapeState({}.get)
    tape.learn_source_fills([proof])
    out = fast.decide(sig, tape=tape, **CTX)
    assert out['reason'] == 'would_send' and out['route_kind'] == 'pons_shared'
    assert out['post_ratio'] is None and out['src_out'] == 10**24
    payload = lane.lane_payload(sig, out, sent_epoch=1000, inputs={'eth_usd': CTX['eth_usd']})
    assert lane.check_sender(payload, 1, os.getuid(), detector_pid=1) is None
    bad = dict(payload, post_ratio=1.0)
    assert lane.check_sender(bad, 1, os.getuid(), detector_pid=1) == 'malformed'
    from types import SimpleNamespace
    from collections import deque
    accepted = []
    live = lane.LiveLane.__new__(lane.LiveLane)
    live.host_clock = lambda: 1000
    live.stopped, live.routes = False, {'pons_shared'}
    live.fills, live._pilot_pending, live.pilot_fills = 0, 0, 100
    live.send_times, live.max_sends_per_hour = deque(), 100
    live.shared_worker = SimpleNamespace(drain=lambda: None,
        offer=lambda p, at: accepted.append(p) or 'shared_queued')
    assert live.handle(payload, recv_epoch=1000, safety_reason=None)['reason'] == 'shared_queued'
    assert accepted == [payload]
    rejected = dict(payload, pool=POOL)
    assert live.handle(rejected, recv_epoch=1000, safety_reason=None)['reason'] == 'source_post_unbound'
    proof['source_post']['receipt_routes_complete'] = False
    tape = fast.TapeState({}.get)
    tape.learn_source_fills([proof])
    assert fast.decide(sig, tape=tape, **CTX)['reason'] != 'would_send'


@pytest.mark.parametrize('counter', ['0x' + ch * 40 for ch in '123'])
def test_preparation_derives_any_counter_and_pins_all_reads(tmp_path, monkeypatch, counter):
    from eth_abi import encode
    from eth_utils import keccak
    from sniper.execution import evm_pons_counter_quote as routes
    from sniper.execution.evm_pons_v2_quote import _GETTERS
    from sniper.settings import memecoin as cfg
    from sniper.services.memecoin_volume_writer import connect, iso
    c = connect(tmp_path / 'volume.db')
    c.execute('INSERT INTO pool_meta(chain_id,pool,token0,token1,resolved_at,resolve_state) VALUES(?,?,?,?,?,?)',
              ('4663', CURVE, counter, MINT, iso(1000), 'pons_verified'))
    c.execute('INSERT INTO volume_quote_prices VALUES(?,?,?,?)', ('4663', WETH, 175, 1000))
    factory, quoter = '0x' + '8' * 40, '0x' + '9' * 40
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_V4_LAUNCHPADS', {'4663': ({
        'address': factory, 'info_selector': '3cf28b5a', 'coin_word': 0, 'counter_word': 4},)})
    monkeypatch.setattr(cfg, 'memecoin_evm_v3_deployments', lambda _: [dict(router=ROUTER, quoter=quoter)])
    monkeypatch.setattr(cfg, 'memecoin_evm_v3_fee_tiers', lambda _: [3000])
    word = lambda n: '0x' + encode(['uint256'], [int(n, 16) if isinstance(n, str) else n]).hex()
    sel = lambda sig: '0x' + keccak(text=sig)[:4].hex()
    values = {(CURVE, sel('factory()')): word(factory),
              (ROUTER, sel('factory()')): word(factory),
              (quoter, sel('factory()')): word(factory),
              (ROUTER, sel('WETH9()')): word(WETH),
              (ROUTER, sel('factoryV2()')): word(factory),
              (ROUTER, sel('positionManager()')): word(factory)}
    curve = [word(MINT), word(counter), '0x' + encode(['uint256']*2, [10**12, 10**27]).hex(),
             word(100), word(0), word(0), word(10**27)]
    values.update({(CURVE, '0x' + k): v for k, v in zip(_GETTERS, curve)})
    calls = []
    def rpc(reqs):
        out = []
        for method, params in reqs:
            calls.append((method, params))
            if method == 'eth_blockNumber':
                out.append('0x7b'); continue
            assert params[1] == '0x7b'
            to, data = params[0]['to'], params[0]['data']
            if data.startswith('0x3cf28b5a'):
                out.append('0x' + encode(['uint256']*5, [int(MINT,16),int(CURVE,16),0,0,int(counter,16)]).hex())
            elif data.startswith(sel('getPool(address,address,uint24)')):
                out.append(word(POOL))
            elif to == quoter and data.startswith('0xc6a5026a'):
                # Forward/reverse outputs depend on actual token identity, not a token name.
                args = decode(['(address,address,uint256,uint24,uint160)'], bytes.fromhex(data[10:]))[0]
                out.append(word(10**9 if args[0] == WETH else 97*10**16))
            else:
                out.append(values[(to,data)])
        return out
    ids, bridges = {}, {}
    rows = routes.prepare(c, rpc, now=1001, identities=ids, bridges=bridges)
    assert rows[MINT]['counter'] == counter and rows[MINT]['bridge_pool'] == POOL
    assert rows[MINT]['block_number'] == 123
    assert rows[MINT]['max_counter'] == 99*10**7
    assert routes.buy_quote(rows[MINT], 10**17, TAKER, 1000)
    # A partial pinned batch raises; it can never shift another curve's fields into this one.
    def partial(reqs):
        answers = rpc(reqs)
        return answers[:-1] if len(reqs) > 1 else answers
    with pytest.raises(ValueError, match='partial'):
        routes.prepare(c, partial, now=1001, identities=ids, bridges=bridges)
    # Expired entry bounds cannot become an exact exit price.
    cache = CounterCache(clock=lambda: 100)
    cache.publish(rows, started=90)
    monkeypatch.setattr(routes, 'CACHE', cache)
    read_calls, verified = [], []
    def exit_read(to, data):
        read_calls.append((to,data))
        return word(5*10**16)
    exit_read.verify_relay = lambda: verified.append(True)
    q = routes.exit_quote(rows[MINT], 10**23, TAKER, 1000, exit_read)
    assert q['buyAmount'] == str(5*10**16) and len(read_calls) == 1 and verified == [True]
    c.close()


def test_factory_and_weth_getters_alone_do_not_prove_router02(monkeypatch):
    from eth_utils import keccak
    from eth_abi import encode
    from sniper.execution.evm_pons_counter_quote import find_bridge
    from sniper.settings import memecoin as cfg
    factory, quoter = '0x' + '8' * 40, '0x' + '9' * 40
    monkeypatch.setattr(cfg, 'memecoin_evm_v3_deployments', lambda _: [dict(router=ROUTER, quoter=quoter)])
    monkeypatch.setattr(cfg, 'memecoin_evm_v3_fee_tiers', lambda _: [3000])
    def read(calls):
        out = []
        for to, data in calls:
            if data == '0x' + keccak(text='WETH9()')[:4].hex():
                value = int(WETH,16)
            elif data == '0x' + keccak(text='factory()')[:4].hex():
                value = int(factory,16)
            elif data.startswith('0xc6a5026a'):
                value = 10**9
            elif data.startswith('0x1698ee82'):
                value = int(POOL,16)
            else:
                out.append(None); continue
            out.append('0x' + encode(['uint256'],[value]).hex())
        return out
    assert find_bridge('0x' + '1'*40, 10**18, read) is None


def test_background_reads_share_one_pinned_multicall_and_partial_answers_raise():
    from eth_abi import encode
    from types import SimpleNamespace
    from sniper.execution.evm_pons_counter_quote import public_rpc
    requests = []
    results = [(True, encode(['uint256'],[123])), (False,b'')]
    def post(url, *, json, timeout):
        requests.append(json)
        assert len(json) == 1
        assert json[0]['params'][1] == '0x7b'
        calls = decode(['(address,bool,bytes)[]'],bytes.fromhex(json[0]['params'][0]['data'][10:]))[0]
        assert len(calls) == 2 and all(c[1] for c in calls)
        raw = '0x' + encode(['(bool,bytes)[]'],[results]).hex()
        return SimpleNamespace(raise_for_status=lambda: None,
            json=lambda: [dict(jsonrpc='2.0',id=0,result=raw)])
    rpc = public_rpc(SimpleNamespace(post=post))
    reqs = [('eth_call',[dict(to=MINT,data='0x12345678'),'0x7b']),
            ('eth_call',[dict(to=CURVE,data='0x12345678'),'0x7b'])]
    assert rpc(reqs) == ['0x' + encode(['uint256'],[123]).hex(),None]
    assert len(requests) == 1
    results.pop()
    with pytest.raises(ValueError,match='partial'):
        rpc(reqs)


def test_shared_admission_uses_verified_receipt_facts_without_a_second_fetch():
    from types import SimpleNamespace
    from collections import deque
    from sniper.services import memecoin_rh_fast_lane as lane
    from test_memecoin_rh_source_binding import SIG
    from rh_source_fill_fixture import proof_for
    sig = dict(SIG, mint=MINT, pool=CURVE, src_out=10**24, paid_usd=100,
               route_kind='pons_shared',post_ratio=None)
    sig['source_fill'] = proof_for(sig,venue='pons_v2')
    sig['source_fill']['source_post'] = dict(reason='pons_post_state_unavailable',venue='pons_v2',
        pool=CURVE,pool_input_atomic='100',pool_output_atomic=str(10**24),receipt_routes_complete=True)
    live = lane.LiveLane.__new__(lane.LiveLane)
    def forbidden(*args, **kwargs):
        raise AssertionError('shared admission fetched already-proven token facts')
    live.stopped = False
    live.cache = SimpleNamespace(facts=forbidden)
    live._inline_inputs = forbidden
    live.ctx,live.contexts,live.conn = dict(address=TAKER),{},None
    live.clip_usd,live.fills,live.send_times = 50,0,deque()
    seen=[]
    def factory(ctx,stats):
        def hook(conn, ev):
            seen.append(ev)
            return dict(status='rejected',reason='fixture_only')
        return hook
    live.shared_hook_factory=factory
    assert live._handle_shared(sig,{})['reason'] == 'fixture_only'
    assert seen[0].their_price == 100 / 10**6
