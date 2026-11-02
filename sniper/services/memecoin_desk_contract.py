"""Pure, fail-closed desk candidate and verdict boundaries. No execution or I/O."""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone

from sniper.services.memecoin_desk_universe import COMPLETENESS, Field, b58decode, build_snapshot
from sniper.services.memecoin_manual_intents import validate_desk_rails
from sniper.services.memecoin_source_initiation import solana_receipt_initiation

_MACHINE = re.compile(r"[a-z][a-z0-9_.:-]{0,127}\Z")
_OPPORTUNITY = re.compile(r"[A-Za-z0-9_:.-]{1,160}\Z")
_ENVELOPE = {"schema", "ticket_id", "producer", "network_id", "mint", "pool", "band_entry_slot",
             "trigger_signature", "trigger_ordinal", "emitted_at", "gate_state", "snapshot"}
_FIELDS = {"mcap_usd", "age_s", "liq_usd", "curve_pct", "creator_prior_launches", "flow_60s",
           "price_vs_60s_low_pct", "price_vs_60s_high_pct", "price_vs_1h_high_pct", "roster",
           "since_roster", "swaps_since_source_5s", "swaps_since_source_2s", "quote_vs_roster_pct",
           "safety", "crawler_flags", "crawler_intent"}
_ROSTER_BUY = {"wallet", "signature", "slot", "source_time", "known_at", "payment_basis",
               "payment_atomic", "acquired_atomic", "commitment"}
PUMPSWAP_SCHEMA = "desk-candidate/pumpswap-v1"
WSOL_MINT = "So11111111111111111111111111111111111111112"
_POOL_STATE = {"base_reserve_atomic", "quote_reserve_atomic", "quote_mint", "base_decimals",
               "supply_atomic", "virtual_quote_atomic", "lp_fee_bps", "protocol_fee_bps",
               "creator_fee_bps", "slot", "map_known_at", "metadata_known_at", "metadata_slot", "metadata_source"}
_POOL_PROVENANCE = {"reserve_known_at", "sol_usd", "sol_age_s", "virtual_quote_known_at",
                    "virtual_quote_slot", "virtual_quote_source", "cut_slot", "map_source", "fee_basis"}


def _number(value, *, minimum=None, maximum=None, integer=False):
    try:
        return (type(value) in ((int,) if integer else (int, float)) and math.isfinite(value)
                and (minimum is None or value >= minimum) and (maximum is None or value <= maximum))
    except OverflowError:
        return False


def _key(value, size):
    try:
        return isinstance(value, str) and size <= len(value) <= size * 2 and len(b58decode(value)) == size
    except (ValueError, TypeError):
        return False


def _utc(value):
    try:
        if not isinstance(value, str):
            raise ValueError
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if at.tzinfo is None or at.utcoffset() != timedelta(0):
            raise ValueError
        return at.timestamp()
    except (ValueError, TypeError, OverflowError):
        raise ValueError("emitted_at") from None


def _shape(value, keys):
    return isinstance(value, dict) and set(value) == set(keys)


def _pool_state(value, *, complete, intake=False):
    if value is None:
        return not complete
    if (not isinstance(value, dict) or not _POOL_STATE <= set(value)
            or set(value) - (_POOL_STATE | _POOL_PROVENANCE)):
        return False
    for key, item in value.items():
        if item is None and not complete:
            continue
        if key.endswith("_atomic"):
            valid = (isinstance(item, str) and re.fullmatch(r"0|[1-9][0-9]{0,19}", item)
                     and int(item) <= 2**64 - 1
                     and (key == "virtual_quote_atomic" or int(item) > 0))
        elif key == "quote_mint":
            valid = item == WSOL_MINT
        elif key == "map_source":                 # intake only (Alpha F2): which proof bound the pool to the mint
            valid = item in ("create_pool", "curve_derived", "intake_canonical_pda")
        elif key == "fee_basis":                  # Office sheets only: an exempt 0/0/0 tip swap was replaced or kept (ps_asof.resolve_fees); absent = the tip's own fees
            valid = item in ("newest_nonzero", "tip_zero_unresolved")
        elif key in ("metadata_source", "virtual_quote_source"):
            valid = isinstance(item, str) and _MACHINE.fullmatch(item)
        else:
            valid = _number(item, integer=key not in ("map_known_at", "metadata_known_at", "reserve_known_at",
                                                     "sol_usd", "sol_age_s", "virtual_quote_known_at"),
                            minimum=1 if key == "slot" else 0,
                            maximum=18 if key == "base_decimals" else 10000 if key.endswith("_bps") else None)
            if key == "sol_usd" and item == 0:
                valid = False
        if not valid:
            return False
    fees = [value[k] for k in ("lp_fee_bps", "protocol_fee_bps", "creator_fee_bps")]
    cut, slot, meta_slot = value.get("cut_slot"), value["slot"], value["metadata_slot"]
    metadata_causal = (meta_slot is None or slot is None or meta_slot <= slot) if cut is None else (
        (slot is None or slot < cut) and (meta_slot is None or meta_slot < cut))
    if intake and value.get("metadata_source") == "intake_mint_read":
        # intake (Alpha 2026-10-27): the assembler's mint read always lands AFTER the print; allowed only for this
        # labelled source on a validated intake candidate (ps_asof proves closed mint authority and exact decimals)
        metadata_causal = slot is None or cut is None or slot < cut
    return ((any(v is None for v in fees) or sum(fees) < 10000)
            and metadata_causal
            and (value.get("virtual_quote_slot") is None or slot is None or value["virtual_quote_slot"] <= slot))


def _fact_value(name, value):
    if value is None:
        return True
    if name == "crawler_intent":
        return value in {"promotion", "warning", "analysis", "unknown"} if isinstance(value, str) else False
    if name == "crawler_flags":
        return (_shape(value, ("copycat_ticker", "bought_hype_pattern", "suspicious_text", "rug_history_claim"))
                and all(type(v) is bool or v is None for v in value.values()))
    if name in {"flow_60s", "roster", "since_roster", "safety"}:
        shapes = {"flow_60s": ("buy_n", "sell_n", "buy_usd", "sell_usd", "makers", "largest_buy"),
                  "roster": ("class", "our_closes", "our_pnl_usd", "buy_usd", "buy_vs_median", "lag_s", "lag_trusted"),
                  "since_roster": ("s", "r_pct"),
                  "safety": ("authority_mint", "authority_freeze", "cluster_hit", "lp_locked_or_burned",
                             "top10_pct", "creator_pct", "sell_probe", "sells_exist_60s")}
        if not _shape(value, shapes[name]):
            return False
        for key, item in value.items():
            if item is None:
                continue
            if key in {"lag_trusted", "cluster_hit", "lp_locked_or_burned", "sells_exist_60s"}:
                valid = type(item) is bool
            elif key == "class":
                valid = isinstance(item, str) and item in {"proven", "unknown"}
            elif key in {"authority_mint", "authority_freeze"}:
                valid = isinstance(item, str) and item in {"closed", "open"}
            elif key == "sell_probe":
                valid = isinstance(item, str) and item in {"ok", "fail"}
            else:
                valid = _number(item, integer=key in {"buy_n", "sell_n", "makers", "our_closes"},
                                minimum=None if key in {"our_pnl_usd", "r_pct"} else 0,
                                maximum=100 if key in {"top10_pct", "creator_pct"} else None)
            if not valid:
                return False
        return True
    return _number(value, integer=name in {"creator_prior_launches", "swaps_since_source_5s", "swaps_since_source_2s"},
                   minimum=None if name.startswith("price_vs_") or name == "quote_vs_roster_pct" else 0,
                   maximum=100 if name == "curve_pct" else None)


INTAKE_FEEDS = frozenset({"pumpfun", "dexscreener", "fomo"})
_INTAKE = ("feed", "filter", "provider_rank", "rank_kind", "observed_at", "host_received_at", "clock_offset_s",
           "intake_row_ref")


def _intake_claim(value):
    """The intake origin of a candidate (spec 2026-10-27-desk-intake-candidate-path-design.md section 2)."""
    return (_shape(value, _INTAKE) and isinstance(value["feed"], str) and value["feed"] in INTAKE_FEEDS
            and isinstance(value["filter"], str) and bool(_MACHINE.fullmatch(value["filter"]))
            and (value["provider_rank"] is None or _number(value["provider_rank"], integer=True, minimum=1))
            and value["rank_kind"] in (None, "list_position")
            and _number(value["observed_at"], minimum=0) and _number(value["host_received_at"], minimum=0)
            and _number(value["clock_offset_s"])
            and isinstance(value["intake_row_ref"], str) and bool(_OPPORTUNITY.fullmatch(value["intake_row_ref"])))


def validate_candidate(candidate: dict) -> dict:
    """Validate without rewriting the producer's frozen snapshot or its hash."""
    if not isinstance(candidate, dict):
        raise ValueError("candidate_keys")
    producer = candidate.get("producer")
    pumpswap = candidate.get("schema") == PUMPSWAP_SCHEMA
    keys = _ENVELOPE | ({"venue"} if pumpswap else {"roster_buy"} if producer == "H" else set())
    if pumpswap and "intake" in candidate:
        keys = keys | {"intake"}           # desk intake (operator 2026-10-27): the feed listing that produced it
        if not _intake_claim(candidate["intake"]):
            raise ValueError("candidate_intake")
    if not pumpswap and producer == "U" and "reentry" in candidate:
        keys = keys | {"reentry"}          # winner re-entry (operator 2026-10-23); the store re-verifies it
        reentry = candidate["reentry"]
        if (not _shape(reentry, ("prev_position_id", "exit_price_usd", "trigger"))
                or reentry["trigger"] not in ("recross", "above_exit", "watch")
                or not _number(reentry["prev_position_id"], integer=True, minimum=1)
                or not _number(reentry["exit_price_usd"], minimum=0) or not reentry["exit_price_usd"] > 0):
            raise ValueError("candidate_reentry")
    if not pumpswap and producer == "U" and "limit_watch" in candidate:
        keys = keys | {"limit_watch"}      # LIMIT arm: one 60 s watch at the sheet price; the store re-verifies it
        claim = candidate["limit_watch"]
        if (not _shape(claim, ("prev_ticket_id",)) or not isinstance(claim["prev_ticket_id"], str)
                or not _OPPORTUNITY.fullmatch(claim["prev_ticket_id"]) or "reentry" in candidate or "drift_requeue" in candidate):
            raise ValueError("candidate_limit_watch")
    if not pumpswap and producer == "U" and "drift_requeue" in candidate:
        keys = keys | {"drift_requeue"}    # v23 fill-drift re-judge: ONE fresh JUDGE call; the store re-verifies it
        claim = candidate["drift_requeue"]
        if not _shape(claim, ("prev_ticket_id", "drift")) or not isinstance(claim["prev_ticket_id"], str) \
                or not _OPPORTUNITY.fullmatch(claim["prev_ticket_id"]) or "reentry" in candidate:
            raise ValueError("candidate_drift_requeue")
        try:
            validate_drift(claim["drift"])
        except ValueError:
            raise ValueError("candidate_drift_requeue") from None
    if not pumpswap and producer == "U" and "watch_look" in candidate:
        keys = keys | {"watch_look"}       # watch-list-v1: look n of a WATCH coin; the store re-verifies it
        claim = candidate["watch_look"]
        if (not _shape(claim, ("prev_ticket_id", "look", "trigger")) or not isinstance(claim["prev_ticket_id"], str)
                or not _OPPORTUNITY.fullmatch(claim["prev_ticket_id"])
                or not _number(claim["look"], integer=True, minimum=2, maximum=10)
                or not isinstance(claim["trigger"], str) or not re.fullmatch(r"[a-z_]{3,32}[0-9]{0,4}", claim["trigger"])
                or {"reentry", "drift_requeue", "limit_watch", "noise_rejudge"} & set(candidate)):
            raise ValueError("candidate_watch_look")
    if not pumpswap and producer == "U" and "noise_rejudge" in candidate:
        keys = keys | {"noise_rejudge"}    # P2: ONE re-judge after a young-coin px_hold WATCH; the store re-verifies it
        claim = candidate["noise_rejudge"]
        # standing-v2 alert_young (Alpha 10-26): a second re-judge carries count 2; count 1 is implied when absent
        if (not (_shape(claim, ("prev_ticket_id",)) or (_shape(claim, ("prev_ticket_id", "count")) and claim["count"] == 2))
                or not isinstance(claim["prev_ticket_id"], str)
                or not _OPPORTUNITY.fullmatch(claim["prev_ticket_id"])
                or {"reentry", "drift_requeue", "limit_watch"} & set(candidate)):
            raise ValueError("candidate_noise_rejudge")
    if set(candidate) - {"opportunity_id"} != keys:
        raise ValueError("candidate_keys")
    if ((not pumpswap and candidate.get("schema") != "desk-candidate/v1") or producer not in ("U", "H")
            or (pumpswap and (producer != "U" or candidate.get("venue") != "pumpswap"))
            or candidate.get("network_id") != "solana" or candidate.get("gate_state") is not None
            or not isinstance(candidate.get("ticket_id"), str)
            or not _OPPORTUNITY.fullmatch(candidate["ticket_id"])
            or ("opportunity_id" in candidate and candidate["opportunity_id"] != candidate["ticket_id"])):
        raise ValueError("candidate_identity")
    if (not _key(candidate.get("mint"), 32) or not _key(candidate.get("pool"), 32)
            or not _key(candidate.get("trigger_signature"), 64)
            or not _number(candidate.get("band_entry_slot"), integer=True, minimum=1)
            or not _number(candidate.get("trigger_ordinal"), integer=True, minimum=0)):
        raise ValueError("candidate_identity")
    if producer == "U" and candidate["pool"] == candidate["mint"]:
        raise ValueError("candidate_pool_mint")
    emitted = _utc(candidate["emitted_at"])
    snapshot = candidate["snapshot"]
    if (not _shape(snapshot, ("emission_at", "fields", "missing", "hash"))
            or not _number(snapshot["emission_at"], minimum=0) or snapshot["emission_at"] != emitted
            or not isinstance(snapshot["fields"], dict)
            or set(snapshot["fields"]) - (_FIELDS | ({"pool_state"} if pumpswap else set()))):
        raise ValueError("snapshot_shape")
    missing = []
    for name, row in snapshot["fields"].items():
        if not _shape(row, ("value", "source", "known_at", "slot", "completeness")):
            raise ValueError("field_shape")
        if not isinstance(row["source"], str) or not _MACHINE.fullmatch(row["source"]):
            raise ValueError("field_source")
        if row["completeness"] not in COMPLETENESS:
            raise ValueError("field_completeness")
        if ((row["known_at"] is not None and not _number(row["known_at"], minimum=0))
                or (row["slot"] is not None and not _number(row["slot"], integer=True, minimum=0))):
            raise ValueError("field_provenance")
        if row["value"] is not None and (row["known_at"] is None or row["known_at"] > emitted):
            raise ValueError("future_fact")
        if name == "pool_state":
            if not _pool_state(row["value"], complete=row["completeness"] == "complete", intake="intake" in candidate):
                raise ValueError("field_value")
            state = row["value"]
            if state is not None:
                late_mint_read = "intake" in candidate and state.get("metadata_source") == "intake_mint_read"
                for clock in ("map_known_at", "metadata_known_at", "reserve_known_at", "virtual_quote_known_at"):
                    if clock == "metadata_known_at" and late_mint_read:
                        # intake (Alpha 2026-10-27): the ACTUAL mint-read receipt, later than the print and the field, kept
                        # exactly; ps_asof's intake mode bounds it (<= its own now, fresh, closed authority: Delta)
                        continue
                    if state.get(clock) is not None and (row["known_at"] is None or state[clock] > row["known_at"]):
                        raise ValueError("future_fact")
            if state is not None and state["slot"] is not None and state["slot"] != row["slot"]:
                raise ValueError("field_provenance")
        elif (pumpswap and name == "curve_pct" and row["value"] is not None) or not _fact_value(name, row["value"]):
            raise ValueError("field_value")
        if row["value"] is None or row["completeness"] != "complete":
            missing.append(name)
    if snapshot["missing"] != sorted(missing):
        raise ValueError("snapshot_missing")
    try:
        body = {k: v for k, v in snapshot.items() if k != "hash"}
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    except (ValueError, TypeError, OverflowError):
        raise ValueError("snapshot_shape") from None
    if snapshot["hash"] != digest:
        raise ValueError("snapshot_hash")
    if producer == "H":
        buy = candidate["roster_buy"]
        if (not _shape(buy, _ROSTER_BUY) or not _key(buy["wallet"], 32)
                or buy["signature"] != candidate["trigger_signature"]
                or not _number(buy["slot"], integer=True, minimum=1) or buy["slot"] != candidate["band_entry_slot"]
                or buy["commitment"] not in ("confirmed", "finalized")
                or not _number(buy["source_time"], minimum=0) or not _number(buy["known_at"], minimum=0)
                or not buy["source_time"] <= buy["known_at"] <= emitted
                or buy["payment_basis"] not in ("pump_event_net_sol", "route_owner_quote_out", "unknown")
                or any(not isinstance(buy[k], str) or not re.fullmatch(r"[1-9][0-9]*", buy[k])
                       for k in ("payment_atomic", "acquired_atomic"))):
            raise ValueError("receipt_identity")
    out = copy.deepcopy(candidate)
    out["opportunity_id"] = candidate["ticket_id"]
    return out


def validate_structure(structure) -> dict:
    """The market-structure block of the JUDGE sheet (hf4): the closed vocabulary of memecoin_desk_structure only. Keys and
    strings come from fixed sets, numbers are finite and bounded, depth and size are bounded: nothing a coin's author wrote
    can pass. ValueError('structure') otherwise."""
    from sniper.services import memecoin_desk_structure as M

    def ok(node, depth):
        if depth > 5:
            return False
        if node is None or type(node) is bool:
            return True
        if type(node) is int:
            return abs(node) <= 2**53
        if type(node) is float:
            return math.isfinite(node) and abs(node) < 1e15
        if isinstance(node, str):
            return node in M.ALLOWED_STRINGS or any(node.startswith(p) and _MACHINE.fullmatch(node) for p in M.ALLOWED_PREFIXES)
        if isinstance(node, list):
            return len(node) <= 64 and all(ok(x, depth + 1) for x in node)
        if isinstance(node, dict):
            return len(node) <= 64 and all(isinstance(k, str) and k in M.ALLOWED_KEYS and ok(v, depth + 1) for k, v in node.items())
        return False
    try:
        if not isinstance(structure, dict) or structure.get("version") not in M.VERSIONS or not ok(structure, 0):
            raise ValueError
        if len(json.dumps(structure, separators=(",", ":"), allow_nan=False)) > 20000:
            raise ValueError
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise ValueError("structure") from None
    return copy.deepcopy(structure)


DRIFT_KEYS = ("sheet_price_usd", "quote_price_usd", "drift_pct", "noise_pct", "k", "limit_pct")


def validate_drift(drift) -> dict:
    """The fill-drift re-queue block (Lambda's drift gate, 2026-10-23): the previous verdict's sheet price, the refused
    quote price, drift % net of modelled fee and impact, the coin noise (range5 %), k and the limit (k x noise). Finite
    numbers only; ValueError('drift') otherwise."""
    if (not _shape(drift, DRIFT_KEYS) or any(not _number(drift[k]) for k in DRIFT_KEYS)
            or not drift["sheet_price_usd"] > 0 or not drift["quote_price_usd"] > 0):
        raise ValueError("drift")
    return dict(drift)


def cohort_policy_hash(policy, *, producer, prompt_version):
    """Canonical ledger cohort digest, including the fixed kill bound (never the inner policy alone)."""
    from sniper.services.memecoin_known_group_canary import _valid_policy
    manual = _valid_policy(policy, producer, prompt_version)
    body = json.dumps({"producer": producer, "prompt_version": prompt_version, "policy": policy,
                       "kill_limit_usd": 0.0 if manual else 200.0,
                       **({'stop_mode':'manual'} if manual else {})},
                      sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(body.encode()).hexdigest()


def validate_ps_inputs(policy, prompt_version):
    """Input semantics and the v28 system prompt are frozen cohort identity."""
    enabled = policy.get("ps_inputs")
    source = (policy.get("hard_filter") or {}).get("intake_source_version")
    if enabled is None and source is None and prompt_version not in ("desk-judge-v27", "desk-judge-v28", "desk-judge-v29"):
        return False
    expected = {"desk-judge-v27": "pumpswap-facts-v1", "desk-judge-v28": "pumpswap-facts-v2",
                "desk-judge-v29": "pumpswap-facts-v3"}.get(prompt_version)
    if (expected is None or enabled != expected or source != "desk-intake-source-v1"
            or policy.get("venue") != "pumpswap"
            or "intake" not in policy.get("hard_filter", {})
            or not isinstance(policy.get("structure"), dict)
            or policy["structure"].get("sheet") != "facts-v2"
            or not isinstance(policy.get("context"), dict) or policy["context"].get("pumpswap") is not True):
        raise ValueError("cohort_policy:ps_inputs")
    if enabled in ("pumpswap-facts-v2", "pumpswap-facts-v3"):
        digest = policy.get("judge_playbook_sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("cohort_policy:ps_inputs_playbook")
    if enabled == "pumpswap-facts-v3":
        # D Amendment 14: the D JUDGE is claude-opus-5-5 via claude_sdk; no GPT-only keys
        if (policy.get("judge_model") != "claude-opus-5-5" or "judge_provider" in policy
                or "judge_effort" in policy):
            raise ValueError("cohort_policy:d_judge_model")
        refusal = d_component_refusal(policy)
        if refusal:
            raise ValueError("cohort_" + refusal)
    return True


# Cohort D sheet-level rules not owned by any one block (hash-bound like the blocks; Gamma 2026-10-28).
D_SHEET_DEFINITION = {
    "version": "desk-d-sheet-v1",
    "clock": {"source": "<state>/data/clock_offset.json via memecoin_desk_intake.clock_offset",
              "max_age_s": 3600,
              "rule": "ONE offset sampled per sheet build converts sheet_as_of, decision_at and pass_persist_at from "
                      "host to corrected UTC for the history blocks; unknown, non-finite or older than max_age_s => "
                      "both history blocks unknown:clock_unknown, never an implicit 0; stored as d_blocks.clock "
                      "{offset_s, sampled_at}"},
    "blocks": ["buyers", "creator_history", "coin_age", "clock"],
}
D_SHEET_DEFINITION_SHA256 = hashlib.sha256(json.dumps(D_SHEET_DEFINITION, sort_keys=True, separators=(",", ":"),
                                                       ensure_ascii=False).encode("utf-8")).hexdigest()   # the one D rule
CLOCK_MAX_AGE_S = float(D_SHEET_DEFINITION["clock"]["max_age_s"])


def d_components():
    """Cohort D (prereg 8a2162552 + amendments): every sheet block's frozen {version, definition_sha256}, from the
    producing modules. The policy must carry each exactly; a mismatch refuses the cohort, never a partial D sheet."""
    from sniper.services import memecoin_desk_buyer_facts as B
    from sniper.services import memecoin_desk_history_facts as H
    from sniper.services import memecoin_desk_ps_facts as F
    return {"buyers": {"version": B.VERSION, "definition_sha256": B.DEFINITION_SHA256},
            "history": {"version": H.VERSION, "definition_sha256": H.DEFINITION_SHA256},
            "chart": {"version": F.VERSION3, "definition_sha256": F.CHART_DEFINITION_SHA256},
            "d_sheet": {"version": D_SHEET_DEFINITION["version"], "definition_sha256": D_SHEET_DEFINITION_SHA256},
            **{name: _context_module(name).definition() for name in D_CONTEXT_MODULES}}


D_RETIRED_COMPONENTS = ("dex_token",)   # Amendment 17: a D policy binding a retired component is refused


def d_component_refusal(policy):
    """None when the policy binds every D component exactly and no retired one, else 'policy:<key>_mismatch' or
    'policy:<key>_retired'."""
    for key, want in d_components().items():
        if not isinstance(policy, dict) or policy.get(key) != want:
            return "policy:%s_mismatch" % key
    for key in D_RETIRED_COMPONENTS:
        if key in policy:
            return "policy:%s_retired" % key
    return None


_BUYERS_KNOWN = {"state", "version", "definition_sha256", "buyers_n", "excluded_n", "followed_n", "known_n", "graded_n",
                 "roster", "real_pnl_grade", "grade_hist", "first_seen_followed_s", "first_seen_basis", "lower_bound",
                 "covered_s", "window_s", "roster_verified_n", "roster_state_lag_s"}
_BUYERS_REASONS = {"ledger_unreadable", "roster_unreadable", "tape_unreadable", "tape_missing", "grade_asof_unknown",
                   "roster_unknown"}
_ROSTER_LABELS = {"armed", "followed", "excluded", "state_unknown", "unknown"}
_GRADE_LABELS = {"pos", "neg", "insufficient"}
_HISTORY_KEYS = {"state", "reason", "definition_sha256", "as_of", "creator_prior_launches_lb", "dev_wallet",
                 "curve_creator", "create_event_user", "fetched_at", "clock_offset_s", "fetched_via", "history_reason",
                 "dev_equals_creator", "field_unknown"}
_COIN_AGE_KEYS = {"state", "reason", "basis", "age_s", "occurred_at", "known_at"}


def _count(v):
    return type(v) is int and 0 <= v <= 10**7


def validate_buyers(block):
    from sniper.services import memecoin_desk_buyer_facts as B
    if not isinstance(block, dict) or block.get("version") != B.VERSION:
        raise ValueError("d_buyers")
    if block.get("state") == "unknown":
        if not _shape(block, {"state", "reason", "version"}) or block["reason"] not in _BUYERS_REASONS:
            raise ValueError("d_buyers")
        return copy.deepcopy(block)
    if (block.get("state") != "known" or not _shape(block, _BUYERS_KNOWN)
            or block["definition_sha256"] != B.DEFINITION_SHA256
            or not all(_count(block[k]) for k in ("buyers_n", "excluded_n", "followed_n", "known_n", "graded_n",
                                                    "roster_verified_n"))
            or not _shape(block["roster"], _ROSTER_LABELS) or not all(_count(v) for v in block["roster"].values())
            or not _shape(block["real_pnl_grade"], _GRADE_LABELS) or block["grade_hist"] != block["real_pnl_grade"]
            or not all(_count(v) for v in block["real_pnl_grade"].values())
            or block["first_seen_basis"] not in (None, "create_block_time", "window_start")
            or (block["first_seen_followed_s"] is not None and not _number(block["first_seen_followed_s"], minimum=0))
            or type(block["lower_bound"]) is not bool
            or not _number(block["covered_s"], minimum=0) or not _number(block["window_s"], minimum=0)
            or block["roster_state_lag_s"] != B.R.LAG_S):
        raise ValueError("d_buyers")
    return copy.deepcopy(block)


def validate_history(block, coin_age, *, as_of):
    from sniper.services import memecoin_desk_history_facts as H
    lb = block.get("creator_prior_launches_lb") if isinstance(block, dict) else None
    if (not _shape(block, _HISTORY_KEYS) or block["definition_sha256"] != H.DEFINITION_SHA256 or block["as_of"] != as_of
            or block["state"] not in ("known", "unknown")
            or not _shape(lb, {"lb", "status", "pages", "source", "reason"}) or lb["status"] not in ("complete", "capped", "unknown")
            or (lb["status"] == "unknown") != (lb["lb"] is None) or (lb["lb"] is not None and not _count(lb["lb"]))
            or (block["state"] == "known") != (lb["status"] != "unknown")
            or any(r is not None and r not in H.REASONS for r in (block["reason"], block["history_reason"], lb["reason"]))
            or not isinstance(block["field_unknown"], dict)
            or any(not isinstance(v, str) or v not in H.REASONS for v in block["field_unknown"].values())
            or block["dev_equals_creator"] not in (None, True, False)
            or block["fetched_via"] not in (None, "nightly", "inline")):
        raise ValueError("d_history")
    if (not _shape(coin_age, _COIN_AGE_KEYS) or coin_age["basis"] != H.BASIS or coin_age["state"] not in ("known", "unknown")
            or (coin_age["state"] == "known") != (coin_age["age_s"] is not None)
            or (coin_age["age_s"] is not None and not _number(coin_age["age_s"], minimum=0))
            or (coin_age["state"] == "unknown" and coin_age["reason"] not in H.REASONS)):
        raise ValueError("d_history")
    return copy.deepcopy(block), copy.deepcopy(coin_age)


D_BLOCK_KEYS = frozenset({"buyers", "creator_history", "coin_age", "clock"})
# Epsilon's D context blocks: sheet key -> producing module. Each module exposes definition() {version,
# definition_sha256}, KNOWN_KEYS, UNKNOWN_KEYS, REASONS, FIELD_REASONS and <key>_for(candidate, *, desk_dir, as_of)
# returning a typed unknown instead of raising. dex_token (DexScreener UI) is NOT in D: the anonymous UI answers
# 403/challenge, so it would be unknown:challenged forever (operator 2026-10-28 ~14:4xZ).
D_CONTEXT_MODULES = {"heat": "sniper.services.memecoin_desk_heat_v2",
                     "list_claims": "sniper.services.memecoin_desk_list_claims"}


def _context_module(name):
    return importlib.import_module(D_CONTEXT_MODULES[name])


def d_block_keys():
    return D_BLOCK_KEYS | frozenset(D_CONTEXT_MODULES)


def validate_context_block(name, block):
    """One D context block against its module's frozen key sets and definition hash; known or typed unknown only."""
    mod = _context_module(name)
    state = block.get("state") if isinstance(block, dict) else None
    keys = {"known": mod.KNOWN_KEYS, "unknown": mod.UNKNOWN_KEYS}.get(state)
    if (keys is None or not _shape(block, keys)
            or block["definition_sha256"] != mod.definition()["definition_sha256"]
            or (state == "unknown" and block["reason"] not in mod.REASONS)
            or (state == "known" and (block["reason"] is not None or not isinstance(block["field_unknown"], dict)
                                      or any(v not in mod.FIELD_REASONS for v in block["field_unknown"].values())))):
        raise ValueError("d_" + name)
    return copy.deepcopy(block)


def _validate_clock(clock, history, age):
    """The offset sampled once per sheet build that converted the host times to corrected UTC. Unknown offset =>
    both history blocks are unknown:clock_unknown."""
    if not _shape(clock, {"offset_s", "sampled_at"}) or (clock["offset_s"] is None) != (clock["sampled_at"] is None):
        raise ValueError("d_clock")
    if clock["offset_s"] is None:
        if history["reason"] != "clock_unknown" or age["reason"] != "clock_unknown":
            raise ValueError("d_clock")
    elif not _number(clock["offset_s"], minimum=-86400, maximum=86400) or not _number(clock["sampled_at"], minimum=0):
        raise ValueError("d_clock")
    return dict(clock)


def validate_d_blocks(blocks, facts):
    """The D sheet's extra blocks, bound to the same ps_facts (one market cut, one sheet_as_of)."""
    if not _shape(blocks, d_block_keys()) or facts.get("version") != "pumpswap-facts-v3":
        raise ValueError("d_blocks")
    history, age = validate_history(blocks["creator_history"], blocks["coin_age"], as_of=facts["as_of"])
    return {"buyers": validate_buyers(blocks["buyers"]), "creator_history": history, "coin_age": age,
            "clock": _validate_clock(blocks["clock"], history, age),
            **{name: _context_clock(validate_context_block(name, blocks[name]), blocks["clock"], name)
               for name in D_CONTEXT_MODULES}}


def _context_clock(block, clock, name):
    """An unknown sheet clock offset => every context block is unknown:clock_unknown (it was never read)."""
    if clock["offset_s"] is None and (block["state"] != "unknown" or block["reason"] != "clock_unknown"):
        raise ValueError("d_" + name)
    return block


def safety_from_evidence(candidate, evidence, *, policy, as_of):
    """Project the candidate's own admission proof; missing holders/LP stay unknown."""
    if hard_filter(candidate, evidence=evidence, policy=policy,
                   evaluated_at=datetime.fromtimestamp(as_of, timezone.utc).isoformat())["action"] != "PASS":
        raise ValueError("safety_evidence")
    checks = evidence["checks"]
    token = checks["token_safety"]
    soft = soft_facts(checks)
    holders = soft["holders"]
    return {"authority_mint": token.get("authority_mint"), "authority_freeze": token.get("authority_freeze"),
            "cluster_hit": None, "lp_locked_or_burned": None,
            "top10_pct": holders.get("top10_pct"), "creator_pct": holders.get("creator_pct"),
            "sell_probe": "ok" if checks["sell_probe"].get("status") == "pass" else None,
            "sells_exist_60s": None}


def render_judge_sheet(candidate: dict, *, soft=None, allow_legacy_soft=False, structure=None, context=None, soft_shape="current", drift=None,
                       ps_facts=None, safety=None, safety_proof=None, d_blocks=None) -> dict:
    """`soft` = soft_facts() output (hard filter hf2): the JUDGE sees cluster / holders as typed facts, never as a kill."""
    candidate = validate_candidate(candidate)
    sheet = {"opportunity_id": candidate["opportunity_id"],
             "trigger": "roster_buy" if candidate["producer"] == "H" else "universe"}
    if candidate.get("venue") == "pumpswap":
        sheet["venue"] = "pumpswap"
    if "intake" in candidate:                   # desk intake (playbook v26): which feed list showed the coin, as context
        sheet["trigger"] = "intake"
        sheet["intake"] = {k: candidate["intake"][k] for k in ("feed", "filter", "provider_rank")}
    for name in sorted(_FIELDS):
        row = candidate["snapshot"]["fields"].get(name)
        sheet[name] = row["value"] if row and row["completeness"] == "complete" else None
    if candidate["producer"] == "U":
        for name in ("roster", "since_roster", "quote_vs_roster_pct",
                     "swaps_since_source_5s", "swaps_since_source_2s"):
            sheet[name] = None
    else:
        for window in (2, 5):
            name = f"swaps_since_source_{window}s"
            row = candidate["snapshot"]["fields"].get(name)
            if row and (row["known_at"] is None
                        or row["known_at"] < candidate["roster_buy"]["source_time"] + window):
                sheet[name] = None
    sheet["missing"] = sorted(k for k in _FIELDS if sheet[k] is None)
    if candidate.get("venue") == "pumpswap":
        from sniper.services.memecoin_desk import priced_verdict
        price = priced_verdict(candidate, clip_usd=25, basis="asof")
        sheet["pool_state"] = copy.deepcopy(candidate["snapshot"]["fields"].get("pool_state", {}).get("value")) if price else None
        sheet["verdict_price"], sheet["supply_basis"] = price, "supply_at_read"
        for name in ("mcap_usd", "liq_usd"):
            sheet[name] = price[name] if price else None
        sheet["missing"] = sorted(k for k in _FIELDS | {"pool_state"} if sheet.get(k) is None)
    if soft is not None:
        sheet["evidence"] = validate_soft_facts(soft, allow_legacy=allow_legacy_soft, shape=soft_shape)
    if structure is not None:
        sheet["structure"] = validate_structure(structure)
    if drift is not None:                       # only on a drift re-queue: why the last ENTER was refused at the fill
        sheet["drift"] = validate_drift(drift)
    if context is not None:                     # as-of collector facts (trending / narrative / DexScreener / heat)
        from sniper.services.memecoin_desk_context_facts import validate as validate_context
        sheet["ctx"] = validate_context(context)
    if ps_facts is not None:
        from sniper.services.memecoin_desk_ps_facts import validate
        facts = validate(ps_facts)
        state = candidate["snapshot"]["fields"].get("pool_state", {}).get("value") or {}
        if (candidate.get("venue") != "pumpswap" or facts["mint"] != candidate["mint"]
                or facts["pool"] != candidate["pool"] or facts["snapshot_hash"] != candidate["snapshot"]["hash"]
                or facts["as_of"] != candidate["snapshot"]["emission_at"]
                or facts["cut_slot"] != state.get("cut_slot")):
            raise ValueError("ps_facts_binding")
        sheet["ps_facts"], sheet["structure"] = facts, facts["structure"]
        for name in ("flow_60s", "price_vs_60s_low_pct", "price_vs_60s_high_pct", "price_vs_1h_high_pct",
                     "age_s", "creator_prior_launches"):
            sheet[name] = facts[name]
        if facts["version"] == "pumpswap-facts-v3":
            sheet["chart"] = facts["chart"]
    if (d_blocks is None) != (ps_facts is None or sheet.get("ps_facts", {}).get("version") != "pumpswap-facts-v3"):
        raise ValueError("d_blocks")                    # a v3 sheet always carries the D blocks; no other sheet does
    if d_blocks is not None:
        sheet["d_blocks"] = validate_d_blocks(d_blocks, sheet["ps_facts"])
        sheet.update(sheet["d_blocks"])
    if safety is not None:
        if not isinstance(safety, dict) or not _fact_value("safety", safety):
            raise ValueError("safety")
        sheet["safety"] = copy.deepcopy(safety)
    if safety_proof is not None:
        proof_keys = {"network_id", "mint", "pool", "emission_snapshot_hash", "known_at", "probe_receipt_cut", "probe_cut_slot"}
        if (ps_facts is None or safety is None or not _shape(safety_proof, proof_keys)
                or any(safety_proof[k] != candidate[k] for k in ("network_id", "mint", "pool"))
                or not isinstance(safety_proof["emission_snapshot_hash"], str)
                or not re.fullmatch("[0-9a-f]{64}", safety_proof["emission_snapshot_hash"])
                or not _number(safety_proof["known_at"], minimum=0, maximum=facts["as_of"])
                or safety_proof["probe_receipt_cut"] != facts["receipt_cut"]
                or safety_proof["probe_cut_slot"] != facts["cut_slot"]):
            raise ValueError("safety_proof")
        sheet["safety_proof"] = copy.deepcopy(safety_proof)
    if ps_facts is not None or safety is not None:
        sheet["missing"] = sorted(k for k in _FIELDS | ({"pool_state"} if candidate.get("venue") == "pumpswap" else set())
                                  if sheet.get(k) is None)
    return sheet


def _skip(reason, code=None):
    return {"action": "SKIP", "reason": reason, "rails": None, "condition": None, "reason_code": code,
            "enter_ttl_s": None, "next_check_s": None}


def validate_timing_caps(timing_caps) -> dict:
    """Validate the frozen operator ceilings; missing caps have no implicit defaults."""
    limits = {"enter_ttl_max_s": (3, 30), "boundary_max_s": (5, 120), "wait_max_s": (10, 300)}
    if not _shape(timing_caps, limits) or any(
            not _number(timing_caps[key], integer=True, minimum=low, maximum=high)
            for key, (low, high) in limits.items()):
        raise ValueError("timing_caps")
    return dict(timing_caps)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


# Closed set of JUDGE call failures, each its own SKIP reason 'judge_<slug>' (rule-5 item 5).
JUDGE_CALL_ERRORS = frozenset({
    "refusal",            # stop_reason refusal
    "rate_limited",       # 429 / subscription limit
    "auth_expired",       # dead OAuth login (is_error with subtype 'success')
    "budget_exhausted",   # seat-call budget spent: no model call made
    "budget_skipped",     # not in this hour's hash sample (arrivals above budget): no model call made
    "ranker_cut",         # cohort U: closed without ever making the runners shortlist: no model call made
    "ranker_missing",     # cohort U: the ranker never ranked it within its life: a coverage hole, no call
    "band_below",         # trigger cohort U: market cap below the band at its claim-time snapshot: no model call made
    "band_curve_complete",  # trigger cohort U: the curve is complete (off-curve coin, never chased): no model call made
    "band_unknown",       # trigger cohort U: cap unreadable, or above the band on a live curve (agent-lifted or unexplained): no model call made
    "moved_since_emission",  # trigger cohort U: the claim-time cap moved past the cohort's judge_moved_max_pct from emission: no call
    "signal_aged",        # older than the cohort's judge_defer_max_s from emission when its turn came: no call
    "seat_cut",           # intake cohort: no seat under its own judge_calls_per_hour before judge_defer_max_s: no call (final)
    "output_budget_unsupported",  # frozen output cap cannot be enforced: no dispatch
    "codex_busy", "cli_missing", "no_isolation", "no_codex_login",
    "tool_event", "stream_too_large", "rate_limit", "invalid_json", "transport_error",
    "cli_failed",         # the CLI process failed to start or died
    "provider_error",     # any other provider-side error
    "timeout",            # no final result inside the deadline
    "lost",               # claimed, but the result was never saved (crash): never re-asked
    "model_result_error", # the SDK result carried is_error=True
    "answer_budget_mismatch",  # v23: the worker's MEMECOIN_DESK_JUDGE_MAX_OUTPUT_TOKENS differs from the cohort's frozen value: no call
})


JUDGE_DEADLINE_DEFAULT_S = 12.0     # operator 2026-10-22: Haiku 5.5 always thinks; a real call took 6.6 s against the old 5 s
JUDGE_DEADLINE_MAX_S = 60.0
LEGACY_JUDGE_DEADLINE_S = 5.0       # what a saved result without a stamped deadline was made under


def judge_deadline_s(environ=None) -> float:
    """The ONE JUDGE call deadline: the SDK adapter's wait_for AND validate_verdict's timeout bound both read it. MEMECOIN_DESK_JUDGE_DEADLINE_S,
    default 12 s (the RISK seat's), finite and in (0, 60]; a typo raises ValueError instead of silently falling back."""
    raw = (os.environ if environ is None else environ).get("MEMECOIN_DESK_JUDGE_DEADLINE_S")
    if raw is None or str(raw).strip() == "":
        return JUDGE_DEADLINE_DEFAULT_S
    try:
        value = float(str(raw).strip())
    except ValueError:
        raise ValueError("MEMECOIN_DESK_JUDGE_DEADLINE_S must be a number") from None
    if not math.isfinite(value) or not 0 < value <= JUDGE_DEADLINE_MAX_S:
        raise ValueError("MEMECOIN_DESK_JUDGE_DEADLINE_S must be in (0, %g]" % JUDGE_DEADLINE_MAX_S)
    return value


JUDGE_DEFER_DEFAULT_S = 120.0       # Gamma 2026-10-22: ~2x the largest claim age seen (61 s, n=16); band coins drift fast
JUDGE_DEFER_MAX_S = 300.0


def judge_defer_max_s(environ=None) -> float:
    """How long a shortlisted ticket may wait (deferred) for a free JUDGE call when the hourly cap binds, counted
    from its emission. MEMECOIN_DESK_JUDGE_DEFER_MAX_S, default 120 s, finite and in (0, 300]; frozen into the cohort
    policy at provisioning; a typo raises ValueError instead of silently falling back."""
    raw = (os.environ if environ is None else environ).get("MEMECOIN_DESK_JUDGE_DEFER_MAX_S")
    if raw is None or str(raw).strip() == "":
        return JUDGE_DEFER_DEFAULT_S
    try:
        value = float(str(raw).strip())
    except ValueError:
        raise ValueError("MEMECOIN_DESK_JUDGE_DEFER_MAX_S must be a number") from None
    return check_defer_max_s(value)


def check_defer_max_s(value) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= JUDGE_DEFER_MAX_S:
        raise ValueError("MEMECOIN_DESK_JUDGE_DEFER_MAX_S must be in (0, %g]" % JUDGE_DEFER_MAX_S)
    return float(value)


STANDING_VERSION = "watch-v1"
STANDING_V2 = "standing-v2"
WATCH_LIST_VERSION = "watch-list-v1"   # operator 10-26: re-look H (WATCH) coins with a fresh sheet (Gamma prereg 6c9b1f37f)


def validate_watch_list(rule) -> dict:
    """policy.watch_list = {"version", "max_looks", "life_s", "cadence_s", "relook_seats_per_h"}; malformed raises."""
    if (not isinstance(rule, dict) or set(rule) != {"version", "max_looks", "life_s", "cadence_s", "relook_seats_per_h"}
            or rule["version"] != WATCH_LIST_VERSION
            or not _number(rule["max_looks"], integer=True, minimum=2, maximum=10)
            or not _number(rule["life_s"], integer=True, minimum=60, maximum=3600)
            or not isinstance(rule["cadence_s"], list) or not rule["cadence_s"]
            or not all(_number(c, integer=True, minimum=10, maximum=3600) for c in rule["cadence_s"])
            or rule["cadence_s"] != sorted(rule["cadence_s"])
            or not _number(rule["relook_seats_per_h"], integer=True, minimum=0, maximum=200)):
        raise ValueError("cohort_policy:watch_list")
    return rule


CHASE_LIMIT_VERSION = "chase-limit-v1"   # Alpha 10-26 option 3: skipped_chase ENTER -> standing limit at the verdict price


def validate_chase_limit(rule) -> dict:
    """policy.chase_limit = {"version", "order_max_s", "book_max", "graduation_guard_pct"}; malformed raises."""
    if (not isinstance(rule, dict) or set(rule) != {"version", "order_max_s", "book_max", "graduation_guard_pct"}
            or rule["version"] != CHASE_LIMIT_VERSION
            or not _number(rule["order_max_s"], integer=True, minimum=600, maximum=7200)
            or not _number(rule["book_max"], integer=True, minimum=1, maximum=200)
            or not _number(rule["graduation_guard_pct"], minimum=50, maximum=100)):
        raise ValueError("cohort_policy:chase_limit")
    return rule     # operator 2026-10-26: long-life standing limit orders (Gamma prereg 7e0e59d41)
PRICE_TRIGGERS = ("px_up", "px_hold")
LIMIT_WATCH_S = 60          # LIMIT arm (operator 2026-10-23, Gamma prereg 0529ce3c6): buy at the sheet price or better, 60 s


def validate_standing(policy) -> dict:
    """The cohort's frozen standing-order rule (operator 2026-10-23, watch v22): {"version", "watch_max_s",
    "graduation_guard_pct", "dead_tape_noise_k", "px_hold_max_noise", "px_hold_hold_s", "px_hold_max_wait_s"}. The px_hold
    shape follows Gamma's pullback read (2026-10-23): at most N x range5 deep, a time hold, a short wait. Optional, cohort-frozen:
    "noise_basis" ("swing" = within-bar median, the default for old cohorts; "true_range") and "drift_k" (fill-drift limit = k x
    the true-range noise; absent = no drift gate), "drift_enforce" (False = LOG-ONLY: the drift is traced, never refused; default True) and
    "limit_arm" (True: each ENTER ticket is flipped, by sha256(ticket_id), into a LIMIT or LOG arm), "drift_noise_floor_pct" (noise is max(noise, floor) so a dead-quiet coin cannot set a sub-fee limit; default 0). Malformed raises."""
    core = {"version", "watch_max_s", "graduation_guard_pct", "dead_tape_noise_k", "px_hold_max_noise", "px_hold_hold_s", "px_hold_max_wait_s"}
    v2 = isinstance(policy, dict) and policy.get("version") == STANDING_V2
    if v2:      # standing-v2 adds the order book: life (s) and live-order bound; never optional inside a v2 cohort
        core = core | {"order_max_s", "book_max"}
    if (not isinstance(policy, dict) or not core <= set(policy) or set(policy) - core - {"noise_basis", "drift_k", "drift_enforce", "drift_noise_floor_pct", "limit_arm"}
            or (v2 and (not _number(policy["order_max_s"], integer=True, minimum=600, maximum=7200)
                        or not _number(policy["book_max"], integer=True, minimum=1, maximum=200)))
            or policy.get("noise_basis", "swing") not in ("swing", "true_range")
            or ("drift_k" in policy and not _number(policy["drift_k"], minimum=0.5, maximum=20))
            or ("drift_enforce" in policy and type(policy["drift_enforce"]) is not bool)
            or ("limit_arm" in policy and type(policy["limit_arm"]) is not bool)
            or ("drift_noise_floor_pct" in policy and not _number(policy["drift_noise_floor_pct"], minimum=0, maximum=5))
            or policy["version"] not in (STANDING_VERSION, STANDING_V2)
            or not _number(policy["px_hold_max_noise"], minimum=0.5, maximum=10)
            or not _number(policy["px_hold_hold_s"], integer=True, minimum=1, maximum=60)
            or not _number(policy["px_hold_max_wait_s"], integer=True, minimum=10, maximum=600)
            or not _number(policy["watch_max_s"], integer=True, minimum=60, maximum=3600)
            or not _number(policy["graduation_guard_pct"], minimum=50, maximum=100)
            or not _number(policy["dead_tape_noise_k"], minimum=1, maximum=20)):
        raise ValueError("cohort_policy:standing_orders")
    return policy


def _plan(raw_plan):
    """{"inv": [kind, value], "h_s": int, "tz": [lo, hi]} or None when malformed. inv kind "px" = % vs entry
    (negative), "support" = the sheet's support level (value null)."""
    if not _shape(raw_plan, ("inv", "h_s", "tz")):
        return None
    inv, tz = raw_plan["inv"], raw_plan["tz"]
    if not (isinstance(inv, list) and len(inv) == 2
            and ((inv[0] == "px" and _number(inv[1], minimum=-60, maximum=-1)) or (inv[0] == "support" and inv[1] is None))):
        return None
    if not _number(raw_plan["h_s"], integer=True, minimum=60, maximum=1800):
        return None
    if not (isinstance(tz, list) and len(tz) == 2 and _number(tz[0], minimum=1, maximum=500)
            and _number(tz[1], minimum=1, maximum=500) and tz[0] <= tz[1]):
        return None
    return copy.deepcopy(raw_plan)


FORECAST_VERSION = "forecast-v1"
# Gamma's frozen grid (docs/audits/2026-10-23-forecast-grading-preregistration.md): an off-grid value voids the answer
FORECAST_U, FORECAST_D, FORECAST_H = (10, 20, 30, 50), (5, 10, 15, 20, 30), (60, 120, 300, 600, 900)
FORECAST_SCENARIOS = ("run", "grind", "chop", "fade", "dump")


def _forecast(raw_f):
    """{"u", "d", "h", "p", "t", "i", "s"} on the grid, else None. p in (0, 1) = P(the first post-swap price >= +u%
    before <= -d% within h s); t = expected high within h (% over ref); i = % under ref that kills the thesis.
    A record only: nothing decides on it until Gamma's bar passes."""
    if not _shape(raw_f, ("u", "d", "h", "p", "t", "i", "s")):
        return None
    grid = lambda v, allowed: type(v) is int and v in allowed
    if (not grid(raw_f["u"], FORECAST_U) or not grid(raw_f["d"], FORECAST_D) or not grid(raw_f["h"], FORECAST_H)
            or not _number(raw_f["p"]) or not 0 < raw_f["p"] < 1
            or not _number(raw_f["t"], minimum=-100, maximum=1000) or not _number(raw_f["i"], minimum=0, maximum=100)
            or raw_f["s"] not in FORECAST_SCENARIOS):
        return None
    return dict(raw_f)


def validate_verdict(raw, *, opportunity_id, bounds, timing_caps, elapsed_ms,
                     call_error=None, bars_available=False, deadline_s=None, standing=None,
                     forecast=False, reentry=None, watch=False) -> dict:
    """`deadline_s` = the deadline the call was made under (stamped with the call, so a replay under another setting cannot change
    the outcome); None = the current knob. forecast=True (a v23 cohort froze forecast-v1): every answer carries "f" and an
    off-grid or missing one voids it to a SKIP. reentry = {"min_px_up_pct": float | None} on a re-entry ticket: only a
    W px_hold, a W px_up above the post-exit high, or S (operator 2026-10-23: no enter-now on a re-entry)."""
    if (forecast or reentry is not None) and standing is None:
        raise ValueError("forecast and reentry need the standing grammar")
    if not _number(elapsed_ms, minimum=0):
        return _skip("invalid_elapsed_ms")
    limit = judge_deadline_s() if deadline_s is None else deadline_s
    if not _number(limit, minimum=0) or limit <= 0 or limit > JUDGE_DEADLINE_MAX_S:
        return _skip("invalid_deadline")
    if elapsed_ms > limit * 1000:
        return _skip("judge_timeout")
    if call_error is not None:
        return _skip("judge_" + call_error if call_error in JUDGE_CALL_ERRORS else "judge_error")
    try:
        timing_caps = validate_timing_caps(timing_caps)
    except ValueError:
        return _skip("timing_caps")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw, object_pairs_hook=_unique_object,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, TypeError, RecursionError):
            return _skip("malformed_json")
    if raw is None:
        return _skip("null_verdict")
    keys = ("k", "v", "e", "r", "c", "enter_ttl_s", "next_check_s")
    raw_f, has_f = (raw.get("f"), "f" in raw) if forecast and isinstance(raw, dict) else (None, False)
    if forecast and has_f:
        raw = {k: v for k, v in raw.items() if k != "f"}      # the forecast never decides the core verdict (prereg line 46)
    if not _shape(raw, keys + (("p",) if standing is not None else ())):
        return _skip("verdict_keys")
    if raw["k"] != opportunity_id:
        return _skip("key_mismatch")
    if raw["v"] not in (("E", "W", "H", "S") if standing is not None or watch else ("E", "W", "S")):
        return _skip("verdict_enum")
    if not _number(raw["c"], integer=True, minimum=1, maximum=16):
        return _skip("reason_code")
    code = raw["c"]
    result = _verdict_body(raw, code, bounds, timing_caps, bars_available, standing, reentry)
    if forecast:
        # record only (Gamma's forecast-grading prereg): an off-grid or missing f is stored UNGRADABLE with a hash of what came,
        # never rounded and never a veto of a valid core verdict. Kept on a SKIP too (selection skill).
        f = _forecast(raw_f) if has_f else None
        result["forecast"] = f if f is not None else {
            "ungradable": "missing" if not has_f else "off_grid",
            "raw_sha256": hashlib.sha256(json.dumps(raw_f, sort_keys=True, default=str).encode()).hexdigest() if has_f else None}
    return result


def _verdict_body(raw, code, bounds, timing_caps, bars_available, standing, reentry):
    if (not _number(raw["enter_ttl_s"], integer=True, minimum=3, maximum=timing_caps["enter_ttl_max_s"])
            or not _number(raw["next_check_s"], integer=True, minimum=5, maximum=timing_caps["boundary_max_s"])):
        return _skip("timing_out_of_bounds", code)
    if raw["v"] != "W" and raw["e"] is not None:
        return _skip("unexpected_condition", code)
    if reentry is not None and raw["v"] == "E":
        return _skip("reentry_enter_refused", code)
    if reentry is not None and raw["v"] == "H":
        return _skip("reentry_watch_refused", code)
    if raw["v"] in ("S", "H"):
        if raw["r"] is not None:
            return _skip("unexpected_rails", code)
        if standing is not None and raw["p"] is not None:
            return _skip("unexpected_plan", code)
        if raw["v"] == "H":            # WATCH: no setup yet; the ticket stays open and is re-judged on a meaningful change
            return {"action": "WATCH", "reason": "watch", "rails": None, "condition": None, "reason_code": code,
                    "enter_ttl_s": raw["enter_ttl_s"], "next_check_s": raw["next_check_s"], "plan": None}
        return {**_skip("model_skip", code), "enter_ttl_s": raw["enter_ttl_s"], "next_check_s": raw["next_check_s"]}
    plan = None
    if standing is not None:
        plan = _plan(raw["p"])
        if plan is None:
            return _skip("plan_out_of_range", code)
    rails = raw["r"]
    if (not isinstance(rails, list) or len(rails) != 3
            or not _number(rails[0]) or not _number(rails[1])
            or (rails[2] is not None and not _number(rails[2]))):
        return _skip("rails_out_of_bounds", code)
    rails = dict(zip(("tp_pct", "sl_pct", "trail_pct"), rails))
    try:
        valid_bounds = (isinstance(bounds, dict) and all(
            isinstance(v, (tuple, list)) and len(v) == 2 and _number(v[0]) and _number(v[1]) and v[0] <= v[1]
            for v in bounds.values()))
        ok, _ = validate_desk_rails(rails, bounds) if valid_bounds else (False, None)
    except (TypeError, ValueError, KeyError):
        ok = False
    if not ok:
        return _skip("rails_out_of_bounds", code)
    condition = raw["e"]
    if raw["v"] == "W" and standing is not None and isinstance(condition, list) and condition[:1] in (["px_up"], ["px_hold"]):
        # level in % of the current sheet price; code converts it to SOL/token from the frozen as-of reserves
        if (len(condition) != 5 or condition[1] != (">=" if condition[0] == "px_up" else "<=")
                or not _number(condition[2], minimum=-50, maximum=200)
                or (condition[0] == "px_up" and not condition[2] > 0) or (condition[0] == "px_hold" and not condition[2] < 0)
                or not _number(condition[3], integer=True, minimum=1, maximum=3)
                or not _number(condition[4], integer=True, minimum=10, maximum=standing["watch_max_s"])):
            return _skip("cond_out_of_range", code)
        if bars_available is not True:
            return _skip("cond_unavailable", code)
        if reentry is not None and condition[0] == "px_up":
            floor = reentry.get("min_px_up_pct")
            if not _number(floor):
                return _skip("cond_unavailable", code)        # unknown post-exit high: no breakout trigger
            if not condition[2] > floor:
                return _skip("reentry_below_post_exit_high", code)
    elif raw["v"] == "W" and standing is not None:
        return _skip("cond_unavailable", code)        # watch-v1 executes price triggers only (no bar executor)
    elif raw["v"] == "W":
        if (not isinstance(condition, list) or len(condition) != 5 or condition[0] not in ("bu", "tr")
                or condition[1] != ">=" or not _number(condition[2], minimum=50 if condition[0] == "bu" else 1,
                                                       maximum=5000 if condition[0] == "bu" else 50)
                or not _number(condition[3], integer=True, minimum=1, maximum=5)
                or not _number(condition[4], integer=True, minimum=10, maximum=timing_caps["wait_max_s"])):
            return _skip("cond_out_of_range", code)
        if bars_available is not True:
            return _skip("cond_unavailable", code)
    result = {"action": "ENTER" if raw["v"] == "E" else "WAIT",
              "reason": "enter" if raw["v"] == "E" else "wait", "rails": rails,
              "condition": copy.deepcopy(condition), "reason_code": code,
              "enter_ttl_s": raw["enter_ttl_s"], "next_check_s": raw["next_check_s"]}
    if standing is not None:
        result["plan"] = plan          # only standing cohorts carry the key: older results stay byte-identical
    return result


HARD_FILTER_VERSION = "hf2"       # hard = exit and pricing blockers only; cluster / holders / crawler are JUDGE facts
_CREATOR_BASIS = ("top20", "short_page", "below_page")
_SOFT_CHECK = {"pass": "pass", "kill": "flagged", "unknown": "unknown"}
_CLUSTER_KEYS_V17 = {"state", "check", "reason", "top_funder_high", "top_funder_pct_of_attempted",
                     "clustered_pct_of_attempted", "attempted_pct_of_supply"}      # desk-judge-v14..v17 sheets (replay of recorded rows)
_CLUSTER_KEYS = _CLUSTER_KEYS_V17 | {"excluded_known_funders_pct_of_attempted"}    # desk-judge-v18+
_LEGACY_CLUSTER_KEYS = {"state", "check", "reason", "top_funder_high", "top_funder_pct", "clustered_pct"}
_HOLDERS_KEYS = {"state", "check", "reason", "top10_pct", "creator_pct", "creator_basis"}


def _pct(value):
    return value if _number(value, minimum=0, maximum=100) else None


def _storable_row(row):
    """A soft evidence row must be a flat dict of JSON scalars so it can be stored; its MEANING is never judged here
    (soft_facts turns bad content into an unknown fact). A nested body or a non-finite number is refused as shape."""
    return isinstance(row, dict) and all(
        isinstance(k, str) and (v is None or type(v) in (bool, str) and len(str(v)) <= 256
                                or _number(v)) for k, v in row.items())


def _soft_row(name, row):
    """One soft check row -> its typed fact. Total: any shape, never raises, never a kill. A status that is not
    pass / kill / unknown, a reason that is not a machine slug, or a number out of range is simply unknown."""
    if not isinstance(row, dict):
        return {"check": "unknown", "reason": "not_yet_ready" if name == "cluster" else "missing"}
    status = row.get("status")
    check = _SOFT_CHECK.get(status) if isinstance(status, str) else None
    reason = row.get("reason")
    reason = reason if isinstance(reason, str) and _MACHINE.fullmatch(reason) else None
    if check is None:
        return {"check": "unknown", "reason": "malformed"}
    return {"check": check, "reason": reason if reason or check != "unknown" else "missing", "row": row}


def soft_facts(checks) -> dict:
    """JUDGE facts from the evidence checks (cluster, holders). Enum / number / machine-slug values only: no
    free text can pass (two-hop isolation). 'unknown' is a fact with its reason; a missing cluster row is
    'unknown:not_yet_ready'. Reported, never gated."""
    checks = checks if isinstance(checks, dict) else {}
    c = _soft_row("cluster", checks.get("cluster"))
    cluster_row = c.pop("row", {})
    if c["check"] == "unknown":
        cluster_row = {}
    cluster = {"state": "ready" if c["check"] != "unknown" else "unknown", "check": c["check"], "reason": c["reason"],
               "top_funder_high": c["check"] == "flagged" and c["reason"] == "cluster_top_funder_high",
               "top_funder_pct_of_attempted": _pct(cluster_row.get("top_funder_pct")),
               "clustered_pct_of_attempted": _pct(cluster_row.get("clustered_pct")),
               "attempted_pct_of_supply": _pct(cluster_row.get("attempted_pct_of_supply")),
               "excluded_known_funders_pct_of_attempted": _pct(cluster_row.get("excluded_known_funders_pct"))}
    h = _soft_row("holders", checks.get("holders"))
    holders_row = h.pop("row", {})
    top10 = creator = basis = None
    if h["check"] != "unknown" and holders_row.get("exclusions_verified") is not True:
        h = {"check": "unknown", "reason": "exclusions_unverified"}     # unverified exclusions: numbers withheld
    elif h["check"] != "unknown":
        top10, creator = _pct(holders_row.get("top10_pct")), _pct(holders_row.get("creator_pct"))
        basis = holders_row.get("creator_basis") if holders_row.get("creator_basis") in _CREATOR_BASIS else None
    holders = {"state": "ready" if h["check"] != "unknown" else "unknown", "check": h["check"], "reason": h["reason"],
               "top10_pct": top10, "creator_pct": creator, "creator_basis": basis}
    return {"cluster": cluster, "holders": holders}


def soft_shape(prompt_version) -> str:
    """Which closed cluster-fact shape a cohort's sheets carry: 'legacy' (v13), 'v17' (v14..v17: no excluded-funders field) or 'current'."""
    return ("legacy" if prompt_version == "desk-judge-v13" else
            "v17" if prompt_version in ("desk-judge-v14", "desk-judge-v15", "desk-judge-v16", "desk-judge-v17") else "current")


def validate_soft_facts(soft, *, allow_legacy=False, shape="current") -> dict:
    """The exact soft_facts() shape or ValueError('soft_facts'): a sheet cannot carry anything else."""
    try:
        if not isinstance(soft, dict) or set(soft) != {"cluster", "holders"}:
            raise ValueError
        cluster_keys = (_LEGACY_CLUSTER_KEYS if allow_legacy and isinstance(soft.get("cluster"), dict)
                        and set(soft["cluster"]) == _LEGACY_CLUSTER_KEYS else
                        _CLUSTER_KEYS_V17 if shape == "v17" else _CLUSTER_KEYS)
        for name, keys in (("cluster", cluster_keys), ("holders", _HOLDERS_KEYS)):
            row = soft[name]
            if (not isinstance(row, dict) or set(row) != keys or row["state"] not in ("ready", "unknown")
                    or row["check"] not in ("pass", "flagged", "unknown")
                    or (row["reason"] is not None and not (isinstance(row["reason"], str) and _MACHINE.fullmatch(row["reason"])))
                    or (row["state"] == "ready") != (row["check"] != "unknown")):
                raise ValueError
            for key in keys - {"state", "check", "reason", "top_funder_high", "creator_basis"}:
                if row[key] is not None and _pct(row[key]) is None:
                    raise ValueError
        if type(soft["cluster"]["top_funder_high"]) is not bool or soft["holders"]["creator_basis"] not in (None, *_CREATOR_BASIS):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise ValueError("soft_facts") from None
    return copy.deepcopy(soft)


DEFAULT_MCAP_BAND = (30000.0, 100000.0)


def mcap_band(policy) -> tuple:
    """The cohort's frozen admission band: hard_filter policy["mcap_band"] = [lo, hi] (operator 2026-10-23, 20k floor),
    else the original 30k-100k. A malformed band raises: a cohort is never admitted on a guessed band."""
    band = (policy or {}).get("mcap_band") if isinstance(policy, dict) else None
    if band is None:
        return DEFAULT_MCAP_BAND
    if (not isinstance(band, (list, tuple)) or len(band) != 2 or not all(_number(v, minimum=0) for v in band)
            or not 0 < band[0] < band[1]):
        raise ValueError("cohort_policy:mcap_band")
    return (float(band[0]), float(band[1]))


def claim_band(policy) -> tuple:
    """The claim-time band: the frozen admission band with its floor lowered by the cohort's
    mcap_band_claim_margin_pct (operator 2026-10-23: a coin that crossed the floor at emission and wobbled a little
    under it by the JUDGE claim still reaches JUDGE). Absent margin = the admission band; the hi bound never moves."""
    lo, hi = mcap_band(policy)
    margin = (policy or {}).get("mcap_band_claim_margin_pct") if isinstance(policy, dict) else None
    if margin is None:
        return (lo, hi)
    if not _number(margin, minimum=0, maximum=50):
        raise ValueError("cohort_policy:mcap_band_claim_margin_pct")
    return (lo * (1 - margin / 100.0), hi)


INTAKE_POLICY_VERSION = "intake-v1"
# Operator order B (2026-10-28): a cohort that freezes require_collected refuses an intake ticket whose holders,
# cluster or dump_group fact, or its chart facts (flow_60s, price_vs_60s high/low, age), is still unknown after the
# assembler's bounded wait. Absent key: today's soft rule.
INTAKE_COLLECTED = ("chart", "cluster", "dump_group", "holders")
# Operator 2026-10-29 (f7a006dd): the core list refuses only unknown chart/holders; an unknown dump_group or cluster stays a
# fact the JUDGE weighs. Only these two frozen lists are accepted.
INTAKE_COLLECTED_CORE = ("chart", "holders")
INTAKE_OPTIONAL_KEYS = ("feeds", "require_collected")
_INTAKE_METADATA_KEYS = frozenset({"base_decimals", "supply_atomic", "metadata_known_at", "metadata_slot", "metadata_source"})


def _intake_policy(policy):
    """The cohort's frozen intake bounds (operator 2026-10-27, brain 03c884f4), None for a band cohort, 'malformed'
    when present but wrong. Optional feeds freezes enrollment; absent preserves Pump.fun-only cohorts.
    It never coexists with mcap_band (market cap is a safety bound only)."""
    rule = policy.get("intake") if isinstance(policy, dict) else None
    if rule is None:
        return None
    keys = ("version", "mcap_safety", "liq_min_quote_sol")
    if (not isinstance(rule, dict) or not _shape(rule, (*keys, *(k for k in INTAKE_OPTIONAL_KEYS if k in rule)))
            or rule["version"] != INTAKE_POLICY_VERSION
            or "mcap_band" in policy or not isinstance(rule["mcap_safety"], (list, tuple))
            or len(rule["mcap_safety"]) != 2 or not all(_number(v, minimum=0) for v in rule["mcap_safety"])
            or not 0 < rule["mcap_safety"][0] < rule["mcap_safety"][1] or not _number(rule["liq_min_quote_sol"], minimum=0)):
        return "malformed"
    if "require_collected" in rule and rule["require_collected"] not in (list(INTAKE_COLLECTED), list(INTAKE_COLLECTED_CORE)):
        return "malformed"
    if "feeds" in rule:
        feeds = rule["feeds"]
        if (not isinstance(feeds, list) or not feeds or any(not isinstance(f, str) or f not in INTAKE_FEEDS for f in feeds)
                or feeds != sorted(set(feeds))):
            return "malformed"
    return rule


def intake_feeds(policy):
    """Validated frozen sources from hard_filter; old intake policies remain Pump.fun-only."""
    rule = _intake_policy(policy)
    if not isinstance(rule, dict):
        raise ValueError("cohort_policy:intake")
    return tuple(rule.get("feeds", ("pumpfun",)))


def _intake_bounds(candidate, evidence, rule):
    """None when the graduated coin is inside the intake safety bounds, else the SKIP reason. mcap = pool price x
    the mint account's own supply (never an assumed 1e9: other launchpads differ, Alpha 2026-10-27); liquidity = the
    pool's real SOL reserve x the snapshot's causal SOL/USD; the price uses the effective quote (real + proven
    virtual). Any missing or disagreeing input (supply, decimals, reserves, FX) refuses."""
    row = (candidate["snapshot"]["fields"].get("pool_state") or {})
    state = row.get("value") if row.get("completeness") in ("complete", "partial") else None
    if not isinstance(state, dict):
        return "unknown:market_data:pool_state"
    # a partial state (intake, no metadata: Alpha F3) may lack ONLY the metadata keys; supply/decimals then come from
    # the assembler's own mint read (token_safety) and nothing else may be unknown
    if row["completeness"] == "partial" and any(state.get(k) is None for k in _POOL_STATE - _INTAKE_METADATA_KEYS):
        return "unknown:market_data:pool_state"
    safety = evidence["token_safety"]
    supply, decimals = safety.get("supply_atomic"), safety.get("decimals")
    if supply is None or decimals is None:
        return "unknown:market_data:supply"            # Alpha: parsed supply AND decimals, both required
    if ((state.get("supply_atomic") is not None and state["supply_atomic"] != supply)
            or (state.get("base_decimals") is not None and state["base_decimals"] != decimals)):
        return "unknown:market_data:supply_mismatch"
    try:
        base, real, sol = int(state["base_reserve_atomic"]), int(state["quote_reserve_atomic"]), state["sol_usd"]
        virtual = int(state["virtual_quote_atomic"])   # the proven virtual quote: part of the PRICE (Delta)
    except (KeyError, TypeError, ValueError):
        return "unknown:market_data:reserves"
    if base <= 0 or real <= 0 or virtual < 0 or not _number(sol, minimum=0) or sol <= 0:
        return "unknown:market_data:reserves"
    mcap = (real + virtual) / 1e9 * sol * int(supply) / base   # price from the effective quote reserve
    lo, hi = rule["mcap_safety"]
    if not lo <= mcap <= hi:
        return "kill:mcap_safety"
    # liquidity floor in SOL, the real SOL in the vault (operator 2026-10-27 after Gamma: a 10k USD floor blocked every
    # pump graduation at ~85 SOL and admitted only coins already up ~38%; a SOL floor does not move with SOL/USD)
    if real / 1e9 < rule["liq_min_quote_sol"]:
        return "kill:liq_min"
    return None


def hard_filter(candidate, *, evidence: dict, policy: dict, evaluated_at=None) -> dict:
    """Consume already collected safety proofs. Unknown mandatory EXIT / PRICING dependencies refuse admission (hf2):
    token_safety, market_data, sell_probe. Cluster, holders and crawler warnings are JUDGE facts (soft_facts), never a
    kill, and an unknown soft fact never refuses admission."""
    def result(reason, gate=None, inputs=None):
        return {"action": "PASS" if reason in ("pass", "no_activity_after_fill") else "SKIP", "reason": reason,
                "gate_state": gate, "gate_inputs": inputs}
    try:
        candidate = validate_candidate(candidate)
    except ValueError as exc:
        return result("invalid_candidate:" + str(exc))
    intake = _intake_policy(policy)                     # None: today's band cohorts, unchanged
    if intake == "malformed":
        return result("unknown:policy:intake")
    if intake is not None and "intake" not in candidate:
        return result("unknown:candidate:not_intake")
    if intake is not None and candidate["intake"]["feed"] not in intake_feeds(policy):
        return result("kill:intake_feed_not_enrolled")
    if intake is None:
        mcap = render_judge_sheet(candidate)["mcap_usd"]
        if mcap is None:
            return result("unknown:market_data:mcap_usd")
        lo, hi = mcap_band(policy)
        if not lo <= mcap <= hi:
            return result("kill:mcap_band")
    source_version = policy.get("intake_source_version")
    if source_version is not None and source_version != "desk-intake-source-v1":
        return result("unknown:policy:intake_source")
    if source_version and (not isinstance(evidence, dict) or "intake_source" not in evidence):
        return result("unknown:intake_source:missing")
    evidence_keys = {"network_id", "mint", "pool", "snapshot_hash", "known_at", "checks"}
    if source_version:
        evidence_keys.add("intake_source")
        if isinstance(evidence, dict) and "market_cut" in evidence:
            evidence_keys.add("market_cut")
    if (not _shape(evidence, evidence_keys)
            or any(evidence[k] != candidate[k] for k in ("network_id", "mint", "pool"))
            or evidence["snapshot_hash"] != candidate["snapshot"]["hash"]):
        return result("unknown:evidence:identity")
    try:
        evaluated = _utc(candidate["emitted_at"] if evaluated_at is None else evaluated_at)
        known = _utc(evidence["known_at"])
    except ValueError:
        return result("unknown:evidence:timestamp")
    if known > evaluated or evaluated < candidate["snapshot"]["emission_at"]:
        return result("unknown:evidence:future")
    if source_version:
        from sniper.services.memecoin_desk_intake_proof import source_proof_refusal
        policy_hash = hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        refusal = source_proof_refusal(evidence["intake_source"], candidate, policy_hash=policy_hash, as_of=evaluated)
        if refusal is not None:
            return result("unknown:intake_source:" + refusal)
    market_cut = evidence.get("market_cut")
    evidence = evidence["checks"]
    allowed = {
        "market_data": {"status", "reason"},
        "token_safety": {"status", "reason", "authority_mint", "authority_freeze", "transfer_hook", "supply_atomic",
                         "decimals", "mint_context_slot"},
        "holders": {"status", "reason", "top10_pct", "creator_pct", "exclusions_verified", "creator_basis"},
        "cluster": {"status", "reason", "top_funder_pct", "clustered_pct"},
        "sell_probe": {"status", "reason", "round_trip_loss_pct", "exact_quantity"},
        "dump_group": set(),
        "chart": {"status", "reason"},
        "activity": {"count", "source_time", "evaluated_at", "known_at", "completeness", "whole_pool"}}
    if (not isinstance(evidence, dict) or set(evidence) - set(allowed)
            or any((not isinstance(row, dict) or set(row) - allowed[name]) if name not in ("holders", "cluster", "dump_group")
                   else not (isinstance(row, dict) if name == "dump_group" else _storable_row(row))
                   for name, row in evidence.items())):
        return result("unknown:evidence:shape")
    for name, row in evidence.items():
        if name in ("holders", "cluster", "dump_group"):
            continue                                    # soft: meaning is sanitized into a fact by soft_facts, never a refusal
        for key, value in row.items():
            if value is None:
                continue
            if key == "status":
                valid = isinstance(value, str) and value in ("pass", "kill", "unknown")
            elif key == "reason":
                valid = isinstance(value, str) and bool(_MACHINE.fullmatch(value))
            elif key in ("authority_mint", "authority_freeze"):
                valid = isinstance(value, str) and value in ("open", "closed")
            elif key == "transfer_hook":
                valid = _key(value, 32)
            elif key == "supply_atomic":
                valid = isinstance(value, str) and bool(re.fullmatch(r"[1-9][0-9]{0,29}", value))
            elif key == "decimals":
                valid = _number(value, integer=True, minimum=0, maximum=18)
            elif key == "mint_context_slot":
                valid = _number(value, integer=True, minimum=1)
            elif key in ("exclusions_verified", "exact_quantity", "whole_pool"):
                valid = type(value) is bool
            elif key == "completeness":
                valid = value in COMPLETENESS
            else:
                valid = _number(value, integer=key == "count", minimum=0 if key != "round_trip_loss_pct" else None,
                                maximum=100 if key in ("top10_pct", "creator_pct") else None)
            if not valid:
                return result("unknown:evidence:shape")
    from sniper.services.memecoin_desk_dump_group import checked_fact
    dump = checked_fact(evidence.get("dump_group"), mint=candidate["mint"], pool=candidate["pool"], now=evaluated)
    if dump["status"] == "kill":
        return result("dump_group_buyers", inputs={"dump_group": dump})
    activity = evidence.get("activity")
    if isinstance(activity, dict) and any(_number(activity.get(k)) and activity[k] > evaluated
                                          for k in ("known_at", "evaluated_at")):
        return result("unknown:evidence:future")
    for name in ("market_data", "token_safety", "sell_probe"):
        row = evidence.get(name)
        if not isinstance(row, dict):
            return result(f"unknown:{name}:missing")
        status = row.get("status")
        if status != "pass":
            reason = row.get("reason", "missing" if status == "unknown" else "malformed")
            if not isinstance(reason, str) or not _MACHINE.fullmatch(reason):
                reason = "malformed"
            return result(f"{'kill' if status == 'kill' else 'unknown'}:{name}:{reason}")
    if source_version:
        state = candidate["snapshot"]["fields"].get("pool_state", {}).get("value") or {}
        if (not _shape(market_cut, {"receipt_cut", "cut_slot", "block_known_at"})
                or market_cut["receipt_cut"] != candidate["snapshot"]["emission_at"]
                or not _number(market_cut["cut_slot"], integer=True, minimum=1)
                or not _number(state.get("slot"), integer=True, minimum=1)
                or market_cut["cut_slot"] <= state["slot"]
                or not _number(market_cut["block_known_at"], minimum=0, maximum=known)):
            return result("unknown:evidence:market_cut")
    if not isinstance(policy, dict):
        policy = {}
    if policy.get("version") != HARD_FILTER_VERSION:
        return result("unknown:policy:version")          # a pre-hf2 cohort fails closed, never open
    for key in ("round_trip_loss_pct_max",):
        if not _number(policy.get(key), minimum=0, maximum=100):
            return result(f"unknown:policy:{key}")
    allowlist = policy.get("transfer_hook_allowlist")
    if not isinstance(allowlist, (tuple, list)) or any(not _key(k, 32) for k in allowlist):
        return result("unknown:policy:transfer_hook_allowlist")
    safety = evidence["token_safety"]
    for key in ("authority_mint", "authority_freeze"):
        if safety.get(key) == "open":
            return result(f"kill:{key}")
        if safety.get(key) != "closed":
            return result(f"unknown:token_safety:{key}")
    if "transfer_hook" not in safety:
        return result("unknown:token_safety:transfer_hook")
    if safety["transfer_hook"] is not None and safety["transfer_hook"] not in allowlist:
        return result("kill:transfer_hook")
    probe = evidence["sell_probe"]
    if probe.get("exact_quantity") is not True:
        return result("unknown:sell_probe:inexact_quantity")
    if not _number(probe.get("round_trip_loss_pct")):
        return result("unknown:sell_probe:round_trip_loss_pct")
    if probe["round_trip_loss_pct"] > policy["round_trip_loss_pct_max"]:
        return result("kill:round_trip_loss_pct")
    if intake is not None:
        why = _intake_bounds(candidate, evidence, intake)
        if why is not None:
            return result(why)
    sheet = render_judge_sheet(candidate)
    flow = sheet["flow_60s"]
    if candidate["producer"] == "U" and flow is None and intake is None:   # intake: flow is a JUDGE fact, not a kill
        return result("kill:flow_missing_universe")
    if flow and flow["buy_n"] is not None and flow["sell_n"] is not None and flow["buy_n"] >= 20 and flow["sell_n"] == 0:
        return result("kill:buys_without_sells")
    if intake is not None and "require_collected" in intake:
        for name in intake["require_collected"]:        # order B: after every hard kill, so a kill stays a kill, never passed as unknown
            row = dump if name == "dump_group" else evidence.get(name)
            status = row.get("status") if isinstance(row, dict) else None
            if status not in ("pass", "kill"):
                reason = row.get("reason") if isinstance(row, dict) else "missing"
                if not isinstance(reason, str) or not _MACHINE.fullmatch(reason):
                    reason = "malformed"
                return result(f"unknown:{name}:{reason}")
    activity = evidence.get("activity")
    if candidate["producer"] == "H" and isinstance(activity, dict):
        source = candidate["roster_buy"]["source_time"]
        if (activity.get("completeness") == "complete" and activity.get("whole_pool") is True
                and activity.get("source_time") == source and _number(activity.get("count"), integer=True, minimum=0)
                and _number(activity.get("evaluated_at")) and _number(activity.get("known_at"))
                and source + 5 <= activity["known_at"] <= activity["evaluated_at"] <= evaluated):
            inputs = {"count": activity["count"], "source_time": source,
                      "gate_evaluated_at": activity["evaluated_at"]}
            return result("no_activity_after_fill" if activity["count"] <= 1 else "pass",
                          "skip" if activity["count"] <= 1 else "pass", inputs)
    return result("pass", inputs={"dump_group": dump})


def build_h_candidate(*, receipt, facts, mint, wallet, tracked, armed, commitment, pool,
                      trigger_ordinal, emitted_at, receipt_known_at, fields) -> dict:
    """Build H only from a tracked unarmed wallet's successful, already-confirmed paid buy."""
    emitted = _utc(emitted_at)
    if (tracked is not True or armed is not False or commitment not in ("confirmed", "finalized")
            or not _key(wallet, 32) or not _key(mint, 32) or not isinstance(facts, dict)
            or facts.get("mint") != mint or not isinstance(receipt, dict)):
        raise ValueError("receipt_identity")
    proof = solana_receipt_initiation(receipt, wallet, facts)
    if (not proof or not _number(receipt.get("slot"), integer=True, minimum=1)
            or not _key(proof.get("tx_hash"), 64) or proof.get("mint") != mint
            or facts.get("signature") != proof["tx_hash"]
            or not _number(facts.get("source_slot"), integer=True, minimum=1)
            or not _number(receipt.get("blockTime"), minimum=0)
            or not _number(receipt_known_at, minimum=0)
            or not receipt["blockTime"] <= receipt_known_at <= emitted):
        raise ValueError("receipt_identity")
    key = f"solana|{mint}|{receipt['slot']}|{proof['tx_hash']}|{trigger_ordinal}"
    if (not isinstance(fields, dict) or any(not isinstance(f, Field) or not _number(f.known_at, minimum=0)
                                           for f in fields.values())):
        raise ValueError("field_provenance")
    snapshot = build_snapshot(emission_at=emitted, fields=fields)
    candidate = {"schema": "desk-candidate/v1", "ticket_id": "desk:H:" + hashlib.sha256(key.encode()).hexdigest()[:24],
        "producer": "H", "network_id": "solana", "mint": mint, "pool": pool, "band_entry_slot": receipt["slot"],
        "trigger_signature": proof["tx_hash"], "trigger_ordinal": trigger_ordinal, "emitted_at": emitted_at,
        "gate_state": None, "snapshot": snapshot,
        "roster_buy": {"wallet": wallet, "signature": proof["tx_hash"], "slot": receipt["slot"],
            "source_time": receipt["blockTime"], "known_at": receipt_known_at, "payment_basis": facts.get("basis"),
            "payment_atomic": str(proof["payment_atomic"]), "acquired_atomic": str(proof["acquired_atomic"]),
            "commitment": commitment}}
    return validate_candidate(candidate)
