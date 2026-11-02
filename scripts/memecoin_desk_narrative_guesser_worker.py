#!/usr/bin/env python3
"""Narrative guesser shadow worker (spec docs/superpowers/specs/2026-10-27-desk-narrative-guesser-design.md). nice 19 / ionice idle, single instance (flock).
    nice -n 19 ionice -c3 python3 scripts/memecoin_desk_narrative_guesser_worker.py [--state-dir data/desk] [--once]
Writes <state-dir>/narrative_guess/narrative_guess.db and worker.heartbeat.json. SHADOW ONLY: no watch-list preload, no desk read. The J7 baseline and the intake matcher are free code;
the model arm calls GPT-6 Luna LOW only while MEMECOIN_DESK_NARRATIVE_GUESSER_MODEL=1 in the funded .env (default OFF; re-read every cycle, so 0 stops the spend within one cycle)."""
import argparse
import fcntl
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper.services import memecoin_desk_narrative_guess_store as ST  # noqa: E402
from sniper.services import memecoin_desk_narrative_guesser as G  # noqa: E402
from sniper.util.clock import real_utc_now  # noqa: E402

ENV_PATH = "/home/bigpapi/Desktop/Sniper/.env"
CYCLE_S = 5.0


def real_call_model():
    """The isolated GPT subscription adapter, imported only when the model arm is on."""
    from sniper.services.memecoin_desk_gpt import call_model
    return call_model


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-dir", default=str(ROOT / "data" / "desk"))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--no-model", action="store_true", help="J7-only launcher: the model arm is forced OFF whatever the .env knob says (the GPT adapter is never imported)")
    ap.add_argument("--env-file", default=ENV_PATH, help="where the MEMECOIN_DESK_NARRATIVE_GUESSER_MODEL kill knob is read (tests point it at a scratch file)")
    a = ap.parse_args()
    base = Path(a.state_dir) / "narrative_guess"
    base.mkdir(parents=True, exist_ok=True)
    lock = open(base / "worker.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 75
    con = ST.connect(str(base / "narrative_guess.db"))
    call = None
    while True:
        now = real_utc_now()
        enabled = (not a.no_model) and G.model_enabled(a.env_file)
        if enabled and call is None:
            call = real_call_model()
        try:
            out = G.run_cycle(con, a.state_dir, now, call_model=call, enabled=enabled, clock=real_utc_now)
            err = None
        except Exception as exc:                                 # a cycle failure is a heartbeat field, never a crash loop of writes
            out, err = {}, "%s: %s" % (type(exc).__name__, str(exc)[:200])
            print(json.dumps({"cycle_error": err}), file=sys.stderr, flush=True)
        tmp = base / "worker.heartbeat.json.tmp"
        tmp.write_text(json.dumps({"ts": time.time(), "pid": os.getpid(), "model_enabled": enabled, "last": out, "error": err}, sort_keys=True))
        os.replace(tmp, base / "worker.heartbeat.json")
        if a.once:
            print(json.dumps(out, sort_keys=True))
            return 0
        time.sleep(CYCLE_S)


if __name__ == "__main__":
    sys.exit(main())
