#!/usr/bin/env python3
"""4663 fast lane M0, the sequencer-feed shadow, as its own process (spec
docs/superpowers/specs/2026-10-01-memecoin-4663-fast-lane-design.md §6; Gamma MEDIUM: not inside
the funded runner, so it never shares the runner's GIL).

Read-only: the public feed, a read-only ledger handle for the armed roster, the swap tape read-only,
and its own SQLite file (data/memecoin_rh_feed_shadow.db). No key, no signing, no send. It exits at
once unless MEMECOIN_RH_FAST_LANE=shadow. Launched release-pinned by
scripts/memecoin_rh_feed_shadow_watchdog.sh (cron, once a minute); the run lock below permits one
process; it writes data/memecoin_rh_feed_shadow.heartbeat every 1 s and exits when a thread dies.
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Settings live with the state, not the code: a pinned release has no .env.
from sniper.util.envfiles import load_env_files                     # noqa: E402
from sniper.util.state_paths import state_path, state_root          # noqa: E402

load_env_files(str(state_root()))

from sniper.settings import memecoin as cfg                          # noqa: E402


def ledger_path():
    """DB_PATH absolute as the cron line exports it; a relative one resolves under the state root,
    never against a pinned release's own directory (trap 1c3a9cbb)."""
    raw = os.environ.get("DB_PATH") or "sniper_tracker.db"
    return Path(raw) if Path(raw).is_absolute() else state_path(raw)


# The processed-head readiness gate expires at 2s. Publish off the entry/stream paths.
HEARTBEAT_S = 1.0


def main(*, sleep_fn=time.sleep):
    if cfg.memecoin_rh_fast_lane_mode() not in ("shadow", "runner_shadow", "live"):
        print("[rh-feed-shadow] MEMECOIN_RH_FAST_LANE is not shadow, runner_shadow or live; exiting",
              flush=True)
        return 0
    from sniper.services.memecoin_runner import acquire_run_lock
    lock = acquire_run_lock(str(state_path("data", "memecoin_rh_feed_shadow.lock")))
    if lock is None:
        return 0                      # a healthy shadow holds it: the normal case
    from sniper.services.memecoin_rh_feed_shadow import start_shadow
    shadow = start_shadow(ledger_path=ledger_path(), log=lambda line: print(line, flush=True))
    beat = state_path("data", "memecoin_rh_feed_shadow.heartbeat")
    while True:
        dead = [t.name for t in getattr(shadow, "threads", []) if not t.is_alive()]
        if dead:                      # no heartbeat: the watchdog restarts the process
            print("[rh-feed-shadow] thread(s) %s died; exiting" % dead, flush=True)
            return 1
        try:
            # pid: the runner's lane hears a datagram only from this process (SO_PASSCRED).
            payload = {"ts": time.time(), "pid": os.getpid(),
                       "counters": dict(shadow.counters),
                       "stats": dict(getattr(shadow, "stats", {}))}
            deposit_stream = getattr(shadow, 'deposit_stream_status', None)
            if isinstance(deposit_stream, dict) and deposit_stream.get('provider') == 'shyft':
                stream = {key: deposit_stream.get(key) for key in (
                    'provider', 'hostname', 'commitment', 'connected', 'generation',
                    'acknowledged_generation', 'watch_generation', 'requested_watch_generation',
                    'acknowledged_watch_generation', 'requested_count', 'acknowledged_count',
                    'requested_filter_fingerprint', 'acknowledged_filter_fingerprint',
                    'head_slot', 'head_monotonic', 'last_frame_monotonic', 'queue_overflows',
                    'coverage_gaps', 'last_gap', 'consumer_stale', 'last_error')}
                if stream['hostname'] not in ('grpc.ams.shyft.to', 'grpc.fra.shyft.to'):
                    stream['hostname'] = None
                mono = time.monotonic()
                stream['head_age_s'] = (max(0., mono - stream['head_monotonic'])
                    if stream['head_monotonic'] is not None else None)
                stream['last_frame_age_s'] = (max(0., mono - stream['last_frame_monotonic'])
                    if stream['last_frame_monotonic'] is not None else None)
                payload['deposit'] = {'stream': stream}
            beat.write_text(json.dumps(payload))
        except Exception as exc:
            print("[rh-feed-shadow] heartbeat write failed: %s" % exc, flush=True)
        sleep_fn(HEARTBEAT_S)


if __name__ == "__main__":
    sys.exit(main())
