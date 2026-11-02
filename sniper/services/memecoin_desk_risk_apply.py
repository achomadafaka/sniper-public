"""RISK-live, the engine side (operator 2026-10-22; Beta, with Lambda's seat lambda/risk-live).

The watcher (scripts/memecoin_desk_risk_watcher.py) is the only writer of data/desk/risk_live.db and
writes one risk_decisions row per open desk position per cycle. The funded sweep reads it READ-ONLY
here and enforces only two actions:

- TIGHTEN: per field, the STRICTER of the position's effective rails and the model's (stop and trail
  closer, take-profit lower; a rail that is off may be turned on). Written as an exit-override row
  source='desk_risk', complete (tp, sl, trail, de-risk all explicit), so no field changes implicitly.
  The frozen contract is never touched. An active operator override refuses it: the operator's rails
  stand whole.
- EXIT: nominates the reason 'desk_risk_exit' only when no deterministic rail fired this sweep, so the
  rails always win and the sell goes through the one shared exit claim.

Every decision the sweep looks at gets one ack row in memecoin_desk_risk_applies (applied, refused +
reason, apply lag), keyed by decision_id, so a decision is acted on at most once. Any read failure
is no action: the rails keep running.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
import logging
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

APPLY_SOURCE = "desk_risk"
LOOSEN_ACTION = "LOOSEN_TRAIL"
EXIT_REASON = "desk_risk_exit"
ORDER_SQL = (
    "SELECT id AS decision_id, position_id, mint, action, rails_json, decided_at, expires_at, call_id, "
    "inputs_hash, reason FROM risk_decisions WHERE position_id=? AND live=1 "
    "AND action IN ('EXIT','TIGHTEN','LOOSEN_TRAIL') "
    "AND expires_at > ? ORDER BY id DESC")


def risk_db_path():
    from sniper.util.state_paths import state_path
    return Path(state_path("data/desk/risk_live.db"))


def live_orders(path, position_id, now_epoch):
    """Unexpired live EXIT/TIGHTEN rows for one position, newest first. [] when the file is absent or
    unreadable (logged): the model being unreadable is never an action."""
    path = Path(path)
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=1)
    except sqlite3.Error:
        logger.warning("desk risk: risk_live.db unopenable", exc_info=True)
        return []
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(ORDER_SQL, (int(position_id), float(now_epoch)))]
    except sqlite3.Error:
        logger.warning("desk risk: risk_live.db unreadable", exc_info=True)
        return []
    finally:
        conn.close()


def _rail(v):
    return float(v) if type(v) in (int, float) and math.isfinite(v) and v >= 0 else None


def tightened(effective, rails):
    """(merged rails or None, why). `effective` is the position's effective policy dict (stop_pct,
    tp_pct, trail_pct, derisk_pct; 0 = off). A field tightens only when the model's value is stricter:
    stop/trail smaller but positive (or turning an off rail on), take-profit smaller but positive."""
    if not isinstance(rails, dict) or not rails or set(rails) - {"sl_pct", "tp_pct", "trail_pct"}:
        return None, "rails_malformed"
    out = {"stop_pct": float(effective.get("stop_pct") or 0.0), "tp_pct": float(effective.get("tp_pct") or 0.0),
           "trail_pct": float(effective.get("trail_pct") or 0.0),
           "derisk_pct": float(effective.get("derisk_pct") or 0.0)}
    changed = False
    for key, field in (("sl_pct", "stop_pct"), ("tp_pct", "tp_pct"), ("trail_pct", "trail_pct")):
        if key not in rails:
            continue
        new = _rail(abs(rails[key]) if type(rails[key]) in (int, float) else None)
        if new is None or new == 0:
            return None, "rails_malformed"
        if out[field] == 0 or new < out[field]:
            out[field], changed = new, True
    return (out, "tightened") if changed else (None, "not_stricter")



@contextmanager
def _tighten_snapshot(conn):
    """Deferred read snapshot through override + ack; a concurrent ledger write cannot race validation."""
    conn.execute("SAVEPOINT desk_risk_tighten")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK TO desk_risk_tighten")
        conn.execute("RELEASE desk_risk_tighten")
        raise
    conn.execute("RELEASE desk_risk_tighten")


def _positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _tighten_quote(conn, pos, now_epoch, current_quote=None):
    try:
        return _read_tighten_quote(conn, pos, now_epoch, current_quote)
    except Exception:
        logger.exception("desk risk exact quote unreadable pos=%s", pos.get("id"))
        return None, None, "exact_quote_unreadable"


def _read_tighten_quote(conn, pos, now_epoch, current_quote):
    """Prefer the sweep's existing quote; observation time never renews its original age."""
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_desk_risk import MARK_MAX_AGE_S
    from sniper.util.clock import offset_s
    fresh = m.get_position(conn, pos["id"])
    if (fresh is None or fresh.get("is_open") != 1 or fresh.get("closed_at") is not None
            or fresh.get("execution_mode") != "real" or fresh.get("signal_source") != "desk"
            or any(fresh.get(k) != pos.get(k) for k in ("id", "mint", "network_id"))):
        return None, None, "position_mismatch"
    if (fresh.get("close_claim_id") or fresh.get("close_sig") or fresh.get("derisk_claim_id")
            or (not fresh.get("derisked_at") and (fresh.get("derisk_sig") or fresh.get("derisk_pending_at")))):
        return None, None, "position_busy"
    quote = m.latest_exit_decision_quote(conn, pos["id"]) if current_quote is None else current_quote
    if not quote:
        return None, None, "no_exact_quote" if current_quote is None else "no_current_exact_quote"
    if (quote.get("seam") != "exit_decision"
            or any(quote.get(k) != fresh.get(k) for k in ("mint", "network_id"))
            or quote.get("position_id") != fresh["id"]):
        return None, None, "quote_identity_mismatch"
    try:
        if type(now_epoch) not in (int, float) or not math.isfinite(now_epoch):
            raise ValueError("invalid now")
        quote_now = now_epoch - offset_s()  # quote timestamps are corrected UTC; decision TTLs use host time
        ages = []
        for key in ("observed_at", "quoted_at"):
            at = datetime.fromisoformat(quote[key])
            if at.utcoffset() is None:
                raise ValueError("naive quote")
            ages.append(quote_now - at.timestamp())
        if any(not math.isfinite(age) or age < 0 for age in ages):
            raise ValueError("future quote")
    except (TypeError, ValueError, KeyError, OverflowError):
        return None, None, "quote_time_invalid"
    if any(age > MARK_MAX_AGE_S for age in ages):
        return None, None, "stale_exact_quote"
    qty = m.open_qty(conn, fresh["id"])
    if (not _positive(qty) or not _positive(quote.get("open_qty"))
            or m.exit_qty_unknown(conn, fresh["id"])
            or not math.isclose(qty, quote["open_qty"], rel_tol=1e-12, abs_tol=0.0)):
        return None, None, "quote_quantity_mismatch"
    if (any(not _positive(quote.get(k)) for k in ("expected_usdc", "unit_price"))
            or any(not _positive(fresh.get(k)) for k in ("entry_price", "entry_notional"))
            or not math.isclose(quote["unit_price"], quote["expected_usdc"] / qty,
                                rel_tol=1e-12, abs_tol=0.0)):
        return None, None, "quote_value_invalid"
    return fresh, quote, None


def _tighten_trigger(conn, pos, quote, effective, merged, override):
    return next(iter(_tighten_refused_fields(conn, pos, quote, effective, merged, override).values()), None)


def _tighten_refused_fields(conn, pos, quote, effective, merged, override):
    """Use the funded confirmation's partial-fill and trail-arming rules for changed rails only."""
    from sniper.services.memecoin_real_runner import executable_exit_allowed
    candidate = {**(override or {}), "tp_pct": merged["tp_pct"], "sl_pct": merged["stop_pct"],
                 "trail_pct": merged["trail_pct"], "derisk_pct": merged["derisk_pct"]}
    refused = {}
    for field, reason, rail in (("stop_pct", "b_stop", "sl_pct"), ("tp_pct", "real_tp", "tp_pct"),
                                ("trail_pct", "real_trail", "trail_pct")):
        if merged[field] == effective.get(field):
            continue
        if field == "trail_pct" and not _positive(pos.get("executable_peak_price")):
            refused[rail] = "quote_peak_invalid"
            continue
        allowed, _detail = executable_exit_allowed(
            conn, pos, reason, {"expected_usdc": quote["expected_usdc"]},
            override=candidate, quote_qty=quote["open_qty"])
        if allowed:
            refused[rail] = "would_trigger:%s" % rail
    return refused


def _ack(conn, order, pos, now_epoch, now_iso, status, reason, before=None, after=None, source="risk_live",
         detail=None):
    from sniper.persistence import db_memecoin as m
    m.record_desk_risk_apply(
        conn, decision_id=order["decision_id"], position_id=int(pos["id"]), action=order["action"],
        status=status, reason=reason, decided_at=order.get("decided_at"), applied_at=now_iso,
        lag_s=(now_epoch - order["decided_at"]) if type(order.get("decided_at")) in (int, float)
        and math.isfinite(order["decided_at"]) else None,
        rails_before=before, rails_after=after, call_id=order.get("call_id"),
        inputs_hash=order.get("inputs_hash"), source=source, detail=detail)


def _decision_time(order, now_epoch):
    decided, expires = order.get("decided_at"), order.get("expires_at")
    if (any(type(v) not in (int, float) or not math.isfinite(v) for v in (decided, expires, now_epoch))
            or decided > now_epoch or expires <= decided):
        return "decision_time_invalid"
    return "decision_expired" if expires <= now_epoch else None


def _fixed_rule(pos):
    """The large-buyer pilot's rails are its frozen rule: neither the risk seat nor auto-tighten moves them."""
    from sniper.services.memecoin_origin import LARGE_BUYER_COHORT_PREFIX
    return str(pos.get("desk_cohort_id") or "").startswith(LARGE_BUYER_COHORT_PREFIX)


def apply_orders(conn, pos, *, now_epoch, now_iso, path=None, orders=None, current_quote=None):
    """Apply this position's newest unacted TIGHTEN (and ack every older unacted row as superseded),
    then return the newest unacted EXIT order or None. Desk positions only; never raises."""
    import sniper.settings.memecoin as cfg
    from sniper.persistence import db_memecoin as m
    if pos.get("signal_source") != "desk" or not getattr(cfg, "MEMECOIN_DESK_RISK_APPLY", True) or _fixed_rule(pos):
        return None
    try:
        rows = live_orders(path or risk_db_path(), pos["id"], now_epoch) if orders is None else orders
        done = m.desk_risk_acted_ids(conn, [r["decision_id"] for r in rows])
        rows = [r for r in rows if r["decision_id"] not in done]
        exit_order, tighten_seen = None, False                    # tighten_seen: one rail CHANGE (tighten or loosen) per sweep
        for order in rows:                                        # newest first
            if order.get("mint") != pos.get("mint"):
                _ack(conn, order, pos, now_epoch, now_iso, "refused", "mint_mismatch")
            elif order["action"] == "EXIT":
                if exit_order is None:
                    exit_order = order
                else:
                    _ack(conn, order, pos, now_epoch, now_iso, "refused", "superseded")
            elif tighten_seen:
                _ack(conn, order, pos, now_epoch, now_iso, "refused", "superseded")
            else:
                tighten_seen = True
                with _tighten_snapshot(conn):
                    if order["action"] == LOOSEN_ACTION:
                        _apply_loosen(conn, pos, order, now_epoch, now_iso, current_quote)
                    else:
                        _apply_tighten(conn, pos, order, now_epoch, now_iso, current_quote)
        conn.commit()
        return exit_order
    except Exception:
        logger.exception("desk risk apply failed pos=%s; rails unchanged", pos.get("id"))
        return None


AUTO_SOURCE = "attention_auto"
AUTO_RULE = ("creator_sell AND sell_burst (distinct non-creator sellers) within 10 s, OR quote_collapse >= Q2 "
             "-> trail to its minimum bound; stop stricter of current and 1 - price*(1-S%)/entry while below entry")


def tape_db_path():
    from sniper.util.state_paths import state_path
    return Path(state_path("data/desk/attention/tape.db"))


def detected_at(row):
    """When the row's condition was detected: the tape worker's value.receipt_cut (an absence event carries a
    stale observed_at), else observed_at (runner rows have no receipt cut)."""
    try:
        cut = json.loads(row.get("value_json") or "{}").get("receipt_cut")
    except ValueError:
        cut = None
    return float(cut) if type(cut) in (int, float) else float(row["observed_at"])


def _fired_rows(path, position_id, since_epoch, *, fired_only=True):
    """Attention rows for one position detected since `since_epoch` (detected_at), oldest first (fired only
    unless asked); [] when absent/unreadable."""
    path = Path(path)
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=1)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT id, signal, value_json, observed_at, fired, thresholds_version FROM attention WHERE position_id=? "
            + ("AND fired=1 " if fired_only else "")
            + "AND COALESCE(json_extract(value_json, '$.receipt_cut'), observed_at) >= ? "
            "ORDER BY COALESCE(json_extract(value_json, '$.receipt_cut'), observed_at), id",
            (int(position_id), float(since_epoch)))]
    except sqlite3.Error:
        logger.warning("desk attention: %s unreadable", path, exc_info=True)
        return []
    finally:
        conn.close()


def auto_trigger(tape_rows, runner_rows, *, q2_pct):
    """The (earlier, later) event pair that fires the auto rule, or None. Pure. Times are detected_at.
    An unknown tape row blinds its signal; signal 'tape' unknown blinds EVERY tape signal of the position,
    so creator+burst cannot fire from a tape known to be gapped (Gamma, attention-tape contract)."""
    def unknown(r):
        try:
            return r.get("fired") is None and json.loads(r.get("value_json") or "{}").get("state") == "unknown"
        except ValueError:
            return False
    blind = {r["signal"] for r in tape_rows if unknown(r)}
    fired = [r for r in tape_rows if r.get("fired", 1) == 1 and "tape" not in blind and r["signal"] not in blind]
    creator = [r for r in fired if r["signal"] == "creator_sell"]
    burst = [r for r in fired if r["signal"] == "sell_burst"]
    for c in creator:
        for b in burst:
            if abs(detected_at(c) - detected_at(b)) <= 10.0:
                return tuple(sorted((c, b), key=lambda r: (detected_at(r), r["id"])))
    if q2_pct is not None:
        for r in runner_rows:
            if r["signal"] == "quote_collapse":
                try:
                    drop = json.loads(r.get("value_json") or "{}").get("drop_pct")
                except ValueError:
                    continue
                if type(drop) in (int, float) and drop >= q2_pct:
                    return (r, r)
    return None


def apply_auto(conn, pos, *, now_epoch, now_iso, price, tape_path=None, runner_path=None, window_s=30.0,
               current_quote=None):
    """The attention auto-tighten for one desk position (all desk trades, no test group). Dry (no action)
    while S or the thresholds version is unset or the kill knob is off. Never raises."""
    import sniper.settings.memecoin as cfg
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_desk_attention as att
    from sniper.services.memecoin_funded_policy import effective_funded_policy
    try:
        t = att.thresholds()
        if (pos.get("signal_source") != "desk" or not getattr(cfg, "MEMECOIN_DESK_ATTENTION_AUTO_TIGHTEN", True) or _fixed_rule(pos)
                or t["S_pct"] is None or t["version"] == "unset"):
            return None
        since = now_epoch - window_s
        pair = auto_trigger(_fired_rows(tape_path or tape_db_path(), pos["id"], since, fired_only=False),
                            # Q2 reads drop_pct on EVERY runner row, not only rows fired at Q (Gamma note 1)
                            _fired_rows(runner_path or att.runner_db_path(), pos["id"], since, fired_only=False),
                            q2_pct=t["Q2_pct"])
        if pair is None:
            return None
        order = {"decision_id": pair[1]["id"], "action": "TIGHTEN", "decided_at": detected_at(pair[1]),
                 "expires_at": detected_at(pair[1]) + window_s}
        if m.desk_risk_acted_ids(conn, [order["decision_id"]], source=AUTO_SOURCE):
            return None
        event_versions = sorted({str(r.get("thresholds_version")) for r in pair})
        detail = {"events": [pair[0]["id"], pair[1]["id"]], "signals": [pair[0]["signal"], pair[1]["signal"]],
                  "thresholds_version": t["version"], "event_thresholds_versions": event_versions,
                  "rule": AUTO_RULE, "price": price}
        ack = lambda status, reason, **kw: _ack(conn, order, pos, now_epoch, now_iso, status, reason,
                                                source=AUTO_SOURCE, detail=detail, **kw)
        if (why := _decision_time(order, now_epoch)):
            ack("refused", why)
            conn.commit()
            return None
        # TWO VERSION KNOBS (Gamma 2026-10-23): the tape worker's MEMECOIN_DESK_ATTN_THRESHOLDS_VERSION stamps the
        # events, the runner's MEMECOIN_DESK_ATTENTION_THRESHOLDS_VERSION arms this rule. A drift (a typo in one)
        # never tightens: refused, acked and logged, so the read counts it.
        if event_versions != [t["version"]]:
            logger.warning("desk attention auto-tighten refused pos=%s: event versions %s != runner %s",
                           pos.get("id"), event_versions, t["version"])
            ack("refused", "thresholds_version_mismatch")
            conn.commit()
            return None
        def _apply():
            override = m.effective_exit_override(conn, pos["id"])
            if override is not None and override.get("source") == "operator":
                ack("refused", "operator_override_active")
                return None
            frozen, effective = effective_funded_policy(pos, override=override)
            if frozen is None:
                ack("refused", "no_frozen_policy")
                return None
            fresh, quote, why = _tighten_quote(conn, pos, now_epoch, current_quote)
            if why:
                ack("refused", why)
                return None
            trail_min = (getattr(cfg, "MEMECOIN_DESK_RAIL_BOUNDS", {}) or {}).get("trail_pct", (None, None))[0]
            detail["price"] = quote["unit_price"]
            rails = att.auto_tighten_rails(effective, price=quote["unit_price"], entry=fresh["entry_price"], s_pct=t["S_pct"],
                                           trail_min=trail_min)
            before = {k: effective.get(k) for k in ("stop_pct", "tp_pct", "trail_pct", "derisk_pct")}
            merged, why = tightened(effective, rails) if rails else (None, "not_stricter")
            if merged is None:
                ack("refused", why, before=before)
                return None
            refused = _tighten_refused_fields(conn, fresh, quote, effective, merged, override)
            fields = {"sl_pct": "stop_pct", "tp_pct": "tp_pct", "trail_pct": "trail_pct"}
            detail["refused_fields"] = refused
            for rail in refused:
                merged[fields[rail]] = before[fields[rail]]
            detail["applied_fields"] = {rail: merged[field] for rail, field in fields.items()
                                        if merged[field] != before[field]}
            if not detail["applied_fields"]:
                ack("refused", "no_safe_tighten", before=before)
                return None
            ov = override or {}
            m.set_position_exit_override(
                conn, pos["id"], APPLY_SOURCE, tp_pct=merged["tp_pct"], sl_pct=merged["stop_pct"],
                trail_pct=merged["trail_pct"], derisk_pct=merged["derisk_pct"],
                tp_target_mcap=ov.get("tp_target_mcap"), sl_target_mcap=ov.get("sl_target_mcap"),
                actor="%s:%s" % (AUTO_SOURCE, order["decision_id"]), note="attention auto-tighten",
                now_iso=now_iso, commit=False)
            ack("applied_partial" if refused else "applied", "tightened", before=before, after=merged)
            return merged
        with _tighten_snapshot(conn):
            merged = _apply()
        conn.commit()
        return merged
    except Exception:
        logger.exception("desk attention auto-tighten failed pos=%s; rails unchanged", pos.get("id"))
        return None


def _apply_tighten(conn, pos, order, now_epoch, now_iso, current_quote):
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_funded_policy import effective_funded_policy
    if order.get("position_id") != pos["id"]:
        return _ack(conn, order, pos, now_epoch, now_iso, "refused", "position_mismatch")
    if (why := _decision_time(order, now_epoch)):
        return _ack(conn, order, pos, now_epoch, now_iso, "refused", why)
    override = m.effective_exit_override(conn, pos["id"])
    if override is not None and override.get("source") == "operator":
        return _ack(conn, order, pos, now_epoch, now_iso, "refused", "operator_override_active")
    frozen, effective = effective_funded_policy(pos, override=override)
    if frozen is None:
        return _ack(conn, order, pos, now_epoch, now_iso, "refused", "no_frozen_policy")
    try:
        rails = json.loads(order.get("rails_json") or "null")
    except ValueError:
        rails = None
    before = {k: effective.get(k) for k in ("stop_pct", "tp_pct", "trail_pct", "derisk_pct")}
    merged, why = tightened(effective, rails)
    if merged is None:
        return _ack(conn, order, pos, now_epoch, now_iso, "refused", why, before=before)
    fresh, quote, why = _tighten_quote(conn, pos, now_epoch, current_quote)
    if why is None:
        why = _tighten_trigger(conn, fresh, quote, effective, merged, override)
    if why:
        return _ack(conn, order, pos, now_epoch, now_iso, "refused", why, before=before)
    ov = override or {}
    m.set_position_exit_override(
        conn, pos["id"], APPLY_SOURCE, tp_pct=merged["tp_pct"], sl_pct=merged["stop_pct"],
        trail_pct=merged["trail_pct"], derisk_pct=merged["derisk_pct"],
        tp_target_mcap=ov.get("tp_target_mcap"), sl_target_mcap=ov.get("sl_target_mcap"),
        actor="desk_risk:%s" % order["decision_id"], note="RISK-live TIGHTEN", now_iso=now_iso, commit=False)
    _ack(conn, order, pos, now_epoch, now_iso, "applied", "tightened", before=before, after=merged)


def loosened(effective, rails, cap):
    """(merged rails or None, why): the ONE loosen function (LOOSEN_TRAIL, v8). Only trail_pct, only wider than the
    position's effective trail, only up to `cap`, only on a trail that is ON and armed like an override trail (a desk
    override arms the trail at once, so a frozen policy with a delayed arm is refused: widening it could arm it early).
    Every other field is copied unchanged."""
    if not isinstance(rails, dict) or set(rails) != {"trail_pct"}:
        return None, "rails_malformed"
    value = rails["trail_pct"]
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        return None, "rails_malformed"
    cur = float(effective.get("trail_pct") or 0.0)
    if not cur > 0:
        return None, "trail_off"
    if not value > cur:
        return None, "not_wider"
    if not value <= cap:
        return None, "above_cap"
    if float(effective.get("trail_arm_pct") or 0.0) > 0 or effective.get("trail_requires_derisk"):
        return None, "trail_arming_semantics"
    return {"stop_pct": float(effective.get("stop_pct") or 0.0), "tp_pct": float(effective.get("tp_pct") or 0.0),
            "trail_pct": float(value), "derisk_pct": float(effective.get("derisk_pct") or 0.0)}, "loosened"


def _applied_since(conn, position_id, since_epoch, actions, source=None):
    """True when this position has an APPLIED ack (status 'applied' or 'applied_partial': a partial attention tighten is
    still a tighten) of one of `actions` (optionally from `source`) at/after since_epoch (None = any time). applied_at is
    the ack's ISO time on the CORRECTED clock; since_epoch must be on the same clock (see _ack_clock)."""
    from datetime import datetime
    q = ("SELECT applied_at FROM memecoin_desk_risk_applies WHERE position_id=? AND status IN ('applied','applied_partial') "
         "AND action IN (%s)" % ",".join("?" * len(actions)))
    args = [int(position_id), *actions]
    if source is not None:
        q += " AND source=?"
        args.append(source)
    for (stamp,) in conn.execute(q, args):
        if since_epoch is None:
            return True
        try:
            if datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp() >= since_epoch:
                return True
        except ValueError:
            return True                                           # an unreadable stamp counts as recent: refuse
    return False


def _ack_clock(now_iso, now_epoch):
    """`now` on the clock the acks are stamped with: now_iso (the corrected UTC clock, real_utc_now_iso) as an epoch, so a
    60 s window compares like with like even though now_epoch is the raw host time.time(). Falls back to now_epoch when
    now_iso is unreadable (a refusal-safe default only for windows, which then use the raw clock)."""
    from datetime import datetime
    try:
        at = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
        return at.timestamp() if at.tzinfo is not None else now_epoch
    except ValueError:
        return now_epoch


def _cited(order):
    """The cited structure tokens of a loosen order (its reason is 'loosen:<tok>,<tok>', code-built), or None."""
    from sniper.services.memecoin_desk_risk import CITE_TOKENS
    reason = str(order.get("reason") or "")
    if not reason.startswith("loosen:"):
        return None
    toks = [t for t in reason[len("loosen:"):].split(",") if t]
    return toks if 1 <= len(toks) <= 4 and all(t in CITE_TOKENS for t in toks) else None


def _current_rails_fire(conn, pos, quote, effective, override):
    """The first deterministic rail that would exit at this exact quote under the position's CURRENT rails, else None."""
    from sniper.services.memecoin_real_runner import executable_exit_allowed
    current = {**(override or {}), "tp_pct": effective.get("tp_pct"), "sl_pct": effective.get("stop_pct"),
               "trail_pct": effective.get("trail_pct"), "derisk_pct": effective.get("derisk_pct")}
    for reason, name in (("b_stop", "stop"), ("real_tp", "tp"), ("real_trail", "trail")):
        if name == "trail" and not _positive(pos.get("executable_peak_price")):
            return "quote_peak_invalid"
        allowed, _detail = executable_exit_allowed(conn, pos, reason, {"expected_usdc": quote["expected_usdc"]},
                                                   override=current, quote_qty=quote["open_qty"])
        if allowed:
            return "rail_would_fire:%s" % name
    return None


def _applied_now_iso():
    """The corrected-UTC time NOW (not the sweep's start): a loosen's ack stamp and its 60 s window start when it lands."""
    import time as _time
    from sniper.util import clock
    return datetime.fromtimestamp(_time.time() - clock.offset_s(), tz=timezone.utc).isoformat()


def _cohort_bounds(conn, pos):
    """The position's FROZEN cohort rail_bounds as {rail: (lo, hi)}, or None when the contract, the cohort row or the
    bound is missing or unreadable (a loosen then refuses: never fall back to the global bounds)."""
    try:
        cohort_id = json.loads(pos["funded_contract_json"])["source"]["desk"]["cohort_id"]
        row = conn.execute("SELECT policy_json FROM memecoin_desk_cohorts WHERE cohort_id=?", (cohort_id,)).fetchone()
        raw = json.loads(row[0])["policy"]["rail_bounds"]
        lo, hi = raw["trail_pct"]
        if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in (lo, hi)) or lo > hi:
            return None
        return {"trail_pct": (float(lo), float(hi))}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError, sqlite3.Error):
        return None


def _apply_loosen(conn, pos, order, now_epoch, now_iso, current_quote):
    """LOOSEN_TRAIL (v8): widen ONLY the trailing stop of a desk position, through the same apply guard as a tighten
    (fresh exact quote, identity, no claim in flight, residual size) plus: no rail may fire now under the CURRENT
    rails (a loosen never un-fires an exit), the wider trail may not fire at once, one applied loosen per position per
    MEMECOIN_DESK_RISK_LOOSEN_GAP_S, none within that gap of a RISK tighten, and NEVER after an applied attention
    auto-tighten. Every refusal is acked with its reason."""
    import time as _time
    import sniper.settings.memecoin as cfg
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_desk_risk import loosen_cap
    from sniper.services.memecoin_funded_policy import effective_funded_policy
    refuse = lambda why, **kw: _ack(conn, order, pos, now_epoch, now_iso, "refused", why, **kw)
    if order.get("position_id") != pos["id"]:
        return refuse("position_mismatch")
    if not getattr(cfg, "MEMECOIN_DESK_RISK_TRAIL_LOOSEN", True):
        return refuse("loosen_disabled")
    if (why := _decision_time(order, now_epoch)):
        return refuse(why)
    override = m.effective_exit_override(conn, pos["id"])
    if override is not None and override.get("source") == "operator":
        return refuse("operator_override_active")
    frozen, effective = effective_funded_policy(pos, override=override)
    if frozen is None:
        return refuse("no_frozen_policy")
    cited = _cited(order)
    if cited is None:
        return refuse("loosen_no_cite")
    try:
        rails = json.loads(order.get("rails_json") or "null")
    except ValueError:
        rails = None
    before = {k: effective.get(k) for k in ("stop_pct", "tp_pct", "trail_pct", "derisk_pct")}
    bounds = _cohort_bounds(conn, pos)
    if bounds is None:
        return refuse("cohort_bounds_unknown", before=before)
    cap = loosen_cap(bounds)
    merged, why = loosened(effective, rails, cap)
    if merged is None:
        return refuse(why, before=before)
    gap = float(getattr(cfg, "MEMECOIN_DESK_RISK_LOOSEN_GAP_S", 60.0))
    if _applied_since(conn, pos["id"], None, ("TIGHTEN",), source="attention_auto"):
        return refuse("attention_tightened", before=before)
    now_iso = _applied_now_iso()                                # stamp at APPLICATION time: the sweep's now_iso can be seconds old
    window_from = _ack_clock(now_iso, now_epoch) - gap
    if _applied_since(conn, pos["id"], window_from, (LOOSEN_ACTION,)):
        return refuse("loosen_rate_limited", before=before)
    if _applied_since(conn, pos["id"], window_from, ("TIGHTEN",)):
        return refuse("recent_tighten", before=before)
    fresh, quote, why = _tighten_quote(conn, pos, now_epoch, current_quote)
    now_iso = _applied_now_iso()                                # after the quote read: the stamp and the 60 s window start when the change lands
    if why is None:
        why = _current_rails_fire(conn, fresh, quote, effective, override)
    if why is None:
        why = _tighten_trigger(conn, fresh, quote, effective, merged, override)       # the wider trail may not fire at once
    if why:
        return refuse(why, before=before)
    ov = override or {}
    m.set_position_exit_override(
        conn, pos["id"], APPLY_SOURCE, tp_pct=merged["tp_pct"], sl_pct=merged["stop_pct"],
        trail_pct=merged["trail_pct"], derisk_pct=merged["derisk_pct"],
        tp_target_mcap=ov.get("tp_target_mcap"), sl_target_mcap=ov.get("sl_target_mcap"),
        actor="desk_risk:%s" % order["decision_id"], note="RISK-live LOOSEN_TRAIL", now_iso=now_iso, commit=False)
    _ack(conn, order, pos, now_epoch, now_iso, "applied", "loosened", before=before, after=merged,
         detail={"cited": cited, "reason": order.get("reason"), "cap": cap, "gap_s": gap,
                 "new_trail_pct": merged["trail_pct"], "old_trail_pct": before["trail_pct"]})


def ack_exit(conn, pos, order, *, now_epoch, now_iso, status, reason):
    """The sweep's verdict on a nominated EXIT: 'applied' when desk_risk_exit went to the shared exit
    claim, else 'refused' (a deterministic rail won, the claim was busy, ...)."""
    try:
        _ack(conn, order, pos, now_epoch, now_iso, status, reason)
        conn.commit()
    except Exception:
        logger.exception("desk risk exit ack failed pos=%s", pos.get("id"))
