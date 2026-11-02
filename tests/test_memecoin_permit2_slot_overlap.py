"""Permit2 proof reads overlap without changing the funded override or failure verdict."""
import threading
from collections import Counter
from contextlib import contextmanager

import pytest

from sniper.execution import evm_real_seams as seams
from sniper.execution import evm_token_safety as S
from sniper.execution.evm_rpc_pool import RequestNotSent
from test_memecoin_evm_token_safety import PERMIT2, TAKER, _slot

MINT = "0x" + "ab" * 20
AMOUNT = 123_456_789
SIMULATED_AMOUNT = 1_000_000_000_000_000_123_456_789
BALANCE_SLOT = _slot(TAKER, 0)
ALLOWANCE_SLOT = _slot(PERMIT2, _slot(TAKER, 1))


def checked(monkeypatch, *, allowance=0, hook=None, balance_found=True, scope=None):
    calls = []

    def read(to, data, url, *, overrides=None, **kwargs):
        assert to == MINT and url == "https://unused.invalid"
        diff = (overrides or {}).get(MINT, {}).get("stateDiff", {})
        kind = ("balance" if data.startswith("0x70a08231") else
                "allowance_slot" if overrides else "existing_allowance")
        calls.append(kind)
        if hook is not None:
            response = hook(kind)
            if response is not None:
                return response
        if kind == "balance":
            return hex(10 ** 24) if balance_found and BALANCE_SLOT in diff else "0x0"
        if kind == "existing_allowance":
            return hex(allowance)
        return hex(10 ** 24) if ALLOWANCE_SLOT in diff else "0x0"

    monkeypatch.setattr(seams, "eth_call_raw", read)
    monkeypatch.setattr(S, "_slot_probe_alternates", lambda _url: ())
    monkeypatch.setattr(S, "_access_list_slots", lambda *args, **kwargs: ())
    fn = S.build_evm_token_safety(chain_id="4663", rpc_url="https://unused.invalid",
        taker=TAKER, quote_fn=lambda *args, **kwargs: None, safety_deadline_scope=scope)
    return fn.fund_overrides, calls


def expected(*, allowance_slot=True, amount=AMOUNT):
    diff = {BALANCE_SLOT: "0x%064x" % (10 ** 24 + amount)}
    if allowance_slot:
        diff[ALLOWANCE_SLOT] = "0x%064x" % (10 ** 24 + amount)
    return {MINT: {"stateDiff": diff}, TAKER: {"balance": S._GAS_HEX}}


def test_balance_proof_and_existing_allowance_read_start_together(monkeypatch):
    balance_started, allowance_started = threading.Event(), threading.Event()

    def rendezvous(kind):
        if kind == "balance":
            balance_started.set()
            assert allowance_started.wait(0.3), "existing allowance waited for balance proof"
        elif kind == "existing_allowance":
            allowance_started.set()
            assert balance_started.wait(0.3), "balance proof waited for existing allowance"

    proof, calls = checked(monkeypatch, allowance=SIMULATED_AMOUNT, hook=rendezvous)
    assert proof(MINT, PERMIT2, AMOUNT) == expected(allowance_slot=False)
    assert Counter(calls) == {"balance": 1, "existing_allowance": 1}


def test_insufficient_allowance_starts_its_proof_while_balance_is_pending(monkeypatch):
    allowance_slot_started = threading.Event()

    def rendezvous(kind):
        if kind == "balance":
            assert allowance_slot_started.wait(0.3), "allowance proof waited for balance proof"
        elif kind == "allowance_slot":
            allowance_slot_started.set()

    proof, calls = checked(monkeypatch, hook=rendezvous)
    assert proof(MINT, PERMIT2, AMOUNT) == expected()
    assert Counter(calls) == {"balance": 1, "existing_allowance": 1, "allowance_slot": 2}


@pytest.mark.parametrize("allowance", [SIMULATED_AMOUNT, SIMULATED_AMOUNT + 1])
def test_sufficient_allowance_skips_its_slot_scan_and_cached_amount_stays_exact(
        monkeypatch, allowance):
    proof, calls = checked(monkeypatch, allowance=allowance)
    assert proof(MINT, PERMIT2, AMOUNT) == expected(allowance_slot=False)
    assert proof(MINT, PERMIT2, 987) == expected(allowance_slot=False, amount=987)
    assert Counter(calls) == {"balance": 1, "existing_allowance": 1}


def test_balance_failure_keeps_precedence_over_allowance_failure(monkeypatch):
    balance_error = S.EvmSafetyDeadlineExceeded("balance deadline")
    allowance_error = RequestNotSent("allowance cancelled")

    def fail(kind):
        raise balance_error if kind == "balance" else allowance_error

    proof, _ = checked(monkeypatch, hook=fail)
    with pytest.raises(S.EvmSafetyDeadlineExceeded) as caught:
        proof(MINT, PERMIT2, AMOUNT)
    assert caught.value is balance_error


def test_balance_cancellation_does_not_join_an_already_started_allowance_read(monkeypatch):
    started, release, done, finished = (threading.Event() for _ in range(4))
    refusal = RequestNotSent("balance cancelled")
    failures = []

    def blocked(kind):
        if kind == "balance":
            assert started.wait(0.5), "allowance worker never started"
            raise refusal
        if kind == "existing_allowance":
            started.set()
            release.wait(2)
        elif kind == "allowance_slot":
            finished.set()

    proof, _ = checked(monkeypatch, hook=blocked)

    def invoke():
        try:
            proof(MINT, PERMIT2, AMOUNT)
        except Exception as error:
            failures.append(error)
        finally:
            done.set()

    caller = threading.Thread(target=invoke)
    caller.start()
    try:
        assert done.wait(0.8), "cancelled proof joined its blocked sibling read"
        assert failures == [refusal]
    finally:
        release.set()
        caller.join(2)
        finished.wait(1)


def test_missing_balance_with_adequate_allowance_still_scans_allowance_and_caches_clean_miss(
        monkeypatch):
    proof, calls = checked(monkeypatch, allowance=SIMULATED_AMOUNT, balance_found=False)
    assert proof(MINT, PERMIT2, AMOUNT) is None
    assert calls.count("existing_allowance") == 1 and calls.count("allowance_slot") == 2
    count = len(calls)
    assert proof(MINT, PERMIT2, AMOUNT) is None
    assert len(calls) == count


@pytest.mark.parametrize("boundary", ["balance", "existing_allowance", "allowance_slot"])
@pytest.mark.parametrize("error_type", [RequestNotSent, S.EvmSafetyDeadlineExceeded])
def test_local_refusal_never_retries_or_negative_caches_and_next_healthy_call_recovers(
        monkeypatch, boundary, error_type):
    failed = True
    refusal = error_type("job stopped")
    sleeps = []
    monkeypatch.setattr(S.time, "sleep", sleeps.append)

    def fail(kind):
        if failed and kind == boundary:
            raise refusal

    proof, calls = checked(monkeypatch, hook=fail)
    with pytest.raises(error_type) as caught:
        proof(MINT, PERMIT2, AMOUNT)
    assert caught.value is refusal and sleeps == []
    assert calls.count(boundary) == 1
    failed = False
    assert proof(MINT, PERMIT2, AMOUNT) == expected()


def test_missing_balance_does_not_hide_allowance_slot_cancellation(monkeypatch):
    refusal = RequestNotSent("slot cancelled")

    def fail(kind):
        if kind == "allowance_slot":
            raise refusal

    proof, calls = checked(monkeypatch, allowance=SIMULATED_AMOUNT, balance_found=False, hook=fail)
    with pytest.raises(RequestNotSent) as caught:
        proof(MINT, PERMIT2, AMOUNT)
    assert caught.value is refusal and calls.count("allowance_slot") == 1


@pytest.mark.parametrize("boundary", ["balance", "existing_allowance"])
def test_malformed_rpc_data_cannot_negative_cache_the_layout(monkeypatch, boundary):
    failed = True
    sleeps = []
    monkeypatch.setattr(S.time, "sleep", sleeps.append)
    def malformed(kind):
        return "0x" if failed and kind == boundary else None

    proof, calls = checked(monkeypatch, hook=malformed)
    with pytest.raises(S._SlotScanTransportError):
        proof(MINT, PERMIT2, AMOUNT)
    assert sleeps == [0.2, 0.4, 0.8]
    failed = False
    assert proof(MINT, PERMIT2, AMOUNT) == expected()


def test_each_parallel_read_has_its_own_deadline_scope(monkeypatch):
    local = threading.local()
    local.deadline = 17.0
    main_thread = threading.get_ident()
    readers = set()

    @contextmanager
    def scope(deadline):
        old = getattr(local, "deadline", None)
        local.deadline = deadline
        try:
            yield lambda: False
        finally:
            local.deadline = old

    def require_deadline(_kind):
        assert getattr(local, "deadline", None) == 42.0
        assert threading.get_ident() != main_thread
        readers.add(threading.get_ident())

    proof, _ = checked(monkeypatch, scope=scope, hook=require_deadline)
    assert proof(MINT, PERMIT2, AMOUNT, deadline=42.0) == expected()
    assert local.deadline == 17.0 and readers
