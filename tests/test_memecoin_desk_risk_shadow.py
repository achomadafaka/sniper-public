"""Desk RISK seat Phase 0 shadow: boundaries, risk-break-v1, tighten-only validation, every decision
recorded (HOLD, cooldown, errors included), nothing ever applied (fake model, temporary DBs only)."""
import hashlib
import importlib.util
import json
import pytest
import sqlite3
from pathlib import Path

from sniper.services import memecoin_desk_risk as R

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("risk_shadow", ROOT / "scripts" / "memecoin_desk_risk_shadow.py")
RW = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(RW)

POLICY = {"stop_pct": 20.0, "trail_pct": 15.0, "tp_pct": 60.0, "catastrophe_pct": 50.0, "max_hold_s": 3600.0}
BOUNDS = {"tp_pct": (15, 150), "sl_pct": (10, 40), "trail_pct": (8, 30)}
OPENED = "2026-10-21T13:00:00+00:00"
T0 = 1792587600.0


def _v(obj):
    return R.validate(json.dumps(obj), key="pos:1", policy=POLICY, bounds=BOUNDS)


def test_only_a_strict_tightening_inside_the_bounds_is_a_tighten():
    assert _v({"k": "pos:1", "a": "H", "r": None, "c": 2})[:3] == ("HOLD", None, "hold")
    assert _v({"k": "pos:1", "a": "E", "r": None, "c": 2})[:3] == ("EXIT", None, "exit")
    assert _v({"k": "pos:1", "a": "T", "r": {"sl_pct": 12, "trail_pct": 10}, "c": 3})[:2] == (
        "TIGHTEN", {"sl_pct": 12.0, "trail_pct": 10.0})
    for rails, why in (({"sl_pct": 25}, "tighten_loosens:sl_pct"), ({"sl_pct": 20}, "tighten_loosens:sl_pct"),
                       ({"trail_pct": 0}, "tighten_loosens:trail_pct"), ({"tp_pct": None}, "tighten_loosens:tp_pct"),
                       ({"tp_pct": 70}, "tighten_loosens:tp_pct"), ({"sl_pct": 5}, "out_of_bounds:sl_pct"),
                       ({"derisk_pct": 10}, "tighten_loosens:derisk_pct"), ({"sl_pct": True}, "tighten_loosens:sl_pct")):
        assert _v({"k": "pos:1", "a": "T", "r": rails, "c": 3})[:3] == ("HOLD", None, why), rails
    assert R.validate(json.dumps({"k": "pos:1", "a": "T", "r": {"trail_pct": 10}, "c": 1}), key="pos:1",
                      policy={**POLICY, "trail_pct": 0.0}, bounds=BOUNDS)[0] == "TIGHTEN"   # adding a trail
    assert _v({"k": "pos:2", "a": "E", "r": None, "c": 1})[2] == "key_mismatch"
    assert R.validate("EXIT now", key="pos:1", policy=POLICY, bounds=BOUNDS)[:3] == ("HOLD", None, "malformed_json")


def _bars(closes, *, low=1.0, start=0, gap_at=None):
    out, t = [], start
    for i, c in enumerate(closes):
        if gap_at is not None and i == gap_at:
            t += 1
        out.append({"t": t, "o": c, "h": c, "l": min(c, low), "c": c})
        t += 1
    return out


def test_risk_break_v1_is_three_consecutive_closes_under_the_prior_60s_low_and_a_gap_resets():
    base = [1.0] * 60
    assert R.structure_break(_bars(base + [0.9, 0.9, 0.9])) == 60
    assert R.structure_break(_bars(base + [0.9, 0.9])) is None                     # two is not three
    assert R.structure_break(_bars(base + [0.9, 0.9, 0.9], gap_at=61)) is None      # a missing second
    assert R.structure_break(_bars(base + [0.9, 1.0, 0.9, 0.9])) is None            # not consecutive under
    longer = _bars(base + [0.9] * 6)
    assert R.structure_break(longer[:-3]) == 60
    assert R.structure_break(longer) is None          # the streak's own lows are now the prior low: no re-fire
    assert R.structure_break(_bars(base + [0.9] * 4 + [0.8] * 3)) == 64             # a new, lower break


def test_boundaries_fire_once_each():
    pos = {"id": 1, "opened_at": OPENED}
    now = T0 + 10
    due = R.boundaries(pos, POLICY, now=now, seen=set(), rail_fill_ids=[7], bars=[])
    assert due == [("fill", "fill"), ("rail_hit", "7")]
    assert R.boundaries(pos, POLICY, now=now, seen=set(due), rail_fill_ids=[7], bars=[]) == []
    late = R.boundaries(pos, POLICY, now=T0 + 3600 - 59, seen=set(due), rail_fill_ids=[7], bars=[])
    assert late == [("timecap", "timecap")]


def test_facts_are_numbers_and_enums_and_absent_is_null_never_zero():
    pos = {"id": 1, "opened_at": OPENED, "entry_notional": 25.0}
    fresh = {"sweep_ts": "2026-10-21T13:01:00+00:00", "executable_exit_value": 30.0}
    f = R.facts({**pos, "entry_price": 2.0}, POLICY, mark=fresh, bars=_bars([2.0, 2.2]), boundary="fill",
                now=T0 + 65, sol_usd=1.0)
    assert f["pnl_pct_exact"] == 20.0 and f["bars_vs_fill"][1]["c"] == 1.1 and f["k"] == "pos:1"
    stale = R.facts(pos, POLICY, mark=fresh, bars=[], boundary="fill", now=T0 + 600)
    assert stale["pnl_pct_exact"] is None and stale["bars_vs_fill"] == []
    assert R.facts(pos, POLICY, mark=None, bars=[], boundary="fill", now=T0 + 65)["pnl_pct_exact"] is None


def _pass(tmp_path, monkeypatch, call, positions, now):
    monkeypatch.setattr(RW, "open_desk_positions", lambda ledger_db: positions)
    monkeypatch.setattr(RW, "tape_bars", lambda *a, **k: [])
    return RW.run_pass(tmp_path / "ledger.db", tmp_path, tmp_path / "risk.db", call_model=call,
                       now_fn=lambda: now, bounds=BOUNDS)


def _rows(tmp_path):
    conn = sqlite3.connect(tmp_path / "risk.db")
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM decisions ORDER BY id")]
    finally:
        conn.close()


def test_every_boundary_is_recorded_once_with_a_cooldown_and_nothing_is_applied(tmp_path, monkeypatch):
    pos = {"id": 1, "mint": "M", "opened_at": OPENED, "entry_notional": 25.0, "executed_pool": "P"}
    calls = []

    def call(system, user):
        calls.append(json.loads(user))
        return {"text": json.dumps({"k": "pos:1", "a": "T", "r": {"sl_pct": 12}, "c": 3}), "is_error": False,
                "call_error": None, "model_echo": R.MODEL, "session_id": "s", "result_id": "r"}
    stats = _pass(tmp_path, monkeypatch, call, [(pos, POLICY, [7], None)], T0 + 10)
    assert stats == {"positions": 1, "boundaries": 2, "asked": 1, "cooldown": 1, "no_policy": 0}
    rows = _rows(tmp_path)
    assert [(r["boundary"], r["action"], r["call_error"]) for r in rows] == [
        ("fill", "TIGHTEN", None), ("rail_hit", None, "cooldown")]                # denominator kept
    first = rows[0]
    assert json.loads(first["rails_json"]) == {"sl_pct": 12.0} and first["inputs_hash"] == R.inputs_hash(calls[0])
    assert first["risk_version"] == "desk-risk-shadow-v3" and first["structure_rule_version"] == "risk-break-v1"
    assert first["decided_at"] is not None and first["expired"] == 0
    _pass(tmp_path, monkeypatch, call, [(pos, POLICY, [7], None)], T0 + 50)
    assert len(_rows(tmp_path)) == 2 and len(calls) == 1                         # never re-asked
    assert not (tmp_path / "ledger.db").exists()                                   # nothing applied anywhere


def test_a_dead_login_or_a_wrong_model_is_a_recorded_hold(tmp_path, monkeypatch):
    pos = {"id": 2, "mint": "M", "opened_at": OPENED, "entry_notional": 25.0, "executed_pool": "P"}
    _pass(tmp_path, monkeypatch, lambda s, u: {"text": "OAuth expired", "is_error": True, "call_error": None},
          [(pos, POLICY, [], None)], T0 + 10)
    (row,) = _rows(tmp_path)
    assert (row["action"], row["reason"], row["call_error"]) == ("HOLD", "call_failed", "model_result_error")
    _pass(tmp_path, monkeypatch, lambda s, u: {"text": "x", "call_error": None, "model_echo": "claude-sonnet-5-5"},
          [({**pos, "id": 3}, POLICY, [], None)], T0 + 10)
    assert _rows(tmp_path)[-1]["call_error"] == "model_mismatch"


def test_the_shadow_never_writes_the_ledger(tmp_path):
    from sniper.persistence import db_memecoin as ledger
    db = tmp_path / "ledger.db"
    conn = sqlite3.connect(db)
    ledger.ensure_memecoin_tables(conn)
    conn.execute("INSERT INTO memecoin_positions(id, mint, entry_price, entry_notional, opened_at, execution_mode, "
                 "signal_source, network_id, is_open) VALUES(1,'M',1,25,?, 'real','desk','solana',1)", (OPENED,))
    conn.commit()
    conn.close()
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    stats = RW.run_pass(db, tmp_path, tmp_path / "risk.db", call_model=lambda s, u: {}, now_fn=lambda: T0 + 10)
    assert stats["no_policy"] == 1 and hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert _rows(tmp_path)[0]["call_error"] == "no_frozen_policy"


def test_main_pops_the_api_key_and_honours_the_kill_knob(tmp_path, monkeypatch, capsys):
    import os
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fixture")
    monkeypatch.setenv("MEMECOIN_DESK_RISK_SHADOW_ENABLED", "0")
    assert RW.main(["--once"]) == 0 and "ANTHROPIC_API_KEY" not in os.environ


def test_a_dated_snapshot_of_the_model_is_accepted_and_the_raw_echo_is_stored(tmp_path, monkeypatch):
    legacy = sqlite3.connect(tmp_path / "risk.db")              # a decisions table from before model_echo
    legacy.execute(RW.DDL.replace(" model_echo TEXT,", ""))
    legacy.commit()
    legacy.close()
    pos = {"id": 4, "mint": "M", "opened_at": OPENED, "entry_notional": 25.0, "executed_pool": "P"}
    for pid, echo, error in ((4, R.MODEL, None), (5, R.MODEL + "-20261015", None), (6, None, None),
                             (7, "claude-sonnet-5-5", "model_mismatch"), (8, R.MODEL + "x", "model_mismatch"),
                             (9, R.MODEL + "-anything", "model_mismatch")):
        def call(system, user, echo=echo, pid=pid):
            return {"text": json.dumps({"k": "pos:%d" % pid, "a": "H", "c": 3}), "is_error": False,
                    "call_error": None, "model_echo": echo}
        _pass(tmp_path, monkeypatch, call, [({**pos, "id": pid}, POLICY, [], None)], T0 + 10)
        row = _rows(tmp_path)[-1]
        assert (row["position_id"], row["call_error"], row["model_echo"]) == (pid, error, echo), echo


@pytest.mark.parametrize("res,err", [({"text": None, "is_error": True, "model_echo": R.MODEL}, "model_result_error"),
                                     ({"text": "x", "is_error": False, "model_echo": "nonstr:list"}, "model_mismatch")])
def test_adapter_shaped_container_results_record_a_failed_row_never_raise(tmp_path, monkeypatch, res, err):
    pos = {"id": 1, "mint": "M", "opened_at": OPENED, "entry_notional": 25.0, "executed_pool": "P"}
    _pass(tmp_path, monkeypatch, lambda s, u: dict(res, call_error=None), [(pos, POLICY, [], None)], T0 + 10)
    rows = _rows(tmp_path)
    assert rows and all(r["call_error"] == err and r["action"] == "HOLD" for r in rows if r["call_error"] != "cooldown")
    assert rows[0]["model_echo"] == res["model_echo"]            # the raw/marker echo is stored as given


def _real_adapter(monkeypatch, tmp_path, *, echo, text):
    """The shared adapter with only the SDK transport faked (Codex 2026-10-22: real-adapter regressions)."""
    import asyncio
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
    return lambda system, user: jw.sdk_call_model(system, user)


@pytest.mark.parametrize("echo,text,err", [(["m"], '{"k":"pos:1","a":"H","c":1}', "model_mismatch"),
                                           ("claude-haiku-5-5", ["a"], "model_result_error"),
                                           ("claude-haiku-5-5", 7, "model_result_error")])
def test_real_adapter_container_echo_or_text_records_a_hold_row_in_risk(tmp_path, monkeypatch, echo, text, err):
    pos = {"id": 1, "mint": "M", "opened_at": OPENED, "entry_notional": 25.0, "executed_pool": "P"}
    _pass(tmp_path, monkeypatch, _real_adapter(monkeypatch, tmp_path, echo=echo, text=text),
          [(pos, POLICY, [], None)], T0 + 10)
    asked = [r for r in _rows(tmp_path) if r["call_error"] != "cooldown"]
    assert asked and all(r["call_error"] == err and r["action"] == "HOLD" for r in asked)
