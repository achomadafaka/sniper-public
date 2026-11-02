from __future__ import annotations

import json
import math
from datetime import datetime, timedelta


def _time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError(f"invalid paper timestamp: {value}") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"paper timestamp must be timezone aware: {value}")
    return parsed


def _positive(value, name):
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be positive and finite") from None
    if value <= 0 or not math.isfinite(value):
        raise ValueError(f"{name} must be positive and finite")
    return value


def _bps(costs, key):
    try:
        value = float(costs.get(key, 0))
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be finite and nonnegative") from None
    if value < 0 or not math.isfinite(value):
        raise ValueError(f"{key} must be finite and nonnegative")
    return value / 10_000.0


def _mint(value):
    value = str(value or "")
    return value.lower() if value.lower().startswith("0x") else value


def open_candidate_position(conn, candidate, signal, quote, now_iso):
    """Open one modeled candidate or paired-control position from an eligible v2 quote."""
    if candidate.execution_mode != "paper" or candidate.timecap_s <= 0:
        raise ValueError("candidate position must be paper with a terminal time cap")
    if candidate.status not in {"candidate_active", "promoted_first_class"}:
        raise ValueError("candidate is not active")
    if not signal.get("matched"):
        raise ValueError("only matched candidate signals can open")
    if signal.get("params_hash") != candidate.params_hash:
        raise ValueError("signal parameter hash does not match candidate")
    if quote.get("quality") != "ok":
        raise ValueError("entry quote must have quality=ok")
    network = str(signal.get("network_id") or "")
    mint = _mint(signal.get("mint"))
    if str(quote.get("network_id") or "") != network or _mint(quote.get("mint")) != mint:
        raise ValueError("entry quote chain or mint mismatch")
    quote_at = _time(quote.get("observed_at"))
    signal_at = _time(signal.get("observed_at"))
    if quote_at < signal_at:
        raise ValueError("entry quote cannot precede signal")
    if quote_at > _time(now_iso):
        raise ValueError("entry quote cannot be from the future")
    price = _positive(quote.get("price"), "entry price")
    notional = _positive(candidate.sizing.get("notional_usd"), "entry notional")
    quantity = notional / price
    entry_cost = notional * _bps(candidate.costs, "entry_bps")
    control_for = signal.get("control_for_signal_id")
    existing = conn.execute(
        "SELECT * FROM memecoin_candidate_positions WHERE candidate_id=? AND signal_id=? "
        "AND control_for_signal_id IS ?",
        (candidate.candidate_id, signal["signal_id"], control_for),
    ).fetchone()
    if existing:
        return dict(existing)
    try:
        conn.execute("SAVEPOINT open_candidate_position")
        cursor = conn.execute(
            "INSERT INTO memecoin_candidate_positions(candidate_id,signal_id,episode_key,"
            "network_id,mint,evidence_stage,control_for_signal_id,execution_mode,timecap_s,entry_observation_id,"
            "entry_price,entry_notional,quantity,entry_cost,opened_at,status,last_observation_id,"
            "last_observed_at,last_price,last_price_observation_id,last_price_observed_at) "
            "VALUES (?,?,?,?,?,?,?,'paper',?,?,?,?,?,?,?,'open',?,?,?,?,?)",
            (candidate.candidate_id, signal["signal_id"], signal["episode_key"], network, mint,
             signal.get("evidence_stage", "candidate"), control_for, candidate.timecap_s,
             quote.get("id"), price, notional, quantity,
             entry_cost, quote.get("observed_at"), quote.get("id"), quote.get("observed_at"),
             price, quote.get("id"), quote.get("observed_at")),
        )
        position_id = cursor.lastrowid
        conn.execute(
            "INSERT INTO memecoin_candidate_fills(position_id,candidate_id,observation_id,side,"
            "price,notional,cost,source,filled_at) VALUES (?,?,?,'entry',?,?,?,?,?)",
            (position_id, candidate.candidate_id, quote.get("id"), price, notional, entry_cost,
             "v2_selected_pair", quote.get("observed_at")),
        )
        conn.execute("RELEASE SAVEPOINT open_candidate_position")
        conn.commit()
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT open_candidate_position")
        conn.execute("RELEASE SAVEPOINT open_candidate_position")
        conn.rollback()
        raise
    return dict(conn.execute(
        "SELECT * FROM memecoin_candidate_positions WHERE position_id=?", (position_id,),
    ).fetchone())


def _quote_key(quote):
    return (_time(quote.get("observed_at")), int(quote.get("id") or 0))


def _close(conn, position, candidate, price, observed_at, observation_id, reason,
           *, progress_id=None, progress_at=None, price_observed_at=None):
    proceeds = float(position["quantity"]) * price
    gross_pnl = proceeds - float(position["entry_notional"])
    exit_cost = proceeds * _bps(candidate["costs"], "exit_bps")
    net_pnl = gross_pnl - float(position["entry_cost"]) - exit_cost
    cursor = conn.execute(
        "UPDATE memecoin_candidate_positions SET status='closed',exit_observation_id=?,"
        "exit_price=?,exit_cost=?,exit_reason=?,closed_at=?,gross_pnl=?,net_pnl=?,"
        "last_observation_id=COALESCE(?,last_observation_id),"
        "last_observed_at=COALESCE(?,last_observed_at),last_price=?,"
        "last_price_observation_id=?,last_price_observed_at=? "
        "WHERE position_id=? AND status='open'",
        (observation_id, price, exit_cost, reason, observed_at, gross_pnl, net_pnl,
         progress_id if progress_id is not None else observation_id,
         progress_at if progress_at is not None else observed_at,
         price, observation_id, price_observed_at or observed_at, position["position_id"]),
    )
    if cursor.rowcount != 1:
        return None
    conn.execute(
        "INSERT INTO memecoin_candidate_fills(position_id,candidate_id,observation_id,side,"
        "price,notional,cost,source,filled_at) VALUES (?,?,?,'exit',?,?,?,?,?)",
        (position["position_id"], position["candidate_id"], observation_id, price, proceeds,
         exit_cost, f"v2_{reason}", observed_at),
    )
    row = dict(position)
    row.update(status="closed", exit_observation_id=observation_id, exit_price=price,
               exit_cost=exit_cost, exit_reason=reason, closed_at=observed_at,
               gross_pnl=gross_pnl, net_pnl=net_pnl)
    return row


def sweep_candidate_positions(conn, quotes, now_iso):
    """Advance open candidate positions through ordered valid quotes and hard time caps."""
    now = _time(now_iso)
    candidate_rows = {row["candidate_id"]: {
        "exit_policy": json.loads(row["exit_policy_json"]),
        "costs": json.loads(row["costs_json"]),
    } for row in conn.execute("SELECT * FROM memecoin_strategy_candidates")}
    ordered = sorted(quotes, key=_quote_key)
    closed = []
    positions = [dict(row) for row in conn.execute(
        "SELECT * FROM memecoin_candidate_positions WHERE status='open' ORDER BY position_id",
    )]
    for position in positions:
        candidate = candidate_rows[position["candidate_id"]]
        policy = candidate["exit_policy"]
        deadline = _time(position["opened_at"]) + timedelta(seconds=int(position["timecap_s"]))
        last_key = (_time(position["last_observed_at"]), int(position["last_observation_id"] or 0))
        stop_hits = int(position["stop_hits"] or 0)
        last_price = float(position["last_price"] or position["entry_price"])
        last_price_id = position["last_price_observation_id"]
        last_price_at = position["last_price_observed_at"]
        last_id = position["last_observation_id"]
        last_at = position["last_observed_at"]
        did_close = False
        for quote in ordered:
            if str(quote.get("network_id") or "") != position["network_id"]:
                continue
            if _mint(quote.get("mint")) != _mint(position["mint"]):
                continue
            key = _quote_key(quote)
            if key <= last_key or key[0] > now or key[0] > deadline:
                continue
            last_key = key
            last_id, last_at = quote.get("id"), quote.get("observed_at")
            if quote.get("quality") != "ok":
                continue
            try:
                price = _positive(quote.get("price"), "quote price")
            except ValueError:
                continue
            last_price = price
            last_price_id, last_price_at = last_id, last_at
            gain_pct = (price / float(position["entry_price"]) - 1.0) * 100.0
            if gain_pct >= float(policy["tp_pct"]):
                result = _close(
                    conn, position, candidate, price, last_at, last_id, "tp",
                    progress_id=last_id, progress_at=last_at,
                )
                if result:
                    closed.append(result)
                did_close = True
                break
            if gain_pct <= -float(policy["sl_pct"]):
                stop_hits += 1
            else:
                stop_hits = 0
            if stop_hits >= int(policy["sl_confirm"]):
                result = _close(
                    conn, position, candidate, price, last_at, last_id, "stop",
                    progress_id=last_id, progress_at=last_at,
                )
                if result:
                    closed.append(result)
                did_close = True
                break
        if did_close:
            continue
        conn.execute(
            "UPDATE memecoin_candidate_positions SET stop_hits=?,last_observation_id=?,"
            "last_observed_at=?,last_price=?,last_price_observation_id=?,"
            "last_price_observed_at=? WHERE position_id=? AND status='open'",
            (stop_hits, last_id, last_at, last_price, last_price_id, last_price_at,
             position["position_id"]),
        )
        if now >= deadline:
            deadline_iso = deadline.isoformat()
            result = _close(
                conn, position, candidate, last_price, deadline_iso, last_price_id, "timecap",
                progress_id=last_id, progress_at=last_at, price_observed_at=last_price_at,
            )
            if result:
                closed.append(result)
    conn.commit()
    return closed
