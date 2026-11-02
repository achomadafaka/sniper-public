"""Spendable wrapped cash bounds the clip; spare native belongs in sizing equity."""
import sqlite3
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_evm_risk as risk
from sniper.services import memecoin_real_runner as runner
from sniper.settings import memecoin as cfg


@pytest.fixture
def ledger(monkeypatch):
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_ALLOWLIST', {'56','4663','5042'})
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_SIZING', 'bankroll')
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_SIZING_BY_CHAIN', {})
    monkeypatch.setattr(cfg, 'MEMECOIN_MAX_CONCURRENT_BY_CHAIN', {'56': 2, '4663': 2})
    monkeypatch.setattr(cfg, 'MEMECOIN_EQUITY_SLOT_TIERS_ENABLED', False)
    monkeypatch.setattr(cfg, 'MEMECOIN_EQUITY_SLOT_TIERS_ENABLED_BY_CHAIN', {})
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_GAS_RESERVE_MIN_NATIVE_BY_CHAIN', {'56': .01, '4663': .01})
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_MAX_CLIP_USD_BY_CHAIN', {})
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_MAX_CLIP_USD', 0)
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_TIER_CLIP_USD_BY_CHAIN', {})
    c = sqlite3.connect(':memory:'); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    yield c
    c.close()


def context(c, *, cash=125, native=.11, price=800, chain='56'):
    return risk.build_evm_risk_context(c, usdc_balance=cash, native_balance=native,
        native_quote_price_usd=price, day='2026-10-03', network_id=chain)


def test_spare_native_increases_equity_without_claiming_it_is_wrapped_cash(ledger):
    ctx = context(ledger)
    assert ctx.native_sizing_equity_usd == pytest.approx(80)
    assert ctx.trade_size_usd == 102.50
    assert ctx.usdc_balance == 125


def test_native_value_never_lifts_clip_above_spendable_wrapped_cash(ledger):
    assert context(ledger, cash=30).trade_size_usd == 30


@pytest.mark.parametrize('price', [None, 0, -1, float('nan'), float('inf'), True])
def test_unknown_or_invalid_price_cannot_add_equity(ledger, price):
    assert context(ledger, price=price).trade_size_usd == 62.50


def test_unknown_native_cannot_add_equity(ledger):
    assert context(ledger, native=None).native_sizing_equity_usd == 0


def test_native_inside_gas_reserve_adds_nothing(ledger):
    assert context(ledger, native=.009).native_sizing_equity_usd == 0


def test_arc_same_asset_never_counts_native_twice(ledger):
    assert context(ledger, chain='5042', native=125, price=1).native_sizing_equity_usd == 0


def test_reserved_quote_cash_is_counted_once_in_equity(ledger):
    m.reserve_pending(ledger, mint='0xother', source_user_id='u', strategy='alpha_b',
        notional=25, reserved_at='2026-10-03T12:00:00+00:00', network_id='56')
    ctx = context(ledger)
    assert ctx.trade_size_usd == 100  # equity 205 / 2, bounded by free wrapped 125 - 25


def test_trial_cap_and_existing_per_entry_cap_still_bind(ledger, monkeypatch):
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_MAX_CLIP_USD_BY_CHAIN', {'56': 70})
    assert context(ledger).trade_size_usd == 70
    assert risk.build_evm_risk_context(ledger, usdc_balance=125, native_balance=.11,
        native_quote_price_usd=800, day='2026-10-03', network_id='56', size_cap_usd=20).trade_size_usd == 20


@pytest.mark.parametrize('age,price,want', [(20,800,800), (1001,800,None),
    (None,800,None), (20,float('nan'),None)])
def test_fresh_cached_price_reaches_leg_without_another_provider_call(monkeypatch, age, price, want):
    from sniper.services import memecoin_evm_leg as leg
    monkeypatch.setattr(runner, '_gate_balance_cache', {})
    monkeypatch.setattr(runner, '_entry_prereads_for', lambda *a: None)
    calls=[]
    def execute(c,e,**kw): calls.append(kw); return {'status':'unknown'}
    monkeypatch.setattr(leg,'evm_real_entry',execute)
    ctx={'usdc_fn':lambda:125,'native_fn':lambda:.11,
        'quote_price_peek_fn':lambda:{'px':price,'age_ms':age},
        'token_safety_fn':lambda *a:None,'execute_fn':lambda *a:None}
    runner._make_evm_entry_fn({'56':ctx})(None,SimpleNamespace(mint='0xabc'),
        route='56',day='2026-10-03',now_iso='2026-10-03T12:00:00+00:00',trade_size=100)
    assert calls[0]['native_quote_price_usd'] == want


def test_dashboard_next_clip_matches_native_inclusive_funded_sizing(ledger):
    from test_memecoin_real_deploy_routes import _client
    client,_ = _client({'acct_stats_fn':lambda mints:{}, 'acct_balances_fn':lambda:{'56':
        {'cash_usd':125,'native':.11,'native_usd':88}}})
    result=client.get('/api/memecoin/real/accounting').get_json()['by_network']['56']
    assert result['clip_next'] == 102.50
    assert result['sizing_equity'] == 205


def test_reservation_seals_the_native_inclusive_clip(ledger, monkeypatch):
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_ENABLED', True)
    monkeypatch.setattr(cfg, 'MEMECOIN_DAILY_LOSS_LIMIT_USD', 0)
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_ROSTER_GATE_ENABLED', False)
    monkeypatch.setattr(cfg, 'MEMECOIN_MAX_TOTAL_EXPOSURE_USD', 1000)
    decision, pid = risk.reserve_entry_evm(ledger, mint='0xnew', source_user_id='u',
        strategy='alpha_b', network_id='56', day='2026-10-03', usdc_balance=125,
        native_balance=.11, native_quote_price_usd=800, now_iso='2026-10-03T12:00:00+00:00')
    assert decision.allowed, decision.reason
    assert decision.approved_size_usdc == 102.50
    assert m.pending_row(ledger, pid)['reserved_notional_usd'] == 102.50
