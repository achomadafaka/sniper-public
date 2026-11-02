"""Operator 2026-10-22: the ranker triggers on the event. A trigger-cohort U ticket is scored inline at its JUDGE claim
from the as-of snapshot and never waits for the 60 s ranker; out of the band it is a no-call SKIP 'band_below'."""
from __future__ import annotations

import copy
import json
from contextlib import nullcontext
from datetime import timedelta

import pytest

from sniper.persistence import db_memecoin_desk as store
from sniper.services import memecoin_desk_asof as A
from sniper.services import memecoin_desk_judge as J
from sniper.services import memecoin_desk_ranker as R
from sniper.services.memecoin_desk_asof import StaleSnapshot
from test_memecoin_desk_asof import SOL, VQ_E, VT_E, cand, go, tape  # noqa: F401
from test_memecoin_desk_asof_flow import refreshed
from test_memecoin_desk_contract import evidence, policy, rehash
from test_memecoin_desk_judge import T0, Clock, databases, enter, make_candidate, model_returning  # noqa: F401
from test_memecoin_desk_pipeline import AT, POLICY, api

FEATURES = {"net_sol": 1.5, "buyers": 4, "n_swaps": 9, "ret_ln": 0.05}


def trigger_cohort(db):
    return api().provision_cohort(*db, producer="U", policy={**POLICY, "judge_p_admit": 1.0,
                                  "select_version": J.TRIGGER_SELECT_VERSION}, prompt_version="desk-judge-v6", created_at=AT)


def waiting(db, cohort, i=0, at=None):
    c = make_candidate(i, at or T0 + timedelta(minutes=1, seconds=i))
    now = (T0 + timedelta(minutes=1, seconds=i + 1)).isoformat()
    t = api().ingest_candidate(db[0], c, cohort_id=cohort, now_iso=now)
    api().evaluate_candidate(db[0], t["ticket_id"], evidence=evidence(c), policy=policy(), now_iso=now)
    return t["ticket_id"]


REAL = 400_000_000_000_000


def rank_fn(mcap, features=FEATURES, calls=None, real=REAL):
    def fn(c, ts):
        if calls is not None:
            calls.append(ts)
        out = refreshed(c, ts, mcap=mcap)
        return out, {"v": "t", "rank": {"features": features, "price_now": 3.0e-8, "base_price": 2.9e-8, "real_token": real}}
    return fn


def go_run(db, rank_asof_fn, calls, clock, gate=None, cph=100):
    return J.run_once(db[0], lambda: nullcontext(db[1]), call_model=model_returning(enter, calls=calls, clock=clock),
                      system_prompt="SYS", worker="w", now_fn=clock, calls_per_hour=cph,
                      shortlist_fn=gate or J.ranker_gate("/nonexistent-ranker-dir"), rank_asof_fn=rank_asof_fn)


def vet(db, ticket):
    return [s for s in store.get_ticket(db[0], ticket)["stages"] if s["stage"] == "vet"][0]


def test_an_in_band_ticket_is_judged_at_once_with_no_ranker_running(databases):
    ticket = waiting(databases, trigger_cohort(databases))
    calls, clock = [], Clock()
    clock.t = T0 + timedelta(minutes=1, seconds=2)
    out = go_run(databases, rank_fn(50_000.0), calls, clock)                         # no ranker db exists at all
    assert len(calls) == 1 and [o["ticket_id"] for o in out] == [ticket]
    view = vet(databases, ticket)["inputs"]["inline_rank"]
    assert view["band"] == "inside" and view["ranker_version"] == R.INLINE_RANKER_VERSION
    assert view["score"] == R.score(FEATURES) and view["features"] == FEATURES and view["mcap_usd"] == 50_000.0


@pytest.mark.parametrize("mcap,real,reason,band,anomaly", [
    (20_000.0, REAL, "band_below", "below", None),
    (42_800.0, 0, "band_curve_complete", "curve_complete", None),         # off the curve: never chased, whatever the cap reads
    (150_000.0, REAL, "band_unknown", "unknown", R.ABOVE_LIVE_CURVE),                # above the band on a live curve is a tape anomaly
    (50_000.0, None, "band_unknown", "unknown", None)])                   # real token reserves unreadable
def test_a_ticket_outside_the_inside_state_is_a_no_call_with_the_numbers_that_decided_it(databases, mcap, real, reason, band, anomaly):
    ticket = waiting(databases, trigger_cohort(databases))
    calls, clock = [], Clock()
    clock.t = T0 + timedelta(minutes=1, seconds=12)                                    # 11 s after the PASS at +1 s
    out = go_run(databases, rank_fn(mcap, real=real), calls, clock)
    assert calls == [] and out[0]["reason"] == "judge_" + reason
    saved = json.loads(databases[0].execute("SELECT result_json FROM desk_judge_calls WHERE ticket_id=?", (ticket,)).fetchone()[0])
    assert saved["call_error"] == reason and saved["since_pass_s"] == 11.0
    view = saved["inline_rank"]
    assert (view["band"], view["anomaly"], view["real_token"], view["price_sol"]) == (band, anomaly, real, 3.0e-8)
    assert view["ranker_version"] == R.INLINE_RANKER_VERSION and view["band_usd"] == [30_000.0, 100_000.0]
    assert vet(databases, ticket)["inputs"]["inline_rank"]["band"] == band            # stamped on the vet row too
    assert store.judge_calls_since(databases[0], since_iso=AT) == 0                    # a no-call claim never spends the hourly cap
    adm = databases[0].execute("SELECT decision FROM desk_arm_admissions WHERE ticket_id=? AND arm='F'", (ticket,)).fetchone()
    assert adm[0] == "not_selected"


def test_an_unknown_market_cap_is_the_stale_snapshot_skip_not_a_side(databases):
    ticket = waiting(databases, trigger_cohort(databases))
    calls, clock = [], Clock()
    clock.t = T0 + timedelta(minutes=1, seconds=3)

    def fn(c, ts):
        out = refreshed(c, ts)
        out["snapshot"]["fields"]["mcap_usd"].update(value=None, completeness="missing")
        out["snapshot"]["missing"] = sorted(set(out["snapshot"]["missing"]) | {"mcap_usd"})
        return rehash(out), {"v": "t", "rank": {"features": FEATURES, "price_now": 3.0e-8, "real_token": REAL}}
    out = go_run(databases, fn, calls, clock)
    saved = json.loads(databases[0].execute("SELECT result_json FROM desk_judge_calls WHERE ticket_id=?", (ticket,)).fetchone()[0])
    assert calls == [] and saved["call_error"] == "band_unknown" and saved["inline_rank"]["mcap_usd"] is None


def test_a_stale_or_gapped_tape_at_the_claim_is_the_existing_stale_snapshot_skip(databases):
    ticket = waiting(databases, trigger_cohort(databases))
    calls, clock = [], Clock()
    clock.t = T0 + timedelta(minutes=1, seconds=3)

    def fn(c, ts):
        raise StaleSnapshot("tape_gap_since_last_swap")
    go_run(databases, fn, calls, clock)
    saved = json.loads(databases[0].execute("SELECT result_json FROM desk_judge_calls WHERE ticket_id=?", (ticket,)).fetchone()[0])
    assert calls == [] and saved["call_error"] == "stale_snapshot" and saved["asof_reason"] == "tape_gap_since_last_swap"


def test_a_trigger_cohort_without_the_rank_snapshot_function_refuses_before_any_claim(databases):
    ticket = waiting(databases, trigger_cohort(databases))
    clock = Clock()
    clock.t = T0 + timedelta(minutes=1, seconds=3)
    with pytest.raises(ValueError, match="rank_asof_fn"):
        go_run(databases, None, [], clock)
    assert databases[0].execute("SELECT COUNT(*) FROM desk_judge_calls").fetchone()[0] == 0


class PendingGate:
    """A 60 s ranker that has not seen anything yet: a legacy U ticket waits behind it."""
    def decided(self, since):
        return set()

    def states(self, ids):
        return dict.fromkeys(ids, "pending")

    def scores(self, ids):
        return {}


def test_a_legacy_cohort_ticket_still_waits_for_the_ranker_while_the_trigger_ticket_goes(databases):
    legacy = api().provision_cohort(*databases, producer="U", policy={**POLICY, "judge_p_admit": 1.0,
                                    "select_version": J.SELECT_VERSION}, prompt_version="desk-judge-v6", created_at=AT)
    old = waiting(databases, legacy, i=0)
    new = waiting(databases, trigger_cohort(databases), i=1)
    calls, clock = [], Clock()
    clock.t = T0 + timedelta(minutes=1, seconds=5)
    out = go_run(databases, rank_fn(50_000.0), calls, clock, gate=PendingGate())
    assert [o["ticket_id"] for o in out] == [new] and len(calls) == 1
    assert databases[0].execute("SELECT COUNT(*) FROM desk_judge_calls WHERE ticket_id=?", (old,)).fetchone()[0] == 0


class NoScoreGate(PendingGate):
    def scores(self, ids):
        raise AssertionError("a trigger ticket is never scored by the 60 s ranker")


def test_the_cap_tiebreak_uses_the_inline_score_not_the_ranker_gate(databases):
    cohort = trigger_cohort(databases)
    ids = [waiting(databases, cohort, i=i) for i in range(3)]
    calls, clock = [], Clock()
    clock.t = T0 + timedelta(minutes=2)
    scores = {0: 0.1, 1: 0.9, 2: 0.5}

    def fn(c, ts):
        i = int(c["ticket_id"][-3:])
        return refreshed(c, ts, mcap=50_000.0), {"v": "t", "rank": {"features": {"net_sol": scores[i], "buyers": 1, "n_swaps": 1, "ret_ln": 0.0},
                                                                    "price_now": 3.0e-8, "real_token": REAL}}
    J._SCORE_CACHE.clear()
    out = go_run(databases, fn, calls, clock, gate=NoScoreGate(), cph=1)
    assert [o["ticket_id"] for o in out] == [ids[1]]                                   # the highest inline score, not the oldest
    assert [c["opportunity_id"] for c in calls] == [ids[1]]


def test_the_unscorable_snapshot_ranks_last_in_the_tiebreak_never_zero(databases):
    cohort = trigger_cohort(databases)
    ids = [waiting(databases, cohort, i=i) for i in range(2)]
    calls, clock = [], Clock()
    clock.t = T0 + timedelta(minutes=2)

    def fn(c, ts):
        if c["ticket_id"].endswith("000"):
            raise StaleSnapshot("tape_gap_since_last_swap")
        return refreshed(c, ts, mcap=50_000.0), {"v": "t", "rank": {"features": {"net_sol": -3.0, "buyers": 0, "n_swaps": 1, "ret_ln": -0.5},
                                                                    "price_now": 3.0e-8, "real_token": REAL}}
    J._SCORE_CACHE.clear()
    out = go_run(databases, fn, calls, clock, gate=NoScoreGate(), cph=1)
    assert [c["opportunity_id"] for c in calls] == [ids[1]]                            # the scored ticket takes the one call
    assert ids[0] not in {o["ticket_id"] for o in out}                                 # the unreadable one waits in line (ranks last), it does not skip it


def test_inline_view_is_pure_and_unknown_is_never_a_side():
    def view(mcap, real=REAL, features=FEATURES):
        c = refreshed(make_candidate(0, T0), T0.timestamp() + 30, mcap=mcap)
        return R.inline_view(c, {"features": features, "price_now": 1.0, "real_token": real})
    assert view(30_000.0)["band"] == "inside" and view(100_000.0)["band"] == "inside"                      # the edges are inside
    assert view(29_999.99)["band"] == "below"
    above = view(100_000.01)
    assert (above["band"], above["anomaly"]) == ("unknown", R.ABOVE_LIVE_CURVE) and above["completeness"] == "band_unknown"
    assert view(50_000.0, real=0)["band"] == "curve_complete" and view(10.0, real=0)["band"] == "curve_complete"
    assert view(50_000.0, real=None)["band"] == "unknown" and view(50_000.0, real=-1)["band"] == "unknown"
    assert view(50_000.0, real=True)["band"] == "unknown"                                                  # a bool is not a reserve
    none = view(50_000.0, features=None)
    assert none["score"] is None and none["inputs_hash"] is None and none["completeness"] == "no_price"    # unknown, never 0
    assert view(50_000.0)["completeness"] == "complete" and view(50_000.0)["score"] == R.score(FEATURES)
    zero = refreshed(make_candidate(0, T0), T0.timestamp() + 30, mcap=0)
    assert R.inline_view(zero, {"features": FEATURES, "real_token": REAL})["band"] == "unknown"


def test_the_as_of_builder_adds_rank_inputs_only_when_asked_and_they_match_the_ranker_features(tape):
    plain_c, plain_meta = go(tape)
    assert "rank" not in plain_meta                                                  # the legacy path's meta is byte-identical
    c, meta = A.refresh(cand(), desk_dir=tape.dir, now=tape.now, rank_inputs=True)
    assert {k: v for k, v in meta.items() if k != "rank"} == plain_meta and c == plain_c
    rank = meta["rank"]
    base = R.price(VQ_E, VT_E)                                                       # no swap at or before now-60: the emission price
    swaps = [(40.0 + tape.now - 60 + 0, True, 0, "x", 0.0)]                          # placeholder to keep the shape obvious
    window = [(tape.now - 40, True, 2_000_000_000, "b", R.price(41_000_000_000, 680_000_000_000_000)),
              (tape.now - 20, False, 1_000_000_000, "c", R.price(44_000_000_000, 640_000_000_000_000))]
    want = R.features([(r[0] - (tape.now - 60) + (tape.now - 60), r[1], r[2], r[3], r[4]) for r in window], rank_at=tape.now,
                      base_price=base)
    assert rank["features"] == want and rank["price_now"] == pytest.approx(R.price(44_000_000_000, 640_000_000_000_000))
    assert rank["base_price"] == pytest.approx(base)


def test_the_as_of_builder_reads_a_completed_curve_for_the_trigger_path_only(tape):
    tape.swap("z" * 80, 0, 13, tape.now - 5, 45_000_000_000, 630_000_000_000_000, real_token=0)
    with pytest.raises(StaleSnapshot) as exc:
        go(tape)                                                                      # the legacy path still refuses
    assert exc.value.reason == "curve_complete"
    c, meta = A.refresh(cand(), desk_dir=tape.dir, now=tape.now, rank_inputs=True)
    assert meta["rank"]["real_token"] == 0 and R.inline_view(c, meta["rank"])["band"] == "curve_complete"


AGENT = next(iter(__import__("sniper.services.memecoin_desk_safety", fromlist=["x"]).MAYHEM_AGENT_IDS))


def _row(slot, actor, vq, vt=700_000_000_000_000):
    # the _COLS order: slot, signature, ordinal, is_buy, quote_atomic, token_atomic, actor, virtual_quote, virtual_token, received_at, real_token
    return (slot, "s%d" % slot, 0, 1, 1, 1, actor, vq, vt, float(slot), 1)


def test_agent_run_lift_is_the_price_over_the_last_regular_row_before_the_current_run():
    agents = {AGENT}
    rows = [_row(5, AGENT, 60_000_000_000), _row(4, AGENT, 50_000_000_000), _row(3, "u", 40_000_000_000), _row(2, AGENT, 30_000_000_000)]
    lift, run = A.run_lift(rows, agents)
    assert run == 2 and lift == pytest.approx(60 / 40)                      # reserves as read: agent rows count in the price
    assert A.run_lift([_row(5, "u", 40_000_000_000)] + rows, agents) == (None, 0)          # no current run: absent, never 1.0
    assert A.run_lift([_row(5, AGENT, 1), _row(4, AGENT, 1)], agents) == (None, 2)         # no regular row before it: unknown
    assert A.run_lift([], agents) == (None, 0)


def test_the_as_of_builder_records_the_trigger_actor_and_the_agent_run_on_the_rank_inputs(tape):
    tape.swap("g1" + "x" * 78, 0, 12, tape.now - 8, 46_000_000_000, 640_000_000_000_000, actor=AGENT)
    tape.swap("g2" + "x" * 78, 0, 13, tape.now - 4, 48_000_000_000, 630_000_000_000_000, actor=AGENT)
    _, meta = A.refresh(cand(), desk_dir=tape.dir, now=tape.now, rank_inputs=True)
    rank = meta["rank"]
    assert rank["trigger_actor"] == "a" and rank["trigger_is_agent"] is False        # the emission trigger swap is a regular actor
    assert rank["agent_run_rows"] == 2 and rank["agent_rows_60s"] == 2
    assert rank["agent_run_lift"] == pytest.approx((48 / 630) / (44 / 640))           # newest agent row over the last regular (c's) row
    view = R.inline_view(refreshed(make_candidate(0, T0), T0.timestamp() + 30, mcap=50_000.0), rank)
    assert (view["trigger_is_agent"], view["agent_rows_60s"]) == (False, 2) and view["agent_run_lift"] == rank["agent_run_lift"]


def test_an_above_band_live_curve_names_the_reason_not_a_decode_anomaly(databases):
    ticket = waiting(databases, trigger_cohort(databases))
    calls, clock = [], Clock()
    clock.t = T0 + timedelta(minutes=1, seconds=4)
    go_run(databases, rank_fn(250_000.0), calls, clock)
    saved = json.loads(databases[0].execute("SELECT result_json FROM desk_judge_calls WHERE ticket_id=?", (ticket,)).fetchone()[0])
    assert saved["call_error"] == "band_unknown" and saved["reason_text"] == "agent_inflated_or_unexplained"
    assert saved["inline_rank"]["anomaly"] == "band_above_live_curve" and calls == []


class CutGate(PendingGate):
    """The 60 s recorder has CLOSED every ticket as cut: for a legacy cohort that is the end of it."""
    def __init__(self, ids):
        self.ids = set(ids)

    def decided(self, since):
        return set(self.ids)

    def states(self, ids):
        return {i: "cut" for i in ids}


def test_a_recorder_cut_never_touches_a_trigger_ticket_but_still_cuts_a_legacy_one(databases):
    legacy = api().provision_cohort(*databases, producer="U", policy={**POLICY, "judge_p_admit": 1.0,
                                    "select_version": J.SELECT_VERSION}, prompt_version="desk-judge-v6", created_at=AT)
    old = waiting(databases, legacy, i=0)
    new = waiting(databases, trigger_cohort(databases), i=1)
    calls, clock = [], Clock()
    clock.t = T0 + timedelta(minutes=1, seconds=5)
    out = go_run(databases, rank_fn(50_000.0), calls, clock, gate=CutGate([old, new]))
    reasons = {o["ticket_id"]: o["reason"] for o in out}
    assert reasons[old] == "judge_ranker_cut" and new not in {r for r, v in reasons.items() if v == "judge_ranker_cut"}
    assert len(calls) == 1 and calls[0]["opportunity_id"] == new                       # the trigger ticket went to the model


def test_a_stale_snapshot_claim_spends_no_hourly_call_in_a_trigger_cohort_only(databases):
    from sniper.persistence import db_memecoin_desk as store_mod
    assert store_mod.TRIGGER_SELECT_VERSION == J.TRIGGER_SELECT_VERSION               # the SQL literal cannot drift from the judge
    legacy = api().provision_cohort(*databases, producer="U", policy={**POLICY, "judge_p_admit": 1.0,
                                    "select_version": J.SELECT_VERSION}, prompt_version="desk-judge-v6", created_at=AT)
    old = waiting(databases, legacy, i=0)
    new = waiting(databases, trigger_cohort(databases), i=1)
    clock = Clock()
    clock.t = T0 + timedelta(minutes=1, seconds=5)

    def stale(c, ts):
        raise StaleSnapshot("tape_gap_since_last_swap")
    calls = []
    go_run(databases, stale, calls, clock, gate=PendingGate())                         # the trigger ticket: stale_snapshot, no call
    saved = {t: json.loads(r)["call_error"] for t, r in databases[0].execute("SELECT ticket_id, result_json FROM desk_judge_calls")}
    assert saved[new] == "stale_snapshot" and calls == []
    assert store.judge_calls_since(databases[0], since_iso=AT) == 0                    # calls_left unchanged
    # the SAME skip claimed in an OLD cohort keeps counting (unchanged behaviour)
    at = clock().isoformat()
    assert store.claim_judge_call(databases[0], ticket_id=old, call_id="judge:old", worker="w", claimed_at=at)
    store.save_judge_response(databases[0], call_id="judge:old", call_started_at=at, response_at=at, elapsed_ms=0.0,
                              result={"call_error": "stale_snapshot", "asof_reason": "tape_stale"})
    assert store.judge_calls_since(databases[0], since_iso=AT) == 1


def test_a_long_agent_run_still_gets_its_lift_up_to_the_frozen_row_bound(tape, monkeypatch):
    for i in range(120):                                                           # 120 agent rows newer than every regular row
        tape.swap("run%03d" % i + "x" * 70, 0, 20 + i, tape.now - 55 + i * 0.4, 45_000_000_000 + i * 10_000_000, 640_000_000_000_000, actor=AGENT)
    _, meta = A.refresh(cand(), desk_dir=tape.dir, now=tape.now, rank_inputs=True)
    rank = meta["rank"]
    assert rank["agent_run_rows"] == 120 and rank["agent_run_rows_max"] == R.RUN_LIFT_ROWS == 300
    assert rank["agent_run_lift"] is not None and rank["agent_run_lift"] > 1.0         # the old 60-row read gave None here
    monkeypatch.setattr(R, "RUN_LIFT_ROWS", 50)                                       # a run past the bound is unknown, never a guess
    _, meta = A.refresh(cand(), desk_dir=tape.dir, now=tape.now, rank_inputs=True)
    assert meta["rank"]["agent_run_lift"] is None and meta["rank"]["agent_run_rows"] == 50 and meta["rank"]["agent_run_rows_max"] == 50


def test_a_below_band_ticket_never_takes_the_slot_of_one_that_will_spend_a_call(databases):
    cohort = trigger_cohort(databases)
    ids = [waiting(databases, cohort, i=i) for i in range(3)]
    calls, clock = [], Clock()
    clock.t = T0 + timedelta(minutes=2)
    mcap = {0: 20_000.0, 1: 50_000.0, 2: 60_000.0}                                     # ticket 0 is below the band
    net = {0: 9.0, 1: 0.2, 2: 0.7}                                                      # ... and has the HIGHEST scratch score

    def fn(c, ts):
        i = int(c["ticket_id"][-3:])
        return refreshed(c, ts, mcap=mcap[i]), {"v": "t", "rank": {"features": {"net_sol": net[i], "buyers": 1, "n_swaps": 1, "ret_ln": 0.0},
                                                                    "price_now": 3.0e-8, "real_token": REAL}}
    J._SCORE_CACHE.clear()
    out = go_run(databases, fn, calls, clock, gate=NoScoreGate(), cph=1)
    reasons = {o["ticket_id"]: o["reason"] for o in out}
    assert [c["opportunity_id"] for c in calls] == [ids[2]]                            # the best INSIDE ticket got the one call
    assert reasons[ids[0]] == "judge_band_below"                                       # the below-band one is a free no-call in the same pass
    assert ids[1] not in reasons                                                       # the other inside ticket waits for a free slot
    assert store.judge_calls_since(databases[0], since_iso=AT) == 1


def test_a_ticket_free_at_the_scratch_read_but_inside_at_its_claim_stays_deferred_when_capacity_is_gone(databases):
    """Codex P2: A (scratch inside) takes the last call; B (scratch below = free) reads INSIDE at its claim. B must stay unclaimed,
    not become a terminal budget_exhausted although a slot frees within the hour."""
    cohort = trigger_cohort(databases)
    a, b = [waiting(databases, cohort, i=i) for i in range(2)]
    calls, clock = [], Clock()
    clock.t = T0 + timedelta(minutes=2)
    reads = {a: 0, b: 0}

    def fn(c, ts):
        tid = c["ticket_id"]
        reads[tid] += 1
        below_at_scratch = tid == b and reads[b] == 1                                    # B: below at the scratch read, inside afterwards
        return refreshed(c, ts, mcap=20_000.0 if below_at_scratch else 50_000.0), {
            "v": "t", "rank": {"features": {"net_sol": 0.9 if tid == a else 0.1, "buyers": 1, "n_swaps": 1, "ret_ln": 0.0},
                               "price_now": 3.0e-8, "real_token": REAL}}
    J._SCORE_CACHE.clear()
    out = go_run(databases, fn, calls, clock, gate=NoScoreGate(), cph=1)
    assert [o["ticket_id"] for o in out] == [a] and [c["opportunity_id"] for c in calls] == [a]
    assert databases[0].execute("SELECT COUNT(*) FROM desk_judge_calls WHERE ticket_id=?", (b,)).fetchone()[0] == 0   # B is UNCLAIMED
    # a later pass, after A's call left the hour: B is judged
    clock.t += timedelta(minutes=61)
    J._SCORE_CACHE.clear()
    out = go_run(databases, lambda c, ts: fn(c, ts), calls, clock, gate=NoScoreGate(), cph=1)
    assert [c["opportunity_id"] for c in calls] == [a, b]


def test_when_both_are_scratch_inside_the_loser_simply_waits(databases):
    cohort = trigger_cohort(databases)
    a, b = [waiting(databases, cohort, i=i) for i in range(2)]
    calls, clock = [], Clock()
    clock.t = T0 + timedelta(minutes=2)
    net = {a: 0.9, b: 0.1}

    def fn(c, ts):
        return refreshed(c, ts, mcap=50_000.0), {"v": "t", "rank": {"features": {"net_sol": net[c["ticket_id"]], "buyers": 1, "n_swaps": 1,
                                                                                  "ret_ln": 0.0}, "price_now": 3.0e-8, "real_token": REAL}}
    J._SCORE_CACHE.clear()
    go_run(databases, fn, calls, clock, gate=NoScoreGate(), cph=1)
    assert [c["opportunity_id"] for c in calls] == [a]
    assert databases[0].execute("SELECT COUNT(*) FROM desk_judge_calls WHERE ticket_id=?", (b,)).fetchone()[0] == 0


def _no_emission_then(ok_after, mcap=50_000.0):
    """Raises no_emission_state (trigger swap not committed yet) until the clock passes `ok_after`."""
    real = rank_fn(mcap)

    def fn(c, ts):
        if ts < ok_after:
            raise StaleSnapshot("no_emission_state")
        return real(c, ts)
    return fn


def test_a_trigger_swap_not_yet_committed_is_retried_next_pass_not_skipped(databases):
    """2026-10-23: 17 of 17 stale_snapshot calls were claimed 0.41-0.85 s after emission, before the tape's ~1 s commit."""
    ticket = waiting(databases, trigger_cohort(databases))                    # emitted at T0 + 60 s
    calls, clock = [], Clock()
    fn = _no_emission_then((T0 + timedelta(seconds=61)).timestamp())
    clock.t = T0 + timedelta(seconds=60.5)
    assert go_run(databases, fn, calls, clock) == [] and calls == []
    assert not [s for s in store.get_ticket(databases[0], ticket)["stages"] if s["stage"] == "vet"]   # unclaimed
    clock.t = T0 + timedelta(seconds=61.2)
    out = go_run(databases, fn, calls, clock)
    assert len(calls) == 1 and [o["ticket_id"] for o in out] == [ticket]


def test_past_the_grace_a_missing_trigger_swap_is_todays_stale_snapshot(databases):
    ticket = waiting(databases, trigger_cohort(databases))
    calls, clock = [], Clock()
    clock.t = T0 + timedelta(seconds=60 + J.TAPE_COMMIT_GRACE_S + 0.1)
    go_run(databases, _no_emission_then(float("inf")), calls, clock)
    saved = databases[0].execute("SELECT result_json FROM desk_judge_calls WHERE ticket_id=?", (ticket,)).fetchone()
    assert calls == [] and json.loads(saved[0])["asof_reason"] == "no_emission_state"


def test_another_stale_reason_inside_the_grace_is_not_deferred(databases):
    ticket = waiting(databases, trigger_cohort(databases))
    calls, clock = [], Clock()
    clock.t = T0 + timedelta(seconds=60.5)

    def gap(c, ts):
        raise StaleSnapshot("tape_gap_since_last_swap")
    go_run(databases, gap, calls, clock)
    saved = databases[0].execute("SELECT result_json FROM desk_judge_calls WHERE ticket_id=?", (ticket,)).fetchone()
    assert json.loads(saved[0])["asof_reason"] == "tape_gap_since_last_swap"
