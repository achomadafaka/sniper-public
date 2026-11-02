"""Hard execution bounds survive compiled account indexes, ALTs and transaction limits."""
import pytest
from solders.address_lookup_table_account import AddressLookupTableAccount
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import Message, MessageHeader, MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction


S = 450_780_262
PAYER, PROGRAM, WRITABLE, READONLY, TABLE = [Pubkey.from_bytes(bytes([i]) * 32) for i in range(1, 6)]


def message(*, lookup=False, legacy=False, payload=b'swap'):
    ix = Instruction(PROGRAM, payload,
        [AccountMeta(WRITABLE, False, True), AccountMeta(READONLY, False, False)])
    if legacy:
        return Message.new_with_blockhash([ix], PAYER, Hash.default())
    tables = [AddressLookupTableAccount(TABLE, [WRITABLE, READONLY])] if lookup else []
    return MessageV0.try_compile(PAYER, [ix], tables, Hash.default())


def test_guard_bytes_match_verified_lighthouse_layout():
    from sniper.execution import solana_slot_guard as g
    lo, hi = g.slot_guard_instructions(S, S + 1)
    assert str(lo.program_id) == str(hi.program_id) == 'L2TExMFKdjpN9kozasaurPirfHy9P8sbXoAN1qA3S95'
    assert bytes(lo.data) == bytes.fromhex('0f0000665cde1a0000000004')
    assert bytes(hi.data) == bytes.fromhex('0f0000675cde1a0000000005')
    assert list(lo.accounts) == list(hi.accounts) == []


@pytest.mark.parametrize('slot,allowed', [(S - 1, False), (S, True), (S + 1, True), (S + 2, False)])
def test_guard_inclusive_source_and_next_slot(slot, allowed):
    from sniper.execution import solana_slot_guard as g
    lo, hi = g.slot_guard_instructions(S, S + 1)
    assert ((slot >= int.from_bytes(bytes(lo.data)[3:11], 'little'))
            and (slot <= int.from_bytes(bytes(hi.data)[3:11], 'little'))) is allowed


@pytest.mark.parametrize('lo,hi', [(True, S), (S, True), (-1, S), (S, 2**64),
    (S + 1, S), (float(S), S + 1), (str(S), S + 1)])
def test_bad_ranges_refuse(lo, hi):
    from sniper.execution import solana_slot_guard as g
    with pytest.raises(ValueError):
        g.slot_guard_instructions(lo, hi)
    with pytest.raises(ValueError):
        g.append_slot_guard(message(), lo, hi)


@pytest.mark.parametrize('lookup,legacy', [(False, False), (True, False), (False, True)])
def test_append_keeps_existing_instruction_and_account_identity(lookup, legacy):
    from sniper.execution import solana_slot_guard as g
    before = message(lookup=lookup, legacy=legacy)
    after = g.append_slot_guard(before, S, S + 1)
    assert type(after) is type(before)
    assert after.recent_blockhash == before.recent_blockhash
    assert after.header.num_required_signatures == before.header.num_required_signatures
    assert after.header.num_readonly_signed_accounts == before.header.num_readonly_signed_accounts
    assert after.header.num_readonly_unsigned_accounts == before.header.num_readonly_unsigned_accounts + 1
    assert list(after.account_keys[:-1]) == list(before.account_keys)
    assert str(after.account_keys[-1]) == g.LIGHTHOUSE_PROGRAM
    old_ix, new_ix = before.instructions[0], after.instructions[0]
    assert bytes(old_ix.data) == bytes(new_ix.data) == b'swap'
    loaded = [WRITABLE, READONLY] if lookup else []
    old_keys, new_keys = list(before.account_keys) + loaded, list(after.account_keys) + loaded
    assert new_keys[new_ix.program_id_index] == old_keys[old_ix.program_id_index] == PROGRAM
    assert [new_keys[i] for i in new_ix.accounts] == [old_keys[i] for i in old_ix.accounts]
    if lookup:
        assert list(after.address_table_lookups) == list(before.address_table_lookups)
        assert list(new_ix.accounts) == [i + 1 for i in old_ix.accounts]
    assert len(after.instructions) == len(before.instructions) + 2
    assert [bytes(ix.data) for ix in after.instructions[-2:]] == [
        bytes.fromhex('0f0000665cde1a0000000004'), bytes.fromhex('0f0000675cde1a0000000005')]
    assert all(ix.program_id_index == len(after.account_keys) - 1 and not ix.accounts
               for ix in after.instructions[-2:])
    old_wire = bytes(VersionedTransaction.populate(before, [Signature.default()]))
    new_wire = bytes(VersionedTransaction.populate(after, [Signature.default()]))
    assert len(new_wire) - len(old_wire) == 62


def test_identical_guard_is_idempotent_but_different_bounds_refuse():
    from sniper.execution import solana_slot_guard as g
    once = g.append_slot_guard(message(lookup=True), S, S + 1)
    assert bytes(g.append_slot_guard(once, S, S + 1)) == bytes(once)
    with pytest.raises(ValueError, match='conflict'):
        g.append_slot_guard(once, S, S + 2)


def test_unrelated_lighthouse_instruction_is_not_treated_as_our_guard():
    from sniper.execution import solana_slot_guard as g
    unrelated = Instruction(Pubkey.from_string(g.LIGHTHOUSE_PROGRAM), b'unrelated', [])
    before = MessageV0.try_compile(PAYER, [unrelated], [], Hash.default())
    with pytest.raises(ValueError, match='conflict'):
        g.append_slot_guard(before, S, S + 1)


def test_guard_refuses_account_index_overflow():
    from sniper.execution import solana_slot_guard as g
    keys = [Pubkey.from_bytes(i.to_bytes(32, 'little')) for i in range(254)]
    lookups = [AddressLookupTableAccount(TABLE, [WRITABLE, READONLY])]
    before = message(lookup=True)
    full = MessageV0(MessageHeader(1, 0, 253), keys, before.recent_blockhash,
                     list(before.instructions), list(before.address_table_lookups))
    with pytest.raises(ValueError, match='account'):
        g.append_slot_guard(full, S, S + 1)


@pytest.mark.parametrize('legacy', [False, True])
def test_guard_refuses_over_packet_limit_before_signing(legacy):
    from sniper.execution import solana_slot_guard as g
    before = message(legacy=legacy, payload=bytes(950))
    assert len(bytes(VersionedTransaction.populate(before, [Signature.default()]))) <= 1232
    with pytest.raises(ValueError, match='packet'):
        g.append_slot_guard(before, S, S + 1)


@pytest.mark.parametrize('limit,expected', [(200_000, 201_016), (1_399_500, 1_400_000),
                                           (1_400_000, 1_400_000)])
def test_guard_reserves_measured_compute_once_without_increasing_priority_fee(limit, expected):
    from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
    from sniper.execution import solana_slot_guard as g
    price = set_compute_unit_price(12345)
    before = MessageV0.try_compile(PAYER, [set_compute_unit_limit(limit), price,
        Instruction(PROGRAM, b'swap', [])], [], Hash.default())
    after = g.append_slot_guard(before, S, S + 1)
    assert bytes(after.instructions[0].data) == b'\x02' + expected.to_bytes(4, 'little')
    assert bytes(after.instructions[1].data) == b'\x03' + (12345 * limit // expected).to_bytes(8, 'little')
    assert bytes(after.instructions[2].data) == b'swap'
    assert bytes(g.append_slot_guard(after, S, S + 1)) == bytes(after)


@pytest.mark.parametrize('data', [b'\x02', b'\x02\x01\x00', b'\x02' + bytes(5),
                                  b'\x02' + bytes(4), b'\x02' + (1_400_001).to_bytes(4, 'little')])
def test_malformed_compute_limit_refuses(data):
    from sniper.execution import solana_slot_guard as g
    limit = Instruction(Pubkey.from_string('ComputeBudget111111111111111111111111111111'), data, [])
    before = MessageV0.try_compile(PAYER, [limit, Instruction(PROGRAM, b'swap', [])], [], Hash.default())
    with pytest.raises(ValueError, match='compute'):
        g.append_slot_guard(before, S, S + 1)


def test_guard_refuses_duplicate_compute_limits():
    from solders.compute_budget import set_compute_unit_limit
    from sniper.execution import solana_slot_guard as g
    before = MessageV0.try_compile(PAYER,
        [set_compute_unit_limit(200_000), set_compute_unit_limit(200_000)], [], Hash.default())
    with pytest.raises(ValueError, match='compute'):
        g.append_slot_guard(before, S, S + 1)


def test_guard_does_not_add_a_compute_limit_when_absent():
    from sniper.execution import solana_slot_guard as g
    after = g.append_slot_guard(message(), S, S + 1)
    assert 'ComputeBudget111111111111111111111111111111' not in [str(k) for k in after.account_keys]


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('price,target', [(17_500_000, 7_000_000), (15_000_000, 6_000_000)])
def test_guard_preserves_actual_priority_and_tip_budget_without_downstream_caps(legacy, price, target):
    import base64
    from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
    from solders.system_program import transfer, TransferParams
    from sniper.execution.solana_swap_executor import signed_priority_fee_lamports
    from sniper.execution import solana_slot_guard as g
    ixs = [set_compute_unit_limit(400_000), set_compute_unit_price(price),
        transfer(TransferParams(from_pubkey=PAYER, to_pubkey=WRITABLE, lamports=5_000_000))]
    before = (Message.new_with_blockhash(ixs, PAYER, Hash.default()) if legacy else
              MessageV0.try_compile(PAYER, ixs, [], Hash.default()))
    after = g.append_slot_guard(before, S, S + 1)
    payload = base64.b64encode(bytes(VersionedTransaction.populate(after, [Signature.default()]))).decode()
    priority = signed_priority_fee_lamports(payload)
    assert priority is not None and priority <= target
    assert priority + 5_000_000 <= target + 5_000_000
    assert bytes(after.instructions[2].data) == bytes(before.instructions[2].data)


@pytest.mark.parametrize('data', [b'\x03', b'\x03' + bytes(7), b'\x03' + bytes(9)])
def test_malformed_compute_price_refuses(data):
    from solders.compute_budget import set_compute_unit_limit
    from sniper.execution import solana_slot_guard as g
    price = Instruction(Pubkey.from_string('ComputeBudget111111111111111111111111111111'), data, [])
    before = MessageV0.try_compile(PAYER, [price, set_compute_unit_limit(200_000)], [], Hash.default())
    with pytest.raises(ValueError, match='compute'):
        g.append_slot_guard(before, S, S + 1)


def test_duplicate_compute_prices_refuse():
    from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
    from sniper.execution import solana_slot_guard as g
    before = MessageV0.try_compile(PAYER, [set_compute_unit_price(1),
        set_compute_unit_limit(200_000), set_compute_unit_price(1)], [], Hash.default())
    with pytest.raises(ValueError, match='compute'):
        g.append_slot_guard(before, S, S + 1)


@pytest.mark.parametrize('price', [0, 1])
def test_implicit_compute_limit_only_allows_zero_price(price):
    from solders.compute_budget import set_compute_unit_price
    from sniper.execution import solana_slot_guard as g
    before = MessageV0.try_compile(PAYER, [set_compute_unit_price(price)], [], Hash.default())
    if price:
        with pytest.raises(ValueError, match='compute'):
            g.append_slot_guard(before, S, S + 1)
    else:
        after = g.append_slot_guard(before, S, S + 1)
        assert bytes(after.instructions[0].data) == bytes(before.instructions[0].data)
