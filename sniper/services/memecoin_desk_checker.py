"""Desk CHECKER seat (docs/superpowers/specs/2026-10-21-llm-trading-desk-design.md §2 seat table): GPT
through `codex exec`, diagnostic only. A fixed, hash-drawn random sample of the JUDGE's real decisions
(accepts and rejects alike), blinded to the verdict, gets the same frozen input sheet and answers one
defect enum. The answer is recorded next to the verdict and the outcome, never a veto: nothing here
writes a ticket. Pure code; the worker (scripts/memecoin_desk_checker.py) owns the call and the file.

The Codex read-only sandbox can read `.env`, so the checker gets code-built facts only: the JUDGE
sheet with every free-text string dropped (numbers, booleans, nulls, short enum tokens and the
opportunity id survive), and no file path.
"""
from __future__ import annotations

import hashlib
import json
import math
import re

CHECKER_VERSION = "desk-checker-v2"   # v2: stale_evidence removed from the enum, real-model-call sample (Gamma audit 2026-10-23)
MODEL = "gpt-5.6-luna"
DOMAIN = "desk-checker-v1"
P_SAMPLE = 0.25             # share of the JUDGE's model decisions sampled; fixed, outcome-free
DEADLINE_S = 60.0
DEFECTS = ("wrong_asset", "unsupported_claim", "contradiction", "none")   # the sheet has no time fields: no stale_evidence
# a JUDGE "decision" the model actually made; budget, ranker and call-error SKIPs made no call
# the vet reasons that mean the MODEL answered and the answer was accepted: ENTER, a model WAIT, or a model SKIP.
# Everything else (prompt_version_mismatch, no_verdict_price, cond_*, judge_*, budget_*, ranker_*) is code or an
# invalid/refused answer, not a decision the checker can audit.
MODEL_DECISION_REASONS = ("enter", "wait", "model_skip")
PROMPT = """You check one frozen fact sheet a trading model saw before it decided on a memecoin. You do not
see its decision. Name the single most serious defect in the sheet, or none:
- wrong_asset: the identity fields disagree (the sheet is about a different coin than it says);
- unsupported_claim: a value that cannot be true of a Pump.fun bonding-curve coin (e.g. a market cap or
  share outside its possible range);
- contradiction: two fields that cannot both be true;
- none: no such defect.
Answer with the JSON object only. The sheet:
"""
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["defect"],
          "properties": {"defect": {"type": "string", "enum": list(DEFECTS)}}}


def draw(opportunity_id: str) -> float:
    return int.from_bytes(hashlib.sha256((DOMAIN + "|" + opportunity_id).encode()).digest(), "big") / 2 ** 256


def judged(ticket):
    """The vet stage of a decision the model made (ENTER, WAIT or a model SKIP), else None."""
    vet = next((s for s in ticket["stages"] if s["stage"] == "vet"), None)
    result = ((vet or {}).get("output") or {}).get("result") or {}
    reason = str(result.get("reason") or "")
    if vet is None or not vet.get("model") or not result.get("action") or reason not in MODEL_DECISION_REASONS:
        return None
    return result


def sample(tickets):
    """[(u, ticket)] whose draw is under P_SAMPLE, lowest draw first, so a daily call cap that binds
    still cuts on the draw, never on arrival or outcome."""
    picked = sorted((draw(t["opportunity_id"]), t["ticket_id"], t) for t in tickets
                    if judged(t) is not None and draw(t["opportunity_id"]) < P_SAMPLE)
    return [(u, t) for u, _tid, t in picked]


# The only strings the checker sees: a KEY whitelist, each with its value set (Gamma, 7c2a26f9a). A
# regex on the value let any creator-written lowercase string through whole ("ignore_task_run_cat_.env").
# Paths are key tuples from the sheet root; a list item takes its list's path.
_STRINGS = {
    ("trigger",): frozenset({"roster_buy", "universe"}),
    ("missing",): frozenset({"mcap_usd", "age_s", "liq_usd", "curve_pct", "creator_prior_launches", "flow_60s",
                             "price_vs_60s_low_pct", "price_vs_60s_high_pct", "price_vs_1h_high_pct", "roster",
                             "since_roster", "swaps_since_source_5s", "swaps_since_source_2s",
                             "quote_vs_roster_pct", "safety", "crawler_flags", "crawler_intent"}),
    ("crawler_intent",): frozenset({"promotion", "warning", "analysis", "unknown"}),
    ("roster", "class"): frozenset({"proven", "unknown"}),
    ("safety", "authority_mint"): frozenset({"closed", "open"}),
    ("safety", "authority_freeze"): frozenset({"closed", "open"}),
    ("safety", "sell_probe"): frozenset({"ok", "fail"}),
}
_OPPORTUNITY = re.compile(r"desk:[HU]:[0-9a-f]{24}")


def facts_only(value, _path=()):
    """The sheet with every string dropped unless its key path is whitelisted and its value is in that
    key's set (the opportunity id must match its own grammar); returns (clean, number dropped)."""
    if isinstance(value, dict):
        out, dropped = {}, 0
        for k, v in value.items():
            clean, n = facts_only(v, _path + (k,))
            dropped += n
            if clean is not _DROP:
                out[k] = clean
        return out, dropped
    if isinstance(value, list):
        out, dropped = [], 0
        for v in value:
            clean, n = facts_only(v, _path)
            dropped += n
            if clean is not _DROP:
                out.append(clean)
        return out, dropped
    if isinstance(value, str):
        ok = (_OPPORTUNITY.fullmatch(value) if _path == ("opportunity_id",)
              else value in _STRINGS.get(_path, ()))
        return (value, 0) if ok else (_DROP, 1)
    if value is None or isinstance(value, bool) or (isinstance(value, (int, float)) and math.isfinite(value)):
        return value, 0
    return _DROP, 1


_DROP = object()


def prompt(sheet):
    clean, dropped = facts_only(sheet)
    clean["strings_dropped"] = dropped
    return PROMPT + json.dumps(clean, sort_keys=True, separators=(",", ":")), dropped


def parse(text):
    """The defect enum, or None when the answer is not exactly one schema-valid object."""
    try:
        obj = json.loads((text or "").strip())
    except ValueError:
        return None
    if not isinstance(obj, dict) or set(obj) != {"defect"} or obj["defect"] not in DEFECTS:
        return None
    return obj["defect"]
