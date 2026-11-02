"""Confirmed Yellowstone receipts preserve the existing signer/payment proof; no RPC."""
import asyncio
import copy
import json
import sqlite3
import struct
import threading
import time
from pathlib import Path

import base58
import pytest

from sniper.feeds.yellowstone import geyser_pb2 as pb

FIX = json.loads((Path(__file__).parent / 'fixtures/memecoin/plt-route-2ebfcf66.json').read_text())
OWNER = FIX['transaction']['message']['accountKeys'][0]['pubkey']
TOKEN = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
WATCH = {OWNER: dict(uid='u1', handle='source', status='registry_verified')}


def wire_receipt(fixture=FIX):
    """Encode the real PLT receipt's parsed instructions as their wire instructions."""
    FIX = fixture
    u = pb.SubscribeUpdate()
    u.transaction.slot = FIX['slot']
    i = u.transaction.transaction
    i.signature = base58.b58decode(FIX['transaction']['signatures'][0])
    i.transaction.signatures.extend(base58.b58decode(x) for x in FIX['transaction']['signatures'])
    m = i.transaction.message
    m.header.num_required_signatures = len(i.transaction.signatures)
    keys = [x['pubkey'] for x in FIX['transaction']['message']['accountKeys']]
    # Exercise lookup-address indexing rather than only the jsonParsed-expanded shape.
    m.account_keys.extend(base58.b58decode(k) for k in keys[:10])
    m.header.num_readonly_unsigned_accounts = 0
    i.meta.loaded_writable_addresses.extend(base58.b58decode(k) for k in keys[10:])
    m.versioned = True
    lookup = m.address_table_lookups.add(account_key=bytes([77]) * 32)
    lookup.writable_indexes = bytes(range(len(keys) - 10))
    i.meta.pre_balances.extend(FIX['meta']['preBalances'])
    i.meta.post_balances.extend(FIX['meta']['postBalances'])
    i.meta.fee = FIX['meta']['fee']
    i.meta.log_messages.extend(FIX['meta']['logMessages'])
    for name, rows in [('pre_token_balances', FIX['meta']['preTokenBalances']),
                       ('post_token_balances', FIX['meta']['postTokenBalances'])]:
        for b in rows:
            r = getattr(i.meta, name).add(account_index=b['accountIndex'], owner=b['owner'],
                mint=b['mint'], program_id=b['programId'])
            r.ui_token_amount.amount = b['uiTokenAmount']['amount']
            r.ui_token_amount.decimals = b['uiTokenAmount']['decimals']
    def compiled(src, dst):
        program = src['programId']
        dst.program_id_index = keys.index(program)
        parsed = src.get('parsed')
        if parsed is None:
            dst.accounts = bytes(keys.index(k) for k in src['accounts'])
            dst.data = base58.b58decode(src['data'])
        else:
            kind, x = parsed['type'], parsed['info']
            accounts, data = [], b'\xff'
            if kind == 'transferChecked':
                accounts = [x[k] for k in ('source', 'mint', 'destination', 'authority')]
                data = bytes([12]) + struct.pack('<Q', int(x['tokenAmount']['amount'])) + bytes([x['tokenAmount']['decimals']])
            elif kind == 'transfer' and src.get('program') in ('spl-token', 'spl-token-2022'):
                accounts = [x['source'], x['destination'],
                            x.get('authority', x.get('multisigAuthority'))]
                accounts.extend(x.get('signers', []))
                data = bytes([3]) + struct.pack('<Q', int(x['amount']))
            elif kind == 'initializeAccount3':
                accounts = [x['account'], x['mint']]
                data = bytes([18]) + base58.b58decode(x['owner'])
            elif kind == 'closeAccount':
                accounts = [x[k] for k in ('account', 'destination', 'owner')]
                data = bytes([9])
            elif kind == 'createAccount':
                accounts = [x['source'], x['newAccount']]
                data = struct.pack('<IQQ', 0, x['lamports'], x['space']) + base58.b58decode(x['owner'])
            elif kind == 'transfer' and src.get('program') == 'system':
                accounts = [x['source'], x['destination']]
                data = struct.pack('<IQ', 2, x['lamports'])
            elif kind == 'allocate':
                accounts = [x['account']]
                data = struct.pack('<IQ', 8, x['space'])
            elif kind == 'assign':
                accounts = [x['account']]
                data = struct.pack('<I', 1) + base58.b58decode(x['owner'])
            elif kind == 'getAccountDataSize':
                accounts = [x['mint']]
                data = bytes([21]) + struct.pack('<H', 7)
            elif kind == 'initializeImmutableOwner':
                accounts = [x['account']]
                data = bytes([22])
            elif kind in ('create', 'createIdempotent'):
                accounts = [x[k] for k in ('source', 'account', 'wallet', 'mint', 'systemProgram', 'tokenProgram')]
                data = bytes([1 if kind == 'createIdempotent' else 0])
            dst.accounts = bytes(keys.index(k) for k in accounts)
            dst.data = data
        if hasattr(dst, 'stack_height') and src.get('stackHeight') is not None:
            dst.stack_height = src['stackHeight']
    for x in FIX['transaction']['message']['instructions']:
        compiled(x, m.instructions.add())
    for g in FIX['meta']['innerInstructions']:
        group = i.meta.inner_instructions.add(index=g['index'])
        for x in g['instructions']:
            compiled(x, group.instructions.add())
    return u


def decode(u, watch=WATCH):
    from sniper.services.memecoin_shyft_confirmed import confirmed_transaction
    return confirmed_transaction(u, watch, head_slot=u.transaction.slot,
                                 recv_monotonic=1.0, head_monotonic=1.0)


def test_confirmed_subscription_is_filtered_empty_safe_and_never_replayed():
    from sniper.services.memecoin_shyft_confirmed import subscription
    r = subscription(WATCH)
    assert r.commitment == pb.CONFIRMED
    assert list(r.transactions['owners'].account_include) == [OWNER]
    assert r.transactions['owners'].failed is False
    assert not r.HasField('from_slot')
    assert not subscription({}).transactions


def test_real_routed_receipt_retains_exact_signer_payment_loaded_keys_and_unknown_time():
    from sniper.services.memecoin_source_valuation import value_source_buy
    from sniper.services.memecoin_source_initiation import solana_receipt_initiation
    tx = decode(wire_receipt())
    assert tx['blockTime'] is None
    facts = value_source_buy(tx, OWNER, sol_usd=None, now=105)
    assert facts['basis'] == 'route_owner_quote_out', facts
    assert facts['source_size_usd'] == 500.0
    assert facts['token_amount_atomic'] == '16675750779965'
    proof = solana_receipt_initiation(tx, OWNER, facts)
    assert proof['payer'] == OWNER and proof['payment_atomic'] == '500000000'
    assert proof['route_payment_atomic'] == '500000000'
    assert proof['block_number'] == FIX['slot']


def test_system_transfer_trailing_marker_preserves_exact_routed_proof():
    from sniper.services.memecoin_shyft_confirmed import SYSTEM
    from sniper.services.memecoin_source_valuation import value_source_buy
    from sniper.services.memecoin_source_initiation import solana_receipt_initiation
    marker = 'jitodontfront1111111111111111111TradeonFomo'
    u = wire_receipt()
    i = u.transaction.transaction
    keys = [base58.b58encode(k).decode() for k in (
        list(i.transaction.message.account_keys) + list(i.meta.loaded_writable_addresses)
        + list(i.meta.loaded_readonly_addresses))]
    i.meta.loaded_readonly_addresses.append(base58.b58decode(marker))
    i.transaction.message.address_table_lookups[0].readonly_indexes = bytes([len(keys) - 10])
    i.meta.pre_balances.append(0)
    i.meta.post_balances.append(0)
    i.transaction.message.instructions.add(program_id_index=keys.index(SYSTEM),
        accounts=bytes([0, 0, len(keys)]), data=struct.pack('<IQ', 2, 0))
    tx = decode(u)
    assert tx is not None, 'a successful transfer may carry trailing read-only marker accounts'
    transfer = tx['transaction']['message']['instructions'][-1]
    assert transfer['accounts'] == [OWNER, OWNER, marker]
    assert transfer['parsed'] == dict(type='transfer', info=dict(
        source=OWNER, destination=OWNER, lamports=0))
    facts = value_source_buy(tx, OWNER, sol_usd=None, now=105)
    proof = solana_receipt_initiation(tx, OWNER, facts)
    assert proof['payer'] == OWNER and proof['payment_atomic'] == '500000000'
    assert proof['route_payment_atomic'] == '500000000'
    assert proof['block_number'] == FIX['slot']
    assert proof['tx_hash'] == FIX['transaction']['signatures'][0]


def test_system_create_account_trailing_marker_preserves_exact_payload():
    from sniper.services.memecoin_shyft_confirmed import SYSTEM
    from sniper.services.memecoin_source_valuation import value_source_buy
    from sniper.services.memecoin_source_initiation import solana_receipt_initiation
    marker = 'jitodontfront1111111111111111111TradeonFomo'
    u = wire_receipt()
    i = u.transaction.transaction
    keys = [base58.b58encode(k).decode() for k in (
        list(i.transaction.message.account_keys) + list(i.meta.loaded_writable_addresses)
        + list(i.meta.loaded_readonly_addresses))]
    i.meta.loaded_readonly_addresses.append(base58.b58decode(marker))
    i.transaction.message.address_table_lookups[0].readonly_indexes = bytes([len(keys) - 10])
    i.meta.pre_balances.append(0)
    i.meta.post_balances.append(0)
    group_index, instruction_index, original = next(
        (g.index, index, ix) for g in i.meta.inner_instructions
        for index, ix in enumerate(g.instructions)
        if keys[ix.program_id_index] == SYSTEM and ix.data[:4] == struct.pack('<I', 0))
    data = bytes(original.data)
    source, new_account = (keys[index] for index in original.accounts[:2])
    lamports, space = struct.unpack_from('<QQ', data, 4)
    account_owner = base58.b58encode(data[20:]).decode()
    assert len(data) == 52
    original.accounts += bytes([len(keys)])
    tx = decode(u)
    assert tx is not None, 'createAccount also permits trailing marker accounts'
    instruction = next(g for g in tx['meta']['innerInstructions']
                       if g['index'] == group_index)['instructions'][instruction_index]
    assert instruction['accounts'] == [source, new_account, marker]
    assert instruction['parsed'] == dict(type='createAccount', info=dict(
        source=source, newAccount=new_account, lamports=lamports, space=space, owner=account_owner))
    facts = value_source_buy(tx, OWNER, sol_usd=None, now=105)
    proof = solana_receipt_initiation(tx, OWNER, facts)
    assert proof['payer'] == OWNER and proof['route_payment_atomic'] == '500000000'


@pytest.mark.parametrize('tag', [0, 2])
@pytest.mark.parametrize('accounts', [b'', bytes([0]), bytes([0, 0, 255])])
def test_system_instructions_still_require_valid_source_destination_and_extra_indices(tag, accounts):
    from sniper.services.memecoin_shyft_confirmed import SYSTEM
    u = wire_receipt()
    i = u.transaction.transaction
    keys = [base58.b58encode(k).decode() for k in (
        list(i.transaction.message.account_keys) + list(i.meta.loaded_writable_addresses)
        + list(i.meta.loaded_readonly_addresses))]
    data = (struct.pack('<IQQ', 0, 1234, 165) + base58.b58decode(TOKEN)
            if tag == 0 else struct.pack('<IQ', 2, 0))
    i.transaction.message.instructions.add(program_id_index=keys.index(SYSTEM),
        accounts=accounts, data=data)
    assert decode(u) is None


@pytest.mark.parametrize('tag', [0, 2])
@pytest.mark.parametrize('length_delta', [-1, 1])
def test_system_instructions_reject_inexact_payload_with_extra_accounts(tag, length_delta):
    from sniper.services.memecoin_shyft_confirmed import SYSTEM
    u = wire_receipt()
    i = u.transaction.transaction
    keys = [base58.b58encode(k).decode() for k in (
        list(i.transaction.message.account_keys) + list(i.meta.loaded_writable_addresses)
        + list(i.meta.loaded_readonly_addresses))]
    data = (struct.pack('<IQQ', 0, 1234, 165) + base58.b58decode(TOKEN)
            if tag == 0 else struct.pack('<IQ', 2, 0))
    data = data[:-1] if length_delta < 0 else data + b'\x00'
    i.transaction.message.instructions.add(program_id_index=keys.index(SYSTEM),
        accounts=bytes([0, 0, 1]), data=data)
    assert decode(u) is None


@pytest.mark.parametrize('bad', ['signature', 'signature_mismatch', 'meta', 'error', 'key',
    'header', 'balances', 'token_amount', 'token_decimals', 'token_owner', 'token_index',
    'instruction_index', 'inner_index', 'logs', 'vote', 'slot'])
def test_malformed_partial_or_failed_metadata_is_not_authority(bad):
    u = wire_receipt(); i = u.transaction.transaction
    if bad == 'signature': i.signature = b'bad'
    elif bad == 'signature_mismatch': i.transaction.signatures[0] = bytes(64)
    elif bad == 'meta': i.ClearField('meta')
    elif bad == 'error': i.meta.err.err = b'failed'
    elif bad == 'key': i.meta.loaded_writable_addresses[0] = b'bad'
    elif bad == 'header': i.transaction.message.ClearField('header')
    elif bad == 'balances': del i.meta.pre_balances[-1]
    elif bad == 'token_amount': i.meta.post_token_balances[0].ui_token_amount.amount = '-1'
    elif bad == 'token_decimals': i.meta.post_token_balances[0].ui_token_amount.decimals = 99
    elif bad == 'token_owner': i.meta.post_token_balances[0].owner = 'bad'
    elif bad == 'token_index': i.meta.post_token_balances[0].account_index = 255
    elif bad == 'instruction_index': i.transaction.message.instructions[0].program_id_index = 255
    elif bad == 'inner_index': i.meta.inner_instructions[0].index = 255
    elif bad == 'logs': i.meta.log_messages_none = True
    elif bad == 'vote': i.is_vote = True
    elif bad == 'slot': u.transaction.slot = 0
    assert decode(u) is None


def test_owner_in_loaded_keys_is_not_a_signer_and_stale_head_cannot_authorize():
    u = wire_receipt()
    other = base58.b58encode(u.transaction.transaction.meta.loaded_writable_addresses[0]).decode()
    assert decode(u, {other: WATCH[OWNER]}) is None
    from sniper.services.memecoin_shyft_confirmed import confirmed_transaction
    assert confirmed_transaction(u, WATCH, head_slot=u.transaction.slot + 3,
        recv_monotonic=1, head_monotonic=1) is None
    assert confirmed_transaction(u, WATCH, head_slot=u.transaction.slot,
        recv_monotonic=4, head_monotonic=1) is None


def test_stream_transaction_uses_existing_writer_proof_without_http_and_preserves_receipt_clock(monkeypatch):
    from scripts.research import onchain_wallet_watch as w
    tx = decode(wire_receipt())
    monkeypatch.setattr(w, '_http_rpc', lambda *a: pytest.fail('confirmed stream called HTTP'))
    monkeypatch.setattr(w, '_stage_write', lambda *a: None)
    monkeypatch.setattr(w, 'get_readonly_connection', lambda *a: sqlite3.connect(':memory:'))
    monkeypatch.setattr(w, 'match_feed', lambda *a: (None, None))
    wakes = []; monkeypatch.setattr(w, 'send_wake', wakes.append)
    c = sqlite3.connect(':memory:'); c.row_factory = sqlite3.Row; c.executescript(w.SCHEMA)
    w.process_transaction(tx['transaction']['signatures'][0], OWNER, WATCH[OWNER], c,
        tx=tx, frame_mono=1, detected_at='1970-01-01T00:01:45+00:00', detected_epoch=105,
        signal_feed='shyft_confirmed:test', commitment='confirmed')
    w.process_transaction(tx['transaction']['signatures'][0], OWNER, WATCH[OWNER], c,
        tx=tx, frame_mono=1, signal_feed='helius', commitment='confirmed')
    assert c.execute('SELECT count(*) FROM onchain_wallet_detections').fetchone()[0] == 1
    row = c.execute('SELECT * FROM onchain_wallet_detections').fetchone()
    facts = json.loads(row['source_valuation_json'])
    assert facts['source_initiation']['source_user_id'] == 'u1'
    assert facts['source_clock_provenance'] == 'confirmed_shyft_grpc'
    assert row['detected_at'] == '1970-01-01T00:01:45+00:00'
    assert row['detect_latency_s'] is None and row['block_time'] is None
    assert len(wakes) == 1


def test_decode_worker_is_bounded_and_receipts_are_off_the_event_loop():
    from sniper.services.memecoin_shyft_confirmed import ReceiptWorker
    entered, release = threading.Event(), threading.Event()
    ids = []
    def consume(*a):
        ids.append(threading.get_ident()); entered.set(); release.wait(1)
    worker = ReceiptWorker(consume, maxsize=1)
    try:
        assert worker.offer(wire_receipt(), WATCH, head_slot=FIX['slot'], head_monotonic=1,
            recv_monotonic=1, recv_utc='receipt', recv_epoch=105, signal_feed='test')
        assert entered.wait(1)
        assert worker.offer(wire_receipt(), WATCH, head_slot=FIX['slot'], head_monotonic=1,
            recv_monotonic=1, recv_utc='receipt', recv_epoch=105, signal_feed='test')
        assert not worker.offer(wire_receipt(), WATCH, head_slot=FIX['slot'], head_monotonic=1,
            recv_monotonic=1, recv_utc='receipt', recv_epoch=105, signal_feed='test')
        assert worker.stats['overflow'] == 1
        assert ids[0] != threading.get_ident()
    finally:
        release.set(); worker.close()


@pytest.mark.parametrize('result', [None, False, 1, 'accepted'])
def test_decode_worker_never_suppresses_http_without_explicit_verified_acceptance(result):
    from sniper.services.memecoin_shyft_confirmed import ReceiptWorker
    worker = ReceiptWorker(lambda *args: result)
    try:
        assert worker.offer(wire_receipt(), WATCH, head_slot=FIX['slot'], head_monotonic=1,
            recv_monotonic=1, recv_utc='receipt', recv_epoch=105, signal_feed='test')
        worker._jobs.join()
        assert not worker.has_receipt(FIX['transaction']['signatures'][0], OWNER)
        assert worker.stats['decoded'] == 0
    finally:
        worker.close()


def _stream_rows(monkeypatch, tx):
    from scripts.research import onchain_wallet_watch as w
    monkeypatch.setattr(w, '_stage_write', lambda *a: None)
    c = sqlite3.connect(':memory:'); c.row_factory = sqlite3.Row; c.executescript(w.SCHEMA)
    ok = w.process_transaction(tx['transaction']['signatures'][0], OWNER, WATCH[OWNER], c,
        tx=tx, require_initiation=True, signal_feed='shyft_confirmed:test')
    return ok, [dict(r) for r in c.execute('SELECT * FROM onchain_wallet_detections')]


@pytest.mark.parametrize('tag', [7, 8, 26])            # MintTo, Burn, an unassigned tag
@pytest.mark.parametrize('leg', ['payment_transfer', 'setup_instruction'])
def test_an_unsupported_compiled_route_instruction_cannot_take_http_fallbacks_unique_row(
        monkeypatch, leg, tag):
    # The stream keeps an instruction it cannot decode raw. Any token instruction no decoder can
    # read, on the payment path or beside it (the receipt's GetAccountDataSize), must refuse
    # before the insert, so the Helius HTTP receipt can still win the signature row.
    tx = decode(wire_receipt())
    inner = [ix for g in tx['meta']['innerInstructions'] for ix in g['instructions']]
    if leg == 'payment_transfer':
        ix = next(ix for ix in inner if (ix.get('parsed') or {}).get('type') == 'transferChecked')
        ix.pop('parsed'); ix.pop('program', None)
    else:
        ix = next(ix for ix in inner if ix.get('programId') == TOKEN and 'parsed' not in ix)
    ix['data'] = base58.b58encode(bytes([tag]) + struct.pack('<Q', 1)).decode()
    ok, rows = _stream_rows(monkeypatch, tx)
    assert ok is False and rows == []


def test_a_decodable_compiled_route_proves_the_same_row_as_its_parsed_form(monkeypatch):
    # Since c9c57f2f8 the route valuation decodes compiled token/system/ATA instructions with
    # the stream's own decoder (_route_instruction -> _instruction), so a receipt whose inner
    # instructions carry no `parsed` is not an unknown route: it must give the parsed form's row.
    # (Before c9c57f2f8 this shape was refused; that test asserted the gap, not the rule.)
    clocks = {'id', 'detected_at', 'created_at', 'detected_epoch'}
    parsed_ok, parsed_rows = _stream_rows(monkeypatch, decode(wire_receipt()))
    tx = decode(wire_receipt())
    for group in tx['meta']['innerInstructions']:
        for ix in group['instructions']:
            ix.pop('parsed', None)
    ok, rows = _stream_rows(monkeypatch, tx)
    assert parsed_ok is True and ok is True and len(rows) == len(parsed_rows) == 1
    assert ({k: v for k, v in rows[0].items() if k not in clocks}
            == {k: v for k, v in parsed_rows[0].items() if k not in clocks})


def test_actual_confirmed_wire_request_full_metadata_and_error_are_isolated(monkeypatch):
    from sniper.services import memecoin_shyft_confirmed as g
    import grpc
    offers, requests, status, stop = [], [], {}, threading.Event()
    class Worker:
        def offer(self, *args, **kwargs):
            offers.append((args, kwargs)); stop.set(); return True
    async def exercise():
        async def subscribe(incoming, context):
            assert dict(context.invocation_metadata())['x-token'] == 'test-token'
            async for req in incoming:
                requests.append(req)
                if req.HasField('ping'):
                    pong = pb.SubscribeUpdate(); pong.pong.id = req.ping.id; yield pong
                    head = pb.SubscribeUpdate(); head.slot.slot = FIX['slot']
                    head.slot.status = pb.SLOT_CONFIRMED; yield head
                    yield wire_receipt()
        server = grpc.aio.server()
        server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler('geyser.Geyser', {
            'Subscribe': grpc.stream_stream_rpc_method_handler(subscribe,
                request_deserializer=pb.SubscribeRequest.FromString,
                response_serializer=pb.SubscribeUpdate.SerializeToString)}),))
        port = server.add_insecure_port('127.0.0.1:0')
        monkeypatch.setattr(grpc.aio, 'secure_channel', lambda *a, **k:
            grpc.aio.insecure_channel(f'127.0.0.1:{port}'))
        await server.start()
        try:
            await asyncio.wait_for(g.run_feed('https://beta-grpc.fra.shyft.to', 'test-token',
                lambda: WATCH, Worker(), status, stop), 3)
        finally:
            await server.stop(0)
    asyncio.run(exercise())
    assert requests[0].commitment == pb.CONFIRMED
    assert requests[0].transactions['owners'].account_include == [OWNER]
    assert len(offers) == 1
    assert offers[0][1]['signal_feed'] == 'shyft_confirmed:beta-grpc.fra.shyft.to'
    assert status['last_error'] is None and not status['connected']


def test_paid_receipt_failure_keeps_helius_fallback_and_success_skips_http(monkeypatch):
    from scripts.research import onchain_wallet_watch as w
    calls = []
    monkeypatch.setattr(w, '_http_rpc', lambda *a: calls.append(a) or None)
    monkeypatch.setattr(w, '_stage_write', lambda *a: None)
    asyncio.run(w.process_signature('sig', OWNER, WATCH[OWNER], object(),
        confirmed_seen=lambda s, o, uid: False))
    assert len(calls) == 1
    asyncio.run(w.process_signature('sig', OWNER, WATCH[OWNER], object(),
        confirmed_seen=lambda s, o, uid: True))
    assert len(calls) == 1


def test_repeated_slots_without_pong_do_not_bypass_subscription_ack_deadline(monkeypatch):
    from sniper.services import memecoin_shyft_confirmed as g
    from types import SimpleNamespace
    import grpc
    clock, stop, status = [0], threading.Event(), {}
    class Stream:
        def __aiter__(self): return self
        async def __anext__(self):
            clock[0] += 4
            if clock[0] >= 16: stop.set()
            head = pb.SubscribeUpdate(); head.slot.slot = FIX['slot']; head.slot.status = pb.SLOT_CONFIRMED
            return head
        def cancel(self): pass
    class Channel:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def stream_stream(self, *a, **kw): return lambda *a, **kw: Stream()
    class Worker:
        def offer(self, *a, **kw): pytest.fail('unacknowledged stream emitted')
    async def no_wait(*a): pass
    monkeypatch.setattr(grpc.aio, 'secure_channel', lambda *a, **kw: Channel())
    monkeypatch.setattr(g, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(g.asyncio, 'sleep', no_wait)
    asyncio.run(g.run_feed('https://beta-grpc.fra.shyft.to', 'test-token', lambda: WATCH,
        Worker(), status, stop))
    assert status['last_error'] == 'subscription_ack_timeout'


@pytest.mark.parametrize('env,reason', [({}, None),
    ({'MEMECOIN_SHYFT_LIVE_ENABLED': '1'}, 'shyft token_missing')])
def test_disabled_or_missing_token_starts_no_socket(monkeypatch, env, reason):
    from scripts.research import onchain_wallet_watch as w
    for name in ('MEMECOIN_SHYFT_LIVE_ENABLED', 'MEMECOIN_SHYFT_GRPC_TOKEN'):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items(): monkeypatch.setenv(name, value)
    group = w.start_confirmed_feed(lambda: WATCH, object(), {})
    assert not group['tasks'] and group['worker'] is None
    assert group['status']['last_error'] == reason


def test_watcher_launches_confirmed_regions_and_processor_with_original_clock(monkeypatch):
    from scripts.research import onchain_wallet_watch as w
    from sniper.services import memecoin_shyft_confirmed as g
    monkeypatch.setenv('MEMECOIN_SHYFT_LIVE_ENABLED', '1')
    monkeypatch.setenv('MEMECOIN_SHYFT_GRPC_TOKEN', 'test-secret')
    monkeypatch.setenv('MEMECOIN_SHYFT_GRPC_URLS', 'https://beta-grpc.fra.shyft.to')
    monkeypatch.setattr(w, '_receipt_utc', lambda mono: 'corrected-receipt')
    processed = []; done = threading.Event()
    def process(*args, **kw):
        processed.append((args, kw)); done.set(); return True
    monkeypatch.setattr(w, 'process_transaction', process)
    async def fake_feed(url, token, owners_fn, worker, status, stop):
        assert token == 'test-secret' and owners_fn() == WATCH
        worker.offer(wire_receipt(), owners_fn(), head_slot=FIX['slot'], head_monotonic=1,
            recv_monotonic=1, recv_utc='receipt', recv_epoch=105, signal_feed='shyft_confirmed:test')
        await asyncio.to_thread(done.wait, 1)
    monkeypatch.setattr(g, 'run_feed', fake_feed)
    async def exercise():
        group = w.start_confirmed_feed(lambda: WATCH, object(), {'snapshot': (120, 100)})
        try:
            await asyncio.gather(*group['tasks'])
            assert group['worker'].has_receipt(FIX['transaction']['signatures'][0], OWNER)
            assert set(group['status']['feeds']) == {'beta-grpc.fra.shyft.to'}
            assert 'test-secret' not in json.dumps(group['status'])
        finally:
            group['stop'].set(); group['worker'].close()
    asyncio.run(exercise())
    assert len(processed) == 1
    _, kwargs = processed[0]
    assert kwargs['detected_at'] == 'corrected-receipt' and kwargs['detected_epoch'] == 105
    assert kwargs['require_initiation'] is True and kwargs['commitment'] == 'confirmed'
    assert kwargs['sol_usd'] == (120, 100)


@pytest.mark.parametrize('bad', ['lookup_missing', 'lookup_counts', 'lookup_key', 'legacy_loaded',
    'cross_owner', 'cross_mint', 'cross_program', 'cross_decimals', 'quote_decimals',
    'checked_decimals', 'unsupported_token', 'empty_token'])
def test_partial_lookup_or_token_identity_never_becomes_existing_payment_proof(bad):
    u = wire_receipt(); i = u.transaction.transaction; m = i.transaction.message
    pre, post = i.meta.pre_token_balances[0], i.meta.post_token_balances[0]
    if bad == 'lookup_missing': m.ClearField('address_table_lookups')
    elif bad == 'lookup_counts': m.address_table_lookups[0].writable_indexes = b''
    elif bad == 'lookup_key': m.address_table_lookups[0].account_key = b'bad'
    elif bad == 'legacy_loaded': m.versioned = False
    elif bad == 'cross_owner': post.owner = FIX['meta']['postTokenBalances'][-1]['owner']
    elif bad == 'cross_mint': post.mint = FIX['meta']['postTokenBalances'][-1]['mint']
    elif bad == 'cross_program': post.program_id = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'
    elif bad == 'cross_decimals': post.ui_token_amount.decimals += 1
    elif bad == 'quote_decimals':
        for row in list(i.meta.pre_token_balances) + list(i.meta.post_token_balances):
            if row.mint == 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v':
                row.ui_token_amount.decimals = 9
    elif bad == 'checked_decimals':
        for group in i.meta.inner_instructions:
            for ix in group.instructions:
                if ix.data[:1] == bytes([12]):
                    ix.data = ix.data[:-1] + bytes([18])
    else:
        keys = list(m.account_keys) + list(i.meta.loaded_writable_addresses)
        ix = i.meta.inner_instructions[0].instructions.add()
        ix.program_id_index = keys.index(base58.b58decode(TOKEN))
        ix.accounts = bytes([0])
        ix.data = b'' if bad == 'empty_token' else bytes([26, 1]) + bytes(17)
    assert decode(u) is None


def test_neutral_metadata_parser_retains_shape_without_granting_commitment():
    from sniper.services.memecoin_shyft_confirmed import transaction_metadata
    tx = transaction_metadata(wire_receipt(), WATCH, head_slot=FIX['slot'],
        recv_monotonic=1, head_monotonic=1)
    assert tx['meta']['err'] is None
    assert 'source_initiation' not in tx and 'commitment' not in tx


@pytest.mark.parametrize('fixture_name,route_payment,whole_payment', [
    ('sol-route-dflow-4rTe5hhH.json', 527041467, 529686722),
    ('sol-route-provf4-27MRyWXY.json', 2995651575, 3000000000)])
def test_second_signer_real_split_route_keeps_exact_existing_payment(fixture_name, route_payment, whole_payment):
    from sniper.services.memecoin_source_valuation import value_source_buy
    from sniper.services.memecoin_source_initiation import solana_receipt_initiation
    fixture = json.loads((Path(__file__).parent / 'fixtures/memecoin' / fixture_name).read_text())
    owner = fixture['transaction']['message']['accountKeys'][1]['pubkey']
    tx = decode(wire_receipt(fixture), {owner: WATCH[OWNER]})
    assert tx is not None
    facts = value_source_buy(tx, owner, sol_usd=None, now=105)
    proof = solana_receipt_initiation(tx, owner, facts)
    assert proof['payer'] == owner
    assert proof['route_payment_atomic'] == str(route_payment)
    assert proof['payment_atomic'] == str(whole_payment)


def test_async_receipt_only_suppresses_http_after_durable_callback_and_failure_can_retry():
    from sniper.services.memecoin_shyft_confirmed import ReceiptWorker
    callbacks = []
    def consume(tx, owner, info, receipt):
        callbacks.append(receipt['on_persisted'])
        return None
    worker = ReceiptWorker(consume)
    r = dict(head_slot=FIX['slot'], head_monotonic=1, recv_monotonic=1,
             recv_utc='receipt', recv_epoch=105, signal_feed='test')
    sig = FIX['transaction']['signatures'][0]
    try:
        worker.offer(wire_receipt(), WATCH, **r); worker._jobs.join()
        assert len(callbacks) == 1 and not worker.has_receipt(sig, OWNER)
        worker.offer(wire_receipt(), WATCH, **r); worker._jobs.join()
        assert len(callbacks) == 1  # pending duplicate cannot enqueue a second writer job
        callbacks.pop()(False)
        worker.offer(wire_receipt(), WATCH, **r); worker._jobs.join()
        assert len(callbacks) == 1 and not worker.has_receipt(sig, OWNER)
        callbacks[0](True)
        assert worker.has_receipt(sig, OWNER)
        callbacks[0](False)  # completion is exactly once
        assert worker.has_receipt(sig, OWNER)
    finally:
        worker.close()


def test_durable_callback_before_consume_returns_is_retained():
    from sniper.services.memecoin_shyft_confirmed import ReceiptWorker
    def consume(tx, owner, info, receipt):
        receipt['on_persisted'](True)
        return None
    worker = ReceiptWorker(consume)
    try:
        worker.offer(wire_receipt(), WATCH, head_slot=FIX['slot'], head_monotonic=1,
            recv_monotonic=1, recv_utc='receipt', recv_epoch=105, signal_feed='test')
        worker._jobs.join()
        assert worker.has_receipt(FIX['transaction']['signatures'][0], OWNER)
    finally:
        worker.close()


def test_receipt_acceptance_is_bound_to_the_current_source_user_id():
    from sniper.services.memecoin_shyft_confirmed import ReceiptWorker
    worker = ReceiptWorker(lambda *args: True)
    try:
        worker.offer(wire_receipt(), WATCH, head_slot=FIX['slot'], head_monotonic=1,
            recv_monotonic=1, recv_utc='receipt', recv_epoch=105, signal_feed='test')
        worker._jobs.join()
        sig = FIX['transaction']['signatures'][0]
        assert worker.has_receipt(sig, OWNER, 'u1')
        assert not worker.has_receipt(sig, OWNER, 'new-user')
    finally:
        worker.close()


def test_metadata_without_any_logs_cannot_prepare_a_route():
    u = wire_receipt(); u.transaction.transaction.meta.ClearField('log_messages')
    assert decode(u) is None


@pytest.mark.parametrize('side', ['pre', 'post'])
def test_missing_existing_token_balance_side_is_unknown_not_zero(side):
    fixture = json.loads((Path(__file__).parent / 'fixtures/memecoin/sol-route-dflow-4rTe5hhH.json').read_text())
    owner = fixture['transaction']['message']['accountKeys'][1]['pubkey']
    u = wire_receipt(fixture)
    rows = getattr(u.transaction.transaction.meta, side + '_token_balances')
    index = next(j for j, b in enumerate(rows) if b.owner == owner
        and b.mint == 'BQxzR4pLkDw46gzbrkRjFuUSsgvN5BhTWHh6EXUwf3F')
    del rows[index]
    assert decode(u, {owner: WATCH[OWNER]}) is None


@pytest.mark.parametrize('bad', ['init_missing', 'prior_lamports'])
def test_post_only_inventory_requires_matching_initialization_and_no_prior_account(bad):
    u = wire_receipt(); i = u.transaction.transaction
    new_index = next(b.account_index for b in i.meta.post_token_balances
        if b.account_index not in {p.account_index for p in i.meta.pre_token_balances})
    if bad == 'prior_lamports':
        i.meta.pre_balances[new_index] = 1
    else:
        for group in i.meta.inner_instructions:
            for j in reversed(range(len(group.instructions))):
                ix = group.instructions[j]
                if ix.data[:1] == bytes([18]) and ix.accounts[0] == new_index:
                    del group.instructions[j]
    assert decode(u) is None


def test_pre_only_closed_account_can_use_zero_only_after_matching_close_and_no_final_account():
    u = wire_receipt(); i = u.transaction.transaction; m = i.transaction.message
    keys = list(m.account_keys) + list(i.meta.loaded_writable_addresses) + list(i.meta.loaded_readonly_addresses)
    row = next(b for b in i.meta.pre_token_balances if b.owner == OWNER
        and b.mint == 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v')
    index = row.account_index
    j = next(j for j, b in enumerate(i.meta.post_token_balances) if b.account_index == index)
    del i.meta.post_token_balances[j]
    i.meta.post_balances[index] = 0
    ix = i.meta.inner_instructions[0].instructions.add(program_id_index=keys.index(base58.b58decode(TOKEN)),
        accounts=bytes([index, 0, 0]), data=bytes([9]), stack_height=2)
    assert decode(u) is not None
    i.meta.post_balances[index] = 1  # closure followed by recreation remains unknown
    assert decode(u) is None


@pytest.mark.parametrize('endpoint', ['source', 'destination'])
def test_both_missing_balance_rows_cannot_hide_existing_transfer_inventory(endpoint):
    u = wire_receipt(); i = u.transaction.transaction; m = i.transaction.message
    keys = list(m.account_keys) + list(i.meta.loaded_writable_addresses)
    old_index = len(keys)
    i.meta.loaded_writable_addresses.append(bytes([85]) * 32)
    m.address_table_lookups[0].writable_indexes += bytes([old_index - len(m.account_keys)])
    i.meta.pre_balances.append(1488440)
    i.meta.post_balances.append(1488440)
    coin = next(b for b in i.meta.post_token_balances if b.owner == OWNER
        and b.account_index not in {p.account_index for p in i.meta.pre_token_balances})
    src, dst = ((old_index, coin.account_index) if endpoint == 'source'
                else (coin.account_index, old_index))
    i.meta.inner_instructions[0].instructions.add(
        program_id_index=keys.index(base58.b58decode(coin.program_id)),
        accounts=bytes([src, keys.index(base58.b58decode(coin.mint)), dst, 0]),
        data=bytes([12]) + struct.pack('<Q', 1000) + bytes([coin.ui_token_amount.decimals]))
    assert decode(u) is None


@pytest.mark.parametrize('bad', ['initialize', 'close', 'pre_lamports', 'post_lamports', 'mint'])
def test_unreported_temporary_transfer_account_requires_exact_lifecycle(bad):
    u = wire_receipt(); i = u.transaction.transaction; m = i.transaction.message
    keys = list(m.account_keys) + list(i.meta.loaded_writable_addresses)
    reported = {b.account_index for b in list(i.meta.pre_token_balances) + list(i.meta.post_token_balances)}
    token_index = keys.index(base58.b58decode(TOKEN))
    temporary = next(ix.accounts[0] for g in i.meta.inner_instructions for ix in g.instructions
        if ix.program_id_index == token_index and ix.data[:1] == bytes([18])
        and ix.accounts[0] not in reported)
    assert decode(u) is not None  # Real transient WSOL has neither balance row.
    if bad in ('pre_lamports', 'post_lamports'):
        getattr(i.meta, bad.replace('_lamports', '_balances'))[temporary] = 1
    elif bad == 'mint':
        ix = next(ix for g in i.meta.inner_instructions for ix in g.instructions
            if ix.program_id_index == token_index and ix.data[:1] == bytes([18])
            and ix.accounts[0] == temporary)
        ix.accounts = bytes([temporary, 0])
    else:
        tag = 18 if bad == 'initialize' else 9
        for g in i.meta.inner_instructions:
            for j in reversed(range(len(g.instructions))):
                ix = g.instructions[j]
                if ix.program_id_index == token_index and ix.data[:1] == bytes([tag]) and ix.accounts[0] == temporary:
                    del g.instructions[j]
    assert decode(u) is None


def shyft_env(monkeypatch):
    monkeypatch.setenv('MEMECOIN_SOLANA_INFRA_PROVIDER', 'shyft')
    monkeypatch.setenv('MEMECOIN_SHYFT_LIVE_ENABLED', '1')
    monkeypatch.setenv('MEMECOIN_SHYFT_GRPC_TOKEN', 'test-token')
    monkeypatch.setenv('MEMECOIN_SHYFT_GRPC_URLS',
                       'https://grpc.ams.shyft.to,https://grpc.fra.shyft.to')
    monkeypatch.setenv('ONCHAIN_WATCH_SOLANA_RPC_URL',
                       'https://rpc.ams.shyft.to?api_key=test-http-key')
    for name in ('ONCHAIN_WATCH_HELIUS_RPC_URL', 'MEMECOIN_RESEARCH_HELIUS_RPC_URL',
                 'MEMECOIN_RESEARCH_HELIUS_RPC_URLS', 'MEMECOIN_HELIUS_EVIDENCE_WS_URL'):
        monkeypatch.delenv(name, raising=False)


def test_shyft_only_watcher_starts_without_legacy_and_recovers_one_exact_receipt(
        monkeypatch, tmp_path):
    from scripts.research import onchain_wallet_watch as w
    from sniper.services import memecoin_shyft_confirmed as g, memecoin_helius_evidence as h
    from sniper.util import envfiles
    shyft_env(monkeypatch)
    monkeypatch.setattr(envfiles, 'load_env_files', lambda *_: None)
    monkeypatch.setattr(w, 'STATE_ROOT', tmp_path)
    (tmp_path / '.env').write_text('')
    monkeypatch.setattr(w, 'DB', tmp_path / 'watch.db')
    monkeypatch.setattr(w, 'HEARTBEAT', tmp_path / 'beat.json')
    monkeypatch.setattr(w, 'READY', tmp_path / 'ready')
    monkeypatch.setattr(w, 'desired_watchlist', lambda: (WATCH, 0))
    monkeypatch.setattr(w, '_stage_write', lambda *_: None)
    monkeypatch.setattr(w, 'HTTP_URL', None)
    monkeypatch.setattr(w, 'WS_URL', None)
    monkeypatch.setattr(w, 'KEY', None)
    monkeypatch.setattr(h, 'live_config', lambda: pytest.fail('Shyft started Helius evidence'))
    monkeypatch.setattr(w.websockets, 'connect', lambda *a, **k:
                        pytest.fail('Shyft started legacy logsSubscribe'))
    async def refresh(_cache):
        await asyncio.Event().wait()
    monkeypatch.setattr(w, '_refresh_sol_usd', refresh)
    recovered = copy.deepcopy(FIX)
    calls, wakes, receipts, clock_reads = [], [], [], []
    clock_stamp = '2026-10-12T09:00:00.000000+00:00'
    monkeypatch.setattr(w, '_receipt_utc', lambda mono: clock_reads.append(mono) or clock_stamp)
    monkeypatch.setattr(w, 'send_wake', wakes.append)
    def rpc(method, params, stats=None):
        calls.append((method, params, w.HTTP_URL, threading.get_ident()))
        return recovered
    monkeypatch.setattr(w, '_http_rpc', rpc)
    async def feed(url, token, owners_fn, worker, status, stop):
        owners = owners_fn()
        status.update(connected=True, acknowledged_owners=tuple(sorted(owners)),
                      requested_owners=tuple(sorted(owners)), head_slot=FIX['slot'],
                      head_monotonic=time.monotonic())
        if 'ams' in url:
            u = wire_receipt()
            u.transaction.transaction.meta.inner_instructions[0].instructions.add(
                program_id_index=next(j for j, raw in enumerate(
                    list(u.transaction.transaction.transaction.message.account_keys) +
                    list(u.transaction.transaction.meta.loaded_writable_addresses))
                    if base58.b58encode(raw).decode() == TOKEN), accounts=b'\x00', data=b'\xff')
            mono = time.monotonic()
            receipts.append(mono)
            assert worker.offer(u, owners, head_slot=FIX['slot'], head_monotonic=mono,
                recv_monotonic=mono, recv_utc='unused-host-clock', recv_epoch=time.time(),
                signal_feed='shyft_confirmed:grpc.ams.shyft.to')
            await asyncio.to_thread(worker._jobs.join)
        while not stop.is_set():
            await asyncio.sleep(.01)
    monkeypatch.setattr(g, 'run_feed', feed)
    asyncio.run(w.run(duration_s=.2))
    with sqlite3.connect(w.DB) as conn:
        rows = conn.execute('SELECT signature,source_slot,source_valuation_json,detected_at '
                            'FROM onchain_wallet_detections').fetchall()
    assert len(rows) == len(wakes) == len(calls) == 1
    assert rows[0][:2] == (FIX['transaction']['signatures'][0], FIX['slot'])
    assert json.loads(rows[0][2])['source_clock_provenance'] == 'confirmed_shyft_grpc'
    assert rows[0][3] == clock_stamp and clock_reads == receipts
    assert calls[0][0] == 'getTransaction'
    assert calls[0][1][1]['commitment'] == 'confirmed'
    assert calls[0][2] == 'https://rpc.ams.shyft.to?api_key=test-http-key'
    assert calls[0][3] != threading.get_ident()
    beat = json.loads(w.HEARTBEAT.read_text())
    assert beat['subs'] == beat['subs_desired'] == 1
    assert set(beat['shyft_confirmed']['feeds']) == {'grpc.ams.shyft.to', 'grpc.fra.shyft.to'}
    assert w.READY.exists()


@pytest.mark.parametrize('damage', ['signature', 'failed', 'stale_head', 'nonsigner'])
def test_shyft_recovery_never_reads_for_untrusted_wire_identity(monkeypatch, damage):
    from scripts.research import onchain_wallet_watch as w
    shyft_env(monkeypatch)
    monkeypatch.setattr(w, '_http_rpc', lambda *a: pytest.fail('untrusted wire read HTTP'))
    u = wire_receipt()
    head = FIX['slot']
    if damage == 'signature': u.transaction.transaction.transaction.signatures[0] = bytes(64)
    elif damage == 'failed': u.transaction.transaction.meta.err.err = b'failed'
    elif damage == 'stale_head': head += 151
    else:
        u.transaction.transaction.transaction.message.header.num_required_signatures = 0
    assert w._shyft_receipt(u, WATCH, head_slot=head,
        recv_monotonic=1, head_monotonic=1) is None


@pytest.mark.parametrize('damage', ['signature', 'slot', 'failed', 'nonsigner'])
def test_shyft_http_recovery_requires_exact_confirmed_identity(monkeypatch, damage):
    from scripts.research import onchain_wallet_watch as w
    shyft_env(monkeypatch)
    u = wire_receipt()
    u.transaction.transaction.meta.ClearField('log_messages')
    tx = decode(wire_receipt())
    if damage == 'signature': tx['transaction']['signatures'][0] = 'another'
    elif damage == 'slot': tx['slot'] += 1
    elif damage == 'failed': tx['meta']['err'] = {'InstructionError': [1, 'failed']}
    else: tx['transaction']['message']['header']['numRequiredSignatures'] = 0
    monkeypatch.setattr(w, '_http_rpc', lambda *a: tx)
    assert w._shyft_receipt(u, WATCH, head_slot=FIX['slot'],
        recv_monotonic=1, head_monotonic=1) is None


@pytest.mark.parametrize('damage', ['unknown_pong', 'withdrawn', 'processed_head', 'dead_head'])
def test_confirmed_stream_never_offers_without_current_roster_and_usable_head(monkeypatch, damage):
    from sniper.services import memecoin_shyft_confirmed as g
    import grpc
    stop, status, offers = threading.Event(), {}, []
    owners = [WATCH]
    class Worker:
        def offer(self, *a, **kw): offers.append((a, kw)); return True
    async def exercise():
        async def subscribe(incoming, _context):
            async for req in incoming:
                if req.HasField('ping'):
                    pong = pb.SubscribeUpdate()
                    pong.pong.id = req.ping.id + (100 if damage == 'unknown_pong' else 0)
                    yield pong
                    head = pb.SubscribeUpdate(); head.slot.slot = FIX['slot']
                    head.slot.status = pb.SLOT_PROCESSED if damage == 'processed_head' else pb.SLOT_CONFIRMED
                    yield head
                    if damage == 'dead_head':
                        dead = pb.SubscribeUpdate(); dead.slot.slot = FIX['slot']; dead.slot.status = pb.SLOT_DEAD
                        yield dead
                        older = pb.SubscribeUpdate(); older.slot.slot = FIX['slot'] - 1
                        older.slot.status = pb.SLOT_CONFIRMED; yield older
                    if damage == 'withdrawn': owners[0] = {}
                    receipt = wire_receipt()
                    if damage == 'dead_head': receipt.transaction.slot += 1
                    yield receipt
                    await asyncio.sleep(.03)
                    stop.set()
                    return
        server = grpc.aio.server()
        server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler('geyser.Geyser', {
            'Subscribe': grpc.stream_stream_rpc_method_handler(subscribe,
                request_deserializer=pb.SubscribeRequest.FromString,
                response_serializer=pb.SubscribeUpdate.SerializeToString)}),))
        port = server.add_insecure_port('127.0.0.1:0')
        monkeypatch.setattr(grpc.aio, 'secure_channel', lambda *a, **k:
                            grpc.aio.insecure_channel(f'127.0.0.1:{port}'))
        await server.start()
        try:
            await asyncio.wait_for(g.run_feed('https://grpc.ams.shyft.to', 'test-token',
                lambda: owners[0], Worker(), status, stop), 3)
        finally:
            await server.stop(0)
    asyncio.run(exercise())
    assert offers == []


@pytest.mark.parametrize('damage', ['missing_depth', 'invalid_depth', 'duplicate_group'])
def test_incomplete_inner_tree_has_no_wire_receipt_authority(damage):
    u = wire_receipt()
    meta = u.transaction.transaction.meta
    if damage == 'duplicate_group':
        meta.inner_instructions.add().CopyFrom(meta.inner_instructions[0])
    elif damage == 'missing_depth':
        meta.inner_instructions[0].instructions[0].ClearField('stack_height')
    else:
        meta.inner_instructions[0].instructions[0].stack_height = 0
    assert decode(u) is None


def test_processed_metadata_cannot_publish_confirmed_proof(monkeypatch):
    from scripts.research import onchain_wallet_watch as w
    monkeypatch.setattr(w, '_stage_write', lambda *_: None)
    tx = decode(wire_receipt())
    c = sqlite3.connect(':memory:'); c.executescript(w.SCHEMA)
    try:
        assert w.process_transaction(tx['transaction']['signatures'][0], OWNER, WATCH[OWNER], c,
            tx=tx, commitment='processed', require_initiation=True) is False
        assert c.execute('SELECT count(*) FROM onchain_wallet_detections').fetchone()[0] == 0
    finally:
        c.close()


def test_shyft_http_null_is_one_bounded_read_without_retry(monkeypatch):
    from scripts.research import onchain_wallet_watch as w
    shyft_env(monkeypatch)
    monkeypatch.setattr(w, 'HTTP_URL', 'https://rpc.ams.shyft.to?api_key=test-http-key')
    timeouts = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'{"jsonrpc":"2.0","id":1,"result":null}'
    def opening(_req, timeout):
        timeouts.append(timeout)
        return Response()
    monkeypatch.setattr(w.urllib.request, 'urlopen', opening)
    monkeypatch.setattr(w, '_cooldown_until', lambda: 0)
    monkeypatch.setattr(w.time, 'sleep', lambda *_: pytest.fail('Shyft recovery retried'))
    assert w._http_rpc('getTransaction', ['test-signature', {'commitment': 'confirmed'}]) is None
    assert timeouts == [2.0]


def test_anonymized_kraw_proto_compatibility_keeps_fee_excluded_payment():
    """Synthetic identity/log wrapper; captured route amounts/order are unchanged."""
    from sniper.services.memecoin_source_valuation import route_payment_evidence, value_source_buy
    from sniper.services.memecoin_source_initiation import solana_receipt_initiation
    fixture = json.loads((Path(__file__).parent / 'fixtures/kraw_closed_wsol_route.json').read_text())
    fixture['transaction']['signatures'] = [base58.b58encode(bytes([41 + j]) * 64).decode()
        for j in range(sum(k['signer'] for k in fixture['transaction']['message']['accountKeys']))]
    fixture['meta']['logMessages'] = ['Program ' + TOKEN + ' invoke [1]']
    owner = fixture['meta']['preTokenBalances'][0]['owner']
    tx = decode(wire_receipt(fixture), {owner: WATCH[OWNER]})
    assert tx is not None
    assert [g['index'] for g in tx['meta']['innerInstructions']] == [0, 2, 3]
    assert tx['meta']['preBalances'] == fixture['meta']['preBalances']
    assert tx['meta']['postBalances'] == fixture['meta']['postBalances']
    assert route_payment_evidence(tx, owner, 500_000_000) == (499_050_000, None)
    facts = value_source_buy(tx, owner, sol_usd=None, now=105)
    proof = solana_receipt_initiation(tx, owner, facts)
    assert proof['route_payment_atomic'] == '499050000'
    assert proof['payment_atomic'] == '500000000'
    assert int(proof['payment_atomic']) - int(proof['route_payment_atomic']) == 950_000


def test_captured_santaro_buy_log_survives_proto_with_cashback_in_gross():
    """Real BuyEvent log in a synthetic transaction, not a captured full receipt."""
    from test_pumpswap_buy import SANTARO_BUY_LOG
    from sniper.execution.pumpswap_buy import parse_buy_event, buy_event_gross_quote
    u = wire_receipt()
    u.transaction.transaction.meta.log_messages.append(SANTARO_BUY_LOG)
    tx = decode(u)
    event = parse_buy_event(tx['meta']['logMessages'][-1])
    assert event['quote_amount_in_with_lp_fee'] == 6_522_235_430
    assert event['protocol_fee'] == 60_644_661
    assert event['cashback'] == 19_562_794
    assert buy_event_gross_quote(event) == 6_602_442_885


def test_real_json_parsed_recovery_reaches_worker_without_rebuilding_receipt(monkeypatch):
    from scripts.research import onchain_wallet_watch as w
    from sniper.services.memecoin_shyft_confirmed import ReceiptWorker
    u = wire_receipt()
    u.transaction.transaction.meta.ClearField('log_messages')
    recovered = copy.deepcopy(FIX)
    monkeypatch.setattr(w, '_http_rpc', lambda *_: recovered)
    consumed = []
    worker = ReceiptWorker(lambda *args: consumed.append(args) or True,
                           decoder=w._shyft_receipt, clock=lambda: 1)
    try:
        assert worker.offer(u, WATCH, head_slot=FIX['slot'], head_monotonic=1,
            recv_monotonic=1, recv_utc='original-receipt', recv_epoch=105)
        worker._jobs.join()
        assert len(consumed) == 1, worker.stats
        assert consumed[0][0] is recovered and recovered == FIX
        assert 'header' not in recovered['transaction']['message']
        assert consumed[0][1] == OWNER
        assert consumed[0][3]['recv_monotonic'] == 1
        assert consumed[0][3]['recv_utc'] == 'original-receipt'
        assert worker.stats['errors'] == 0
        assert worker.has_receipt(FIX['transaction']['signatures'][0], OWNER, 'u1')
    finally:
        worker.close()


@pytest.mark.parametrize('damage', ['missing_signer', 'nonbool_signer', 'mixed_keys',
    'duplicate_keys', 'invalid_key', 'no_signer', 'lookup_signer', 'signer_order',
    'header_mismatch', 'signature_count'])
def test_parsed_recovery_and_worker_refuse_ambiguous_signer_identity(monkeypatch, damage):
    from scripts.research import onchain_wallet_watch as w
    from sniper.services.memecoin_shyft_confirmed import ReceiptWorker
    recovered = copy.deepcopy(FIX)
    msg = recovered['transaction']['message']
    keys = msg['accountKeys']
    if damage == 'missing_signer': del keys[1]['signer']
    elif damage == 'nonbool_signer': keys[0]['signer'] = 1
    elif damage == 'mixed_keys': keys[1] = keys[1]['pubkey']
    elif damage == 'duplicate_keys': keys[1]['pubkey'] = OWNER
    elif damage == 'invalid_key': keys[1]['pubkey'] = 'invalid-address'
    elif damage == 'no_signer': keys[0]['signer'] = False
    elif damage == 'lookup_signer': keys[0]['source'] = 'lookupTable'
    elif damage == 'signer_order': keys[0], keys[1] = keys[1], keys[0]
    elif damage == 'header_mismatch': msg['header'] = {'numRequiredSignatures': 2}
    else: recovered['transaction']['signatures'].append(recovered['transaction']['signatures'][0])
    u = wire_receipt(); u.transaction.transaction.meta.ClearField('log_messages')
    monkeypatch.setattr(w, '_http_rpc', lambda *_: recovered)
    assert w._shyft_receipt(u, WATCH, head_slot=FIX['slot'], recv_monotonic=1,
        head_monotonic=1) is None
    worker = ReceiptWorker(lambda *a: pytest.fail('ambiguous signer reached consume'),
        decoder=lambda *_a, **_kw: recovered, clock=lambda: 1)
    try:
        assert worker.offer(u, WATCH, head_slot=FIX['slot'], head_monotonic=1, recv_monotonic=1)
        worker._jobs.join()
        assert worker.stats['invalid'] == 1 and worker.stats['errors'] == 0
    finally:
        worker.close()


def test_real_json_parsed_sell_recovery_preserves_source_sell_once(monkeypatch, tmp_path):
    from scripts.research import onchain_wallet_watch as w
    from sniper.persistence.db_memecoin import ensure_onchain_sells_table
    from sniper.services.memecoin_shyft_confirmed import ReceiptWorker
    captured = json.loads((Path(__file__).parent /
        'fixtures/memecoin/source_sell_routes.json').read_text())['dflow_coin_usdc_sale_second_signer']
    tx, owner = captured['tx'], captured['owner']
    watch = {owner: WATCH[OWNER]}
    u = wire_receipt(tx); u.transaction.transaction.meta.ClearField('log_messages')
    monkeypatch.setattr(w, '_http_rpc', lambda *_: copy.deepcopy(tx))
    monkeypatch.setattr(w, '_stage_write', lambda *_: None)
    path = tmp_path / 'sell.db'
    with sqlite3.connect(path) as conn:
        conn.executescript(w.SCHEMA)
        ensure_onchain_sells_table(conn)
    writer = w.DetectionWriter(path)
    def consume(receipt, address, info, clocks):
        assert receipt == tx and address == owner
        assert clocks['recv_monotonic'] == 1
        w.process_transaction(tx['transaction']['signatures'][0], address, info, writer,
            tx=receipt, frame_mono=1, commitment='confirmed', require_initiation=True)
        return False  # Sell observations never claim a source-buy proof acknowledgement.
    worker = ReceiptWorker(consume, decoder=w._shyft_receipt, clock=lambda: 1)
    try:
        for _ in range(2):
            assert worker.offer(u, watch, head_slot=tx['slot'], head_monotonic=1, recv_monotonic=1)
        worker._jobs.join(); writer._jobs.join()
        with sqlite3.connect(path) as conn:
            sales = conn.execute('SELECT signature,address,mint,sold_atomic,pre_atomic,post_atomic '
                                 'FROM onchain_wallet_sells_v2').fetchall()
            assert conn.execute('SELECT count(*) FROM onchain_wallet_detections').fetchone()[0] == 0
        assert len(sales) == 1
        assert sales[0] == (tx['transaction']['signatures'][0], owner,
            '3RNy7erxjwGRRYvM6KzVKVtuFnNSpkQFwwiHhUH26xoa',
            17_453_262_401_215, 17_453_262_401_215, 0)
        assert worker.stats['errors'] == 0 and writer.stats['errors'] == 0
    finally:
        worker.close(); writer.close()
