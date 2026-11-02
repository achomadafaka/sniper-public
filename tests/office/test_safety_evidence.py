"""Holder, cluster and LP evidence on sheets: reported, dated, and null when not known."""
import base64
import json
import sqlite3
import struct
from datetime import datetime, timezone

import base58
import pytest

from sniper.office import pool_state_rpc as P, safety_evidence as SE

MINT, POOL = 'M' * 32, 'P' * 32
AT = 1793450000.0


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def store(tmp_path, rows):
    c = sqlite3.connect(tmp_path / 'cluster.db')
    c.execute('CREATE TABLE cluster_snapshots(snapshot_id TEXT,mint TEXT,snapshot_at TEXT)')
    c.execute('CREATE TABLE cluster_decisions(decision_id TEXT,snapshot_id TEXT,source_path TEXT,allowed INTEGER,reason TEXT,policy_id TEXT,snapshot_at TEXT,completed_at TEXT,metrics_json TEXT)')
    c.execute('INSERT INTO cluster_snapshots VALUES(?,?,?)', ('s', MINT, iso(AT - 100)))
    for i, (allowed, reason, done, metrics) in enumerate(rows):
        c.execute('INSERT INTO cluster_decisions VALUES(?,?,?,?,?,?,?,?,?)', (str(i), 's', 'desk', allowed, reason, 'p', iso(AT - 100), iso(done), metrics))
    c.commit(); c.close()
    return tmp_path


METRICS = json.dumps(dict(attempted_supply=880, clustered_of_attempted=0.0, coverage=1.0, eligible=19, resolved=19, top_funder_of_attempted=0.15))


def test_a_decision_gives_cluster_hit_with_dated_shares_and_a_measured_zero_stays_zero(tmp_path):
    out = SE.cluster_evidence(store(tmp_path, [(1, 'allowed', AT - 50, METRICS)]), MINT, AT)
    assert out['state'] == 'known' and out['cluster_hit'] is False and out['age_s'] == 50 and out['denominator'] == 'attempted_supply'
    assert out['clustered_pct_of_attempted'] == 0.0 and abs(out['top_funder_pct_of_attempted'] - 15.0) < 1e-9 and out['attempted_supply_atomic'] == 880


def test_a_refused_decision_is_a_hit_and_a_missing_metric_is_null(tmp_path):
    out = SE.cluster_evidence(store(tmp_path, [(0, 'cluster_share_high', AT - 5, '{}')]), MINT, AT)
    assert out['cluster_hit'] is True and out['reason'] == 'cluster_share_high' and out['clustered_pct_of_attempted'] is None


def test_a_decision_after_the_as_of_is_not_used(tmp_path):
    d = store(tmp_path, [(0, 'x', AT + 5, METRICS), (1, 'allowed', AT - 5, METRICS)])
    assert SE.cluster_evidence(d, MINT, AT)['cluster_hit'] is False
    (tmp_path / 'cluster.db').unlink()
    d = store(tmp_path, [(0, 'x', AT + 5, METRICS)])
    assert SE.cluster_evidence(d, MINT, AT) == dict(state='unknown', reason='only_later_decisions', cluster_hit=None)


def test_no_decision_and_no_store_are_unknown_not_clear(tmp_path):
    assert SE.cluster_evidence(tmp_path / 'absent', MINT, AT)['reason'] == 'cluster_store_unreadable'
    assert SE.cluster_evidence(store(tmp_path, []), MINT, AT) == dict(state='unknown', reason='no_decision_by_as_of', cluster_hit=None)


MINT32 = base58.b58encode(b'\5' * 32).decode()
LP32 = base58.b58encode(b'\2' * 32).decode()


def reader(tmp_path, supply, largest=(), owner='OWNER'):
    calls = []
    raw = base64.b64encode(b'\0' * 11 + b'\1' * 32 + b'\5' * 32 + base58.b58decode(P.WSOL_MINT) + b'\2' * 32 + b'\3' * 32 + b'\4' * 32 + struct.pack('<Q', 7)).decode()

    def post(url, body):
        calls.append(body['method'])
        if body['method'] == 'getTokenSupply':
            return dict(result=dict(context=dict(slot=9), value=dict(amount=str(supply))))
        if body['method'] == 'getTokenLargestAccounts':
            return dict(result=dict(value=list(largest)))
        if body['params'][0] == POOL:
            return dict(result=dict(value=dict(owner=P.PUMP_AMM_PROGRAM, data=[raw, 'base64'])))
        return dict(result=dict(value=dict(data=dict(parsed=dict(info=dict(owner=owner))))))
    return P.PoolStateReader('u', tmp_path / 'c.json', post=post, clock=lambda: AT), calls


def test_zero_lp_supply_is_burned_at_its_slot_and_a_reread_uses_the_cache(tmp_path):
    r, calls = reader(tmp_path, 0)
    out = SE.lp_evidence(r, POOL, MINT32)
    assert out['lp_locked_or_burned'] is True and out['slot'] == 9 and out['lp_mint'] == LP32 and out['read_at'] == AT and out['lp_supply_recorded_atomic'] == 7
    SE.lp_evidence(r, POOL, MINT32)
    assert calls == ['getAccountInfo', 'getTokenSupply']


def test_outstanding_lp_is_not_called_locked_and_names_its_holder(tmp_path):
    r, _ = reader(tmp_path, 5, [dict(address='A', amount='5')])
    out = SE.lp_evidence(r, POOL, MINT32)
    assert out['lp_locked_or_burned'] is None and out['custody'] == 'supply_outstanding_lock_not_sourced' and out['largest_holder'] == dict(token_account='A', amount_atomic=5, owner='OWNER')


def test_a_wrong_pool_a_failed_read_and_no_reader_are_unknown_and_charged(tmp_path):
    r, _ = reader(tmp_path, 0)
    assert SE.lp_evidence(r, POOL, 'other')['reason'] == 'lp_pool_mismatch'
    assert SE.lp_evidence(None, POOL, MINT32)['reason'] == 'lp_reader_off'
    def boom(url, body): raise OSError('down')
    bad = P.PoolStateReader('u', tmp_path / 'd.json', post=boom, clock=lambda: AT)
    out = SE.lp_evidence(bad, POOL, MINT32)
    assert out['state'] == 'unknown' and out['reason'].startswith('rpc_error') and json.load(open(tmp_path / 'd.json'))['credits'] == P.CREDITS_PER_CALL
    capped = P.PoolStateReader('u', tmp_path / 'e.json', post=boom, clock=lambda: AT, cap_credits=1)
    assert SE.lp_evidence(capped, POOL, MINT32)['reason'] == 'rpc_cap'


def test_the_risk_check_reads_none_of_it():
    from pathlib import Path
    import sniper.office.risk as R
    source = Path(R.__file__).read_text()
    assert 'cluster_hit' not in source and 'lp_locked_or_burned' not in source and 'safety_evidence' not in source


def test_price_basis_shows_both_prices_and_their_ratio():
    out = SE.price_basis(dict(base_reserve_atomic='1000000000', quote_reserve_atomic='946000000', virtual_quote_atomic='175000000', base_decimals=6, sol_usd=100.0))
    assert out['basis'] == 'virtual_inclusive' and abs(out['virtual_over_real'] - 1.185) < 1e-3
    assert abs(out['real_reserve_price_usd'] - 0.0946) < 1e-9 and abs(out['virtual_inclusive_price_usd'] - 0.1121) < 1e-9


def test_price_basis_unusable_inputs_are_null_and_a_proven_zero_virtual_is_ratio_one():
    assert SE.price_basis(None)['state'] == 'unknown' and SE.price_basis(dict(base_reserve_atomic='0', quote_reserve_atomic='1', virtual_quote_atomic='0', base_decimals=6, sol_usd=1.))['virtual_over_real'] is None
    assert SE.price_basis(dict(base_reserve_atomic='5', quote_reserve_atomic='7', virtual_quote_atomic='0', base_decimals=0, sol_usd=1.))['virtual_over_real'] == 1.0


def test_newest_decision_is_chronological_not_text_order(tmp_path):
    d = store(tmp_path, [])
    c = sqlite3.connect(d / 'cluster.db')
    for i, (allowed, done) in enumerate(((0, '2026-10-31T12:00:00+02:00'), (1, '2026-10-31T11:00:00+00:00'))):
        c.execute('INSERT INTO cluster_decisions VALUES(?,?,?,?,?,?,?,?,?)', (str(i), 's', 'desk', allowed, 'r', 'p', done, done, '{}'))
    c.commit(); c.close()
    assert SE.cluster_evidence(d, MINT, datetime(2026, 10, 31, 12, tzinfo=timezone.utc).timestamp())['cluster_hit'] is False


class _Fixed:
    def __init__(self, got): self.got = got
    def read_lp(self, pool, mint): return dict(self.got)


def test_burned_needs_a_positive_integer_slot():
    for slot in (None, 0, -1, True, 9.0):
        out = SE.lp_evidence(_Fixed(dict(lp_mint_supply_atomic=0, slot=slot)), POOL, MINT)
        assert out == dict(state='unknown', reason='lp_slot_unusable', lp_locked_or_burned=None)


def test_cluster_hit_is_the_host_check_and_a_store_disagreement_is_labelled(tmp_path):
    from sniper.services import memecoin_desk_contract as K
    d = store(tmp_path, [(1, 'allowed', AT - 5, METRICS)])
    hit, _, block = SE.build(desk_dir=d, mint=MINT, pool=POOL, as_of=AT, soft=K.soft_facts(dict(cluster=dict(status='kill', reason='cluster_share_high'))), reader=None)
    assert hit is True and block['cluster']['source'] == 'evidence.checks.cluster' and block['cluster']['store_agrees'] is False and block['cluster']['store_decision']['top_funder_pct_of_attempted'] is not None
    assert SE.build(desk_dir=d, mint=MINT, pool=POOL, as_of=AT, soft=K.soft_facts(dict(cluster=dict(status='pass', reason='cluster_clear'))), reader=None)[0] is False
    hit, _, block = SE.build(desk_dir=d, mint=MINT, pool=POOL, as_of=AT, soft=K.soft_facts({}), reader=None)
    assert hit is None and block['cluster']['reason'] == 'not_yet_ready' and block['cluster']['store_agrees'] is None    # a store decision does not fill a missing host check


@pytest.mark.parametrize('decimals', [-1, 19, 1000, True, None])
def test_price_basis_bounds_decimals_and_never_raises(decimals):
    out = SE.price_basis(dict(base_reserve_atomic='5', quote_reserve_atomic='7', virtual_quote_atomic='0', base_decimals=decimals, sol_usd=1.))
    assert out['state'] == 'unknown' and out['virtual_over_real'] is None


def test_price_basis_huge_reserves_are_unknown_not_a_raise():
    assert SE.price_basis(dict(base_reserve_atomic='1', quote_reserve_atomic=str(10 ** 400), virtual_quote_atomic='0', base_decimals=0, sol_usd=1.))['state'] == 'unknown'


def test_dump_group_is_the_host_check_and_free_values_are_unknown():
    assert SE._dump_group(dict(dump_group=dict(status='kill', reason='x'))) == dict(source='evidence.checks.dump_group', status='kill', reason='x')
    assert SE._dump_group(None)['status'] == 'unknown' and SE._dump_group(dict(dump_group=dict(status='odd')))['status'] == 'unknown'


def _tape(tmp_path, rows):
    (tmp_path / 'pumpswap').mkdir(exist_ok=True)
    c = sqlite3.connect(tmp_path / 'pumpswap/tape.db')
    c.execute('CREATE TABLE swaps_ps(pool TEXT,is_buy INTEGER,received_at REAL)')
    c.executemany('INSERT INTO swaps_ps VALUES(?,?,?)', rows)
    c.commit(); c.close()
    return tmp_path


def test_a_sell_after_the_last_priced_swap_is_counted_and_a_later_row_is_not(tmp_path):
    d = _tape(tmp_path, [(POOL, 1, 90.), (POOL, 0, 97.), (POOL, 0, 101.), ('other', 0, 98.)])
    out = SE.unpriced_after_last(d, POOL, dict(last_swap_age_s=10., as_of=100.), 100.)
    assert (out['state'], out['count'], out['sells'], out['newest_age_s']) == ('known', 1, 1, 3.) and 'reference price' in out['note']


def test_no_swap_after_the_last_priced_is_a_known_zero_and_unreadable_is_unknown(tmp_path):
    d = _tape(tmp_path, [(POOL, 1, 90.)])
    out = SE.unpriced_after_last(d, POOL, dict(last_swap_age_s=10., as_of=100.), 100.)
    assert out['state'] == 'known' and out['count'] == 0 and 'note' not in out
    assert SE.unpriced_after_last(tmp_path / 'absent', POOL, dict(last_swap_age_s=1., as_of=100.), 100.)['count'] is None
    assert SE.unpriced_after_last(d, POOL, dict(last_swap_age_s=None, as_of=100.), 100.)['reason'] == 'no_priced_swap'


def test_a_swap_inside_the_age_rounding_is_a_lower_bound_never_a_zero(tmp_path):
    d = _tape(tmp_path, [(POOL, 1, 90.0001), (POOL, 0, 90.0004)])
    out = SE.unpriced_after_last(d, POOL, dict(last_swap_age_s=10., as_of=100.), 100.)
    assert out['state'] == 'lower_bound' and out['reason'] == 'swaps_within_age_rounding'


def test_the_last_priced_time_is_read_from_structures_own_cut(tmp_path):
    d = _tape(tmp_path, [(POOL, 1, 85.), (POOL, 0, 92.)])
    assert SE.unpriced_after_last(d, POOL, dict(last_swap_age_s=5., as_of=90.), 100.)['sells'] == 1
