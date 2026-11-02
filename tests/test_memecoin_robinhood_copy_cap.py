"""Operator's 30% Robinhood maximum binds every shared signed-output cap."""
import os
import subprocess
import sys

import pytest

from sniper.settings import memecoin as cfg


@pytest.fixture(autouse=True)
def caps(monkeypatch):
    monkeypatch.setattr(cfg,'MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT',35.)
    monkeypatch.setattr(cfg,'MEMECOIN_REAL_4663_MAX_COPY_PREMIUM_PCT',30.,raising=False)
    monkeypatch.setattr(cfg,'MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT_BY_WALLET',{})


@pytest.mark.parametrize('uid',[None,'ordinary','overridden'])
def test_robinhood_maximum_binds_global_and_wallet_caps(monkeypatch, uid):
    monkeypatch.setattr(cfg,'MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT_BY_WALLET',{'overridden':50.})
    assert cfg.copy_premium_cap(uid,'4663')[0] == 30.


def test_lower_wallet_cap_and_other_chains_keep_their_limits(monkeypatch):
    monkeypatch.setattr(cfg,'MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT_BY_WALLET',{'4663:u':20.,'solana:u':150.})
    assert cfg.copy_premium_cap('u','4663') == (20.,'wallet')
    assert cfg.copy_premium_cap('u','solana') == (150.,'wallet')
    for chain in ('1','56','8453','5042'):
        assert cfg.copy_premium_cap('u',chain) == (35.,'global')


@pytest.mark.parametrize('global_cap',[0.,20.])
def test_robinhood_limit_never_arms_or_raises_the_global_cap(monkeypatch, global_cap):
    monkeypatch.setattr(cfg,'MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT',global_cap)
    assert cfg.copy_premium_cap(None,4663)[0] == global_cap


def test_disabled_chain_limit_preserves_the_old_global_cap(monkeypatch):
    monkeypatch.setattr(cfg,'MEMECOIN_REAL_4663_MAX_COPY_PREMIUM_PCT',0.)
    assert cfg.copy_premium_cap(None,'4663') == (35.,'global')


@pytest.mark.parametrize('value',['-1','nan','inf','bad'])
def test_invalid_chain_limit_refuses_startup(value):
    env = dict(os.environ,MEMECOIN_REAL_4663_MAX_COPY_PREMIUM_PCT=value)
    result = subprocess.run([sys.executable,'-c','from sniper.settings import memecoin'],
                            env=env,capture_output=True,text=True)
    assert result.returncode != 0
    assert 'MEMECOIN_REAL_4663_MAX_COPY_PREMIUM_PCT' in result.stderr


def test_missing_chain_limit_defaults_to_the_operator_30_percent():
    env = dict(os.environ)
    env.pop('MEMECOIN_REAL_4663_MAX_COPY_PREMIUM_PCT',None)
    result = subprocess.run([sys.executable,'-c',
        'from sniper.settings import memecoin as c; print(c.MEMECOIN_REAL_4663_MAX_COPY_PREMIUM_PCT)'],
        env=env,capture_output=True,text=True,check=True)
    assert float(result.stdout) == 30.


def test_override_telemetry_compares_with_the_actual_chain_bound(monkeypatch):
    monkeypatch.setattr(cfg,'MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT_BY_WALLET',{'u':50.})
    facts = cfg.copy_cap_override_fields('u','4663',lane='evm',price=1.31,average=1.)
    assert facts['cap_without_override'] == 30.
    assert facts['cap_used'] == 30.
    assert facts['override_applied'] is False


@pytest.mark.parametrize('limit',[0.,30.])
def test_quote_under_30_with_signed_worst_case_over_30_refuses_before_approval(monkeypatch, limit):
    from sniper.execution.evm_swap_executor import execute_entry_evm
    from sniper.persistence import db_memecoin as m
    from test_memecoin_evm_entry_executor import _QUOTE, _c, _seams
    monkeypatch.setattr(cfg,'MEMECOIN_REAL_4663_MAX_COPY_PREMIUM_PCT',limit)
    c = _c()
    try:
        pid = m.reserve_pending(c,mint='0xtok',source_user_id='u',strategy='alpha_b',notional=10.,
                                reserved_at='2026-10-09T20:00:00Z',network_id='4663',source_price=1.)
        q = {**_QUOTE,'buyAmount':'8000000','minBuyAmount':'7500000'}  # +25% quote, +33.3% minimum
        seams,calls = _seams(quote=q,token=((0.,6),(8.,6)))
        result = execute_entry_evm(c,pending_id=pid,mint='0xtok',network_id='4663',usd_amount=10.,
                                  strategy='alpha_b',source_user_id='u',now_iso='t',**seams)
        if limit:
            assert result['status'] == 'skipped_chase' and 'cap 30.0%' in result['reason']
            assert calls['approve'] == [] and calls['sign'] == calls['send'] == 0
        else:
            assert 'copy price' not in str(result.get('reason')) and calls['sign'] > 0
    finally:
        c.close()
