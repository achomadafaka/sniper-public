"""A Solana entry quote miss names its class, in the EVM vocabulary.

solana_swap_executor.py released every empty entry quote as a bare "no quote": 36 funded attempts
from 2026-09-09 to 09-16 (bopa x26 on 09-14 ran +155% in 5 min; SCRIBE +785% in 15 min; CrowBrain
23:08:14Z). The history was read before building: all 36 had zero Jupiter 429s in their minute,
and their quote stage took 349-444 ms (p10-p90) against 122 ms (p50) for filled entries -- two round
trips, i.e. Jupiter answered without a route and the Raydium fallback also had none. They were
no_route, so this change labels the miss and does NOT add a retry. The reservation is released
exactly as before.
"""
import threading
import time
import urllib.error
import io
import json

import pytest

from sniper.execution import solana_real_seams as S
from sniper.execution import solana_swap_executor as X
from test_memecoin_entry_executor import _c, _Deps, _run


@pytest.fixture(autouse=True)
def _no_host_cooldown():
    saved = dict(S._jup_host_cooldown)
    S._jup_host_cooldown.clear()
    yield
    S._jup_host_cooldown.clear()
    S._jup_host_cooldown.update(saved)


def _executor(c, pid, d, **extra):
    return X.execute_entry(
        c, pending_id=pid, mint="MINT", usd_amount=10.0, strategy="alpha_b",
        source_user_id="u1", now_iso="2026-07-09T00:00:00Z",
        quote_fn=d.quote_fn, reverse_quote_fn=d.reverse_quote_fn,
        sign_fn=d.sign_fn, send_fn=d.send_fn, confirm_fn=d.confirm_fn,
        usdc_fn=d.usdc_fn, token_fn=d.token_fn, **extra)


def test_a_quote_miss_names_its_class_and_releases_as_today(monkeypatch):
    c, pid = _c()
    d = _Deps(); d.quote_fn = lambda mint, usd: None
    released = []
    real_release = X.m.release_pending
    monkeypatch.setattr(X.m, "release_pending", lambda conn, p, r, **k: released.append(r) or real_release(conn, p, r, **k))
    miss = {"code": "no_route", "detail": "jupiter 400 COULD_NOT_FIND_ANY_ROUTE; raydium ROUTE_NOT_FOUND"}
    result = _executor(c, pid, d, quote_miss_fn=lambda: miss)
    assert result["status"] == "aborted"
    assert result["reason"] == "no quote (no_route: jupiter 400 COULD_NOT_FIND_ANY_ROUTE; raydium ROUTE_NOT_FOUND)"
    assert result["quote_miss_code"] == "no_route"
    assert released == ["quote_unavailable"]
    assert "sign" not in d.calls


@pytest.mark.parametrize("seam", [None, lambda: None, lambda: {}, lambda: {"detail": "x"},
                                  lambda: (_ for _ in ()).throw(RuntimeError("boom"))])
def test_an_unattributed_miss_keeps_the_bare_reason(seam):
    c, pid = _c()
    d = _Deps(); d.quote_fn = lambda mint, usd: None
    extra = {} if seam is None else {"quote_miss_fn": seam}
    result = _executor(c, pid, d, **extra)
    assert result["reason"] == "no quote" and result.get("quote_miss_code") is None


def test_the_labelled_miss_still_enters_the_retry_lane():
    from sniper.services.memecoin_real_runner import _is_transient_entry_miss
    assert _is_transient_entry_miss({"status": "aborted", "reason": "no quote (no_route: jupiter 400)"})
    assert _is_transient_entry_miss({"status": "aborted", "reason": "no quote"})


def test_the_oversize_requote_keeps_the_miss_seam(monkeypatch):
    from test_memecoin_oversize_retry import _Harness
    import sniper.settings.memecoin as cfg
    h = _Harness(first_bytes=1300, retry_bytes=900)
    first = h.quote_fn
    h.quote_fn = lambda mint, usd, max_accounts=None, **k: None if max_accounts else first(mint, usd)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_OVERSIZE_RETRY_MAX_ACCOUNTS", 48)
    monkeypatch.setattr(X.m, "release_pending", lambda c, p, r, **k: None)
    monkeypatch.setattr(X.m, "pending_row", lambda c, p: {"network_id": "solana"})
    monkeypatch.setattr(X.m, "validated_pending_funded_contract", lambda c, p: {})
    monkeypatch.setattr(X.m, "set_pending_sig", lambda *a, **k: None)
    monkeypatch.setattr(X, "_post_peak_confirmation", lambda *a, **k: None)
    res = X.execute_entry(
        None, pending_id=1, mint="M", usd_amount=30.0, strategy="s", source_user_id="u",
        now_iso="2026-08-23T00:00:00+00:00", quote_fn=h.quote_fn, reverse_quote_fn=h.reverse_quote_fn,
        sign_fn=h.sign_fn, send_fn=h.send_fn, confirm_fn=lambda sig: "failed",
        usdc_fn=lambda: 100.0, token_fn=lambda m_: (0.0, 6), pending={"network_id": "solana"},
        sleep_fn=lambda s: None,
        quote_miss_fn=lambda: {"code": "provider_timeout", "detail": "jupiter TimeoutError"})
    assert h.sent == [], "nothing was signed or sent on the capped re-quote"
    assert res["reason"] == "no quote (provider_timeout: jupiter TimeoutError)"


# ------------------------------------------------------------------------------ the classifier
JUP = S.JUP_BASE


def _http(jupiter, raydium=None, calls=None):
    """`jupiter`/`raydium`: a (status, payload) tuple, or an exception to raise."""
    def http(method, url, headers=None, body=None, timeout=10):
        answer = jupiter if url.startswith(("https://lite-api.jup.ag", "https://api.jup.ag")) else raydium
        if calls is not None:
            calls.append(url)
        if isinstance(answer, BaseException):
            raise answer
        return answer[0], answer[1], None
    return http


def _http_error(code, body):
    raw = json.dumps(body).encode()
    return urllib.error.HTTPError("https://lite-api.jup.ag/swap/v1/quote", code, "err", {}, io.BytesIO(raw))


NO_RAY = (200, {"success": False, "msg": "ROUTE_NOT_FOUND"})


@pytest.mark.parametrize("c2", [False, True], ids=["serial", "c2_parallel"])
def test_a_jupiter_no_route_answer_and_a_raydium_miss_is_no_route(monkeypatch, c2):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RAYDIUM_PARALLEL", c2, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RAYDIUM_NONBLOCKING", False, raising=False)  # the C2 blocking contract
    body = {"error": "Could not find any route", "errorCode": "COULD_NOT_FIND_ANY_ROUTE"}
    for jupiter in ((400, body), _http_error(400, body), (200, body)):
        quote, miss = S.typed_jupiter_entry_quote("Mint", 10.0, http=_http(jupiter, NO_RAY))
        assert quote is None and miss["code"] == "no_route", jupiter
        assert "COULD_NOT_FIND_ANY_ROUTE" in miss["detail"] and "raydium" in miss["detail"]


def test_a_jupiter_throttle_on_every_host_is_a_rate_limit():
    for jupiter in ((429, {"message": "Too Many Requests"}), _http_error(429, {"message": "slow down"})):
        quote, miss = S.typed_jupiter_entry_quote("Mint", 10.0, http=_http(jupiter, NO_RAY))
        assert quote is None and miss["code"] == "provider_rate_limit"


@pytest.mark.parametrize("c2", [False, True], ids=["serial", "c2_parallel"])
def test_a_jupiter_server_error_names_its_status_and_skips_raydium(monkeypatch, c2):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RAYDIUM_PARALLEL", c2, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RAYDIUM_NONBLOCKING", False, raising=False)  # the C2 blocking contract
    calls = []
    ray_route = (200, {"success": True, "data": {"outputAmount": "5", "otherAmountThreshold": "4",
                                                 "priceImpactPct": 0.1, "inputAmount": "10000000"}})
    quote, miss = S.typed_jupiter_entry_quote("Mint", 10.0, http=_http((503, {}), ray_route, calls))
    # A 5xx never falls back, even when C2 already holds a Raydium route beside it.
    assert quote is None and miss["code"] == "provider_http_503"
    if not c2:
        assert not any("raydium" in u for u in calls)


@pytest.mark.parametrize("exc,code", [
    (TimeoutError("read timed out"), "provider_timeout"),
    (urllib.error.URLError(TimeoutError("timed out")), "provider_timeout"),
    (urllib.error.URLError(ConnectionRefusedError("refused")), "provider_rpc_transport"),
    (OSError("network unreachable"), "provider_rpc_transport"),
])
@pytest.mark.parametrize("c2", [False, True], ids=["serial", "c2_parallel"])
def test_a_transport_failure_is_typed_and_skips_raydium(monkeypatch, c2, exc, code):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RAYDIUM_PARALLEL", c2, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RAYDIUM_NONBLOCKING", False, raising=False)  # the C2 blocking contract
    calls = []
    ray_route = (200, {"success": True, "data": {"outputAmount": "5", "otherAmountThreshold": "4",
                                                 "priceImpactPct": 0.1, "inputAmount": "10000000"}})
    quote, miss = S.typed_jupiter_entry_quote("Mint", 10.0, http=_http(exc, ray_route, calls))
    # A transport failure never falls back. With C2 on Raydium is CALLED beside Jupiter, and its
    # route is never used (Gamma LOW on 1074ecbf6): same outcome, same code.
    assert quote is None and miss["code"] == code
    if not c2:
        assert not any("raydium" in u for u in calls)


def test_an_answer_with_neither_a_route_nor_an_error_is_malformed():
    quote, miss = S.typed_jupiter_entry_quote("Mint", 10.0, http=_http((200, {"routePlan": []}), NO_RAY))
    assert quote is None and miss["code"] == "quote_malformed"


def test_a_quote_has_no_miss():
    ok = (200, {"inAmount": "10000000", "outAmount": "500", "otherAmountThreshold": "470",
                "priceImpactPct": "0.001"})
    quote, miss = S.typed_jupiter_entry_quote("Mint", 10.0, http=_http(ok, NO_RAY))
    assert quote["outAmount"] == "500" and miss is None


def test_the_recorder_keeps_the_body_for_the_quote_seam():
    """Reading an HTTPError body to classify it must leave the same body for _http_status."""
    body = {"error": "Could not find any route", "errorCode": "COULD_NOT_FIND_ANY_ROUTE"}
    seen = {}
    real = S._http_status

    def spy(http, method, url, body=None, headers=None):
        result = real(http, method, url, body=body, headers=headers)
        if "jup.ag" in url:
            seen["payload"] = result[1]
        return result
    import unittest.mock as um
    with um.patch.object(S, "_http_status", spy):
        S.typed_jupiter_entry_quote("Mint", 10.0, http=_http(_http_error(400, body), NO_RAY))
    assert seen["payload"] == body


def test_each_thread_reads_its_own_last_miss():
    """The seams build one recorder per executor, and entries can run concurrently."""
    recorder = S.EntryQuoteMissRecorder(http=None)
    results, barrier = {}, threading.Barrier(2)
    a = threading.Thread(target=_thread_quote, args=(recorder, "a", TimeoutError("t"), results, barrier))
    b = threading.Thread(target=_thread_quote, args=(recorder, "b", (503, {}), results, barrier))
    a.start(); b.start(); a.join(); b.join()
    assert results["a"]["code"] == "provider_timeout" and results["b"]["code"] == "provider_http_503"


def test_a_later_quote_clears_the_previous_miss():
    recorder = S.EntryQuoteMissRecorder(http=None)
    recorder.quote("Mint", 10.0, http=_http((503, {}), NO_RAY))
    ok = (200, {"inAmount": "1", "outAmount": "5", "otherAmountThreshold": "4", "priceImpactPct": "0"})
    assert recorder.quote("Mint", 10.0, http=_http(ok, NO_RAY))["outAmount"] == "5"
    assert recorder.miss() is None


def _thread_quote(recorder, name, jupiter, results, barrier):
    recorder.quote("Mint", 10.0, http=_http(jupiter, NO_RAY))
    barrier.wait()
    results[name] = recorder.miss()


def test_the_funded_solana_entry_passes_its_miss_seam(monkeypatch):
    import sqlite3
    from solders.keypair import Keypair
    from sniper.execution import solana_swap_executor
    from sniper.execution.submission_transport import AttemptState
    from sniper.persistence import db_memecoin as m
    from test_memecoin_fast_submit_wiring import StubTransport
    seen = {}

    def fake_execute_entry(conn, **kwargs):
        seen["quote"] = kwargs["quote_fn"]("MintX", 1.0)
        seen["miss"] = kwargs["quote_miss_fn"]()
        return {"status": "aborted", "reason": "no quote"}

    monkeypatch.setattr(solana_swap_executor, "execute_entry", fake_execute_entry)
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    pending_id = m.reserve_pending(conn, mint="MintX", source_user_id="u", strategy="alpha_b",
                                   notional=1.0, reserved_at="t", network_id="solana")
    transport = StubTransport(AttemptState.ACCEPTED)
    executor = S.build_real_solana_executor(
        keypair=Keypair(), rpc_url="https://rpc", http=_http((503, {}), NO_RAY),
        submission_transport=transport, usdc_fn=lambda: 1.0)
    executor["execute_fn"](conn, pending_id, "MintX", 1.0, "t", chase_max_pct=30.0,
        entry_min_slot=100, entry_max_slot=101, entry_head_fn=lambda: (100, time.monotonic()))
    assert seen["quote"] is None and seen["miss"]["code"] == "provider_http_503"
    assert transport.calls == 0


def test_a_malformed_exit_quote_names_the_body_shape(monkeypatch, caplog):
    """The 2026-09-17 19:0xZ QUOTE-INFRA page said only "malformed_response" and the body was gone,
    so nothing could tell a route-less answer from a provider shape change."""
    import logging
    from sniper.execution import solana_real_seams as S
    from sniper.analysis.memecoin_quote_failures import QuoteFailure

    monkeypatch.setattr(S, "jupiter_exit_quote",
                        lambda *a, **k: k["http"]("GET", "https://jup") and None)
    monkeypatch.setattr(S, "_http", lambda *a, **k: (200, {"contextSlot": 1, "timeTaken": 0.1}, None))

    with caplog.at_level(logging.WARNING):
        quote, failure = S._typed_jupiter_exit_quote("MintAAA", 1000, 8.0)

    assert quote is None and failure is QuoteFailure.MALFORMED_RESPONSE
    assert any("exact exit quote malformed" in r.message for r in caplog.records)
    assert any("contextSlot" in str(r.args) for r in caplog.records)
