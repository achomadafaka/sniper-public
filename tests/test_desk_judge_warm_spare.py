"""P2 warm spare (operator yes 2026-10-23): one pre-connected CLI client per JUDGE seat, used for ONE sheet,
then disconnected and replaced in the background; any missing/stale/dead spare falls back to the cold call."""
import asyncio
import importlib.util
import threading
import time
from pathlib import Path

import claude_agent_sdk
from claude_agent_sdk import ResultMessage

spec = importlib.util.spec_from_file_location("jw", Path(__file__).resolve().parents[1] / "scripts/memecoin_desk_judge_worker.py")
JW = importlib.util.module_from_spec(spec)
spec.loader.exec_module(JW)


def _result(text='{"v":"S"}'):
    return ResultMessage(subtype="success", duration_ms=10, duration_api_ms=8, is_error=False, num_turns=1,
                         session_id="s", result=text, usage={})


class FakeClient:
    made = []

    def __init__(self, options, *, fail_connect=False, fail_query=False, hang=False):
        self.options, self.queries, self.connected, self.disconnected = options, [], False, False
        self.fail_connect, self.fail_query, self.hang = fail_connect, fail_query, hang
        FakeClient.made.append(self)

    async def connect(self):
        if self.fail_connect:
            raise RuntimeError("spawn failed")
        self.connected = True

    async def query(self, prompt):
        if self.fail_query:
            raise RuntimeError("cli died")
        self.queries.append(prompt)

    async def receive_response(self):
        if self.hang:
            await asyncio.sleep(30)
        yield _result('{"v":"warm"}')

    async def disconnect(self):
        self.disconnected = True


def _wait(cond, s=3.0):
    end = time.monotonic() + s
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def _cold(monkeypatch, calls):
    async def fake_query(*, prompt, options):
        calls.append(prompt)
        yield _result('{"v":"cold"}')
    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    monkeypatch.setattr(JW, "auth_page", lambda *a, **k: None)


def _spare(cls=FakeClient, **kw):
    FakeClient.made = []
    opts = JW._sdk_options("SYS", model=JW.J.MODEL, effort="low", thinking=None, persist_session=False)
    sp = JW.WarmSpare(opts, client_cls=cls, **kw)
    assert _wait(lambda: sp._client is not None or sp.stats["spawn_failed"])
    return sp


def test_one_sheet_per_spare_then_a_fresh_one(monkeypatch):
    cold = []
    _cold(monkeypatch, cold)
    sp = _spare()
    first = sp._client
    out = JW.sdk_call_model("SYS", "sheet-1", deadline_s=5, persist_session=False, spare=sp)
    assert out["text"] == '{"v":"warm"}' and out["timing"]["transport"] == "warm_spare" and cold == []
    assert out["timing"]["spare_miss_reason"] is None and out["timing"]["spare_connect_ms"] >= 0
    assert first.queries == ["sheet-1"] and _wait(lambda: first.disconnected)      # never reused
    assert _wait(lambda: sp._client is not None and sp._client is not first)        # next spare, fresh process
    out = JW.sdk_call_model("SYS", "sheet-2", deadline_s=5, persist_session=False, spare=sp)
    assert sp._client is not first and first.queries == ["sheet-1"]
    sp.close()


def test_spare_uses_the_cold_paths_exact_options():
    sp = _spare()
    cold = JW._sdk_options("SYS", model=JW.J.MODEL, effort="low", thinking=None, persist_session=False)
    assert (sp._client.options.model, sp._client.options.effort, sp._client.options.extra_args,
            sp._client.options.system_prompt) == (cold.model, cold.effort, cold.extra_args, cold.system_prompt)
    sp.close()


def test_no_ready_spare_makes_the_cold_call(monkeypatch):
    cold = []
    _cold(monkeypatch, cold)
    sp = _spare(cls=lambda o: FakeClient(o, fail_connect=True))
    out = JW.sdk_call_model("SYS", "sheet", deadline_s=5, persist_session=False, spare=sp)
    assert cold == ["sheet"] and out["timing"]["transport"] == "cold" and out["text"] == '{"v":"cold"}'
    assert out["timing"]["spare_miss_reason"] == "spawn_failed"
    assert out["timing"]["spare_connect_ms"] >= 0     # failed preparation, not the cold query's connect time
    sp.close()


def test_a_different_system_prompt_never_uses_the_spare(monkeypatch):
    cold = []
    _cold(monkeypatch, cold)
    sp = _spare()
    out = JW.sdk_call_model("OTHER", "sheet", deadline_s=5, persist_session=False, spare=sp)
    assert cold == ["sheet"] and sp._client.queries == []
    assert out["timing"]["spare_miss_reason"] == "options_mismatch"
    assert out["timing"]["spare_connect_ms"] is None
    sp.close()


def test_a_stale_spare_is_discarded_unused(monkeypatch):
    cold, now = [], [0.0]
    _cold(monkeypatch, cold)
    sp = _spare(max_age_s=600, clock=lambda: now[0])
    old = sp._client
    now[0] = 601.0
    out = JW.sdk_call_model("SYS", "sheet", deadline_s=5, persist_session=False, spare=sp)
    assert cold == ["sheet"] and old.queries == [] and _wait(lambda: old.disconnected)
    assert sp.stats["stale"] == 1 and out["timing"]["transport"] == "cold"
    assert out["timing"]["spare_miss_reason"] == "stale"
    sp.close()


def test_a_spare_that_dies_before_answering_falls_back_cold(monkeypatch):
    cold = []
    _cold(monkeypatch, cold)
    sp = _spare(cls=lambda o: FakeClient(o, fail_query=True))
    out = JW.sdk_call_model("SYS", "sheet", deadline_s=5, persist_session=False, spare=sp)
    assert cold == ["sheet"] and out["text"] == '{"v":"cold"}'
    assert out["timing"]["transport"] == "warm_spare_failed_cold" and out["call_error"] is None
    sp.close()


def test_a_hung_spare_times_out_at_the_deadline_without_a_second_call(monkeypatch):
    cold = []
    _cold(monkeypatch, cold)
    sp = _spare(cls=lambda o: FakeClient(o, hang=True))
    t0 = time.monotonic()
    out = JW.sdk_call_model("SYS", "sheet", deadline_s=0.5, persist_session=False, spare=sp)
    assert time.monotonic() - t0 < 2.0 and cold == [] and out["call_error"] == "timeout"
    sp.close()


def test_concurrent_callers_never_share_a_spare(monkeypatch):
    cold = []
    _cold(monkeypatch, cold)
    sp = _spare()
    got = []
    barrier = threading.Barrier(2)

    def take():
        barrier.wait()
        got.append(sp.take())
    ts = [threading.Thread(target=take) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    taken = [c for c in got if c is not None]
    assert taken and len({id(c) for c in taken}) == len(taken)       # a fast respawn may serve both, never one twice
    for client in taken:
        sp.run(client, "one-sheet", lambda message: None, 5)
    sp.close()
    assert _wait(lambda: not sp._loop.is_running())


def test_close_during_spawn_disconnects_the_late_client():
    gate = threading.Event()

    class Slow(FakeClient):
        async def connect(self):
            await asyncio.get_running_loop().run_in_executor(None, gate.wait)
            self.connected = True
    FakeClient.made = []
    opts = JW._sdk_options("SYS", model=JW.J.MODEL, effort="low", thinking=None, persist_session=False)
    sp = JW.WarmSpare(opts, client_cls=Slow)
    assert _wait(lambda: FakeClient.made)
    sp.close(wait=False)
    gate.set()
    assert _wait(lambda: FakeClient.made[0].disconnected) and sp._client is None


def test_a_stubbed_adapter_never_builds_a_spare():
    assert JW.sdk_call_model.real_sdk_adapter is True
    stub = lambda *a, **k: None                                              # noqa: E731
    assert getattr(stub, "real_sdk_adapter", False) is False


def test_idle_refresh_renews_only_an_unused_spare_before_expiry(monkeypatch):
    now, cold = [0.0], []
    _cold(monkeypatch, cold)
    sp = _spare(max_age_s=600, clock=lambda: now[0])
    old = sp._client
    try:
        now[0] = 569.0
        sp.refresh()
        assert sp._client is old and len(FakeClient.made) == 1
        now[0] = 571.0
        started = time.monotonic()
        sp.refresh()
        assert time.monotonic() - started < 0.5
        assert _wait(lambda: sp._client is not None and sp._client is not old)
        assert old.queries == [] and _wait(lambda: old.disconnected)
        new = sp._client
        out = JW.sdk_call_model("SYS", "sheet", deadline_s=5, persist_session=False, spare=sp)
        assert cold == [] and new.queries == ["sheet"] and old.queries == []
        assert out["timing"]["spare_miss_reason"] is None
        assert out["timing"]["spare_connect_ms"] >= 0
    finally:
        sp.close()


def test_idle_refresh_does_not_disconnect_a_consumed_client():
    now = [0.0]
    sp = _spare(max_age_s=600, clock=lambda: now[0])
    taken = sp.take()
    try:
        assert _wait(lambda: sp._client is not None)
        now[0] = 571.0
        sp.refresh()
        assert not taken.disconnected
        sp.run(taken, "only-sheet", lambda message: None, 5)
        assert taken.queries == ["only-sheet"] and _wait(lambda: taken.disconnected)
    finally:
        sp.close()


def test_ready_publication_can_prepare_the_next_client_before_the_call_runs():
    gate, published, resume = threading.Event(), threading.Event(), threading.Event()

    class GatedConnect(FakeClient):
        async def connect(self):
            while not gate.is_set():
                await asyncio.sleep(0.001)
            self.connected = True
    FakeClient.made = []
    opts = JW._sdk_options("SYS", model=JW.J.MODEL, effort="low", thinking=None, persist_session=False)
    sp = JW.WarmSpare(opts, client_cls=GatedConnect)
    lock, first = sp._lock, None

    class PauseAfterPublication:
        def __enter__(self):
            lock.acquire()

        def __exit__(self, *args):
            pause = sp._client is not None and not published.is_set()
            lock.release()
            if pause:
                published.set()
                resume.wait(2)
    sp._lock = PauseAfterPublication()
    try:
        gate.set()
        assert published.wait(2)
        first = sp.take()
        resume.set()
        assert first is not None and _wait(lambda: sp._client is not None and sp._client is not first)
    finally:
        gate.set()
        resume.set()
        if first is not None:
            sp.run(first, "one-sheet", lambda message: None, 5)
        sp.close()


def test_failed_connect_is_cleaned_up_and_idle_retries_are_bounded():
    now = [0.0]
    sp = _spare(cls=lambda o: FakeClient(o, fail_connect=True), clock=lambda: now[0])
    try:
        assert _wait(lambda: FakeClient.made[0].disconnected)
        timing = {}
        assert sp.take(timing=timing) is None
        assert timing["spare_miss_reason"] == "spawn_failed" and timing["spare_connect_ms"] >= 0
        for _ in range(10):
            sp.refresh()
        now[0] = 29.9
        sp.refresh()
        assert len(FakeClient.made) == 1
        now[0] = 30.1
        sp.refresh()
        assert _wait(lambda: sp.stats["spawn_failed"] == 2)
        assert len(FakeClient.made) == 2 and _wait(lambda: FakeClient.made[-1].disconnected)
    finally:
        sp.close()


def test_close_during_idle_renewal_disconnects_late_client_without_waiting():
    gate, now = threading.Event(), [0.0]

    class SlowRenewal(FakeClient):
        async def connect(self):
            if len(FakeClient.made) > 1:
                await asyncio.get_running_loop().run_in_executor(None, gate.wait)
            self.connected = True
    sp = _spare(cls=SlowRenewal, max_age_s=600, clock=lambda: now[0])
    old = sp._client
    try:
        now[0] = 571.0
        sp.refresh()
        assert _wait(lambda: len(FakeClient.made) == 2)
        started = time.monotonic()
        sp.refresh()                                      # an in-flight replacement is not duplicated
        sp.close(wait=False)
        assert time.monotonic() - started < 0.5 and len(FakeClient.made) == 2
        gate.set()
        assert _wait(lambda: FakeClient.made[-1].disconnected)
        assert old.queries == [] and sp._client is None
        sp.refresh()                                      # a closed spare never restarts
        assert len(FakeClient.made) == 2
    finally:
        gate.set()
        if not sp._closed:
            sp.close()


def test_pending_or_closed_preparation_has_explicit_miss_reason():
    gate = threading.Event()

    class Slow(FakeClient):
        async def connect(self):
            await asyncio.get_running_loop().run_in_executor(None, gate.wait)
            self.connected = True
    FakeClient.made = []
    opts = JW._sdk_options("SYS", model=JW.J.MODEL, effort="low", thinking=None, persist_session=False)
    sp = JW.WarmSpare(opts, client_cls=Slow)
    try:
        assert _wait(lambda: FakeClient.made)
        timing = {}
        assert sp.take(timing=timing) is None
        assert timing == {"spare_miss_reason": "spawning", "spare_connect_ms": None}
        sp.close(wait=False)
        timing = {}
        assert sp.take(timing=timing) is None
        assert timing["spare_miss_reason"] == "closed" and timing["spare_connect_ms"] is None
    finally:
        gate.set()
        if not sp._closed:
            sp.close()
        assert _wait(lambda: FakeClient.made[-1].disconnected)


def test_idle_renewal_and_close_drain_slow_cleanup_without_cancelling_it():
    for renew in (False, True):
        now, cancelled = [0.0], []

        class SlowCleanup(FakeClient):
            async def disconnect(self):
                try:
                    await asyncio.sleep(1.05)
                    self.disconnected = True
                except asyncio.CancelledError:
                    cancelled.append(self)
                    raise
        sp = _spare(cls=SlowCleanup, max_age_s=600, clock=lambda: now[0])
        try:
            if renew:
                now[0] = 571.0
                sp.refresh()
            started = time.monotonic()
            sp.close(wait=False)
            assert time.monotonic() - started < 0.5          # cleanup remains on the spare loop
            assert _wait(lambda: all(client.disconnected for client in FakeClient.made), s=4.0)
            assert cancelled == [] and _wait(lambda: not sp._loop.is_running())
        finally:
            if not sp._closed:
                sp.close()


def test_interpreter_exit_waits_for_ready_spare_cleanup(tmp_path):
    import subprocess
    import sys
    import textwrap

    marker = tmp_path / "disconnected"
    program = textwrap.dedent('''
        import ast, asyncio, atexit, logging, math, sys, time
        from pathlib import Path
        source = Path(sys.argv[1])
        node = next(n for n in ast.parse(source.read_text()).body
                    if isinstance(n, ast.ClassDef) and n.name == "WarmSpare")
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"))
        class Fake:
            async def connect(self):
                pass
            async def disconnect(self):
                await asyncio.sleep(0.05)
                Path(sys.argv[2]).write_text("disconnected")
        spare = WarmSpare(None, client_cls=lambda options: Fake())
        deadline = time.monotonic() + 3.0
        while spare._client is None and time.monotonic() < deadline:
            time.sleep(0.005)
        assert spare._client is not None
        atexit.register(spare.close)
    ''')
    result = subprocess.run([sys.executable, "-I", "-c", program, str(Path(JW.__file__)), str(marker)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert marker.is_file(), "interpreter exited before the ready client's disconnect completed"
    assert marker.read_text() == "disconnected"


def test_close_registers_held_cleanup_before_an_old_retirement_can_stop_loop():
    release, started, interleaved = threading.Event(), threading.Event(), threading.Event()
    sp = _spare()
    held = sp._client

    class PriorCleanup(FakeClient):
        async def disconnect(self):
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.001)
            self.disconnected = True

    prior = PriorCleanup(sp.options)
    sp._discard(prior)
    assert started.wait(3)
    original_lock, closing_thread = sp._lock, threading.get_ident()

    class InterleaveAtCloseUnlock:
        def __enter__(self):
            original_lock.acquire()
            return self

        def __exit__(self, *exc):
            fire = (threading.get_ident() == closing_thread and sp._closed and not interleaved.is_set())
            if fire:
                interleaved.set()                         # claim the hook before releasing the real lock
            original_lock.release()
            if fire:
                release.set()
                assert _wait(lambda: prior.disconnected)
                _wait(lambda: not sp._loop.is_running(), s=0.2)  # let the old gap stop it; a published retirement must keep it alive

    sp._lock = InterleaveAtCloseUnlock()
    try:
        sp.close()
        assert interleaved.is_set() and prior.disconnected
        assert held.disconnected, "the old retirement stopped the loop before held cleanup was registered"
        assert not sp._retiring and not sp._loop.is_running()
    finally:
        release.set()
        if not sp._closed:
            sp.close()
        # Drain the fake cleanup stranded by the old bug, so a RED run leaves no pending coroutine.
        if sp._retiring and not sp._loop.is_running():
            rescue = threading.Thread(target=sp._loop.run_forever, daemon=True)
            rescue.start()
            assert _wait(lambda: held.disconnected and not sp._retiring)
            sp._loop.call_soon_threadsafe(sp._loop.stop)
            rescue.join(1)
