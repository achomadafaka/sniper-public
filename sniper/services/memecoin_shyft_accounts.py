"""One confirmed Yellowstone account stream with a bounded off-thread handoff.

``watch_fn`` returns a prepared address snapshot; ``on_account`` is a nonblocking
queue offer. The consumer thread owns account layouts, tapes, seeds and SQLite.
Account notifications never prove transaction execution or source authority.
"""
from __future__ import annotations

import asyncio
import queue
import time
from urllib.parse import urlsplit

import base58

from sniper.feeds.yellowstone import geyser_pb2 as pb

MAX_WATCH_ACCOUNTS = 512
MAX_ACCOUNT_DATA = 64 * 1024
WATCH_REFRESH_S = 2.0


def account_update(update):
    """Normalize the actual account envelope without interpreting its raw layout."""
    try:
        if not update.HasField('account') or not update.account.HasField('account'):
            return None
        envelope, account = update.account, update.account.account
        if (not envelope.slot or len(account.pubkey) != 32 or len(account.owner) != 32
                or len(account.data) > MAX_ACCOUNT_DATA):
            return None
        sig = bytes(account.txn_signature) if account.HasField('txn_signature') else b''
        return dict(pubkey=base58.b58encode(account.pubkey).decode(),
            txn_signature=base58.b58encode(sig).decode() if len(sig) == 64 else None,
            owner=base58.b58encode(account.owner).decode(), lamports=account.lamports,
            data=bytes(account.data), slot=envelope.slot, write_version=account.write_version,
            is_startup=envelope.is_startup, deleted=account.lamports == 0)
    except (AttributeError, TypeError, ValueError):
        return None


def _watch(watched):
    keys = set()
    try:
        for key in watched:
            if (not isinstance(key, str) or not 32 <= len(key) <= 44
                    or len(base58.b58decode(key)) != 32):
                raise ValueError('watch_invalid')
            keys.add(key)
            if len(keys) > MAX_WATCH_ACCOUNTS:
                raise ValueError('watch_limit')
    except (TypeError, ValueError) as exc:
        raise ValueError('watch_limit' if str(exc) == 'watch_limit' else 'watch_invalid') from None
    return tuple(sorted(keys))


def subscription(watched, label, *, commitment=pb.CONFIRMED):
    if commitment not in (pb.PROCESSED, pb.CONFIRMED):
        raise ValueError("account_commitment_invalid")
    keys = _watch(watched)
    # Empty account filters match everything; omit the filter entirely when idle.
    accounts = {label: pb.SubscribeRequestFilterAccounts(account=keys)} if keys else {}
    return pb.SubscribeRequest(accounts=accounts,
        slots={'head': pb.SubscribeRequestFilterSlots(filter_by_commitment=True)},
        commitment=commitment)


class AccountQueue(queue.Queue):
    """Single producer; the existing price worker drains this on its own thread."""
    def __init__(self, maxsize=256):
        if not isinstance(maxsize, int) or maxsize < 1:
            raise ValueError('queue_size_invalid')
        super().__init__(maxsize=maxsize)
        self.stats = {'overflow': 0, 'discarded': 0}

    def _discard(self):
        while True:
            try:
                self.get_nowait()
                self.task_done()
                self.stats['discarded'] += 1
            except queue.Empty:
                return

    def offer(self, row):
        if row.get('kind') == 'account_coverage':
            self._discard()
        try:
            self.put_nowait(row)
            return True
        except queue.Full:
            # A missing intermediate vault write makes subsequent deltas unusable.
            self._discard()
            self.stats['overflow'] += 1
            gap = {k: row[k] for k in ('connection_generation', 'watch_generation') if k in row}
            self.put_nowait(dict(gap, kind='account_coverage', reason='queue_overflow'))
            return False


async def run_accounts(url, token, watch_fn, on_account, status, stop, *, commitment=pb.CONFIRMED):
    """Receive one bounded confirmed stream; enqueue only, never materialize here.

    A matching pong records request-channel acknowledgement. Only an account
    update bearing this exact filter label records observed address coverage.
    Return False from the offer seam on loss so the next row repairs a baseline.
    """
    import grpc

    connection, watch_generation, backoff = 0, 0, 1.0
    ordered, dirty, dead = {}, set(), set()
    status.update(connected=False, reconnects=0, accounts=0, updates=0,
        stale_updates=0, stale_generations=0, stale_pongs=0, queue_overflows=0,
        last_error=None, last_frame_monotonic=None, requested_accounts=(), requested_account_count=0,
        acknowledged_accounts=(), coverage='unknown')
    while not stop.is_set():
        stream, ready, label = None, False, None
        observed, ping_generations = set(), {}
        requested = ()
        ack_deadline = time.monotonic() + 10

        def gap(reason):
            dirty.update(requested)
            observed.clear()
            status.update(acknowledged_accounts=(), accounts=0, coverage='unknown')
            on_account(dict(kind='account_coverage', reason=reason,
                connection_generation=connection, watch_generation=watch_generation,
                accounts=requested))

        try:
            connection += 1
            status['connection_generation'] = connection

            async def requests():
                nonlocal ready, label, requested, watch_generation, ack_deadline
                last, ping, last_ping = None, 0, None
                while not stop.is_set():
                    current = _watch(watch_fn())
                    changed = current != last
                    if changed:
                        if last is not None:
                            gap('watch_changed')
                        requested = current
                        for key in set(ordered).difference(current):
                            del ordered[key]
                        dirty.intersection_update(current)
                        dirty.update(current)
                        watch_generation += 1
                        label = f'accounts:c{connection}:w{watch_generation}'
                        ready = False
                        ack_deadline = time.monotonic() + 10
                        status.update(requested_accounts=current, requested_account_count=len(current),
                            acknowledged_accounts=(), accounts=0, connected=False,
                            watch_generation=watch_generation, coverage='pending',
                            subscription_acknowledged=False)
                        yield subscription(current, label, commitment=commitment)
                        last = current
                    now = time.monotonic()
                    if changed or last_ping is None or now - last_ping >= 5:
                        ping += 1
                        ping_generations[ping] = (label, requested)
                        if len(ping_generations) > 4:
                            del ping_generations[min(ping_generations)]
                        yield pb.SubscribeRequest(ping=pb.SubscribeRequestPing(id=ping))
                        last_ping = now
                    await asyncio.sleep(WATCH_REFRESH_S)

            async with grpc.aio.secure_channel(urlsplit(url).hostname + ':443',
                    grpc.ssl_channel_credentials(), options=[
                        ('grpc.max_receive_message_length', 4 * 1024 * 1024),
                        ('grpc.keepalive_time_ms', 10000)]) as channel:
                call = channel.stream_stream('/geyser.Geyser/Subscribe',
                    request_serializer=pb.SubscribeRequest.SerializeToString,
                    response_deserializer=pb.SubscribeUpdate.FromString)
                stream = call(requests(), metadata=(('x-token', token),))
                iterator = stream.__aiter__()
                while not stop.is_set():
                    remaining = 10 if ready else max(.001, ack_deadline - time.monotonic())
                    update = await asyncio.wait_for(iterator.__anext__(), remaining)
                    mono = time.monotonic()
                    if not ready and mono >= ack_deadline:
                        raise TimeoutError
                    status['last_frame_monotonic'] = mono
                    if update.HasField('pong'):
                        acknowledged = ping_generations.pop(update.pong.id, None)
                        if acknowledged == (label, requested):
                            ready, backoff = True, 1.0
                            status.update(connected=True, subscription_acknowledged=True,
                                last_error=None)
                            if not requested:
                                status['coverage'] = 'idle'
                        else:
                            status['stale_pongs'] += 1
                        continue
                    if update.HasField('slot'):
                        if update.slot.status == pb.SLOT_DEAD and update.slot.slot:
                            dead.add(update.slot.slot)
                            if len(dead) > 32:
                                dead.remove(min(dead))
                            gap('dead_slot')
                        continue
                    if not update.HasField('account'):
                        continue
                    if label not in update.filters:
                        status['stale_generations'] += 1
                        continue
                    if not ready:
                        gap('account_before_ack')
                        continue
                    if _watch(watch_fn()) != requested:
                        ready = False
                        ack_deadline = mono + 10
                        status.update(connected=False, subscription_acknowledged=False)
                        gap('watch_changed')
                        continue
                    row = account_update(update)
                    if row is None:
                        gap('account_malformed')
                        continue
                    if row['pubkey'] not in requested:
                        gap('account_unwatched')
                        continue
                    if row['slot'] in dead:
                        continue
                    key, point = row['pubkey'], (row['slot'], row['write_version'])
                    previous = ordered.get(key)
                    payload = row['owner'], row['lamports'], row['data']
                    if previous is not None:
                        if previous[3] != connection:
                            # Validator write versions are local; reconcile at a newer slot.
                            stale = row['slot'] <= previous[0][0]
                        else:
                            stale = point <= previous[0]
                            if point == previous[0] and payload != previous[2]:
                                gap('account_conflict')
                                continue
                        if stale:
                            status['stale_updates'] += 1
                            continue
                    baseline = (previous is None or previous[1] or key in dirty
                        or row['is_startup'] or row['deleted']
                        or previous[2][0] != row['owner'])
                    row.update(kind='account', connection_generation=connection,
                        watch_generation=watch_generation, baseline_only=baseline,
                        recv_monotonic=mono)
                    if not baseline:
                        # LP authority needs the writing transaction (VaultTape tx-bound pairs);
                        # an unsigned live write is blind for the LP rail. Counted, never guessed.
                        name = 'signed_updates' if row.get('txn_signature') else 'lp_unsigned_rows'
                        status[name] = status.get(name, 0) + 1
                    ordered[key] = (point, row['deleted'], payload, connection)
                    dirty.discard(key)
                    observed.add(key)
                    status.update(updates=status['updates'] + 1, accounts=len(observed),
                        acknowledged_accounts=tuple(sorted(observed)),
                        coverage='observed' if observed == set(requested) else 'partial')
                    if on_account(row) is False:
                        status['queue_overflows'] += 1
                        gap('queue_overflow')
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            status['last_error'] = 'stream_silent' if ready else 'subscription_ack_timeout'
        except Exception as exc:
            # Provider descriptions may echo authentication metadata.
            status['last_error'] = (exc.code().name if isinstance(exc, grpc.RpcError)
                                    else type(exc).__name__)
        finally:
            if stream is not None:
                stream.cancel()
            status.update(connected=False, subscription_acknowledged=False)
            gap('connection_lost')
        status['reconnects'] += 1
        # Shutdown need not wait out the reconnect backoff.
        deadline = time.monotonic() + backoff
        while not stop.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(min(.1, max(0, deadline - time.monotonic())))
        backoff = min(backoff * 2, 30)
