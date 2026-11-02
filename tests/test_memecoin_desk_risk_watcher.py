"""Desk RISK-live watcher: one call per cycle over every open position, a row per position per cycle,
failures are HOLDs with a cause, nothing but its own DB is written (fake model, temp DBs only)."""
import importlib.util
import json
import sqlite3
import zlib
from pathlib import Path

import pytest

from sniper.services import memecoin_desk_risk as R
from sniper.services import memecoin_desk_risk_live as L

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("risk_watcher", ROOT / "scripts" / "memecoin_desk_risk_watcher.py")
W = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(W)

POLICY = {"stop_pct": 20.0, "trail_pct": 15.0, "tp_pct": 60.0, "catastrophe_pct": 50.0, "max_hold_s": 0}
BOUNDS = {"tp_pct": (15, 150), "sl_pct": (10, 40), "trail_pct": (8, 30)}
OPENED = "2026-10-22T09:00:00+00:00"
T0 = 1792659600.0


def pos(i):
    return {"id": i, "mint": "M%d" % i, "opened_at": OPENED, "entry_notional": 25.0, "executed_pool": "P%d" % i,
            "network_id": "solana"}


def flat_bars(n=80, close=1.0):
    return [{"t": int(T0 + i), "o": 1.0, "h": 1.01, "l": 0.99, "c": close, "buy_usd": 1.0, "sell_usd": 1.0,
             "n_makers": 2} for i in range(n)]


def broken_bars():
    """risk-break-v2: a coin with ordinary noise (about 2% a bar), then a complete 5 s bucket closing 20% under its support with sellers dominating."""
    noisy = [{"t": int(T0 + i), "o": 1.0, "h": 1.01, "l": 0.99, "c": 1.0, "buy_usd": 1.0, "sell_usd": 1.0, "n_makers": 2} for i in range(70)]
    return noisy + [{"t": int(T0 + 70 + i), "o": 0.9, "h": 0.9, "l": 0.78, "c": 0.8, "buy_usd": 1.0, "sell_usd": 9.0, "n_makers": 2} for i in range(8)]


class Env:
    def __init__(self, tmp_path, monkeypatch, positions, bars=None, policy=POLICY):
        self.tmp, self.out = tmp_path, tmp_path / "risk_live.db"
        self.positions, self.policy = positions, policy
        self.bars = bars if bars is not None else flat_bars()
        self.t = T0 + 80.0
        self.calls, self.alerts, self.state = [], [], W.new_state()
        self.answer = lambda blocks: json.dumps([])
        monkeypatch.setattr(W, "bars_since", lambda *a, **k: self.bars)
        monkeypatch.setattr(W, "capture_healthy", lambda *a: True)                     # the capture proof has its own tests (test_desk_risk_break_v2)
        monkeypatch.setattr(W, "capture_proof", lambda _d, now, *_a, **_kw: {"from": T0 - 600, "through": now - 1})

    def model(self, system, user):
        blocks = json.loads(user)["pos"]
        self.calls.append(blocks)
        return {"text": self.answer(blocks), "is_error": False, "call_error": None, "model_echo": R.MODEL,
                "session_id": "s", "result_id": "r", "usage": {"output_tokens": 9}, "notional_usd": 0.001}

    def tick(self, dt=0.0, **kw):
        self.t += dt
        return W.run_pass(self.tmp / "ledger.db", self.tmp, self.out, call_model=self.model, now_fn=lambda: self.t,
                          bounds=BOUNDS, state=self.state, alert_fn=lambda k, t: self.alerts.append(k),
                          positions_fn=lambda _db: [(p, self.policy, [], None) for p in self.positions], **kw)

    def rows(self, table="risk_decisions"):
        c = sqlite3.connect(self.out)
        c.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in c.execute("SELECT * FROM %s ORDER BY id" % table)]
        finally:
            c.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch, [pos(1), pos(2)])


def test_no_open_position_means_no_call_and_no_rows(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch, [])
    assert e.tick()["cycle"] is False and e.calls == [] and not e.out.exists()


def test_one_call_covers_every_position_and_each_gets_a_row(env):
    env.answer = lambda blocks: json.dumps([{"k": "pos:1", "a": "E", "r": None, "c": 2}])
    stats = env.tick()
    assert stats["calls"] == 1 and len(env.calls) == 1 and [b["k"] for b in env.calls[0]] == ["pos:1", "pos:2"]
    rows = env.rows()
    assert [(r["key"], r["action"], r["reason"]) for r in rows] == [("pos:1", "EXIT", "exit"),
                                                                    ("pos:2", "HOLD", "omitted")]
    assert rows[0]["expires_at"] == pytest.approx(rows[0]["decided_at"] + L.ORDER_TTL_S) and rows[1]["expires_at"] is None
    call = env.rows("risk_calls")[0]
    assert call["model_echo"] == R.MODEL and call["live"] == 1 and call["n_positions"] == 2
    assert json.loads(zlib.decompress(call["user_z"]))["pos"] == env.calls[0]                  # the audit input, exactly
    assert call["prompt_hash"] == L.inputs_hash(env.calls[0], BOUNDS) and rows[0]["call_id"] == call["id"]
    assert rows[0]["inputs_hash"] == L.block_hash(env.calls[0][0])


def test_the_cycle_period_and_the_event_wake(env, monkeypatch):
    monkeypatch.setattr(L, "CYCLE_S", 15.0)                                            # the wake logic at a long period
    assert env.tick()["cycle"] is True
    assert env.tick(5.0)["cycle"] is False and env.tick(5.0)["cycle"] is False        # 10 s in: not due
    assert env.tick(6.0)["cycle"] is True                                              # 16 s: due
    env.bars = broken_bars()                                                           # a new structure break
    assert env.tick(2.0)["cycle"] is False                                             # inside EVENT_MIN_GAP_S
    assert env.tick(4.0)["cycle"] is True                                              # woke early (6 s after the last)
    assert env.tick(6.0)["cycle"] is False                                             # the same break never re-wakes


def test_a_call_error_is_a_hold_row_per_position_with_the_cause(env):
    env.model = lambda s, u: {"text": None, "is_error": True, "call_error": "provider_error", "model_echo": None}
    stats = env.tick()
    assert stats["failed_calls"] == 1
    rows = env.rows()
    assert [(r["action"], r["call_error"]) for r in rows] == [("HOLD", "provider_error")] * 2 and rows[0]["expires_at"] is None


def test_auth_or_rate_limit_backs_off_for_a_minute_without_calling(env):
    calls = []
    env.model = lambda s, u: calls.append(u) or {"text": None, "is_error": True, "call_error": "rate_limited", "model_echo": None}
    env.tick()
    stats = env.tick(L.CYCLE_S + 0.1)
    assert len(calls) == 1 and stats["skipped"] == 2
    assert [r["call_error"] for r in env.rows()][-2:] == ["backoff", "backoff"]
    env.model = lambda s, u: calls.append(u) or {"text": "[]", "is_error": False, "call_error": None, "model_echo": R.MODEL}
    env.tick(60.0)
    assert len(calls) == 2


def test_the_hourly_budget_is_a_hold_not_a_call(env):
    env.tick(hourly_cap=1)
    stats = env.tick(L.CYCLE_S + 0.1, hourly_cap=1)
    assert stats["calls"] == 0 and stats["skipped"] == 2
    assert {r["call_error"] for r in env.rows()[-2:]} == {"budget_exhausted"}
    assert len(env.rows("risk_calls")) == 1


def test_another_model_or_a_container_echo_is_a_hold(env):
    for echo in ("claude-sonnet-5-5", "nonstr:list:['claude-haiku-5-5']"):
        env.model = lambda s, u, echo=echo: {"text": json.dumps([{"k": "pos:1", "a": "E", "r": None, "c": 1}]),
                                              "is_error": False, "call_error": None, "model_echo": echo}
        env.tick(L.CYCLE_S + 1)
        assert {(r["action"], r["call_error"]) for r in env.rows()[-2:]} == {("HOLD", "model_mismatch")}


def test_six_failed_cycles_page_once_and_a_good_cycle_resets(env):
    env.model = lambda s, u: {"text": None, "is_error": True, "call_error": "timeout", "model_echo": None}
    for _ in range(8):
        env.tick(L.CYCLE_S + 1)
    assert env.alerts == ["desk_risk_blind"]
    env.model = lambda s, u: {"text": "[]", "is_error": False, "call_error": None, "model_echo": R.MODEL}
    env.tick(L.CYCLE_S + 1)
    assert env.state["consec_fail"] == 0 and env.state["paged"] is False


def test_no_frozen_policy_is_a_row_and_no_call(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch, [pos(1)], policy=None)
    e.tick()
    assert e.calls == [] and e.rows()[0]["call_error"] == "no_frozen_policy"


def test_live_off_still_records_but_marks_the_rows_not_live(env):
    env.answer = lambda blocks: json.dumps([{"k": "pos:1", "a": "E", "r": None, "c": 2}])
    env.tick(live=False)
    assert {r["live"] for r in env.rows()} == {0} and env.rows("risk_calls")[0]["live"] == 0


def test_more_than_the_per_call_cap_takes_a_second_call(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch, [pos(i) for i in range(1, L.MAX_POSITIONS + 4)])
    assert e.tick()["calls"] == 2 and [len(c) for c in e.calls] == [L.MAX_POSITIONS, 3]
    assert len(e.rows()) == L.MAX_POSITIONS + 3


def test_a_replayed_cycle_never_duplicates_a_decision_row(env):
    env.tick()
    c = sqlite3.connect(env.out)
    first = c.execute("SELECT COUNT(*) FROM risk_decisions").fetchone()[0]
    W._row(c, env.t, pos(1), "pos:1")                          # same (cycle_at, position): ignored
    c.commit()
    assert c.execute("SELECT COUNT(*) FROM risk_decisions").fetchone()[0] == first


def _real_adapter(monkeypatch, tmp_path, *, echo, text):
    """The shared adapter with only the SDK transport faked (Codex 2026-10-22: real-adapter regressions)."""
    import claude_agent_sdk as sdk
    jw_spec = importlib.util.spec_from_file_location("jw_real", ROOT / "scripts/memecoin_desk_judge_worker.py")
    jw = importlib.util.module_from_spec(jw_spec)
    jw_spec.loader.exec_module(jw)
    monkeypatch.setattr(jw, "AUTH_PAGE_STATE", tmp_path / "auth_page.json")
    a = sdk.AssistantMessage.__new__(sdk.AssistantMessage)
    for k in sdk.AssistantMessage.__dataclass_fields__:
        setattr(a, k, None)
    a.error, a.model, a.content = None, echo, []
    r = sdk.ResultMessage.__new__(sdk.ResultMessage)
    for k in sdk.ResultMessage.__dataclass_fields__:
        setattr(r, k, None)
    r.subtype, r.is_error, r.session_id, r.uuid, r.result = "success", False, "s1", "r-1", text
    r.total_cost_usd, r.usage = 0.0004, {"output_tokens": 60}

    async def q(prompt, options):
        for m in (a, r):
            yield m
    monkeypatch.setattr(sdk, "query", q)
    return lambda system, user: jw.sdk_call_model(system, user, deadline_s=L.DEADLINE_S, model=L.MODEL)


@pytest.mark.parametrize("echo,text,err", [(R.MODEL, '[{"k":"pos:1","a":"E","r":null,"c":1}]', None),
                                           (R.MODEL, ["a"], "model_result_error"),
                                           (R.MODEL, 7, "model_result_error"),
                                           (["m"], '[{"k":"pos:1","a":"E","r":null,"c":1}]', "model_mismatch"),
                                           ("\ud800", '[{"k":"pos:1","a":"E","r":null,"c":1}]', "model_mismatch")])
def test_through_the_real_adapter_a_bad_text_or_echo_is_a_hold_row_never_an_exception(env, monkeypatch, tmp_path, echo, text, err):
    env.model = _real_adapter(monkeypatch, tmp_path, echo=echo, text=text)
    env.tick()
    rows = {r["key"]: r for r in env.rows()}
    assert rows["pos:1"]["call_error"] == err
    assert rows["pos:1"]["action"] == ("EXIT" if err is None else "HOLD") and rows["pos:2"]["action"] == "HOLD"


def test_the_cycle_period_runs_from_the_end_of_the_calls_so_a_slow_call_never_overlaps(env, monkeypatch):
    monkeypatch.setattr(L, "CYCLE_S", 15.0)
    def slow(system, user):
        env.t += 12.0                                               # a 12 s call (the new deadline)
        return {"text": "[]", "is_error": False, "call_error": None, "model_echo": R.MODEL}
    env.model = slow
    assert env.tick()["cycle"] is True
    assert env.tick(8.0)["cycle"] is False                           # 8 s after the end: not due
    assert env.tick(6.0)["cycle"] is False                           # 14 s after the end: not due
    assert env.tick(1.5)["cycle"] is True                            # 15.5 s after the end


def test_the_deadline_is_twelve_seconds_and_the_order_ttl_runs_from_the_response():
    assert L.DEADLINE_S == 12.0 and L.ORDER_TTL_S == 10.0 and L.CYCLE_S == 5.0       # v4: ~5 s (was 15)


def test_tape_state_is_recorded_ok_or_none(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch, [pos(1)])
    e.positions[0]["entry_price"] = 1.0
    (tmp_path / "universe.heartbeat").write_text(json.dumps(
        {"ts": e.t, "connected": True, "sol_usd": 1.0, "sol_age_s": 0.0}))
    e.tick()
    assert e.rows()[0]["tape_state"] == "ok"
    e2 = Env(tmp_path / "b", monkeypatch, [pos(1)], bars=[])
    (tmp_path / "b").mkdir()
    e2.tick()
    assert e2.rows()[0]["tape_state"] == "none"


def test_between_cycles_the_tape_is_probed_every_few_seconds_not_every_tick(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch, [pos(1)])
    reads = []
    monkeypatch.setattr(W, "bars_since", lambda *a, **k: reads.append(1) or e.bars)
    e.tick()                                                       # cycle: one read
    for _ in range(5):
        e.tick(0.5)                                                # 2.5 s of 0.5 s ticks: no probe yet
    assert len(reads) == 1
    e.tick(1.0)                                                    # 3.5 s after the cycle: a probe
    assert len(reads) == 2


def test_v4_rows_carry_basis_cadence_signal_and_mixed_version(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch, [pos(1)])
    e.state = W.new_state(started_at=T0 - 86400 * 2)              # the watcher started before the position opened
    e.tick()
    r = e.rows()[0]
    assert (r["risk_version"], r["pnl_basis"], r["cadence_s"], r["attention_signal"], r["risk_version_mixed"]) == (
        L.RISK_LIVE_VERSION, "no_exact_quote", L.CYCLE_S, None, 0)
    assert r["pnl_pct_exact"] is None
    e2 = Env(tmp_path / "b", monkeypatch, [pos(1)])
    (tmp_path / "b").mkdir()
    e2.state = W.new_state(started_at=T0 + 1e6)                    # opened before this process started
    e2.tick()
    assert e2.rows()[0]["risk_version_mixed"] == 1


def test_a_fired_attention_event_wakes_the_cycle_at_once_and_reaches_the_prompt(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch, [pos(1)])
    events = []
    tick = lambda dt=0.0: e.tick(dt, attention_fn=lambda pid, now: list(events))   # noqa: E731
    assert tick()["cycle"] is True
    assert tick(1.0)["cycle"] is False                                   # 1 s after: not due, nothing fired
    events.append(("creator_sell", e.t + 1.0))
    assert tick(1.0)["cycle"] is True                                    # woke at once, inside the period
    assert e.calls[-1][0]["attn"] == [["creator_sell", 0]]
    assert e.rows()[-1]["attention_signal"] == "creator_sell"
    assert tick(1.0)["cycle"] is False                                   # the same event never re-wakes


def test_a_broken_attention_read_is_no_events_never_a_dead_pass(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch, [pos(1)])

    def boom(pid, now):
        raise RuntimeError("db gone")
    assert e.tick(attention_fn=boom)["cycle"] is True and e.calls[-1][0]["attn"] == []


def test_fired_attention_reads_only_fired_rows_of_both_files(tmp_path):
    import sqlite3
    from sniper.services import memecoin_desk_attention_store as S
    tape, runner = tmp_path / "tape.db", tmp_path / "runner.db"
    for path, rows in ((tape, [("a", "creator_sell", 1), ("b", "sell_burst", None)]), (runner, [("c", "quote_collapse", 1)])):
        c = S.connect(path)
        for i, sig, fired in rows:
            S.insert(c, event_id=i, position_id=7, mint="M", signal=sig, value={}, observed_at=T0, source="x",
                     fired=fired, thresholds_version="v")
        c.commit(); c.close()
    assert W.fired_attention(7, T0 + 5, paths=(tape, runner)) == [("creator_sell", T0), ("quote_collapse", T0)]
    assert W.fired_attention(7, T0 + W.ATTN_WINDOW_S + 1, paths=(tape, runner)) == []
    assert W.fired_attention(7, T0, paths=(tmp_path / "absent.db",)) == []
