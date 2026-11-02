import copy
import json
from pathlib import Path

import pytest

from sniper.services import memecoin_evm_onchain_watch as M


WISP = json.loads((Path(__file__).parent / 'fixtures/memecoin/wisp_relay_tax_20261004.json').read_text())


def decode(frame):
    return M.relayer_purchase_from_receipt(frame['receipt'], frame['transaction'], frame['watched'][0],
                                          chain='56', contracts_verified=True, any_route=True)


def test_wisp_pool_tax_keeps_exact_wallet_output():
    proof = decode(WISP)
    assert proof is not None
    assert proof['output_atomic'] == 27170417015396852807106572
    assert proof['payment_atomic'] == 3966977369000000000000


def test_wisp_reaches_bsc_fast_lane_from_its_captured_receipt():
    from sniper.services.memecoin_evm_post_trade import source_post_trade
    from sniper.services.memecoin_bsc_fast_lane import make_watcher_signal
    proof = decode(WISP)
    assert proof is not None
    wallet = WISP['watched'][0]
    buy = M.price_relayer_purchase(proof, decimals_fn=lambda mint: 18, price_fn=lambda token: 1.)
    post = source_post_trade('56', WISP['receipt'], proof['mint'], owner=wallet)
    assert post['buy_tax'] == pytest.approx(.01)
    row = dict(tx_hash=WISP['tx_hash'], wallet=wallet, buy=buy,
               log=WISP['receipt']['logs'][-1], now=WISP['arrival'],
               source_user_id='uid', handle='bigwarzeth')
    sent = []
    signal = make_watcher_signal('live', 'unused', send_fn=lambda address, payload: sent.append(payload) or True,
                                 hop_state_fn=lambda *a, **k: None)
    result = signal(row, WISP['receipt'], post, decimals=18, bnb_usd=600, bnb_usd_age_s=.1)
    assert result['reason'] == 'would_send' and result['sent']
    assert len(sent) == 1 and sent[0]['source_initiation'] == buy['source_initiation']
    assert sent[0]['seq'] == int(WISP['receipt']['blockNumber'], 16)


@pytest.mark.parametrize('mutation', ['missing_swap', 'wrong_output', 'wrong_recipient', 'wrong_tax_target'])
def test_tax_exception_needs_exact_pool_swap(mutation):
    f = copy.deepcopy(WISP)
    pool = '0xc0a73cf0698fb59fbb38bc8ec6ef1a7b2c219c1c'
    swap = next(l for l in f['receipt']['logs'] if l['address'].lower() == pool and len(l['topics']) == 3
                and l['topics'][0] != M.TRANSFER_TOPIC)
    if mutation == 'missing_swap':
        f['receipt']['logs'].remove(swap)
    elif mutation == 'wrong_output':
        swap['data'] = '0x' + format(int(swap['data'], 16) + 1, '0256x')
    elif mutation == 'wrong_recipient':
        swap['topics'][2] = '0x' + '00' * 12 + 'ab' * 20
    else:
        tax = next(l for l in f['receipt']['logs'] if l['address'].lower() ==
                   '0xda2fd814da34a489051c3471c0169c936aa37777' and len(l['topics']) == 3
                   and l['topics'][2][-40:] == l['address'][2:].lower())
        tax['topics'][2] = '0x' + '00' * 12 + 'ab' * 20
    assert decode(f) is None


