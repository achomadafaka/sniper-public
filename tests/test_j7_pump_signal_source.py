"""Pump trades must retain identity and pass through the existing funded controls."""
import copy
import json
import sqlite3
import sys
import types

import pytest

from sniper.feeds import j7_feed as J
from sniper.feeds.fomo_feed import normalize_feed_item
from sniper.persistence import db_memecoin as m

WALLET = '2M2vLX34LXMg24dMEnjWHvRXS1tshpEDRWzmXgV8ENNZ'
MINT = 'CPJeph8rcjzXiwHTwYcm8mBESajTQwmRYWxZLUxUpump'
SIG = '4fNfG9hoirmAHNS1rufVGVBfcbLSjHuZg1MXWoRJseBWfp4EbNn4qXu9kf84NvPZBQquhfXmBmuMYP1sFgCBRqw'
# The public J7 client supplies this trade envelope. Live capture checks it separately.
RELAY = {'type': 'PUMP_EVENT', 'kind': 'trade', 'received_at': 1789552810000,
         'data': {'id': 'pump:' + SIG, 'platform': 'pump.fun', 'side': 'buy', 'tx': SIG,
                  'usdAmount': 12500.5, 'solAmount': 100.1, 'priceUsd': 0.000114,
                  'marketCapUsd': 114376.13, 'isBondingCurve': True, 'program': 'pump',
                  'timestamp': '2026-09-16T10:00:09Z',
                  'author': {'wallet': WALLET, 'username': 'SossaDotKek', 'displayName': 'SossaDotKek'},
                  'token': {'address': MINT, 'networkId': 1399811149, 'symbol': 'INU'}}}
UID = 'pump:' + WALLET
NOW = '2026-09-16T10:00:10+00:00'


def pump_item(relay):
    adapter = getattr(J, 'pump_event_to_feed_item', None)
    assert callable(adapter), 'The Pump trade adapter is absent'
    return adapter(relay)


def test_pump_trade_keeps_wallet_signature_source_time_and_trade_size():
    item = pump_item(RELAY)
    ev = normalize_feed_item(item, NOW, None)
    assert (ev.source_user_id, ev.source_handle, ev.network_id, ev.side) == (UID, 'SossaDotKek', 'solana', 'buy')
    assert (ev.mint, ev.their_price, ev.their_size_usd) == (MINT, 0.000114, 12500.5)
    assert ev.created_at == '2026-09-16T10:00:09Z'
    assert ev.signal_source == 'follow_activity' and ev.delivery_lane == 'j7_pump'
    assert item['body']['source_wallet'] == WALLET and item['body']['source_transaction'] == SIG
    assert 'currentSizeUsd' not in item['body']
    replay = copy.deepcopy(RELAY)
    replay['data'].update(id='changed-provider-id', timestamp='2026-09-16T10:00:09.000Z')
    assert pump_item(replay)['id'] == item['id']
    replay['data']['side'] = 'sell'
    assert pump_item(replay)['id'] != item['id']
    assert normalize_feed_item(pump_item(replay), NOW, None).side == 'sell'


@pytest.mark.parametrize('path,value', [
    (('type',), 'FOMO_EVENT'), (('kind',), 'callout'), (('data', 'side'), 'transfer'),
    (('data', 'author', 'wallet'), ''), (('data', 'author', 'wallet'), '0x' + '1' * 40),
    (('data', 'token', 'address'), 'truncated...'), (('data', 'token', 'networkId'), 56),
    (('data', 'tx'), ''), (('data', 'tx'), 'not-a-signature'),
    (('data', 'timestamp'), ''), (('data', 'timestamp'), '2026-09-16T10:00:09'),
    (('data', 'priceUsd'), float('nan')), (('data', 'usdAmount'), float('inf')),
    (('data', 'marketCapUsd'), float('nan')), (('data', 'marketCapUsd'), -1),
    (('data', 'usdAmount'), -5), (('data', 'usdAmount'), True),
])
def test_pump_callouts_and_invalid_trade_identity_never_become_signals(path, value):
    relay = copy.deepcopy(RELAY)
    target = relay
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert pump_item(relay) is None


def test_socket_pump_handler_wakes_runner_and_keeps_receipt_identity(monkeypatch):
    handlers = {}
    class Socket:
        def __init__(self, **kw): pass
        def event(self, fn): return fn
        def on(self, name):
            def register(fn): handlers[name] = fn; return fn
            return register
    monkeypatch.setitem(sys.modules, 'socketio', types.SimpleNamespace(Client=Socket))
    monkeypatch.setattr('threading.Thread.start', lambda self: None)
    monkeypatch.setattr('sniper.util.clock.record_real_utc', lambda *a: None)
    feed = J.J7WalletsFeed('test')
    feed._mono_fn = lambda: 123
    feed.start()
    assert 'pump_event' in handlers and 'tracked_pump' in handlers
    handlers['tracked_pump']({'pump_users': [{'wallet': WALLET}]})
    handlers['pump_event'](RELAY)
    assert feed.wait(0)
    items, _ = feed.drain()
    assert len(items) == 1 and items[0]['delivery_lane'] == 'j7_pump'
    receipt = feed.take_receipts()[0]
    identity = json.loads(receipt['identity_json'])
    assert (identity['source_user_id'], identity['tx_sig']) == (UID, SIG)
    assert receipt['received_mono_ns'] == 123
    assert feed.status()['pump_tracked_count'] == 1
    assert feed.status()['pump_trades_total'] == 1


def test_pump_trade_reaches_existing_hook_only_after_chain_approval(monkeypatch):
    from sniper.services import memecoin_real_runner as rr
    from sniper.settings import memecoin as cfg
    c = sqlite3.connect(':memory:'); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.add_wallet(c, UID, 'SossaDotKek', source='pump')
    ev = normalize_feed_item(pump_item(RELAY), NOW, None)
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_ROSTER_GATE_ENABLED', True)
    calls = []
    monkeypatch.setattr(rr, 'real_entry_hook', lambda conn, event, ctx, **kw:
                        calls.append(event) or {'status': 'filled'})
    hook = rr.make_real_hook(None, {'solana': object()}, now_fn=lambda: NOW,
                            trade_size=10, max_age_s=60)
    assert hook(c, ev)['status'] == 'not_approved'
    assert m.set_wallet_chain_armed(c, UID, 'solana', True, force=True,
                                   now_iso='2026-09-16T10:00:00+00:00')['ok']
    assert hook(c, ev)['status'] == 'filled'
    assert len(calls) == 1 and calls[0].source_user_id == UID
    old = copy.deepcopy(ev); old.created_at = '2026-09-16T09:59:59Z'
    assert hook(c, old)['status'] == 'pre_arm_backlog'
    assert len(calls) == 1
    c.close()


def test_pump_redelivery_does_not_repeat_funded_evaluation(monkeypatch):
    from sniper.services import memecoin_runner as runner
    from sniper.settings import memecoin as cfg
    item = pump_item(RELAY)
    c = sqlite3.connect(':memory:'); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); m.add_wallet(c, UID, 'SossaDotKek', source='pump')
    class Feed:
        def drain(self): return [item], None
        def status(self): return {}
    monkeypatch.setattr(J, 'j7_feed_or_none', lambda **kw: Feed())
    monkeypatch.setattr(cfg, 'MEMECOIN_SIGNAL_SOURCE', 'j7')
    monkeypatch.setattr(cfg, 'MEMECOIN_TG_ALERTS', False)
    monkeypatch.setattr('sniper.services.memecoin_copy_trader.copy_event_to_arms', lambda *a, **kw: {})
    calls = []
    for _ in range(2):
        runner.run_once(c, None, 10, now_fn=lambda: NOW, real_now_fn=lambda: NOW,
                        real_hook=lambda conn, ev: calls.append(ev) or {'status': 'not_approved'},
                        stats_fn=lambda *a, **kw: {}, entry_max_age_s=60)
    assert len(calls) == 1
    stored = c.execute('SELECT source_user_id,delivery_lane FROM memecoin_events').fetchall()
    assert [tuple(row) for row in stored] == [(UID, 'j7_pump')]
    c.close()
