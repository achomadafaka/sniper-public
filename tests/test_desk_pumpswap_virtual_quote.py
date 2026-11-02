"""Pinned IDL event tails; only the proven legacy-buy state may price a research snapshot."""
import base64
import hashlib
import json
import struct
import sys
from pathlib import Path

import pytest

from sniper.execution.pumpswap_buy import _BUY_EVENT_FIELDS
from sniper.services import memecoin_desk_pumpswap as PS
from test_memecoin_desk_pumpswap import buy, logs, sell

LAYOUT = json.loads((Path(__file__).parent / 'fixtures' / 'desk_pumpswap_virtual_quote_layout.json').read_text())


def append(line, tail):
    return 'Program data: ' + base64.b64encode(base64.b64decode(line[14:]) + tail).decode()


def source_buy(ix='buy'):
    args = {'ix_name': ix, 'pool_quote_token_reserves': 4000}
    if ix in ('buy_exact_quote_in', 'buy_exact_quote_in_v2'):
        args.update(quote_amount_in=1004, user_quote_amount_in=1000)
    return buy(**args)


def test_layout_is_the_pinned_official_signed_extension_after_the_shared_buy_head():
    assert PS.PUMP_AMM_IDL_SHA256 == LAYOUT['idl_sha256']
    assert len(_BUY_EVENT_FIELDS) == LAYOUT['BuyEvent']['prefix_fields']
    assert _BUY_EVENT_FIELDS[-1] == (LAYOUT['BuyEvent']['previous'], 'u64')
    digest = hashlib.sha256(json.dumps(_BUY_EVENT_FIELDS, separators=(',', ':')).encode()).hexdigest()
    assert digest == LAYOUT['BuyEvent']['prefix_sha256']
    assert LAYOUT['BuyEvent']['type'] == LAYOUT['SellEvent']['type'] == 'i128'


@pytest.mark.parametrize('ix', ['buy', 'buy_exact_quote_in'])
@pytest.mark.parametrize('virtual', [0, 17_584_505_596, 2**64 - 1])
def test_legacy_buy_carries_explicit_supported_virtual_quote_with_a_proof_source(ix, virtual):
    tail = virtual.to_bytes(16, 'little', signed=True)
    event = PS.events(logs(append(source_buy(ix), tail)))[0][1]
    assert event.get('virtual_quote_atomic') == str(virtual)
    assert event.get('virtual_quote_source') == 'pumpswap_grpc:buy_event_virtual_quote'
    assert event['payload']['virtual_quote_reserves'] == virtual
    assert event['payload']['virtual_quote_source'] == event['virtual_quote_source']
    assert event['payload']['virtual_quote_atomic'] == event['virtual_quote_atomic']
    assert base64.b64decode(event['payload']['unparsed_tail_b64']) == tail


@pytest.mark.parametrize('virtual', [-1, -(2**127), 2**64, 2**127 - 1])
def test_unsupported_signed_or_oversized_virtual_quote_is_preserved_but_cannot_price(virtual):
    event = PS.events(logs(append(source_buy(), virtual.to_bytes(16, 'little', signed=True))))[0][1]
    assert event['payload']['virtual_quote_reserves'] == virtual
    assert event.get('virtual_quote_atomic') is None and event.get('virtual_quote_source') is None


@pytest.mark.parametrize('ix', ['buy_v2', 'buy_exact_quote_in_v2'])
def test_v2_virtual_quote_is_raw_evidence_without_an_unproved_post_state_claim(ix):
    event = PS.events(logs(append(source_buy(ix), bytes(16))))[0][1]
    assert event['payload']['virtual_quote_reserves'] == 0
    assert event.get('virtual_quote_atomic') is None and event.get('virtual_quote_source') is None


@pytest.mark.parametrize('line', [source_buy(), sell()])
def test_old_events_without_the_extension_do_not_invent_a_zero(line):
    event = PS.events(logs(line))[0][1]
    assert 'virtual_quote_reserves' not in event['payload']
    assert event.get('virtual_quote_atomic') is None and event.get('virtual_quote_source') is None


@pytest.mark.parametrize('size', [1, 15])
@pytest.mark.parametrize('is_sell', [False, True])
def test_partial_recognized_i128_extension_is_a_decode_loss(size, is_sell):
    line = append(sell(), bytes(32)) if is_sell else source_buy()
    with pytest.raises(PS.DecodeLoss, match='event'):
        PS.events(logs(append(line, bytes(size))))


def test_sell_signed_virtual_quote_uses_the_pinned_offset_and_has_no_post_state_proof():
    line = append(sell(), struct.pack('<4Q', 3, 4, 5, 6) + (-123).to_bytes(16, 'little', signed=True))
    raw = base64.b64decode(line[14:])
    assert raw[LAYOUT['SellEvent']['virtual_quote_offset']:] == (-123).to_bytes(16, 'little', signed=True)
    event = PS.events(logs(line))[0][1]
    assert event['payload']['virtual_quote_reserves'] == -123
    assert event.get('virtual_quote_atomic') is None and event.get('virtual_quote_source') is None
    assert event['post_base_reserve_atomic'] is None and event['post_quote_reserve_atomic'] is None


@pytest.mark.parametrize('discriminator', LAYOUT['state_change_events'].values())
def test_state_change_events_invalidate_a_prior_priced_swap(discriminator):
    changed = 'Program data: ' + base64.b64encode(bytes.fromhex(discriminator)).decode()
    with pytest.raises(PS.DecodeLoss, match='^pumpswap_virtual_quote_unproven$'):
        PS.events(logs(append(source_buy(), bytes(16)), changed))


def test_a_foreign_state_change_event_cannot_invalidate_pumpswap_state():
    changed = 'Program data: ' + base64.b64encode(bytes.fromhex(LAYOUT['state_change_events']['SweepPoolFeeEvent'])).decode()
    foreign = '11111111111111111111111111111111'
    rows = PS.events(logs(append(source_buy(), bytes(16)), f'Program {foreign} invoke [2]', changed,
                          f'Program {foreign} success'))
    assert len(rows) == 1 and rows[0][1].get('virtual_quote_atomic') == '0'


def test_later_v2_in_the_same_transaction_never_inherits_legacy_virtual_quote_proof():
    rows = PS.events(logs(append(source_buy(), bytes(16)), append(source_buy('buy_v2'), bytes(16))))
    assert [ordinal for ordinal, _ in rows] == [1, 2]
    assert rows[0][1].get('virtual_quote_atomic') == '0'
    assert rows[1][1].get('virtual_quote_atomic') is None
    assert rows[1][1]['payload']['virtual_quote_reserves'] == 0


def verify_idl_file(path):
    """Offline check of the full specified source, distinct from the small committed layout receipt."""
    blob = Path(path).read_bytes()
    assert hashlib.sha256(blob).hexdigest() == LAYOUT['idl_sha256'], 'official IDL hash differs from the pinned source'
    idl = json.loads(blob)
    assert idl['address'] == PS.AMM_PROGRAM
    types = {row['name']: row['type']['fields'] for row in idl['types'] if row['name'] in ('Pool', 'BuyEvent', 'SellEvent')}
    for name in ('BuyEvent', 'SellEvent'):
        fields = types[name]
        index = LAYOUT[name]['prefix_fields']
        prefix = [(row['name'], row['type']) for row in fields[:index]]
        assert hashlib.sha256(json.dumps(prefix, separators=(',', ':')).encode()).hexdigest() == LAYOUT[name]['prefix_sha256']
        assert fields[index]['name'] == LAYOUT[name]['field'] and fields[index]['type'] == 'i128'
    sizes = {'u8': 1, 'u16': 2, 'u64': 8, 'pubkey': 32, 'bool': 1}
    fields = types['Pool']
    index = next(i for i, row in enumerate(fields) if row['name'] == 'virtual_quote_reserves')
    assert fields[index]['type'] == 'i128'
    assert 8 + sum(sizes[row['type']] for row in fields[:index]) == LAYOUT['Pool']['virtual_quote_offset']
    events = {row['name']: bytes(row['discriminator']).hex() for row in idl['events']}
    assert {name: events[name] for name in LAYOUT['state_change_events']} == LAYOUT['state_change_events']


def test_offline_idl_verifier_refuses_a_changed_source(tmp_path):
    path = tmp_path / 'changed.json'
    path.write_text('{}')
    with pytest.raises(AssertionError, match='hash differs'):
        verify_idl_file(path)


if __name__ == '__main__':
    verify_idl_file(sys.argv[1])
    print('Pinned PumpSwap IDL hash and layouts verified')
