"""Trial arm: a small-clip funded trial for a new or re-armed (wallet, chain).

Spec docs/superpowers/specs/2026-10-01-memecoin-trial-arm-design.md. Rule: Gamma's pre-registration
docs/audits/2026-10-01-trial-arm-promotion-preregistration.md (3a0b141ee), implemented exactly in
`trial_verdict`. Thresholds are fractions (-0.30, not -30) and are never retuned on trial results:
a change is a new stamp, applied to trials started after it.

A trial position carries `source.trial` in its frozen funded contract. The grader reads closes by
that marker; nothing else makes a position a trial position.
"""
from __future__ import annotations

import logging
import math
import random
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

RULE = "3a0b141ee"
TP_TWO_OF_THREE_RULE = "tp-two-of-three-v1"
# The reservation's refusal for an entry path that cannot carry a trial's clip and marker (the slot
# lane). Nothing is held, so the on-chain lane gives that signature its one ordinary attempt.
TRIAL_UNMARKED = "trial_unmarked"
MAX_POSITIONS = 20                     # the rule never needs a 21st
CHECKPOINTS = (5, 10, 20)
EXPIRY_DAYS = 14
SHARED_S = 30.0                        # the shortlist's test 6
DRAWS, SEED = 4000, 20260930
MANUAL_REPORT_SHARE = 0.20             # above this, the verdict is reported with and without them
# Fixed round-trip fee, p90 of the current regime (spec section 4, measured 2026-10-01 with
# scripts/research/trial_clip_fees.py). Reported beside r as a share of the clip, never a condition.
FIXED_FEE_P90_USD = {"solana": 0.131, "4663": 0.110}


def _ts(iso):
    return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))


def r_of(close):
    """r = realized_pnl / entry_notional, or None when the notional is null or not positive."""
    try:
        notional = float(close.get("entry_notional"))
        pnl = float(close.get("realized_pnl"))
    except (TypeError, ValueError):
        return None
    return pnl / notional if notional > 0 else None


def _stats(closes):
    """Mean, median, best coin and its share, mean without it, over closes carrying `r`."""
    rs = [c["r"] for c in closes]
    by_mint = defaultdict(float)
    for c in closes:
        by_mint[c["mint"]] += c["r"]
    best_mint = max(by_mint, key=by_mint.get) if by_mint else None
    best = by_mint.get(best_mint, 0.0)
    positive = sum(v for v in by_mint.values() if v > 0)
    rest = [c["r"] for c in closes if c["mint"] != best_mint]
    return {"n": len(rs), "mean": statistics.fmean(rs) if rs else None,
            "median": statistics.median(rs) if rs else None,
            "best_mint": best_mint,
            "best_share_of_positive": (best / positive) if positive > 0 and best > 0 else None,
            "mean_without_best": statistics.fmean(rest) if rest else None,
            "shared_share": (sum(1 for c in closes if c.get("shared")) / len(closes)
                             if closes else None)}


def _promotable(st, floor):
    return (st["mean"] > floor and st["mean_without_best"] is not None
            and st["mean_without_best"] > 0 and st["best_share_of_positive"] is not None
            and st["best_share_of_positive"] <= 0.5)


def _rule(checkpoint, st):
    if checkpoint == 5:
        return "DEMOTE" if st["mean"] < -0.30 else "CONTINUE"
    if checkpoint == 10:
        if st["mean"] < -0.10:
            return "DEMOTE"
        return "PROMOTE" if _promotable(st, 0.10) else "CONTINUE"
    return "PROMOTE" if _promotable(st, 0.05) else "DEMOTE"


def ci90_by_mint(closes, draws=DRAWS, seed=SEED):
    """90% bootstrap CI of the mean r, resampling MINTS (a coin's closes move together)."""
    by_mint = defaultdict(list)
    for c in closes:
        by_mint[c["mint"]].append(c["r"])
    groups = list(by_mint.values())
    if len(groups) < 2:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(draws):
        pick = [r for _g in range(len(groups)) for r in groups[rng.randrange(len(groups))]]
        means.append(sum(pick) / len(pick))
    means.sort()
    return (means[int(0.05 * draws)], means[int(0.95 * draws) - 1])


def trial_verdict(closes, started_at, now, *, done):
    """The next unwritten verdict of one trial, or None.

    `closes` are its marked closes (dicts with position_id, mint, realized_pnl, entry_notional,
    closed_at, exit_reason, shared). `done` holds the checkpoints already written. The lowest
    reached checkpoint not in `done` is graded on the FIRST k closes by close time; day 14 with
    fewer than 10 closes is INSUFFICIENT. SHADOW replaces a PROMOTE when >= 50% of the graded
    closes were also bought by another armed wallet on the chain within 30 s.
    """
    ordered = sorted(closes, key=lambda c: (str(c.get("closed_at") or ""), c.get("position_id") or 0))
    valid, excluded = [], []
    for c in ordered:
        r = r_of(c)
        if r is None:
            excluded.append({"position_id": c.get("position_id"), "mint": c.get("mint")})
            continue
        valid.append({**c, "r": r, "manual_exit": c.get("exit_reason") == "manual"})
    checkpoint = next((k for k in CHECKPOINTS if k not in done and len(valid) >= k), None)
    if checkpoint is None:
        expired = _ts(now) >= _ts(started_at) + timedelta(days=EXPIRY_DAYS)
        if not (expired and len(valid) < 10 and "day14" not in done):
            return None
        checkpoint, graded = "day14", valid
    else:
        graded = valid[:checkpoint]
    st = _stats(graded)
    if checkpoint == "day14":
        verdict = "INSUFFICIENT"
    else:
        verdict = _rule(checkpoint, st)
        if verdict == "PROMOTE" and st["shared_share"] is not None and st["shared_share"] >= 0.5:
            verdict = "SHADOW"
    detail = {**st, "ci90": ci90_by_mint(graded) if graded else None, "excluded": excluded,
              "closes": [{"position_id": c["position_id"], "mint": c["mint"], "r": c["r"],
                          "exit_reason": c.get("exit_reason"), "manual_exit": c["manual_exit"],
                          "shared": bool(c.get("shared"))} for c in graded]}
    manual = [c for c in graded if c["manual_exit"]]
    if graded and len(manual) / len(graded) > MANUAL_REPORT_SHARE:
        detail["without_manual"] = _stats([c for c in graded if not c["manual_exit"]])
    return {"checkpoint": checkpoint, "verdict": verdict, "detail": detail}


def tp_two_of_three_closes(closes):
    """First three known full trial closes, in close-time/position-id order.

    Callers supply the canonical `trial_closes` population (real, closed, marked, non-manual
    entries). Unknown PnL, notional or close metadata remains pending, never a non-TP outcome.
    """
    valid = []
    for close in closes:
        try:
            position_id = int(close["position_id"])
            closed_at = _ts(close["closed_at"])
            notional = float(close["entry_notional"])
            pnl = float(close["realized_pnl"])
            reason = close.get("exit_reason")
            if (closed_at.utcoffset() is None or not isinstance(reason, str)
                    or not reason.strip()):
                continue
            if position_id <= 0 or not math.isfinite(notional) or notional <= 0 or not math.isfinite(pnl):
                continue
            r = pnl / notional
            if not math.isfinite(r):
                continue
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        valid.append((closed_at, {**close, "position_id": position_id, "r": r,
                                "manual_exit": reason == "manual",
                                "tp": reason in {"real_tp", "tp"} and pnl > 0}))
    return [close for _at, close in sorted(valid, key=lambda item: (
        item[0], item[1]["position_id"]))[:3]]


def tp_two_of_three_verdict(closes, *, done=()):
    """Operator rule: two actual profitable TP exits promote; two other full closes demote.

    A split waits for the third close without an expiry. Shared origins and the old stamped
    rule's statistical thresholds do not change this rule. No bootstrap runs on admission.
    """
    graded, tp = [], 0
    for close in tp_two_of_three_closes(closes):
        graded.append(close)
        tp += int(close["tp"])
        non_tp = len(graded) - tp
        if tp < 2 and non_tp < 2:
            continue
        checkpoint = len(graded)
        if checkpoint in done:
            return None
        detail = {**_stats(graded), "ci90": None, "shared_share": None,
                  "excluded": [], "closes": graded,
                  "tp_count": tp, "non_tp_count": non_tp}
        return {"checkpoint": checkpoint, "verdict": "PROMOTE" if tp == 2 else "DEMOTE",
                "detail": detail}
    return None


def marker(row):
    """The contract marker of one trial row: {trial_id, rule, clip_usd}."""
    return {"trial_id": int(row["id"]), "rule": row["rule"], "clip_usd": float(row["clip_usd"])}


# --- grading (spec 5.3) and actions (spec 5.4, 5.6): runner maintenance, never the entry path ---

TERMINAL = {"DEMOTE", "PROMOTE", "INSUFFICIENT", "SHADOW"}


def closes_with_shared(conn, trial):
    """The trial's closes, each flagged `shared` when another wallet armed on the chain bought the
    same coin within 30 s of the source's own buy (the shortlist's test 6)."""
    from sniper.persistence import db_memecoin as m
    chain = trial["chain"]
    armed = m.real_approved_user_ids(conn, network_id=chain) - {trial["source_user_id"]}
    out = []
    for close in m.trial_closes(conn, trial):
        at = m.source_buy_at(conn, close.get("origin_event_id")) or close.get("opened_at")
        shared = False
        if at and armed:
            t0 = _ts(at)
            shared = any(uid in armed and abs((_ts(created) - t0).total_seconds()) <= SHARED_S
                         for uid, created in m.buys_of_mint(conn, chain, close["mint"]))
        out.append({**close, "shared": shared})
    return out


def _action(verdict, auto):
    """Only DEMOTE (and PROMOTE under 'both') acts on its own. INSUFFICIENT and SHADOW are REPORTS in
    the stamped rule 3a0b141ee ("the operator decides"; "reported as shadow"), and the operator's
    roster rule disarms on funded results only (Gamma MEDIUM on 92646580c): they stop and page."""
    if verdict not in TERMINAL:
        return None
    if verdict == "PROMOTE":
        return "ended_kept_armed" if auto == "both" else "stopped"
    if verdict == "DEMOTE":
        return "ended_disarmed" if auto in ("demote", "both") else "stopped"
    return "stopped"


def _pct(x):
    return "n/a" if x is None else "%+.1f%%" % (100.0 * x)


def _page_text(trial, handle, v, action):
    d = v["detail"]
    if trial["rule"] == TP_TWO_OF_THREE_RULE:
        lines = ["%s %s trial #%d at %s closes: %s, action %s" % (
                     handle, trial["chain"], trial["id"], v["checkpoint"], v["verdict"], action),
                 "TP %d, non-TP %d; rule %s" % (
                     d["tp_count"], d["non_tp_count"], TP_TWO_OF_THREE_RULE)]
        for c in d["closes"]:
            lines.append("  #%s %s %s %s [%s]" % (
                c["position_id"], str(c["mint"])[:12], _pct(c["r"]), c.get("exit_reason"),
                "TP" if c["tp"] else "non-TP"))
        return "\n".join(lines)
    fee = FIXED_FEE_P90_USD.get(trial["chain"])
    ci = d.get("ci90")
    lines = ["%s %s trial #%d at %s closes: %s, action %s" % (
                 handle, trial["chain"], trial["id"], v["checkpoint"], v["verdict"], action),
             "n %d, mean %s, median %s, 90%% CI by mint %s, best coin share %s, mean without it %s, "
             "shared-origin %s" % (
                 d["n"], _pct(d["mean"]), _pct(d["median"]),
                 ("[%s, %s]" % (_pct(ci[0]), _pct(ci[1]))) if ci else "n/a",
                 "n/a" if d["best_share_of_positive"] is None else "%.2f" % d["best_share_of_positive"],
                 _pct(d["mean_without_best"]),
                 "n/a" if d["shared_share"] is None else "%.0f%%" % (100 * d["shared_share"])),
             "fee share %s (chain p90 fixed fee / $%.0f clip)" % (
                 "n/a" if fee is None else "%.2f%%" % (100.0 * fee / float(trial["clip_usd"])),
                 float(trial["clip_usd"]))]
    if "without_manual" in d:
        w = d["without_manual"]
        lines.append("without manual exits: n %d, mean %s" % (w["n"], _pct(w["mean"])))
    for c in d["closes"]:
        lines.append("  #%s %s %s %s%s%s" % (c["position_id"], str(c["mint"])[:12], _pct(c["r"]),
                                           c["exit_reason"], " [manual exit]" if c["manual_exit"] else "",
                                           " [shared]" if c["shared"] else ""))
    if d["excluded"]:
        lines.append("excluded (no entry notional): %s" % ", ".join(
            "#%s" % x["position_id"] for x in d["excluded"]))
    return "\n".join(lines)


def _send(alert_fn, kind, text):
    try:
        if alert_fn is not None:
            alert_fn(kind, text)
        else:
            from sniper.services.memecoin_alerts import send_alert
            send_alert(kind, text, icon="🧪")
    except Exception:
        logger.exception("trial page failed: %s", kind)       # a page never blocks the action


def grade_trial(conn, trial, *, now_iso, alert_fn=None):
    """One trial, one pass: end it when its approval is gone, else write the next verdict once and
    apply its action. Returns the verdict dict, or None."""
    from sniper.persistence import db_memecoin as m
    from sniper.settings import memecoin as cfg
    import json
    row = conn.execute("SELECT handle FROM memecoin_wallets WHERE source_user_id=?",
                       (trial["source_user_id"],)).fetchone()
    handle = (row[0] if row and row[0] else trial["source_user_id"])
    if trial["source_user_id"] not in m.real_approved_user_ids(conn, network_id=trial["chain"]):
        m.end_trial(conn, trial["id"], now_iso=now_iso, reason="approval_cleared")
        _send(alert_fn, "TRIAL", "%s %s trial #%d ended: approval_cleared (paused, excluded or "
                                 "disarmed)" % (handle, trial["chain"], trial["id"]))
        return None
    if trial["stopped_at"]:
        return None                                    # a terminal verdict already acted
    two_of_three = trial["rule"] == TP_TWO_OF_THREE_RULE
    done = m.trial_checkpoints_done(conn, trial["id"])
    v = (tp_two_of_three_verdict(m.trial_closes(conn, trial), done=done) if two_of_three else
         trial_verdict(closes_with_shared(conn, trial), trial["started_at"], now_iso, done=done))
    if v is None:
        return None
    action = _action(v["verdict"], "both" if two_of_three else cfg.memecoin_real_trial_auto())
    record = dict(trial_id=trial["id"], checkpoint=v["checkpoint"], checked_at=now_iso,
                  closes=v["detail"]["n"], mean_r=v["detail"]["mean"], verdict=v["verdict"],
                  detail_json=json.dumps(v["detail"], sort_keys=True, default=str))
    recorded = False
    if action == "stopped":
        m.stop_trial(conn, trial["id"], now_iso=now_iso)
    elif action == "ended_kept_armed":
        if two_of_three:
            # The full-size grant reads both rows. Neither may be published alone on a crash.
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                if not m.end_trial(conn, trial["id"], now_iso=now_iso,
                                   reason=v["verdict"].lower(), commit=False):
                    return None
                m.record_trial_verdict(conn, **record, action=action, commit=False)
            recorded = True
        else:
            m.end_trial(conn, trial["id"], now_iso=now_iso, reason=v["verdict"].lower())
    elif action == "ended_disarmed":
        res = m.set_wallet_chain_armed(
            conn, trial["source_user_id"], trial["chain"], False, actor="trial_rule",
            reason="trial %s #%d %s at %s" % (trial["rule"], trial["id"], v["verdict"], v["checkpoint"]))
        if res.get("ok"):
            m.end_trial(conn, trial["id"], now_iso=now_iso, reason=v["verdict"].lower())
        else:
            action = "disarm_failed"
            m.stop_trial(conn, trial["id"], now_iso=now_iso)
            _send(alert_fn, "TRIAL", "DISARM FAILED %s %s trial #%d: %s; the trial is stopped" % (
                handle, trial["chain"], trial["id"], res.get("reason")))
    if not recorded:
        m.record_trial_verdict(conn, **record, action=action)
    if v["verdict"] != "CONTINUE":
        _send(alert_fn, "TRIAL", _page_text(trial, handle, v, action))
    return {**v, "action": action}


def grade_open_trials(conn, *, now_iso, alert_fn=None):
    """Every open trial, each in its own try: one trial's error never blocks another, and a pass
    that raised wrote nothing, so the next pass retries it."""
    from sniper.persistence import db_memecoin as m
    out = []
    for trial in m.open_trials(conn):
        try:
            out.append(grade_trial(conn, trial, now_iso=now_iso, alert_fn=alert_fn))
        except Exception:
            logger.exception("trial grading failed trial=%s", trial.get("id"))
            try:
                conn.rollback()
            except Exception:
                pass
    return out


GRADE_PERIOD_S = 60.0


def run_grader_forever(connect, *, now_iso, period_s=GRADE_PERIOD_S, sleep=None):
    """The runner's trial grader: its own thread and writable connection, one pass a minute. A failed
    pass drops the connection and the next pass reconnects. With no trial rows a pass is one read."""
    import time
    sleep = sleep or time.sleep
    conn = None
    while True:
        try:
            if conn is None:
                conn = connect()
            grade_open_trials(conn, now_iso=now_iso())
        except Exception:
            logger.exception("trial grader pass failed; reconnecting next pass")
            try:
                conn.close()
            except Exception:
                pass
            conn = None
        sleep(period_s)
