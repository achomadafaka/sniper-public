"""Who decided a funded entry, and which gates that decision-maker answers to.

Spec: docs/superpowers/specs/2026-10-21-desk-execution-plumbing-design.md §1.5.

Two non-copy origins exist. Both skip the SELECTION gates (chase, source-buy floor, first-buy
window, band floor, sell-share, ...), because the operator or the desk seat IS the decision and
those gates only mean something when copying somebody. They differ on CAPACITY: a manual buy is
the operator's own call and is exempt from concurrency, quiet hours and the min-trade floor
(operator 2026-08-27); a desk buy is automated and keeps every one of them, plus its own book
limits below. Safety gates (kill switch, token safety, impact, bankroll) bind both.

Zero dependencies at import: funded legs, risk and persistence all import this.
"""
from __future__ import annotations

import math
import re
from datetime import datetime

MANUAL = "manual"
DESK = "desk"
SELECTION_BYPASS_SOURCES = frozenset({MANUAL, DESK})


def bypasses_selection(signal_source) -> bool:
    return str(signal_source or "") in SELECTION_BYPASS_SOURCES


def enforces_capacity(signal_source) -> bool:
    return str(signal_source or "") != MANUAL


VERDICT_PRICE_KEYS = frozenset({"ref_price_usd", "mcap_usd", "liq_usd", "supply_tokens", "clip_usd",
                                 "tolerance_pct", "fee_pct", "impact_pct", "cap_pct", "cap_price_usd",
                                 "snapshot_at", "source", "basis"})


DRIFT_KEYS = frozenset({"drift_k", "noise_pct", "drift_limit_pct", "drift_enforce"})      # optional, all together (fill-drift gate; enforce 0 = log only)


def validate_verdict_price(vp):
    """(True, None) for a well-formed desk verdict price cap (memecoin_desk.verdict_price), else why."""
    if type(vp) is not dict or set(vp) - {"limit_arm"} not in (VERDICT_PRICE_KEYS, VERDICT_PRICE_KEYS | DRIFT_KEYS):
        return False, "verdict_price keys"
    if "limit_arm" in vp and (type(vp["limit_arm"]) is not int or vp["limit_arm"] not in (0, 1)):
        return False, "verdict_price limit_arm"
    for k in set(vp) - {"source", "basis", "limit_arm"}:
        v = vp[k]
        try:
            bad = type(v) not in (int, float) or not math.isfinite(v) or v < 0
        except OverflowError:                       # a giant int: malformed, never an exception (Codex)
            bad = True
        if bad:
            return False, f"verdict_price {k}"
    if vp["ref_price_usd"] <= 0 or vp["cap_pct"] <= 0:
        return False, "verdict_price ref/cap"
    if DRIFT_KEYS <= set(vp):
        if vp["drift_enforce"] not in (0, 1) or min(vp[k] for k in DRIFT_KEYS - {"drift_enforce"}) <= 0 or abs(vp["drift_limit_pct"] - vp["drift_k"] * vp["noise_pct"]) > 1e-9 * vp["drift_limit_pct"]:
            return False, "verdict_price drift"
    if abs(vp["cap_price_usd"] - vp["ref_price_usd"] * (1 + vp["cap_pct"] / 100)) > 1e-12 * vp["cap_price_usd"]:
        return False, "verdict_price cap_price"
    if vp["basis"] not in ("asof", "emission", "trigger"):        # trigger: a fired watch-v22 standing order
        return False, "verdict_price basis"
    if not isinstance(vp["source"], str) or not vp["source"]:
        return False, "verdict_price source"
    return True, None


def validate_desk_metadata(meta):
    """Validate immutable desk provenance without coercing or defaulting any identity. The optional
    verdict_price (S6) must be well formed when present; the executor refuses a desk entry without it."""
    if type(meta) is not dict or set(meta) - {"verdict_price", "standing_order", "plan", "time_stop_s", "min_others_60s", "admission_proof", "crowd_proof", "history_basis", "known_group_proof", "entry_deadline_utc", "known_group_policy_hash", "known_group_cut_host"} != {
            "cohort_id", "producer", "opportunity_id", "snapshot_hash"}:
        return False, "desk metadata needs exactly cohort_id, producer, opportunity_id, snapshot_hash"
    if ("standing_order" in meta) != (meta.get("verdict_price", {}).get("basis") == "trigger"):
        return False, "desk metadata standing_order must come with a trigger-basis verdict_price"
    if "standing_order" in meta:
        so = meta["standing_order"]
        level = so.get("level_price_sol") if type(so) is dict else None
        if (type(so) is not dict or set(so) != {"kind", "fire", "level_price_sol"} or so["kind"] not in ("px_up", "px_hold", "px_limit")
                or type(level) not in (int, float) or not math.isfinite(level) or level <= 0 or type(so["fire"]) is not dict):
            return False, "desk metadata standing_order"
    if meta.get("cohort_id") == "large-buyer-crowd-v1":
        if meta.get("history_basis") != "observed_tape":
            return False, "crowd policy requires explicit observed tape history basis"
        if type(meta.get("min_others_60s")) is not int or meta["min_others_60s"] != 1:
            return False, "crowd policy requires a frozen minimum of one prior qualifying buyer"
        if not _crowd_proof_valid(meta.get("admission_proof")):
            return False, "crowd admission proof invalid"
        if not _crowd_source_valid(meta.get("crowd_proof")):
            return False, "crowd causal proof invalid"
    elif "min_others_60s" in meta or "admission_proof" in meta or "crowd_proof" in meta or "history_basis" in meta:
        return False, "crowd policy field on another cohort"
    known_group = meta.get('cohort_id') == 'known-group-launch-v1'
    if known_group:
        from sniper.services.memecoin_known_group_canary import _candidate_identity, _stamp, _json, _number, POLICY, POLICY_HASH
        import hashlib
        try:
            proof = meta['known_group_proof']
            if (meta['producer'] != 'H' or meta.get('time_stop_s') != POLICY['time_stop_s']
                    or meta['known_group_policy_hash'] != POLICY_HASH
                    or _number(meta['known_group_cut_host']) < proof['creation']['received_at_host']
                    or proof.get('cohort_id') != 'known-group-launch-v1'
                    or meta['snapshot_hash'] != hashlib.sha256(_json(proof).encode()).hexdigest()
                    or (_stamp(meta['entry_deadline_utc']) - _stamp(proof['creation']['received_at_iso'])).total_seconds() != POLICY['entry_ttl_s']):
                return False, 'known group metadata invalid'
            _candidate_identity(proof)
        except (ValueError,TypeError,KeyError,AttributeError):
            return False, 'known group metadata invalid'
    elif any(k in meta for k in ('known_group_proof','entry_deadline_utc','known_group_policy_hash','known_group_cut_host')):
        return False, 'known group field on another cohort'
    if "time_stop_s" in meta:          # the large-buyer pilot only: frozen into the contract's max hold
        ts = meta["time_stop_s"]
        if not (is_large_buyer(meta) or known_group) or type(ts) not in (int, float) or not math.isfinite(ts) or ts <= 0:
            return False, "desk metadata time_stop_s"
    if "plan" in meta and (type(meta["plan"]) is not dict or set(meta["plan"]) != {"inv", "h_s", "tz"}):
        return False, "desk metadata plan"
    if "verdict_price" in meta:
        ok, why = validate_verdict_price(meta["verdict_price"])
        if not ok:
            return False, f"desk metadata {why}"
    for name in ("cohort_id", "opportunity_id"):
        value = meta[name]
        if (not isinstance(value, str) or not 0 < len(value) <= 200
                or re.fullmatch(r"[A-Za-z0-9_.:/-]+", value) is None):
            return False, f"desk metadata {name} must be a bounded identifier"
    if meta["producer"] not in ("H", "U"):
        return False, "desk metadata producer must be H or U"
    digest = meta["snapshot_hash"]
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        return False, "desk metadata snapshot_hash must be 64 lowercase hex characters"
    return True, None


LARGE_BUYER_COHORT_PREFIX = "large-buyer-"


def _crowd_source_valid(proof):
    fields = {"crowd_others_60s", "source_slot", "source_tx_index", "source_ordinal",
              "source_signature", "detect_lag_s"}
    if type(proof) is not dict or set(proof) != fields:
        return False
    if any(type(proof[k]) is not int or proof[k] <= 0
           for k in ("crowd_others_60s", "source_slot", "source_tx_index")):
        return False
    return (type(proof["source_ordinal"]) is int and proof["source_ordinal"] >= 0
            and isinstance(proof["source_signature"], str) and 0 < len(proof["source_signature"]) <= 128
            and type(proof["detect_lag_s"]) in (int, float) and math.isfinite(proof["detect_lag_s"])
            and 0 <= proof["detect_lag_s"] <= 5)


def _crowd_proof_valid(proof):
    fields = {"mint", "decimals", "metadata_slot", "metadata_known_at_host", "metadata_source",
              "token_program", "entry_due_host", "entry_deadline_utc", "clock_domain"}
    if type(proof) is not dict or set(proof) != fields:
        return False
    if (not isinstance(proof["mint"], str) or not 0 < len(proof["mint"]) <= 128
            or type(proof["decimals"]) is not int or not 0 <= proof["decimals"] <= 18
            or type(proof["metadata_slot"]) is not int or proof["metadata_slot"] <= 0
            or proof["metadata_source"] != "helius_research:get_account_info:json_parsed"
            or proof["token_program"] not in ("spl-token", "spl-token-2022")
            or proof["clock_domain"] != "corrected_utc"):
        return False
    if any(type(proof[k]) not in (int, float) or not math.isfinite(proof[k]) or proof[k] < 0
           for k in ("metadata_known_at_host", "entry_due_host")):
        return False
    try:
        deadline = datetime.fromisoformat(proof["entry_deadline_utc"].replace("Z", "+00:00"))
        return deadline.utcoffset() is not None and deadline.utcoffset().total_seconds() == 0
    except (ValueError, TypeError, AttributeError, OverflowError):
        return False


def crowd_admission_refusal(meta, *, mint, now_iso, now_host):
    """Local-only proof binding before a CROWD entry; other desk policies stay unchanged."""
    if not isinstance(meta, dict) or meta.get("cohort_id") != "large-buyer-crowd-v1":
        return None
    if not validate_desk_metadata(meta)[0]:
        return "crowd_metadata_invalid"
    proof = meta["admission_proof"]
    if proof["mint"] != mint:
        return "crowd_mint_mismatch"
    if type(now_host) not in (int, float) or not math.isfinite(now_host) or proof["metadata_known_at_host"] > now_host:
        return "crowd_metadata_future"
    if now_host < proof["entry_due_host"]:
        return "crowd_entry_not_due"
    try:
        now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        deadline = datetime.fromisoformat(proof["entry_deadline_utc"].replace("Z", "+00:00"))
        if now.utcoffset() is None or now >= deadline:
            return "crowd_entry_expired"
    except (ValueError, TypeError, AttributeError, OverflowError):
        return "crowd_entry_expired"
    return None


def is_large_buyer(meta) -> bool:
    return isinstance(meta, dict) and str(meta.get("cohort_id") or "").startswith(LARGE_BUYER_COHORT_PREFIX)


def desk_lane_open(meta) -> bool:
    """The LLM desk and the large-buyer pilot share one queue and one book, each behind its own knob:
    the pilot's knob never opens an LLM desk entry, and the desk's never opens a pilot entry."""
    import sniper.settings.memecoin as cfg
    if isinstance(meta, dict) and meta.get('cohort_id') == 'known-group-launch-v1':
        # Durable policy + candidate checks are mandatory at enqueue, drain, runner and reservation.
        return bool(cfg.MEMECOIN_REAL_TOKEN_SAFETY_ENABLED and validate_desk_metadata(meta)[0])
    if is_large_buyer(meta):
        return bool(cfg.MEMECOIN_LARGE_BUYER_ENABLED and cfg.MEMECOIN_REAL_TOKEN_SAFETY_ENABLED)
    return bool(cfg.MEMECOIN_DESK_ENABLED)


def desk_book_refusal(conn, *, now_iso, desk_metadata=None):
    """The desk's own limits, read before any entry work. None = within limits.

    Both knobs are operator-tuned (ruling 61859b83). They sit ON TOP of the global caps, which the
    funded leg still enforces for a desk entry. Realized PnL counts from 00:00 UTC; an open or
    unresolved desk position is not a realized loss and is not guessed at here. The halt latches
    per UTC day in memecoin_desk_loss_trips. The cap is enforced again inside reserve_pending.
    """
    import sniper.settings.memecoin as cfg
    from sniper.persistence import db_memecoin as m
    ok, why = validate_desk_metadata(desk_metadata)
    if not ok:
        return f"desk_metadata_invalid: {why}"
    refusal = m.desk_cohort_refusal(conn, cohort_id=desk_metadata["cohort_id"],
                                    producer=desk_metadata["producer"])
    if refusal:
        return refusal
    limit = int(cfg.MEMECOIN_DESK_MAX_CONCURRENT or 0)
    if limit <= 0 or m.desk_open_count(conn) >= limit:
        return "desk_max_concurrent"
    halt = float(cfg.MEMECOIN_DESK_DAILY_LOSS_USD or 0.0)
    day = str(now_iso)[:10]
    if halt <= 0 or m.desk_loss_latched(conn, day):
        return "desk_daily_loss"
    realized = m.desk_realized_pnl_since(conn, day + "T00:00:00+00:00")
    if realized <= -halt:
        # LATCHED (Codex audit #3): a later winner, or a restart, never reopens the day.
        m.latch_desk_loss(conn, day, realized_usd=realized, limit_usd=halt, tripped_at=str(now_iso))
        return "desk_daily_loss"
    return None
