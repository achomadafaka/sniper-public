"""A malformed Relay origin attempt names every Relay field it is missing (2026-10-07, Alpha).

Since the attempt detail deployed, all 15 4663 malformed attempts are KeyError('block') at the
destination outTx match: Relay lists our tx as a success before it writes the block. Whether to
accept that match with `block` absent turns on what ELSE is absent at the same moment (timestamp,
data, settlement fills, the origin inTx's block), because relaxing one line may only move the
KeyError. Each malformed attempt's detail now carries `@miss=<names>` over a fixed list of the
Relay fields the bound proof reads, from the answer already fetched. Diagnostic only: no new call,
and the verdict and reason are unchanged.
"""
import copy

import pytest

from sniper.services import memecoin_relay_initiation as relay
from test_memecoin_relay_bound_origin import _item, inputs
from test_relay_origin_malformed_capture import _verifier


def _answer(**drop):
    req = inputs()[0]
    for tx in req['data']['outTxs']:
        for key in drop.get('out', ()):
            tx.pop(key, None)
    for tx in req['data']['inTxs']:
        for key in drop.get('inp', ()):
            tx.pop(key, None)
    return {'requests': [req]}


def _refusal(monkeypatch, answer):
    monkeypatch.setattr(relay.time, 'sleep', lambda _: None)
    with pytest.raises(relay.ProofError, match='^malformed_origin_evidence$') as got:
        _verifier(answer, poll_s=0)(_item())
    return got.value


def test_a_missing_block_names_every_field_missing_with_it(monkeypatch):
    """THE shape: Relay has our outTx as a success, without block and timestamp."""
    exc = _refusal(monkeypatch, _answer(out=('block', 'timestamp')))
    assert exc.detail.startswith("KeyError('block') at memecoin_relay_initiation.py:")
    assert exc.detail.endswith('@miss=out.block:out.ts'), exc.detail
    assert all(note.endswith('@miss=out.block:out.ts') for note in exc.origin_attempts), exc.origin_attempts


def test_the_origin_side_is_named_too(monkeypatch):
    exc = _refusal(monkeypatch, _answer(out=('block',), inp=('block', 'timestamp')))
    assert exc.detail.endswith('@miss=out.block:in.block:in.ts'), exc.detail


def test_a_null_is_named_apart_from_an_absent_key(monkeypatch):
    answer = _answer(out=('block',))
    for tx in answer['requests'][0]['data']['inTxs']:
        tx['timestamp'] = None
    exc = _refusal(monkeypatch, answer)
    assert exc.detail.endswith('@miss=out.block:in.ts=null'), exc.detail


def test_no_outtx_for_our_tx_is_named_as_such(monkeypatch):
    answer = _answer()
    for tx in answer['requests'][0]['data']['outTxs']:
        tx['txHash'] = '0x' + 'ab' * 32
    with pytest.raises(relay.ProofError) as got:
        monkeypatch.setattr(relay.time, 'sleep', lambda _: None)
        _verifier(answer, poll_s=0)(_item())
    assert str(got.value) == 'destination_mismatch'      # not malformed: no key set, verdict unchanged
    assert not getattr(got.value, 'detail', None)


def test_the_key_set_is_bounded_and_never_raises():
    assert relay._relay_missing_keys(None, {}, True) == ''
    assert relay._relay_missing_keys({'requests': 'x'}, {}, True) == ''
    empty = relay._relay_missing_keys({'requests': [{}]}, {'tx_hash': '0x1', 'network_id': '56'}, True)
    assert empty.startswith('@miss=') and len(empty) <= 200


def test_a_complete_answer_verifies_and_carries_no_key_set(monkeypatch):
    monkeypatch.setattr(relay.time, 'sleep', lambda _: None)
    out = _verifier(_answer())(_item())
    assert out['origin_attempts'] == [n for n in out['origin_attempts'] if '@miss=' not in n]
    assert out['row']['buy']['source_initiation']['proof_version'] == relay.BOUND_PROOF_VERSION
