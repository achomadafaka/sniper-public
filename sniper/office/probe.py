"""Office Task 15 step 2 probe driver: ONE real candidate end to end, labelled non-evaluation (Alpha 2026-10-29).

Chain: sheet -> frozen bundle -> 2 blind judges (claude + gpt, served_model recorded) -> aggregate -> Risk -> Trader
(paper) [front], then the always-on paper watcher exits the order, then a probe grade [back]; the auditor runs under
its own authority. Blinding lives here: every judge receives the same view built only
from the persisted, re-verified sheet receipt and its frozen bundle hash, never another judge's memo; the aggregate is
computed only after both answers are in. Model calls are injected (judge_call), so this module makes none itself.
Every link raises ProbeStop with a typed reason instead of continuing on an unknown."""
from __future__ import annotations

import contextlib
import hashlib
import json
from dataclasses import dataclass

from sniper.office import grader, ledger as L, risk as R, trader as T
from sniper.office import store as S
from sniper.office.store import canonical_json
from sniper.office.tools import sheet as SH

PROBE_LABEL = "non-evaluation"
PROBE_EPOCH = "office-probe-t15-1"           # never office-pilot-discovery-1; never in a scored export
PROBE_BOOK = "probe-floor"
JUDGE_SLOTS = ("judge_claude", "judge_gpt")   # pilot_org.json: judges_per_pod 2, families claude + gpt
SLOT_FAMILY = {"judge_claude": "claude-", "judge_gpt": "gpt-"}   # served model prefix per slot: two-family proof
UNKNOWN = "UNKNOWN"                           # not a decision: stops the probe before Risk/Trader, never persisted
DECISIONS = ("ENTER", "WAIT", "SKIP")
JUDGE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["decision", "memo"],
                "properties": {"decision": {"type": "string", "enum": list(DECISIONS)},
                               "memo": {"type": "string", "maxLength": 16384}}}


class ProbeStop(RuntimeError):
    """A link could not prove its result; the probe stops there."""


class ProbeRetry(RuntimeError):
    """Not terminal: the phase deadline passed or the lease is no longer ours. Nothing after this point was written;
    the next pass resumes from the persisted stages (Zeta FAIL af4434702)."""


@dataclass(frozen=True)
class TraderContext:
    decision_id: str
    book_id: str = PROBE_BOOK
    actor_id: str = "probe-trader"
    role: str = T.TRADER_ROLE


def sheet_link(store_conn, decision_id, **sheet_kwargs):
    """-> (receipt, bundle_hash). Only a known receipt is frozen; refused/unknown stops the probe."""
    receipt = SH.sheet(store_conn=store_conn, **sheet_kwargs)
    if receipt.get("state") != "known":
        raise ProbeStop("sheet_%s:%s" % (receipt.get("state"), receipt.get("reason")))
    bundle_hash = SH.freeze_bundle(store_conn, decision_id, receipt["cache_key"],
                                   definition_bundle_hash=receipt["definition_bundle_hash"],
                                   strategy_epoch=receipt["strategy_epoch"])
    return receipt, bundle_hash


def resume_sheet(store_conn, decision_id, saved, *, policy, strategy_epoch):
    """The ORIGINAL persisted receipt and bundle, re-verified; never a new sheet (a refreshed clock sample would change
    the cache key and refreezing the decision would be bundle_conflict)."""
    key = saved["cache_key"]
    hit = S.latest(store_conn, "receipts", "cache_key", key)
    receipt = None if hit is None else SH._verify(hit, key, SH.definition_bundle_hash(policy, strategy_epoch),
                                                 strategy_epoch)
    if receipt is None or receipt.get("state") != "known":
        raise ProbeStop("resume_receipt_unverified")
    bundle_hash = SH.freeze_bundle(store_conn, decision_id, key, definition_bundle_hash=receipt["definition_bundle_hash"],
                                   strategy_epoch=strategy_epoch)               # verifies and returns the stored bundle
    if bundle_hash != saved["bundle_hash"]:
        raise ProbeStop("resume_bundle_mismatch")
    return receipt, bundle_hash


def judge_view(decision_id, bundle_hash, receipt):
    """The single judge input. Built from the receipt only: no memo, no judgement, no other seat's output."""
    return canonical_json({"label": PROBE_LABEL, "decision_id": decision_id, "bundle_hash": bundle_hash,
                           "sheet_hash": receipt["sheet_hash"], "sheet": receipt["sheet"]}).decode()


def judges_link(view, judge_call, *, saved=None, on_result=lambda slot, row: None):
    """judge_call(slot, user, schema) -> models.Envelope. Each slot gets the identical view. A failed call, an unknown
    served_model or an off-schema answer is recorded as decision None, never as SKIP or ENTER. saved = slots already
    answered in an earlier pass (reused, never re-asked); on_result(slot, row) persists each answer before the next
    call, so a retry between judges keeps the completed one (Zeta FAIL b8a5fe463)."""
    out = {}
    for slot in JUDGE_SLOTS:
        if saved and slot in saved:
            out[slot] = saved[slot]
            continue
        env = judge_call(slot, view, JUDGE_SCHEMA)
        row = {"served_model": getattr(env, "served_model", None), "attempt_id": getattr(env, "attempt_id", None),
               "decision": None, "memo": None, "reason": None}
        if getattr(env, "outcome", None) != "success":
            row["reason"] = "call_%s:%s" % (getattr(env, "outcome", None), getattr(env, "error_reason", None))
        elif not isinstance(row["served_model"], str) or not row["served_model"]:
            row["reason"] = "served_model_unknown"
        elif not row["served_model"].startswith(SLOT_FAMILY[slot]):
            row["reason"] = "served_model_wrong_family"
        else:
            try:
                answer = json.loads(env.text)
                if (type(answer) is not dict or set(answer) != {"decision", "memo"} or answer["decision"] not in DECISIONS
                        or not isinstance(answer["memo"], str) or len(answer["memo"]) > 16384):
                    raise ValueError
                row["decision"], row["memo"] = answer["decision"], answer["memo"]
            except (ValueError, TypeError, AttributeError):
                row["reason"] = "answer_off_schema"
        on_result(slot, row)
        out[slot] = row
    return out


def aggregate(judgements):
    """ENTER only when every judge answered ENTER; SKIP when all are known and not unanimous ENTER. An incomplete or
    unknown judge set is UNKNOWN, never a SKIP (Zeta FAIL e35b88fab)."""
    if set(judgements) != set(JUDGE_SLOTS):
        return UNKNOWN, "judge_set_incomplete"
    unknown = [s for s in JUDGE_SLOTS if judgements[s]["decision"] is None]
    if unknown:
        return UNKNOWN, "judge_unknown:" + ",".join(unknown)
    if all(judgements[s]["decision"] == "ENTER" for s in JUDGE_SLOTS):
        return "ENTER", None
    return "SKIP", "judges_not_unanimous_enter"


def risk_link(policy, decision, decision_id, bundle_hash, receipt):
    """Risk reads safety from the persisted sheet body, never from a judge."""
    req = receipt["request"]
    bundle = {"decision": decision, "decision_id": decision_id, "bundle_hash": bundle_hash,
              "network_id": req["network_id"], "mint": req["mint"], "pool": req["pool"]}
    return bundle, R.risk_check(policy, bundle=bundle, safety_blocks=receipt["sheet"].get("safety"))


def trader_link(ledger_conn, policy, bundle, risk_result, *, now, sol_usd=None, sol_at=None):
    ctx = TraderContext(decision_id=bundle["decision_id"])
    return T.execute_decision(ledger_conn, policy, context=ctx, action_key="probe:" + bundle["decision_id"],
                              bundle=bundle, risk_result=risk_result, now=now, sol_usd=sol_usd, sol_at=sol_at)


def _order(ledger_conn, order_id):
    row = ledger_conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
    if row is None:
        raise ProbeStop("order_missing")
    return dict(row)


def grade_link(order, *, look_at, bound_usd_per_fill):
    """Probe-epoch economic grade of the one order. The execution receipt hash is over the stored order row."""
    closed = order["state"] == "closed"
    horizon_at = order["closed_at"] if closed else look_at
    execution = {"execution_receipt_hash": hashlib.sha256(canonical_json(order)).hexdigest(),
                 "horizon_at": horizon_at, "as_of_cut": horizon_at, "known_at": look_at,
                 "state": order["state"], "entry_filled_at": order["filled_at"], "closed_at": order["closed_at"],
                 "fill_count": 2 if closed else 1, "partial": False,
                 "net_cashflow_usd": (order["exit_usd"] - order["spend_usd"] - order["costs_usd"]) if closed else None}
    result = grader.economic_outcome(execution, bound_usd_per_fill=bound_usd_per_fill,
                                     horizon_at=horizon_at, look_at=look_at)
    return dict(result, epoch_id=PROBE_EPOCH, label=PROBE_LABEL, order_id=order["order_id"])


def front(*, decision_id, store_conn, ledger_conn, policy, sheet_kwargs, judge_call, now, sol_usd, sol_at, report,
          prior=None, write_fence=None, deadline_at=None, clock=None):
    """Sheet through Trader in one supervisor pass. report(step, payload) persists each link before the next runs.
    prior = the steps already persisted by an earlier pass: saved judgements are reused, never re-asked (a stable
    request id would be refused by the launcher). write_fence() is a context manager wrapping every mutation: it commits
    only if the supervisor lease is still ours at commit time (compare-and-write). deadline_at/clock bound the whole
    phase. An unknown judge set stops before Risk/Trader."""
    prior = prior or {}
    write_fence = write_fence or contextlib.nullcontext

    def guard():
        if deadline_at is not None and clock() > deadline_at:
            raise ProbeRetry("phase_deadline")
    guard()
    with write_fence():
        if "sheet" in prior:
            receipt, bundle_hash = resume_sheet(store_conn, decision_id, prior["sheet"], policy=sheet_kwargs["policy"],
                                                strategy_epoch=sheet_kwargs["strategy_epoch"])
        else:
            receipt, bundle_hash = sheet_link(store_conn, decision_id, **sheet_kwargs)
    if "sheet" not in prior:
        report("sheet", {"cache_key": receipt["cache_key"], "sheet_hash": receipt["sheet_hash"], "bundle_hash": bundle_hash})
    judgements = prior.get("judges")
    if judgements is None:
        saved = {slot: prior["judge:" + slot] for slot in JUDGE_SLOTS if "judge:" + slot in prior}
        judgements = judges_link(judge_view(decision_id, bundle_hash, receipt), judge_call, saved=saved,
                                 on_result=lambda slot, row: report("judge:" + slot, row))
        guard()
        report("judges", judgements)
    decision, why = aggregate(judgements)
    if "aggregate" not in prior:
        report("aggregate", {"decision": decision, "reason": why})
    if decision == UNKNOWN:
        raise ProbeStop("aggregate_unknown:%s" % why)       # no Risk, no Trader receipt: a retry starts clean
    bundle, risk_result = risk_link(policy, decision, decision_id, bundle_hash, receipt)
    if "risk" not in prior:
        report("risk", {"decision": risk_result.decision, "reason": risk_result.reason, "clip_usd": risk_result.clip_usd})
    guard()
    with write_fence():
        rec = trader_link(ledger_conn, policy, bundle, risk_result, now=now, sol_usd=sol_usd, sol_at=sol_at)  # idempotent
    report("trader", rec)
    return rec


def back(ledger_conn, rec, *, look_at, bound_usd_per_fill, report):
    """After the paper watcher has exited the order: grade it. Returns 'graded', 'waiting_exit' or 'no_order'.
    The auditor runs under its own authority (scripts/office_auditor.py), never from the supervisor."""
    if rec["outcome"] != "submitted":
        report("grade", {"state": "no_order", "outcome": rec["outcome"], "reason": rec["reason"]})
        return "no_order"
    order = _order(ledger_conn, rec["order_id"])
    if order["state"] not in ("closed", "censored"):
        return "waiting_exit"
    report("exit", {k: order[k] for k in ("state", "filled_at", "closed_at", "exit_reason")})
    report("grade", grade_link(order, look_at=look_at, bound_usd_per_fill=bound_usd_per_fill))
    return "graded"


def judge_call_over(manifest, prompts, transports, clock, *, decision_id, deadline_s, allow_model_calls):
    """Real judge_call: pilot_models.call over the launcher transports, the same invoke shape as AgentStep. Without
    allow_model_calls it refuses before any transport is touched (Alpha: model calls need a separate go). The request
    id is uuid5(decision_id, slot), so a retried call reuses it and the launcher's reserve() refuses a second billed
    attempt (Zeta caveat on c831958db)."""
    import uuid
    from sniper.office import pilot_models

    def call(slot, user, schema):
        if allow_model_calls is not True:
            raise ProbeStop("model_calls_not_allowed")
        entry, prompt = manifest["seats"][slot], prompts[slot]
        if hashlib.sha256(prompt["text"].encode()).hexdigest() != prompt["sha256"]:
            raise ProbeStop("prompt_pin_mismatch")
        transport = transports[entry["provider"]]
        rid = str(uuid.uuid5(uuid.NAMESPACE_URL, "office-probe:%s:%s" % (decision_id, slot)))

        def invoke(system, user, *, model, deadline_s, schema_obj):
            return transport.client.call(transport.adapter, dict(system=system, user=user, model=model,
                                         deadline_s=deadline_s, schema=schema_obj),
                                         deadline_at=clock() + deadline_s, request_id=rid)
        return pilot_models.call(manifest, slot, prompt["text"], user, deadline_s=deadline_s, schema_obj=schema,
                                 clock=clock, transports={entry["provider"]: invoke})
    return call


def bootstrap_probe_book(ledger_conn, config_path, *, now):
    """Probe book from the pilot policy file, on the probe epoch and book (never the discovery pilot's)."""
    policy, b = T.pilot_config(config_path)
    T.bootstrap_pilot(ledger_conn, epoch_id=PROBE_EPOCH, book_id=PROBE_BOOK, clip_usd=policy.clip_usd,
                      rule_version=b["rule_version"], capital_usd=b["capital_usd"], actor="probe", now=now)
    return policy
