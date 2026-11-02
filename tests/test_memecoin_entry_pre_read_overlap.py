"""The executor's pre-balance reads run while the quote is fetched (2026-09-27, Alpha GO "L2").

MEASURED BEFORE: timing_balance_reads p50 112.7 / p90 138.7 ms (n=8 first general-path Solana
attempts, 2026-09-27T12:27:51Z..15:24:39Z). The reads started only after every price gate, so the
entry paid them on top of the forward quote, the reverse quote and the round-trip check. Modelled
saving if they start with the quote: p50 109.8 / p90 133.7 ms.

The reads stay LIVE. They are the fill-delta baseline (spent = usdc_pre - usdc_post), so they must
start inside serialize_executor's wallet lock, where no actor of ours can move the wallet between
the baseline and our fill. execute_entry runs inside that lock, so the reads start at its top.

Pinned here: the overlap itself, the wait stamp, an abort that never waits on the reads, the chase
gate pricing with the JOINED decimals, the lock, and the rollback flag.
"""
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from sniper.execution import solana_swap_executor as ex
from sniper.persistence import db_memecoin as m
from test_memecoin_entry_call_timings import _Trace, _named
from test_memecoin_entry_roundtrip_cap import _Deps


def _c(source_price=None):
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.reserve_pending(c, mint="MINT", source_user_id="u1", strategy="alpha_b",
                            notional=10.0, reserved_at="t", source_price=source_price)
    return c, pid


def _slow(fn, seconds, *, pre_only=True, deps=None):
    """Sleep before a PRE read only; the post-send reads stay instant."""
    def call(*args):
        if not pre_only or deps is None or "sent" not in deps.calls:
            time.sleep(seconds)
        return fn(*args)
    return call


def _entry(monkeypatch, deps, *, quote_fn=None, usdc_fn=None, token_fn=None, sign_fn=None,
           conn_pid=None, chase_max_pct=None, flag=True):
    monkeypatch.setattr("sniper.settings.memecoin.entry_roundtrip_cap_pct", lambda *_: 10.0)
    monkeypatch.setattr("sniper.settings.memecoin.MEMECOIN_ENTRY_OVERLAP_PRE_READS", flag,
                        raising=False)
    conn, pid = conn_pid or _c()
    trace = _Trace()
    try:
        result = ex.execute_entry(
            conn, pending_id=pid, mint="MINT", usd_amount=10.0, strategy="alpha_b",
            source_user_id="u1", now_iso="2026-09-27T15:40:00Z",
            quote_fn=quote_fn or deps.quote_fn, reverse_quote_fn=deps.reverse_quote_fn,
            sign_fn=sign_fn or deps.sign_fn, send_fn=deps.send_fn, confirm_fn=deps.confirm_fn,
            usdc_fn=usdc_fn or deps.usdc_fn, token_fn=token_fn or deps.token_fn,
            impact_cap_pct=15.0, chase_max_pct=chase_max_pct, trace=trace)
    finally:
        conn.close()
    return result, trace


def test_the_pre_reads_run_while_the_quote_is_fetched(monkeypatch):
    """Quote 0.30 s, each read 0.25 s. Serial-after-gates reaches the signature at >= 0.55 s;
    overlapped it reaches it at ~0.30 s. Fails on the pre-overlap executor."""
    deps = _Deps(quote_impact=0.01, usd_back=9.9)
    started, signed_at = [], []

    def quote(mint, usd):
        started.append(time.monotonic())
        time.sleep(0.30)
        return deps.quote_fn(mint, usd)

    def sign(quote_):
        signed_at.append(time.monotonic())
        return deps.sign_fn(quote_)

    result, trace = _entry(monkeypatch, deps, quote_fn=quote, sign_fn=sign,
                           usdc_fn=_slow(deps.usdc_fn, 0.25, deps=deps),
                           token_fn=_slow(deps.token_fn, 0.25, deps=deps))
    assert result["status"] == "filled"
    assert signed_at[0] - started[0] < 0.45, "the pre-reads were paid after the quote"
    wait = _named(trace, "timing_balance_wait")
    assert wait and wait[0]["duration_ms"] < 100.0, wait
    # The old marks keep their names; the combined wall is still the reads' own duration.
    reads = _named(trace, "timing_balance_reads")
    assert reads and reads[0]["duration_ms"] >= 240.0, reads
    assert _named(trace, "timing_usdc_read") and _named(trace, "timing_token_read")


def test_the_wait_stamp_carries_the_time_the_entry_blocked_on_the_reads(monkeypatch):
    """Reads 0.30 s against a near-instant quote: the entry waits ~0.3 s at the join."""
    deps = _Deps(quote_impact=0.01, usd_back=9.9)
    result, trace = _entry(monkeypatch, deps,
                           usdc_fn=_slow(deps.usdc_fn, 0.30, deps=deps),
                           token_fn=_slow(deps.token_fn, 0.30, deps=deps))
    assert result["status"] == "filled"
    wait = _named(trace, "timing_balance_wait")
    assert wait and 150.0 <= wait[0]["duration_ms"] <= 1000.0, wait


def test_an_abort_before_the_join_leaves_no_waiter(monkeypatch):
    """No quote: the entry returns at once. The two read threads are daemons, the only two this
    attempt started, and a read that raises later never reaches the caller."""
    deps = _Deps()
    gate = threading.Event()
    raised = []

    def blocked_usdc():
        gate.wait(5.0)
        raised.append("usdc")
        raise RuntimeError("late usdc read failure")

    def blocked_token(mint):
        gate.wait(5.0)
        return 0.0

    before = {t.ident for t in threading.enumerate()}
    t0 = time.monotonic()
    result, trace = _entry(monkeypatch, deps, quote_fn=lambda mint, usd: None,
                           usdc_fn=blocked_usdc, token_fn=blocked_token)
    elapsed = time.monotonic() - t0
    started = [t for t in threading.enumerate() if t.ident not in before]
    try:
        assert result["status"] == "aborted" and result["reason"] == "no quote"
        assert elapsed < 0.5, "the abort waited on the pre-reads"
        assert len(started) == 2, [t.name for t in started]
        assert all(t.daemon for t in started), [t.name for t in started]
        assert not _named(trace, "timing_balance_wait"), "an abort never joins the reads"
    finally:
        gate.set()
        for t in started:
            t.join(2.0)
    assert raised == ["usdc"] and not any(t.is_alive() for t in started)


class _Snap:
    def __init__(self, human, decimals):
        self.human = human
        self.decimals = decimals
        self.atomic = int(round(human * 10 ** decimals))


def test_the_chase_gate_prices_with_the_joined_decimals_never_a_default(monkeypatch):
    """The token read (slower than the quote) carries decimals 9. The chase gate's quote_price must
    use them: 10 USDC for 5e-7 tokens = $2e7. Priced before the join it would be unpriceable
    (chase_quote_skipped), and priced on a default 6 it would read $2e4."""
    deps = _Deps(quote_impact=0.01, usd_back=9.9)

    def quote(mint, usd):
        return dict(deps.quote_fn(mint, usd), inAmount="10000000")

    def token(mint):
        time.sleep(0.20)
        return _Snap(500e-9 if "sent" in deps.calls else 0.0, 9)

    result, trace = _entry(monkeypatch, deps, quote_fn=quote, token_fn=token,
                           conn_pid=_c(source_price=2e7), chase_max_pct=30.0)
    assert result["status"] == "filled", result
    assert not _named(trace, "chase_quote_skipped")
    chase = _named(trace, "chase_quote_confirmation")
    assert chase and chase[0]["quote_price"] == pytest.approx(2e7), chase


def test_the_pre_reads_start_only_inside_the_wallet_lock(monkeypatch, tmp_path):
    """Each read probes the wallet lock without blocking. The probe first proves it can see a free
    lock (negative control), then every read inside serialize_executor must find it HELD."""
    from sniper.execution.exec_lock import serialize_executor, wallet_exec_lock
    key, lock_dir = "sol-test-wallet", str(tmp_path)

    def lock_free():
        with wallet_exec_lock(key, lock_dir=lock_dir, blocking=False) as got:
            return got

    assert lock_free() is True, "the probe cannot see a free lock, so it proves nothing"
    deps = _Deps(quote_impact=0.01, usd_back=9.9)
    seen = []

    def usdc():
        seen.append(("usdc", lock_free()))
        return deps.usdc_fn()

    def token(mint):
        seen.append(("token", lock_free()))
        return deps.token_fn(mint)

    def execute_fn(conn, pid, mint, usd, now_iso):
        return ex.execute_entry(
            conn, pending_id=pid, mint=mint, usd_amount=usd, strategy="alpha_b",
            source_user_id="u1", now_iso=now_iso, quote_fn=deps.quote_fn,
            reverse_quote_fn=deps.reverse_quote_fn, sign_fn=deps.sign_fn, send_fn=deps.send_fn,
            confirm_fn=deps.confirm_fn, usdc_fn=usdc, token_fn=token, impact_cap_pct=15.0)

    monkeypatch.setattr("sniper.settings.memecoin.entry_roundtrip_cap_pct", lambda *_: 10.0)
    monkeypatch.setattr("sniper.settings.memecoin.MEMECOIN_ENTRY_OVERLAP_PRE_READS", True,
                        raising=False)
    conn, pid = _c()
    try:
        result = serialize_executor(execute_fn, key, lock_dir=lock_dir)(
            conn, pid, "MINT", 10.0, "2026-09-27T15:40:00Z")
    finally:
        conn.close()
    assert result["status"] == "filled"
    assert seen and all(held is False for _name, held in seen), seen


def test_the_rollback_flag_keeps_the_reads_after_the_gates(monkeypatch):
    """MEMECOIN_ENTRY_OVERLAP_PRE_READS=0: a no-quote abort never touches the wallet."""
    deps = _Deps()
    calls = []
    result, trace = _entry(monkeypatch, deps, quote_fn=lambda mint, usd: None, flag=False,
                           usdc_fn=lambda: calls.append("usdc") or 100.0,
                           token_fn=lambda mint: calls.append("token") or 0.0)
    assert result["status"] == "aborted"
    assert calls == []


def test_the_solana_gate_read_is_timed_in_the_reservation(monkeypatch):
    """L1 step 0, measurement only: the gate's usdc || sol read gets its own duration mark."""
    import sniper.services.memecoin_real_runner as runner
    from test_memecoin_entry_identity import (
        FakeClock, NOW, _arm, _conn, _event, _solana_context)
    _arm(monkeypatch)
    conn = _conn()
    context = _solana_context({})
    context["usdc_fn"] = _slow(context["usdc_fn"], 0.05)
    marks = []
    trace = SimpleNamespace(
        attempt_id="attempt-l1",
        mark=lambda *a, **k: True,
        mark_substage=lambda name, **kw: marks.append({"name": name, **kw}) or True,
        finish=lambda *a, **k: True,
    )
    monkeypatch.setattr(runner, "real_entry", lambda *a, **k: {"status": "rejected",
                                                                "reason": "stub"})
    monkeypatch.setattr(runner, "_alert_entry_result", lambda *a, **k: None)
    runner.real_entry_hook(conn, _event(network_id="solana", mint="So1Mint"),
                           {"solana": context}, now_fn=lambda: NOW, trade_size=10.0,
                           max_age_s=120, trace=trace, trace_clock=FakeClock())
    gate = [x for x in marks if x["name"] == "timing_gate_balance_read"]
    assert gate and gate[0]["duration_ms"] >= 45.0, marks
