"""Pure sponsored-account setup facts from the existing program stream, never trade authority.

Account layout: pump-fun/pump-public-docs idl/{pump,pump_amm}.json,
init_user_volume_accumulator. A sponsor is not a proven funder or controller.
"""
from solders.pubkey import Pubkey

PUMP = '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P'
PUMPSWAP = 'pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA'
INIT = bytes.fromhex('5e06ca73ff60e8b7')
PROGRAMS = {bytes(Pubkey.from_string(p)): Pubkey.from_string(p) for p in (PUMP, PUMPSWAP)}
EVENTS = {p: bytes(Pubkey.find_program_address([b'__event_authority'], v)[0]) for p, v in PROGRAMS.items()}


def decode_setups(keys, signer_count, readonly_signed, instructions, watched):
    """Direct top-level instructions only; no CPI signer inference or network reads."""
    if not watched:
        return []
    if len(instructions) > 128:
        raise ValueError('instruction_cap')
    if not 0 <= readonly_signed <= signer_count <= len(keys):
        raise ValueError('signer_header')
    out = []
    for outer, ix in enumerate(instructions):
        if bytes(ix.data) != INIT or not 0 <= ix.program_id_index < len(keys):
            continue
        program = bytes(keys[ix.program_id_index])
        if program not in PROGRAMS:
            continue
        accounts = list(ix.accounts)
        if len(accounts) != 6 or any(not 0 <= i < len(keys) for i in accounts):
            continue
        payer, user, accumulator, system, event, declared = (bytes(keys[i]) for i in accounts)
        if any(len(k) != 32 for k in (payer, user, accumulator, system, event, declared)):
            continue
        if accounts[0] >= signer_count - readonly_signed or system != bytes(32) or declared != program or event != EVENTS[program]:
            continue
        payer_s, user_s = str(Pubkey(payer)), str(Pubkey(user))
        if payer_s not in watched and user_s not in watched:
            continue
        expected = bytes(Pubkey.find_program_address([b'user_volume_accumulator', user], PROGRAMS[program])[0])
        if accumulator != expected:
            continue
        out.append(dict(outer_ix=outer, program=str(PROGRAMS[program]), payer=payer_s, wallet=user_s, kind='account_setup_sponsor'))
    return out
