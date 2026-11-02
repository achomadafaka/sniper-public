"""Task 10 loops (Lambda): stable turn ids for Codex's Supervisor.tasks(), role cadence, runaway guard, Head floor rhythm. Pure."""
import hashlib
from pathlib import Path

import pytest

from sniper.office import loops as LP

CFG = Path(__file__).resolve().parents[2] / "config" / "office" / "cadence.json"
T = 1_800_000_000.0


def cfg():
    return LP.load(CFG, hashlib.sha256(CFG.read_bytes()).hexdigest())


def agent(aid="a1", role="Scout", gen=1, state="active"):
    return {"agent_id": aid, "role": role, "generation": gen, "state": state}


def test_config_pinned(tmp_path):
    other = tmp_path / "c.json"
    other.write_text(CFG.read_text().replace('"Judge": 60', '"Judge": 1'))
    with pytest.raises(LP.ConfigMismatch):
        LP.load(other, hashlib.sha256(CFG.read_bytes()).hexdigest())


def test_turn_id_is_stable_across_a_restart_and_changes_with_slot_and_generation():
    c = cfg()
    a = LP.due_turns(c, [agent()], now=T + 5, done_turn_ids=set())
    b = LP.due_turns(c, [agent()], now=T + 7, done_turn_ids=set())          # same 120 s slot, later in it
    assert a == b and len(a) == 1 and a[0]["agent_id"] == "a1"
    nxt = LP.due_turns(c, [agent()], now=T + 125, done_turn_ids=set())
    assert nxt[0]["turn_id"] != a[0]["turn_id"]
    assert LP.due_turns(c, [agent(gen=2)], now=T + 5, done_turn_ids=set())[0]["turn_id"] != a[0]["turn_id"]


def test_a_done_turn_is_not_due_again_and_archived_agents_get_no_turns():
    c = cfg()
    first = LP.due_turns(c, [agent()], now=T + 5, done_turn_ids=set())
    assert LP.due_turns(c, [agent()], now=T + 5, done_turn_ids={first[0]["turn_id"]}) == []
    assert LP.due_turns(c, [agent(state="archived")], now=T + 5, done_turn_ids=set()) == []


def test_unknown_role_is_refused_not_given_a_default_cadence():
    with pytest.raises(LP.UnknownRole):
        LP.due_turns(cfg(), [agent(role="Wizard")], now=T, done_turn_ids=set())


def test_runaway_guard_pauses_an_agent_over_the_hourly_cap_and_names_it():
    c = cfg()
    calls = {"a1": [T + i for i in range(241)], "a2": [T + i for i in range(10)]}
    over = LP.runaway(c, calls, now=T + 300)
    assert over == [{"agent_id": "a1", "turns_last_hour": 241, "cap": 240}]
    assert LP.runaway(c, {"a1": [T - 7200]}, now=T) == []                    # old calls fall out of the hour


def test_head_rhythm_names_the_three_daily_events_on_utc_hours():
    c = cfg()
    day0 = T // 86400 * 86400
    assert LP.floor_rhythm(c, day0 + 0 * 3600 + 5) == "morning_plan"
    assert LP.floor_rhythm(c, day0 + 8 * 3600 + 5) == "midday_check"
    assert LP.floor_rhythm(c, day0 + 16 * 3600 + 5) == "end_of_day_review"
    assert LP.floor_rhythm(c, day0 + 12 * 3600) is None
