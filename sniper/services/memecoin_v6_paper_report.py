"""Honest V6 live-paper lifecycle reporting.

Every number is reconstructed from the immutable V6 entries, fills, and decisions. Fills are always
labeled ``modeled_live_quote`` — never an actual/on-chain fill. Gross modeled PnL is derived from the
modeled entry cost and modeled exit proceeds in the position's quote asset; USD gross uses the
explicit entry notional/cost FX mark for non-USDC quotes. Net modeled PnL stays ``None`` with reason
``unknown_cost_components`` because network fees are not captured. Censored positions are excluded
from every performance denominator (PnL, winner, duration, concentration, reason performance).

Read-only: this module never writes. Pass it a ``connect_existing`` (mode=ro) V6 connection.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sniper.analysis.memecoin_v6_statistics import linear_percentile
from sniper.persistence import db_memecoin_v6_paper as ledger
from sniper.services.memecoin_v6_engine import (
    CATASTROPHE_LOSS_PCT, ROUTE_ABSENT_MIN_CHECKS, ROUTE_ABSENT_MIN_S, TIME_EXPIRY_S,
    policy_document, policy_hash,
)
from sniper.services.memecoin_v6_position_policy import EARLY_S

FILL_LABEL = "modeled_live_quote"

# Every discriminator that makes two V6 rows different measurements. Separation has to
# be enforced in the QUERY: a report that renders radar-sourced and fomo-sourced rows
# in separate panels but computes a shared aggregate underneath has pooled them, and
# the pooling is invisible in the layout.
#
# launch_mode is in the key because capturing standard/cashback/mayhem together is
# fine but pooling them is not -- the standard-vs-cashback split is the open question
# this evidence will eventually be asked about.
COHORT_DISCRIMINATORS = (
    "signal_source",
    "adapter_version",
    "fill_model_version",
    "rule_version",
    "evaluator_version",
    "launch_mode",
    "policy_hash",
)


def cohort_filter_sql(*, table_alias: str = "e", **discriminators) -> tuple[str, tuple]:
    """Build the cohort predicate. Every discriminator is required.

    Omitting one silently widens the row set, which is exactly how two books end up in
    one number, so a missing key raises instead of defaulting.
    """
    missing = [name for name in COHORT_DISCRIMINATORS if name not in discriminators]
    if missing:
        raise ValueError(f"cohort filter is missing discriminators: {sorted(missing)}")
    unknown = [name for name in discriminators if name not in COHORT_DISCRIMINATORS]
    if unknown:
        raise ValueError(f"unknown cohort discriminators: {sorted(unknown)}")
    prefix = f"{table_alias}." if table_alias else ""
    # SQLite's `IS ?` is equality for values and null-safe equality for legacy
    # rows whose non-fomo provenance columns are intentionally NULL.
    clauses = " AND ".join(f"{prefix}{name} IS ?" for name in COHORT_DISCRIMINATORS)
    values = tuple(discriminators[name] for name in COHORT_DISCRIMINATORS)
    return clauses, values

NET_UNAVAILABLE_REASON = "unknown_cost_components"
COCKPIT_LATENCY_LIMIT = 5_000
RULE_ORDER = ("staged_break", "catastrophe", "route_loss", "time_expiry", "lp_loss")
MARKET_FACT_KEYS = (
    "pool",
    "executable_price", "quote_status", "quote_freshness_s", "held_s", "drawdown",
    "executable_high_water", "price", "prior_low", "is_break", "arrival_rate", "buy_share",
    "realized_vol", "chain_vol_p90", "net", "depth_pull_flag", "tape_complete",
    "depth_provenance", "provenance", "capture_version", "route_degraded", "sellable",
)


def _rows(conn, sql, *args):
    prior = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.row_factory = prior


def _one(conn, sql, *args):
    got = _rows(conn, sql, *args)
    return got[0] if got else None


def _epoch(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _ui(atomic, decimals):
    if atomic is None or decimals is None:
        return None
    return Decimal(int(atomic)) / (Decimal(10) ** int(decimals))


def _positive_decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def _price_return(current, entry):
    current_value = _positive_decimal(current)
    entry_value = _positive_decimal(entry)
    if current_value is None or entry_value is None:
        return None
    result = (current_value / entry_value) - Decimal(1)
    try:
        rendered = float(result)
    except (OverflowError, ValueError):
        return None
    return rendered if math.isfinite(rendered) else None


def _market_facts(payload):
    return {key: payload.get(key) for key in MARKET_FACT_KEYS}


def _duration_label(seconds):
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError, OverflowError):
        return "time unavailable"
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return f"{total}s"


def _missing_label(prefix, rule):
    missing = tuple(rule.get("missing_fields") or ())
    return f"{prefix}: missing {', '.join(missing)}" if missing else f"{prefix}: insufficient"


def _rule_label(rule_id, rule, payload, engine_state, price_return, observed_at):
    status = str(rule.get("status") or "insufficient")
    if status == "insufficient":
        prefixes = {
            "staged_break": "Break manager", "catastrophe": "Catastrophe",
            "route_loss": "Route", "time_expiry": "Time cap", "lp_loss": "LP pull",
        }
        return _missing_label(prefixes.get(rule_id, rule_id.replace("_", " ")), rule)
    if rule_id == "staged_break":
        episode = engine_state.get("break_episode") or {}
        if status == "triggered":
            reasons = rule.get("reason_codes") or ()
            detail = ", ".join(str(reason).replace("_", " ") for reason in reasons)
            return f"Break manager: {detail or 'triggered'}"
        if episode.get("status") == "active":
            return f"Break manager: active break, confirmation streak {episode.get('neg_streak', 0)}"
        return "Break manager: no confirmed break"
    if rule_id == "catastrophe":
        if price_return is None:
            return "Catastrophe: executable return unavailable"
        current = f"{price_return * 100:+.1f}%"
        verb = "reached" if status == "triggered" else "above"
        return f"Catastrophe: {current}, {verb} -{CATASTROPHE_LOSS_PCT:g}% exit"
    if rule_id == "route_loss":
        route = engine_state.get("route_absence") or {}
        quote_status = payload.get("quote_status")
        if status == "triggered":
            return f"Route: absent for at least {ROUTE_ABSENT_MIN_CHECKS} checks and " \
                   f"{ROUTE_ABSENT_MIN_S:g}s"
        if quote_status == "route_absent":
            first = _epoch(route.get("first_absent_at"))
            current = _epoch(observed_at)
            elapsed = max(0, int(current - first)) if first is not None and current is not None else 0
            return f"Route: absent {int(route.get('checks') or 0)}/{ROUTE_ABSENT_MIN_CHECKS} " \
                   f"checks; {elapsed}s/{ROUTE_ABSENT_MIN_S:g}s"
        return f"Route: {str(quote_status).replace('_', ' ') if quote_status else 'status unavailable'}"
    if rule_id == "time_expiry":
        held_s = payload.get("held_s")
        if status == "triggered":
            return f"Time cap: reached after {_duration_label(held_s)}"
        try:
            remaining = max(0.0, TIME_EXPIRY_S - float(held_s))
        except (TypeError, ValueError, OverflowError):
            return "Time cap: held time unavailable"
        return f"Time cap: {_duration_label(remaining)} remaining"
    if rule_id == "lp_loss":
        if status == "triggered":
            return "LP pull: trusted depth pull detected"
        if payload.get("depth_pull_flag") is False:
            return "LP pull: no trusted pull detected"
        return "LP pull: not triggered"
    return f"{rule_id.replace('_', ' ')}: {status.replace('_', ' ')}"


def _decision_context(*, action, rules, payload, engine_state, active_intent,
                      price_return, has_decision, observed_at=None):
    missing = sorted({
        str(field)
        for rule in rules.values()
        for field in (rule.get("missing_fields") or ())
    } | {str(field) for field in (payload.get("missing_fields") or ())})
    ordered_ids = [rule_id for rule_id in RULE_ORDER if rule_id in rules]
    ordered_ids.extend(sorted(rule_id for rule_id in rules if rule_id not in RULE_ORDER))
    watching = [
        {"rule_id": rule_id, "state": str(rules[rule_id].get("status") or "insufficient"),
         "label": _rule_label(
             rule_id, rules[rule_id], payload, engine_state, price_return, observed_at)}
        for rule_id in ordered_ids
    ]
    quote_status = payload.get("quote_status")
    if active_intent:
        pending_action = str(active_intent.get("action") or action or "action")
        return {
            "action": pending_action, "severity": "urgent",
            "why": f"{pending_action.replace('_', ' ').title()} is awaiting a modeled fill; "
                   "V6 retains the action intent.",
            "watching": watching, "missing_facts": missing,
        }
    if not has_decision:
        return {
            "action": action or "HOLD", "severity": "degraded",
            "why": "Waiting for the first V6 observation and decision.",
            "watching": watching, "missing_facts": missing,
        }
    action = str(action or "HOLD")
    triggered = [item["label"] for item in watching if item["state"] == "triggered"]
    if action != "HOLD":
        reason = triggered[0] if triggered else "a V6 exit rule triggered"
        why = f"{action.replace('_', ' ').title()}: {reason}."
        severity = "urgent"
    elif missing or quote_status not in (None, "healthy"):
        detail = f"missing {', '.join(missing)}" if missing else \
            f"executable quote is {str(quote_status).replace('_', ' ')}"
        why = f"Holding: no V6 exit rule is triggered; evidence is incomplete ({detail})."
        severity = "degraded"
    else:
        why = "Holding: no V6 exit rule is triggered; executable quote is healthy."
        severity = "normal"
    return {
        "action": action, "severity": severity, "why": why,
        "watching": watching, "missing_facts": missing,
    }


def _latest_observation(conn, position_id):
    return _one(conn, "SELECT * FROM v6_paper_observations WHERE position_id=? "
                "ORDER BY observed_at DESC, observation_id DESC LIMIT 1", position_id)


def _latest_decision(conn, position_id):
    return _one(conn, "SELECT * FROM v6_paper_decisions WHERE position_id=? "
                "ORDER BY decided_at DESC, rowid DESC LIMIT 1", position_id)


def _action_fills(conn, position_id):
    return _rows(conn, "SELECT * FROM v6_paper_fills WHERE position_id=? AND leg='action' "
                 "ORDER BY fill_id", position_id)


def _exit_followups(conn, position_id):
    rows = _rows(
        conn,
        "SELECT s.followup_id,s.action_token,s.horizon_s,s.due_at,s.token_qty_atomic,"
        "f.created_at AS exit_at,f.quote_out_atomic AS actual_exit_proceeds_atomic,"
        "f.quote_out_asset AS actual_exit_asset,f.quote_out_decimals AS actual_exit_decimals,"
        "r.status,r.observed_at,r.quote_out_asset,r.quote_out_atomic,r.quote_out_decimals,"
        "r.unit_price,r.quote_provider,r.detail_code "
        "FROM v6_paper_exit_followup_schedules s "
        "JOIN v6_paper_fills f ON f.action_token=s.action_token "
        "LEFT JOIN v6_paper_exit_followup_results r ON r.followup_id=s.followup_id "
        "WHERE s.position_id=? ORDER BY s.due_at,s.followup_id",
        position_id,
    )
    out = []
    for row in rows:
        actual = (int(row["actual_exit_proceeds_atomic"])
                  if row["actual_exit_proceeds_atomic"] is not None else None)
        later = int(row["quote_out_atomic"]) if row["quote_out_atomic"] is not None else None
        exit_epoch = _epoch(row["exit_at"])
        observed_epoch = _epoch(row["observed_at"])
        comparable = (actual is not None and later is not None
                      and row["actual_exit_asset"] == row["quote_out_asset"]
                      and row["actual_exit_decimals"] == row["quote_out_decimals"])
        delta_atomic = later - actual if comparable else None
        delta_quote = (_ui(delta_atomic, row["quote_out_decimals"])
                       if delta_atomic is not None else None)
        out.append({
            "followup_id": row["followup_id"], "action_token": row["action_token"],
            "target_horizon_s": int(row["horizon_s"]), "due_at": row["due_at"],
            "actual_horizon_s": ((observed_epoch - exit_epoch)
                                  if observed_epoch is not None and exit_epoch is not None else None),
            "status": row["status"] or "pending",
            "token_qty_atomic": int(row["token_qty_atomic"]),
            "actual_exit_proceeds_atomic": actual,
            "followup_proceeds_atomic": later,
            "proceeds_delta_atomic": delta_atomic,
            "proceeds_delta_quote": str(delta_quote) if delta_quote is not None else None,
            "quote_out_asset": row["quote_out_asset"] or row["actual_exit_asset"],
            "quote_out_decimals": (row["quote_out_decimals"]
                                    if row["quote_out_decimals"] is not None
                                    else row["actual_exit_decimals"]),
            "unit_price": row["unit_price"], "quote_provider": row["quote_provider"],
            "observed_at": row["observed_at"], "detail_code": row["detail_code"],
        })
    return out


def _rule_results(row):
    if row is None:
        return {}
    try:
        results = json.loads(row["rule_results_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return {result["rule_id"]: result for result in results if result.get("rule_id")}


def _active_intent(conn, position_id):
    row = _one(
        conn, "SELECT action_token, action, fraction_ppm, priority, trigger_evidence_id, "
        "supersedes_token, attempt_count, last_attempt_at, next_attempt_at, last_quote_status, "
        "reason FROM v6_paper_action_intents WHERE position_id=? AND status='active'",
        position_id)
    return _active_intent_row(row)


def _active_intent_row(row):
    if row is None:
        return None
    return {
        "token": row["action_token"], "action": row["action"],
        "fraction_ppm": row["fraction_ppm"], "priority": row["priority"],
        "trigger_evidence_id": row["trigger_evidence_id"],
        "supersedes_token": row["supersedes_token"],
        "attempt_count": row["attempt_count"], "last_attempt_at": row["last_attempt_at"],
        "next_attempt_at": row["next_attempt_at"],
        "last_quote_status": row["last_quote_status"], "reason": row["reason"],
    }


def _economics(conn, entry, position, *, action_fills=None):
    """Reconstructed modeled economics with an explicit entry-time FX mark when needed."""
    quote_asset = entry["quote_in_asset"]
    entry_cost = _ui(entry["quote_in_atomic"], entry["quote_in_decimals"]) or Decimal(0)
    entry_qty = int(entry["token_qty_atomic"])
    fills = (_action_fills(conn, position["position_id"])
             if action_fills is None else action_fills)
    sold_qty = sum(int(f["token_qty_atomic"]) for f in fills)
    proceeds = sum((_ui(f["quote_out_atomic"], f["quote_out_decimals"]) or Decimal(0))
                   for f in fills)
    cost_of_sold = entry_cost * (Decimal(sold_qty) / Decimal(entry_qty)) if entry_qty else Decimal(0)
    realized_gross = proceeds - cost_of_sold if sold_qty else Decimal(0)
    gross_return = (realized_gross / cost_of_sold) if cost_of_sold > 0 and sold_qty else None
    notional_usd = Decimal(int(entry["notional_usd_micros"])) / Decimal(1_000_000)
    entry_fx = (notional_usd / entry_cost) if entry_cost > 0 else None
    usd_pnl = realized_gross * entry_fx if entry_fx is not None else None
    return {
        "quote_asset": quote_asset,
        "entry_cost_quote": float(entry_cost),
        "modeled_exit_proceeds_quote": float(proceeds),
        "sold_qty_atomic": sold_qty,
        "realized_gross_pnl_quote": float(realized_gross),
        "realized_gross_pnl_usd": float(usd_pnl) if usd_pnl is not None else None,
        "usd_conversion": ("quote_is_usd" if quote_asset == "USDC" else "entry_fx_mark"),
        "gross_return_frac": float(gross_return) if gross_return is not None else None,
        "net_modeled_pnl_usd": None,
        "net_unavailable_reason": NET_UNAVAILABLE_REASON,
        "known_fees_usd": None,
        "route_impact_cost_quote": None,
        "action_fill_count": len(fills),
    }


_MISSING = object()


def _card(conn, position, *, entry=_MISSING, obs=_MISSING, dec=_MISSING,
          action_fills=_MISSING, active_intent=_MISSING):
    if entry is _MISSING:
        entry = _one(
            conn, "SELECT * FROM v6_paper_entries WHERE entry_id=?", position["entry_id"])
    if obs is _MISSING:
        obs = _latest_observation(conn, position["position_id"])
    if dec is _MISSING:
        dec = _latest_decision(conn, position["position_id"])
    payload = json.loads(obs["payload_json"]) if obs else {}
    opened = _epoch(entry["fill_ts"])
    current = _epoch(position["closed_at"] or position["censored_at"]
                     or (obs["observed_at"] if obs else None))
    age_s = (current - opened) if (opened and current) else None
    fills = None if action_fills is _MISSING else action_fills
    econ = _economics(conn, entry, position, action_fills=fills)
    try:
        engine_state = json.loads(position["engine_state_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        engine_state = {}
    rules = _rule_results(dec)
    if active_intent is _MISSING:
        active_intent = _active_intent(conn, position["position_id"])
    latest_action = dec["action"] if dec else (position["latest_action"] or "HOLD")
    price_return = _price_return(payload.get("executable_price"), entry["entry_price"])
    entry_qty = int(position["entry_qty_atomic"])
    remaining_qty = int(position["remaining_qty_atomic"])
    decision_context = _decision_context(
        action=latest_action, rules=rules, payload=payload, engine_state=engine_state,
        active_intent=active_intent, price_return=price_return, has_decision=dec is not None,
        observed_at=obs["observed_at"] if obs else None)
    return {
        "position_id": position["position_id"], "entry_id": position["entry_id"],
        "network_id": position["network_id"], "mint": position["mint"], "ticker": entry["ticker"],
        "source_wallet_id": entry["source_wallet_id"],
        "source_wallet_handle": entry["source_wallet_handle"],
        "signal_source": entry["signal_source"], "status": position["status"],
        "fill_kind": FILL_LABEL, "funded": False,
        "modeled_entry": {"unit_price": entry["entry_price"],
                          "notional_usd": (entry["notional_usd_micros"] or 0) / 1_000_000,
                          "quote_asset": entry["quote_in_asset"],
                          "entry_qty_atomic": int(entry["token_qty_atomic"]),
                          "label": "modeled live quote"},
        "opened_at": entry["fill_ts"], "age_s": age_s,
        "latest_observed_at": obs["observed_at"] if obs else None,
        "remaining_qty_atomic": remaining_qty,
        "entry_qty_atomic": entry_qty,
        "inventory": {
            "remaining_fraction": (remaining_qty / entry_qty) if entry_qty else None,
        },
        "executable_price": payload.get("executable_price"),
        "whole_bag_proceeds_atomic": payload.get("whole_bag_proceeds_atomic"),
        "quote_freshness_s": payload.get("quote_freshness_s"),
        "executable_high_water": payload.get("executable_high_water"),
        "drawdown": payload.get("drawdown"),
        "modeled_price_return_frac": price_return,
        "modeled_high_water_return_frac": _price_return(
            payload.get("executable_high_water"), entry["entry_price"]),
        "market_facts": _market_facts(payload),
        "modeled_pnl": econ,
        "latest_action": latest_action,
        "latest_thought": dec["thought"] if dec else None,
        "rule_results": rules,
        "decision_context": decision_context,
        "break_episode": engine_state.get("break_episode"),
        "active_intent": active_intent,
        "quote_status": payload.get("quote_status"),
        "terminal_reason": position["terminal_reason"],
        "censor_reason": position["censor_reason"], "censored_at": position["censored_at"],
    }


def list_positions(conn) -> list[dict]:
    positions = _rows(conn, "SELECT * FROM v6_paper_positions "
                      "ORDER BY (status='open') DESC, updated_at DESC, position_id")
    entries = {row["entry_id"]: row for row in _rows(conn, "SELECT * FROM v6_paper_entries")}
    observations = {row["position_id"]: row for row in _rows(
        conn, "SELECT o.* FROM v6_paper_observations o JOIN ("
        "SELECT position_id,MAX(rowid) AS latest_rowid FROM v6_paper_observations "
        "GROUP BY position_id) latest ON o.rowid=latest.latest_rowid")}
    decisions = {row["position_id"]: row for row in _rows(
        conn, "SELECT d.* FROM v6_paper_decisions d JOIN ("
        "SELECT position_id,MAX(rowid) AS latest_rowid FROM v6_paper_decisions "
        "GROUP BY position_id) latest ON d.rowid=latest.latest_rowid")}
    fills = {}
    for row in _rows(conn, "SELECT * FROM v6_paper_fills WHERE leg='action' ORDER BY fill_id"):
        fills.setdefault(row["position_id"], []).append(row)
    intents = {row["position_id"]: _active_intent_row(row) for row in _rows(
        conn, "SELECT * FROM v6_paper_action_intents WHERE status='active'")}
    return [
        _card(
            conn, position, entry=entries[position["entry_id"]],
            obs=observations.get(position["position_id"]),
            dec=decisions.get(position["position_id"]),
            action_fills=fills.get(position["position_id"], ()),
            active_intent=intents.get(position["position_id"]),
        )
        for position in positions
    ]


def _current_copy_where(table_alias="e") -> tuple[str, tuple]:
    prefix = f"{table_alias}." if table_alias else ""
    return (
        f"{prefix}signal_source!='radar_v5' AND {prefix}policy_hash=?",
        (policy_hash(),),
    )


def list_cockpit_positions(conn) -> list[dict]:
    """Return only open positions from the arm currently under test."""
    predicate, params = _current_copy_where()
    positions = _rows(
        conn,
        "SELECT p.* FROM v6_paper_positions p "
        "JOIN v6_paper_entries e ON e.entry_id=p.entry_id "
        f"WHERE p.status='open' AND {predicate} "
        "ORDER BY p.updated_at DESC,p.position_id",
        *params,
    )
    return [_card(conn, position) for position in positions]


def position_detail(conn, position_id, *, observation_limit: int = 1,
                    decision_limit: int = 12, before_observation: int | None = None,
                    before_decision: int | None = None) -> dict:
    for name, value, maximum in (
            ("observation_limit", observation_limit, 25),
            ("decision_limit", decision_limit, 50)):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise ValueError(f"{name} must be in 1..{maximum}")
    for name, value in (("before_observation", before_observation),
                        ("before_decision", before_decision)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
            raise ValueError(f"{name} must be a positive integer or null")
    position = _one(conn, "SELECT * FROM v6_paper_positions WHERE position_id=?", position_id)
    if position is None:
        raise KeyError(f"no v6 paper position: {position_id}")
    entry = _one(conn, "SELECT * FROM v6_paper_entries WHERE entry_id=?", position["entry_id"])
    obs_sql = "SELECT rowid AS timeline_rowid,* FROM v6_paper_observations WHERE position_id=?"
    obs_args = [position_id]
    if before_observation is not None:
        obs_sql += " AND rowid<?"
        obs_args.append(before_observation)
    obs_desc = _rows(
        conn, obs_sql + " ORDER BY rowid DESC LIMIT ?", *obs_args, observation_limit + 1)
    obs_more = len(obs_desc) > observation_limit
    obs_page = obs_desc[:observation_limit]
    obs = list(reversed(obs_page))

    decision_sql = "SELECT rowid AS timeline_rowid,* FROM v6_paper_decisions WHERE position_id=?"
    decision_args = [position_id]
    if before_decision is not None:
        decision_sql += " AND rowid<?"
        decision_args.append(before_decision)
    decision_desc = _rows(
        conn, decision_sql + " ORDER BY rowid DESC LIMIT ?",
        *decision_args, decision_limit + 1)
    decision_more = len(decision_desc) > decision_limit
    decision_page = decision_desc[:decision_limit]
    decisions = list(reversed(decision_page))
    fills = _rows(conn, "SELECT * FROM v6_paper_fills WHERE position_id=? ORDER BY fill_id",
                  position_id)
    latest = _latest_decision(conn, position_id)
    latest_obs = _latest_observation(conn, position_id)
    payload = json.loads(latest_obs["payload_json"]) if latest_obs else {}
    try:
        engine_state = json.loads(position["engine_state_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        engine_state = {}
    latest_rules = _rule_results(latest)
    return {
        "funded": False, "fill_label": FILL_LABEL, "status": position["status"],
        "entry_evidence": {
            "entry_id": entry["entry_id"], "origin_event_id": entry["origin_event_id"],
            "network_id": entry["network_id"], "mint": entry["mint"], "ticker": entry["ticker"],
            "source_wallet_id": entry["source_wallet_id"],
            "source_wallet_handle": entry["source_wallet_handle"],
            "signal_source": entry["signal_source"], "entry_price": entry["entry_price"],
            "notional_usd": (entry["notional_usd_micros"] or 0) / 1_000_000,
            "quote_in_asset": entry["quote_in_asset"], "quote_out_asset": entry["quote_out_asset"],
            "route_binding": json.loads(entry["route_binding_json"]),
            "quote_provider": entry["quote_provider"], "policy_hash": entry["policy_hash"],
            "fill_kind": entry["fill_kind"], "signal_ts": entry["signal_ts"],
            "quote_ts": entry["quote_ts"], "fill_ts": entry["fill_ts"],
        },
        "inventory": {
            "entry_qty_atomic": int(position["entry_qty_atomic"]),
            "remaining_qty_atomic": int(position["remaining_qty_atomic"]),
            "exited_qty_atomic": (int(position["entry_qty_atomic"])
                                   - int(position["remaining_qty_atomic"])),
        },
        "latest_executable_value": {
            "executable_price": payload.get("executable_price"),
            "whole_bag_proceeds_atomic": payload.get("whole_bag_proceeds_atomic"),
            "quote_freshness_s": payload.get("quote_freshness_s"),
        },
        "latest_thought": ({
            "text": latest["thought"], "action": latest["action"],
            "reason_codes": json.loads(latest["reason_codes_json"]),
            "rule_results": latest_rules,
        } if latest else None),
        "latest_action": position["latest_action"],
        "rule_results": latest_rules,
        "break_episode": engine_state.get("break_episode"),
        "active_intent": _active_intent(conn, position_id),
        "quote_status": payload.get("quote_status"),
        "modeled_pnl": _economics(conn, entry, position),
        "terminal_reason": position["terminal_reason"],
        "censor_reason": position["censor_reason"], "censored_at": position["censored_at"],
        "exit_followups": _exit_followups(conn, position_id),
        "timeline": {
            "observations": [{"observation_id": o["observation_id"], "observed_at": o["observed_at"],
                              "network_id": o["network_id"], "block_identity": o["block_identity"],
                              "capture_version": o["capture_version"],
                              "payload": json.loads(o["payload_json"])} for o in obs],
            "decisions": [{"action_token": d["action_token"], "action": d["action"],
                           "fraction_ppm": d["fraction_ppm"], "thought": d["thought"],
                           "reason_codes": json.loads(d["reason_codes_json"]),
                           "rule_results": _rule_results(d),
                           "decided_at": d["decided_at"], "applied_outcome": d["applied_outcome"],
                           "applied_at": d["applied_at"]} for d in decisions],
            "fills": [{"fill_id": f["fill_id"], "leg": f["leg"], "action_token": f["action_token"],
                       "token_qty_atomic": int(f["token_qty_atomic"]),
                       "quote_out_atomic": (int(f["quote_out_atomic"])
                                            if f["quote_out_atomic"] is not None else None),
                       "unit_price": f["unit_price"],
                       "fill_kind": f["fill_kind"], "created_at": f["created_at"]} for f in fills],
            "pagination": {
                "observations": {
                    "has_more": obs_more,
                    "next_before": (int(obs_page[-1]["timeline_rowid"])
                                    if obs_more and obs_page else None),
                },
                "decisions": {
                    "has_more": decision_more,
                    "next_before": (int(decision_page[-1]["timeline_rowid"])
                                    if decision_more and decision_page else None),
                },
            },
        },
    }


_RUNTIME_STAGES = (
    "provider_to_receive", "receive_to_observation", "observation_to_decision", "step", "quote",
)


def _latency_stats(rows):
    values = [int(row["duration_us"]) / 1000.0 for row in rows]
    return {
        "samples": len(values), "p50": linear_percentile(values, 50),
        "p95": linear_percentile(values, 95), "p99": linear_percentile(values, 99),
    }


def runtime_health(conn, *, sample_limit: int | None = None) -> dict:
    """Read compact feed state and local/provider latency stages without merging their clocks."""
    health_rows = _rows(
        conn, "SELECT * FROM v6_paper_feed_health ORDER BY network_id,pool")
    feeds = {}
    for row in health_rows:
        feeds.setdefault(row["network_id"], {})[row["pool"]] = {
            "mode": row["mode"], "current": bool(row["current"]), "gap": bool(row["gap"]),
            "last_received_at": row["last_received_at"],
            "reconnect_count": row["reconnect_count"], "cursor_height": row["cursor_height"],
            "cursor_event_identity": row["cursor_event_identity"],
            "accumulator_freshness_s": row["accumulator_freshness_s"],
            "updated_at": row["updated_at"],
        }
    active_pools = set()
    for row in _rows(
            conn, "SELECT p.network_id,p.mint,e.route_binding_json,o.payload_json "
            "FROM v6_paper_positions p JOIN v6_paper_entries e ON e.entry_id=p.entry_id "
            "LEFT JOIN v6_paper_observations o ON o.position_id=p.position_id "
            "AND o.observation_id=p.latest_observation_id WHERE p.status='open'"):
        network = row["network_id"]
        try:
            route = json.loads(row["route_binding_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            route = {}
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        payload_pool = payload.get("pool")
        candidates = [payload_pool] if payload_pool else [
            route.get("market_pool"), route.get("pool_or_vault"), route.get("coin_vault")]
        if network == "solana" and not payload_pool:
            for leg in route.get("route_plan") or ():
                swap = leg.get("swapInfo") if isinstance(leg, dict) else None
                if isinstance(swap, dict) and row["mint"] in {
                        swap.get("inputMint"), swap.get("outputMint")}:
                    candidates.append(swap.get("ammKey"))
            if not any(candidates):
                candidates.extend(route.get("amm_keys") or ())
        elif not payload_pool and route.get("protocol") == "uniswap_v4" \
                and isinstance(route.get("pool_key"), dict):
            from sniper.services.memecoin_evm_v4_events import v4_pool_id
            candidates.append(v4_pool_id(route["pool_key"]))
        for pool in candidates:
            if pool:
                active_pools.add((network, str(pool).lower() if network == "4663" else str(pool)))
    active_feeds, draining_feeds = {}, {}
    for network, network_feeds in feeds.items():
        for pool, detail in network_feeds.items():
            target = active_feeds if (network, pool) in active_pools else draining_feeds
            target.setdefault(network, {})[pool] = detail
    # Bounded by default (cold-scan cap 5000); the cockpit passes its own tighter limit.
    latency_rows = _rows(
        conn, "SELECT stage,duration_us,network_id,provider,feed_mode,recorded_at "
        "FROM v6_paper_latency_samples ORDER BY sample_id DESC LIMIT ?",
        sample_limit if sample_limit is not None else 5000)
    out = {
        "feed_health": feeds,
        "feed_health_active": active_feeds,
        "feed_health_draining": draining_feeds,
    }
    for stage in _RUNTIME_STAGES:
        out[f"{stage}_ms"] = _latency_stats(
            [row for row in latency_rows if row["stage"] == stage])

    def _group(tag):
        grouped = {}
        for value in sorted({row[tag] for row in latency_rows}):
            selected = [row for row in latency_rows if row[tag] == value]
            grouped[value] = {
                f"{stage}_ms": _latency_stats(
                    [row for row in selected if row["stage"] == stage])
                for stage in _RUNTIME_STAGES
            }
        return grouped

    out["by_network"] = _group("network_id")
    out["by_provider"] = _group("provider")
    out["by_feed_mode"] = _group("feed_mode")
    return out


def cockpit_revision(conn) -> tuple:
    """Cheap revision token for state the cockpit renders; high-rate telemetry is excluded."""
    row = _one(conn, """
        SELECT
          (SELECT COUNT(*) FROM v6_paper_positions),
          (SELECT COALESCE(MAX(updated_at),'') FROM v6_paper_positions),
          (SELECT COALESCE(MAX(rowid),0) FROM v6_paper_decisions),
          (SELECT COUNT(*) FROM v6_paper_admission_queue),
          (SELECT COALESCE(SUM(attempt_count),0) FROM v6_paper_admission_queue),
          (SELECT COALESCE(SUM(state='queued'),0) FROM v6_paper_admission_queue),
          (SELECT COALESCE(SUM(state='inflight'),0) FROM v6_paper_admission_queue),
          (SELECT COALESCE(SUM(state='retry_wait'),0) FROM v6_paper_admission_queue),
          (SELECT COALESCE(SUM(state='filled'),0) FROM v6_paper_admission_queue),
          (SELECT COALESCE(SUM(state='terminal_unfilled'),0)
             FROM v6_paper_admission_queue),
          (SELECT COALESCE(MAX(updated_at),'') FROM v6_paper_admission_queue),
          (SELECT COUNT(*) FROM v6_paper_admission_rejections),
          (SELECT COALESCE(MAX(source_position_id),0) FROM v6_paper_admission_rejections)
    """)
    return tuple(row)


def _empty_chain_counts() -> dict:
    return {
        key: {"solana": 0, "4663": 0, "total": 0}
        for key in ("opened", "open", "closed", "censored", "action_unfilled")
    }


def _cockpit_outcomes_by_launch_mode(conn) -> dict[str, dict]:
    """Bounded current-contract outcomes, never pooled across launch modes."""
    predicate, params = _current_copy_where()
    positions = _rows(
        conn,
        "SELECT p.position_id,p.status,e.quote_in_asset,e.quote_in_atomic,"
        "e.quote_in_decimals,e.token_qty_atomic,e.notional_usd_micros,e.launch_mode "
        "FROM v6_paper_positions p JOIN v6_paper_entries e ON e.entry_id=p.entry_id "
        f"WHERE p.status='closed' AND {predicate}",
        *params,
    )
    fills_by_position: dict[str, list] = {}
    for fill in _rows(
        conn,
        "SELECT f.* FROM v6_paper_fills f "
        "JOIN v6_paper_positions p ON p.position_id=f.position_id "
        "JOIN v6_paper_entries e ON e.entry_id=p.entry_id "
        f"WHERE f.leg='action' AND p.status='closed' AND {predicate} "
        "ORDER BY f.fill_id",
        *params,
    ):
        fills_by_position.setdefault(fill["position_id"], []).append(fill)

    grouped: dict[str, list[dict]] = {}
    for position in positions:
        mode = str(position["launch_mode"] or "unknown")
        grouped.setdefault(mode, []).append(_economics(
            conn,
            position,
            position,
            action_fills=fills_by_position.get(position["position_id"], ()),
        ))

    outcomes = {}
    for mode in sorted(grouped):
        economics = grouped[mode]
        returns = [
            item["gross_return_frac"] for item in economics
            if item["gross_return_frac"] is not None
        ]
        usd = [
            item["realized_gross_pnl_usd"] for item in economics
            if item["realized_gross_pnl_usd"] is not None
        ]
        outcomes[mode] = {
            "graded_lifecycles": len(economics),
            "winner_rate": (
                sum(value > 0 for value in returns) / len(returns)
                if returns else None
            ),
            "gross_modeled_pnl_usd": sum(usd) if usd else 0.0,
            "net_modeled_pnl_usd": None,
        }
    return outcomes


def cockpit_summary(conn) -> dict:
    """Small current-Radar-arm health summary; legacy cohorts are invisible."""
    current_identity = (policy_document()["engine_policy_id"], policy_hash())
    predicate, params = _current_copy_where()
    counts = _empty_chain_counts()
    for row in _rows(
        conn,
        "SELECT p.network_id,p.status,COUNT(*) AS count FROM v6_paper_positions p "
        "JOIN v6_paper_entries e ON e.entry_id=p.entry_id "
        f"WHERE {predicate} GROUP BY p.network_id,p.status",
        *params,
    ):
        network = row["network_id"]
        amount = int(row["count"])
        status = str(row["status"])
        for key in ("opened", "censored" if status.startswith("censored_") else status):
            counts[key][network] += amount
            counts[key]["total"] += amount

    quote_row = _one(
        conn,
        "SELECT COUNT(*) AS total,"
        "SUM(CASE WHEN json_extract(o.payload_json,'$.executable_price') IS NOT NULL "
        "THEN 1 ELSE 0 END) AS available FROM v6_paper_observations o "
        "JOIN v6_paper_positions p ON p.position_id=o.position_id "
        "JOIN v6_paper_entries e ON e.entry_id=p.entry_id "
        f"WHERE {predicate}",
        *params,
    )
    quote_total = int(quote_row["total"] or 0)
    insufficient = _one(
        conn,
        "SELECT COUNT(*) AS count FROM v6_paper_decisions d "
        "JOIN v6_paper_positions p ON p.position_id=d.position_id "
        "JOIN v6_paper_entries e ON e.entry_id=p.entry_id, "
        "json_each(d.reason_codes_json) reason "
        f"WHERE {predicate} AND p.status NOT LIKE 'censored_%' "
        "AND reason.value LIKE 'data_insufficient%'", *params,
    )
    return {
        "funded": False,
        "fill_label": FILL_LABEL,
        "admission": {"states": {
            "queued": 0, "inflight": 0, "retry_wait": 0,
            "filled": 0, "terminal_unfilled": 0,
        }},
        "arm_identity": {
            "source": "funded_copy_system",
            "policy_hash": policy_hash(),
        },
        "current_policy": {
            "engine_policy_id": current_identity[0], "policy_hash": current_identity[1],
        },
        "counts_by_chain": counts,
        "outcomes_by_launch_mode": _cockpit_outcomes_by_launch_mode(conn),
        "quote_availability": ((int(quote_row["available"] or 0) / quote_total)
                               if quote_total else None),
        "data_insufficient_decisions": int(insufficient["count"] or 0),
    }


def cockpit_snapshot(conn) -> dict:
    return {
        "mode": "v6_paper",
        "funded": False,
        "fill_label": FILL_LABEL,
        "positions": list_cockpit_positions(conn),
        "runtime": runtime_health(conn, sample_limit=COCKPIT_LATENCY_LIMIT),
        "summary": cockpit_summary(conn),
    }


def _summary_facts(conn) -> dict:
    entries = {row["entry_id"]: row for row in _rows(conn, "SELECT * FROM v6_paper_entries")}
    observation_stats = {row["position_id"]: dict(row) for row in _rows(
        conn,
        "SELECT o.position_id,COUNT(*) AS observation_count,"
        "SUM(CASE WHEN json_extract(o.payload_json,'$.executable_price') IS NOT NULL "
        "THEN 1 ELSE 0 END) AS observations_with_price,"
        "MAX(CASE WHEN json_type(o.payload_json,'$.drawdown') IN ('integer','real') "
        "AND NOT EXISTS (SELECT 1 FROM v6_paper_decisions q "
        "WHERE q.position_id=o.position_id AND q.observation_id=o.observation_id "
        "AND instr(q.rule_results_json,'catastrophe:quote_quarantined')>0) "
        "THEN CAST(json_extract(o.payload_json,'$.drawdown') AS REAL) END) AS max_drawdown "
        "FROM v6_paper_observations o GROUP BY o.position_id")}

    reason_outcomes = {}
    data_insufficient = {}
    for row in _rows(
            conn,
            "WITH filled AS (SELECT DISTINCT position_id,action_token "
            "FROM v6_paper_fills WHERE leg='action' AND action_token IS NOT NULL),"
            "attempted AS (SELECT position_id,action_token,attempt_count "
            "FROM v6_paper_action_intents) "
            "SELECT d.position_id,reasons.value AS reason_code,"
            "CASE WHEN d.action='HOLD' THEN 'held' "
            "WHEN filled.action_token IS NOT NULL THEN 'filled' "
            "WHEN attempted.action_token IS NOT NULL AND attempted.attempt_count>0 "
            "THEN 'unfilled' ELSE 'pending' END AS outcome,COUNT(*) AS outcome_count "
            "FROM v6_paper_decisions d JOIN json_each(d.reason_codes_json) reasons "
            "LEFT JOIN filled ON filled.position_id=d.position_id "
            "AND filled.action_token=d.action_token "
            "LEFT JOIN attempted ON attempted.position_id=d.position_id "
            "AND attempted.action_token=d.action_token "
            "GROUP BY d.position_id,reasons.value,outcome"):
        code = str(row["reason_code"])
        bucket = reason_outcomes.setdefault(row["position_id"], {}).setdefault(
            code, {"count": 0, "applied": {}})
        count = int(row["outcome_count"])
        bucket["count"] += count
        bucket["applied"][row["outcome"]] = count
        if code.startswith("data_insufficient"):
            data_insufficient[row["position_id"]] = (
                data_insufficient.get(row["position_id"], 0) + count)

    action_stats = {row["position_id"]: dict(row) for row in _rows(
        conn,
        "SELECT position_id,SUM(CASE WHEN action!='HOLD' THEN 1 ELSE 0 END) "
        "AS action_count,MIN(CASE WHEN action!='HOLD' THEN decided_at END) "
        "AS first_action_at FROM v6_paper_decisions GROUP BY position_id")}

    special_rows = {}
    special_sql = (
        "SELECT d.position_id,d.observation_id,d.rule_results_json,"
        "json_extract(o.payload_json,'$.held_s') AS held_s,"
        "json_extract(o.payload_json,'$.wallet_tier_adjustment') AS wallet_adjustment "
        "FROM v6_paper_decisions d LEFT JOIN v6_paper_observations o "
        "ON o.position_id=d.position_id AND o.observation_id=d.observation_id WHERE "
    )
    for predicate in (
            "instr(d.reason_codes_json,'score:')>0",
            "instr(d.rule_results_json,'catastrophe:quote_quarantined')>0"):
        for row in _rows(conn, special_sql + predicate):
            special_rows[(row["position_id"], row["observation_id"])] = row
    special_decisions = {}
    for row in special_rows.values():
        item = dict(row)
        item["_rules"] = _rule_results(row)
        item["_payload"] = {
            "held_s": row["held_s"],
            "wallet_tier_adjustment": row["wallet_adjustment"],
        }
        special_decisions.setdefault(row["position_id"], []).append(item)
    intents = {}
    for row in _rows(
            conn, "SELECT position_id,action_token,attempt_count "
            "FROM v6_paper_action_intents"):
        intents.setdefault(row["position_id"], []).append(row)
    action_fills = {}
    for row in _rows(
            conn, "SELECT position_id,action_token,token_qty_atomic,quote_out_atomic,"
            "quote_out_decimals FROM v6_paper_fills WHERE leg='action' ORDER BY fill_id"):
        action_fills.setdefault(row["position_id"], []).append(row)
    return {
        "entries": entries,
        "observation_stats": observation_stats,
        "reason_outcomes": reason_outcomes,
        "data_insufficient": data_insufficient,
        "action_stats": action_stats,
        "special_decisions": special_decisions,
        "intents": intents,
        "action_fills": action_fills,
    }


def _summarize_positions(conn, positions, facts) -> dict:
    by_chain = {}
    for status_key in ("opened", "open", "closed", "censored", "action_unfilled"):
        by_chain[status_key] = {"solana": 0, "4663": 0, "total": 0}

    def _bump(key, chain):
        by_chain[key][chain] = by_chain[key].get(chain, 0) + 1
        by_chain[key]["total"] += 1

    graded = []          # closed, non-censored economics
    gross_usd = Decimal(0)
    gross_by_asset = {}
    drawdowns = []
    hold_durations = []
    first_action_lags = []
    action_counts = []
    reason_outcomes = {}
    data_insufficient = 0
    obs_total = obs_with_price = 0
    staged_break_diagnostics = {
        "triggered": 0,
        "derisk": 0,
        "exit_full": 0,
        "early_point_present": 0,
        "full_exit_would_derisk_without_early": 0,
        "score_histogram": {},
        "tier_histogram": {},
    }

    for p in positions:
        chain = p["network_id"]
        _bump("opened", chain)
        if p["status"] == "open":
            _bump("open", chain)
        elif p["status"] == "closed":
            _bump("closed", chain)
        elif str(p["status"]).startswith("censored_"):
            _bump("censored", chain)
        position_id = p["position_id"]
        entry = facts["entries"][p["entry_id"]]
        observation_stats = facts["observation_stats"].get(position_id, {})
        # capture/action availability uses open + closed (NOT censored is fine to include for capture,
        # but performance denominators exclude censored below).
        obs_total += int(observation_stats.get("observation_count") or 0)
        obs_with_price += int(observation_stats.get("observations_with_price") or 0)
        intent_rows = facts["intents"].get(position_id, ())
        intent_by_token = {row["action_token"]: row for row in intent_rows}
        action_fills = facts["action_fills"].get(position_id, ())
        filled_tokens = {row["action_token"] for row in action_fills
                         if row["action_token"] is not None}
        unfilled_here = sum(
            1 for row in intent_rows
            if int(row["attempt_count"] or 0) > 0 and row["action_token"] not in filled_tokens)
        if unfilled_here:
            _bump("action_unfilled", chain)
        censored = str(p["status"]).startswith("censored_")
        if not censored:
            data_insufficient += int(facts["data_insufficient"].get(position_id, 0))
            for code, source_bucket in facts["reason_outcomes"].get(position_id, {}).items():
                bucket = reason_outcomes.setdefault(code, {"count": 0, "applied": {}})
                bucket["count"] += int(source_bucket["count"])
                for outcome, count in source_bucket["applied"].items():
                    bucket["applied"][outcome] = (
                        bucket["applied"].get(outcome, 0) + int(count))
            for d in facts["special_decisions"].get(position_id, ()):
                staged = d["_rules"].get("staged_break")
                if not staged or staged.get("status") != "triggered":
                    continue
                staged_break_diagnostics["triggered"] += 1
                staged_action = str(staged.get("action") or "")
                if staged_action == "DERISK":
                    staged_break_diagnostics["derisk"] += 1
                elif staged_action == "EXIT_FULL":
                    staged_break_diagnostics["exit_full"] += 1
                reasons = tuple(staged.get("reason_codes") or ())
                score = next(
                    (int(code.split(":", 1)[1]) for code in reasons
                     if code.startswith("score:") and code.split(":", 1)[1].isdigit()),
                    None,
                )
                tier = next(
                    (int(code.split(":", 1)[1]) for code in reasons
                     if code.startswith("tier:") and code.split(":", 1)[1].isdigit()),
                    None,
                )
                if score is not None:
                    histogram = staged_break_diagnostics["score_histogram"]
                    histogram[str(score)] = histogram.get(str(score), 0) + 1
                if tier is not None:
                    histogram = staged_break_diagnostics["tier_histogram"]
                    histogram[str(tier)] = histogram.get(str(tier), 0) + 1
                observation_payload = d["_payload"]
                held_s = observation_payload.get("held_s")
                early = (isinstance(held_s, (int, float)) and not isinstance(held_s, bool)
                         and math.isfinite(float(held_s)) and float(held_s) < EARLY_S)
                if early:
                    staged_break_diagnostics["early_point_present"] += 1
                if (staged_action == "EXIT_FULL" and "break" in reasons
                        and score is not None and early):
                    without_early_score = max(0, score - 1)
                    without_early_tier = (2 if without_early_score >= 2
                                          else 1 if without_early_score == 1 else 0)
                    adjustment = observation_payload.get("wallet_tier_adjustment")
                    adjustment = adjustment if isinstance(adjustment, int) \
                        and not isinstance(adjustment, bool) else 0
                    without_early_tier = max(0, min(2, without_early_tier + adjustment))
                    if without_early_tier < 2:
                        staged_break_diagnostics["full_exit_would_derisk_without_early"] += 1
        if p["status"] == "closed" and not censored:
            econ = _economics(conn, entry, p, action_fills=action_fills)
            graded.append(econ)
            if econ["realized_gross_pnl_usd"] is not None:
                gross_usd += Decimal(str(econ["realized_gross_pnl_usd"]))
            asset = econ["quote_asset"]
            gross_by_asset[asset] = gross_by_asset.get(asset, 0.0) + econ["realized_gross_pnl_quote"]
            opened = _epoch(entry["fill_ts"])
            closed = _epoch(p["closed_at"])
            if opened and closed:
                hold_durations.append(closed - opened)
            stats = facts["action_stats"].get(position_id, {})
            action_count = int(stats.get("action_count") or 0)
            action_counts.append(action_count)
            if action_count and opened:
                first = _epoch(stats.get("first_action_at"))
                if first:
                    first_action_lags.append(first - opened)
            dd = observation_stats.get("max_drawdown")
            if isinstance(dd, (int, float)) and math.isfinite(float(dd)):
                drawdowns.append(float(dd))

    returns = [g["gross_return_frac"] for g in graded if g["gross_return_frac"] is not None]
    winners = [r for r in returns if r > 0]
    losers = [r for r in returns if r <= 0]
    top_removed = None
    if returns:
        usd_vals = [g["realized_gross_pnl_usd"] for g in graded
                    if g["realized_gross_pnl_usd"] is not None]
        if usd_vals:
            top_removed = float(sum(usd_vals) - max(usd_vals))

    return {
        "counts_by_chain": by_chain,
        "gross_modeled_pnl_usd": float(gross_usd) if graded else 0.0,
        "gross_modeled_pnl_by_quote_asset": gross_by_asset,
        "net_modeled_pnl_usd": None,
        "net_unavailable_reason": NET_UNAVAILABLE_REASON,
        "graded_lifecycles": len(graded),
        "winner_rate": (len(winners) / len(returns)) if returns else None,
        "median_outcome": linear_percentile(returns, 50),
        "worst_outcome": min(returns) if returns else None,
        "worst_decile_outcome": linear_percentile(returns, 10),
        "max_drawdown": max(drawdowns) if drawdowns else None,
        "winner_retention": (sum(winners) / len(winners)) if winners else None,
        "loss_containment": (sum(losers) / len(losers)) if losers else None,
        "median_time_to_first_action_s": linear_percentile(first_action_lags, 50),
        "total_action_count": sum(action_counts),
        "median_action_count": linear_percentile(action_counts, 50),
        "median_hold_duration_s": linear_percentile(hold_durations, 50),
        "quote_availability": (obs_with_price / obs_total) if obs_total else None,
        "data_insufficient_decisions": data_insufficient,
        "gross_usd_top_winner_removed": top_removed,
        "reason_code_outcomes": reason_outcomes,
        "staged_break_diagnostics": staged_break_diagnostics,
    }


def _entry_policy_identity(position) -> tuple[str, str]:
    raw_document = position["cohort_policy_document"]
    try:
        document = json.loads(raw_document) if isinstance(raw_document, str) else raw_document
    except (TypeError, ValueError, json.JSONDecodeError):
        document = {}
    raw_policy_id = document.get("engine_policy_id") if isinstance(document, dict) else None
    engine_policy_id = str(raw_policy_id or "unknown")
    return engine_policy_id, str(position["cohort_policy_hash"] or "unknown")


def _measurement_identity(position) -> tuple:
    return tuple(position[f"cohort_{name}"] for name in COHORT_DISCRIMINATORS)


def cohort_summary(conn, **discriminators) -> dict:
    """Aggregate exactly one fully identified measurement cohort."""
    predicate, params = cohort_filter_sql(**discriminators)
    positions = _rows(
        conn,
        "SELECT p.*,e.policy_hash AS cohort_policy_hash,"
        "e.policy_document AS cohort_policy_document "
        "FROM v6_paper_positions p JOIN v6_paper_entries e ON e.entry_id=p.entry_id "
        f"WHERE {predicate}",
        *params,
    )
    return _summarize_positions(conn, positions, _summary_facts(conn))


def summary(conn) -> dict:
    positions = _rows(
        conn,
        "SELECT p.*,e.policy_hash AS cohort_policy_hash,"
        "e.policy_document AS cohort_policy_document,"
        + ",".join(
            f"e.{name} AS cohort_{name}" for name in COHORT_DISCRIMINATORS
        ) + " FROM v6_paper_positions p "
        "JOIN v6_paper_entries e ON e.entry_id=p.entry_id",
    )
    # Keep the existing headline as the legacy/fomo book. Radar is a separate
    # measurement and appears only in exact discriminator cohorts below.
    legacy_positions = [
        position for position in positions
        if position["cohort_signal_source"] != "radar_v5"
    ]
    grouped: dict[tuple[str, str], list] = {}
    for position in legacy_positions:
        grouped.setdefault(_entry_policy_identity(position), []).append(position)

    measurement_grouped: dict[tuple, list] = {}
    for position in positions:
        measurement_grouped.setdefault(_measurement_identity(position), []).append(position)

    facts = _summary_facts(conn)
    current_identity = (policy_document()["engine_policy_id"], policy_hash())
    cohorts = []
    cohort_metrics = {}
    for identity in sorted(grouped):
        engine_policy_id, cohort_hash = identity
        metrics = _summarize_positions(conn, grouped[identity], facts)
        cohort_metrics[identity] = metrics
        cohorts.append({
            "engine_policy_id": engine_policy_id,
            "policy_hash": cohort_hash,
            "is_current": identity == current_identity,
            **metrics,
        })
    measurement_cohorts = []
    for identity in sorted(
            measurement_grouped,
            key=lambda values: tuple("" if value is None else str(value) for value in values)):
        measurement_cohorts.append({
            **dict(zip(COHORT_DISCRIMINATORS, identity, strict=True)),
            **_summarize_positions(conn, measurement_grouped[identity], facts),
        })
    current_metrics = cohort_metrics.get(
        current_identity, _summarize_positions(conn, [], facts))
    all_policy_counts = {
        key: {"solana": 0, "4663": 0, "total": 0}
        for key in ("opened", "open", "closed", "censored", "action_unfilled")
    }
    for metrics in cohort_metrics.values():
        for key, counts in metrics["counts_by_chain"].items():
            for chain, count in counts.items():
                all_policy_counts[key][chain] = all_policy_counts[key].get(chain, 0) + count

    return {
        "funded": False,
        "fill_label": FILL_LABEL,
        "admission": ledger.admission_summary(conn),
        "current_policy": {
            "engine_policy_id": current_identity[0],
            "policy_hash": current_identity[1],
        },
        "policy_cohorts": cohorts,
        "measurement_cohorts": measurement_cohorts,
        "all_policy_counts_by_chain": all_policy_counts,
        **current_metrics,
    }


# Raw historical measurements remain readable; hypothetical conclusions are ineligible.
from sniper.research.wallet_execution.publication import diagnostic_result as _diagnostic_result
summary = _diagnostic_result(summary)
cohort_summary = _diagnostic_result(cohort_summary)
cockpit_summary = _diagnostic_result(cockpit_summary)
