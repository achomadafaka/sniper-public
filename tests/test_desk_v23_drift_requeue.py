"""v23 fill-drift re-judge: a drift refusal opens ONE fresh JUDGE ticket with the drift on its sheet; a second drift
refusal on it is final; older cohorts and other refusals never re-queue."""
import json

import pytest

from sniper.persistence import db_memecoin_desk as store
from sniper.services import memecoin_desk_judge as J
from sniper.services import memecoin_desk_contract as C
from test_memecoin_desk_contract import candidate, evidence, policy
from test_memecoin_desk_pipeline import AT, LATER, POLICY, RESPONSE, api, databases, decide  # noqa: F401

VP_EXTRA = {"noise_pct": 0.39, "drift_k": 2.0, "drift_limit_pct": 0.79}
REASON = ("desk drift: quote is +4.90% over the verdict price net of fee and impact (limit 0.79% = 2.0 x noise 0.390%)")


def entered(databases, pol):
    cohort = api().provision_cohort(*databases, producer="U", policy=pol, prompt_version="desk-judge-v6", created_at=AT)
    t = api().ingest_candidate(databases[0], candidate(), cohort_id=cohort, now_iso=AT)["ticket_id"]
    api().evaluate_candidate(databases[0], t, evidence=evidence(), policy=policy(), now_iso=AT)
    decide(databases, t)
    request = store.list_outbox(databases[0])[0]
    api().dispatch_outbox(*databases, now_iso=RESPONSE)
    return t, request["intent_id"]


def refuse(db, intent_id, reason=REASON, status="skipped_chase"):
    row = db[1].execute("SELECT desk_metadata_json FROM memecoin_manual_intents WHERE intent_id=?", (intent_id,)).fetchone()
    meta = json.loads(row[0])
    meta["verdict_price"] = {**meta["verdict_price"], **VP_EXTRA}
    db[1].execute("UPDATE memecoin_manual_intents SET state='refused', result_status=?, reason=?, resolved_at=?, "
                  "desk_metadata_json=? WHERE intent_id=?", (status, reason, LATER, json.dumps(meta), intent_id))
    db[1].commit()
    return meta["verdict_price"]


ON = {**POLICY, "drift_requeue": "drift-requeue-v1"}


def test_drift_refusal_opens_one_rejudge_ticket_with_the_drift_on_its_sheet(databases):
    t, intent = entered(databases, ON)
    vp = refuse(databases, intent)
    out = api().reconcile_intents(*databases, now_iso=LATER)
    assert [r["state"] for r in out] == ["refused", "drift_requeued"]
    new = store.get_ticket(databases[0], t + ":drift")
    assert new["state"] == "open" and [s["verdict"] for s in new["stages"] if s["stage"] == "hard_filter"] == ["PASS"]
    drift = J._drift_block(new)
    assert drift["drift_pct"] == 4.9 and drift["limit_pct"] == 0.79 and drift["sheet_price_usd"] == vp["ref_price_usd"]
    assert drift["quote_price_usd"] == pytest.approx(vp["ref_price_usd"] * (1 + (4.9 + vp["fee_pct"] + vp["impact_pct"]) / 100))
    assert C.render_judge_sheet(new["candidate"], drift=drift)["drift"] == drift
    assert api().reconcile_intents(*databases, now_iso=LATER) == []          # idempotent: no second ticket


def test_second_drift_refusal_is_final(databases):
    from test_memecoin_desk_contract import verdict
    t, intent = entered(databases, ON)
    refuse(databases, intent)
    api().reconcile_intents(*databases, now_iso=LATER)
    assert decide(databases, t + ":drift", verdict(k=t + ":drift"), now_iso=LATER, call_started_at=LATER,
                  response_at=LATER)["action"] == "ENTER"
    req = [r for r in store.list_outbox(databases[0]) if r["ticket_id"] == t + ":drift"][0]
    api().dispatch_outbox(*databases, now_iso=LATER)
    refuse(databases, req["intent_id"])
    out = api().reconcile_intents(*databases, now_iso=LATER)
    assert [(r["state"], r.get("reason")) for r in out] == [("refused", None), ("drift_requeue_refused", "drift_requeue_final")]


def test_old_cohort_never_requeues_but_keeps_the_drift_fact(databases):
    t, intent = entered(databases, POLICY)
    refuse(databases, intent)
    out = api().reconcile_intents(*databases, now_iso=LATER)
    assert [r["state"] for r in out] == ["refused"] and out[0]["drift"]["drift_pct"] == 4.9
    assert store.get_ticket(databases[0], t + ":drift") is None


@pytest.mark.parametrize("reason,status", [("confirmed failed", "refused"), (REASON, "refused"),
                                           ("desk price cap: quote over the cap", "skipped_chase")])
def test_other_refusals_never_requeue(databases, reason, status):
    t, intent = entered(databases, ON)
    refuse(databases, intent, reason=reason, status=status)
    assert [r["state"] for r in api().reconcile_intents(*databases, now_iso=LATER)] == ["refused"]


def test_forged_claim_without_the_refusal_is_unproven(databases):
    t, intent = entered(databases, ON)
    refuse(databases, intent, reason="confirmed failed", status="refused")
    api().reconcile_intents(*databases, now_iso=LATER)
    drift = {"sheet_price_usd": 1e-5, "quote_price_usd": 1.1e-5, "drift_pct": 5.0, "noise_pct": 1.0, "k": 2.0, "limit_pct": 2.0}
    c = {**{k: v for k, v in candidate().items()}, "ticket_id": "desk:U:forged", "drift_requeue": {"prev_ticket_id": t, "drift": drift}}
    got = store.record_candidate(databases[0], C.validate_candidate(c), cohort_id=store.get_ticket(databases[0], t)["cohort_id"],
                                 now_iso=LATER, cooldown_s=900)
    assert got["is_primary"] is False and got["reason"] == "drift_requeue_unproven"


def test_a_refused_claim_is_not_retried_every_pass(databases):
    from test_memecoin_desk_contract import verdict
    t, intent = entered(databases, ON)
    refuse(databases, intent)
    api().reconcile_intents(*databases, now_iso=LATER)
    decide(databases, t + ":drift", verdict(k=t + ":drift"), now_iso=LATER, call_started_at=LATER, response_at=LATER)
    req = [r for r in store.list_outbox(databases[0]) if r["ticket_id"] == t + ":drift"][0]
    api().dispatch_outbox(*databases, now_iso=LATER)
    refuse(databases, req["intent_id"])
    api().reconcile_intents(*databases, now_iso=LATER)
    assert api().reconcile_intents(*databases, now_iso=LATER) == []
    assert api().reconcile_intents(*databases, now_iso=LATER) == []


def test_an_old_cohort_drift_fill_is_cheap_and_silent(databases):
    t, intent = entered(databases, POLICY)
    refuse(databases, intent)
    api().reconcile_intents(*databases, now_iso=LATER)
    assert api().reconcile_intents(*databases, now_iso=LATER) == []
