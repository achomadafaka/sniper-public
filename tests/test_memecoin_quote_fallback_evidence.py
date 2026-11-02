"""The Solana entry quote records WHICH Raydium-fallback branch fired, and Jupiter's own impact.

2026-09-24: the Raydium fallback was off on every funded Solana entry for 23 h by accident
(a861e755 restored it), and its thin-route branch could not be audited afterwards because the branch
decides on Jupiter's `priceImpactPct` and 0 of 840 entry traces carried it. `_quote_with_fallback`
now tags the returned quote; no call is added and routing is unchanged.
"""
import pytest

from sniper.execution import solana_real_seams as S
from sniper.settings import memecoin as cfg


@pytest.fixture(autouse=True)
def _fallback_on(monkeypatch):
    S._jup_host_cooldown.clear()
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_VENUE_FALLBACK", True)
    yield
    S._jup_host_cooldown.clear()


@pytest.fixture(params=[False, True], ids=["serial", "c2_parallel"])
def c2(request, monkeypatch):
    """MEMECOIN_ENTRY_RAYDIUM_PARALLEL off and on (C2, 2026-10-01): the decision and the tag are the
    same; with it on the tag also carries the timing fields."""
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RAYDIUM_PARALLEL", request.param, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RAYDIUM_NONBLOCKING", False, raising=False)  # the C2 blocking contract
    return request.param


def _strip_timing(tag, parallel):
    tag = dict(tag)
    timing = {k: tag.pop(k) for k in ("jupiter_done_ms", "raydium_done_ms", "raydium_parallel",
                                      "raydium_wait_ms") if k in tag}
    if parallel:
        assert timing["raydium_parallel"] is True
        assert timing["jupiter_done_ms"] >= 0 and timing["raydium_done_ms"] >= 0
        assert timing.get("raydium_wait_ms", 0) >= 0
    else:
        assert timing == {}
    return tag


def _eventually(calls, n, timeout_s=2.0):
    import time
    deadline = time.monotonic() + timeout_s
    while len(calls) < n and time.monotonic() < deadline:
        time.sleep(0.005)
    return len(calls)


def _jup(monkeypatch, quote, status=200):
    monkeypatch.setattr(S, "_jup_quote_status", lambda *a, **k: (quote, status))


def _ray(monkeypatch, quote):
    calls = []
    monkeypatch.setattr(S, "_ray_quote", lambda *a, **k: calls.append(a) or quote)
    return calls


def _q(**kw):
    return S._quote_with_fallback(S.USDC_MINT, "MINT", 1_000_000, 300, None, tag_evidence=True,
                                  **kw)


def test_an_exit_quote_is_never_tagged(monkeypatch):
    """Exits call the seam without the flag: an exit quote must reach the signer exactly as
    Jupiter answered it (tests/test_memecoin_solana_stop_quote.py pins that end to end)."""
    jup = {"outAmount": "100", "priceImpactPct": "0.20"}
    _jup(monkeypatch, jup)
    _ray(monkeypatch, {"outAmount": "50"})
    out = S._quote_with_fallback("MINT", S.USDC_MINT, 1_000_000, 300, None)
    assert out is jup and "_sniper_fallback" not in out


def test_the_entry_wrapper_is_the_one_that_tags(monkeypatch):
    _jup(monkeypatch, {"outAmount": "100", "priceImpactPct": "0.012"})
    _ray(monkeypatch, None)
    assert S.jupiter_entry_quote("MINT", 1.0)["_sniper_fallback"]["branch"] == "none"


def test_a_flat_route_is_tagged_none_and_raydium_is_not_asked(monkeypatch, c2):
    _jup(monkeypatch, {"outAmount": "100", "priceImpactPct": "0.012"})
    calls = _ray(monkeypatch, {"outAmount": "999"})
    out = _q()
    assert out["_sniper_fallback"] == {"branch": "none", "raydium": "not_asked",
                                       "jupiter_impact_pct": pytest.approx(0.012),
                                       "jupiter_route_labels": None,
                                       "jupiter_out_atomic": "100", "raydium_out_atomic": None}
    if c2:
        # C2 asks Raydium beside Jupiter on every eligible entry quote; a flat route drops it unread.
        assert _eventually(calls, 1) == 1
    else:
        assert calls == [], "the common case must cost zero extra requests"


def test_an_absent_impact_is_recorded_absent_never_zero(monkeypatch):
    """The decision substitutes 0.0 for a missing field; the RECORD must not, or a missing value
    reads as a measured flat route."""
    _jup(monkeypatch, {"outAmount": "100"})
    _ray(monkeypatch, None)
    assert _q()["_sniper_fallback"]["jupiter_impact_pct"] is None


@pytest.mark.parametrize("ray,winner,outcome,ray_out", [
    ({"outAmount": "110", "_venue": "raydium"}, "raydium", "won", "110"),
    # 5x Jupiter at 20% impact is a phantom (test_memecoin_raydium_phantom_guard.py)
    ({"outAmount": "500", "_venue": "raydium"}, "jupiter", "distrusted", "500"),
    ({"outAmount": "50", "_venue": "raydium"}, "jupiter", "lost", "50"),
    (None, "jupiter", "no_quote", None),
])
def test_a_thin_route_asks_raydium_and_records_the_outcome(monkeypatch, c2, ray, winner, outcome,
                                                            ray_out):
    """BOTH venues' outAmount, whichever won (SPAM 4Kz2Fw, 2026-09-26): Raydium won the thin-route
    contest, its build then failed simulation (router 0x26, slippage), and whether Jupiter's quote
    could have filled inside the caps was unanswerable because the losing quote's size was never
    recorded. Atomic units of the output mint, as exact decimal strings."""
    jup = {"outAmount": "100", "priceImpactPct": "0.20"}
    _jup(monkeypatch, jup)
    calls = _ray(monkeypatch, ray)
    out = _q()
    assert len(calls) == 1
    assert (out is ray) if winner == "raydium" else (out is jup)
    assert _strip_timing(out["_sniper_fallback"], c2) == {
        "branch": "thin", "raydium": outcome, "jupiter_impact_pct": pytest.approx(0.20),
        "jupiter_route_labels": None, "jupiter_out_atomic": "100", "raydium_out_atomic": ray_out}


def test_a_thin_route_the_fallback_may_not_serve_is_tagged_blocked(monkeypatch):
    _jup(monkeypatch, {"outAmount": "100", "priceImpactPct": "0.20"})
    calls = _ray(monkeypatch, {"outAmount": "500"})
    out = _q(exclude_dexes=("Raydium CLMM",))
    assert calls == []
    assert out["_sniper_fallback"]["branch"] == "thin_blocked"
    assert out["_sniper_fallback"]["raydium"] == "not_asked"


def test_a_jupiter_miss_rescued_by_raydium_is_tagged_miss(monkeypatch, c2):
    _jup(monkeypatch, None, status=400)
    ray = {"outAmount": "500", "_venue": "raydium"}
    _ray(monkeypatch, ray)
    out = _q()
    assert out is ray
    assert _strip_timing(out["_sniper_fallback"], c2) == {
        "branch": "miss", "raydium": "won", "jupiter_impact_pct": None, "jupiter_route_labels": None,
        "jupiter_out_atomic": None, "raydium_out_atomic": "500"}


@pytest.mark.parametrize("raw,recorded", [
    ("0", "0"),                      # a measured zero stays a zero
    (None, None), ("", None),        # absent stays absent
    ("1e3", None), ("abc", None),    # unparseable is unknown, never a guess
    ("3353299328750", "3353299328750"),
    (2**60, str(2**60)),             # exact past the float mantissa
])
def test_an_out_amount_is_recorded_exactly_or_not_at_all(monkeypatch, raw, recorded):
    jup = {"priceImpactPct": "0.012"}
    if raw is not None:
        jup["outAmount"] = raw
    _jup(monkeypatch, jup)
    _ray(monkeypatch, None)
    assert _q()["_sniper_fallback"]["jupiter_out_atomic"] == recorded


def test_a_total_miss_returns_none_and_carries_nothing(monkeypatch):
    _jup(monkeypatch, None, status=400)
    _ray(monkeypatch, None)
    assert _q() is None


def test_the_entry_trace_records_the_fallback_tag(monkeypatch):
    """The tag is only useful if it reaches the attempt's trace, which is what `trace_json` stores."""
    from sniper.execution import solana_swap_executor as ex
    from test_memecoin_entry_call_timings import _Trace
    from test_memecoin_entry_roundtrip_cap import _c, _Deps

    monkeypatch.setattr("sniper.settings.memecoin.entry_roundtrip_cap_pct", lambda *_: 10.0)
    conn, pid = _c()
    deps = _Deps(quote_impact=0.01, usd_back=9.9)
    inner = deps.quote_fn
    tag = {"branch": "thin", "raydium": "lost", "jupiter_impact_pct": 0.2}

    def quote_fn(*a, **k):
        quote = inner(*a, **k)
        quote["_sniper_fallback"] = dict(tag)
        return quote

    trace = _Trace()
    ex.execute_entry(
        conn, pending_id=pid, mint="MINT", usd_amount=10.0, strategy="alpha_b",
        source_user_id="u1", now_iso="2026-09-24T15:52:00Z",
        quote_fn=quote_fn, reverse_quote_fn=deps.reverse_quote_fn,
        sign_fn=deps.sign_fn, send_fn=deps.send_fn, confirm_fn=deps.confirm_fn,
        usdc_fn=deps.usdc_fn, token_fn=deps.token_fn, impact_cap_pct=15.0, trace=trace)
    conn.close()
    marks = [s for s in trace.substages if s["name"] == "quote_fallback"]
    assert len(marks) == 1, trace.substages
    assert {k: marks[0][k] for k in tag} == tag


def test_a_failed_sign_timing_is_logged_not_swallowed(monkeypatch, caplog):
    """The telemetry wrapper must neither interrupt the entry nor vanish (CLAUDE.md: never swallow
    silently). A DEBUG line with the traceback, on the exception path only."""
    import logging
    from test_memecoin_entry_call_timings import _run

    def boom():
        raise RuntimeError("timing accessor broke")

    with caplog.at_level(logging.DEBUG, logger="sniper.execution.solana_swap_executor"):
        result, _trace = _run(monkeypatch, sign_timing=boom)
    assert result["status"] == "filled"
    hits = [r for r in caplog.records if "sign-timing telemetry failed" in r.getMessage()]
    assert len(hits) == 1 and hits[0].exc_info is not None


@pytest.mark.parametrize("venue,builder", [
    ("raydium", "raydium_swap"), ("pump_native", "pump_native"), (None, "jupiter_swap")])
@pytest.mark.parametrize("built", [True, False])
@pytest.mark.usefixtures("fake_payload_clamp")
def test_the_build_timing_names_the_venue_that_built(monkeypatch, venue, builder, built):
    """SPAM 4Kz2Fw's trace read builder=jupiter_swap for a transaction Raydium built and Raydium's
    simulation refused. The label must name the venue, on the refused build as well."""
    from types import SimpleNamespace
    from sniper.execution import solana_swap_executor as ex

    # Synthetic payloads isolate builder telemetry, outside automatic admission.
    monkeypatch.setattr(ex.m, "pending_row", lambda *_: {
        "strategy": "alpha_b", "source_user_id": "u1", "signal_source": "manual"})
    monkeypatch.setattr(ex, "execute_entry", lambda *a, **k: k["sign_fn"])
    monkeypatch.setattr(S, "jupiter_swap_tx", lambda quote, *a, **k: "tx" if built else None)
    monkeypatch.setattr(S, "sign_swap_tx", lambda tx, key: b"signed")
    executor = S.build_real_solana_executor(
        keypair=SimpleNamespace(pubkey=lambda: "offline-owner"), rpc_url="offline://rpc")
    sign = executor["execute_fn"](None, 1, "MINT", 10, "2026-09-26T23:44:31Z")
    quote = {"outAmount": "1"}
    if venue:
        quote["_venue"] = venue
    assert (sign(quote) is not None) is built
    assert sign.sign_timing()["builder"] == builder


# --- 2026-10-02 (Alpha): Jupiter's own route, and the sell proof's second opinion ---------------------

def test_jupiters_own_route_is_kept_when_raydium_wins(monkeypatch):
    """A Raydium win swaps the route plan, so the losing Jupiter route was unreadable afterwards."""
    jup = {"outAmount": "100", "priceImpactPct": "0.20", "routePlan": [
        {"swapInfo": {"label": "BisonFi"}}, {"swapInfo": {"label": "Raydium CP"}}]}
    _jup(monkeypatch, jup)
    ray = {"outAmount": "110", "_venue": "raydium"}
    _ray(monkeypatch, ray)
    out = _q()
    assert out is ray and out["_sniper_fallback"]["jupiter_route_labels"] == ["BisonFi", "Raydium CP"]


def test_the_sell_proof_is_tagged_with_how_long_its_raydium_second_opinion_blocked(monkeypatch):
    """The entry's sell proof (evidence=True) is tagged like the entry, with raydium_wait_ms, and
    starts NO parallel call; a plain exit quote stays untagged."""
    import time
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RAYDIUM_PARALLEL", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RAYDIUM_NONBLOCKING", False, raising=False)  # the C2 blocking contract
    monkeypatch.setattr(S, "_jup_quote_status",          # a fresh answer per call, as Jupiter sends
                        lambda *a, **k: ({"outAmount": "100", "priceImpactPct": "0.20"}, 200))
    started = []
    monkeypatch.setattr(S, "_ray_parallel_start", lambda *a, **k: started.append(a))

    def slow_ray(*a, **k):
        time.sleep(0.05)
        return {"outAmount": "50"}
    monkeypatch.setattr(S, "_ray_quote", slow_ray)
    tag = S.jupiter_exit_quote("MINT", 1_000_000, 3.0, evidence=True)["_sniper_fallback"]
    assert (tag["branch"], tag["raydium"], tag["raydium_parallel"]) == ("thin", "lost", False)
    assert tag["raydium_wait_ms"] >= 40.0 and started == []
    assert "_sniper_fallback" not in S.jupiter_exit_quote("MINT", 1_000_000, 3.0)


def test_the_entry_recorders_sell_proof_asks_for_the_tag(monkeypatch):
    seen = {}

    def exit_quote(mint, amount, slippage, *, http=None, evidence=False, entry_proof=False):
        seen.update(evidence=evidence, entry_proof=entry_proof)
        return {"outAmount": "1"}
    monkeypatch.setattr(S, "jupiter_exit_quote", exit_quote)
    S.EntryQuoteMissRecorder(http=lambda *a, **k: None).reverse("MINT", 10, 3.0)
    assert seen == {"evidence": True, "entry_proof": True}


from entry_clamp_fake import fake_payload_clamp  # noqa: E402,F401
