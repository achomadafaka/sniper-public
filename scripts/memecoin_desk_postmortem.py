#!/usr/bin/env python3
"""Desk POST-MORTEM seat worker (sniper/services/memecoin_desk_postmortem.py). Daily offline batch,
never on the fill path: reads the desk DB and the funded ledger read-only, makes at most one Sonnet 5.5
call through the JUDGE's Agent SDK adapter (subscription, no API key), and writes
data/desk/postmortem/<day>.md (the text proposals) and <day>.json (facts + call proof). It writes no
config and no ticket. A day already reported is not re-asked unless --force. No ticket that day: no
model call. MEMECOIN_DESK_POSTMORTEM_ENABLED=0 stops it.

    python3 scripts/memecoin_desk_postmortem.py [--day YYYY-MM-DD]      # default: yesterday UTC
    # cron (operator), after midnight UTC:
    # 30 0 * * * cd /home/bigpapi/Desktop/Sniper/.runtime/memecoin/current && SNIPER_STATE_ROOT=/home/bigpapi/Desktop/Sniper /home/bigpapi/.pyenv/versions/3.13.0/bin/python3 scripts/memecoin_desk_postmortem.py >> /home/bigpapi/Desktop/Sniper/logs/memecoin_desk_postmortem.log 2>&1
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sniper.services import memecoin_desk_postmortem as PM     # noqa: E402


def facts_for_day(desk_db, ledger_db, day):
    from sniper.persistence import db_memecoin as ledger
    from sniper.persistence import db_memecoin_desk as store
    from sniper.persistence.db_core import get_readonly_connection
    since = "%sT00:00:00+00:00" % day
    until = (datetime.fromisoformat(since) + timedelta(days=1)).isoformat()
    desk = sqlite3.connect("file:%s?mode=ro" % desk_db, uri=True, timeout=5)
    desk.row_factory = sqlite3.Row
    led = get_readonly_connection(str(ledger_db))
    try:
        ids = set()
        for stage in ("close", "fill", "vet"):                # the day's closes, refusals and SKIPs
            ids |= set(store.list_tickets_with_stage(desk, stage=stage, since_iso=since, until_iso=until))
        facts = []
        for tid in sorted(ids):
            t = store.get_ticket(desk, tid)
            if t["state"] in ("open", "waiting", "queued", "unresolved", "filled"):
                continue                                         # not terminal yet: a later day grades it
            close = next((s for s in t["stages"] if s["stage"] == "close"), None)
            pid = (close or {}).get("position_id") or ((close or {}).get("inputs") or {}).get("position_id")
            position = ledger.get_position(led, pid) if pid else None
            facts.append(PM.ticket_facts(t, dict(position) if position is not None else None))
        return facts
    finally:
        desk.close()
        led.close()


def run(desk_db, ledger_db, out_dir, day, *, call_model, force=False):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if (out_dir / ("%s.json" % day)).exists() and not force:
        return {"day": day, "status": "already_reported"}
    facts = facts_for_day(desk_db, ledger_db, day)
    if not facts:
        result, reason = {}, "no desk ticket closed this day"
    else:
        result = call_model(PM.SYSTEM_PROMPT, PM.user_message(day, facts))
        reason = PM.call_usable(result)
    (out_dir / ("%s.md" % day)).write_text(PM.report_markdown(day, facts, result, reason))
    record = {"day": day, "prompt_version": PM.PROMPT_VERSION, "model": PM.MODEL, "summary": PM.summary(facts),
              "facts": facts, "call": {k: v for k, v in result.items() if k != "text"}, "unusable": reason}
    (out_dir / ("%s.json" % day)).write_text(json.dumps(record, sort_keys=True, indent=1, default=str))
    return {"day": day, "status": "reported", "tickets": len(facts), "unusable": reason}


def main(argv=None):
    from sniper.util.state_paths import require_state_root_outside_release
    require_state_root_outside_release()   # never a state root inside .runtime/ (Gamma 2026-10-21)
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_root, state_path
    load_env_files(str(state_root()))
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):     # the subscription seat, never a key
        os.environ.pop(key, None)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--day", default=(datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat())
    p.add_argument("--desk-db", default=str(state_path("data", "memecoin_desk.db")))
    p.add_argument("--ledger-db", default=os.getenv("DB_PATH") or str(state_path("sniper_tracker.db")))
    p.add_argument("--force", action="store_true", help="re-ask a day already reported")
    a = p.parse_args(argv)
    if os.getenv("MEMECOIN_DESK_POSTMORTEM_ENABLED", "1").strip() == "0":
        print("[desk-postmortem] MEMECOIN_DESK_POSTMORTEM_ENABLED=0; exiting")
        return 0
    if not Path(a.desk_db).is_file() or not Path(a.ledger_db).is_file():
        print("[desk-postmortem] refused: desk or ledger DB missing")
        return 2
    out_dir = Path(state_root()) / "data" / "desk" / "postmortem"
    out_dir.mkdir(parents=True, exist_ok=True)
    lock = open(out_dir / "postmortem.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[desk-postmortem] another post-mortem holds the lock; exiting")
        return 0
    sys.path.insert(0, str(ROOT / "scripts"))
    from memecoin_desk_judge_worker import sdk_call_model

    def call_model(system, user):
        return sdk_call_model(system, user, deadline_s=PM.DEADLINE_S, model=PM.MODEL, effort="medium", persist_session=False)
    print(json.dumps(run(a.desk_db, a.ledger_db, out_dir, a.day, call_model=call_model, force=a.force)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
