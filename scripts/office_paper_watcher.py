#!/usr/bin/env python3
"""Office deterministic paper watcher (Task 7 unit, Beta's seat since 2026-10-28; draft by Delta): runs
sniper.office.ledger.manage on a fixed cadence with ONE clock_offset.json sample per tick, outside office.slice, so
open paper positions keep being valued and exited while agents are paused. No network beyond the local tape, no funded
ledger capability. Unknown clock = no tick (clock_unknown); unknown FX is passed as None and the ledger refuses."""
import argparse
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


log = logging.getLogger("office.paper_watcher")


def write_atomic(path, doc):
    """Heartbeat write: temp file in the same directory, fsync, rename."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(doc, f, sort_keys=True, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def sandbox_refusal(status_text):
    """None when this process runs as the bwrap wrapper leaves it (NoNewPrivs 1, no effective capabilities), else the
    reason. The unit starts bwrap with systemd NoNewPrivileges=no (bwrap needs it to build namespaces); this check
    proves the payload itself is locked down."""
    fields = dict(line.split(":", 1) for line in status_text.splitlines() if ":" in line)
    if fields.get("NoNewPrivs", "").strip() != "1":
        return "no_new_privs_not_set"
    try:
        if int(fields.get("CapEff", "").strip(), 16) != 0:
            return "effective_capabilities_present"
    except ValueError:
        return "capabilities_unreadable"
    return None


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def read_fx(desk_dir, *, now_host, offset):
    """(sol_usd, sol_at corrected UTC) from a fresh connected universe heartbeat, else (None, None)."""
    try:
        beat = json.loads((Path(desk_dir) / 'universe.heartbeat').read_text())
        at, age, rate = beat.get('ts'), beat.get('sol_age_s'), beat.get('sol_usd')
        if (beat.get('connected') is True and all(finite(v) for v in (at, age, rate))
                and 0 <= now_host - at <= 5. and age >= 0 and 0 < rate < 1e9
                and age + now_host - at <= 120.):
            return rate, at - age - offset
    except (OSError, ValueError, TypeError, AttributeError):
        pass  # Unknown FX is passed explicitly; ledger refuses unprovable fills.
    return None, None


def tick(conn, *, desk_dir, clock_file, now_host, manage, mark_every_s=15.0):
    from sniper.services.memecoin_desk_intake import clock_sample
    offset, sampled_at = clock_sample(clock_file, now=now_host)
    if offset is None:
        return {"state": "clock_unknown", "ts_host": now_host, "through_utc": None}
    now = now_host - offset
    frozen = {"offset_s": offset, "host_utc": now_host, "corrected_utc": now,
              "sampled_at": sampled_at}
    fx, fx_at = read_fx(desk_dir, now_host=now_host, offset=offset)
    try:
        result = manage(conn, None, desk_dir=Path(desk_dir), now=now, sol_usd=fx, sol_at=fx_at,
                        clock_sample=frozen, mark_min_interval_s=mark_every_s)
    except Exception as exc:                  # loud: a typed heartbeat AND a traceback, never a silent empty tick
        log.exception("office paper watcher: ledger.manage failed")
        return {"state": "paper_error", "error": type(exc).__name__, "ts_host": now_host, "through_utc": None}
    return {"state": "managed", "ts_host": now_host, "through_utc": now,
            "fx_state": "known" if fx is not None else "unknown",
            "clock_sample": frozen, "result": None if result is None else result.__dict__}


def main(argv=None):
    from sniper.office import ledger
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger', type=Path, required=True)
    parser.add_argument('--desk-dir', type=Path, required=True)
    parser.add_argument('--clock-file', type=Path, required=True)
    parser.add_argument('--heartbeat', type=Path, required=True)
    parser.add_argument('--period-s', type=float, default=.5)
    parser.add_argument('--expect-epoch',help='refuse a ledger holding any other epoch: a v2 unit can never manage an old book')
    parser.add_argument('--no-sandbox-check', action='store_true', help='tests only')
    parser.add_argument('--mark-every-s', type=float, default=15.0,
                        help='re-mark an open position at most this often (pilot 15 s, Alpha); fills and due exits are '
                             'never throttled')
    args = parser.parse_args(argv)
    if not args.no_sandbox_check:
        why = sandbox_refusal(Path("/proc/self/status").read_text())
        if why:
            raise SystemExit("office paper watcher refuses to run outside its sandbox: %s" % why)
    if not finite(args.period_s) or not .1 <= args.period_s <= 2.:
        parser.error('period must be in [.1,2] seconds')
    if not finite(args.mark_every_s) or not 0 < args.mark_every_s <= 300.:
        parser.error('--mark-every-s must be a finite number in (0, 300] seconds')
    args.heartbeat.parent.mkdir(parents=True, exist_ok=True)
    with (args.heartbeat.parent / 'paper-watcher.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Never create an empty book because a deployment used the wrong path.
        if not args.ledger.is_file():
            raise SystemExit('office_ledger_missing')
        conn = ledger.open_ledger(args.ledger)
        if args.expect_epoch:
            # open_ledger also bootstraps a pilot-floor book in any new file. What matters: the evaluated book is this epoch's and no other epoch has an order to manage.
            evaluated = {r[0] for r in conn.execute("SELECT epoch_id FROM books WHERE kind='evaluated'")}
            ordered = {r[0] for r in conn.execute('SELECT DISTINCT epoch_id FROM orders')}
            if evaluated != {args.expect_epoch} or ordered - {args.expect_epoch}:
                raise SystemExit('office_ledger_epoch_mismatch')
        last_good = None
        try:
            previous = json.loads(args.heartbeat.read_text())
            last_good = previous.get('last_good_utc')
        except (OSError, ValueError, AttributeError):
            pass
        try:
            while True:
                beat = tick(conn, desk_dir=args.desk_dir, clock_file=args.clock_file,
                            now_host=time.time(), manage=ledger.manage, mark_every_s=args.mark_every_s)
                if beat['state'] == 'managed':
                    last_good = beat['through_utc']
                beat['last_good_utc'] = last_good
                write_atomic(args.heartbeat, beat)
                time.sleep(args.period_s)
        finally:
            conn.close()


if __name__ == '__main__':
    main()
