"""chase-limit-v1 (Alpha 10-26 option 3, Gamma prereg b7d133fc8): on a replica cohort (no standing_orders, prompt
unchanged) an ENTER refused skipped_chase becomes, in arm B (bit 1), ONE 2 h standing limit at the verdict price."""
import json

import pytest

from sniper.persistence import db_memecoin_desk as store
from sniper.services import memecoin_desk as service
from sniper.settings import memecoin as cfg
from test_memecoin_desk_contract import candidate, evidence, policy, verdict
from test_memecoin_desk_pipeline import AT, LATER, POLICY, RESPONSE, api, databases, decide  # noqa: F401
from test_desk_limit_arm_watch import refuse, parent_vp, t_of, iso
from test_desk_watch_v22_pipeline import BASIS

CHASE = {**POLICY, "chase_limit": {"version": "chase-limit-v1", "order_max_s": 7200, "book_max": 50,
                                   "graduation_guard_pct": 97.0}}


def entered(databases, cand=None, pol=CHASE):
    c = cand or candidate()
    cohort = api().provision_cohort(*databases, producer="U", policy=pol, prompt_version="desk-judge-v19", created_at=AT)
    t = api().ingest_candidate(databases[0], c, cohort_id=cohort, now_iso=AT)["ticket_id"]
    api().evaluate_candidate(databases[0], t, evidence=evidence(c), policy=policy(), now_iso=AT)
    out = decide(databases, t, verdict(k=t), asof={"candidate": c, "meta": {"rank": {"price_now": BASIS}}})
    assert out["action"] == "ENTER"
    request = store.list_outbox(databases[0])[0]
    api().dispatch_outbox(*databases, now_iso=RESPONSE)
    return t, request["intent_id"]


def chased(databases, **kw):
    t, intent = entered(databases, **kw)
    refuse(databases, intent, arm=0, reason="desk verdict price: exact quote is +8.81% over the verdict price (cap 4.64%)")
    return t, api().reconcile_intents(*databases, now_iso=LATER)


def test_arm_b_skipped_chase_becomes_a_two_hour_order_at_the_verdict_price(databases):
    t, out = chased(databases)                                              # desk:U:fixture: standing_arm 1
    assert "limit_watching" in [r["state"] for r in out]
    child = store.get_ticket(databases[0], t + ":limit")
    vet = [s for s in child["stages"] if s["stage"] == "vet"][0]
    trig = vet["inputs"]["trigger"]
    assert vet["reason"] == "chase_limit" and trig["order"] == "standing-v2" and trig["order_mode"] == "chase_limit"
    assert trig["level_price_usd"] == pytest.approx(parent_vp(databases, t)["ref_price_usd"])
    assert trig["expires_at"] == iso(t_of(LATER, 7200)) and vet["inputs"]["arms"]["standing_arm"] == 1
    assert trig["cancel_band_usd_mcap"] == list(service.contract.mcap_band(POLICY["hard_filter"]))


def test_arm_a_skipped_chase_stays_refused(databases):
    c = {**candidate(), "ticket_id": "desk:U:fx1", "mint": candidate()["mint"][:-1] + "A"}  # standing_arm 0
    t, out = chased(databases, cand=c)
    assert store.get_ticket(databases[0], t + ":limit") is None


def test_other_refusals_and_old_cohorts_open_nothing(databases):
    t, intent = entered(databases)
    refuse(databases, intent, arm=0, reason="liquidity", status="skipped_liquidity")
    api().reconcile_intents(*databases, now_iso=LATER)
    assert store.get_ticket(databases[0], t + ":limit") is None


def test_kill_knob_off_opens_nothing_and_expires_waiting_orders(databases, monkeypatch):
    t, _ = chased(databases)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_CHASE_LIMIT", False)
    assert service.expire_watches(databases[0], now_iso=iso(t_of(LATER, 10))) == [t + ":limit"]


def test_the_replica_judge_path_is_unchanged(databases):
    """No standing_orders: a W answer is judged under the old grammar, exactly as the replica (no cond_unavailable)."""
    cohort = api().provision_cohort(*databases, producer="U", policy=CHASE, prompt_version="desk-judge-v19", created_at=AT)
    assert service._standing(store.get_cohort(databases[0], cohort)["policy"]) is None


def test_malformed_chase_rule_refuses_provisioning(databases):
    with pytest.raises(ValueError):
        api().provision_cohort(*databases, producer="U", policy={**CHASE, "chase_limit": {"version": "x"}},
                               prompt_version="desk-judge-v19", created_at=AT)


def test_a_chase_order_fires_on_usd_then_waits_for_its_fresh_filter(databases):
    from test_desk_limit_arm_watch import fire
    t, _ = chased(databases)
    child = t + ":limit"
    level = parent_vp(databases, t)["ref_price_usd"]
    at = t_of(LATER, 30)
    service.record_fire(databases[0], child, fire(level / 180.0 * 0.99, at), now_iso=iso(at + 1))
    assert service.dispatch_outbox(*databases, now_iso=iso(at + 2)) == []
    service.record_refilter(databases[0], child, evidence=evidence(), now_iso=iso(at + 3), timing={"refilter_ms": 500.0})
    sent = service.dispatch_outbox(*databases, now_iso=iso(at + 4))
    assert [r.get("state") for r in sent] == ["sent"], sent


def test_every_order_end_writes_one_section4_row(databases):
    t, _ = chased(databases)
    child = t + ":limit"
    assert service.record_order_ends(databases[0], now_iso=iso(t_of(LATER, 5))) == []        # live: no row
    service.expire_watches(databases[0], now_iso=iso(t_of(LATER, 7201)))        # writes the end row itself
    row = [s for s in store.get_ticket(databases[0], child)["stages"] if s["stage"] == "order_end"][0]["output"]
    assert row["end_reason"] == "max_life" and row["mode"] == "chase_limit" and row["arm"] == 1
    assert row["chase_gap_pct"] == 8.81 and row["level_unit_label"] == "usd_mcap" and row["life_s"] == pytest.approx(7201, abs=2)
    assert service.record_order_ends(databases[0], now_iso=iso(t_of(LATER, 7203))) == []    # once


def test_a_refused_fresh_filter_ends_as_touched_refused(databases):
    from test_desk_limit_arm_watch import fire
    t, _ = chased(databases)
    child, at = t + ":limit", t_of(LATER, 30)
    service.record_fire(databases[0], child, fire(parent_vp(databases, t)["ref_price_usd"] / 180.0 * 0.99, at),
                        now_iso=iso(at + 1))
    ev = evidence()
    ev["checks"] = {k: {**v, "status": "unknown", "reason": "rpc_failed"} for k, v in ev["checks"].items()}
    service.record_refilter(databases[0], child, evidence=ev, now_iso=iso(at + 2), timing={"refilter_ms": 700.0})
    row = [s for s in store.get_ticket(databases[0], child)["stages"] if s["stage"] == "order_end"][0]["output"]
    assert row["end_reason"].startswith("touched_refused:filter:") and row["fresh_filter_verdict_at_touch"] == "SKIP"
    assert row["refilter_ms"] == 700.0 and row["touch_at"] == at


def test_kill_knob_off_the_refusal_is_exactly_todays(databases, monkeypatch):
    """Knob 0: the parent's fill row is byte-identical to a cohort without chase_limit, and nothing else is written."""
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_CHASE_LIMIT", False)
    t, out = chased(databases)
    fill = [s for s in store.get_ticket(databases[0], t)["stages"] if s["stage"] == "fill"][0]
    assert "limit_watch" not in fill["output"] and store.get_ticket(databases[0], t + ":limit") is None
    assert [r["state"] for r in out] == ["refused"]


def test_restart_restores_refilter_rpc_spend(tmp_path):
    """Zeta on 18e74e1de: the daily/per-minute RPC budget rebuilt after a restart counts refilter reads too."""
    import importlib.util
    import time
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("asm", Path(__file__).resolve().parents[1] / "scripts/memecoin_desk_assembler.py")
    A = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(A)
    now = time.time()
    with open(tmp_path / ("%s.jsonl" % A.utc_day(now)), "w") as fh:
        for stage in ("safety_reserve", "refilter_reserve"):
            fh.write(json.dumps({"stage": stage, "reserved_at": now - 5, "rpc_calls": 3}) + "\n")
    calls, window = A.counters(tmp_path, now)
    assert calls == 6 and len(window) == 2


def test_restart_budget_keeps_retry_rows_daily_only(tmp_path):
    """main aaea55ac1: a safety_retry_reserve charges the day, not a minute ticket; refilter_reserve charges both."""
    import importlib.util
    import time
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("asm2", Path(__file__).resolve().parents[1] / "scripts/memecoin_desk_assembler.py")
    A = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(A)
    now = time.time()
    with open(tmp_path / ("%s.jsonl" % A.utc_day(now)), "w") as fh:
        for stage in ("safety_reserve", "safety_retry_reserve", "refilter_reserve", "refilter"):
            fh.write(json.dumps({"stage": stage, "reserved_at": now - 5, "rpc_calls": 3}) + "\n")
    calls, window = A.counters(tmp_path, now)
    assert calls == 9 and len(window) == 2


def test_a_filled_chase_order_gets_its_end_row_and_keeps_its_head_state(databases):
    """Zeta on 0ed40df71: the end row must never move a filled (or later closed) head."""
    from test_desk_limit_arm_watch import fire
    from test_memecoin_desk_pipeline import intents
    from sniper.persistence import db_memecoin as ledger
    t, _ = chased(databases)
    child, at = t + ":limit", t_of(LATER, 30)
    service.record_fire(databases[0], child, fire(parent_vp(databases, t)["ref_price_usd"] / 180.0 * 0.99, at), now_iso=iso(at + 1))
    service.record_refilter(databases[0], child, evidence=evidence(), now_iso=iso(at + 2), timing={"refilter_ms": 400.0})
    service.dispatch_outbox(*databases, now_iso=iso(at + 3))
    request = [s for s in store.get_ticket(databases[0], child)["stages"] if s["stage"] == "size"][0]["inputs"]["request"]
    frozen = json.dumps({"source": {"desk": request["payload"]["desk_metadata"]}})
    pid = ledger.open_position(databases[1], source_user_id=None, source_handle="fixture", mint=candidate()["mint"],
        ticker=None, entry_price=0.0001, entry_notional=25, entry_fill_source="chain", entry_lag_s=0, their_size_usd=0,
        opened_at=iso(at + 4), execution_mode="real", network_id="solana", signal_source="desk", funded_contract_json=frozen)
    intents.resolve(databases[1], request["intent_id"], state="filled", now_iso=iso(at + 4), position_id=pid,
                    result_status="filled", resolved_size_usd=25)
    service.reconcile_intents(*databases, now_iso=iso(at + 5))
    head = store.get_ticket(databases[0], child)
    assert head["state"] == "filled"
    row = [s for s in head["stages"] if s["stage"] == "order_end"][0]["output"]
    assert row["end_reason"] == "touched_filled" and row["position_id"] == pid
