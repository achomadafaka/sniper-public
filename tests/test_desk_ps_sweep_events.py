"""SweepPoolFeeEvent recorder: parser on real transactions and the worker's additive record. A sweep stays a decode loss; nothing here stops a window from censoring."""
import base64
import json
import time
from pathlib import Path

import pytest

from sniper.services import memecoin_desk_pumpswap as P

FX = Path(__file__).resolve().parent / 'fixtures/memecoin/pumpswap_boost_20261030'
WSOL = 'So11111111111111111111111111111111111111112'


def load(name):
    d = json.loads((FX / name).read_text())
    return d, d['result']['meta']['logMessages']


def test_a_real_sweep_transaction_is_still_a_decode_loss():
    _, logs = load('sweep_two_buckets.json')
    with pytest.raises(P.DecodeLoss, match='virtual_quote_unproven'):
        P.events(logs)


def test_both_bucket_events_of_a_pool_are_recorded_and_sum_to_the_vault_delta():
    d, logs = load('sweep_two_buckets.json')
    pool = d['pool']
    evs = [e for e in P.sweep_events(logs) if e['pool'] == pool]
    assert sorted(e['bucket'] for e in evs) == [0, 1] and all(type(e['amount']) is str for e in evs)
    meta = d['result']['meta']
    pre = {b['accountIndex']: int(b['uiTokenAmount']['amount']) for b in meta['preTokenBalances']}
    post = {b['accountIndex']: int(b['uiTokenAmount']['amount']) for b in meta['postTokenBalances']}
    vault = next(b['accountIndex'] for b in meta['postTokenBalances'] if b['owner'] == pool and b['mint'] == WSOL)
    assert sum(int(e['amount']) for e in evs) == pre[vault] - post[vault] == 62825831           # the identity the closure test rests on, measured on a real transaction


def test_all_events_of_a_multi_pool_sweep_are_recorded_with_distinct_ordinals():
    _, logs = load('sweep_pool_fee.json')
    got = P.sweep_events(logs)
    assert len(got) == 4 and len({e['ordinal'] for e in got}) == 4 and {e['bucket'] for e in got} == {1}
    assert all(e['quote_mint'] and e['pool'] and e['payer'] for e in got)


def line_of(logs, disc):
    return next(i for i, l in enumerate(logs) if l.startswith('Program data: ') and base64.b64decode(l[14:])[:8] == disc)


def test_a_wrong_length_event_records_nothing_for_that_event():
    _, logs = load('sweep_pool_fee.json')
    i = line_of(logs, P.SWEEP_POOL_FEE_DISCRIMINATOR)
    cut = list(logs); cut[i] = 'Program data: ' + base64.b64encode(base64.b64decode(logs[i][14:])[:-1]).decode()
    assert len(P.sweep_events(cut)) == 3


def test_incomplete_or_foreign_logs_record_nothing():
    _, logs = load('sweep_pool_fee.json')
    assert P.sweep_events(logs[:-1]) == []                                  # unterminated invocation stack
    foreign = [l.replace(P.AMM_PROGRAM, 'Foreign1111111111111111111111111111111111111') for l in logs]
    assert P.sweep_events(foreign) == []                                   # not the pAMM program
    assert P.sweep_events(5) == []


def test_other_state_events_are_not_sweeps():
    _, logs = load('boost_buy_and_burn.json')
    assert P.sweep_events(logs) == []
