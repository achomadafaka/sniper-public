"""Relay's outTx `block` absent: our own receipt carries the block (operator YES via Alpha, 2026-10-07).

Since the attempt detail deployed, every 4663 malformed attempt is KeyError('block') at the
destination outTx match: Relay lists our tx as a success, with matching hash and chain, before it
writes the block. The wait for that one label was most of the 4663 verify (verified p50/p90
4.59/7.81 s). Behind MEMECOIN_RELAY_ACCEPT_OUTTX_BLOCK_ABSENT (code default 0):
- an ABSENT outTx `block` matches on hash, chain and success. The block number and hash are our
  receipt's, already required equal to our own read of the destination tx;
- an absent outTx `timestamp` is our own block header's time (the watcher's log carries it), used
  only when that log is the receipt's own block (number and hash);
- every other binding stays mandatory: an absent one is malformed and keeps polling, as today;
- a block that is present and differs, or is null, still refuses.
"""
import copy

import pytest

from sniper.services import memecoin_relay_initiation as relay
from test_memecoin_relay_bound_origin import FRAME, _item, inputs


def _answer(drop_out=(), drop_protocol=()):
    req = inputs()[0]
    for tx in req['data']['outTxs']:
        for key in drop_out:
            tx.pop(key, None)
    for key in drop_protocol:
        req['protocol']['settlement']['destination'].pop(key, None)
    return {'requests': [req]}


def _with_header(item, *, number=None, block_hash=None, ts=1791128321):
    proof = item['row']['buy']['source_initiation']
    item['row']['log'] = {'blockNumber': hex(proof['block_number'] if number is None else number),
                          'blockHash': block_hash or proof['block_hash'], 'blockTimestamp': hex(ts)}
    return item


def _verifier(answer, *, accept=True, sleeps=None):
    def fetch(url, **kwargs):
        if 'requests/' in url:
            return copy.deepcopy(answer)
        if url.endswith('/chains'):
            return {'chains': FRAME['chains']}
        return FRAME['origin-sol']
    return relay.WatcherVerifier(lambda _: FRAME['identity'], chains=FRAME['chains'], fetch=fetch,
                                 api_key='key', requests_version='v3', poll_s=0, requests_per_s=0,
                                 sleep=(sleeps.append if sleeps is not None else (lambda _: None)),
                                 accept_block_absent=accept)


def test_block_absent_and_everything_else_bound_verifies():
    """THE regression shape (operator YES): 6 malformed polls on 4663 waited for this one label."""
    out = _verifier(_answer(drop_out=('block',)))(_with_header(_item()))
    proof = out['row']['buy']['source_initiation']
    assert proof['proof_version'] == relay.BOUND_PROOF_VERSION
    assert out['origin_attempts'][0].startswith('ok/'), out['origin_attempts']


def test_block_and_timestamp_absent_use_our_own_header():
    out = _verifier(_answer(drop_out=('block', 'timestamp')))(_with_header(_item()))
    assert out['row']['buy']['source_initiation']['proof_version'] == relay.BOUND_PROOF_VERSION


def test_our_header_time_still_has_to_fit_the_origin_window():
    with pytest.raises(relay.ProofError, match='^origin_time_mismatch$'):
        _verifier(_answer(drop_out=('block', 'timestamp')))(_with_header(_item(), ts=1791128319 + 121))


@pytest.mark.parametrize('header', [None, 'other_number', 'other_hash'])
def test_without_our_own_header_for_that_block_a_missing_timestamp_keeps_polling(header):
    item = _item()
    if header == 'other_number':
        _with_header(item, number=item['row']['buy']['source_initiation']['block_number'] + 1)
    elif header == 'other_hash':
        _with_header(item, block_hash='0x' + '11' * 32)
    sleeps = []
    with pytest.raises(relay.ProofError, match='^malformed_origin_evidence$') as got:
        _verifier(_answer(drop_out=('block', 'timestamp')), sleeps=sleeps)(item)
    assert len(sleeps) == 6, "polled, never accepted"
    assert "KeyError('timestamp')" in got.value.detail, "named as the missing label, never a None time"


def test_block_absent_and_fills_absent_keeps_polling():
    sleeps = []
    with pytest.raises(relay.ProofError, match='^malformed_origin_evidence$') as got:
        _verifier(_answer(drop_out=('block',), drop_protocol=('fills',)), sleeps=sleeps)(_with_header(_item()))
    assert len(sleeps) == 6 and "KeyError('fills')" in got.value.detail


@pytest.mark.parametrize('label', ['data', 'status'])
def test_block_absent_with_another_outtx_label_absent_never_accepts(label):
    with pytest.raises(relay.ProofError) as got:
        _verifier(_answer(drop_out=('block', label)))(_with_header(_item()))
    assert str(got.value) in {'malformed_origin_evidence', 'destination_mismatch'}


@pytest.mark.parametrize('value', [125701673, None, 0])
def test_a_present_block_that_differs_from_our_receipt_refuses(value):
    answer = _answer()
    for tx in answer['requests'][0]['data']['outTxs']:
        tx['block'] = value
    with pytest.raises(relay.ProofError, match='^destination_mismatch$'):
        _verifier(answer)(_with_header(_item()))


def test_knob_off_keeps_todays_malformed_poll():
    sleeps = []
    with pytest.raises(relay.ProofError, match='^malformed_origin_evidence$') as got:
        _verifier(_answer(drop_out=('block',)), accept=False, sleeps=sleeps)(_with_header(_item()))
    assert len(sleeps) == 6 and "KeyError('block')" in got.value.detail


def test_the_knob_defaults_off(monkeypatch):
    import importlib
    from sniper.settings import memecoin as cfg
    monkeypatch.delenv('MEMECOIN_RELAY_ACCEPT_OUTTX_BLOCK_ABSENT', raising=False)
    assert importlib.reload(cfg).MEMECOIN_RELAY_ACCEPT_OUTTX_BLOCK_ABSENT is False
    assert relay.WatcherVerifier(lambda _: [], requests_version='v3').accept_block_absent is False


def test_the_pure_verifier_uses_a_header_time_only_with_the_knob():
    req, org, prf, dst = inputs()
    for tx in req['data']['outTxs']:
        tx.pop('timestamp')
    kw = dict(identities=FRAME['identity'], chains=FRAME['chains'], destination_time=1791128321)
    with pytest.raises(relay.ProofError, match='^malformed_origin_evidence$'):
        relay.verify_bound_origin(copy.deepcopy(req), org, prf, dst, block_absent_ok=False, **kw)
    out = relay.verify_bound_origin(req, org, prf, dst, block_absent_ok=True, **kw)
    assert out['proof_version'] == relay.BOUND_PROOF_VERSION
