"""On-chain lane latency cut C2 (spec docs/superpowers/specs/2026-10-01-memecoin-onchain-lane-
latency-design.md; operator order 2026-10-01, LILMET 68051).

(C1 is tests/test_memecoin_onchain_lane_c1.py.)
C2: with MEMECOIN_ENTRY_RAYDIUM_PARALLEL the entry quote's Raydium second opinion starts beside
Jupiter's instead of after it; the decision rule is today's.
"""
import threading
import time
from types import SimpleNamespace

import pytest

from sniper.execution import solana_real_seams as S
from sniper.settings import memecoin as cfg

from test_memecoin_raydium_fallback import MINT, RAY_COMPUTE, _http  # noqa: E402

RAY_BETTER = {"success": True, "data": {**RAY_COMPUTE["data"], "outputAmount": "999",
                                        "otherAmountThreshold": "990", "priceImpactPct": 0.4}}
THIN = {"outAmount": "100", "otherAmountThreshold": "90", "priceImpactPct": "0.83"}
GOOD = {"outAmount": "100", "otherAmountThreshold": "97", "priceImpactPct": "0.01"}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    S._jup_host_cooldown.clear()
    monkeypatch.setattr(S, "_mint_is_token_2022", lambda mint, http=None: False)
    yield
    S._jup_host_cooldown.clear()


# --- C2 -------------------------------------------------------------------------------------------

def _parallel(monkeypatch, on=True):
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RAYDIUM_PARALLEL", on, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RAYDIUM_NONBLOCKING", False, raising=False)  # the C2 blocking contract


@pytest.mark.parametrize("jupiter,ray", [(THIN, RAY_BETTER), (THIN, RAY_COMPUTE),
                                         ((429, {"error": "busy"}), RAY_COMPUTE),
                                         ((400, {"error": "unknown"}), RAY_COMPUTE)])
def test_c2_the_parallel_answer_equals_todays_serial_answer(monkeypatch, jupiter, ray):
    status, jup = jupiter if isinstance(jupiter, tuple) else (200, jupiter)
    out = {}
    for on in (False, True):
        _parallel(monkeypatch, on)
        S._jup_host_cooldown.clear()
        http = _http([("jup.ag", status, dict(jup)), ("raydium.io", 200, ray)])
        q = S.jupiter_entry_quote(MINT, 45.0, http=http, slippage_bps=300)
        tag = dict(q.pop("_sniper_fallback")) if q else {}
        out[on] = (q, {k: v for k, v in tag.items()
                       if k not in ("jupiter_done_ms", "raydium_done_ms", "raydium_parallel",
                                    "raydium_wait_ms")}, tag)
    assert out[True][:2] == out[False][:2], "same quote, same branch, same sizes"
    assert out[True][2]["raydium_parallel"] is True
    assert out[True][2]["raydium_done_ms"] >= 0 and out[True][2]["jupiter_done_ms"] >= 0
    assert out[False][2] == out[False][1], "knob off: today's tag, no timing fields"


def test_c2_a_healthy_jupiter_route_never_uses_the_raydium_answer(monkeypatch):
    _parallel(monkeypatch)
    http = _http([("jup.ag", 200, dict(GOOD)), ("raydium.io", 200, RAY_BETTER)])
    q = S.jupiter_entry_quote(MINT, 45.0, http=http, slippage_bps=300)
    assert q["outAmount"] == "100" and "_venue" not in q
    assert q["_sniper_fallback"]["raydium"] == "not_asked"
    assert "raydium_done_ms" not in q["_sniper_fallback"]


def test_c2_raydium_starts_before_jupiter_answers(monkeypatch):
    _parallel(monkeypatch)
    t = {}

    def http(method, url, body=None):
        if "raydium.io" in url:
            t["ray_start"] = time.monotonic()
            return 200, RAY_BETTER, b""
        time.sleep(0.2)
        t["jup_end"] = time.monotonic()
        return 200, dict(THIN), b""
    q = S.jupiter_entry_quote(MINT, 45.0, http=http, slippage_bps=300)
    assert q["_venue"] == "raydium"
    assert t["ray_start"] < t["jup_end"]
    tag = q["_sniper_fallback"]
    assert tag["raydium_done_ms"] < tag["jupiter_done_ms"], tag


def test_c2_exit_quotes_never_start_a_parallel_raydium_call(monkeypatch):
    _parallel(monkeypatch)
    started = []
    monkeypatch.setattr(S, "_ray_parallel_start", lambda *a, **k: started.append(1))
    http = _http([("jup.ag", 200, dict(GOOD)), ("raydium.io", 200, RAY_BETTER)])
    S.jupiter_exit_quote(MINT, 10 ** 9, 5.0, http=http)
    assert started == []


def test_c2_both_workers_busy_takes_todays_serial_path(monkeypatch):
    _parallel(monkeypatch)
    assert S._RAY_PARALLEL_SLOTS.acquire(blocking=False)
    assert S._RAY_PARALLEL_SLOTS.acquire(blocking=False)
    try:
        http = _http([("jup.ag", 200, dict(THIN)), ("raydium.io", 200, RAY_BETTER)])
        q = S.jupiter_entry_quote(MINT, 45.0, http=http, slippage_bps=300)
        assert q["_venue"] == "raydium"
        assert q["_sniper_fallback"]["raydium_parallel"] is False
    finally:
        S._RAY_PARALLEL_SLOTS.release()
        S._RAY_PARALLEL_SLOTS.release()


# --- C2 on the PRODUCTION typed seam (Gamma HIGH on 15ec9ade1) ------------------------------------
# The funded entry quotes through EntryQuoteMissRecorder -> typed_jupiter_entry_quote, whose http
# reads the CALLER thread's recorder. The parallel Raydium call must run inside that recorder.

JUP_429 = (429, {"message": "Too Many Requests"})


def _typed_http(jupiter, raydium):
    def http(method, url, headers=None, body=None, timeout=10):
        answer = jupiter if "jup.ag" in url else raydium
        return answer[0], answer[1], None
    return http


@pytest.mark.parametrize("on", [False, True])
def test_c2_the_production_seam_still_rescues_a_jupiter_429_through_raydium(monkeypatch, on):
    _parallel(monkeypatch, on)
    recorder = S.EntryQuoteMissRecorder(_typed_http(JUP_429, (200, RAY_COMPUTE)))
    q = recorder.quote(MINT, 45.0)
    assert q is not None and q["_venue"] == "raydium", recorder.miss()
    assert recorder.miss() is None
    assert q["_sniper_fallback"]["branch"] == "miss" and q["_sniper_fallback"]["raydium"] == "won"


@pytest.mark.parametrize("on", [False, True])
def test_c2_the_production_seam_keeps_a_thin_route_win(monkeypatch, on):
    _parallel(monkeypatch, on)
    recorder = S.EntryQuoteMissRecorder(_typed_http((200, dict(THIN)), (200, RAY_BETTER)))
    q = recorder.quote(MINT, 45.0)
    assert q["_venue"] == "raydium" and q["_sniper_fallback"]["raydium"] == "won"
    assert q["_sniper_fallback"].get("raydium_parallel", False) is on


@pytest.mark.parametrize("on", [False, True])
def test_c2_the_miss_classifier_still_sees_the_raydium_answer(monkeypatch, on):
    _parallel(monkeypatch, on)
    no_route = (400, {"error": "Could not find any route", "errorCode": "COULD_NOT_FIND_ANY_ROUTE"})
    recorder = S.EntryQuoteMissRecorder(_typed_http(no_route, (200, {"success": False,
                                                                     "msg": "ROUTE_NOT_FOUND"})))
    assert recorder.quote(MINT, 45.0) is None
    miss = recorder.miss()
    assert miss["code"] == "no_route" and "raydium ROUTE_NOT_FOUND" in miss["detail"], miss


def test_c2_the_worker_slots_are_released_so_every_quote_stays_parallel(monkeypatch):
    """Gamma LOW 1: a slot never released would degrade C2 to serial after two quotes."""
    _parallel(monkeypatch)
    recorder = S.EntryQuoteMissRecorder(_typed_http((200, dict(THIN)), (200, RAY_BETTER)))
    for _ in range(5):
        q = recorder.quote(MINT, 45.0)
        assert q["_sniper_fallback"]["raydium_parallel"] is True
        S._jup_host_cooldown.clear()


def test_c2_the_worker_leaves_no_recorder_behind_on_its_thread(monkeypatch):
    _parallel(monkeypatch)
    recorder = S.EntryQuoteMissRecorder(_typed_http((200, dict(THIN)), (200, RAY_BETTER)))
    recorder.quote(MINT, 45.0)
    leaked = S._RAY_PARALLEL_POOL[0].submit(
        lambda: (getattr(S._ENTRY_QUOTE_RECORDING, "base", None),
                 getattr(S._ENTRY_QUOTE_RECORDING, "calls", None))).result(2)
    assert leaked == (None, None)
