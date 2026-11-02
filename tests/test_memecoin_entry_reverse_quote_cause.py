"""The planned-quantity sell proof names WHY it failed.

docs/audits/2026-09-26-refusal-26ebbe40.md, item 2: `execute_entry` turned every failure of the
entry's reverse quote into the one refusal "no planned-quantity sell route" and the trace kept only
`available=false`. A provider timeout, a Jupiter 429, a true no-route answer, a quantity mismatch
and a zero output all read the same, so triage could not tell infra from market (26ebbe40, HZ494,
2026-09-26 23:02:24Z).

The refusal text and the released reason are unchanged, because the retry family matcher keys on
them. The cause goes into the `planned_exit_route` trace substage. No call is added: the seam
records the calls `jupiter_exit_quote` already makes, the way the entry quote's miss does.
"""
import threading

import pytest

from sniper.execution import solana_real_seams as S
from sniper.execution import solana_swap_executor as ex
from test_memecoin_entry_call_timings import _Trace
from test_memecoin_entry_roundtrip_cap import _c, _Deps
from test_memecoin_solana_quote_miss import NO_RAY, _http, _http_error

REASON = "no planned-quantity sell route"


@pytest.fixture(autouse=True)
def _no_host_cooldown():
    saved = dict(S._jup_host_cooldown)
    S._jup_host_cooldown.clear()
    yield
    S._jup_host_cooldown.clear()
    S._jup_host_cooldown.update(saved)


def _entry(reverse, *, miss=None, buy=None):
    conn, pid = _c()
    d = _Deps(usd_back=9.9)
    trace = _Trace()

    def reverse_quote_fn(mint, atomic):
        d.calls.append("roundtrip")
        return reverse(mint, atomic)

    if miss is not None:
        reverse_quote_fn.miss = miss
    quote_fn = d.quote_fn if buy is None else (lambda mint, usd: dict(buy))
    released = []
    real_release = ex.m.release_pending

    def release(c, p, reason, **k):
        released.append(reason)
        return real_release(c, p, reason, **k)

    ex.m.release_pending, saved = release, ex.m.release_pending
    try:
        result = ex.execute_entry(
            conn, pending_id=pid, mint="MINT", usd_amount=10.0, strategy="alpha_b",
            source_user_id="u1", now_iso="2026-09-27T00:00:00Z", quote_fn=quote_fn,
            reverse_quote_fn=reverse_quote_fn, sign_fn=d.sign_fn, send_fn=d.send_fn,
            confirm_fn=d.confirm_fn, usdc_fn=d.usdc_fn, token_fn=d.token_fn,
            impact_cap_pct=15.0, trace=trace)
    finally:
        ex.m.release_pending = saved
        conn.close()
    route = [s for s in trace.substages if s["name"] == "planned_exit_route"]
    assert len(route) == 1, trace.substages
    return result, route[0], d, released


def _refused_as_before(result, d, released):
    assert result["status"] == "route_failed" and result["reason"] == REASON
    assert released == ["sell_route_unavailable"]
    assert "sign" not in d.calls


def _boom():
    raise RuntimeError("accessor broke")


@pytest.mark.parametrize("reverse,miss,cause,detail", [
    (lambda m, a: None, lambda: {"code": "provider_rate_limit", "detail": "jupiter 429 on every host"},
     "provider_rate_limit", "jupiter 429 on every host"),
    (lambda m, a: None, lambda: {"code": "provider_timeout", "detail": "jupiter TimeoutError: t"},
     "provider_timeout", "jupiter TimeoutError: t"),
    (lambda m, a: None, lambda: {"code": "no_route", "detail": "jupiter 400 COULD_NOT_FIND_ANY_ROUTE"},
     "no_route", "jupiter 400 COULD_NOT_FIND_ANY_ROUTE"),
    # UNKNOWN IS NOT NO-ROUTE: a seam with no attribution, or one that breaks, says so.
    (lambda m, a: None, None, "unattributed", None),
    (lambda m, a: None, _boom, "unattributed", None),
    (lambda m, a: None, lambda: {"detail": "no code"}, "unattributed", None),
])
def test_an_empty_reverse_quote_carries_the_seams_typed_miss(reverse, miss, cause, detail):
    result, route, d, released = _entry(reverse, miss=miss)
    _refused_as_before(result, d, released)
    assert route["available"] is False
    assert route["cause"] == cause and route["detail"] == detail


def test_a_raising_reverse_quote_names_the_exception_not_a_stale_miss():
    def reverse(m, a):
        raise TimeoutError("read timed out")
    result, route, d, released = _entry(
        reverse, miss=lambda: {"code": "no_route", "detail": "from an earlier attempt"})
    _refused_as_before(result, d, released)
    assert route["cause"] == "reverse_quote_raised"
    assert route["detail"] == "TimeoutError: read timed out"


@pytest.mark.parametrize("answer,cause,detail", [
    ({"inAmount": 449, "outAmount": 9, "otherAmountThreshold": 8}, "quantity_mismatch",
     "in=449 planned=450"),
    ({"inAmount": 450, "outAmount": "0", "otherAmountThreshold": "0"}, "zero_output",
     "out=0 min_out=0"),
    ({"inAmount": 450, "outAmount": 9, "otherAmountThreshold": 0}, "zero_output",
     "out=9 min_out=0"),
    ({"inAmount": 450, "otherAmountThreshold": 8}, "quote_malformed", "missing outAmount"),
    ({"inAmount": "abc", "outAmount": 9, "otherAmountThreshold": 8}, "quote_malformed",
     "unparseable inAmount"),
    (["not", "a", "dict"], "quote_malformed", "list"),
])
def test_an_unusable_reverse_quote_says_what_was_wrong(answer, cause, detail):
    result, route, d, released = _entry(lambda m, a: answer, miss=lambda: None)
    _refused_as_before(result, d, released)
    assert route["cause"] == cause and route["detail"] == detail


def test_a_buy_without_a_minimum_never_asks_and_says_so():
    buy = {"outAmount": "500", "priceImpactPct": 0.01}
    result, route, d, released = _entry(lambda m, a: pytest.fail("no reverse call"), buy=buy)
    _refused_as_before(result, d, released)
    assert "roundtrip" not in d.calls
    assert route["cause"] == "planned_quantity_missing" and route["amount_atomic"] == 0


def test_a_usable_reverse_quote_adds_nothing_to_the_trace():
    answer = {"inAmount": 450, "outAmount": 8_910_000, "otherAmountThreshold": 8_900_000}
    _result, route, _d, _released = _entry(lambda m, a: answer, miss=lambda: None)
    assert route == {"name": "planned_exit_route", "duration_ms": None, "amount_atomic": 450,
                     "available": True}


# ------------------------------------------------------------------------------ the real seam
def test_the_recorder_types_a_reverse_miss_with_the_entry_classifier():
    cases = [
        (_http_error(429, {"message": "slow down"}), "provider_rate_limit"),
        (TimeoutError("read timed out"), "provider_timeout"),
        ((400, {"error": "Could not find any route", "errorCode": "COULD_NOT_FIND_ANY_ROUTE"}),
         "no_route"),
        ((503, {}), "provider_http_503"),
    ]
    for jupiter, code in cases:
        recorder = S.EntryQuoteMissRecorder(http=_http(jupiter, NO_RAY))
        assert recorder.reverse("Mint", 450, 30.0) is None
        assert recorder.reverse_miss()["code"] == code, jupiter


def test_the_recorded_reverse_quote_makes_exactly_the_calls_it_made_before():
    """Zero new calls: the recorder watches the transport, it does not add to it."""
    for jupiter in ((400, {"errorCode": "COULD_NOT_FIND_ANY_ROUTE"}), (503, {}),
                    (200, {"inAmount": "450", "outAmount": "9", "otherAmountThreshold": "8"})):
        plain, recorded = [], []
        S._jup_host_cooldown.clear()
        S.jupiter_exit_quote("Mint", 450, 30.0, http=_http(jupiter, NO_RAY, plain))
        S._jup_host_cooldown.clear()
        S.EntryQuoteMissRecorder(http=_http(jupiter, NO_RAY, recorded)).reverse("Mint", 450, 30.0)
        assert recorded == plain, jupiter


def test_a_later_reverse_quote_clears_the_previous_miss_and_the_entry_miss_is_separate():
    recorder = S.EntryQuoteMissRecorder(http=None)
    recorder.http = _http((503, {}), NO_RAY)
    recorder.reverse("Mint", 450, 30.0)
    assert recorder.reverse_miss()["code"] == "provider_http_503"
    assert recorder.miss() is None, "the buy quote's miss is a different seam"
    recorder.http = _http((200, {"inAmount": "450", "outAmount": "9",
                                  "otherAmountThreshold": "8"}), NO_RAY)
    assert recorder.reverse("Mint", 450, 30.0)["outAmount"] == "9"
    assert recorder.reverse_miss() is None


def test_each_thread_reads_its_own_reverse_miss():
    """One recorder per executor; entries run on worker threads concurrently."""
    answers = {"a": TimeoutError("t"), "b": (503, {})}

    def http(method, url, headers=None, body=None, timeout=10):
        return _http(answers[threading.current_thread().name], NO_RAY)(method, url)

    recorder = S.EntryQuoteMissRecorder(http=http)
    results, barrier = {}, threading.Barrier(2)

    def run():
        recorder.reverse("Mint", 450, 30.0)
        barrier.wait(timeout=5)
        results[threading.current_thread().name] = recorder.reverse_miss()["code"]

    threads = [threading.Thread(target=run, name=n) for n in answers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert results == {"a": "provider_timeout", "b": "provider_http_503"}


def test_the_funded_executor_wires_the_reverse_miss(monkeypatch):
    """At the boundary the real caller uses: the executor's reverse seam carries `.miss`."""
    from types import SimpleNamespace
    monkeypatch.setattr(ex.m, "pending_row",
                        lambda *_: {"strategy": "alpha_b", "source_user_id": "u1"})
    monkeypatch.setattr(ex, "execute_entry", lambda *a, **k: k)
    executor = S.build_real_solana_executor(
        keypair=SimpleNamespace(pubkey=lambda: "offline-owner"), rpc_url="offline://rpc",
        http=_http(_http_error(429, {"message": "slow down"}), NO_RAY))
    kwargs = executor["execute_fn"](None, 1, "MINT", 10, "2026-09-27T00:00:00Z")
    reverse = kwargs["reverse_quote_fn"]
    assert reverse("MINT", 450) is None
    assert reverse.miss()["code"] == "provider_rate_limit"
