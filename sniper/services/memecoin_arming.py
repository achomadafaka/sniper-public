"""Item 18 — arming + kill switch for the real-money memecoin leg.

Two independent gates: MEMECOIN_REAL_ENABLED (the arm flag, default OFF) AND the absence of a
kill-switch file. The kill switch is a FILE (operator- or UI-created) so it can be tripped without
touching env or restarting the process; it is checked every cycle and again immediately before
every submit. Tripping it halts ENTRIES only — the exit sweep keeps running so open real positions
still get their SL/TP/timecap brackets.
"""
from __future__ import annotations

import os
import fcntl
import hashlib
import uuid
from contextlib import contextmanager

from sniper.settings import memecoin as cfg
from sniper.util.state_paths import state_path

def _kill_path(path=None):
    p = path or os.getenv("MEMECOIN_KILL_SWITCH_FILE",
                          str(state_path("data", "memecoin_kill_switch")))
    return p if os.path.isabs(p) else str(state_path(p))


def is_kill_tripped(path=None):
    """True iff the kill-switch file exists. Fail-CLOSED: any error reading it counts as tripped."""
    try:
        return os.path.exists(_kill_path(path))
    except Exception:
        return True


def kill_switch_status(path=None):
    """Read the holder and token from one switch snapshot. Read errors keep entries paused."""
    try:
        with open(_kill_path(path), "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return {"kill_tripped": False, "kill_source": None, "kill_token": None,
                "kill_deploy_held": False}
    except OSError:
        return {"kill_tripped": True, "kill_source": None, "kill_token": None,
                "kill_deploy_held": False}
    first = raw.decode("utf-8", errors="replace").splitlines()
    source = first[0].removeprefix("tripped by ").strip() if first else None
    return {"kill_tripped": True, "kill_source": source or None,
            "kill_token": hashlib.sha256(raw).hexdigest(),
            "kill_deploy_held": "deploy" in (source or "").lower()}


def kill_switch_source(path=None):
    """Return the source label. The label does not identify a person."""
    return kill_switch_status(path)["kill_source"]


@contextmanager
def _kill_lock(path):
    """Serialize all switch writes across threads and processes."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Keep this lock file. Removing it would allow two processes to lock different inodes.
    with open(path + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _audit(action, source, *, token=None):
    """Append the request source and hold token. The source is not a person identity."""
    try:
        import datetime
        audit_path = state_path("logs", "kill_switch_audit.log")
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with open(audit_path, "a") as f:
            f.write("%s %s source=%s token=%s\n" % (
                datetime.datetime.now(datetime.timezone.utc).isoformat(), action, source, token or "-"))
    except Exception:
        pass


def trip_kill_switch(path=None, source="unattributed", *, only_if_clear=False):
    """Return a new hold token, or None if another hold prevents acquisition."""
    if not isinstance(source, str) or not source or len(source) > 512 or any(c in source for c in "\r\n"):
        raise ValueError("The switch source must be one nonempty line, at most 512 characters.")
    p = _kill_path(path)
    with _kill_lock(p):
        if only_if_clear and kill_switch_status(p)["kill_tripped"]:
            return None
        raw = ("tripped by %s\nhold %s\n" % (source, uuid.uuid4().hex)).encode()
        token = hashlib.sha256(raw).hexdigest()
        temporary = p + "." + uuid.uuid4().hex + ".tmp"
        try:
            with open(temporary, "xb") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, p)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        _audit("TRIP", source, token=token)
        return token


# HOLDS TAKEN BY A FUNDS-TRANSFER SCRIPT, keyed by the source prefix each script trips with.
# The script clears its own hold when the credit is proven, and an uncertain transfer keeps it
# until `--resume` proves the credit. The dashboard must not release one: on 2026-09-22
# web:127.0.0.1 cleared arc-to-base's hold 9.5 s after it was taken, with the transfer in flight
# (logs/kill_switch_audit.log:3147-3148). A deliberate CLI clear still works.
TRANSFER_HOLDS = {
    "arc-funding": "scripts/memecoin_bridge_solana_to_arc.py",
    "arc-to-base": "scripts/memecoin_bridge_arc_to_base.py",
    "arc-to-robinhood": "scripts/memecoin_bridge_arc_to_robinhood.py",
}


def transfer_hold(source):
    """(script, receipt) when `source` is a transfer script's hold, else None."""
    name, sep, request_id = str(source or "").partition(":")
    if not sep or not request_id or name not in TRANSFER_HOLDS:
        return None
    return TRANSFER_HOLDS[name], "data/%s-%s.json" % (name, request_id)


def clear_kill_switch(path=None, source="unattributed", *, expected_token=None, override_deploy=False,
                      refuse_transfer=False):
    """Clear only the observed hold. A different source must explicitly override a deploy hold.
    `refuse_transfer` (the dashboard) never releases a transfer script's hold."""
    p = _kill_path(path)
    with _kill_lock(p):
        state = kill_switch_status(p)
        if (not isinstance(expected_token, str) or not expected_token
                or expected_token != state["kill_token"]
                or (refuse_transfer and transfer_hold(state["kill_source"]))
                or (state["kill_deploy_held"] and source != state["kill_source"]
                    and override_deploy is not True)):
            _audit("CLEAR_REJECTED", source)
            return False
        os.unlink(p)
        _audit("CLEAR_OVERRIDE" if state["kill_deploy_held"] and source != state["kill_source"]
               else "CLEAR", source, token=expected_token)
        return True


# A DEPLOY'S OWN TRIP (2026-09-11 page; 2026-10-07 refusal card, operator ref 1bf3a775). Every deploy
# trips the switch for ~20-25 s while the runner restarts. A runtime-deploy hold that ends inside this
# grace is that restart, not an emergency stop. Read at call time, never at import (Gamma: a process
# that imports this before load_env_files would freeze the default; the ef22fe047 trap class).
_AUDIT_TAIL_BYTES = 65536


def kill_deploy_grace_s():
    return float(os.getenv("MEMECOIN_KILL_DEPLOY_GRACE_S", "180"))


def read_kill_audit_tail(audit_path=None):
    """The last 64 KB of logs/kill_switch_audit.log as text, or None when it cannot be read."""
    try:
        with open(audit_path or state_path("logs", "kill_switch_audit.log"), "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - _AUDIT_TAIL_BYTES))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return None


def kill_deploy_restart_at(at_iso, *, now_iso=None, audit_path=None, slack_s=2.0, tail=None):
    """Whether the switch hold that covered `at_iso` was a deploy's own restart.

    True: tripped by runtime-deploy, and it cleared (or, still open, has lasted until `now_iso`)
    within kill_deploy_grace_s(). False: any other source, or a longer hold. None: the audit log's
    last 64 KB shows no hold covering `at_iso` (no log, rotated past it, or a time it cannot parse).
    Read from logs/kill_switch_audit.log, not the live file: the HOTDOG hold cleared 2.7 s after its
    refusal, before the card rendered. `slack_s` absorbs the decision clock versus the audit's host
    clock. `tail` is that log text already read (the dashboard reads it once for all its rows)."""
    from datetime import datetime, timedelta, timezone

    def stamp(value):
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError(value)
        return parsed

    try:
        at = stamp(at_iso)
        now = stamp(now_iso) if now_iso is not None else datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return None
    text = tail if tail is not None else read_kill_audit_tail(audit_path)
    if text is None:
        return None
    slack = timedelta(seconds=float(slack_s))
    trip, cleared = None, None              # the latest TRIP at or before at + slack, and its CLEAR
    for line in text.splitlines():          # "<iso> ACTION source=<source> token=<token>" (_audit)
        when, _, rest = line.partition(" ")
        action, _, rest = rest.partition(" source=")
        source, _, token = rest.rpartition(" token=")
        try:
            t = stamp(when)
        except ValueError:
            continue                        # the tail's first partial line, or a foreign one
        if action == "TRIP" and source and t <= at + slack:
            trip, cleared = (t, source, token), None
        elif (trip is not None and cleared is None and action in ("CLEAR", "CLEAR_OVERRIDE")
              and token == trip[2]):
            cleared = t
    if trip is None or (cleared is not None and cleared < at - slack):
        return None
    if trip[1] != "runtime-deploy":
        return False
    return ((cleared or now) - trip[0]).total_seconds() < kill_deploy_grace_s()


def quiet_hours_window(spec=None):
    """Parse "start-end" UTC hours -> (start, end) with end exclusive, or None when off/invalid.
    A malformed value is OFF, and says so once: a typo must not silently halt entries all day."""
    raw = (cfg.MEMECOIN_REAL_QUIET_HOURS_UTC if spec is None else spec) or ""
    raw = str(raw).strip()
    if not raw:
        return None
    try:
        a, b = raw.split("-", 1)
        start, end = int(a), int(b)
        if not (0 <= start <= 23 and 0 <= end <= 24) or start == end:
            raise ValueError(raw)
        return start, end
    except (TypeError, ValueError):
        import logging
        logging.getLogger(__name__).warning(
            "MEMECOIN_REAL_QUIET_HOURS_UTC=%r is not 'start-end' hours; quiet window OFF", raw)
        return None


def in_quiet_hours(now_epoch=None, spec=None):
    """True inside the operator's quiet window (corrected UTC). Never raises."""
    window = quiet_hours_window(spec)
    if window is None:
        return False
    try:
        from sniper.util.clock import real_utc_now
        import datetime as _dt
        epoch = float(now_epoch) if now_epoch is not None else real_utc_now()
        hour = _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc).hour
    except Exception:
        return False
    start, end = window
    return (start <= hour < end) if start < end else (hour >= start or hour < end)


def real_entries_allowed(kill_path=None):
    """Real ENTRIES are allowed only when armed (MEMECOIN_REAL_ENABLED) and the kill switch is
    clear. Exits are NOT gated by this — they always run."""
    return bool(cfg.MEMECOIN_REAL_ENABLED) and not is_kill_tripped(kill_path)


def real_entry_block_reason(kill_path=None):
    """WHICH of the two independent conditions blocks real entries, or None when allowed.

    `real_entries_allowed` collapses both into one boolean and both legs then return the same
    bare `disarmed`. Those are different operator situations: MEMECOIN_REAL_ENABLED=0 is a
    deliberate stand-down, a tripped kill switch is an emergency stop somebody has to clear.

    On 2026-09-02 the switch stayed tripped for 11.6 hours while the refusal tape read `disarmed`,
    which is exactly what a deliberate stand-down reads like. The gate in memecoin_risk.py that
    would have said "kill switch tripped" is UNREACHABLE -- this check short-circuits above it --
    so that string has never once appeared in memecoin_entry_attempts.
    """
    if not bool(cfg.MEMECOIN_REAL_ENABLED):
        return "real_disabled"
    if is_kill_tripped(kill_path):
        return "kill_switch_tripped"
    return None
