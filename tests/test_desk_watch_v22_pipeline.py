"""Watch v22 executor seam: a waiting price trigger fires the existing entry path with no model call, once; never
late, stale, mispriced, past the graduation guard or with the knob off; an expired watch releases the mint."""
from datetime import datetime, timedelta

import pytest

from sniper.persistence import db_memecoin_desk as store
from sniper.services import memecoin_manual_intents as intents
from sniper.settings import memecoin as cfg
from test_memecoin_desk_contract import candidate, evidence, policy, verdict
from test_memecoin_desk_pipeline import AT, POLICY, RESPONSE, api, databases, decide  # noqa: F401

def vol_structure(med, bars):
    from test_desk_structure_sheet import real_structure
    st = real_structure()
    st["volatility"] = {"median_5s_swing_pct": med, "bars": bars, "median_5s_true_range_pct": med, "true_range_bars": bars}
    return st


STANDING = {"version": "watch-v1", "watch_max_s": 1800, "graduation_guard_pct": 90, "dead_tape_noise_k": 3, "px_hold_max_noise": 2, "px_hold_hold_s": 5, "px_hold_max_wait_s": 60}
PLAN = {"inv": ["px", -12], "h_s": 300, "tz": [20, 60]}
BASIS = 3.0e-8                                   # SOL/token at the as-of read
T_RESP = datetime.fromisoformat(RESPONSE).timestamp()


def iso(t):
    return datetime.fromtimestamp(t, datetime.fromisoformat(RESPONSE).tzinfo).isoformat()


@pytest.fixture
def waiting(databases, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_STANDING_ORDERS", True)
    cohort = api().provision_cohort(*databases, producer="U", policy={**POLICY, "standing_orders": STANDING},
                                    prompt_version="desk-judge-v22", created_at=AT)
    ticket = api().ingest_candidate(databases[0], candidate(), cohort_id=cohort, now_iso=AT)["ticket_id"]
    api().evaluate_candidate(databases[0], ticket, evidence=evidence(), policy=policy(), now_iso=AT)
    out = decide(databases, ticket, {**verdict(v="W", e=["px_hold", "<=", -8, 2, 600]), "p": PLAN},
                 asof={"candidate": candidate(), "meta": {"rank": {"price_now": BASIS}}},
                 structure=vol_structure(4.0, 30))
    assert out["action"] == "WAIT"
    return databases, ticket


def fire(**kw):
    row = {"slot": 9, "signature": "sig", "ordinal": 1, "tx_index": 3, "block_time": T_RESP + 30,
           "received_at": T_RESP + 30.5, "price_sol": BASIS * 0.95, "real_quote_sol": 40.0, "sol_usd": 180.0,
           "sol_at": T_RESP + 25, "curve_pct": 60.0}
    row.update(kw)
    return row


def test_trigger_level_is_frozen_in_sol_from_the_asof_price(waiting):
    db, ticket = waiting
    trig = [s for s in store.get_ticket(db[0], ticket)["stages"] if s["stage"] == "vet"][0]["inputs"]["trigger"]
    # asked -8 %, clipped to 2 x range5 (4 %) = -8 %: equal here; wait clipped 600 -> 60 s; 5 s time hold
    assert trig["level_price_sol"] == pytest.approx(BASIS * 0.92) and store.get_ticket(db[0], ticket)["state"] == "waiting"
    assert trig["hold_s"] == 5 and trig["expires_at"] == iso(T_RESP + 60)


def test_fire_queues_one_entry_capped_at_the_trigger_trade(waiting):
    db, ticket = waiting
    api().record_fire(db[0], ticket, fire(), now_iso=iso(T_RESP + 31))
    with pytest.raises(ValueError):                                       # a second fire cannot overwrite the first
        api().record_fire(db[0], ticket, fire(signature="other"), now_iso=iso(T_RESP + 32))
    sent = api().dispatch_outbox(*db, now_iso=iso(T_RESP + 31))
    assert [r.get("state") for r in sent] == ["sent"], [(r.get("state"), r.get("reason")) for r in sent]
    queued = intents.pending_count(db[1])
    assert queued == 1
    head = store.get_ticket(db[0], ticket)
    size = [s for s in head["stages"] if s["stage"] == "size"][0]
    meta = size["inputs"]["request"]["payload"]["desk_metadata"]
    assert meta["verdict_price"]["basis"] == "trigger"
    assert meta["verdict_price"]["ref_price_usd"] == pytest.approx(BASIS * 0.95 * 180.0)
    assert meta["standing_order"]["kind"] == "px_hold" and meta["plan"] == PLAN


@pytest.mark.parametrize("change,reason", [
    ({"block_time": T_RESP - 5}, "fire_before_decision"),
    ({"received_at": T_RESP + 70}, "fire_expired"),
    ({"price_sol": BASIS * 0.90}, "fire_price"),
    ({"curve_pct": 91.0}, "fire_graduation_guard"),
    ({"sol_at": T_RESP - 200}, "fire_sol_stale"),
])
def test_bad_fire_is_refused(waiting, change, reason):
    db, ticket = waiting
    with pytest.raises(ValueError, match=reason):
        api().record_fire(db[0], ticket, fire(**change), now_iso=iso(T_RESP + 31))


def test_knob_off_refuses_fires_and_expires_watches(waiting, monkeypatch):
    db, ticket = waiting
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_STANDING_ORDERS", False)
    with pytest.raises(ValueError, match="standing_off"):
        api().record_fire(db[0], ticket, fire(), now_iso=iso(T_RESP + 31))
    assert api().expire_watches(db[0], now_iso=iso(T_RESP + 40)) == [ticket]
    assert store.get_ticket(db[0], ticket)["state"] == "expired"


def test_trigger_expiry_releases_the_mint(waiting):
    db, ticket = waiting
    assert api().expire_watches(db[0], now_iso=iso(T_RESP + 50)) == []
    assert api().expire_watches(db[0], now_iso=iso(T_RESP + 61)) == [ticket]
    assert store.get_ticket(db[0], ticket)["state"] == "expired"


def _book(kind="px_hold", level=BASIS * 0.92, confirm=2):
    import json as _json
    from sniper.services.memecoin_desk_universe import StandingBook
    b = StandingBook()
    trig = {"kind": kind, "level_price_sol": level, "confirm": confirm, "expires_at": iso(T_RESP + 600)}
    b.load([("t1", "M", _json.dumps(trig), RESPONSE)], T_RESP)
    return b


def _tr(b, price, t, lag=0.2, gap=False, flow=50.0):
    return b.on_trade(mint="M", price_sol=price, block_time=t, received_at=t + lag, gap_since_load=gap, net_flow_usd=flow)


@pytest.mark.parametrize("flow", [None, -10.0])
def test_px_hold_needs_known_positive_net_flow(flow):
    b = _book(confirm=1)
    _tr(b, BASIS * 0.91, T_RESP + 5)
    assert _tr(b, BASIS * 0.95, T_RESP + 6, flow=flow) == []
    assert len(_tr(b, BASIS * 0.95, T_RESP + 7)) == 1


def test_px_hold_fires_after_a_dip_then_confirm_prints_above():
    b = _book()
    assert _tr(b, BASIS * 0.95, T_RESP + 5) == []           # above, no dip yet
    assert _tr(b, BASIS * 0.91, T_RESP + 6) == []           # the dip
    assert _tr(b, BASIS * 0.93, T_RESP + 7) == []           # 1 of 2
    assert [o["ticket_id"] for o in _tr(b, BASIS * 0.94, T_RESP + 8)] == ["t1"]
    b.commit("t1")                                          # the worker commits after the request line is durable
    assert _tr(b, BASIS * 0.95, T_RESP + 9) == []           # fired once


def test_px_up_needs_consecutive_prints_at_or_above():
    b = _book(kind="px_up", level=BASIS * 1.06, confirm=2)
    assert _tr(b, BASIS * 1.07, T_RESP + 5) == [] and _tr(b, BASIS * 1.05, T_RESP + 6) == []
    assert _tr(b, BASIS * 1.07, T_RESP + 7) == [] and len(_tr(b, BASIS * 1.08, T_RESP + 8)) == 1


@pytest.mark.parametrize("kw", [{"lag": 6.0}, {"gap": True}])
def test_stale_print_or_gap_never_fires_and_resets_the_confirmation(kw):
    """Codex on 1073128bb: unusable evidence breaks a partial confirmation; the dip must happen again after it."""
    b = _book(confirm=1)
    _tr(b, BASIS * 0.91, T_RESP + 5)
    assert _tr(b, BASIS * 0.95, T_RESP + 6, **kw) == []
    assert _tr(b, BASIS * 0.95, T_RESP + 7) == []           # the pre-gap dip no longer counts
    _tr(b, BASIS * 0.91, T_RESP + 8)
    assert len(_tr(b, BASIS * 0.95, T_RESP + 9)) == 1


def test_a_gap_before_reload_resets_confirmation_across_the_reload():
    import json as _json
    b = _book(confirm=2)
    _tr(b, BASIS * 0.91, T_RESP + 5)
    _tr(b, BASIS * 0.93, T_RESP + 6)                        # 1 of 2 before the gap
    trig = {"kind": "px_hold", "level_price_sol": BASIS * 0.92, "confirm": 2, "expires_at": iso(T_RESP + 600)}
    b.load([("t1", "M", _json.dumps(trig), RESPONSE)], T_RESP + 7, reset=True)
    assert _tr(b, BASIS * 0.94, T_RESP + 8) == []           # no carry-over: needs a new dip


def test_an_uncommitted_fire_does_not_burn_the_watch():
    b = _book(confirm=1)
    _tr(b, BASIS * 0.91, T_RESP + 5)
    assert len(_tr(b, BASIS * 0.95, T_RESP + 6)) == 1        # returned, not committed (pricing failed upstream)
    assert len(_tr(b, BASIS * 0.96, T_RESP + 7)) == 1        # still live


def test_print_after_expiry_never_fires():
    b = _book(confirm=1)
    _tr(b, BASIS * 0.91, T_RESP + 5)
    assert _tr(b, BASIS * 0.95, T_RESP + 601) == []


def test_failed_read_fails_closed():
    b = _book(confirm=1)
    b.failed(T_RESP + 31)
    _tr(b, BASIS * 0.91, T_RESP + 32)
    assert _tr(b, BASIS * 0.95, T_RESP + 33) == []


def test_standing_pass_freezes_a_fire_then_the_courier_queues_it(waiting, tmp_path):
    import importlib.util, json as _json
    spec = importlib.util.spec_from_file_location("jw", "scripts/memecoin_desk_judge_worker.py")
    jw = importlib.util.module_from_spec(spec); spec.loader.exec_module(jw)
    db, ticket = waiting
    now = iso(T_RESP + 31)
    fires = tmp_path / "fires"; fires.mkdir()
    (fires / f"{now[:10]}.jsonl").write_text(_json.dumps({"ticket_id": ticket, "fire": fire()}) + "\n"
                                             + _json.dumps({"ticket_id": ticket, "fire": fire(signature="dup")}) + "\n")
    out = jw.standing_pass(db[0], fires, now, tmp_path / "cursor")
    assert [r.get("state") or "refused" for r in out] == ["fired", "refused"]
    assert jw.standing_pass(db[0], fires, now, tmp_path / "cursor") == []      # consumed once
    sent = api().dispatch_outbox(*db, now_iso=now)
    assert [r.get("state") for r in sent] == ["sent"]


def test_px_hold_depth_is_clipped_to_two_noise_units(databases, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_STANDING_ORDERS", True)
    cohort = api().provision_cohort(*databases, producer="U", policy={**POLICY, "standing_orders": STANDING},
                                    prompt_version="desk-judge-v22", created_at=AT)
    ticket = api().ingest_candidate(databases[0], candidate(), cohort_id=cohort, now_iso=AT)["ticket_id"]
    api().evaluate_candidate(databases[0], ticket, evidence=evidence(), policy=policy(), now_iso=AT)
    decide(databases, ticket, {**verdict(v="W", e=["px_hold", "<=", -30, 2, 600]), "p": PLAN},
           asof={"candidate": candidate(), "meta": {"rank": {"price_now": BASIS}}},
           structure=vol_structure(3.0, 30))
    trig = [s for s in store.get_ticket(databases[0], ticket)["stages"] if s["stage"] == "vet"][0]["inputs"]["trigger"]
    assert trig["level_pct"] == -6.0 and trig["asked_level_pct"] == -30


@pytest.mark.parametrize("vol", [None, (3.0, 11), (None, 30)])
def test_px_hold_on_unknown_noise_is_a_skip(databases, monkeypatch, vol):
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_STANDING_ORDERS", True)
    cohort = api().provision_cohort(*databases, producer="U", policy={**POLICY, "standing_orders": STANDING},
                                    prompt_version="desk-judge-v22", created_at=AT)
    ticket = api().ingest_candidate(databases[0], candidate(), cohort_id=cohort, now_iso=AT)["ticket_id"]
    api().evaluate_candidate(databases[0], ticket, evidence=evidence(), policy=policy(), now_iso=AT)
    out = decide(databases, ticket, {**verdict(v="W", e=["px_hold", "<=", -8, 2, 600]), "p": PLAN},
                 asof={"candidate": candidate(), "meta": {"rank": {"price_now": BASIS}}},
                 structure=vol_structure(*vol) if vol else None)
    assert out["action"] == "SKIP" and out["reason"] == "cond_unavailable"
    assert store.get_ticket(databases[0], ticket)["state"] != "waiting"


def test_px_hold_time_hold():
    import json as _json
    from sniper.services.memecoin_desk_universe import StandingBook
    b = StandingBook()
    trig = {"kind": "px_hold", "level_price_sol": BASIS * 0.92, "confirm": 1, "hold_s": 5, "expires_at": iso(T_RESP + 60)}
    b.load([("t1", "M", _json.dumps(trig), RESPONSE)], T_RESP)
    tr = lambda p, t, f=1.0: b.on_trade(mint="M", price_sol=p, block_time=t, received_at=t + 0.1, gap_since_load=False,
                                        net_flow_usd=f)
    tr(BASIS * 0.91, T_RESP + 2)
    assert tr(BASIS * 0.93, T_RESP + 3) == [] and tr(BASIS * 0.94, T_RESP + 7) == []    # 4 s held
    assert len(tr(BASIS * 0.94, T_RESP + 8)) == 1                                        # 5 s held


def test_entry_ttl_counts_from_the_fire_receipt_not_from_consumption(waiting):
    db, ticket = waiting
    with pytest.raises(ValueError, match="fire_ttl_expired"):     # enter_ttl_s 20: consumed 25 s after receipt
        api().record_fire(db[0], ticket, fire(received_at=T_RESP + 30.5), now_iso=iso(T_RESP + 55.5))
    api().record_fire(db[0], ticket, fire(), now_iso=iso(T_RESP + 40))
    api().dispatch_outbox(*db, now_iso=iso(T_RESP + 41))
    size = [s for s in store.get_ticket(db[0], ticket)["stages"] if s["stage"] == "size"][0]
    req = size["inputs"]["request"]
    assert req["requested_at"] == iso(T_RESP + 30.5) and req["expires_at"] == iso(T_RESP + 50.5)


def test_an_accepted_fire_is_never_expired_as_a_watch(waiting):
    db, ticket = waiting
    api().record_fire(db[0], ticket, fire(received_at=T_RESP + 59), now_iso=iso(T_RESP + 61))
    assert api().expire_watches(db[0], now_iso=iso(T_RESP + 62)) == []


def test_trigger_cap_uses_the_frozen_tolerance_and_runtime_may_only_tighten(databases, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_STANDING_ORDERS", True)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_VERDICT_TOLERANCE_PCT", 6.0)
    from sniper.services import memecoin_desk as D
    assert D._trigger_tolerance({"verdict_tolerance_pct": 3.0}) == 3.0     # frozen tighter wins
    assert D._trigger_tolerance({"verdict_tolerance_pct": 9.0}) == 6.0     # runtime tighter wins
    assert D._trigger_tolerance({}) == 6.0                                 # legacy cohort: runtime
    with pytest.raises(ValueError):
        D._trigger_tolerance({"verdict_tolerance_pct": "6"})


def test_a_worklist_outage_drops_partial_confirmation():
    """Codex on 28994d4f1: failed() past stale_s clears the work list AND the partial state; a below-level print during
    the outage is unseen, so a reload must not resume a hold begun before it."""
    import json as _json
    from sniper.services.memecoin_desk_universe import StandingBook
    b = StandingBook(stale_s=30)
    trig = {"kind": "px_hold", "level_price_sol": BASIS * 0.92, "confirm": 1, "hold_s": 5, "expires_at": iso(T_RESP + 600)}
    rows = [("t1", "M", _json.dumps(trig), RESPONSE)]
    b.load(rows, T_RESP)
    tr = lambda p, t: b.on_trade(mint="M", price_sol=p, block_time=t, received_at=t, gap_since_load=False, net_flow_usd=1.0)
    tr(BASIS * 0.91, T_RESP + 1)
    tr(BASIS * 0.93, T_RESP + 2)
    b.failed(T_RESP + 32)
    tr(BASIS * 0.91, T_RESP + 33)                    # unseen: the list is empty
    b.load(rows, T_RESP + 34)
    assert tr(BASIS * 0.93, T_RESP + 34) == []        # no resumed hold


def test_a_pre_midnight_fire_is_drained_after_the_day_changes(waiting, tmp_path):
    import importlib.util, json as _json
    spec = importlib.util.spec_from_file_location("jw", "scripts/memecoin_desk_judge_worker.py")
    jw = importlib.util.module_from_spec(spec); spec.loader.exec_module(jw)
    db, ticket = waiting
    fires = tmp_path / "fires"; fires.mkdir()
    now = iso(T_RESP + 31)
    (tmp_path / "cursor").write_text(_json.dumps({"day": "2026-10-20", "offset": 0}))
    (fires / "2026-10-20.jsonl").write_text(_json.dumps({"ticket_id": ticket, "fire": fire()}) + "\n")
    out = jw.standing_pass(db[0], fires, now, tmp_path / "cursor")
    assert [r.get("state") for r in out] == ["fired"]
