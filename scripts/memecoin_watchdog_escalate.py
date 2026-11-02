#!/usr/bin/env python3
"""Outage-escalation state machine for the memecoin watchdog (2026-07-21).

The watchdog runs every minute; before this, a persistent failure paged an identical,
reason-less Telegram line each minute (26 pages during the 07-21 funded outage) and the
recovery page carried no outage context. This helper decides WHEN to page and WHAT to say:

  failure  --state F --reason TEXT [--repeat-s 900]   -> page on the FIRST failure, then one
           escalation page per repeat window while still down (downtime + attempt count + the
           actual error). Silent otherwise.
  recovery --state F --detail TEXT                    -> one page iff a failure streak was
           active (attempts + downtime), then clears the state. Silent on the healthy path.
  unsent   --state F                                  -> the page `failure` just returned was NOT
           delivered: rewind the alert clock so the next tick pages again (2026-09-29: the state
           advanced before delivery, so an undelivered page waited a whole repeat window).

Prints the page text to stdout when a page is due, nothing otherwise; the bash watchdog does
`msg=$(... ) && [ -n "$msg" ] && alert "$msg"`. Always exits 0 on the decision path — a broken
state file must degrade to "page like a first failure", never to silence.

Timestamps are HOST epoch (interval math only, one clock — never mixed with corrected UTC).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time


def _load(path):
    try:
        with open(path) as fh:
            state = json.load(fh)
        return {"count": int(state["count"]), "first_at": float(state["first_at"]),
                "last_alert_at": float(state["last_alert_at"])}
    except Exception:
        return None


def _save(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh)
    os.replace(tmp, path)


def _mins(seconds):
    return "%dm" % max(1, round(seconds / 60.0))


def decide_failure(state, *, now, reason, repeat_s):
    """Returns (message_or_empty, new_state)."""
    reason = (reason or "").strip() or "(no reason captured)"
    if state is None:
        return ("memecoin runner DOWN — launch failed: %s" % reason,
                {"count": 1, "first_at": now, "last_alert_at": now})
    state = dict(state)
    state["count"] += 1
    if now - state["last_alert_at"] >= repeat_s:
        state["last_alert_at"] = now
        return ("memecoin runner STILL DOWN %s (%d failed launches) — last error: %s"
                % (_mins(now - state["first_at"]), state["count"], reason), state)
    return ("", state)


def decide_recovery(state, *, now, detail):
    """Returns message_or_empty; state file is cleared by the caller."""
    if state is None:
        return ""
    return ("memecoin runner RECOVERED (%s) after %d failed launches over %s"
            % ((detail or "").strip() or "relaunched", state["count"],
               _mins(now - state["first_at"])))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("event", choices=("failure", "recovery", "unsent"))
    ap.add_argument("--state", required=True)
    ap.add_argument("--reason", default="")
    ap.add_argument("--detail", default="")
    ap.add_argument("--repeat-s", type=float, default=900.0)
    ap.add_argument("--now", type=float, default=None, help="epoch override for tests")
    args = ap.parse_args(argv)
    now = time.time() if args.now is None else args.now

    state = _load(args.state)
    if args.event == "unsent":
        if state is not None:
            try:
                _save(args.state, dict(state, last_alert_at=0.0))
            except Exception:
                pass
        return 0
    if args.event == "failure":
        msg, new_state = decide_failure(state, now=now, reason=args.reason,
                                        repeat_s=args.repeat_s)
        try:
            _save(args.state, new_state)
        except Exception:
            pass   # paging matters more than persistence
        if msg:
            print(msg)
    else:
        msg = decide_recovery(state, now=now, detail=args.detail)
        try:
            if os.path.exists(args.state):
                os.remove(args.state)
        except Exception:
            pass
        if msg:
            print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
