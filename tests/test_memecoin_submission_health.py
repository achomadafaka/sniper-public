"""Task 3 — process-local endpoint health + quarantine state machine.

Health is OBSERVABILITY, never authority: ``should_attempt`` does no network I/O and reads only the
last recorded snapshot. Three consecutive failures quarantine a free endpoint for 60s; a success ends
quarantine and clears the streak. A wrong-chain identity is a hard, restart-scoped exclusion that a
later success can never clear. The sanitized snapshot exposes labels/states/counts/health-age only —
never a URL, identity value, or raw error string.
"""
from sniper.execution.submission_health import HealthRegistry


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _reg(ids=("8453:base_standard",)):
    clk = Clock()
    return HealthRegistry(ids, clock=clk), clk


def test_new_provider_is_unverified_and_not_attemptable():
    reg, _ = _reg()
    assert reg.should_attempt("8453:base_standard") is False


def test_success_activates_new_provider_and_startup_failure_does_not():
    reg, _ = _reg()
    reg.record_failure("8453:base_standard", "transport")
    assert reg.should_attempt("8453:base_standard") is False
    reg.record_success("8453:base_standard", "0x2105")
    assert reg.should_attempt("8453:base_standard") is True


def test_three_consecutive_failures_quarantine_for_60s():
    reg, clk = _reg()
    reg.record_success("8453:base_standard", "0x2105")
    for _ in range(3):
        reg.record_failure("8453:base_standard", "http_429")
    assert reg.should_attempt("8453:base_standard") is False      # quarantined
    clk.t += 59.0
    assert reg.should_attempt("8453:base_standard") is False      # still inside the 60s window
    clk.t += 1.5
    assert reg.should_attempt("8453:base_standard") is True       # quarantine elapsed -> attemptable


def test_two_failures_do_not_quarantine():
    reg, _ = _reg()
    reg.record_success("8453:base_standard", "0x2105")
    reg.record_failure("8453:base_standard", "x")
    reg.record_failure("8453:base_standard", "x")
    assert reg.should_attempt("8453:base_standard") is True


def test_success_recovers_and_clears_failure_streak():
    reg, _ = _reg()
    reg.record_success("8453:base_standard", "0x2105")
    for _ in range(3):
        reg.record_failure("8453:base_standard", "x")
    assert reg.should_attempt("8453:base_standard") is False
    reg.record_success("8453:base_standard", "0x2105")
    assert reg.should_attempt("8453:base_standard") is True
    # streak cleared: two fresh failures again do not quarantine
    reg.record_failure("8453:base_standard", "x")
    reg.record_failure("8453:base_standard", "x")
    assert reg.should_attempt("8453:base_standard") is True


def test_wrong_chain_excludes_until_new_registry_and_success_cannot_clear():
    reg, _ = _reg()
    reg.record_wrong_chain("8453:base_standard", "0xdead")
    assert reg.should_attempt("8453:base_standard") is False
    reg.record_success("8453:base_standard", "0x2105")            # a later success must NOT un-exclude
    assert reg.should_attempt("8453:base_standard") is False
    # A restart forgets the exclusion but requires startup identity proof again.
    fresh = HealthRegistry(("8453:base_standard",), clock=Clock())
    assert fresh.should_attempt("8453:base_standard") is False
    fresh.record_success("8453:base_standard", "0x2105")
    assert fresh.should_attempt("8453:base_standard") is True


def test_should_attempt_performs_no_network_call():
    # HealthRegistry never receives an http seam; should_attempt is pure state lookup.
    reg, _ = _reg()
    assert reg.should_attempt("8453:base_standard") in (True, False)
    assert not hasattr(reg, "_http")


def test_unknown_provider_defaults_to_attemptable():
    reg, _ = _reg()
    assert reg.should_attempt("never:registered") is True


def test_runtime_checker_quarantines_then_recovers_without_action_thread_io():
    import time

    from sniper.execution import submission_backends as B
    from sniper.execution.free_endpoint_registry import free_endpoints
    from sniper.execution.submission_health import apply_probe

    endpoint = free_endpoints("8453")[0]
    reg = HealthRegistry((endpoint.provider_id,), health_interval_s=0.01)
    verdicts = iter([("fail", "transport"), ("fail", "transport"),
                     ("fail", "transport"), ("ok", "0x2105")])
    calls = []

    def probe_one(e):
        calls.append(e.provider_id)
        return apply_probe(reg, e, next(verdicts, ("ok", "0x2105")))

    stop, thread = B._start_health_checks((endpoint,), reg, probe_one, interval_s=0.01,
                                          register=False)
    try:
        deadline = time.monotonic() + 1.0
        saw_quarantine = False
        while time.monotonic() < deadline:
            state = reg.sanitized_snapshot()[endpoint.provider_id]["state"]
            saw_quarantine = saw_quarantine or state == "quarantined"
            if saw_quarantine and state == "active" and len(calls) >= 4:
                break
            time.sleep(0.005)
        assert saw_quarantine is True
        assert reg.should_attempt(endpoint.provider_id) is True
        assert len(calls) >= 4
    finally:
        stop.set()
        thread.join(timeout=1.0)


def test_sanitized_snapshot_has_no_url_identity_or_raw_error():
    reg, clk = _reg(("8453:base_standard", "4663:sequencer"))
    reg.record_failure("8453:base_standard", "https://secret-host/path 500 boom")
    reg.record_wrong_chain("4663:sequencer", "0xbadc0ffee")
    snap = reg.sanitized_snapshot()
    blob = repr(snap)
    assert "https" not in blob and "secret-host" not in blob and "boom" not in blob
    assert "0xbadc0ffee" not in blob                              # raw received identity not exposed
    row = snap["8453:base_standard"]
    assert set(row) >= {"state", "consecutive_failures", "ok", "fail", "wrong_chain"}
    assert snap["4663:sequencer"]["state"] == "excluded"


def test_snapshot_reports_last_health_age():
    reg, clk = _reg()
    reg.record_success("8453:base_standard", "0x2105")
    clk.t += 12.0
    snap = reg.sanitized_snapshot()
    assert snap["8453:base_standard"]["last_health_age_s"] == 12.0
    assert snap["8453:base_standard"]["state"] == "active"
