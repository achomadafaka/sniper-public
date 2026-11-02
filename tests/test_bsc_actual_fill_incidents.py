import copy
from fractions import Fraction
import json
from pathlib import Path

import pytest

from sniper.services import memecoin_evm_onchain_watch as watch
from sniper.services import memecoin_evm_post_trade as post
from sniper.services import memecoin_bsc_fast_lane as lane

FIXTURES = Path(__file__).parent / 'fixtures/memecoin'
GME = json.loads((FIXTURES / 'gme_bsc_source_20261005.json').read_text())
LINGARD = json.loads((FIXTURES / 'lingard_bsc_source_20261005.json').read_text())
GME_MINT = '0x3e7d61d4ac560fcfc1902a5c54d93172dafb19de'
LINGARD_MINT = '0xf402eea2bf4d660c962e67eaa1997e8ba354ffff'


def decode(frame):
    return watch.relayer_purchase_from_receipt(
        frame['receipt'], frame['transaction'], frame['watched'][0],
        chain='56', contracts_verified=True, any_route=True)


def test_gme_pancake_v3_fee_preserves_wallet_net_and_unknown_initiation():
    proof = decode(GME)
    assert proof is not None
    assert proof['mint'] == GME_MINT
    assert proof['output_atomic'] == 18301887071005797094219072
    buy = watch.price_relayer_purchase(proof, decimals_fn=lambda _: 18, price_fn=lambda _: 1.)
    assert buy['source_initiation']['proof_version'] != 'relay-origin-paid-v1'
    state = post.source_post_trade('56', GME['receipt'], GME_MINT, owner=GME['watched'][0])
    sent = []
    signal = lane.make_watcher_signal('live', 'unused', send_fn=lambda _, p: sent.append(p) or True)
    row = dict(tx_hash=GME['tx_hash'], wallet=GME['watched'][0], buy=buy,
               log={'blockNumber': GME['receipt']['blockNumber']}, source_user_id='uid')
    result = signal(row, GME['receipt'], state, decimals=18, bnb_usd=600, bnb_usd_age_s=.1)
    assert result['reason'] == 'would_send' and result['sent']
    assert sent[0]['route_kind'] == 'v3_shared'
    assert sent[0]['source_initiation'] == buy['source_initiation']


@pytest.mark.parametrize('mutation', ['output', 'recipient', 'missing_swap'])
def test_gme_fee_requires_exact_pancake_output(mutation):
    frame = copy.deepcopy(GME)
    swap = next(l for l in frame['receipt']['logs']
                if l['address'].lower() == '0x477e484c7aae67669117e0a74fc5c2fa0d8358b9'
                and l['topics'][0] == post.PANCAKE_V3_SWAP)
    if mutation == 'missing_swap':
        frame['receipt']['logs'].remove(swap)
    elif mutation == 'recipient':
        swap['topics'][2] = '0x' + '00' * 12 + 'ab' * 20
    else:
        raw = bytes.fromhex(swap['data'][2:])
        amount = int.from_bytes(raw[:32], 'big')
        swap['data'] = '0x' + (((amount + 1) % 2**256).to_bytes(32, 'big') + raw[32:]).hex()
    assert decode(frame) is None


def decision(frame):
    state = post.source_post_trade('56', frame['receipt'], LINGARD_MINT, owner=frame['watched'][0])
    return lane.decide_from_receipt(frame['receipt'], mint=LINGARD_MINT, evm_post=state,
                                   chase_pct=30, premium=1.5,
                                   seq=int(frame['receipt']['blockNumber'], 16),
                                   hop_state_fn=lambda *_: None)


def test_lingard_buy_tax_with_following_sell_is_exact():
    state = post.source_post_trade('56', LINGARD['receipt'], LINGARD_MINT, owner=LINGARD['watched'][0])
    assert state['buy_tax'] == pytest.approx(.01)
    result = decision(LINGARD)
    # Its quote hop is not touched by the source: this stays an honest fallback until the
    # independently maintained hop tape covers it. The tax itself is now known.
    assert result['reason'] == 'would_send' or result['reason'].startswith('hop_state_stale:')
    covered = lane.decide_from_receipt(LINGARD['receipt'], mint=LINGARD_MINT, evm_post=state,
              chase_pct=30, premium=1.5, seq=int(LINGARD['receipt']['blockNumber'], 16),
              hop_state_fn=lambda _pair, seq: (10**24, 10**24, seq))
    assert covered['reason'] == 'would_send'
    assert covered['tax'] == Fraction(291162461888835860625412, 29116246188883586062541284)


@pytest.mark.parametrize('mutation', ['duplicate_buy', 'wrong_gross', 'wrong_tax_recipient'])
def test_lingard_multiple_buy_or_inexact_tax_refuses(mutation):
    frame = copy.deepcopy(LINGARD)
    pool = '0xeea29ac95f98061e26fde390ff58f531ec6b3895'
    swap = next(l for l in frame['receipt']['logs'] if l['address'].lower() == pool
                and l['topics'][0] == post.V2_SWAP)
    if mutation == 'duplicate_buy':
        frame['receipt']['logs'].append(copy.deepcopy(swap))
    elif mutation == 'wrong_gross':
        swap['data'] = '0x' + format(int(swap['data'], 16) + 1, '0256x')
    else:
        tax = next(l for l in frame['receipt']['logs'] if l['address'].lower() == LINGARD_MINT
                   and l['topics'][0] == post.TRANSFER and l['topics'][1][-40:] == pool[2:]
                   and l['topics'][2][-40:] == LINGARD_MINT[2:])
        tax['topics'][2] = '0x' + '00' * 12 + 'ab' * 20
    assert decision(frame)['reason'] in ('tax_unknown', 'post:split_route', 'post:no_venue_leg')
