"""Offline desk funnel. Supplied facts and model results only; no provider or model calls."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone

from sniper.persistence import db_memecoin as ledger
from sniper.persistence import db_memecoin_desk as store
from sniper.services import memecoin_desk_contract as contract
from sniper.services import memecoin_manual_intents as intents


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _response_hash(value):
    try:
        body = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=True)
    except (TypeError, ValueError, OverflowError, RecursionError):
        body = repr(value)
    return hashlib.sha256((type(value).__name__ + ":" + body).encode()).hexdigest()


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _time(value):
    try:
        at = datetime.fromisoformat(value)
        if at.utcoffset() is None or at.utcoffset().total_seconds() != 0:
            raise ValueError
        return at
    except (TypeError, ValueError, AttributeError):
        raise ValueError("chronology:aware_utc_required") from None


def _ticket(conn, ticket_id):
    head = store.get_ticket(conn, ticket_id)
    if head is None:
        raise ValueError("ticket_unknown")
    return head


def _cohort(conn, head):
    cohort = store.get_cohort(conn, head["cohort_id"])
    if cohort is None or cohort["producer"] != head["producer"]:
        raise ValueError("cohort_unknown")
    return cohort


def _stage(head, name):
    return next((row for row in head["stages"] if row["stage"] == name), None)


def _timing_caps(policy):
    if "enter_ttl_s" in policy:
        raise ValueError("cohort_policy:fixed_enter_ttl_s")
    return contract.validate_timing_caps({key: policy.get(key) for key in
        ("enter_ttl_max_s", "boundary_max_s", "wait_max_s")})


def _frozen_entry_timing(conn, head):
    """Legacy or incomplete verdicts cannot acquire a new execution deadline."""
    vet = _stage(head, "vet")
    try:
        caps = _timing_caps(_cohort(conn, head)["policy"])
        result, inputs = vet["output"]["result"], vet["inputs"]
        ttl, next_check = result["enter_ttl_s"], result["next_check_s"]
        if (inputs.get("timing_caps") != caps or result["action"] != "ENTER"
                or type(ttl) is not int or not 3 <= ttl <= caps["enter_ttl_max_s"]
                or type(next_check) is not int or not 5 <= next_check <= caps["boundary_max_s"]):
            raise ValueError
        response_at = inputs["response_at"]
        expiry = (_time(response_at) + timedelta(seconds=ttl)).isoformat()
        if inputs["expires_at"] != expiry or vet["response_at"] != response_at:
            raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError("timing_contract_missing") from None
    return response_at, expiry, ttl


def provision_cohort(desk_conn, ledger_conn, *, producer, policy, prompt_version, created_at,
                     cohort_id=None):
    """Explicitly provision both stores. A retry heals a crash between the two commits."""
    desk_path = desk_conn.execute("PRAGMA database_list").fetchone()[2]
    ledger_path = ledger_conn.execute("PRAGMA database_list").fetchone()[2]
    if (desk_conn is ledger_conn or (desk_path and ledger_path
            and (os.path.realpath(desk_path) == os.path.realpath(ledger_path)
                 or os.path.samefile(desk_path, ledger_path)))):
        raise ValueError("separate_databases_required")
    if (not isinstance(policy, dict) or policy.get("clip_usd") != 25 or policy.get("max_open") != 5
            or policy.get("daily_halt_usd") != 200 or policy.get("cumulative_kill_usd") != -200
            or not isinstance(policy.get("hard_filter"), dict) or not isinstance(policy.get("rail_bounds"), dict)):
        raise ValueError("cohort_policy")
    if policy.get("model_billing_basis", "unknown") not in ("unknown", "subscription_flat", "billed_receipt"):
        raise ValueError("cohort_model_billing_basis")
    contract.validate_ps_inputs(policy, prompt_version)
    _timing_caps(policy)
    contract.claim_band(policy["hard_filter"])         # a malformed frozen band or claim margin refuses provisioning
    if "intake" in policy["hard_filter"]:
        contract.intake_feeds(policy["hard_filter"])  # reject malformed enrollment before either store writes
    if "standing_orders" in policy:
        contract.validate_standing(policy["standing_orders"])
    _v23_policy(policy)
    _time(created_at)
    identity = ledger.register_desk_cohort(ledger_conn, producer=producer, policy=policy,
        prompt_version=prompt_version, created_at=created_at, cohort_id=cohort_id)
    store.register_cohort(desk_conn, cohort_id=identity, producer=producer,
        policy=policy, prompt_version=prompt_version, created_at=created_at)
    return identity


def _watch(policy):
    """The cohort's frozen watch-list rule (watch-list-v1), or None when absent. The kill knob is applied at the verdict
    (MEMECOIN_DESK_WATCH_LIST=0: an H answer is FINAL, like arm C) so the grammar the JUDGE was given never changes."""
    rule = policy.get("watch_list")
    return None if rule is None else contract.validate_watch_list(rule)


def watch_arm(ticket_id):
    """Gamma prereg 6c9b1f37f: bit 2 of sha256(ROOT ticket id): 1 = arm W (re-looked), 0 = arm C (WATCH is final)."""
    return (int(hashlib.sha256(ticket_root(ticket_id).encode()).hexdigest(), 16) >> 2) & 1


RELOOK_TRIGGERS = ("new_high", "volume_burst", "pullback_reclaim", "fresh_buyers")


def watch_end_row(desk_conn, ticket_id, now_iso=None):
    """Gamma prereg section 4: the ONE end row of a watch-list coin, on its last look, once that look is terminal and is
    not continuing (watch_relook). None while the watch is live."""
    head = _ticket(desk_conn, ticket_id)
    if head["state"] in ("open", "waiting", "queued", "unresolved"):
        return None
    stages = {st["stage"]: st for st in head["stages"]}
    vet, expire, refilter, size, fill = (stages.get(k) for k in ("vet", "expire", "refilter", "size", "fill"))
    if expire is not None and expire["reason"] == "watch_relook":
        child = store.get_ticket(desk_conn, ticket_root(ticket_id) + ":look%d" % (_watch_look(head)[0] + 1))
        if child is not None or now_iso is None or _watch_alive(desk_conn, head, now_iso):
            return None                                 # continued (or still may continue) in the next look
        # Zeta P2: the re-look never resumed and the life is over: this look is the watch's last one
    end_relook_dead = expire is not None and expire["reason"] == "watch_relook"
    watch = ((vet or {}).get("inputs") or {}).get("watch") or {}
    if fill is not None:
        end = "entered_filled" if fill["verdict"] == "ENTER" else "entered_refused:%s" % fill["output"].get("status")
    elif refilter is not None and refilter["verdict"] != "PASS":
        end = "look_filter_kill:" + refilter["reason"]
    elif size is not None and size["verdict"] == "SKIP":
        end = size["reason"]
    elif end_relook_dead:
        end = "watch_life_unresumed"
    elif expire is not None:
        end = expire["reason"]
    elif vet is not None:
        end = {"model_skip": "drop"}.get(vet["reason"], vet["reason"])
    else:
        end = "unjudged"
    root = ticket_root(ticket_id)
    looks = desk_conn.execute(
        "SELECT v.ticket_id, v.cost_usd, v.reason FROM desk_tickets v WHERE v.stage='vet' AND (v.ticket_id=? OR v.ticket_id LIKE ?) "
        "ORDER BY v.ts", (root, root + ":look%")).fetchall()
    costs = [r[1] for r in looks]
    return {"root": root, "last_look": ticket_id, "look_index": _watch_look(head)[0], "end_reason": end,
            "looks_judged": len(looks), "look_cost_usd_total": None if any(c is None for c in costs) else sum(costs),
            "watch_arm": watch.get("watch_arm", watch_arm(ticket_id)), "limit_arm": watch.get("limit_arm"),
            "standing_arm": watch.get("standing_arm"), "cohort_key": head["cohort_id"],
            "seconds_since_band_entry": watch.get("seconds_since_band_entry"), "position_id": (fill or {}).get("position_id")}


def record_watch_ends(desk_conn, *, now_iso, limit=100):
    """Write the end row (stage 'watch_end', a state-neutral HOLD record) for every finished watch-list coin. Idempotent."""
    rows = desk_conn.execute(
        # a look child that never got a vet (seat-starved, preflight, band, no-call) still ends its watch (Zeta 2702b500d P1)
        "SELECT h.ticket_id FROM desk_ticket_heads h WHERE h.state NOT IN ('open','waiting','queued','unresolved') "
        "AND (EXISTS (SELECT 1 FROM desk_tickets v WHERE v.ticket_id=h.ticket_id AND v.stage='vet' "
        "AND json_extract(v.inputs_json,'$.watch') IS NOT NULL) OR EXISTS (SELECT 1 FROM desk_candidate_emissions e "
        "WHERE e.opportunity_id=h.ticket_id AND e.is_primary=1 AND json_extract(e.candidate_json,'$.watch_look') IS NOT NULL)) "
        # a parent continued by an existing child never takes a LIMIT slot (Zeta f0ef698b0 P1 starvation)
        "AND NOT (EXISTS (SELECT 1 FROM desk_tickets x WHERE x.ticket_id=h.ticket_id AND x.stage='expire' AND x.reason='watch_relook') "
        "AND EXISTS (SELECT 1 FROM desk_candidate_emissions c WHERE json_extract(c.candidate_json,'$.watch_look.prev_ticket_id')=h.ticket_id)) "
        "AND NOT EXISTS (SELECT 1 FROM desk_tickets e WHERE e.ticket_id=h.ticket_id AND e.stage='watch_end') LIMIT ?",
        (limit,)).fetchall()
    out = []
    for (ticket_id,) in rows:
        row = watch_end_row(desk_conn, ticket_id, now_iso)
        if row is None:
            continue
        store.record_stage(desk_conn, ticket_id=ticket_id, stage="watch_end", seat="code", verdict="HOLD",
            reason=row["end_reason"].split(":")[0],
            inputs={"opportunity_id": _ticket(desk_conn, ticket_id)["opportunity_id"], "watch_end": row["end_reason"]},
            output=row, ts=now_iso)
        out.append({"ticket_id": ticket_id, "state": "watch_end", "reason": row["end_reason"]})
    return out


def record_relook(desk_conn, ticket_id, req, *, now_iso):
    """A producer change trigger for a waiting arm-W WATCH: re-checked here, then the look expires watch_relook with its
    trigger and change score; relook_watches opens the next look. ValueError(reason) on anything else."""
    head = _ticket(desk_conn, ticket_id)
    vet = _stage(head, "vet")
    if head["state"] != "waiting" or vet is None or vet["reason"] != "watch" or _watch(_cohort(desk_conn, head)["policy"]) is None:
        raise ValueError("not_a_waiting_watch")
    if watch_arm(ticket_id) != 1:
        raise ValueError("watch_arm_c")
    if not isinstance(req, dict) or req.get("trigger") not in RELOOK_TRIGGERS:
        raise ValueError("relook_shape")
    if not _watch_alive(desk_conn, head, now_iso):
        raise ValueError("watch_life_passed")      # Zeta 2702b500d P1: a late request never opens a look; relook_watches
                                                    # expires the look watch_life on its own clock (no reset)
    look, _ = _watch_look(head)
    return store.record_stage(desk_conn, ticket_id=ticket_id, stage="expire", seat="code", verdict="SKIP",
        reason="watch_relook", inputs={"opportunity_id": head["opportunity_id"], "look": look, "trigger": req["trigger"],
                                       "change_score": req.get("change_score"),
                                       "prev_look_high": ((vet.get("inputs") or {}).get("watch") or {}).get("look_high")},
        output={"reason": "watch_relook"}, ts=now_iso, ticket_state="expired")


def _watch_alive(desk_conn, head, now_iso):
    """True while now is within the frozen life_s of the watch's ROOT band entry (the one clock every look shares)."""
    rule = _watch(_cohort(desk_conn, head)["policy"])
    root = _ticket(desk_conn, ticket_root(head["ticket_id"]))
    return rule is not None and (_time(now_iso) - _time(root["emitted_at"])).total_seconds() <= rule["life_s"]


def relook_watches(desk_conn, *, now_iso, limit=50, resume_s=3600):
    """watch-list-v1 cadence (step 1; change triggers come next): a waiting WATCH whose cadence_s[look-1] has passed since
    its verdict expires (reason watch_relook) and opens <root>:look<n+1> (same candidate, the root's hard-filter evidence)
    for a fresh JUDGE sheet; a WATCH past life_s from band entry expires watch_life. Crash-safe like the noise re-judge."""
    import logging
    out, now = [], _time(now_iso)
    for head in store.list_waiting_tickets(desk_conn, limit=limit):
        vet = _stage(head, "vet")
        if vet is None or vet["reason"] != "watch" or _stage(head, "trigger") is not None:
            continue
        rule = _watch(_cohort(desk_conn, head)["policy"])
        if rule is None:
            continue
        look, _ = _watch_look(head)
        root = _ticket(desk_conn, ticket_root(head["ticket_id"]))
        if (now - _time(root["emitted_at"])).total_seconds() > rule["life_s"]:
            store.record_stage(desk_conn, ticket_id=head["ticket_id"], stage="expire", seat="code", verdict="SKIP",
                reason="watch_life", inputs={"opportunity_id": head["opportunity_id"], "look": look},
                output={"reason": "watch_life"}, ts=now_iso, ticket_state="expired")
            continue
        cadence = rule["cadence_s"][min(look - 1, len(rule["cadence_s"]) - 1)]
        if now >= _time(vet["inputs"]["response_at"]) + timedelta(seconds=cadence):
            store.record_stage(desk_conn, ticket_id=head["ticket_id"], stage="expire", seat="code", verdict="SKIP",
                reason="watch_relook", inputs={"opportunity_id": head["opportunity_id"], "look": look,
                                               "trigger": "cadence_%d" % cadence, "change_score": None,
                                               "prev_look_high": ((vet.get("inputs") or {}).get("watch") or {}).get("look_high")},
                output={"reason": "watch_relook"}, ts=now_iso, ticket_state="expired")
    for head in store.list_awaiting_looks(desk_conn, limit=limit):   # a look that never got a seat still dies at life_s
        rule = _watch(_cohort(desk_conn, head)["policy"])
        root = _ticket(desk_conn, ticket_root(head["ticket_id"]))
        if rule is not None and (now - _time(root["emitted_at"])).total_seconds() > rule["life_s"]:
            store.record_stage(desk_conn, ticket_id=head["ticket_id"], stage="expire", seat="code", verdict="SKIP",
                reason="watch_life", inputs={"opportunity_id": head["opportunity_id"], "look": _watch_look(head)[0],
                                             "unseated": True},
                output={"reason": "watch_life"}, ts=now_iso, ticket_state="expired")
    since = (now - timedelta(seconds=resume_s)).isoformat()
    for parent, inputs_json in desk_conn.execute(
            "SELECT t.ticket_id, t.inputs_json FROM desk_tickets t WHERE t.stage='expire' AND t.reason='watch_relook' "
            "AND t.ts>=? ORDER BY t.ts LIMIT ?", (since, limit)).fetchall():
        try:
            info = json.loads(inputs_json)
            new_id = ticket_root(parent) + ":look%d" % (info["look"] + 1)
            child = store.get_ticket(desk_conn, new_id)
            head = _ticket(desk_conn, parent)
            if child is None and not _watch_alive(desk_conn, head, now_iso):
                continue                            # past life: no new look (Zeta 2702b500d P1); the parent's end row stands
            evidence = ((_stage(_ticket(desk_conn, ticket_root(parent)), "hard_filter") or {}).get("inputs") or {}).get("evidence")
            if child is None:
                cand = {k: v for k, v in head["candidate"].items() if k not in ("opportunity_id", "snapshot_hash")}
                cand.update(ticket_id=new_id, watch_look={"prev_ticket_id": parent, "look": info["look"] + 1,
                                                          "trigger": info["trigger"]})
                got = store.record_candidate(desk_conn, contract.validate_candidate(cand), cohort_id=head["cohort_id"],
                                             now_iso=now_iso, cooldown_s=0)
                if not got["is_primary"]:
                    continue
                child = store.get_ticket(desk_conn, new_id)
            if _stage(child, "hard_filter") is None and evidence is not None:
                evaluate_candidate(desk_conn, new_id, evidence=evidence,
                                   policy=_cohort(desk_conn, head)["policy"]["hard_filter"], now_iso=now_iso)
                out.append(new_id)
        except Exception:                           # named and logged; the watch life still bounds it
            logging.getLogger(__name__).exception("desk watch re-look failed ticket=%s", parent)
    record_watch_ends(desk_conn, now_iso=now_iso)
    return out


def _look_high(structure, key="high"):
    """This look's reference high in USD (the 300 s window high of the frozen structure; key="price_usd": its price
    at the look, same unit), None when unknown. USD on both, so the change trigger uses only their ratio."""
    st = structure or {}
    hi = st.get("price_usd") if key == "price_usd" else (((st.get("windows") or {}).get("300s")) or {}).get("high")
    return float(hi) if type(hi) in (int, float) and math.isfinite(hi) and hi > 0 else None


def _watch_look(head):
    claim = (head.get("candidate") or {}).get("watch_look")
    return (1, "band_entry") if claim is None else (int(claim["look"]), claim["trigger"])


def _chase(policy):
    """The cohort's frozen chase-limit rule while its kill knob (MEMECOIN_DESK_CHASE_LIMIT) is on, else None. It carries
    the keys record_fire / expire_watches read from a standing rule, so a chase order is executed without the JUDGE's
    watch grammar (the replica prompt stays byte-identical)."""
    rule = policy.get("chase_limit")
    from sniper.settings import memecoin as mc
    if rule is None or not mc.MEMECOIN_DESK_CHASE_LIMIT:
        return None
    rule = contract.validate_chase_limit(rule)
    return {**rule, "version": contract.STANDING_V2}


def _v23_policy(policy):
    """v23 frozen keys: "forecast" = forecast-v1 and "facts" = v23-facts-v1, each needing the watch grammar. Malformed raises."""
    from sniper.services import memecoin_desk_v23_facts as F
    if "forecast" in policy and (policy["forecast"] != contract.FORECAST_VERSION or "standing_orders" not in policy):
        raise ValueError("cohort_policy:forecast")
    if "facts" in policy and (policy["facts"] != F.VERSION or "structure" not in policy):
        raise ValueError("cohort_policy:facts")
    if "exit_observer" in policy:          # CORTEX-root's record-only exit observer (Gamma's death-vs-pullback prereg)
        obs = policy["exit_observer"]
        # exactly what the observer implements (eCat): any other variant would be provisioned and then silently never logged
        if (obs != {"version": "exit-observer-v1", "grid": [2, 3, 4, 6], "baseline_drop_pct": 10.0}
                or any(type(k) is not int for k in obs["grid"])):      # 2.0 == 2 in Python; the observer reads ints only
            raise ValueError("cohort_policy:exit_observer")
    if {"judge_model", "judge_provider", "judge_effort"} & policy.keys():
        from sniper.services.memecoin_desk_judge import JUDGE_MODELS, judge_model, MODEL
        if policy.get("judge_model", MODEL) not in JUDGE_MODELS:
            raise ValueError("cohort_policy:judge_model")
        judge_model({"policy": policy}, environ={})
    if "entry_gate" in policy and (policy["entry_gate"] != ENTRY_GATE_WAIT or "standing_orders" not in policy):
        raise ValueError("cohort_policy:entry_gate")
    if "chase_limit" in policy:
        contract.validate_chase_limit(policy["chase_limit"])        # malformed refuses provisioning
    if "watch_list" in policy:
        contract.validate_watch_list(policy["watch_list"])
        if "standing_orders" in policy:
            raise ValueError("cohort_policy:watch_list")             # the watch list rides on the non-standing grammar
    if "drift_requeue" in policy and policy["drift_requeue"] != store.DRIFT_REQUEUE_VERSION:
        raise ValueError("cohort_policy:drift_requeue")
    if "judge_max_output_tokens" in policy and (type(policy["judge_max_output_tokens"]) is not int
                                                or not 64 <= policy["judge_max_output_tokens"] <= 32000):
        raise ValueError("cohort_policy:judge_max_output_tokens")


def _reentry_rule(head, standing, structure):
    """{"min_px_up_pct"} for a v23 re-entry WATCH ticket (trigger "watch") under the watch grammar, else None. The floor is
    the post-exit high over the current price, from the structure block the model saw; unknown stays None."""
    claim = (head.get("candidate") or {}).get("reentry")
    if standing is None or not isinstance(claim, dict) or claim.get("trigger") != "watch":
        return None
    block = (structure or {}).get("reentry") if isinstance(structure, dict) else None
    floor = block.get("high_vs_now_pct") if isinstance(block, dict) else None
    return {"min_px_up_pct": floor if type(floor) in (int, float) and math.isfinite(floor) else None}


def _forecast_inputs(result, *, head, cohort, model, asof, basis_price, structure):
    """The vet stage's immutable forecast-v1 record (Gamma's prereg): the model's f plus the code-computed reference.
    ref_price_sol = the as-of curve spot price (post-swap), never an executable price; None when unknown (graded censored)."""
    meta = (asof or {}).get("meta") or {}
    sol, sol_age = meta.get("sol_usd"), meta.get("sol_age_s")
    cand = head["candidate"]
    return {"v": contract.FORECAST_VERSION, "f": result.get("forecast"), "ref_price_sol": basis_price,
            "ref_price_usd": basis_price * sol if basis_price is not None and type(sol) in (int, float) else None,
            "sol_usd": sol, "sol_age_s": sol_age, "range5_pct": _range5_pct(structure),
            "range5_bars": (((structure or {}).get("volatility") or {}).get("bars") if isinstance(structure, dict) else None),
            "t_f": meta.get("as_of"), "model": model, "prompt_version": cohort["prompt_version"],
            "cohort_id": head["cohort_id"], "ticket_id": head["ticket_id"],
            "feed": "%s:%s" % (cand.get("producer"), cand.get("venue") or "curve"),
            "trigger": ((cand.get("reentry") or {}).get("trigger") or "first")}


ENTRY_GATE_VERSION = "entry-gate-v0"       # always pass; Gamma's ENTER-now vs WAIT EV table replaces the body
# entry-gate-v1 (cohort key policy.entry_gate; Alpha GO 2026-10-26 on Gamma's holdout read: the frozen px_hold WAIT loses
# less than entering now, -15% vs -21%, everywhere): every E-now becomes a px_hold WAIT at the cohort's frozen shape
# (px_hold_max_noise x range5 deep, px_hold_hold_s held, px_hold_max_wait_s). Gamma measured depth from the running high
# after the call; this sets it from the sheet price (the basis every px_hold uses). Timing, not edge: fewer fills.
ENTRY_GATE_WAIT = "entry-gate-v1"
PX_HOLD_NOISE_WATCH = "px_hold_noise_watch"


def entry_gate(state):
    """ONE slot at ENTER time (operator GO 2026-10-23 after pos 74173, an E-now at the top of its range): 'pass' or 'watch'
    (convert to a WATCH: no entry now, the ticket stays open and is re-judged). No hardcoded top rule: it passes until
    Gamma's table lands. `state` = {"structure", "asof_meta", "basis_price_sol", "result", "candidate"}."""
    return "pass"


def _entry_gate_record(*, head, result, asof, structure, basis_price, version=ENTRY_GATE_VERSION):
    """The gate's decision plus provenance, or None with the kill knob off (MEMECOIN_DESK_ENTRY_GATE=0). A gate error is
    'pass' with the error named: the gate never blocks an entry on its own failure (it only defers on a positive read)."""
    from sniper.settings import memecoin as mc
    if not mc.MEMECOIN_DESK_ENTRY_GATE:
        return None
    state = {"structure": structure, "asof_meta": (asof or {}).get("meta"), "basis_price_sol": basis_price,
             "result": result, "candidate": head["candidate"]}
    if version == ENTRY_GATE_WAIT:
        return {"version": version, "decision": "wait", "error": None}
    try:
        decision, error = entry_gate(state), None
        if decision not in ("pass", "watch"):
            decision, error = "pass", "invalid_decision"
    except Exception as exc:                    # named, logged, never a silent pass
        import logging
        logging.getLogger(__name__).exception("desk entry gate failed ticket=%s", head.get("ticket_id"))
        decision, error = "pass", type(exc).__name__
    return {"version": ENTRY_GATE_VERSION, "decision": decision, "error": error}


def _standing(policy):
    """The cohort's frozen standing-order rule while the kill knob is on, else None (old grammar)."""
    rule = policy.get("standing_orders")
    if rule is None:
        return None
    from sniper.settings import memecoin as mc
    if not mc.MEMECOIN_DESK_STANDING_ORDERS:
        return None
    return contract.validate_standing(rule)


def assign_limit_arm(standing, result, candidate, ticket_id, *, enabled):
    """The ticket's LIMIT (1) / LOG (0) arm, or None (no arm): only an ENTER in a cohort that froze limit_arm, with the kill
    knob on, and never on PumpSwap (the px_limit executor reads the curve tape only: a PumpSwap watch could never fire)."""
    if not ((standing or {}).get("limit_arm") and enabled and result["action"] == "ENTER"):
        return None
    if (candidate or {}).get("venue") == "pumpswap":
        return None
    return limit_arm_bit(ticket_id)


def ticket_root(ticket_id):
    """desk:<producer>:<id> without any child suffix (':noise', ':drift', ':limit', chained)."""
    return ":".join(str(ticket_id).split(":")[:3])


ALERT_YOUNG_MAX = 2       # standing-v2 young-coin alert: at most 2 re-judges per root order (Alpha 10-26)


def standing_arms(ticket_id):
    """Gamma prereg 7e0e59d41: standing-order arm = bit 1 of sha256(ticket_id) (1 = B, a standing order); the LIMIT arm
    keeps the low bit. The two cross independently."""
    h = int(hashlib.sha256(str(ticket_id).encode()).hexdigest(), 16)
    return {"standing_arm": (h >> 1) & 1, "limit_arm": h & 1}


def _standing_order(desk_conn, trig, *, standing, asof, head, response_at, cohort=None):
    """Arm B: the JUDGE's px_hold level becomes ONE standing limit order (operator 2026-10-26): fires on a print at or below
    the level, entry capped at the frozen USD level + the frozen fee/impact allowance, life order_max_s. The level is
    frozen in USD (and as mcap with its unit label); SOL is derived per fire with a causal FX. Returns (order, None) or
    (None, refusal): unknown FX or liquidity at the verdict = order_unpriced; book at its bound = book_full (never evicts)."""
    from sniper.services.memecoin_desk_universe import PUMP_SUPPLY_TOKENS
    import sniper.settings.memecoin as cfg
    fx, sheet_usd = _asof_sol_usd(asof), _sheet_ref_usd(asof, head)
    liq = None
    for cand in ((asof or {}).get("candidate") if isinstance(asof, dict) else None, head.get("candidate")):
        value = ((((cand or {}).get("snapshot") or {}).get("fields") or {}).get("liq_usd") or {}).get("value")
        if type(value) in (int, float) and math.isfinite(value) and value > 0:
            liq = float(value)
            break
    if fx is None or liq is None or sheet_usd is None:
        return None, "order_unpriced"
    live = desk_conn.execute(
        "SELECT COUNT(*) FROM desk_ticket_heads h JOIN desk_tickets t ON t.ticket_id=h.ticket_id AND t.stage='vet' "
        "WHERE h.state='waiting' AND json_extract(t.inputs_json,'$.trigger.order')=?", (contract.STANDING_V2,)).fetchone()[0]
    if live >= standing["book_max"]:
        return None, "book_full"
    level_usd = sheet_usd * (1 + trig["level_pct"] / 100.0)   # the JUDGE's % applied to the price its sheet showed
    order = {**trig, "kind": "px_limit", "order": contract.STANDING_V2, "order_mode": "limit", "confirm": 1, "hold_s": None,
             "level_price_usd": level_usd, "level_mcap_usd": level_usd * PUMP_SUPPLY_TOKENS, "level_unit": "usd_mcap",
             "level_sol_usd": fx, "level_fee_pct": float(cfg.MEMECOIN_DESK_VERDICT_FEE_PCT),
             "level_impact_pct": 25.0 / liq * 100.0,
             "expires_at": (_time(response_at) + timedelta(seconds=standing["order_max_s"])).isoformat()}
    try:            # the order dies when the coin leaves the cohort's frozen band (step 2); no band = no band cancel
        order["cancel_band_usd_mcap"] = list(contract.mcap_band(((cohort or {}).get("policy") or {}).get("hard_filter")))
    except (ValueError, TypeError):
        pass
    return order, None


ORDER_CANCEL_REASONS = ("left_band",)


def record_cancel(desk_conn, ticket_id, cancel, *, now_iso):
    """Close a waiting standing-v2 order on a producer cancel (the producer saw the coin's USD mcap leave the frozen band
    with a causal FX). Re-checked here against the frozen order: ValueError(reason) on anything else, never a guess."""
    head = _ticket(desk_conn, ticket_id)
    vet = _stage(head, "vet")
    trig = ((vet or {}).get("inputs") or {}).get("trigger") or {}
    if head["state"] != "waiting" or trig.get("order") != contract.STANDING_V2:
        raise ValueError("not_a_waiting_order")
    if not isinstance(cancel, dict) or cancel.get("reason") not in ORDER_CANCEL_REASONS:
        raise ValueError("cancel_shape")
    mcap, band = cancel.get("mcap_usd"), trig.get("cancel_band_usd_mcap")
    if not (type(mcap) in (int, float) and math.isfinite(mcap) and band and not band[0] <= mcap <= band[1]):
        raise ValueError("cancel_inside_band")
    row = store.record_stage(desk_conn, ticket_id=ticket_id, stage="expire", seat="code", verdict="SKIP",
        reason=cancel["reason"], inputs={"opportunity_id": head["opportunity_id"], "cancel": cancel},
        output={"reason": cancel["reason"]}, ts=now_iso, ticket_state="expired")
    record_order_ends(desk_conn, now_iso=now_iso)
    return row


def limit_arm_bit(ticket_id):
    """The frozen coin flip (Gamma's prereg): the low bit of sha256(ticket_id). 1 = LIMIT arm, 0 = LOG-only control."""
    return int(hashlib.sha256(str(ticket_id).encode()).hexdigest(), 16) & 1


def _range5_pct(structure, basis="swing"):
    """The coin's own 5 s noise over the 300 s bars, only with >= 12 bars (Gamma's range5 minimum); else None: a pullback depth or a
    drift limit is never set on unknown noise. basis "swing" = volatility.median_5s_swing_pct (within-bar; the frozen basis of old
    cohorts), "true_range" = volatility.median_5s_true_range_pct (move from the previous print, so a thin single-swap bar carries it)."""
    vol = (structure or {}).get("volatility") if isinstance(structure, dict) else None
    if not isinstance(vol, dict):
        return None
    key, bars_key = ("median_5s_true_range_pct", "true_range_bars") if basis == "true_range" else ("median_5s_swing_pct", "bars")
    med, bars = vol.get(key), vol.get(bars_key)
    return med if type(med) in (int, float) and med > 0 and type(bars) is int and bars >= 12 else None


def rejudge_noise_watches(desk_conn, *, now_iso, limit=50, resume_s=3600):
    """P2's ONE re-judge (Zeta on 027132ff5: a vetted ticket never reaches JUDGE again). A waiting young-coin px_hold WATCH
    whose next_check_s has passed expires (reason noise_rejudge; one head per mint), then opens <ticket>:noise with the same
    candidate and the parent's hard-filter evidence. Crash-safe: an expired parent without its child is resumed for
    resume_s. Never raises into the caller."""
    import logging
    out, now = [], _time(now_iso)
    for head in store.list_waiting_tickets(desk_conn, limit=limit):
        vet = _stage(head, "vet")
        if vet is None or vet["reason"] != PX_HOLD_NOISE_WATCH or _stage(head, "trigger") is not None:
            continue
        due = _time(vet["inputs"]["response_at"]) + timedelta(seconds=vet["output"]["result"]["next_check_s"] or 0)
        if now >= due:
            store.record_stage(desk_conn, ticket_id=head["ticket_id"], stage="expire", seat="code", verdict="SKIP",
                reason="noise_rejudge", inputs={"opportunity_id": head["opportunity_id"]},
                output={"reason": "noise_rejudge"}, ts=now_iso, ticket_state="expired")
    since = (now - timedelta(seconds=resume_s)).isoformat()
    for (parent,) in desk_conn.execute(
            "SELECT t.ticket_id FROM desk_tickets t WHERE t.stage='expire' AND t.reason='noise_rejudge' AND t.ts>=? "
            "AND NOT EXISTS (SELECT 1 FROM desk_tickets c WHERE c.ticket_id=t.ticket_id || ':noise' AND c.stage='hard_filter') "
            "AND NOT EXISTS (SELECT 1 FROM desk_candidate_emissions e WHERE e.opportunity_id=t.ticket_id || ':noise' "
            "AND e.is_primary=0) ORDER BY t.ts LIMIT ?", (since, limit)).fetchall():
        new_id = parent + ":noise"
        try:
            head = _ticket(desk_conn, parent)
            if store.get_ticket(desk_conn, new_id) is not None:      # inserted before a crash: finish its hard filter
                evidence = ((_stage(head, "hard_filter") or {}).get("inputs") or {}).get("evidence")
                if evidence is not None:
                    evaluate_candidate(desk_conn, new_id, evidence=evidence,
                                       policy=_cohort(desk_conn, head)["policy"]["hard_filter"], now_iso=now_iso)
                    out.append(new_id)
                continue
            prior = _stage(head, "hard_filter")
            evidence = (prior or {}).get("inputs", {}).get("evidence")
            cand = {k: v for k, v in head["candidate"].items() if k not in ("opportunity_id", "snapshot_hash")}
            count = _rejudge_count(head) + 1
            cand.update(ticket_id=new_id, noise_rejudge={"prev_ticket_id": parent, **({"count": count} if count > 1 else {})})
            got = store.record_candidate(desk_conn, contract.validate_candidate(cand), cohort_id=head["cohort_id"],
                                         now_iso=now_iso, cooldown_s=0)
            if got["is_primary"] and evidence is not None:
                evaluate_candidate(desk_conn, new_id, evidence=evidence,
                                   policy=_cohort(desk_conn, head)["policy"]["hard_filter"], now_iso=now_iso)
                out.append(new_id)
        except Exception:                           # named and logged; nothing else depends on the child
            logging.getLogger(__name__).exception("desk noise re-judge failed ticket=%s", parent)
    return out


def _noise_watch_allowed(desk_conn, ticket_id, cohort, standing=None):
    """One noise-WATCH per ticket, v23-facts cohorts only, kill knob MEMECOIN_DESK_PX_HOLD_WATCH; a second unknown-noise
    px_hold is SKIP cond_unavailable as before (a coin that never reaches 12 bars cannot burn JUDGE seats)."""
    import sniper.settings.memecoin as cfg
    if not cfg.MEMECOIN_DESK_PX_HOLD_WATCH or cohort["policy"].get("facts") != "v23-facts-v1":
        return False
    # the re-judge itself is final, and a ticket that already carries another claim cannot take a second one (the
    # contract refuses the combination; Zeta on e49d1b0f9): it SKIPs as before
    cand = _ticket(desk_conn, ticket_id)["candidate"]
    if {"drift_requeue", "reentry", "limit_watch"} & set(cand):
        return False
    if "noise_rejudge" not in cand:
        return True
    # standing-v2 arm B (alert_young): a re-judge may watch again until ALERT_YOUNG_MAX re-judges are spent
    return ((standing or {}).get("version") == contract.STANDING_V2
            and standing_arms(ticket_root(ticket_id))["standing_arm"] == 1 and _rejudge_count({"candidate": cand}) < ALERT_YOUNG_MAX)


def _rejudge_count(head):
    claim = (head.get("candidate") or {}).get("noise_rejudge")
    return 0 if claim is None else int(claim.get("count", 1))


def _curve_pct(asof, head):
    """Curve progress % at the verdict, or None: unknown never refuses a WAIT (the writer's guard still checks every
    fire). The as-of candidate is used EXCLUSIVELY when present (Zeta final: never fall back to an older emission value);
    only a complete, finite value counts as known."""
    cand = (asof or {}).get("candidate") if isinstance(asof, dict) else None
    field = ((((cand if cand is not None else head.get("candidate")) or {}).get("snapshot") or {}).get("fields") or {}).get("curve_pct") or {}
    value = field.get("value")
    if field.get("completeness") == "complete" and type(value) in (int, float) and math.isfinite(value):
        return float(value)
    return None


def _sheet_ref_usd(asof, head):
    """The USD/token price the JUDGE's sheet and S6 used: priced_verdict on the as-of candidate (else the emission), or
    None when it cannot be priced."""
    cand = (asof or {}).get("candidate") if isinstance(asof, dict) else None
    price = priced_verdict(cand if cand is not None else head.get("candidate"), clip_usd=25,
                           basis="asof" if cand is not None else "emission")
    return None if price is None else price["ref_price_usd"]


def _asof_sol_usd(asof):
    """SOL/USD frozen with the as-of read, else None (a USD ceiling is never derived from an unknown FX)."""
    fx = ((asof or {}).get("meta") or {}).get("sol_usd") if isinstance(asof, dict) else None
    return fx if type(fx) in (int, float) and math.isfinite(fx) and fx > 0 else None


def _asof_price_sol(asof):
    """SOL/token at the as-of read (curve reserves), else None: a price trigger is never set on an unknown basis."""
    price = ((((asof or {}).get("meta") or {}).get("rank") or {}).get("price_now")
             if isinstance(asof, dict) else None)
    return price if type(price) in (int, float) and math.isfinite(price) and price > 0 else None


def _close_outcome(position, fills):
    """realized_pnl and the USD/token price a winner re-entry must beat: the ONE confirmed full-exit fill
    whose tx_sig is the position's close_sig (position 74139: fill 149356, 3.25374e-5). A partial or
    de-risk history, a missing/unpriced/unbound fill, or a leftover claim or pending flag -> None (no
    re-arm). Never synthesized from total PnL, never the exact-preview decision price."""
    pnl = position.get("realized_pnl")
    pnl = pnl if type(pnl) in (int, float) and math.isfinite(pnl) else None
    exits = [f for f in fills if f.get("kind") in ledger.EXIT_KINDS]
    entries = [f for f in fills if f.get("kind") in ledger.ENTRY_KINDS]
    price = exits[0].get("price") if len(exits) == 1 else None
    def positive(v):
        return type(v) in (int, float) and math.isfinite(v) and v > 0
    if len(exits) == 1 and not (positive(exits[0].get("qty")) and positive(exits[0].get("notional"))
            and entries and all(positive(f.get("qty")) for f in entries)
            and all(f.get("fill_source") == "chain" for f in entries + exits)
            and abs(sum(f["qty"] for f in entries) - exits[0]["qty"]) <= 1e-6 * exits[0]["qty"]):
        price = None                       # the one exit must retire exactly the entered quantity
    settled = all(position.get(k) is None for k in ("close_pending_at", "close_claim_id",
                                                     "derisk_pending_at", "derisk_claim_id", "derisked_at"))
    if (not settled or not position.get("close_sig") or len(exits) != 1
            or exits[0].get("tx_sig") != position["close_sig"]
            or type(price) not in (int, float) or not math.isfinite(price) or price <= 0):
        price = None
    # soft-loss re-entry (winner_reentry v2): the reason and OUR confirmed single chain entry fill price
    entry = entries[0].get("price") if len(entries) == 1 and entries[0].get("fill_source") == "chain" else None
    reason = position.get("close_exit_reason") or position.get("exit_reason")
    return {"realized_pnl": pnl, "exit_price_usd": price,
            "exit_reason": reason if isinstance(reason, str) and reason else None,
            "entry_fill_price_usd": entry if type(entry) in (int, float) and math.isfinite(entry) and entry > 0
            and price is not None else None}


def ingest_candidate(desk_conn, candidate, *, cohort_id, now_iso, cooldown_s=900, reentry_enabled=None,
                     intake_dir=None, source_now_fn=None):
    candidate = contract.validate_candidate(candidate)
    if _time(now_iso) < _time(candidate["emitted_at"]):
        raise ValueError("chronology:future_emission")
    if reentry_enabled is None:
        from sniper.settings import memecoin as mc
        reentry_enabled = mc.MEMECOIN_DESK_WINNER_REENTRY
    return store.record_candidate(desk_conn, candidate, cohort_id=cohort_id, now_iso=now_iso,
                                  cooldown_s=cooldown_s, reentry_enabled=reentry_enabled,
                                  intake_dir=intake_dir, source_now_fn=source_now_fn)


def evaluate_candidate(desk_conn, ticket_id, *, evidence, policy, now_iso):
    head = _ticket(desk_conn, ticket_id)
    cohort = _cohort(desk_conn, head)
    if _json(policy) != _json(cohort["policy"]["hard_filter"]):
        raise ValueError("cohort_policy")
    if _time(now_iso) < _time(head["emitted_at"]):
        raise ValueError("chronology:future_emission")
    if head["producer"] == "H" and _time(now_iso).timestamp() < head["candidate"]["roster_buy"]["source_time"] + 5:
        raise ValueError("activity_window_pending")
    if policy.get("intake_source_version") == "desk-intake-source-v1" and isinstance(evidence, dict):
        proof = store.get_intake_source_receipt(desk_conn, head["opportunity_id"], cohort_id=head["cohort_id"],
            hard_policy=policy, candidate=head["candidate"], as_of=_time(now_iso).timestamp())
        evidence = {**evidence, "intake_source": proof}  # caller-provided pass JSON never authenticates admission
    result = contract.hard_filter(head["candidate"], evidence=evidence, policy=policy, evaluated_at=now_iso)
    inputs = {"opportunity_id": head["opportunity_id"], "snapshot_hash": head["snapshot_hash"],
              "evidence_hash": _response_hash(evidence), "policy_hash": _hash(policy),
              "evidence": evidence if not result["reason"].startswith("unknown:evidence:") else None}
    gate_at = result.get("gate_inputs", {}).get("gate_evaluated_at") if result.get("gate_inputs") else None
    if gate_at is not None:
        gate_at = datetime.fromtimestamp(gate_at, timezone.utc).isoformat()
        if _time(gate_at) > _time(now_iso):
            raise ValueError("chronology:future_gate")
    prior = _stage(head, "hard_filter")
    filtered = store.record_stage(desk_conn, ticket_id=ticket_id, stage="hard_filter", seat="code",
        verdict=result["action"], reason=result["reason"], inputs=inputs, output=result,
        ts=prior["ts"] if prior else now_iso, gate_state=result["gate_state"], gate_inputs=result["gate_inputs"], gate_evaluated_at=gate_at)
    _exclude_gated_f(desk_conn, ticket_id, filtered)
    return filtered


def _exclude_gated_f(desk_conn, ticket_id, filtered):
    if filtered is None or filtered["verdict"] != "PASS" or filtered["gate_state"] != "skip":
        return False
    store.record_admission(desk_conn, ticket_id=ticket_id, arm="F", decision="not_selected",
                           decided_at=filtered["ts"])
    return True


def record_decision(desk_conn, ledger_conn, ticket_id, raw, *, bounds, elapsed_ms,
                    call_started_at, response_at, now_iso, expires_at=None,
                    model_cost_usd=None, cost_provenance="desk:unpriced", call_error=None, model=None,
                    model_cost_receipt=None, asof=None, soft=None, structure=None, context=None, deadline_s=None,
                    context_error=None, inline_rank=None, drift=None, sheet_sha256=None,
                    ps_facts=None, safety=None, safety_proof=None):
    """Freeze one supplied response and its incurred charge before creating an ENTER request. With `asof`
    ({"candidate", "meta"} from memecoin_desk_asof.refresh) the recorded sheet is the as-of-decision snapshot the
    JUDGE actually saw, kept whole in the vet inputs (inputs["asof"]["candidate"]) for the entry cap."""
    head = _ticket(desk_conn, ticket_id)
    cohort = _cohort(desk_conn, head)
    ps_version = cohort["policy"].get("ps_inputs")
    ps_mode = ps_version in ("pumpswap-facts-v1", "pumpswap-facts-v2")
    if any(value is not None for value in (ps_facts, safety, safety_proof)):
        if not ps_mode:
            raise ValueError("ps_inputs_cohort")
        if (not isinstance(ps_facts, dict) or safety is None or not isinstance(safety_proof, dict)
                or safety_proof.get("emission_snapshot_hash") != head["snapshot_hash"]):
            raise ValueError("ps_inputs_binding")
        if ps_facts.get("version") != ps_version:
            raise ValueError("ps_inputs_version_mismatch")
    elif ps_mode and call_error is None:
        raise ValueError("ps_inputs_required")
    filtered = _stage(head, "hard_filter")
    if filtered is None or filtered["verdict"] != "PASS":
        raise ValueError("hard_filter_required")
    if _json(bounds) != _json(cohort["policy"]["rail_bounds"]):
        raise ValueError("cohort_policy")
    if not (_time(head["emitted_at"]) <= _time(call_started_at) <= _time(response_at) <= _time(now_iso)):
        raise ValueError("chronology")
    def refuse_cost(reason, marker=None):
        if _stage(head, "vet") is None:
            scalar_hash = _response_hash({"usd": model_cost_usd, "provenance": cost_provenance, "model": model})
            proof_hash = _response_hash(model_cost_receipt)
            provenance = (marker["provenance"] if marker else
                "model-invalid:" + (proof_hash if model_cost_receipt is not None else scalar_hash))
            occurred_at = marker["occurred_at"] if marker else response_at
            if marker is None:
                ledger.record_desk_cohort_cost(ledger_conn, cohort_id=head["cohort_id"],
                    event_id="desk-judge:" + ticket_id, usd=None, kind="model", occurred_at=occurred_at,
                    provenance=provenance)
            _exclude_gated_f(desk_conn, ticket_id, filtered)
            result = {"action": "SKIP", "reason": "judge_malformed_result", "rails": None,
                      "condition": None, "reason_code": None, "enter_ttl_s": None, "next_check_s": None}
            inputs = {"opportunity_id": head["opportunity_id"], "snapshot_hash": head["snapshot_hash"],
                      "billing_error": reason, "cost_provenance": provenance}
            if marker is None:
                inputs.update(response_hash=_response_hash(raw), proof_hash=proof_hash, scalar_hash=scalar_hash)
            store.record_stage(desk_conn, ticket_id=ticket_id, stage="vet", seat="judge", verdict="SKIP",
                reason="judge_malformed_result", inputs=inputs,
                output={"result": result}, ts=occurred_at, response_at=occurred_at,
                prompt_version=cohort["prompt_version"])
        raise ValueError(reason)

    if _stage(head, "vet") is None:
        marker = ledger.get_desk_cohort_cost(ledger_conn, head["cohort_id"], "desk-judge:" + ticket_id)
        if marker is not None and marker["kind"] == "model" and marker["provenance"].startswith("model-invalid:"):
            refuse_cost("model_cost_previously_refused", marker)
    if model_cost_usd is not None and not _finite(model_cost_usd):
        refuse_cost("model_cost")
    if not isinstance(cost_provenance, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", cost_provenance):
        refuse_cost("cost_provenance")
    if model is not None and (not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", model)):
        refuse_cost("model_identifier")
    model_cost_evidence = None
    if model_cost_receipt is not None:
        from sniper.services.memecoin_desk_model_costs import normalize_model_cost_receipt

        try:
            priced = normalize_model_cost_receipt(model_cost_receipt, ticket_id=ticket_id, model=model,
                response_at=response_at, billing_basis=cohort["policy"].get("model_billing_basis", "unknown"))
        except ValueError:
            refuse_cost("model_cost_receipt_invalid")
        if ((model_cost_usd is not None and model_cost_usd != priced["cost_usd"])
                or (cost_provenance != "desk:unpriced" and cost_provenance != priced["provenance"])):
            refuse_cost("model_cost_conflict")
        model_cost_usd, cost_provenance = priced["cost_usd"], priced["provenance"]
        model_cost_evidence = priced["evidence"]
        if model_cost_receipt["is_error"] and call_error is None:
            call_error = "model_result_error"
    raw_error, raw_audit = None, None
    if call_error is None:
        try:
            encoded = _json(raw)
            if len(encoded.encode()) > 8192:
                raw_error = "response_too_large"
            else:
                raw_audit = raw
        except (ValueError, TypeError, OverflowError, RecursionError):
            raw_error = "non_json:" + type(raw).__name__
    valid_elapsed = _finite(elapsed_ms)
    wall_ms = (_time(response_at) - _time(call_started_at)).total_seconds() * 1000
    latency_ms = max(elapsed_ms, wall_ms) if valid_elapsed else None
    try:
        timing_caps = _timing_caps(cohort["policy"])
    except ValueError:
        timing_caps = None
    standing = _standing(cohort["policy"])
    basis_price = _asof_price_sol(asof)
    forecast = standing is not None and cohort["policy"].get("forecast") == contract.FORECAST_VERSION
    result = contract.validate_verdict(raw, opportunity_id=head["opportunity_id"], bounds=bounds,
        elapsed_ms=latency_ms if valid_elapsed else elapsed_ms, call_error=call_error,
        bars_available=standing is not None and basis_price is not None,
        timing_caps=timing_caps, deadline_s=deadline_s, standing=standing,
        forecast=forecast, reentry=_reentry_rule(head, standing, structure),
        watch=standing is None and "watch_list" in cohort["policy"])
    watch_rule = _watch(cohort["policy"]) if standing is None else None
    watch_log = None
    if watch_rule is not None:
        import sniper.settings.memecoin as cfg
        look, trigger = _watch_look(head)
        bits = standing_arms(ticket_root(ticket_id))
        arm = watch_arm(ticket_id)
        snap = (((asof or {}).get("candidate") or head["candidate"]) or {}).get("snapshot") or {}
        root = _ticket(desk_conn, ticket_root(ticket_id)) if ticket_root(ticket_id) != ticket_id else head
        prev_id = ((head.get("candidate") or {}).get("watch_look") or {}).get("prev_ticket_id")
        prev_expire = _stage(_ticket(desk_conn, prev_id), "expire") if prev_id else None
        parent_relook = (prev_expire or {}).get("inputs") or {} if (prev_expire or {}).get("reason") == "watch_relook" else {}
        watch_log = {"version": watch_rule["version"], "look_index": look, "trigger_reason": trigger, "watch_arm": arm,
                     "limit_arm": bits["limit_arm"], "standing_arm": bits["standing_arm"],
                     "sheet_sha": sheet_sha256,         # the rendered sheet the model saw; None = not recorded (unknown)
                     "change_score": parent_relook.get("change_score"), "prev_look_high": parent_relook.get("prev_look_high"),
                     "look_high": _look_high(structure), "look_price_usd": _look_high(structure, key="price_usd"),
                     "mcap_usd_at_look": ((snap.get("fields") or {}).get("mcap_usd") or {}).get("value"),
                     "seconds_since_band_entry": (_time(response_at) - _time(root["emitted_at"])).total_seconds()}
        if result["action"] == "WATCH" and (arm == 0 or not cfg.MEMECOIN_DESK_WATCH_LIST or look >= watch_rule["max_looks"]):
            # arm C, kill knob off, or the last look: a WATCH is FINAL (today's SKIP), named for the verdict mix
            why = ("watch_final_arm_c" if arm == 0 else "watch_off" if not cfg.MEMECOIN_DESK_WATCH_LIST else "watch_max_looks")
            result = {"action": "SKIP", "reason": why, "rails": None, "condition": None,
                      "reason_code": result["reason_code"], "enter_ttl_s": result["enter_ttl_s"],
                      "next_check_s": result["next_check_s"]}
        elif look > 1 and result["action"] != "SKIP" and not _watch_alive(desk_conn, head, response_at):
            # Zeta f0ef698b0 P1: a look answered after the root's frozen life never enters or watches on
            result = {"action": "SKIP", "reason": "watch_life", "rails": None, "condition": None,
                      "reason_code": result["reason_code"], "enter_ttl_s": result["enter_ttl_s"],
                      "next_check_s": result["next_check_s"]}
    if raw_error == "response_too_large":
        result = {"action": "SKIP", "reason": raw_error, "rails": None, "condition": None, "reason_code": None,
                  "enter_ttl_s": None, "next_check_s": None}
    gate = None
    if result["action"] == "ENTER" and standing is not None:
        gate = _entry_gate_record(head=head, result=result, asof=asof, structure=structure, basis_price=basis_price,
                                  version=cohort["policy"].get("entry_gate", ENTRY_GATE_VERSION))
        if gate is not None and gate["decision"] == "wait":     # no instant buy: the frozen px_hold shape below sets the level
            result = {**result, "action": "WAIT", "reason": "entry_gate_wait",
                      "condition": ["px_hold", "<=", -50.0, 1, standing["px_hold_max_wait_s"]]}
        if gate is not None and gate["decision"] == "watch":    # the ticket stays open and is re-judged; no entry now
            result = {"action": "WATCH", "reason": "entry_gate_watch", "rails": None, "condition": None,
                      "reason_code": result["reason_code"], "enter_ttl_s": result["enter_ttl_s"],
                      "next_check_s": result["next_check_s"], "plan": None,
                      **({"forecast": result["forecast"]} if "forecast" in result else {})}
    computed_expiry = None
    if result["action"] == "ENTER":
        try:
            computed_expiry = (_time(response_at) + timedelta(seconds=result["enter_ttl_s"])).isoformat()
            if expires_at is not None and _time(expires_at) != _time(computed_expiry):
                raise ValueError
        except (ValueError, OverflowError):
            result = {**result, "action": "SKIP", "reason": "expiry_conflict", "rails": None, "condition": None}
    sheet_candidate, asof_inputs = head["candidate"], None
    if asof is not None:
        try:
            seen = contract.validate_candidate(asof["candidate"])
            same = ({k: v for k, v in seen.items() if k not in ("emitted_at", "snapshot")}
                    == {k: v for k, v in head["candidate"].items() if k not in ("emitted_at", "snapshot")})
            if (not same or _time(seen["emitted_at"]) < _time(head["emitted_at"])
                    or _time(seen["emitted_at"]) > _time(call_started_at)):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise ValueError("asof_candidate") from None
        sheet_candidate = seen
        asof_inputs = {"candidate": seen, "meta": asof.get("meta"), "emission_snapshot_hash": head["snapshot_hash"]}
    audit = {"raw": raw_audit, "raw_error": raw_error, "result": result}
    # The response is audit data only; the model sees the typed sheet.
    if watch_log is not None:          # Gamma prereg section 4: one per-look row on the vet stage
        watch_log = {**watch_log, "verdict": result["action"], "look_cost_usd": model_cost_usd}
    inputs = {"opportunity_id": head["opportunity_id"], "snapshot_hash": head["snapshot_hash"],
        **({"watch": watch_log} if watch_log is not None else {}),
        "sheet": contract.render_judge_sheet(sheet_candidate, soft=soft, structure=structure, context=context, drift=drift,
            ps_facts=ps_facts, safety=safety, safety_proof=safety_proof,
            allow_legacy_soft=cohort["prompt_version"] == "desk-judge-v13",
            soft_shape=contract.soft_shape(cohort["prompt_version"])), "model": model,
        "response_hash": _response_hash(raw), "result": result, "bounds": bounds, "elapsed_ms": latency_ms,
        "elapsed_input_hash": _response_hash(elapsed_ms),
        "call_started_at": call_started_at, "response_at": response_at, "expires_at": computed_expiry,
        "timing_caps": timing_caps, "deadline_s": contract.judge_deadline_s() if deadline_s is None else deadline_s,
        "model_cost_usd": model_cost_usd, "cost_provenance": cost_provenance, "call_error_hash": _response_hash(call_error)}
    if ps_facts is not None:
        digest = hashlib.sha256(_json(inputs["sheet"]).encode()).hexdigest()
        if sheet_sha256 is not None and sheet_sha256 != digest:
            raise ValueError("sheet_hash_conflict")
        inputs["sheet_sha256"] = digest
    if model_cost_evidence is not None:
        inputs["model_cost_evidence"] = model_cost_evidence
    if gate is not None:
        inputs["entry_gate"] = gate
    if forecast and call_error is None and raw_error is None:     # a record only: nothing below reads it (Gamma's bar first)
        inputs["forecast"] = _forecast_inputs(result, head=head, cohort=cohort, model=model, asof=asof,
                                              basis_price=basis_price, structure=structure)
    if asof_inputs is not None:
        inputs["asof"] = asof_inputs
    cond = result.get("condition")
    curve = _curve_pct(asof, head)
    if (standing is not None and result["action"] == "WAIT" and cond and cond[0] in contract.PRICE_TRIGGERS and curve is not None
            and curve >= standing["graduation_guard_pct"]):
        # operator 10-26 (19 of 19 fires refused by the guard): an order the writer can never accept is not placed
        result = {**result, "action": "SKIP", "reason": "graduation_guard_at_wait", "rails": None, "condition": None,
                  "plan": None}
        audit["result"] = result
        inputs["result"] = result
        inputs["curve_pct_at_wait"] = curve
        cond = None
    if result["action"] == "WAIT" and cond and cond[0] in contract.PRICE_TRIGGERS:
        # frozen once: the level in SOL/token from the as-of curve reserves the JUDGE's sheet was rendered from
        level_pct, wait_s, hold_s, noise = cond[2], cond[4], None, None
        if cond[0] == "px_hold":         # Gamma's shape: clip to N x range5 deep, time hold, short wait (cohort-frozen)
            noise = _range5_pct(structure, standing.get("noise_basis", "swing"))
            unpriced = basis_price is None or (result.get("reason") == "entry_gate_wait"
                                               and (_sheet_ref_usd(asof, head) is None or _asof_sol_usd(asof) is None))
            if (noise is None or unpriced) and _noise_watch_allowed(desk_conn, ticket_id, cohort, standing=standing):
                # P2 (Alpha GO 2026-10-26): < 12 bars is too young to size a pullback, not a dead coin: re-judge once later
                result = {**result, "action": "WATCH", "reason": PX_HOLD_NOISE_WATCH, "rails": None, "condition": None,
                          "plan": None}
                audit["result"] = result
                inputs["result"] = result
                if standing["version"] == contract.STANDING_V2 and standing_arms(ticket_root(ticket_id))["standing_arm"] == 1:
                    inputs["order_mode"] = "alert_young"            # graded outside the primary (Gamma)
                    inputs["rejudge_count"] = _rejudge_count(_ticket(desk_conn, ticket_id))
            elif noise is None or unpriced:                 # unknown basis / ceiling: never a level (no crash, named skip)
                result = {**result, "action": "SKIP", "reason": "cond_unavailable", "rails": None, "condition": None,
                          "plan": None}
                audit["result"] = result
                inputs["result"] = result
            else:
                level_pct = max(level_pct, -standing["px_hold_max_noise"] * noise)
                wait_s, hold_s = min(wait_s, standing["px_hold_max_wait_s"]), standing["px_hold_hold_s"]
        if result["action"] == "WAIT":
            inputs["trigger"] = {"kind": cond[0], "level_pct": level_pct, "asked_level_pct": cond[2],
                                 "range5_pct": noise, "hold_s": hold_s, "basis_price_sol": basis_price,
                                 "level_price_sol": basis_price * (1 + level_pct / 100.0), "confirm": cond[3],
                                 "expires_at": (_time(response_at) + timedelta(seconds=wait_s)).isoformat()}
            if result.get("reason") == "entry_gate_wait":
                # the JUDGE said ENTER at this sheet price: a deferred buy never pays more than it (Zeta P1 on 027132ff5)
                # frozen USD from the SAME helper that priced the JUDGE's sheet / S6 (Zeta final: not rank SOL x as-of FX)
                inputs["trigger"]["ceiling_price_usd"] = _sheet_ref_usd(asof, head)
    if standing is not None and standing["version"] == contract.STANDING_V2:
        arms = standing_arms(ticket_root(ticket_id))           # a child (:noise/:drift/:limit) keeps its root's arm
        inputs["arms"] = arms                                   # both bits on every ticket (Gamma prereg section 4)
        if arms["standing_arm"] == 1 and result["action"] == "WAIT" and inputs.get("trigger", {}).get("kind") == "px_hold":
            order, refusal = _standing_order(desk_conn, inputs["trigger"], standing=standing, asof=asof, head=head,
                                             response_at=response_at, cohort=cohort)
            if refusal is not None:
                result = {**result, "action": "SKIP", "reason": refusal, "rails": None, "condition": None, "plan": None}
                audit["result"] = result
                inputs["result"] = result
                inputs.pop("trigger")
            else:
                inputs["trigger"] = order
    import sniper.settings.memecoin as cfg
    drift_k = (standing or {}).get("drift_k")
    drift_noise = _range5_pct(structure, "true_range") if drift_k and cfg.MEMECOIN_DESK_DRIFT_ENABLED else None
    if drift_noise is not None and result["action"] == "ENTER":
        drift_noise = max(drift_noise, float(standing.get("drift_noise_floor_pct", 0)))
        inputs["drift"] = {"k": float(drift_k), "noise_pct": drift_noise, "enforce": 1 if standing.get("drift_enforce", True) else 0}   # frozen at the verdict: the cohort K, the figure the JUDGE saw
    arm = assign_limit_arm(standing, result, head["candidate"], ticket_id, enabled=cfg.MEMECOIN_DESK_LIMIT_ARM_ENABLED)
    if arm is not None:
        inputs["limit_arm"] = arm                            # frozen at the verdict: the same ticket always lands in the same arm
    if inline_rank:                    # trigger cohort: the claim-time rank view (band, features, score) of the same snapshot
        inputs["inline_rank"] = inline_rank
    if context_error:                  # a no-call row whose context read failed: reference-outcome code censors it
        inputs["context_error"] = str(context_error)
    prior = _stage(head, "vet")
    if prior is not None and (_json(prior["inputs"]) != _json(inputs) or prior["output"] != audit):
        raise ValueError("stage_conflict")
    _exclude_gated_f(desk_conn, ticket_id, filtered)
    if prior is None:
        ledger.record_desk_cohort_cost(ledger_conn, cohort_id=head["cohort_id"], event_id="desk-judge:" + ticket_id,
            usd=model_cost_usd, kind="model", occurred_at=response_at, provenance=cost_provenance)
        # a WATCH is stored as verdict WAIT (no trigger in inputs): the live desk_tickets CHECK predates WATCH, and the
        # waiting state, the work list and expire_watches already treat a trigger-less WAIT as a watch (result keeps WATCH)
        store.record_stage(desk_conn, ticket_id=ticket_id, stage="vet", seat="judge",
            verdict="WAIT" if result["action"] == "WATCH" else result["action"],
            reason=result["reason"], inputs=inputs, output=audit, ts=response_at, response_at=response_at,
            latency_ms=latency_ms, cost_usd=model_cost_usd, prompt_version=cohort["prompt_version"], model=model)
    if result["action"] == "ENTER":
        _queue_decision(desk_conn, head, now_iso=response_at)
    return result


def verdict_price(candidate, *, clip_usd, basis="emission"):
    """S6 (operator 2026-10-22): the price a desk verdict was made on and the most an entry may pay, or
    None when the snapshot cannot price it (the executor then refuses: never uncapped). ref = the U
    snapshot's curve mcap over the Pump supply; cap_pct = tolerance + the modelled curve fee + the
    modelled impact of the clip on the snapshot's REAL quote reserves (the curve's virtual SOL makes
    the true impact smaller, so this errs loose by about clip/liq - clip/(liq + virtual))."""
    import sniper.settings.memecoin as cfg
    from sniper.services.memecoin_desk_universe import PUMP_SUPPLY_TOKENS
    snap = (candidate or {}).get("snapshot") or {}
    fields = snap.get("fields") or {}
    mcap, liq = fields.get("mcap_usd") or {}, fields.get("liq_usd") or {}
    ok = lambda f: (f.get("completeness") == "complete" and type(f.get("value")) in (int, float)
                    and math.isfinite(f["value"]) and f["value"] > 0)
    if not (ok(mcap) and ok(liq) and str(mcap.get("source", "")).endswith(":curve_reserves")
            and type(clip_usd) in (int, float) and clip_usd > 0):
        return None
    tolerance, fee = float(cfg.MEMECOIN_DESK_VERDICT_TOLERANCE_PCT), float(cfg.MEMECOIN_DESK_VERDICT_FEE_PCT)
    impact = float(clip_usd) / float(liq["value"]) * 100.0
    ref = float(mcap["value"]) / PUMP_SUPPLY_TOKENS
    cap_pct = tolerance + fee + impact
    return {"ref_price_usd": ref, "mcap_usd": float(mcap["value"]), "liq_usd": float(liq["value"]),
            "supply_tokens": PUMP_SUPPLY_TOKENS, "clip_usd": float(clip_usd), "tolerance_pct": tolerance,
            "fee_pct": fee, "impact_pct": impact, "cap_pct": cap_pct, "cap_price_usd": ref * (1 + cap_pct / 100),
            "snapshot_at": float(snap.get("emission_at") or 0.0), "source": str(mcap["source"]),
            "basis": basis}


WSOL_MINT = "So11111111111111111111111111111111111111112"
PUMPSWAP_PRICE_KEYS = ("base_reserve_atomic", "quote_reserve_atomic", "quote_mint", "base_decimals",
                       "supply_atomic", "virtual_quote_atomic", "lp_fee_bps", "protocol_fee_bps",
                       "creator_fee_bps", "slot", "metadata_known_at", "reserve_known_at",
                       "virtual_quote_known_at", "virtual_quote_slot", "virtual_quote_source", "metadata_slot", "cut_slot")
_PS_SOURCE = re.compile(r"[a-z][a-z0-9_.:-]{0,63}\Z")
# The curve formula never prices a pumpswap candidate: verdict_price below only reads ':curve_reserves'.


_U64 = 2 ** 64


def _atomic(v):
    """A u64 atomic from an int or CANONICAL decimal TEXT ('0' or no leading zero), else None (never a
    guessed 0, never an overflow)."""
    if type(v) is int:
        return v if 0 <= v < _U64 else None
    if (isinstance(v, str) and v.isascii() and v.isdecimal() and len(v) <= 20
            and (v == "0" or not v.startswith("0"))):
        n = int(v)
        return n if n < _U64 else None
    return None


def _ps_num(v, *, positive=False):
    """A finite non-negative (or positive) number. A giant int makes math.isfinite raise OverflowError:
    that is malformed, never an exception out of a pricing boundary (Codex)."""
    try:
        return type(v) in (int, float) and math.isfinite(v) and (v > 0 if positive else v >= 0)
    except OverflowError:
        return False


def verdict_price_pumpswap(pool_state, *, as_of, sol_usd, sol_age_s, clip_usd, basis="asof"):
    """The S6 cap for a PumpSwap pool, the same dict verdict_price returns, or None when any input is
    unknown or malformed (the leg then refuses: never uncapped, never a default, never raises). WSOL-
    quoted pools only. ref = (vault quote + virtual quote) / vault base in the mint's own decimals x
    SOL/USD; fee = lp + protocol + creator bps (sum < 10000); impact = clip / (quote + virtual) in USD
    (constant product on the virtual-inclusive quote: the AVERAGE-fill impact, the same shape as the
    curve cap). virtual_quote_atomic must be PRESENT (Lambda omits it when unproven; '0' is a proven
    zero) with its OWN virtual_quote_known_at (the pool-account read or decoder proof that set it, never
    the swap or metadata clock), held to the same reserve age bound (Codex). Metadata and reserves must be known at or before `as_of`; reserves no older than
    MEMECOIN_DESK_PS_RESERVE_MAX_AGE_S and SOL/USD no older than MEMECOIN_DESK_PS_SOL_MAX_AGE_S."""
    import sniper.settings.memecoin as cfg
    try:
        ps = pool_state if isinstance(pool_state, dict) else {}
        if any(k not in ps or ps[k] is None for k in PUMPSWAP_PRICE_KEYS) or ps["quote_mint"] != WSOL_MINT:
            return None
        base, quote, virtual, supply = (_atomic(ps[k]) for k in (
            "base_reserve_atomic", "quote_reserve_atomic", "virtual_quote_atomic", "supply_atomic"))
        fees = [ps[k] for k in ("lp_fee_bps", "protocol_fee_bps", "creator_fee_bps")]
        decimals, slot = ps["base_decimals"], ps["slot"]
        known_at, reserve_at, vq_at = ps["metadata_known_at"], ps["reserve_known_at"], ps["virtual_quote_known_at"]
        vq_slot = ps.get("virtual_quote_slot")
        meta_slot = ps["metadata_slot"]
        tolerance = float(cfg.MEMECOIN_DESK_VERDICT_TOLERANCE_PCT)
        max_reserve_age = float(cfg.MEMECOIN_DESK_PS_RESERVE_MAX_AGE_S)
        max_sol_age = float(cfg.MEMECOIN_DESK_PS_SOL_MAX_AGE_S)
        if (None in (base, quote, virtual, supply) or base == 0 or supply == 0 or quote + virtual == 0
                or type(decimals) is not int or not 0 <= decimals <= 18
                or type(slot) is not int or slot <= 0
                # metadata (immutable decimals) only needs to precede the cut, not the reserve swap
                # (Gamma/Alpha 2026-10-22): metadata_slot < cut_slot; reserves also before the cut
                or type(ps["cut_slot"]) is not int or ps["cut_slot"] <= slot
                or type(meta_slot) is not int or meta_slot <= 0
                # intake (Alpha 2026-10-27): the assembler's mint read lands after the cut; ps_asof's intake mode
                # labels it intake_mint_read only after proving closed mint authority + exact decimals
                or (meta_slot >= ps["cut_slot"] and ps.get("metadata_source") != "intake_mint_read")
                or not all(type(f) is int and 0 <= f < 10_000 for f in fees) or sum(fees) >= 10_000
                or type(vq_slot) is not int or vq_slot <= 0 or vq_slot > slot
                or not isinstance(ps["virtual_quote_source"], str) or not _PS_SOURCE.match(ps["virtual_quote_source"])
                or not all(_ps_num(t) for t in (known_at, reserve_at, vq_at, as_of))
                or known_at >= as_of or reserve_at >= as_of or as_of - reserve_at > max_reserve_age
                or vq_at >= as_of or as_of - vq_at > max_reserve_age
                or not _ps_num(sol_age_s) or sol_age_s > max_sol_age
                or not _ps_num(sol_usd, positive=True) or not _ps_num(clip_usd, positive=True)
                or not all(_ps_num(v) for v in (tolerance, max_reserve_age, max_sol_age))):
            return None
        depth_sol = (quote + virtual) / 1e9
        ref = depth_sol / (base / 10 ** decimals) * float(sol_usd)
        fee = sum(fees) / 100.0
        impact = float(clip_usd) / (depth_sol * float(sol_usd)) * 100.0
        cap_pct = tolerance + fee + impact
        out = {"ref_price_usd": ref, "mcap_usd": ref * supply / 10 ** decimals,
               "liq_usd": quote / 1e9 * float(sol_usd), "supply_tokens": supply / 10 ** decimals,
               "clip_usd": float(clip_usd), "tolerance_pct": tolerance, "fee_pct": fee, "impact_pct": impact,
               "cap_pct": cap_pct, "cap_price_usd": ref * (1 + cap_pct / 100), "snapshot_at": float(as_of),
               "source": "pumpswap:pool_state", "basis": basis}
    except (TypeError, ValueError, OverflowError, ZeroDivisionError, KeyError, AttributeError):
        return None
    nums = [v for k, v in out.items() if k not in ("source", "basis")]
    if not all(_ps_num(v) for v in nums) or ref <= 0 or cap_pct <= 0:
        return None
    return out


def pumpswap_pool_state(candidate):
    """(pool_state, as_of) from a venue='pumpswap' candidate's COMPLETE snapshot.fields.pool_state, else
    (None, None)."""
    snap = (candidate or {}).get("snapshot") or {}
    field = (snap.get("fields") or {}).get("pool_state") or {}
    if field.get("completeness") != "complete" or not isinstance(field.get("value"), dict):
        return None, None
    return field["value"], snap.get("emission_at")


def pumpswap_unpriced_reason(candidate, *, clip_usd=25):
    """None when a pumpswap candidate is priceable, else ONE typed censor reason (Gamma's list), for a
    pre-JUDGE no-call SKIP counted per day. Order: the first missing proof names the reason."""
    import sniper.settings.memecoin as cfg
    field = (((candidate or {}).get("snapshot") or {}).get("fields") or {}).get("pool_state")
    if not field:
        return "unmapped_pool"
    ps, as_of = pumpswap_pool_state(candidate)
    if ps is None:
        return "pool_state_incomplete"
    if not _ps_num(as_of):
        return "pool_state_malformed"
    known = ps.get("metadata_known_at")
    if not _ps_num(known) or known >= as_of or ps.get("base_decimals") is None:
        return "metadata_not_yet_known"
    reserve_at = ps.get("reserve_known_at")
    if not _ps_num(reserve_at) or reserve_at >= as_of or as_of - reserve_at > float(cfg.MEMECOIN_DESK_PS_RESERVE_MAX_AGE_S):
        return "reserve_stale"
    age = ps.get("sol_age_s")
    if not _ps_num(age) or age > float(cfg.MEMECOIN_DESK_PS_SOL_MAX_AGE_S) or not _ps_num(ps.get("sol_usd"), positive=True):
        return "sol_stale"
    if any(type(ps.get(k)) is not int for k in ("lp_fee_bps", "protocol_fee_bps", "creator_fee_bps")):
        return "fee_unknown"
    vq_at = ps.get("virtual_quote_known_at")
    if (ps.get("virtual_quote_atomic") is None or not _ps_num(vq_at) or vq_at >= as_of
            or as_of - vq_at > float(cfg.MEMECOIN_DESK_PS_RESERVE_MAX_AGE_S)):
        return "virtual_quote_unproven"
    if priced_verdict(candidate, clip_usd=clip_usd, basis="asof") is None:
        return "pool_state_malformed"
    return None


def priced_verdict(candidate, *, clip_usd, basis):
    """The shared cap boundary (Codex seam): a pumpswap candidate is priced only from its own complete
    pool_state, never by the curve formula; every other candidate by the curve cap."""
    if (candidate or {}).get("venue") == "pumpswap":
        ps, as_of = pumpswap_pool_state(candidate)
        if ps is None:
            return None
        return verdict_price_pumpswap(ps, as_of=as_of, sol_usd=ps.get("sol_usd"), sol_age_s=ps.get("sol_age_s"),
                                      clip_usd=clip_usd, basis=basis)
    return verdict_price(candidate, clip_usd=clip_usd, basis=basis)


def _queue_decision(desk_conn, head, now_iso=None):
    ticket_id = head["ticket_id"]
    head = _ticket(desk_conn, ticket_id)
    filtered = _stage(head, "hard_filter")
    if filtered is None or filtered["verdict"] != "PASS":
        raise ValueError("hard_filter_required")
    vet = _stage(head, "vet")
    response_at = vet["inputs"].get("response_at", vet["ts"])
    trigger = _stage(head, "trigger")
    if trigger is not None:
        return _queue_trigger(desk_conn, head, vet, trigger, now_iso=now_iso)
    if _is_enter_look(head, vet):
        # watch-list (Alpha 10-26 (c)): a later-look ENTER waits for its fresh hard filter inside its own enter_ttl_s
        refilter = _stage(head, "refilter")
        if refilter is None:
            expires = _time(response_at) + timedelta(seconds=vet["output"]["result"]["enter_ttl_s"])
            if (_time(now_iso) if now_iso else datetime.now(timezone.utc)) < expires:
                return
            store.record_stage(desk_conn, ticket_id=ticket_id, stage="size", seat="code", verdict="SKIP",
                reason="refilter_timeout", inputs={"opportunity_id": head["opportunity_id"], "look": _watch_look(head)[0],
                    "timing": {"refilter_ms": None, "waited_ms": vet["output"]["result"]["enter_ttl_s"] * 1000.0}},
                output={"action": "SKIP", "reason": "refilter_timeout"}, ts=expires.isoformat(), ticket_state="refused")
            return
        if refilter["verdict"] != "PASS":
            return                              # look_filter_kill: record_refilter already refused the ticket
    if _exclude_gated_f(desk_conn, ticket_id, filtered):
        store.record_stage(desk_conn, ticket_id=ticket_id, stage="size", seat="code", verdict="SKIP",
            reason="activity_gate_skip", inputs={"opportunity_id": head["opportunity_id"],
            "gate_state": "skip", "hard_filter_ts": filtered["ts"]},
            output={"action": "SKIP", "reason": "activity_gate_skip"}, ts=response_at)
        return
    try:
        response_at, expires_at, _ = _frozen_entry_timing(desk_conn, head)
    except ValueError as exc:
        reason = str(exc)
        store.record_stage(desk_conn, ticket_id=ticket_id, stage="size", seat="code", verdict="SKIP",
            reason=reason, inputs={"opportunity_id": head["opportunity_id"], "reason": reason},
            output={"action": "SKIP", "reason": reason}, ts=response_at, ticket_state="refused")
        return
    result = vet["output"]["result"]
    metadata = {key: head[key] for key in ("cohort_id", "producer", "opportunity_id", "snapshot_hash")}
    intent_id = "desk-intent:" + _hash({"ticket_id": ticket_id, **metadata})
    # The cap reads the SAME candidate the JUDGE's sheet was rendered from: the as-of-claim refresh
    # (lambda/judge-asof-refresh, vet inputs['asof']) when present, else the emission candidate.
    asof = ((vet["inputs"].get("asof") or {}).get("candidate")
            if isinstance(vet.get("inputs"), dict) else None)
    # A pumpswap ticket is priced ONLY from its frozen as-of candidate: no emission fallback (Codex seam).
    if asof is None and (head["candidate"] or {}).get("venue") == "pumpswap":
        price = None
    else:
        price = priced_verdict(asof or head["candidate"], clip_usd=25, basis="asof" if asof else "emission")
    drift = vet["inputs"].get("drift") if isinstance(vet.get("inputs"), dict) else None
    if price is not None and isinstance(drift, dict):
        price = {**price, "drift_k": drift["k"], "noise_pct": drift["noise_pct"], "drift_limit_pct": drift["k"] * drift["noise_pct"],
                 "drift_enforce": drift["enforce"]}
    arm = vet["inputs"].get("limit_arm") if isinstance(vet.get("inputs"), dict) else None
    if price is not None and arm in (0, 1) and type(arm) is int:
        price = {**price, "limit_arm": arm}
    if price is not None:              # absent = the executor refuses the entry (S6), never uncapped
        metadata["verdict_price"] = price
    if result.get("plan") is not None:  # watch v22: the JUDGE's position plan travels with the entry
        metadata["plan"] = result["plan"]
    payload = {"mint": head["mint"], "network_id": head["network_id"], "size_usd": 25,
        "rails": result["rails"], "rails_mode": "custom", "signal_source": "desk",
        "desk_ticket_id": ticket_id, "desk_metadata": metadata,
        "origin_event_id": "desk:" + ticket_id}
    request = {"intent_id": intent_id, "requested_at": response_at,
               "expires_at": expires_at, "payload": payload}
    store.record_stage(desk_conn, ticket_id=ticket_id, stage="size", seat="code", verdict="ENTER",
        reason="flat_clip", inputs={"opportunity_id": head["opportunity_id"], "request": request},
        output=result, ts=response_at, outbox=request)


def _queue_trigger(desk_conn, head, vet, trigger, now_iso=None):
    """A fired standing order becomes the same size/outbox request as an ENTER, capped at the TRIGGER trade's own
    price (re-anchored, cohort tolerance + fee + impact): no model call, no new RPC."""
    ticket_id, result = head["ticket_id"], vet["output"]["result"]
    fired_at = _fire_clock(trigger)                 # the ORIGINAL fire receipt: a late file read never extends the ttl
    expires_at = (_time(fired_at) + timedelta(seconds=result["enter_ttl_s"])).isoformat()
    if vet["inputs"]["trigger"].get("order") == contract.STANDING_V2:
        # a standing order is minutes to hours old at its touch: it enters only on a FRESH hard filter (the assembler's
        # refilter, off the tape and fire threads). Not yet run: wait for it inside the fire's own ttl, never longer.
        refilter = _stage(head, "refilter")
        if refilter is None:
            if (_time(now_iso) if now_iso else datetime.now(timezone.utc)) < _time(expires_at):
                return
            store.record_stage(desk_conn, ticket_id=ticket_id, stage="size", seat="code", verdict="SKIP",
                reason="refilter_timeout", inputs={"opportunity_id": head["opportunity_id"], "fired_at": fired_at,
                # Gamma: the latency distribution must include the misses (no refilter ran: waited = the whole ttl)
                "timing": {"refilter_ms": None, "waited_ms": result["enter_ttl_s"] * 1000.0,
                           "fire_received_at": (trigger["inputs"].get("fire") or {}).get("received_at")}},
                output={"action": "SKIP", "reason": "refilter_timeout"}, ts=expires_at, ticket_state="refused")
            return
        if refilter["verdict"] != "PASS":
            return                                  # record_refilter already closed the ticket
    metadata = {key: head[key] for key in ("cohort_id", "producer", "opportunity_id", "snapshot_hash")}
    metadata["verdict_price"] = trigger["output"]["verdict_price"]
    if vet["inputs"]["trigger"]["kind"] == "px_limit":   # the frozen USD ceiling can never be widened on the way out
        vp, trig = metadata["verdict_price"], vet["inputs"]["trigger"]
        level = trig.get("level_price_usd")
        frozen_cap = level * (1 + (trig["level_fee_pct"] + trig["level_impact_pct"]) / 100)
        if (vp.get("ref_price_usd") != level or vp.get("tolerance_pct") != 0.0
                or not vp["cap_price_usd"] <= frozen_cap * (1 + 1e-12)):
            raise ValueError("limit_ceiling_widened")
    metadata["standing_order"] = {"kind": vet["inputs"]["trigger"]["kind"], "fire": trigger["inputs"]["fire"],
                                  "level_price_sol": vet["inputs"]["trigger"]["level_price_sol"]}
    if result.get("plan") is not None:
        metadata["plan"] = result["plan"]
    intent_id = "desk-intent:" + _hash({"ticket_id": ticket_id, **{k: head[k] for k in
                                        ("cohort_id", "producer", "opportunity_id", "snapshot_hash")}})
    payload = {"mint": head["mint"], "network_id": head["network_id"], "size_usd": 25,
        "rails": result["rails"], "rails_mode": "custom", "signal_source": "desk",
        "desk_ticket_id": ticket_id, "desk_metadata": metadata, "origin_event_id": "desk:" + ticket_id}
    request = {"intent_id": intent_id, "requested_at": fired_at, "expires_at": expires_at, "payload": payload}
    store.record_stage(desk_conn, ticket_id=ticket_id, stage="size", seat="code", verdict="ENTER",
        reason="standing_order", inputs={"opportunity_id": head["opportunity_id"], "request": request},
        output=result, ts=fired_at, outbox=request)


REFILTER_SEAT = "assembler"


def list_pending_refilters(desk_conn, *, limit=20):
    """Fired standing-v2 orders still waiting for their fresh hard filter, oldest fire first."""
    rows = desk_conn.execute(
        "SELECT t.ticket_id FROM desk_tickets t JOIN desk_tickets v ON v.ticket_id=t.ticket_id AND v.stage='vet' "
        "WHERE t.stage='trigger' AND json_extract(v.inputs_json,'$.trigger.order')=? "
        "AND NOT EXISTS (SELECT 1 FROM desk_tickets r WHERE r.ticket_id=t.ticket_id AND r.stage IN ('refilter','size')) "
        "ORDER BY t.ts LIMIT ?", (contract.STANDING_V2, limit)).fetchall()
    # watch-list ENTER looks (Alpha 10-26 (c)): a later look enters only on a fresh hard filter too
    looks = desk_conn.execute(
        "SELECT v.ticket_id FROM desk_tickets v JOIN desk_candidate_emissions e ON e.opportunity_id=v.ticket_id "
        "AND e.is_primary=1 WHERE v.stage='vet' AND v.verdict='ENTER' "
        "AND json_extract(e.candidate_json,'$.watch_look') IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM desk_tickets r WHERE r.ticket_id=v.ticket_id AND r.stage IN ('refilter','size')) "
        "ORDER BY v.ts LIMIT ?", (limit,)).fetchall()
    return [_ticket(desk_conn, r[0]) for r in rows + looks]


def _is_enter_look(head, vet):
    return vet is not None and vet["verdict"] == "ENTER" and "watch_look" in (head.get("candidate") or {})


def record_refilter(desk_conn, ticket_id, *, evidence, now_iso, timing):
    """The fresh hard filter for a fired standing-v2 order, on the cohort's frozen hard-filter policy and the same
    contract.hard_filter as admission. PASS lets the courier queue the fire; anything else refuses the ticket by name
    (touched_refused:<reason>). `timing` = {"refilter_ms", "fire_received_at"} for the p50/p90 the operator asked for."""
    head = _ticket(desk_conn, ticket_id)
    cohort = _cohort(desk_conn, head)
    vet, trigger = _stage(head, "vet"), _stage(head, "trigger")
    order = trigger is not None and vet is not None and (vet["inputs"].get("trigger") or {}).get("order") == contract.STANDING_V2
    if not (order or _is_enter_look(head, vet)) or _stage(head, "refilter") is not None or _stage(head, "size") is not None:
        raise ValueError("not_a_pending_refilter")
    if not order and _watch_look(head)[0] > 1 and not _watch_alive(desk_conn, head, now_iso):
        result = {"action": "SKIP", "reason": "watch_life"}     # Zeta P1: no entry queued after the root's life
    else:
        result = contract.hard_filter(head["candidate"], evidence=evidence, policy=cohort["policy"]["hard_filter"],
                                      evaluated_at=now_iso)
    passed = result["action"] == "PASS"
    row = store.record_stage(desk_conn, ticket_id=ticket_id, stage="refilter", seat=REFILTER_SEAT,
        verdict="PASS" if passed else "SKIP", reason=result["reason"],
        inputs={"opportunity_id": head["opportunity_id"], "evidence_hash": _response_hash(evidence),
                "evidence": evidence, "timing": timing},
        output=result, ts=now_iso, **({} if passed else {"ticket_state": "refused"}))
    if not passed:
        record_order_ends(desk_conn, now_iso=now_iso)
    if not order and passed:                    # an ENTER look: queue it now, like an ordinary ENTER
        _queue_decision(desk_conn, _ticket(desk_conn, ticket_id), now_iso=now_iso)
    return row


def _fire_clock(trigger):
    return datetime.fromtimestamp(trigger["inputs"]["fire"]["received_at"], timezone.utc).isoformat()


def _trigger_tolerance(policy):
    """The cohort's frozen verdict_tolerance_pct when present (runtime may only tighten it), else the runtime knob
    (legacy cohorts unchanged). A malformed frozen value refuses the fire."""
    import sniper.settings.memecoin as cfg
    runtime = float(cfg.MEMECOIN_DESK_VERDICT_TOLERANCE_PCT)
    if "verdict_tolerance_pct" not in policy:
        return runtime
    frozen = policy["verdict_tolerance_pct"]
    if type(frozen) not in (int, float) or not math.isfinite(frozen) or not 0 <= frozen <= 50:
        raise ValueError("cohort_policy:verdict_tolerance_pct")
    return min(float(frozen), runtime)


_FIRE_KEYS = ("slot", "signature", "ordinal", "tx_index", "block_time", "received_at", "price_sol",
              "real_quote_sol", "sol_usd", "sol_at", "curve_pct")


def record_fire(desk_conn, ticket_id, fire, *, now_iso):
    """Freeze one standing-order fire (the executor's matching live trade) as the ticket's 'trigger' stage; the
    courier then queues it like an ENTER. Every precondition is re-checked here, in the writer: a stale, late,
    mispriced or post-graduation-guard fire is refused (ValueError(reason)), never queued."""
    head = _ticket(desk_conn, ticket_id)
    cohort = _cohort(desk_conn, head)
    standing = _standing(cohort["policy"]) or _chase(cohort["policy"])
    vet = _stage(head, "vet")
    if standing is None:
        raise ValueError("standing_off")
    if head["state"] != "waiting" or vet is None or vet["verdict"] != "WAIT" or "trigger" not in vet["inputs"]:
        raise ValueError("not_a_waiting_trigger")
    if not isinstance(fire, dict) or set(fire) != set(_FIRE_KEYS) or not all(
            type(fire[k]) in (int, float) and math.isfinite(fire[k]) for k in _FIRE_KEYS if k not in ("signature",)):
        raise ValueError("fire_shape")
    trig, now = vet["inputs"]["trigger"], _time(now_iso)
    expires = datetime.fromisoformat(trig["expires_at"])
    if not fire["block_time"] > datetime.fromisoformat(vet["inputs"]["response_at"]).timestamp() - 1:
        raise ValueError("fire_before_decision")
    if fire["received_at"] > expires.timestamp() or now > expires + timedelta(seconds=2):
        raise ValueError("fire_expired")
    if not 0 <= now.timestamp() - fire["sol_at"] <= 120 or fire["sol_usd"] <= 0 or fire["sol_at"] > fire["received_at"]:
        raise ValueError("fire_sol_stale")         # a future-stamped FX is no causal FX either (eCat on 7d8dd4998)
    level = trig["level_price_sol"]
    if fire["price_sol"] <= 0 or (trig["kind"] == "px_up" and not fire["price_sol"] >= level) \
            or (trig["kind"] == "px_hold" and not fire["price_sol"] > level) \
            or (trig["kind"] == "px_limit" and trig.get("order") != contract.STANDING_V2 and not fire["price_sol"] <= level):
        # a standing-v2 order's contract is its frozen USD level (checked below with the fire's FX), not the SOL it implied
        raise ValueError("fire_price")
    if not fire["curve_pct"] < standing["graduation_guard_pct"]:
        raise ValueError("fire_graduation_guard")
    import sniper.settings.memecoin as cfg
    from sniper.services.memecoin_desk_universe import PUMP_SUPPLY_TOKENS
    tolerance, fee = _trigger_tolerance(cohort["policy"]), float(cfg.MEMECOIN_DESK_VERDICT_FEE_PCT)
    if trig["kind"] == "px_limit":
        tolerance = 0.0                 # LIMIT arm: the sheet price or better, plus only the modelled fee and impact
    if now.timestamp() >= fire["received_at"] + vet["output"]["result"]["enter_ttl_s"]:
        raise ValueError("fire_ttl_expired")        # the entry window counts from the fire receipt, never from consumption
    ref, liq = fire["price_sol"] * fire["sol_usd"], fire["real_quote_sol"] * fire["sol_usd"]
    if not liq > 0:
        raise ValueError("fire_liquidity")
    if trig["kind"] == "px_limit":
        # the ceiling is the parent's frozen USD sheet price, never SOL x a fresh FX (eCat: SOL 100 -> 110 at the same SOL
        # price would buy 10 % above the sheet). The fired entry's ref IS that frozen USD level; cap = fee + impact only.
        level_usd = trig.get("level_price_usd")
        if not (type(level_usd) in (int, float) and math.isfinite(level_usd) and level_usd > 0):
            raise ValueError("fire_limit_unpriced")
        if not ref <= level_usd:
            raise ValueError("fire_price_usd")
        ref = level_usd
    if "ceiling_price_usd" in trig:          # entry-gate-v1: the original E-now USD sheet price stays the reference ceiling
        ref = min(ref, trig["ceiling_price_usd"])
    impact = 25.0 / liq * 100.0
    if trig["kind"] == "px_limit":           # never a wider allowance than the parent froze (thinner liquidity at the fire)
        fee, impact = min(fee, float(trig["level_fee_pct"])), min(impact, float(trig["level_impact_pct"]))
    cap_pct = tolerance + fee + impact
    price = {"ref_price_usd": ref, "mcap_usd": ref * PUMP_SUPPLY_TOKENS, "liq_usd": liq, "supply_tokens": PUMP_SUPPLY_TOKENS,
             "clip_usd": 25.0, "tolerance_pct": tolerance, "fee_pct": fee, "impact_pct": impact, "cap_pct": cap_pct,
             "cap_price_usd": ref * (1 + cap_pct / 100), "snapshot_at": float(fire["received_at"]),
             "source": "standing_order:fire_trade", "basis": "trigger"}
    return store.record_stage(desk_conn, ticket_id=ticket_id, stage="trigger", seat="code", verdict="ENTER",
        reason="standing_order_" + trig["kind"], inputs={"fire": fire, "trigger": trig},
        output={"verdict_price": price}, ts=now_iso)


def expire_watches(desk_conn, *, now_iso, limit=200):
    """Release every waiting ticket whose trigger expired, whose WATCH passed the cohort's watch_max_s, or whose
    cohort's standing orders are off: the mint is free again. Returns the expired ticket ids."""
    now, out = _time(now_iso), []
    for head in store.list_waiting_tickets(desk_conn, limit=limit):
        vet = _stage(head, "vet")
        if _stage(head, "trigger") is not None:
            continue                     # a durably accepted fire is transitioning to the courier: its ENTER ttl rules it
        policy = _cohort(desk_conn, head)["policy"]
        standing = _standing(policy) or _chase(policy)
        if standing is None and vet is not None and vet["reason"] == "watch" and "watch_list" in policy:
            continue                     # a watch-list WATCH: relook_watches owns its life and its looks
        if standing is None:
            reason = "standing_off"
        elif vet is not None and vet["verdict"] == "WAIT" and "trigger" in vet["inputs"]:
            expired = now > datetime.fromisoformat(vet["inputs"]["trigger"]["expires_at"])
            # a LIMIT-arm miss is its own outcome row (Gamma's intention-to-treat primary counts it as a $0 ticket)
            kind = ("max_life" if vet["inputs"]["trigger"].get("order") == contract.STANDING_V2 else
                    "limit_watch_miss" if vet["inputs"]["trigger"]["kind"] == "px_limit" else "trigger_expired")
            reason = kind if expired else None
        else:
            start = datetime.fromisoformat(vet["inputs"]["response_at"] if vet else head["created_at"])
            reason = "watch_expired" if (now - start).total_seconds() > standing["watch_max_s"] else None
        if reason is not None:
            store.record_stage(desk_conn, ticket_id=head["ticket_id"], stage="expire", seat="code", verdict="SKIP",
                reason=reason, inputs={"opportunity_id": head["opportunity_id"]}, output={"reason": reason},
                ts=now_iso, ticket_state="expired")
            out.append(head["ticket_id"])
    record_order_ends(desk_conn, now_iso=now_iso)        # prereg section 4: the end row lands with the terminal write
    return out


def _existing_matches(existing, request):
    payload = request["payload"]
    expected = {k: payload[k] for k in ("mint", "network_id", "size_usd", "rails_mode", "origin_event_id")}
    expected.update(payload["rails"])
    expected.update(requested_at=request["requested_at"], expires_at=request["expires_at"],
                    desk_ticket_id=request["ticket_id"], desk_metadata=payload["desk_metadata"])
    return all(existing.get(k) == v for k, v in expected.items())


def _at(exc, ticket_id):
    """The refused row travels with the error: the courier's page names it (Codex/Gamma on dd39f54ba)."""
    exc.ticket_id = ticket_id
    return exc


def _hold_facts(metadata, gate):
    """What a cost-gate hold is made of, for the log line, the page and the censored-expiry stage (Gamma)."""
    return {"cohort_id": metadata["cohort_id"], "unknown_by_kind": gate["unknown_by_kind"],
            "flat_model": gate["flat_model"], "unpriced_closes": gate["unpriced_closes"],
            "known_net_usd": gate["known_net_usd"], "bounded_net_usd": gate["bounded_net_usd"],
            "bound_usd": gate["bound_usd"], "oldest_unknown_at": gate["oldest_unknown_at"], "legacy": gate["legacy"]}


def dispatch_outbox(desk_conn, ledger_conn, *, now_iso, limit=10, dump_cache=None):
    """Courier only. Existing ledger requests win over expiry and current admission holds. `dump_cache` (Codex's
    memecoin_desk_dump_group_cache.SnapshotCache, started once by the JUDGE worker; lookup has zero I/O) is the final
    dump-group gate right before the real or paper route: a kill refuses the request durably by name."""
    now = _time(now_iso)
    results = []
    for head in store.list_undispatched_decisions(desk_conn, limit=limit):
        try:
            _queue_decision(desk_conn, head, now_iso=now_iso)
        except ValueError as exc:
            raise _at(exc, head["ticket_id"])
    for request in store.list_outbox(desk_conn, limit=limit):
        existing = intents.read_status(ledger_conn, request["intent_id"])
        if existing is not None:
            if not _existing_matches(existing, request):
                raise _at(ValueError("intent_conflict"), request["ticket_id"])
            results.append(store.resolve_outbox(desk_conn, request["ticket_id"], state="sent",
                intent_id=request["intent_id"], now_iso=now_iso))
            continue
        head = _ticket(desk_conn, request["ticket_id"])
        filtered = _stage(head, "hard_filter")
        if filtered is None or filtered["verdict"] != "PASS":
            results.append(store.resolve_outbox(desk_conn, request["ticket_id"], state="refused",
                reason="hard_filter_required", now_iso=now_iso))
            continue
        if _exclude_gated_f(desk_conn, request["ticket_id"], filtered):
            results.append(store.resolve_outbox(desk_conn, request["ticket_id"], state="refused",
                reason="activity_gate_skip", now_iso=now_iso))
            continue
        try:
            trigger = _stage(head, "trigger")
            if trigger is not None:            # a fired standing order: the fire receipt clock, the vet's frozen enter_ttl_s
                ttl = _stage(head, "vet")["output"]["result"]["enter_ttl_s"]
                response_at = _fire_clock(trigger)
                expires_at = (_time(response_at) + timedelta(seconds=ttl)).isoformat()
            else:
                response_at, expires_at, ttl = _frozen_entry_timing(desk_conn, head)
            if request["requested_at"] != response_at or request["expires_at"] != expires_at:
                raise ValueError("expiry_conflict")
        except ValueError as exc:
            results.append(store.resolve_outbox(desk_conn, request["ticket_id"], state="refused",
                reason=str(exc), now_iso=now_iso))
            continue
        if now >= _time(request["expires_at"]):
            if request.get("first_hold_at"):
                # JUDGE chose ENTER and the cost gate held it before its expiry (durable first-hold evidence):
                # a CENSORED execution, not a miss. Stage + outbox expiry in one transaction; head -> expired.
                facts = {**json.loads(request["first_hold_json"]), "first_hold_at": request["first_hold_at"]}
                store.record_stage(desk_conn, ticket_id=request["ticket_id"], stage="courier", seat="engine",
                    verdict="SKIP", reason="enter_expired_accounting", inputs=facts, ts=now_iso,
                    expire_outbox_reason="enter_expired_accounting")
                results.append({"ticket_id": request["ticket_id"], "state": "expired",
                                "reason": "enter_expired_accounting"})
                continue
            results.append(store.resolve_outbox(desk_conn, request["ticket_id"], state="expired",
                reason="entry_expired", now_iso=now_iso))
            continue
        if now < _time(request["requested_at"]):
            raise _at(ValueError("chronology:future_request"), request["ticket_id"])
        metadata = request["payload"]["desk_metadata"]
        gate = ledger.desk_cohort_gate(ledger_conn, cohort_id=metadata["cohort_id"], producer=metadata["producer"])
        refusal = gate["refusal"]
        if refusal:
            if refusal == "cohort_accounting_unknown":
                facts = _hold_facts(metadata, gate)
                store.note_outbox_hold(desk_conn, request["ticket_id"], facts=facts, at_iso=now_iso)
                results.append({"ticket_id": request["ticket_id"], "state": "pending", "reason": refusal,
                                "hold": facts})
            else:
                results.append(store.resolve_outbox(desk_conn, request["ticket_id"], state="refused",
                    reason=refusal, now_iso=now_iso))
            continue
        head_c = head["candidate"] or {}
        if dump_cache is not None:
            dump = dump_cache.lookup(mint=head["mint"], pool=head_c.get("pool"), now=now.timestamp())
        else:                                   # Codex: an absent gate is a typed unknown on the ticket, never silent
            dump = {"status": "unknown", "reason": "dump_group_unknown", "detail": "cache_absent"}
        try:
            store.record_stage(desk_conn, ticket_id=request["ticket_id"], stage="dump_gate", seat="code", verdict="HOLD",
                reason=str((dump or {}).get("status")), inputs={"opportunity_id": head["opportunity_id"], "dump_group": dump},
                output=dump if isinstance(dump, dict) else {"raw": str(dump)}, ts=now_iso)
        except ValueError:
            pass                                # already recorded for this ticket (a retried pass)
        if isinstance(dump, dict) and dump.get("status") == "kill" and dump.get("reason") == "dump_group_buyers":
            results.append(store.resolve_outbox(desk_conn, request["ticket_id"], state="refused",
                reason="dump_group_buyers", now_iso=now_iso))
            continue
        from sniper.services import memecoin_desk_paper as paper
        if paper.execution_mode() == "paper":
            # DESK PAPER MODE (operator 10-26): NO manual intent is written, so the funded runner never sees this entry;
            # the paper engine (its own table in this desk DB) owns it from here. The ticket closes as paper_routed.
            refusal = paper.admit(desk_conn, request, now_iso=now_iso)
            results.append({**store.resolve_outbox(desk_conn, request["ticket_id"], state="refused",
                reason="paper_routed" if refusal is None else "paper_refused:" + refusal, now_iso=now_iso),
                "dump_group": dump})
            continue
        try:
            identity = intents.enqueue(ledger_conn, **request["payload"], intent_id=request["intent_id"],
                                      now_iso=request["requested_at"], ttl_s=ttl)
        except ValueError as exc:
            results.append(store.resolve_outbox(desk_conn, request["ticket_id"], state="refused",
                reason=str(exc), now_iso=now_iso))
            continue
        results.append({**store.resolve_outbox(desk_conn, request["ticket_id"], state="sent",
            intent_id=identity, now_iso=now_iso), "dump_group": dump})        # typed unknown / pass metadata logged
    return results


_DRIFT_REASON = re.compile(r"desk drift: quote is ([+-][0-9]+\.[0-9]+)% over the verdict price")


def drift_block_of(status):
    """The drift block of a fill-drift refusal (Lambda's S6 gate: result_status skipped_chase, reason 'desk drift: ...'),
    from the frozen verdict_price (ref, fee, impact, noise, k, limit) plus the refused drift %; else None. The quote price
    is reconstructed as ref x (1 + (drift + fee + impact) / 100), the gate's own arithmetic (2-decimal drift)."""
    found = _DRIFT_REASON.match(str((status or {}).get("reason") or ""))
    vp = ((status or {}).get("desk_metadata") or {}).get("verdict_price") or {}
    if found is None or (status or {}).get("result_status") != "skipped_chase":
        return None
    nums = {k: vp.get(k) for k in ("ref_price_usd", "fee_pct", "impact_pct", "noise_pct", "drift_k", "drift_limit_pct")}
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in nums.values()) or not nums["ref_price_usd"] > 0:
        return None
    drift = float(found.group(1))
    block = {"sheet_price_usd": nums["ref_price_usd"],
             "quote_price_usd": nums["ref_price_usd"] * (1 + (drift + nums["fee_pct"] + nums["impact_pct"]) / 100),
             "drift_pct": drift, "noise_pct": nums["noise_pct"], "k": nums["drift_k"], "limit_pct": nums["drift_limit_pct"]}
    try:
        return contract.validate_drift(block)
    except ValueError:
        return None


def _drift_requeue(desk_conn, ticket_id, drift, *, now_iso):
    """ONE fresh JUDGE call after a fill-drift refusal (operator: the gate re-judges, it never hard-blocks): a new ticket
    carrying the prior candidate and a drift_requeue claim, hard-filtered on the prior ticket's evidence (same snapshot).
    Only in a cohort that froze drift-requeue-v1; the store refuses a second re-judge. Never raises into reconcile."""
    import logging
    try:
        head = _ticket(desk_conn, ticket_id)
        cohort = _cohort(desk_conn, head)
        if cohort["policy"].get("drift_requeue") != store.DRIFT_REQUEUE_VERSION:
            return None
        prior = _stage(head, "hard_filter")
        evidence = (prior or {}).get("inputs", {}).get("evidence")
        if prior is None or prior["verdict"] != "PASS" or evidence is None:
            return {"ticket_id": ticket_id, "state": "drift_requeue_skipped", "reason": "no_prior_evidence"}
        cand = {k: v for k, v in head["candidate"].items() if k not in ("opportunity_id", "snapshot_hash", "reentry")}
        new_id = ticket_id + ":drift"
        child = store.get_ticket(desk_conn, new_id)
        if child is None:                         # the candidate is deterministic: a retry after a crash is a duplicate
            cand.update(ticket_id=new_id, drift_requeue={"prev_ticket_id": ticket_id, "drift": drift})
            got = store.record_candidate(desk_conn, contract.validate_candidate(cand), cohort_id=head["cohort_id"],
                                         now_iso=now_iso, cooldown_s=0)
            if not got["is_primary"]:
                return {"ticket_id": ticket_id, "state": "drift_requeue_refused", "reason": got["reason"]}
            child = store.get_ticket(desk_conn, new_id)
        if _stage(child, "hard_filter") is not None:
            return None                           # done before (or resumed): nothing to report again
        evaluate_candidate(desk_conn, new_id, evidence=evidence, policy=cohort["policy"]["hard_filter"], now_iso=now_iso)
        return {"ticket_id": new_id, "state": "drift_requeued", "prev_ticket_id": ticket_id}
    except Exception as exc:                          # named and logged: the refusal row above stands either way
        logging.getLogger(__name__).exception("desk drift re-queue failed ticket=%s", ticket_id)
        return {"ticket_id": ticket_id, "state": "drift_requeue_error", "reason": type(exc).__name__}


LIMIT_WATCH_PREFIX = "desk limit watch:"     # Lambda's S6 refusal reason prefix (pinned by a test on both sides)


def is_limit_watch_refusal(status):
    """A LIMIT-arm refusal: result_status skipped_chase, the fixed reason prefix, and the ticket's frozen limit_arm == 1."""
    vp = ((status or {}).get("desk_metadata") or {}).get("verdict_price") or {}
    return ((status or {}).get("result_status") == "skipped_chase"
            and str((status or {}).get("reason") or "").startswith(LIMIT_WATCH_PREFIX) and vp.get("limit_arm") == 1)


_CHASE_GAP = re.compile(r"([+-]?[0-9]+(?:\.[0-9]+)?)% over the verdict price")


def order_end_row(desk_conn, ticket_id):
    """Gamma prereg b7d133fc8 section 4: ONE row per standing-v2 order, derived from its durable stages once it is
    terminal, else None. Facts this process never sees (quote/tape price at touch+1.5 s, fill price) are None by name;
    Gamma joins them from the tape and the ledger."""
    head = _ticket(desk_conn, ticket_id)
    vet = _stage(head, "vet")
    trig = ((vet or {}).get("inputs") or {}).get("trigger") or {}
    if trig.get("order") != contract.STANDING_V2 and not (vet and vet["reason"] in ("book_full", "limit_watch_unpriced")
                                                         and vet["inputs"].get("limit_of")):
        return None
    stages = {st["stage"]: st for st in head["stages"]}
    fire, refilter, size, fill, expire = (stages.get(k) for k in ("trigger", "refilter", "size", "fill", "expire"))
    if vet["verdict"] == "SKIP":
        end = "book_full" if vet["reason"] == "book_full" else "unpriced"
    elif fill is not None:
        end = "touched_filled" if fill["verdict"] == "ENTER" else "touched_refused:intent_%s:%s" % (
            fill["output"].get("state"), fill["output"].get("status"))
    elif refilter is not None and refilter["verdict"] != "PASS":
        end = "touched_refused:filter:" + refilter["reason"]
    elif size is not None and size["verdict"] == "SKIP":
        end = "touched_refused:" + size["reason"]
    elif expire is not None:
        end = {"standing_off": "cancelled", "trigger_expired": "max_life"}.get(expire["reason"], expire["reason"])
    else:
        return None                               # live: no row yet
    parent = _ticket(desk_conn, vet["inputs"]["limit_of"]) if vet["inputs"].get("limit_of") else None
    pfill = _stage(parent, "fill") if parent else None
    psize = _stage(parent, "size") if parent else None
    pvp = ((((psize or {}).get("inputs") or {}).get("request") or {}).get("payload", {}).get("desk_metadata", {})
           .get("verdict_price"))
    refused = (pfill or {}).get("output", {}).get("refused_reason") if pfill else None
    gap = _CHASE_GAP.search(str(refused or ""))
    fired = (fire or {}).get("inputs", {}).get("fire") or {}
    end_at = max(st["ts"] for st in head["stages"])
    arms = vet["inputs"].get("arms") or standing_arms(ticket_root(ticket_id))
    return {"order_id": ticket_id, "cohort_key": head["cohort_id"], "mode": trig.get("order_mode"),
            "level_usd_mcap": trig.get("level_mcap_usd"), "level_unit_label": trig.get("level_unit"),
            "level_basis": {"drop_pct": trig.get("level_pct"), "noise_multiple": None, "basis": "verdict_price"},
            "sheet_price_at_create": trig.get("level_price_usd"), "created_at": vet["ts"],
            "touch_at": fired.get("block_time"), "tape_price_at_touch": fired.get("price_sol"),
            "tape_price_at_touch_plus_1_5s": None, "quote_at_touch": None, "fill_price": None, "slippage_vs_level": None,
            "fresh_filter_verdict_at_touch": (refilter or {}).get("verdict"),
            "refilter_ms": (((refilter or size or {}).get("inputs") or {}).get("timing") or {}).get("refilter_ms"),
            "refused_quote": refused, "verdict_price": pvp, "chase_gap_pct": float(gap.group(1)) if gap else None,
            "end_reason": end, "life_s": (_time(end_at) - _time(vet["ts"])).total_seconds(), "gap_at_touch": False if fire else None,
            "arm": arms["standing_arm"], "limit_arm": arms["limit_arm"], "position_id": (fill or {}).get("position_id")}


def record_order_ends(desk_conn, *, now_iso, limit=100):
    """Write the section-4 row (stage 'order_end') for every terminal standing-v2 order that has none. Idempotent."""
    rows = desk_conn.execute(
        "SELECT h.ticket_id FROM desk_ticket_heads h JOIN desk_tickets v ON v.ticket_id=h.ticket_id AND v.stage='vet' "
        "WHERE h.state NOT IN ('open','waiting','queued','unresolved') AND (json_extract(v.inputs_json,'$.trigger.order')=? "
        "OR (v.verdict='SKIP' AND json_extract(v.inputs_json,'$.limit_of') IS NOT NULL AND v.reason IN ('book_full'))) "
        "AND NOT EXISTS (SELECT 1 FROM desk_tickets e WHERE e.ticket_id=h.ticket_id AND e.stage='order_end') LIMIT ?",
        (contract.STANDING_V2, limit)).fetchall()
    out = []
    for (ticket_id,) in rows:
        row = order_end_row(desk_conn, ticket_id)
        if row is None:
            continue
        # verdict HOLD and no ticket_state: a pure record that never moves the head (filled/closed included; Zeta)
        store.record_stage(desk_conn, ticket_id=ticket_id, stage="order_end", seat="code", verdict="HOLD",
            reason=row["end_reason"].split(":")[0],
            inputs={"opportunity_id": _ticket(desk_conn, ticket_id)["opportunity_id"], "order_end": row["end_reason"]},
            output=row, ts=now_iso)
        out.append({"ticket_id": ticket_id, "state": "order_end", "reason": row["end_reason"]})
    return out


def paper_chase(desk_conn, ticket_id, refusal, *, now_iso):
    """Paper mode, item 3 (Alpha 10-26): a PAPER entry refused skipped_chase takes the SAME arm logic as a real one
    (_is_chase_refusal: chase_limit cohort, root ticket, standing arm 1). The paper refusal is written as the parent's fill
    stage, exactly the shape reconcile_intents writes for a real refusal, and the LIMIT/chase child opens from it."""
    status = {"result_status": "skipped_chase", "reason": refusal}
    if not (_is_chase_refusal(desk_conn, ticket_id, status) or str(refusal).startswith(LIMIT_WATCH_PREFIX)):
        return None
    head = _ticket(desk_conn, ticket_id)
    if _stage(head, "fill") is None:
        store.record_stage(desk_conn, ticket_id=ticket_id, stage="fill", seat="engine", verdict="SKIP",
            reason="intent_refused", inputs={"intent_id": "paper:" + ticket_id},
            output={"state": "refused", "status": "skipped_chase", "limit_watch": True, "refused_reason": refusal,
                    "paper": True}, ts=now_iso)
    return _limit_watch(desk_conn, ticket_id, now_iso=now_iso)


def _is_chase_refusal(desk_conn, ticket_id, status):
    """chase-limit-v1 arm B: an ENTER the executor refused as skipped_chase (quote above the verdict cap) in a cohort that
    froze chase_limit, on the ROOT ticket only (a :limit child is never chased again), standing_arm (bit 1) = 1."""
    if (status or {}).get("result_status") != "skipped_chase" or ticket_root(ticket_id) != ticket_id:
        return False
    if _chase(_cohort(desk_conn, _ticket(desk_conn, ticket_id))["policy"]) is None:
        return False
    return standing_arms(ticket_id)["standing_arm"] == 1


def _limit_watch(desk_conn, ticket_id, *, now_iso):
    """The LIMIT arm's child: <ticket>:limit, the same candidate and evidence, and a code-written vet WAIT whose trigger is
    px_limit at the sheet's as-of price (the basis px_hold uses), 60 s, confirm 1. No model call. The fired entry is capped
    at the fire price + fee + impact (tolerance 0, record_fire). An unpriced sheet is a recorded miss. Idempotent."""
    import logging
    try:
        head = _ticket(desk_conn, ticket_id)
        cohort = _cohort(desk_conn, head)
        chase = _chase(cohort["policy"])
        if (cohort["policy"].get("standing_orders") or {}).get("limit_arm") is not True and chase is None:
            return None
        prior, vet = _stage(head, "hard_filter"), _stage(head, "vet")
        evidence = (prior or {}).get("inputs", {}).get("evidence")
        new_id = ticket_id + ":limit"
        child = store.get_ticket(desk_conn, new_id)
        if child is None:
            if prior is None or prior["verdict"] != "PASS" or evidence is None or vet is None:
                return {"ticket_id": ticket_id, "state": "limit_watch_skipped", "reason": "no_prior_evidence"}
            cand = {k: v for k, v in head["candidate"].items() if k not in ("opportunity_id", "snapshot_hash", "reentry",
                                                                           "drift_requeue")}
            cand.update(ticket_id=new_id, limit_watch={"prev_ticket_id": ticket_id})
            got = store.record_candidate(desk_conn, contract.validate_candidate(cand), cohort_id=head["cohort_id"],
                                         now_iso=now_iso, cooldown_s=0)
            if not got["is_primary"]:
                return {"ticket_id": ticket_id, "state": "limit_watch_refused", "reason": got["reason"]}
            child = store.get_ticket(desk_conn, new_id)
        if _stage(child, "hard_filter") is None:
            evaluate_candidate(desk_conn, new_id, evidence=evidence, policy=cohort["policy"]["hard_filter"], now_iso=now_iso)
            child = store.get_ticket(desk_conn, new_id)
        if _stage(child, "vet") is not None:
            return None
        basis = _asof_price_sol(vet["inputs"].get("asof"))
        size = _stage(head, "size")                 # the parent's frozen sheet price in USD: the watch's ceiling (eCat FX RC)
        parent_vp = ((((size or {}).get("inputs") or {}).get("request") or {}).get("payload", {})
                     .get("desk_metadata", {}).get("verdict_price", {}))
        level_usd = parent_vp.get("ref_price_usd")
        allowance = {k: parent_vp.get(k) for k in ("fee_pct", "impact_pct")}   # the parent's frozen fee + impact allowance
        num = lambda v: type(v) in (int, float) and math.isfinite(v)                                    # noqa: E731
        if not (num(level_usd) and level_usd > 0 and all(num(v) and v >= 0 for v in allowance.values())):
            basis = None                            # no frozen USD ceiling: nothing to hold the fire to
        result = {**vet["output"]["result"], "action": "WAIT", "reason": "chase_limit" if chase else "limit_watch",
                  "condition": ["px_limit", "<=", 0, 1, contract.LIMIT_WATCH_S]}
        book_full = False
        if chase is not None and basis is not None:
            live = desk_conn.execute(
                "SELECT COUNT(*) FROM desk_ticket_heads h JOIN desk_tickets t ON t.ticket_id=h.ticket_id AND t.stage='vet' "
                "WHERE h.state='waiting' AND json_extract(t.inputs_json,'$.trigger.order')=?", (contract.STANDING_V2,)).fetchone()[0]
            if live >= chase["book_max"]:
                basis, book_full = None, True
        if basis is None:                       # nothing to watch against: a recorded miss, never a guessed level
            result = {**result, "action": "SKIP", "reason": "book_full" if book_full else "limit_watch_unpriced",
                      "rails": None, "condition": None}
        inputs = {"opportunity_id": child["opportunity_id"], "snapshot_hash": child["snapshot_hash"],
                  "limit_of": ticket_id, "response_at": now_iso, "result": result}
        if basis is not None:
            inputs["trigger"] = {"kind": "px_limit", "level_pct": 0.0, "asked_level_pct": 0.0, "range5_pct": None,
                                 "hold_s": None, "basis_price_sol": basis, "level_price_sol": basis,
                                 "level_price_usd": level_usd, "level_fee_pct": allowance["fee_pct"],
                                 "level_impact_pct": allowance["impact_pct"], "confirm": 1,
                                 "expires_at": (_time(now_iso) + timedelta(seconds=contract.LIMIT_WATCH_S)).isoformat()}
            if chase is not None:       # chase-limit B: a standing-v2 order (USD level, 2 h, band cancel, fresh refilter)
                from sniper.services.memecoin_desk_universe import PUMP_SUPPLY_TOKENS
                inputs["trigger"].update(
                    order=contract.STANDING_V2, order_mode="chase_limit", level_unit="usd_mcap",
                    level_mcap_usd=level_usd * PUMP_SUPPLY_TOKENS,
                    expires_at=(_time(now_iso) + timedelta(seconds=chase["order_max_s"])).isoformat())
                inputs["arms"] = standing_arms(ticket_root(ticket_id))
                try:
                    inputs["trigger"]["cancel_band_usd_mcap"] = list(contract.mcap_band(cohort["policy"]["hard_filter"]))
                except (ValueError, TypeError, KeyError):
                    pass
        store.record_stage(desk_conn, ticket_id=new_id, stage="vet", seat="code",
            verdict="WAIT" if basis is not None else "SKIP", reason=result["reason"], inputs=inputs,
            output={"raw": None, "raw_error": None, "result": result}, ts=now_iso, response_at=now_iso,
            prompt_version=cohort["prompt_version"])
        return {"ticket_id": new_id, "state": "limit_watching" if basis is not None else "limit_watch_miss",
                "prev_ticket_id": ticket_id}
    except Exception as exc:
        logging.getLogger(__name__).exception("desk limit watch failed ticket=%s", ticket_id)
        return {"ticket_id": ticket_id, "state": "limit_watch_error", "reason": type(exc).__name__}


def expire_stranded_children(desk_conn, *, now_iso, window_s=600.0, limit=50):
    """eCat on 4b3e40593: a re-judge / limit child whose setup never finished inside the resume window (a :drift child with
    no hard filter, a :limit child with no vet) is expired terminally, never left open blocking its mint and never run late.
    Bounded: open children whose emission was received before the window, oldest first."""
    cutoff = (_time(now_iso) - timedelta(seconds=window_s)).isoformat()
    rows = desk_conn.execute(
        "SELECT h.ticket_id FROM desk_ticket_heads h JOIN desk_candidate_emissions e ON e.opportunity_id=h.ticket_id "
        # the exact partial predicate of idx_desk_active_coin lets SQLite use that index (no DDL; eCat EXPLAIN note)
        "AND e.is_primary=1 WHERE h.state IN ('open','waiting','queued','unresolved','filled') AND h.state='open' "
        "AND e.received_at<? AND ("
        "((h.ticket_id LIKE '%:drift' OR h.ticket_id LIKE '%:noise') AND NOT EXISTS (SELECT 1 FROM desk_tickets c WHERE c.ticket_id=h.ticket_id "
        "AND c.stage='hard_filter')) OR (h.ticket_id LIKE '%:limit' AND NOT EXISTS (SELECT 1 FROM desk_tickets c "
        "WHERE c.ticket_id=h.ticket_id AND c.stage='vet'))) ORDER BY e.received_at LIMIT ?", (cutoff, limit)).fetchall()
    out = []
    for (ticket_id,) in rows:
        head = _ticket(desk_conn, ticket_id)
        store.record_stage(desk_conn, ticket_id=ticket_id, stage="expire", seat="code", verdict="SKIP",
            reason="child_unfinished", inputs={"opportunity_id": head["opportunity_id"]},
            output={"reason": "child_unfinished"}, ts=now_iso, ticket_state="expired")
        out.append({"ticket_id": ticket_id, "state": "expired", "reason": "child_unfinished"})
    return out


def resume_limit_watches(desk_conn, *, now_iso, window_s=600.0):
    """Crash-safe, like the drift re-judge: re-derive from durable fill rows each pass; done = the child has a vet stage."""
    cutoff = (_time(now_iso) - timedelta(seconds=window_s)).isoformat()
    rows = desk_conn.execute(
        "SELECT t.ticket_id FROM desk_tickets t WHERE t.stage='fill' AND t.ts>=? "
        "AND json_extract(t.output_json,'$.limit_watch')=1 AND NOT EXISTS (SELECT 1 FROM desk_tickets c "
        "WHERE c.ticket_id=t.ticket_id || ':limit' AND c.stage='vet') AND NOT EXISTS (SELECT 1 FROM "
        "desk_candidate_emissions e WHERE e.opportunity_id=t.ticket_id || ':limit' AND e.is_primary=0) ORDER BY t.ts",
        (cutoff,)).fetchall()
    out = []
    for (ticket_id,) in rows:
        got = _limit_watch(desk_conn, ticket_id, now_iso=now_iso)
        if got is not None:
            out.append(got)
    return out


DRIFT_RESUME_WINDOW_S = 600.0


def resume_drift_requeues(desk_conn, *, now_iso, window_s=DRIFT_RESUME_WINDOW_S):
    """Crash-safe re-judge (eCat RC on cd8385b1f): every pass re-derives the work from durable rows: each recent fill stage
    that recorded a drift block, whose re-judge child is missing or has no hard filter yet, is (re)done. Idempotent: the child
    candidate is deterministic, and a refused or finished one reports nothing. Bounded to fills within window_s."""
    cutoff = (_time(now_iso) - timedelta(seconds=window_s)).isoformat()
    rows = desk_conn.execute(
        "SELECT t.ticket_id, t.output_json FROM desk_tickets t WHERE t.stage='fill' AND t.ts>=? "
        "AND json_extract(t.output_json,'$.drift') IS NOT NULL AND NOT EXISTS (SELECT 1 FROM desk_tickets c "
        "WHERE c.ticket_id=t.ticket_id || ':drift' AND c.stage='hard_filter') "
        # a claim the store already refused (its emission row exists but opened no ticket) is final: not retried
        "AND NOT EXISTS (SELECT 1 FROM desk_candidate_emissions e WHERE e.opportunity_id=t.ticket_id || ':drift' "
        "AND e.is_primary=0) ORDER BY t.ts", (cutoff,)).fetchall()
    out = []
    for ticket_id, output_json in rows:
        got = _drift_requeue(desk_conn, ticket_id, json.loads(output_json)["drift"], now_iso=now_iso)
        if got is not None:
            out.append(got)
    return out


def reconcile_intents(desk_conn, ledger_conn, *, now_iso, limit=10):
    """Append fill and confirmed close truth; unknown submissions and close claims stay active."""
    now = _time(now_iso)
    results = []
    for request in store.list_intents(desk_conn, limit=limit):
        status = intents.read_status(ledger_conn, request["intent_id"])
        state = status.get("state") if status else "unresolved"
        if state not in ("filled", "refused", "expired"):
            results.append({"ticket_id": request["ticket_id"], "state": "unresolved"})
            continue
        output = {"state": state, "status": status.get("status"), "position_id": status.get("position_id"),
                  "attempt_id": status.get("attempt_id"), "resolved_at": status.get("resolved_at")}
        drift = drift_block_of(status) if state == "refused" else None
        if drift is not None:                  # the fill-drift refusal, as facts: the re-judge sheet shows exactly this
            output["drift"] = drift
        if state == "refused" and is_limit_watch_refusal(status):
            output["limit_watch"] = True       # LIMIT arm: the ticket's one 60 s watch at the sheet price (resumed below)
        elif state == "refused" and _is_chase_refusal(desk_conn, request["ticket_id"], status):
            output["limit_watch"] = True       # chase-limit B: one standing order at the verdict price (resumed below)
            output["refused_reason"] = status.get("reason")     # prereg section 4: refused_quote / chase_gap_pct
        store.record_stage(desk_conn, ticket_id=request["ticket_id"], stage="fill", seat="engine",
            verdict="ENTER" if state == "filled" else "SKIP", reason="intent_" + state,
            inputs={"intent_id": request["intent_id"]}, output=output, ts=now_iso,
            intent_id=request["intent_id"], position_id=status.get("position_id"), ticket_state=state)
        results.append({"ticket_id": request["ticket_id"], **output})
    results += resume_drift_requeues(desk_conn, now_iso=now_iso)
    results += resume_limit_watches(desk_conn, now_iso=now_iso)
    results += expire_stranded_children(desk_conn, now_iso=now_iso)
    results += record_order_ends(desk_conn, now_iso=now_iso)
    results += record_watch_ends(desk_conn, now_iso=now_iso)
    for head in store.list_filled_tickets(desk_conn, limit=limit):
        position_id = head["position_id"]
        position = ledger.get_position(ledger_conn, position_id)
        if position is None:
            results.append({"ticket_id": head["ticket_id"], "state": head["state"], "reason": "position_unknown"})
            continue
        identity = {"signal_source": "desk", "execution_mode": "real", "network_id": head["network_id"],
                    "mint": head["mint"], "desk_cohort_id": head["cohort_id"], "desk_producer": head["producer"],
                    "desk_opportunity_id": head["opportunity_id"], "desk_snapshot_hash": head["snapshot_hash"]}
        if any(position.get(key) != value for key, value in identity.items()):
            results.append({"ticket_id": head["ticket_id"], "state": head["state"], "reason": "position_identity_mismatch"})
            continue
        if type(position.get("is_open")) is not int or position["is_open"] != 0:
            continue
        try:
            closed = _time(position.get("closed_at"))
        except ValueError:
            continue
        if closed > now:
            continue
        output = {"state": "closed", "position_id": position_id, "is_open": 0,
                  "closed_at": position["closed_at"],
                  **_close_outcome(position, ledger.operator_position_fills(ledger_conn, position_id))}
        store.record_stage(desk_conn, ticket_id=head["ticket_id"], stage="close", seat="engine",
            verdict="EXIT", reason="position_closed", inputs={"position_id": position_id, "identity": identity},
            output=output, ts=now_iso, position_id=position_id, ticket_state="closed")
        results.append({"ticket_id": head["ticket_id"], **output})
    return results
