"""Desk POST-MORTEM seat (docs/superpowers/specs/2026-10-21-llm-trading-desk-design.md §2 seat table):
Sonnet 5.5, a daily offline batch. Grades the day's closed desk tickets, verdict against ledger
outcome, and asks the model for text proposals to the crew. It never writes config, never touches a
ticket, and is never on the fill path. Pure code here; the worker (scripts/memecoin_desk_postmortem.py)
owns the DBs, the model call and the report files.

What reaches the model is code-built facts only: enums (action, reason, reason_code, exit_reason),
numbers (rails, timings, PnL) and identifiers. No ticket free text and no model output from an earlier
seat is passed on, so a prompt injection in a coin's metadata cannot reach this seat.
"""
from __future__ import annotations

from sniper.services import memecoin_desk_model_echo as model_echo
import json

MODEL = "claude-sonnet-5-5"
DEADLINE_S = 120.0
PROMPT_VERSION = "desk-postmortem-v1"
SYSTEM_PROMPT = """You are the post-mortem seat of an automated memecoin trading desk. You read one UTC
day of graded closes: what the JUDGE seat decided (ENTER with rails, or SKIP), and what the ledger
says happened. Write short, concrete PROPOSALS for the human crew: a pattern in the reason codes or
rails that lost or won, a rail bound worth testing, a gap in the facts. Each proposal states the
evidence (counts, PnL) it rests on and what would falsify it. With fewer than 10 graded closes, say the
sample is too small and propose nothing but what to measure. You cannot change any setting: never
phrase a proposal as an applied change. Plain text, at most 15 lines."""


# Printed at the top of every report (Gamma, 99ee5076d): acting on a proposal inside H1/H2 would tune on
# outcomes inside the pre-registered windows.
STANDING_RULE = ("Proposals are reports only. An adopted change ships as a new cohort or a new prompt_version; "
                 "the running cohort is never tuned on its own outcomes (H1 10-22..28, H2 10-29..11-04).")


def ticket_facts(ticket, position):
    """One closed or terminal ticket as enums and numbers. `position` is the ledger row (or None)."""
    stages = {s["stage"]: s for s in ticket["stages"]}
    vet = stages.get("vet")
    result = ((vet or {}).get("output") or {}).get("result") or {}
    fact = {"ticket_id": ticket["ticket_id"], "cohort_id": ticket["cohort_id"], "producer": ticket["producer"],
            "state": ticket["state"], "action": result.get("action"), "reason": result.get("reason"),
            "reason_code": result.get("reason_code"), "rails": result.get("rails"),
            "enter_ttl_s": result.get("enter_ttl_s"), "next_check_s": result.get("next_check_s"),
            "fill_reason": (stages.get("fill") or {}).get("reason"), "graded": False}
    if position is not None and position.get("is_open") == 0:
        notional, pnl = position.get("entry_notional"), position.get("realized_pnl")
        fact.update(exit_reason=position.get("exit_reason"), realized_pnl=pnl, entry_notional=notional,
                    opened_at=position.get("opened_at"), closed_at=position.get("closed_at"))
        # absent is never zero: a close without a PnL or a notional stays ungraded
        if isinstance(pnl, (int, float)) and isinstance(notional, (int, float)) and notional > 0:
            fact.update(graded=True, ret_pct=round(pnl / notional * 100.0, 4), win=pnl > 0)
    return fact


def summary(facts):
    graded = [f for f in facts if f["graded"]]
    by_code = {}
    for f in graded:
        row = by_code.setdefault(str(f["reason_code"]), {"n": 0, "wins": 0, "pnl": 0.0})
        row["n"] += 1
        row["wins"] += int(f["win"])
        row["pnl"] = round(row["pnl"] + f["realized_pnl"], 6)
    return {"tickets": len(facts), "graded": len(graded), "wins": sum(f["win"] for f in graded),
            "pnl": round(sum(f["realized_pnl"] for f in graded), 6), "by_reason_code": by_code,
            "ungraded_by_state": _count(f["state"] for f in facts if not f["graded"])}


def _count(values):
    out = {}
    for v in values:
        out[str(v)] = out.get(str(v), 0) + 1
    return out


def user_message(day, facts):
    return json.dumps({"day": day, "prompt_version": PROMPT_VERSION, "summary": summary(facts), "closes": facts},
                      sort_keys=True, separators=(",", ":"))


def echo_ok(echo):
    """This seat's use of the shared rule (memecoin_desk_model_echo): MODEL or a dated snapshot of it.
    A missing echo is allowed, as in every seat."""
    return model_echo.echo_ok(echo, MODEL)


def call_usable(result):
    """None when the model answer can go in the report, else why not. A dead login reads as a
    'success' with is_error (the JUDGE's trap), and a wrong model echo is not this seat's answer."""
    if result.get("call_error"):
        return result["call_error"]
    if result.get("is_error") or not result.get("text"):
        return "model_result_error"
    if not echo_ok(result.get("model_echo")):
        return "model_mismatch"
    return None


def report_markdown(day, facts, result, reason):
    s = summary(facts)
    lines = ["# Desk post-mortem %s" % day, "", STANDING_RULE, "",
             "Graded closes: %d of %d tickets; wins %d; realized PnL %.2f USD." % (s["graded"], s["tickets"],
                                                                                 s["wins"], s["pnl"]),
             "", "| reason_code | n | wins | pnl |", "|---|---|---|---|"]
    lines += ["| %s | %d | %d | %.2f |" % (k, v["n"], v["wins"], v["pnl"]) for k, v in sorted(s["by_reason_code"].items())]
    lines += ["", "Ungraded by state: %s" % json.dumps(s["ungraded_by_state"], sort_keys=True), "",
              "## Proposals (%s, text only; nothing here is applied)" % MODEL, ""]
    if reason is not None:
        lines.append("No proposals: %s." % reason)
    else:
        lines.append(result["text"].strip())
    proof = {k: result.get(k) for k in ("session_id", "result_id", "model_echo", "billing_basis", "notional_usd")}
    lines += ["", "Call: %s" % json.dumps(proof, sort_keys=True)]
    return "\n".join(lines) + "\n"
