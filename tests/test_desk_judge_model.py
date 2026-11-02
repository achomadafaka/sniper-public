"""JUDGE model is cohort identity (operator 2026-10-23): absent = Haiku (byte-identical), the override knob rolls back."""
import pytest

from sniper.services import memecoin_desk_judge as J


def test_resolution_order():
    opus = {"policy": {"judge_model": "claude-opus-5-5"}}
    assert J.judge_model({"policy": {}}, environ={}) == "claude-haiku-5-5"
    assert J.judge_model(opus, environ={}) == "claude-opus-5-5"
    assert J.judge_model(opus, environ={"MEMECOIN_DESK_JUDGE_MODEL_OVERRIDE": "claude-haiku-5-5"}) == "claude-haiku-5-5"
    assert J.judge_model(opus, environ={"MEMECOIN_DESK_JUDGE_MODEL_OVERRIDE": " "}) == "claude-opus-5-5"


@pytest.mark.parametrize("bad", [{"policy": {"judge_model": "gpt-5"}}, {"policy": {}}])
def test_unknown_model_refuses(bad):
    env = {"MEMECOIN_DESK_JUDGE_MODEL_OVERRIDE": "claude-opus-4"} if not bad["policy"] else {}
    with pytest.raises(ValueError):
        J.judge_model(bad, environ=env)


def test_the_proof_names_the_model_that_was_asked():
    assert J._proof("t", {"model_requested": "claude-opus-5-5"}, "2026-10-23T00:00:00+00:00")["model"] == "claude-opus-5-5"
    assert J._proof("t", {}, "2026-10-23T00:00:00+00:00")["model"] == "claude-haiku-5-5"


def test_provisioning_refuses_an_unknown_model():
    from test_memecoin_desk_pipeline import POLICY, AT
    import sqlite3, tempfile, os
    from sniper.services import memecoin_desk as desk
    from sniper.persistence import db_memecoin as ledger
    from sniper.persistence import db_memecoin_desk as store
    d = tempfile.mkdtemp()
    lc = sqlite3.connect(os.path.join(d, "l.db")); ledger.ensure_memecoin_tables(lc)
    dc = store.connect(os.path.join(d, "d.db"))
    with pytest.raises(ValueError, match="judge_model"):
        desk.provision_cohort(dc, lc, producer="U", policy={**POLICY, "judge_model": "claude-opus-4"},
                              prompt_version="desk-judge-v22", created_at=AT)
    assert desk.provision_cohort(dc, lc, producer="U", policy={**POLICY, "judge_model": "claude-opus-5-5"},
                                 prompt_version="desk-judge-v22", created_at=AT)


def _worker():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("jw_model", Path(__file__).resolve().parents[1] / "scripts" / "memecoin_desk_judge_worker.py")
    W = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(W)
    return W


def test_cli_path_is_judge_seat_only(monkeypatch):
    """Lambda G1: the env knob must not move RISK / post-mortem / scout seats (they share _sdk_options) onto another CLI."""
    W = _worker()
    monkeypatch.setenv("MEMECOIN_DESK_JUDGE_CLI_PATH", "/home/x/claude")
    assert W._sdk_options("s", model="m", effort="low", thinking=None, persist_session=False).cli_path is None
    assert W.judge_cli_path() == "/home/x/claude"
    assert str(W._sdk_options("s", model="m", effort="low", thinking=None, persist_session=False,
                              cli_path=W.judge_cli_path()).cli_path) == "/home/x/claude"
    monkeypatch.delenv("MEMECOIN_DESK_JUDGE_CLI_PATH")
    assert W.judge_cli_path() is None


def test_the_warm_spare_follows_the_cohort_model():
    W = _worker()

    class Spare:
        def __init__(self, model):
            self.options, self.closed = type("O", (), {"model": model})(), False

        def close(self):
            self.closed = True
    slot = {"spare": Spare("claude-haiku-5-5")}
    first = slot["spare"]
    assert W.spare_for(slot, "claude-haiku-5-5", Spare) is first
    got = W.spare_for(slot, "claude-opus-5-5", Spare)
    assert got.options.model == "claude-opus-5-5" and slot["spare"] is got
    import time
    deadline = time.monotonic() + 2                       # the old one retires on its own thread (eCat: never assume scheduling)
    while not first.closed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert first.closed
    assert W.spare_for({"spare": None}, "claude-opus-5-5", Spare) is None          # spare off stays off



def test_a_slow_or_failing_old_spare_never_delays_the_new_model_call():
    """eCat on 319470b4e: the old spare is retired off the decision path; a 5 s or raising close() costs the call nothing."""
    import threading
    import time
    W = _worker()
    gate = threading.Event()

    class Old:
        options = type("O", (), {"model": "claude-haiku-5-5"})()

        def __init__(self, fail):
            self.fail, self.closed = fail, threading.Event()

        def close(self):
            gate.wait(5)
            self.closed.set()
            if self.fail:
                raise TimeoutError("disconnect")

    class New:
        def __init__(self, model):
            self.options = type("O", (), {"model": model})()
    for fail in (False, True):
        gate.clear()
        old = Old(fail)
        slot = {"spare": old}
        t0 = time.monotonic()
        got = W.spare_for(slot, "claude-opus-5-5", New)
        assert time.monotonic() - t0 < 0.5 and got.options.model == "claude-opus-5-5" and not old.closed.is_set()
        gate.set()
        assert old.closed.wait(2)


def test_startup_spare_uses_only_the_configured_compatible_u_cohort(monkeypatch):
    W = _worker()
    cohort = {"producer": "U", "prompt_version": "desk-judge-v24",
              "policy": {"judge_model": "claude-opus-5-5", "judge_max_output_tokens": 256}}
    seen = []

    def get_cohort(conn, cohort_id):
        seen.append((conn, cohort_id))
        return cohort
    monkeypatch.setattr(W.store, "get_cohort", get_cohort)
    conn, env = object(), {"MEMECOIN_DESK_U_COHORT_ID": "live-u"}
    assert W.initial_spare_model(conn, "desk-judge-v24", 256, environ=env) == "claude-opus-5-5"
    assert seen == [(conn, "live-u")]
    assert W.initial_spare_model(conn, "desk-judge-v24", 256, environ={}) == J.MODEL
    assert len(seen) == 1                                  # never infer active from historical cohorts
    override = {**env, "MEMECOIN_DESK_JUDGE_MODEL_OVERRIDE": "claude-haiku-5-5"}
    assert W.initial_spare_model(conn, "desk-judge-v24", 256, environ=override) == "claude-haiku-5-5"
    cohort["policy"]["judge_max_output_tokens"] = None
    assert W.initial_spare_model(conn, "desk-judge-v24", None, environ=env) == "claude-opus-5-5"
    assert W.initial_spare_model(conn, "desk-judge-v24", None,
                                 environ={**env, "MEMECOIN_DESK_JUDGE_MODEL_OVERRIDE": "invalid"}) == J.MODEL


def test_startup_spare_does_not_guess_from_missing_or_incompatible_cohorts(monkeypatch):
    W = _worker()
    env = {"MEMECOIN_DESK_U_COHORT_ID": "live-u"}
    valid = {"producer": "U", "prompt_version": "desk-judge-v24",
             "policy": {"judge_model": "claude-opus-5-5", "judge_max_output_tokens": 256}}
    for cohort in (None, {}, {**valid, "producer": "H"}, {**valid, "prompt_version": "old"},
                   {**valid, "policy": ["malformed"]},
                   {**valid, "policy": {**valid["policy"], "judge_max_output_tokens": 128}},
                   {**valid, "policy": {**valid["policy"], "judge_model": "gpt-5"}}):
        monkeypatch.setattr(W.store, "get_cohort", lambda conn, cid, cohort=cohort: cohort)
        assert W.initial_spare_model(object(), "desk-judge-v24", 256, environ=env) == J.MODEL


@pytest.mark.parametrize("lock_lost", [False, True])
@pytest.mark.parametrize("model", ["claude-opus-5-5", "gpt-6-luna"])
def test_main_wires_cohort_startup_and_idle_refresh_after_singleton_lock(tmp_path, monkeypatch, lock_lost, model):
    import atexit
    import fcntl
    import time
    from types import SimpleNamespace
    from sniper.persistence import db_core, db_memecoin as ledger
    from sniper.util import state_paths

    W = _worker()
    events, spares, callbacks = [], [], []
    conn = SimpleNamespace(close=lambda: events.append("desk_closed"))
    ledger_conn = SimpleNamespace(close=lambda: None)
    cohort = {"producer": "U", "prompt_version": "desk-judge-v24",
              "policy": {"judge_model": model, "judge_max_output_tokens": 256,
                         **({"judge_provider": "codex_cli", "judge_effort": "low"} if model.startswith("gpt-") else {})}}

    class Spare:
        def __init__(self, options, **kwargs):
            self.options = options
            spares.append(self)

        def refresh(self):
            events.append("refresh")

        def close(self):
            events.append("spare_closed")

    def lock(*args):
        if lock_lost:
            raise BlockingIOError("already owned")
        events.append("lock")

    def connect(path):
        events.append("connect")
        return conn

    def run_once(*args, **kwargs):
        events.append("run")
        assert (spares == []) if model.startswith("gpt-") else (spares[0].options.model == model)
        if events.count("run") == 2:
            raise KeyboardInterrupt
        return []

    def no_sdk(*args, **kwargs):
        raise AssertionError("offline witness must not invoke a model")
    no_sdk.real_sdk_adapter = True
    for key, value in {"MEMECOIN_DESK_JUDGE_ENABLED": "1", "MEMECOIN_DESK_JUDGE_WARM_SPARE": "1",
                       "MEMECOIN_DESK_U_COHORT_ID": "live-u", "MEMECOIN_DESK_JUDGE_MAX_OUTPUT_TOKENS": "256",
                       "MEMECOIN_DESK_JUDGE_ASOF": "0", "MEMECOIN_DESK_JUDGE_STRUCTURE": "0",
                       "MEMECOIN_DESK_JUDGE_CONTEXT": "0"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("MEMECOIN_DESK_JUDGE_MODEL_OVERRIDE", raising=False)
    monkeypatch.setattr(W, "WarmSpare", Spare)
    # Options construction is not under test; importing the SDK registers unrelated atexit hooks.
    monkeypatch.setattr(W, "_sdk_options", lambda *a, **kw: SimpleNamespace(model=kw["model"]))
    monkeypatch.setattr(W, "sdk_call_model", no_sdk)
    monkeypatch.setattr(W, "load_env_files", lambda path: None)
    monkeypatch.setattr(W, "state_root", lambda: tmp_path)
    monkeypatch.setattr(state_paths, "require_state_root_outside_release", lambda: None)
    monkeypatch.setattr(fcntl, "flock", lock)
    monkeypatch.setattr(atexit, "register", callbacks.append)
    monkeypatch.setattr(W.signal, "signal", lambda *args: None)
    monkeypatch.setattr(W, "time", SimpleNamespace(monotonic=time.monotonic, sleep=lambda seconds: events.append("sleep")))
    monkeypatch.setattr(W.store, "connect", connect)
    monkeypatch.setattr(W.store, "get_cohort", lambda c, cid: cohort)
    monkeypatch.setattr(db_core, "get_connection", lambda path: ledger_conn)
    monkeypatch.setattr(ledger, "desk_cohort_gates", lambda c: [])
    monkeypatch.setattr(W.J, "ranker_gate", lambda path: None)
    monkeypatch.setattr(W.J, "recover", lambda *args, **kwargs: None)
    monkeypatch.setattr(W.J, "close_orphan_claims", lambda c: [])
    monkeypatch.setattr(W.J, "run_once", run_once)
    for name in ("standing_pass", "courier_pass", "page_refusals", "note_holds", "note_aged"):
        monkeypatch.setattr(W, name, lambda *args, **kwargs: [])
    (tmp_path / "ledger.db").touch()
    playbook = tmp_path / "playbook.md"
    playbook.write_text("prompt_version: `desk-judge-v24`\n---\nOffline system prompt\n")
    assert W.main(["--desk-db", str(tmp_path / "desk.db"), "--ledger-db", str(tmp_path / "ledger.db"),
                   "--playbook", str(playbook), "--calls-per-hour", "15", "--loop"], env_root=tmp_path) == 0
    if lock_lost:
        assert spares == [] and events == [] and callbacks == []
    elif model.startswith("gpt-"):
        assert spares == [] and callbacks == []
        assert events == ["lock", "connect", "run", "sleep", "run", "desk_closed"]
    else:
        assert len(spares) == 1 and events == ["lock", "connect", "run", "refresh", "sleep", "run", "desk_closed"]
        assert len(callbacks) == 1
        callbacks[0]()
        assert events[-1] == "spare_closed"
