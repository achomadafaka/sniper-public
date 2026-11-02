"""Native cash preparation yields after funded 429s without changing funded quote policy."""
import json
import logging
import threading
from urllib.parse import parse_qs, urlsplit

import pytest

from sniper.execution import pump_curve_buy as P, pumpswap_buy as A, solana_real_seams as S


USER = "11111111111111111111111111111111"


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(S, "_monotonic", lambda: now[0])
    monkeypatch.setattr(S, "_funded_last_429_at", None)
    monkeypatch.setattr(S, "_jup_host_cooldown", {})
    monkeypatch.setattr(S, "_KEY_VAR", S.JUP_API_KEY_VAR)
    monkeypatch.setenv(S.JUP_API_KEY_VAR, "fixture-funded-key")
    return now


def _builder(kind, clock, monkeypatch):
    cls = P.SlotLaneCurveBuilder if kind == "curve" else A.SlotLanePumpSwapBuilder
    b = cls(user=USER, clock=lambda: clock[0], rpc=lambda *a: pytest.fail("unexpected RPC"))
    b._buyback, b._config_read_at = "known", clock[0]
    if kind == "curve":
        b._pump_alt_done = True
    else:
        monkeypatch.setattr(b.pools, "learn_pending", lambda: None)
    return b


def _http(calls, hook=None):
    def request(method, url, headers=None, body=None):
        calls.append((method, url, headers, body))
        if hook:
            response = hook(method, len(calls))
            if response is not None:
                return response
        if method == "GET":
            q = parse_qs(urlsplit(url).query)
            return 200, {"inputMint": P.USDC_MINT, "outputMint": P.WSOL_MINT,
                         "inAmount": q.get("amount", ["1"])[0], "outAmount": "1000",
                         "otherAmountThreshold": "900", "routePlan": []}, None
        return 200, {"swapInstruction": {"programId": USER, "accounts": [], "data": ""},
                     "addressLookupTableAddresses": []}, None
    return request


@pytest.mark.parametrize("kind", ["curve", "amm"])
def test_recent_funded_429_stops_native_cash_http_but_keeps_old_stamp(kind, clock, monkeypatch):
    b = _builder(kind, clock, monkeypatch)
    old = {"usdc_atomic": 10_000_000, "fetched_at": 998.0}
    b._leg, b._cash_legs = old, (old,)
    calls = []
    monkeypatch.setattr(S, "_http", _http(calls))
    S._funded_last_429_at = 999.0
    assert b.refresh(10.0, alternate_clip_usd=5.0)
    assert calls == [], "cash preparation must yield while funded quota recovers"
    assert b._leg is old and b._leg["fetched_at"] == 998.0
    clock[0] = 1001.01
    assert not b.refresh(10.0, alternate_clip_usd=5.0)
    assert b.matching_leg(10.0) is None, "quiet time must not extend the accepted leg age"
    assert calls == []


@pytest.mark.parametrize("kind", ["curve", "amm"])
@pytest.mark.parametrize("stage,expected_calls", [("quote", 1), ("post", 2)])
def test_own_429_stops_physical_failover_remaining_sizes_and_sibling_builder(
        kind, stage, expected_calls, clock, monkeypatch, caplog):
    calls = []
    def hook(method, n):
        if (stage == "quote" and method == "GET") or (stage == "post" and method == "POST"):
            return 429, {"error": "fixture rate limit"}, None
    monkeypatch.setattr(S, "_http", _http(calls, hook))
    b = _builder(kind, clock, monkeypatch)
    with caplog.at_level(logging.WARNING):
        assert not b.refresh(10.0, alternate_clip_usd=5.0, extra_clip_usds=(3.0,))
        sibling = _builder("amm" if kind == "curve" else "curve", clock, monkeypatch)
        assert not sibling.refresh(10.0)
    assert len(calls) == expected_calls, "no cash fallback, POST, next size or sibling after 429"
    assert S.funded_429_age_s() == 0.0
    assert not any("cash leg refresh failed" in r.getMessage() for r in caplog.records)
    assert b._leg is None and not b._cash_legs


@pytest.mark.parametrize("kind", ["curve", "amm"])
def test_unrelated_funded_429_during_get_stops_cash_post(kind, clock, monkeypatch, caplog):
    calls = []
    def hook(method, n):
        if n == 1:
            S._note_host_429(S.JUP_FALLBACK_BASE)
    monkeypatch.setattr(S, "_http", _http(calls, hook))
    b = _builder(kind, clock, monkeypatch)
    with caplog.at_level(logging.WARNING):
        assert not b.refresh(10.0, alternate_clip_usd=5.0)
    assert [c[0] for c in calls] == ["GET"]
    assert b._leg is None and not b._cash_legs
    assert not any("cash leg refresh failed" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("kind", ["curve", "amm"])
def test_midcycle_429_preserves_completed_primary_and_older_secondary(kind, clock, monkeypatch):
    calls = []
    def hook(method, n):
        if method == "POST":
            S._note_host_429(S.JUP_FALLBACK_BASE)
    monkeypatch.setattr(S, "_http", _http(calls, hook))
    b = _builder(kind, clock, monkeypatch)
    old = {"usdc_atomic": 5_000_000, "fetched_at": 998.0}
    b._cash_legs = (old,)
    assert b.refresh(10.0, alternate_clip_usd=5.0)
    assert [c[0] for c in calls] == ["GET", "POST"]
    assert b.matching_leg(10.0)["fetched_at"] == 1000.0
    assert b.matching_leg(5.0) is old and old["fetched_at"] == 998.0


def test_cash_recovers_at_ten_seconds_with_exact_sizes_headers_and_wrap_shape(clock, monkeypatch):
    calls = []
    monkeypatch.setattr(S, "_http", _http(calls))
    S._funded_last_429_at = 1000.0
    builders = [_builder(k, clock, monkeypatch) for k in ("curve", "amm")]
    clock[0] = 1009.999
    for b in builders:
        assert not b.refresh(10.0, alternate_clip_usd=5.0)
    assert calls == []
    clock[0] = 1010.0
    for b in builders:
        assert b.refresh(10.0, alternate_clip_usd=5.0)
        assert b.matching_leg(10.0)["fetched_at"] == 1010.0
        assert b.matching_leg(5.0)["usdc_atomic"] == 5_000_000
    assert [c[0] for c in calls] == ["GET", "POST"] * 4
    assert all(c[2] == {"x-api-key": "fixture-funded-key"} for c in calls)
    bodies = [json.loads(c[3]) for c in calls if c[0] == "POST"]
    assert [b["wrapAndUnwrapSol"] for b in bodies] == [True, True, False, False]
    assert [b["quoteResponse"]["inAmount"] for b in bodies] == ["10000000", "5000000"] * 2


def test_paper_429_does_not_quiet_native_cash(clock, monkeypatch):
    with S.jupiter_role(S.ROLE_PAPER):
        S._note_host_429(S.JUP_BASE)
    calls = []
    monkeypatch.setattr(S, "_http", _http(calls))
    assert _builder("curve", clock, monkeypatch).refresh(10.0)
    assert [c[0] for c in calls] == ["GET", "POST"]


def test_cash_quiet_scope_is_reset_before_an_ordinary_funded_failover(clock, monkeypatch):
    calls = []
    monkeypatch.setattr(S, "_http", _http(calls, lambda *_: (429, {}, None)))
    assert not _builder("curve", clock, monkeypatch).refresh(10.0)
    calls.clear()
    def fallback(method, n):
        return (429, {}, None) if n == 1 else (200, {"ok": True}, None)
    monkeypatch.setattr(S, "_http", _http(calls, fallback))
    assert S._jup_call("GET", "/quote?ordinary") == (200, {"ok": True})
    assert len(calls) == 2, "ordinary funded calls still try both cooled hosts"


def test_cash_scope_does_not_quiet_another_threads_funded_failover(clock, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls, live, failures = [], [], []
    def hook(method, n):
        if threading.current_thread() is cash_thread:
            if method == "GET":
                entered.set()
                assert release.wait(2)
            return None
        live.append(calls[-1])
        return (429, {}, None) if len(live) == 1 else (200, {"ok": True}, None)
    monkeypatch.setattr(S, "_http", _http(calls, hook))
    b = _builder("curve", clock, monkeypatch)
    def refresh():
        try:
            assert not b.refresh(10.0)
        except BaseException as exc:
            failures.append(exc)
    cash_thread = threading.Thread(target=refresh, name="fixture-cash")
    cash_thread.start()
    try:
        assert entered.wait(2)
        S._note_host_429(S.JUP_FALLBACK_BASE)
        assert S._jup_call("GET", "/quote?ordinary") == (200, {"ok": True})
        assert len(live) == 2
        # The keyed host may be second while cooled, but only it receives the funded header.
        keyed = [c for c in live if c[1].startswith(S.JUP_FALLBACK_BASE)]
        assert len(keyed) == 1 and keyed[0][2] == {"x-api-key": "fixture-funded-key"}
    finally:
        release.set()
        cash_thread.join(2)
    assert not cash_thread.is_alive() and not failures
    assert len(calls) == 3, "the paused cash GET must not start its POST after the live429"
