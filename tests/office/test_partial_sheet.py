"""A seat reads what the ticket holds when the full sheet cannot be built; orders still need a known sheet."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sniper.office.partial_sheet import failure_class, partial_sheet

TICKET = json.loads((Path(__file__).resolve().parents[1] / 'fixtures/office/partial_ticket_419P2.json').read_text())      # a real intake ticket (419P2wrV...), PASS, cluster kill
NOW = 1793441000.0


def test_a_real_ticket_returns_its_stored_facts_with_the_rest_typed_unknown():
    out = partial_sheet(TICKET, dict(state='unknown', reason='refresh_failed:StaleSnapshot:intake_mint_unusable'), now_host=NOW)
    body = out['sheet']
    assert out['state'] == 'partial' and body['order_ready'] is False and body['partial'] is True
    assert body['why_partial']['class'] == 'mint_receipt_too_old_or_invalid' and body['identity']['mint'].startswith('419P2wrV')
    assert body['mint_receipt']['older_than_sheet_bound'] is True and body['mint_receipt']['age_s'] > 3600 and body['mint_receipt']['decimals'] == 6
    assert body['emission_snapshot']['fields']['liq_usd']['value'] == pytest.approx(9373.057158076608)             # a value that existed at emission stays visible
    assert 'snapshot.mcap_usd' in body['unknown'] and 'check.dump_group' in body['unknown'] and 'derived.lp_locked_or_burned' in body['unknown']
    assert body['derived']['cluster_hit'] is True and body['hard_filter']['checks']['cluster']['status'] == 'kill'      # cluster_hit is no longer a bare null when the check ran
    assert not ({'cache_key', 'sheet_hash', 'lenses', 'row_hash'} & set(out) | {'cache_key', 'sheet_hash', 'row_hash'} & set(body))      # nothing an order could bind to
    assert len(json.dumps(out)) < 12000


@pytest.mark.parametrize('reason,cls', [
    ('refresh_failed:StaleSnapshot:tape_gap_since_cut', 'tape_loss_since_cut'),
    ('refresh_failed:StaleSnapshot:ps_commit_unproven', 'tape_commit_unproven'),
    ('sheet_unavailable:safety_evidence', 'evidence_not_pass_at_read'),
    ('refresh_failed:StaleSnapshot:latest_swap_not_buy;rpc_pool_unusable', 'pool_state_unproven'),
    ('hard_filter_missing', 'ticket_incomplete'), ('not_near_pass_eligible', 'ticket_not_eligible'), ('something new', 'other')])
def test_every_observed_failure_has_a_named_class(reason, cls):
    assert failure_class(reason)[0] == cls
    assert partial_sheet(TICKET, dict(state='unknown', reason=reason), now_host=NOW)['sheet']['why_partial']['class'] == cls


@pytest.mark.parametrize('ticket', [None, {}, {'candidate': 'x', 'stages': 'y'}, {'candidate': {'snapshot': {'fields': 'bad'}}, 'stages': [{'stage': 'hard_filter', 'inputs': None}]}])
def test_a_thin_or_odd_ticket_never_raises_and_lists_what_is_missing(ticket):
    out = partial_sheet(ticket, dict(state='refused', reason='hard_filter_missing'), now_host=NOW)
    assert out['state'] == 'partial' and out['sheet']['order_ready'] is False and out['sheet']['unknown']


def test_a_failed_read_with_no_reason_is_still_a_partial_view():
    assert partial_sheet(TICKET, {}, now_host=None)['sheet']['why_partial']['reason'] == 'unknown'



def _t(**kw):
    t = json.loads(json.dumps(TICKET)); t.update(kw); return t


@pytest.mark.parametrize('ticket', [
    _t(stages=None), _t(stages=7), _t(stages=['x', None]),
    _t(stages=[{'stage': 'hard_filter', 'verdict': 'PASS', 'inputs': 'a string'}]),
    _t(candidate={**TICKET['candidate'], 'snapshot': {**TICKET['candidate']['snapshot'], 'missing': [None, 3, 'ok']}}),
    _t(candidate={**TICKET['candidate'], 'snapshot': {'fields': {'x': 5, 'y': None}, 'missing': 'zzz'}})])
def test_malformed_containers_stay_a_typed_partial_view(ticket):
    out = partial_sheet(ticket, dict(state='unknown', reason='tape_gap_since_cut'), now_host=NOW)
    assert out['state'] == 'partial' and out['sheet']['order_ready'] is False and out['sheet']['unknown']
    json.dumps(out, allow_nan=False)


def test_the_view_is_byte_bounded_and_keeps_identity_and_the_unknown_map():
    t = _t()
    hf = t['stages'][0]
    hf['inputs']['evidence']['checks']['detail'] = {'big': 'x' * 60000, **{'k%d' % i: i for i in range(5000)}}
    hf['inputs']['evidence']['checks'].update({'extra%d' % i: {'status': 'unknown', 'reason': 'r' * 500} for i in range(300)})
    out = partial_sheet(t, dict(state='unknown', reason='x' * 5000), now_host=NOW)
    text = json.dumps(out, allow_nan=False)
    assert len(text) <= 20000 and out['sheet']['order_ready'] is False and out['sheet']['identity']['mint'].startswith('419P2wrV') and len(out['sheet']['unknown']) <= 61
    assert len(out['reason']) <= 200


def test_a_view_over_the_size_limit_drops_named_detail_not_identity():
    t = _t()
    t['candidate']['snapshot']['fields'] = {'f%d' % i: {'value': 'v' * 150, 'completeness': 'c' * 150, 'source': 's' * 150, 'known_at': 1.0, 'slot': 1} for i in range(200)}
    out = partial_sheet(t, dict(state='unknown', reason='tape_gap_since_cut'), now_host=NOW)
    body = out['sheet']
    assert len(json.dumps(out, allow_nan=False)) <= 16100 and body.get('omitted') and body['identity']['ticket_id'] and body['order_ready'] is False


def test_non_finite_numbers_become_null_and_are_named_never_zero():
    t = _t()
    t['stages'][0]['inputs']['evidence']['checks']['holders']['top10_pct'] = float('nan')
    t['candidate']['snapshot']['fields']['liq_usd']['value'] = float('inf')
    out = partial_sheet(t, dict(state='unknown', reason='tape_gap_since_cut'), now_host=NOW)
    text = json.dumps(out, allow_nan=False)                                       # strict JSON: no NaN / Infinity on the wire
    assert out['sheet']['hard_filter']['checks']['holders']['top10_pct'] is None and out['sheet']['emission_snapshot']['fields']['liq_usd']['value'] is None
    assert 'check.holders.top10_pct' in out['sheet']['unknown']['nonfinite_numbers'] and 'snapshot.liq_usd.value' in out['sheet']['unknown']['nonfinite_numbers'] and 'top10_pct": 0' not in text


def test_an_unhashable_check_status_is_unknown_not_a_crash():
    t = {'stages': [{'stage': 'hard_filter', 'inputs': {'evidence': {'checks': {'cluster': {'status': []}}}}}]}
    assert partial_sheet(t, {'state': 'unknown'}, now_host=100.)['sheet']['derived']['cluster_hit'] is None


def test_the_whole_envelope_is_bounded_by_the_final_size_check_and_truncations_are_counted():
    out = partial_sheet({}, {'state': 'x' * 60000, 'reason': 'r'}, now_host=100.)
    assert len(json.dumps(out, allow_nan=False).encode()) <= 16000 and out['sheet']['order_ready'] is False
    t = {'candidate': {'snapshot': {'fields': {'x' * 60000: {'value': float('nan')}}}}}
    out = partial_sheet(t, {'state': 'unknown'}, now_host=100.)
    assert len(json.dumps(out, allow_nan=False).encode()) <= 16000 and out['sheet']['truncated_strings'] >= 1       # the cut is a counted fact, never silent
    huge = {'stages': [{'stage': 'hard_filter', 'inputs': {'evidence': {'checks': {'k%d' % i: {'status': 'unknown', 'reason': 'z' * 500} for i in range(50)}}}}]}
    assert len(json.dumps(partial_sheet(huge, {'reason': 'q' * 9000}, now_host=1.), allow_nan=False).encode()) <= 16000
