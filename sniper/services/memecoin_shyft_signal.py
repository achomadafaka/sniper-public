"""Authenticated Shyft processed logs for the existing native Pump slot lane.

No RPC reads, signing, DB access or new trade authority. Routed balance-pair
candidates use the existing pairer; confirmed proof retains the watcher-owned
receipt path. All callbacks run on the
slot-signal event loop, so cross-feed dedup needs no lock.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from urllib.parse import urlsplit

from sniper.feeds.yellowstone import geyser_pb2 as pb

DEFAULT_URLS = 'https://beta-grpc.ams.shyft.to,https://beta-grpc.fra.shyft.to'


def live_config(env=None):
    env = os.environ if env is None else env
    from sniper.services.memecoin_solana_provider import infra_provider
    profile = infra_provider(env)
    enabled = env.get('MEMECOIN_SHYFT_LIVE_ENABLED', '0')
    if enabled == '0':
        if profile == 'shyft':
            raise ValueError('shyft live_required')
        return None
    if enabled != '1':
        raise ValueError('shyft enabled flag must be 0 or 1')
    token = env.get('MEMECOIN_SHYFT_GRPC_TOKEN', '').strip()
    if not token:
        raise ValueError('shyft token_missing')
    default = '' if profile == 'shyft' else DEFAULT_URLS
    urls = tuple(x.strip() for x in env.get('MEMECOIN_SHYFT_GRPC_URLS', default).split(','))
    if not 1 <= len(urls) <= (2 if profile == 'shyft' else 3) or len(set(urls)) != len(urls):
        raise ValueError('shyft endpoint_invalid')
    for url in urls:
        try:
            u = urlsplit(url)
            if (u.scheme != 'https' or not u.hostname or not u.hostname.endswith('.shyft.to')
                    or u.username is not None or u.password is not None
                    or u.path not in ('', '/') or u.query or u.fragment or u.port not in (None, 443)
                    or not u.hostname.isascii() or any(c.isspace() for c in url)):
                raise ValueError
            if profile == 'shyft' and u.hostname not in {'grpc.ams.shyft.to', 'grpc.fra.shyft.to'}:
                raise ValueError
        except ValueError:
            raise ValueError('shyft endpoint_invalid') from None
    if len({urlsplit(url).hostname for url in urls}) != len(urls):
        raise ValueError('shyft endpoint_invalid')
    return token, urls


def subscription(owners, *, migration=False, migration_label='migrations', damm=False, damm_label='damm'):
    # An empty account_include means ALL transactions. Empty roster means no tx filter.
    transactions = ({'owners': pb.SubscribeRequestFilterTransactions(
        vote=False, failed=False, account_include=sorted(owners))} if owners else {})
    if migration:
        from sniper.services.memecoin_slot_signal import PUMP_PROGRAM, PUMP_AMM_PROGRAM, PUMP_MIGRATION_ACCOUNT
        transactions[migration_label] = pb.SubscribeRequestFilterTransactions(vote=False, failed=False,
            account_include=(PUMP_PROGRAM, PUMP_AMM_PROGRAM), account_required=(PUMP_MIGRATION_ACCOUNT,))
    if damm:
        from sniper.execution.meteora_damm_v2_buy import PROGRAM
        transactions[damm_label] = pb.SubscribeRequestFilterTransactions(
            vote=False, failed=False, account_include=(PROGRAM,))
    return pb.SubscribeRequest(transactions=transactions,
        slots={'head': pb.SubscribeRequestFilterSlots(filter_by_commitment=True)},
        commitment=pb.CommitmentLevel.PROCESSED)


def migration_pool(update):
    """A successful canonical migration is a bounded read target, never pool/source proof."""
    import base58
    from sniper.execution.pumpswap_buy import create_pool_event
    from sniper.services.memecoin_slot_signal import PUMP_PROGRAM, PUMP_AMM_PROGRAM, PUMP_MIGRATION_ACCOUNT
    from sniper.services.memecoin_opportunity_live import program_data_logs
    if not update.HasField('transaction'):
        return None
    info = update.transaction.transaction
    if (not update.transaction.slot or not info.HasField('transaction') or not info.HasField('meta')
            or info.meta.HasField('err') or info.is_vote or info.meta.log_messages_none
            or len(info.signature) != 64):
        return None
    tx, msg = info.transaction, info.transaction.message
    logs = info.meta.log_messages
    if len(logs) > 256 or sum(len(line) for line in logs) > 65536:
        return None
    keys = tuple(msg.account_keys) + tuple(info.meta.loaded_writable_addresses) + tuple(info.meta.loaded_readonly_addresses)
    required = {base58.b58decode(key) for key in (PUMP_PROGRAM, PUMP_AMM_PROGRAM, PUMP_MIGRATION_ACCOUNT)}
    if (len(keys) > 256 or not msg.HasField('header')
            or not 0 < msg.header.num_required_signatures <= len(msg.account_keys)
            or len(tx.signatures) != msg.header.num_required_signatures
            or tx.signatures[0] != info.signature or any(len(sig) != 64 for sig in tx.signatures)
            or any(len(key) != 32 for key in keys) or not required.issubset(keys)):
        return None
    event = create_pool_event(line for _, line in program_data_logs(logs, PUMP_AMM_PROGRAM))
    return ((base58.b58encode(info.signature).decode(), event['pool'])
            if event and event['pump_migrated'] else None)



def damm_creation(update):
    """Bounded program-log recognition; the off-path cache verifies the actual pool."""
    import base58
    from sniper.execution.meteora_damm_v2_buy import PROGRAM, creation_signature
    if not update.HasField('transaction'):
        return None
    info = update.transaction.transaction
    if (not update.transaction.slot or not info.HasField('meta') or not info.HasField('transaction')
            or info.meta.HasField('err') or info.is_vote or info.meta.log_messages_none
            or len(info.signature) != 64):
        return None
    logs = info.meta.log_messages
    keys = tuple(info.transaction.message.account_keys) + tuple(info.meta.loaded_writable_addresses) + tuple(info.meta.loaded_readonly_addresses)
    if (len(logs) > 256 or sum(len(line) for line in logs) > 65536 or len(keys) > 256
            or base58.b58decode(PROGRAM) not in keys):
        return None
    return creation_signature({'signature': base58.b58encode(info.signature).decode(),
                               'err': None, 'logs': logs})

def _signed_log_frames(update, owners, *, recv_monotonic, head_slot, head_mono):
    if (not update.HasField('transaction') or head_slot is None or head_mono is None
            or not 0 <= recv_monotonic - head_mono <= 2.0):
        return ()
    slot, info = update.transaction.slot, update.transaction.transaction
    if (slot == 0 or not head_slot - 2 <= slot <= head_slot + 2 or info.is_vote or not info.HasField('meta')
            or info.meta.HasField('err') or info.meta.log_messages_none or not info.meta.log_messages
            or len(info.signature) != 64 or not info.HasField('transaction')):
        return ()
    tx = info.transaction
    msg = tx.message
    n = msg.header.num_required_signatures
    keys = tuple(msg.account_keys) + tuple(info.meta.loaded_writable_addresses) + tuple(info.meta.loaded_readonly_addresses)
    if (not msg.HasField('header') or not 1 <= n <= len(msg.account_keys)
            or len(tx.signatures) != n or tx.signatures[0] != info.signature
            or any(len(s) != 64 for s in tx.signatures) or any(len(k) != 32 for k in keys)):
        return ()
    import base58
    frame = {'params': {'result': {'context': {'slot': slot}, 'value': {
        'signature': base58.b58encode(info.signature).decode(), 'err': None,
        'logs': list(info.meta.log_messages)}}}}
    frames = []
    for raw in msg.account_keys[:n]:
        owner = base58.b58encode(raw).decode()
        if owner in owners:
            frames.append((frame, owner, raw, *owners[owner]))
    return frames


def _source_pre_atomic(tx, buy):
    """Validated transaction-touched owner/mint pre tokens; absent rows stay unknown.

    transaction_metadata binds row identities and permits post-only rows only for an initialized
    account with zero prior lamports. This never enumerates accounts outside the transaction.
    """
    meta = tx['meta']
    pre, post = meta['preTokenBalances'], meta['postTokenBalances']
    mint = buy.mint
    if mint is None:
        # PumpSwap logs name the base account, not the mint. Bind that account to the source owner
        # before using receipt rows; its event actor alone does not prove account ownership.
        from sniper.services.memecoin_slot_signal import _amm_accounts
        base_account = _amm_accounts(buy.trade_event_log)[1]
        keys = (tx['transaction']['message']['accountKeys'] + meta['loadedAddresses']['writable']
                + meta['loadedAddresses']['readonly'])
        mint = next((row['mint'] for row in pre + post
                     if row['owner'] == buy.owner and keys[row['accountIndex']] == base_account), None)
    if mint is None or not any(row['owner'] == buy.owner and row['mint'] == mint for row in pre + post):
        return None
    amount = sum(int(row['uiTokenAmount']['amount']) for row in pre
                 if row['owner'] == buy.owner and row['mint'] == mint)
    # A swap event is later than transaction-start metadata. Never erase its known positive
    # participating-account pre balance with a zero from before earlier instructions executed.
    return buy.source_pre_atomic if amount == 0 and buy.source_pre_atomic else amount


def decode_update(update, owners, *, recv_monotonic, recv_utc, head_slot, head_mono, receipt_evidence=None, signal_feed='helius'):
    from dataclasses import replace
    from sniper.services.memecoin_slot_signal import decode_owner_buy
    buys, metadata_read, tx = [], False, None
    for frame, owner, raw, uid, handle in _signed_log_frames(update, owners,
            recv_monotonic=recv_monotonic, head_slot=head_slot, head_mono=head_mono):
        buy = decode_owner_buy(frame, owner, raw, uid, handle, recv_monotonic=recv_monotonic,
                               recv_utc=recv_utc, head_at_recv=head_slot, head_at_recv_mono=head_mono,
                               receipt_evidence=receipt_evidence, signal_feed=signal_feed)
        if buy is not None:
            if not metadata_read:
                from sniper.services.memecoin_shyft_confirmed import transaction_metadata
                tx = transaction_metadata(update, owners, head_slot=head_slot,
                    recv_monotonic=recv_monotonic, head_monotonic=head_mono, inventory_only=True)
                metadata_read = True
            if tx is not None:
                buy = replace(buy, source_pre_atomic=_source_pre_atomic(tx, buy))
            buys.append(buy)
    return tuple(buys)


def decode_sells(update, owners, *, recv_monotonic, recv_utc, head_slot, head_mono):
    from sniper.services.memecoin_slot_signal import decode_owner_sells
    sells = []
    for frame, owner, raw, uid, handle in _signed_log_frames(update, owners,
            recv_monotonic=recv_monotonic, head_slot=head_slot, head_mono=head_mono):
        sells.extend(decode_owner_sells(frame, owner, raw, uid, handle,
                     recv_monotonic=recv_monotonic, recv_utc=recv_utc))
    return sells


def _note_metadata_refusal(update, owners, why):
    """A tracked signer's successful processed transaction the validator refused (Gamma 2026-10-14:
    PVPLAND left no trace). Local fields only; one deque append; never raises."""
    import base58
    from sniper.services.memecoin_slot_skips import note
    try:
        info = update.transaction.transaction
        message = info.transaction.message
        signers = {base58.b58encode(bytes(k)).decode()
                   for k in message.account_keys[:message.header.num_required_signatures]}
        if (len(info.signature) != 64 or not why or not signers.intersection(owners)
                or (info.HasField('meta') and info.meta.HasField('err'))):
            return
        note(base58.b58encode(bytes(info.signature)).decode(), 'metadata_invalid', why[0])
    except Exception:  # noqa: BLE001 - a fill-timing hint must never break the decode thread
        logging.getLogger(__name__).debug('metadata refusal note failed', exc_info=True)


def routed_buys(update, owners, *, recv_monotonic, recv_utc, head_slot, head_mono,
                signal_feed='helius', receipt_evidence=None, native_buys=None, why=None):
    """Processed USDC pair candidates for unsigned preparation; never initiation proof. `why`, a list,
    receives transaction_metadata's refusal reason when the update is refused."""
    from sniper.services.memecoin_shyft_confirmed import transaction_metadata
    from sniper.services.memecoin_slot_signal import RoutedPairer, ROUTED_PROGRAMS, routed_venue_events
    refused_why = []
    tx = transaction_metadata(update, owners, head_slot=head_slot,
        recv_monotonic=recv_monotonic, head_monotonic=head_mono, why=refused_why)
    if tx is None:
        _note_metadata_refusal(update, owners, refused_why)
        if why is not None:
            why.extend(refused_why)
        return ()
    # Direct Pump authority already owns these signature/owner keys.
    direct = native_buys
    if direct is None:
        direct = decode_update(update, owners, recv_monotonic=recv_monotonic,
            recv_utc=recv_utc, head_slot=head_slot, head_mono=head_mono)
    native = {b.owner for b in direct if not b.refusal}
    refused = {b.owner for b in direct if b.refusal}
    msg, meta = tx['transaction']['message'], tx['meta']
    keys = msg['accountKeys'] + meta['loadedAddresses']['writable'] + meta['loadedAddresses']['readonly']
    sig, slot = tx['transaction']['signatures'][0], tx['slot']
    from sniper.execution import raydium_cpmm_buy, raydium_launchlab_buy, meteora_damm_v2_buy, meteora_dbc_buy
    from sniper.settings import memecoin as cfg
    frame = {'params': {'result': {'value': {'logs': meta['logMessages']}}}}
    events = routed_venue_events(frame, cpmm_on=raydium_cpmm_buy.lane_mode() != 'off',
        launchlab_on=raydium_launchlab_buy.lane_mode() != 'off',
        pump_on=bool(cfg.MEMECOIN_SLOT_LANE_ROUTED_PUMP),
        damm_on=meteora_damm_v2_buy.lane_mode() != 'off', dbc_on=meteora_dbc_buy.lane_mode() != 'off')
    buys, expired = [], set()
    for owner in msg['accountKeys'][:msg['header']['numRequiredSignatures']]:
        if owner not in owners or owner in native:
            continue
        uid, handle = owners[owner]
        pairer = RoutedPairer()
        pairer.watch(owner, uid, handle)
        pre = {b['accountIndex']: b for b in meta['preTokenBalances'] if b['owner'] == owner}
        post = {b['accountIndex']: b for b in meta['postTokenBalances'] if b['owner'] == owner}
        for kind, program in ROUTED_PROGRAMS:
            pairer.set_baseline(owner, kind, slot - 1, {
                keys[index]: int(b['uiTokenAmount']['amount'])
                for index, b in pre.items() if b['programId'] == program})
        # Logs last prevents emission before all account deltas are known.
        for index in sorted(pre.keys() | post.keys()):
            b = post.get(index, pre.get(index))
            kind = next(k for k, p in ROUTED_PROGRAMS if p == b['programId'])
            pairer.on_token(owner, kind, slot, keys[index], b['mint'],
                int(post[index]['uiTokenAmount']['amount']) if index in post else 0,
                b['uiTokenAmount']['decimals'], recv_monotonic=recv_monotonic, recv_utc=recv_utc)
        emitted = pairer.on_logs(owner, slot, sig, lane=False, recv_monotonic=recv_monotonic,
            recv_utc=recv_utc, venue_events=events, requires_receipt_proof=owner in refused)
        buys.extend(emitted)
        if not emitted and sig not in expired:
            # The legacy socket's expire() named every pair that moved and never paired (fill timing,
            # Gamma 2026-10-14: none since the Shyft cutover). One deque append per signature.
            expired.add(sig)
            pairer.expire(math.inf)
    from dataclasses import replace
    result = []
    for buy in buys:
        fields = dict(paired_head_slot=head_slot, signal_feed=signal_feed, receipt_evidence=receipt_evidence,
                      source_pre_atomic=_source_pre_atomic(tx, buy))
        if any(isinstance(event, meteora_damm_v2_buy.DammV2Seen) for event in buy.venue_events):
            hint = meteora_damm_v2_buy.pool_read_hint(tx, buy.mint, buy.owner)
            if hint is not None:
                fields['damm_read_hint'] = hint
        if cfg.MEMECOIN_SLOT_LANE_ROUTED_PROCESSED_PROOF:
            fields['source_initiation'] = _processed_proof(tx, buy)
        result.append(replace(buy, **fields))
    return tuple(result)


def _processed_proof(tx, buy):
    """The confirmed watcher's own proof builder on this processed transaction; None when it cannot
    bind signer, payment and acquisition. Local CPU only, no provider call."""
    from sniper.services.memecoin_source_valuation import value_source_buy
    from sniper.services.memecoin_source_initiation import solana_receipt_initiation
    try:
        facts = value_source_buy(tx, buy.owner, sol_usd=None, now=time.time())
        proof = solana_receipt_initiation(tx, buy.owner, facts)
    except Exception:  # noqa: BLE001 - no proof keeps the confirmed-only path, never a crash
        logging.getLogger(__name__).debug('processed routed proof failed sig=%s', buy.signature,
                                          exc_info=True)
        return None
    if proof is None or proof.get('mint') != buy.mint:
        return None
    return dict(proof, source_user_id=buy.source_user_id)


def _publish_empty_receipt(update, measurement, *, feed, generation, mono, evidence, signature=None):
    if measurement is None:
        return
    from sniper.services.memecoin_slot_signal import _unknown_receipt, _coverage_gap
    try:
        if signature is None:
            import base58
            raw = bytes(update.transaction.transaction.signature)
            signature = base58.b58encode(raw).decode() if len(raw) == 64 else None
        _unknown_receipt(measurement, feed=feed, generation=generation, mono=mono,
            state='empty_decode_unknown', evidence=evidence, signature=signature)
    except Exception:
        _coverage_gap(measurement, feed, 'receipt_publication_failed', generation, mono, mono, 1)


def _dropped_frame(update, owners):
    """The dropped frame's signature and its tracked signer (None when no tracked owner signs),
    for the gap row: a later read matches drops to buys. Never raises."""
    try:
        from sniper.services.memecoin_shyft_prepare import owner_signer
        import base58
        signature = update.transaction.transaction.signature
        signed = owner_signer(update, owners or {})
        return dict(source_sig=base58.b58encode(signature).decode() if len(signature) == 64 else None,
                    owner=signed[1] if signed else None)
    except Exception:
        return {}

REROSTER_MIN_S = 0.1          # a flapping roster re-requests at most every 100 ms
PING_S = 5.0                  # the request loop pings this often while the roster holds
from asyncio import Event as _RosterEvent    # bound at import: tests swap the module's asyncio


async def run_feed(url, token, owners_fn, on_buy, status, stop, *, on_metadata=None, measurement=None,
                   on_migration=None, on_damm_creation=None, on_sell=None):
    import grpc
    from sniper.services.memecoin_slot_signal import (
        _receipt_identity, _publish_receipt, _coverage_gap)
    measurement = measurement or getattr(on_buy, "measurement", None)
    feed = "shyft:" + urlsplit(url).hostname
    backoff, generation = 1.0, 0
    status.update(connected=False, reconnects=0, owners=0, transactions=0, decoded=0,
                  decode_errors=0, sells=0, last_error=None, last_frame_mono=None, head_slot=None,
                  head_slot_mono=None,
                  requested_owners=(), acknowledged_owners=(),
                  migration_filter_requested=on_migration is not None, migration_filter_acknowledged=False,
                  migration_filter_generation=None, migration_frames=0, migration_pools=0,
                  damm_filter_requested=on_damm_creation is not None, damm_filter_acknowledged=False,
                  damm_filter_generation=None, damm_frames=0, damm_creations=0,
                  owners_errors=0, owners_error=None)
    last_owners = {}

    def owners_now():
        # A roster snapshot that raises keeps the last good roster and is named (Gamma W6): it must
        # not drop the stream and blind every owner for a reconnect.
        nonlocal last_owners
        try:
            last_owners = dict(owners_fn())
        except Exception as exc:
            status['owners_errors'] += 1
            status['owners_error'] = type(exc).__name__
        return last_owners
    while not stop.is_set():
        stream = None
        ready = False
        ping_rosters = {}
        ping_migrations = {}
        ping_damm = {}
        migration_label = damm_label = None
        observed_roster, roster_epoch, requested_epoch, request_fingerprint = None, 0, None, None
        try:
            generation += 1
            status["connection_generation"] = generation
            ack_deadline = time.monotonic() + 10.0
            # A roster change wakes the request loop at once (Alpha (c'), 2026-10-15): it used to
            # wait out the 5 s ping sleep, and every owner's frames drop until the new roster acks
            # (70 such bursts, 506 s blind, in the journal to 10-15).
            roster_changed = _RosterEvent()
            def observe_roster(owners, wake=True):
                nonlocal ready, ack_deadline, observed_roster, roster_epoch
                current = tuple(sorted(owners))
                if current != observed_roster:
                    wake = wake and observed_roster is not None   # the first roster is the first request's
                    observed_roster = current
                    roster_epoch += 1
                    ready = False
                    ack_deadline = time.monotonic() + 10.0
                    if wake:
                        roster_changed.set()
                    status.update(connected=False, acknowledged_owners=(), watch_generation=roster_epoch,
                        acknowledged_watch_generation=None, acknowledged_filter_fingerprint=None,
                        migration_filter_acknowledged=False, damm_filter_acknowledged=False)
                return current
            async def requests():
                nonlocal migration_label, damm_label, requested_epoch, request_fingerprint
                last = None
                ping = 0
                requested_at = float('-inf')
                while not stop.is_set():
                    owners = owners_now()
                    current = observe_roster(owners, wake=False)
                    if current != last or requested_epoch != roster_epoch:
                        requested_epoch = roster_epoch
                        status.update(owners=len(owners), requested_owners=tuple(sorted(current)),
                                      acknowledged_owners=(), connected=False)
                        migration_label = f'migrations:c{generation}:r{requested_epoch}' if on_migration else None
                        damm_label = f'damm:c{generation}:r{requested_epoch}' if on_damm_creation else None
                        status.update(damm_filter_generation=damm_label, damm_filter_acknowledged=False)
                        status.update(migration_filter_generation=migration_label,
                                      migration_filter_acknowledged=False)
                        request = subscription(owners, migration=on_migration is not None,
                                           migration_label=migration_label,
                                           damm=on_damm_creation is not None, damm_label=damm_label)
                        import hashlib
                        request_fingerprint = hashlib.sha256(request.SerializeToString(deterministic=True)).hexdigest()
                        status.update(requested_watch_generation=requested_epoch,
                            requested_filter_fingerprint=request_fingerprint)
                        yield request
                        requested_at = time.monotonic()
                        last = current
                    ping += 1
                    ping_rosters[ping] = (status['requested_owners'], requested_epoch, request_fingerprint)
                    ping_migrations[ping] = migration_label
                    ping_damm[ping] = damm_label
                    if len(ping_rosters) > 2:
                        expired_ping = min(ping_rosters)
                        del ping_rosters[expired_ping]
                        ping_migrations.pop(expired_ping, None)
                        ping_damm.pop(expired_ping, None)
                    yield pb.SubscribeRequest(ping=pb.SubscribeRequestPing(id=ping))
                    try:                     # the next ping at PING_S, or at once on a roster change
                        await asyncio.wait_for(roster_changed.wait(), timeout=PING_S)
                    except TimeoutError:
                        pass
                    roster_changed.clear()
                    pause = REROSTER_MIN_S - (time.monotonic() - requested_at)   # one re-request per 100 ms
                    if pause > 0:
                        await asyncio.sleep(pause)

            target = urlsplit(url).hostname + ':443'
            async with grpc.aio.secure_channel(target, grpc.ssl_channel_credentials(), options=[
                    ('grpc.max_receive_message_length', 4 * 1024 * 1024),
                    ('grpc.keepalive_time_ms', 10000)]) as channel:
                call = channel.stream_stream('/geyser.Geyser/Subscribe',
                    request_serializer=pb.SubscribeRequest.SerializeToString,
                    response_deserializer=pb.SubscribeUpdate.FromString)
                stream = call(requests(), metadata=(('x-token', token),))
                iterator = stream.__aiter__()
                head, head_mono, highest_head, dead = None, None, None, set()
                status.update(head_slot=None, head_slot_mono=None)
                while not stop.is_set():
                    observe_roster(owners_now())
                    before = time.monotonic()
                    if not ready and before >= ack_deadline:
                        raise TimeoutError
                    remaining = 10.0 if ready else ack_deadline - before
                    update = await asyncio.wait_for(iterator.__anext__(), timeout=remaining)
                    mono = time.monotonic()
                    receipt_owners = owners_now()
                    current = observe_roster(receipt_owners)
                    if not ready and time.monotonic() >= ack_deadline:
                        raise TimeoutError
                    status['last_frame_mono'] = mono
                    if update.HasField('pong'):
                        roster = ping_rosters.pop(update.pong.id, None)
                        migration_ack = ping_migrations.pop(update.pong.id, None)
                        damm_ack = ping_damm.pop(update.pong.id, None)
                        # An old/unknown pong cannot attest the current transaction filter.
                        if (roster == (current, roster_epoch, request_fingerprint)
                                and current == status['requested_owners'] and requested_epoch == roster_epoch):
                            if not ready and time.monotonic() >= ack_deadline:
                                raise TimeoutError
                            status.update(acknowledged_owners=current, acknowledged_watch_generation=roster_epoch,
                                acknowledged_filter_fingerprint=request_fingerprint)
                            ready = True
                            status.update(connected=True, last_error=None)
                            status['migration_filter_acknowledged'] = bool(
                                on_migration is not None and migration_ack == migration_label)
                            status['damm_filter_acknowledged'] = bool(
                                on_damm_creation is not None and damm_ack == damm_label)
                            backoff = 1.0
                    elif update.HasField('slot') and update.slot.slot > 0:
                        if update.slot.status == pb.SLOT_DEAD:
                            dead.add(update.slot.slot)
                            if len(dead) > 32:
                                dead.remove(min(dead))
                            if head == update.slot.slot:
                                head = head_mono = None
                                status.update(head_slot=None, head_slot_mono=None)
                            continue
                        if update.slot.slot in dead:
                            continue
                        if highest_head is None or update.slot.slot > highest_head:
                            head = highest_head = update.slot.slot
                            head_mono = mono
                            status.update(head_slot=head, head_slot_mono=head_mono)
                    elif ready and update.HasField('transaction'):
                        if on_damm_creation is not None and damm_label in update.filters:
                            status['damm_frames'] += 1
                            if (status['damm_filter_acknowledged']
                                    and update.transaction.slot not in dead and head is not None and head_mono is not None
                                    and 0 <= mono - head_mono <= 2.0
                                    and head - 2 <= update.transaction.slot <= head + 2):
                                signature = damm_creation(update)
                                if signature is not None:
                                    on_damm_creation(signature)
                                    status['damm_creations'] += 1
                            if 'owners' not in update.filters:
                                continue
                        elif on_damm_creation is not None and any(f.startswith('damm:') for f in update.filters):
                            continue
                        if on_migration is not None and migration_label in update.filters:
                            status['migration_frames'] += 1
                            if (status['migration_filter_acknowledged']
                                    and update.transaction.slot not in dead and head is not None and head_mono is not None
                                    and 0 <= mono - head_mono <= 2.0
                                    and head - 2 <= update.transaction.slot <= head + 2):
                                hit = migration_pool(update)
                                if hit is not None:
                                    on_migration(*hit)
                                    status['migration_pools'] += 1
                            if 'owners' not in update.filters:
                                continue
                        elif on_migration is not None and any(f.startswith('migrations:') for f in update.filters):
                            continue
                        status['transactions'] += 1
                        if (update.transaction.slot in dead or head is None or head_mono is None
                                or not 0 <= mono - head_mono <= 2.0
                                or not head - 2 <= update.transaction.slot <= head + 2):
                            tx_slot = update.transaction.slot
                            _coverage_gap(measurement, feed, 'transaction_slot_unusable',
                                          generation, mono, mono, 1,
                                          why=('dead_slot' if tx_slot in dead else 'head_none' if head is None
                                               or head_mono is None else 'head_stale'
                                               if not 0 <= mono - head_mono <= 2.0 else 'slot_below_head'
                                               if tx_slot < head - 2 else 'slot_above_head'),
                                          tx_slot=tx_slot, head_slot=head,
                                          head_age_ms=(round((mono - head_mono) * 1000.0, 1)
                                                       if head_mono is not None else None),
                                          **_dropped_frame(update, receipt_owners))
                            continue
                        from sniper.util.clock import real_utc_now_iso
                        utc = real_utc_now_iso()
                        if current != status['acknowledged_owners']:
                            ready = False
                            status.update(connected=False, acknowledged_owners=())
                            _coverage_gap(measurement, feed, 'transaction_before_roster_ack',
                                          generation, mono, mono, 1, tx_slot=update.transaction.slot,
                                          **_dropped_frame(update, receipt_owners))
                            continue
                        frozen_receipt = _receipt_identity(measurement, mono, generation)
                        try:
                            buys = decode_update(update, receipt_owners, recv_monotonic=mono,
                                recv_utc=utc,
                                head_slot=head, head_mono=head_mono,
                                receipt_evidence=frozen_receipt, signal_feed=feed)
                        except Exception as exc:
                            status['decode_errors'] += 1
                            status['last_error'] = 'decode:' + type(exc).__name__
                            _coverage_gap(measurement, feed, 'decode_error', generation, mono, mono)
                            continue
                        for buy in buys:
                            try:
                                on_buy(buy)
                            finally:
                                if not getattr(on_buy, 'publishes_receipts', False):
                                    _publish_receipt(measurement, buy, feed, generation)
                            status['decoded'] += 1
                        if on_sell is not None:
                            try:
                                sells = decode_sells(update, receipt_owners, recv_monotonic=mono,
                                    recv_utc=utc, head_slot=head, head_mono=head_mono)
                                if sells:
                                    on_sell(sells)
                                    status['sells'] += len(sells)
                            except Exception as exc:
                                status['decode_errors'] += 1
                                status['last_error'] = 'sell:' + type(exc).__name__
                        if on_metadata is not None:
                            # Both decoders use this exact update and receipt-time owner snapshot.
                            extra = {'native_buys': buys, 'receipt_owners': receipt_owners}
                            if measurement is not None:
                                extra['receipt_evidence'] = frozen_receipt
                            if not buys and measurement is not None:
                                extra['_pending_unknown'] = generation
                            try:
                                on_metadata(update, recv_monotonic=mono, recv_utc=utc,
                                    head_slot=head, head_mono=head_mono, **extra)
                            except Exception:
                                if not buys:
                                    _publish_empty_receipt(update, measurement, feed=feed,
                                        generation=generation, mono=mono, evidence=frozen_receipt)
                                _coverage_gap(measurement, feed, 'metadata_offer_error', generation, mono, mono, 1)
                                raise
                        elif not buys:
                            _publish_empty_receipt(update, measurement, feed=feed,
                                generation=generation, mono=mono, evidence=frozen_receipt)
                    elif update.HasField('transaction'):
                        _coverage_gap(measurement, feed, 'transaction_before_roster_ack',
                                      generation, mono, mono, 1, tx_slot=update.transaction.slot,
                                      **_dropped_frame(update, receipt_owners))
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            status['last_error'] = 'stream_silent' if ready else 'subscription_ack_timeout'
        except Exception as exc:
            # Provider details can echo authentication metadata. Only expose typed failures.
            status['last_error'] = (exc.code().name if isinstance(exc, grpc.RpcError)
                                    else type(exc).__name__)
        finally:
            _coverage_gap(measurement, feed, 'connection_lost', generation,
                          status.get('last_frame_mono'), time.monotonic())
            if stream is not None:
                stream.cancel()
            status.update(connected=False, acknowledged_owners=())
            status['migration_filter_acknowledged'] = False
            status['damm_filter_acknowledged'] = False
        status['reconnects'] += 1
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30.0)
