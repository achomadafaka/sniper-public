"""Desk JUDGE caller (build step 4): one model call per hard-filter-PASS ticket, recorded by step 3.

Order per ticket: durable claim -> one model call (deadline) -> freeze the single final result ->
record_decision through step 3 with the subscription billing proof. The model is injected
(`call_model(system, user_json) -> result dict`); the SDK adapter lives in the worker script.
Prompt and price preflight refusals persist before any model claim or billing handle.
Nothing here fetches data, signs, or touches the fill path. A claim whose result was never saved
is recorded as SKIP judge_lost and is never re-asked.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import zlib
import base64
import logging
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone

from sniper.persistence import db_memecoin_desk as store
from sniper.services.memecoin_desk_model_echo import echo_ok
from sniper.services import memecoin_desk as desk
from sniper.services import memecoin_desk_contract as contract

MODEL = "claude-haiku-5-5"                # the default JUDGE model: a cohort without policy.judge_model runs it (byte-identical)
GPT_MODEL = "gpt-6-luna"
JUDGE_MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", GPT_MODEL)


def judge_model(cohort, environ=None):
    """The model this cohort's JUDGE calls run (operator 2026-10-23: Opus): the kill knob MEMECOIN_DESK_JUDGE_MODEL_OVERRIDE
    when set (Claude-only rollback; recorded per call), else the cohort's frozen policy.judge_model, else MODEL.
    GPT provider/model/LOW identity is explicit and cannot cross providers through an override.
    An unknown name refuses (ValueError), never a silent default."""
    env = os.environ if environ is None else environ
    override = (env.get("MEMECOIN_DESK_JUDGE_MODEL_OVERRIDE") or "").strip()
    policy = (cohort or {}).get("policy") or {}
    frozen = policy.get("judge_model") or MODEL
    model = override or frozen
    if model not in JUDGE_MODELS:
        raise ValueError("judge_model:%s" % model)
    if GPT_MODEL in (frozen, model):
        if model != frozen:
            raise ValueError("judge_model_provider_mismatch")
        if policy.get("judge_provider") != "codex_cli" or policy.get("judge_effort") != "low":
            raise ValueError("judge_gpt_policy")
    elif "judge_provider" in policy or "judge_effort" in policy:
        raise ValueError("judge_gpt_policy")
    return model


def judge_provider(model):
    return "codex_cli" if model == GPT_MODEL else "claude_sdk" if model in JUDGE_MODELS else None


PROVIDER = "claude_sdk"
DEADLINE_S = contract.JUDGE_DEADLINE_DEFAULT_S      # default only; the live value is contract.judge_deadline_s() (MEMECOIN_DESK_JUDGE_DEADLINE_S)
_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
               "total_tokens", "reasoning_tokens")


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def _proof(ticket_id, result, response_at, model=None):
    """The caller's billing proof, exactly as the call adapter declared it. The declared basis comes
    from the adapter (the SDK adapter declares 'subscription_flat'), never from the cohort policy, and
    billed_usd/charge_id pass through untouched; step 3 compares them with the frozen policy. Missing
    identity is passed as missing, so step 3 refuses and leaves an unknown-cost marker (Codex)."""
    usage = {k: int(v) for k, v in (result.get("usage") or {}).items()
             if k in _USAGE_KEYS and type(v) in (int, float) and v >= 0}
    proof = {"ticket_id": ticket_id, "provider": judge_provider(model or result.get("model_requested") or MODEL), "model": model or result.get("model_requested") or MODEL,
             "session_id": result.get("session_id"), "result_id": result.get("result_id"),
             "response_at": response_at, "usage": usage, "is_error": bool(result.get("is_error")),
             # declared by the call adapter only; undeclared stays unknown, never a flat $0 (Gamma)
             "billing_basis": result.get("billing_basis") or "unknown"}
    if result.get("replayed_result"):
        proof["result_id"] = None                      # a replayed SDK result authorizes nothing
    if type(result.get("notional_usd")) in (int, float):
        proof["notional_api_equivalent_usd"] = float(result["notional_usd"])
    for key in ("billed_usd", "charge_id"):
        if key in result:
            proof[key] = result[key]
    return proof


def _structure(structure_fn, ticket, candidate, ts):
    """structure_fn(ticket, candidate, now_epoch) -> the structure dict. Total: an exception or an invalid block is an UNKNOWN
    block with a named reason, never a raise into the call path and never a silent omission."""
    from sniper.services import memecoin_desk_structure as M
    try:
        out = structure_fn(ticket, candidate, ts)
        return None if out is None else contract.validate_structure(out)
    except ValueError:
        reason = "structure_invalid"
    except Exception:
        reason = "structure_error"
    return {"version": M.VERSION, "tape": "unknown", "reason": reason}


_PROMPT_SEEN = {}


def _prompt_hash(desk_conn, system_prompt, cohort, at):
    """sha256 of the exact system prompt this call sent; its bytes are stored once (desk_judge_prompts). Record-only: a
    failed write is logged and the hash still returned, never a blocked call."""
    digest = hashlib.sha256(system_prompt.encode()).hexdigest()
    if digest not in _PROMPT_SEEN:
        try:
            store.record_judge_prompt(desk_conn, text=system_prompt, prompt_version=cohort["prompt_version"], seen_at=_iso(at))
            _PROMPT_SEEN[digest] = True
        except Exception:
            logging.getLogger(__name__).exception("desk judge prompt bytes write failed sha=%s", digest)
    return digest


def _drift_block(ticket):
    """The drift block of a fill-drift re-judge ticket (its candidate's drift_requeue claim, store-verified), else None."""
    claim = ((ticket or {}).get("candidate") or {}).get("drift_requeue")
    if not isinstance(claim, dict):
        return None
    try:
        return contract.validate_drift(claim.get("drift"))
    except ValueError:
        return None


def _context(context_fn, ticket, candidate, ts, *, ps_facts=None):
    """context_fn(ticket, candidate, now_epoch) -> the ctx block. Total, like _structure: an error or an invalid block is
    an all-unknown block with a named reason, never a raise and never a silent omission."""
    from sniper.services import memecoin_desk_context_facts as C
    try:
        kwargs = {"ps_facts": ps_facts} if ps_facts is not None else {}
        out = context_fn(ticket, candidate, ts, **kwargs)
        return None if out is None else C.validate(out)
    except ValueError:
        reason = "context_invalid"
    except Exception:
        reason = "context_error"
    return C.unknown_block(reason)


def _lost_flat_proof(cohort, call, *, kind="lost"):
    """The flat provenance for a LOST call (claimed, never saved: a worker killed mid-call), only when the cohort froze
    model_billing_basis subscription_flat AND the claimer was the JUDGE worker ("judge-<pid>").
    The digest identifies an assumed-policy lost call, never a fabricated provider receipt.
    Historical Claude records retain their original bytes; validated GPT records name codex_cli.
    Anything else: None (desk:unpriced, the gate holds)."""
    import hashlib
    if cohort["policy"].get("model_billing_basis") != "subscription_flat" or not re.fullmatch(r"judge-[0-9]+", str(call.get("worker"))):
        return None
    frozen_model = (cohort.get("policy") or {}).get("judge_model") or MODEL
    try:
        judge_model(cohort, environ={})
    except ValueError:
        return None
    provider = judge_provider(frozen_model)
    if provider is None:
        return None
    receipt = {"ticket_id": call["ticket_id"], "call_id": call["call_id"], "worker": call["worker"],
               "claimed_at": call["claimed_at"], "model": ((cohort.get("policy") or {}).get("judge_model") or MODEL),
               # a lost call never saved what it asked: the model is the cohort's frozen choice, ASSUMED (an override may
               # have sent another); the flat $0 does not depend on it (eCat attribution P2)
               "model_source": "cohort_policy_assumed",
               "provider": provider,
               "billing_basis": "subscription_flat", "lost": True}
    if kind != "lost":                                   # a timed-out call: no SDK receipt either (operator 10-26 cost-gate page)
        receipt = {**receipt, "lost": False, "call_error": kind}
    digest = hashlib.sha256(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return "model:" + provider + ":subscription_flat:" + digest


def _record(desk_conn, ledger_factory, call, now_fn, record_fn=None):
    """Record one claimed call from its frozen state. Never calls the model."""
    ticket = store.get_ticket(desk_conn, call["ticket_id"])
    cohort = store.get_cohort(desk_conn, ticket["cohort_id"])
    result = call.get("result")
    now_iso = _iso(now_fn())
    requested = ((result or {}).get("model_requested") or (cohort.get("policy") or {}).get("judge_model") or MODEL)
    kwargs = {"bounds": cohort["policy"]["rail_bounds"], "model": requested}
    if result is None:                                   # claimed, never saved: lost, not re-asked
        started = call["claimed_at"]
        kwargs.update(raw=None, elapsed_ms=0.0, call_started_at=started, response_at=started,   # no answer ever
                      call_error="lost", model_cost_usd=None, cost_provenance="desk:unpriced")  # absent proof is never $0
        flat = _lost_flat_proof(cohort, call)
        if flat is not None:                             # flat subscription seat: a lost call costs $0 (operator 10-23)
            kwargs.update(model_cost_usd=0, cost_provenance=flat)
    elif (result.get("call_error") in NO_CALL_SKIPS or
          requested == GPT_MODEL and result.get("call_error") in GPT_NO_CALL_ERRORS
          and result.get("no_model_call") is True):
        kwargs.update(raw=None, elapsed_ms=0.0, call_started_at=call["call_started_at"],
                      response_at=call["response_at"], call_error=result["call_error"],
                      model_cost_usd=0, cost_provenance="desk:no_model_call")
        for key in ("asof", "soft", "structure", "context", "context_error", "inline_rank", "drift", "deadline_s",
                    "sheet_sha256", "ps_facts", "safety", "safety_proof"):
            if result.get(key):
                kwargs[key] = result[key]
    else:
        response_at = call["response_at"]
        error = result.get("call_error")              # a late answer is judge_timeout in validate_verdict
        if error is None and not echo_ok(result.get("model_echo"), requested):
            # another model answered: a SKIP through the existing call-error path, never an ENTER. The proof
            # keeps MODEL (a dated echo must not break step 3's identity); the raw echo stays on the saved
            # call result (Gamma/Alpha 2026-10-21)
            error = "model_mismatch"
        kwargs.update(raw=result.get("text"), elapsed_ms=call["elapsed_ms"],
                      call_started_at=call["call_started_at"], response_at=response_at, call_error=error)
        # always the proof path: missing identity or a replay makes step 3 refuse with an unknown-cost
        # marker; there is no scalar fallback that could read as a subscription $0 (Codex/Gamma)
        flat = None
        if (error and not result.get("result_id") and not result.get("session_id")
                and (error == "timeout" or result.get("billing_basis") == "subscription_flat")):
            # the SDK call timed out before any receipt (2026-10-26 13:49Z, Opus 12 s deadline): on the flat seat it
            # cost $0, proven like a lost call; otherwise the unknown-cost marker holds the cohort as before
            flat = _lost_flat_proof(cohort, call, kind=error)
        if flat is not None:
            kwargs.update(model_cost_usd=0, cost_provenance=flat)
        else:
            kwargs["model_cost_receipt"] = _proof(ticket["ticket_id"], result, response_at, model=requested)
        if result.get("asof"):
            kwargs["asof"] = result["asof"]
        if result.get("soft"):
            kwargs["soft"] = result["soft"]
        if result.get("structure"):
            kwargs["structure"] = result["structure"]
        if result.get("inline_rank"):
            kwargs["inline_rank"] = result["inline_rank"]
        if result.get("sheet_sha256"):                     # the per-look watch log binds the sheet the model saw
            kwargs["sheet_sha256"] = result["sheet_sha256"]
        if result.get("context"):
            kwargs["context"] = result["context"]
        if result.get("drift"):
            kwargs["drift"] = result["drift"]
        for key in ("ps_facts", "safety", "safety_proof"):
            if result.get(key) is not None:
                kwargs[key] = result[key]
        # the deadline THIS call was made under; a result saved before the stamp existed was made under the old 5 s
        kwargs["deadline_s"] = result.get("deadline_s", contract.LEGACY_JUDGE_DEADLINE_S)
    raw = kwargs.pop("raw")       # the raw verdict, unchanged: step 3 validates timings against the frozen
                                  # cohort caps and derives the ENTER expiry from response_at + enter_ttl_s
    fn = record_fn or desk.record_decision
    # ledger_factory() is a context manager: a short-lived writer, never held over a model call
    with ledger_factory() as ledger_conn:
        out = fn(desk_conn, ledger_conn, ticket["ticket_id"], raw, now_iso=max(now_iso, kwargs["response_at"]),
                 **kwargs)
    if result is None:                                   # close the lost claim too: no claimed row outlives its vet
        _close_lost_claim(desk_conn, call)
    return out


def _close_lost_claim(desk_conn, call):
    store.save_judge_response(desk_conn, call_id=call["call_id"], call_started_at=call["claimed_at"],
                              response_at=call["claimed_at"], elapsed_ms=0.0, result={"call_error": "lost"})


ADMIT_DOMAIN = "desk-judge-admit-v1|"


def hash_unit(opportunity_id):
    """Outcome-free uniform draw in [0, 1): sha256("desk-judge-admit-v1|" + opportunity_id).
    The admission sample cannot depend on arrival order or on anything the market did (Gamma)."""
    return int(hashlib.sha256((ADMIT_DOMAIN + opportunity_id).encode()).hexdigest()[:16], 16) / float(1 << 64)


def _p_admit(cohort):
    p = cohort["policy"].get("judge_p_admit")
    if type(p) not in (int, float) or not 0 < p <= 1:
        raise ValueError("judge_p_admit missing from the frozen cohort policy")   # frozen per cohort
    return float(p)


TAPE_COMMIT_GRACE_S = 2.0      # a claim younger than this (from emission) re-reads a not-yet-committed trigger swap
GPT_NO_CALL_ERRORS = ("output_budget_unsupported", "codex_busy", "cli_missing", "no_isolation", "no_codex_login")
NO_CALL_SKIPS = ("answer_budget_mismatch", "budget_exhausted", "budget_skipped", "ranker_cut", "ranker_missing", "stale_snapshot", "signal_aged", "band_below", "band_curve_complete", "band_unknown", "moved_since_emission", "seat_cut")
# The U selection rule, recorded on every SKIP it writes: shortlisted judged, closed-unshortlisted
# ranker_cut, still pending MISSING_AFTER_S after its hard-filter PASS ranker_missing (a ranker
# coverage hole: down, or the emission aged past MAX_LIFE before it ran; never a cut).
SELECT_VERSION = "desk-judge-select-v1"
# Trigger selection (operator 2026-10-22: the ranker triggers on the event, not on a 60 s cron): a cohort frozen under this
# rule never waits for the ranker. At its claim the ticket is scored inline from the as-of snapshot (pure ranker functions);
# in the market-cap band it is judged; below it, off the curve, or on an unreadable / anomalous cap it is a no-call SKIP
# (band_below, band_curve_complete, band_unknown). The 60 s ranker keeps running as a study recorder.
TRIGGER_SELECT_VERSION = "desk-judge-select-v2"     # pre-registration amendment 11
# Desk intake (operator 2026-10-27, Alpha): candidates come from the intake feeds, not the ranker; every hard-filter PASS of
# an intake cohort is eligible on arrival (the cohort's frozen judge_p_admit still samples it). Graduated PumpSwap coins only.
INTAKE_SELECT_VERSION = "intake-v1"
# Gamma prereg Amendment 2 (19bdbce4a): seats inside an intake cohort go by an OUTCOME-BLIND order frozen in the
# policy: the first hard-filter PASS as-of (a0), oldest first, then ticket id. Over the cohort's own judge_calls_per_hour
# a ticket stays unclaimed and re-competes; past judge_defer_max_s from emission it is seat_cut (final, never judged).
INTAKE_ORDER = "a0_oldest_first_ticket_id"


def _intake_seats(desk_conn, picks, now):
    """(kept [(ticket, 'shortlisted')], seat_cut {ticket_id: record}) for the intake cohorts' shortlisted picks."""
    keep, cut = [], {}
    by_cohort = {}
    for t, _ in picks:
        by_cohort.setdefault(t["cohort_id"], []).append(t)
    for cohort_id, tickets in by_cohort.items():
        policy = store.get_cohort(desk_conn, cohort_id)["policy"]
        cap, order = policy.get("judge_calls_per_hour"), policy.get("judge_order")
        if type(cap) is not int or cap < 1 or order != INTAKE_ORDER:
            raise ValueError("intake cohort %s needs a frozen judge_calls_per_hour and judge_order %r" % (cohort_id, INTAKE_ORDER))
        ceiling = contract.check_defer_max_s(policy["judge_defer_max_s"])
        a0 = lambda t: min(s["ts"] for s in t["stages"] if s["stage"] == "hard_filter" and s["verdict"] == "PASS")   # noqa: E731
        live = []
        for t in sorted(tickets, key=lambda t: (a0(t), t["ticket_id"])):
            if _age_s(t, now) > ceiling:
                cut[t["ticket_id"]] = {"age_s": _age_s(t, now), "defer_max_s": ceiling, "seat_cap_per_hour": cap,
                                       "seat_cut_reason": "age_out"}
            else:
                live.append(t)
        left = max(0, cap - store.judge_calls_since(desk_conn, since_iso=_iso(now - timedelta(hours=1)), cohort_id=cohort_id))
        keep += [(t, "shortlisted") for t in live[:left]]          # the rest stay unclaimed: they re-compete next pass
    return keep, cut


def intake_mint_of(ticket, verdicts=("PASS",)):
    """The assembler's mint read for an intake ticket, from its OWN hard-filter PASS stage (the evidence the filter
    decided on): {"supply_atomic", "decimals", "authority_mint", "known_at" (host epoch), "context_slot"}, or None.
    Bound to the ticket: the stored evidence's identity and snapshot hash must match the candidate, and token_safety must
    have passed. ps_asof re-checks shape, clocks and units (Delta)."""
    import math
    cand = (ticket or {}).get("candidate") or {}
    stage = next((s for s in (ticket or {}).get("stages") or [] if s.get("stage") == "hard_filter"
                  and s.get("verdict") in verdicts), None)
    evidence = ((stage or {}).get("inputs") or {}).get("evidence")
    if not isinstance(evidence, dict) or "intake" not in cand:
        return None
    if any(evidence.get(k) != cand.get(k) for k in ("network_id", "mint", "pool")) or \
            evidence.get("snapshot_hash") != (cand.get("snapshot") or {}).get("hash"):
        return None
    safety = (evidence.get("checks") or {}).get("token_safety") or {}
    if safety.get("status") != "pass":
        return None
    try:
        known = datetime.fromisoformat(evidence["known_at"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(known):
        return None
    return {"supply_atomic": safety.get("supply_atomic"), "decimals": safety.get("decimals"),
            "authority_mint": safety.get("authority_mint"), "known_at": known,
            "context_slot": safety.get("mint_context_slot")}
SELECT_VERSIONS = (SELECT_VERSION, TRIGGER_SELECT_VERSION, INTAKE_SELECT_VERSION)
SELECT_SCAN = 200          # waiting tickets compared per pass when the hourly cap binds


def missing_after_s():
    from sniper.services.memecoin_desk_ranker import MAX_LIFE_S
    return MAX_LIFE_S + 120.0


class RankerGate:
    """shortlist_fn for cohort U (pre-registration 2026-10-21-desk-u-ranker §1-§2: the JUDGE sees only
    the runners shortlist). Reads ranker/candidates.db read-only, one connection per call.
    decided(since_epoch): opportunity ids the ranker shortlisted or closed since then, the set run_once
    claims from, so pending tickets are never scanned and cannot fill a pass (Codex/Gamma, 5bb94b169).
    states(ids): {id: 'shortlisted' | 'cut' | 'pending'}; pending includes a ranker that has not seen
    it or is down: the ticket waits, unclaimed, and is never judged on a missing ranker."""

    def __init__(self, desk_dir):
        self.path = Path(desk_dir) / "ranker" / "candidates.db"

    def _read(self, sql, args, path=None):
        import sqlite3
        path = path or self.path
        if not path.exists():
            return []
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
        except sqlite3.Error:
            return []
        try:
            return conn.execute(sql, args).fetchall()
        except sqlite3.Error:
            return []                                # unreadable: everything waits
        finally:
            conn.close()

    def decided(self, since_epoch):
        return {r[0] for r in self._read(
            "SELECT opportunity_id FROM candidates WHERE first_rank_at >= ? AND "
            "(first_shortlisted_at IS NOT NULL OR closed_at IS NOT NULL)", (since_epoch,))}

    def states(self, opportunity_ids):
        out = dict.fromkeys(opportunity_ids, "pending")
        if out:
            for oid, short, closed in self._read(
                    "SELECT opportunity_id, first_shortlisted_at, closed_at FROM candidates "
                    "WHERE opportunity_id IN (SELECT value FROM json_each(?))", (json.dumps(sorted(out)),)):
                out[oid] = "shortlisted" if short is not None else ("cut" if closed is not None else "pending")
        return out

    def scores(self, opportunity_ids):
        """{id: score} the ranker gave each at its shortlist cycle (ranker/<UTC day>.db rank_rows). An id
        with no shortlist, no row or an unreadable db is absent: unknown, never a zero."""
        at = dict(self._read("SELECT opportunity_id, first_shortlisted_at FROM candidates WHERE first_shortlisted_at "
                             "IS NOT NULL AND opportunity_id IN (SELECT value FROM json_each(?))",
                             (json.dumps(sorted(set(opportunity_ids))),)))
        days = {}
        for oid, ts in at.items():
            days.setdefault(datetime.fromtimestamp(ts, timezone.utc).date().isoformat(), []).append([oid, ts])
        out = {}
        for day, pairs in days.items():
            out.update({oid: float(score) for oid, score in self._read(
                "SELECT r.opportunity_id, r.score FROM rank_rows r JOIN json_each(?) j "
                "ON r.opportunity_id = json_extract(j.value, '$[0]') AND r.rank_at = json_extract(j.value, '$[1]') "
                "WHERE r.score IS NOT NULL", (json.dumps(pairs),), self.path.parent / ("%s.db" % day))})
        return out


# _TRIGGER and _SCORE_CACHE are process-wide module state: fine in the single-threaded JUDGE worker; a second thread must not share them.
_TRIGGER = {}


def _is_trigger(desk_conn, ticket):
    """True when the ticket's cohort froze the trigger selection rule (producer U only). Cohort policy is immutable: cached."""
    cid = ticket["cohort_id"]
    if cid not in _TRIGGER:
        _TRIGGER[cid] = (ticket["producer"] == "U"
                         and store.get_cohort(desk_conn, cid)["policy"].get("select_version") == TRIGGER_SELECT_VERSION)
    return _TRIGGER[cid]


_BAND = {}


def _mcap(candidate):
    row = (((candidate or {}).get("snapshot") or {}).get("fields") or {}).get("mcap_usd") or {}
    v = row.get("value") if row.get("completeness") == "complete" else None
    return v if type(v) in (int, float) and v > 0 else None


def _moved_since_emission(desk_conn, ticket, asof_candidate):
    """{emission/claim mcap, move} when the claim-time cap moved more than the cohort's frozen
    hard_filter.judge_moved_max_pct from the emission's, either way; else None (absent rule or unknown cap)."""
    limit = (store.get_cohort(desk_conn, ticket["cohort_id"])["policy"].get("hard_filter") or {}).get("judge_moved_max_pct")
    then, now_mcap = _mcap(ticket["candidate"]), _mcap(asof_candidate)
    if type(limit) not in (int, float) or then is None or now_mcap is None:
        return None
    move = (now_mcap / then - 1) * 100
    if abs(move) <= limit:
        return None
    return {"emission_mcap_usd": then, "claim_mcap_usd": now_mcap, "moved_pct": round(move, 2), "limit_pct": limit}


def _cohort_band(desk_conn, ticket):
    """The ticket cohort's frozen CLAIM band: hard_filter policy["mcap_band"] (default 30k-100k) with the floor lowered
    by mcap_band_claim_margin_pct when frozen. The emission-time crossing stays the admission proof. Immutable: cached."""
    cid = ticket["cohort_id"]
    if cid not in _BAND:
        from sniper.services.memecoin_desk_contract import claim_band
        _BAND[cid] = claim_band(store.get_cohort(desk_conn, cid)["policy"].get("hard_filter"))
    return _BAND[cid]


_SCORE_CACHE, _SCORE_TTL_S = {}, 5.0


def _inline_scores(rank_asof_fn, desk_conn=None):
    """{ticket_id: (competes, score)} for trigger tickets, for the cap tiebreak only: ONE as-of read per ticket gives its band
    and its score, cached _SCORE_TTL_S so a pass that repeats while the cap binds does not re-read the tape per waiting ticket.
    competes is False only for a ticket whose snapshot READ and is outside the band: it will be a no-call at its claim
    (band_below / band_curve_complete / band_unknown) and so must never take a slot from one that will spend a call. A failed
    scratch read (StaleSnapshot) competes with an unknown score: the claim-time read decides, and it must not skip the line.
    score None = unknown (ranks last), never zero. The recorded score is the claim-time one; this one is scratch."""
    def scores(tickets, now):
        from sniper.services import memecoin_desk_ranker as R
        from sniper.services.memecoin_desk_asof import StaleSnapshot
        at, out = now.timestamp(), {}
        for key in [k for k, (t, _c, _s) in _SCORE_CACHE.items() if at - t > _SCORE_TTL_S or t > at]:
            del _SCORE_CACHE[key]
        for t in tickets:
            hit = _SCORE_CACHE.get(t["ticket_id"])
            if hit is None:
                try:
                    cand, meta = rank_asof_fn(t["candidate"], at)
                    view = R.inline_view(cand, meta.get("rank"), **(
                        {"band": _cohort_band(desk_conn, t)} if desk_conn is not None else {}))
                    competes, value = view["band"] == "inside", view["score"]
                except (StaleSnapshot, KeyError, TypeError, ValueError):
                    competes, value = True, None             # a failed scratch read still takes its place in line (score unknown, ranks last)
                hit = _SCORE_CACHE[t["ticket_id"]] = (at, competes, value)
            out[t["ticket_id"]] = (hit[1], hit[2])
        return out
    return scores


def _pass_epoch(ticket):
    """Epoch of the ticket's hard-filter PASS (the moment it became eligible for the JUDGE)."""
    return min(datetime.fromisoformat(s["ts"]).timestamp() for s in ticket["stages"]
               if s["stage"] == "hard_filter" and s["verdict"] == "PASS")


def _age_s(ticket, at):
    """Seconds from the ticket's emission to `at` (Gamma: lets reads stratify or censor late calls)."""
    return round(at.timestamp() - datetime.fromisoformat(ticket["candidate"]["emitted_at"]).timestamp(), 3)


def _watch_looks(desk_conn, *, now, calls_per_hour, fresh, limit):
    """watch-list-v1 seats (Alpha 10-26): fresh candidates first, then looks, oldest first, inside BOTH the hourly
    calls left after the fresh picks AND the cohort's frozen relook_seats_per_h. Returns (picks, skipped records);
    a skipped look stays open and competes again next pass (its watch life still runs)."""
    looks = store.list_awaiting_looks(desk_conn, limit=limit)
    if not looks:
        return [], []
    from sniper.services.memecoin_desk_universe import WATCH_TRIGGER_SCORE
    looks = sorted(looks, key=lambda t: -WATCH_TRIGGER_SCORE.get(t["candidate"]["watch_look"]["trigger"], 0))   # change score
    hour_ago = _iso(now - timedelta(hours=1))
    left = max(0, calls_per_hour - store.judge_calls_since(desk_conn, since_iso=hour_ago) - fresh)
    caps = {}
    picks, skipped = [], []
    spent = store.look_calls_since(desk_conn, since_iso=hour_ago)
    for t in looks:
        rule = store.get_cohort(desk_conn, t["cohort_id"])["policy"].get("watch_list") or {}
        cap = caps.setdefault(t["cohort_id"], rule.get("relook_seats_per_h", 0))
        if left > 0 and spent < cap:
            picks.append((t, "shortlisted"))
            left, spent = left - 1, spent + 1
        else:
            skipped.append({"ticket_id": t["ticket_id"], "state": "look_skipped_seat",
                            "reason": "relook_cap" if spent >= cap else "hourly_cap"})
    return picks, skipped


def _cap_tiebreak(desk_conn, gate, picks, *, now, calls_per_hour, limit, inline_scores=None):
    """Operator 8b4030a4: the hourly cap is the only JUDGE limit. While calls are left, shortlisted tickets
    are judged on arrival in pass order. When more wait than calls are left, the highest ranker score is
    judged and the rest stay unclaimed (deferred) to compete again in later passes. Unknown score ranks last;
    ties fall back to the oldest PASS. No wait and no call is added.
    A cohort with a frozen judge_defer_max_s: EVERY shortlisted ticket older than it from emission is a no-call
    signal_aged, whether or not the cap binds (Gamma: a worker outage or backlog must not judge a stale coin).
    A cohort frozen before the knob keeps today's rule: only a deferred ticket past MAX_LIFE_S, budget_exhausted.
    Returns ([(ticket, rank_state)], {ticket_id: budget_exhausted record}, {ticket_id: signal_aged record})."""
    from sniper.services.memecoin_desk_ranker import MAX_LIFE_S
    # intake cohorts never meet the ranker scores: their own frozen order, cap and age-out (Gamma 19bdbce4a, Zeta)
    intake = [p for p in picks if p[1] == "shortlisted" and "intake" in (p[0].get("candidate") or {})]
    if intake:
        picks = [p for p in picks if p not in intake]
        intake_keep, intake_cut = _intake_seats(desk_conn, intake, now)
        out, dropped, aged = _cap_tiebreak(desk_conn, gate, picks, now=now, calls_per_hour=calls_per_hour, limit=limit,
                                           inline_scores=inline_scores) if picks else ([], {}, {})
        aged = {**aged, **intake_cut}
        return out + intake_keep[:limit] + [p for p in intake if p[0]["ticket_id"] in intake_cut], dropped, aged
    left = max(0, calls_per_hour - store.judge_calls_since(desk_conn, since_iso=_iso(now - timedelta(hours=1))))
    ceiling = {}
    for t, state in picks:
        if state == "shortlisted" and t["cohort_id"] not in ceiling:
            policy = store.get_cohort(desk_conn, t["cohort_id"])["policy"]
            ceiling[t["cohort_id"]] = (contract.check_defer_max_s(policy["judge_defer_max_s"])
                                       if "judge_defer_max_s" in policy else None)   # a bad frozen value stops the worker
    aged = {t["ticket_id"]: {"age_s": _age_s(t, now), "defer_max_s": ceiling[t["cohort_id"]], "calls_left": left}
            for t, state in picks if state == "shortlisted" and ceiling[t["cohort_id"]] is not None
            and _age_s(t, now) > ceiling[t["cohort_id"]]}
    old = [p for p in picks if p[0]["ticket_id"] in aged]
    picks = [p for p in picks if p[0]["ticket_id"] not in aged]
    live = [p for p in picks if p[1] == "shortlisted"]
    if len(live) <= left:
        return picks[:limit] + old, {}, aged
    trig = {t["ticket_id"] for t, _ in live if _is_trigger(desk_conn, t)}
    info = inline_scores([t for t, _ in live if t["ticket_id"] in trig], now) if trig and inline_scores is not None else {}
    # a trigger ticket whose snapshot is not inside the band (or not readable) is a no-call at its claim: it spends no call, so it
    # does not compete for the `left` slots and is processed alongside the winners
    free = [p for p in live if p[0]["ticket_id"] in trig and not info.get(p[0]["ticket_id"], (False, None))[0]]
    live = [p for p in live if p not in free]
    legacy = [t["opportunity_id"] for t, _ in live if t["ticket_id"] not in trig]
    scores = gate.scores(legacy) if legacy and hasattr(gate, "scores") else {}
    scores.update({t["opportunity_id"]: info[t["ticket_id"]][1] for t, _ in live
                   if t["ticket_id"] in info and info[t["ticket_id"]][1] is not None})
    if len(live) <= left:
        return (picks if not free else [p for p in picks if p not in free])[:limit] + free[:limit] + old, {}, aged
    order = sorted(range(len(live)), key=lambda i: (live[i][0]["opportunity_id"] not in scores,
                                                    -scores.get(live[i][0]["opportunity_id"], 0.0), i))
    keep, rest = [live[i] for i in order[:left]], [live[i] for i in order[left:]]
    drop = [p for p in rest if ceiling[p[0]["cohort_id"]] is None and _age_s(p[0], now) > MAX_LIFE_S]
    cut = scores[keep[-1][0]["opportunity_id"]] if keep and keep[-1][0]["opportunity_id"] in scores else None
    # the scratch tiebreak score is labelled as such for a trigger ticket (its recorded score is the claim-time one)
    dropped = {t["ticket_id"]: {("tiebreak_score" if t["ticket_id"] in trig else "score"): scores.get(t["opportunity_id"]),
                                "calls_left": left, "waiting": len(live), "lowest_kept_score": cut,
                                "expired_after_s": MAX_LIFE_S} for t, _ in drop}
    return (keep[:limit] + free[:limit] + drop + old + [p for p in picks if p[1] != "shortlisted"][:limit]), dropped, aged


def ranker_gate(desk_dir):
    return RankerGate(desk_dir)


def _select(desk_conn, gate, *, limit, now):
    """[(ticket, rank_state)], at most `limit`, oldest hard-filter PASS first, from three bounded
    queries: non-U tickets; U tickets the ranker decided inside the window; U tickets older than
    missing_after_s() (looked up one by one: shortlisted late is still judged, pending is missing)."""
    window_s = missing_after_s()
    old_iso = _iso(now - timedelta(seconds=window_s))
    since = now.timestamp() - window_s - 60.0
    picks = [(t, "shortlisted") for t in store.list_awaiting_judge_where(desk_conn, limit=limit, not_producer="U")]
    # a trigger cohort's U tickets are all eligible on arrival: the band and the score come from the claim-time snapshot
    picks += [(t, "shortlisted") for t in store.list_awaiting_judge_where(desk_conn, limit=limit, producer="U",
                                                                         select_version=TRIGGER_SELECT_VERSION)
              if "watch_look" not in (t.get("candidate") or {})]          # looks are seated by _watch_looks
    # intake: eligible on arrival; the cohort's own seat cap is decided per CLAIM in run_once (seat_cut, Gamma prereg)
    picks += [(t, "shortlisted") for t in store.list_awaiting_judge_where(desk_conn, limit=limit, producer="U",
                                                                         select_version=INTAKE_SELECT_VERSION)]
    decided = gate.decided(since)
    recent = store.list_awaiting_judge_where(desk_conn, limit=limit, producer="U", opportunity_ids=decided,
                                             not_select_version=TRIGGER_SELECT_VERSION)
    old = store.list_awaiting_judge_where(desk_conn, limit=limit, producer="U", passed_before=old_iso,
                                          not_select_version=TRIGGER_SELECT_VERSION)
    states = gate.states([t["opportunity_id"] for t in recent + old])
    old_ids, seen = {t["ticket_id"] for t in old}, {t["ticket_id"] for t, _ in picks}      # a trigger ticket is picked once, by its own rule
    for t in recent + old:
        if t["ticket_id"] in seen or "watch_look" in (t.get("candidate") or {}) or "intake" in (t.get("candidate") or {}):
            continue                     # a watch-list look is never ranked again: _watch_looks seats it; intake: no ranker
        seen.add(t["ticket_id"])
        state = states[t["opportunity_id"]]
        if state == "pending":
            if t["ticket_id"] not in old_ids:
                continue
            state = "missing"
        picks.append((t, state))
    pass_ts = lambda t: min(s["ts"] for s in t["stages"] if s["stage"] == "hard_filter" and s["verdict"] == "PASS")
    picks = sorted(picks, key=lambda p: (pass_ts(p[0]), p[0]["ticket_id"]))
    from sniper.settings import memecoin as mc
    if mc.MEMECOIN_DESK_JUDGE_FRESHEST_FIRST:    # operator 2026-10-23: the newest PASS first; a stale one ages out (signal_aged)
        picks.reverse()
    return picks


def _skip_context(desk_conn, ticket, now, *, asof, asof_fn, soft_fn, structure_fn):
    """Decision-moment context for a ticket that gets NO model call (amendment 10): the as-of snapshot (when the tape allows one), the
    chart and the cluster/holder facts, all as of `now`, so a JUDGE-independent reference outcome can be computed from the tape later.
    Best effort and total: a failing component is absent and the row says so ({"context_error": exception type}), never an exception;
    reference-outcome code censors a row that carries it instead of reading it as 'no context'."""
    out = {}
    try:
        if asof is None and asof_fn is not None:
            from sniper.services.memecoin_desk_asof import StaleSnapshot
            try:
                asof = asof_fn(ticket["candidate"], now.timestamp())
            except StaleSnapshot:
                asof = None
        if asof is not None:
            out["asof"] = {"candidate": asof[0], "meta": asof[1]}
        if soft_fn is not None:
            out["soft"] = soft_fn(store.get_ticket(desk_conn, ticket["ticket_id"]))
        if structure_fn is not None:
            block = _structure(structure_fn, ticket, asof[0] if asof else ticket["candidate"], now.timestamp())
            if block is not None:
                out["structure"] = block
    except Exception as exc:
        logging.getLogger(__name__).exception("skip context failed ticket=%s", ticket.get("ticket_id"))
        out["context_error"] = type(exc).__name__
    return out


def _preflight_skip(desk_conn, ticket, reason, now, *, asof=None, extra=None):
    inputs = {"opportunity_id": ticket["opportunity_id"], "snapshot_hash": ticket["snapshot_hash"],
              "preflight_reason": reason, "cost_provenance": "desk:no_model_call",
              "age_at_call_s": _age_s(ticket, now), **(extra or {})}
    if asof is not None:
        inputs["asof"] = {"candidate": asof[0], "meta": asof[1]}
    if store.record_preflight_skip(desk_conn, ticket_id=ticket["ticket_id"], reason=reason,
                                   inputs=inputs, ts=_iso(now)):
        return {"ticket_id": ticket["ticket_id"], **contract._skip(reason)}
    return None


def run_once(desk_conn, ledger_factory, *, call_model, system_prompt, worker, now_fn, calls_per_hour,
             limit=5, record_fn=None, admit_fn=None, shortlist_fn=None, asof_fn=None, soft_fn=None, structure_fn=None, context_fn=None,
             expected_prompt_version=None, rank_asof_fn=None, stop_fn=None, ps_inputs_fn=None, release_sha=None):
    """Judge up to `limit` waiting tickets. Returns the recorded results. With `shortlist_fn`
    (ranker_gate), a producer-U ticket is judged only once shortlisted; a cut one is a no-call SKIP
    ('ranker_cut'); a pending one is left unclaimed for a later pass, until its PASS is older than the
    ranker could ever cover, then a no-call SKIP 'ranker_missing'. With `asof_fn(candidate, now_epoch) ->
    (candidate, meta)` (memecoin_desk_asof.refresh) the JUDGE sheet and the recorded vet inputs carry ONE
    as-of-decision snapshot; a stale or gapped tape is a no-call SKIP 'stale_snapshot', never an emission-facts
    fallback. With `soft_fn(ticket) -> soft_facts` (hard filter hf2) the sheet and the recorded vet inputs carry the
    cluster / holders facts as of the call. A worker supplies expected_prompt_version: incompatible
    queued tickets are retired without a model claim rather than blocking compatible cohorts."""
    if release_sha is not None and (not isinstance(release_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", release_sha)):
        raise ValueError("release_sha")
    if expected_prompt_version is not None and (not isinstance(expected_prompt_version, str)
                                                or not expected_prompt_version.strip()):
        raise ValueError("expected_prompt_version")
    out, dropped, aged = [], {}, {}
    from sniper.services import memecoin_desk as _desk
    _desk.rejudge_noise_watches(desk_conn, now_iso=now_fn().isoformat())    # P2 re-judges join this pass's queue
    _desk.relook_watches(desk_conn, now_iso=now_fn().isoformat())           # watch-list-v1 cadence looks
    if shortlist_fn is None:
        selected = [(t, "shortlisted") for t in store.list_awaiting_judge(desk_conn, limit=limit)]
    else:
        now0 = now_fn()
        selected, dropped, aged = _cap_tiebreak(desk_conn, shortlist_fn, _select(desk_conn, shortlist_fn, limit=SELECT_SCAN,
                                          now=now0), now=now0, calls_per_hour=calls_per_hour, limit=limit,
                                          inline_scores=_inline_scores(rank_asof_fn, desk_conn) if rank_asof_fn is not None else None)
    # watch-list looks are seated last, inside the hourly calls left and the frozen re-look cap (never by the ranker)
    selected = [(t, st) for t, st in selected if "watch_look" not in (t.get("candidate") or {})]
    looks, skipped_looks = _watch_looks(desk_conn, now=now_fn(), calls_per_hour=calls_per_hour, limit=limit,
                                        fresh=sum(1 for _, st in selected if st == "shortlisted"))
    selected += looks
    out += skipped_looks
    for ticket, rank_state in selected:
        cohort = store.get_cohort(desk_conn, ticket["cohort_id"])
        now = now_fn()
        if expected_prompt_version is not None and cohort["prompt_version"] != expected_prompt_version:
            refusal = _preflight_skip(desk_conn, ticket, "prompt_version_mismatch", now,
                extra={"expected_prompt_version": expected_prompt_version,
                       "cohort_prompt_version": cohort["prompt_version"]})
            if refusal is not None:
                out.append(refusal)
            continue
        new_ps = contract.validate_ps_inputs(cohort["policy"], cohort["prompt_version"])
        identity = {}
        if cohort["prompt_version"] in ("desk-judge-v28", "desk-judge-v29"):
            system_digest = hashlib.sha256(system_prompt.encode()).hexdigest()
            if system_digest != cohort["policy"]["judge_playbook_sha256"]:
                raise ValueError("judge_playbook_mismatch")
            identity = {"prompt_version": cohort["prompt_version"], "system_sha256": system_digest,
                        "policy_hash": contract.cohort_policy_hash(cohort["policy"], producer=cohort["producer"],
                                                                   prompt_version=cohort["prompt_version"])}
        if new_ps:
            from sniper.services.memecoin_desk_paper import cohort_refusal
            reason = cohort_refusal(desk_conn, ticket["cohort_id"], now_iso=_iso(now))
            if reason is None and ps_inputs_fn is None:
                reason = "ps_inputs_required"
            if reason is not None:
                refusal = _preflight_skip(desk_conn, ticket, reason, now)
                if refusal is not None:
                    out.append(refusal)
                continue
        p_admit = _p_admit(cohort)                                          # refuse before claiming
        if shortlist_fn is not None and ticket["producer"] == "U" \
                and cohort["policy"].get("select_version") not in SELECT_VERSIONS:
            # a cohort frozen under another selection rule (or none) is never judged under this one
            raise ValueError("cohort select_version %r is not one of %s"
                             % (cohort["policy"].get("select_version"), SELECT_VERSIONS))
        trigger = ticket["producer"] == "U" and cohort["policy"].get("select_version") == TRIGGER_SELECT_VERSION
        if trigger and rank_asof_fn is None:
            raise ValueError("a trigger cohort needs rank_asof_fn (the claim-time as-of snapshot with rank inputs)")
        view = None
        call_id = "judge:" + uuid.uuid4().hex
        asof = None
        asof_failure = None
        # an aged-out intake ticket is seat_cut BEFORE any as-of / pricing dependency (Zeta 3b8ed3b74): its final row
        # must be the typed seat_cut of the a0 denominator, never a preflight reason from a stale tape or mint read
        if ticket["candidate"].get("venue") == "pumpswap" and not ("intake" in ticket["candidate"]
                                                                    and ticket["ticket_id"] in aged):
            reason = "ps_asof_required"
            if asof_fn is not None:
                from sniper.services.memecoin_desk_asof import StaleSnapshot
                try:
                    intake_mint = intake_mint_of(ticket) if "intake" in ticket["candidate"] else None
                    args = {"integrity": True} if new_ps else {}
                    if "intake" in ticket["candidate"]:
                        args["intake_mint"] = intake_mint
                    asof = asof_fn(ticket["candidate"], now.timestamp(), **args)
                    reason = desk.pumpswap_unpriced_reason(asof[0])
                except StaleSnapshot as exc:
                    reason = exc.reason
            if reason is not None:
                refusal = _preflight_skip(desk_conn, ticket, reason, now, asof=asof)
                if refusal is not None:
                    out.append(refusal)
                continue
        admitted = (admit_fn(ticket) if admit_fn is not None
                    else hash_unit(ticket["opportunity_id"]) < p_admit)
        if asof is None and rank_state == "shortlisted" and admitted and ticket["ticket_id"] not in dropped \
                and ticket["ticket_id"] not in aged:
            if trigger or asof_fn is not None:
                from sniper.services.memecoin_desk_asof import StaleSnapshot
                try:
                    asof = (rank_asof_fn if trigger else asof_fn)(ticket["candidate"], now.timestamp())
                except StaleSnapshot as exc:
                    asof_failure = exc.reason
            if (asof is None and asof_failure == "no_emission_state"
                    and _age_s(ticket, now) < TAPE_COMMIT_GRACE_S):
                # TAPE COMMIT RACE (2026-10-23, 17 of 17 stale_snapshot calls): the universe worker commits the tape in ~1 s
                # batches and a fast claim (0.41-0.85 s after emission) read before the trigger swap landed. Leave it
                # UNCLAIMED: the next pass (worker interval 0.5 s) reads again; past the grace the failure stands as today.
                continue
            if trigger and asof is not None:
                from sniper.services import memecoin_desk_ranker as R
                view = R.inline_view(asof[0], asof[1].get("rank"),            # the band and the score from ONE snapshot
                                     band=_cohort_band(desk_conn, ticket))
            if (asof is not None or (asof_fn is None and not trigger)) and (view is None or view["band"] == "inside") and desk.priced_verdict(
                    asof[0] if asof else ticket["candidate"], clip_usd=25,
                    basis="asof" if asof else "emission") is None:
                refusal = _preflight_skip(desk_conn, ticket, "no_verdict_price", now, asof=asof,
                                          extra=_skip_context(desk_conn, ticket, now, asof=asof, asof_fn=None, soft_fn=soft_fn,
                                                              structure_fn=structure_fn))
                if refusal is not None:
                    out.append(refusal)
                continue
        if (trigger and rank_state == "shortlisted" and admitted and asof is not None and view["band"] == "inside"
                and ticket["ticket_id"] not in dropped and ticket["ticket_id"] not in aged
                and store.judge_calls_since(desk_conn, since_iso=_iso(now - timedelta(hours=1))) >= calls_per_hour):
            # This ticket reads INSIDE at its claim and will spend a call, but the hour has none left (it was free at the scratch read,
            # so it never competed for a slot). It stays UNCLAIMED and deferred: it re-competes next pass and ages out as signal_aged at
            # its own ceiling. Claiming it now would make it terminal budget_exhausted although a slot frees within the hour (Codex P2).
            # The post-claim hourly guard below stays as the backstop for a second worker racing this check.
            continue
        if stop_fn is not None and stop_fn():           # SIGTERM drain: claim nothing new; the in-flight call finished
            break
        seat_cap = cohort["policy"].get("judge_calls_per_hour") if "intake" in ticket["candidate"] else None
        if (type(seat_cap) is int and ticket["ticket_id"] not in aged
                and store.judge_calls_since(desk_conn, since_iso=_iso(now - timedelta(hours=1)),
                                            cohort_id=ticket["cohort_id"]) >= seat_cap):
            # the intake cohort's own frozen seat cap, re-counted BEFORE each claim (claims of this pass are already
            # committed and counted): a full hour leaves the ticket UNCLAIMED, it re-competes (Gamma 19bdbce4a, Zeta)
            continue
        ps_inputs = None
        if (new_ps and rank_state == "shortlisted" and admitted and ticket["ticket_id"] not in dropped
                and ticket["ticket_id"] not in aged):
            phase = "read"
            try:
                ps_inputs = ps_inputs_fn(ticket, asof[0], asof[1], now.timestamp())
                phase = "validate"
                keys = {"ps_facts", "safety", "safety_proof"} | (
                    {"d_blocks"} if cohort["policy"]["ps_inputs"] == "pumpswap-facts-v3" else set())
                if not contract._shape(ps_inputs, keys) or not isinstance(ps_inputs["ps_facts"], dict):
                    raise ValueError("ps_inputs_invalid")
                if ps_inputs["ps_facts"].get("version") != cohort["policy"]["ps_inputs"]:
                    raise ValueError("ps_inputs_version_mismatch")
                frozen = contract.render_judge_sheet(asof[0], **ps_inputs)
                ps_inputs = {key: frozen[key] for key in keys}
            except ValueError as exc:
                reason = (str(exc) if (phase == "read" and re.fullmatch(r"ps_[a-z0-9_]{1,80}", str(exc))
                                      or str(exc) == "ps_inputs_version_mismatch")
                          else "ps_inputs_invalid")
            except Exception:
                reason = "ps_inputs_error"
                logging.getLogger(__name__).exception("desk PS input read failed ticket=%s", ticket["ticket_id"])
            else:
                reason = None
            if reason is not None:
                refusal = _preflight_skip(desk_conn, ticket, reason, now, asof=asof)
                if refusal is not None:
                    out.append(refusal)
                continue
        if not store.claim_judge_call(desk_conn, ticket_id=ticket["ticket_id"], call_id=call_id,
                                      worker=worker, claimed_at=_iso(now)):
            continue                                     # another worker won it
        hour_ago = _iso(now - timedelta(hours=1))
        skip = None
        # a trigger ticket's own no-call reasons (amendment 11 item 4) are decided FIRST among the budget-like ones: a claim that will
        # spend no call must not be turned into budget_exhausted by the hourly count (which includes its own in-flight claim)
        trig_skip, trig_extra = None, {}
        if trigger:
            if asof is None:
                trig_skip, trig_extra = "stale_snapshot", {"asof_reason": asof_failure}
            elif view["band"] != "inside":                  # each a no-call row with the numbers that decided it
                trig_skip = {"below": "band_below", "curve_complete": "band_curve_complete", "unknown": "band_unknown"}[view["band"]]
                trig_extra = {"inline_rank": view, "since_pass_s": round(now.timestamp() - _pass_epoch(ticket), 3)}
                if view["anomaly"]:
                    from sniper.services.memecoin_desk_ranker import ABOVE_LIVE_CURVE_TEXT
                    trig_extra["reason_text"] = ABOVE_LIVE_CURVE_TEXT
            else:
                moved = _moved_since_emission(desk_conn, ticket, asof[0])
                if moved is not None:            # a different setup from the one emitted: no call on it (operator 2026-10-23)
                    trig_skip, trig_extra = "moved_since_emission", moved
        if rank_state in ("cut", "missing"):
            skip = "ranker_" + rank_state
        elif not admitted:
            skip = "budget_skipped"
        elif ticket["ticket_id"] in aged:                   # older than the cohort's defer ceiling: no call
            skip = "seat_cut" if "intake" in ticket["candidate"] else "signal_aged"
        elif ticket["ticket_id"] in dropped:                # deferred past its life without a free call
            skip = "budget_exhausted"
        elif trig_skip is not None:
            skip = trig_skip
        elif store.judge_calls_since(desk_conn, since_iso=hour_ago) > calls_per_hour:   # includes this claim
            skip = "budget_exhausted"
        elif (type(cohort["policy"].get("judge_calls_per_hour")) is int
              and store.judge_calls_since(desk_conn, since_iso=hour_ago, cohort_id=ticket["cohort_id"])
              > cohort["policy"]["judge_calls_per_hour"]):
            # backstop: over the cap AFTER the claim is committed means a second JUDGE worker raced the pre-claim
            # check (the worker is a flock singleton, so this should not happen). Judge-call rows are immutable, so
            # the claim cannot be released: it ends seat_cut (never a model call over the frozen cap)
            skip = "seat_cut"
        extra = ({"cap_tiebreak": dropped[ticket["ticket_id"]]} if ticket["ticket_id"] in dropped
                 else {"aged": aged[ticket["ticket_id"]]} if ticket["ticket_id"] in aged else {})
        if skip is not None and skip == trig_skip:
            extra = trig_extra
        elif skip == "seat_cut" and ticket["ticket_id"] not in aged:     # Gamma: the read tags seat_cut by reason
            extra = {"seat_cut_reason": "raced_over_cap"}
        elif skip is None and not trigger and asof_fn is not None and asof is None:
            skip, extra = "stale_snapshot", {"asof_reason": asof_failure}
        if skip is not None:
            started = _iso(now)
            context = _skip_context(desk_conn, ticket, now, asof=asof, asof_fn=asof_fn, soft_fn=soft_fn, structure_fn=structure_fn)
            store.save_judge_response(desk_conn, call_id=call_id, call_started_at=started, response_at=started,
                                      elapsed_ms=0.0, result={"call_error": skip, "age_at_call_s": _age_s(ticket, now),
                                                                 **extra, **context, **(
                                          {"select_version": SELECT_VERSION} if skip.startswith("ranker_") else {}),
                                          "release_sha": release_sha, **identity})
            # outside the sample: never a funded take. An F row may already exist (H gate-skip wrote
            # not_selected at the hard-filter clock); it stands, never rewritten (Gamma LOW, 6b58552db).
            if skip in ("budget_skipped", "ranker_cut", "ranker_missing", "stale_snapshot", "band_below", "band_curve_complete", "band_unknown", "moved_since_emission") and store.get_admission(
                    desk_conn, ticket_id=ticket["ticket_id"], arm="F") is None:
                store.record_admission(desk_conn, ticket_id=ticket["ticket_id"], arm="F",
                                       decision="not_selected", decided_at=started)
        else:
            soft = None
            if soft_fn is not None:                         # hf2 facts at CALL time: cluster may have landed since the filter
                soft = soft_fn(store.get_ticket(desk_conn, ticket["ticket_id"]))
            sheet_candidate = asof[0] if asof else ticket["candidate"]
            structure = None
            if structure_fn is not None and not new_ps:     # new PS facts already use the exact frozen pool cut
                structure = _structure(structure_fn, ticket, sheet_candidate, now.timestamp())
            context = None
            if context_fn is not None:                      # collector facts AS OF the call: known_at <= now, missing = unknown
                context_inputs = ({"ps_facts": ps_inputs["ps_facts"]}
                                  if ps_inputs is not None and "ps_trending" in cohort["policy"]["context"] else {})
                context = _context(context_fn, ticket, sheet_candidate, now.timestamp(), **context_inputs)
            drift = _drift_block(ticket)
            sheet = contract.render_judge_sheet(sheet_candidate, soft=soft, structure=structure, context=context, drift=drift,
                                                  **(ps_inputs or {}))
            started_dt = now_fn()
            deadline_s = contract.judge_deadline_s()        # stamped with the call: the adapter reads the same knob
            try:                                            # the runner pre-quotes the mint while this call runs
                from sniper.services.memecoin_desk_prequote import enabled as _prequote_on
                if _prequote_on():
                    store.write_prequote_hint(desk_conn, ticket_id=ticket["ticket_id"], network_id=ticket["network_id"],
                                              mint=ticket["mint"], created_at=started_dt.timestamp(),
                                              expires_at=started_dt.timestamp() + float(deadline_s) + 5.0)
            except Exception:                               # a hint is an optimization; it never blocks a call
                logging.getLogger(__name__).exception("desk prequote hint write failed ticket=%s", ticket["ticket_id"])
            try:                                            # inside the try: a serialization error still clears the hint
                user = json.dumps(sheet, separators=(",", ":"), sort_keys=True)
                result = call_model(system_prompt, user, cohort=store.get_cohort(desk_conn, ticket["cohort_id"]))
            finally:                                        # answered or failed: the pre-quote stops (Gamma)
                try:
                    store.clear_prequote_hint(desk_conn, ticket["ticket_id"])
                except Exception:
                    logging.getLogger(__name__).exception("desk prequote hint clear failed ticket=%s",
                                                          ticket["ticket_id"])
            # THE EXACT SHEET THE MODEL SAW (Gamma's CHECKER audit, 2026-10-23): its hash and the zlib+base64 text, so audits
            # and replays stop re-rendering from the candidate. Record-only: the prompt and the decision are unchanged.
            result = {**result, "deadline_s": deadline_s, "age_at_call_s": _age_s(ticket, started_dt),
                      "sheet_sha256": hashlib.sha256(user.encode()).hexdigest(),
                      "system_sha256": _prompt_hash(desk_conn, system_prompt, cohort, started_dt),
                      "sheet_zb64": base64.b64encode(zlib.compress(user.encode(), 6)).decode()}
            if asof:                                       # frozen with the call: a restart records the same snapshot
                result = {**result, "asof": {"candidate": asof[0], "meta": asof[1]}}
            if soft is not None:                           # frozen with the call, like the as-of snapshot
                result = {**result, "soft": soft}
            if structure is not None:
                result = {**result, "structure": structure}
            if view is not None:                            # the claim-time rank view the call was made under
                result = {**result, "inline_rank": view}
            if context is not None:
                result = {**result, "context": context}
            if drift is not None:                           # frozen with the call: record re-renders the same sheet
                result = {**result, "drift": drift}
            if ps_inputs is not None:
                result = {**result, **ps_inputs}
            done_dt = now_fn()
            store.save_judge_response(desk_conn, call_id=call_id, call_started_at=_iso(started_dt),
                                      response_at=_iso(done_dt),
                                      elapsed_ms=max(0.0, (done_dt - started_dt).total_seconds() * 1000),
                                      result={**result, "release_sha": release_sha, **identity})
        call = [c for c in store.list_judge_calls_to_record(desk_conn, limit=1000) if c["call_id"] == call_id][0]
        out.append({"ticket_id": ticket["ticket_id"], **_record(desk_conn, ledger_factory, call, now_fn, record_fn)})
    return out


def recover(desk_conn, ledger_factory, *, now_fn, limit=50):
    """Record claimed calls with no vet stage from their frozen state. Never calls the model.
    Runs at worker start under the singleton flock: any bare claim then belongs to a dead worker and
    becomes judge_lost at once (no age margin; Gamma/Alpha 2026-10-21)."""
    out, after = [], None
    while True:                                       # page past refused rows; each page is `limit` calls
        page = store.list_judge_calls_to_record(desk_conn, limit=limit, after=after)
        if not page:
            return out
        for call in page:
            try:
                out.append(_record(desk_conn, ledger_factory, call, now_fn))
            except ValueError as exc:                 # refused proof: the unknown-cost marker stands;
                out.append({"ticket_id": call["ticket_id"], "action": "REFUSED", "reason": str(exc)})  # never re-asked
        after = (page[-1]["claimed_at"], page[-1]["call_id"])


def close_orphan_claims(desk_conn):
    """Claimed rows whose ticket already has a vet stage (recorded lost by an older release that left the claim
    open): close them as lost. Runs at worker start under the flock; spends nothing, records nothing new."""
    closed = []
    for call in store.list_orphan_judge_claims(desk_conn):
        _close_lost_claim(desk_conn, call)
        closed.append(call["call_id"])
    return closed
