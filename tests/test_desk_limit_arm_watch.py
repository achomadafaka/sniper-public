"""LIMIT arm, desk side (operator 2026-10-23, Gamma prereg 0529ce3c6): an above-sheet quote refused by S6 becomes ONE 60 s
watch at the sheet price; a print at or below it fires the entry capped at fire + fee + impact (tolerance 0); no print =
a recorded limit_watch_miss. LOG tickets and old cohorts are unchanged."""
import json
from datetime import datetime, timedelta

import pytest

from sniper.persistence import db_memecoin_desk as store
from sniper.services.memecoin_desk_universe import StandingBook
from sniper.settings import memecoin as cfg
from test_memecoin_desk_contract import candidate, evidence, policy, verdict
from test_memecoin_desk_pipeline import AT, LATER, POLICY, RESPONSE, api, databases, decide  # noqa: F401
from test_desk_watch_v22_pipeline import STANDING, PLAN, BASIS, vol_structure

REASON = "desk limit watch: quote +2.10% over the sheet price net of fee and impact"
ARMED = {**POLICY, "standing_orders": {**STANDING, "limit_arm": True}}


def entered(databases, monkeypatch, pol=ARMED):
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_STANDING_ORDERS", True)
    cohort = api().provision_cohort(*databases, producer="U", policy=pol, prompt_version="desk-judge-v23", created_at=AT)
    t = api().ingest_candidate(databases[0], candidate(), cohort_id=cohort, now_iso=AT)["ticket_id"]
    api().evaluate_candidate(databases[0], t, evidence=evidence(), policy=policy(), now_iso=AT)
    out = decide(databases, t, {**verdict(), "p": PLAN},
                 asof={"candidate": candidate(), "meta": {"rank": {"price_now": BASIS}}}, structure=vol_structure(4.0, 30))
    assert out["action"] == "ENTER"
    request = store.list_outbox(databases[0])[0]
    api().dispatch_outbox(*databases, now_iso=RESPONSE)
    return t, request["intent_id"]


def refuse(db, intent_id, *, arm=1, reason=REASON, status="skipped_chase"):
    meta = json.loads(db[1].execute("SELECT desk_metadata_json FROM memecoin_manual_intents WHERE intent_id=?",
                                    (intent_id,)).fetchone()[0])
    meta["verdict_price"] = {**meta["verdict_price"], "limit_arm": arm}
    db[1].execute("UPDATE memecoin_manual_intents SET state='refused', result_status=?, reason=?, resolved_at=?, "
                  "desk_metadata_json=? WHERE intent_id=?", (status, reason, LATER, json.dumps(meta), intent_id))
    db[1].commit()


def parent_vp(db, t):
    size = [x for x in store.get_ticket(db[0], t)["stages"] if x["stage"] == "size"][0]
    return size["inputs"]["request"]["payload"]["desk_metadata"]["verdict_price"]


def watching(databases, monkeypatch):
    t, intent = entered(databases, monkeypatch)
    refuse(databases, intent)
    api().reconcile_intents(*databases, now_iso=LATER)
    return t


def t_of(iso, s=0.0):
    return datetime.fromisoformat(iso).timestamp() + s


def fire(price, at, **kw):
    row = {"slot": 9, "signature": "sig", "ordinal": 1, "tx_index": 3, "block_time": at, "received_at": at + 0.5,
           "price_sol": price, "real_quote_sol": 40.0, "sol_usd": 180.0, "sol_at": at - 5, "curve_pct": 60.0}
    row.update(kw)
    return row


def iso(t):
    return datetime.fromtimestamp(t, datetime.fromisoformat(LATER).tzinfo).isoformat()


def test_refusal_opens_one_watch_at_the_sheet_price(databases, monkeypatch):
    t, intent = entered(databases, monkeypatch)
    refuse(databases, intent)
    out = api().reconcile_intents(*databases, now_iso=LATER)
    assert [r["state"] for r in out] == ["refused", "limit_watching"]
    child = store.get_ticket(databases[0], t + ":limit")
    assert child["state"] == "waiting"
    trig = [s for s in child["stages"] if s["stage"] == "vet"][0]["inputs"]["trigger"]
    assert trig["kind"] == "px_limit" and trig["level_price_sol"] == BASIS and trig["confirm"] == 1
    assert trig["expires_at"] == iso(t_of(LATER, 60))
    assert api().reconcile_intents(*databases, now_iso=LATER) == []                     # idempotent


def test_print_at_or_below_sheet_fires_with_zero_tolerance(databases, monkeypatch):
    t, intent = entered(databases, monkeypatch)
    refuse(databases, intent)
    api().reconcile_intents(*databases, now_iso=LATER)
    at = t_of(LATER, 10)
    with pytest.raises(ValueError, match="fire_price"):
        api().record_fire(databases[0], t + ":limit", fire(BASIS * 1.001, at), now_iso=iso(at + 1))
    got = api().record_fire(databases[0], t + ":limit", fire(BASIS, at), now_iso=iso(at + 1))
    vp, parent = got["output"]["verdict_price"], parent_vp(databases, t)
    assert vp["tolerance_pct"] == 0.0 and vp["cap_pct"] == pytest.approx(vp["fee_pct"] + vp["impact_pct"])
    assert vp["ref_price_usd"] == parent["ref_price_usd"] and "limit_arm" not in vp           # frozen USD ref; no second watch


def test_no_print_is_a_recorded_miss(databases, monkeypatch):
    t, intent = entered(databases, monkeypatch)
    refuse(databases, intent)
    api().reconcile_intents(*databases, now_iso=LATER)
    assert api().expire_watches(databases[0], now_iso=iso(t_of(LATER, 59))) == []
    assert api().expire_watches(databases[0], now_iso=iso(t_of(LATER, 61))) == [t + ":limit"]
    exp = [s for s in store.get_ticket(databases[0], t + ":limit")["stages"] if s["stage"] == "expire"][0]
    assert exp["reason"] == "limit_watch_miss"


@pytest.mark.parametrize("arm,reason,status", [(0, REASON, "skipped_chase"), (1, "desk price cap: over", "skipped_chase"),
                                               (1, REASON, "refused")])
def test_log_arm_and_other_refusals_open_nothing(databases, monkeypatch, arm, reason, status):
    t, intent = entered(databases, monkeypatch)
    refuse(databases, intent, arm=arm, reason=reason, status=status)
    assert [r["state"] for r in api().reconcile_intents(*databases, now_iso=LATER)] == ["refused"]
    assert store.get_ticket(databases[0], t + ":limit") is None


def test_cohort_without_limit_arm_opens_nothing(databases, monkeypatch):
    t, intent = entered(databases, monkeypatch, pol={**POLICY, "standing_orders": STANDING})
    refuse(databases, intent)
    assert [r["state"] for r in api().reconcile_intents(*databases, now_iso=LATER)] == ["refused"]


def test_crash_between_fill_and_watch_resumes(databases, monkeypatch):
    t, intent = entered(databases, monkeypatch)
    refuse(databases, intent)
    real = api()._limit_watch
    monkeypatch.setattr(api(), "_limit_watch", lambda *a, **k: None)
    api().reconcile_intents(*databases, now_iso=LATER)
    monkeypatch.setattr(api(), "_limit_watch", real)
    assert [r["state"] for r in api().reconcile_intents(*databases, now_iso=LATER)] == ["limit_watching"]


def test_standing_book_px_limit_fires_at_or_below_only():
    book = StandingBook()
    trig = {"kind": "px_limit", "level_price_sol": 1.0, "level_price_usd": 100.0, "confirm": 1,
            "expires_at": "2026-10-23T12:01:00+00:00"}
    t0 = datetime.fromisoformat("2026-10-23T12:00:00+00:00").timestamp()
    book.load([("T", "M", json.dumps(trig), "2026-10-23T12:00:00+00:00")], t0)
    on = lambda p, dt, fx: book.on_trade(mint="M", price_sol=p, block_time=t0 + dt, received_at=t0 + dt + 0.2,   # noqa: E731
                                         gap_since_load=False, sol_usd=fx)
    assert on(1.01, 1, 100.0) == []                       # over the SOL level
    assert on(1.0, 2, None) == []                         # unknown FX never fires
    assert on(1.0, 3, 110.0) == []                        # same SOL price, FX up: over the USD ceiling
    assert [o["ticket_id"] for o in on(1.0, 4, 100.0)] == ["T"]


def test_an_fx_refusal_never_burns_the_watch(databases, monkeypatch):
    """eCat BLOCKER on 4b3e40593 (adapted witness): the producer checks the frozen USD ceiling with the fresh FX before it
    commits a fire, so an FX-rich print is no fire at all, and a later valid print inside the 60 s still fires."""
    t = watching(databases, monkeypatch)
    book, at = StandingBook(), t_of(LATER, 2)
    book.load(databases[0].execute(StandingBook.SQL).fetchall(), at)
    mint = databases[0].execute("SELECT mint FROM desk_ticket_heads WHERE ticket_id=?", (t,)).fetchone()[0]
    level = parent_vp(databases, t)["ref_price_usd"]
    rich = level / BASIS * 1.10
    assert book.on_trade(mint=mint, price_sol=BASIS, block_time=at, received_at=at, gap_since_load=False, sol_usd=rich) == []
    ok = level / BASIS * 0.95
    later = book.on_trade(mint=mint, price_sol=BASIS * 0.9, block_time=at + 2, received_at=at + 2, gap_since_load=False,
                          sol_usd=ok)
    assert [o["ticket_id"] for o in later] == [t + ":limit"]
    book.commit(t + ":limit")
    got = api().record_fire(databases[0], t + ":limit", fire(BASIS * 0.9, at + 2, sol_usd=ok), now_iso=iso(at + 3))
    assert got["output"]["verdict_price"]["ref_price_usd"] == level


def test_fx_rise_at_the_same_sol_price_never_buys_above_the_usd_sheet(databases, monkeypatch):
    """eCat FX RC: SOL/USD up 10 % with the SOL price unchanged must not buy 10 % above the original USD sheet."""
    t = watching(databases, monkeypatch)
    level = parent_vp(databases, t)["ref_price_usd"]
    at = t_of(LATER, 10)
    rich = level / BASIS * 1.10                                # this FX puts BASIS SOL at +10 % over the USD sheet
    with pytest.raises(ValueError, match="fire_price_usd"):
        api().record_fire(databases[0], t + ":limit", fire(BASIS, at, sol_usd=rich), now_iso=iso(at + 1))


def test_fx_fall_fires_at_the_frozen_usd_ceiling(databases, monkeypatch):
    t = watching(databases, monkeypatch)
    level = parent_vp(databases, t)["ref_price_usd"]
    at = t_of(LATER, 10)
    got = api().record_fire(databases[0], t + ":limit", fire(BASIS, at, sol_usd=level / BASIS * 0.9), now_iso=iso(at + 1))
    assert got["output"]["verdict_price"]["ref_price_usd"] == level


def test_unknown_fx_refuses(databases, monkeypatch):
    t = watching(databases, monkeypatch)
    at = t_of(LATER, 10)
    with pytest.raises(ValueError, match="fire_sol_stale"):
        api().record_fire(databases[0], t + ":limit", fire(BASIS, at, sol_at=at - 500), now_iso=iso(at + 1))


def test_thinner_liquidity_at_the_fire_never_widens_the_allowance(databases, monkeypatch):
    t = watching(databases, monkeypatch)
    parent = parent_vp(databases, t)
    at = t_of(LATER, 10)
    vp = api().record_fire(databases[0], t + ":limit", fire(BASIS, at, real_quote_sol=0.01), now_iso=iso(at + 1))[
        "output"]["verdict_price"]
    assert vp["impact_pct"] == parent["impact_pct"]
    assert vp["cap_price_usd"] <= parent["ref_price_usd"] * (1 + (parent["fee_pct"] + parent["impact_pct"]) / 100) * (1 + 1e-12)


def test_writer_refuses_a_widened_ceiling(databases, monkeypatch):
    from sniper.services import memecoin_desk as desk
    t = watching(databases, monkeypatch)
    child = store.get_ticket(databases[0], t + ":limit")
    vet = [x for x in child["stages"] if x["stage"] == "vet"][0]
    parent = parent_vp(databases, t)
    wide = {"output": {"verdict_price": {**parent, "tolerance_pct": 0.0, "cap_price_usd": parent["ref_price_usd"] * 1.2}},
            "inputs": {"fire": fire(BASIS, t_of(LATER, 10))}}
    with pytest.raises(ValueError, match="limit_ceiling_widened"):
        desk._queue_trigger(databases[0], child, vet, wide)


def test_pumpswap_tickets_get_no_arm():
    from sniper.services.memecoin_desk import assign_limit_arm, limit_arm_bit
    st, enter = {"limit_arm": True}, {"action": "ENTER"}
    assert assign_limit_arm(st, enter, {"venue": "pumpswap"}, "t", enabled=True) is None
    assert assign_limit_arm(st, enter, {}, "t", enabled=True) == limit_arm_bit("t")
    assert assign_limit_arm(st, enter, {}, "t", enabled=False) is None
    assert assign_limit_arm(st, {"action": "WAIT"}, {}, "t", enabled=True) is None
    assert assign_limit_arm({}, enter, {}, "t", enabled=True) is None


def test_a_limit_child_stranded_past_the_window_expires(databases, monkeypatch):
    from datetime import timedelta
    t, intent = entered(databases, monkeypatch)
    refuse(databases, intent)
    monkeypatch.setattr(api(), "evaluate_candidate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash")))
    api().reconcile_intents(*databases, now_iso=LATER)
    assert store.get_ticket(databases[0], t + ":limit")["state"] == "open"
    late = (datetime.fromisoformat(LATER) + timedelta(seconds=601)).isoformat()
    out = api().reconcile_intents(*databases, now_iso=late)
    assert {"ticket_id": t + ":limit", "state": "expired", "reason": "child_unfinished"} in out
    assert store.get_ticket(databases[0], t + ":limit")["state"] == "expired"


def test_causal_fx_boundaries():
    """eCat on 7d8dd4998: future FX, FX older than the writer's 120 s (whatever the producer knob), or a non-finite FX never
    fires a px_limit (so the writer can never burn the watch on it)."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("uw_fx", Path(__file__).resolve().parents[1] / "scripts" / "memecoin_desk_universe_worker.py")
    W = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(W)
    assert W.causal_fx(100.0, 1000.0, 1000.0, 120) == 100.0
    assert W.causal_fx(100.0, 1001.0, 1000.0, 120) is None              # stamped in the future
    assert W.causal_fx(100.0, 880.0, 1000.0, 240) == 100.0              # age 120: the edge
    assert W.causal_fx(100.0, 879.0, 1000.0, 240) is None               # age 121 with the knob at 240: the writer's cap wins
    assert W.causal_fx(float("nan"), 1000.0, 1000.0, 120) is None and W.causal_fx(0.0, 1000.0, 1000.0, 120) is None
    assert W.causal_fx(100.0, None, 1000.0, 120) is None


def test_writer_refuses_future_fx(databases, monkeypatch):
    t = watching(databases, monkeypatch)
    at = t_of(LATER, 10)
    with pytest.raises(ValueError, match="fire_sol_stale"):
        api().record_fire(databases[0], t + ":limit", fire(BASIS, at, sol_at=at + 5), now_iso=iso(at + 1))
