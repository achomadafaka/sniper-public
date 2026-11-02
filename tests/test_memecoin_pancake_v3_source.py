"""SHIELD's exact PancakeSwap V3 source event, not its USDT conversion hop."""
import copy
import json
from pathlib import Path

from eth_utils import keccak
import pytest

from sniper.services import memecoin_evm_post_trade as pt
from sniper.services import memecoin_tick_stream as ticks

MINT = '0x3b4cbdb0a05d08638d6c11a79d0455fb9ffa65ff'
POOL = '0x2fefebe6be6874cbce54709279a8403c333f3e23'
OWNER = '0xd3375d0fe9e0e0a0cd3a5b87b833b61f167d04f8'
TOPIC = '0x19b47279256b2a23a1665c810c8d55a1758940ee09377d4f8d26497a3577dc83'
RECEIPT = json.loads((Path(__file__).parent/'fixtures/memecoin/bsc_shield_source_receipt.json').read_text())
LOG = next(x for x in RECEIPT['logs'] if x['address'].lower()==POOL and x['topics'][0]==TOPIC)


def test_topic_is_the_deployed_extended_v3_abi():
    assert '0x'+keccak(b'Swap(address,address,int256,int256,uint160,uint128,int24,uint128,uint128)').hex() == TOPIC


def test_source_decoder_binds_the_coin_leg_not_the_conversion_pool():
    post = pt.source_post_trade('56',RECEIPT,MINT,owner=OWNER)
    assert post['venue'] == 'v3' and post['pool'] == POOL
    assert post['ratio'] > 0 and post['ratio'] < 10
    # Independent price unit check: source received exactly the coin pool's negative token1 leg.
    words=[LOG['data'][2+i:2+i+64] for i in range(0,len(LOG['data'])-2,64)]
    payout=(1<<256)-int(words[1],16)
    received=sum(int(x['data'],16) for x in RECEIPT['logs'] if x['address'].lower()==MINT and x['topics'][-1][-40:]==OWNER[2:])
    assert payout == received


def test_tick_ingest_captures_the_extended_event_with_atomic_units():
    row=ticks.decode_evm_log('56',LOG,'2026-10-03T14:43:39+00:00')
    assert row['kind']=='v3_swap' and row['pool']==POOL
    assert int(row['amount0']) > 0 and int(row['amount1']) < 0
    assert row['sqrt_price_x96'] == str(int(LOG['data'][130:194],16))
    assert TOPIC in ticks.INGEST_TOPICS


def test_truncated_extended_event_is_unknown():
    log=dict(LOG,data=LOG['data'][:2+5*64])
    receipt=copy.deepcopy(RECEIPT)
    receipt['logs']=[log if x['logIndex']==LOG['logIndex'] else x for x in receipt['logs']]
    assert pt.source_post_trade('56',receipt,MINT,owner=OWNER)=={'reason':'no_venue_leg'}
    assert ticks.decode_evm_log('56',log,'2026-10-03T14:43:39+00:00') is None


def test_wrong_owner_is_not_source_price_evidence():
    post=pt.source_post_trade('56',RECEIPT,MINT,owner='0x'+'ab'*20)
    assert post=={'reason':'owner_received_nothing'}


@pytest.mark.parametrize('data', [
    LOG['data'][:2 + 6 * 64 + 2],
    LOG['data'][:2 + 5 * 64] + 'z' * 128,
    LOG['data'][:2 + 5 * 64] + format(1 << 128, '064x') + LOG['data'][2 + 6 * 64:],
])
def test_partial_or_invalid_protocol_fee_tail_is_not_an_event(data):
    log = dict(LOG, data=data)
    receipt = copy.deepcopy(RECEIPT)
    receipt['logs'] = [log if x['logIndex'] == LOG['logIndex'] else x for x in receipt['logs']]
    assert pt.source_post_trade('56', receipt, MINT, owner=OWNER) == {'reason': 'no_venue_leg'}
    assert ticks.decode_evm_log('56', log, '2026-10-03T14:43:39+00:00') is None
