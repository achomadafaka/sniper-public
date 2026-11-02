"""A same-attempt safety PASS must cover the reserved clip, regardless of scheduling."""
import threading
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_evm_leg as leg
from sniper.services.memecoin_evm_early_quote import EarlyEvmQuote
from sniper.settings import memecoin as cfg
from test_memecoin_evm_leg import _Ev, _c


def _run(monkeypatch, tmp_path, *, early_clip=142.11, early_mint="0xtok",
         early_verdict=(True, "early_ok"), expired=False, schedule="immediate",
         early_present=True, chain="56", early_safety=True, order=None):
    conn, kill = _c(monkeypatch, tmp_path)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", set(cfg.MEMECOIN_EVM_ALLOWLIST) | {chain})
    monkeypatch.setattr(leg._arming, "in_quiet_hours", lambda: False)
    marks = []
    trace = SimpleNamespace(mark=lambda *a, **kw: None,
                            mark_substage=lambda name, **kw: marks.append((name, kw)))
    early, early_queries = None, []
    if early_present:
        early = EarlyEvmQuote(
            mint=early_mint, trace=trace, sizing_fn=lambda: (early_clip, None),
            quote_fn=lambda *a: {}, safety_fn=(lambda *a: early_verdict) if early_safety else None)
        early.worker.join(1)
        assert not early.worker.is_alive()
        if early_safety:
            assert early.safety.done.wait(1)
        if expired:
            early.safety.done_at = early.safety.deadline + 1
        original_lookup = early.safety_for

        def lookup(mint, usd):
            early_queries.append((mint, usd))
            return original_lookup(mint, usd)
        early.safety_for = lookup

    fresh_calls, fresh_started = [], threading.Event()
    now = [100.0]
    monkeypatch.setattr(leg, "time", SimpleNamespace(monotonic=lambda: now[0],
                                                    perf_counter=lambda: now[0]))
    monkeypatch.setattr(leg, "_FUNDED_EVM_SAFETY_WAIT_S", 0.5)

    def fresh_safety(mint, *, usd, deadline):
        fresh_calls.append((mint, usd, deadline))
        if order is not None:
            order.append(("safety", usd))
        fresh_started.set()
        return False, "fresh_required"

    class ScheduledThread(threading.Thread):
        def start(self):
            if schedule == "immediate":
                super().start()
                self.join(1)
                assert not self.is_alive()

        def join(self, timeout=None):
            if schedule == "delayed" and not self._started.is_set():
                super().start()
            super().join(timeout)

    monkeypatch.setattr(leg, "threading", SimpleNamespace(Thread=ScheduledThread))

    def reserve(*args, **kwargs):
        if not early_present:
            assert fresh_started.is_set(), "ordinary safety must overlap the reservation"
        now[0] = 100.2
        return SimpleNamespace(allowed=True, approved_size_usdc=142.11), 1

    monkeypatch.setattr(leg, "reserve_entry_evm", reserve)

    def execute(conn, pid, mint, size, now_iso, safety_gate_fn=None, early_quote=None):
        assert size == 142.11
        if order is not None:
            order.append(("execute", size))
        safe, reason = safety_gate_fn()
        return {"status": "filled", "position_id": pid} if safe else {
            "status": "unsafe_gate", "reason": reason}

    event = _Ev()
    event.network_id = chain
    try:
        result = leg.evm_real_entry(
            conn, event, route=f"evm:{chain}", day="2026-10-12", usdc_balance=300,
            native_balance=1, now_iso="2026-10-12T17:40:27Z", trade_size=175,
            token_safety_fn=fresh_safety, execute_fn=execute, kill_path=kill,
            early_quote=early, trace=trace)
        safety_mark = dict([kw for name, kw in marks if name == "evm.token_safety"][-1],
                           early_queries=tuple(early_queries))
        return result, fresh_calls, safety_mark
    finally:
        conn.close()


@pytest.mark.parametrize("schedule", ["immediate", "delayed"])
def test_reserved_clip_reuses_its_early_pass_under_either_worker_schedule(monkeypatch, tmp_path, schedule):
    result, fresh_calls, mark = _run(monkeypatch, tmp_path, schedule=schedule)
    assert result["status"] == "filled"
    assert fresh_calls == []
    assert mark["early"] is True and mark["reason"] == "early_ok"
    assert mark["early_queries"] == (("0xtok", 142.11),)


@pytest.mark.parametrize("schedule", ["immediate", "delayed"])
def test_a_pass_for_the_preliminary_size_cannot_authorize_the_reserved_clip(monkeypatch, tmp_path, schedule):
    result, fresh_calls, mark = _run(monkeypatch, tmp_path, early_clip=175, schedule=schedule)
    assert result["status"] == "unsafe"
    assert fresh_calls == [("0xtok", 142.11, 100.5)]
    assert mark["early"] is False


@pytest.mark.parametrize("case", ["wrong_mint", "refused", "expired", "no_verdict"])
def test_unusable_early_evidence_runs_fresh_with_the_original_deadline(monkeypatch, tmp_path, case):
    result, fresh_calls, mark = _run(
        monkeypatch, tmp_path, early_mint="other" if case == "wrong_mint" else "0xtok",
        early_verdict=(False, "refused") if case == "refused" else None if case == "no_verdict" else (True, "early_ok"),
        expired=case == "expired")
    assert result["status"] == "unsafe"
    assert fresh_calls == [("0xtok", 142.11, 100.5)]
    assert mark["early"] is False


def test_bsc_without_an_early_candidate_still_starts_safety_before_reservation(monkeypatch, tmp_path):
    result, fresh_calls, _mark = _run(monkeypatch, tmp_path, early_present=False)
    assert result["status"] == "unsafe"
    assert fresh_calls == [("0xtok", 175, 100.5), ("0xtok", 142.11, 100.5)]


def test_other_chains_start_before_reservation_and_recheck_the_approved_clip(monkeypatch, tmp_path):
    result, fresh_calls, mark = _run(monkeypatch, tmp_path, chain="8453")
    assert result["status"] == "filled"
    assert fresh_calls == [("0xtok", 175, 100.5)]
    assert mark["early_queries"] == (("0xtok", 175), ("0xtok", 142.11))
    assert mark["early"] is True and mark["reason"] == "early_ok"
