"""PumpSwap keep-window extension file (Alpha ruling 2026-10-30): a one-way list of pools whose tape the capture worker should keep longer.

A writer outside the worker (scripts/memecoin_desk_keep_extend_writer.py) lists the pools of hard_filter PASS tickets and of open Office orders with an `until`;
the worker reads the list and EXTENDS: ps_keep.keep_until = MAX(keep_until, until), keep_from untouched. Everything here is in the worker's host
receipt clock (time.time()); the writer runs on the same host and uses the same clock, nothing is converted. A missing, stale, oversized or malformed file
changes nothing.

Shortening: file deletion, a shorter request and ordinary rollback NEVER shorten a window. Only a pressure shed does (80 percent of the PS disk cap or under 100 GB
free): it returns each extended window to max(newest ordinary baseline, now), logged and counted, so extensions can never be what stops capture.

Change note: the extra stored tape can indirectly change mark and entry availability for extended pools (keep_from, and so retained_from, is unchanged; more rows
exist to read). Policy, rails and entry code are unchanged. Design and evidence: docs/superpowers/specs/2026-10-30-pumpswap-keep-extension-design.md.
Pure functions only: no database, no worker."""
import json
import math
import os
import re
import time

FILE_NAME = "keep_extend.json"
VERSION = 1
MAX_BYTES = 1 << 20
MAX_POOLS = 2000
FRESH_S = 300.0              # written_at older than this: the writer is down, so the list is not current
FUTURE_S = 60.0              # written_at ahead of the worker clock by more than this: clock disagreement, unknown
MAX_AHEAD_S = 7 * 3600.0     # an until further out than this is not a keep request
SPAN_S = 3700.0              # PASS / open-order tape kept through +3700 s (max hold 3600 s plus margin)
SHED_FREE_GB = 100.0         # host free space under this sheds extensions (Alpha 2026-10-30)
_POOL = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def _num(x):
    return type(x) in (int, float) and math.isfinite(x)


def read(path, now):
    """-> {state: 'ok'|'unavailable', reason, pools: {pool: until}, dropped: n}. Never raises: an unreadable list is unavailable, the worker then changes nothing."""
    def no(reason):
        return dict(state="unavailable", reason=reason, pools={}, dropped=0)
    if not _num(now):
        return no("clock_unknown")
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return no("missing")
    except OSError:
        return no("unreadable")
    if st.st_size > MAX_BYTES:
        return no("oversized")
    try:
        with open(path, "rb") as f:
            doc = json.loads(f.read(MAX_BYTES + 1))
    except (OSError, ValueError):
        return no("unreadable")
    if not isinstance(doc, dict) or doc.get("version") != VERSION or not isinstance(doc.get("pools"), dict):
        return no("malformed")
    written = doc.get("written_at")
    if not _num(written):
        return no("malformed")
    if written > now + FUTURE_S:
        return no("clock_disagreement")
    if now - written > FRESH_S:
        return no("stale")
    pools, dropped = {}, 0
    for pool, until in doc["pools"].items():
        if (len(pools) >= MAX_POOLS or not isinstance(pool, str) or not _POOL.match(pool) or not _num(until)
                or until <= now or until > now + MAX_AHEAD_S):
            dropped += 1
            continue
        pools[pool] = float(until)
    return dict(state="ok", reason=None, pools=pools, dropped=dropped)


class Warn:
    """One warning per cause per hour."""
    def __init__(self, every_s=3600.0):
        self.every_s, self.last = every_s, {}

    def due(self, cause, now):
        if now - self.last.get(cause, -math.inf) >= self.every_s:
            self.last[cause] = now
            return True
        return False


def write(path, pools, now):
    """Atomic write (tmp + rename) of the whole list; the file never grows past the pools given."""
    body = json.dumps(dict(version=VERSION, written_at=now, pools=pools), sort_keys=True, separators=(",", ":"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
