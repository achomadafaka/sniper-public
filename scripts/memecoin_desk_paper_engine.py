#!/usr/bin/env python3
"""Desk PAPER engine loop (operator 2026-10-26; sniper/services/memecoin_desk_paper.py has the contract).

Every POLL_S: fill pending paper entries (exact native curve buy at the decision + 1.5 s on our tape), then mark and
exit open paper positions on the runner's rails (exact full-quantity sell at the exit + 1.5 s). SOL/USD comes from the
universe worker's heartbeat (its causal FX), never a provider call. Single instance (flock). No signing, ever.

usage: memecoin_desk_paper_engine.py [--loop] [--desk-db PATH]
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

POLL_S = 1.0


def sol_from_heartbeat(desk_dir, now):
    """(sol_usd, sol_at) from the universe worker's heartbeat, or (None, None) when unreadable."""
    path = Path(desk_dir) / "universe.heartbeat"
    try:
        beat = json.loads(path.read_text())
        age = float(beat["sol_age_s"])
        return float(beat["sol_usd"]), path.stat().st_mtime - age
    except (OSError, ValueError, KeyError, TypeError):
        return None, None


FX = None


def run_once(conn, desk_dir, now=None, risk_exits=None, send_fn=None, fx=None):
    from sniper.services import memecoin_desk_paper as paper
    global FX
    now = time.time() if now is None else now
    if fx is None:
        FX = FX or paper.FxHistory()             # one causal FX history per engine process (single-threaded loop)
        fx = FX
    fx.add(*sol_from_heartbeat(desk_dir, now))
    paper.ensure(conn)
    out = paper.fill_pending(conn, desk_dir=desk_dir, now=now, fx_at=fx.at)
    if risk_exits is None:                     # the PAPER RISK watcher's decisions (scripts/memecoin_desk_risk_watcher.py --paper)
        risk_exits = paper.risk_orders(conn, desk_dir=desk_dir, now=now)
    out += paper.tick(conn, desk_dir=desk_dir, now=now, risk_exits=risk_exits, fx_at=fx.at)
    if send_fn is not None:
        def judge_code(ticket_id):
            row = conn.execute("SELECT json_extract(output_json,'$.result.reason_code') FROM desk_tickets WHERE "
                               "ticket_id=? AND stage='vet'", (ticket_id,)).fetchone()
            return row[0] if row else None
        out += [{"step": "card", "ticket_id": t, "card": k}
                for t, k in paper.send_cards(conn, send_fn=send_fn, judge_code_fn=judge_code, now_iso=paper._iso(now))]
    return out


def publish_status(desk_dir, paper, *, started_at, now, engine_error):
    from sniper.util.atomic import atomic_write_json
    atomic_write_json(Path(desk_dir)/'desk_paper_engine.status',dict(
        version='desk-paper-engine-status-v1',ts_host=now,started_at_host=started_at,
        run_id=getattr(paper,'RUN_ID',None),
        mark_log_errors=getattr(paper,'MARK_LOG_ERRORS',{}).get('mark_log_errors'),
        engine_error=engine_error),mode=0o600)


def main(argv=None):
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_root
    load_env_files(str(state_root()))
    p = argparse.ArgumentParser()
    p.add_argument("--loop", action="store_true")
    p.add_argument("--desk-db", default=str(state_root() / "data" / "memecoin_desk.db"))
    a = p.parse_args(argv)
    desk_dir = Path(a.desk_db).parent / "desk"
    lock = open(Path(a.desk_db).with_name("desk_paper_engine.lock"), "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(json.dumps({"step": "start", "refused": "already_running"}), flush=True)
        return 0
    conn = sqlite3.connect(a.desk_db, timeout=30)
    conn.row_factory = sqlite3.Row
    log = Path(a.desk_db).parent.parent / "logs" / "memecoin_desk_paper.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    from sniper.services.memecoin_telegram import send_telegram_audited
    from sniper.services import memecoin_desk_paper as paper
    started_at=time.time()
    while True:
        engine_error=False
        try:
            for row in run_once(conn, desk_dir, send_fn=send_telegram_audited):
                with open(log, "a") as fh:
                    fh.write(json.dumps({"ts": time.time(), **row}, default=str) + "\n")
        except Exception as exc:                       # named in the log; the next pass retries
            engine_error=True
            with open(log, "a") as fh:
                fh.write(json.dumps({"ts": time.time(), "step": "error", "error": "%s: %s" % (type(exc).__name__, exc)}) + "\n")
        finally:
            try:
                publish_status(desk_dir,paper,started_at=started_at,now=time.time(),engine_error=engine_error)
            except Exception as exc:
                print(json.dumps(dict(step='status_error',error='%s: %s' % (type(exc).__name__,exc))),file=sys.stderr,flush=True)
        if not a.loop:
            return 0
        time.sleep(POLL_S)


if __name__ == "__main__":
    raise SystemExit(main())
