import json
import sqlite3
import time
from dataclasses import asdict
from types import SimpleNamespace

import base58
import pytest

from sniper.feeds.fomo_feed import CopyEvent
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_entry_opportunity as policy
from sniper.services import memecoin_real_runner as R
from sniper.services.memecoin_slot_signal import HEAD
from sniper.settings import memecoin as cfg

ARM = '2026-10-18T17:00:00+00:00'
NOW = '2026-10-18T17:00:01+00:00'
OWNER = base58.b58encode(bytes([21]) * 32).decode()
UID = 'pump:' + OWNER
MINT = base58.b58encode(bytes([22]) * 32).decode()
SIG = base58.b58encode(bytes([23]) * 64).decode()


@pytest.fixture
def env(monkeypatch):
    c = sqlite3.connect(':memory:')
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.add_wallet(c, UID, 'source')
    c.execute("UPDATE memecoin_wallets SET real_approved=1,real_chains='solana' WHERE source_user_id=?", (UID,))
    c.execute("INSERT INTO memecoin_wallet_approvals(source_user_id,action,created_at,chain) VALUES(?,'chain_arm',?,'solana')", (UID, ARM))
    c.commit()
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_SOLANA_FIRST_BUY_WINDOW', True)
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_SOLANA_ENTRY_EXCEPTIONS_JSON', '{}')
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_INFRA_PROVIDER', 'helius')
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_ROSTER_GATE_ENABLED', True)
    monkeypatch.setitem(HEAD, 'slot', 101)
    monkeypatch.setitem(HEAD, 'mono', time.monotonic())
    monkeypatch.setattr(R, '_start_trace_best_effort', lambda *a, **k: None)
    monkeypatch.setattr(R, '_persist_terminal_classification_best_effort', lambda *a, **k: None)
    return c


def event(**updates):
    values = dict(source_user_id=UID, source_handle='source', source_wallet=OWNER,
        mint=MINT, ticker='T', side='buy', fomo_event_id='onchain:'+SIG,
        source_block_number=100, source_pre_atomic=0, created_at=NOW, received_at=NOW,
        network_id='solana', signal_source='onchain_watch', their_price=1.,
        their_market_cap=500_000., their_size_usd=1000., lag_s=0.)
    values.update(updates)
    if 'source_initiation' not in values:
        values['source_initiation'] = dict(schema_version=1, proof_version='direct-wallet-paid-v1',
            network_id='solana', tx_hash=values['fomo_event_id'].split(':')[1],
            block_number=values['source_block_number'], source_user_id=values['source_user_id'],
            mint=values['mint'], beneficiary=values['source_wallet'], payer=values['source_wallet'],
            acquired_atomic='100', payment_atomic='10')
    return SimpleNamespace(**values)


def hook():
    return R.make_real_hook(None, {'solana': {}}, now_fn=lambda: NOW,
        trade_size=50., max_age_s=10., resolve_fn=lambda *_a, **_k: 'solana')


def test_raw_verified_first_is_consumed_before_startup_refusal(env, monkeypatch):
    monkeypatch.setattr(R, 'source_initiation_refusal', lambda *a: None)
    monkeypatch.setattr(R, '_entry_startup_refusal', lambda *a: {'status':'rejected', 'reason':'startup_hold'})
    result = hook()(env, event())
    assert result['reason'] == 'startup_hold'
    assert env.execute('SELECT source_signature FROM memecoin_first_source_buys').fetchone()[0] == SIG


def test_unproven_observation_does_not_consume_first(env, monkeypatch):
    monkeypatch.setattr(R, '_entry_startup_refusal', lambda *a: None)
    result = hook().raw(env, event(source_initiation=None))
    assert result['status'] == 'source_initiation_unknown'
    assert env.execute('SELECT COUNT(*) FROM memecoin_first_source_buys').fetchone()[0] == 0


@pytest.mark.parametrize('entry', ['adapter', 'raw'])
def test_unproven_independent_feed_cannot_poison_first_claim(env, monkeypatch, entry):
    missing = event(signal_source='follow_activity', fomo_event_id='swap:raybot:'+SIG,
        source_initiation=None, source_block_number=None, source_pre_atomic=None)
    monkeypatch.setattr(R, '_entry_startup_refusal', lambda *a: None)
    if entry == 'raw':
        monkeypatch.setattr(R, 'entry_route_closed', lambda *a: False)
        result = hook().raw(env, missing)
        assert result['reason'] == 'missing_receipt_proof'
    else:
        assert policy.event_window(env, missing, observed_at=NOW)[1] == 'missing_receipt_proof'
    assert env.execute('SELECT COUNT(*) FROM memecoin_first_source_buys').fetchone()[0] == 0
    later_sig = base58.b58encode(bytes([24]) * 64).decode()
    proven = event(fomo_event_id='onchain:'+later_sig)
    window, why = policy.event_window(env, proven, observed_at=NOW)
    assert why is None and window['first_source_signature'] == later_sig


def test_unarmed_observation_does_not_consume_first(env, monkeypatch):
    monkeypatch.setattr(R, '_entry_startup_refusal', lambda *a: None)
    monkeypatch.setattr(R, 'source_initiation_refusal', lambda *a: None)
    env.execute('UPDATE memecoin_wallets SET real_approved=0'); env.commit()
    result = hook().raw(env, event())
    assert result['status'] == 'not_approved'
    assert env.execute('SELECT COUNT(*) FROM memecoin_first_source_buys').fetchone()[0] == 0


@pytest.mark.parametrize('entry', ['raw', 'shared'])
def test_canonical_j7_shadow_never_consumes_funded_first(env, monkeypatch, entry):
    monkeypatch.setattr(R, 'source_initiation_refusal', lambda *a: None)
    monkeypatch.setattr(R, '_entry_startup_refusal', lambda *a: None)
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO', 0.)
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_MAX_SELL_SHARE_PCT', 0.)
    ev = event(delivery_lane=R.J7_SHADOW_LANE)
    if entry == 'raw':
        monkeypatch.setattr(R, 'entry_route_closed', lambda *a: False)
        monkeypatch.setattr(R, 'real_entry_hook', lambda *a,**k: {'status':'shadow'})
        hook().raw(env, ev)
    else:
        result = R.real_entry_hook(env, ev, {'solana': {}}, now_fn=lambda:NOW,
            trade_size=50., max_age_s=10.,
            resolved_identity=R.ResolvedEntryIdentity('solana','resolved','test'),
            classification_context={'effective_mcap':500_000., 'assumed_micro':False,
                'consensus_n_60m':1, 'market_stats':{'price':1., 'liquidity':10_000.}})
        assert result['status'] == 'shadow'
    assert env.execute('SELECT COUNT(*) FROM memecoin_first_source_buys').fetchone()[0] == 0


def test_shadow_miss_never_queues_funded_retry(env, monkeypatch):
    from sniper.services import memecoin_dip_retry as dip
    ev = queued_event()
    ev.delivery_lane = R.J7_SHADOW_LANE
    monkeypatch.setattr(dip, 'maybe_enqueue', lambda *a, **k: pytest.fail('shadow dip queued'))
    monkeypatch.setattr(R, '_is_transient_entry_miss', lambda _:True)
    monkeypatch.setattr(m, 'enqueue_entry_retry', lambda *a, **k: pytest.fail('shadow retry queued'))
    assert R._maybe_enqueue_retry(env, ev, {'status':'skipped_chase'}, NOW) is False


@pytest.mark.parametrize('first_buy_window,delta', [(True, 1), (False, 1), (True, 0)])
def test_shared_execution_keeps_head_freshness_without_slot_expiry_after_market_work(env, monkeypatch, first_buy_window, delta):
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_SOLANA_FIRST_BUY_WINDOW', first_buy_window)
    monkeypatch.setitem(HEAD, 'slot', 100)
    if delta == 0:
        monkeypatch.setattr(cfg, 'MEMECOIN_REAL_SOLANA_ENTRY_EXCEPTIONS_JSON',
                            json.dumps({UID: dict(max_slot_delta=0, allow_later_buys=False)}))
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO', 0.)
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_MAX_SELL_SHARE_PCT', 0.)
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_MIN_SOURCE_BUY_USD', 0.)
    monkeypatch.setattr(R, '_entry_startup_refusal', lambda *a: None)
    seen = []
    def execute(*a, entry_min_slot=None, entry_max_slot=None, entry_head_fn=None, **kw):
        seen.append((entry_min_slot, entry_max_slot, entry_head_fn))
        return {'status':'rejected', 'reason':'captured_automatic'}
    def leg(*a, execute_fn, **kw):
        HEAD['slot'] = 200
        return execute_fn(None, 1, MINT, 50., NOW)
    monkeypatch.setattr(R, 'real_entry', leg)
    result = R.real_entry_hook(env, event(), {'solana': {'execute_fn':execute,
        'usdc_fn':lambda:300., 'sol_fn':lambda:1., 'token_safety_fn':lambda *a,**k:(True,'ok')}},
        now_fn=lambda: NOW, trade_size=50., max_age_s=10.,
        resolved_identity=R.ResolvedEntryIdentity('solana','resolved','test'),
        classification_context={'effective_mcap':500_000., 'assumed_micro':False,
            'consensus_n_60m':1, 'market_stats':{'price':1., 'liquidity':10_000.}})
    assert result['reason'] == 'captured_automatic'
    assert len(seen) == 1 and seen[0][:2] == (None, None)
    assert callable(seen[0][2]) and seen[0][2]()[0] == 200


def test_shared_manual_execution_has_no_automatic_source_deadline(env, monkeypatch):
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_SOLANA_FIRST_BUY_WINDOW', False)
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO', 0.)
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_MAX_SELL_SHARE_PCT', 0.)
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_MIN_SOURCE_BUY_USD', 0.)
    monkeypatch.setattr(R, '_entry_startup_refusal', lambda *a: None)
    seen = []
    def execute(*a, **kw):
        seen.append(kw)
        return {'status': 'rejected', 'reason': 'captured_manual'}
    monkeypatch.setattr(R, 'real_entry', lambda *a, execute_fn, **kw: execute_fn(None, 1, MINT, 50., NOW))
    result = R.real_entry_hook(env, event(signal_source='manual'), {'solana': {'execute_fn': execute,
        'usdc_fn': lambda: 300., 'sol_fn': lambda: 1., 'token_safety_fn': lambda *a, **k: (True, 'ok')}},
        now_fn=lambda: NOW, trade_size=50., max_age_s=10.,
        resolved_identity=R.ResolvedEntryIdentity('solana', 'resolved', 'test'),
        classification_context={'effective_mcap': 500_000., 'assumed_micro': False,
            'consensus_n_60m': 1, 'market_stats': {'price': 1., 'liquidity': 10_000.}})
    assert result['reason'] == 'captured_manual'
    assert seen == [{}]


@pytest.mark.parametrize('network,source', [('solana','manual'), ('4663','onchain_watch')])
def test_manual_and_evm_skip_window(env, monkeypatch, network, source):
    monkeypatch.setattr(policy, 'event_window', lambda *a, **k: pytest.fail('unrelated entry reached Solana policy'))
    monkeypatch.setattr(R, '_entry_startup_refusal', lambda *a: {'status':'rejected', 'reason':'existing_startup'})
    result = R.real_entry_hook(env, event(network_id=network, signal_source=source), {},
        now_fn=lambda: NOW, trade_size=50., max_age_s=10.,
        resolved_identity=R.ResolvedEntryIdentity(network,'resolved','test'))
    assert result['reason'] == 'existing_startup'


def queued_event(network='solana', source='follow_activity'):
    return CopyEvent(source_user_id=UID, source_handle='source', mint=MINT if network=='solana' else '0x'+'ab'*20,
        ticker='T', side='buy', their_price=1., their_market_cap=500_000., their_size_usd=1000.,
        is_first_buy=True, fomo_event_id='onchain:'+SIG, created_at=NOW, received_at=NOW,
        lag_s=0., signal_source=source, network_id=network)


@pytest.mark.parametrize('network,source,expected', [('solana','follow_activity',0), ('solana','manual',1), ('4663','follow_activity',1)])
def test_old_timer_rows_cannot_reoffer_automatic_sol_buys(env, monkeypatch, network, source, expected):
    monkeypatch.setattr(cfg, 'MEMECOIN_ENTRY_RETRY_WINDOW_S', 90.)
    monkeypatch.setattr(R, '_run_dip_hits', lambda *a, **k: None)
    ev = queued_event(network, source)
    m.enqueue_entry_retry(env, ev.fomo_event_id, ev.mint, network, json.dumps(asdict(ev)), NOW, retry_after_s=0)
    seen = []
    R.run_entry_retry_lane(env, lambda *a: seen.append(1) or {'status':'filled'},
        '2026-10-18T17:00:03+00:00', include_dips=False)
    assert len(seen) == expected
    assert env.execute('SELECT COUNT(*) FROM memecoin_entry_retry_queue').fetchone()[0] == 0


@pytest.mark.parametrize('network', ['solana', '1399811149', 'sol'])
def test_old_dip_row_is_retired_without_another_quote_or_attempt(env, monkeypatch, network):
    from sniper.services import memecoin_dip_retry as dip
    ev = queued_event()
    m.enqueue_dip_retry(env, ev.fomo_event_id, ev.mint, network, json.dumps(asdict(ev)), NOW,
        json.dumps({'deadline':9999999999, 'signal_at':1792342801., 'source_user_id':UID}))
    monkeypatch.setattr(dip, 'lane_alive', lambda: False)
    monkeypatch.setattr(dip, 'page_stale_lane', lambda _: None)
    monkeypatch.setattr(dip, 'run_hits', lambda c,*a,**k: {'hits':0,'dropped':0} if not m.dip_retry_rows(c) else pytest.fail('old Solana dip reached quote-hit dispatch'))
    R._run_dip_hits(env, lambda *a: pytest.fail('old dip reoffered'), NOW)
    assert m.dip_retry_rows(env) == []
    assert env.execute('SELECT status FROM memecoin_dip_retry_log').fetchone()[0] == 'policy_disabled'


@pytest.mark.parametrize('timer', ['attempt','revival'])
def test_no_timer_path_can_resume_even_within_source_window(env, monkeypatch, timer):
    monkeypatch.setattr(R, '_entry_startup_refusal', lambda *a: pytest.fail('timer reached entry work'))
    h = hook()
    ev = event(revival_reask=timer=='revival')
    result = h.attempt(env, ev) if timer=='attempt' else h(env, ev)
    assert result['reason'] == 'source_buy_timer_retry_disabled'
