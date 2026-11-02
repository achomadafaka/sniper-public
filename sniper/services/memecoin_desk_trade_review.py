"""Per-trade GPT review for the daily desk report (operator order 2026-10-23 via Alpha). REPORT ONLY: the answer is rendered
on the daily page and summarised as a tag histogram; it never feeds a JUDGE/RISK/entry decision (rule b9930440), never
writes a ledger or config row.

What reaches the model: the stored JUDGE sheet (numbers, booleans, enum tokens and ids: the same free-text guard as the GPT
shadow seat voids anything else), the RISK decisions of the position (action enum, reason code, the code-built reason
token string, rails numbers, exact pnl), and the position's price path as [seconds since entry, percent vs entry] points.
No ticker, no coin text. The call runs in the CHECKER jail (scripts/research/judge_gpt_replay.codex_json: codex_slot, per-call
CODEX_HOME, tool-event abort, guarded auth write-back). The answer's free text is untrusted output: printable-only, length-capped
and markup-stripped before it is rendered.

(Not the Sonnet POST-MORTEM seat in memecoin_desk_postmortem.py: that seat grades the JUDGE verdicts in aggregate. This one reads
one closed trade at a time.)
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime

TAGS = ("good_entry_good_exit", "good_entry_early_exit", "good_entry_late_exit", "bad_entry_avoidable", "bad_entry_unavoidable",
        "rail_worked", "model_exit_helped", "model_exit_hurt", "graduation_event", "rug_or_dump", "insufficient_data",
        # the learning loop (lessons-v1): the closed vocabulary of the digest, shared with the code tags
        "entered_top", "entered_farm", "late_curve", "shaken_out", "held_through_death", "good_exit", "migration_step")
ORDER_TTL_S = 10                # a RISK order older than this at apply time is refused by the engine (memecoin_desk_risk_live.ORDER_TTL_S)
FIELD_MAX = 400
MAX_POINTS = 40
REVIEW_SCHEMA = {"type": "object", "additionalProperties": False,
                 "required": ["what_the_sheet_showed", "what_happened", "what_was_missed", "tag"],
                 "properties": {"what_the_sheet_showed": {"type": "string"}, "what_happened": {"type": "string"},
                                "what_was_missed": {"type": "string"}, "tag": {"type": "string", "enum": list(TAGS)}}}
PROMPT = """You review ONE closed trade of an automated memecoin desk, after the fact. Report only: your answer is read by humans
and never changes a rule. Input (JSON, numbers and short code tokens only): the JUDGE's fact sheet at entry (null = unknown, never
zero), the RISK seat's decisions during the hold, the exit, and the price path as [seconds since entry, percent versus the entry price].
"lessons" (when present) are COMPUTED FACTS from the tape, not opinions. "known_at_entry" was visible at the verdict. Everything under
"hindsight" was NOT knowable at the time and covers: the entry versus the best achievable buy within two minutes before and after our fill, every
alternative exit (R = value over cost minus 1, each with its reason, horizon in seconds and a censored flag; death_k is PROVISIONAL), and values
after our exit. Never present hindsight as something the desk should have seen; use it only to say what was left, and say luck when the outcome
came from hindsight alone. Each "risk" row carries "apply": only a RISK decision with apply "applied" or "applied_partial" can be credited or
blamed for the outcome; "no_ack" or a refusal means the funded engine did not act on it (apply_lag_s over 10 = late). "code_tags" are the deterministic tags already
computed; a null value is unknown, never zero.
Answer with one JSON object:
- what_the_sheet_showed: what the entry sheet said about the coin (strengths and warnings), at most 3 sentences;
- what_happened: how the trade played out and how it exited, at most 3 sentences;
- what_was_missed: what the sheet or the RISK decisions did not use or got wrong, or "nothing" (at most 3 sentences);
- tag: exactly one of: """ + ", ".join(TAGS) + """.
Use only the input. If the data is too thin to judge, tag insufficient_data. Facts:
"""
_TOKEN = re.compile(r"[A-Za-z0-9_:.,\-]{1,64}\Z")
_FINITE = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def token(value):
    """A code-built enum string passes; anything else becomes 'unknown' (never forwarded)."""
    return value if isinstance(value, str) and _TOKEN.match(value) else "unknown"


def number(value, nd=4):
    return round(float(value), nd) if _FINITE(value) else None


def downsample(points, limit=MAX_POINTS):
    if len(points) <= limit:
        return points
    step = (len(points) - 1) / float(limit - 1)
    return [points[int(round(i * step))] for i in range(limit)]


def path_points(position, marks):
    """[[seconds since entry, percent vs entry]] from (sweep_ts ISO, mid) marks, ascending, downsampled; [] when unknown."""
    try:
        t0 = datetime.fromisoformat(position["opened_at"]).timestamp()
        t1 = datetime.fromisoformat(position["closed_at"]).timestamp() if position.get("closed_at") else None
        entry = float(position["entry_price"])
    except (KeyError, TypeError, ValueError):
        return []
    if not entry > 0:
        return []
    pts = []
    for ts, mid in marks:
        try:
            t = datetime.fromisoformat(ts).timestamp()
        except (TypeError, ValueError):
            continue
        if _FINITE(mid) and mid > 0 and t >= t0 and (t1 is None or t <= t1):
            pts.append([int(round(t - t0)), round((mid / entry - 1) * 100, 2)])
    return downsample(pts)


def risk_rows(rows):
    """RISK decisions as numbers + enums: [[t_rel_unused], ...] -> list of dicts."""
    out = []
    for r in rows:
        try:
            rails = json.loads(r.get("rails_json") or "null")
        except ValueError:
            rails = None
        out.append({"t_s": number(r.get("t_s"), 0), "action": token(r.get("action")), "code": r.get("reason_code") if isinstance(r.get("reason_code"), int) else None,
                    "reason": token(r.get("reason")) if r.get("reason") else None,
                    "rails": {token(k): number(v) for k, v in rails.items()} if isinstance(rails, dict) else None,
                    "pnl_pct": number(r.get("pnl_pct_exact"), 2),
                    "apply": token(r["apply_status"]) if r.get("apply_status") else "no_ack", "apply_lag_s": number(r.get("apply_lag_s"), 1)})
    return out[:60]


def risk_apply_summary(rows):
    """Counts over the actionable (not HOLD) RISK decisions before the exit: how many the funded engine APPLIED, how many were late, how many
    have no applied ack. Only applied ones can be credited or blamed."""
    act = [r for r in rows if str(r.get("action")).upper() not in ("HOLD", "NONE", "")]
    applied = [r for r in act if str(r.get("apply_status")) in ("applied", "applied_partial")]
    late = [r for r in act if _FINITE(r.get("apply_lag_s")) and r["apply_lag_s"] > ORDER_TTL_S]
    return {"actionable": len(act), "applied": len(applied), "late": len(late), "unapplied": len(act) - len(applied)}


def lessons_facts(row):
    """The compact, model-safe subset of one lessons row (numbers, enums, null). What was knowable AT THE TIME (curve fill, the paid price)
    is separate from HINDSIGHT (the entry optima in the window around the fill, every alternative exit and every post-exit value, all
    computed from tape the desk could not see). Each alternative carries its reason, horizon and censor flag; death_k is provisional.
    None = no row."""
    if not row:
        return None
    f = row["facts"]
    alts = {}
    for k, v in (f.get("alternatives") or {}).items():
        if isinstance(v, dict):
            alts[token(k)] = {"R": number(v.get("R"), 4), "reason": token(v.get("reason")) if v.get("reason") else None,
                              "after_s": number(v.get("after_s"), 1), "censored": bool(v.get("censored"))}
    ident = f.get("identity") or {}
    return {"known_at_entry": {k: number(f.get(k), 3) for k in ("curve_fill_at_entry", "secs_band_cross_to_fill")},
            "code_tags": [token(t) for t in row.get("tags") or []],
            "hindsight": {"entry_optima": {k: number(f.get(k), 3) for k in ("entry_regret_pct", "entry_vs_best_before_pct", "entry_vs_best_after_pct",
                                                                           "entry_rank_pct")},
                          "alternatives": alts, "death_k": number(f.get("death_k"), 2), "death_k_provisional": f.get("death_k_provisional"),
                          "after_exit_R": {k: number(v, 4) for k, v in (f.get("after_exit_R_hindsight") or {}).items()},
                          "best_R_in_900s": number(f.get("best_R_hindsight"), 4), "exit_regret_R": number(f.get("exit_regret_R"), 4),
                          "exit_window_complete": bool((f.get("exit_window") or {}).get("complete")),
                          "migration_step_R": number((f.get("migration") or {}).get("step_R"), 4)},
            "realized_R_basis": token(((f.get("exit") or {}).get("realized_R_basis")) or "unknown"),
            "identity": {"cohort_id": token(ident.get("cohort_id")), "prompt_version": token(ident.get("prompt_version"))},
            "censored": [token(c) for c in f.get("censored") or []]}


def build_facts(position, sheet, risk, marks, lessons=None):
    """The whole model input for one trade, as a JSON-able dict (numbers, enums, null)."""
    return {"sheet": sheet, "risk": risk_rows(risk), "path": path_points(position, marks), "lessons": lessons_facts(lessons),
            "risk_summary": risk_apply_summary(risk), "exit": {"reason": token(position.get("exit_reason")), "pnl_pct": number(position.get("pnl_pct"), 2),
                     "hold_s": number(position.get("hold_s"), 0)},
            "entry": {"notional_usd": number(position.get("entry_notional"), 2)}}


def build_prompt(facts):
    return PROMPT + json.dumps(facts, sort_keys=True, separators=(",", ":"))


def clean_text(value):
    s = "".join(ch for ch in str(value) if ch.isprintable() or ch in " \n")
    for ch in ("|", "`", "<", ">", "[", "]", "#", "*"):
        s = s.replace(ch, " ")
    return " ".join(s.split())[:FIELD_MAX]


def sanitize_review(text):
    """The model's JSON answer -> {fields (clean), tag} or None when it is not exactly the schema."""
    try:
        obj = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(obj, dict) or set(obj) != set(REVIEW_SCHEMA["required"]) or obj.get("tag") not in TAGS:
        return None
    if any(not isinstance(obj[k], str) for k in ("what_the_sheet_showed", "what_happened", "what_was_missed")):
        return None
    return {k: clean_text(obj[k]) for k in ("what_the_sheet_showed", "what_happened", "what_was_missed")} | {"tag": obj["tag"]}


def render_review(position, review, note=None):
    head = "### pos %s (%s) %s" % (position.get("id"), str(position.get("mint"))[:8], position.get("exit_class") or position.get("exit_reason") or "")
    if review is None:
        return [head, "- review unavailable: %s" % (clean_text(note) if note else "no answer"), ""]
    return [head, "- tag: **%s**" % review["tag"], "- sheet: %s" % review["what_the_sheet_showed"], "- happened: %s" % review["what_happened"],
            "- missed: %s" % review["what_was_missed"], ""]
