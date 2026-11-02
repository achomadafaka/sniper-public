"""The top-up planner must not act on one native read (phantom 2501595900762000 wei, 2026-09-05)."""
from sniper.services import memecoin_evm_gas_topup as G


def test_three_agreeing_reads_return_the_latest_value():
    vals = iter([6_000_000_000_000_000, 6_000_000_000_000_000, 5_999_900_000_000_000])
    assert G.agreeing_native_read(lambda: next(vals)) == 5_999_900_000_000_000


def test_one_phantom_among_three_reads_blocks_the_pass_and_logs_every_value():
    logged = []
    vals = iter([2_501_595_900_762_000, 6_000_000_000_000_000, 6_000_000_000_000_000])
    out = G.agreeing_native_read(lambda: next(vals), log=lambda msg, v: logged.append(v))
    assert out is None
    assert logged[0] == [2_501_595_900_762_000, 6_000_000_000_000_000, 6_000_000_000_000_000]
    assert len(logged) == 2 and len(logged[1]) == 3, "one (member, raw) entry per read"


def test_an_unreadable_balance_is_still_none_not_zero():
    assert G.agreeing_native_read(lambda: None) is None
    def boom():
        raise RuntimeError("rpc")
    assert G.agreeing_native_read(boom) is None


def test_small_gas_drift_between_reads_is_agreement():
    vals = iter([6_000_000_000_000_000, 5_990_000_000_000_000, 5_985_000_000_000_000])
    assert G.agreeing_native_read(lambda: next(vals)) == 5_985_000_000_000_000


def test_the_disagreement_log_names_the_member_that_answered_each_read():
    from sniper.execution import evm_wallet as W
    seq = iter([("https://good.example/x", "0x1550f7dca70000", 6_000_000_000_000_000),
                ("https://phantom.example/y", "0x8e3100b0a3f10", 2_501_595_900_762_000),
                ("https://good.example/x", "0x1550f7dca70000", 6_000_000_000_000_000)])
    def read():
        url, raw, val = next(seq)
        W._note_answer(url, {"result": raw})
        return val
    logged = []
    assert G.agreeing_native_read(read, log=lambda msg, v: logged.append(v)) is None
    members = logged[1]
    assert members[1] == ("phantom.example", "0x8e3100b0a3f10")
    assert members[0][0] == "good.example"


def test_reads_that_exceed_the_budget_return_none_and_do_not_finish_the_series(monkeypatch):
    """2026-09-05: three hedged reads of up to 12s each held the main loop 36-72s while two pool
    members were down; the watchdog restarted the runner twice. Past the budget the pass must
    give up, not keep reading."""
    import time
    from sniper.services import memecoin_evm_gas_topup as T
    clock = {"t": 100.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
    calls = []

    def slow_read():
        calls.append(1)
        clock["t"] += 12.0                    # each read takes the full hedge ceiling
        return 5_000_000_000_000_000
    logs = []
    out = T.agreeing_native_read(slow_read, reads=3, log=lambda *a: logs.append(a[0] % a[1:]), deadline_s=8.0)
    assert out is None
    assert len(calls) == 1, "a second read must not start once the budget is spent"
    assert any("exceeded" in l for l in logs)


def test_fast_reads_inside_the_budget_still_agree(monkeypatch):
    import time
    from sniper.services import memecoin_evm_gas_topup as T
    clock = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])

    def fast_read():
        clock["t"] += 0.3
        return 5_000_000_000_000_000
    assert T.agreeing_native_read(fast_read, reads=3, deadline_s=8.0) == 5_000_000_000_000_000
