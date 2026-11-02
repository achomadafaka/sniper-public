"""A malformed Relay origin proof names what failed and keeps the response (dfeaef22 (b), 2026-10-07).

41 of 107 chain-56 origin checks (10-04..10-06) read malformed_origin_evidence and nothing said which
key was missing; the v3 bound path is new and its unit tests build v3 from v2 (v3_of). A malformed
refusal now carries the exception class, the missing key and the verifier line, and the final one
keeps a bounded, key-stripped copy of the Relay response for a real recording. No verdict changes.
"""
import copy
import json
import threading
import time

import pytest

from sniper.services import memecoin_relay_initiation as relay
from test_memecoin_relay_bound_origin import _item, inputs


def _verifier(answer, **kw):
    from test_memecoin_relay_bound_origin import FRAME

    def fetch(url, **kwargs):
        if 'requests/' in url:
            return copy.deepcopy(answer)
        if url.endswith('/chains'):
            return {'chains': FRAME['chains']}
        return FRAME['origin-sol']
    return relay.WatcherVerifier(lambda _: FRAME['identity'], chains=FRAME['chains'], fetch=fetch,
                                 api_key='SECRET-KEY-123', requests_version='v3', **kw)


def _no_route():
    req = inputs()[0]
    req['data'].pop('route')
    return {'requests': [req]}


def test_a_malformed_refusal_names_the_class_the_missing_key_and_the_line(monkeypatch):
    monkeypatch.setattr(relay.time, 'sleep', lambda _: None)
    with pytest.raises(relay.ProofError, match='^malformed_origin_evidence$') as got:
        _verifier(_no_route())(_item())
    assert got.value.detail.startswith("KeyError('route') at memecoin_relay_initiation.py:")


def test_an_unknown_v3_status_names_the_status(monkeypatch):
    monkeypatch.setattr(relay.time, 'sleep', lambda _: None)
    req = dict(inputs()[0], status='something_new')
    with pytest.raises(relay.ProofError, match='^malformed_origin_evidence$') as got:
        _verifier({'requests': [req]})(_item())
    assert got.value.detail == "status='something_new'"


def test_only_the_final_malformed_answer_is_captured_key_stripped_and_bounded(monkeypatch):
    monkeypatch.setattr(relay.time, 'sleep', lambda _: None)
    captured = []
    answer = _no_route()
    answer['requests'][0]['note'] = 'echo SECRET-KEY-123'
    with pytest.raises(relay.ProofError):
        _verifier(answer, capture_fn=captured.append)(_item())
    [rec] = captured
    assert rec['error'] == 'malformed_origin_evidence' and rec['detail'].startswith("KeyError('route')")
    assert rec['request_url'].startswith('https://api.relay.link/requests/v3?orderId=0x')
    assert 'SECRET-KEY-123' not in json.dumps(rec), "the key never reaches the capture"
    assert json.loads(rec['payload'])['requests'][0]['status'] == 'success'


def test_a_capture_that_fails_never_changes_the_verdict(monkeypatch):
    monkeypatch.setattr(relay.time, 'sleep', lambda _: None)

    def broken(_rec):
        raise OSError("disk full")
    with pytest.raises(relay.ProofError, match='^malformed_origin_evidence$'):
        _verifier(_no_route(), capture_fn=broken)(_item())


def test_a_verified_answer_is_never_captured(monkeypatch):
    monkeypatch.setattr(relay.time, 'sleep', lambda _: None)
    captured = []
    out = _verifier({'requests': [inputs()[0]]}, capture_fn=captured.append)(_item())
    assert out['row']['buy']['source_initiation']['proof_version'] == relay.BOUND_PROOF_VERSION
    assert captured == []


def test_verify_ms_includes_the_verifiers_retries_and_the_detail_reaches_the_publisher():
    """WHICH LOOP (Alpha, dfeaef22): malformed retries inside WatcherVerifier.__call__ (6 steps since
    72bc0f22c); OriginWorker times around verify_fn, so origin_verification_ms includes them."""
    done, out = threading.Event(), {}

    def slow_malformed(item):
        time.sleep(.08)                                      # stands in for the retry backoff
        err = relay.ProofError('malformed_origin_evidence')
        err.detail = "KeyError('route') at memecoin_relay_initiation.py:92"
        raise err

    def publish(result):
        out.update(result)
        done.set()
    worker = relay.OriginWorker(slow_malformed, publish, workers=1)
    try:
        worker.submit({'row': {'tx_hash': 'x', 'buy': {'source_initiation': {'block_number': 1}}}})
        assert done.wait(2)
    finally:
        worker.close()
    assert out['origin_verification_ms'] >= 80
    assert out['origin_verification_error'] == 'malformed_origin_evidence'
    assert out['origin_verification_detail'].startswith("KeyError('route')")


def test_the_watcher_line_names_the_detail_only_when_there_is_one():
    from scripts.research import onchain_evm_wallet_watch as watch
    base = {'row': {'tx_hash': '0xa868f032b8124f94'}, 'origin_verification_ms': 690.06}
    assert watch._relay_origin_line(base) == 'relay origin 0xa868f032b8 reason=verified verify_ms=690.060'
    bad = dict(base, origin_verification_error='malformed_origin_evidence',
               origin_verification_detail="KeyError('route') at memecoin_relay_initiation.py:92")
    assert watch._relay_origin_line(bad) == ("relay origin 0xa868f032b8 reason=malformed_origin_evidence "
                                             "verify_ms=690.060 detail=KeyError('route') at "
                                             "memecoin_relay_initiation.py:92")


def test_a_capture_rides_the_watchers_bounded_capture_queue_once_per_tx(monkeypatch):
    """Reuses the rotating JSONL writer (two 8 MB parts a chain, one writer thread, put_nowait)."""
    import queue
    from scripts.research import onchain_evm_wallet_watch as watch
    q = queue.Queue(maxsize=4)
    monkeypatch.setattr(watch, '_capture_queue', lambda start=True: q)
    rec = {'tx_hash': '0xA868', 'error': 'malformed_origin_evidence', 'payload': '{}'}
    assert watch.capture_relay_origin(rec, chain='56') is True
    assert watch.capture_relay_origin(dict(rec), chain='56') is False, "one capture per tx"
    path, record = q.get_nowait()
    assert str(path).endswith('onchain_evm_relay_origin_malformed.56.jsonl')
    assert record['chain'] == '56' and record['tx_hash'] == '0xa868' and record['captured_at']
    assert q.empty()


# (i) PER-ATTEMPT TRACE (Alpha 2026-10-07): 7 of 21 Solana-origin verifies took 7.6-8.1 s, the 7th
# and last backoff step. Each attempt now names its error, its own ms, Relay's status and whether
# deposit.origin.transactionId was already there, from the answer already fetched: no added call.
import re

NOTE = re.compile(r'^(?P<reason>[A-Za-z0-9_:]+)/(?P<ms>\d+)/(?P<status>[^/,]+)/(?P<deposit>d1|d0|-)$')


def _sequence(answers):
    """A v3 verifier whose /requests answers come from `answers` in order; counts the Relay calls."""
    from test_memecoin_relay_bound_origin import FRAME
    calls = []

    def fetch(url, **kwargs):
        if 'requests/' in url:
            calls.append(url)
            answer = answers[min(len(calls), len(answers)) - 1]
            if isinstance(answer, Exception):
                raise answer
            return copy.deepcopy(answer)
        return FRAME['origin-sol']
    verifier = relay.WatcherVerifier(lambda _: FRAME['identity'], chains=FRAME['chains'], fetch=fetch,
                                     api_key='SECRET-KEY-123', requests_version='v3')
    return verifier, calls


def _parts(notes):
    return [(m['reason'], m['status'], m['deposit']) for m in map(NOTE.match, notes)]


def test_a_late_settle_names_each_attempt_with_status_and_deposit_and_adds_no_call(monkeypatch):
    monkeypatch.setattr(relay.time, 'sleep', lambda _: None)
    settled = inputs()[0]
    pending = dict(copy.deepcopy(settled), status='submitted')
    no_deposit = copy.deepcopy(pending)
    no_deposit['protocol']['deposit']['origin'].pop('transactionId')
    verifier, calls = _sequence([{'requests': []}, {'requests': [no_deposit]}, {'requests': [pending]},
                                 {'requests': [settled]}])
    out = verifier(_item())
    assert out['row']['buy']['source_initiation']['proof_version'] == relay.BOUND_PROOF_VERSION
    assert len(calls) == 4, "one Relay call per attempt, as before"
    assert _parts(out['origin_attempts']) == [
        ('relay_request_not_indexed', 'none', '-'), ('relay_not_settled', 'submitted', 'd0'),
        ('relay_not_settled', 'submitted', 'd1'), ('ok', 'success', 'd1')]


def test_a_refusal_carries_its_attempts_to_the_publisher_and_the_watcher_line(monkeypatch):
    monkeypatch.setattr(relay.time, 'sleep', lambda _: None)
    from scripts.research import onchain_evm_wallet_watch as watch
    verifier, calls = _sequence([relay.ProofError('origin_rate_limit')])
    done, out = threading.Event(), {}
    worker = relay.OriginWorker(verifier, lambda r: (out.update(r), done.set()), workers=1)
    try:
        item = _item()
        item['row']['tx_hash'] = '0x' + 'ab' * 32
        worker.submit(item)
        assert done.wait(2)
    finally:
        worker.close()
    assert out['origin_verification_error'] == 'origin_rate_limit:relay_requests'
    assert _parts(out['origin_attempts']) == [('origin_rate_limit:relay_requests', '-', '-')] * 3
    assert len(calls) == 3
    line = watch._relay_origin_line(out)
    assert re.search(r' verify_ms=[\d.]+ attempts=origin_rate_limit:relay_requests/\d+/-/-'
                     r'(,origin_rate_limit:relay_requests/\d+/-/-){2}$', line), line


def test_an_unexpected_error_still_names_its_attempt_and_the_verdict_is_unchanged():
    def fetch(url, **kwargs):
        raise RuntimeError('boom')
    verifier = relay.WatcherVerifier(lambda _: None, fetch=fetch, api_key='k', requests_version='v3')
    with pytest.raises(RuntimeError) as got:
        verifier(_item())
    assert _parts(got.value.origin_attempts) == [('RuntimeError', '-', '-')]


@pytest.mark.parametrize('answer', [None, [], 'text', {'requests': 'x'}, {'requests': [None]},
                                    {'requests': [{'status': 'a/b,c', 'protocol': None}]}])
def test_a_malformed_answer_never_breaks_the_note(answer):
    note = relay._attempt_note(None, time.perf_counter(), {'payload': answer})
    assert NOTE.match(note), note


def test_the_watcher_line_appends_attempts_before_the_detail():
    from scripts.research import onchain_evm_wallet_watch as watch
    base = {'row': {'tx_hash': '0xa868f032b8124f94'}, 'origin_verification_ms': 7969.0,
            'origin_attempts': ['relay_not_settled/231/submitted/d1', 'ok/245/success/d1']}
    assert watch._relay_origin_line(base) == ('relay origin 0xa868f032b8 reason=verified verify_ms=7969.000 '
                                              'attempts=relay_not_settled/231/submitted/d1,ok/245/success/d1')
    bad = dict(base, origin_verification_error='malformed_origin_evidence', origin_verification_detail='KeyError')
    assert watch._relay_origin_line(bad).endswith('attempts=relay_not_settled/231/submitted/d1,'
                                                  'ok/245/success/d1 detail=KeyError')


# --- each attempt's detail rides its own note (Alpha 2026-10-07: learn which field Relay indexes late)

DETAIL_NOTE = re.compile(r'^malformed_origin_evidence/\d+/success/d1/(?P<detail>[^,/ ]+)$')


def test_a_malformed_attempt_that_later_settles_keeps_its_detail_on_its_note(monkeypatch):
    """THE regression: 4663 attempts read malformed_origin_evidence/296/success/d1 x3 then ok, and
    the line said nothing of what was missing; the detail existed (exc.detail) but the note dropped
    it, and only a FINAL refusal printed detail=."""
    monkeypatch.setattr(relay.time, 'sleep', lambda _: None)
    verifier, calls = _sequence([_no_route(), {'requests': [inputs()[0]]}])
    out = verifier(_item())
    first, second = out['origin_attempts']
    m = DETAIL_NOTE.match(first)
    assert m, first
    assert m['detail'].startswith("KeyError('route')@memecoin_relay_initiation.py:")
    assert NOTE.match(second) and second.startswith('ok/'), "a note without a detail is unchanged"
    assert len(calls) == 2, "no extra call"


def test_a_detail_never_breaks_the_line_separators(monkeypatch):
    monkeypatch.setattr(relay.time, 'sleep', lambda _: None)
    req = dict(inputs()[0], status='a/b, c')
    with pytest.raises(relay.ProofError) as got:
        _verifier({'requests': [req]})(_item())
    note = got.value.origin_attempts[0]
    assert note.count('/') == 4 and ',' not in note and ' ' not in note, note
    assert note.endswith("/status='a?b??c'")
