#!/usr/bin/env python3
"""Daily desk report + per-trade GPT review (operator order 2026-10-23 via Alpha). REPORT ONLY, read-only on every source
(ledger, desk DB, checker.db, gpt_judge.db, risk_live.db, the PumpSwap tape's gaps), no ledger write, nothing on the money
path, nothing here feeds a decision. For one UTC day (default: the previous one) it writes data/desk/reports/<day>.md and
sends ONE short Telegram summary (counts and enums only, never model text). Closed desk positions of the day get a per-trade
review from GPT through the CHECKER's codex jail (free-text guard on the sheet, tool-event abort, one codex call at a time,
guarded login write-back); a review that fails is a line saying so, never a hole in the page.

    python3 scripts/memecoin_desk_daily_report.py [--day YYYY-MM-DD] [--no-review] [--no-alert] [--max-reviews 20]
Cron (operator installs; cron runs local time, EDT now): the report needs the UTC day to be over, so run it after 00:30Z. Local
21:05 daily is 01:05Z in EDT and 02:05Z in EST, after 00:30Z on both sides of the November DST change, and "the previous UTC
day" is computed from the clock, so it is right in both:
  5 21 * * * cd /home/bigpapi/Desktop/Sniper/.runtime/memecoin/current && SNIPER_STATE_ROOT=/home/bigpapi/Desktop/Sniper /home/bigpapi/.pyenv/versions/3.13.0/bin/python3 scripts/memecoin_desk_daily_report.py >> /home/bigpapi/Desktop/Sniper/logs/memecoin_desk_daily_report.log 2>&1
"""
from __future__ import annotations

import argparse
import fcntl
import glob
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sniper.services import memecoin_desk_report as REP            # noqa: E402
from sniper.services import memecoin_desk_trade_review as TR       # noqa: E402


def _ro(path):
    p = Path(path)
    if not p.is_file():
        return None
    try:
        c = sqlite3.connect("file:%s?mode=ro" % p, uri=True, timeout=10)
        c.row_factory = sqlite3.Row
        return c
    except sqlite3.Error:
        return None


def _replay():
    spec = importlib.util.spec_from_file_location("judge_gpt_replay", ROOT / "scripts/research/judge_gpt_replay.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def previous_utc_day(now=None):
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=1)).strftime("%Y-%m-%d")


def sheet_for(desk, ticket_id, R):
    """The JUDGE sheet text of the ticket's newest responded call: the stored exact bytes when their hash holds, else a
    re-render from the frozen inputs; None when neither exists."""
    rows = REP._q(desk, "SELECT result_json FROM desk_judge_calls WHERE ticket_id=? AND state='responded' ORDER BY call_started_at DESC",
                  (ticket_id,))
    for (rj,) in map(tuple, rows or []):
        try:
            res = json.loads(rj or "{}")
        except ValueError:
            continue
        import base64, hashlib, zlib
        try:
            text = zlib.decompress(base64.b64decode(res["sheet_zb64"])).decode()
            if hashlib.sha256(text.encode()).hexdigest() == res.get("sheet_sha256"):
                return text
        except Exception:                                           # noqa: BLE001 - fall through to the re-render
            pass
        try:
            text = R.sheet_of(res)
            if text:
                return text
        except Exception:                                           # noqa: BLE001
            continue
    return None


def review_one(pos, *, ledger, desk, risk, R, W, call, page, lessons=None, stats=None):
    """(review dict | None, note): the GPT review of one closed desk position."""
    row = REP._q(desk, "SELECT ticket_id FROM desk_tickets WHERE position_id=? ORDER BY id LIMIT 1", (pos["id"],))
    if not row:
        return None, "no desk ticket for the position"
    sheet_text = sheet_for(desk, tuple(row[0])[0], R)
    if sheet_text is None:
        return None, "no stored sheet"
    bad = R.free_text_guard(sheet_text)
    if bad is not None:
        page("daily report review: sheet of pos %s voided by the free-text guard (%s); no call" % (pos["id"], bad))
        return None, "sheet voided by the free-text guard"
    risk_rows = []
    if risk is not None:
        t0 = datetime.fromisoformat(pos["opened_at"]).timestamp()
        t_exit = datetime.fromisoformat(pos["closed_at"]).timestamp() if pos.get("closed_at") else None
        # only decisions up to the exit are part of the hold; each is joined to the funded runner's apply ack (never credit or blame RISK
        # for a recommendation that was refused, expired, superseded or never read)
        for r in REP._q(risk, "SELECT decided_at, action, reason_code, reason, rails_json, pnl_pct_exact, id FROM risk_decisions WHERE position_id=? "
                              "AND live=1 AND decided_at IS NOT NULL ORDER BY decided_at", (pos["id"],)) or []:
            r = tuple(r)
            if t_exit is not None and r[0] > t_exit:
                continue
            ack = REP._q(ledger, "SELECT status, lag_s FROM memecoin_desk_risk_applies WHERE source='risk_live' AND decision_id=?", (r[6],)) or []
            ack = tuple(ack[0]) if ack else (None, None)
            risk_rows.append({"t_s": r[0] - t0, "action": r[1], "reason_code": r[2], "reason": r[3], "rails_json": r[4], "pnl_pct_exact": r[5],
                              "apply_status": ack[0], "apply_lag_s": ack[1]})
    if stats is not None:
        for k, v in TR.risk_apply_summary(risk_rows).items():
            stats[k] = stats.get(k, 0) + v
    from sniper.persistence import db_memecoin as m
    chain_id = m.coin_identity(pos.get("network_id"), pos["mint"])[0] if pos.get("network_id") else None
    marks = REP._q(ledger, "SELECT sweep_ts, mid FROM memecoin_position_marks WHERE chain_id=? AND mint=? AND sweep_ts >= ? AND "
                           "COALESCE(sweep_ts, '') <= ? AND COALESCE(quality,'') != 'quarantined_spike' ORDER BY sweep_ts",
                   (chain_id, pos["mint"], pos["opened_at"], pos["closed_at"] or "9999")) or []
    facts = TR.build_facts(pos, json.loads(sheet_text), risk_rows, [tuple(x) for x in marks], lessons=(lessons or {}).get("pos:%s" % pos["id"]))
    text, err, _wall, meta = call(W, TR.build_prompt(facts), schema_obj=TR.REVIEW_SCHEMA, page=page)
    if err is not None:
        return None, "call_error " + err
    review = TR.sanitize_review(text)
    return (review, None) if review else (None, "answer is not the review schema")


REVIEW_GAP_S = 2.0       # the codex slot is an unfair flock: pause between reviews so the GPT shadow seat and the CHECKER get a turn


def run(day, *, ledger, desk, checker, gpt, risk, gaps, deploy_logs, out_dir, reviews=True, max_reviews=20, alert=None,
        call=None, page=None, R=None, W=None, sleep=None, lessons_db=None):
    rep = REP.collect(day, ledger=ledger, desk=desk, checker=checker, gpt=gpt, risk=risk, gaps=gaps, deploy_logs=deploy_logs)
    lines, tags, done = [], {}, 0
    risk_stats = {}
    lessons, digest_lines = {}, []
    if lessons_db and os.getenv("MEMECOIN_DESK_LESSONS_DIGEST", "1").strip() != "0":
        from sniper.services import memecoin_desk_lessons_digest as DG
        try:
            all_rows = DG.load(lessons_db, strict=True)
            display_rows = DG.load(lessons_db, strict=True, latest_per_key=True)
            lessons = {r["key"]: r for r in all_rows}
            t_lo, t_hi = REP.day_bounds(day)[2:4]
            day_rows = [r for r in all_rows if DG.report_t(r) is not None and (t_lo is None or t_lo <= DG.report_t(r) < t_hi)]
            # decision cohorts / prompt versions are never pooled: one digest per cohort key, the day first, then all-time
            for title, rows_ in ((day, day_rows), ("All-time (every stored unit)", all_rows)):
                for ck, group in DG.split_by_cohort(rows_).items():
                    digest_lines += DG.render_markdown(DG.digest(group), "%s | %s" % (title, ck)) + [""]
            digest_lines += DG.render_learning_loop(display_rows)
        except (OSError, sqlite3.Error, RuntimeError, ValueError, TypeError, AttributeError, OverflowError) as exc:
            lessons = {}
            digest_lines = ["## Desk learning loop", "", "_unavailable: lessons_source_error:%s_" % type(exc).__name__,
                            "_descriptive; no rule change from a digest alone_", ""]
    page = page or (lambda text: print("[daily-report] " + text, file=sys.stderr))
    if reviews and isinstance(rep["positions"], dict) and desk is not None and ledger is not None:
        R = R or _replay()
        W = W or R._checker()
        call = call or R.codex_json
        closed = [p for p in rep["positions"]["positions"] if p["closed_today"]]
        import time as _time
        sleep = sleep or _time.sleep
        for i, pos in enumerate(closed[:max_reviews]):
            if i:
                sleep(REVIEW_GAP_S)
            try:
                review, note = review_one(pos, ledger=ledger, desk=desk, risk=risk, R=R, W=W, call=call, page=page, lessons=lessons, stats=risk_stats)
            except Exception as exc:                                # noqa: BLE001 - a bad review never costs the page
                review, note = None, "error " + type(exc).__name__
            lines += TR.render_review(pos, review, note)
            if review:
                tags[review["tag"]] = tags.get(review["tag"], 0) + 1
            done += 1
        if len(closed) > max_reviews:
            lines.append("_%d more closed positions not reviewed (cap %d)._" % (len(closed) - max_reviews, max_reviews))
        if not closed:
            lines.append("_No closed desk position this day._")
        if risk_stats.get("actionable"):
            lines.append("_RISK recommendations before the exit (reviewed trades): %(actionable)d actionable, %(applied)d applied, %(late)d late "
                         "(lag over the %(ttl)ss order TTL), %(unapplied)d with no applied ack. RISK is credited or blamed only for applied ones._"
                         % {**risk_stats, "ttl": TR.ORDER_TTL_S})
    md = REP.render_markdown(rep, lines if reviews else "reviews disabled")
    if digest_lines:
        md += "\n" + "\n".join(digest_lines) + "\n"
    out = Path(out_dir) / ("%s.md" % day)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=out.parent, prefix=".%s." % day, suffix=".tmp", delete=False) as fh:
        fh.write(md)
        tmp = fh.name
    os.replace(tmp, out)
    msg = REP.render_telegram(rep, tags=tags or None, path=str(out))
    if alert is not None:
        try:
            alert(msg)
        except Exception:                                            # noqa: BLE001 - the page is written; the alert may fail alone
            print("[daily-report] alert failed", file=sys.stderr)
    return {"path": str(out), "telegram": msg, "reviewed": done, "tags": tags, "report": rep}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--day", default=None)
    p.add_argument("--out-dir", default=None)
    p.add_argument("--no-review", action="store_true")
    p.add_argument("--no-alert", action="store_true")
    p.add_argument("--max-reviews", type=int, default=int(os.getenv("MEMECOIN_DESK_DAILY_REPORT_MAX_REVIEWS", "20")))
    a = p.parse_args(argv)
    from sniper.util.state_paths import require_state_root_outside_release, state_path, state_root
    require_state_root_outside_release()
    from sniper.util.envfiles import load_env_files
    load_env_files(str(state_root()))
    if os.getenv("MEMECOIN_DESK_DAILY_REPORT_ENABLED", "1").strip().lower() in ("0", "false", "off", "no"):
        print("[daily-report] MEMECOIN_DESK_DAILY_REPORT_ENABLED=0; exiting")
        return 0
    day = a.day or previous_utc_day()
    out_dir = Path(a.out_dir or state_path("data", "desk", "reports"))
    out_dir.mkdir(parents=True, exist_ok=True)
    lock = open(out_dir / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[daily-report] another run holds the lock; exiting")
        return 0
    from sniper.persistence.db_core import get_readonly_connection
    db_env = os.getenv("DB_PATH") or "sniper_tracker.db"
    ledger_path = db_env if os.path.isabs(db_env) else str(state_path(db_env))      # .env says a relative name; cron's cwd is a release
    ledger = None
    if Path(ledger_path).is_file():
        ledger = get_readonly_connection(ledger_path)
        ledger.row_factory = sqlite3.Row
    desk = _ro(state_path("data", "memecoin_desk.db"))
    alert = None
    if not a.no_alert:
        from sniper.services.memecoin_alerts import send_alert
        alert = lambda text: send_alert("DESK-DAILY-REPORT", text, icon="\U0001F4CA")
    res = run(day, ledger=ledger, desk=desk, checker=_ro(state_path("data", "desk", "checker.db")),
              gpt=_ro(state_path("data", "desk", "gpt_judge.db")), risk=_ro(state_path("data", "desk", "risk_live.db")),
              gaps=_ro(state_path("data", "desk", "pumpswap", "tape.db")),
              deploy_logs=glob.glob(str(state_path(".runtime", "memecoin", "releases")) + "/*"), out_dir=out_dir,
              reviews=not a.no_review, max_reviews=a.max_reviews, alert=alert, lessons_db=str(state_path("data", "desk", "lessons.db")))
    print(json.dumps({"day": day, "path": res["path"], "reviewed": res["reviewed"], "tags": res["tags"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
