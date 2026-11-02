import json
from dataclasses import FrozenInstanceError
import pytest
from sniper.office.null_schedule import due_intent
from sniper.office.ledger import ENTRY_DELAY_S, TICK_GRACE_S


def inputs():
    op = dict(candidate_seq=1, opportunity_hash='a'*64, epoch_id='e', network_id='solana', mint='mint', pool='pool', first_eligible_at=100.)
    contract = dict(epoch_id='e', book_id='null', clip_usd=25., initial_capital_usd=100., max_concurrent=2, daily_loss_usd=50., policy_hash='b'*64, exit_policy_hash='c'*64, fill_model_version='v1', tick_grace_s=TICK_GRACE_S)
    sample = dict(clock_sample_id='s', offset_s=17., sampled_at=116.)
    return op, contract, sample


def test_intent_is_immutable_stable_and_converts_only_the_quote_boundary():
    op, contract, sample = inputs()
    at = 110. - ENTRY_DELAY_S
    result = due_intent(op, latency_s=10., contract=contract, clock_sample=sample, now=at)
    assert result.state == 'due'
    body = json.loads(result.payload_json)
    assert body['submit_at'] == at and body['fill_at'] == 110. and body['quote_target_host'] == 127.
    assert body['clock_sample']['clock_sample_id'] == 's'
    assert result == due_intent(op, latency_s=10., contract=contract, clock_sample=sample, now=at)
    with pytest.raises(FrozenInstanceError):
        result.intent_id = 'changed'
    contract['clip_usd'] = 100.
    assert json.loads(result.payload_json)['contract']['clip_usd'] == 25.


def test_never_reads_future_or_backdates_a_missed_window():
    op, contract, sample = inputs()
    at = 110. - ENTRY_DELAY_S
    assert due_intent(op, latency_s=10., contract=contract, clock_sample=sample, now=at-.01).state == 'pending'
    assert due_intent(op, latency_s=10., contract=contract, clock_sample=sample, now=at+TICK_GRACE_S+.01).reason == 'missed_submit_window'
    assert due_intent(op, latency_s=0., contract=contract, clock_sample=sample, now=at).reason == 'infeasible_null_latency'
    assert due_intent(op, latency_s=10., contract=contract, clock_sample=None, now=at).reason == 'clock_unknown'
    assert due_intent(op, latency_s=10., contract=contract, clock_sample=dict(sample, sampled_at=-4000.), now=at).reason == 'clock_unknown'


def test_identity_binds_contract_and_no_runtime_grace_default():
    op, contract, sample = inputs()
    at = 110. - ENTRY_DELAY_S
    first = due_intent(op, latency_s=10., contract=contract, clock_sample=sample, now=at)
    other = due_intent(op, latency_s=10., contract=dict(contract, book_id='other'), clock_sample=sample, now=at)
    assert first.intent_id != other.intent_id
    del contract['tick_grace_s']
    assert due_intent(op, latency_s=10., contract=contract, clock_sample=sample, now=at).reason == 'unsupported_tick_grace'
