"""The 405 ms between the quote and the signature has to be attributable to a call.

MEASURED 2026-09-24, 277 solana funded fills over 14 days: quote_completed_at ->
signing_completed_at is p50 405 ms / p90 915 ms / max 5,131 ms, and `signing_ms` on the same rows
is p50 405 / p90 915. The two agree, so `signing_ms` is not a sub-measure of that window -- it IS
the window. Everything inside it was unattributed, because the three substages that fire there are
decision marks and not timers: roundtrip_cap p50 0.05 ms, post_peak_confirmation 0.03 ms,
chase_quote_confirmation 0.02 ms. No amount of re-reading the traces could split the 405 ms, which
is why this is an instrument and not an analysis.

WHAT RUNS THERE. Network: the reverse quote (a second Jupiter call, for the round-trip cap), the
USDC balance read, the token supply read, and the swap BUILD (a third Jupiter call, POST /swap).
CPU: the local signature. Conditional: the sequential round-trip measurement, and the oversize
rebuild above Solana's 1,232-byte packet limit.

TIMING ONLY. No new network call, no behaviour change, no shared state -- the build/sign split
comes back off the seam through a THREAD-LOCAL that is popped when read, because the funded runner
is threaded and one seam serves every concurrent entry.
"""
import pytest

from sniper.execution import solana_swap_executor as ex
from test_memecoin_entry_roundtrip_cap import _c, _Deps


class _Trace:
    """Records substage marks the way the real trace does, and nothing else."""

    def __init__(self):
        self.substages = []

    def mark_substage(self, name, *, duration_ms=None, **metadata):
        self.substages.append({"name": name, "duration_ms": duration_ms, **metadata})
        return True

    def mark_submission(self, stage, attempt_no, **metadata):
        return True

    def stage_completed_at(self, stage):
        return None


def _run(monkeypatch, *, sign_timing=None, measure=None):
    monkeypatch.setattr("sniper.settings.memecoin.entry_roundtrip_cap_pct", lambda *_: 10.0)
    conn, pid = _c()
    deps = _Deps(quote_impact=0.01, usd_back=9.9)
    trace = _Trace()
    inner = deps.sign_fn

    def sign_fn(quote):                  # a plain function: the seam's real one carries attributes
        return inner(quote)

    if sign_timing is not None:
        # POPPED, like the seam's own: a split read twice is the second reader stealing the first's.
        box = [sign_timing]
        sign_fn.sign_timing = sign_timing if callable(sign_timing) else lambda: box.pop() if box else None
    result = ex.execute_entry(
        conn, pending_id=pid, mint="MINT", usd_amount=10.0, strategy="alpha_b",
        source_user_id="u1", now_iso="2026-09-24T15:52:00Z",
        quote_fn=deps.quote_fn, reverse_quote_fn=deps.reverse_quote_fn,
        sign_fn=sign_fn, send_fn=deps.send_fn, confirm_fn=deps.confirm_fn,
        usdc_fn=deps.usdc_fn, token_fn=deps.token_fn, impact_cap_pct=15.0,
        roundtrip_measure_fn=measure, trace=trace)
    conn.close()
    return result, trace


def _named(trace, name):
    return [s for s in trace.substages if s["name"] == name]


def test_a_filled_entry_times_every_call_between_the_quote_and_the_signature(monkeypatch):
    result, trace = _run(monkeypatch, sign_timing={
        "builder": "jupiter_swap", "build_ms": 210.5, "sign_ms": 1.25, "combined_ms": None})
    assert result["status"] == "filled"
    for name in ("timing_reverse_quote", "timing_usdc_read", "timing_token_read",
                 "timing_swap_build", "timing_local_sign"):
        marks = _named(trace, name)
        assert marks, "%s was never recorded; that call is still unattributed" % name
        assert marks[0]["duration_ms"] is not None and marks[0]["duration_ms"] >= 0.0, name
    assert _named(trace, "timing_swap_build")[0]["duration_ms"] == pytest.approx(210.5)
    assert _named(trace, "timing_local_sign")[0]["duration_ms"] == pytest.approx(1.25)
    # The one number that is NOT reported on this path, because there is no boundary to report.
    assert not _named(trace, "timing_sign_combined")


def test_the_fast_builder_reports_one_number_and_names_itself(monkeypatch):
    """Splitting a builder that constructs AND signs behind one call would invent a boundary."""
    _, trace = _run(monkeypatch, sign_timing={
        "builder": "fast_tx_builder", "build_ms": None, "sign_ms": None, "combined_ms": 88.0})
    combined = _named(trace, "timing_sign_combined")
    assert combined and combined[0]["duration_ms"] == pytest.approx(88.0)
    assert combined[0]["builder"] == "fast_tx_builder"
    assert not _named(trace, "timing_swap_build") and not _named(trace, "timing_local_sign")


def test_a_seam_without_the_split_still_fills_and_simply_records_nothing(monkeypatch):
    """The instrument may not become a dependency of the money path."""
    result, trace = _run(monkeypatch, sign_timing=None)
    assert result["status"] == "filled"
    assert not _named(trace, "timing_swap_build")
    assert _named(trace, "timing_usdc_read"), "the calls this side of the seam are still timed"


def test_a_broken_timing_accessor_cannot_interrupt_a_signed_entry(monkeypatch):
    def broken():
        raise RuntimeError("telemetry failed")

    result, trace = _run(monkeypatch, sign_timing=broken)
    assert result["status"] == "filled"
    assert not _named(trace, "timing_swap_build")


def test_malformed_timing_cannot_interrupt_a_signed_entry(monkeypatch):
    result, trace = _run(monkeypatch, sign_timing={"build_ms": "unavailable"})
    assert result["status"] == "filled"
    assert not _named(trace, "timing_swap_build")


def test_the_conditional_measurement_is_timed_when_it_runs(monkeypatch):
    def measure(buy, sell):
        return {"version": "solana_sequential_roundtrip_v1", "status": "measured",
                "buy_spent_atomic": "10000000", "sell_received_atomic": "9400000",
                "cost_pct": -999}

    monkeypatch.setattr("sniper.settings.memecoin.entry_roundtrip_cap_pct", lambda *_: 10.0)
    conn, pid = _c()
    deps = _Deps(quote_impact=0.01, usd_back=8.7)     # 13% independent: over the 10% cap, so it runs
    trace = _Trace()
    ex.execute_entry(
        conn, pending_id=pid, mint="MINT", usd_amount=10.0, strategy="alpha_b",
        source_user_id="u1", now_iso="2026-09-24T15:52:00Z",
        quote_fn=deps.quote_fn, reverse_quote_fn=deps.reverse_quote_fn,
        sign_fn=deps.sign_fn, send_fn=deps.send_fn, confirm_fn=deps.confirm_fn,
        usdc_fn=deps.usdc_fn, token_fn=deps.token_fn, impact_cap_pct=15.0,
        roundtrip_measure_fn=measure, trace=trace)
    conn.close()
    assert _named(trace, "timing_roundtrip_measure"), "the simulation ran and was not timed"


def test_a_call_that_raises_is_still_timed():
    """A call that failed is still a call that spent time; dropping it biases the number."""
    trace = _Trace()

    def boom():
        raise RuntimeError("provider")

    with pytest.raises(RuntimeError):
        ex._timed(trace, "timing_usdc_read", boom)
    assert _named(trace, "timing_usdc_read")[0]["duration_ms"] >= 0.0


def test_the_split_is_popped_so_a_stale_one_cannot_be_reattributed():
    """Per thread, and consumed: a build that never ran must not inherit the last coin's number."""
    from sniper.execution import solana_real_seams as seams

    seams._SIGN_TIMING.split = {"builder": "jupiter_swap", "build_ms": 1.0, "sign_ms": 2.0}
    assert seams.take_sign_timing()["build_ms"] == 1.0
    assert seams.take_sign_timing() is None, "read twice means the second read was someone else's"


@pytest.mark.usefixtures("fake_payload_clamp")
def test_real_sign_seam_keeps_concurrent_timings_separate_and_clears_failed_attempt(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, local
    from types import SimpleNamespace
    from sniper.execution import solana_real_seams as seams

    # This isolates CPU timing with synthetic unsigned payloads, outside automatic admission.
    monkeypatch.setattr(ex.m, "pending_row", lambda *_: {
        "strategy": "alpha_b", "source_user_id": "u1", "signal_source": "manual"})
    monkeypatch.setattr(ex, "execute_entry", lambda *a, **k: k["sign_fn"])
    executor = seams.build_real_solana_executor(
        keypair=SimpleNamespace(pubkey=lambda: "offline-owner"), rpc_url="offline://rpc")
    sign = executor["execute_fn"](None, 1, "MINT", 10, "2026-09-24T00:00:00Z")
    clock = local()

    def monotonic():
        clock.tick += clock.step
        return clock.tick

    def build(quote, *a, **k):
        if quote.get("fail"):
            raise RuntimeError("build failed")
        return quote

    monkeypatch.setattr(seams, "_monotonic", monotonic)
    monkeypatch.setattr(seams, "jupiter_swap_tx", build)
    monkeypatch.setattr(seams, "sign_swap_tx", lambda quote, key: (b"offline", quote["id"]))
    ready = Barrier(2)

    def run(step):
        clock.tick, clock.step = 0.0, step
        assert sign({"id": str(step)}) == (b"offline", str(step))
        ready.wait(timeout=5)
        split = sign.sign_timing()
        assert sign.sign_timing() is None
        return split

    with ThreadPoolExecutor(max_workers=2) as pool:
        splits = list(pool.map(run, (0.001, 0.002)))
    assert [s["build_ms"] for s in splits] == pytest.approx([1.0, 2.0])
    assert [s["sign_ms"] for s in splits] == pytest.approx([1.0, 2.0])
    clock.tick, clock.step = 0.0, 0.001
    seams._SIGN_TIMING.split = {"build_ms": 999.0}
    with pytest.raises(RuntimeError, match="build failed"):
        sign({"fail": True})
    assert sign.sign_timing() is None


def test_failed_trace_write_preserves_the_original_provider_exception():
    class BrokenTrace:
        def mark_substage(self, *a, **k):
            raise RuntimeError("telemetry failed")

    error = OSError("provider failed")

    def provider():
        raise error

    with pytest.raises(OSError) as caught:
        ex._timed(BrokenTrace(), "timing_usdc_read", provider)
    assert caught.value is error


# --- the pre-balance reads run at the same time (2026-09-27, Alpha GO; audit
# docs/audits/2026-09-27-lemon-dougherty-s7.md). Measured before: usdc p50 66 ms, token p50 179 ms,
# serial sum p50 244 / p90 318 ms over 93 first attempts.

def _run_with(monkeypatch, *, usdc_fn=None, token_fn=None):
    monkeypatch.setattr("sniper.settings.memecoin.entry_roundtrip_cap_pct", lambda *_: 10.0)
    conn, pid = _c()
    deps = _Deps(quote_impact=0.01, usd_back=9.9)
    trace = _Trace()
    try:
        result = ex.execute_entry(
            conn, pending_id=pid, mint="MINT", usd_amount=10.0, strategy="alpha_b",
            source_user_id="u1", now_iso="2026-09-24T15:52:00Z",
            quote_fn=deps.quote_fn, reverse_quote_fn=deps.reverse_quote_fn,
            sign_fn=deps.sign_fn, send_fn=deps.send_fn, confirm_fn=deps.confirm_fn,
            usdc_fn=usdc_fn(deps) if usdc_fn else deps.usdc_fn,
            token_fn=(token_fn(deps) if token_fn else deps.token_fn),
            impact_cap_pct=15.0, trace=trace)
    finally:
        conn.close()
    return result, trace


def test_the_two_pre_balance_reads_run_at_the_same_time(monkeypatch):
    """Each PRE read waits for the other at a barrier: a serial executor breaks it and the entry
    raises. The post-send reads are left alone."""
    import threading
    barrier = threading.Barrier(2, timeout=2.0)
    pre = {"usdc": True, "token": True}

    def usdc(deps):
        def read():
            if pre.pop("usdc", False):
                barrier.wait()
            return deps.usdc_fn()
        return read

    def token(deps):
        def read(mint):
            if pre.pop("token", False):
                barrier.wait()
            return deps.token_fn(mint)
        return read

    result, trace = _run_with(monkeypatch, usdc_fn=usdc, token_fn=token)
    assert result["status"] == "filled"
    for name in ("timing_usdc_read", "timing_token_read", "timing_balance_reads"):
        assert _named(trace, name) and _named(trace, name)[0]["duration_ms"] >= 0.0, name
    # Recorded once each, from the caller's thread, in the old order.
    names = [s["name"] for s in trace.substages]
    assert names.index("timing_usdc_read") < names.index("timing_token_read") \
        < names.index("timing_balance_reads")


def test_a_failed_pre_balance_read_still_raises_on_the_caller(monkeypatch):
    def usdc(deps):
        def read():
            raise RuntimeError("usdc read failed")
        return read

    with pytest.raises(RuntimeError, match="usdc read failed"):
        _run_with(monkeypatch, usdc_fn=usdc)


from entry_clamp_fake import fake_payload_clamp  # noqa: E402,F401
