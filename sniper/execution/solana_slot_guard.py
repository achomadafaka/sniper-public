"""Atomic Clock.slot bounds using the immutable mainnet Lighthouse program.

AssertSysvarClock: discriminator 15, silent log 0, Slot variant 0, u64 LE, integer operator.
The unchanged deployment at slot 294179293 was read and simulated again on 2026-10-19:
two assertions add 1016 CU and 62 bytes; a breach rolls back swaps but still costs a fee.
"""
from solders.instruction import CompiledInstruction, Instruction
from solders.message import Message, MessageHeader, MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction


LIGHTHOUSE_PROGRAM = 'L2TExMFKdjpN9kozasaurPirfHy9P8sbXoAN1qA3S95'
GUARD_COMPUTE_UNITS = 1016


class SlotGuardPacketOverflow(ValueError):
    def __init__(self, wire_size):
        self.wire_size = wire_size
        super().__init__('entry_slot_guard_packet_oversized')


def slot_guard_failure(receipt, *, signature):
    """Prove our upper Clock.slot assertion failed, using an already-read exact receipt."""
    import base58
    try:
        meta, transaction = receipt['meta'], receipt['transaction']
        if not signature or transaction['signatures'][0] != signature:
            return None
        index, error = meta['err']['InstructionError']
        message = transaction['message']
        instructions, keys = message['instructions'], message['accountKeys']
        if (type(index) is not int or index < 1 or index != len(instructions) - 1
                or error != {'Custom': 6001} or type(error['Custom']) is not int):
            return None
        bounds = []
        for ix, op in zip(instructions[-2:], (4, 5)):
            program_index = ix['programIdIndex']
            if (type(program_index) is not int or not 0 <= program_index < len(keys)
                    or keys[program_index] != LIGHTHOUSE_PROGRAM or ix['accounts'] != []
                    or not isinstance(ix['data'], str)):
                return None
            data = base58.b58decode(ix['data'])
            if len(data) != 12 or data[:3] != b'\x0f\x00\x00' or data[-1] != op:
                return None
            bounds.append(int.from_bytes(data[3:11], 'little'))
        source, maximum = bounds
        landed = receipt['slot']
        if not (source <= maximum <= source + 1 and type(landed) is int and landed > maximum):
            return None
        fee = meta.get('fee')
        return dict(reason='slot_window_expired', source_slot=source, max_slot=maximum,
            landing_slot=landed, instruction_index=index, failed_program=LIGHTHOUSE_PROGRAM,
            program_error='AssertionFailed', fee_lamports=fee if type(fee) is int and fee >= 0 else None,
            no_tokens_bought=True)
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def slot_guard_instructions(min_slot, max_slot):
    """Two account-free assertions for the inclusive execution range; no reads or signing."""
    if (type(min_slot) is not int or type(max_slot) is not int
            or not 0 <= min_slot <= max_slot < 2**64):
        raise ValueError('entry_slot_guard_range_invalid')
    program = Pubkey.from_string(LIGHTHOUSE_PROGRAM)
    return [Instruction(program, b'\x0f\x00\x00' + value.to_bytes(8, 'little') + bytes([op]), [])
            for value, op in ((min_slot, 4), (max_slot, 5))]


def append_slot_guard(message, min_slot, max_slot):
    """Append exact bounds without changing old instruction positions or ALT identities.

    Existing identical final guards are idempotent; conflicting Lighthouse use refuses.
    Placeholder signatures measure packet size without ever signing a funded transaction.
    """
    guards = slot_guard_instructions(min_slot, max_slot)
    if not isinstance(message, (Message, MessageV0)):
        raise ValueError('entry_slot_guard_message_invalid')
    keys, header, instructions = list(message.account_keys), message.header, list(message.instructions)
    lookups = list(message.address_table_lookups) if isinstance(message, MessageV0) else []
    loaded = sum(len(table.writable_indexes) + len(table.readonly_indexes) for table in lookups)
    total = len(keys) + loaded
    if (not 1 <= header.num_required_signatures <= len(keys) or len(set(keys)) != len(keys)
            or header.num_readonly_signed_accounts >= header.num_required_signatures
            or header.num_readonly_unsigned_accounts > len(keys) - header.num_required_signatures
            or total > 256 or any(ix.program_id_index >= len(keys)
                or any(index >= total for index in ix.accounts) for ix in instructions)):
        raise ValueError('entry_slot_guard_accounts_invalid')
    program = guards[0].program_id
    if program in keys:
        index = keys.index(program)
        if index < len(keys) - header.num_readonly_unsigned_accounts:
            raise ValueError('entry_slot_guard_program_writable')
        existing = [ix for ix in instructions if ix.program_id_index == index]
        exact = [CompiledInstruction(index, bytes(ix.data), b'') for ix in guards]
        if existing and (existing != exact or instructions[-2:] != exact):
            raise ValueError('entry_slot_guard_conflict')
        out = message if existing else None
    else:
        if total >= 256:
            raise ValueError('entry_slot_guard_accounts_overflow')
        index = len(keys)
        keys.append(program)
        header = MessageHeader(header.num_required_signatures, header.num_readonly_signed_accounts,
                               header.num_readonly_unsigned_accounts + 1)
        instructions = [CompiledInstruction(ix.program_id_index, bytes(ix.data),
            bytes(a + 1 if a >= index else a for a in ix.accounts)) for ix in instructions]
        out = None
    budget_program = Pubkey.from_string('ComputeBudget111111111111111111111111111111')
    limit = price = price_position = None
    for position, ix in enumerate(instructions):
        data = bytes(ix.data)
        if keys[ix.program_id_index] != budget_program:
            continue
        if data[:1] == b'\x02':
            value = int.from_bytes(data[1:], 'little')
            if limit is not None or len(data) != 5 or ix.accounts or not 0 < value <= 1_400_000:
                raise ValueError('entry_slot_guard_compute_limit_invalid')
            limit = value
            if out is None:
                instructions[position] = CompiledInstruction(ix.program_id_index,
                    b'\x02' + min(limit + GUARD_COMPUTE_UNITS, 1_400_000).to_bytes(4, 'little'), b'')
        elif data[:1] == b'\x03':
            if price_position is not None or len(data) != 9 or ix.accounts:
                raise ValueError('entry_slot_guard_compute_price_invalid')
            price, price_position = int.from_bytes(data[1:], 'little'), position
    if price:
        if limit is None:
            raise ValueError('entry_slot_guard_compute_budget_unknown')
        if out is None:
            # Extra guard capacity must never raise the signed priority fee or crowd budget.
            adjusted = price * limit // min(limit + GUARD_COMPUTE_UNITS, 1_400_000)
            ix = instructions[price_position]
            instructions[price_position] = CompiledInstruction(ix.program_id_index,
                b'\x03' + adjusted.to_bytes(8, 'little'), b'')
    if out is None:
        instructions.extend(CompiledInstruction(index, bytes(ix.data), b'') for ix in guards)
        out = (MessageV0(header, keys, message.recent_blockhash, instructions, lookups)
               if isinstance(message, MessageV0) else Message.new_with_compiled_instructions(
                   header.num_required_signatures, header.num_readonly_signed_accounts,
                   header.num_readonly_unsigned_accounts, keys, message.recent_blockhash, instructions))
    wire = bytes(VersionedTransaction.populate(out, [Signature.default()] * header.num_required_signatures))
    if len(wire) > 1232:
        raise SlotGuardPacketOverflow(len(wire))
    return out
