#!/usr/bin/env python3
"""Desk INTAKE phase 1 collector (sniper/services/memecoin_desk_intake.py): COLLECT ONLY, one process per feed.

    python3 scripts/memecoin_desk_intake.py --feed pumpfun --loop
    python3 scripts/memecoin_desk_intake.py --feed dexscreener --loop     # zero HTTP: normalizes the producer DBs

Writes data/desk/intake/<feed>/<UTC day>.db under the state root. One writer per feed (flock). Pump.fun: one GET per
list per pass at MEMECOIN_DESK_INTAKE_PUMPFUN_PASS_S; a refusal backs off along MEMECOIN_DESK_INTAKE_BACKOFF_S and
resets after a clean pass. Kill knob MEMECOIN_DESK_INTAKE_ENABLED=0.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper.util.envfiles import load_env_files                     # noqa: E402
from sniper.util.state_paths import state_root                      # noqa: E402


def run_once(feed, root, *, refusals=0, clock=time.time, get=None):
    """One pass. Returns (stats, refused)."""
    from sniper.services import memecoin_desk_intake as I
    now = clock()
    offset = I.clock_offset(Path(root) / "data" / "clock_offset.json")
    intake_dir = Path(root) / "data" / "desk" / "intake"
    if feed == "pumpfun":
        out = I.open_day(intake_dir, feed, now)
        try:
            return I.pumpfun_pass(out, get=get or I.http_get, clock=clock, offset_s=offset)
        finally:
            out.close()
    # DexScreener: yesterday's producer file too, into YESTERDAY's intake file (its cursor lives there), so the rows
    # written between the last pass and UTC midnight are normalized; an absent yesterday file is no error.
    stats = {}
    for at in (now - 86400, now):
        src = Path(root) / "data" / "desk" / "dexscreener" / ("%s.db" % I.utc_day(at))
        if at < now and not src.exists():
            continue
        out = I.open_day(intake_dir, feed, at)
        try:
            got = I.dexscreener_pass(src, out, now=now, offset_s=offset)
        finally:
            out.close()
        for k, v in got.items():
            stats[k] = v if k == "error" else stats.get(k, 0) + v
    return stats, False


def main(argv=None):
    from sniper.util.state_paths import require_state_root_outside_release
    require_state_root_outside_release()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--feed", choices=("pumpfun", "dexscreener"), required=True)
    p.add_argument("--loop", action="store_true")
    a = p.parse_args(argv)
    root = state_root()
    load_env_files(str(root))
    import sniper.settings.memecoin as cfg
    if not cfg.MEMECOIN_DESK_INTAKE_ENABLED:
        print("[desk-intake] MEMECOIN_DESK_INTAKE_ENABLED=0; exiting")
        return 0
    lock_dir = Path(root) / "data" / "desk" / "intake" / a.feed
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock = open(lock_dir / "collector.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[desk-intake] another %s collector holds the lock; exiting" % a.feed)
        return 0
    pass_s = cfg.MEMECOIN_DESK_INTAKE_PUMPFUN_PASS_S if a.feed == "pumpfun" else cfg.MEMECOIN_DESK_INTAKE_DEXSCREENER_PASS_S
    refusals = 0
    try:
        while True:
            t0 = time.time()
            stats, refused = run_once(a.feed, root)
            refusals = refusals + 1 if refused else 0
            if cfg.MEMECOIN_DESK_INTAKE_PRUNE_ENABLED:     # off until Gamma posts the feed's results doc
                from sniper.services import memecoin_desk_intake as I
                I.prune(lock_dir, now=time.time(), keep_days=cfg.MEMECOIN_DESK_INTAKE_KEEP_DAYS)
            wait = cfg.MEMECOIN_DESK_INTAKE_BACKOFF_S[min(refusals, len(cfg.MEMECOIN_DESK_INTAKE_BACKOFF_S)) - 1] if refused else pass_s
            print(json.dumps({**stats, "feed": a.feed, "refused": refused, "next_s": wait}, sort_keys=True), flush=True)
            if not a.loop:
                return 0
            time.sleep(max(1.0, wait - (time.time() - t0)))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
