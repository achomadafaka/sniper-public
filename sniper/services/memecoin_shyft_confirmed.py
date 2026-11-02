"""Confirmed Yellowstone receipts for the existing on-chain observation writer.

The confirmed wrapper is called only by the authenticated CONFIRMED subscription.
The neutral metadata parser also supplies processed preparation, without proof authority.
Unsupported token instructions fail closed and may use the watcher's bounded HTTP recovery.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import datetime, timezone
import json
import math
import queue
import re
import struct
import threading
import time
from urllib.parse import urlsplit

import base58

from sniper.feeds.yellowstone import geyser_pb2 as pb

TOKEN_PROGRAMS = {'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA',
                  'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'}
TOKEN_2022_PROGRAM = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'
SYSTEM = '11111111111111111111111111111111'
ATA = 'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL'


def subscription(owners):
    transactions = ({'owners': pb.SubscribeRequestFilterTransactions(
        vote=False, failed=False, account_include=sorted(owners))} if owners else {})
    return pb.SubscribeRequest(transactions=transactions,
        slots={'head': pb.SubscribeRequestFilterSlots(filter_by_commitment=True)},
        commitment=pb.CONFIRMED)


def _pubkey(raw):
    if len(raw) != 32:
        raise ValueError('pubkey_invalid')
    return base58.b58encode(raw).decode()


def _address(text):
    if _pubkey(base58.b58decode(text)) != text:
        raise ValueError('pubkey_invalid')
    return text


def receipt_signers(receipt):
    """Return ordered signers for raw or jsonParsed keys without changing the receipt."""
    try:
        transaction = receipt['transaction']
        message = transaction['message']
        keys, signatures = message['accountKeys'], transaction['signatures']
        if (not isinstance(keys, list) or not 1 <= len(keys) <= 256
                or not isinstance(signatures, list) or not 1 <= len(signatures) <= len(keys)):
            return None
        for signature in signatures:
            if not isinstance(signature, str):
                return None
            raw = base58.b58decode(signature)
            if len(raw) != 64 or base58.b58encode(raw).decode() != signature:
                return None
        header = message.get('header')
        if header is not None:
            count = header.get('numRequiredSignatures')
            if type(count) is not int or not 1 <= count <= len(keys):
                return None
        if all(isinstance(key, str) for key in keys):
            if header is None:
                return None
            addresses = [_address(key) for key in keys]
            signed = tuple(addresses[:count])
        elif all(isinstance(key, dict) for key in keys):
            addresses = [_address(key['pubkey']) for key in keys]
            if any(type(key.get('signer')) is not bool or
                   key.get('source', 'transaction') not in ('transaction', 'lookupTable')
                   for key in keys):
                return None
            signed = tuple(address for address, key in zip(addresses, keys) if key['signer'])
            if (not signed or signed != tuple(addresses[:len(signed)])
                    or any(key['signer'] and key.get('source') == 'lookupTable' for key in keys)
                    or header is not None and count != len(signed)):
                return None
        else:
            return None
        if len(set(addresses)) != len(addresses) or len(signatures) != len(signed):
            return None
        return signed
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def _instruction(ix, keys, *, inventory_only=False):
    if ix.program_id_index >= len(keys) or any(a >= len(keys) for a in ix.accounts):
        raise ValueError('instruction_index_invalid')
    program = keys[ix.program_id_index]
    accounts = [keys[a] for a in ix.accounts]
    data = ix.data
    result = dict(programId=program, accounts=accounts, data=base58.b58encode(data).decode())
    if hasattr(ix, 'stack_height') and ix.HasField('stack_height'):
        result['stackHeight'] = ix.stack_height
    kind, info = None, None
    if program in TOKEN_PROGRAMS:
        if not data:
            raise ValueError('token_instruction_missing')
        tag = data[0]
        if tag in (3, 12):
            checked = tag == 12
            if len(data) != (10 if checked else 9) or len(accounts) < (4 if checked else 3):
                raise ValueError('token_transfer_invalid')
            amount = str(struct.unpack_from('<Q', data, 1)[0])
            info = dict(source=accounts[0], destination=accounts[2 if checked else 1],
                        authority=accounts[3 if checked else 2])
            if checked:
                if data[9] > 18:
                    raise ValueError('token_decimals_invalid')
                info.update(mint=accounts[1], tokenAmount=dict(amount=amount, decimals=data[9]))
            else:
                info['amount'] = amount
            kind = 'transferChecked' if checked else 'transfer'
        elif tag in (1, 16, 18):
            if len(data) != (1 if tag == 1 else 33) or len(accounts) < (3 if tag == 1 else 2):
                raise ValueError('token_initialize_invalid')
            info = dict(account=accounts[0], mint=accounts[1],
                        owner=accounts[2] if tag == 1 else _pubkey(data[1:]))
            kind = {1: 'initializeAccount', 16: 'initializeAccount2', 18: 'initializeAccount3'}[tag]
        elif tag == 26 and len(data) >= 2 and data[1] == 1 and program == TOKEN_2022_PROGRAM:
            # Token-2022 TransferFeeExtension::TransferCheckedWithFee (Gamma 2026-10-14, PVPLAND via
            # Xsc9qvGR): [26, 1, amount u64, decimals u8, fee u64]; accounts [source, mint, destination,
            # authority]. Named as jsonParsed names it, so downstream valuation reads it as the RPC
            # receipt does. The source is debited `amount`; the destination gains amount - fee.
            if len(data) != 19 or len(accounts) < 4:
                raise ValueError('token_transfer_fee_invalid')
            amount, decimals, fee = struct.unpack_from('<QBQ', data, 2)
            if decimals > 18 or fee > amount:
                raise ValueError('token_transfer_fee_invalid')
            info = dict(source=accounts[0], mint=accounts[1], destination=accounts[2],
                        authority=accounts[3], tokenAmount=dict(amount=str(amount), decimals=decimals),
                        feeAmount=dict(amount=str(fee), decimals=decimals))
            kind = 'transferCheckedWithFee'
        elif tag == 26 and len(data) >= 2 and data[1] == 4 and program == TOKEN_2022_PROGRAM:
            # TransferFeeExtension::HarvestWithheldTokensToMint (DFlow v1 routes, 2026-10-14..16):
            # [26, 4]; accounts [mint, sources...]. Withheld fees move to the mint; no account's
            # amount changes, so there is no transfer leg (as SyncNative).
            if len(data) != 2 or not accounts:
                raise ValueError('token_harvest_invalid')
        elif tag == 6 and len(data) >= 2 and data[1] == 3:
            # SetAuthority, type CloseAccount (DFlow hands the owner's coin account's close authority
            # to its program; 4 armed J7 buys refused 2026-10-14..16): [6, 3, 0] or [6, 3, 1, key];
            # accounts [account, current authority]. No token moves. Every other type stays refused,
            # AccountOwner (2) above all: it changes who holds the coin.
            if (len(data) != (35 if data[2:3] == b'\x01' else 3 if data[2:3] == b'\x00' else -1)
                    or len(accounts) < 2):
                raise ValueError('token_set_authority_invalid')
        elif tag == 9:
            if len(data) != 1 or len(accounts) < 3:
                raise ValueError('token_close_invalid')
            kind, info = 'closeAccount', dict(account=accounts[0], destination=accounts[1], owner=accounts[2])
        elif tag in (17, 21, 22):
            # SyncNative and account setup/read instructions have no hidden transfer leg.
            if not accounts or (len(data) != 1 if tag != 21 else (len(data) - 1) % 2 != 0):
                raise ValueError('token_setup_invalid')
        # Creation setup is inventory evidence only; payment/route parsing remains strict.
        elif inventory_only and tag == 20:
            if (not accounts or len(data) != (67 if data[34:35] == b'\x01' else
                    35 if data[34:35] == b'\x00' else -1) or data[1] > 18):
                raise ValueError('token_mint_initialize_invalid')
            kind, info = 'inventoryMintSetup', dict(mint=accounts[0], decimals=data[1])
        elif inventory_only and tag == 7:
            if len(data) != 9 or len(accounts) < 3:
                raise ValueError('token_mint_to_invalid')
            kind, info = 'inventoryMintTo', dict(mint=accounts[0], account=accounts[1])
        elif inventory_only and tag == 6 and data[1:2] in (b'\x00', b'\x01'):
            if (len(data) != (35 if data[2:3] == b'\x01' else 3 if data[2:3] == b'\x00' else -1)
                    or len(accounts) < 2):
                raise ValueError('token_set_authority_invalid')
            kind, info = 'inventoryMintSetup', dict(mint=accounts[0])
        elif inventory_only and program == TOKEN_2022_PROGRAM and data[:2] == b'\x27\x00':
            if len(data) != 66 or not accounts:
                raise ValueError('token_metadata_pointer_invalid')
            kind, info = 'inventoryMintSetup', dict(mint=accounts[0])
        elif (inventory_only and program == TOKEN_2022_PROGRAM
                and data[:8] == bytes.fromhex('d2e11ea258b84d8d')):
            if len(accounts) < 4 or accounts[0] != accounts[2]:
                raise ValueError('token_metadata_initialize_invalid')
            cursor = 8
            for _ in range(3):
                if cursor + 4 > len(data):
                    raise ValueError('token_metadata_initialize_invalid')
                size = struct.unpack_from('<I', data, cursor)[0]
                cursor += 4
                if cursor + size > len(data):
                    raise ValueError('token_metadata_initialize_invalid')
                data[cursor:cursor + size].decode('utf-8')
                cursor += size
            if cursor != len(data):
                raise ValueError('token_metadata_initialize_invalid')
            kind, info = 'inventoryMintSetup', dict(mint=accounts[2])
        elif (inventory_only and program == TOKEN_2022_PROGRAM
                and data[:8] == bytes.fromhex('d7e4a6e45464567b')):
            if len(data) != 40 or len(accounts) < 2:
                raise ValueError('token_metadata_authority_invalid')
            kind, info = 'inventoryMintSetup', dict(mint=accounts[0])
        else:
            raise ValueError('token_instruction_unsupported:%d' % tag)
        if kind:
            result['program'] = 'spl-token' if program.endswith('V5DA') else 'spl-token-2022'
    elif program == SYSTEM and len(data) >= 4:
        tag = struct.unpack_from('<I', data)[0]
        if tag == 0:
            if len(data) != 52 or len(accounts) < 2:
                raise ValueError('system_create_invalid')
            lamports, space = struct.unpack_from('<QQ', data, 4)
            kind, info = 'createAccount', dict(source=accounts[0], newAccount=accounts[1],
                lamports=lamports, space=space, owner=_pubkey(data[20:]))
        elif tag == 2:
            if len(data) != 12 or len(accounts) < 2:
                raise ValueError('system_transfer_invalid')
            kind, info = 'transfer', dict(source=accounts[0], destination=accounts[1],
                                         lamports=struct.unpack_from('<Q', data, 4)[0])
        if kind:
            result['program'] = 'system'
    elif program == ATA and data in (b'', b'\x00', b'\x01'):
        if len(accounts) < 6:
            raise ValueError('ata_create_invalid')
        kind, info = ('createIdempotent' if data == b'\x01' else 'create'), dict(
            source=accounts[0], account=accounts[1], wallet=accounts[2], mint=accounts[3],
            systemProgram=accounts[4], tokenProgram=accounts[5])
        result['program'] = 'spl-associated-token-account'
    if kind:
        result['parsed'] = dict(type=kind, info=info)
    return result


def confirmed_transaction(update, owners, *, head_slot, recv_monotonic, head_monotonic):
    """Convert full CONFIRMED wire metadata to the existing receipt shape, or None.

    Geyser has no commitment field per transaction: authority comes from this
    module's CONFIRMED subscription. Never call this on the PROCESSED stream.
    Block time is absent from a transaction update and must stay unknown.
    """
    return transaction_metadata(update, owners, head_slot=head_slot,
        recv_monotonic=recv_monotonic, head_monotonic=head_monotonic)


def _refuse(why, reason):
    if why is not None:
        why.append(reason)
    return None


def _exception_reason(exc):
    """A bounded name: this module's own ValueError slug (with an unsupported token tag), else the type."""
    text = str(exc)
    if type(exc) is ValueError and re.fullmatch(r"[a-z0-9_]+(:[0-9]+)?", text):
        return text
    return type(exc).__name__


def transaction_metadata(update, owners, *, head_slot, recv_monotonic, head_monotonic,
                         allow_failed=False, why=None, inventory_only=False):
    """Validate complete wire metadata; the caller owns commitment and authority. `why`, a list,
    receives the refusal reason when this returns None (Gamma 2026-10-14: a refused processed
    update used to leave no trace). Default acceptance is unchanged. inventory_only is solely
    pre-balance evidence and must never supply signer/payment authority."""
    try:
        if (not owners or not update.HasField('transaction') or head_slot is None
                or head_monotonic is None or not 0 <= recv_monotonic - head_monotonic <= 2):
            return _refuse(why, 'receipt_input_unusable')
        slot, i = update.transaction.slot, update.transaction.transaction
        if (not slot or not head_slot - 2 <= slot <= head_slot + 2 or i.is_vote or not i.HasField('meta')
                or i.meta.HasField('err') and not allow_failed or not i.HasField('transaction')):
            return _refuse(why, 'slot_or_status_unusable')
        tx, meta = i.transaction, i.meta
        error = None
        if meta.HasField('err'):
            from solders.transaction_status import TransactionStatus
            raw = bytes(meta.err.err)
            if not 1 <= len(raw) <= 1024:
                return _refuse(why, 'error_bytes_invalid')
            # Use the installed Solana enum decoder; its status envelope holds two equal errors.
            encoded = b'\0' * 9 + b'\x01\0\0\0' + raw + b'\x01' + raw + b'\0'
            decoded = TransactionStatus.from_bytes(encoded)
            if bytes(decoded) != encoded:
                return _refuse(why, 'error_decode_mismatch')
            error = json.loads(decoded.to_json())['err']
            if error is None:
                return _refuse(why, 'error_null')
        if (meta.log_messages_none and meta.log_messages or error is None
                and (meta.log_messages_none or not meta.log_messages)):
            return _refuse(why, 'logs_missing')
        msg = tx.message
        n = msg.header.num_required_signatures
        if (not tx.HasField('message') or not msg.HasField('header')
                or not 1 <= n <= len(msg.account_keys) or len(tx.signatures) != n
                or len(i.signature) != 64 or tx.signatures[0] != i.signature
                or any(len(s) != 64 for s in tx.signatures)
                or msg.header.num_readonly_signed_accounts > n
                or msg.header.num_readonly_unsigned_accounts > len(msg.account_keys) - n):
            return _refuse(why, 'message_invalid')
        static = [_pubkey(k) for k in msg.account_keys]
        writable = [_pubkey(k) for k in meta.loaded_writable_addresses]
        readonly = [_pubkey(k) for k in meta.loaded_readonly_addresses]
        keys = static + writable + readonly
        lookups = msg.address_table_lookups
        if (not msg.versioned and (lookups or writable or readonly)
                or sum(len(x.writable_indexes) for x in lookups) != len(writable)
                or sum(len(x.readonly_indexes) for x in lookups) != len(readonly)):
            return _refuse(why, 'lookup_count_mismatch')
        for lookup in lookups:
            _pubkey(lookup.account_key)
            indexes = bytes(lookup.writable_indexes) + bytes(lookup.readonly_indexes)
            if len(set(indexes)) != len(indexes):
                return _refuse(why, 'lookup_index_duplicate')
        if (len(keys) > 256 or not set(static[:n]).intersection(owners) or len(set(keys)) != len(keys)
                or len(meta.pre_balances) != len(keys) or len(meta.post_balances) != len(keys)):
            return _refuse(why, 'keys_or_balances_invalid')
        from sniper.services.memecoin_source_valuation import ROUTE_QUOTE_DECIMALS
        identities, mint_decimals = {}, {}
        def balances(rows):
            out, seen = [], set()
            for b in rows:
                if (b.account_index >= len(keys) or b.account_index in seen or not b.HasField('ui_token_amount')
                        or b.program_id not in TOKEN_PROGRAMS or b.ui_token_amount.decimals > 18):
                    raise ValueError('token_balance_invalid')
                seen.add(b.account_index)
                amount = b.ui_token_amount.amount
                if (not amount or len(amount) > 20 or not amount.isascii() or not amount.isdigit()
                        or int(amount) >= 2**64):
                    raise ValueError('token_amount_invalid')
                decimals = b.ui_token_amount.decimals
                identity = (b.owner, b.mint, b.program_id, decimals)
                if (identities.get(b.account_index, identity) != identity
                        or mint_decimals.get(b.mint, decimals) != decimals
                        or ROUTE_QUOTE_DECIMALS.get(b.mint, decimals) != decimals):
                    raise ValueError('token_identity_mismatch')
                identities[b.account_index] = identity
                mint_decimals[b.mint] = decimals
                out.append(dict(accountIndex=b.account_index, owner=_address(b.owner), mint=_address(b.mint),
                    programId=b.program_id, uiTokenAmount=dict(amount=amount, decimals=decimals,
                                                             uiAmount=int(amount) / 10**decimals)))
            return out
        top = [_instruction(ix, keys, inventory_only=inventory_only) for ix in msg.instructions]
        inner, seen = [], set()
        if meta.inner_instructions_none and meta.inner_instructions:
            return _refuse(why, 'inner_instructions_conflict')
        for g in meta.inner_instructions:
            if (g.index >= len(top) or g.index in seen or any(
                    not ix.HasField('stack_height') or not 2 <= ix.stack_height <= 64
                    for ix in g.instructions)):
                return _refuse(why, 'inner_instructions_invalid')
            seen.add(g.index)
            inner.append(dict(index=g.index, instructions=[_instruction(ix, keys,
                inventory_only=inventory_only) for ix in g.instructions]))
        pre, post = balances(meta.pre_token_balances), balances(meta.post_token_balances)
        result = dict(slot=slot, blockTime=None,
            transaction=dict(signatures=[base58.b58encode(s).decode() for s in tx.signatures],
                message=dict(accountKeys=static, header=dict(numRequiredSignatures=n), instructions=top)),
            meta=dict(err=error, fee=meta.fee, preBalances=list(meta.pre_balances),
                postBalances=list(meta.post_balances), logMessages=list(meta.log_messages),
                loadedAddresses=dict(writable=writable, readonly=readonly), innerInstructions=inner,
                preTokenBalances=pre, postTokenBalances=post))
        if error is not None:
            return result  # Failed instructions are intents, not executed inventory or payment proof.
        instructions = top + [ix for group in inner for ix in group['instructions']]
        initialized, closed = set(), set()
        for ix in instructions:
            parsed = ix.get('parsed') or {}
            info = parsed.get('info') or {}
            if ix['programId'] not in TOKEN_PROGRAMS:
                continue
            if parsed.get('type') in ('initializeAccount', 'initializeAccount2', 'initializeAccount3'):
                initialized.add((info['account'], info['mint'], info['owner'], ix['programId']))
            elif parsed.get('type') == 'closeAccount':
                closed.add((info['account'], ix['programId']))
        pre_indexes, post_indexes = {b['accountIndex'] for b in pre}, {b['accountIndex'] for b in post}
        for b in post:
            index = b['accountIndex']
            if index not in pre_indexes and (meta.pre_balances[index] != 0
                    or (keys[index], b['mint'], b['owner'], b['programId']) not in initialized):
                return _refuse(why, 'post_only_row_unbound')  # Missing existing inventory is unknown; idempotent ATA create is insufficient.
        for b in pre:
            index = b['accountIndex']
            if index not in post_indexes and (meta.post_balances[index] != 0
                    or (keys[index], b['programId']) not in closed):
                return _refuse(why, 'pre_only_row_unclosed')  # Closure plus no final account is required to infer final zero.
        bindings = {keys[index]: identity for index, identity in identities.items()}
        key_indexes = {key: index for index, key in enumerate(keys)}
        temporary = {}
        for account, mint, owner, program in initialized:
            index = key_indexes[account]
            if (account not in bindings and meta.pre_balances[index] == meta.post_balances[index] == 0
                    and (account, program) in closed):
                identity = (owner, mint, program, mint_decimals.get(mint))
                if temporary.get(account, identity) != identity:
                    return _refuse(why, 'temporary_identity_conflict')
                temporary[account] = identity
        for ix in instructions:
            parsed = ix.get('parsed') or {}
            if parsed.get('type') == 'inventoryMintSetup':
                info = parsed['info']
                if (info['mint'] in bindings or info['mint'] in temporary
                        or ('decimals' in info and info['decimals'] != mint_decimals.get(
                            info['mint'], info['decimals']))):
                    return _refuse(why, 'mint_setup_target_invalid')
            elif parsed.get('type') == 'inventoryMintTo':
                info = parsed['info']
                target = bindings.get(info['account'], temporary.get(info['account']))
                if target is None or target[1:3] != (info['mint'], ix['programId']):
                    return _refuse(why, 'mint_to_endpoint_unbound')
            if (parsed.get('type') in ('transfer', 'transferChecked', 'transferCheckedWithFee')
                    and ix['programId'] in TOKEN_PROGRAMS):
                info = parsed['info']
                endpoints = [bindings.get(info[name], temporary.get(info[name]))
                             for name in ('source', 'destination')]
                if (any(b is None or b[2] != ix['programId'] for b in endpoints)
                        or endpoints[0][1] != endpoints[1][1]):
                    return _refuse(why, 'transfer_endpoint_unbound')  # Missing both balance rows must not hide existing wallet inventory.
                if parsed['type'] == 'transfer':
                    continue
                decimals = info['tokenAmount']['decimals']
                if (any(b[1] != info['mint'] or b[3] not in (None, decimals) for b in endpoints)
                        or mint_decimals.get(info['mint'], decimals) != decimals
                        or ROUTE_QUOTE_DECIMALS.get(info['mint'], decimals) != decimals):
                    return _refuse(why, 'transfer_checked_mismatch')
        return result
    except (ValueError, TypeError, IndexError, OverflowError) as exc:
        return _refuse(why, _exception_reason(exc))


class ReceiptWorker:
    """Bounded decode residence; the existing writer remains SQLite's sole owner."""
    MAX_RESIDENCE_S = 2.0

    def __init__(self, consume, *, maxsize=256, decoder=confirmed_transaction, clock=None):
        self.consume = consume
        self.decoder = decoder
        self.clock = time.monotonic if clock is None else clock
        self._jobs, self._stop = queue.Queue(maxsize=maxsize), threading.Event()
        self.stats = dict(offered=0, overflow=0, decoded=0, invalid=0, duplicates=0, errors=0,
                          queue_expired=0, decode_expired=0, release_errors=0,
                          shutdown_dropped=0, shutdown_refused=0, last_error=None)
        self._seen = OrderedDict()
        self._accepted, self._pending, self._seen_lock = {}, {}, threading.Lock()
        self._pending_cap = maxsize
        self._thread = threading.Thread(target=self._run, name='shyft-confirmed-decode', daemon=True)
        self._thread.start()

    def offer(self, update, owners, **receipt):
        if self._stop.is_set():
            self.stats['shutdown_refused'] += 1
            self._release(receipt)
            return False
        try:
            offered_mono = self.clock()
            self._jobs.put_nowait((update, {owner: dict(info) for owner, info in owners.items()},
                                  receipt, offered_mono))
        except queue.Full:
            self.stats['overflow'] += 1
            self._release(receipt)
            return False
        except Exception:
            self._release(receipt)
            raise
        self.stats['offered'] += 1
        if self._stop.is_set():
            # close may have drained before this nonblocking offer finished copying the roster.
            self._discard_queued()
            return False
        return True

    def _release(self, receipt):
        # Optional raw-frame credit, independent of durable proof acceptance. Shyft has none.
        callback = receipt.get('on_released')
        if callback is not None:
            try:
                callback()
            except Exception:
                self.stats['release_errors'] += 1

    def _expired(self, offered_mono, phase):
        # This clock bounds residence only. Never substitute it for the socket's receipt clock.
        age = self.clock() - offered_mono
        if not math.isfinite(age) or not 0 <= age <= self.MAX_RESIDENCE_S:
            self.stats[phase + '_expired'] += 1
            self.stats['last_error'] = 'receipt_' + phase + '_expired'
            return True
        return False

    def _run(self):
        while not self._stop.is_set():
            try:
                update, owners, r, offered_mono = self._jobs.get(timeout=.1)
            except queue.Empty:
                continue
            try:
                if self._stop.is_set():
                    self.stats['shutdown_dropped'] += 1
                    continue
                if self._expired(offered_mono, 'queue'):
                    continue
                tx = self.decoder(update, owners, head_slot=r['head_slot'],
                    head_monotonic=r['head_monotonic'], recv_monotonic=r['recv_monotonic'])
                if self._stop.is_set():
                    self.stats['shutdown_dropped'] += 1
                    continue
                if tx is None:
                    self.stats['invalid'] += 1
                    self.stats['last_error'] = 'receipt_invalid_or_stale'
                    continue
                signed = receipt_signers(tx)
                if signed is None or not set(signed).intersection(owners):
                    self.stats['invalid'] += 1
                    self.stats['last_error'] = 'receipt_signers_invalid'
                    continue
                sig = tx['transaction']['signatures'][0]
                for owner in signed:
                    if owner not in owners:
                        continue
                    if self._expired(offered_mono, 'decode'):
                        break  # Cannot preempt a decoder, but a late result gains no proof authority.
                    info = owners[owner]
                    uid = info['uid']
                    key = (sig, tx['slot'], owner, uid)
                    token = object()
                    with self._seen_lock:
                        if key in self._seen or key in self._pending:
                            self.stats['duplicates'] += 1
                            continue
                        if len(self._pending) >= self._pending_cap:
                            self.stats['overflow'] += 1
                            continue
                        self._pending[key] = token
                    def completed(durable, key=key, token=token):
                        with self._seen_lock:
                            if self._pending.get(key) is not token:
                                return
                            del self._pending[key]
                            if durable is not True:
                                return
                            self._seen[key] = None
                            self._accepted[(key[0], key[2])] = key[3]
                            if len(self._seen) > 5000:
                                old, _ = self._seen.popitem(last=False)
                                if self._accepted.get((old[0], old[2])) == old[3]:
                                    self._accepted.pop((old[0], old[2]), None)
                            self.stats['decoded'] += 1
                    try:
                        result = self.consume(tx, owner, info, dict(r, on_persisted=completed))
                    except Exception:
                        completed(False)
                        raise
                    # None is asynchronous; only the writer's durable callback can accept it.
                    if result is not None:
                        completed(result is True)
            except Exception as exc:
                self.stats['errors'] += 1
                self.stats['last_error'] = type(exc).__name__
            finally:
                self._release(r)
                self._jobs.task_done()

    def close(self):
        self._stop.set()
        self._thread.join(.5)
        with self._seen_lock:
            self._pending.clear()
        self._discard_queued()

    def _discard_queued(self):
        while True:
            try:
                _update, _owners, receipt, _offered_mono = self._jobs.get_nowait()
                self._release(receipt)
                self._jobs.task_done()
                self.stats['shutdown_dropped'] += 1
            except queue.Empty:
                break

    def has_receipt(self, signature, owner, source_user_id=None):
        # Watcher only: async queue acceptance never suppresses a receipt read.
        with self._seen_lock:
            key = (signature, owner)
            return key in self._accepted and (source_user_id is None
                or self._accepted[key] == source_user_id)


async def run_feed(url, token, owners_fn, worker, status, stop):
    """Persistent filtered CONFIRMED socket; no HTTP/SQLite work on this receiver."""
    import grpc
    backoff = 1.0
    status.update(connected=False, reconnects=0, owners=0, transactions=0,
                  last_error=None, last_frame_monotonic=None, head_slot=None,
                  head_monotonic=None, requested_owners=(), acknowledged_owners=(),
                  coverage_gaps=0, last_gap=None)
    latest_head = None
    while not stop.is_set():
        stream, ready = None, False
        ping_rosters = {}
        try:
            deadline = time.monotonic() + 10
            async def requests():
                nonlocal ready, deadline
                last, ping = None, 0
                while not stop.is_set():
                    owners = dict(owners_fn())
                    current = frozenset(owners)
                    if current != last:
                        ready = False
                        deadline = time.monotonic() + 10
                        status.update(owners=len(owners), requested_owners=tuple(sorted(current)),
                                      acknowledged_owners=(), connected=False)
                        yield subscription(owners)
                        last = current
                    ping += 1
                    ping_rosters[ping] = status['requested_owners']
                    if len(ping_rosters) > 2:
                        del ping_rosters[min(ping_rosters)]
                    yield pb.SubscribeRequest(ping=pb.SubscribeRequestPing(id=ping))
                    await asyncio.sleep(5)
            async with grpc.aio.secure_channel(urlsplit(url).hostname + ':443', grpc.ssl_channel_credentials(),
                    options=[('grpc.max_receive_message_length', 4 * 1024 * 1024),
                             ('grpc.keepalive_time_ms', 10000)]) as channel:
                call = channel.stream_stream('/geyser.Geyser/Subscribe',
                    request_serializer=pb.SubscribeRequest.SerializeToString,
                    response_deserializer=pb.SubscribeUpdate.FromString)
                stream = call(requests(), metadata=(('x-token', token),))
                iterator = stream.__aiter__()
                head, head_mono, highest_head, dead = None, None, None, set()
                status.update(head_slot=None, head_monotonic=None)
                while not stop.is_set():
                    update = await asyncio.wait_for(iterator.__anext__(),
                        timeout=10 if ready else max(.001, deadline - time.monotonic()))
                    mono = time.monotonic()
                    if not ready and mono >= deadline:
                        raise TimeoutError
                    status['last_frame_monotonic'] = mono
                    if update.HasField('pong'):
                        roster = ping_rosters.pop(update.pong.id, None)
                        if roster is not None and roster == status['requested_owners']:
                            ready = True
                            status.update(connected=True, last_error=None, acknowledged_owners=roster)
                            backoff = 1
                    elif update.HasField('slot'):
                        if update.slot.status == pb.SLOT_DEAD:
                            dead.add(update.slot.slot)
                            if len(dead) > 32:
                                dead.remove(min(dead))
                            if head == update.slot.slot:
                                head = head_mono = None
                                status.update(head_slot=None, head_monotonic=None)
                        elif update.slot.status in (pb.SLOT_CONFIRMED, pb.SLOT_FINALIZED):
                            slot = update.slot.slot
                            if slot in dead or slot == 0:
                                continue
                            if highest_head is None or slot > highest_head:
                                if head is None and latest_head is not None and slot - latest_head > 150:
                                    status['coverage_gaps'] += 1
                                    status['last_gap'] = dict(reason='replay_depth_exceeded',
                                        after_slot=latest_head, before_slot=slot)
                                head = highest_head = slot
                                latest_head = max(latest_head or 0, head)
                                head_mono = mono
                                status.update(head_slot=head, head_monotonic=head_mono)
                                dead = {s for s in dead if s >= head - 2}
                    elif ready and update.HasField('transaction'):
                        status['transactions'] += 1
                        if (update.transaction.slot in dead or head is None or head_mono is None
                                or not 0 <= mono - head_mono <= 2.0
                                or not head - 2 <= update.transaction.slot <= head + 2):
                            continue
                        owners = dict(owners_fn())
                        if tuple(sorted(owners)) != status['acknowledged_owners']:
                            ready, deadline = False, mono + 10
                            status.update(connected=False, acknowledged_owners=())
                            continue
                        worker.offer(update, owners, head_slot=head, head_monotonic=head_mono,
                            recv_monotonic=mono, recv_utc=datetime.now(timezone.utc).isoformat(),
                            recv_epoch=time.time(), signal_feed='shyft_confirmed:' + urlsplit(url).hostname)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            status['last_error'] = 'stream_silent' if ready else 'subscription_ack_timeout'
        except Exception as exc:
            status['last_error'] = exc.code().name if isinstance(exc, grpc.RpcError) else type(exc).__name__
        finally:
            if stream is not None:
                stream.cancel()
            status.update(connected=False, acknowledged_owners=())
        status['reconnects'] += 1
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)
