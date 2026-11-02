"""A refused direct multi-buy must still prepare its confirmed routed target."""
import json
from pathlib import Path

from sniper.services import memecoin_slot_signal as signal
import pytest

RECEIPT = json.loads((Path(__file__).parent / 'fixtures/memecoin/alon_routed_buy_20261011.json').read_text())
OWNER = 'DAejzMs5cUeCCENNvapy9KWFwzwegh7LvcgNkZ6hnf1y'
ALON = 'ALoNmPYp9HxW5Gi4QHGRomXoBuwepwZWcygtvdEXzfBH'


def test_shyft_multi_buy_prepares_whole_target_without_program_authority(monkeypatch):
    from sniper.services.memecoin_shyft_signal import routed_buys
    from sniper.settings import memecoin as cfg
    from test_memecoin_shyft_confirmed import wire_receipt
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_ROUTED_PUMP', True)
    buys = routed_buys(wire_receipt(RECEIPT), {OWNER: ('u1', 'fmpumpguy')},
        recv_monotonic=100., recv_utc='original', head_slot=455671383, head_mono=100.)
    assert len(buys) == 1
    buy = buys[0]
    assert (buy.mint, buy.usdc_spent_atomic, buy.token_delta_atomic) == (ALON, 1500000000, 10908646100860)
    assert buy.requires_receipt_proof is True
    assert (buy.recv_monotonic, buy.recv_utc) == (100., 'original')
    assert getattr(buy, 'source_initiation', None) is None  # knob MEMECOIN_SLOT_LANE_ROUTED_PROCESSED_PROOF off (default)
    assert [dict(e.fields)['pool'] for e in buy.venue_events] == [
        '2uF4Xh61rDwxnG9woyxsVQP7zuA6kLFpb3NvnRQeoiSd',
        '782aJ74HWj1uxMAnf66jyiP46vNUwSHo4gFHoYoK8yoJ']


def test_helius_multi_buy_refusal_does_not_consume_routed_pair(monkeypatch):
    from test_slot_lane_routed import _run_socket
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_ROUTED_PUMP', True)
    slot = RECEIPT['slot']
    keys = [k['pubkey'] for k in RECEIPT['transaction']['message']['accountKeys']]
    before = {b['accountIndex']: b for b in RECEIPT['meta']['preTokenBalances'] if b['owner'] == OWNER}
    after = {b['accountIndex']: b for b in RECEIPT['meta']['postTokenBalances'] if b['owner'] == OWNER}
    frames = []
    for index in sorted(before.keys() | after.keys()):
        balance = after.get(index, before.get(index))
        kind = next(k for k, p in signal.ROUTED_PROGRAMS if p == balance['programId'])
        info = {'owner': OWNER, 'mint': balance['mint'], 'tokenAmount': dict(balance['uiTokenAmount'])}
        info['tokenAmount']['amount'] = after[index]['uiTokenAmount']['amount'] if index in after else '0'
        frames.append((OWNER, kind, {'method': 'programNotification', 'params': {'result': {
            'context': {'slot': slot}, 'value': {'pubkey': keys[index], 'account': {
                'owner': balance['programId'], 'data': {'parsed': {'info': info}}}}}}}))
    frames.append((OWNER, 'logs', {'method': 'logsNotification', 'params': {'result': {
        'context': {'slot': slot}, 'value': {'signature': RECEIPT['transaction']['signatures'][0],
            'err': None, 'logs': RECEIPT['meta']['logMessages']}}}}))
    def baseline(owner):
        assert owner == OWNER
        return {kind: (slot - 1, {keys[i]: int(b['uiTokenAmount']['amount'])
                    for i, b in before.items() if b['programId'] == program})
                for kind, program in signal.ROUTED_PROGRAMS}
    routed, direct, _, _ = _run_socket(monkeypatch, frames, owners={OWNER: ('u1', 'fmpumpguy')}, baseline_fn=baseline)
    assert len(direct) == 1 and direct[0].refusal == 'multi_own_buy:chain'   # USDC -> pumpCm -> ALON
    assert len(routed) == 1
    assert routed[0].requires_receipt_proof is True
    assert (routed[0].mint, routed[0].token_delta_atomic, routed[0].usdc_spent_atomic) == (ALON, 10908646100860, 1500000000)


@pytest.mark.parametrize('native_claimed', [False, True])
def test_shared_feed_refusal_keeps_routed_handoff_and_original_clock(monkeypatch, native_claimed):
    import asyncio
    import threading
    from dataclasses import replace
    from sniper.services import memecoin_shyft_signal as shyft
    from sniper.settings import memecoin as cfg
    from test_memecoin_shyft_confirmed import wire_receipt
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_ROUTED_PUMP', True)
    watch = {OWNER: ('u1', 'fmpumpguy')}
    stamp = dict(recv_monotonic=0., recv_utc='original', head_slot=RECEIPT['slot'], head_mono=0.)
    direct = shyft.decode_update(wire_receipt(RECEIPT), watch, **stamp)[0]
    assert direct.refusal == 'multi_own_buy:chain'
    routed = shyft.routed_buys(wire_receipt(RECEIPT), watch, **stamp)[0]
    received, diagnostics, stop, status = [], [], threading.Event(), {}

    async def helius(on_buy, owners_fn, ws_url, feed_status, stop, connect, on_sell, on_routed, baseline):
        on_buy(replace(direct, refusal=None) if native_claimed else direct)
        on_routed(routed)
        await asyncio.sleep(.03)
        on_buy(direct)
        on_routed(replace(routed, recv_monotonic=.02, recv_utc='later'))
        await asyncio.sleep(.03)
        stop.set()

    async def grpc(url, token, owners_fn, on_buy, feed_status, stop, *, on_metadata):
        await asyncio.sleep(.01)
        on_buy(direct)
        on_metadata(wire_receipt(RECEIPT), **dict(stamp, recv_monotonic=.01, recv_utc='later'))
        await asyncio.sleep(1)

    monkeypatch.setattr(signal, '_run', helius)
    monkeypatch.setattr(shyft, 'run_feed', grpc)
    asyncio.run(signal._run_signals(diagnostics.append, lambda: watch, 'unused', status, stop,
        None, None, received.append, None, ('secret', ('https://beta-grpc.ams.shyft.to',))))
    assert len(diagnostics) == 1
    assert len(received) == (0 if native_claimed else 1)
    if received:
        assert (received[0].recv_monotonic, received[0].recv_utc, received[0].signal_feed) == (0., 'original', 'helius')
        assert received[0].requires_receipt_proof is True


@pytest.mark.parametrize('event_index,base,quote', [
    (0, 2335936014339259, 13153299873569),
    (1, 121088550767074, 2703227654757),
])
def test_v2_post_reserves_include_fees_retained_in_the_pool(event_index, base, quote):
    from sniper.execution import pumpswap_buy as ps
    from sniper.services.memecoin_opportunity_live import program_data_logs
    lines = program_data_logs(RECEIPT['meta']['logMessages'], ps.AMM_PROGRAM)
    event = ps.parse_buy_event(lines[event_index][1])
    assert ps.post_trade_reserves(event) == (base, quote)


@pytest.mark.parametrize('damage', ['gross', 'buyback', 'cashback'])
def test_v2_unproved_retained_fee_reserves_cannot_supply_a_price(damage):
    from sniper.execution import pumpswap_buy as ps
    from sniper.services.memecoin_opportunity_live import program_data_logs
    event = ps.parse_buy_event(program_data_logs(RECEIPT['meta']['logMessages'], ps.AMM_PROGRAM)[1][1])
    if damage == 'gross':
        event['user_quote_amount_in'] += 1
    elif damage == 'buyback':
        event['buyback_fee'] = event['protocol_fee'] + 1
    else:
        event['cashback'] = 1
    with pytest.raises(ValueError, match='v2 quote reserves unproved'):
        ps.post_trade_reserves(event)
    assert ps.source_price_usd(event, {'base_decimals': 6, 'virtual_quote': 0}, 100.) is None
