"""RabbitStream static preparation. No source proof, executor, signing or DB access.

The pending index belongs to the slot-signal event loop. Its two-second lifetime
is independent of the authoritative source's original 500 ms execution window.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import functools
import math
import os
import time
from urllib.parse import urlsplit

import base58
from sniper.feeds.yellowstone import geyser_pb2 as pb


EUROPE_HOSTS = frozenset({'rabbitstream.ams.shyft.to', 'rabbitstream.fra.shyft.to'})
MISS_REASONS = ('structure', 'signature', 'header', 'alt', 'config', 'unsupported_target')
DFLOW_PROGRAM = 'DF1ow4tspfHX9JwWJsAb9epbkA8hmpSEAtxXy1V27QBH'
OKX_PROGRAM = 'proVF4pMXVaYqmy4NjniPh4pqKNfMmsihgd4wdkCX3u'   # okx_swap.OKX_PROGRAM
JUPITER_PROGRAM = 'JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4'   # jupiter_route.JUPITER_PROGRAM
FLASHX_PROGRAM = 'FLASHX8DrLbgeR8FcfNV1F5krxYcYMUdBkrP1EPBtxB9'


def dflow_preparation_accounts(accounts, data, owner):
    """Known DFlow swap actions, yielding references only, never inferred pool offsets.

    ABI: confirmed on-chain IDL Cp2dCjxCWdktak2JiSrh87X6sz31EnDVKoTGtsHJvhYq,
    SHA256 e9e1382b42816ef93615e788a8588bc3b53e7051d0f27fdbb0a02664d028cdc1.
    That IDL does not specify remaining-account layouts. The bounded reference batch
    therefore goes through the existing chain pool/mint validation before publication.
    Unknown actions, dynamic variants and ambiguous PumpSwap plans remain unsupported.
    """
    from solders.pubkey import Pubkey
    from sniper.execution.pump_curve_buy import TOKEN_PROGRAM, ATA_PROGRAM, SYSTEM_PROGRAM
    event = str(Pubkey.find_program_address([b'__event_authority'], Pubkey.from_string(DFLOW_PROGRAM))[0])
    refs = tuple(dict.fromkeys(accounts))
    if (not 6 <= len(accounts) <= 256 or not 0 < len(refs) <= 64
            or accounts[:6] != (TOKEN_PROGRAM, ATA_PROGRAM, SYSTEM_PROGRAM, owner, event, DFLOW_PROGRAM)
            or data[:8] != bytes.fromhex('f8c69e91e17587c8')):
        return None
    offset, buys = 8, 0

    def take(size):
        nonlocal offset
        value = data[offset:offset + size]
        if len(value) != size:
            raise ValueError('short DFlow action')
        offset += size
        return value

    try:
        count = int.from_bytes(take(4), 'little')
        if not 0 < count <= 16:
            return None
        for _ in range(count):
            tag = take(1)[0]
            if tag in (12, 82):  # PumpFunAmmBuy / PumpFunAmmBuyV2: amount u64, flags u8
                take(9)
                buys += 1
            elif tag == 54:      # DeriverseSwap: amount u64, flags u8
                take(9)
            elif tag in (37, 38):  # RecordId / RecordId2
                take(76 if tag == 37 else 4)
            elif tag in (50, 60, 78):  # sponsor, ATA setup, sponsor close authority
                pass
            elif tag == 49:      # SetMinimumLegPrices: Vec<LegPrice {u64,u8}>
                prices = int.from_bytes(take(4), 'little')
                if not 0 < prices <= 8:
                    return None
                take(prices * 9)
            elif tag == 31:      # DFlowDynamicRouteV1; these two candidate variants have no fields
                variants = int.from_bytes(take(4), 'little')
                if not 0 < variants <= 4 or any(v not in (2, 8) for v in take(variants)):
                    return None
                take(9)
            else:
                return None
        take(12)  # SwapParams: quoted output u64, slippage u16, platform fee u16; not our price
    except ValueError:
        return None
    return refs if buys == 1 and offset == len(data) else None


@functools.cache
def _dflow_event_authority():
    from solders.pubkey import Pubkey
    return str(Pubkey.find_program_address([b'__event_authority'], Pubkey.from_string(DFLOW_PROGRAM))[0])


def dflow_idl_target(accounts, data, owner):
    """{'venue', 'candidate_accounts'} for an owner-paid DFlow swap whose route, decoded from the pinned
    IDL (dflow_swap), ends in exactly one known buy leg; None otherwise. References only: the IDL
    names no leg's accounts, so a pool or mint is never inferred here."""
    from sniper.execution import dflow_swap
    from sniper.execution.pump_curve_buy import TOKEN_PROGRAM, ATA_PROGRAM, SYSTEM_PROGRAM
    event = _dflow_event_authority()
    refs = tuple(dict.fromkeys(accounts))
    if (not 6 <= len(accounts) <= 256 or not 0 < len(refs) <= 64
            or accounts[:6] != (TOKEN_PROGRAM, ATA_PROGRAM, SYSTEM_PROGRAM, owner, event, DFLOW_PROGRAM)):
        return None
    decoded = dflow_swap.decode_swap(data)
    if decoded is None or decoded['instruction'] not in ('swap', 'swap2'):
        return None
    venue = dflow_swap.buy_leg(decoded)
    return None if venue is None else {'venue': venue, 'candidate_accounts': refs}


def okx_idl_target(accounts, data, owner):
    """{'venue': 'pump_amm', 'candidate_accounts'} for an owner-paid OKX swap_tob (proVF4) whose route,
    decoded from the pinned IDL (okx_swap), has a PumpSwap leg; None otherwise. The PumpSwap buy is a
    CPI inside swap_tob, so without this the frame has no top-level target at all. References only:
    whether the route is one the pre-execution handler models is memecoin_rabbit_okx_amm's call."""
    from sniper.execution import okx_swap
    refs = tuple(dict.fromkeys(accounts))
    if not 5 <= len(accounts) <= 256 or not 0 < len(refs) <= 64 or accounts[0] != owner:
        return None
    decoded = okx_swap.decode_swap_tob(data)
    if decoded is None or not any(dex == okx_swap.PUMP_AMM_DEX for dex, _w, _i in decoded['routes']):
        return None
    return {'venue': 'pump_amm', 'candidate_accounts': refs}


def jupiter_idl_target(accounts, data, owner):
    """{'venue': 'pump_amm', 'candidate_accounts'} for an owner-authorised Jupiter route_v2 with a
    PumpSwap step, decoded from the pinned IDL (jupiter_route); None otherwise. As okx_idl_target: the
    buy is a CPI inside the route, and whether the route is modelled is the handler's call."""
    from sniper.execution import jupiter_route
    refs = tuple(dict.fromkeys(accounts))
    if not 5 <= len(accounts) <= 256 or not 0 < len(refs) <= 64 or accounts[0] != owner:
        return None
    decoded = jupiter_route.decode_route_v2(data)
    if decoded is None or not jupiter_route.pump_steps(decoded):
        return None
    return {'venue': 'pump_amm', 'candidate_accounts': refs}


def _miss(stats, reason):
    # The Rabbit event-loop owns these fixed counters. Diagnostics cannot change decoding.
    if stats is not None:
        try:
            stats[reason] = stats.get(reason, 0) + 1
        except Exception:
            pass
    return None


def _endpoint(url):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.hostname not in EUROPE_HOSTS
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.path not in ('', '/')
                or parsed.query or parsed.fragment or any(c.isspace() for c in url)):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError('rabbit endpoint_invalid') from None
    return parsed.hostname


def prepare_config(env=None):
    env = os.environ if env is None else env
    enabled = env.get('MEMECOIN_SHYFT_PREPARE_ENABLED', '0')
    if enabled == '0':
        return None
    if enabled != '1':
        raise ValueError('rabbit enabled flag must be 0 or 1')
    url = env.get('MEMECOIN_SHYFT_PREPARE_GRPC_URL', '').strip()
    _endpoint(url)
    token = env.get('MEMECOIN_SHYFT_GRPC_TOKEN', '').strip()
    if not token:
        raise ValueError('rabbit token_missing')
    return token, url


def subscription(owners):
    # Success is unknowable before execution. Never subscribe to all with an empty roster.
    transactions = ({'owners': pb.SubscribeRequestFilterTransactions(
        vote=False, account_include=sorted(owners))} if owners else {})
    return pb.SubscribeRequest(transactions=transactions, commitment=pb.PROCESSED)


def flashx_curve_buy(candidate):
    """The receipt-backed native-SOL FLASHX -> Pump V3 buy, from signed top-level fields only."""
    from sniper.execution import pump_curve_buy as curve
    if candidate.get('version') != 'v1':
        return None
    calls = [(i, accounts, data) for i, (program, accounts, data) in enumerate(candidate['instructions'])
             if program == FLASHX_PROGRAM]
    if len(calls) != 2:
        return None
    (setup_index, setup, setup_data), (leg_index, accounts, data) = calls
    # shortcut: only position 73924's route/setup/payees; rotations need another verified receipt.
    if (setup_data != b'\x05' or len(setup) != 7 or len(accounts) != 29
            or len(data) != 22 or data[:1] != b'\x00' or data[17:] != bytes.fromhex('0001344e64')):
        return None
    owner, mint, program, holder = candidate['owner'], accounts[7], accounts[9], accounts[15]
    if (program not in (curve.TOKEN_PROGRAM, curve.TOKEN_2022_PROGRAM)
            or any(accounts[i] != owner for i in (0, 1, 14))
            or setup != (owner, holder, owner, mint, curve.SYSTEM_PROGRAM, program, curve.ATA_PROGRAM)):
        return None
    spend, minimum = int.from_bytes(data[1:9], 'little'), int.from_bytes(data[9:17], 'little')
    if spend <= 0 or minimum <= 0:
        return None
    try:
        base, token = curve._pk(mint), curve._pk(program)
        pool = curve._pda([b'bonding-curve', bytes(base)], curve.PUMP_PROGRAM)
        if holder != str(curve.ata(curve._pk(owner), base, token)):
            return None
        fixed = {2: 'H2cDR3EkJjtTKDQKk8SJS48du9mhsdzQhy8xJx5UMqQK',
                 3: curve.SYSTEM_PROGRAM, 4: FLASHX_PROGRAM, 5: curve.PUMP_PROGRAM,
                 6: str(curve._pda([b'global'], curve.PUMP_PROGRAM)), 8: curve.WSOL_MINT,
                 10: curve.TOKEN_PROGRAM, 11: str(pool), 12: str(curve.ata(pool, base, token)),
                 13: str(curve.ata(pool, curve._pk(curve.WSOL_MINT), curve._pk(curve.TOKEN_PROGRAM))),
                 16: str(curve.ata(curve._pk(owner), curve._pk(curve.WSOL_MINT), curve._pk(curve.TOKEN_PROGRAM))),
                 17: str(curve._pda([b'user_volume_accumulator', bytes(curve._pk(owner))], curve.PUMP_PROGRAM)),
                 18: str(curve._pda([b'fee_config', curve.FEE_CONFIG_SEED], curve.FEE_PROGRAM)),
                 19: 'A7hAgCzFw14fejgCp387JUJRMNyz4j89JKnhtKU8piqW',
                 20: curve.SYSTEM_PROGRAM, 21: str(curve._pda([b'__event_authority'], curve.PUMP_PROGRAM)),
                 22: curve.PUMP_PROGRAM, 23: FLASHX_PROGRAM,
                 24: '3Tu1Y9aNveLFN4WTAwnAwXL6tbUp5MMe3RxyybG4jTAS',
                 25: 'B5FP82QU34BvWnKKBT6fULDZSWmDxegRKKGtuMkEDodX',
                 26: '9uhnAoAjGcextZDgHefmCf7b4ZcmQMT3qWc5LexPgfK',
                 27: '4PVvvzvHhAyvcAHgqvYJajt5y9UvbBritR3jMeDnc4R8', 28: FLASHX_PROGRAM}
        if any(accounts[i] != value for i, value in fixed.items()):
            return None
    except (TypeError, ValueError):
        return None
    return dict(mint=mint, pool=str(pool), holder=holder, token_program=program,
                spend=spend, min_tokens_out=minimum, leg_index=leg_index, setup_index=setup_index)


def _curve_creation_setup(candidate):
    """Only the bound native-SOL create_v2/extend/buy bundle; structure is never curve state.

    Pump's public IDL, sha256 7616681ea557975f9f623ac211080f22a93060781eaf4765b0a488d980997207.
    """
    from sniper.execution import pump_curve_buy as curve
    create, extend = bytes.fromhex('d6904cec5f8b31b4'), bytes.fromhex('ea66c2cb96483ee5')
    calls = [(i, accounts, data) for i, (program, accounts, data) in enumerate(candidate['instructions'])
             if program == curve.PUMP_PROGRAM]
    if len(calls) != 3 or tuple(data[:8] for _, _, data in calls) != (
            create, extend, curve.BUY_EXACT_SOL_IN_DISCRIMINATOR):
        return ()
    (ci, accounts, data), (ei, extension, extension_data), (_, buy, buy_data) = calls
    if len(accounts) != 19 or len(buy) != 18 or len(buy_data) != 25:
        return ()
    mint, owner = accounts[0], candidate['owner']
    if mint not in candidate.get('signers', ()):
        return ()
    try:
        offset = 8
        for _ in range(3):  # Borsh name, symbol, URI; exact bounds, including their length fields.
            if offset + 4 > len(data):
                return ()
            size = int.from_bytes(data[offset:offset + 4], 'little')
            offset += 4
            if offset + size > len(data):
                return ()
            data[offset:offset + size].decode('utf-8')
            offset += size
        # shortcut: recorded self-created, non-mayhem/non-cashback SOL shape; new modes need proof.
        if data[offset:] != bytes(curve._pk(owner)) + b'\x00\x00':
            return ()
        pool = str(curve._pda([b'bonding-curve', bytes(curve._pk(mint))], curve.PUMP_PROGRAM))
        event = str(curve._pda([b'__event_authority'], curve.PUMP_PROGRAM))
        mayhem = 'MAyhSmzXzV1pTf7LsNkrNwkWKTo4ougAJ1PPg47MD4e'
        state = str(curve._pda([b'mayhem-state', bytes(curve._pk(mint))], mayhem))
        expected = {1: str(curve._pda([b'mint-authority'], curve.PUMP_PROGRAM)), 2: pool,
                    3: str(curve.ata(pool, mint, curve.TOKEN_2022_PROGRAM)),
                    4: str(curve._pda([b'global'], curve.PUMP_PROGRAM)), 5: owner,
                    6: curve.SYSTEM_PROGRAM, 7: curve.TOKEN_2022_PROGRAM, 8: curve.ATA_PROGRAM,
                    9: mayhem, 10: str(curve._pda([b'global-params'], mayhem)),
                    11: str(curve._pda([b'sol-vault'], mayhem)), 12: state,
                    13: str(curve.ata(str(curve._pda([b'sol-vault'], mayhem)), mint,
                                      curve.TOKEN_2022_PROGRAM)),
                    14: event, 15: curve.PUMP_PROGRAM, 16: curve.WSOL_MINT,
                    17: str(curve.ata(pool, curve.WSOL_MINT, curve.TOKEN_PROGRAM)),
                    18: curve.TOKEN_PROGRAM}
        if (any(accounts[i] != value for i, value in expected.items())
                or extension != (pool, owner, curve.SYSTEM_PROGRAM, event, curve.PUMP_PROGRAM)
                or extension_data != extend
                or buy[2:5] != (mint, pool, accounts[3]) or buy[6] != owner
                or buy[8] != curve.TOKEN_2022_PROGRAM or buy[10:12] != (event, curve.PUMP_PROGRAM)):
            return ()
    except (ValueError, TypeError, OverflowError):
        return ()
    return ci, ei


def preparation_target(candidate):
    """One known direct instruction or routed static read target; amounts are ignored."""
    from sniper.execution import pump_curve_buy as curve, pumpswap_buy as amm
    from sniper.execution import raydium_launchlab_buy as launch, raydium_cpmm_buy as cpmm
    layouts = {
        (curve.PUMP_PROGRAM, curve.BUY_EXACT_SOL_IN_DISCRIMINATOR): ('pump_curve', 25, (18,), 6, 2, 3),
        (curve.PUMP_PROGRAM, curve.BUY_EXACT_QUOTE_IN_V2_DISCRIMINATOR): ('pump_curve', 24, (27,), 13, 1, 10),
        (amm.AMM_PROGRAM, amm.BUY_EXACT_QUOTE_IN_DISCRIMINATOR): ('pump_amm', 25, (25, 26), 1, 3, 0),
        (launch.LAUNCHLAB_PROGRAM, launch.BUY_EXACT_IN_DISCRIMINATOR): ('raydium_launchlab', 32, (18,), 0, 9, 4),
        (cpmm.CPMM_PROGRAM, cpmm.SWAP_BASE_INPUT_DISCRIMINATOR): ('raydium_cpmm', 24, (13,), 0, 11, 3),
    }
    known_programs = {key[0] for key in layouts}
    found = []
    creation_setup = _curve_creation_setup(candidate)
    for index, (program, accounts, data) in enumerate(candidate['instructions']):
        if index in creation_setup:
            continue
        if program == DFLOW_PROGRAM:
            from sniper.settings import memecoin as cfg
            if cfg.MEMECOIN_SLOT_LANE_RABBIT_SIGNAL:
                target = dflow_idl_target(accounts, data, candidate['owner'])
                if target is None:
                    return None
                found.append(target)
                continue
            refs = dflow_preparation_accounts(accounts, data, candidate['owner'])
            if refs is None:
                return None
            found.append({'venue': 'pump_amm', 'candidate_accounts': refs})
            continue
        if program in (OKX_PROGRAM, JUPITER_PROGRAM):
            decode = okx_idl_target if program == OKX_PROGRAM else jupiter_idl_target
            target = decode(accounts, data, candidate['owner'])
            if target is not None:            # any other router route stays what it was: no target
                found.append(target)
            continue
        if program == FLASHX_PROGRAM:
            if (len(data), len(accounts), data[:1]) in ((1, 7, b'\x05'), (10, 5, b'\x01')):
                continue  # The observed router's setup instructions carry no venue read target.
            curve_buy = flashx_curve_buy(candidate)
            if curve_buy is not None:
                found.append({'venue': 'pump_curve', 'mint': curve_buy['mint'], 'pool': curve_buy['pool']})
                continue
            # shortcut: only the receipt-backed WORM route shape; extend with another bound receipt.
            if (len(data) != 23 or data[:1] != b'\x00' or data[17:] != bytes.fromhex('000231353200')
                    or len(accounts) != 43 or any(accounts[i] != candidate['owner'] for i in (1, 8, 20))
                    or any(accounts[i] != amm.AMM_PROGRAM for i in (18, 35))
                    or accounts[23] != amm.WSOL_MINT or accounts[22] == accounts[23]
                    or accounts[28] not in (amm.TOKEN_PROGRAM, amm.TOKEN_2022_PROGRAM)
                    or accounts[29] != amm.TOKEN_PROGRAM or accounts[30] != amm.SYSTEM_PROGRAM):
                return None
            found.append({'venue': 'pump_amm', 'mint': accounts[22], 'pool': accounts[19]})
            continue
        if program not in known_programs:
            continue
        layout = layouts.get((program, data[:8]))
        if layout is None:
            return None
        venue, size, counts, actor, mint, pool = layout
        if len(data) != size or len(accounts) not in counts or accounts[actor] != candidate['owner']:
            return None
        found.append({'venue': venue, 'mint': accounts[mint], 'pool': accounts[pool]})
    return found[0] if len(found) == 1 else None


def owner_signer(update, owners):
    """(signature, owner) when exactly one tracked owner is a REQUIRED SIGNER of this frame, from
    the static message only (the header and the static account keys; signers are never loaded from
    a lookup table), else None. Needs no execution metadata, so a pre-execution frame with address
    lookup tables still answers. Never raises."""
    try:
        info = update.transaction.transaction
        msg = info.transaction.message
        n = msg.header.num_required_signatures
        if len(info.signature) != 64 or not 0 < n <= len(msg.account_keys):
            return None
        static = msg.account_keys[:n]
        if any(len(key) != 32 for key in static):
            return None
        watched = [key for key in (base58.b58encode(k).decode() for k in static) if key in owners]
        return (base58.b58encode(info.signature).decode(), watched[0]) if len(watched) == 1 else None
    except Exception:
        return None


def preparation_candidate(update, owners, *, recv_monotonic, generation, stats=None, on_owner_frame=None,
                          shapes=None, on_owner_miss=None, on_rejected_candidate=None):
    """Decode static identities only. Missing metadata never implies successful execution.

    `on_owner_frame(signature, owner, slot, recv_monotonic, generation)` (MEMECOIN_SLOT_LANE_RABBIT_SIGNAL)
    stamps every owner-signed frame, decodable or not, for the arrival comparison; it cannot change
    the result, and its own failure is only counted. It runs once the static signer is proven,
    BEFORE any lookup table is resolved: a v0 frame with tables and no execution metadata is still
    stamped (Codex P2, 2026-10-15), then refused for preparation exactly as before.
    `on_owner_miss(signature, owner, reason)` (the rabbit frame line, Lambda 2026-10-16) hears the miss
    reason of a frame refused AFTER its owner signed; it cannot change the result either."""
    if (not update.HasField('transaction') or not math.isfinite(recv_monotonic)
            or not update.transaction.slot or generation < 0):
        return _miss(stats, 'structure')
    info = update.transaction.transaction
    if (info.is_vote or not info.HasField('transaction')
            or (info.HasField('meta') and info.meta.HasField('err'))):
        return _miss(stats, 'structure')
    if len(info.signature) != 64:
        return _miss(stats, 'signature')
    tx, msg = info.transaction, info.transaction.message
    if not tx.HasField('message'):
        return _miss(stats, 'structure')
    if not msg.HasField('header'):
        return _miss(stats, 'header')
    if msg.HasField('config') and not msg.versioned:
        return _miss(stats, 'config')
    n = msg.header.num_required_signatures
    if (not 0 < n <= len(msg.account_keys)
            or msg.header.num_readonly_signed_accounts >= n
            or msg.header.num_readonly_unsigned_accounts > len(msg.account_keys) - n):
        return _miss(stats, 'header')
    if (len(tx.signatures) != n or tx.signatures[0] != info.signature
            or any(len(sig) != 64 for sig in tx.signatures)):
        return _miss(stats, 'signature')
    if len(msg.recent_blockhash) != 32:
        return _miss(stats, 'structure')
    signed = owner_signer(update, owners)
    if signed is None:
        return _miss(stats, 'signature')
    signature, owner = signed

    def miss(reason):
        if on_owner_miss is not None:
            try:
                on_owner_miss(signature, owner, reason)
            except Exception:
                _miss(stats, 'observer_errors')
        return _miss(stats, reason)

    if on_owner_frame is not None:
        try:
            on_owner_frame(signature, owner, update.transaction.slot, recv_monotonic, generation)
        except Exception:
            _miss(stats, 'observer_errors')
    writable, readonly = (), ()
    lookups = msg.address_table_lookups
    if lookups:
        # How RabbitStream presents a lookup-table frame (it resolves tables server-side since
        # 2026-07-24): counted in `shapes` (never a miss reason), so the heartbeat shows whether the
        # resolved addresses arrive in meta.loaded_*_addresses or somewhere this decoder misses.
        _miss(shapes, 'alt_frames')
        if info.HasField('meta'):
            _miss(shapes, 'alt_with_meta')
        if not msg.versioned or not info.HasField('meta'):
            return miss('alt')
        tables = set()
        for table in lookups:
            indices = bytes(table.writable_indexes) + bytes(table.readonly_indexes)
            if (len(table.account_key) != 32 or table.account_key in tables
                    or len(indices) != len(set(indices))):
                return miss('alt')
            tables.add(table.account_key)
        writable = tuple(info.meta.loaded_writable_addresses)
        readonly = tuple(info.meta.loaded_readonly_addresses)
        if (len(writable) != sum(len(t.writable_indexes) for t in lookups)
                or len(readonly) != sum(len(t.readonly_indexes) for t in lookups)):
            return miss('alt')
        _miss(shapes, 'alt_resolved')
    elif info.HasField('meta') and (info.meta.loaded_writable_addresses or info.meta.loaded_readonly_addresses):
        return miss('alt')
    raw_keys = tuple(msg.account_keys) + writable + readonly
    if (not 0 < len(raw_keys) <= 256 or len(raw_keys) != len(set(raw_keys))
            or any(len(key) != 32 for key in raw_keys) or not 0 < len(msg.instructions) <= 64):
        return miss('structure')
    keys = tuple(base58.b58encode(key).decode() for key in raw_keys)
    static_n = len(msg.account_keys)
    n_ro_signed = msg.header.num_readonly_signed_accounts
    n_ro_unsigned = msg.header.num_readonly_unsigned_accounts
    # THE PRE-EXECUTION DECODER SEAM (Beta, 2026-10-17): which keys are writable, by the header for the
    # static keys and by the loaded split for lookup-table keys.
    writable_flags = tuple(
        (i < n - n_ro_signed if i < n else i < static_n - n_ro_unsigned) if i < static_n
        else i < static_n + len(writable) for i in range(len(raw_keys)))
    instructions = []
    for ix in msg.instructions:
        if (ix.program_id_index >= len(keys) or any(index >= len(keys) for index in ix.accounts)
                or len(ix.data) > 1232):
            return miss('structure')
        instructions.append((keys[ix.program_id_index], tuple(keys[i] for i in ix.accounts), bytes(ix.data)))
    candidate = dict(signature=signature, owner=owner,
        slot=update.transaction.slot, recv_monotonic=recv_monotonic, generation=generation,
        account_keys=keys, instructions=tuple(instructions),
        # No transactionConfig here: a preparation candidate carries no execution input (pinned in
        # tests/test_memecoin_shyft_prepare.py); a decoder that records it reads update's message.
        version='v1' if msg.HasField('config') else 'v0' if msg.versioned else 'legacy',
        signers=keys[:n], writable=writable_flags)
    if preparation_target(candidate) is not None:
        return candidate
    miss('unsupported_target')
    if on_rejected_candidate is not None:
        try:
            on_rejected_candidate(candidate)
        except Exception:
            _miss(stats, 'observer_errors')
    return None


class PendingPreparation:
    """Event-loop-owned 128-entry intent/tombstone index; duplicates cannot renew age."""
    def __init__(self):
        self._rows = OrderedDict()
        self.stats = dict(capacity=128, retention_s=2.0, offered=0, duplicate=0,
                          expired=0, overflow=0, matched=0, mismatch=0, late=0, failed=0, retained=0)

    def __len__(self):
        return len(self._rows)

    def expire(self, *, now):
        for key, (stamp, row) in tuple(self._rows.items()):
            if not 0 <= now - stamp < 2.0:
                del self._rows[key]
                self.stats['expired'] += 1
        self.stats['retained'] = len(self._rows)

    def _put(self, key, stamp, row):
        self._rows[key] = (stamp, row)
        if len(self._rows) > 128:
            self._rows.popitem(last=False)
            self.stats['overflow'] += 1
        self.stats['retained'] = len(self._rows)

    def offer(self, candidate, *, now):
        self.expire(now=now)
        stamp = candidate['recv_monotonic']
        if not 0 <= now - stamp < 2.0:
            self.stats['expired'] += 1
            return False
        key = (candidate['signature'], candidate['owner'])
        if key in self._rows:
            self.stats['duplicate'] += 1
            return False
        self._put(key, stamp, dict(candidate))
        self.stats['offered'] += 1
        return True

    def take(self, authoritative, *, now):
        """Join structure only; caller must retain its independent proof and admission gates."""
        self.expire(now=now)
        key = (authoritative['signature'], authoritative['owner'])
        stored = self._rows.get(key)
        if stored is None:
            self._put(key, now, None)  # processed first: a late Rabbit cannot trigger new prep
            self.stats['late'] += 1
            return None
        stamp, row = stored
        if row is None:
            return None
        if any(row[field] != authoritative[field] for field in
               ('signature', 'owner', 'slot', 'account_keys', 'instructions')):
            self._rows[key] = (stamp, None)
            self.stats['mismatch'] += 1
            return None
        self._rows[key] = (stamp, None)
        self.stats['matched'] += 1
        return row


    def discard_signature(self, signature, *, now):
        self.expire(now=now)
        for key, (stamp, row) in tuple(self._rows.items()):
            if key[0] == signature and row is not None:
                self._rows[key] = (stamp, None)
                self.stats['failed'] += 1


def drain_preparations(pending, builders, status, *, now):
    """Existing prefetch owner only: newest two fresh targets, existing cold-read queues."""
    import queue
    for key in ('expired', 'paced_drop', 'unsupported', 'delivered', 'errors'):
        status.setdefault(key, 0)
    rows, consumed = [], 0
    for _ in range(128):
        try:
            row = pending.get_nowait()
        except queue.Empty:
            break
        try:
            consumed += 1
            if 0 <= now - row['recv_monotonic'] < 2.0:
                rows.append(row)
            else:
                status['expired'] += 1
        finally:
            pending.task_done()
    status['paced_drop'] += max(0, len(rows) - 2)
    for row in rows[-2:]:
        try:
            target = preparation_target(row)
            builder = builders.get(target['venue']) if target else None
            pools = getattr(builder, 'pools', None)
            if pools is None:
                status['unsupported'] += 1
                continue
            if 'candidate_accounts' in target and target['venue'] != 'pump_amm':
                # An IDL-decoded DFlow leg into a venue whose cache takes no unsigned references yet.
                key = 'no_prebuild_' + target['venue']
                status[key] = status.get(key, 0) + 1
                continue
            if target['venue'] == 'pump_amm':
                if 'candidate_accounts' in target:
                    pools.offer_candidates(target['candidate_accounts'], received_at=row['recv_monotonic'])
                else:
                    pools.offer_stream(target['pool'])
            elif target['venue'] in ('raydium_launchlab', 'raydium_cpmm'):
                pools.get(target['pool'])
            else:
                status['unsupported'] += 1
                continue
            status['delivered'] += 1
        except Exception:
            status['errors'] += 1
    return consumed


def wait_for_preparation(wake, *, clock=time, not_before=0.0, timeout_s=0.5):
    """Wake the existing owner promptly while absorbing burst hints during its cooldown."""
    deadline, pending = clock.monotonic() + timeout_s, False
    while True:
        now = clock.monotonic()
        if pending and now >= not_before:
            return True
        if now >= deadline:
            return False
        until = min(deadline, not_before) if pending else deadline
        signalled = wake.wait(max(0.0, until - now))
        wake.clear()
        pending = pending or signalled


REROSTER_MIN_S = 0.1          # a flapping roster re-requests at most every 100 ms
PING_S = 5.0                  # the request loop pings this often while the roster holds
ROSTER_POLL_S = 0.1           # between pings the request loop reads the roster itself this often
from asyncio import Event as _RosterEvent    # bound at import: tests swap the module's asyncio


async def run_prepare_feed(url, token, owners_fn, on_prepare, status, stop, *, on_owner_frame=None,
                           on_candidate=None, on_owner_miss=None, on_rejected_candidate=None):
    """One authenticated transaction-only stream. Callback may enqueue static reads only.
    `on_owner_frame` (see preparation_candidate) is passed through unchanged; None = today.
    `on_candidate(candidate, update)` (MEMECOIN_SLOT_LANE_RABBIT_BUY) sees each decoded candidate with
    its frame, on this loop, before the preparation callback; it must not block, and its failure is
    only counted."""
    import grpc
    target = _endpoint(url) + ':443'
    if not token:
        raise ValueError('rabbit token_missing')
    status.update(connected=False, reconnects=0, transactions=0, decoded=0, misses=0,
        miss_reasons=dict.fromkeys(MISS_REASONS, 0),
        alt_frames=dict(alt_frames=0, alt_with_meta=0, alt_resolved=0),
        before_ack=0, callback_errors=0, last_error=None, last_frame_mono=None,
        requested_owners=(), acknowledged_owners=(), endpoint_hostname=_endpoint(url),
        owners_errors=0, owners_error=None)
    generation, backoff = 0, 1.0
    last_owners = {}

    def owners_now():
        # A roster snapshot that raises keeps the last good roster and is named (Gamma W6).
        nonlocal last_owners
        try:
            last_owners = dict(owners_fn())
        except Exception as exc:
            status['owners_errors'] += 1
            status['owners_error'] = type(exc).__name__
        return last_owners
    while not stop.is_set():
        stream, ready, ping_rosters = None, False, {}
        observed_roster, roster_epoch, requested_epoch = None, 0, None
        try:
            generation += 1
            status['connection_generation'] = generation
            ack_deadline = time.monotonic() + 10.0
            # A roster change wakes the request loop at once, as on the processed feed (Alpha (c'),
            # 2026-10-15): the flat 5 s ping sleep held every owner's frames until the new roster acked.
            roster_changed = _RosterEvent()
            def observe_roster(owners, wake=True):
                nonlocal observed_roster, roster_epoch, ready, ack_deadline
                current = tuple(sorted(owners))
                if current != observed_roster:
                    wake = wake and observed_roster is not None   # the first roster is the first request's
                    observed_roster = current
                    roster_epoch += 1
                    ready = False
                    ack_deadline = time.monotonic() + 10.0
                    if wake:
                        roster_changed.set()
                    status.update(connected=False, acknowledged_owners=())
                return current
            async def requests():
                nonlocal requested_epoch
                last, ping = None, 0
                requested_at = float('-inf')
                while not stop.is_set():
                    current = observe_roster(owners_now(), wake=False)
                    if current != last or requested_epoch != roster_epoch:
                        requested_epoch = roster_epoch
                        status['requested_owners'] = current
                        yield subscription(current)
                        requested_at = time.monotonic()
                        last = current
                    ping += 1
                    ping_rosters[ping] = (current, requested_epoch)
                    if len(ping_rosters) > 2:
                        del ping_rosters[min(ping_rosters)]
                    yield pb.SubscribeRequest(ping=pb.SubscribeRequestPing(id=ping))
                    # The next ping at PING_S, or at once on a roster change. Rabbit subscribes
                    # transactions only, so a quiet stream delivers nothing the receive loop could
                    # notice a new owner on (Codex F7): this loop reads the roster itself every
                    # ROSTER_POLL_S (memory only, owners_now) instead of sleeping to the ping.
                    wake_by = time.monotonic() + PING_S
                    while not stop.is_set():
                        left = wake_by - time.monotonic()
                        if left <= 0:
                            break
                        try:
                            await asyncio.wait_for(roster_changed.wait(), timeout=min(ROSTER_POLL_S, left))
                            break
                        except TimeoutError:
                            pass
                        if observe_roster(owners_now(), wake=False) != current:
                            break
                    roster_changed.clear()
                    pause = REROSTER_MIN_S - (time.monotonic() - requested_at)   # one re-request per 100 ms
                    if pause > 0:
                        await asyncio.sleep(pause)
            async with grpc.aio.secure_channel(target, grpc.ssl_channel_credentials(), options=[
                    ('grpc.max_receive_message_length', 4 * 1024 * 1024),
                    ('grpc.keepalive_time_ms', 10000)]) as channel:
                call = channel.stream_stream('/geyser.Geyser/Subscribe',
                    request_serializer=pb.SubscribeRequest.SerializeToString,
                    response_deserializer=pb.SubscribeUpdate.FromString)
                stream = call(requests(), metadata=(('x-token', token),))
                iterator = stream.__aiter__()
                while not stop.is_set():
                    observe_roster(owners_now())
                    before = time.monotonic()
                    if not ready and before >= ack_deadline:
                        raise TimeoutError
                    remaining = 10.0 if ready else ack_deadline - before
                    update = await asyncio.wait_for(iterator.__anext__(), timeout=remaining)
                    mono = time.monotonic()
                    owners = owners_now()
                    current = observe_roster(owners)
                    if not ready and time.monotonic() >= ack_deadline:
                        raise TimeoutError
                    status['last_frame_mono'] = mono
                    if update.HasField('pong'):
                        acknowledgement = ping_rosters.pop(update.pong.id, None)
                        if (acknowledgement == (current, roster_epoch)
                                and requested_epoch == roster_epoch
                                and current == status['requested_owners']):
                            if not ready and time.monotonic() >= ack_deadline:
                                raise TimeoutError
                            ready, backoff = True, 1.0
                            status.update(connected=True, acknowledged_owners=current, last_error=None)
                        continue
                    if not update.HasField('transaction'):
                        continue  # Rabbit cannot publish an authoritative head/account state
                    status['transactions'] += 1
                    if not ready or current != status['acknowledged_owners']:
                        status['before_ack'] += 1
                        status.update(connected=False, acknowledged_owners=())
                        ready = False
                        continue
                    candidate = preparation_candidate(update, owners, recv_monotonic=mono,
                                                       generation=generation, stats=status['miss_reasons'],
                                                       shapes=status['alt_frames'],
                                                       **({'on_owner_frame': on_owner_frame}
                                                          if on_owner_frame is not None else {}),
                                                       **({'on_owner_miss': on_owner_miss}
                                                          if on_owner_miss is not None else {}),
                                                       **({'on_rejected_candidate': on_rejected_candidate}
                                                          if on_rejected_candidate is not None else {}))
                    if candidate is None:
                        status['misses'] += 1
                        continue
                    if on_candidate is not None:
                        try:
                            on_candidate(candidate, update)
                        except Exception as exc:
                            status['callback_errors'] += 1
                            status['last_error'] = 'rabbit:' + type(exc).__name__
                    try:
                        on_prepare(candidate)
                        status['decoded'] += 1
                    except Exception as exc:
                        status['callback_errors'] += 1
                        status['last_error'] = 'prepare:' + type(exc).__name__
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            status['last_error'] = type(exc).__name__
            status['reconnects'] += 1
        finally:
            if stream is not None:
                stream.cancel()
            status.update(connected=False, acknowledged_owners=())
        # Close the old channel before reconnect; only one Rabbit connection ever lives.
        deadline = time.monotonic() + backoff
        while not stop.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(min(.1, max(0, deadline - time.monotonic())))
        backoff = min(backoff * 2, 30.0)
