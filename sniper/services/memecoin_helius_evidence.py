"""Confirmed Helius evidence on a separate research/watch key, never the send path."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import threading
import time
from urllib.parse import parse_qsl, urlsplit

import base58

from sniper.feeds.yellowstone import geyser_pb2 as pb
from sniper.services.memecoin_shyft_confirmed import transaction_metadata

MAX_PENDING_BYTES = 8 * 1024 * 1024
MAX_TRANSACTION_BYTES = 1232  # Supported legacy/v0 wire; transaction v1 remains unsupported.


class _PendingBytes:
    """Serialized-byte admission only; object/RSS overhead is a separate measurement.

    One budget spans reconnects. Its tiny lock is used only by this observation receiver
    and its decoder's terminal release callback, never by the funded entry path.
    """
    def __init__(self, status):
        self.status, self._lock, self._used = status, threading.Lock(), 0
        status.update(pending_bytes=0, pending_byte_limit=MAX_PENDING_BYTES, byte_budget_refused=0)

    def reserve(self, size):
        with self._lock:
            if size > MAX_PENDING_BYTES - self._used:
                self.status['byte_budget_refused'] += 1
                return None
            self._used += size
            self.status['pending_bytes'] = self._used
        released = False
        def release():
            nonlocal released
            with self._lock:
                if released:
                    return
                released = True
                self._used -= size
                self.status['pending_bytes'] = self._used
        return release


def live_config(env=None):
    env = os.environ if env is None else env
    enabled = env.get('MEMECOIN_HELIUS_EVIDENCE_ENABLED', '0')
    if enabled == '0':
        return None
    if enabled != '1':
        raise ValueError('helius evidence flag must be 0 or 1')
    url = env.get('MEMECOIN_RESEARCH_HELIUS_RPC_URL') or env.get('ONCHAIN_WATCH_HELIUS_RPC_URL', '')
    from sniper.services.memecoin_slot_signal import _shares_funded_key, ws_url_from_http
    try:
        u = urlsplit(url)
        port = u.port
        pairs = parse_qsl(u.query, strict_parsing=True, keep_blank_values=True)
    except (ValueError, TypeError):
        raise ValueError('helius evidence requires separate watch key') from None
    key = pairs[0][1] if len(pairs) == 1 and pairs[0][0] == 'api-key' else ''
    funded_keys = set()
    for name in ('MEMECOIN_SOLANA_RPC_URL', 'MEMECOIN_SOLANA_RPC_POOL_URLS'):
        for funded_url in (env.get(name) or '').split(','):
            try:
                funded_keys.update(value for field, value in parse_qsl(urlsplit(funded_url).query)
                                   if field == 'api-key')
            except ValueError:
                continue
    if (u.scheme != 'https' or u.hostname != 'mainnet.helius-rpc.com'
            or u.username is not None or u.password is not None
            or u.fragment or u.path not in ('', '/') or port not in (None, 443)
            or not u.query.startswith('api-key=') or not key or '&' in u.query
            or key in funded_keys or _shares_funded_key(url, env)):
        raise ValueError('helius evidence requires separate watch key')
    return ws_url_from_http(url)


def subscription(owners, request_id):
    # Empty accountInclude means the whole chain, not an empty roster.
    if not owners:
        return None
    if len(owners) > 50_000:
        raise ValueError('owner_limit_exceeded')
    return dict(jsonrpc='2.0', id=request_id, method='transactionSubscribe', params=[
        dict(accountInclude=sorted(owners), failed=False, vote=False),
        dict(commitment='confirmed', encoding='base64', transactionDetails='full',
             showRewards=False, maxSupportedTransactionVersion=0)])


def _uint(value):
    if type(value) is not int or not 0 <= value < 2**64:
        raise ValueError('integer_invalid')
    return value


def confirmed_transaction(frame, owners, *, head_slot, recv_monotonic, head_monotonic):
    """Only the ACK-bound CONFIRMED receiver may supply these frames to proof."""
    from solders.transaction import VersionedTransaction
    from solders.message import MessageV0
    try:
        if frame['method'] != 'transactionNotification':
            return None
        r = frame['params']['result']
        body, meta = r['transaction'], r['transaction']['meta']
        if meta['err'] is not None or body['transaction'][1] != 'base64':
            return None
        encoded = body['transaction'][0]
        if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_TRANSACTION_BYTES + 2) // 3):
            return None
        raw = base64.b64decode(encoded, validate=True)
        if not raw or len(raw) > MAX_TRANSACTION_BYTES:
            return None
        tx = VersionedTransaction.from_bytes(raw)
        if bytes(tx) != raw or not tx.signatures or str(tx.signatures[0]) != r['signature']:
            return None
        # Use the existing strict instruction/balance validator, not a second proof parser.
        update = pb.SubscribeUpdate()
        update.transaction.slot = _uint(r['slot'])
        i = update.transaction.transaction
        i.signature = bytes(tx.signatures[0])
        i.transaction.signatures.extend(bytes(s) for s in tx.signatures)
        msg, m = tx.message, i.transaction.message
        m.header.num_required_signatures = msg.header.num_required_signatures
        m.header.num_readonly_signed_accounts = msg.header.num_readonly_signed_accounts
        m.header.num_readonly_unsigned_accounts = msg.header.num_readonly_unsigned_accounts
        m.account_keys.extend(bytes(k) for k in msg.account_keys)
        m.versioned = isinstance(msg, MessageV0)
        if m.versioned:
            for lookup in msg.address_table_lookups:
                m.address_table_lookups.add(account_key=bytes(lookup.account_key),
                    writable_indexes=bytes(lookup.writable_indexes),
                    readonly_indexes=bytes(lookup.readonly_indexes))
        for ix in msg.instructions:
            m.instructions.add(program_id_index=ix.program_id_index,
                               accounts=bytes(ix.accounts), data=bytes(ix.data))
        out = i.meta
        out.fee = _uint(meta['fee'])
        out.pre_balances.extend(_uint(x) for x in meta['preBalances'])
        out.post_balances.extend(_uint(x) for x in meta['postBalances'])
        out.log_messages.extend(meta['logMessages'])
        for name, field in [('writable', out.loaded_writable_addresses),
                            ('readonly', out.loaded_readonly_addresses)]:
            field.extend(base58.b58decode(x) for x in meta['loadedAddresses'][name])
        for group in meta['innerInstructions']:
            g = out.inner_instructions.add(index=_uint(group['index']))
            for ix in group['instructions']:
                item = g.instructions.add(program_id_index=_uint(ix['programIdIndex']),
                    accounts=bytes(_uint(x) for x in ix['accounts']), data=base58.b58decode(ix['data']))
                if ix.get('stackHeight') is not None:
                    item.stack_height = _uint(ix['stackHeight'])
        for name, field in [('preTokenBalances', out.pre_token_balances),
                            ('postTokenBalances', out.post_token_balances)]:
            for b in meta[name]:
                row = field.add(account_index=_uint(b['accountIndex']), owner=b['owner'],
                               mint=b['mint'], program_id=b['programId'])
                row.ui_token_amount.amount = b['uiTokenAmount']['amount']
                row.ui_token_amount.decimals = _uint(b['uiTokenAmount']['decimals'])
        return transaction_metadata(update, owners, head_slot=head_slot,
            recv_monotonic=recv_monotonic, head_monotonic=head_monotonic)
    except (KeyError, ValueError, TypeError, IndexError, OverflowError):
        return None


async def run_feed(url, owners_fn, worker, status, stop, *, connect=None):
    """Bounded enqueue only; full decoding belongs to the existing receipt worker."""
    if connect is None:
        from websockets import connect
    backoff = 1.0
    status.update(connected=False, reconnects=0, owners=0, transactions=0,
                  last_error=None, last_frame_monotonic=None, head_slot=None,
                  head_slot_monotonic=None, bytes_received=0, subscriptions=0,
                  queue_refused=0, requested_owners=(), acknowledged_owners=())
    budget = _PendingBytes(status)
    while not stop.is_set():
        try:
            async with connect(url, max_size=4 * 1024 * 1024, max_queue=2, ping_interval=20) as ws:
                status.update(head_slot=None, head_slot_monotonic=None, acknowledged_owners=())
                await ws.send(json.dumps(dict(jsonrpc='2.0', id=0, method='slotSubscribe')))
                current, active, pending = None, None, None
                slot_active = None
                next_id, refresh_at = 1, 0.0
                head, head_mono, ack_deadline = None, None, None
                opened_mono = time.monotonic()
                while not stop.is_set():
                    mono = time.monotonic()
                    if pending is not None and mono >= ack_deadline:
                        raise TimeoutError('subscription_ack_timeout')
                    if slot_active is None and mono - opened_mono >= 10:
                        raise TimeoutError('slot_subscription_ack_timeout')
                    if mono - (head_mono if head_mono is not None else opened_mono) > 10:
                        raise TimeoutError('stream_silent')
                    if mono >= refresh_at:
                        refresh_at = mono + 5
                        owners = dict(owners_fn())
                        roster = frozenset(owners)
                        if roster != current:
                            if pending is not None:
                                raise ValueError('roster_changed_before_ack')
                            if active is not None:
                                await ws.send(json.dumps(dict(jsonrpc='2.0', id=-1,
                                    method='transactionUnsubscribe', params=[active])))
                            active, pending = None, None
                            status.update(subscriptions=0, connected=False, owners=len(owners),
                                          requested_owners=tuple(sorted(owners)), acknowledged_owners=())
                            request = subscription(owners, next_id)
                            if request is not None:
                                await ws.send(json.dumps(request))
                                pending, ack_deadline = next_id, mono + 10
                                next_id += 1
                            current = roster
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1)
                    except asyncio.TimeoutError:
                        if head_mono is not None and mono - head_mono > 10:
                            raise TimeoutError('stream_silent')
                        continue
                    mono = time.monotonic()
                    recv_epoch = time.time()  # Both receipt clocks belong to arrival, before parsing/queueing.
                    if pending is not None and mono >= ack_deadline:
                        raise TimeoutError('subscription_ack_timeout')
                    if slot_active is None and mono - opened_mono >= 10:
                        raise TimeoutError('slot_subscription_ack_timeout')
                    size = len(raw.encode() if isinstance(raw, str) else raw)
                    status['bytes_received'] += size
                    frame = json.loads(raw)
                    request_id = frame.get('id')
                    if (type(request_id) is int and
                            ((pending is not None and request_id == pending)
                             or (request_id == 0 and slot_active is None))):
                        if (frame.get('error') is not None or type(frame.get('result')) is not int
                                or not 0 <= frame['result'] < 2**64):
                            error = frame.get('error') or {}
                            status['rejection_code'] = error.get('code') if type(error.get('code')) is int else None
                            if 'not available on the free plan' in str(error.get('message', '')):
                                raise ValueError('plan_unavailable')
                            raise ValueError('slot_subscription_rejected' if request_id == 0
                                             else 'subscription_rejected')
                        if request_id == 0:
                            slot_active = frame['result']
                        else:
                            active, pending = frame['result'], None
                            status.update(subscriptions=1, acknowledged_owners=tuple(sorted(current)))
                        status.update(connected=active is not None and slot_active is not None,
                                      last_error=None, rejection_code=None)
                        if status['connected']:
                            backoff = 1.0
                    elif (slot_active is not None and frame.get('method') == 'slotNotification'
                          and type(frame.get('params', {}).get('subscription')) is int
                          and frame['params']['subscription'] == slot_active):
                        slot = _uint(frame['params']['result']['slot'])
                        if head is None or slot > head:
                            head, head_mono = slot, mono
                            status.update(head_slot=head, head_slot_monotonic=head_mono)
                    elif (active is not None and slot_active is not None and head is not None
                          and frame.get('method') == 'transactionNotification'
                          and type(frame.get('params', {}).get('subscription')) is int
                          and frame['params']['subscription'] == active):
                        status['transactions'] += 1
                        status['last_frame_monotonic'] = mono
                        release = budget.reserve(size)
                        if release is None:
                            continue
                        try:
                            accepted = worker.offer(frame, dict(owners_fn()), head_slot=head, head_monotonic=head_mono,
                                recv_monotonic=mono, recv_epoch=recv_epoch,
                                signal_feed='helius_enhanced_confirmed', on_released=release)
                        except Exception:
                            release()
                            raise
                        if accepted is not True:
                            release()
                            status['queue_refused'] += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Provider exceptions may contain the key-bearing URL.
            status['last_error'] = (str(exc) if isinstance(exc, (TimeoutError, ValueError))
                and str(exc) in ('stream_silent', 'subscription_ack_timeout', 'subscription_rejected',
                                 'slot_subscription_ack_timeout', 'slot_subscription_rejected',
                                 'plan_unavailable', 'roster_changed_before_ack', 'owner_limit_exceeded')
                else type(exc).__name__)
        finally:
            status.update(connected=False, subscriptions=0, acknowledged_owners=(),
                          head_slot=None, head_slot_monotonic=None)
        status['reconnects'] += 1
        if not stop.is_set():
            await asyncio.sleep(backoff)
        backoff = min(30.0, backoff * 2)
