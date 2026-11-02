"""Read-only Flask API for the LLM desk (Desk tab). Display only: every handle is read-only (the
funded ledger through get_readonly_connection, the desk DB and the ranker/checker files with
mode=ro), nothing here writes, and nothing is on the money path. With every worker switched off it
still answers, and says "not running".

Outcomes are shown per cohort, never pooled, under the standing no-tuning rule (Gamma).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, jsonify

from sniper.persistence.db_core import get_readonly_connection

NO_TUNING = ("Outcomes are per cohort and never pooled. The running cohort is never tuned on its own "
             "outcomes (H1 10-22..28, H2 10-29..11-04); an adopted change ships as a new cohort.")
STALE_S = 120.0
# (label, script argv[1] basename, the file whose freshness shows it working, relative to data/desk)
WORKERS = (
    ("U producer", "memecoin_desk_universe_worker.py", "universe.heartbeat"),
    ("X producer", "memecoin_desk_x_producer.py", "x/producer.heartbeat"),
    ("crawler", "memecoin_desk_crawler.py", None),
    ("cluster", "memecoin_desk_cluster_worker.py", "cluster.status.json"),
    ("ranker", "memecoin_desk_ranker_worker.py", "ranker/status.json"),
    ("assembler", "memecoin_desk_assembler.py", None),
    ("JUDGE", "memecoin_desk_judge_worker.py", None),
    ("RISK shadow", "memecoin_desk_risk_shadow.py", None),
    ("offchain collector", "memecoin_desk_offchain.py", None),
    # Lambda's feeds: their freshness is in the Feeds panel (day files), so process state only here
    ("trending", "memecoin_desk_trending.py", None),
    ("DexScreener", "memecoin_desk_dexscreener.py", None),
    ("narrative", "memecoin_desk_narrative.py", None),
)


def _ro(path):
    path = Path(path)
    if not path.is_file():
        return None
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    return conn


def _running(proc_root="/proc"):
    """Script basenames with a live process (argv[1]), read from /proc; never a pattern kill."""
    found = set()
    for d in Path(proc_root).glob("[0-9]*"):
        try:
            argv = (d / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if len(argv) > 1 and os.path.basename(argv[0].decode(errors="replace")).startswith("python"):
            found.add(os.path.basename(argv[1].decode(errors="replace")))
    return found


def workers(desk_dir, *, now=None, proc_root="/proc"):
    now = time.time() if now is None else now
    live, out = _running(proc_root), []
    for label, script, beat in WORKERS:
        age = None
        if beat:
            p = Path(desk_dir) / beat
            try:
                age = round(now - p.stat().st_mtime, 1)
            except OSError:
                age = None
        running = script in live
        state = ("not running" if not running else
                 "stale" if age is not None and age > STALE_S else "running")
        out.append({"worker": label, "script": script, "state": state, "heartbeat_age_s": age})
    return out


def cohorts(ledger_db, *, today):
    from sniper.persistence import db_memecoin as m
    from sniper.settings import memecoin as cfg
    if not ledger_db or not Path(ledger_db).is_file():
        return {"available": False, "reason": "ledger_missing"}
    conn = get_readonly_connection(str(ledger_db))
    try:
        try:
            rows = m.desk_cohort_dashboard_rows(conn)
        except sqlite3.Error:
            rows = []
        per = []
        for r in rows:
            acct = m.desk_cohort_accounting(conn, r["cohort_id"])
            open_n = conn.execute("SELECT COUNT(*) FROM memecoin_positions WHERE desk_cohort_id=? AND "
                                  "execution_mode='real' AND signal_source='desk' AND is_open=1",
                                  (r["cohort_id"],)).fetchone()[0]
            per.append({"cohort_id": r["cohort_id"], "producer": r["producer"], "prompt_version": r["prompt_version"],
                        "created_at": r["created_at"], "open": open_n,
                        "kill_limit_usd": None if r['stop_mode']=='manual' else -float(r["kill_limit_usd"]),
                        "stop_mode": r['stop_mode'], "admission_enabled": bool(r['admission_enabled']),
                        "known_net_usd": round(acct["known_net_usd"], 2), "net_usd": acct["net_usd"],
                        "unknown_costs": acct["unknown_costs"], "unpriced_closes": acct["unpriced_closes"],
                        "killed": m.desk_cohort_killed(conn, r["cohort_id"])})
        return {"available": True, "cohorts": per,
                "desk": {"open": m.desk_open_count(conn), "max_open": int(cfg.MEMECOIN_DESK_MAX_CONCURRENT or 0),
                         "daily_realized_usd": round(m.desk_realized_pnl_since(conn, today + "T00:00:00+00:00"), 2),
                         "daily_halt_usd": -float(cfg.MEMECOIN_DESK_DAILY_LOSS_USD or 0),
                         "daily_latched": m.desk_loss_latched(conn, today)}}
    finally:
        conn.close()


def tickets_today(desk_db, *, today, cap_per_hour, now_iso):
    conn = _ro(desk_db)
    if conn is None:
        return {"available": False, "reason": "desk_db_missing"}
    since = today + "T00:00:00+00:00"
    try:
        emitted = conn.execute("SELECT COUNT(*) FROM desk_candidate_emissions WHERE emitted_at >= ?",
                               (since,)).fetchone()[0]
        stages = {}
        for stage, verdict, reason, n in conn.execute(
                "SELECT stage, verdict, reason, COUNT(*) FROM desk_tickets WHERE ts >= ? "
                "GROUP BY stage, verdict, reason ORDER BY COUNT(*) DESC", (since,)):
            stages.setdefault(stage, []).append({"verdict": verdict, "reason": reason, "n": n})
        hour_ago = datetime.fromtimestamp(datetime.fromisoformat(now_iso).timestamp() - 3600,
                                          timezone.utc).isoformat()
        from sniper.persistence.db_memecoin_desk import CALL_SPENT_SQL
        calls_hour = conn.execute("SELECT COUNT(*) FROM desk_judge_calls c WHERE c.claimed_at>=? AND " + CALL_SPENT_SQL,
                                  (hour_ago,)).fetchone()[0]
    except sqlite3.Error as exc:
        if "no such table" in str(exc):          # the file exists but no cohort was ever provisioned
            return {"available": False, "reason": "desk_not_provisioned"}
        return {"available": False, "reason": "desk_db_unreadable", "detail": str(exc)[:120]}
    finally:
        conn.close()
    return {"available": True, "emitted": emitted, "stages": stages,
            "model_calls_last_hour": calls_hour, "seat_cap_per_hour": cap_per_hour}


def shortlisted_today(desk_dir, *, today):
    conn = _ro(Path(desk_dir) / "ranker" / "candidates.db")
    if conn is None:
        return None
    t0 = datetime.fromisoformat(today + "T00:00:00+00:00").timestamp()
    try:
        row = conn.execute("SELECT COUNT(*), SUM(first_shortlisted_at IS NOT NULL) FROM candidates "
                           "WHERE first_rank_at >= ?", (t0,)).fetchone()
        return {"ranked": row[0], "shortlisted": row[1] or 0}
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def reports(desk_dir):
    pm = sorted((Path(desk_dir) / "postmortem").glob("*.md")) if (Path(desk_dir) / "postmortem").is_dir() else []
    checker = None
    conn = _ro(Path(desk_dir) / "checker.db")
    if conn is not None:
        try:
            row = conn.execute("SELECT COUNT(*), MAX(day) FROM checks").fetchone()
            checker = {"checks": row[0], "latest_day": row[1]}
        except sqlite3.Error:
            checker = None
        finally:
            conn.close()
    return {"latest_postmortem": pm[-1].stem if pm else None, "checker": checker}


def copy_lane(ledger_db, *, now, days=7):
    """Funded closes (execution_mode='real') in the last `days` days, grouped by exit reason, chain and
    signal source, plus per-chain fills that carry no network fee. A close with realized_pnl NULL is
    counted as unpriced, never as 0."""
    if not ledger_db or not Path(ledger_db).is_file():
        return {"available": False, "reason": "ledger_missing"}
    since = datetime.fromtimestamp(now.timestamp() - days * 86400, timezone.utc).isoformat()
    conn = get_readonly_connection(str(ledger_db))
    try:
        from sniper.persistence.db_memecoin import fee_missing_sql
        def grouped(col):
            return [{"key": r[0], "closes": r[1], "wins": r[2] or 0, "unpriced": r[3] or 0,
                     "realized_usd": round(r[4] or 0.0, 2)} for r in conn.execute(
                "SELECT COALESCE(%s, 'unknown'), COUNT(*), SUM(realized_pnl > 0), SUM(realized_pnl IS NULL), "
                "SUM(realized_pnl) FROM memecoin_positions WHERE execution_mode='real' AND is_open=0 "
                "AND closed_at >= ? GROUP BY 1 ORDER BY SUM(realized_pnl)" % col, (since,))]
        fees = [{"chain": r[0], "kind": r[1], "fills": r[2], "no_fee": r[3] or 0} for r in conn.execute(
            "SELECT COALESCE(p.network_id, 'unknown'), CASE WHEN f.kind='entry' THEN 'entry' ELSE 'exit' END, "
            "COUNT(*), SUM(%s) "
            "FROM memecoin_fills f JOIN memecoin_positions p ON p.id = f.position_id "
            "WHERE p.execution_mode='real' AND p.is_open=0 AND p.closed_at >= ? GROUP BY 1, 2 ORDER BY 1, 2"
            % fee_missing_sql(conn), (since,))]
        return {"available": True, "since": since, "by_exit_reason": grouped("exit_reason"),
                "by_chain": grouped("network_id"), "by_signal_source": grouped("signal_source"),
                "fills_fee": fees}
    finally:
        conn.close()


def paper_positions(desk_db, *, limit=50):
    """PAPER desk positions (operator 10-26): display only, never pooled with real ones."""
    conn = _ro(desk_db)
    if conn is None:
        return {"available": False, "reason": "desk_db_missing"}
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='desk_paper_positions'").fetchone():
            return {"available": False, "reason": "paper_engine_never_ran"}
        rows = [dict(r) for r in conn.execute(
            "SELECT ticket_id, mint, state, requested_at, filled_at, closed_at, size_usd, entry_price_usd, last_mark_usd, "
            "exit_reason, pnl_usd, pnl_pct, refusal FROM desk_paper_positions ORDER BY requested_at DESC LIMIT ?", (limit,))]
        totals = dict(conn.execute("SELECT COUNT(*) AS closes, COALESCE(SUM(pnl_usd),0) AS pnl_usd FROM desk_paper_positions "
                                   "WHERE state='closed'").fetchone())
        return {"available": True, "paper": True, "positions": rows, "closed": totals}
    except sqlite3.Error as exc:
        return {"available": False, "reason": "desk_db_unreadable", "detail": str(exc)[:120]}
    finally:
        conn.close()


def create_api_memecoin_desk_blueprint(ctx):
    bp = Blueprint("api_memecoin_desk", __name__)

    @bp.get("/api/memecoin/desk/status")
    def desk_status():
        now = datetime.now(timezone.utc)
        today = now.date().isoformat()
        desk_dir = Path(ctx["desk_dir"])
        cap = int(os.getenv("MEMECOIN_DESK_SEAT_CALLS_PER_HOUR", "0") or 0)
        body = {"display_only": True, "no_tuning": NO_TUNING, "as_of": now.isoformat(),
                "workers": workers(desk_dir),
                "cohorts": cohorts(ctx["ledger_db"], today=today),
                "tickets": tickets_today(ctx["desk_db"], today=today, cap_per_hour=cap, now_iso=now.isoformat()),
                "ranker": shortlisted_today(desk_dir, today=today),
                "reports": reports(desk_dir)}
        return jsonify(json.loads(json.dumps(body, default=str)))

    @bp.get("/api/memecoin/desk/paper")
    def desk_paper():
        body = paper_positions(ctx["desk_db"])
        return jsonify(json.loads(json.dumps({"display_only": True, **body}, default=str)))

    @bp.get("/api/memecoin/desk/copy-lane")
    def desk_copy_lane():
        body = copy_lane(ctx["ledger_db"], now=datetime.now(timezone.utc))
        return jsonify(json.loads(json.dumps({"display_only": True, **body}, default=str)))

    @bp.get("/api/memecoin/desk/feeds")
    def desk_feeds():
        from sniper.routes.desk_feeds_view import feeds
        return jsonify(json.loads(json.dumps(feeds(ctx["desk_dir"]), default=str)))

    @bp.get("/api/memecoin/desk/postmortem/<day>")
    def desk_postmortem(day):
        if len(day) != 10 or not day.replace("-", "").isdigit():
            return jsonify({"error": "day must be YYYY-MM-DD"}), 400
        path = Path(ctx["desk_dir"]) / "postmortem" / ("%s.md" % day)
        if not path.is_file():
            return jsonify({"available": False, "reason": "no_report"}), 404
        return jsonify({"display_only": True, "day": day, "markdown": path.read_text()})

    return bp
