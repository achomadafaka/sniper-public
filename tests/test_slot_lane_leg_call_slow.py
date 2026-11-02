"""Finding ca32c4b6 (2026-10-06): slot-lane prefetch slow loops rose from ~0.1% to ~3% after the
15:06Z reboot, all in the leg refresh, and the stall was inside a request the report could not name.
A leg call over 500 ms on the prefetch thread now logs one line: which call, how long, which Jupiter
host answered, its status, how many hosts were tried, the response size, and the source address and
interface the kernel routes that host through (Wi-Fi and ethernet share one subnet since the reboot).
"""
import logging
import socket

import pytest

from sniper.execution import pump_curve_buy as pcb
from sniper.execution import solana_real_seams as S

USER = "11111111111111111111111111111111"


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _jup(clock, *, quote_s=0.1, swap_s=0.1, raise_quote=False):
    def jup(method, path, body=None):
        if method == "GET":
            clock.t += quote_s
            S._JUP_LAST.base, S._JUP_LAST.tries = "https://api.jup.ag/swap/v1", 2
            if raise_quote:
                raise TimeoutError("timed out")
            return 200, {"otherAmountThreshold": "9", "outAmount": "10", "inAmount": "1"}
        clock.t += swap_s
        S._JUP_LAST.base, S._JUP_LAST.tries = "https://lite-api.jup.ag/swap/v1", 1
        return 200, {"swapInstruction": {"programId": USER, "accounts": [], "data": ""},
                     "addressLookupTableAddresses": []}
    return jup


def _leg(clock, jup, *, prefetch=True):
    if prefetch:
        pcb.PREFETCH_TIMINGS.sink, pcb.PREFETCH_TIMINGS.clock = {}, clock
    try:
        return pcb.prefetch_usdc_sol_leg(1_000_000, 50, user=USER, jup_call=jup, rpc=None)
    finally:
        pcb.PREFETCH_TIMINGS.sink = pcb.PREFETCH_TIMINGS.clock = None


@pytest.fixture(autouse=True)
def _fresh_line_cap(monkeypatch):
    monkeypatch.setattr(pcb, "_SLOW_LAST", {})


@pytest.fixture
def egress(monkeypatch):
    monkeypatch.setattr(pcb, "_leg_call_egress", lambda base: ("192.168.1.20", "wlp0s20f3", 12.0))


def _lines(caplog):
    return [r.getMessage() for r in caplog.records if "slot lane leg call slow" in r.getMessage()]


def test_a_slow_leg_call_names_its_call_host_status_tries_and_route(caplog, egress):
    clock = Clock()
    with caplog.at_level(logging.WARNING):
        _leg(clock, _jup(clock, quote_s=1.2))
    (line,) = _lines(caplog)
    for part in ("call=quote", "ms=1200", "host=https://api.jup.ag/swap/v1", "status=200", "tries=2",
                 "src=192.168.1.20", "iface=wlp0s20f3", "egress_age_s=12", "bytes="):
        assert part in line, (part, line)


def test_fast_calls_log_nothing(caplog, egress):
    clock = Clock()
    with caplog.at_level(logging.WARNING):
        _leg(clock, _jup(clock))
    assert _lines(caplog) == []


def test_a_call_that_raises_is_logged_and_still_raises(caplog, egress):
    """The 10 s transport timeout is exactly the stall to name."""
    clock = Clock()
    with caplog.at_level(logging.WARNING), pytest.raises(TimeoutError):
        _leg(clock, _jup(clock, quote_s=10.0, raise_quote=True))
    (line,) = _lines(caplog)
    assert "call=quote" in line and "status=exception:TimeoutError" in line and "ms=10000" in line


def test_the_exotic_worker_path_logs_and_routes_nothing(caplog, monkeypatch):
    """Outside the prefetch thread (no timing sink) a leg call pays only for itself."""
    monkeypatch.setattr(pcb, "_leg_call_egress", lambda base: pytest.fail("egress read off-thread"))
    clock = Clock()
    with caplog.at_level(logging.WARNING):
        _leg(clock, _jup(clock, quote_s=1.2), prefetch=False)
    assert _lines(caplog) == []


def test_an_unresolvable_host_is_unknown_never_an_error(monkeypatch):
    monkeypatch.setattr(pcb, "_EGRESS", {})
    monkeypatch.setattr(pcb, "_EGRESS_WORKER", {"thread": None})
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(OSError("dns")))
    assert pcb._leg_call_egress("https://api.jup.ag/swap/v1")[0] == "unknown"
    pcb._EGRESS_WORKER["thread"].join(2)
    assert pcb._leg_call_egress("https://api.jup.ag/swap/v1")[0] == "unknown" and pcb._EGRESS == {}


def test_the_seam_counts_every_host_a_call_tried(monkeypatch):
    monkeypatch.setattr(S, "_KEY_VAR", S.JUP_API_KEY_VAR)
    monkeypatch.delenv(S.JUP_API_KEY_VAR, raising=False)
    monkeypatch.delenv(S.CAPTURE_JUP_API_KEY_VAR, raising=False)
    S._jup_host_cooldown.clear()

    def http(method, url, body=None):
        return (429, {}, b"") if url.startswith(S.JUP_BASE) else (200, {"ok": 1}, b"")
    try:
        assert S._jup_call("GET", "/quote?x=1", http=http)[0] == 200
        assert S._JUP_LAST.tries == 2
        assert S._jup_call("GET", "/quote?x=1", http=http)[0] == 200   # the cooled host goes last
        assert S._JUP_LAST.tries == 1
    finally:
        S._jup_host_cooldown.clear()


def test_a_host_that_stalls_continuously_logs_once_per_ten_seconds_with_a_count(caplog, egress, monkeypatch):
    """Alpha: no repeat-line flood. One line per host per 10 s; the next says how many it held back."""
    clock = Clock()
    with caplog.at_level(logging.WARNING):
        for _ in range(3):                                  # three slow quotes inside 10 s
            _leg(clock, _jup(clock, quote_s=1.0))
        clock.t += 10.0
        _leg(clock, _jup(clock, quote_s=1.0))
    lines = _lines(caplog)
    assert len(lines) == 2
    assert "suppressed=0" in lines[0] and "suppressed=2" in lines[1]


# --- SOL-2 (Codex two-day review 2026-10-07): the egress read never blocks the refresh ----------

@pytest.fixture
def blocked_dns(monkeypatch):
    """A resolver that hangs until released, counting its calls; an empty egress cache."""
    import threading
    gate, calls = threading.Event(), []
    monkeypatch.setattr(pcb, "_EGRESS", {})
    monkeypatch.setattr(pcb, "_EGRESS_WORKER", {"thread": None})

    def hang(*a, **k):
        calls.append(a[0])
        gate.wait(10)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
    monkeypatch.setattr(socket, "getaddrinfo", hang)
    yield gate, calls
    gate.set()
    worker = pcb._EGRESS_WORKER["thread"]
    if worker is not None:
        worker.join(2)               # before monkeypatch restores the cache it would write into


def _bounded(fn, timeout=2.0):
    import threading
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - handed back to the test
            box["error"] = exc
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    assert not t.is_alive(), "the leg refresh waited on the egress read"
    return box


@pytest.mark.parametrize("quote_s, swap_s", [(1.2, 0.1), (0.1, 1.2)], ids=["quote", "swap_instructions"])
def test_a_hung_resolver_never_holds_a_finished_leg(caplog, blocked_dns, quote_s, swap_s):
    clock = Clock()
    with caplog.at_level(logging.WARNING):
        box = _bounded(lambda: _leg(clock, _jup(clock, quote_s=quote_s, swap_s=swap_s)))
    assert "error" not in box
    (line,) = _lines(caplog)
    assert "src=unknown" in line and "iface=unknown" in line and "egress_age_s=None" in line


def test_a_hung_resolver_never_holds_a_failed_call_and_the_error_still_raises(caplog, blocked_dns):
    clock = Clock()
    with caplog.at_level(logging.WARNING):
        box = _bounded(lambda: _leg(clock, _jup(clock, quote_s=10.0, raise_quote=True)))
    assert isinstance(box.get("error"), TimeoutError)


def test_repeated_slow_calls_start_one_egress_worker_and_a_later_line_has_the_facts(blocked_dns):
    import time
    gate, calls = blocked_dns
    for _ in range(5):
        assert pcb._leg_call_egress("https://api.jup.ag/swap/v1") == ("unknown", "unknown", None)
    time.sleep(0.05)
    assert calls == ["api.jup.ag"]                          # one worker, not five
    gate.set()
    pcb._EGRESS_WORKER["thread"].join(2)
    src, _iface, age = pcb._leg_call_egress("https://api.jup.ag/swap/v1")
    assert src == "127.0.0.1" and age is not None and age >= 0.0


def test_worker_metrics_name_subcalls_without_slow_logging(monkeypatch, caplog):
    clock = Clock()
    def forbidden(*args, **kwargs):
        pytest.fail('worker telemetry performed logging or an egress read')
    monkeypatch.setattr(pcb, '_leg_call_egress', forbidden)
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)
    monkeypatch.setattr(logging.getLogger(pcb.__name__), 'warning', forbidden)
    monkeypatch.setattr(pcb, '_payload_bytes', forbidden)
    with pcb.leg_metrics(clock=clock) as timings:
        leg = pcb.prefetch_usdc_sol_leg(
            1_000_000, 50, user=USER,
            jup_call=_jup(clock, quote_s=1.2, swap_s=.1), rpc=None)
    assert leg is not None
    assert timings['quote'] == pytest.approx(1200)
    assert timings['swap_instructions'] == pytest.approx(100)
    assert 'instruction_parse' in timings and 'alt_parse' not in timings
    assert _lines(caplog) == [] and pcb._SLOW_LAST == {}


def test_worker_metrics_exception_and_nested_restoration():
    clock = Clock()
    before = {n: getattr(pcb.PREFETCH_TIMINGS, n, None) for n in ('sink', 'clock', 'emit_slow')}
    with pcb.leg_metrics(clock=clock) as outer:
        with pytest.raises(TimeoutError), pcb.leg_metrics(clock=clock) as inner:
            pcb.prefetch_usdc_sol_leg(1, 50, user=USER,
                                     jup_call=_jup(clock, quote_s=1.2, raise_quote=True), rpc=None)
        assert inner == {'quote': pytest.approx(1200)}
        assert pcb.PREFETCH_TIMINGS.sink is outer
        with pcb.prefetch_timed('outer'):
            clock.t += .2
    assert outer == {'outer': pytest.approx(200)}
    assert {n: getattr(pcb.PREFETCH_TIMINGS, n, None) for n in before} == before


def test_worker_metrics_are_thread_local():
    import threading
    barrier = threading.Barrier(2)
    results, errors = [], []
    def run(seconds):
        try:
            clock = Clock()
            with pcb.leg_metrics(clock=clock) as sink:
                with pcb.prefetch_timed('thread'):
                    barrier.wait(timeout=2)
                    clock.t += seconds
                results.append(sink)
        except BaseException as exc:
            errors.append(exc)
    threads = [threading.Thread(target=run, args=(s,)) for s in (.1, .2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)
    assert not errors and not any(t.is_alive() for t in threads)
    assert sorted(r['thread'] for r in results) == pytest.approx([100, 200])


def test_worker_metrics_uncached_and_cached_alt(monkeypatch):
    clock = Clock()
    base = _jup(clock)
    def jup(method, path, body=None):
        status, payload = base(method, path, body)
        if method == 'POST':
            payload['addressLookupTableAddresses'] = ['table']
        return status, payload
    def rpc(*args):
        clock.t += .3
        return {'value': {'data': ['encoded']}}
    def parse(*args):
        clock.t += .2
        return 'parsed'
    monkeypatch.setattr(pcb, '_rpc_result', rpc)
    monkeypatch.setattr(pcb, 'lookup_table_from_account', parse)
    cache = {}
    for cached in (False, True):
        with pcb.leg_metrics(clock=clock) as sink:
            leg = pcb.prefetch_usdc_sol_leg(1, 50, user=USER, jup_call=jup, rpc=None, alt_cache=cache)
        assert leg['tables'] == ['parsed']
        if cached:
            assert 'alt_rpc' not in sink and 'alt_parse' not in sink
        else:
            assert sink['alt_rpc'] == pytest.approx(300)
            assert sink['alt_parse'] == pytest.approx(200)
