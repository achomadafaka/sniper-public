"""Intake cohort seats through the REAL judge.run_once (Zeta da8b1fc49, Gamma prereg Amendment 2 19bdbce4a): the
cohort's own judge_calls_per_hour is enforced per claim (spent cap-1 + 2 eligible -> exactly ONE model call, the other
stays unclaimed and re-competes), the outcome-blind order is first-PASS (a0) oldest first, and only age-out is seat_cut.
The model, the recorder and the as-of are stubs: this is the spend path, not the pricing chain."""
import contextlib
from datetime import datetime, timezone

import pytest

from sniper.persistence import db_memecoin_desk as D
from sniper.services import memecoin_desk as desk
from sniper.services import memecoin_desk_contract as K
from sniper.services import memecoin_desk_judge as J
from sniper.services import memecoin_desk_pumpswap_candidates as P
from sniper.services import memecoin_desk_safety as S
from sniper.services import memecoin_desk_universe as U
from test_memecoin_desk_contract import evidence as base_evidence, policy

NOW = 1_800_000_000.0
SIG = U.b58encode(bytes([3]) * 64)


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def intake_candidate(n, at):
    mint = U.b58encode(bytes([n]) * 32)
    pool = S.pumpswap_pool_address(mint)
    mapping = {"pool": pool, "mint": mint, "quote_mint": K.WSOL_MINT, "known_at": at - 100, "slot": 1, "canonical": True,
               "map_source": "create_pool"}
    meta = {"pool": pool, "mint": mint, "quote_mint": K.WSOL_MINT, "base_decimals": 6, "supply_atomic": "1000000000000000",
            "known_at": at - 80, "slot": 2, "source": "fixture:independent_pool_facts"}
    ev = {"kind": "buy", "pool": pool, "post_base_reserve_atomic": "100000000000000",
          "post_quote_reserve_atomic": "100000000000", "lp_fee_bps": 20, "protocol_fee_bps": 5, "creator_fee_bps": 5,
          "cashback_fee_bps": 0, "virtual_quote_atomic": "0", "virtual_quote_source": "pumpswap_grpc:buy_event_virtual_quote"}
    pool, mint, state, mcap, liq, when = P.state_from_print(event=ev, mapping=mapping, metadata=meta, slot=10, signature=SIG,
        ordinal=0, tx_index=None, now=at, received_at=at, sol_usd=100, sol_usd_at=at - 1, reserve_max_age_s=60,
        sol_max_age_s=120, metadata_optional=True)
    intake = {"feed": "pumpfun", "filter": "active", "provider_rank": n, "rank_kind": "list_position",
              "observed_at": at - 1, "host_received_at": at - 1, "clock_offset_s": 0.0, "intake_row_ref": "d:%d" % n}
    return K.validate_candidate(P.candidate_from_state(pool=pool, mint=mint, state=state, mcap=mcap, liq=liq, at=when,
        slot=10, signature=SIG, ordinal=0, ticket_id="desk:U:intake:%032x" % n, extra={"intake": intake}))


@pytest.fixture
def cohort(tmp_path):
    conn = D.connect(tmp_path / "desk.db")
    hard = {**policy(), "intake": {"version": "intake-v1", "mcap_safety": [5000, 50_000_000], "liq_min_quote_sol": 40}}
    pol = {"clip_usd": 25, "venue": "pumpswap", "hard_filter": hard, "evidence_wait_s": 30, "judge_p_admit": 1.0,
           "select_version": J.INTAKE_SELECT_VERSION, "judge_calls_per_hour": 40, "judge_order": J.INTAKE_ORDER,
           "judge_defer_max_s": 120, "rail_bounds": {"sl_pct": [10, 40], "tp_pct": [15, 150], "trail_pct": [8, 30]}}
    D.register_cohort(conn, cohort_id="intake", producer="U", prompt_version="desk-judge-v26", policy=pol,
                      created_at=iso(NOW - 3600))
    return conn, hard


def passed(conn, hard, n, at):
    c = intake_candidate(n, at)
    got = desk.ingest_candidate(conn, c, cohort_id="intake", now_iso=iso(at + .1))
    e = base_evidence(c)
    e["known_at"] = iso(at + .5)
    e["checks"]["token_safety"].update(supply_atomic="1000000000000000", decimals=6)
    assert desk.evaluate_candidate(conn, got["ticket_id"], evidence=e, policy=hard, now_iso=iso(at + .5))["verdict"] == "PASS"
    return got["ticket_id"]


def run(conn, now, monkeypatch, calls):
    monkeypatch.setattr(desk, "pumpswap_unpriced_reason", lambda c, **k: None)    # pricing is the chain test's job
    monkeypatch.setattr(J, "_record", lambda *a, **k: {"recorded": True})
    def call_model(system, user, **kw):
        calls.append(user)
        return {"text": '{"v":"S"}'}
    return J.run_once(conn, lambda: contextlib.nullcontext(), call_model=call_model, system_prompt="v26", worker="w",
                      now_fn=lambda: datetime.fromtimestamp(now, timezone.utc), calls_per_hour=100, limit=5,
                      shortlist_fn=J.ranker_gate("/nonexistent"), asof_fn=lambda c, ts, intake_mint=None: (c, {}),
                      admit_fn=lambda t: True)


def spend(conn, hard, n0, count):
    for n in range(n0, n0 + count):
        tid = passed(conn, hard, n, NOW - 60)
        assert D.claim_judge_call(conn, ticket_id=tid, call_id="judge:spent:%d" % n, worker="w", claimed_at=iso(NOW - 20))


def test_39_spent_cap_40_plus_two_eligible_makes_exactly_one_call(cohort, monkeypatch):
    conn, hard = cohort
    spend(conn, hard, 100, 39)
    older, newer = passed(conn, hard, 1, NOW - 10), passed(conn, hard, 2, NOW - 5)
    calls = []
    run(conn, NOW, monkeypatch, calls)
    assert len(calls) == 1                                                  # 39 spent + 1 = cap 40
    claimed = {r[0] for r in conn.execute("SELECT ticket_id FROM desk_judge_calls")}
    assert older in claimed and newer not in claimed                         # a0 oldest first; the other re-competes


def test_only_age_out_is_seat_cut(cohort, monkeypatch):
    conn, hard = cohort
    spend(conn, hard, 100, 40)                                               # fill the hour
    waiting = passed(conn, hard, 1, NOW - 10)
    calls = []
    run(conn, NOW, monkeypatch, calls)
    assert calls == [] and D.get_ticket(conn, waiting)["state"] != "skipped"   # cap full: unclaimed, waiting
    assert conn.execute("SELECT COUNT(*) FROM desk_judge_calls WHERE ticket_id=?", (waiting,)).fetchone()[0] == 0
    run(conn, NOW + 200, monkeypatch, calls)                                 # past judge_defer_max_s 120 s
    row = conn.execute("SELECT result_json FROM desk_judge_calls WHERE ticket_id=?", (waiting,)).fetchone()
    assert calls == [] and row is not None and '"seat_cut"' in row[0] and '"seat_cut_reason":"age_out"' in row[0]


def test_a_raced_claim_over_the_cap_never_makes_a_call(cohort, monkeypatch):
    """Backstop for a second worker racing the pre-claim check (the JUDGE worker is a flock singleton): with the
    pre-claim count blinded, the post-claim recount sees the committed claims and makes at most cap calls; the
    over-cap claim ends seat_cut (judge-call rows are immutable, so it cannot be released)."""
    conn, hard = cohort
    spend(conn, hard, 100, 39)
    a, b = passed(conn, hard, 1, NOW - 10), passed(conn, hard, 2, NOW - 5)
    monkeypatch.setattr(J, "_intake_seats", lambda c, picks, now: (list(picks), {}))   # a stale selection
    real = D.judge_calls_since
    state = {"n": 0}

    def blind(conn_, *, since_iso, cohort_id=None):
        state["n"] += 1                       # the two pre-claim reads see 38 (as if another worker had not committed)
        n = real(conn_, since_iso=since_iso, cohort_id=cohort_id)
        return n - 1 if cohort_id and state["n"] in (1, 3) else n
    monkeypatch.setattr(D, "judge_calls_since", blind)
    calls = []
    run(conn, NOW, monkeypatch, calls)
    assert len(calls) <= 1
    rows = dict(conn.execute("SELECT ticket_id, result_json FROM desk_judge_calls WHERE ticket_id IN (?,?)", (a, b)))
    assert sum(1 for r in rows.values() if r and '"seat_cut"' in r) == len(rows) - len(calls)
    assert all('"raced_over_cap"' in r for r in rows.values() if r and '"seat_cut"' in r)


def test_an_aged_ticket_is_seat_cut_before_any_asof_or_pricing(cohort, monkeypatch):
    """Zeta 3b8ed3b74: with a failing as-of (a real aged mint read would be stale) the final row is still seat_cut."""
    from sniper.services.memecoin_desk_asof import StaleSnapshot
    conn, hard = cohort
    spend(conn, hard, 100, 40)
    waiting = passed(conn, hard, 1, NOW - 10)
    monkeypatch.setattr(J, "_record", lambda *a, **k: {"recorded": True})
    asked = []

    def asof(c, ts, intake_mint=None):
        asked.append(c["ticket_id"])
        raise StaleSnapshot("intake_mint_unusable")
    J.run_once(conn, lambda: contextlib.nullcontext(), call_model=lambda *a, **k: pytest.fail("no model call"),
               system_prompt="v26", worker="w", now_fn=lambda: datetime.fromtimestamp(NOW + 200, timezone.utc),
               calls_per_hour=100, shortlist_fn=J.ranker_gate("/nonexistent"), asof_fn=asof, admit_fn=lambda t: True)
    row = conn.execute("SELECT result_json FROM desk_judge_calls WHERE ticket_id=?", (waiting,)).fetchone()
    assert row is not None and '"call_error":"seat_cut"' in row[0] and '"seat_cut_reason":"age_out"' in row[0]
    # the no-call row's best-effort decision context (_skip_context) may still try the as-of; it never decides the reason
