"""Default-off, independent processed custody witnesses. No execution authority or ledger IO."""
from __future__ import annotations

import json
import math
import asyncio
from collections import OrderedDict
import hashlib
import threading
import time
from urllib.parse import urlsplit


def _positive_int(value):
    return type(value) is int and value > 0


def _signature(value):
    if not isinstance(value, str):
        return False
    try:
        from solders.signature import Signature
        return str(Signature.from_string(value)) == value
    except (ValueError, TypeError):
        return False


def processed_observation(frame, *, subscription_id, generation, received_generation,
                          received_monotonic, boot_id, clock_domain):
    if not (_positive_int(generation) and type(received_generation) is int
            and generation == received_generation):
        return None, 'wrong_generation'
    if (isinstance(received_monotonic, bool) or not isinstance(received_monotonic, (int, float))
            or not math.isfinite(received_monotonic) or received_monotonic < 0
            or not boot_id or not clock_domain):
        return None, 'invalid_clock'
    if not isinstance(frame, dict) or frame.get('method') != 'logsNotification':
        return None, 'not_logs_notification'
    params = frame.get('params')
    if (not isinstance(params, dict) or not _positive_int(subscription_id)
            or type(params.get('subscription')) is not int
            or params.get('subscription') != subscription_id):
        return None, 'wrong_subscription'
    result = params.get('result')
    if not isinstance(result, dict) or not isinstance(result.get('context'), dict):
        return None, 'invalid_result'
    slot, value = result['context'].get('slot'), result.get('value')
    if not _positive_int(slot):
        return None, 'invalid_slot'
    if not isinstance(value, dict):
        return None, 'invalid_value'
    if 'err' not in value:
        return None, 'missing_err'
    if not _signature(value.get('signature')):
        return None, 'invalid_signature'
    return dict(network_id='solana', our_sig=value['signature'], slot=slot,
        error_state='success' if value['err'] is None else 'failed',
        error_json=json.dumps(value['err'], sort_keys=True), recv_monotonic=received_monotonic,
        boot_id=boot_id, clock_domain=clock_domain, connection_generation=generation), None


def yellowstone_subscription(custody_pubkey):
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    from sniper.services.memecoin_shyft_confirmed import _address
    _address(custody_pubkey)
    return pb.SubscribeRequest(transactions={'custody': pb.SubscribeRequestFilterTransactions(
        vote=False, account_include=[custody_pubkey])},
        slots={'head': pb.SubscribeRequestFilterSlots(filter_by_commitment=True)},
        commitment=pb.PROCESSED)


def yellowstone_observation(update, *, custody_pubkey, generation, received_generation,
                            received_monotonic, boot_id, clock_domain, head_slot, head_monotonic):
    from sniper.services.memecoin_shyft_confirmed import transaction_metadata
    if (not _positive_int(generation) or type(received_generation) is not int
            or generation != received_generation):
        return None, 'wrong_generation'
    if (type(received_monotonic) not in (int, float) or not math.isfinite(received_monotonic)
            or received_monotonic < 0 or not boot_id or not clock_domain):
        return None, 'invalid_clock'
    receipt = transaction_metadata(update, {custody_pubkey: {}}, head_slot=head_slot,
        head_monotonic=head_monotonic, recv_monotonic=received_monotonic, allow_failed=True)
    if receipt is None:
        return None, 'invalid_transaction_metadata'
    if receipt['transaction']['message']['accountKeys'][0] != custody_pubkey:
        return None, 'custody_not_payer'
    error = receipt['meta']['err']
    return dict(network_id='solana', our_sig=receipt['transaction']['signatures'][0],
        slot=receipt['slot'], error_state='success' if error is None else 'failed',
        error_json=json.dumps(error, sort_keys=True), fee_lamports=str(receipt['meta']['fee']),
        recv_monotonic=received_monotonic, boot_id=boot_id, clock_domain=clock_domain,
        connection_generation=generation), None


async def _observer_backoff(seconds):
    await asyncio.sleep(seconds)


async def _run_yellowstone_execution_observer(*, custody_pubkey, url, token, journal, status, stop,
                                             clock=time):
    import grpc
    from sniper.services.memecoin_grpc_wakeup import install_grpc_wakeup_handler
    install_grpc_wakeup_handler()
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    from google.protobuf.message import DecodeError
    request = yellowstone_subscription(custody_pubkey)
    fingerprint = hashlib.sha256(request.SerializeToString(deterministic=True)).hexdigest()
    status.update(provider='shyft', hostname=urlsplit(url).hostname, commitment='processed',
        state='unknown', reason='observer_starting', connected=False, generation=0,
        acknowledged_generation=None, requested_count=1, acknowledged_count=0,
        requested_filter_fingerprint=fingerprint, acknowledged_filter_fingerprint=None,
        head_slot=None, head_monotonic=None, last_frame_monotonic=None,
        reconnects=0, observations=0, duplicates=0, coverage_gaps=0, last_gap=None,
        journal_evicted=0)
    generation, highest_head, backoff = 0, None, 1
    seen, dead = OrderedDict(), set()
    def gap(reason, start=None, end=None, *, lost_count=None):
        journal.record_gap(component='processed', reason=reason, connection_generation=generation,
            mono_start=start, mono_end=end, lost_count=lost_count)
        if lost_count != 0:
            status.update(state='unknown', reason=reason, coverage_gaps=status['coverage_gaps'] + 1,
                last_gap=reason)
    while not stop.is_set():
        generation += 1
        start = clock.monotonic()
        deadline, ready, stream = start + 10, False, None
        head = head_mono = None
        ping_ids = set()
        status.update(generation=generation, connected=False, acknowledged_count=0,
            acknowledged_generation=None, acknowledged_filter_fingerprint=None,
            head_slot=None, head_monotonic=None)
        try:
            async def requests():
                yield request
                ping = 0
                while not stop.is_set():
                    ping += 1
                    ping_ids.add(ping)
                    if len(ping_ids) > 2:
                        ping_ids.remove(min(ping_ids))
                    yield pb.SubscribeRequest(ping=pb.SubscribeRequestPing(id=ping))
                    await asyncio.sleep(5)
            async with grpc.aio.secure_channel(urlsplit(url).hostname + ':443',
                    grpc.ssl_channel_credentials(), options=[
                        ('grpc.max_receive_message_length', 4 * 1024 * 1024),
                        ('grpc.keepalive_time_ms', 10000)]) as channel:
                call = channel.stream_stream('/geyser.Geyser/Subscribe',
                    request_serializer=pb.SubscribeRequest.SerializeToString,
                    response_deserializer=lambda raw: raw)
                stream = call(requests(), metadata=(('x-token', token),))
                iterator = stream.__aiter__()
                while not stop.is_set():
                    timeout = 10
                    if not ready:
                        timeout = deadline - clock.monotonic()
                        if timeout <= 0:
                            raise TimeoutError
                    raw = await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
                    mono = clock.monotonic()  # Timestamp received bytes before protobuf decoding.
                    status['last_frame_monotonic'] = mono
                    if not ready and mono >= deadline:
                        raise TimeoutError
                    try:
                        update = pb.SubscribeUpdate.FromString(raw)
                    except DecodeError:
                        gap('invalid_frame', mono, mono)
                        continue
                    if update.HasField('pong'):
                        if update.pong.id in ping_ids:
                            if not ready and clock.monotonic() >= deadline:
                                raise TimeoutError
                            ping_ids.remove(update.pong.id)
                            if not ready:
                                gap('observer_ready', mono, mono, lost_count=0)
                            ready, backoff = True, 1
                            status.update(connected=True, state='observing', reason=None,
                                acknowledged_count=1, acknowledged_generation=generation,
                                acknowledged_filter_fingerprint=fingerprint)
                        continue
                    if update.HasField('slot') and 'head' in update.filters:
                        slot = update.slot.slot
                        if update.slot.status == pb.SLOT_DEAD:
                            if slot == 0:
                                continue
                            if slot not in dead:
                                gap('dead_slot', mono, mono)
                            dead.add(slot)
                            if len(dead) > 32:
                                dead.remove(min(dead))
                            if slot == head:
                                head = head_mono = None
                                status.update(head_slot=None, head_monotonic=None)
                            continue
                        if (slot == 0 or slot in dead or update.slot.status not in
                                (pb.SLOT_PROCESSED, pb.SLOT_CONFIRMED, pb.SLOT_FINALIZED)):
                            continue
                        if highest_head is None or slot > highest_head or head is None and slot == highest_head:
                            if highest_head is not None and slot - highest_head > 150:
                                gap('replay_depth_exceeded', mono, mono)
                            head = highest_head = slot
                            head_mono = mono
                            status.update(head_slot=head, head_monotonic=head_mono)
                        continue
                    if not update.HasField('transaction'):
                        continue
                    if not ready:
                        gap('unacknowledged_generation', mono, mono)
                        continue
                    if list(update.filters) != ['custody']:
                        gap('wrong_filter', mono, mono)
                        continue
                    if (head is None or head_mono is None or not 0 <= mono - head_mono <= 2
                            or update.transaction.slot in dead or not head - 2 <= update.transaction.slot <= head + 2):
                        gap('stale_or_dead_slot', mono, mono)
                        continue
                    row, reason = yellowstone_observation(update, custody_pubkey=custody_pubkey,
                        generation=generation, received_generation=generation,
                        received_monotonic=mono, boot_id=journal.boot_id,
                        clock_domain=journal.clock_domain, head_slot=head, head_monotonic=head_mono)
                    if row is None:
                        gap(reason, mono, mono)
                        continue
                    key = (row['our_sig'], row['slot'], row['error_json'])
                    if key in seen:
                        status['duplicates'] += 1
                        continue
                    if len(seen) >= 4096:
                        seen.popitem(last=False)
                    seen[key] = None
                    journal.record_processed(**row)
                    status['observations'] += 1
                    if journal.evicted_cap > status['journal_evicted']:
                        gap('journal_overflow', mono, mono)
                        status['journal_evicted'] = journal.evicted_cap
        except asyncio.CancelledError:
            gap('observer_cancelled', start, clock.monotonic())
            raise
        except Exception as exc:
            if not stop.is_set():
                reason = ('stream_silent' if ready else 'subscription_ack_timeout') if isinstance(exc, TimeoutError) else (
                    'overflow' if isinstance(exc, grpc.RpcError) and exc.code() == grpc.StatusCode.RESOURCE_EXHAUSTED else
                    'connection_loss')
                gap(reason, start, clock.monotonic())
        finally:
            if stream is not None:
                stream.cancel()
            status.update(connected=False, acknowledged_count=0, acknowledged_generation=None,
                acknowledged_filter_fingerprint=None)
        if not stop.is_set():
            status['reconnects'] += 1
            await _observer_backoff(backoff)
            backoff = min(backoff * 2, 30)
    status.update(state='stopped', reason='observer_stopped')


def start_yellowstone_execution_observer(*, custody_pubkey, url, token, journal, status, stop):
    """One public-key-only, processed custody stream; observations confer no authority."""
    try:
        parts = urlsplit(url)
        valid = (parts.scheme == 'https' and parts.hostname in ('grpc.ams.shyft.to', 'grpc.fra.shyft.to')
            and not parts.username and not parts.password and parts.port in (None, 443)
            and parts.path in ('', '/') and not parts.query and not parts.fragment
            and isinstance(token, str) and bool(token.strip()))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError('observer_endpoint_invalid')
    yellowstone_subscription(custody_pubkey)
    def run():
        try:
            asyncio.run(_run_yellowstone_execution_observer(custody_pubkey=custody_pubkey,
                url=url, token=token, journal=journal, status=status, stop=stop))
        except Exception:
            status.update(state='unknown', reason='observer_start_failed', connected=False)
            journal.record_gap(component='processed', reason='observer_start_failed',
                connection_generation=status.get('generation', 0), lost_count=None)
    thread = threading.Thread(target=run, name='memecoin-yellowstone-execution-observer', daemon=True)
    thread.start()
    return thread


def receipt_execution_evidence(receipt, *, custody_pubkey, mint, payment_asset, native_accounting=None):
    """Only complete cached receipt atoms qualify; float booked quantities never enter here."""
    def unknown(reason):
        return dict(state='unknown', reason=reason)
    try:
        from sniper.execution.solana_swap_executor import USDC_MINT
        tx, meta = receipt['transaction'], receipt['meta']
        slot = receipt['slot']
        keys, sigs = tx['message']['accountKeys'], tx['signatures']
        if (not _positive_int(slot) or not isinstance(meta, dict) or 'err' not in meta
                or meta['err'] is not None):
            return unknown('receipt_not_successful')
        if not isinstance(keys, list) or not keys:
            return unknown('receipt_identity')
        payer = keys[0]
        if isinstance(payer, dict):
            payer_matches = payer.get('pubkey') == custody_pubkey and payer.get('signer') is True
        else:
            required = (tx['message'].get('header') or {}).get('numRequiredSignatures')
            payer_matches = (isinstance(payer, str) and payer == custody_pubkey
                and type(required) is int and 0 < required <= len(keys)
                and all(isinstance(key, str) for key in keys))
        if not payer_matches or not isinstance(sigs, list) or not sigs or not _signature(sigs[0]):
            return unknown('receipt_identity')
        pre, post = meta['preBalances'], meta['postBalances']
        if (not isinstance(pre, list) or not isinstance(post, list) or len(pre) != len(post)
                or len(pre) < len(keys) or any(type(a) is not int or a < 0 for a in (*pre, *post))):
            return unknown('native_balances_unknown')
        sides = []
        for name in ('preTokenBalances', 'postTokenBalances'):
            rows = meta[name]
            if not isinstance(rows, list):
                return unknown('token_balances_unknown')
            balances = {}
            for row in rows:
                ix, amount = row['accountIndex'], row['uiTokenAmount']
                atoms, decimals = amount['amount'], amount['decimals']
                if (type(ix) is not int or ix < 0 or ix >= len(keys) or ix in balances
                        or not isinstance(row.get('owner'), str) or not row['owner']
                        or not isinstance(row.get('mint'), str) or not row['mint']
                        or not isinstance(atoms, str) or not atoms.isascii() or not atoms.isdigit()
                        or type(decimals) is not int or not 0 <= decimals <= 255):
                    return unknown('token_balance_malformed')
                balances[ix] = (row['owner'], row['mint'], int(atoms), decimals)
            sides.append(balances)
        old, new = sides
        deltas, places = {}, {}
        for ix in old.keys() | new.keys():
            a, b = old.get(ix), new.get(ix)
            if a is None:
                if pre[ix] != 0:
                    return unknown('missing_pre_balance')
                a = (b[0], b[1], 0, b[3])
            if b is None:
                if post[ix] != 0:
                    return unknown('missing_post_balance')
                b = (a[0], a[1], 0, a[3])
            if (a[0], a[1], a[3]) != (b[0], b[1], b[3]):
                return unknown('token_identity_mismatch')
            if a[0] != custody_pubkey:
                continue
            token = a[1]
            if token in places and places[token] != a[3]:
                return unknown('token_decimals_mismatch')
            places[token] = a[3]
            deltas[token] = deltas.get(token, 0) + b[2] - a[2]
        acquired = deltas.get(mint)
        if acquired is None or acquired <= 0:
            return unknown('acquisition_unknown')
        extra = {}
        if payment_asset == 'usdc':
            payment = -deltas.get(USDC_MINT, 0)
            if places.get(USDC_MINT) != 6:
                return unknown('payment_decimals')
        elif payment_asset == 'sol':
            if not isinstance(native_accounting, dict):
                return unknown('native_accounting_unknown')
            payment = native_accounting.get('sol_spent_lamports')
            fee, rent, tip = (native_accounting.get(name, 0) for name in
                ('fee_lamports', 'rent_lamports', 'tip_lamports'))
            if (any(type(a) is not int or a < 0 for a in (payment, fee, rent, tip))
                    or meta.get('fee') != fee or pre[0] - post[0] != payment + fee + rent + tip
                    or native_accounting.get('decimals') != places[mint]):
                return unknown('native_accounting_mismatch')
            from sniper.execution.solana_swap_executor import receipt_sol_entry_fill
            validated = receipt_sol_entry_fill(receipt, signature=sigs[0], owner=custody_pubkey,
                mint=mint, maximum_lamports=payment, tip_lamports=tip)
            if not validated or any(validated.get(name, 0) != native_accounting.get(name, 0)
                    for name in ('sol_spent_lamports', 'fee_lamports', 'rent_lamports', 'tip_lamports')):
                return unknown('native_accounting_mismatch')
            extra = dict(fee_lamports=str(fee), rent_lamports=str(rent), tip_lamports=str(tip))
        else:
            return unknown('payment_asset_unknown')
        if payment <= 0:
            return unknown('payment_unknown')
        return dict(state='verified', reason=None, receipt_slot=slot, tx_sig=sigs[0],
            custody_pubkey=custody_pubkey, mint=mint, network_id='solana',
            payment_asset=payment_asset, payment_atomic=str(payment),
            acquired_atomic=str(acquired), acquired_decimals=places[mint], **extra)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError, OverflowError):
        return unknown('receipt_malformed')


def start_execution_observer(*, custody_pubkey, ws_url, journal, status, stop, connect=None, clock=time):
    """One mention subscription, one journal owner; faults affect this daemon alone."""
    def run():
        nonlocal connect
        generation = 0
        def gap(reason, start=None, end=None):
            journal.record_gap(component='processed', reason=reason, connection_generation=generation,
                               mono_start=start, mono_end=end, lost_count=None)
            status.update(state='unknown', reason=reason, generation=generation)
        if connect is None:
            try:
                from websockets.sync.client import connect
            except Exception:
                gap('observer_dependency_unavailable')
                return
        while not stop.is_set():
            generation += 1
            subscription = None
            start = clock.monotonic()
            try:
                with connect(ws_url, open_timeout=5, close_timeout=1, max_size=1024 * 1024) as ws:
                    ws.send(json.dumps(dict(jsonrpc='2.0', id=generation, method='logsSubscribe',
                        params=[{'mentions': [custody_pubkey]}, {'commitment': 'processed'}])))
                    while not stop.is_set():
                        raw = ws.recv(timeout=30)
                        received = clock.monotonic()  # first instruction after recv, before JSON
                        try:
                            frame = json.loads(raw)
                        except (ValueError, TypeError):
                            gap('invalid_json', received, received)
                            continue
                        if subscription is None:
                            if (not isinstance(frame, dict) or frame.get('jsonrpc') != '2.0'
                                    or type(frame.get('id')) is not int or frame['id'] != generation
                                    or 'error' in frame or not _positive_int(frame.get('result'))):
                                raise ValueError('ack_refused')
                            subscription = frame['result']
                            journal.record_gap(component='processed', reason='observer_ready',
                                connection_generation=generation, mono_start=received,
                                mono_end=received, lost_count=0)
                            status.update(state='observing', generation=generation, subscription=subscription)
                            continue
                        row, reason = processed_observation(frame, subscription_id=subscription,
                            generation=generation, received_generation=generation,
                            received_monotonic=received, boot_id=journal.boot_id,
                            clock_domain=journal.clock_domain)
                        if row is None:
                            gap(reason, received, received)
                        else:
                            journal.record_processed(**row)
            except Exception as exc:
                reason = ('timeout' if isinstance(exc, TimeoutError) else
                          'ack_refused' if str(exc) == 'ack_refused' else
                          'overflow' if 'too big' in str(exc).lower() else 'connection_loss')
                gap(reason, start, clock.monotonic())
                stop.wait(1.0)
        status.update(state='stopped')
    thread = threading.Thread(target=run, name='memecoin-execution-observer', daemon=True)
    thread.start()
    return thread
