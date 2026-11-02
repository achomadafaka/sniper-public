"""Read-only shadows for funded memecoin entry traces."""
from __future__ import annotations

import json
import math

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_origin import bypasses_selection


SHADOW_VERSION = "funded_entry_shadow_v1"


def _finite(value, *, positive=False):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or (positive and number <= 0.0):
        return None
    return number


def _unknown(reason):
    return {"state": "unknown", "reason": reason}


def _attempt(conn, attempt_id):
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        return None
    row = conn.execute(
        "SELECT signal_source,network_id,mint,source_user_id,decision_started_at,classification_json,"
        "exemptions_applied "
        "FROM memecoin_entry_attempts WHERE attempt_id=?",
        (attempt_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _classification(attempt):
    try:
        value = json.loads(attempt.get("classification_json") or "")
    except (TypeError, ValueError, AttributeError):
        return {}
    return value if isinstance(value, dict) else {}


def _small_band_shadow(attempt):
    if bypasses_selection(attempt.get("signal_source")):
        return {"state": "not_applicable"}
    band = _classification(attempt).get("coin_band")
    if not isinstance(band, str) or not band:
        return _unknown("absent_classification")
    return {"state": "would_block" if band == "small" else "would_allow", "coin_band": band}


def _prior_shadow(conn, attempt, current_position_id, *, exclude_mint=False):
    if bypasses_selection(attempt.get("signal_source")):
        return {"state": "not_applicable"}
    source_user_id = attempt.get("source_user_id")
    network_id = attempt.get("network_id")
    decision_at = attempt.get("decision_started_at")
    if not all(isinstance(value, str) and value.strip()
               for value in (source_user_id, network_id, decision_at)):
        return _unknown("absent_identity")
    try:
        current_id = int(current_position_id) if current_position_id is not None else -1
    except (TypeError, ValueError):
        return _unknown("invalid_current_position_id")
    if exclude_mint and not str(attempt.get("mint") or "").strip():
        return _unknown("absent_target_mint")
    rows = m.funded_entry_prior_rows(
        conn, source_user_id=source_user_id, network_id=network_id,
        decision_at=decision_at, current_position_id=current_id,
        exclude_mint=attempt.get("mint") if exclude_mint else None, strict_copy=exclude_mint,
    )
    eligible = []
    for row in rows:
        pnl = _finite(row["realized_pnl"])
        notional = _finite(row["entry_notional"], positive=True)
        if pnl is not None and notional is not None:
            eligible.append((pnl, notional))
    pnl_total = sum(row[0] for row in eligible)
    notional_total = sum(row[1] for row in eligible)
    result = {
        "prior_closed_n": len(eligible),
        "prior_realized_pnl": pnl_total,
        "prior_entry_notional": notional_total,
        "prior_roi_pct": (pnl_total / notional_total * 100.0 if notional_total else None),
        "as_of_decision_at": decision_at,
    }
    if len(eligible) < 5:
        return {"state": "insufficient_history", **result}
    return {"state": "would_block" if pnl_total <= 0.0 else "would_allow", **result}


def _volume_evaluations(event):
    """Describe the recorded volume call without another decision."""
    active = event.get("active") is True
    scope = "out_of_scope" if _finite(event.get("mcap"), positive=True) is not None else "unknown"
    if not (_finite(event.get("boundary")) or 0) > 0:
        scope = "disabled"
    evaluations = {}
    # The caller disables the in-band floor and ceiling below their own boundary.
    for name, key in (("m5_turnover_over_ceiling", "configured_ceiling"),
                      ("m5_turnover_under_floor", "configured_m5_floor")):
        ratio = _finite(event.get(key)) or 0.0
        evaluations[name] = {"state": "out_of_scope" if ratio > 0 else "disabled",
                             "configured_ratio": ratio, "effective_ratio": 0.0,
                             "boundary_mcap": event.get("boundary")}
    reason = event.get("reason")
    if event.get("allowed") is False and reason in evaluations:
        # A recorded refusal proves the gate ran, even when the old scope flag says otherwise.
        threshold = _finite(event.get("required_usd"), positive=True)
        mcap = _finite(event.get("mcap"), positive=True)
        evaluations[reason].update({
            "state": "fail", "input": _finite(event.get("volume_m5")),
            "threshold": threshold,
            "effective_ratio": threshold / mcap if threshold is not None and mcap else None,
        })
    for name, input_key, threshold_key in (
            ("m5_txns_under_floor", "txns", "txns_floor"),
            ("m5_turnover_under_big_floor", "volume_m5", "floor_over_band"),
            ("turnover_under_floor", "volume_h1", "h1_floor")):
        threshold = _finite(event.get(threshold_key)) or 0.0
        value = _finite(event.get(input_key))
        if not active:
            state = scope
        elif threshold <= 0:
            state = "disabled"
        elif (reason == name
              or (name == "m5_turnover_under_big_floor" and reason == "m5_volume_unreadable")
              or (name == "m5_txns_under_floor" and reason == "m5_txns_unreadable")):
            state = "fail"
        elif value is None:
            state = "unknown"
        elif event.get("allowed") is True or (name == "m5_txns_under_floor" and value >= threshold):
            state = "pass"
        else:
            state = "unknown"
        evaluations[name] = {"state": state, "input": value, "threshold": threshold,
                             "boundary_mcap": event.get("boundary"),
                             "source_volume_excluded_usd": event.get("source_volume_excluded_usd")}
    if event.get("h1_scope") == "all_market_caps":
        # Only new traces declare this scope. Historical traces retain their band scope.
        ratio = _finite(event.get("h1_floor"), positive=True) or 0.0
        absolute = _finite(event.get("h1_abs_floor"), positive=True) or 0.0
        mcap = _finite(event.get("mcap"), positive=True)
        relative = bool(ratio and mcap is not None)
        threshold = mcap * ratio if relative else absolute
        if ratio and mcap is None and not absolute:
            threshold = None
        raw = _finite(event.get("volume_h1"))
        excluded = _finite(event.get("source_volume_excluded_usd"), positive=True) or 0.0
        value = raw
        if raw is not None:
            # The relative leg excludes the source only when the observed volume can contain it.
            # The absolute fallback retains its existing subtraction rule.
            value = raw if relative and raw < excluded else max(0.0, raw - excluded)
        if threshold is not None and threshold <= 0:
            state = "disabled"
        elif threshold is None or value is None:
            state = "unknown"
        elif reason in ("turnover_under_floor", "under_abs_floor") and event.get("allowed") is False:
            state = "fail"
        elif event.get("allowed") is True:
            state = "pass"
        else:
            # An earlier m5 refusal does not prove an h1 result.
            state = "unknown"
        evaluations["turnover_under_floor"] = {
            "state": state, "input": value, "raw_input": raw, "threshold": threshold,
            "units": "USD", "scope": "all_market_caps", "configured_ratio": ratio,
            "configured_abs_floor": absolute, "source_volume_excluded_usd": excluded,
        }
    waived = event.get("waived_m5_reason")
    if waived:
        # The final allowed value describes the h1 retry, not the original m5 call.
        for name, evidence in evaluations.items():
            if name.startswith("m5") and evidence["state"] == "pass":
                evidence["state"] = "unknown"
        family = {"m5_volume_unreadable": "m5_turnover_under_floor",
                  "m5_txns_unreadable": "m5_txns_under_floor"}.get(waived, waived)
        if family in evaluations:
            evaluations[family]["state"] = "bypassed"
    return evaluations


def _gate_evaluations(entry_context):
    """Read actual trace observations. Completion alone proves no gate result."""
    events = (entry_context or {}).get("gate_events") or []
    evaluations = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        name = event.get("name")
        state = "unknown"
        if name == "volume_gate":
            evaluations.update(_volume_evaluations(event))
            evaluations.update(event.get("gate_evaluations") or {})
            continue
        if name == "source_buy_floor":
            if event.get("rejected") is True:
                state = "fail"
            elif _finite(event.get("floor_usd"), positive=True) is not None:
                state = "pass" if event.get("rejected") is False else "unknown"
            else:
                state = event.get("inactive_state", "unknown")
        elif name == "sell_share":
            cap = _finite(event.get("cap_pct"))
            total = _finite(event.get("txns"))
            minimum = _finite(event.get("min_txns"))
            if cap is not None and cap <= 0:
                state = "disabled"
            elif event.get("allowed") is False:
                state = "fail"
            elif (_finite(event.get("share_pct")) is not None and total is not None
                  and minimum is not None and total >= minimum):
                state = "pass" if event.get("allowed") is True else "unknown"
        elif name == "execution_premium":
            if event.get("reason") == "cap_off":
                state = "disabled"
            elif event.get("allow") is False:
                state = "fail"
            elif event.get("reason") == "ok" and event.get("allow") is True:
                state = "pass"
        elif name == "chase_quote_skipped":
            name = "chase_past_entry"
            state = "disabled" if event.get("reason") == "cap_off" else "unknown"
        elif name == "chase_quote_confirmation":
            name = "chase_past_entry"
            cap = _finite(event.get("cap_pct"))
            gap = _finite(event.get("gap_pct"))
            if cap is not None and cap <= 0:
                state = "disabled"
            elif cap is not None and gap is not None:
                state = "pass" if gap <= cap else "fail"
        elif name == "chase_first_seen":
            cap = _finite(event.get("cap_ratio"))
            if cap is not None and cap <= 0:
                state = "disabled"
            elif event.get("allowed") is False:
                state = "fail"
            elif (event.get("allowed") is True
                  and _finite(event.get("first_price"), positive=True) is not None
                  and _finite(event.get("price_now"), positive=True) is not None):
                state = "pass"
        else:
            continue
        evaluations[name] = {**event, "state": state}
    return evaluations


def _selection_gate_states(attempt, result, entry_context):
    manual = bypasses_selection(attempt.get("signal_source"))
    configured = {value.strip().rstrip("?") for value in
                  str(attempt.get("exemptions_applied") or "").split(",") if value.strip()}
    evaluations = _gate_evaluations(entry_context)
    from sniper.services.memecoin_refusal_capture import classify_gate
    terminal_gate = classify_gate((result or {}).get("reason"))
    families = m.EXEMPTIBLE_GATE_FAMILIES | set(evaluations)
    if terminal_gate:
        families = families | {terminal_gate}
    states = {}
    for family in sorted(families):
        if (manual and family in m.EXEMPTIBLE_GATE_FAMILIES) or family in configured:
            states[family] = "bypassed"
        elif terminal_gate == family:
            states[family] = "fail"
        else:
            states[family] = evaluations.get(family, {}).get("state", "unknown")
    return states


def _gate_provenance(attempt, result, entry_context):
    context = entry_context if isinstance(entry_context, dict) else {}
    facts = _classification(attempt).get("facts")
    facts = facts if isinstance(facts, dict) else {}
    source_size = _finite(context.get("source_size_usd"))
    candidate_clip = _finite(context.get("candidate_clip_usd"), positive=True)
    effective_mcap = _finite(facts.get("effective_mcap"), positive=True)
    raw_m5 = _finite(facts.get("volume_m5"))
    organic_m5 = max(0.0, raw_m5 - source_size) if raw_m5 is not None and source_size is not None else None
    return {
        "inputs": {
            "coin_band": _classification(attempt).get("coin_band"),
            "source_size_usd": source_size,
            "candidate_clip_usd": candidate_clip,
            "effective_mcap_usd": effective_mcap,
            "raw_m5_volume_usd": raw_m5,
            "organic_m5_volume_usd": organic_m5,
        },
        "schema_version": 2,
        "selection_gate_states": _selection_gate_states(attempt, result, context),
        "gate_evaluations": _gate_evaluations(context),
    }


def _threshold_shadow(attempt, provenance, *, networks, input_name, threshold):
    if bypasses_selection(attempt.get("signal_source")):
        return {"state": "not_applicable"}
    if str(attempt.get("network_id") or "") not in networks:
        return {"state": "not_applicable"}
    value = provenance["inputs"].get(input_name)
    if value is None:
        return _unknown("absent_input")
    return {
        "state": "would_allow" if value >= threshold else "would_block",
        input_name: value,
        "threshold_usd": threshold,
    }


def _band_shadow(attempt, provenance, *, networks, input_name, floor, ceiling_exclusive):
    if bypasses_selection(attempt.get("signal_source")):
        return {"state": "not_applicable"}
    if str(attempt.get("network_id") or "") not in networks:
        return {"state": "not_applicable"}
    value = provenance["inputs"].get(input_name)
    if value is None:
        return _unknown("absent_input")
    return {
        "state": "would_allow" if floor <= value < ceiling_exclusive else "would_block",
        input_name: value,
        "floor_usd": floor,
        "ceiling_usd_exclusive": ceiling_exclusive,
    }


def _conjunction_shadow(*sub_shadows):
    """AND several shadow states, never guessing past a state either side does not know.

    docs/audits/2026-09-08-entry-knob-winner-evidence.md: turnover 10-50% AND source buy
    $1,000-$10,000. The rule was found by searching the same data it was scored on -- this
    records what it WOULD have done on every future funded decision, admission unchanged, so a
    pre-registered forward read can judge it on data the search never touched."""
    states = [s.get("state") for s in sub_shadows]
    if any(s == "not_applicable" for s in states):
        return {"state": "not_applicable"}
    if any(s == "unknown" for s in states):
        return _unknown("a_component_is_unknown")
    if all(s == "would_allow" for s in states):
        return {"state": "would_allow"}
    return {"state": "would_block"}


def _research_candidates(conn, attempt, provenance, current_position_id):
    """Record the frozen September study without funded authority or provider work."""
    conjunction_names = ("turnover_10_50_v1", "source_1k_10k_v1", "turnover_source_conjunction_v1")
    if bypasses_selection(attempt.get("signal_source")):
        return {name: {"state": "not_applicable"} for name in
                conjunction_names + ("nano_prior_profitable_v1",)}
    inputs = provenance["inputs"]
    mcap = _finite(inputs.get("effective_mcap_usd"), positive=True)
    volume = _finite(inputs.get("raw_m5_volume_usd"))
    ratio = volume / mcap if mcap is not None and volume is not None and volume >= 0 else None
    turnover = {"state": "unknown" if ratio is None else
                "would_allow" if 0.10 <= ratio < 0.50 else "would_block",
                "raw_turnover_ratio": ratio, "min_ratio": 0.10, "max_ratio_exclusive": 0.50}
    source_band = _band_shadow(attempt, provenance, networks={"4663", "solana"},
                               input_name="source_size_usd", floor=1_000.0,
                               ceiling_exclusive=10_000.0)
    conjunction = _conjunction_shadow(turnover, source_band)
    nano = {"state": "unknown" if mcap is None else "would_block", "effective_mcap_usd": mcap}
    if mcap is not None and mcap < 50_000:
        try:
            prior = _prior_shadow(conn, attempt, current_position_id, exclude_mint=True)
        except Exception:
            prior = _unknown("prior_read_error")
        nano = {**prior, "effective_mcap_usd": mcap,
                "target_mint_excluded": prior.get("state") != "unknown",
                "prior_population": "frozen_contract_copy"}
    return {"turnover_10_50_v1": turnover, "source_1k_10k_v1": source_band,
            "turnover_source_conjunction_v1": conjunction, "nano_prior_profitable_v1": nano}


def evaluate_entry_shadows(conn, *, attempt_id: str, current_position_id: int | None,
                           result=None, entry_context=None) -> dict:
    """Return the immutable, observation-only shadow payload for one entry attempt."""
    try:
        attempt = _attempt(conn, attempt_id)
    except Exception:
        return {
            "version": SHADOW_VERSION,
            "small_band_v1": _unknown("attempt_read_error"),
            "prior_5_negative_v1": _unknown("attempt_read_error"),
        }
    if attempt is None:
        return {
            "version": SHADOW_VERSION,
            "small_band_v1": _unknown("absent_attempt"),
            "prior_5_negative_v1": _unknown("absent_attempt"),
        }
    try:
        prior = _prior_shadow(conn, attempt, current_position_id)
    except Exception:
        prior = _unknown("prior_read_error")
    provenance = _gate_provenance(attempt, result, entry_context)
    return {
        "version": SHADOW_VERSION,
        "small_band_v1": _small_band_shadow(attempt),
        "prior_5_negative_v1": prior,
        "gate_provenance_v1": provenance,
        **_research_candidates(conn, attempt, provenance, current_position_id),
        "robinhood_organic_m5_lt_1000_v1": _threshold_shadow(
            attempt, provenance, networks={"4663"}, input_name="organic_m5_volume_usd",
            threshold=1_000.0),
        "source_size_ge_1000_v1": _threshold_shadow(
            attempt, provenance, networks={"4663", "solana"},
            input_name="source_size_usd", threshold=1_000.0),
        "solana_clip_ge_100_v1": _threshold_shadow(
            attempt, provenance, networks={"solana"}, input_name="candidate_clip_usd",
            threshold=100.0),
    }
