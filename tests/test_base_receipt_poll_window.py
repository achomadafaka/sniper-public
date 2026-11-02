"""Base's receipt-first executor observes early receipts without changing send ownership."""
import json
import time

from eth_account import Account
import pytest

from sniper.execution import evm_real_seams as S
from sniper.settings import memecoin as cfg


class ReceiptClock:
    """Offline JSON-RPC boundary with receipt availability fixed independently of polling."""
    def __init__(self, *, ready_at=.39, status='0x1', failure=None):
        self.now = 0.0
        self.ready_at, self.status, self.failure = ready_at, status, failure
        self.receipt_reads, self.sleeps = [], []

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay

    def http(self, method, url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        if req['method'] == 'eth_getTransactionReceipt':
            self.receipt_reads.append(self.now)
            if self.failure == 'timeout': raise TimeoutError('receipt unavailable')
            if self.failure == 'rate_limit': return 429, {'error': 'rate limit'}, None
            if self.failure == 'partial': return 200, {'result': {'transactionHash': req['params'][0]}}, None
            if self.now >= self.ready_at:
                return 200, {'result': {'transactionHash': req['params'][0],
                    'status': self.status, 'blockNumber': '0x17'}}, None
            return 200, {'result': None}, None
        if req['method'] == 'eth_getTransactionCount': return 200, {'result': '0x5'}, None
        if req['method'] == 'eth_gasPrice': return 200, {'result': '0x3b9aca00'}, None
        raise AssertionError('unexpected live dependency: ' + req['method'])


def executor(monkeypatch, clock, chain='8453'):
    monkeypatch.setattr(time, 'sleep', clock.sleep)
    monkeypatch.setattr(cfg, 'memecoin_evm_approval_pipeline', lambda *_: False)
    monkeypatch.setattr(cfg, 'memecoin_evm_preapprove_chains', lambda: frozenset())
    return S.build_real_evm_executor(acct=Account.create(), chain_id=chain,
        rpc_url='https://offline.invalid', http=clock.http)


def test_base_swap_observes_receipt_inside_bounded_fast_window(monkeypatch):
    clock = ReceiptClock()
    ex = executor(monkeypatch, clock)
    assert ex['_confirm']('0x' + 'ab' * 32) == 'confirmed'
    assert clock.now == .5
    assert clock.receipt_reads == [0.0, .25, .5]


def test_base_approval_still_waits_for_receipt_and_observes_it_promptly(monkeypatch):
    clock = ReceiptClock()
    ex = executor(monkeypatch, clock)
    accepted = []
    def send(raw):
        accepted.append(clock.now)
        assert clock.now == 0.0  # Provider acceptance alone does not finish the approval.
        return '0x' + 'cd' * 32
    assert ex['_approve']('0x' + '33' * 20, 10**6, send_fn=send) is True
    assert accepted == [0.0] and clock.now == .5
    assert clock.receipt_reads == [0.0, .25, .5]


@pytest.mark.parametrize('failure', [None, 'timeout', 'rate_limit', 'partial'])
def test_unavailable_base_receipt_stays_unknown_with_same_sixty_second_bound(monkeypatch, failure):
    clock = ReceiptClock(ready_at=float('inf'), failure=failure)
    ex = executor(monkeypatch, clock)
    assert ex['_confirm']('0x' + 'ab' * 32) == 'unknown'
    assert clock.now == 60.0 and len(clock.receipt_reads) == 37
    assert clock.sleeps[:8] == [.25] * 8 and clock.sleeps[8:] == [2.0] * 29


def test_base_receipt_revert_is_failed_not_accepted_or_retried(monkeypatch):
    clock = ReceiptClock(status='0x0')
    ex = executor(monkeypatch, clock)
    assert ex['_confirm']('0x' + 'ab' * 32) == 'failed'
    assert clock.now == .5 and len(clock.receipt_reads) == 3


def test_base_missing_receipt_reoffers_original_bytes_at_original_times(monkeypatch):
    clock = ReceiptClock(ready_at=float('inf'))
    ex = executor(monkeypatch, clock)
    tx = {'to': '0x' + '22' * 20, 'data': '0x', 'value': 0, 'gas': 21000,
          'gasPrice': 10**9, 'nonce': 1, 'chainId': 8453}
    raw, identity = S.sign_tx(tx, Account.create())
    sent = []
    def send(payload):
        sent.append((clock.now, payload))
        return identity
    ex['_remember_sent'](send)(raw)
    assert ex['_confirm'](identity) == 'unknown'
    assert [stamp for stamp, _ in sent] == [0.0, 4.0, 14.0]
    assert len(sent) == 3 and all(payload == raw for _, payload in sent)


def test_an_ambiguous_base_approval_is_not_treated_as_mined(monkeypatch):
    clock = ReceiptClock(ready_at=float('inf'))
    ex = executor(monkeypatch, clock)
    sent = []
    def send(raw):
        sent.append((clock.now, raw))
        return None
    assert ex['_approve']('0x' + '33' * 20, 10**6, send_fn=send) is False
    assert [stamp for stamp, _ in sent] == [0.0, 4.0, 14.0]
    assert all(payload == sent[0][1] for _, payload in sent)


def test_other_chain_receipt_cadence_remains_unchanged(monkeypatch):
    clock = ReceiptClock()
    ex = executor(monkeypatch, clock, chain='999')
    assert ex['_confirm']('0x' + 'ab' * 32) == 'confirmed'
    assert clock.now == 2.0 and clock.receipt_reads == [0.0, 2.0]


def test_base_confirmation_retains_caller_stop_without_releasing_unknown_transaction(monkeypatch):
    clock = ReceiptClock(ready_at=float('inf'))
    ex = executor(monkeypatch, clock)
    stopped = []
    def stop():
        stopped.append(clock.now)
        return True
    assert ex['_confirm']('0x' + 'ab' * 32, stop_fn=stop) == 'unknown'
    assert stopped == [0.0] and clock.receipt_reads == [0.0] and clock.sleeps == []
