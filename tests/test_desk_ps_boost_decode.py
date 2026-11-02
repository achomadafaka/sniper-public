"""BoostBuyAndBurnEvent restates the state after the boost buy; accepted only when the same transaction's BuyEvent proves the same reserves (decoder ps-decode-v2)."""
import base64
import json
from pathlib import Path

import pytest

from sniper.services import memecoin_desk_pumpswap as P

FX = Path(__file__).resolve().parent / 'fixtures/memecoin/pumpswap_boost_20261030'


def logs_of(name='boost_buy_and_burn'):
    return json.loads((FX / (name + '.json')).read_text())['result']['meta']['logMessages']


def boost_index(logs):
    for i, line in enumerate(logs):
        if line.startswith('Program data: ') and base64.b64decode(line[14:])[:8] == P.BOOST_BUY_AND_BURN_DISCRIMINATOR:
            return i
    raise AssertionError('no boost event in fixture')


def edit(logs, mutate):
    i = boost_index(logs)
    raw = bytearray(base64.b64decode(logs[i][14:]))
    mutate(raw)
    out = list(logs)
    out[i] = 'Program data: ' + base64.b64encode(bytes(raw)).decode()
    return out


def test_a_real_boost_transaction_keeps_its_buy_and_is_not_a_loss():
    found = P.events(logs_of())
    kinds = [e['kind'] for _, e in found]
    assert kinds == ['buy']                                             # the boost event itself is a confirmation, never a row
    buy = found[0][1]
    assert buy['payload']['ix_name'] == 'buy_exact_quote_in' and (buy['post_base_reserve_atomic'], buy['post_quote_reserve_atomic']) == ('179251070979905', '2259917751744')


@pytest.mark.parametrize('offset', [192, 184, 168])     # base_reserves_after, real_quote_reserves_after, virtual_quote_reserves
def test_a_boost_event_that_disagrees_with_the_buy_is_still_a_loss(offset):
    def bump(raw): raw[offset] ^= 1
    with pytest.raises(P.DecodeLoss, match='virtual_quote_unproven'):
        P.events(edit(logs_of(), bump))


def test_a_boost_event_for_another_pool_is_still_a_loss():
    def other_pool(raw): raw[8 + 8 + 64] ^= 1
    with pytest.raises(P.DecodeLoss):
        P.events(edit(logs_of(), other_pool))


def test_a_boost_event_without_a_buy_in_the_transaction_is_still_a_loss():
    logs = logs_of()
    keep = []
    for line in logs:
        if line.startswith('Program data: ') and base64.b64decode(line[14:])[:8] == P.BUY_EVENT_DISCRIMINATOR:
            continue
        keep.append(line)
    with pytest.raises(P.DecodeLoss):
        P.events(keep)


def test_a_boost_event_of_the_wrong_length_is_a_loss():
    logs = logs_of()
    i = boost_index(logs)
    out = list(logs); out[i] = 'Program data: ' + base64.b64encode(base64.b64decode(logs[i][14:]) + b'\0').decode()
    with pytest.raises(P.DecodeLoss):
        P.events(out)


def test_the_other_state_events_are_untouched_the_sweep_and_deposit_shapes_still_lose():
    with pytest.raises(P.DecodeLoss):
        P.events(logs_of('sweep_pool_fee'))


def test_a_negative_boost_virtual_quote_is_a_loss():
    def negative(raw): raw[168:184] = (-1).to_bytes(16, 'little', signed=True)
    with pytest.raises(P.DecodeLoss, match='virtual_quote_unproven'):
        P.events(edit(logs_of(), negative))


def test_a_buy_without_a_proven_virtual_quote_cannot_confirm_a_boost(monkeypatch):
    real = P._decode
    def no_virtual(line, ix=None):
        ev = real(line, ix)
        if ev is not None and ev['kind'] == 'buy':
            ev = {k: v for k, v in ev.items() if k not in ('virtual_quote_atomic', 'virtual_quote_source')}
        return ev
    monkeypatch.setattr(P, '_decode', no_virtual)
    with pytest.raises(P.DecodeLoss, match='virtual_quote_unproven'):
        P.events(logs_of())
