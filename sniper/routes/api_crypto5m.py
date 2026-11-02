"""Read-only API for Polymarket crypto-5m capture health."""
from __future__ import annotations

import json
import os
import random
import sqlite3
import time

from flask import Blueprint, jsonify, request

from sniper.algos.polymarket_crypto5m import forward_persistence as crypto5m_forward_persistence
from sniper.algos.polymarket_crypto5m import forward_report as crypto5m_forward_report
from sniper.algos.polymarket_crypto5m import persistence as crypto5m_persistence
from sniper.algos.polymarket_crypto5m import quant_persistence as crypto5m_quant_persistence
from sniper.algos.polymarket_crypto5m import quant_report as crypto5m_quant_report
from sniper.algos.polymarket_crypto5m import report as crypto5m_report
from sniper.algos.polymarket_crypto5m import maker_regime_report as maker_regime_report
from sniper.algos.polymarket_crypto5m import window_bootstrap as WB


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_DB = os.path.join(ROOT, "data", "crypto5m_capture.sqlite")
FORWARD_DEFAULT_DB = os.path.join(ROOT, "data", "crypto5m", "btc5m_forward_v0.sqlite")
QUANT_DEFAULT_DB = os.path.join(ROOT, "data", "crypto5m", "btc5m_quant_shadow_v4.sqlite")
LANE_DEFAULT_DB = os.path.join(ROOT, "data", "crypto5m", "lane_eval.sqlite")
ACTIVE_DEFAULT_DB = os.path.join(ROOT, "data", "crypto5m", "active_trader_v0.sqlite")
MAKER_DEFAULT_DB = os.path.join(ROOT, "data", "crypto5m", "maker_v0.sqlite")
ADAPTIVE_MAKER_DEFAULT_DB = os.path.join(ROOT, "data", "crypto5m", "maker_adaptive_v3_1.sqlite")
ADAPTIVE_CONTROL_DEFAULT_DB = os.path.join(ROOT, "data", "crypto5m", "maker_adaptive_v2.sqlite")


def _c5m_db(name: str) -> str:
    return os.path.join(ROOT, "data", "crypto5m", name)


# Whitelisted campaign registry for the adaptive cockpit + fleet board. The UI passes ?campaign=<key>;
# arbitrary DB paths from the client are NOT accepted. iso_* = the lever-isolated control for the
# second paired panel (same code except one lever); the global control is always maker-regime-v1.
ADAPTIVE_CAMPAIGN_REGISTRY: dict = {
    "v3.2f": {"db": _c5m_db("maker_adaptive_v3_2f.sqlite"), "campaign": "maker-adaptive-paper-v3.2f",
              "iso_db": _c5m_db("maker_adaptive_v3_1.sqlite"), "iso_campaign": "maker-adaptive-paper-v3.1",
              "label": "v3.2f — honest vol floor"},
    "v3.2f-frozen": {"db": _c5m_db("maker_adaptive_v3_2f_frozen.sqlite"),
                     "campaign": "maker-adaptive-paper-v3.2f-frozen",
                     "iso_db": _c5m_db("maker_adaptive_v3_2f.sqlite"),
                     "iso_campaign": "maker-adaptive-paper-v3.2f",
                     "label": "v3.2f-frozen — launch priors, learner off"},
    "v3.3L": {"db": _c5m_db("maker_v33_learner.sqlite"),
              "campaign": "maker-v33-learner-paper",
              "iso_db": _c5m_db("maker_adaptive_v3_2f_frozen.sqlite"),
              "iso_campaign": "maker-adaptive-paper-v3.2f-frozen",
              "label": "v3.3L — window-paired reward learner"},
    "regime-gated-v1": {"db": _c5m_db("maker_regime_gated_v1.sqlite"),
                        "campaign": "maker-regime-gated-v1",
                        "iso_db": _c5m_db("maker_adaptive_v3_2f_frozen.sqlite"),
                        "iso_campaign": "maker-adaptive-paper-v3.2f-frozen",
                        "label": "regime-gated-v1 — 2-state floor switch (quiet 2e-5 / contested 1e-4)"},
    "regime-gated-v2": {"db": _c5m_db("maker_regime_gated_v2.sqlite"),
                        "campaign": "maker-regime-gated-v2",
                        "iso_db": _c5m_db("maker_regime_gated_v1.sqlite"),
                        "iso_campaign": "maker-regime-gated-v1",
                        "label": "regime-gated-v2 — continuous log-floor ramp by odds-extremity"},
    "deep-only-v1": {"db": _c5m_db("maker_deep_only_v1.sqlite"),
                     "campaign": "maker-deep-only-v1",
                     "iso_db": _c5m_db("maker_adaptive_v3_2f_frozen.sqlite"),
                     "iso_campaign": "maker-adaptive-paper-v3.2f-frozen",
                     "label": "deep-only-v1 — no top-of-book bids, rest >=3c deep (profit-eater fix)"},
    "sweep-1e-5": {"db": _c5m_db("maker_sweep_1e5.sqlite"),
                   "campaign": "maker-sweep-floor-1e5",
                   "iso_db": _c5m_db("maker_adaptive_v3_2f_frozen.sqlite"),
                   "iso_campaign": "maker-adaptive-paper-v3.2f-frozen",
                   "label": "sweep 1e-5 — tight fixed floor, finish floor curve"},
    "sweep-5e-5": {"db": _c5m_db("maker_sweep_5e5.sqlite"),
                   "campaign": "maker-sweep-floor-5e5",
                   "iso_db": _c5m_db("maker_adaptive_v3_2f_frozen.sqlite"),
                   "iso_campaign": "maker-adaptive-paper-v3.2f-frozen",
                   "label": "sweep 5e-5 — medium fixed floor, finish floor curve"},
    "v3.2r": {"db": _c5m_db("maker_adaptive_v3_2r.sqlite"), "campaign": "maker-adaptive-paper-v3.2r",
              "iso_db": _c5m_db("maker_adaptive_v3_1.sqlite"), "iso_campaign": "maker-adaptive-paper-v3.1",
              "label": "v3.2r — markout reward"},
    "v3.1": {"db": _c5m_db("maker_adaptive_v3_1.sqlite"), "campaign": "maker-adaptive-paper-v3.1",
             "iso_db": _c5m_db("maker_adaptive_v2.sqlite"), "iso_campaign": "maker-adaptive-paper-v2",
             "label": "v3.1 — learner closure"},
    "v2": {"db": _c5m_db("maker_adaptive_v2.sqlite"), "campaign": "maker-adaptive-paper-v2",
           "iso_db": None, "iso_campaign": None, "label": "v2 — profile learner"},
    "v3": {"db": _c5m_db("maker_adaptive_v3.sqlite"), "campaign": "maker-adaptive-paper-v3",
           "iso_db": _c5m_db("maker_adaptive_v2.sqlite"), "iso_campaign": "maker-adaptive-paper-v2",
           "label": "v3 — retired 07-06"},
    "v1": {"db": _c5m_db("maker_adaptive_v1.sqlite"), "campaign": "maker-adaptive-paper-v1",
           "iso_db": None, "iso_campaign": None, "label": "v1 — retired 07-06"},
}
ADAPTIVE_DEFAULT_CAMPAIGN_KEY = "v3.2f"
# Fleet board rows (order = display order); control listed first.
# RETIRED 2026-07-07 (portfolio review): v3.2r/v3.1/v2 dropped from the board — all were <=+0.12/w
# absolute on the forward common set and only beat an underwater control. Registry entries kept above
# for revival/history; retiring them here just stops the board from showing dead rows.
# regime-gated-v1/v2 RETIRED 2026-07-07: clean-feature recompute refuted the crossover premise.
# v3.2f old learner and sweep-5e-4 are also retired. Board = control + champion + final learner
# read + deep-only main experiment + the two live floor sweeps finishing the curve.
FLEET_CAMPAIGN_KEYS = ("v3.2f-frozen", "v3.3L", "deep-only-v1", "sweep-1e-5", "sweep-5e-5")
PAIRED_GATE_MIN_WINDOWS = 200
# Forward-gate anchors must use the engine/Binance window_start_ts timebase. This host's local
# wall clock runs about +7h fast, so never derive campaign verdict anchors from time.time().
FORWARD_GATE_ANCHOR_TS = 1783355700
FORWARD_GATE_ANCHOR_ISO = "2026-07-06T16:35:00Z"
SELECTION_NOTE = "winner-picked-after-looking; paired gate is the honest test"
EXECUTION_HAIRCUT_CENTS = (1, 2, 3, 4)

# Gate 2 (v3.3 flow lever) status — read-only, cached: the exact pooled-obs computation parses the
# whole print tape (~70k JSON payloads), too heavy for the UI's 4s poll, cheap once per 5 min.
_FLOW_GATE_CACHE: dict = {"ts": 0.0, "data": None}
_FLOW_GATE_TTL_S = 300


def _flow_gate_status() -> dict | None:
    now = time.time()
    if now - _FLOW_GATE_CACHE["ts"] < _FLOW_GATE_TTL_S:
        return _FLOW_GATE_CACHE["data"]
    data = None
    try:
        from scripts import crypto5m_flow_validation as FV
        FV.CAPTURE_DB = os.path.join(ROOT, "data", "crypto5m_capture.sqlite")
        flow, span_days = FV.early_flow_by_condition()
        pooled = 0
        sources = [(os.path.join(ROOT, "data", "crypto5m", "maker_v0.sqlite"), "maker-regime-v1")]
        sources += [(ADAPTIVE_CAMPAIGN_REGISTRY[k]["db"], ADAPTIVE_CAMPAIGN_REGISTRY[k]["campaign"])
                    for k in FLEET_CAMPAIGN_KEYS]
        for db, camp in sources:
            if not os.path.exists(db):
                continue
            outcomes = FV.late_fill_outcomes(db, camp)
            pooled += sum(1 for cid, o in outcomes.items() if cid in flow and o["shares"] > 0)
        data = {"pooled_obs": pooled, "required_obs": 400,
                "span_days": round(span_days, 2), "required_days": 2.0,
                "ready": pooled >= 400 and span_days >= 2.0}
    except Exception:
        data = None
    _FLOW_GATE_CACHE["ts"] = now
    _FLOW_GATE_CACHE["data"] = data
    return data
DISCLAIMER = (
    "Capture and calibration health only. No paper trades, no live orders, no model edge claim."
)
FORWARD_DISCLAIMER = (
    "Frozen-rule forward PAPER campaign. Pre-registered rules, no live orders, no model edge claim. "
    "Fee is unresolved (Gamma 0.07 vs realized 0) — PnL is shown under every scenario."
)
QUANT_DISCLAIMER = (
    "Quant SHADOW cohort — records the fair-value/market comparison and a prediction per window; no "
    "orders, no entries, no edge claim. The residual split (agree vs differ from the market favorite) "
    "is the test; results are not significant until n is large."
)
LANE_DISCLAIMER = (
    "Pre-registered quant lanes (read-only). The lane SET was derived from in-sample exploration, so ONLY "
    "the OOS column is evidence — after a window-clustered bootstrap + FDR screen with a min-n gate. "
    "Family A = favorite + a quant selector (benchmark C1, via the kept-vs-skipped gap); Family B = "
    "underdog/long-shots (benchmark 0). No orders, no paper entries, no deployable-edge claim."
)
_LANE_GROUP_ORDER = (
    ("control", "Controls (incumbents)"),
    ("A", "Family A — favorite + quant selector"),
    ("B", "Family B — underdog (long shots)"),
)
LIVE_DISCLAIMER = (
    "Live read-only: it does NOT forecast where BTC is going (that horizon is ~efficient). It computes "
    "where BTC ALREADY IS vs where the book is priced, and flags a DYNAMIC entry only when the book "
    "underprices a side after fees. No orders."
)
_LIVE_CACHE: dict = {"mono": 0.0, "data": None}
ACTIVE_DISCLAIMER = (
    "PAPER directional SCALPER (no orders). It fuses momentum, microprice, order-flow imbalance and Binance "
    "buy-volume into a live read of where BTC is headed; when enough signals agree it takes the market price, "
    "rides the move, and banks the profit (exits on read-flip / trailing give-back / timeout). Realized fee 0, "
    "so a move past the ~1c spread is profit — NOT a profit claim; the forward review judges."
)
MAKER_DISCLAIMER = (
    "PAPER Binance-reactive MAKER (no orders). Posts resting quotes centered on the Binance-led fair value "
    "(which leads the book ~1s) and EARNS the spread instead of paying it. Fills are modeled conservatively "
    "(a print must trade to/through the quote). NOT a profit claim — the pre-registered window count judges."
)
MAKER_EVAL_MIN_SETTLED = 200
MAKER_INTERIM_EVAL_MIN_SETTLED = 60


def _count_by(rows, col):
    out: dict = {}
    for r in rows:
        k = r[col] or "?"
        out[k] = out.get(k, 0) + 1
    return out


def _maker_fill_attribution(fill, window, active_window_start_ts=None):
    """Explain maker fill economics without treating an open buy as realized PnL."""
    try:
        price = float(fill["price"])
        size = float(fill["size"])
    except (TypeError, ValueError):
        return {"cash_delta": None, "edge_value": None, "settlement_pnl": None,
                "window_realized_pnl": None, "window_status": "unknown"}

    side = str(fill["side"] or "").lower()
    token = str(fill["token"] or "").lower()
    is_bid = side == "bid"
    cash_delta = (-price * size) if is_bid else (price * size)
    edge_value = None
    if fill["fair"] is not None:
        fair = float(fill["fair"])
        edge_value = ((fair - price) if is_bid else (price - fair)) * size

    status = "unknown"
    window_realized = None
    settlement_pnl = None
    if window is not None:
        winner = str(window["winning_outcome"] or "").lower()
        if window["settled"] and winner == "unresolved":
            status = "unresolved"
        elif window["settled"]:
            status = "settled"
        else:
            status = "open"
            if active_window_start_ts is not None and window["window_start_ts"] != active_window_start_ts:
                status = "pending"
        window_realized = window["realized_pnl"]
        if status == "settled" and window_realized is not None and winner in ("up", "down"):
            payoff = 1.0 if token == winner else 0.0
            settlement_pnl = ((payoff - price) if is_bid else (price - payoff)) * size

    return {
        "cash_delta": round(cash_delta, 2),
        "edge_value": (round(edge_value, 2) if edge_value is not None else None),
        "settlement_pnl": (round(settlement_pnl, 2) if settlement_pnl is not None else None),
        "window_realized_pnl": (round(window_realized, 2) if window_realized is not None else None),
        "window_status": status,
    }


def _maker_ledger_state(conn, campaign: str | None, condition_id: str) -> dict:
    q = "SELECT token, side, price, size FROM maker_fills WHERE condition_id=?"
    args: list = [condition_id]
    if campaign:
        q += " AND campaign_id=?"
        args.append(campaign)
    q += " ORDER BY id"
    up_inv = down_inv = cash = 0.0
    for r in conn.execute(q, args).fetchall():
        token = str(r["token"] or "").lower()
        side = str(r["side"] or "").lower()
        price = float(r["price"] or 0.0)
        size = float(r["size"] or 0.0)
        if side == "bid":
            cash -= price * size
            if token == "up":
                up_inv += size
            elif token == "down":
                down_inv += size
        elif side == "ask":
            if token == "up":
                sold = min(size, max(0.0, up_inv))
                cash += price * sold
                up_inv -= sold
            elif token == "down":
                sold = min(size, max(0.0, down_inv))
                cash += price * sold
                down_inv -= sold
    return {"up_inv": up_inv, "down_inv": down_inv, "cash": cash}


def _maker_settlement_identity(conn, window, campaign: str | None) -> dict | None:
    if not campaign:
        # without a campaign filter the ledger replay would mix main + shadow fills for the same
        # condition_id and report a phantom identity break — refuse rather than mislead
        return None
    winner = str(window["winning_outcome"] or "").lower()
    if not window["settled"] or winner not in ("up", "down") or window["realized_pnl"] is None:
        return None
    ledger = _maker_ledger_state(conn, campaign, window["condition_id"])
    payout = ledger["up_inv"] if winner == "up" else ledger["down_inv"]
    ledger_realized = ledger["cash"] + payout
    error = float(window["realized_pnl"] or 0.0) - ledger_realized
    return {
        "ledger_up_inv": round(ledger["up_inv"], 8),
        "ledger_down_inv": round(ledger["down_inv"], 8),
        "ledger_cash": round(ledger["cash"], 8),
        "ledger_realized_pnl": round(ledger_realized, 8),
        "settlement_identity_error": round(error, 8),
        "settlement_identity_ok": abs(error) < 0.005,
    }


def _maker_identity_summary(identities: dict) -> dict:
    checked = len(identities)
    breaks = [v for v in identities.values() if not v["settlement_identity_ok"]]
    max_abs = max((abs(v["settlement_identity_error"]) for v in identities.values()), default=0.0)
    return {"checked": checked, "breaks": len(breaks), "max_abs_error": round(max_abs, 8)}


def _maker_realized_for_readout(window, identity_by_cid: dict) -> float:
    identity = identity_by_cid.get(window["condition_id"])
    if identity is not None:
        return float(identity["ledger_realized_pnl"] or 0.0)
    return float(window["realized_pnl"] or 0.0)


def _float_or_none(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _row_get(row, key, default=None):
    try:
        if key in row.keys():
            return row[key]
    except AttributeError:
        pass
    except (KeyError, IndexError, TypeError):
        return default
    return default


def _maker_window_accounting(rows, active_window_start_ts=None) -> dict:
    stats = {
        "n_open": 0,
        "n_pending": 0,
        "n_unresolved": 0,
        "open_cash": 0.0,
        "pending_cash": 0.0,
        "unresolved_cash": 0.0,
    }
    try:
        active_ws = int(active_window_start_ts) if active_window_start_ts is not None else None
    except (TypeError, ValueError):
        active_ws = None

    for r in rows:
        cash = float(r["cash"] or 0.0)
        winner = str(r["winning_outcome"] or "").lower()
        if r["settled"] and winner == "unresolved" and r["realized_pnl"] is None:
            stats["n_unresolved"] += 1
            stats["unresolved_cash"] += cash
        elif not r["settled"]:
            try:
                ws = int(r["window_start_ts"])
            except (TypeError, ValueError):
                ws = None
            if active_ws is not None and ws == active_ws:
                stats["n_open"] += 1
                stats["open_cash"] += cash
            else:
                stats["n_pending"] += 1
                stats["pending_cash"] += cash

    for key in ("open_cash", "pending_cash", "unresolved_cash"):
        stats[key] = round(stats[key], 2)
    return stats


def _maker_accounting_ladder(rows, *, settled_realized_pnl: float, current_open_mark_pnl=None,
                             active_window_start_ts=None) -> dict:
    try:
        active_ws = int(active_window_start_ts) if active_window_start_ts is not None else None
    except (TypeError, ValueError):
        active_ws = None
    pending_min = pending_max = pending_cash = 0.0
    pending_windows = 0
    unresolved_cash = 0.0
    unresolved_windows = 0
    for r in rows:
        cash = float(r["cash"] or 0.0)
        winner = str(r["winning_outcome"] or "").lower()
        if r["settled"] and winner == "unresolved" and r["realized_pnl"] is None:
            unresolved_windows += 1
            unresolved_cash += cash
            continue
        if r["settled"]:
            continue
        try:
            ws = int(r["window_start_ts"])
        except (TypeError, ValueError):
            ws = None
        if active_ws is not None and ws == active_ws:
            continue
        up_inv = float(r["up_inv"] or 0.0)
        down_inv = float(r["down_inv"] or 0.0)
        pending_windows += 1
        pending_cash += cash
        pending_min += cash + min(up_inv, down_inv)
        pending_max += cash + max(up_inv, down_inv)
    open_mark = _float_or_none(current_open_mark_pnl)
    return {
        "settled_realized_pnl": round(float(settled_realized_pnl or 0.0), 2),
        "current_open_mark_pnl": (round(open_mark, 2) if open_mark is not None else None),
        "settled_plus_open_mark_pnl": round(float(settled_realized_pnl or 0.0) + (open_mark or 0.0), 2),
        "pending_windows": pending_windows,
        "pending_cash": round(pending_cash, 2),
        "pending_payout_min": round(pending_min, 2),
        "pending_payout_max": round(pending_max, 2),
        "unresolved_windows": unresolved_windows,
        "unresolved_cash": round(unresolved_cash, 2),
        "note": "settled is locked; open is marked live; pending waits for market resolution and is not counted",
    }


def _table_exists(conn, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _table_columns(conn, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _empty_regime_shadow_report() -> dict:
    return {
        "available": False,
        "campaign": None,
        "windows": {"n_open": 0, "n_settled": 0, "n_unresolved": 0, "realized_pnl": 0.0},
        "by_regime": {},
        "recent_decisions": [],
    }


def _maker_regime_state(think: dict | None) -> dict:
    if not think:
        return {"available": False}
    episode = think.get("regime_episode") or {}
    profiles = think.get("profile_decisions") or {}
    active = think.get("regime_active") or {}
    return {
        "available": bool(episode or active or profiles),
        "episode": episode,
        "current_regime": episode.get("current_regime"),
        "dwell_seconds": episode.get("dwell_seconds"),
        "moment_key": episode.get("moment_key") or {},
        "profile_mode": next((p.get("profile_mode") for p in profiles.values() if isinstance(p, dict)), None),
        "profile_decisions": profiles,
        "active_tokens": active,
    }


def _maker_campaign_snapshot(conn, campaign_hint: str | None = None) -> dict:
    srow = None
    think = None
    if _table_exists(conn, "maker_state"):
        srow = conn.execute("SELECT think_json, ts FROM maker_state WHERE id=1").fetchone()
        think = json.loads(srow["think_json"]) if srow and srow["think_json"] else None
    campaign = (think or {}).get("campaign") or campaign_hint
    rows = []
    if campaign and _table_exists(conn, "maker_windows"):
        cols = _table_columns(conn, "maker_windows")
        if "campaign_id" in cols:
            rows = conn.execute(
                "SELECT * FROM maker_windows WHERE campaign_id=? ORDER BY window_start_ts DESC",
                (campaign,),
            ).fetchall()
    settled = [r for r in rows if r["settled"] and r["realized_pnl"] is not None]
    accounting = _maker_window_accounting(rows, (think or {}).get("window_start_ts"))
    fill_shares_by_window = _fill_shares_by_window(conn, campaign)
    return {
        "available": True,
        "campaign": campaign,
        "engine_age_s": (max(0, int(time.time()) - int(srow["ts"] or 0)) if srow else None),
        "n_settled": len(settled),
        "realized_pnl": round(sum(float(r["realized_pnl"] or 0.0) for r in settled), 2),
        "latest_window_start_ts": (think or {}).get("window_start_ts"),
        "latest_condition_id": (think or {}).get("condition_id"),
        **accounting,
        "_execution_haircut_panel": _execution_haircut_panel_from_rows(campaign, settled, fill_shares_by_window),
        "_fill_shares_by_window": fill_shares_by_window,
        "_think": think,
        "_settled_windows": settled,
        "_all_windows": rows,
    }


def _public_snapshot(snapshot: dict, db_path: str) -> dict:
    out = {k: v for k, v in snapshot.items() if not k.startswith("_")}
    out["db_path"] = db_path
    return out


def _adaptive_current_state(think: dict | None) -> dict:
    if not think:
        return {"available": False, "profiles_by_token": {}}
    adaptive = think.get("adaptive_paper") or {}
    episode = think.get("regime_episode") or {}
    return {
        "available": bool(adaptive),
        "condition_id": think.get("condition_id"),
        "window_start_ts": think.get("window_start_ts"),
        "seconds_left": think.get("seconds_left"),
        "fair_value_up": think.get("fair_value_up"),
        "regime": episode.get("current_regime"),
        "dwell_seconds": episode.get("dwell_seconds"),
        "moment_key": episode.get("moment_key") or {},
        "selector_version": adaptive.get("selector_version"),
        "reward_version": adaptive.get("reward_version"),
        "profiles_by_token": adaptive.get("profiles") or {},
    }


def _adaptive_v3_current_state(think: dict | None) -> dict:
    if not think:
        return {"available": False, "tokens": {}}
    adaptive = think.get("adaptive_v3") or {}
    episode = think.get("regime_episode") or {}
    return {
        "available": bool(adaptive),
        "condition_id": think.get("condition_id"),
        "window_start_ts": think.get("window_start_ts"),
        "seconds_left": think.get("seconds_left"),
        "fair_value_up": think.get("fair_value_up"),
        "regime": episode.get("current_regime"),
        "dwell_seconds": episode.get("dwell_seconds"),
        "moment_key": episode.get("moment_key") or {},
        "selector_version": adaptive.get("selector_version"),
        "reward_version": adaptive.get("reward_version"),
        "ablation_groups": adaptive.get("ablation_groups") or [],
        "tokens": adaptive.get("tokens") or {},
    }


def _paired_window_summary(control_rows, adaptive_rows, *, forward_anchor_ts: int = FORWARD_GATE_ANCHOR_TS) -> tuple[list[dict], dict]:
    control_by_ws = {int(r["window_start_ts"]): r for r in control_rows if r["window_start_ts"] is not None}
    adaptive_by_ws = {int(r["window_start_ts"]): r for r in adaptive_rows if r["window_start_ts"] is not None}
    paired = []
    raw_deltas = []
    forward_deltas = []
    for ws in sorted(set(control_by_ws) & set(adaptive_by_ws), reverse=True):
        c = control_by_ws[ws]
        a = adaptive_by_ws[ws]
        cpnl = float(c["realized_pnl"] or 0.0)
        apnl = float(a["realized_pnl"] or 0.0)
        delta = apnl - cpnl
        raw_deltas.append(delta)
        if ws >= int(forward_anchor_ts):
            forward_deltas.append(delta)
        paired.append({
            "window_start_ts": ws,
            "control_realized_pnl": round(cpnl, 2),
            "adaptive_realized_pnl": round(apnl, 2),
            "delta_pnl": round(apnl - cpnl, 2),
            "control_winning_outcome": c["winning_outcome"],
            "adaptive_winning_outcome": a["winning_outcome"],
            "status": "paired",
        })
    deltas = raw_deltas
    summary = {
        "n_paired": len(paired),
        "adaptive_minus_control": round(sum(deltas), 2),
        "adaptive_wins": sum(1 for d in deltas if d > 0),
        "control_wins": sum(1 for d in deltas if d < 0),
        "mean_delta": round(sum(deltas) / len(deltas), 2) if deltas else 0.0,
        "median_delta": round(_median(deltas), 2) if deltas else 0.0,
        "selection_note": SELECTION_NOTE,
        "pre_registered_gate": _paired_gate(deltas),
        "forward_pre_registered_gate": _forward_pre_registered_gate(forward_deltas, forward_anchor_ts=forward_anchor_ts),
    }
    return paired, summary


def _round_or_none(x, ndigits=3):
    return None if x is None else round(float(x), ndigits)


def _paired_gate(deltas: list[float], *, min_windows: int = PAIRED_GATE_MIN_WINDOWS) -> dict:
    n = len(deltas)
    out = {
        "comparison": "paired_vs_control",
        "n_paired": n,
        "min_paired_windows": int(min_windows),
        "eval_ready": n >= int(min_windows),
        "ci_low": None,
        "ci_high": None,
        "status": "insufficient_sample",
        "recommendation": "keep_collecting",
        "reason": "below paired-window sample gate",
    }
    if not deltas:
        return out
    means = WB.boot_means(
        {i: [float(d)] for i, d in enumerate(deltas)},
        random.Random(WB.BOOT_SEED),
        nb=WB.BOOT_N,
    )
    ci_low = WB.pctile(means, 0.025)
    ci_high = WB.pctile(means, 0.975)
    out["ci_low"] = _round_or_none(ci_low)
    out["ci_high"] = _round_or_none(ci_high)
    if not out["eval_ready"]:
        return out
    if ci_low is not None and ci_low > 0:
        out.update({
            "status": "candidate_good",
            "recommendation": "candidate_good",
            "reason": "paired-window CI lower bound is above zero",
        })
    elif ci_high is not None and ci_high < 0:
        out.update({
            "status": "candidate_bad",
            "recommendation": "reject_challenger",
            "reason": "paired-window CI upper bound is below zero",
        })
    else:
        out.update({
            "status": "needs_review",
            "recommendation": "keep_collecting",
            "reason": "paired-window CI overlaps zero",
        })
    return out


def _forward_pre_registered_gate(deltas: list[float], *, forward_anchor_ts: int = FORWARD_GATE_ANCHOR_TS) -> dict:
    out = _paired_gate(deltas)
    out["anchor_ts"] = int(forward_anchor_ts)
    out["anchor_iso"] = FORWARD_GATE_ANCHOR_ISO if int(forward_anchor_ts) == FORWARD_GATE_ANCHOR_TS else None
    out["scope"] = "forward_windows_only"
    out["note"] = "binding verdict excludes windows settled before the gate was pre-registered"
    return out


def _fill_shares_by_window(conn, campaign: str | None) -> dict[int, dict[str, float]]:
    if not campaign or not _table_exists(conn, "maker_fills"):
        return {}
    cols = _table_columns(conn, "maker_fills")
    required = {"campaign_id", "window_start_ts", "size", "fill_model"}
    if not required.issubset(cols):
        return {}
    out: dict[int, dict[str, float]] = {}
    for r in conn.execute(
        "SELECT window_start_ts,size,fill_model FROM maker_fills WHERE campaign_id=?",
        (campaign,),
    ).fetchall():
        if r["window_start_ts"] is None:
            continue
        ws = int(r["window_start_ts"])
        bucket = out.setdefault(ws, {"print": 0.0, "hard_cut": 0.0, "total": 0.0})
        size = float(r["size"] or 0.0)
        bucket["total"] += size
        model = r["fill_model"] or "unknown"
        if model == "print":
            bucket["print"] += size
        elif model == "hard-cut":
            bucket["hard_cut"] += size
    return out


def _execution_haircut_panel(conn, campaign: str | None, settled_rows) -> dict:
    return _execution_haircut_panel_from_rows(campaign, settled_rows, _fill_shares_by_window(conn, campaign))


def _execution_haircut_panel_from_rows(campaign: str | None, settled_rows, shares_by_window: dict[int, dict[str, float]] | None) -> dict:
    base = sum(float(r["realized_pnl"] or 0.0) for r in settled_rows or [])
    out = {
        "available": False,
        "campaign": campaign,
        "base_realized_pnl": round(base, 2),
        "settled_fill_shares": 0.0,
        "settled_print_shares": 0.0,
        "settled_hard_cut_shares": 0.0,
        "rows": [],
        "note": "Per-share haircut stress: print drag approximates queue/latency/missed-fill cost; total drag also charges hard-cut exits.",
    }
    if not campaign or not settled_rows or not shares_by_window:
        return out

    print_shares = 0.0
    hard_cut_shares = 0.0
    total_shares = 0.0
    for r in settled_rows:
        if r["window_start_ts"] is None:
            continue
        shares = shares_by_window.get(int(r["window_start_ts"])) or {}
        print_shares += float(shares.get("print") or 0.0)
        hard_cut_shares += float(shares.get("hard_cut") or 0.0)
        total_shares += float(shares.get("total") or 0.0)

    out["available"] = total_shares > 0
    out["settled_fill_shares"] = round(total_shares, 2)
    out["settled_print_shares"] = round(print_shares, 2)
    out["settled_hard_cut_shares"] = round(hard_cut_shares, 2)
    rows = []
    for cents in EXECUTION_HAIRCUT_CENTS:
        per_share = float(cents) / 100.0
        print_drag = print_shares * per_share
        hard_drag = hard_cut_shares * per_share
        total_drag = (print_shares + hard_cut_shares) * per_share
        rows.append({
            "haircut_cents": int(cents),
            "print_fill_drag": round(print_drag, 2),
            "hard_cut_drag": round(hard_drag, 2),
            "total_drag": round(total_drag, 2),
            "pnl_after_print_haircut": round(base - print_drag, 2),
            "pnl_after_total_haircut": round(base - total_drag, 2),
        })
    out["rows"] = rows
    return out


def _paired_haircut_delta_panel(control_panel: dict | None, adaptive_panel: dict | None) -> dict:
    out = {
        "available": False,
        "unstressed_delta": None,
        "rows": [],
        "note": "Stresses both paired campaigns at the same per-share haircut, then reports adaptive minus control.",
    }
    if not control_panel or not adaptive_panel:
        return out
    c_base = float(control_panel.get("base_realized_pnl") or 0.0)
    a_base = float(adaptive_panel.get("base_realized_pnl") or 0.0)
    out["unstressed_delta"] = round(a_base - c_base, 2)
    c_rows = {int(r["haircut_cents"]): r for r in control_panel.get("rows") or []}
    a_rows = {int(r["haircut_cents"]): r for r in adaptive_panel.get("rows") or []}
    rows = []
    for cents in EXECUTION_HAIRCUT_CENTS:
        c = c_rows.get(int(cents))
        a = a_rows.get(int(cents))
        if not c or not a:
            continue
        rows.append({
            "haircut_cents": int(cents),
            "control_total_drag": round(float(c.get("total_drag") or 0.0), 2),
            "adaptive_total_drag": round(float(a.get("total_drag") or 0.0), 2),
            "unstressed_delta": round(a_base - c_base, 2),
            "delta_after_print_haircut": round(
                float(a.get("pnl_after_print_haircut") or 0.0)
                - float(c.get("pnl_after_print_haircut") or 0.0),
                2,
            ),
            "delta_after_total_haircut": round(
                float(a.get("pnl_after_total_haircut") or 0.0)
                - float(c.get("pnl_after_total_haircut") or 0.0),
                2,
            ),
        })
    out["rows"] = rows
    out["available"] = bool(rows)
    return out


def _median(values: list[float]) -> float:
    vals = sorted(values)
    if not vals:
        return 0.0
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2


def _adaptive_profile_stats(conn, campaign: str | None) -> list[dict]:
    if not campaign or not _table_exists(conn, "crypto5m_maker_profile_stats"):
        return []
    rows = conn.execute(
        """SELECT * FROM crypto5m_maker_profile_stats
           WHERE campaign_id=?
           ORDER BY disabled DESC, n_fills DESC, n_decisions DESC
           LIMIT 40""",
        (campaign,),
    ).fetchall()
    out = []
    for r in rows:
        n_quotes = int(r["n_quotes"] or 0)
        n_fills = int(r["n_fills"] or 0)
        reward_sum = float(r["reward_sum"] or 0.0)
        out.append({
            "context_key": r["context_key"],
            "profile_id": r["profile_id"],
            "n_decisions": int(r["n_decisions"] or 0),
            "n_quotes": n_quotes,
            "n_fills": n_fills,
            "fill_rate": round(n_fills / n_quotes, 4) if n_quotes else 0.0,
            "mean_reward": round(reward_sum / n_fills, 6) if n_fills else 0.0,
            "pnl_sum": round(float(r["pnl_sum"] or 0.0), 6),
            "reward_sum": round(reward_sum, 6),
            "hard_cuts": int(r["hard_cuts"] or 0),
            "disabled": bool(r["disabled"]),
            "last_updated_ts": r["last_updated_ts"],
        })
    return out


def _adaptive_disabled_profiles(conn, campaign: str | None) -> list[dict]:
    if not campaign or not _table_exists(conn, "crypto5m_maker_profile_disables"):
        return []
    return [
        {"context_key": r["context_key"], "profile_id": r["profile_id"],
         "disabled_at": r["disabled_at"], "disabled_reason": r["disabled_reason"],
         "selector_version": r["selector_version"], "reward_version": r["reward_version"]}
        for r in conn.execute(
            """SELECT * FROM crypto5m_maker_profile_disables
               WHERE campaign_id=? ORDER BY disabled_at DESC LIMIT 30""",
            (campaign,),
        ).fetchall()
    ]


def _adaptive_recent_fills(conn, campaign: str | None) -> list[dict]:
    if not campaign or not _table_exists(conn, "maker_fills"):
        return []
    has_rewards = _table_exists(conn, "crypto5m_maker_adaptive_rewards")
    if has_rewards:
        rows = conn.execute(
            """SELECT f.*, r.reward, r.fee_adjusted_pnl, r.hard_cut_penalty, r.reward_version
               FROM maker_fills f
               LEFT JOIN crypto5m_maker_adaptive_rewards r
                 ON r.campaign_id=f.campaign_id AND r.fill_id=f.id
               WHERE f.campaign_id=?
               ORDER BY f.ts DESC, f.id DESC LIMIT 30""",
            (campaign,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM maker_fills WHERE campaign_id=? ORDER BY ts DESC, id DESC LIMIT 30",
            (campaign,),
        ).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r["id"], "ts": r["ts"], "condition_id": r["condition_id"],
            "window_start_ts": r["window_start_ts"], "token": r["token"], "side": r["side"],
            "price": r["price"], "size": r["size"], "fair": r["fair"],
            "fill_model": _row_get(r, "fill_model"), "profile_id": _row_get(r, "fill_profile_id")
            or _row_get(r, "profile_id"), "selection_mode": _row_get(r, "selection_mode"),
            "adaptive_context_key": _row_get(r, "adaptive_context_key"), "reason": r["reason"],
            "reward": _row_get(r, "reward"), "fee_adjusted_pnl": _row_get(r, "fee_adjusted_pnl"),
            "hard_cut_penalty": _row_get(r, "hard_cut_penalty"),
            "reward_version": _row_get(r, "reward_version"),
        })
    return out


def _adaptive_v3_action_stats(conn, campaign: str | None) -> list[dict]:
    if not campaign or not _table_exists(conn, "crypto5m_maker_v3_action_arm_stats"):
        return []
    rows = conn.execute(
        """SELECT *
           FROM crypto5m_maker_v3_action_arm_stats
           WHERE campaign_id=?
           ORDER BY paper_preferred DESC, disabled DESC, n_fills DESC, n_decisions DESC
           LIMIT 40""",
        (campaign,),
    ).fetchall()
    out = []
    for r in rows:
        n_quotes = int(r["n_quotes"] or 0)
        n_fills = int(r["n_fills"] or 0)
        reward_sum = float(r["reward_sum"] or 0.0)
        out.append({
            "context_key": r["context_key"],
            "arm_id": r["arm_id"],
            "n_decisions": int(r["n_decisions"] or 0),
            "n_quotes": n_quotes,
            "n_fills": n_fills,
            "fill_rate": round(n_fills / n_quotes, 4) if n_quotes else 0.0,
            "mean_reward": round(reward_sum / n_fills, 6) if n_fills else 0.0,
            "reward_sum": round(reward_sum, 6),
            "adverse_sum": round(float(r["adverse_sum"] or 0.0), 6),
            "disabled": bool(r["disabled"]),
            "paper_preferred": bool(r["paper_preferred"]),
            "status": _row_get(r, "status"),
            "disable_type": _row_get(r, "disable_type"),
            "disabled_reason": _row_get(r, "disabled_reason"),
            "paper_preferred_reason": _row_get(r, "paper_preferred_reason"),
            "confidence_low": _row_get(r, "confidence_low"),
            "confidence_high": _row_get(r, "confidence_high"),
            "max_window_fill_share": _row_get(r, "max_window_fill_share"),
            "last_updated_ts": r["last_updated_ts"],
        })
    return out


def _adaptive_v3_fair_stats(conn, campaign: str | None) -> list[dict]:
    if not campaign or not _table_exists(conn, "crypto5m_maker_v3_fair_blend_stats"):
        return []
    rows = conn.execute(
        """SELECT *
           FROM crypto5m_maker_v3_fair_blend_stats
           WHERE campaign_id=?
           ORDER BY n_observations DESC, last_updated_ts DESC
           LIMIT 30""",
        (campaign,),
    ).fetchall()
    out = []
    for r in rows:
        n = int(r["n_observations"] or 0)
        out.append({
            "fair_context_key": r["fair_context_key"],
            "blend_weight": r["blend_weight"],
            "best_blend_arm": _row_get(r, "best_blend_arm"),
            "n_observations": n,
            "brier_mean": round(float(_row_get(r, "brier_sum") or 0.0) / n, 6) if n else 0.0,
            "log_loss_mean": round(float(_row_get(r, "log_loss_sum") or 0.0) / n, 6) if n else 0.0,
            "calibration_abs_mean": (
                round(float(_row_get(r, "calibration_abs_sum") or 0.0) / n, 6) if n else 0.0
            ),
            "correct_rate": round(float(_row_get(r, "settled_correct_sum") or 0.0) / n, 4) if n else 0.0,
            "last_updated_ts": r["last_updated_ts"],
        })
    return out


def _adaptive_v3_selector_sources(conn, campaign: str | None) -> dict:
    if not campaign or not _table_exists(conn, "crypto5m_maker_v3_policy_actions"):
        return {}
    return {
        r["selector_source"] or "unknown": int(r["n"] or 0)
        for r in conn.execute(
            """SELECT selector_source, COUNT(*) n
               FROM crypto5m_maker_v3_policy_actions
               WHERE campaign_id=?
               GROUP BY selector_source
               ORDER BY n DESC""",
            (campaign,),
        ).fetchall()
    }


def _adaptive_v3_recent_actions(conn, campaign: str | None) -> list[dict]:
    if not campaign or not _table_exists(conn, "crypto5m_maker_v3_policy_actions"):
        return []
    return [
        {
            "id": r["id"],
            "condition_id": r["condition_id"],
            "window_start_ts": r["window_start_ts"],
            "token": r["token"],
            "context_key": r["context_key"],
            "parent_context_used": r["parent_context_used"],
            "fair_model_id": r["fair_model_id"],
            "q_structural": r["q_structural"],
            "q_market_mid": r["q_market_mid"],
            "q_used": r["q_used"],
            "fair_blend_weight": r["fair_blend_weight"],
            "fair_confidence": r["fair_confidence"],
            "risk_state": r["risk_state"],
            "tail_risk_score": r["tail_risk_score"],
            "quote_action_arm": r["quote_action_arm"],
            "selector_source": r["selector_source"],
            "exploration_flag": bool(r["exploration_flag"]),
            "created_ts": r["created_ts"],
        }
        for r in conn.execute(
            """SELECT *
               FROM crypto5m_maker_v3_policy_actions
               WHERE campaign_id=?
               ORDER BY created_ts DESC, id DESC LIMIT 30""",
            (campaign,),
        ).fetchall()
    ]


def _adaptive_v3_ablations(conn, campaign: str | None) -> dict:
    if not campaign or not _table_exists(conn, "crypto5m_maker_v3_ablations"):
        return {"groups": [], "recent": []}
    groups = [
        r["ablation_group"]
        for r in conn.execute(
            """SELECT DISTINCT ablation_group
               FROM crypto5m_maker_v3_ablations
               WHERE campaign_id=?
               ORDER BY ablation_group""",
            (campaign,),
        ).fetchall()
    ]
    recent = [
        {
            "ablation_group": r["ablation_group"],
            "token": r["token"],
            "selected_arm": r["selected_arm"],
            "q_used": r["q_used"],
            "risk_state": r["risk_state"],
            "created_ts": r["created_ts"],
        }
        for r in conn.execute(
            """SELECT *
               FROM crypto5m_maker_v3_ablations
               WHERE campaign_id=?
               ORDER BY created_ts DESC, id DESC LIMIT 30""",
            (campaign,),
        ).fetchall()
    ]
    return {"groups": groups, "recent": recent}


def _regime_bucket(by_regime: dict, regime_id: str | None) -> dict:
    key = regime_id or "unknown"
    if key not in by_regime:
        by_regime[key] = {
            "decisions": 0,
            "quotes_posted": 0,
            "quotes_pulled": 0,
            "fills": 0,
            "cash_delta": 0.0,
            "edge_value": 0.0,
            "realized_attr": 0.0,
            "attr_windows": 0,
            "fill_models": {},
            "hard_cuts": 0,
        }
    return by_regime[key]


def _attribute_settled_realized(by_regime: dict, fills_by_window: dict, winners: dict) -> None:
    """Per-regime REALIZED attribution over settled windows: each regime's bids pay cash and build its own
    inventory bucket; asks credit the selling regime's cash, drawing shares pro-rata across buckets; at
    settlement each bucket's remaining winner shares pay $1. Sums to the windows' ledger realized. This is
    what raw cash_delta can't show — a bid-heavy regime looks like a loser while holding shares that pay out."""
    for cid, fills in fills_by_window.items():
        winner = winners.get(cid)
        if winner not in ("up", "down"):
            continue
        inv: dict = {}
        touched = set()
        for f in fills:
            key = f["regime_id"] or "unknown"
            b = _regime_bucket(by_regime, key)
            touched.add(key)
            token, price, size = f["token"], float(f["price"] or 0.0), float(f["size"] or 0.0)
            if f["side"] == "bid":
                b["realized_attr"] -= price * size
                inv.setdefault(key, {}).setdefault(token, 0.0)
                inv[key][token] += size
            else:
                b["realized_attr"] += price * size
                total_held = sum(v.get(token, 0.0) for v in inv.values())
                if total_held > 0:
                    for k in inv:
                        held = inv[k].get(token, 0.0)
                        inv[k][token] = held - size * held / total_held
        for key, tokens in inv.items():
            payout = tokens.get(winner, 0.0)
            if payout > 1e-9:
                _regime_bucket(by_regime, key)["realized_attr"] += payout
        for key in touched:
            by_regime[key]["attr_windows"] += 1


def _maker_regime_shadow_report(conn, live_campaign: str | None, think: dict | None) -> dict:
    out = _empty_regime_shadow_report()
    if not _table_exists(conn, "maker_regime_decisions"):
        return out

    shadow_campaign = ((think or {}).get("regime_shadow") or {}).get("campaign")
    if not shadow_campaign and (think or {}).get("regime_active"):
        return out
    if not shadow_campaign:
        row = conn.execute(
            "SELECT campaign_id, MAX(ts) last_ts FROM maker_regime_decisions "
            "WHERE campaign_id IS NOT NULL AND campaign_id != ? "
            "GROUP BY campaign_id ORDER BY last_ts DESC LIMIT 1",
            (live_campaign or "",),
        ).fetchone()
        shadow_campaign = row["campaign_id"] if row else None
    if not shadow_campaign:
        return out

    by_regime: dict = {}
    for r in conn.execute(
        "SELECT regime_id, COUNT(*) decisions FROM maker_regime_decisions "
        "WHERE campaign_id=? GROUP BY regime_id",
        (shadow_campaign,),
    ).fetchall():
        _regime_bucket(by_regime, r["regime_id"])["decisions"] = int(r["decisions"] or 0)

    qcols = _table_columns(conn, "maker_quotes")
    if {"regime_id", "status", "campaign_id"}.issubset(qcols):
        for r in conn.execute(
            "SELECT regime_id, status, COUNT(*) c FROM maker_quotes "
            "WHERE campaign_id=? GROUP BY regime_id, status",
            (shadow_campaign,),
        ).fetchall():
            b = _regime_bucket(by_regime, r["regime_id"])
            if r["status"] == "posted":
                b["quotes_posted"] += int(r["c"] or 0)
            else:
                b["quotes_pulled"] += int(r["c"] or 0)

    fcols = _table_columns(conn, "maker_fills")
    fills_by_window: dict = {}
    if {"regime_id", "campaign_id", "side", "price", "size", "fair", "fill_model"}.issubset(fcols):
        for r in conn.execute("SELECT * FROM maker_fills WHERE campaign_id=? ORDER BY id",
                              (shadow_campaign,)).fetchall():
            b = _regime_bucket(by_regime, r["regime_id"])
            b["fills"] += 1
            econ = _maker_fill_attribution(r, None)
            if econ["cash_delta"] is not None:
                b["cash_delta"] += econ["cash_delta"]
            if econ["edge_value"] is not None:
                b["edge_value"] += econ["edge_value"]
            model = r["fill_model"] or "untagged"
            b["fill_models"][model] = b["fill_models"].get(model, 0) + 1
            if model == "hard-cut":
                b["hard_cuts"] += 1
            if r["condition_id"]:
                fills_by_window.setdefault(r["condition_id"], []).append(r)

    if fills_by_window and _table_exists(conn, "maker_shadow_windows"):
        winners = {r["condition_id"]: str(r["winning_outcome"] or "").lower()
                   for r in conn.execute(
                       "SELECT condition_id, winning_outcome FROM maker_shadow_windows "
                       "WHERE campaign_id=? AND settled=1 AND realized_pnl IS NOT NULL",
                       (shadow_campaign,)).fetchall()}
        _attribute_settled_realized(by_regime, fills_by_window, winners)

    for b in by_regime.values():
        b["cash_delta"] = round(b["cash_delta"], 2)
        b["edge_value"] = round(b["edge_value"], 2)
        b["realized_attr"] = round(b["realized_attr"], 2)

    windows = out["windows"]
    if _table_exists(conn, "maker_shadow_windows"):
        row = conn.execute(
            "SELECT "
            "SUM(CASE WHEN settled=0 THEN 1 ELSE 0 END) n_open, "
            "SUM(CASE WHEN settled=1 AND realized_pnl IS NOT NULL THEN 1 ELSE 0 END) n_settled, "
            "SUM(CASE WHEN settled=1 AND winning_outcome='unresolved' THEN 1 ELSE 0 END) n_unresolved, "
            "SUM(CASE WHEN settled=1 AND realized_pnl IS NOT NULL THEN realized_pnl ELSE 0 END) realized_pnl "
            "FROM maker_shadow_windows WHERE campaign_id=?",
            (shadow_campaign,),
        ).fetchone()
        if row:
            windows = {
                "n_open": int(row["n_open"] or 0),
                "n_settled": int(row["n_settled"] or 0),
                "n_unresolved": int(row["n_unresolved"] or 0),
                "realized_pnl": round(row["realized_pnl"] or 0.0, 2),
            }

    recent = [
        {
            "ts": r["ts"],
            "window_start_ts": r["window_start_ts"],
            "condition_id": r["condition_id"],
            "token": r["token"],
            "regime_id": r["regime_id"],
            "policy_id": r["policy_id"],
            "risk_action": r["risk_action"],
            "reason": r["route_reason"],
        }
        for r in conn.execute(
            "SELECT * FROM maker_regime_decisions WHERE campaign_id=? ORDER BY ts DESC, id DESC LIMIT 20",
            (shadow_campaign,),
        ).fetchall()
    ]

    return {
        "available": bool(by_regime or windows["n_open"] or windows["n_settled"]),
        "campaign": shadow_campaign,
        "windows": windows,
        "by_regime": by_regime,
        "recent_decisions": recent,
    }


def _empty_report() -> dict:
    """A zeroed report in the CURRENT ``build_report`` schema, for the DB-not-found fallback. Built
    from an in-memory schema so it can never drift from the real report's keys."""
    conn = crypto5m_persistence.connect(":memory:")
    try:
        crypto5m_persistence.ensure_schema(conn)
        return crypto5m_report.build_report(conn)
    finally:
        conn.close()


def _empty_forward_report() -> dict:
    """A zeroed forward report in the current schema, for the DB-not-found fallback."""
    conn = crypto5m_persistence.connect(":memory:")
    try:
        crypto5m_forward_persistence.ensure_forward_schema(conn)
        return crypto5m_forward_report.build_forward_report(conn)
    finally:
        conn.close()


def _empty_quant_report() -> dict:
    """A zeroed quant report in the current schema, for the DB-not-found fallback."""
    conn = crypto5m_persistence.connect(":memory:")
    try:
        crypto5m_quant_persistence.ensure_quant_schema(conn)
        return crypto5m_quant_report.build_quant_report(conn)
    finally:
        conn.close()


def _open_readonly(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _live_lane_picks(shadow_db: str) -> dict:
    """For each lane, its MOST RECENT actual decision (side, price, decision_ts, won-if-settled) computed
    live from the quant-shadow matrix — so the board shows the lanes deciding, not just a static 'accruing'.
    Returns {} on any failure (the board still renders without the live column)."""
    if not os.path.exists(shadow_db):
        return {}
    try:
        from scripts import crypto5m_lane_eval as lane_eval
        conn = _open_readonly(shadow_db)
        try:
            windows = lane_eval.load_windows(conn)
        finally:
            conn.close()
        out: dict = {}
        items = list(windows.items())
        for name, fn in lane_eval.LANES.items():
            best = None
            for _cid, rec in items:
                pick = fn(rec)
                if not pick:
                    continue
                cp, side, price = pick
                ts = rec["_ts"].get(cp, 0)
                if best is None or ts > best["ts"]:
                    won = (1 if rec.get("_win") == side else 0) if rec.get("_win") in ("Up", "Down") else None
                    best = {"side": side, "price": price, "ts": ts, "won": won}
            out[name] = best
        return out
    except Exception:  # noqa: BLE001 — live column is best-effort; never break the board
        return {}


def create_api_crypto5m_blueprint(ctx=None) -> Blueprint:
    bp = Blueprint("api_crypto5m", __name__)
    ctx = ctx or {}

    @bp.get("/api/crypto5m/status")
    def status():
        db_path = ctx.get("db_path") or os.getenv("CRYPTO5M_CAPTURE_DB") or DEFAULT_DB
        if not os.path.exists(db_path):
            return jsonify({
                "available": False,
                "phase": 0,
                "db_path": db_path,
                "message": "capture DB not found",
                "report": _empty_report(),
                "disclaimer": DISCLAIMER,
            })

        try:
            conn = _open_readonly(db_path)
            try:
                report = crypto5m_report.build_report(conn)
            finally:
                conn.close()
            return jsonify({
                "available": True,
                "phase": report.get("phase", 0),
                "db_path": db_path,
                "report": report,
                "disclaimer": DISCLAIMER,
            })
        except sqlite3.Error as e:
            return jsonify({
                "available": False,
                "phase": 0,
                "db_path": db_path,
                "message": f"capture DB unavailable: {e}",
                "report": _empty_report(),
                "disclaimer": DISCLAIMER,
            })

    @bp.get("/api/crypto5m/forward-status")
    def forward_status():
        db_path = ctx.get("forward_db_path") or os.getenv("BTC5M_FORWARD_DB") or FORWARD_DEFAULT_DB
        if not os.path.exists(db_path):
            return jsonify({
                "available": False,
                "db_path": db_path,
                "message": "forward paper DB not found",
                "report": _empty_forward_report(),
                "disclaimer": FORWARD_DISCLAIMER,
            })
        try:
            conn = _open_readonly(db_path)
            try:
                report = crypto5m_forward_report.build_forward_report(conn)
            finally:
                conn.close()
            return jsonify({
                "available": True,
                "db_path": db_path,
                "report": report,
                "disclaimer": FORWARD_DISCLAIMER,
            })
        except sqlite3.Error as e:
            return jsonify({
                "available": False,
                "db_path": db_path,
                "message": f"forward paper DB unavailable: {e}",
                "report": _empty_forward_report(),
                "disclaimer": FORWARD_DISCLAIMER,
            })

    @bp.get("/api/crypto5m/quant-status")
    def quant_status():
        db_path = ctx.get("quant_db_path") or os.getenv("BTC5M_QUANT_DB") or QUANT_DEFAULT_DB
        if not os.path.exists(db_path):
            return jsonify({
                "available": False,
                "db_path": db_path,
                "message": "quant shadow DB not found",
                "report": _empty_quant_report(),
                "disclaimer": QUANT_DISCLAIMER,
            })
        try:
            conn = _open_readonly(db_path)
            try:
                report = crypto5m_quant_report.build_quant_report(conn)
            finally:
                conn.close()
            return jsonify({
                "available": True,
                "db_path": db_path,
                "report": report,
                "disclaimer": QUANT_DISCLAIMER,
            })
        except sqlite3.Error as e:
            return jsonify({
                "available": False,
                "db_path": db_path,
                "message": f"quant shadow DB unavailable: {e}",
                "report": _empty_quant_report(),
                "disclaimer": QUANT_DISCLAIMER,
            })

    @bp.get("/api/crypto5m/active")
    def active():
        """The REAL continuous engine: its latest think, current position + unrealized PnL, session realized
        PnL, and recent trades — read from the active-trader paper DB (the runner updates it every ~3s)."""
        db_path = ctx.get("active_db_path") or os.getenv("CRYPTO5M_ACTIVE_DB") or ACTIVE_DEFAULT_DB
        out = {"available": False, "engine": None, "open_position": None,
               "session": {"n_closed": 0, "wins": 0, "realized_pnl": 0.0}, "recent": [],
               "disclaimer": ACTIVE_DISCLAIMER}
        if not os.path.exists(db_path):
            out["message"] = "active engine has not started yet"
            return jsonify(out)
        try:
            conn = _open_readonly(db_path)
            try:
                srow = conn.execute("SELECT state_json FROM crypto5m_active_state WHERE id=1").fetchone()
                eng = json.loads(srow["state_json"]) if srow and srow["state_json"] else None
                rows = conn.execute("SELECT * FROM crypto5m_active_positions ORDER BY entry_ts DESC "
                                    "LIMIT 50").fetchall()
            finally:
                conn.close()
        except (sqlite3.Error, ValueError) as e:
            out["message"] = f"active DB unavailable: {e}"
            return jsonify(out)

        out["engine"] = eng
        # BLANK SLATE PER ENGINE CHANGE: count ONLY trades from the LIVE engine version (the runner stamps each
        # trade + declares its version in the state). Bumping ENGINE_VERSION in the runner resets the count —
        # old-version trades stay in the DB but are excluded so pre-change behavior can't pollute the readout.
        ev = (eng or {}).get("engine_version")
        cols = rows[0].keys() if rows else []
        closed = [r for r in rows if r["status"] == "closed"]
        if ev and "engine_version" in cols:
            closed = [r for r in closed if r["engine_version"] == ev]
        out["session"] = {
            "engine_version": ev,
            "n_closed": len(closed),
            "wins": sum(1 for r in closed if (r["pnl"] or 0) > 0),
            "realized_pnl": round(sum(r["pnl"] or 0 for r in closed), 2),
            "by_kind": _count_by(closed, "exit_kind"),
        }
        # EV-gate selectivity: how many confirmed reads the reachability gate admits (both fees). Read from the
        # observation log; degrades to None if the table/engine predates the EV gate.
        try:
            from sniper.algos.polymarket_crypto5m import ev_observation_log as EVLOG
            _ac = _open_readonly(db_path)
            try:
                out["session"]["admit"] = EVLOG.admit_fraction(_ac, ev)
            finally:
                _ac.close()
        except (sqlite3.Error, ValueError, ImportError):
            out["session"]["admit"] = None
        op = next((r for r in rows if r["status"] == "open"), None)
        if op:
            epos = ((eng or {}).get("position") or {})
            eng_now = (eng or {}).get("now_ts")
            # a position whose 5m window has ended is awaiting settlement (it'll hit the history row shortly)
            settling = bool(eng_now is not None and op["window_start_ts"] is not None
                            and eng_now >= op["window_start_ts"] + 300)
            same = (epos.get("side") == op["side"]) and not settling
            mark = epos.get("mark") if same else None
            unreal = epos.get("unrealized_pnl") if same else None
            if unreal is None and mark is not None:
                unreal = round(op["shares"] * mark - op["shares"] * op["entry_price"], 2)
            out["open_position"] = {
                "side": op["side"], "entry_price": op["entry_price"], "shares": op["shares"],
                "mark": mark, "mark_is_estimate": (epos.get("mark_is_estimate") if same else None),
                "resolved": (epos.get("resolved") if same else None), "settling": settling,
                "peak_bid": (epos.get("peak_bid") if same else None),
                "window_start_ts": op["window_start_ts"], "entry_reason": op["entry_reason"],
                "unrealized_pnl": unreal}
        out["recent"] = [{"window_start_ts": r["window_start_ts"], "side": r["side"],
                          "entry_ts": r["entry_ts"], "entry_price": r["entry_price"],
                          "entry_reason": r["entry_reason"], "exit_ts": r["exit_ts"],
                          "exit_price": r["exit_price"], "exit_kind": r["exit_kind"],
                          "pnl": r["pnl"], "winner": r["winner"]}
                         for r in closed[:40]]
        out["available"] = True
        return jsonify(out)

    @bp.get("/api/crypto5m/maker-adaptive")
    def maker_adaptive():
        """Read-only cockpit for the adaptive paper challenger vs the maker control.

        ?campaign=<registry key> selects which campaign DB the cockpit reads (whitelist only).
        Precedence: explicit ?campaign > ctx/env deployment override > registry default."""
        control_path = ctx.get("maker_db_path") or os.getenv("CRYPTO5M_MAKER_DB") or MAKER_DEFAULT_DB
        campaign_key = request.args.get("campaign")
        entry = None
        if campaign_key is not None:
            entry = ADAPTIVE_CAMPAIGN_REGISTRY.get(campaign_key)
            if entry is None:
                return jsonify({
                    "available": False,
                    "error": f"unknown campaign key {campaign_key!r}",
                    "campaigns": [{"key": k, "label": v["label"]}
                                  for k, v in ADAPTIVE_CAMPAIGN_REGISTRY.items()],
                }), 400
        elif not (ctx.get("adaptive_maker_db_path") or os.getenv("CRYPTO5M_ADAPTIVE_MAKER_DB")):
            campaign_key = ADAPTIVE_DEFAULT_CAMPAIGN_KEY
            entry = ADAPTIVE_CAMPAIGN_REGISTRY[campaign_key]
        if entry is not None:
            adaptive_path = entry["db"]
        else:
            adaptive_path = (ctx.get("adaptive_maker_db_path")
                             or os.getenv("CRYPTO5M_ADAPTIVE_MAKER_DB")
                             or ADAPTIVE_MAKER_DEFAULT_DB)
        warnings: list[str] = []
        control = {"available": False, "db_path": control_path}
        adaptive = {"available": False, "db_path": adaptive_path}
        control_rows = []
        adaptive_rows = []
        adaptive_control_rows = []
        adaptive_think = None
        profile_stats: list[dict] = []
        disabled_profiles: list[dict] = []
        recent_fills: list[dict] = []
        v3_action_stats: list[dict] = []
        v3_fair_stats: list[dict] = []
        v3_recent_actions: list[dict] = []
        v3_ablations: dict = {"groups": [], "recent": []}
        v3_selector_sources: dict = {}
        if entry is not None:
            adaptive_control_path = entry["iso_db"]
            adaptive_control_campaign = entry["iso_campaign"]
        else:
            adaptive_control_path = (
                ctx.get("adaptive_control_maker_db_path")
                or os.getenv("CRYPTO5M_ADAPTIVE_CONTROL_DB")
                or ADAPTIVE_CONTROL_DEFAULT_DB
            )
            adaptive_control_campaign = "maker-adaptive-paper-v2"

        if not os.path.exists(control_path):
            warnings.append("control DB not found")
        else:
            try:
                conn = _open_readonly(control_path)
                try:
                    snap = _maker_campaign_snapshot(conn, "maker-regime-v1")
                    control_rows = snap.get("_settled_windows", [])
                    control = _public_snapshot(snap, control_path)
                    control["execution_haircut_panel"] = snap.get("_execution_haircut_panel")
                finally:
                    conn.close()
            except (sqlite3.Error, ValueError) as e:
                warnings.append(f"control DB unavailable: {e}")

        if not os.path.exists(adaptive_path):
            warnings.append("adaptive DB not found")
        else:
            try:
                conn = _open_readonly(adaptive_path)
                try:
                    snap = _maker_campaign_snapshot(
                        conn, entry["campaign"] if entry else "maker-adaptive-paper-v3.1")
                    adaptive_rows = snap.get("_settled_windows", [])
                    adaptive_think = snap.get("_think")
                    adaptive = _public_snapshot(snap, adaptive_path)
                    adaptive["execution_haircut_panel"] = snap.get("_execution_haircut_panel")
                    v2_think = (adaptive_think or {}).get("adaptive_paper") or {}
                    v3_think = (adaptive_think or {}).get("adaptive_v3") or {}
                    # campaign table FIRST: it is the source of truth for versions. The think blob
                    # embeds module constants at runner build time, which mislabeled v3.2r as
                    # reward v3.1 (codex finding 2026-07-06).
                    adaptive["selector_version"] = None
                    adaptive["reward_version"] = None
                    if _table_exists(conn, "crypto5m_maker_v3_campaigns"):
                        row = conn.execute(
                            "SELECT selector_version,reward_version FROM crypto5m_maker_v3_campaigns "
                            "WHERE campaign_id=?",
                            (adaptive.get("campaign"),),
                        ).fetchone()
                        if row:
                            adaptive["selector_version"] = row["selector_version"]
                            adaptive["reward_version"] = row["reward_version"]
                    if not adaptive["selector_version"]:
                        adaptive["selector_version"] = v3_think.get("selector_version") or v2_think.get(
                            "selector_version"
                        )
                    if not adaptive["reward_version"]:
                        adaptive["reward_version"] = v3_think.get("reward_version") or v2_think.get(
                            "reward_version"
                        )
                    profile_stats = _adaptive_profile_stats(conn, adaptive.get("campaign"))
                    disabled_profiles = _adaptive_disabled_profiles(conn, adaptive.get("campaign"))
                    recent_fills = _adaptive_recent_fills(conn, adaptive.get("campaign"))
                    v3_action_stats = _adaptive_v3_action_stats(conn, adaptive.get("campaign"))
                    v3_fair_stats = _adaptive_v3_fair_stats(conn, adaptive.get("campaign"))
                    v3_recent_actions = _adaptive_v3_recent_actions(conn, adaptive.get("campaign"))
                    v3_ablations = _adaptive_v3_ablations(conn, adaptive.get("campaign"))
                    v3_selector_sources = _adaptive_v3_selector_sources(conn, adaptive.get("campaign"))
                    adaptive["n_profile_stats"] = len(profile_stats)
                    adaptive["n_disabled_profiles"] = len(disabled_profiles)
                    adaptive["n_v3_action_stats"] = len(v3_action_stats)
                    adaptive["n_v3_fair_stats"] = len(v3_fair_stats)
                    adaptive["n_v3_disabled_arms"] = sum(1 for r in v3_action_stats if r.get("disabled"))
                    adaptive["n_v3_paper_preferred_arms"] = sum(
                        1 for r in v3_action_stats if r.get("paper_preferred")
                    )
                finally:
                    conn.close()
            except (sqlite3.Error, ValueError) as e:
                warnings.append(f"adaptive DB unavailable: {e}")

        if adaptive_control_path and os.path.exists(adaptive_control_path):
            try:
                conn = _open_readonly(adaptive_control_path)
                try:
                    adaptive_control_rows = _maker_campaign_snapshot(
                        conn, adaptive_control_campaign
                    ).get("_settled_windows", [])
                finally:
                    conn.close()
            except (sqlite3.Error, ValueError) as e:
                warnings.append(f"adaptive control DB unavailable: {e}")
        elif adaptive_control_path:
            warnings.append("adaptive control DB not found")

        paired_windows, paired_summary = _paired_window_summary(control_rows, adaptive_rows)
        v2_paired_windows, v2_paired_summary = _paired_window_summary(adaptive_control_rows, adaptive_rows)
        return jsonify({
            "available": bool(control.get("available") or adaptive.get("available")),
            "control": control,
            "adaptive": adaptive,
            "paired_windows": paired_windows[:40],
            "paired_summary": paired_summary,
            "current": _adaptive_current_state(adaptive_think),
            "current_v3": _adaptive_v3_current_state(adaptive_think),
            "profile_stats": profile_stats,
            "disabled_profiles": disabled_profiles,
            "recent_fills": recent_fills,
            "v3_action_stats": v3_action_stats,
            "v3_fair_stats": v3_fair_stats,
            "v3_recent_actions": v3_recent_actions,
            "v3_ablations": v3_ablations,
            "v3_selector_sources": v3_selector_sources,
            "v3_vs_v2_paired_windows": v2_paired_windows[:40],
            "v3_vs_v2_paired_summary": v2_paired_summary,
            "iso_control_campaign": adaptive_control_campaign,
            "selected_campaign_key": campaign_key,
            "campaigns": [{"key": k, "label": v["label"], "campaign": v["campaign"]}
                          for k, v in ADAPTIVE_CAMPAIGN_REGISTRY.items()],
            "warnings": warnings,
            "disclaimer": MAKER_DISCLAIMER,
        })

    @bp.get("/api/crypto5m/fleet")
    def maker_fleet():
        """One row per live campaign: lifetime + since-v3.2-launch settled PnL and the paired-window
        delta vs the maker-regime-v1 control. Read-only; whitelisted DBs only."""
        control_path = ctx.get("maker_db_path") or os.getenv("CRYPTO5M_MAKER_DB") or MAKER_DEFAULT_DB

        def _settled(db_path: str, campaign_hint: str | None):
            if not db_path or not os.path.exists(db_path):
                return None
            try:
                conn = _open_readonly(db_path)
                try:
                    snap = _maker_campaign_snapshot(conn, campaign_hint)
                finally:
                    conn.close()
            except (sqlite3.Error, ValueError):
                return None
            return snap

        control_snap = _settled(control_path, "maker-regime-v1")
        control_rows = (control_snap or {}).get("_settled_windows", [])
        control_by_ws = {r["window_start_ts"]: float(r["realized_pnl"]) for r in control_rows}
        control_rows_by_ws = {int(r["window_start_ts"]): r for r in control_rows if r["window_start_ts"] is not None}

        # anchor = first window of the newest campaign generation (v3.2f), so every row's "since"
        # column covers the identical span
        anchor = None
        newest = _settled(ADAPTIVE_CAMPAIGN_REGISTRY["v3.2f"]["db"], "maker-adaptive-paper-v3.2f")
        if newest and newest.get("_settled_windows"):
            anchor = min(r["window_start_ts"] for r in newest["_settled_windows"])

        def _row(name: str, key: str | None, snap: dict | None) -> dict:
            out = {"key": key, "campaign": name, "available": bool(snap),
                   "lifetime": None, "since": None, "paired_vs_control": None}
            if not snap:
                return out
            rows = snap.get("_settled_windows", [])
            vals = [float(r["realized_pnl"]) for r in rows]
            out["campaign"] = snap.get("campaign") or name
            out["execution_haircut_panel"] = snap.get("_execution_haircut_panel")
            out["lifetime"] = {"n": len(vals), "total": round(sum(vals), 2)}
            since_rows = [r for r in rows if anchor is None or r["window_start_ts"] >= anchor]
            sv = [float(r["realized_pnl"]) for r in since_rows]
            if sv:
                # concentration check: the read must survive dropping the single best window,
                # else one lucky settlement is doing the talking
                best = max(sv)
                out["since"] = {"n": len(sv), "total": round(sum(sv), 2),
                                "mean": round(sum(sv) / len(sv), 3),
                                "win_rate": round(sum(1 for x in sv if x > 0) / len(sv), 3),
                                "best_window": round(best, 2),
                                "total_ex_best": round(sum(sv) - best, 2)}
            common_rows = [r for r in since_rows if r["window_start_ts"] in control_by_ws]
            common = [(float(r["realized_pnl"]), control_by_ws[r["window_start_ts"]]) for r in common_rows]
            if common:
                deltas = [a - b for a, b in common]
                forward_deltas = [
                    d for r, d in zip(common_rows, deltas)
                    if int(r["window_start_ts"]) >= FORWARD_GATE_ANCHOR_TS
                ]
                best_d = max(deltas)
                common_ws = [int(r["window_start_ts"]) for r in common_rows if r["window_start_ts"] is not None]
                control_paired_rows = [control_rows_by_ws[ws] for ws in common_ws if ws in control_rows_by_ws]
                adaptive_paired_rows = common_rows
                control_panel = _execution_haircut_panel_from_rows(
                    control_snap.get("campaign"), control_paired_rows, control_snap.get("_fill_shares_by_window") or {}
                )
                adaptive_panel = _execution_haircut_panel_from_rows(
                    snap.get("campaign"), adaptive_paired_rows, snap.get("_fill_shares_by_window") or {}
                )
                out["paired_vs_control"] = {"n": len(deltas), "delta_total": round(sum(deltas), 2),
                                            "delta_mean": round(sum(deltas) / len(deltas), 3),
                                            "delta_ex_best": round(sum(deltas) - best_d, 2),
                                            "selection_note": SELECTION_NOTE,
                                            "pre_registered_gate": _paired_gate(deltas),
                                            "forward_pre_registered_gate": _forward_pre_registered_gate(forward_deltas),
                                            "paired_haircut_delta_panel": _paired_haircut_delta_panel(
                                                control_panel, adaptive_panel
                                            )}
            return out

        fleet = [_row("maker-regime-v1", None, control_snap)]
        for key in FLEET_CAMPAIGN_KEYS:
            e = ADAPTIVE_CAMPAIGN_REGISTRY[key]
            fleet.append(_row(e["campaign"], key, _settled(e["db"], e["campaign"])))
        return jsonify({
            "available": any(r["available"] for r in fleet),
            "anchor_window_start_ts": anchor,
            "fleet": fleet,
            "flow_gate": _flow_gate_status(),
            "disclaimer": MAKER_DISCLAIMER,
        })

    @bp.get("/api/crypto5m/maker")
    def maker():
        """PAPER maker quoter state: latest think (quotes, inventory, fair value), per-window settled PnL,
        and recent modeled fills — read from the maker paper DB (the runner updates it every ~1s)."""
        db_path = ctx.get("maker_db_path") or os.getenv("CRYPTO5M_MAKER_DB") or MAKER_DEFAULT_DB
        out = {"available": False, "engine": None,
               "session": {"n_settled": 0, "wins": 0, "realized_pnl": 0.0},
               "recent_windows": [], "recent_fills": [], "regime_shadow": _empty_regime_shadow_report(),
               "regime_eval": maker_regime_report.empty_report(),
               "regime_state": {"available": False},
               "disclaimer": MAKER_DISCLAIMER}
        if not os.path.exists(db_path):
            out["message"] = "maker engine has not started yet"
            return jsonify(out)
        try:
            conn = _open_readonly(db_path)
            try:
                srow = conn.execute("SELECT think_json, ts FROM maker_state WHERE id=1").fetchone()
                think = json.loads(srow["think_json"]) if srow and srow["think_json"] else None
                # CAMPAIGN PARTITION: count only the live campaign's rows. maker-paper-v0 rows are
                # contaminated (naked-short accounting bug, codex review 2026-07-04) and must never pool
                # into the eval. STRICT: no campaign in think (fresh boot / mid-reset / stale state) means
                # stats are WITHHELD, not pooled — pooling across engine versions was the exact bug the
                # partition exists to prevent, and it made the campaign counter jump around on every reset.
                camp = (think or {}).get("campaign")
                if camp:
                    # realized_pnl IS NOT NULL excludes 'unresolved' abandons — they are not $0 windows.
                    # NO LIMIT here: session aggregates must cover the WHOLE campaign or n_settled freezes
                    # at the cap and the total silently drops old windows right at the n>=200 eval bar.
                    wq = ("SELECT * FROM maker_windows WHERE settled=1 AND realized_pnl IS NOT NULL"
                          " AND campaign_id = :camp")
                    wrows = conn.execute(wq + " ORDER BY window_start_ts DESC",
                                         {"camp": camp}).fetchall()
                    all_wrows = conn.execute(
                        "SELECT * FROM maker_windows WHERE campaign_id = :camp",
                        {"camp": camp},
                    ).fetchall()
                    identity_by_cid = {
                        r["condition_id"]: identity
                        for r in wrows
                        if (identity := _maker_settlement_identity(conn, r, camp)) is not None
                    }
                    fq = "SELECT * FROM maker_fills WHERE campaign_id = :camp"
                    frows = conn.execute(fq + " ORDER BY ts DESC LIMIT 30", {"camp": camp}).fetchall()
                else:
                    wrows, all_wrows, identity_by_cid, frows = [], [], {}, []
                win_by_cid = {}
                fill_cids = sorted({r["condition_id"] for r in frows if r["condition_id"]})
                if fill_cids:
                    ph = ",".join("?" for _ in fill_cids)
                    wctx_q = f"SELECT * FROM maker_windows WHERE condition_id IN ({ph})"
                    wctx_args = list(fill_cids)
                    if camp:
                        wctx_q += " AND campaign_id=?"
                        wctx_args.append(camp)
                    win_by_cid = {r["condition_id"]: r for r in conn.execute(wctx_q, wctx_args).fetchall()}
                fm_counts = ({r["fill_model"] or "untagged": r["c"] for r in conn.execute(
                    "SELECT fill_model, COUNT(*) c FROM maker_fills WHERE campaign_id = :camp"
                    " GROUP BY fill_model", {"camp": camp})} if camp else {})
                regime_shadow = _maker_regime_shadow_report(conn, camp, think)
                regime_eval = (
                    maker_regime_report.build_maker_regime_report(
                        conn, camp, lookback_fills=1500, campaign_min_windows=MAKER_EVAL_MIN_SETTLED,
                    )
                    if camp else maker_regime_report.empty_report(camp)
                )
            finally:
                conn.close()
        except (sqlite3.Error, ValueError) as e:
            out["message"] = f"maker DB unavailable: {e}"
            return jsonify(out)
        if think is not None:
            think["think_age_s"] = max(0, int(time.time()) - int(srow["ts"] or 0))
            # a think older than ~30s means the runner is down — show "starting up" instead of a stale desk
            if think["think_age_s"] > 30:
                out["message"] = f"maker runner idle ({think['think_age_s']}s since last think)"
                think = None
        out["engine"] = think
        out["regime_state"] = _maker_regime_state(think)
        accounting = _maker_window_accounting(all_wrows, (think or {}).get("window_start_ts"))
        eval_by_fill = {r["fill_id"]: r for r in regime_eval.get("fill_evaluations", [])}
        realized_values = [_maker_realized_for_readout(r, identity_by_cid) for r in wrows]
        realized_sum = sum(realized_values)
        ladder = _maker_accounting_ladder(
            all_wrows,
            settled_realized_pnl=realized_sum,
            current_open_mark_pnl=(think or {}).get("unrealized_pnl"),
            active_window_start_ts=(think or {}).get("window_start_ts"),
        )
        # session PnL from SETTLED windows in the DB (restart-proof), not the runner's in-memory counter.
        # fill_models: print-fill share is core to the verdict — a book/untagged-heavy campaign is suspect.
        out["session"] = {
            "campaign": camp,
            "n_settled": len(wrows),
            "wins": sum(1 for v in realized_values if v > 0),
            "realized_pnl": round(realized_sum, 2),
            "stored_realized_pnl": round(sum(r["realized_pnl"] or 0 for r in wrows), 2),
            "realized_pnl_source": "fill_ledger_when_available",
            "fill_models": fm_counts,
            "settlement_identity": _maker_identity_summary(identity_by_cid),
            "eval_min_settled": MAKER_EVAL_MIN_SETTLED,
            "eval_ready": len(wrows) >= MAKER_EVAL_MIN_SETTLED,
            "interim_eval_min_settled": MAKER_INTERIM_EVAL_MIN_SETTLED,
            "interim_eval_ready": len(wrows) >= MAKER_INTERIM_EVAL_MIN_SETTLED,
            "accounting_ladder": ladder,
            **accounting,
        }
        if accounting["n_unresolved"]:
            out["session"]["unresolved_note"] = (
                "unresolved windows are excluded from realized PnL and remain retryable by the runner"
            )
        if not camp:
            out["session"]["note"] = ("no campaign in maker_state — stats withheld rather than pooled "
                                      "across engine versions (runner starting or mid-reset)")
        out["recent_windows"] = [{"window_start_ts": r["window_start_ts"],
                                  "up_inv": r["up_inv"], "down_inv": r["down_inv"], "cash": r["cash"],
                                  "realized_pnl": r["realized_pnl"], "winning_outcome": r["winning_outcome"],
                                  **(identity_by_cid.get(r["condition_id"]) or {})}
                                 for r in wrows[:20]]
        out["recent_fills"] = [
            {"id": r["id"], "ts": r["ts"], "window_start_ts": r["window_start_ts"], "condition_id": r["condition_id"],
             "token": r["token"], "side": r["side"], "price": r["price"], "size": r["size"], "fair": r["fair"],
             "reason": r["reason"], "fill_model": r["fill_model"],
             "regime_id": _row_get(r, "fill_regime_id") or _row_get(r, "regime_id"),
             "profile_id": _row_get(r, "fill_profile_id") or _row_get(r, "profile_id"),
             "quote_regime_id": _row_get(r, "quote_regime_id"),
             "fill_regime_id": _row_get(r, "fill_regime_id"),
             "quote_profile_id": _row_get(r, "quote_profile_id"),
             "fill_profile_id": _row_get(r, "fill_profile_id"),
             **{k: v for k, v in (eval_by_fill.get(r["id"]) or {}).items()
                if k not in {"condition_id", "window_start_ts", "token", "side"}},
             **_maker_fill_attribution(r, win_by_cid.get(r["condition_id"]),
                                       (think or {}).get("window_start_ts"))}
            for r in frows
        ]
        out["regime_shadow"] = regime_shadow
        out["regime_eval"] = regime_eval
        out["available"] = think is not None
        return jsonify(out)

    @bp.get("/api/crypto5m/z-series")
    def z_series():
        """Display-only: the brain-v2 shadow probability path (current z vs stabilized z_v2 vs market_z) over
        a window, for the desk graph. Read-only from crypto5m_z_shadow. Never feeds a decision."""
        db_path = ctx.get("active_db_path") or os.getenv("CRYPTO5M_ACTIVE_DB") or ACTIVE_DEFAULT_DB
        out = {"available": False, "series": []}
        if not os.path.exists(db_path):
            return jsonify(out)
        try:
            conn = _open_readonly(db_path)
            try:
                has = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='crypto5m_z_shadow'").fetchone()
                if not has:
                    out["message"] = "no shadow rows yet (accruing)"
                    return jsonify(out)
                ws_row = conn.execute("SELECT MAX(window_start_ts) w FROM crypto5m_z_shadow").fetchone()
                ws = request.args.get("window", type=int) or (ws_row["w"] if ws_row else None)
                rows = conn.execute(
                    "SELECT tick_ts, z, z_v2, z_market FROM crypto5m_z_shadow "
                    "WHERE window_start_ts=? ORDER BY tick_ts", (ws,)).fetchall() if ws else []
            finally:
                conn.close()
        except sqlite3.Error as e:
            out["message"] = f"z-series unavailable: {e}"
            return jsonify(out)
        out["available"] = bool(rows)
        out["window_start_ts"] = ws
        out["series"] = [{"tick_ts": r["tick_ts"], "z": r["z"], "z_v2": r["z_v2"], "z_market": r["z_market"]}
                         for r in rows]
        return jsonify(out)

    @bp.get("/api/crypto5m/live")
    def live():
        import time as _time
        mono = _time.monotonic()
        cached = _LIVE_CACHE.get("data")
        if cached and (mono - _LIVE_CACHE["mono"]) < 4.0:      # cache 4s so polling can't hammer Binance/CLOB
            return jsonify(cached)
        try:
            from sniper.algos.polymarket_crypto5m import live_desk
            desk = live_desk.build_live_desk()
        except Exception as e:  # noqa: BLE001 — never 500 the dashboard
            desk = {"available": False, "message": f"live desk error: {e}"}
        desk["disclaimer"] = LIVE_DISCLAIMER
        _LIVE_CACHE.update(mono=mono, data=desk)
        return jsonify(desk)

    @bp.get("/api/crypto5m/lanes")
    def lanes():
        db_path = ctx.get("lane_db_path") or os.getenv("CRYPTO5M_LANE_DB") or LANE_DEFAULT_DB
        if not os.path.exists(db_path):
            return jsonify({"available": False, "db_path": db_path, "message": "lane DB not found",
                            "lane_version": None, "groups": [], "disclaimer": LANE_DISCLAIMER})
        try:
            conn = _open_readonly(db_path)
            try:
                latest = conn.execute("SELECT lane_version FROM crypto5m_lane_results "
                                      "ORDER BY updated_ts DESC LIMIT 1").fetchone()
                if not latest:
                    return jsonify({"available": True, "db_path": db_path, "lane_version": None,
                                    "groups": [], "disclaimer": LANE_DISCLAIMER})
                version = latest["lane_version"]
                rows = conn.execute("SELECT * FROM crypto5m_lane_results WHERE lane_version=?",
                                    (version,)).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as e:
            return jsonify({"available": False, "db_path": db_path, "lane_version": None,
                            "message": f"lane DB unavailable: {e}", "groups": [], "disclaimer": LANE_DISCLAIMER})

        live = _live_lane_picks(ctx.get("quant_db_path") or os.getenv("BTC5M_QUANT_DB") or QUANT_DEFAULT_DB)
        by_lane: dict = {}
        freeze_ts = updated_ts = None
        for r in rows:
            freeze_ts = freeze_ts or r["freeze_ts"]
            updated_ts = max(updated_ts or 0, r["updated_ts"] or 0)
            d = by_lane.setdefault(r["lane"], {
                "lane": r["lane"], "family": r["family"], "checkpoint": r["checkpoint"],
                "description": r["description"], "benchmark": r["benchmark"], "live": live.get(r["lane"])})
            samp = {"n": r["n"], "win_pct": r["win_pct"], "net_roi_pct": r["net_roi_pct"],
                    "avg_price": r["avg_price"]}
            if r["sample"] == "oos":
                samp.update({"gap_pct": r["gap_pct"], "gap_ci_lo": r["gap_ci_lo"], "gap_ci_hi": r["gap_ci_hi"],
                             "skipped_n": r["skipped_n"], "skipped_roi_pct": r["skipped_roi_pct"],
                             "pval": r["pval"], "status": r["status"],
                             "fdr_pass": (bool(r["fdr_pass"]) if r["fdr_pass"] is not None else None)})
                d["oos"] = samp
            else:
                d["in_sample"] = samp

        groups = []
        for fam, title in _LANE_GROUP_ORDER:
            members = sorted((d for d in by_lane.values() if d["family"] == fam), key=lambda x: x["lane"])
            if members:
                groups.append({"family": fam, "title": title, "lanes": members})
        return jsonify({"available": True, "db_path": db_path, "lane_version": version,
                        "freeze_ts": freeze_ts, "updated_ts": updated_ts, "groups": groups,
                        "disclaimer": LANE_DISCLAIMER})

    return bp
