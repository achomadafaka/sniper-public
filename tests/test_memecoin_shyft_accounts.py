"""Account transport never turns a gap or baseline into a reserve delta."""
import asyncio
import struct
import threading

import base58
import pytest

from sniper.feeds.yellowstone import geyser_pb2 as pb


def address(byte):
    return base58.b58encode(bytes([byte]) * 32).decode()


ACCOUNT, OTHER, OWNER = address(1), address(3), address(2)


def update(*, key=1, slot=123, version=7, lamports=1, data=b'raw', startup=False,
           label='accounts:c1:w1'):
    u = pb.SubscribeUpdate(filters=[label])
    u.account.slot = slot
    u.account.is_startup = startup
    a = u.account.account
    a.pubkey, a.owner, a.lamports = bytes([key]) * 32, bytes([2]) * 32, lamports
    a.data, a.write_version = data, version
    return u


def test_exact_bytes_and_zero_values_are_not_absent():
    from sniper.services.memecoin_shyft_accounts import account_update
    u = update(lamports=0, version=0, data=b'\0\xff')
    assert account_update(u) == dict(pubkey=ACCOUNT, owner=OWNER, lamports=0,
        data=b'\0\xff', slot=123, write_version=0, is_startup=False, deleted=True,
        txn_signature=None)
    signed = update()
    signed.account.account.txn_signature = bytes(range(64))
    assert account_update(signed)['txn_signature'] == base58.b58encode(bytes(range(64))).decode()


@pytest.mark.parametrize('bad', ['no_envelope', 'no_info', 'pubkey', 'owner', 'slot', 'oversize'])
def test_normalizer_rejects_malformed_account(bad):
    from sniper.services.memecoin_shyft_accounts import account_update, MAX_ACCOUNT_DATA
    u = update()
    if bad == 'no_envelope': u.ClearField('account')
    if bad == 'no_info': u.account.ClearField('account')
    if bad == 'pubkey': u.account.account.pubkey = bytes(31)
    if bad == 'owner': u.account.account.owner = bytes(33)
    if bad == 'slot': u.account.slot = 0
    if bad == 'oversize': u.account.account.data = bytes(MAX_ACCOUNT_DATA + 1)
    assert account_update(u) is None


def test_normalizer_does_not_misread_other_update_types():
    from sniper.services.memecoin_shyft_accounts import account_update
    u = pb.SubscribeUpdate(); u.slot.slot = 123
    assert account_update(u) is None


def test_empty_watch_has_no_catchall_and_no_replay():
    from sniper.services.memecoin_shyft_accounts import subscription
    req = subscription((), 'accounts:c1:w1')
    assert not req.accounts and not req.transactions
    assert req.commitment == pb.CONFIRMED and not req.HasField('from_slot')
    req = subscription((OTHER, ACCOUNT, ACCOUNT), 'accounts:c1:w2')
    assert list(req.accounts) == ['accounts:c1:w2']
    assert tuple(req.accounts['accounts:c1:w2'].account) == tuple(sorted((ACCOUNT, OTHER)))


@pytest.mark.parametrize('watched', [('bad',), (bytes(32),)])
def test_invalid_watch_is_refused_instead_of_becoming_an_empty_filter(watched):
    from sniper.services.memecoin_shyft_accounts import subscription
    with pytest.raises(ValueError, match='watch_invalid'):
        subscription(watched, 'accounts:c1:w1')


def test_watch_bound_refuses_instead_of_silently_truncating():
    from sniper.services.memecoin_shyft_accounts import subscription, MAX_WATCH_ACCOUNTS
    keys = tuple(base58.b58encode(i.to_bytes(32, 'little')).decode()
                 for i in range(MAX_WATCH_ACCOUNTS + 1))
    with pytest.raises(ValueError, match='watch_limit'):
        subscription(keys, 'accounts:c1:w1')


def test_queue_overflow_discards_deltas_and_preserves_typed_gap():
    from sniper.services.memecoin_shyft_accounts import AccountQueue
    q = AccountQueue(maxsize=2)
    assert q.offer({'kind': 'account', 'pubkey': ACCOUNT, 'slot': 1})
    assert q.offer({'kind': 'account', 'pubkey': OTHER, 'slot': 1})
    assert not q.offer({'kind': 'account', 'pubkey': ACCOUNT, 'slot': 2})
    gap = q.get_nowait()
    assert gap['kind'] == 'account_coverage' and gap['reason'] == 'queue_overflow'
    assert q.empty() and q.stats['overflow'] == 1


def test_coverage_event_clears_prior_queue_and_can_cross_threads():
    from sniper.services.memecoin_shyft_accounts import AccountQueue
    q = AccountQueue(maxsize=2)
    t = threading.Thread(target=lambda: q.offer({'kind': 'account', 'slot': 1}))
    t.start(); t.join()
    q.offer({'kind': 'account_coverage', 'reason': 'connection_lost'})
    assert q.get(timeout=.1)['reason'] == 'connection_lost' and q.empty()


def fake_stream(monkeypatch, frames, *, on_request=None):
    """Consume real request generators without a socket or provider traffic."""
    import grpc
    from sniper.services import memecoin_shyft_accounts as g
    calls, requests = [], []

    class Stream:
        def __init__(self, incoming):
            self.incoming, self.index = incoming, 0
        def __aiter__(self): return self
        async def __anext__(self):
            if self.index == 0:
                req = await anext(self.incoming); requests.append(req)
                ping = await anext(self.incoming); requests.append(ping)
                self.index += 1
                pong = pb.SubscribeUpdate(); pong.pong.id = ping.ping.id
                return pong
            if frames:
                item = frames.pop(0)
                if callable(item):
                    item = await item(self.incoming, requests)
                if isinstance(item, Exception): raise item
                return item
            raise StopAsyncIteration
        def cancel(self): calls.append('cancel')

    class Channel:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def stream_stream(self, path, **kw):
            assert path == '/geyser.Geyser/Subscribe'
            def call(incoming, **kw):
                assert kw['metadata'] == (('x-token', 'test-token'),)
                if on_request: on_request()
                return Stream(incoming)
            return call

    def channel(target, *a, **kw):
        assert target == 'grpc.ams.shyft.to:443'
        calls.append('channel')
        return Channel()
    monkeypatch.setattr(grpc.aio, 'secure_channel', channel)
    return g, calls, requests


def run_once(g, watched, on_account, status, stop):
    async def run():
        task = asyncio.create_task(g.run_accounts('https://grpc.ams.shyft.to', 'test-token',
            watched, on_account, status, stop))
        while not status.get('reconnects') and not task.done():
            await asyncio.sleep(.001)
        stop.set()
        await asyncio.wait_for(task, 2)
    asyncio.run(run())


def test_one_confirmed_connection_many_accounts_and_observed_coverage(monkeypatch):
    g, calls, requests = fake_stream(monkeypatch, [update(), update(key=3)])
    rows, snapshots, status, stop = [], [], {}, threading.Event()
    def offer(row):
        rows.append(row); snapshots.append(dict(status)); return True
    run_once(g, lambda: (ACCOUNT, OTHER), offer, status, stop)
    accounts = [r for r in rows if r['kind'] == 'account']
    assert [r['pubkey'] for r in accounts] == [ACCOUNT, OTHER]
    assert calls.count('channel') == 1 and requests[0].commitment == pb.CONFIRMED
    assert accounts[0]['baseline_only'] is True
    assert accounts[0]['connection_generation'] == 1 and accounts[0]['watch_generation'] == 1
    assert snapshots[0]['acknowledged_accounts'] == (ACCOUNT,)
    assert snapshots[0]['coverage'] == 'partial'
    assert snapshots[1]['coverage'] == 'observed'
    assert rows[-1]['reason'] == 'connection_lost' and status['coverage'] == 'unknown'


def test_ordering_is_before_consumer_and_startup_never_creates_delta(monkeypatch):
    frames = [update(version=2), update(version=1), update(version=2), update(slot=122, version=99),
        update(version=3), update(slot=122, version=999, startup=True),
        update(slot=124, version=0, startup=True), update(slot=124, version=1)]
    g, _, _ = fake_stream(monkeypatch, frames)
    rows, status, stop = [], {}, threading.Event()
    run_once(g, lambda: (ACCOUNT,), rows.append, status, stop)
    accepted = [r for r in rows if r['kind'] == 'account']
    assert [(r['slot'], r['write_version']) for r in accepted] == [(123, 2), (123, 3), (124, 0), (124, 1)]
    assert [r['baseline_only'] for r in accepted] == [True, False, True, False]
    assert status['stale_updates'] == 4


def test_deletion_is_not_balance_zero_and_recreation_is_baseline(monkeypatch):
    g, _, _ = fake_stream(monkeypatch, [update(), update(version=8, lamports=0),
        update(version=9), update(version=10)])
    rows, status, stop = [], {}, threading.Event()
    run_once(g, lambda: (ACCOUNT,), rows.append, status, stop)
    accepted = [r for r in rows if r['kind'] == 'account']
    assert [r['deleted'] for r in accepted] == [False, True, False, False]
    assert [r['baseline_only'] for r in accepted] == [True, True, True, False]


@pytest.mark.parametrize('label', ['accounts:c0:w1', 'accounts:c1:w0', '', 'foreign'])
def test_previous_connection_or_subscription_cannot_be_relabeled(monkeypatch, label):
    g, _, _ = fake_stream(monkeypatch, [update(label=label), update()])
    rows, status, stop = [], {}, threading.Event()
    run_once(g, lambda: (ACCOUNT,), rows.append, status, stop)
    assert len([r for r in rows if r['kind'] == 'account']) == 1
    assert status['stale_generations'] == 1


def test_wrong_watched_address_and_malformed_current_row_censor_coverage(monkeypatch):
    bad = update(); bad.account.account.owner = b'wrong'
    g, _, _ = fake_stream(monkeypatch, [update(key=3), bad, update()])
    rows, status, stop = [], {}, threading.Event()
    run_once(g, lambda: (ACCOUNT,), rows.append, status, stop)
    reasons = {r['reason'] for r in rows if r['kind'] == 'account_coverage'}
    assert {'account_unwatched', 'account_malformed'} <= reasons
    accepted = [r for r in rows if r['kind'] == 'account']
    assert len(accepted) == 1 and accepted[0]['baseline_only'] is True


def test_overflow_resets_ordering_and_next_update_is_baseline(monkeypatch):
    g, _, _ = fake_stream(monkeypatch, [update(), update(version=8), update(version=9), update(version=10)])
    rows, status, stop = [], {}, threading.Event()
    def offer(row):
        rows.append(row)
        return not (row.get('write_version') == 9)
    run_once(g, lambda: (ACCOUNT,), offer, status, stop)
    accepted = [r for r in rows if r['kind'] == 'account']
    assert accepted[-1]['baseline_only'] is True
    assert status['queue_overflows'] == 1
    assert any(r.get('reason') == 'queue_overflow' for r in rows)


def test_provider_failure_is_typed_and_invalidates_consumer(monkeypatch):
    import grpc
    class ProviderFailure(grpc.RpcError):
        def code(self): return grpc.StatusCode.RESOURCE_EXHAUSTED
        def details(self): raise AssertionError('provider text may leak token')
    g, _, _ = fake_stream(monkeypatch, [update(), ProviderFailure()])
    rows, status, stop = [], {}, threading.Event()
    run_once(g, lambda: (ACCOUNT,), rows.append, status, stop)
    assert status['last_error'] == 'RESOURCE_EXHAUSTED'
    assert rows[-1]['reason'] == 'connection_lost'
    assert not status['connected'] and not status['acknowledged_accounts']


def test_no_pong_alone_can_claim_address_coverage(monkeypatch):
    g, _, _ = fake_stream(monkeypatch, [])
    rows, status, stop = [], {}, threading.Event()
    run_once(g, lambda: (ACCOUNT,), rows.append, status, stop)
    assert not status['acknowledged_accounts'] and status['accounts'] == 0
    assert not [r for r in rows if r['kind'] == 'account']


def test_dynamic_watch_old_pong_and_rows_do_not_ack_current_generation(monkeypatch):
    from sniper.services import memecoin_shyft_accounts as g
    watched = {'keys': (ACCOUNT,)}
    async def changed(incoming, requests):
        watched['keys'] = (ACCOUNT, OTHER)
        req = await anext(incoming); requests.append(req)
        assert list(req.accounts) == ['accounts:c1:w2']
        ping = await anext(incoming); requests.append(ping)
        old = pb.SubscribeUpdate(); old.pong.id = 1
        return old
    async def matching(incoming, requests):
        u = pb.SubscribeUpdate(); u.pong.id = requests[-1].ping.id
        return u
    g, _, requests = fake_stream(monkeypatch, [update(), changed, update(), matching,
        update(key=3, label='accounts:c1:w2')])
    rows, status, stop = [], {}, threading.Event()
    run_once(g, lambda: watched['keys'], rows.append, status, stop)
    accounts = [r for r in rows if r['kind'] == 'account']
    assert [(r['pubkey'], r['watch_generation']) for r in accounts] == [(ACCOUNT, 1), (OTHER, 2)]
    assert accounts[-1]['baseline_only'] is True and status['stale_pongs'] == 1
    assert len([r for r in requests if r.accounts]) == 2


def test_dead_slot_censors_baselines_and_never_becomes_zero(monkeypatch):
    dead = pb.SubscribeUpdate(); dead.slot.slot = 123; dead.slot.status = pb.SLOT_DEAD
    g, _, _ = fake_stream(monkeypatch, [update(), dead, update(version=8), update(slot=124)])
    rows, status, stop = [], {}, threading.Event()
    run_once(g, lambda: (ACCOUNT,), rows.append, status, stop)
    accounts = [r for r in rows if r['kind'] == 'account']
    assert [r['slot'] for r in accounts] == [123, 124]
    assert accounts[-1]['baseline_only'] is True
    assert any(r.get('reason') == 'dead_slot' for r in rows)


def test_same_write_identity_with_different_payload_censors_instead_of_ignoring(monkeypatch):
    g, _, _ = fake_stream(monkeypatch, [update(), update(data=b'contradiction'), update(version=8)])
    rows, status, stop = [], {}, threading.Event()
    run_once(g, lambda: (ACCOUNT,), rows.append, status, stop)
    assert any(r.get('reason') == 'account_conflict' for r in rows)
    accounts = [r for r in rows if r['kind'] == 'account']
    assert len(accounts) == 2 and accounts[-1]['baseline_only'] is True


def test_reconnect_does_not_compare_validator_write_versions_or_admit_old_slots(monkeypatch):
    g, calls, _ = fake_stream(monkeypatch, [update(version=900), RuntimeError('provider ended'),
        update(slot=122, version=999, label='accounts:c2:w2'),
        update(version=999, label='accounts:c2:w2'),
        update(slot=124, version=0, label='accounts:c2:w2')])
    rows, status, stop = [], {}, threading.Event()
    def offer(row):
        rows.append(row)
        if row.get('kind') == 'account' and row['connection_generation'] == 2:
            stop.set()
    asyncio.run(asyncio.wait_for(g.run_accounts('https://grpc.ams.shyft.to', 'test-token',
        lambda: (ACCOUNT,), offer, status, stop), 3))
    accounts = [r for r in rows if r['kind'] == 'account']
    assert [(r['slot'], r['write_version']) for r in accounts] == [(123, 900), (124, 0)]
    assert accounts[-1]['baseline_only'] is True and calls.count('channel') == 2


def price_book():
    from sniper.services.memecoin_solana_swap_marks import PoolBinding
    from sniper.services.memecoin_solana_ws import VaultTape
    mint, quote, pool = address(4), address(5), address(6)
    tape = VaultTape(ACCOUNT, OTHER, mint=pool)
    binding = PoolBinding(mint=mint, pool=pool, coin_vault=ACCOUNT,
                          quote_vault=OTHER, quote_mint=quote)
    return {ACCOUNT: tape, OTHER: tape}, {pool: binding}, mint, quote


def vault_row(vault, mint, amount, *, slot=124, baseline=False, startup=False,
              owner='TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA', tail=b'', sig=None, version=1):
    raw = bytearray(165)
    raw[:32] = base58.b58decode(mint)
    raw[32:64] = bytes([8]) * 32
    struct.pack_into('<Q', raw, 64, amount)
    raw[108] = 1
    return dict(kind='account', pubkey=vault, owner=owner, lamports=1, data=bytes(raw) + tail,
        slot=slot, write_version=version, is_startup=startup, deleted=False,
        txn_signature=sig if sig is not None else f'tx-{slot}',   # one transaction per slot
        baseline_only=baseline, connection_generation=1, watch_generation=1,
        recv_monotonic=0)


def test_raw_vault_updates_feed_current_tape_with_exact_decimals_and_lp_rules():
    from sniper.services.memecoin_solana_swap_marks import apply_account_row
    vaults, bindings, mint, quote = price_book()
    decimals = {ACCOUNT: 6, OTHER: 9}
    assert apply_account_row(vault_row(ACCOUNT, mint, 100, baseline=True),
        vaults, bindings, decimals=decimals) is None
    seeded = apply_account_row(vault_row(OTHER, quote, 1000, baseline=True),
        vaults, bindings, decimals=decimals)
    assert seeded['kind'] == 'sol_seed'
    apply_account_row(vault_row(ACCOUNT, mint, 90), vaults, bindings, decimals=decimals)
    swap = apply_account_row(vault_row(OTHER, quote, 1100), vaults, bindings, decimals=decimals)
    assert (swap['kind'], swap['amount0'], swap['amount1']) == ('sol_swap', '-10', '100')
    assert (swap['decimals0'], swap['decimals1']) == (6, 9)
    apply_account_row(vault_row(ACCOUNT, mint, 80, slot=125), vaults, bindings, decimals=decimals)
    lp = apply_account_row(vault_row(OTHER, quote, 1000, slot=125), vaults, bindings, decimals=decimals)
    assert lp['kind'] == 'sol_burn'


@pytest.mark.parametrize('bad', ['owner', 'mint', 'truncated', 'state', 'coption', 'deleted',
                               'classic_extension', 'token22_mint_type', 'token22_truncated_tlv'])
def test_wrong_identity_or_account_layout_invalidates_both_vault_baselines(bad):
    from sniper.services.memecoin_solana_swap_marks import apply_account_row, AccountCoverageError
    vaults, bindings, mint, quote = price_book()
    tape = vaults[ACCOUNT]
    tape.seed_balance(ACCOUNT, 123, 100, 6)
    tape.seed_balance(OTHER, 123, 1000, 9)
    row = vault_row(ACCOUNT, mint, 90)
    if bad == 'owner': row['owner'] = OWNER
    if bad == 'mint': row['data'] = bytes([9]) * 32 + row['data'][32:]
    if bad == 'truncated': row['data'] = row['data'][:164]
    if bad == 'state': row['data'] = row['data'][:108] + b'\0' + row['data'][109:]
    if bad == 'coption': row['data'] = row['data'][:72] + b'\x02\0\0\0' + row['data'][76:]
    if bad == 'deleted': row['deleted'], row['lamports'] = True, 0
    if bad == 'classic_extension': row['data'] += b'\2\7\0\0\0'
    if bad.startswith('token22'):
        row['owner'] = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'
        row['data'] += b'\1\7\0\0\0' if bad.endswith('mint_type') else b'\2\7\0\5\0\0'
    with pytest.raises(AccountCoverageError):
        apply_account_row(row, vaults, bindings, decimals={ACCOUNT: 6, OTHER: 9})
    assert tape._last == {} and tape._slot == {} and tape._decimals == {}


def test_token2022_account_extensions_keep_base_balance_and_absent_decimals_unknown():
    from sniper.services.memecoin_solana_swap_marks import apply_account_row
    vaults, bindings, mint, quote = price_book()
    owner = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'
    row = vault_row(ACCOUNT, mint, 100, baseline=True, owner=owner, tail=b'\2\7\0\0\0')
    apply_account_row(row, vaults, bindings, decimals={})
    seed = apply_account_row(vault_row(OTHER, quote, 1000, baseline=True),
        vaults, bindings, decimals={OTHER: 9})
    assert seed['decimals0'] is None and seed['amount0'] == '100'


def test_startup_repairs_both_baselines_without_fabricating_a_swap():
    from sniper.services.memecoin_solana_swap_marks import apply_account_row
    vaults, bindings, mint, quote = price_book()
    tape, decimals = vaults[ACCOUNT], {ACCOUNT: 6, OTHER: 9}
    tape.seed_balance(ACCOUNT, 123, 100, 6); tape.seed_balance(OTHER, 123, 1000, 9)
    assert apply_account_row(vault_row(ACCOUNT, mint, 90, baseline=True, startup=True),
        vaults, bindings, decimals=decimals) is None
    seed = apply_account_row(vault_row(OTHER, quote, 1100, baseline=True, startup=True),
        vaults, bindings, decimals=decimals)
    assert seed['kind'] == 'sol_seed' and not tape._slot


def test_curve_requires_pump_owner_canonical_pda_and_supported_raw_layout():
    from sniper.execution.pump_native_exit import PUMP_PROGRAM, _pda, _pk
    from sniper.services.memecoin_solana_swap_marks import apply_account_row, PoolBinding, AccountCoverageError
    from sniper.services.memecoin_solana_ws import CurveTape, CURVE_DISCRIMINATOR, PUMP_CURVE_TOTAL_SUPPLY
    mint = address(4)
    curve = str(_pda([b'bonding-curve', bytes(_pk(mint))], PUMP_PROGRAM))
    vaults = {curve: CurveTape(curve, mint=curve)}
    bindings = {curve: PoolBinding(mint=mint, pool=curve, coin_vault=curve,
                                 quote_vault=curve, quote_mint='So11111111111111111111111111111111111111112')}
    raw = CURVE_DISCRIMINATOR + struct.pack('<5Q', 100, 100, 10, 10, PUMP_CURVE_TOTAL_SUPPLY) + b'\0'
    row = dict(kind='account', pubkey=curve, owner=PUMP_PROGRAM, data=raw, lamports=1,
               slot=124, deleted=False, baseline_only=True, is_startup=False)
    assert apply_account_row(row, vaults, bindings, decimals={})['kind'] == 'sol_curve_state'
    row['owner'] = OWNER
    with pytest.raises(AccountCoverageError):
        apply_account_row(row, vaults, bindings, decimals={})


def test_price_adapter_runs_refresh_on_consumer_thread_and_invalidates_on_gap():
    from sniper.services.memecoin_solana_swap_marks import AccountPriceStream, AccountCoverageError
    stream = AccountPriceStream('https://grpc.ams.shyft.to', 'test-token')
    vaults, bindings, mint, quote = price_book()
    tid, refresh_tids, rows = threading.get_ident(), [], []
    def refresh():
        refresh_tids.append(threading.get_ident()); return dict(vaults)
    async def exercise():
        task = asyncio.create_task(stream.stream(vaults, rows.append, bindings=bindings,
            refresh_fn=refresh, refresh_s=.001))
        await asyncio.sleep(.005)
        stream.queue.offer(dict(kind='account_coverage', reason='queue_overflow'))
        with pytest.raises(AccountCoverageError, match='queue_overflow'):
            await task
    asyncio.run(exercise())
    assert refresh_tids and set(refresh_tids) == {tid}
    assert stream.watched() == () and not rows and not vaults[ACCOUNT]._last


def test_account_producer_starts_once_and_close_cancels_its_own_receiver():
    from sniper.services.memecoin_solana_swap_marks import AccountPriceStream
    started, stopped, calls = threading.Event(), threading.Event(), []
    async def receiver(url, token, watch_fn, offer, status, stop):
        calls.append(threading.get_ident()); started.set()
        try:
            await asyncio.sleep(10)
        finally:
            stopped.set()
    stream = AccountPriceStream('https://grpc.ams.shyft.to', 'test-token', run_fn=receiver)
    stream.start(); stream.start()
    assert started.wait(1) and len(calls) == 1
    stream.close()
    assert stopped.wait(1) and not stream.thread.is_alive()
    assert stream.status['endpoint'] == 'grpc.ams.shyft.to'


@pytest.mark.parametrize('value', [None, {}, {'owner': 'wrong'},
    {'owner': 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA', 'data': {'parsed': {'type': 'account', 'info': {'mint': address(9),
     'state': 'initialized', 'tokenAmount': {'amount': '1', 'decimals': 6}}}}}])
def test_strict_seed_refuses_missing_wrong_or_unbound_identity(value):
    from sniper.services.memecoin_solana_swap_marks import vault_seed_fn
    read = vault_seed_fn('unused', rpc_call=lambda *_: {'context': {'slot': 10}, 'value': value},
                        expected_mint_fn=lambda _: address(4))
    assert read(ACCOUNT) is None


def test_account_seed_reads_have_only_two_in_flight():
    from sniper.services.memecoin_solana_swap_marks import AccountPriceStream
    from sniper.services.memecoin_solana_ws import VaultTape
    from sniper.services.memecoin_solana_swap_marks import PoolBinding
    import time
    stream = AccountPriceStream('https://grpc.ams.shyft.to', 'token')
    active, maximum = [0], [0]
    guard = threading.Lock()
    addresses = [address(i) for i in range(10, 16)]
    vaults, bindings = {}, {}
    for n in range(0, len(addresses), 2):
        a, b = addresses[n:n+2]
        tape = VaultTape(a, b, mint=a)
        vaults.update({a: tape, b: tape})
        bindings[a] = PoolBinding(address(4), a, a, b, address(5))
    def seed(_):
        with guard:
            active[0] += 1; maximum[0] = max(maximum[0], active[0])
        time.sleep(.03)
        with guard:
            active[0] -= 1
        return 10, 100, 6
    async def exercise():
        task = asyncio.create_task(stream.stream(vaults, lambda _: None, bindings=bindings,
                                                 vault_seed_fn=seed))
        await asyncio.sleep(.005)
        stream.status.update(connected=True, subscription_acknowledged=True,
                             requested_accounts=stream.watched())
        await asyncio.sleep(.13)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(exercise())
    assert maximum[0] == 2 and active[0] == 0


def test_dynamic_watch_gap_clears_cache_and_continues_on_current_generation():
    from sniper.services.memecoin_solana_swap_marks import AccountPriceStream, run_price_stream_epoch
    stream = AccountPriceStream('https://grpc.ams.shyft.to', 'token')
    vaults, bindings, mint, quote = price_book()
    vaults[ACCOUNT].seed_balance(ACCOUNT, 10, 100, 6)
    cleared = []
    class Cache:
        def record(self, *_a, **_k): pass
        def invalidate(self): cleared.append('raw')
        def invalidate_prices(self): cleared.append('prices')
    async def producer():
        await asyncio.sleep(.01)
        stream.queue.offer(dict(kind='account_coverage', reason='watch_changed'))
    async def exercise():
        task = asyncio.create_task(producer())
        from functools import partial
        await run_price_stream_epoch(vaults, bindings, Cache(), ws_url=None, duration_s=.04,
                                    stream_fn=partial(stream.stream, bindings=bindings))
        await task
    asyncio.run(exercise())
    assert cleared == ['raw', 'prices'] and not vaults[ACCOUNT]._last


def test_connection_gap_between_epochs_cannot_be_discarded_as_an_old_delta():
    from sniper.services.memecoin_solana_swap_marks import AccountPriceStream, AccountCoverageError
    stream = AccountPriceStream('https://grpc.ams.shyft.to', 'token')
    vaults, bindings, mint, quote = price_book()
    stream.queue.offer(dict(kind='account_coverage', reason='connection_lost'))
    async def exercise():
        with pytest.raises(AccountCoverageError, match='connection_lost'):
            await asyncio.wait_for(stream.stream(vaults, lambda _: None, bindings=bindings), .03)
    asyncio.run(exercise())


@pytest.mark.parametrize('age,generation', [(3, 1), (0, 0)])
def test_account_consumer_rejects_expired_or_prior_generation_rows(age, generation):
    import time
    from sniper.services.memecoin_solana_swap_marks import AccountPriceStream, AccountCoverageError
    stream = AccountPriceStream('https://grpc.ams.shyft.to', 'token')
    vaults, bindings, mint, quote = price_book()
    rows = []
    async def exercise():
        task = asyncio.create_task(stream.stream(vaults, rows.append, bindings=bindings))
        await asyncio.sleep(.005)
        stream.status.update(connection_generation=1, watch_generation=1)
        row = vault_row(ACCOUNT, mint, 100)
        row.update(connection_generation=generation, recv_monotonic=time.monotonic() - age)
        stream.queue.offer(row)
        await asyncio.sleep(.02)
        if age:
            with pytest.raises(AccountCoverageError, match='account_queue_stale'): await task
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(exercise())
    assert not rows and not vaults[ACCOUNT]._last


def test_newer_http_context_cannot_be_rewound_by_current_startup_account():
    import time
    from sniper.services.memecoin_solana_swap_marks import AccountPriceStream
    stream = AccountPriceStream('https://grpc.ams.shyft.to', 'token')
    vaults, bindings, mint, quote = price_book()
    tape, before, after = vaults[ACCOUNT], [], []
    def seed(key): return 105, 100 if key == ACCOUNT else 1000, 6 if key == ACCOUNT else 9
    async def exercise():
        task = asyncio.create_task(stream.stream(vaults, lambda _: None, bindings=bindings,
                                                vault_seed_fn=seed))
        await asyncio.sleep(.005)
        stream.status.update(connected=True, subscription_acknowledged=True,
            requested_accounts=stream.watched(), connection_generation=1, watch_generation=1)
        for _ in range(100):
            if len(tape._last) == 2: break
            await asyncio.sleep(.005)
        before.append(dict(tape._last))
        row = vault_row(ACCOUNT, mint, 90, slot=104, baseline=True, startup=True)
        row['recv_monotonic'] = time.monotonic()
        stream.queue.offer(row)
        await asyncio.sleep(.025)
        after.append(dict(tape._last))
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(exercise())
    assert before == [{ACCOUNT: 100, OTHER: 1000}] and after == before


@pytest.mark.parametrize('seed_first,seed_slot,live_slot', [(True, 105, 104), (True, 105, 105),
    (False, 105, 106), (False, 106, 106), (False, 107, 106)])
def test_http_bank_context_and_stream_write_race_never_rewinds_or_invents_flow(seed_first, seed_slot, live_slot):
    from sniper.services.memecoin_solana_swap_marks import apply_account_row, apply_account_seed
    vaults, bindings, mint, quote = price_book()
    tape, order, decimals, rows = vaults[ACCOUNT], {}, {}, []
    def seeds():
        for address_, amount, precision in ((ACCOUNT, 100, 6), (OTHER, 1000, 9)):
            rows.append(apply_account_seed(address_, (seed_slot, amount, precision), vaults,
                decimals=decimals, ordering=order, generation=(1, 1)))
    def live():
        for address_, mint_, amount in ((ACCOUNT, mint, 90), (OTHER, quote, 1100)):
            rows.append(apply_account_row(vault_row(address_, mint_, amount, slot=live_slot),
                vaults, bindings, decimals=decimals, ordering=order))
    if seed_first: seeds(); live()
    else: live(); seeds()
    assert tape._last == ({ACCOUNT: 100, OTHER: 1000} if seed_first else {ACCOUNT: 90, OTHER: 1100})
    assert all(row is None or row['kind'] == 'sol_seed' for row in rows)
    assert all(point[2] is None for point in order.values()) if seed_first else True
    next_slot = max(seed_slot, live_slot) + 1
    apply_account_row(vault_row(ACCOUNT, mint, 91, slot=next_slot), vaults, bindings,
                      decimals=decimals, ordering=order)
    swap = apply_account_row(vault_row(OTHER, quote, 1090, slot=next_slot), vaults, bindings,
                             decimals=decimals, ordering=order)
    assert swap['kind'] == 'sol_swap'


def test_newer_http_snapshot_repairs_both_legs_as_baseline_and_reconnect_retains_context_floor():
    from sniper.services.memecoin_solana_swap_marks import apply_account_row, apply_account_seed, _reset_account_tape
    vaults, bindings, mint, quote = price_book()
    tape, order, decimals = vaults[ACCOUNT], {}, {ACCOUNT: 6, OTHER: 9}
    for address_, mint_, amount in ((ACCOUNT, mint, 90), (OTHER, quote, 1100)):
        apply_account_row(vault_row(address_, mint_, amount, slot=106), vaults, bindings,
                          decimals=decimals, ordering=order)
    # Existing live balances win a late seed, even when its bank is newer; applying that bank
    # snapshot mid-flow would discard ordered writes and turn a later write into an invented delta.
    assert apply_account_seed(ACCOUNT, (107, 100, 6), vaults, decimals=decimals,
                             ordering=order, generation=(1, 1)) is None
    assert tape._last == {ACCOUNT: 90, OTHER: 1100}
    _reset_account_tape(tape)  # Real coverage loss permits baseline repair, never inferred flow.
    assert apply_account_seed(ACCOUNT, (107, 100, 6), vaults, decimals=decimals,
                             ordering=order, generation=(1, 1)) is None
    assert tape._last == {ACCOUNT: 100}
    seed = apply_account_seed(OTHER, (107, 1000, 9), vaults, decimals=decimals,
                             ordering=order, generation=(1, 1))
    assert seed['kind'] == 'sol_seed'
    _reset_account_tape(tape)
    for address_, amount, precision in ((ACCOUNT, 100, 6), (OTHER, 1000, 9)):
        apply_account_seed(address_, (107, amount, precision), vaults, decimals=decimals,
                           ordering=order, generation=(2, 2))
    older = vault_row(ACCOUNT, mint, 1, slot=106, baseline=True, startup=True)
    older.update(connection_generation=2, watch_generation=2)
    assert apply_account_row(older, vaults, bindings, decimals=decimals, ordering=order) is None
    assert tape._last == {ACCOUNT: 100, OTHER: 1000}


@pytest.mark.parametrize('reack', [True, False])
def test_dynamic_watch_after_healthy_eleven_seconds_gets_new_bounded_ack_interval(monkeypatch, reack):
    from types import SimpleNamespace
    from sniper.services import memecoin_solana_swap_marks as marks
    now = [100.0]
    monkeypatch.setattr(marks, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    stream = marks.AccountPriceStream('https://grpc.ams.shyft.to', 'token')
    vaults, bindings, _, _ = price_book()
    async def exercise():
        task = asyncio.create_task(stream.stream(vaults, lambda _: None, bindings=bindings))
        await asyncio.sleep(.005)
        stream.status.update(connected=True, subscription_acknowledged=True,
            requested_accounts=stream.watched(), connection_generation=1, watch_generation=1)
        await asyncio.sleep(.015)
        now[0] += 11
        stream.status.update(connected=False, subscription_acknowledged=False, watch_generation=2)
        stream.queue.offer(dict(kind='account_coverage', reason='watch_changed',
                               connection_generation=1, watch_generation=2))
        await asyncio.sleep(.025)
        assert not task.done(), 'healthy epoch age was reused as a new watch ACK deadline'
        if reack:
            stream.status.update(connected=True, subscription_acknowledged=True)
            now[0] += 10.1
            await asyncio.sleep(.025)
            assert not task.done()
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
        else:
            now[0] += 10.1
            with pytest.raises(marks.AccountCoverageError, match='subscription_ack_timeout'):
                await asyncio.wait_for(task, .1)
    asyncio.run(exercise())


def test_initial_account_subscription_retains_its_ten_second_bound(monkeypatch):
    from types import SimpleNamespace
    from sniper.services import memecoin_solana_swap_marks as marks
    now = [100.0]
    monkeypatch.setattr(marks, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    stream = marks.AccountPriceStream('https://grpc.ams.shyft.to', 'token')
    vaults, bindings, _, _ = price_book()
    async def exercise():
        task = asyncio.create_task(stream.stream(vaults, lambda _: None, bindings=bindings))
        await asyncio.sleep(.015)
        now[0] += 10.1
        with pytest.raises(marks.AccountCoverageError, match='subscription_ack_timeout'):
            await asyncio.wait_for(task, .1)
    asyncio.run(exercise())


def test_vault_pairs_need_one_transaction_and_an_unnamed_write_never_pairs():
    """Codex P1 on 56a8c4676: per-account ordering cannot pair writes ACROSS transactions. From
    100/100, two real same-slot swaps A 10/1000 and B 1000/10 (both k=10000) arrive coinA(v10)=10,
    quoteB(v20)=10, coinB(v19)=1000. The old tape published 10/10 (a 90% 'pull') before any void;
    tx-bound pairing never pairs A with B, and the funded LP rail never nominates."""
    from sniper.services.memecoin_solana_swap_marks import (SolanaSwapMarkCache, apply_account_row,
                                                           LP_COMPLETE_KINDS)
    import dataclasses
    vaults, bindings, mint, quote = price_book()
    bindings = {k: dataclasses.replace(v, vault_owner='GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL')
                for k, v in bindings.items()}                      # an LP-eligible CPMM pool
    decimals, order = {ACCOUNT: 6, OTHER: 9}, {}
    cache = SolanaSwapMarkCache()
    cache.set_lp_bindings(bindings)
    def feed(row):
        out = apply_account_row(row, vaults, bindings, decimals=decimals, ordering=order)
        if out:
            cache.record(out, bindings[out['pool']], now_ts=1.0, observed_at='2026-10-21T12:00:00+00:00')
        return out
    feed(vault_row(ACCOUNT, mint, 90, slot=98, sig='s0', version=1))
    feed(vault_row(OTHER, quote, 111, slot=98, sig='s0', version=2))
    base = [feed(vault_row(ACCOUNT, mint, 100, slot=99, sig='t0', version=3)),
            feed(vault_row(OTHER, quote, 100, slot=99, sig='t0', version=4))]
    assert base[1]['kind'] in LP_COMPLETE_KINDS                     # one tx, both vaults: L 100
    rows = [feed(vault_row(ACCOUNT, mint, 10, slot=100, sig='A', version=10)),
            feed(vault_row(OTHER, quote, 10, slot=100, sig='B', version=20))]
    assert all(r['kind'] == 'sol_sync' for r in rows)              # A's coin never pairs with B's quote
    assert cache.lp_removal(mint)['fraction'] == 0.0               # nothing for the rail to nominate
    rows = [feed(vault_row(ACCOUNT, mint, 1000, slot=100, sig='B', version=21))]
    assert rows[0]['kind'] == 'sol_swap' and (rows[0]['balance0'], rows[0]['balance1']) == ('1000', '10')
    assert cache.lp_removal(mint)['fraction'] == 0.0               # B's true post-state: k unchanged
    unnamed = vault_row(OTHER, quote, 50, slot=101, sig='', version=30)
    named = vault_row(ACCOUNT, mint, 2000, slot=101, sig='', version=31)
    assert feed(unnamed)['kind'] == 'sol_sync' and feed(named)['kind'] == 'sol_sync'


def test_live_writes_count_signed_and_unsigned_for_the_lp_rail(monkeypatch):
    """An unsigned live write is blind for LP authority, so the heartbeat counts it (Alpha)."""
    signed = update(version=2)
    signed.account.account.txn_signature = bytes(64)
    g, _, _ = fake_stream(monkeypatch, [update(version=1), signed, update(version=3)])
    rows, status, stop = [], {}, threading.Event()
    run_once(g, lambda: (ACCOUNT,), rows.append, status, stop)
    assert status['signed_updates'] == 1 and status['lp_unsigned_rows'] == 1   # baseline not counted
    from sniper.services import memecoin_runner
    src = open(memecoin_runner.__file__).read()
    assert "'signed_updates', 'lp_unsigned_rows')" in src                    # heartbeat whitelist
