"""standing-v2 (operator 2026-10-26, Gamma prereg 7e0e59d41): arm B turns a JUDGE px_hold WAIT into one long-life standing
limit order frozen in USD; arm A keeps today's 60 s px_hold; both arm bits on every ticket; bounded book."""
import pytest

from sniper.persistence import db_memecoin_desk as store
from sniper.services import memecoin_desk as service
from sniper.services.memecoin_desk_universe import StandingBook, PUMP_SUPPLY_TOKENS
from test_memecoin_desk_contract import candidate
from test_memecoin_desk_pipeline import decide, databases  # noqa: F401
from test_desk_v23_pipeline import V23, ASOF, PLAN, F, BASIS, HOLD, ticket_in, vet, vol_structure
from test_desk_watch_v22_pipeline import STANDING, T_RESP, iso
ASOF_NOW = iso(T_RESP + 2)

V2 = {**V23, "standing_orders": {**STANDING, "version": "standing-v2", "order_max_s": 7200, "book_max": 50}}
SHEET = service.priced_verdict(ASOF["candidate"], clip_usd=25, basis="asof")["ref_price_usd"]   # USD/token the sheet showed
LEVEL = SHEET * 0.92                                                                           # HOLD's -8 % level


def other(ticket_id, tail):
    c = candidate()
    return {**c, "ticket_id": ticket_id, "mint": c["mint"][:-1] + tail}


def test_arms_are_bit1_standing_and_low_bit_limit():
    assert service.standing_arms("desk:U:fixture") == {"standing_arm": 1, "limit_arm": 0}
    assert service.standing_arms("desk:U:fx1") == {"standing_arm": 0, "limit_arm": 0}
    assert service.standing_arms("desk:U:fx2") == {"standing_arm": 1, "limit_arm": 1}


def test_arm_b_px_hold_wait_becomes_a_frozen_usd_standing_limit(databases, monkeypatch):
    t = ticket_in(databases, monkeypatch, pol=V2)                         # desk:U:fixture: standing_arm 1
    out = decide(databases, t, HOLD, asof=ASOF, structure=vol_structure(4.0, 30))
    assert out["action"] == "WAIT"
    inputs = vet(databases, t)["inputs"]
    assert inputs["arms"] == {"standing_arm": 1, "limit_arm": 0}
    o = inputs["trigger"]
    assert o["kind"] == "px_limit" and o["order"] == "standing-v2" and o["order_mode"] == "limit" and o["level_unit"] == "usd_mcap"
    assert o["level_price_usd"] == pytest.approx(LEVEL)             # sheet USD x (1 - 8 %), never rank SOL x FX
    assert o["level_mcap_usd"] == pytest.approx(o["level_price_usd"] * PUMP_SUPPLY_TOKENS)
    assert o["level_impact_pct"] == pytest.approx(25.0 / 5000.0 * 100.0)
    from datetime import datetime
    assert (datetime.fromisoformat(o["expires_at"]) - datetime.fromisoformat(inputs["response_at"])).total_seconds() == 7200


def test_arm_a_keeps_todays_px_hold(databases, monkeypatch):
    c = other("desk:U:fx1", "A")
    t = ticket_in(databases, monkeypatch, pol=V2, cand=c)
    out = decide(databases, t, {**HOLD, "k": "desk:U:fx1"}, asof={**ASOF, "candidate": c}, structure=vol_structure(4.0, 30))
    assert out["action"] == "WAIT"
    inputs = vet(databases, t)["inputs"]
    assert inputs["arms"]["standing_arm"] == 0 and inputs["trigger"]["kind"] == "px_hold" and "order" not in inputs["trigger"]


def test_unknown_fx_is_a_named_no_order(databases, monkeypatch):
    t = ticket_in(databases, monkeypatch, pol=V2)
    meta = {k: v for k, v in ASOF["meta"].items() if k != "sol_usd"}
    out = decide(databases, t, HOLD, asof={**ASOF, "meta": meta}, structure=vol_structure(4.0, 30))
    assert out["action"] == "SKIP" and out["reason"] == "order_unpriced" and "trigger" not in vet(databases, t)["inputs"]


def test_a_full_book_refuses_and_never_evicts(databases, monkeypatch):
    t = ticket_in(databases, monkeypatch, pol={**V2, "standing_orders": {**V2["standing_orders"], "book_max": 1}})
    assert decide(databases, t, HOLD, asof=ASOF, structure=vol_structure(4.0, 30))["action"] == "WAIT"
    c = other("desk:U:fx2", "B")
    t2 = ticket_in(databases, monkeypatch, pol={**V2, "standing_orders": {**V2["standing_orders"], "book_max": 1}}, cand=c)
    out = decide(databases, t2, {**HOLD, "k": "desk:U:fx2"}, asof={**ASOF, "candidate": c}, structure=vol_structure(4.0, 30))
    assert out["action"] == "SKIP" and out["reason"] == "book_full"
    assert store.get_ticket(databases[0], t)["state"] == "waiting"


def test_order_fires_on_the_usd_level_not_the_sol_it_implied(databases, monkeypatch):
    t = ticket_in(databases, monkeypatch, pol=V2)
    decide(databases, t, HOLD, asof=ASOF, structure=vol_structure(4.0, 30))
    b = StandingBook()
    b.load(databases[0].execute(StandingBook.SQL).fetchall(), T_RESP)
    mint = store.get_ticket(databases[0], t)["mint"]
    # a print just under the USD level at FX 162 fires, whatever SOL level the verdict FX implied
    assert b.on_trade(mint=mint, price_sol=LEVEL / 162.0 * 0.99, block_time=T_RESP + 1, received_at=T_RESP + 1.1,
                      gap_since_load=False, net_flow_usd=None, sol_usd=162.0)
    b2 = StandingBook()
    b2.load(databases[0].execute(StandingBook.SQL).fetchall(), T_RESP)
    # the same SOL print at FX 198 is above the USD level -> no fire
    assert not b2.on_trade(mint=mint, price_sol=LEVEL / 162.0 * 0.99, block_time=T_RESP + 1, received_at=T_RESP + 1.1,
                           gap_since_load=False, net_flow_usd=None, sol_usd=198.0)


def test_v1_cohort_rejects_v2_keys_and_v2_needs_them():
    from sniper.services import memecoin_desk_contract as contract
    with pytest.raises(ValueError):
        contract.validate_standing({**STANDING, "order_max_s": 7200, "book_max": 50})
    with pytest.raises(ValueError):
        contract.validate_standing({**STANDING, "version": "standing-v2"})
    with pytest.raises(ValueError):
        contract.validate_standing({**STANDING, "version": "standing-v2", "order_max_s": 9000, "book_max": 50})


def _banded(monkeypatch, lo, hi):
    """The fixture hard filter must stay byte-identical (evaluate_candidate checks it), so the frozen band is patched."""
    monkeypatch.setattr(service.contract, "mcap_band", lambda policy: (lo, hi))
    return V2


def test_order_dies_when_the_coin_leaves_the_band_and_the_writer_rechecks(databases, monkeypatch):
    t = ticket_in(databases, monkeypatch, pol=_banded(monkeypatch, 3960.0, 100_000.0))
    decide(databases, t, HOLD, asof=ASOF, structure=vol_structure(4.0, 30))
    o = vet(databases, t)["inputs"]["trigger"]
    assert o["cancel_band_usd_mcap"] == [3960.0, 100_000.0]
    b = StandingBook()
    b.load(databases[0].execute(StandingBook.SQL).fetchall(), T_RESP)
    mint = store.get_ticket(databases[0], t)["mint"]
    above = 100_001.0 / PUMP_SUPPLY_TOKENS / 180.0                     # SOL price that puts mcap over the band
    assert not b.on_trade(mint=mint, price_sol=above, block_time=T_RESP + 1, received_at=T_RESP + 1.1,
                          gap_since_load=False, sol_usd=180.0)
    [(tid, cancel)] = b.take_cancels()
    assert tid == t and cancel["reason"] == "left_band"
    # Zeta: an unwritten cancel stays pending and is retried on the next out-of-band print, never duplicated
    b.on_trade(mint=mint, price_sol=above, block_time=T_RESP + 2, received_at=T_RESP + 2.1, gap_since_load=False, sol_usd=180.0)
    assert [x[0] for x in b.take_cancels()] == [t]
    b.commit(t)                                                           # the caller's line is durable
    assert b.take_cancels() == []
    assert not b.on_trade(mint=mint, price_sol=above, block_time=T_RESP + 3, received_at=T_RESP + 3.1,
                          gap_since_load=False, sol_usd=180.0) and b.take_cancels() == []
    with pytest.raises(ValueError):                                        # inside the band: refused, order stays
        service.record_cancel(databases[0], t, {**cancel, "mcap_usd": 5000.0}, now_iso=ASOF_NOW)
    service.record_cancel(databases[0], t, cancel, now_iso=ASOF_NOW)
    head = store.get_ticket(databases[0], t)
    assert head["state"] == "expired" and [s["reason"] for s in head["stages"] if s["stage"] == "expire"] == ["left_band"]


def test_unknown_fx_never_cancels(databases, monkeypatch):
    t = ticket_in(databases, monkeypatch, pol=_banded(monkeypatch, 3960.0, 100_000.0))
    decide(databases, t, HOLD, asof=ASOF, structure=vol_structure(4.0, 30))
    b = StandingBook()
    b.load(databases[0].execute(StandingBook.SQL).fetchall(), T_RESP)
    mint = store.get_ticket(databases[0], t)["mint"]
    b.on_trade(mint=mint, price_sol=BASIS * 10, block_time=T_RESP + 1, received_at=T_RESP + 1.1, gap_since_load=False,
               sol_usd=None)
    assert b.take_cancels() == []


def _fired(databases, monkeypatch):
    from test_desk_watch_v22_pipeline import fire
    t = ticket_in(databases, monkeypatch, pol=V2)
    decide(databases, t, HOLD, asof=ASOF, structure=vol_structure(4.0, 30))
    service.record_fire(databases[0], t, fire(price_sol=LEVEL / 180.0 * 0.99), now_iso=iso(T_RESP + 31))
    return t


def _evidence():
    from test_memecoin_desk_contract import evidence
    return evidence(candidate())


def test_a_fired_order_waits_for_its_fresh_hard_filter_then_queues(databases, monkeypatch):
    t = _fired(databases, monkeypatch)
    assert [h["ticket_id"] for h in service.list_pending_refilters(databases[0])] == [t]
    assert service.dispatch_outbox(*databases, now_iso=iso(T_RESP + 32)) == []   # no entry before the fresh filter
    row = service.record_refilter(databases[0], t, evidence=_evidence(), now_iso=iso(T_RESP + 33),
                                  timing={"refilter_ms": 812.0, "fire_received_at": T_RESP + 30.5})
    assert row["verdict"] == "PASS" and service.list_pending_refilters(databases[0]) == []
    sent = service.dispatch_outbox(*databases, now_iso=iso(T_RESP + 34))
    assert [r.get("state") for r in sent] == ["sent"], sent
    size = [s for s in store.get_ticket(databases[0], t)["stages"] if s["stage"] == "size"][0]
    assert size["inputs"]["request"]["payload"]["desk_metadata"]["verdict_price"]["ref_price_usd"] == pytest.approx(LEVEL)
    with pytest.raises(ValueError):
        service.record_refilter(databases[0], t, evidence=_evidence(), now_iso=iso(T_RESP + 35), timing={})


def test_a_failed_fresh_filter_refuses_the_order_by_name(databases, monkeypatch):
    t = _fired(databases, monkeypatch)
    ev = _evidence()
    ev["checks"] = {k: {**v, "status": "unknown", "reason": "rpc_failed"} for k, v in ev["checks"].items()}
    row = service.record_refilter(databases[0], t, evidence=ev, now_iso=iso(T_RESP + 33), timing={})
    assert row["verdict"] == "SKIP" and store.get_ticket(databases[0], t)["state"] == "refused"
    assert service.dispatch_outbox(*databases, now_iso=iso(T_RESP + 34)) == []


def test_no_fresh_filter_inside_the_fire_ttl_is_a_named_timeout(databases, monkeypatch):
    t = _fired(databases, monkeypatch)
    service.dispatch_outbox(*databases, now_iso=iso(T_RESP + 30.5 + 21))     # enter_ttl_s 20 from the fire receipt
    head = store.get_ticket(databases[0], t)
    assert head["state"] == "refused" and [s["reason"] for s in head["stages"] if s["stage"] == "size"] == ["refilter_timeout"]
    size = [s for s in head["stages"] if s["stage"] == "size"][0]
    assert size["inputs"]["timing"]["waited_ms"] == 20000.0 and size["inputs"]["timing"]["fire_received_at"] == T_RESP + 30.5


def test_child_tickets_keep_the_root_arm():
    assert service.ticket_root("desk:U:fixture:noise:noise") == "desk:U:fixture"
    assert service.standing_arms(service.ticket_root("desk:U:fx1:drift")) == service.standing_arms("desk:U:fx1")


def test_young_coin_alert_rejudges_at_most_twice_then_skips(databases, monkeypatch):
    from datetime import datetime, timedelta
    t = ticket_in(databases, monkeypatch, pol=V2)                          # arm B
    out = decide(databases, t, HOLD, asof=ASOF, structure=vol_structure(4.0, 5))
    inputs = vet(databases, t)["inputs"]
    assert out["reason"] == "px_hold_noise_watch" and inputs["order_mode"] == "alert_young" and inputs["rejudge_count"] == 0
    ids, now = [t], datetime.fromisoformat(inputs["response_at"])
    for n in (1, 2):
        now = now + timedelta(seconds=31)
        [child] = service.rejudge_noise_watches(databases[0], now_iso=now.isoformat())
        assert store.get_ticket(databases[0], child)["candidate"]["noise_rejudge"].get("count", 1) == n
        out = decide(databases, child, {**HOLD, "k": child}, asof={**ASOF, "candidate": store.get_ticket(databases[0], child)["candidate"]},
                     structure=vol_structure(4.0, 5), call_started_at=now.isoformat(), response_at=now.isoformat(),
                     now_iso=now.isoformat())
        ids.append(child)
        if n == 1:
            assert out["reason"] == "px_hold_noise_watch" and vet(databases, child)["inputs"]["rejudge_count"] == 1
    assert out["action"] == "SKIP" and out["reason"] == "cond_unavailable"     # the 2nd re-judge is final
    assert ids[-1] == t + ":noise:noise"
