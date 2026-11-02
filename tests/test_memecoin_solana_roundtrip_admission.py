"""A complete sequential measure can correct an independent quote refusal."""
import time

import pytest
from sniper.execution import solana_swap_executor as ex
from test_memecoin_entry_roundtrip_cap import _c, _Deps


def run_case(monkeypatch, evidence, *, independent_return=8.7):
    monkeypatch.setattr('sniper.settings.memecoin.entry_roundtrip_cap_pct', lambda *_: 10.0)
    conn, pid = _c()
    deps = _Deps(quote_impact=0.01, usd_back=independent_return)
    def measure(buy, sell):
        deps.calls.append('measure')
        assert 'sign' not in deps.calls
        if isinstance(evidence, Exception):
            raise evidence
        return evidence
    result = ex.execute_entry(
        conn, pending_id=pid, mint='MINT', usd_amount=10.0, strategy='alpha_b',
        source_user_id='u1', now_iso='2026-09-11T11:09:34Z',
        quote_fn=deps.quote_fn, reverse_quote_fn=deps.reverse_quote_fn,
        sign_fn=deps.sign_fn, send_fn=deps.send_fn, confirm_fn=deps.confirm_fn,
        usdc_fn=deps.usdc_fn, token_fn=deps.token_fn, impact_cap_pct=15.0,
        roundtrip_measure_fn=measure)
    conn.close()
    return result, deps.calls


def measured(returned='9400000', spent='10000000'):
    return {'version': 'solana_sequential_roundtrip_v1', 'status': 'measured',
            'buy_spent_atomic': spent, 'sell_received_atomic': returned,
            'cost_pct': -999}


def test_complete_measure_replaces_the_estimate_at_the_same_cap(monkeypatch):
    result, calls = run_case(monkeypatch, measured())
    assert result['status'] == 'filled'
    assert result['roundtrip_cost_pct'] == pytest.approx(6.0)
    assert calls.index('measure') < calls.index('sign')


@pytest.mark.parametrize('evidence', [measured('8500000'), measured(spent='20000000'),
    {'status':'unknown', 'reason':'simulation_failed'}, None, RuntimeError('provider'),
    measured(returned='NaN'), measured(returned='-1')])
def test_expensive_or_unproven_measure_does_not_clear_the_cap(monkeypatch, evidence):
    result, calls = run_case(monkeypatch, evidence)
    assert result['status'] == 'aborted'
    assert 'sign' not in calls


def test_an_independent_pass_does_not_add_a_simulation(monkeypatch):
    result, calls = run_case(monkeypatch, RuntimeError('must not run'), independent_return=9.9)
    assert result['status'] == 'filled'
    assert 'measure' not in calls


def test_live_executor_supplies_the_unsigned_measurement_callback(monkeypatch):
    from sniper.execution import solana_roundtrip_measurement as measurement
    from test_memecoin_solana_exit_now_fn_wired import _build_executor
    calls = []
    def measure(buy, sell, *, user, jup_call, rpc, max_transfer_fee_bps=0):
        # the live executor forwards token safety's fee cap so a fee-on-transfer sell can measure
        from sniper.settings import memecoin as cfg
        assert max_transfer_fee_bps == int(cfg.MEMECOIN_SOLANA_MAX_IMMUTABLE_TRANSFER_FEE_BPS)
        calls.append((buy, sell, user))
        assert rpc('simulateTransaction', []) == {}
        return {'status': 'unknown', 'reason': 'fixture'}
    monkeypatch.setattr(measurement, 'measure_roundtrip', measure)
    monkeypatch.setattr(ex, 'execute_entry', lambda conn, **kw: kw['roundtrip_measure_fn']({'buy':1}, {'sell':2}))
    executor = _build_executor([100])
    conn, pid = _c()
    result = executor['execute_fn'](conn, pid, 'MINT', 10, '2026-09-11T11:09:34Z',
        entry_min_slot=100, entry_max_slot=101, entry_head_fn=lambda: (100, time.monotonic()))
    conn.close()
    assert result['reason'] == 'fixture'
    assert len(calls) == 1 and calls[0][2]


@pytest.mark.parametrize('evidence', [
    measured(returned='4900000', spent='5000000'),
    dict(measured(), version='solana_sequential_roundtrip_v0'),
], ids=['half_the_clip', 'other_version'])
def test_a_cheap_measure_of_a_different_trade_does_not_clear_the_cap(monkeypatch, evidence):
    """A sequential measure may overturn a refusal only for THE trade being admitted. A smaller buy
    moves the price less, so its cost is lower by construction; to accept it admits the full clip on
    a cheaper trade's evidence. The mismatch case above spends 20 USDC and is expensive as well, so
    it passes without the size guard. Both cases here are cheap, so only the guard can refuse them."""
    result, calls = run_case(monkeypatch, evidence)
    assert result['status'] == 'aborted'
    assert 'sign' not in calls
