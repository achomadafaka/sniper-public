#!/usr/bin/env python3
"""GPT blind second JUDGE, phase 2: the forward SHADOW seat (Gamma pre-reg 654669397 + A1, frozen read from
UTC 2026-10-25). Measurement only: never trades, never vetoes, never read by the entry path.

Every real JUDGE model call (desk_judge_calls state='responded', desk DB read-only) is answered ONCE more, after the
fact, by gpt-5.6-luna through `codex exec` under the CHECKER's bwrap jail (same jail, same per-call CODEX_HOME,
guarded auth write-back only, any tool item in the `--json` stream aborts and pauses the seat). The prompt is the live
playbook + the exact sheet the live call saw (re-rendered from its frozen inputs; the live verdict is never in it).
Rows go to ITS OWN file, data/desk/gpt_judge.db, with the rendered sheet text and its sha256 so the forward
read audits the exact bytes. Free-text guard: a sheet with any non-allowlisted string is voided (no call).

Pause rules (the seat stops and pages, restart refuses until --resume): any tool event; a REJECTED auth write-back (the
jail's copy of the login came back as another account or with other fields); or, once a UTC day has
>= MIN_DAY_CALLS rows, a call-error + guard-void rate above 2%. Three consecutive CLI failures back off 15 min.
Knob MEMECOIN_DESK_GPT_JUDGE_ENABLED (default 0: exits at once). One instance (flock).

    python3 scripts/memecoin_desk_gpt_judge_shadow.py [--since 2026-10-25T00:00:00] [--once] [--resume]
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MIN_DAY_CALLS = 20
MAX_ERROR_RATE = 0.02
BACKOFF_S = 900.0
CLI_FAIL_STOP = 3
DDL = (
    "CREATE TABLE IF NOT EXISTS gpt_judge(call_id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL, call_started_at TEXT NOT NULL, "
    "day TEXT NOT NULL, live_prompt_version TEXT, playbook TEXT, model TEXT NOT NULL, live_text TEXT, sheet_sha256 TEXT, "
    "sheet_source TEXT, sheet_matches_stored INTEGER, auth_writeback TEXT, playbook_sha256 TEXT, "
    "sheet_text TEXT, gpt_text TEXT, parse_ok INTEGER, valid INTEGER, skip_reason TEXT, call_error TEXT, "
    "guard_void TEXT, tool_events INTEGER, wall_ms REAL, usage_json TEXT, created_at REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS seat_state(k TEXT PRIMARY KEY, v TEXT NOT NULL)",
)


def _replay():
    spec = importlib.util.spec_from_file_location("judge_gpt_replay", ROOT / "scripts/research/judge_gpt_replay.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def connect(path):
    conn = sqlite3.connect(str(path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    for stmt in DDL:
        conn.execute(stmt)
    return conn


def paused(conn):
    row = conn.execute("SELECT v FROM seat_state WHERE k='paused'").fetchone()
    return row[0] if row else None


def pause(conn, reason, page, now=time.time):
    conn.execute("INSERT OR REPLACE INTO seat_state VALUES('paused', ?)", (reason,))
    conn.commit()
    page("GPT judge shadow seat PAUSED: %s" % reason)


NO_CALL_ERRORS = ("playbook_version_mismatch", "codex_busy", "render_%")      # rows that made no GPT call (codex_busy: the slot was held by the CHECKER or the daily report)


def day_rate(conn, day):
    """(attempted, bad, rate) for the UTC day. Attempted = rows that reached the guard or the model (a version
    mismatch or a render failure made no call and is outside BOTH counts, so a playbook bump cannot pause the
    seat); bad = a real call fault (exit_*, timeout, tool_event, ...) or a guard void."""
    where = ("day=? AND NOT (call_error IN ('playbook_version_mismatch', 'codex_busy') OR call_error LIKE 'render_%')")
    n = conn.execute("SELECT COUNT(*) FROM gpt_judge WHERE " + where, (day,)).fetchone()[0]
    bad = conn.execute("SELECT COUNT(*) FROM gpt_judge WHERE " + where + " AND (call_error IS NOT NULL OR guard_void IS NOT NULL)",
                       (day,)).fetchone()[0]
    return n, bad, (bad / n if n else None)


def auth_age_days(auth_path=None, now=time.time):
    """Days since the REAL login's last_refresh (None when unknown): a stale refresh token means the login will lapse."""
    path = Path(auth_path or Path.home() / ".codex" / "auth.json")
    try:
        stamp = json.loads(path.read_text()).get("last_refresh")
        return (now() - datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()) / 86400.0
    except (OSError, ValueError, TypeError):
        return None


def page_auth_age(conn, page, *, now=time.time, auth_path=None):
    """One page per UTC day when the real login's last_refresh is older than 6 days."""
    age = auth_age_days(auth_path, now)
    day = datetime.fromtimestamp(now(), timezone.utc).strftime("%Y-%m-%d")
    done = conn.execute("SELECT v FROM seat_state WHERE k='auth_age_page_day'").fetchone()
    if age is not None and age > 6 and (not done or done[0] != day):
        conn.execute("INSERT OR REPLACE INTO seat_state VALUES('auth_age_page_day', ?)", (day,))
        conn.commit()
        page("GPT judge shadow: codex login last_refresh is %.1f days old (> 6); refresh it before it lapses" % age)
        return True
    return False


def pending(desk_db, since, done):
    c = sqlite3.connect("file:%s?mode=ro" % desk_db, uri=True, timeout=5)
    try:
        rows = c.execute("SELECT call_id, ticket_id, call_started_at, result_json FROM desk_judge_calls "
                         "WHERE state='responded' AND call_started_at >= ? ORDER BY call_started_at", (since,)).fetchall()
        out = []
        for call_id, ticket_id, started, rj in rows:
            if call_id in done:
                continue
            # the vet row's prompt_version is the COHORT's frozen version; the judge worker refuses a call when the
            # cohort version != the playbook it loaded (prompt_version_mismatch preflight), so on a RESPONDED call it
            # is the version of the playbook the call ran under
            pv = c.execute("SELECT prompt_version FROM desk_tickets WHERE ticket_id=? AND stage='vet'", (ticket_id,)).fetchone()
            out.append((call_id, ticket_id, started, rj, pv[0] if pv else None))
        return out
    finally:
        c.close()


def _stored_sheet(sha, zb64):
    """The live call's stored exact sheet (Beta 4d0ad66b7: sheet_sha256 + sheet_zb64 = base64(zlib(text))) or None
    when absent / undecodable / the hash does not match its bytes (a corrupt store is never trusted)."""
    if not sha or not zb64:
        return None
    try:
        import base64
        import zlib
        text = zlib.decompress(base64.b64decode(zb64)).decode()
    except Exception:                                           # noqa: BLE001
        return None
    return text if hashlib.sha256(text.encode()).hexdigest() == sha else None


def answer_one(R, W, conn, desk_db, row, playbook, version, bounds, caps, *, call, page, now=time.time):
    """Answer one JUDGE call and store it. Returns 'ok' | 'tool_event' | 'cli_fail' | 'skipped'."""
    call_id, ticket_id, started, rj, live_pv = row
    result = json.loads(rj or "{}")
    base = {"call_id": call_id, "ticket_id": ticket_id, "call_started_at": started, "day": str(started)[:10],
            "live_prompt_version": live_pv, "playbook": version, "model": R.MODEL, "live_text": result.get("text"),
            "guard_void": None, "call_error": None, "tool_events": 0, "created_at": now(), "sheet_source": None,
            "playbook_sha256": hashlib.sha256(playbook.encode()).hexdigest(),   # the exact system-prompt bytes sent
            "sheet_matches_stored": None, "auth_writeback": None}
    if not result.get("text") or not result.get("asof"):
        return "skipped"                                        # a call with no frozen inputs: nothing to replay
    if live_pv is not None and version is not None and live_pv != version:
        base["call_error"] = "playbook_version_mismatch"        # the live call ran another playbook: not comparable
        sheet = None
    else:
        # Beta 4d0ad66b7 stores the exact user string INSIDE result_json (no columns): sheet_sha256 + sheet_zb64
        stored = _stored_sheet(result.get("sheet_sha256"), result.get("sheet_zb64"))
        try:
            rendered = R.sheet_of(result)
        except Exception as exc:                                # noqa: BLE001 - recorded, never fatal
            rendered = None
            if stored is None:
                base["call_error"] = "render_" + type(exc).__name__
        if stored is not None:                                  # the exact bytes the live call saw
            sheet, base["sheet_source"] = stored, "stored"
            base["sheet_matches_stored"] = None if rendered is None else int(rendered == stored)
        else:
            sheet, base["sheet_source"] = rendered, ("rerender" if rendered is not None else None)
    status = "ok"
    if sheet is not None:
        base["sheet_sha256"], base["sheet_text"] = hashlib.sha256(sheet.encode()).hexdigest(), sheet
        bad = R.free_text_guard(sheet)
        if bad is not None:
            base["guard_void"] = bad
            page("GPT judge shadow: sheet %s voided by the free-text guard (%s); no call" % (call_id[:14], bad))
        else:
            text, err, wall, meta = call(W, playbook + "\n\nFact sheet:\n" + sheet)
            base.update(gpt_text=text, call_error=err, wall_ms=wall, tool_events=meta["tool_events"],
                        usage_json=json.dumps(meta["usage"]) if meta["usage"] else None, auth_writeback=meta.get("auth"))
            if str(meta.get("auth") or "").startswith("rejected:") and meta["auth"] != "rejected:unreadable":
                status = "auth_rejected"
            if err is None:
                ok, valid, reason = R.grade(text, sheet, bounds, caps)
                base.update(parse_ok=int(ok), valid=int(valid), skip_reason=reason)
            elif err == "tool_event":
                status = "tool_event"
            elif err.startswith("exit_"):
                status = "cli_fail"
    cols = ["call_id", "ticket_id", "call_started_at", "day", "live_prompt_version", "playbook", "model", "live_text",
            "sheet_sha256", "sheet_text", "sheet_source", "sheet_matches_stored", "auth_writeback", "playbook_sha256", "gpt_text", "parse_ok", "valid", "skip_reason", "call_error", "guard_void",
            "tool_events", "wall_ms", "usage_json", "created_at"]
    conn.execute("INSERT OR IGNORE INTO gpt_judge(%s) VALUES(%s)" % (",".join(cols), ",".join("?" * len(cols))),
                 [base.get(c) for c in cols])
    conn.commit()
    return status


def run(desk_db, out_db, since, *, once=False, resume=False, call=None, page=None, sleep=time.sleep, now=time.time,
        auth_path=None):
    R = _replay()
    W = R._checker()
    playbook, version = R.load_playbook()
    bounds, caps = R.cohort_limits(desk_db)
    conn = connect(out_db)
    page = page or R._page
    call = call or (lambda W_, prompt: R.codex_json(W_, prompt, page=page))
    if resume:
        conn.execute("DELETE FROM seat_state WHERE k='paused'")
        conn.commit()
    if paused(conn):
        print("[gpt-judge-shadow] PAUSED (%s); restart with --resume after reading the cause" % paused(conn))
        return 3
    streak = 0
    while True:
        page_auth_age(conn, page, now=now, auth_path=auth_path)
        done = {r[0] for r in conn.execute("SELECT call_id FROM gpt_judge")}
        for row in pending(desk_db, since, done):
            status = answer_one(R, W, conn, desk_db, row, playbook, version, bounds, caps, call=call, page=page, now=now)
            if status == "tool_event":
                pause(conn, "tool event in the codex stream on %s" % row[0][:14], page)
                return 3
            if status == "auth_rejected":
                pause(conn, "guarded auth write-back REJECTED on %s: the jail's login copy is not the same login" % row[0][:14], page)
                return 3
            streak = streak + 1 if status == "cli_fail" else 0
            n, bad, rate = day_rate(conn, str(row[2])[:10])
            if n >= MIN_DAY_CALLS and rate > MAX_ERROR_RATE:
                pause(conn, "error/guard-void rate %.1f%% over %d calls on %s" % (rate * 100, n, str(row[2])[:10]), page)
                return 3
            if streak >= CLI_FAIL_STOP:
                streak = 0
                if once:
                    return 0
                sleep(BACKOFF_S)
            sleep(1)
        if once:
            return 0
        sleep(5)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--desk-db", default=None)
    p.add_argument("--out", default=None)
    p.add_argument("--since", default="2026-10-25T00:00:00")
    p.add_argument("--once", action="store_true")
    p.add_argument("--resume", action="store_true")
    a = p.parse_args(argv)
    from sniper.util.state_paths import require_state_root_outside_release, state_path, state_root
    require_state_root_outside_release()
    from sniper.util.envfiles import load_env_files
    load_env_files(str(state_root()))
    if os.getenv("MEMECOIN_DESK_GPT_JUDGE_ENABLED", "0").strip() != "1":
        print("[gpt-judge-shadow] MEMECOIN_DESK_GPT_JUDGE_ENABLED is not 1; exiting")
        return 0
    desk_db = a.desk_db or str(state_path("data", "memecoin_desk.db"))
    out_db = Path(a.out or state_path("data", "desk", "gpt_judge.db"))
    out_db.parent.mkdir(parents=True, exist_ok=True)
    lock = open(out_db.with_suffix(".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[gpt-judge-shadow] another instance holds the lock; exiting")
        return 0
    return run(desk_db, out_db, a.since, once=a.once, resume=a.resume)


if __name__ == "__main__":
    sys.exit(main())
