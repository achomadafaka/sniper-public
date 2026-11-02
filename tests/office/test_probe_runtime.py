"""T15 probe runtime factory: phases per --once pass, terminal records, the model-call go, and that the factory
drops agent turns while keeping the base runtime's readiness and close."""
import json
from types import SimpleNamespace

import pytest

from office.test_sheet_tool import Reads, candidate, hard, CALL_NOW
from office.test_pilot_seats import ROOT, NOW, FX
from office.test_probe import env, SERVED

from sniper.office import ledger as L, probe as P, probe_runtime as PR, store as S


class Sched:
    def __init__(self):
        self.calls = []

    def submit(self, provider, fn, **kw):
        self.calls.append(provider)
        return fn()


@pytest.fixture
def world(tmp_path, monkeypatch):
    Reads(monkeypatch)
    monkeypatch.setattr(L, "_canonical_pool", lambda mint: "P")
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()

    def write_input(**over):
        doc = {"decision_id": "d1", "candidate": candidate(), "hard_filter_row": hard(),
               "desk_policy": {"rail_bounds": {}, "hard_filter": {}, "v": 1}, "strategy_epoch": P.PROBE_EPOCH,
               "allow_model_calls": True, "bound_usd_per_fill": 0.1, **over}
        (probe_dir / "input.json").write_text(json.dumps(doc))
    store, ledger = S.open_store(tmp_path / "office.db"), L.open_ledger(tmp_path / "ledger.db")
    sched = Sched()
    monkeypatch.setattr(P, "judge_call_over", lambda *a, **k: lambda slot, u, sc: env(served=SERVED[slot]))

    def run(**over):
        kw = dict(probe_dir=probe_dir, store_conn=store, ledger_conn=ledger,
                  book_policy_path=ROOT / "config/office/book_policy_pilot.json", desk_dir="/d", clock_file="/c",
                  scheduler=sched, transports={}, deadline_s=30,
                  manifest={"seats": {s: {"provider": "claude" if s == "judge_claude" else "codex"} for s in P.JUDGE_SLOTS}},
                  prompts={}, now_host=lambda: CALL_NOW, ledger_reader=lambda: [],
                  fx_reader=lambda d, **k: (FX, NOW), clock_reader=lambda f, now: (0.25, CALL_NOW - 10))
        return PR.probe_pass(**{**kw, **over})
    yield SimpleNamespace(run=run, write_input=write_input, probe_dir=probe_dir, sched=sched, ledger=ledger)
    store.close(); ledger.close()


def steps(world):
    return [r["step"] for r in PR.read_report(world.probe_dir / "report.jsonl")]


def test_front_runs_once_through_the_scheduler_then_grades_then_is_done(world):
    world.write_input()
    assert world.run() == "front" and world.sched.calls == ["claude", "codex"]
    assert steps(world)[:7] == ["sheet", "judge:judge_claude", "judge:judge_gpt", "judges", "aggregate", "risk", "trader"]
    assert world.run() == "no_order"                      # stubbed safety vetoes: graded as no order
    assert world.run() == "done" and world.sched.calls == ["claude", "codex"]     # no second judge call
    rows = PR.read_report(world.probe_dir / "report.jsonl")
    assert all(r["label"] == "non-evaluation" and r["epoch_id"] == P.PROBE_EPOCH for r in rows)


def test_without_the_go_no_scheduler_call_and_a_terminal_stop(world):
    world.write_input(allow_model_calls=False)
    assert world.run() == "stopped" and world.sched.calls == []
    assert steps(world)[-1] == "stop" and world.run() == "done"


def test_unknown_clock_is_not_terminal_and_bad_input_stops(world):
    world.write_input()
    assert world.run(clock_reader=lambda f, now: (None, None)) == "clock_unknown" and steps(world) == []
    world.write_input(extra=1)
    with pytest.raises(P.ProbeStop, match="input_keys"):
        world.run()


def test_factory_keeps_readiness_drops_agent_turns_and_closes_both(monkeypatch, tmp_path):
    seen = {"tasks": 0, "closed": 0, "pass": 0}
    base = {"supervisor": SimpleNamespace(conn=None, lease=None), "scheduler": None, "transports": {}, "deadline_s": 30,
            "max_steps": 4, "tasks": lambda: seen.__setitem__("tasks", seen["tasks"] + 1) or [{"agent_id": "a"}],
            "close": lambda: seen.__setitem__("closed", 1)}
    monkeypatch.setattr(PR.RT, "build", lambda **kw: base)
    monkeypatch.setattr(PR.RT, "_read_config", lambda p: ({"state_root": str(tmp_path), "config_root": str(tmp_path),
        "clock_file": "/c", "turn_deadline_s": 30, "model_manifest": {"path": "m", "sha256": "s"},
        "pilot_seed_launcher_config": {"prompts": {}}}, "pin"))
    monkeypatch.setattr(PR, "approved_path", lambda p: tmp_path)
    monkeypatch.setattr(PR.pilot_models, "load", lambda p, s: {"seats": {}})
    monkeypatch.setattr(PR, "probe_pass", lambda **kw: seen.__setitem__("pass", 1) or "front")
    rt = PR.build(store_path="s", ledger_path=tmp_path / "ledger.db", config_path="c", guard_path="g")
    assert rt["tasks"]() == [] and seen["tasks"] == 1 and seen["pass"] == 1        # agent task list is dropped
    rt["close"]()
    assert seen["closed"] == 1


def factory(monkeypatch, tmp_path, base):
    monkeypatch.setattr(PR.RT, "build", lambda **kw: base)
    monkeypatch.setattr(PR.RT, "_read_config", lambda p: ({"state_root": str(tmp_path), "config_root": str(tmp_path),
        "clock_file": "/c", "turn_deadline_s": 30, "model_manifest": {"path": "m", "sha256": "s"},
        "pilot_seed_launcher_config": {"prompts": {}}}, "pin"))
    monkeypatch.setattr(PR, "approved_path", lambda p: tmp_path)
    monkeypatch.setattr(PR.pilot_models, "load", lambda p, s: {"seats": {}})
    seen = {}
    monkeypatch.setattr(PR, "probe_pass", lambda **kw: seen.update(kw) or "front")
    rt = PR.build(store_path="s", ledger_path=tmp_path / "ledger.db", config_path="c", guard_path="g")
    rt["tasks"]()
    return rt, seen


def test_write_fence_is_compare_and_write_on_the_real_lease(monkeypatch, tmp_path):
    from sniper.office.supervisor import Lease
    office = S.open_store(tmp_path / "office.db")
    clock = [1000.0]
    lease = Lease(office, clock=lambda: clock[0], ttl_s=31)
    base = {"supervisor": SimpleNamespace(conn=office, lease=lease), "scheduler": None, "transports": {},
            "deadline_s": 30, "max_steps": 4, "tasks": lambda: [], "close": lambda: None}
    rt, seen = factory(monkeypatch, tmp_path, base)
    fence, ledger = seen["write_fence"], seen["ledger_conn"]
    write = lambda: ledger.execute("INSERT INTO capital_flows(book_id, kind, usd, at, actor, note) "
                                   "VALUES('b','topup',1,0,'t','n')")
    with pytest.raises(P.ProbeRetry, match="lease_unknown"):
        with fence():
            write()
    gen = lease.acquire("me")
    rt["initialize"](gen)
    with fence():
        write()                                            # lease ours at commit: committed
    assert ledger.execute("SELECT COUNT(*) FROM capital_flows").fetchone()[0] == 1
    with pytest.raises(P.ProbeRetry, match="lease_lost"):
        with fence():
            write()
            clock[0] += 40                                 # lease expires mid-write: the commit must fail
    assert ledger.execute("SELECT COUNT(*) FROM capital_flows").fetchone()[0] == 1
    clock[0] += 1
    gen2 = Lease(office, clock=lambda: clock[0], ttl_s=31).acquire("other")       # takeover: generation changed
    assert gen2 != gen
    with pytest.raises(P.ProbeRetry, match="lease_lost"):
        with fence():
            write()
    assert ledger.execute("SELECT COUNT(*) FROM capital_flows").fetchone()[0] == 1
    assert seen["lease_left"]() > 0                        # the other holder's lease, read from the row
    office.close()


def test_no_model_call_starts_when_the_lease_cannot_outlive_it(world):
    world.write_input()
    assert world.run(lease_left=lambda: 5.0) == "retry" and world.sched.calls == []      # budget 30 > 5 left
    assert "stop" not in steps(world)
    assert world.run(lease_left=lambda: 100.0) == "front"


def test_trader_committed_before_its_report_is_recovered_without_new_calls(world, monkeypatch):
    world.write_input()
    real = P.trader_link
    def commit_then_crash(*a, **k):
        real(*a, **k)
        raise RuntimeError("crash after trader commit")
    monkeypatch.setattr(P, "trader_link", commit_then_crash)
    with pytest.raises(RuntimeError):
        world.run()
    monkeypatch.setattr(P, "trader_link", real)
    calls = list(world.sched.calls)
    assert world.run() == "front" and world.sched.calls == calls      # judges reused, receipt returned idempotently
    assert steps(world).count("trader") == 1


def test_lease_lost_before_trader_writes_nothing_and_is_not_terminal(world):
    import contextlib
    world.write_input()
    n = {"k": 0}

    @contextlib.contextmanager
    def fence():
        n["k"] += 1
        if n["k"] >= 3:                                    # bootstrap and sheet commit; lost at the trader
            raise P.ProbeRetry("lease_lost")
        yield
    assert world.run(write_fence=fence) == "retry"
    assert world.ledger.execute("SELECT COUNT(*) FROM decision_receipts").fetchone()[0] == 0
    assert "stop" not in steps(world) and world.run() == "front"


# ---- Zeta FAIL af4434702 witnesses (verbatim from ~/.cache/codex/zeta-probe-af4434702)
def test_zeta_completed_judges_survive_front_crash(world,monkeypatch):
    world.write_input()
    calls=[]
    def make_call(*a,**k):
        def call(slot,user,schema):
            repeated=slot in calls
            calls.append(slot)
            return env(outcome='partial' if repeated else 'success',served=SERVED[slot])
        return call
    monkeypatch.setattr(P,'judge_call_over',make_call)
    real_risk=P.risk_link
    def crash(*a,**k): raise RuntimeError('crash after saved judges')
    monkeypatch.setattr(P,'risk_link',crash)
    with pytest.raises(RuntimeError):world.run()
    assert 'judges' in steps(world) and 'trader' not in steps(world)
    monkeypatch.setattr(P,'risk_link',real_risk)
    assert world.run()=='front', 'saved known judges were lost to duplicate-request refusal'
    assert calls==list(P.JUDGE_SLOTS)


def test_zeta_probe_does_not_write_after_lease_expires(world,monkeypatch):
    from sniper.office.supervisor import Lease
    world.write_input()
    office=S.open_store(world.probe_dir.parent/'office.db')
    clock=[CALL_NOW]
    lease=Lease(office,clock=lambda:clock[0],ttl_s=31)
    gen=lease.acquire('probe-owner')
    def make_call(*a,**k):
        def call(slot,user,schema):
            clock[0]+=20
            return env(served=SERVED[slot])
        return call
    monkeypatch.setattr(P,'judge_call_over',make_call)
    world.run(now_host=lambda:clock[0])
    assert clock[0]>CALL_NOW+31
    assert world.ledger.execute('SELECT COUNT(*) FROM decision_receipts').fetchone()[0]==0, 'stale lease wrote trader receipt'
    office.close()


# ---- Zeta FAIL b8a5fe463 witnesses (verbatim from ~/.cache/codex/zeta-probe-b8a5fe463)
def test_zeta_saved_judges_resume_after_clock_sample_refresh(world,monkeypatch):
    world.write_input()
    original=P.risk_link
    monkeypatch.setattr(P,'risk_link',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('crash')))
    with pytest.raises(RuntimeError):world.run()
    monkeypatch.setattr(P,'risk_link',original)
    assert world.run(clock_reader=lambda f,now:(0.25,CALL_NOW-9))=='front'


def test_zeta_retry_between_judges_keeps_completed_answer(world,monkeypatch):
    world.write_input()
    seen=[]
    interrupted=[False]
    def factory(*a,**k):
        def call(slot,user,schema):
            if slot=='judge_gpt' and not interrupted[0]:
                interrupted[0]=True
                raise P.ProbeRetry('phase_deadline')
            duplicate=slot in seen
            seen.append(slot)
            return env(outcome='partial' if duplicate else 'success',served=SERVED[slot])
        return call
    monkeypatch.setattr(P,'judge_call_over',factory)
    assert world.run()=='retry'
    assert world.run()=='front', 'completed Claude answer lost on phase retry'
    assert seen.count('judge_claude')==1

def test_zeta_takeover_between_last_check_and_trader_cannot_commit(world,monkeypatch):
    from sniper.office.supervisor import Lease
    world.write_input()
    office=S.open_store(world.probe_dir.parent/'office.db')
    ticks=[CALL_NOW]
    old=Lease(office,clock=lambda:ticks[0],ttl_s=31)
    gen=old.acquire('old-owner')
    original=P.trader_link
    def takeover(*a,**k):
        ticks[0]+=32
        successor=Lease(office,clock=lambda:ticks[0],ttl_s=31)
        assert successor.acquire('new-owner')>gen
        return original(*a,**k)
    monkeypatch.setattr(P,'trader_link',takeover)
    world.run(fence=lambda:old.check(gen),now_host=lambda:ticks[0])
    assert world.ledger.execute('SELECT COUNT(*) FROM decision_receipts').fetchone()[0]==0,'old generation committed after takeover'
    office.close()
