"""The recorded 45Ej creator packet provides structure, never invented curve state."""
import copy
import json
from pathlib import Path

import base58
import pytest

from sniper.feeds.yellowstone import geyser_pb2 as pb
from sniper.services import memecoin_rabbit_signal as rabbit
from sniper.services import memecoin_shyft_prepare as prep


REC = json.loads((Path(__file__).parent / 'fixtures/memecoin/herrrrr_create_buy_2b382050.json').read_text())


def recorded_candidate():
    return dict(signature=REC['signature'], owner=REC['owner'], slot=REC['slot'],
                recv_monotonic=1., generation=1, account_keys=tuple(REC['keys']),
                signers=tuple(REC['keys'][i] for i in REC['signer_indices']),
                writable=tuple(REC['writable']), version=REC['version'],
                instructions=tuple((REC['keys'][ix['program']],
                    tuple(REC['keys'][i] for i in ix['accounts']), bytes.fromhex(ix['data_hex']))
                    for ix in REC['instructions']))


def test_recorded_create_extend_buy_prepares_the_same_bound_curve():
    candidate = recorded_candidate()
    assert prep.preparation_target(candidate) == {
        'venue': 'pump_curve', 'mint': REC['keys'][1], 'pool': REC['keys'][3]}
    assert rabbit.source_buy(candidate, prep.preparation_target(candidate)) == (
        REC['keys'][1], {'spend': 4_000_000_000}, 'direct')
    assert not {'source_initiation', 'source_price', 'reserves'} & candidate.keys()


@pytest.mark.parametrize('damage', ['mint', 'owner', 'program', 'curve', 'mint_signer',
    'creator', 'mayhem', 'cashback', 'truncated', 'trailing', 'extend_owner', 'extend_curve',
    'extend_data', 'extra_create', 'extra_buy', 'unknown_pump', 'mayhem_program', 'mayhem_global',
    'mayhem_vault', 'mayhem_state', 'mayhem_token_vault', 'utf8', 'string_length'])
def test_creator_setup_must_bind_exactly_to_its_single_buy(damage):
    candidate = copy.deepcopy(recorded_candidate())
    instructions = list(candidate['instructions'])
    index = 3 if damage.startswith('extend_') else 2
    program, accounts, data = instructions[index]
    accounts = list(accounts)
    if damage == 'mint': accounts[0] = REC['keys'][2]
    if damage == 'owner': accounts[5] = REC['keys'][2]
    if damage == 'program': accounts[7] = REC['keys'][28]
    if damage == 'curve': accounts[2] = REC['keys'][2]
    if damage.startswith('mayhem_'):
        accounts[{'mayhem_program': 9, 'mayhem_global': 10, 'mayhem_vault': 11,
                  'mayhem_state': 12, 'mayhem_token_vault': 13}[damage]] = REC['keys'][2]
    if damage == 'mint_signer': candidate['signers'] = (candidate['owner'],)
    if damage == 'creator': data = data[:-34] + bytes(32) + data[-2:]
    if damage == 'mayhem': data = data[:-2] + b'\x01\x00'
    if damage == 'cashback': data = data[:-1] + b'\x01'
    if damage == 'truncated': data = data[:-1]
    if damage == 'trailing': data += b'\x00'
    if damage == 'utf8': data = data[:12] + b'\xff' + data[13:]
    if damage == 'string_length': data = data[:8] + bytes([255]) * 4 + data[12:]
    if damage == 'extend_owner': accounts[1] = REC['keys'][2]
    if damage == 'extend_curve': accounts[0] = REC['keys'][2]
    if damage == 'extend_data': data += b'\x00'
    instructions[index] = (program, tuple(accounts), data)
    if damage == 'extra_create': instructions.append(instructions[2])
    if damage == 'extra_buy': instructions.append(instructions[5])
    if damage == 'unknown_pump': instructions.append((instructions[2][0], (), bytes(8)))
    candidate['instructions'] = tuple(instructions)
    assert prep.preparation_target(candidate) is None


def test_recognized_creator_packet_cannot_invent_a_not_yet_existing_curve():
    candidate = recorded_candidate()
    update = pb.SubscribeUpdate()
    msg = update.transaction.transaction.transaction.message
    msg.header.num_required_signatures = len(candidate['signers'])
    msg.header.num_readonly_unsigned_accounts = len(REC['keys']) - sum(REC['writable'])
    msg.account_keys.extend(base58.b58decode(key) for key in REC['keys'])
    calls = []
    handler = rabbit.CurveHandler()
    handler.bind(type('Buyer', (), {'read': lambda _self, method, params:
        calls.append((method, params)) or {'context': {'slot': REC['slot']}, 'value': [None] * 4}})())
    matched = handler.match(candidate, update)
    assert matched is not None
    state = handler.read(matched[0])
    assert state == {'reason': 'no_curve'}
    assert calls[0][0] == 'getMultipleAccounts'
