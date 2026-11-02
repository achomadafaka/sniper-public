#!/usr/bin/env python3
"""Continuous ACTIVE paper-trading engine for the Polymarket BTC Up/Down 5-minute binary (READ-ONLY,
NO ORDERS). Thinks every few seconds across the WHOLE window: computes our probability (where BTC already
is, projected to close) vs the LIVE book on both sides, ENTERS dynamically the instant a side is
underpriced (second 1, second 4:59, or never), MANAGES the position (take-profit / stop), and settles
held positions at close. Paper only; collects data on whether dynamic entry+exit beats the price.

Decisions are the unit-tested active_trader.decide(); live data is live_desk.build_live_desk(). Restart-safe.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sniper.algos.polymarket_crypto5m import scalp_trader as ST           # noqa: E402
from sniper.algos.polymarket_crypto5m import direction as DIR             # noqa: E402
from sniper.algos.polymarket_crypto5m import direction_state as DS        # noqa: E402
from sniper.algos.polymarket_crypto5m import live_desk as LD              # noqa: E402
from sniper.algos.polymarket_crypto5m import market_resolver as MR        # noqa: E402
from sniper.algos.polymarket_crypto5m import settlement as S              # noqa: E402
from sniper.algos.polymarket_crypto5m.scalp_trader import Position        # noqa: E402
from sniper.algos.polymarket_crypto5m import reachability as RZ           # noqa: E402
from sniper.algos.polymarket_crypto5m import ev_observation_log as EVLOG  # noqa: E402
from sniper.algos.polymarket_crypto5m import z_shadow_log as ZL           # noqa: E402
from sniper.algos.polymarket_crypto5m import drift_reach_log as DL        # noqa: E402
from sniper.algos.polymarket_crypto5m import drift_reachability as DR     # noqa: E402
from sniper.algos.polymarket_crypto5m import maker_entry as ME            # noqa: E402
from sniper.algos.polymarket_crypto5m import position_marks as PM         # noqa: E402
from sniper.algos.polymarket_crypto5m.regime_vol import sharp_window_label  # noqa: E402

log = logging.getLogger("crypto5m_active")
CAMPAIGN_ID = "active-paper-v0"
# BLANK SLATE PER ENGINE CHANGE: every trade is stamped with this version; the readout counts ONLY trades whose
# engine_version matches the LIVE engine. BUMP this string whenever the engine's decision logic changes — the
# count resets to 0 (old-version trades are excluded from the count, not deleted). Mixing pre/post-change trades
# made the PnL meaningless (the old tight-stop churn polluted it), so each change gets a clean slate.
ENGINE_VERSION = "maker-entry-v1"   # MAKER entry execution (2026-07-04): the read stays; instead of paying
                                    # the ask on confirmation, post a resting bid on that side and let the
                                    # market trade into us (print-verified fills). Taker was cost-bound
                                    # (forward C0 −19.5% ROI baseline). Blank-slate count resets.
Z_SHADOW_VERSION = "brain-v2-shadow-v1"   # brain-v2 stabilized-probability shadow log (no decision use)
# EV gate config. v1 gates on the REALIZED fee (gate_on_realized=True, default) so the engine trades its
# selective +EV-over-spread set on paper; the documented 0.07 column is ALWAYS logged as the real-money-
# readiness overlay. Set gate_on_realized=False to gate on 0.07 (real-money mode), no data change.
EVCFG = RZ.EVConfig()
STAKE = 10.0
SETTLE_GRACE_S = 12     # wait this long after window close before settling (Gamma needs a moment to post the
                        # resolution); kept short so closed trades hit the history row promptly
REENTRY_COOLDOWN_S = 8      # scalper: short cool-off after an exit before re-scouting the same window


def _connect(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    c = sqlite3.connect(path, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS crypto5m_active_positions (
        position_key TEXT PRIMARY KEY, campaign_id TEXT, window_start_ts INTEGER, condition_id TEXT,
        side TEXT, entry_ts INTEGER, entry_price REAL, shares REAL, entry_reason TEXT,
        status TEXT, exit_ts INTEGER, exit_price REAL, exit_kind TEXT, pnl REAL, winner TEXT, persist_ts INTEGER)""")
    c.execute("""CREATE TABLE IF NOT EXISTS crypto5m_active_state (
        id INTEGER PRIMARY KEY, state_json TEXT, updated_ts INTEGER)""")
    # migrations (idempotent): entry_prob = fair value at entry (restart-safe exits); the rest measure the
    # LIVE EXECUTION GAP the walk-forward couldn't — decision_price (the touch we read), fill_slip (what the
    # 1-tick-later fill actually cost us), entry_conviction + entry_ofi (the gate's inputs, for the review).
    for col, typ in (("entry_prob", "REAL"), ("decision_price", "REAL"), ("fill_slip", "REAL"),
                     ("entry_conviction", "REAL"), ("entry_ofi", "REAL"), ("entry_persistence", "INTEGER"),
                     ("engine_version", "TEXT")):
        try:
            c.execute(f"ALTER TABLE crypto5m_active_positions ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass  # column already exists
    EVLOG.ensure_schema(c)
    ZL.ensure_schema(c)
    DL.ensure_schema(c)
    PM.ensure_schema(c)
    c.commit()
    return c


def _window_log_returns(w):
    """The closed window's 1s log-returns, fetched fresh from Binance klines (for the sharp-window label at
    resolution). Best-effort: returns [] on any fetch failure (-> 'calm' label, still resolved)."""
    kl = LD._safe(lambda: LD._get(
        f"{LD.BINANCE_KLINES}&startTime={w * 1000}&endTime={(w + 300) * 1000}&limit=400"))
    if not kl:
        return []
    closes = {}
    for k in kl:
        sec = int(k[0]) // 1000
        if w <= sec <= w + 300:
            closes[sec] = float(k[4])
    secs = sorted(closes)
    return [math.log(closes[b] / closes[a]) for a, b in zip(secs, secs[1:])
            if closes[a] > 0 and closes[b] > 0]


def _pos_key(ws, seq):
    return f"{CAMPAIGN_ID}|{ws}|{seq}"


def _open_position(conn, key, ws, cond, pos: Position, reason, now, *,
                   decision_price=None, fill_slip=None, conviction=None, ofi=None, persistence=None):
    conn.execute("""INSERT OR REPLACE INTO crypto5m_active_positions
        (position_key, campaign_id, window_start_ts, condition_id, side, entry_ts, entry_price, shares,
         entry_reason, entry_prob, decision_price, fill_slip, entry_conviction, entry_ofi, entry_persistence,
         engine_version, status, persist_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'open', ?)""",
        (key, CAMPAIGN_ID, ws, cond, pos.side, pos.entry_ts, pos.entry_price, pos.shares,
         reason, pos.entry_prob, decision_price, fill_slip, conviction, ofi, persistence, ENGINE_VERSION, now))
    conn.commit()


def _close_position(conn, ws, kind, exit_price, pnl, winner, now):
    """Close the OPEN position for a window by (window_start_ts, status='open') — robust to ANY key format
    (old 'CAMPAIGN|ws' rows and new 'CAMPAIGN|ws|seq' rows alike). Only one position is open per window at a
    time, so this matches exactly the right row. Matching by a reconstructed key left old-format rows stuck
    open forever (the '— settling…' hang)."""
    conn.execute("""UPDATE crypto5m_active_positions SET status='closed', exit_ts=?, exit_price=?,
        exit_kind=?, pnl=?, winner=?, persist_ts=? WHERE window_start_ts=? AND status='open'""",
        (now, exit_price, kind, pnl, winner, now, ws))
    conn.commit()


def _position_view(desk, pos):
    """Display state for an open position with a ROBUST mark: when the bid on our side dries up (near-
    resolved / illiquid — the screenshot bug), estimate the mark from the other side's ask or our fair
    value so unrealized PnL always shows, and flag whether our side is effectively resolved win/lose."""
    if not pos:
        return None
    side = pos.side
    bid = desk.get("up_bid") if side == "Up" else desk.get("down_bid")
    ask = desk.get("up_ask") if side == "Up" else desk.get("down_ask")
    other_ask = desk.get("down_ask") if side == "Up" else desk.get("up_ask")
    fair = desk.get("fair_value_up")
    fair_side = None if fair is None else (fair if side == "Up" else 1 - fair)
    mark, est = bid, False
    if mark is None:                                    # no bid to sell into — estimate the mark
        est = True
        if other_ask is not None:
            mark = round(1 - other_ask, 4)              # implied price of our side
        elif ask is not None:
            mark = ask
        elif fair_side is not None:
            mark = round(fair_side, 4)
    resolved = None
    if fair_side is not None:
        resolved = "winning" if fair_side >= 0.99 else ("losing" if fair_side <= 0.01 else None)
    unreal = round(pos.shares * mark - pos.shares * pos.entry_price, 2) if mark is not None else None
    return {"side": side, "entry_price": pos.entry_price, "shares": pos.shares,
            "entry_prob": pos.entry_prob, "mark": mark, "mark_is_estimate": est,
            "unrealized_pnl": unreal, "resolved": resolved,
            "peak_bid": getattr(pos, "peak_bid", None)}


def _write_state(conn, desk, pos, action, now, thesis=None, confirm=None, ev=None):
    cf = confirm or {}
    st = {"now_ts": now, "slug": desk.get("slug"), "seconds_left": desk.get("seconds_left"),
          "seconds_elapsed": desk.get("seconds_elapsed"), "btc_start": desk.get("btc_start"),
          "btc_now": desk.get("btc_now"), "btc_move_pct": desk.get("btc_move_pct"),
          "momentum_bps": desk.get("momentum_bps"), "momentum_horizon_s": desk.get("momentum_horizon_s"),
          "momentum_agreement": desk.get("momentum_agreement"), "book_imbalance": desk.get("book_imbalance"),
          "microprice_gap": desk.get("microprice_gap"), "volume_dir": desk.get("volume_dir"),
          "tape_ofi_10s": desk.get("tape_ofi_10s"), "tape_ofi_30s": desk.get("tape_ofi_30s"),
          "tape_n_10s": desk.get("tape_n_10s"),
          "confirmed_side": cf.get("confirmed_side"), "persistence_ticks": cf.get("persistence_ticks"),
          "horizons_agree": cf.get("horizons_agree"), "flow_agree": cf.get("flow_agree"),
          "read_side": cf.get("read_side"), "engine_version": ENGINE_VERSION,
          "ret_5s": desk.get("ret_5s"), "ret_15s": desk.get("ret_15s"), "ret_30s": desk.get("ret_30s"),
          "ema_slope": desk.get("ema_slope"), "thesis": thesis, "vol_per_s": desk.get("vol_per_s"),
          "spread": ((desk.get("up_ask") - desk.get("up_bid"))
                     if (desk.get("up_ask") is not None and desk.get("up_bid") is not None) else None),
          "prob_up": desk.get("fair_value_up"), "up_ask": desk.get("up_ask"), "up_bid": desk.get("up_bid"),
          "down_ask": desk.get("down_ask"), "down_bid": desk.get("down_bid"), "action": action,
          "z": desk.get("z"), "ev": ev,            # reachability EV gate (the economic "worth trading?" layer)
          "signal_series": desk.get("signal_series"),   # our EMAs + P(Up) projection, plottable (the moving brain)
          "position": _position_view(desk, pos)}
    conn.execute("INSERT OR REPLACE INTO crypto5m_active_state (id, state_json, updated_ts) VALUES (1,?,?)",
                 (json.dumps(st), now))
    conn.commit()


def _resolve(slug):
    try:
        ev = LD._get(f"{LD.GAMMA}{slug}")
        if not ev:
            return None
        spec, _ = MR.parse_event(ev[0])
        if spec is None:
            return None
        return S.from_gamma(spec).winning_outcome
    except Exception:  # noqa: BLE001
        return None


def run(conn, *, poll_interval=3, max_iterations=None):
    log.info("[active] campaign %s started (continuous paper; dynamic entry, re-entry, smart exits)", CAMPAIGN_ID)
    live = {}            # ws -> currently-open Position (at most one per window at a time)
    seq = {}             # ws -> count of positions opened this window (for unique re-entry keys)
    cooldown = {}        # ws -> ts before which we won't re-enter this window
    pending = {}         # ws -> resting ME.PendingQuote entry bid (maker-entry-v1): posted on confirmation,
                         # print-verified fill / bounded reprice / cancel on read-loss-TTL-close
    last_tick_ts = {}    # ws -> previous tick ts (bounds the tape-print window for the fill check)
    dstate = DS.DirectionState()   # ONE instance — BTC's short-term direction persists across windows; the
                                   # ~6s memory confirms a real move before we commit (not a micro-wiggle)
    # restart recovery: reload still-open positions (key = CAMPAIGN|ws|seq; legacy keys have no seq)
    for r in conn.execute("SELECT * FROM crypto5m_active_positions WHERE status='open'"):
        w = r["window_start_ts"]
        ep = r["entry_prob"] if "entry_prob" in r.keys() else None
        live[w] = Position(r["side"], r["entry_price"], r["entry_ts"], r["shares"], entry_prob=ep)
        parts = (r["position_key"] or "").split("|")
        seq[w] = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0
    # seed seq for any window with closed positions so re-entry keys don't collide
    for r in conn.execute("SELECT window_start_ts, position_key FROM crypto5m_active_positions"):
        w = r["window_start_ts"]; parts = (r["position_key"] or "").split("|")
        s = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0
        seq[w] = max(seq.get(w, 0), s)
    if live:
        log.info("[active] recovered %d open position(s)", len(live))
    it = 0
    while True:
        it += 1
        try:
            desk = LD.build_live_desk()
            now, ws = desk["now_ts"], desk["window_start_ts"]
            pos = live.get(ws)
            book = {k: desk.get(k) for k in ("up_ask", "up_bid", "down_ask", "down_bid")}
            _ua, _ub = desk.get("up_ask"), desk.get("up_bid")
            _spread = (_ua - _ub) if (_ua is not None and _ub is not None and _ua > _ub) else None
            thesis = DIR.directional_thesis(
                ret_5s=desk.get("ret_5s"), ret_15s=desk.get("ret_15s"), ret_30s=desk.get("ret_30s"),
                ema_slope=desk.get("ema_slope"), microprice_gap=desk.get("microprice_gap"),
                imbalance=desk.get("book_imbalance"), volume_dir=desk.get("volume_dir"),
                order_flow=desk.get("tape_ofi_10s"), vol_per_s=desk.get("vol_per_s"), spread=_spread)
            confirm = dstate.update(thesis, desk, now)     # memory/confirmation gate
            # maker-entry-v1: manage the RESTING entry quote (print-verified fill / bounded reprice / cancel)
            # BEFORE deciding anew. The quote was posted on a confirmed read; the market must trade into us.
            pend = pending.get(ws)
            if pend is not None and pos is None:
                pq = pend["pq"]
                prints = LD.tape_prints(desk.get("condition_id"), last_tick_ts.get(ws), now,
                                        token=pq.side.lower())
                qact = ME.update_quote(pq, book=book, confirm=confirm, now_ts=now,
                                       seconds_left=desk.get("seconds_left", 0), prints=prints)
                if qact["action"] == "fill":
                    pending.pop(ws, None)
                    seq[ws] = seq.get(ws, 0) + 1
                    pos = Position(pq.side, qact["price"], now, round(STAKE / qact["price"], 4),
                                   entry_prob=pend["entry_prob"])
                    live[ws] = pos
                    _open_position(conn, _pos_key(ws, seq[ws]), ws, desk.get("condition_id"), pos,
                                   f"{pend['reason']} [maker fill/{qact['fill_model']}]", now,
                                   decision_price=pq.decision_ask, fill_slip=qact.get("slip"),
                                   conviction=pend["conviction"], ofi=pend["ofi"],
                                   persistence=pend.get("persistence"))
                    log.info("[active] MAKER FILL %s %s @ %.3f (taker would've paid %.3f, slip %+.3f, %s)",
                             desk["slug"], pos.side, pos.entry_price, pq.decision_ask or -1,
                             qact.get("slip") or 0, qact["fill_model"])
                elif qact["action"] == "cancel":
                    pending.pop(ws, None)
                    log.info("[active] QUOTE CANCEL %s %s @ %.3f — %s", desk["slug"], pq.side, pq.price,
                             qact.get("reason"))
                elif qact["action"] == "reprice":
                    pq.reprices += 1
                    log.info("[active] QUOTE REPRICE %s %s %.3f -> %.3f (#%d)", desk["slug"], pq.side,
                             pq.price, qact["price"], pq.reprices)
                    pq.price = qact["price"]
            # drop stale quotes from rolled-over windows (their market is gone)
            for w in [w for w in pending if w != ws]:
                pending.pop(w, None)
            # brain v2 SHADOW: log the stabilized probability every tick (never reads back into a decision).
            # ISOLATED try/except: this write runs BEFORE the decision path, so a shadow-write failure (locked
            # DB / disk-full) must NOT propagate to the loop-wide handler and suppress this tick's live
            # entry/exit/settlement. The "zero live behavior change" invariant has to hold under DB stress too.
            if desk.get("z_v2") is not None:
                try:
                    ZL.record_tick(conn, {
                        "engine_version": Z_SHADOW_VERSION, "window_start_ts": ws, "tick_ts": now,
                        "condition_id": desk.get("condition_id"),
                        "btc_now": desk.get("btc_now"), "btc_start": desk.get("btc_start"),
                        "seconds_left": desk.get("seconds_left"),
                        "vol_slow": desk.get("vol_slow"), "vol_fast": desk.get("vol_fast"),
                        "jump_flag": 1 if desk.get("jump_flag") else 0, "divergence": desk.get("divergence"),
                        "z": desk.get("z"), "z_ours": desk.get("z_ours"), "z_market": desk.get("z_market"),
                        "market_mid": desk.get("up_mid"), "z_v2": desk.get("z_v2"), "weight": desk.get("z_weight")})
                except Exception:  # noqa: BLE001 — shadow logging must never break the live decision path
                    log.exception("[active] brain-v2 shadow record failed (non-fatal; decision path unaffected)")
            # reachability EV gate: compute for every CONFIRMED tick, log the counterfactual (both fees),
            # and gate entry on it. live `passed` = realized-fee EV (gate_on_realized default); 0.07 logged too.
            ev = None
            if confirm.get("confirmed") and confirm.get("confirmed_side"):
                cside = confirm["confirmed_side"]
                s_ask = desk.get("up_ask") if cside == "Up" else desk.get("down_ask")
                s_bid = desk.get("up_bid") if cside == "Up" else desk.get("down_bid")
                s_mid = desk.get("up_mid") if cside == "Up" else desk.get("down_mid")
                ev = RZ.entry_ev(z=desk.get("z"), vol_per_s=desk.get("vol_per_s"),
                                 seconds_left=desk.get("seconds_left", 0),
                                 conviction=thesis.get("conviction"), side_ask=s_ask, side_bid=s_bid, cfg=EVCFG)
                if ev.get("gain") is not None:   # log only computable-EV ticks (skip degenerate/no-room/band blanks)
                    obs = {"engine_version": ENGINE_VERSION, "campaign_id": CAMPAIGN_ID, "window_start_ts": ws,
                           "tick_ts": now, "side": cside, "condition_id": desk.get("condition_id"), "token_id": None,
                           "z_model": desk.get("z"), "market_probit_z": RZ.probit(s_mid),
                           "conviction": thesis.get("conviction"), "vol_per_s": desk.get("vol_per_s"),
                           "seconds_left": desk.get("seconds_left"), "side_ask": s_ask, "side_bid": s_bid,
                           "side_mid": s_mid, "spread": ev.get("spread"), "delta": ev.get("delta"),
                           "theta": ev.get("theta"), "t_star": ev.get("t_star"), "t_hold": ev.get("t_hold"),
                           "gain": ev.get("gain"), "gate_stat": ev.get("gate_stat"), "cost_007": ev.get("cost_007"),
                           "cost_realfee": ev.get("cost_realfee"), "ev_007": ev.get("ev_007"),
                           "ev_realfee": ev.get("ev_realfee"),
                           "passed_007": 1 if ev.get("passed_007") else 0,
                           "passed_realfee": 1 if ev.get("passed_realfee") else 0,
                           "mid_at_entry": s_mid, "bid_at_entry": s_bid, "ask_at_entry": s_ask,
                           "resolve_at_ts": now + int(ev.get("t_hold") or 0)}
                    EVLOG.record_pending(conn, obs)
                    # drift-reachability SHADOW: the MEASURED-drift prediction for the confirmed side, paired
                    # with the driftless 0 baseline, resolved at the SAME t_hold as the ev obs. Never feeds a
                    # decision (v1). Isolated try/except so a shadow-write failure can't suppress the live tick.
                    try:
                        _th = ev.get("t_hold")
                        _mu = DR.drift_estimate(tape_ofi=desk.get("tape_ofi_10s"), ema_slope=desk.get("ema_slope"),
                                                volume_dir=desk.get("volume_dir"))
                        _pred = DR.side_prediction(side=cside, z=desk.get("z"), vol_per_s=desk.get("vol_per_s"),
                                                   seconds_left=desk.get("seconds_left", 0), mu_hat=_mu,
                                                   t_hold=_th) if _th else None
                        if _pred and _pred.get("exp_fav_move") is not None:
                            DL.record_pending(conn, {
                                "engine_version": ENGINE_VERSION, "window_start_ts": ws, "tick_ts": now,
                                "side": cside, "condition_id": desk.get("condition_id"), "z": desk.get("z"),
                                "vol_per_s": desk.get("vol_per_s"), "seconds_left": desk.get("seconds_left"),
                                "mu_hat": _mu, "b_barrier": _pred["b_barrier"], "touch_prob": _pred["touch_prob"],
                                "exp_fav_move": _pred["exp_fav_move"],
                                "exp_fav_move_driftless": _pred["exp_fav_move_driftless"],
                                "max_hold_s": _pred["max_hold_s"], "jump_flag": 1 if desk.get("jump_flag") else 0,
                                "side_ask": s_ask, "side_bid": s_bid, "side_mid": s_mid, "mid_at_entry": s_mid,
                                "t_hold": _th, "resolve_at_ts": now + int(_th)})
                    except Exception:  # noqa: BLE001 — shadow logging must never break the live decision path
                        log.exception("[active] drift-reach shadow record failed (non-fatal)")
            # resolve any EV observations whose t_hold has elapsed (same window, book still live)
            for o in EVLOG.due_pending(conn, ws, now):
                rs = o["side"]
                EVLOG.complete_observation(conn, o["id"],
                    mid=(desk.get("up_mid") if rs == "Up" else desk.get("down_mid")),
                    bid=(desk.get("up_bid") if rs == "Up" else desk.get("down_bid")),
                    ask=(desk.get("up_ask") if rs == "Up" else desk.get("down_ask")), realized_ts=now)
            EVLOG.sweep_incomplete(conn, now)
            # resolve drift-reach shadow rows whose t_hold elapsed (same window, book still live)
            for o in DL.due_pending(conn, ws, now):
                rs = o["side"]
                DL.complete_observation(conn, o["id"],
                    mid=(desk.get("up_mid") if rs == "Up" else desk.get("down_mid")), realized_ts=now)
            DL.sweep_incomplete(conn, now)
            if pos is not None:                        # maintain the exit timers + peak (gap-immune exits)
                cb = book["up_bid"] if pos.side == "Up" else book["down_bid"]
                if cb is not None:
                    pos.peak_bid = cb if pos.peak_bid is None else max(pos.peak_bid, cb)
                ST.update_exit_state(pos, book, thesis, now)
                # intra-trade mid mark for the held side (exit counterfactual data; logging only, no decision).
                # Isolated try/except so a mark-write failure can't suppress the live tick.
                try:
                    _hm = desk.get("up_mid") if pos.side == "Up" else desk.get("down_mid")
                    if _hm is not None:
                        PM.record_mark(conn, {"position_key": _pos_key(ws, seq.get(ws, 0)),
                            "window_start_ts": ws, "tick_ts": now, "seconds_held": now - pos.entry_ts,
                            "side": pos.side, "mid": _hm,
                            "bid": (desk.get("up_bid") if pos.side == "Up" else desk.get("down_bid")),
                            "ask": (desk.get("up_ask") if pos.side == "Up" else desk.get("down_ask"))})
                except Exception:  # noqa: BLE001 — mark logging must never break the live tick
                    log.exception("[active] position-mark record failed (non-fatal)")
            action = ST.decide(thesis=thesis, book=book, position=pos, now_ts=now,
                               seconds_left=desk.get("seconds_left", 0), stake_usdc=STAKE, confirm=confirm, ev=ev)
            if action["action"] == "enter" and pos is None and ws not in pending and now >= cooldown.get(ws, 0):
                # maker-entry-v1: POST a resting bid on the confirmed side instead of paying the ask.
                # action["price"] (the ask we would have paid) becomes the taker counterfactual for slip.
                px = ME.desired_entry_bid(action["side"], book)
                if px is not None:
                    pending[ws] = {"pq": ME.PendingQuote(side=action["side"], price=px, posted_ts=now,
                                                         decision_ask=action["price"]),
                                   "entry_prob": action.get("entry_prob"), "conviction": action.get("conviction"),
                                   "ofi": desk.get("tape_ofi_10s"), "persistence": action.get("persistence"),
                                   "reason": action.get("reason")}
                    log.info("[active] QUOTE %s %s bid @ %.3f (taker ask %.3f) — %s", desk["slug"],
                             action["side"], px, action["price"], action.get("reason"))
            if pos is None and ws in pending:
                # display truth: while the quote rests, the live action is 'quote', not a re-fired 'enter'
                _pq = pending[ws]["pq"]
                action = {"action": "quote", "side": _pq.side, "price": _pq.price,
                          "reason": f"maker bid resting @ {_pq.price:.2f} on {_pq.side} "
                                    f"(taker would pay {_pq.decision_ask:.2f}; fills only on a print)"}
            elif action["action"] == "exit" and pos is not None:
                r = ST.exit_pnl(pos, action.get("price"), STAKE)
                _close_position(conn, ws, action["kind"], action.get("price"), r["pnl"], None, now)
                live[ws] = None
                cooldown[ws] = now + REENTRY_COOLDOWN_S   # allow a fresh re-scout later in the window
                log.info("[active] EXIT %s %s @ %s kind=%s pnl=%+.2f", desk["slug"], pos.side,
                         (f"{action['price']:.3f}" if action.get("price") is not None else "—"),
                         action["kind"], r["pnl"])
            _write_state(conn, desk, live.get(ws), action, now, thesis, confirm, ev)
            last_tick_ts[ws] = now                       # bounds the next tick's tape-print fill window
            for w in [w for w in last_tick_ts if w != ws]:
                last_tick_ts.pop(w, None)
            # settle any still-open position from a CLOSED window
            for w, p in list(live.items()):
                if isinstance(p, Position) and now >= w + 300 + SETTLE_GRACE_S:
                    winner = _resolve(f"{LD.SLUG_PREFIX}{w}")
                    if winner in ("Up", "Down"):
                        r = ST.settle_pnl(p, winner, STAKE)
                        _close_position(conn, w, "settlement", r["exit_price"], r["pnl"], winner, now)
                        live[w] = None
                        log.info("[active] SETTLE win=%s %s pnl=%+.2f", winner, p.side, r["pnl"])
                    elif now >= w + 300 + 1800:           # couldn't resolve after 30m — close the row so it
                        _close_position(conn, w, "abandoned", None, None, None, now)  # doesn't hang open forever
                        live[w] = None
                        log.info("[active] ABANDON unresolved window %s %s", w, p.side)
            # resolve brain-v2 shadow rows for ANY closed window with pending rows (incl. windows where no
            # position was opened); reuse the same Gamma resolver. Sweep windows unresolved after 30m.
            # ISOLATED: shadow resolution must never abort a tick whose live decision already ran above.
            try:
                for zr in conn.execute(
                        "SELECT DISTINCT window_start_ts FROM crypto5m_z_shadow WHERE status='pending' "
                        "AND (window_start_ts + 300 + ?) <= ?", (SETTLE_GRACE_S, now)).fetchall():
                    zw = zr["window_start_ts"]
                    zwin = _resolve(f"{LD.SLUG_PREFIX}{zw}")
                    if zwin in ("Up", "Down"):
                        z_settle = 1 if zwin == "Up" else 0
                        ZL.resolve_window(conn, zw, z_settle,
                                          sharp_window_label(_window_log_returns(zw), z_settle), now)
                ZL.sweep_incomplete(conn, now - 1800)   # mark windows unresolved after 30m incomplete (excluded)
            except Exception:  # noqa: BLE001 — shadow resolution must never break the live engine
                log.exception("[active] brain-v2 shadow resolution failed (non-fatal)")
        except Exception:  # noqa: BLE001 — never let a transient error kill the engine
            log.exception("[active] iteration %d failed (transient); continuing", it)
        if max_iterations and it >= max_iterations:
            return it
        time.sleep(poll_interval)


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="crypto5m continuous active paper engine (read-only, no orders)")
    ap.add_argument("--paper-db", default="data/crypto5m/active_trader_v0.sqlite")
    ap.add_argument("--continuous", action="store_true", help="no-op (always continuous); launcher compat")
    ap.add_argument("--poll-interval", type=int, default=3)
    ap.add_argument("--max-iterations", type=int, default=None)
    a = ap.parse_args(argv)
    conn = _connect(a.paper_db)
    n = run(conn, poll_interval=a.poll_interval, max_iterations=a.max_iterations)
    log.info("[active] exit: %s iterations", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
