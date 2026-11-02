"""Operator 8b4030a4: the hourly JUDGE cap is the only limit; when it binds, the lowest ranker score is
dropped, never the newest arrival (no first-come, no random slice, no quantile admission)."""
from __future__ import annotations

import json

import pytest
import sqlite3
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

from sniper.persistence import db_memecoin_desk as store
from sniper.services import memecoin_desk_judge as J
from sniper.services.memecoin_desk_ranker import MAX_LIFE_S
from test_memecoin_desk_pipeline import databases, api, AT, POLICY  # noqa: F401
from test_memecoin_desk_contract import evidence, policy
from test_memecoin_desk_judge import Clock, FakeGate, T0, enter, make_candidate, model_returning


class ScoredGate(FakeGate):
    def __init__(self, state, scores):
        super().__init__(state)
        self._scores = scores

    def scores(self, ids):
        return {i: self._scores[i] for i in ids if i in self._scores}


def _waiting(db, n):
    cohort = api().provision_cohort(*db, producer="U", policy={**POLICY, "judge_p_admit": 1.0,
                                    "select_version": J.SELECT_VERSION}, prompt_version="desk-judge-v6", created_at=AT)
    ids = []
    for i in range(n):                                   # oldest first: t000 passed first
        c = make_candidate(i, T0 + timedelta(minutes=1, seconds=i))
        at = (T0 + timedelta(minutes=1, seconds=i + 1)).isoformat()
        t = api().ingest_candidate(db[0], c, cohort_id=cohort, now_iso=at)
        api().evaluate_candidate(db[0], t["ticket_id"], evidence=evidence(c), policy=policy(), now_iso=at)
        ids.append(t["ticket_id"])
    return ids


def _run(db, gate, calls, cph, clock):
    return J.run_once(db[0], lambda: nullcontext(db[1]), call_model=model_returning(enter, calls=calls),
                      system_prompt="SYS", worker="w", now_fn=clock, calls_per_hour=cph, shortlist_fn=gate)


def _calls(db):
    return {t: json.loads(r) for t, r in db[0].execute("SELECT ticket_id, result_json FROM desk_judge_calls")}


def test_cap_binding_judges_the_highest_scores_and_defers_the_rest(databases):
    ids = _waiting(databases, 4)
    clock, calls = Clock(), []
    clock.t = T0 + timedelta(minutes=2)
    gate = ScoredGate(dict.fromkeys(ids, "shortlisted"), {ids[0]: 0.1, ids[1]: 0.9, ids[3]: 0.5})  # ids[2] unknown
    out = _run(databases, gate, calls, 2, clock)
    assert [c["opportunity_id"] for c in calls] == [ids[1], ids[3]]          # not the two oldest
    assert {o["ticket_id"] for o in out} == {ids[1], ids[3]}                  # the rest wait, unclaimed
    assert set(_calls(databases)) == {ids[1], ids[3]}
    # past MAX_LIFE with the hour still full: a no-call budget_exhausted with the tiebreak facts
    clock.t = T0 + timedelta(minutes=1) + timedelta(seconds=MAX_LIFE_S + 10)
    out = _run(databases, gate, calls, 2, clock)
    assert {o["ticket_id"]: o["reason"] for o in out} == {ids[0]: "judge_budget_exhausted",
                                                         ids[2]: "judge_budget_exhausted"}
    rows = _calls(databases)
    assert rows[ids[0]]["cap_tiebreak"] == {"score": 0.1, "calls_left": 0, "waiting": 2, "lowest_kept_score": None,
                                            "expired_after_s": MAX_LIFE_S}
    assert rows[ids[2]]["cap_tiebreak"]["score"] is None                     # unknown, never 0
    assert len(calls) == 2 and store.judge_calls_since(databases[0], since_iso=AT) == 2


def test_a_freed_slot_goes_to_the_top_score_not_the_oldest(databases):
    first, old_low, new_high = _waiting(databases, 3)
    clock, calls = Clock(), []
    gate = ScoredGate({first: "shortlisted"}, {first: 0.5, old_low: 0.1, new_high: 0.9})
    clock.t = T0 + timedelta(minutes=2)
    _run(databases, gate, calls, 1, clock)                                    # the hour's one call
    gate.state.update({old_low: "shortlisted", new_high: "shortlisted"})
    clock.t = T0 + timedelta(minutes=20)
    assert _run(databases, gate, calls, 1, clock) == []                       # cap full: both deferred
    clock.t = T0 + timedelta(minutes=62, seconds=1)                           # the first call left the hour
    _run(databases, gate, calls, 1, clock)
    assert [c["opportunity_id"] for c in calls] == [first, new_high]


@pytest.mark.parametrize("freshest,order", [(False, slice(None)), (True, slice(None, None, -1))])
def test_cap_not_binding_judges_everyone_in_pass_order(databases, monkeypatch, freshest, order):
    """Operator 2026-10-23: MEMECOIN_DESK_JUDGE_FRESHEST_FIRST serves the newest PASS first; off = oldest first."""
    from sniper.settings import memecoin as mc
    monkeypatch.setattr(mc, "MEMECOIN_DESK_JUDGE_FRESHEST_FIRST", freshest)
    ids = _waiting(databases, 3)
    clock, calls = Clock(), []
    clock.t = T0 + timedelta(minutes=2)
    gate = ScoredGate(dict.fromkeys(ids, "shortlisted"), dict(zip(ids, [0.1, 0.9, 0.5])))
    _run(databases, gate, calls, 15, clock)
    assert [c["opportunity_id"] for c in calls] == ids[order]


def test_ranker_gate_reads_the_shortlist_cycle_score(tmp_path):
    rdir = tmp_path / "ranker"
    rdir.mkdir()
    at = T0.timestamp() + 60
    c = sqlite3.connect(rdir / "candidates.db")
    c.execute("CREATE TABLE candidates(opportunity_id TEXT PRIMARY KEY, first_rank_at REAL, "
              "first_shortlisted_at REAL, closed_at REAL)")
    c.executemany("INSERT INTO candidates VALUES(?,?,?,?)",
                  [("a", at - 60, at, None), ("b", at - 60, None, at), ("c", at, at, None)])
    c.commit()
    day = datetime.fromtimestamp(at, timezone.utc).date().isoformat()
    d = sqlite3.connect(rdir / ("%s.db" % day))
    d.execute("CREATE TABLE rank_rows(rank_at REAL, opportunity_id TEXT, score REAL)")
    d.executemany("INSERT INTO rank_rows VALUES(?,?,?)",
                  [(at - 60, "a", 0.2), (at, "a", 0.7), (at, "b", 0.4), (at, "c", None)])
    d.commit()
    assert J.ranker_gate(tmp_path).scores(["a", "b", "c", "zz"]) == {"a": 0.7}


def _waiting_policy(db, n, **extra):
    cohort = api().provision_cohort(*db, producer="U", policy={**POLICY, "judge_p_admit": 1.0,
                                    "select_version": J.SELECT_VERSION, **extra}, prompt_version="desk-judge-v6",
                                    created_at=AT)
    ids = []
    for i in range(n):
        c = make_candidate(i, T0 + timedelta(minutes=1, seconds=i))
        at = (T0 + timedelta(minutes=1, seconds=i + 1)).isoformat()
        t = api().ingest_candidate(db[0], c, cohort_id=cohort, now_iso=at)
        api().evaluate_candidate(db[0], t["ticket_id"], evidence=evidence(c), policy=policy(), now_iso=at)
        ids.append(t["ticket_id"])
    return ids


def test_the_frozen_defer_ceiling_expires_a_deferred_ticket(databases):
    """Gamma MEDIUM: a deferred ticket waits at most the cohort's judge_defer_max_s from emission, not MAX_LIFE_S."""
    a, b = _waiting_policy(databases, 2, judge_defer_max_s=120)
    clock, calls = Clock(), []
    gate = ScoredGate(dict.fromkeys([a, b], "shortlisted"), {a: 0.9, b: 0.1})
    clock.t = T0 + timedelta(minutes=1, seconds=60)                            # b is 59 s old: deferred
    assert [o["ticket_id"] for o in _run(databases, gate, calls, 1, clock)] == [a]
    clock.t = T0 + timedelta(minutes=1, seconds=118)                           # 117 s: still waiting
    assert _run(databases, gate, calls, 1, clock) == []
    clock.t = T0 + timedelta(minutes=1, seconds=125)                           # 124 s > 120: expired, no call
    out = _run(databases, gate, calls, 1, clock)
    assert [(o["ticket_id"], o["reason"]) for o in out] == [(b, "judge_signal_aged")]
    assert _calls(databases)[b]["aged"] == {"age_s": 124.0, "defer_max_s": 120.0, "calls_left": 0}
    assert len(calls) == 1


def test_a_bad_frozen_defer_ceiling_stops_the_pass(databases):
    import pytest
    a, b = _waiting_policy(databases, 2, judge_defer_max_s=301)
    clock, calls = Clock(), []
    clock.t = T0 + timedelta(minutes=2)
    with pytest.raises(ValueError, match="DEFER_MAX_S"):
        _run(databases, ScoredGate(dict.fromkeys([a, b], "shortlisted"), {}), calls, 1, clock)
    assert calls == []


def test_the_defer_knob_is_bounded_and_a_typo_raises():
    import pytest
    from sniper.services import memecoin_desk_contract as C
    assert C.judge_defer_max_s({}) == 120.0 and C.judge_defer_max_s({"MEMECOIN_DESK_JUDGE_DEFER_MAX_S": "300"}) == 300.0
    for bad in ("12o", "0", "-1", "301", "nan", "inf"):
        with pytest.raises(ValueError):
            C.judge_defer_max_s({"MEMECOIN_DESK_JUDGE_DEFER_MAX_S": bad})


def test_every_judge_row_carries_age_at_call(databases):
    """Gamma MEDIUM (b): age_at_call_s = call start - emission on the model row and on the no-call row."""
    a, b = _waiting_policy(databases, 2, judge_defer_max_s=120)
    clock, calls = Clock(), []
    clock.t = T0 + timedelta(minutes=1, seconds=40)
    gate = ScoredGate({a: "shortlisted", b: "cut"}, {a: 0.5})
    _run(databases, gate, calls, 5, clock)
    rows = _calls(databases)
    assert rows[a]["age_at_call_s"] == 40.0 and rows[b]["age_at_call_s"] == 39.0


def test_a_stale_ticket_is_never_judged_even_with_free_calls(databases):
    """Gamma MEDIUM on 491b13bbb: after a worker outage the ceiling applies without the cap binding."""
    a, b = _waiting_policy(databases, 2, judge_defer_max_s=120)
    clock, calls = Clock(), []
    clock.t = T0 + timedelta(minutes=1, seconds=131)                           # a 131 s old, b 130 s old
    gate = ScoredGate(dict.fromkeys([a, b], "shortlisted"), {a: 0.9, b: 0.5})
    out = _run(databases, gate, calls, 15, clock)
    assert calls == [] and {o["reason"] for o in out} == {"judge_signal_aged"}
    rows = _calls(databases)
    assert rows[b]["call_error"] == "signal_aged" and rows[b]["age_at_call_s"] == 130.0
    assert rows[b]["aged"]["calls_left"] == 15
    assert store.judge_calls_since(databases[0], since_iso=AT) == 0            # no call, no budget


def test_a_legacy_cohort_without_the_ceiling_judges_an_old_ticket_with_free_calls(databases):
    [a] = _waiting(databases, 1)
    clock, calls = Clock(), []
    clock.t = T0 + timedelta(minutes=20)
    _run(databases, ScoredGate({a: "shortlisted"}, {}), calls, 15, clock)
    assert [c["opportunity_id"] for c in calls] == [a]


@pytest.mark.parametrize("limit,claim,moved", [(25, 14000 * 1.40, True), (25, 14000 * 0.70, True),
                                               (25, 14000 * 1.20, False), (None, 14000 * 3.0, False)])
def test_moved_since_emission_is_a_cohort_frozen_no_call(tmp_path, limit, claim, moved):
    """Operator 2026-10-23 (SAM 74156: 18k at PASS, 37k at the claim): past the cohort's judge_moved_max_pct either way
    the claim is a different setup and gets no call; a cohort without the rule is unchanged."""
    conn = store.connect(tmp_path / "d.db")
    hf = {} if limit is None else {"judge_moved_max_pct": limit}
    store.register_cohort(conn, cohort_id="c", producer="U", prompt_version="v", created_at="2026-10-23T00:00:00+00:00",
                          policy={"clip_usd": 25, "hard_filter": hf})
    cand = lambda v: {"snapshot": {"fields": {"mcap_usd": {"value": v, "completeness": "complete"}}}}
    out = J._moved_since_emission(conn, {"cohort_id": "c", "candidate": cand(14000)}, cand(claim))
    assert (out is not None) is moved
    if moved:
        assert out["limit_pct"] == limit and out["emission_mcap_usd"] == 14000
    assert "moved_since_emission" in J.NO_CALL_SKIPS
