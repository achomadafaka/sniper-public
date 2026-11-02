"""Pre-signal native-price reads preserve selected prices and their original one-second age."""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from sniper.execution import evm_real_seams as seams, submission_backends


@pytest.fixture(autouse=True)
def owned_workers():
    submission_backends.close_all()
    yield
    submission_backends.close_all()


def executor(price=None, http=None, chain="56"):
    return seams.build_real_evm_executor(
        acct=SimpleNamespace(address="0x" + "ab" * 20), chain_id=chain, rpc_url="offline",
        http=http or (lambda *a, **k: (200, {"result": "0x0"}, None)), price_fn=price)


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(.001)


def test_background_refresh_preserves_the_selected_price_until_original_expiry():
    calls = []
    ex = executor(lambda: calls.append(time.monotonic()) or (740 if len(calls) == 1 else 800))
    stop, thread = ex["start_quote_price_warmer_fn"]()
    try:
        wait_for(lambda: ex["quote_price_peek_fn"].status()["ready"])
        first = ex["quote_price_peek_fn"]()
        wait_for(lambda: len(calls) == 2)
        stop.set()
        thread.join(1)
        assert calls[1] - calls[0] >= .49
        assert ex["warm_quote_price_fn"]() == first["px"] == 740
        wait_for(lambda: ex["quote_price_peek_fn"]()["px"] == 800)
        assert ex["quote_price_peek_fn"]()["age_ms"] > 300, "selection must keep original preparation age"
        assert ex["quote_price_peek_fn"]()["source"] == "native_price_prepared"
    finally:
        stop.set()
        thread.join(1)


def test_background_failures_back_off_exponentially_and_cap_at_thirty_seconds(monkeypatch):
    waits, calls = [], []
    register = submission_backends._register_warmer

    def register_with_controlled_wait(stop, thread):
        def wait(seconds):
            waits.append(seconds)
            if len(waits) == 6:
                stop.set()
            return stop.is_set()

        stop.wait = wait
        register(stop, thread)

    monkeypatch.setattr(submission_backends, "_register_warmer", register_with_controlled_wait)
    ex = executor(lambda: calls.append(1) or None)
    stop, thread = ex["start_quote_price_warmer_fn"]()
    thread.join(1)
    assert not thread.is_alive() and stop.is_set()
    assert waits == [1, 2, 4, 8, 16, 30] and len(calls) == 6


def test_preparation_shares_an_existing_live_read_and_does_not_clear_its_owner():
    from concurrent.futures import ThreadPoolExecutor
    calls = []
    reading, release = threading.Event(), threading.Event()

    def price():
        calls.append(1)
        reading.set()
        assert release.wait(2)
        return 740

    ex = executor(price)
    with ThreadPoolExecutor(max_workers=2) as pool:
        live = pool.submit(ex["warm_quote_price_fn"])
        assert reading.wait(1)
        stop, thread = ex["start_quote_price_warmer_fn"]()
        try:
            other = pool.submit(ex["warm_quote_price_fn"])
            time.sleep(.02)
            assert len(calls) == 1
            release.set()
            assert live.result(1) == other.result(1) == 740
            assert ex["quote_price_peek_fn"]()["source"] == "native_price_live"
            assert len(calls) == 1
        finally:
            release.set()
            stop.set()
            thread.join(1)
    assert not thread.is_alive()


def test_a_fresh_selected_price_never_waits_for_background_refresh():
    calls = []
    reading, release = threading.Event(), threading.Event()

    def price():
        calls.append(1)
        if len(calls) == 1:
            return 740
        reading.set()
        assert release.wait(2)
        return 800

    ex = executor(price)
    assert ex["warm_quote_price_fn"]() == 740
    stop, thread = ex["start_quote_price_warmer_fn"]()
    try:
        assert reading.wait(1)
        started = time.perf_counter()
        assert ex["warm_quote_price_fn"]() == ex["quote_price_peek_fn"]()["px"] == 740
        assert time.perf_counter() - started < .05
    finally:
        stop.set()
        release.set()
        thread.join(1)
    assert not thread.is_alive()


@pytest.mark.parametrize("failure", [None, 0, -1, float("nan"), float("inf"), RuntimeError("secret URL")])
def test_failure_preserves_original_age_and_does_not_publish_an_unknown_price(failure):
    calls = []

    def price():
        calls.append(1)
        if len(calls) == 1:
            return 740
        if isinstance(failure, Exception):
            raise failure
        return failure

    ex = executor(price)
    assert ex["warm_quote_price_fn"]() == 740
    before = ex["quote_price_peek_fn"]()["age_ms"]
    stop, thread = ex["start_quote_price_warmer_fn"]()
    try:
        wait_for(lambda: ex["quote_price_peek_fn"].status()["last_error"] is not None)
        assert ex["quote_price_peek_fn"]()["age_ms"] >= before
        assert ex["warm_quote_price_fn"]() == 740
        assert "secret URL" not in json.dumps(ex["quote_price_peek_fn"].status())
    finally:
        stop.set()
        thread.join(1)
    assert not thread.is_alive()


def test_old_preparation_never_gains_a_new_second_when_selected(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(seams._time, "monotonic", lambda: now[0])
    ex = executor(lambda: 740)
    stop, thread = ex["start_quote_price_warmer_fn"]()
    try:
        deadline = time.perf_counter() + 1
        while not ex["quote_price_peek_fn"].status()["ready"]:
            assert time.perf_counter() < deadline
            time.sleep(.001)
        stop.set()
        thread.join(1)
        now[0] = 100.9
        assert ex["quote_price_peek_fn"]()["age_ms"] == pytest.approx(900)
        now[0] = 101.01
        assert ex["quote_price_peek_fn"].status()["ready"] is False
        assert ex["quote_price_peek_fn"]()["age_ms"] > 1000
    finally:
        stop.set()
        thread.join(1)


def test_cold_failed_preparation_is_unknown_and_live_fallback_can_recover():
    answers = iter([None, 740])
    ex = executor(lambda: next(answers))
    stop, thread = ex["start_quote_price_warmer_fn"]()
    try:
        wait_for(lambda: ex["quote_price_peek_fn"].status()["last_error"] is not None)
        stop.set()
        thread.join(1)
        assert ex["quote_price_peek_fn"]() is None
        assert ex["warm_quote_price_fn"]() == 740
        assert ex["quote_price_peek_fn"]()["source"] == "native_price_live"
    finally:
        stop.set()
        thread.join(1)


def test_standalone_factory_does_not_start_and_start_is_owned_and_idempotent():
    calls = []
    ex = executor(lambda: calls.append(1) or 740)
    assert calls == [] and ex["quote_price_peek_fn"]() is None
    first = ex["start_quote_price_warmer_fn"]()
    assert ex["start_quote_price_warmer_fn"]() == first
    wait_for(lambda: calls)
    submission_backends.close_all()
    assert first[0].is_set() and not first[1].is_alive()


@pytest.mark.parametrize("chain,injected,expected", [("56", False, 1), ("56", True, 0),
                                                     ("4663", False, 0)])
def test_only_the_default_bsc_context_starts_price_preparation(
        monkeypatch, tmp_path, chain, injected, expected):
    from sniper.services import memecoin_evm_leg as leg
    from sniper.settings import memecoin as cfg
    from sniper.execution.evm_wallet import generate_keypair_file
    keypair = str(tmp_path / "evm.json")
    generate_keypair_file(keypair)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", keypair)
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_" + chain, "direct")
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_" + chain, "https://offline")
    monkeypatch.setenv("MEMECOIN_EVM_SUBMIT_MODE", "rpc")
    calls = []

    def http(method, url, **kwargs):
        if method == "GET":
            calls.append(url)
            return 200, {"price": "740"}, None
        request = json.loads(kwargs["body"])
        return 200, {"result": hex(int(chain)) if request["method"] == "eth_chainId" else "0x"}, None

    original_read_http = leg._funded_evm_read_http
    monkeypatch.setattr(leg, "_funded_evm_read_http", lambda _: original_read_http(http))
    assert leg.build_real_evm_context(chain, http=http if injected else None) is not None
    if expected:
        wait_for(lambda: calls)
    assert len(calls) == expected


@pytest.mark.parametrize("prepared", [740, None, RuntimeError("timeout")])
def test_expired_price_uses_live_fallback_without_waiting_for_preparation(monkeypatch, prepared):
    from concurrent.futures import ThreadPoolExecutor
    calls = []
    reading, release, fallback = threading.Event(), threading.Event(), threading.Event()
    now = [100.0]
    monkeypatch.setattr(seams._time, "monotonic", lambda: now[0])

    def price():
        calls.append(1)
        if len(calls) == 1:
            return 700
        if len(calls) == 2:
            reading.set()
            assert release.wait(2)
            if isinstance(prepared, Exception):
                raise prepared
            return prepared
        fallback.set()
        return 800

    ex = executor(price)
    assert ex["warm_quote_price_fn"]() == 700
    stop, thread = ex["start_quote_price_warmer_fn"]()
    try:
        assert reading.wait(1)
        now[0] = 101.01
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(ex["warm_quote_price_fn"]) for _ in range(2)]
            try:
                assert fallback.wait(.2), "optional preparation blocked the live fallback"
                assert [future.result(.2) for future in results] == [800, 800]
                assert not release.is_set()
            finally:
                release.set()
        assert ex["quote_price_peek_fn"]()["px"] == 800
        assert len(calls) == 3
    finally:
        release.set()
        stop.set()
        thread.join(1)


@pytest.mark.parametrize("failure", [None, RuntimeError("timeout")])
def test_failed_preparation_cannot_clear_the_inflight_live_fallback(monkeypatch, failure):
    from concurrent.futures import Future, ThreadPoolExecutor
    now, calls = [100.0], []
    monkeypatch.setattr(seams._time, "monotonic", lambda: now[0])
    preparing, release_preparation = threading.Event(), threading.Event()
    live_started, release_live = threading.Event(), threading.Event()
    joined = threading.Event()

    class JoiningFuture(Future):
        def result(self, *args, **kwargs):
            joined.set()
            return super().result(*args, **kwargs)

    monkeypatch.setattr(seams, "Future", JoiningFuture)

    def price():
        calls.append(1)
        if len(calls) == 1:
            return 700
        if len(calls) == 2:
            preparing.set()
            assert release_preparation.wait(2)
            if failure is not None:
                raise failure
            return None
        assert len(calls) == 3, "preparation cleared another read's owner"
        live_started.set()
        assert release_live.wait(2)
        return 800

    ex = executor(price)
    assert ex["warm_quote_price_fn"]() == 700
    stop, thread = ex["start_quote_price_warmer_fn"]()
    try:
        assert preparing.wait(1)
        now[0] = 101.01
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(ex["warm_quote_price_fn"])
            try:
                assert live_started.wait(1)
                stop.set()
                release_preparation.set()
                thread.join(1)
                assert not thread.is_alive()
                second = pool.submit(ex["warm_quote_price_fn"])
                assert joined.wait(1), "live consumers lost their shared pending read"
                release_live.set()
                assert first.result(1) == second.result(1) == 800
            finally:
                release_live.set()
                release_preparation.set()
        assert len(calls) == 3
    finally:
        stop.set()
        release_live.set()
        release_preparation.set()
        thread.join(1)


def test_background_never_spends_fmp_quota_but_live_fallback_retains_it(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "test-key")
    calls = []

    def http(method, url, **kwargs):
        calls.append(url)
        if "financialmodelingprep.com" in url:
            return 200, [{"price": "740"}], None
        return 429, {}, None

    ex = executor(http=http)
    stop, thread = ex["start_quote_price_warmer_fn"]()
    try:
        wait_for(lambda: ex["quote_price_peek_fn"].status()["last_error"] is not None)
        stop.set()
        thread.join(1)
        assert len(calls) == 1 and "binance.com" in calls[0]
        assert ex["quote_price_peek_fn"]() is None
        assert ex["warm_quote_price_fn"]() == 740
        assert len(calls) == 3 and "financialmodelingprep.com" in calls[-1]
    finally:
        stop.set()
        thread.join(1)


@pytest.mark.parametrize("status", [429, 403, 500])
def test_background_does_not_publish_a_price_in_a_rejected_http_answer(status):
    ex = executor(http=lambda *args, **kwargs: (status, {"price": "740"}, None))
    stop, thread = ex["start_quote_price_warmer_fn"]()
    try:
        wait_for(lambda: ex["quote_price_peek_fn"].status()["last_error"] is not None)
        assert ex["quote_price_peek_fn"]() is None
    finally:
        stop.set()
        thread.join(1)


@pytest.mark.parametrize("failure", [RuntimeError("thread budget"), OSError("thread unavailable")])
def test_optional_preparation_thread_start_failure_preserves_live_price(monkeypatch, failure):
    register = submission_backends._register_warmer

    def register_failing_start(stop, thread):
        register(stop, thread)

        def start():
            raise failure

        monkeypatch.setattr(thread, "start", start)

    monkeypatch.setattr(submission_backends, "_register_warmer", register_failing_start)
    ex = executor(lambda: 740)
    stop, thread = ex["start_quote_price_warmer_fn"]()
    assert stop.is_set() and not thread.is_alive()
    assert ex["quote_price_peek_fn"].status()["last_error"] == type(failure).__name__
    assert ex["quote_price_peek_fn"].status()["thread_alive"] is False
    assert ex["warm_quote_price_fn"]() == 740
    assert ex["quote_price_peek_fn"]()["source"] == "native_price_live"
    assert ex["start_quote_price_warmer_fn"]() == (stop, thread)
    submission_backends.close_all()


def test_price_status_distinguishes_an_unstarted_worker_without_reading_price():
    calls = []
    ex = executor(lambda: calls.append(1) or 740)
    assert ex["quote_price_peek_fn"].status() == {
        "ready": False, "last_error": None, "thread_alive": False, "age_s": None,
    }
    assert calls == []


def test_price_status_distinguishes_a_blocked_active_worker_from_a_stopped_worker():
    reading, release = threading.Event(), threading.Event()

    def price():
        reading.set()
        assert release.wait(1)
        return 740

    ex = executor(price)
    stop, thread = ex["start_quote_price_warmer_fn"]()
    try:
        assert reading.wait(1)
        assert ex["quote_price_peek_fn"].status() == {
            "ready": False, "last_error": None, "thread_alive": True, "age_s": None,
        }
        stop.set()
        assert thread.is_alive()
        assert ex["quote_price_peek_fn"].status()["thread_alive"] is True
    finally:
        stop.set()
        release.set()
        thread.join(1)
    assert ex["quote_price_peek_fn"].status()["thread_alive"] is False


def test_price_status_returns_unknown_without_waiting_for_its_owned_memo_lock():
    import inspect

    ex = executor(lambda: 740)
    status = ex["quote_price_peek_fn"].status
    lock = inspect.getclosurevars(status).nonlocals["_price_lock"]
    held, release = threading.Event(), threading.Event()

    def hold():
        with lock:
            held.set()
            release.wait(.2)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    try:
        assert held.wait(1)
        started = time.perf_counter()
        answer = status()
        assert time.perf_counter() - started < .05
        assert answer == {"ready": None, "last_error": "busy", "thread_alive": None}
    finally:
        release.set()
        thread.join(1)
    assert not thread.is_alive()
