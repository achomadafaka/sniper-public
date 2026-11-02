"""Offline provenance and exact-unit checks for the research PumpSwap tape."""
import base64
import json
import struct
from pathlib import Path

import pytest
from solders.pubkey import Pubkey

from sniper.execution.pumpswap_buy import _BUY_EVENT_FIELDS, BUY_EVENT_DISCRIMINATOR
from sniper.services.memecoin_rabbit_dflow_amm import encode_buy_event
from sniper.services import memecoin_desk_pumpswap as PS

FIXTURES = Path(__file__).parent / 'fixtures' / 'memecoin'
KEYS = [str(Pubkey.from_bytes(bytes([n]) * 32)) for n in range(1, 9)]
POOL, ACTOR, CREATOR = KEYS[:3]


def logs(*lines):
    return [f'Program {PS.AMM_PROGRAM} invoke [1]', *lines, f'Program {PS.AMM_PROGRAM} success']


def buy(**overrides):
    event = {name: (KEYS[3] if kind == 'pubkey' else False if kind == 'bool'
                    else 'buy' if kind == 'string' else 0) for name, kind in _BUY_EVENT_FIELDS}
    event.update(timestamp=1_800_000_000, pool=POOL, user=ACTOR, coin_creator=CREATOR,
                 base_amount_out=100, pool_base_token_reserves=1000,
                 pool_quote_token_reserves=2**64 - 1, quote_amount_in=1000,
                 lp_fee_basis_points=20, lp_fee=2, protocol_fee_basis_points=5, protocol_fee=1,
                 coin_creator_fee_basis_points=5, coin_creator_fee=1,
                 quote_amount_in_with_lp_fee=1002, user_quote_amount_in=1004)
    event.update(overrides)
    return encode_buy_event(event)


def sell():
    values = [1_800_000_000, 100, 1000, 200, 300, 2**64-1, 4000,
              1005, 20, 2, 5, 1, 1003, 1001]
    raw = (bytes.fromhex('3e2f370aa503dc2a') + struct.pack('<q13Q', *values)
           + b''.join(bytes(Pubkey.from_string(k)) for k in (POOL, ACTOR, *KEYS[3:7], CREATOR))
           + struct.pack('<QQ', 5, 1))
    return 'Program data: ' + base64.b64encode(raw).decode()


def test_buy_exact_units_identity_and_pre_reserves():
    line = buy()
    ordinal, event = PS.events(logs(line))[0]
    assert ordinal == 1
    assert (event['kind'], event['pool'], event['actor'], event['coin_creator']) == ('buy', POOL, ACTOR, CREATOR)
    assert event['base_atomic'] == '100' and event['quote_atomic'] == '1004'
    assert event['pre_base_reserve_atomic'] == '1000'
    assert event['pre_quote_reserve_atomic'] == str(2**64-1)
    assert (event['lp_fee_bps'], event['protocol_fee_bps'], event['creator_fee_bps']) == (20, 5, 5)
    assert event['payload']['pool_quote_token_reserves'] == 2**64-1
    assert event['payload_b64'] == line[14:]
    assert not {'base_mint', 'base_decimals', 'supply_atomic', 'virtual_quote_atomic'} & event.keys()


def test_exact_quote_v2_uses_paid_quote_and_shared_post_reserve_rule():
    _, event = PS.events(logs(buy(ix_name='buy_exact_quote_in_v2', quote_amount_in=1004,
                                  user_quote_amount_in=1000, buyback_fee=1, pool_quote_token_reserves=4000)))[0]
    assert event['quote_atomic'] == '1004'
    assert event['post_base_reserve_atomic'] == '900'
    assert event['post_quote_reserve_atomic'] == '5003'


def test_sell_preserves_actor_and_all_three_known_fee_legs():
    event = PS.events(logs(sell()))[0][1]
    assert (event['kind'], event['pool'], event['actor'], event['coin_creator']) == ('sell', POOL, ACTOR, CREATOR)
    assert event['base_atomic'] == '100' and event['quote_atomic'] == '1001'
    assert event['pre_base_reserve_atomic'] == str(2**64-1)
    assert event['payload']['pool'] == POOL and event['payload']['user'] == ACTOR
    assert event['post_base_reserve_atomic'] is None and event['post_quote_reserve_atomic'] is None


def test_all_events_keep_actual_log_ordinal_and_ignore_nested_foreign_program():
    foreign = KEYS[-1]
    rows = PS.events(logs(buy(), f'Program {foreign} invoke [2]', buy(), f'Program {foreign} success', sell()))
    assert [row[0] for row in rows] == [1, 5]
    assert [row[1]['kind'] for row in rows] == ['buy', 'sell']
    assert PS.events([buy()]) == []


@pytest.mark.parametrize('corruption', ['truncated', 'unbalanced', 'wrong_return', 'bad_depth', 'failed'])
def test_incomplete_or_failed_log_stack_refuses_all_events(corruption):
    lines = logs(buy())
    if corruption == 'truncated':
        lines.insert(-1, 'Log truncated')
    elif corruption == 'unbalanced':
        lines.pop()
    elif corruption == 'wrong_return':
        lines[-1] = f'Program {KEYS[-1]} success'
    elif corruption == 'bad_depth':
        lines[0] = f'Program {PS.AMM_PROGRAM} invoke [2]'
    else:
        lines[-1] = f'Program {PS.AMM_PROGRAM} failed: custom program error: 1'
    with pytest.raises(PS.DecodeLoss, match='logs'):
        PS.events(lines)


@pytest.mark.parametrize('line', ['Program data: !bad!', 'Program data: YQ==',
    'Program data: ' + base64.b64encode(BUY_EVENT_DISCRIMINATOR + bytes(20)).decode()])
def test_malformed_payload_is_not_an_event(line):
    with pytest.raises(PS.DecodeLoss, match='event'):
        PS.events(logs(line))


def test_inconsistent_gross_quote_and_unknown_buy_version_refuse():
    with pytest.raises(PS.DecodeLoss):
        PS.events(logs(buy(user_quote_amount_in=1005)))
    with pytest.raises(PS.DecodeLoss):
        PS.events(logs(buy(ix_name='some_future_buy')))
    raw = base64.b64decode(buy()[14:]) + b'unknown-version-tail'
    assert base64.b64decode(PS.events(logs('Program data: ' + base64.b64encode(raw).decode()))[0][1]['payload']['unparsed_tail_b64']) == b'unknown-version-tail'


def test_real_migration_maps_only_the_proven_canonical_wsol_pool():
    fixture = json.loads((FIXTURES / 'pump_migrate_frames.json').read_text())
    with pytest.raises(PS.DecodeLoss):  # recorded fixture is truncated after the event
        PS.events(fixture['wsol']['logs'])
    create = lambda key: next(line for line in fixture[key]['logs'] if line.startswith('Program data: ')
                              and base64.b64decode(line[14:])[:8].hex() == 'b1310cd2a076a774')
    event = PS.events(logs(create('wsol')))[0][1]
    assert event['base_mint'] == '6UTgBWACzB1zX2GA2uEvVn6325XE99wNvkVTXv5qpump'
    assert event['quote_mint'] == PS.WSOL_MINT and event['canonical'] is True
    assert event['actor'] is None and event['pool_creator'] != event['base_mint']
    usdc = PS.events(logs(create('usdc')))[0][1]
    assert usdc['canonical'] is False and usdc['quote_mint'] != PS.WSOL_MINT
    assert not {'base_decimals', 'supply_atomic', 'virtual_quote_atomic'} & event.keys()


def test_canonical_requires_the_pool_pda_not_only_authority():
    fixture = json.loads((FIXTURES / 'pump_migrate_frames.json').read_text())
    create = next(line for line in fixture['wsol']['logs'] if line.startswith('Program data: ')
                  and base64.b64decode(line[14:])[:8].hex() == 'b1310cd2a076a774')
    raw = bytearray(base64.b64decode(create[14:]))
    raw[173:205] = bytes(Pubkey.from_string(POOL))
    event = PS.events(logs('Program data: ' + base64.b64encode(raw).decode()))[0][1]
    assert event['canonical'] is False


def test_real_v2_receipts_decode_without_network_or_account_reads():
    fixture = json.loads((FIXTURES / 'pumpswap_v2_receipts_20261008.json').read_text())
    for name in ('tardigrade', 'v2_second'):
        rows = [row for _, row in PS.events(fixture[name]['tx']['meta']['logMessages']) if row['kind'] == 'buy']
        assert len(rows) == 1 and rows[0]['payload']['ix_name'] == 'buy_exact_quote_in_v2'
        assert int(rows[0]['quote_atomic']) > 0


@pytest.mark.parametrize('disc', ['67f4521f2cf57777', '3e2f370aa503dc2a', 'b1310cd2a076a774'])
def test_valid_event_plus_malformed_recognized_event_loses_the_whole_transaction(disc):
    bad = 'Program data: ' + base64.b64encode(bytes.fromhex(disc) + bytes(10)).decode()
    with pytest.raises(PS.DecodeLoss, match='event'):
        PS.events(logs(buy(), bad))


def test_unknown_well_formed_discriminator_does_not_hide_valid_events():
    irrelevant = 'Program data: ' + base64.b64encode(bytes.fromhex('1122334455667788')).decode()
    assert len(PS.events(logs(irrelevant, buy()))) == 1
