"""Alert when a funded position's loss rails have no price to read.

THE FAILURE THIS EXISTS FOR, and why the existing detector could not see it. `rails_blind_streak`
increments INSIDE the exit sweep (memecoin_real_runner.py:2640), so it detects "the sweep ran and
resolved nothing" and is structurally blind to "the sweep did not run". Measured 2026-08-24: 87.1
minutes of holding a real position with an ACTIVE 35% stop and ZERO marks written for any mint,
worst single window 926 seconds on position 44624 — and not one alert, because a counter that only
advances inside the sweep cannot advance when the sweep is absent.

The whole point is the asymmetry: while that condition holds it is unobservable, so "we have not
seen it lately" is not evidence it is not happening. 2026-08-25 measured 1.0 minute against the
previous day's 87.1, which says intermittent, not fixed.

So this checks TIME, not failures, and it runs OUTSIDE the sweep: for every open real position
carrying a loss rail, how old is the newest mark for its coin.

RESIDUAL, stated so nobody "fixes" it without weighing the cost. A fully stalled loop stalls this
checker too. That class is already owned by the heartbeat watchdog — a stale heartbeat gets the
runner restarted — so in-loop is the right home for v1. Moving this into the watchdog would buy
detection of a case already covered and would pay for it by putting a DB read of open positions
into the process whose job is to be simple enough never to fail.

IT ALERTS. IT DOES NOT ACT.
  - No auto-exit. A blind exit needs a price too, and forcing a sale on missing data is worse than
    the blindness it would be reacting to.
  - No entry pause. "Refuse NEW entries while the mark stream is globally stale" is a real option
    and a defensible one, but it is a GATE change: the operator should decide it once this alert
    has shown the true frequency, not have it arrive bundled with the instrument that measures it.

`MEMECOIN_STUCK_MARK_MAX_AGE_S` (600s) is deliberately NOT reused. It is calibrated for a different
question, and overloading one knob across two questions is how the last threshold ended up inert.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Per-position alert state: position_id -> monotonic stamp of the last page. Process-local, so a
# restart re-pages once — the right side to err on for a condition that means "a rail is deaf".
_ALERTED = {}


def newest_mark_age_s(conn, *, mint, now_iso, chain_id=None):
    """Seconds since the newest mark for `mint`, or None when the coin has never been marked.

    None is NOT zero and NOT infinity: a coin with no mark at all is a different fault from a coin
    whose marks stopped, and the caller decides. Cutoffs are built in Python — SQLite's
    datetime('now') renders a space separator and silently widens every window it touches.
    """
    from datetime import datetime
    row = conn.execute(
        # CHAIN FIRST. `memecoin_position_marks` is keyed (chain_id, mint) and the same EVM address
        # can exist on several chains, so a mint-only read can answer with another chain's mark and
        # hide a blind-position alert. chain_id is also the index's leading column, so passing it
        # turns a scan into a seek. None keeps the old behaviour for a caller that has no chain.
        "SELECT MAX(sweep_ts) FROM memecoin_position_marks WHERE mint=?"
        + (" AND chain_id=?" if chain_id else ""),
        ((mint, str(chain_id)) if chain_id else (mint,))).fetchone()
    newest = row[0] if row else None
    if not newest:
        return None
    try:
        a = datetime.fromisoformat(str(newest).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (b - a).total_seconds()


def _age_s(stamp, now_iso):
    """Seconds between an ISO stamp and now_iso; None when either is missing or unparseable."""
    from datetime import datetime
    if not stamp:
        return None
    try:
        a = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (b - a).total_seconds()


def evaluate(conn, *, now_iso, open_positions, has_loss_rail_fn, opened_age_fn=None,
             notice_s=60.0, alert_s=120.0):
    """Blind positions right now. Returns (findings, worst_age_s, blind_count); never raises.

    A position is blind when it carries a loss rail AND the newest mark for its coin is older than
    `alert_s`. A coin never marked at all counts once the POSITION itself is older than the bound —
    otherwise every fresh entry would page during the seconds before its first mark lands.
    """
    findings, worst = [], 0.0
    for index, pos in enumerate(open_positions or ()):
        try:
            if not has_loss_rail_fn(pos):
                continue
            age = newest_mark_age_s(conn, mint=pos.get("mint"), now_iso=now_iso,
                                    chain_id=pos.get("network_id"))
            # THE RAILS READ EXACT QUOTES TOO (2026-09-11). On Solana a mark is written only when
            # someone swaps the coin, so a quiet coin went "deaf" here while its stop was reading an
            # exact whole-bag quote every ~1.3 s (QQQ 55669: 47 s "no price", 45-54 exact quotes a
            # minute, largest gap 1.8 s). The freshest price the rails can use is the younger of
            # the newest mark and the position's last exact executable quote.
            exec_age = _age_s(pos.get("last_executable_at"), now_iso)
            if exec_age is not None:
                age = exec_age if age is None else min(age, exec_age)
            if age is None:
                # Never marked. Judge by the position's own age so a fresh entry is not paged for
                # the gap before its first mark.
                age = opened_age_fn(pos) if opened_age_fn else None
                if age is None:
                    continue
            if age <= float(notice_s):
                continue
            worst = max(worst, float(age))
            findings.append({"position_id": pos.get("id"), "mint": pos.get("mint"),
                             "age_s": float(age), "blind": float(age) > float(alert_s)})
        except Exception:
            # Report by INDEX, not by re-reading the row. A handler that calls back into the object
            # that just failed can raise from inside the handler, which would defeat the whole
            # fail-soft guarantee and take the maintenance pass down with it.
            logger.exception("rails observability could not evaluate open position #%d", index)
    blind = [f for f in findings if f["blind"]]
    return findings, worst, len(blind)


def page_blind(findings, *, now_mono, alert_fn, realert_s=300.0):
    """Page once per blind position, then at most every `realert_s` while it persists.

    Deduped PER POSITION, not per pass: at a 120s maintenance cadence an un-deduped alert would
    fire every pass for the whole outage, which is how an alert gets muted and then missed.
    Recovery clears the state so the next occurrence pages immediately.
    """
    paged = 0
    live = {f["position_id"] for f in findings if f["blind"]}
    for key in list(_ALERTED):
        if key not in live:
            _ALERTED.pop(key, None)          # recovered; the next blind window pages at once
    for f in findings:
        if not f["blind"]:
            continue
        last = _ALERTED.get(f["position_id"])
        if last is not None and (float(now_mono) - float(last)) < float(realert_s):
            continue
        _ALERTED[f["position_id"]] = float(now_mono)
        paged += 1
        try:
            alert_fn("⚠ memecoin RAILS DEAF | pos %s %s has an active loss rail and NO price "
                     "for %.0fs. The stop cannot fire on a price that does not exist."
                     % (f["position_id"], str(f["mint"])[:12], f["age_s"]))
        except Exception:
            # An alert failure must never reach the loop that holds the money.
            logger.exception("rails observability alert failed for %s", f["position_id"])
    return paged


def snapshot(worst_age_s, blind_count, checked):
    """Heartbeat projection for this checker.

    THE HEARTBEAT PROJECTION IS A WHITELIST. Fields added here must ALSO be named wherever the
    heartbeat is assembled, or they vanish silently — which is exactly what happened to the RPC
    pool's `degraded` field on 2026-08-25: added to the snapshot, dropped one layer up, and the
    operator kept reading a number that could not change.
    """
    return {"worst_blind_age_s": round(float(worst_age_s or 0.0), 1),
            "blind_positions": int(blind_count or 0),
            "checked_positions": int(checked or 0)}
