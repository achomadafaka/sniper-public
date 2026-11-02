"""memecoin_digest.py — periodic operator digest: the system reports its own anomalies.

"if I'm not reading the logs we wouldn't even know" (operator, 2026-07-17). One compact Telegram
message per MEMECOIN_TG_DIGEST_PERIOD_S built ONLY from the DB — no live APIs, so a feed/RPC
outage can never block the report that would surface it. All window anchors are computed in
Python from the corrected real-UTC clock (`sniper.util.clock`). SQLite `datetime('now')`
string-comparisons are forbidden because the host clock can diverge from the observed clock.

Severity contract (first line): 🟢 all normal · 🟡 warnings present · 🔴 breaker-relevant
(daily-loss breaker tripped, or realized daily loss beyond 60% of the limit).

Fail-soft: digest_pass never raises into the worker. It only submits to the existing bounded
Telegram dispatcher; HTTP remains off-thread from both the digest collector and funded loop.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone

from sniper.persistence import db_memecoin as m
from sniper.settings import memecoin as cfg
from sniper.util.clock import real_utc_now

logger = logging.getLogger(__name__)

GREEN, YELLOW, RED = "\U0001F7E2", "\U0001F7E1", "\U0001F534"
_SEVERITY_EMOJI = {"green": GREEN, "yellow": YELLOW, "red": RED}

# Pre-registered ladder read gate: >=30 closes per band before the verdict is read.
LADDER_READ_GATE = 30
# A funded reservation should confirm/release in seconds; older than this = stuck slot.
STALE_RESERVATION_S = 600.0
# Feed silence beyond this during the 17:00-03:00 UTC activity peak is anomalous
# (off-peak droughts are market-wide and normal — 2026-07-11 drought finding).
FEED_GAP_WARN_S = 900.0
PEAK_START_HOUR, PEAK_END_HOUR = 17, 3
# Fraction of the daily-loss limit at which the digest gets loud before the breaker latches.
DAILY_LOSS_WARN_FRACTION = 0.60
# Entry-attempt terminal states that are failures (refused = a normal gate, filled = success).
_FAILURE_STATUSES = (
    "failed", "unknown", "route_failed", "safety_failed", "aborted", "anomaly", "error",
)
_MAX_LIST = 6           # per-section line cap — the digest must stay one compact message
_MAX_MESSAGE_CHARS = 3900  # Telegram hard cap is 4096


def _iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _parse_iso_epoch(ts):
    """ISO string -> epoch seconds; None on garbage. Handles trailing 'Z' and naive stamps
    (naive = assumed UTC — all our writers stamp UTC)."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _usd(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "$?"
    return "%s$%.2f" % ("+" if v >= 0 else "-", abs(v))


def _mins(seconds):
    return "%.0fm" % (max(0.0, float(seconds)) / 60.0)


def _in_peak_hours(now_epoch):
    hour = datetime.fromtimestamp(now_epoch, tz=timezone.utc).hour
    return hour >= PEAK_START_HOUR or hour < PEAK_END_HOUR


def _ladder_band(entry_mcap):
    """Read-time cohort split (matches memecoin_real_runner.ladder_band_of; NULL entry_mcap =
    assumed-micro = nano by construction)."""
    from sniper.services.memecoin_real_runner import ladder_band_of
    return ladder_band_of(entry_mcap, entry_mcap is None)


def collect_digest(conn, *, now_epoch=None, period_s=3600.0, since_epoch=None):
    """Assemble the digest data dict from the DB only. `now_epoch` is a real-UTC epoch
    (defaults to the corrected clock); the window is (now - period_s, now]."""
    now_epoch = float(now_epoch) if now_epoch is not None else real_utc_now()
    period_s = float(period_s)
    if not math.isfinite(period_s) or period_s <= 0:
        raise ValueError("digest period must be positive")
    since_epoch = (now_epoch - period_s) if since_epoch is None else float(since_epoch)
    if not math.isfinite(since_epoch) or since_epoch > now_epoch:
        raise ValueError("digest window is invalid")
    period_s = now_epoch - since_epoch
    since_iso = _iso(since_epoch)
    hour_ago_iso = _iso(now_epoch - 3600.0)
    now_iso = _iso(now_epoch)
    day = now_iso[:10]
    warns = []

    # --- real fills + exits in the window -------------------------------------------------
    entries = [dict(r) for r in conn.execute(
        "WITH real_entries AS ("
        "SELECT p.id, p.ticker, p.mint, p.network_id, p.entry_notional, p.opened_at, "
        "COALESCE(MAX(CASE WHEN a.terminal_status='filled' THEN a.completed_at END), "
        "p.opened_recorded_at, p.opened_at) AS entry_observed_at "
        "FROM memecoin_positions p LEFT JOIN memecoin_entry_attempts a ON a.position_id=p.id "
        "WHERE p.execution_mode='real' GROUP BY p.id) "
        "SELECT ticker, mint, network_id, entry_notional, opened_at, entry_observed_at "
        "FROM real_entries WHERE julianday(entry_observed_at) > julianday(?) "
        "AND julianday(entry_observed_at) <= julianday(?) "
        "ORDER BY julianday(entry_observed_at) DESC",
        (since_iso, now_iso))]
    exits = [dict(r) for r in conn.execute(
        "SELECT ticker, mint, network_id, realized_pnl, exit_reason, closed_at, "
        "closed_recorded_at "
        "FROM memecoin_positions WHERE execution_mode='real' AND is_open=0 "
        "AND julianday(COALESCE(closed_recorded_at, closed_at)) > julianday(?) "
        "AND julianday(COALESCE(closed_recorded_at, closed_at)) <= julianday(?) "
        "ORDER BY julianday(COALESCE(closed_recorded_at, closed_at)) DESC",
        (since_iso, now_iso))]
    exit_pnl = sum(float(x["realized_pnl"] or 0.0) for x in exits)

    # --- refusal counts by reason (top 6) --------------------------------------------------
    refusals = [(r[0], r[1]) for r in conn.execute(
        "SELECT COALESCE(terminal_reason, '?') AS rsn, COUNT(*) AS n "
        "FROM memecoin_entry_attempts WHERE terminal_status='refused' "
        "AND julianday(COALESCE(completed_at, decision_started_at)) > julianday(?) "
        "AND julianday(COALESCE(completed_at, decision_started_at)) <= julianday(?) "
        "GROUP BY rsn ORDER BY n DESC, rsn LIMIT ?",
        (since_iso, now_iso, _MAX_LIST))]

    # --- failed/unknown entry attempts with terminal reasons -------------------------------
    failed_attempts = [(r[0], r[1], r[2]) for r in conn.execute(
        "SELECT terminal_status, COALESCE(terminal_reason, '?') AS rsn, COUNT(*) AS n "
        "FROM memecoin_entry_attempts WHERE julianday(COALESCE(completed_at, decision_started_at)) "
        "> julianday(?) AND julianday(COALESCE(completed_at, decision_started_at)) "
        "<= julianday(?) AND terminal_status IN "
        "(%s) GROUP BY terminal_status, rsn ORDER BY n DESC, rsn"
        % ",".join("?" for _ in _FAILURE_STATUSES),
        (since_iso, now_iso, *_FAILURE_STATUSES))]
    failed_total = sum(n for _, _, n in failed_attempts)
    if failed_total:
        # Name the statuses that actually occurred. The old line always said "FAILED/unknown",
        # so a window of 4 aborted + 2 safety_failed read as if 'unknown' reservations existed
        # and sent the operator hunting a chain explorer for nothing (2026-08-07).
        present = sorted({status for status, _rsn, _n in failed_attempts})
        line = "⚠ %d entry attempt(s) in window with a failure status: %s — reasons below" % (
            failed_total, "/".join(present))
        if "unknown" in present:
            line += "; 'unknown' = reservation recovery + chain explorer check"
        warns.append(line)

    # --- daily realized PnL vs the loss breaker --------------------------------------------
    from sniper.services.memecoin_entry_knobs import effective
    value, source = effective(conn, cfg)["MEMECOIN_DAILY_LOSS_LIMIT_USD"]
    daily_limit = float(value if source == "override" else cfg.MEMECOIN_DAILY_LOSS_LIMIT_USD)
    day_start_iso = day + "T00:00:00+00:00"
    # ALL-MONEY, DELIBERATELY, because this number sits beside the breaker state and the breaker
    # reads the same quantity. Filtering manual out here would show the operator headroom he does
    # not have. The robot figure is added as a SEPARATE labelled line below rather than replacing
    # it — an unlabelled pair on one line is the thing that must not happen (Alpha, 2026-08-27).
    daily_realized = float(conn.execute(
        "SELECT COALESCE(SUM(realized_pnl),0) FROM memecoin_positions "
        "WHERE execution_mode='real' AND is_open=0 AND julianday(closed_at) "
        "BETWEEN julianday(?) AND julianday(?)",
        (day_start_iso, now_iso),
    ).fetchone()[0] or 0.0)
    from sniper.persistence.db_memecoin import robot_performance_sql
    robot_realized = float(conn.execute(
        "SELECT COALESCE(SUM(realized_pnl),0) FROM memecoin_positions "
        "WHERE is_open=0 AND julianday(closed_at) BETWEEN julianday(?) AND julianday(?) AND "
        + robot_performance_sql(),
        (day_start_iso, now_iso),
    ).fetchone()[0] or 0.0)
    breaker_tripped = daily_limit > 0 and m.is_daily_loss_tripped(conn, day)
    severity_red = bool(breaker_tripped)
    # THE ROBOT LINE, LABELLED AND SEPARATE. daily_realized above is ALL-MONEY because the breaker
    # reads that quantity; this is the system's own result. Emitted only when they differ, so a
    # day with no manual entries stays one number instead of two identical ones.
    # Carried on the payload; the renderer emits it. `lines` does not exist in this scope — it is
    # built ~100 lines below, and appending here would have been a NameError on every digest.
    robot_note = None
    if abs(robot_realized - daily_realized) >= 0.01:
        robot_note = ("robot realized %s today · all-money %s (the difference is your manual "
                      "entries; the breaker reads all-money)"
                      % (_usd(robot_realized), _usd(daily_realized)))
    if breaker_tripped:
        warns.append("⚠ daily loss breaker TRIPPED for %s — real entries halted until UTC "
                     "midnight (memecoin_daily_loss_trips)" % day)
    elif daily_limit > 0 and abs(daily_realized) > DAILY_LOSS_WARN_FRACTION * daily_limit:
        pct = abs(daily_realized) / daily_limit * 100.0
        if daily_realized < 0:
            severity_red = True
            warns.append("⚠ daily realized %s is %.0f%% of the $%.0f loss limit — breaker "
                         "trips at -$%.0f" % (_usd(daily_realized), pct, daily_limit, daily_limit))
        else:
            warns.append("⚠ daily realized %s exceeds 60%% of the $%.0f limit in magnitude — "
                         "big day, sanity-check fills" % (_usd(daily_realized), daily_limit))

    # --- open positions + reservation age --------------------------------------------------
    open_by_chain = [(r[0] or "?", r[1]) for r in conn.execute(
        "SELECT COALESCE(network_id,'?'), COUNT(*) FROM memecoin_positions "
        "WHERE execution_mode='real' AND is_open=1 GROUP BY 1 ORDER BY 2 DESC")]
    open_real = sum(n for _, n in open_by_chain)
    reservations = [(r[0] or "?", r[1], _parse_iso_epoch(r[2])) for r in conn.execute(
        "SELECT network_id, mint, reserved_at FROM memecoin_pending_entries "
        "WHERE status='reserved'")]
    oldest_res_s = None
    for network_id, mint, res_epoch in reservations:
        if res_epoch is None:
            warns.append("⚠ pending reservation %s/%s… has unparseable reserved_at — age and "
                         "staleness are unknown; inspect/reconcile the reservation"
                         % (network_id, str(mint)[:10]))
            continue
        age = (now_epoch - res_epoch) if res_epoch is not None else None
        if age is not None and (oldest_res_s is None or age > oldest_res_s):
            oldest_res_s = age
        if age is not None and age > STALE_RESERVATION_S:
            warns.append("⚠ pending reservation %s/%s… stuck %s (>10m) — check chain confirm / "
                         "recover_stale_reservations; it holds a coin slot"
                         % (network_id, str(mint)[:10], _mins(age)))

    # --- feed freshness, PER LANE ---------------------------------------------------------
    #
    # THE LANE THIS MEASURED HAS BEEN DEAD SINCE 2026-09-09 (fixed 2026-09-18). It read only
    # `global_feed`, and the fomo account ban moved every funded signal onto the J7 relay as
    # `follow_activity`. The 06:36Z digest therefore reported "last feed event: 12408m ago" while
    # the relay was delivering and an entry had just filled. A lane that no longer runs must not
    # decide whether the feed is alive, and a silent live lane must still raise the peak-hours
    # alarm — so the freshness is the NEWEST event of any lane, and the lanes are named.
    # SQLite returns the timestamp from the row that supplies the single MAX aggregate.
    lane_rows = conn.execute(
        "SELECT COALESCE(signal_source,'?'), "
        "       COALESCE(received_at_normalized, received_at), "
        "       MAX(julianday(COALESCE(received_at_normalized, received_at))) "
        "FROM memecoin_events "
        "WHERE julianday(COALESCE(received_at_normalized, received_at)) <= julianday(?) "
        "GROUP BY COALESCE(signal_source,'?')",
        (now_iso,),
    ).fetchall()
    lane_ages = {}
    for source, last_seen, _last_julian in lane_rows:
        epoch = _parse_iso_epoch(last_seen) if last_seen else None
        if epoch is not None:
            lane_ages[str(source)] = now_epoch - epoch
    events_age_s = min(lane_ages.values()) if lane_ages else None
    events_lane = (min(lane_ages, key=lane_ages.get) if lane_ages else None)
    # A lane that was alive within the last day and has now gone quiet is worth naming; one that
    # was retired days ago is not, and repeating its age every hour is how this line went unread.
    quiet_lanes = sorted(
        (source, age) for source, age in lane_ages.items()
        if source != events_lane and FEED_GAP_WARN_S < age <= 86400.0)
    in_peak = _in_peak_hours(now_epoch)
    if in_peak and (events_age_s is None or events_age_s > FEED_GAP_WARN_S):
        warns.append("⚠ no feed events on ANY lane for %s during the 17:00-03:00 UTC peak — check "
                     "the J7 relay + data/memecoin_runner.heartbeat + runner log"
                     % (_mins(events_age_s) if events_age_s is not None else "ever"))
    # A LANE THAT WAS DELIVERING TODAY AND STOPPED IS A REGRESSION, and a fresh sibling lane must
    # not hide it (the 2026-08 rule this keeps). A lane silent for days is retired rather than
    # broken: `global_feed` has delivered nothing since 2026-09-09 and warning hourly about it is
    # what made this whole line unread.
    if in_peak:
        for source, age in quiet_lanes:
            warns.append("⚠ %s quiet for %s during the 17:00-03:00 UTC peak while %s still "
                         "delivers — check that lane's auth and the runner log"
                         % (source, _mins(age), events_lane or "no lane"))

    # --- GT/marks health --------------------------------------------------------------------
    marks_by_chain = {r[0]: r[1] for r in conn.execute(
        "SELECT chain_id, COUNT(*) FROM memecoin_position_marks WHERE julianday(sweep_ts) "
        "BETWEEN julianday(?) AND julianday(?) GROUP BY chain_id", (hour_ago_iso, now_iso))}
    marks_total = sum(marks_by_chain.values())
    for chain, _n in open_by_chain:
        if chain != "?" and not marks_by_chain.get(chain):
            warns.append("⚠ 0 price marks in the last hour for chain %s with an open real "
                         "position — exits are blind there; check GT/DexScreener sources, API "
                         "keys, rate limits" % chain)

    # --- ladder cohort progress (closed real positions with a 'micro' override) ------------
    ladder = {"nano": 0, "micro": 0, "mid": 0}
    for r in conn.execute(
            "SELECT p.entry_mcap FROM memecoin_positions p "
            "WHERE p.execution_mode='real' AND p.is_open=0 "
            "AND julianday(p.closed_at) <= julianday(?) "
            "AND EXISTS (SELECT 1 FROM memecoin_position_exit_overrides o "
            "WHERE o.position_id=p.id AND o.source='micro')", (now_iso,)):
        band = _ladder_band(r[0])
        if band in ladder:
            ladder[band] += 1

    severity = "red" if severity_red else ("yellow" if warns else "green")
    return {
        "severity": severity, "now_iso": now_iso, "since_iso": since_iso,
        "period_s": period_s, "warns": warns, "robot_note": robot_note,
        "entries": entries, "exits": exits, "exit_pnl": exit_pnl,
        "refusals": refusals, "failed_attempts": failed_attempts,
        "daily_realized": daily_realized, "daily_limit": daily_limit,
        "breaker_tripped": breaker_tripped,
        "open_real": open_real, "open_by_chain": open_by_chain,
        "reservations": len(reservations), "oldest_reservation_s": oldest_res_s,
        "events_age_s": events_age_s, "in_peak": in_peak,
        "events_lane": events_lane, "quiet_lanes": quiet_lanes,
        "marks_total": marks_total, "marks_by_chain": marks_by_chain,
        "ladder": ladder,
    }


def format_digest(d):
    """Data dict -> one compact Telegram message. First line = severity emoji + window."""
    lines = ["%s MEMECOIN DIGEST — last %s (%s UTC)"
             % (_SEVERITY_EMOJI.get(d["severity"], YELLOW), _mins(d["period_s"]),
                d["now_iso"][11:16])]
    for w in d["warns"]:
        lines.append(w)
    if d.get("robot_note"):
        lines.append(d["robot_note"])

    entries, exits = d["entries"], d["exits"]
    if entries:
        lines.append("real entries: %d" % len(entries))
        for e in entries[:_MAX_LIST]:
            lines.append("  + %s %s @%s" % (e["ticker"] or str(e["mint"])[:10],
                                            _usd(e["entry_notional"]).lstrip("+"),
                                            e["network_id"] or "?"))
    else:
        lines.append("real entries: 0")
    if exits:
        lines.append("real exits: %d · PnL %s" % (len(exits), _usd(d["exit_pnl"])))
        for x in exits[:_MAX_LIST]:
            lines.append("  - %s %s (%s) @%s"
                         % (x["ticker"] or str(x["mint"])[:10],
                            _usd(x["realized_pnl"]), x["exit_reason"] or "?",
                            x["network_id"] or "?"))
    else:
        lines.append("real exits: 0")

    if d["refusals"]:
        lines.append("refusals: " + " · ".join("%s ×%d" % (r, n) for r, n in d["refusals"]))
    if d["failed_attempts"]:
        for status, reason, n in d["failed_attempts"][:_MAX_LIST]:
            lines.append("  ! %s/%s ×%d" % (status, reason, n))

    if d["daily_limit"] > 0:
        lines.append("daily real PnL: %s / $%.0f breaker"
                     % (_usd(d["daily_realized"]), d["daily_limit"]))
    else:
        lines.append("daily real PnL: %s / breaker OFF" % _usd(d["daily_realized"]))

    chain_str = (" (%s)" % ", ".join("%s:%d" % (c, n) for c, n in d["open_by_chain"])
                 if d["open_by_chain"] else "")
    if not d["reservations"]:
        res_str = "none"
    elif d["oldest_reservation_s"] is None:
        res_str = "%d (oldest age unknown)" % d["reservations"]
    else:
        res_str = "%d (oldest %s)" % (
            d["reservations"], _mins(d["oldest_reservation_s"]))
    lines.append("open real: %d%s · reservations: %s" % (d["open_real"], chain_str, res_str))

    lane_note = " (%s)" % d["events_lane"] if d.get("events_lane") else ""
    quiet_note = "".join(" · %s quiet %s" % (source, _mins(age))
                         for source, age in (d.get("quiet_lanes") or []))
    lines.append("last feed event: %s ago%s%s%s"
                 % (_mins(d["events_age_s"]) if d["events_age_s"] is not None else "none",
                    lane_note, quiet_note,
                    " · peak hours" if d["in_peak"] else ""))

    marks_str = (" (%s)" % ", ".join("%s:%d" % (c, n)
                                     for c, n in sorted(d["marks_by_chain"].items()))
                 if d["marks_by_chain"] else "")
    lines.append("marks last hour: %d%s" % (d["marks_total"], marks_str))

    lad = d["ladder"]
    lines.append("ladder closes: nano %d/%d · micro %d/%d · mid %d/%d"
                 % (lad["nano"], LADDER_READ_GATE, lad["micro"], LADDER_READ_GATE,
                    lad["mid"], LADDER_READ_GATE))

    msg = "\n".join(lines)
    if len(msg) > _MAX_MESSAGE_CHARS:
        msg = msg[:_MAX_MESSAGE_CHARS - 2] + " …"
    return msg


def digest_pass(
        conn, *, period_s=None, now_epoch=None, since_epoch=None, enqueue_fn=None):
    """Assemble and enqueue one digest without performing Telegram HTTP in this thread."""
    try:
        period_s = float(period_s if period_s is not None
                         else cfg.MEMECOIN_TG_DIGEST_PERIOD_S)
        data = collect_digest(
            conn, now_epoch=now_epoch, period_s=period_s, since_epoch=since_epoch)
        message = format_digest(data)
    except Exception:
        logger.exception("memecoin digest collection failed (fail-soft)")
        return {
            "collected": False, "queued": False, "ok": False,
            "severity": None, "message": None,
        }
    try:
        if enqueue_fn is None:
            from sniper.services.memecoin_telegram import enqueue_telegram as enqueue_fn
        queued = bool(enqueue_fn(message))
        return {
            "collected": True, "queued": queued, "ok": queued,
            "severity": data["severity"], "message": message,
        }
    except Exception:
        logger.exception("memecoin digest enqueue failed (fail-soft)")
        return {
            "collected": True, "queued": False, "ok": False,
            "severity": data["severity"], "message": message,
        }


def run_digest_worker(
        *, connection_factory, period_s, clock_fn=real_utc_now,
        monotonic_fn=time.monotonic, sleep_fn=time.sleep, digest_fn=digest_pass,
        log_fn=None, max_runs=None):
    """Run gap-free digest windows on fixed monotonic deadlines.

    The connection is created only after the first sleep and therefore inside the dedicated
    worker thread. A failed collection or queue submission keeps the old window start so the next
    successful digest covers the missed interval instead of silently dropping it. ``max_runs`` is
    a deterministic test bound; production leaves it unset.
    """
    period_s = float(period_s)
    if not math.isfinite(period_s) or period_s <= 0:
        return False
    if max_runs is not None and int(max_runs) <= 0:
        return True

    def report(message):
        if log_fn is not None:
            log_fn(message)

    conn = None
    window_start = float(clock_fn())
    deadline = float(monotonic_fn()) + period_s
    runs = 0
    while max_runs is None or runs < int(max_runs):
        sleep_fn(max(0.0, deadline - float(monotonic_fn())))
        window_end = float(clock_fn())
        try:
            if conn is None:
                conn = connection_factory()
            result = digest_fn(
                conn, period_s=period_s, since_epoch=window_start, now_epoch=window_end)
            if result.get("queued"):
                window_start = window_end
            if result.get("collected") is False:
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None
            report("[memecoin] digest: severity=%s queued=%s"
                   % (result.get("severity"), result.get("queued")))
        except Exception as exc:
            if conn is not None and getattr(conn, "in_transaction", False):
                conn.rollback()
            report("[err] operator digest worker failed: %s" % exc)
        runs += 1
        deadline += period_s
        now_mono = float(monotonic_fn())
        if deadline <= now_mono:
            deadline += (math.floor((now_mono - deadline) / period_s) + 1) * period_s
    return True
