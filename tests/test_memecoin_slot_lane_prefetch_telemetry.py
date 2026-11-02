"""Slot lane prefetch telemetry (spec 2026-10-01 slot lane prefetch cadence, step 1).

Every failed input is logged with its name, reason and duration. Each worker pass is timed and
each input's peak age is kept across independent essential and extra refreshes. A 10-minute report
line carries all of it. Snapshot freshness and paired-balance publication retain their rules.
"""
from sniper.execution.solana_wallet import NativeSolBalanceRead
from sniper.services import memecoin_slot_lane as lane


class Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t


def _pre(clock, lines, **fns):
    base = dict(usdc_fn=lambda: 400.0, sol_fn=lambda: 0.5, blockhash_fn=lambda: ("H", 9),
                sol_usd_fn=lambda: 150.0)
    base.update(fns)
    return lane.LanePrefetcher(clock=clock, log=lines.append, **base)


def _slow(clock, seconds, value):
    def fn(*_a):
        clock.t += seconds
        return value
    return fn


def test_every_failed_input_is_logged_with_its_reason_and_duration():
    clock, lines = Clock(), []

    def blockhash():
        clock.t += 1.5
        raise RuntimeError("down")
    pre = _pre(clock, lines, usdc_fn=_slow(clock, 0.2, None),
               sol_fn=lambda: NativeSolBalanceRead(False, None, "json_rpc_error"),
               blockhash_fn=blockhash, sol_usd_fn=lambda: 0.0)
    pre.refresh_once()
    failed = sorted(l for l in lines if "prefetch read failed" in l)
    assert failed == [
        "[memecoin] slot lane prefetch read failed name=blockhash ms=1500.0 reason=RuntimeError",
        "[memecoin] slot lane prefetch read failed name=sol ms=0.0 reason=json_rpc_error",
        "[memecoin] slot lane prefetch read failed name=sol_usd ms=0.0 reason=invalid",
        "[memecoin] slot lane prefetch read failed name=usdc ms=200.0 reason=none",
    ]
    snap = pre.snapshot()                       # the snapshot rules are unchanged
    assert snap.usdc_balance is None and snap.observed_mono == {}


def test_a_good_pass_logs_nothing():
    clock, lines = Clock(), []
    _pre(clock, lines).refresh_once()
    assert lines == []


def test_peak_age_is_the_gap_between_two_good_refreshes():
    clock, lines = Clock(), []
    state = {"ok": True}
    pre = _pre(clock, lines, usdc_fn=lambda: 400.0 if state["ok"] else None)
    pre.refresh_once()                          # t=1000: all good
    state["ok"] = False
    for _ in range(3):                          # balances fail at 1003, 1006, 1009
        clock.t += 3.0
        pre.refresh_once()
    state["ok"] = True
    clock.t += 3.0                              # good again at 1012
    pre.refresh_once()
    report = pre.take_report(force=True)
    assert report["age_max_s"]["balances"] == 12.0
    assert report["age_max_s"]["blockhash"] == 3.0
    assert report["failed"]["usdc"] == 3


def test_an_input_still_failing_reports_its_current_age():
    clock, lines = Clock(), []
    state = {"ok": True}
    pre = _pre(clock, lines, blockhash_fn=lambda: ("H", 9) if state["ok"] else None)
    pre.refresh_once()
    state["ok"] = False
    clock.t += 25.0
    pre.refresh_once()
    assert pre.take_report(force=True)["age_max_s"]["blockhash"] == 25.0


def test_the_loop_timer_covers_the_roster_the_reads_and_the_jupiter_work():
    clock, lines = Clock(), []
    pre = _pre(clock, lines, sol_usd_fn=_slow(clock, 0.4, 150.0),
               usdc_fn=_slow(clock, 0.1, 400.0), extra_refresh_fn=_slow(clock, 1.8, None))
    pre.loop_once(pre_fn=_slow(clock, 0.3, None))
    report = pre.take_report(force=True)
    assert report["loops"] == 1
    assert report["work_ms"]["max"] == 2600.0
    assert report["phase_max_ms"] == {"roster": 300.0, "sol_usd": 400.0, "usdc": 100.0, "sol": 0.0,
                                      "blockhash": 0.0, "extra": 1800.0}
    slow = [l for l in lines if "prefetch slow loop" in l]
    assert slow == ["[memecoin] slot lane prefetch slow loop work_ms=2600 roster=300 sol_usd=400 "
                    "usdc=100 sol=0 blockhash=0 extra=1800"]


def test_the_report_is_emitted_once_per_window_and_then_resets():
    clock, lines = Clock(), []
    pre = _pre(clock, lines)
    for _ in range(5):
        pre.loop_once()
        clock.t += 1.0
    assert pre.take_report() is None            # the window has not elapsed
    clock.t += lane.PREFETCH_REPORT_S
    report = pre.take_report()
    assert report["loops"] == 5 and report["work_ms"]["p50"] == 0.0
    assert pre.take_report(force=True)["loops"] == 0
    line = lane.prefetch_report_line(report)
    assert line.startswith("[memecoin] slot lane prefetch loops n=5 work_ms p50=0 p90=0 p99=0 max=0 ")
    assert "age_max_s balances=" in line and "failed usdc=0 sol=0 blockhash=0 sol_usd=0" in line


def test_a_broken_log_never_breaks_the_refresh():
    clock = Clock()

    def boom(_line):
        raise RuntimeError("log down")
    pre = lane.LanePrefetcher(usdc_fn=lambda: None, sol_fn=lambda: 0.5,
                              blockhash_fn=lambda: ("H", 9), sol_usd_fn=lambda: 150.0,
                              extra_refresh_fn=_slow(clock, 2.5, None), clock=clock, log=boom)
    pre.loop_once()                             # a failed read AND a slow loop both try to log
    snap = pre.snapshot()
    assert snap.blockhash == "H" and snap.sol_usd == 150.0
    assert pre.take_report(force=True)["loops"] == 1


def test_a_failed_read_retries_on_the_next_loop_not_the_next_period():
    """Gamma L1: step 2's margin arithmetic rests on this rule."""
    clock, lines = Clock(), []
    calls = {"usdc": 0, "blockhash": 0}
    state = {"ok": False}

    def usdc():
        calls["usdc"] += 1
        return 400.0 if state["ok"] else None

    def blockhash():
        calls["blockhash"] += 1
        return ("H", 9) if state["ok"] else None
    pre = _pre(clock, lines, usdc_fn=usdc, blockhash_fn=blockhash)
    pre.refresh_once()                          # t=1000: both fail
    clock.t += 0.5
    pre.refresh_once()                          # t=1000.5: inside the 2 s period, retried anyway
    assert calls == {"usdc": 2, "blockhash": 2}
    state["ok"] = True
    clock.t += 0.5
    pre.refresh_once()                          # t=1001: good
    clock.t += 0.5
    pre.refresh_once()                          # t=1001.5: inside the period after a success: no read
    assert calls == {"usdc": 3, "blockhash": 3}


def test_a_phase_that_did_not_run_reports_zero_in_the_slow_line():
    clock, lines = Clock(), []
    pre = _pre(clock, lines, usdc_fn=_slow(clock, 2.2, 400.0), extra_refresh_fn=_slow(clock, 2.1, None))
    pre.loop_once()                             # the balances ran (slow)
    clock.t = 1000.5                            # back inside every period: only the Jupiter leg runs
    pre.loop_once()
    slow = [l for l in lines if "prefetch slow loop" in l]
    assert len(slow) == 2
    assert " usdc=0 " in slow[1] and slow[1].endswith("extra=2100")


def test_an_input_never_read_reports_never_not_fresh():
    clock, lines = Clock(), []
    pre = _pre(clock, lines, blockhash_fn=lambda: None)   # a start during an outage
    pre.loop_once()
    report = pre.take_report(force=True)
    assert report["age_max_s"]["blockhash"] is None
    assert "blockhash=never" in lane.prefetch_report_line(report)


def test_the_slowest_extra_pass_is_broken_down_by_call():
    """b0f346d1 (2026-10-06): the extra phase's p99 stayed 2.3-3.0 s and the report could not say
    which call. A leg's quote, swap-instructions and lookup-table reads, the leg plan and each
    venue's refresh are timed on the prefetch thread, and the slowest pass is reported."""
    import threading
    from sniper.execution import pump_curve_buy as pcb
    clock, lines = Clock(), []

    def jup(method, path, body=None):
        clock.t += 1.2 if method == "GET" else 0.3
        if method == "GET":
            return 200, {"otherAmountThreshold": "9", "outAmount": "10", "inAmount": "1"}
        return 200, {"swapInstruction": {"programId": "11111111111111111111111111111111",
                                         "accounts": [], "data": ""}, "addressLookupTableAddresses": []}

    def extra(_snapshot):
        with pcb.prefetch_timed("plan"):
            clock.t += 0.05
        with pcb.prefetch_timed("venue:pump_amm"):
            pcb.prefetch_usdc_sol_leg(1_000_000, 50, user="11111111111111111111111111111111",
                                      jup_call=jup, rpc=None)
    pre = _pre(clock, lines, extra_refresh_fn=extra)
    pre.loop_once()
    seen = []                                    # another thread is never timed by this pass
    threading.Thread(target=lambda: seen.append(getattr(pcb.PREFETCH_TIMINGS, "sink", None))).start()
    report = pre.take_report(force=True)
    worst = report["extra_worst_ms"]
    assert worst["total"] == 1550.0 and worst["quote"] == 1200.0 and worst["swap_instructions"] == 300.0
    assert worst["plan"] == 50.0 and worst["venue:pump_amm"] == 1500.0
    assert "extra_worst_ms total=1550 " in lane.prefetch_report_line(report)
    assert getattr(pcb.PREFETCH_TIMINGS, "sink", None) is None and seen == [None]


def test_slow_extras_do_not_hold_balance_or_blockhash_refresh():
    import threading
    clock, lines = Clock(), []
    entered, release = threading.Event(), threading.Event()

    def slow_extra(_snapshot):
        entered.set()
        assert release.wait(2.0)

    pre = _pre(clock, lines, extra_refresh_fn=slow_extra)
    extra = threading.Thread(target=lambda: pre.loop_once(groups=()), daemon=True)
    extra.start()
    try:
        assert entered.wait(1.0)
        pre.loop_once(groups=("balances",), include_extra=False)
        pre.loop_once(groups=("blockhash",), include_extra=False)
        snap = pre.snapshot()
        assert snap.usdc_balance == 400.0 and snap.sol_balance == 0.5
        assert snap.blockhash == "H" and snap.sol_usd is None
        first_stamp = snap.observed_mono["balances"]
        clock.t += 3.0
        pre.loop_once(groups=("balances",), include_extra=False)
        assert pre.snapshot().observed_mono["balances"] > first_stamp
        assert extra.is_alive()
    finally:
        release.set()
        extra.join(timeout=1.0)
    assert not extra.is_alive()
    assert pre.take_report(force=True)["loops"] == 4


def test_slow_sol_price_does_not_hold_other_essential_refreshes():
    import threading
    clock, lines = Clock(), []
    entered, release = threading.Event(), threading.Event()

    def slow_price():
        entered.set()
        assert release.wait(2.0)
        return 150.0

    pre = _pre(clock, lines, sol_usd_fn=slow_price)
    price = threading.Thread(target=lambda: pre.loop_once(
        groups=("sol_usd",), include_extra=False), daemon=True)
    price.start()
    try:
        assert entered.wait(1.0)
        pre.loop_once(groups=("balances",), include_extra=False)
        pre.loop_once(groups=("blockhash",), include_extra=False)
        snap = pre.snapshot()
        assert snap.usdc_balance == 400.0 and snap.blockhash == "H"
        assert snap.sol_usd is None and "sol_usd" not in snap.observed_mono
    finally:
        release.set()
        price.join(timeout=1.0)
    assert pre.snapshot().sol_usd == 150.0


def test_partial_pair_failure_keeps_both_old_balances_and_their_stamp():
    clock, lines = Clock(), []
    state = {"usdc": 400.0, "sol": 0.5}
    pre = _pre(clock, lines, usdc_fn=lambda: state["usdc"], sol_fn=lambda: state["sol"])
    pre.loop_once(groups=("balances",), include_extra=False)
    original = pre.snapshot()
    state.update(usdc=0.0, sol=None)
    clock.t += 3.0
    pre.loop_once(groups=("balances",), include_extra=False)
    failed = pre.snapshot()
    assert (failed.usdc_balance, failed.sol_balance) == (400.0, 0.5)
    assert failed.observed_mono["balances"] == original.observed_mono["balances"]
    assert original.observed_mono == {"balances": 1000.0}
    state["sol"] = 0.0
    clock.t += 0.5
    pre.loop_once(groups=("balances",), include_extra=False)
    zero = pre.snapshot()
    assert (zero.usdc_balance, zero.sol_balance) == (0.0, 0.0)
    assert zero.observed_mono["balances"] == clock.t
