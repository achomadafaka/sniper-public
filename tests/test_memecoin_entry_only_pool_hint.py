"""An unbound source price may name a discovery candidate, never a safety or price proof."""
import copy
import json
from pathlib import Path

import pytest

from sniper.execution import evm_source_route as src
from sniper.services import memecoin_evm_post_trade as pt
from sniper.settings import memecoin as cfg


FIXTURE = json.loads((Path(__file__).parent / 'fixtures/memecoin/evm_post_trade_receipts.json').read_text())


def key_for(chain, pool):
    return FIXTURE['v4_keys'].get(f'{chain}:{pool.lower()}')


def unbound_buy():
    case = copy.deepcopy(FIXTURE['cases']['v4_hooked_usdg'])
    transfers = [log for log in case['receipt']['logs']
                 if log.get('topics', [None])[0] == pt.TRANSFER
                 and log['address'].lower() == case['mint']]
    owner = case.get('trader') or '0x' + transfers[-1]['topics'][2][-40:]
    post = pt.source_post_trade('4663', case['receipt'], case['mint'], owner=owner, pool_key_fn=key_for)
    assert post['venue'] == 'v4'
    delta = sum(int(log['data'], 16) *
                ((log['topics'][2][-40:].lower() == owner[2:].lower()) -
                 (log['topics'][1][-40:].lower() == owner[2:].lower())) for log in transfers)
    # Representative hook dust: acquisition exceeds the leg by one atomic unit.
    extra = max(1, int(post['pool_output_atomic']) - delta + 1)
    case['receipt']['logs'].append({'address': case['mint'], 'topics': [pt.TRANSFER,
        '0x' + '0' * 64, '0x' + owner[2:].zfill(64)], 'data': hex(extra)})
    return case, owner, post['pool']


class Ref:
    def __init__(self, post):
        self.post, self.reads = post, 0

    def payload(self):
        self.reads += 1
        return self.post


def test_unbound_single_v4_leg_keeps_only_an_entry_discovery_candidate(monkeypatch):
    case, owner, pool = unbound_buy()
    post = pt.source_post_trade('4663', case['receipt'], case['mint'], owner=owner, pool_key_fn=key_for)
    assert post == {'reason': 'leg_does_not_bind', 'entry_hint_pool': pool}
    monkeypatch.setattr(src, 'enabled', lambda chain: chain == '4663')
    monkeypatch.setattr(cfg, 'memecoin_evm_safety_exit_hint_chains', lambda: ('4663',))
    ref = Ref(post)
    assert src.attempt_hint('4663', ref, case['mint']) == pool
    assert ref.reads == 1
    assert src.hint_from_reference(ref) is None
    assert src.safety_exit_hint('4663', ref) is None
    assert 'ratio' not in post and 'pool' not in post and 'venue' not in post
    monkeypatch.setattr(pt, 'enabled', lambda chain: True)
    assert pt.resolve('4663', avg_price=2., post=post, our_pool=pool) == (
        2., 'avg_fill', 'no_ratio leg_does_not_bind')


def test_entry_only_candidate_is_consumed_with_one_nonblocking_peek(monkeypatch):
    case, _owner, pool = unbound_buy()
    monkeypatch.setattr(src, 'enabled', lambda chain: True)
    monkeypatch.setattr(src, 'remembered', lambda *_args: None)

    class Once(Ref):
        def payload(self):
            assert self.reads == 0, 'reference was read twice'
            return super().payload()

    ref = Once({'reason': 'leg_does_not_bind', 'entry_hint_pool': pool})
    assert src.attempt_hint('4663', ref, case['mint']) == pool


@pytest.mark.parametrize('variant', ['unknown_key', 'split', 'no_acquisition', 'reverted',
                                     'truncated_swap', 'missing_swap_sender', 'short_swap_word'])
def test_ambiguous_or_unacquired_receipt_never_supplies_a_candidate(variant):
    case, owner, _pool = unbound_buy()
    receipt = case['receipt']
    if variant in ('unknown_key', 'split', 'truncated_swap', 'missing_swap_sender', 'short_swap_word'):
        log = copy.deepcopy(next(log for log in receipt['logs']
                                 if log.get('topics', [None])[0] == pt.V4_SWAP))
        if variant == 'unknown_key':
            log['topics'][1] = '0x' + 'ff' * 32
        elif variant == 'truncated_swap':
            log['data'] = '0x'
        elif variant == 'missing_swap_sender':
            log['topics'] = log['topics'][:2]
        elif variant == 'short_swap_word':
            log['data'] = log['data'][:-2]
        receipt['logs'].append(log)
    elif variant == 'no_acquisition':
        owner = '0x' + 'ab' * 20
    else:
        receipt['status'] = '0x0'
    post = pt.source_post_trade('4663', receipt, case['mint'], owner=owner, pool_key_fn=key_for)
    assert 'entry_hint_pool' not in post
    assert 'ratio' not in post


@pytest.mark.parametrize('pool', [None, '', '0x123', 123, '0x' + 'gg' * 32])
def test_malformed_candidate_cannot_become_an_entry_hint(monkeypatch, pool):
    monkeypatch.setattr(src, 'enabled', lambda chain: True)
    monkeypatch.setattr(src, 'remembered', lambda *_args: None)
    assert src.attempt_hint('4663', Ref({'reason': 'leg_does_not_bind', 'entry_hint_pool': pool}), 'coin') is None


def test_entry_candidate_does_not_override_a_bound_pool_or_disabled_chain(monkeypatch):
    _case, _owner, pool = unbound_buy()
    other = '0x' + 'ab' * 32
    monkeypatch.setattr(src, 'enabled', lambda chain: chain == '4663')
    ref = Ref({'venue': 'v4', 'pool': other, 'entry_hint_pool': pool})
    assert src.attempt_hint('4663', ref, 'coin') == other
    assert src.attempt_hint('56', Ref({'entry_hint_pool': pool}), 'coin') is None
