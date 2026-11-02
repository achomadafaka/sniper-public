"""Pre-dispatch refusal stops safety work without becoming a token verdict or RPC traceback."""
import json
import logging
import time

import pytest

from sniper.execution import evm_token_safety as safety
from sniper.execution.evm_rpc_pool import RequestNotSent
from test_bsc_preparation_scheduling import full_safety_network, setup_job
from test_memecoin_bsc_fast_lane_live import live56


def _safety_sleeps(monkeypatch):
    """Every product-code sleep (a `sniper.` caller on any thread: the safety check's own readers
    included), recorded instead of slept. `safety.time` IS the time module, so a test thread's sleep
    would be caught too; only those sleep for real. Release 8's gate red (sleeps == [0.15]) was this
    job's own reader: erc20_decimals retried a cancelled job's RequestNotSent (Gamma, 2026-10-14)."""
    import sys
    real, sleeps = time.sleep, []

    def sleep(seconds):
        if str(sys._getframe(1).f_globals.get("__name__") or "").startswith("sniper."):
            sleeps.append(seconds)
        else:
            real(seconds)
    monkeypatch.setattr(safety.time, "sleep", sleep)
    return sleeps


def test_only_product_sleeps_are_recorded(monkeypatch):
    """The recorder's guard: a test thread sleeping while a test runs is never a product retry."""
    import threading
    started, stop = threading.Event(), threading.Event()

    def foreign():
        started.set()
        while not stop.is_set():
            time.sleep(0.001)
    thread = threading.Thread(target=foreign, daemon=True)
    sleeps = _safety_sleeps(monkeypatch)
    thread.start()
    started.wait(1)
    answers = iter([RuntimeError("blip"), RuntimeError("blip"), "0x1"])

    def read(_key):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer
    try:
        assert safety._read_with_retry(read, "slot") == "0x1"
    finally:
        stop.set()
        thread.join(1)
    assert sleeps == [0.2, 0.4]                 # the product's own backoff, never the test thread's


def test_not_sent_never_retries_or_rotates(monkeypatch):
    calls = []
    refusal = RequestNotSent("bsc speculative read unavailable")
    def read(key, **kwargs):
        calls.append(kwargs)
        raise refusal
    sleeps = _safety_sleeps(monkeypatch)
    with pytest.raises(RequestNotSent) as error:
        safety._read_with_retry(read, "slot", alternates=("https://alternate.invalid",))
    assert error.value is refusal
    assert calls == [{}] and sleeps == []


@pytest.mark.parametrize("refusal", [RequestNotSent("job closed"), "no_eligible"])
def test_decimals_not_sent_returns_none_without_a_retry_or_sleep(monkeypatch, refusal):
    """Release 8's gate red: the safety check's reader slept 0.15 s retrying a cancelled job's refusal."""
    from sniper.execution import evm_rpc_pool, evm_wallet
    if refusal == "no_eligible":
        refusal = evm_rpc_pool.NoEligibleReadEndpoint("no eligible read endpoint")
    calls = []

    def eth_call(*args, **kwargs):
        calls.append(args)
        raise refusal
    monkeypatch.setattr(evm_wallet, "_eth_call", eth_call)
    sleeps = _safety_sleeps(monkeypatch)
    assert evm_wallet.erc20_decimals("0x" + "c2" * 20, "https://read.invalid", retries=1) is None
    assert len(calls) == 1 and sleeps == []


def test_decimals_still_retries_a_sent_failure_once(monkeypatch):
    """Today's funded rule for a request that WAS sent (2026-08-30 GBA): one retry after 0.15 s."""
    from sniper.execution import evm_wallet
    answers = iter([RuntimeError("timeout"), "0x12"])

    def eth_call(*args, **kwargs):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(evm_wallet, "_eth_call", eth_call)
    sleeps = _safety_sleeps(monkeypatch)
    assert evm_wallet.erc20_decimals("0x" + "c2" * 20, "https://read.invalid", retries=1) == 18
    assert sleeps == [0.15]


def test_slot_probe_preserves_not_sent(monkeypatch):
    refusal = RequestNotSent("bsc speculative read unavailable")
    _safety_sleeps(monkeypatch)
    def read(_key):
        raise refusal
    with pytest.raises(RequestNotSent) as error:
        safety._find_slot(read, str)
    assert error.value is refusal


@pytest.mark.parametrize("strict", [False, True])
def test_optional_access_list_preserves_not_sent(monkeypatch, strict):
    from sniper.execution import evm_wallet
    refusal = RequestNotSent("job closed")
    def rpc(*args, **kwargs):
        raise refusal
    monkeypatch.setattr(evm_wallet, "_rpc", rpc)
    with pytest.raises(RequestNotSent) as error:
        safety._access_list_slots("0x" + "01" * 20, "0x00", "https://read.invalid", strict=strict)
    assert error.value is refusal


def test_cancelled_bonus_probe_cannot_become_negative_layout():
    refusal = RequestNotSent("job closed")
    def late():
        raise refusal
    with pytest.raises(RequestNotSent) as error:
        safety._find_slot(lambda _: "0x0", str, late_slots=late)
    assert error.value is refusal


def test_permit2_allowance_preserves_not_sent(monkeypatch, caplog):
    from test_memecoin_evm_token_safety import (
        PERMIT2, TAKER, _solady_erc20_http, _standard_quote,
    )
    mint = "0x" + "c2" * 20
    base, quote_base = _solady_erc20_http(mint, permit2_infinite=True), _standard_quote(mint)
    calls = []
    def http(method, url, *, body=None, **kwargs):
        row = json.loads(body) if body else None
        if (row and row["method"] == "eth_call"
                and row["params"][0]["data"].startswith("0xdd62ed3e")):
            calls.append(row)
            raise RequestNotSent("job closed")
        return base(method, url, body=body, **kwargs)
    def quote(*args, **kwargs):
        got = quote_base(*args, **kwargs)
        got["issues"] = {"allowance": {"spender": PERMIT2}}
        return got
    sleeps = _safety_sleeps(monkeypatch)
    checked = safety.build_evm_token_safety(
        chain_id="4663", rpc_url="https://rpc", taker=TAKER, http=http, quote_fn=quote)
    with caplog.at_level(logging.WARNING, logger=safety.__name__):
        assert checked(mint) == (False, "sell simulation failed (fail-closed)")
    assert len(calls) == 1 and not sleeps
    assert not any(r.levelno >= logging.ERROR or r.exc_info for r in caplog.records)


@pytest.mark.parametrize("boundary,reason", [
    ("code", "code read failed (fail-closed)"),
    ("route", "sell-route probe failed (fail-closed)"),
    ("simulation", "sell simulation failed (fail-closed)"),
])
def test_not_sent_retains_fail_closed_reason_without_error_traceback(caplog, boundary, reason):
    def refused(_mint):
        raise RequestNotSent("bsc speculative read unavailable")
    seams = dict(code_fn=lambda _: "0x6001", sell_route_fn=lambda _: True,
                 sell_sim_fn=lambda _: True)
    seams[{"code": "code_fn", "route": "sell_route_fn", "simulation": "sell_sim_fn"}[boundary]] = refused
    with caplog.at_level(logging.WARNING, logger=safety.__name__):
        assert safety.token_safety_ok_evm("0xtoken", **seams) == (False, reason)
    assert caplog.records and all(r.levelno < logging.ERROR and r.exc_info is None
                                  for r in caplog.records)


def test_real_preparation_cancellation_stops_probes_then_can_recover(live56, monkeypatch, caplog):
    live, mint, clock = setup_job(live56, monkeypatch)
    calls = []
    base = full_safety_network(live, clock, calls)
    def network(method, url, *, body, **kwargs):
        response = base(method, url, body=body, **kwargs)
        row = json.loads(body)
        if (isinstance(row, dict) and row["method"] == "eth_call"
                and row["params"][0]["data"].startswith(("0x70a08231", "0xdd62ed3e"))):
            live.entry_active = True
        return response
    live.paced.base = network
    sleeps = _safety_sleeps(monkeypatch)
    with caplog.at_level(logging.WARNING, logger=safety.__name__):
        assert live.cache.run_one(mint, now=time.time()) is None
    assert not sleeps
    assert not any(r.levelno >= logging.ERROR or r.exc_info for r in caplog.records)
    assert live.cache.reason(mint, now=time.time()) == "no_safety_verdict"
    assert live.cache.facts(mint) is None
    assert live.cache.counts["preparation_cancelled"] == 1
    assert live.cache.counts["verdicts_unsafe"] == 0
    live.entry_active = False
    live.snapshot._last["read_at"] = time.time()
    live.paced.min_gap_s = 0
    live.paced.base = base
    result = live.cache.run_one(mint, now=time.time())
    assert result is not None and result[0]
    assert live.cache.reason(mint, now=time.time()) is None
