"""Stale-heartbeat page for the narrative-guesser worker (G4b). Pages ONCE when worker.heartbeat.json is older than STALE_S (or missing/unreadable/in the future) and ONCE on recovery;
a failed send does not advance the state, so it is retried next minute. The worker writes `ts` = host time.time() every ~5 s, and this reads it on the same host, so the age is one clock.
Research-only: it reads one file and sends one Telegram line; never the funded path."""
import json
import os
from pathlib import Path

STALE_S = 120.0
GRACE_S = 120.0                                            # a launch earns at most this much grace since the FIRST unhealthy observation; repeated launches cannot reset it
FUTURE_S = 60.0


def _read_ts(path):
    try:
        ts = json.loads(Path(path).read_text()).get("ts")
    except FileNotFoundError:
        return None, "no_heartbeat"
    except (OSError, ValueError, AttributeError):
        return None, "heartbeat_unreadable"
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or ts != ts or ts <= 0:
        return None, "heartbeat_malformed"
    return float(ts), None


def check(base, now, send, *, stale_s=STALE_S, started_this_run=False):
    """-> 'ok' | 'stale' | 'paged_stale' | 'paged_recovered' | 'skipped_just_started' | 'send_failed'. `send(text)` -> truthy on a delivered page."""
    base = Path(base)
    ts, reason = _read_ts(base / "worker.heartbeat.json")
    if reason is None and ts > now + FUTURE_S:
        reason = "heartbeat_in_future"
    if reason is None and now - ts > stale_s:
        reason = "heartbeat_stale_%ds" % int(now - ts)
    state_path = base / "page_state.json"
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    was_stale = bool(state.get("stale"))
    if reason is None and state.get("grace_since") is not None:
        state["grace_since"] = None                        # healthy again: the grace clock is cleared
        _save(state_path, state)
    if started_this_run and reason is not None:
        since = state.get("grace_since")
        if since is None:
            state["grace_since"] = now                     # persisted: a crash loop relaunching every minute cannot keep restarting the grace
            _save(state_path, state)
            return "skipped_just_started"
        if now - since <= GRACE_S:
            return "skipped_just_started"                  # a worker launched this minute has not written a beat yet; judge it next minute
        # grace spent: a worker that still has no healthy heartbeat after GRACE_S is a failed start, page it below
    if reason is not None and not was_stale:
        if not send("narrative guesser worker STALE: %s (limit %ds). Check data/desk/narrative_guess/worker.heartbeat.json and the watchdog log." % (reason, int(stale_s))):
            return "send_failed"
        _save(state_path, {"stale": True, "since": now, "reason": reason, "grace_since": state.get("grace_since")})
        return "paged_stale"
    if reason is None and was_stale:
        if not send("narrative guesser worker RECOVERED (heartbeat fresh, was stale since %s)." % state.get("since")):
            return "send_failed"
        _save(state_path, {"stale": False, "since": None, "reason": None, "grace_since": None})
        return "paged_recovered"
    return "stale" if reason is not None else "ok"


def _save(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)      # a worker that fails before it creates its own directory must still get a persisted grace (Delta 9b8fbe4b7)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(obj, sort_keys=True))
    os.replace(tmp, path)


def load_page_env(release_root, environ=None):
    """The Telegram credentials live in the STATE root's .env (SNIPER_STATE_ROOT), not in the pinned release directory (a release worktree has no .env). Load the state root first, then the
    release root; the shell env and earlier files win (setdefault). Without this a real stale page is silently 'alerts disabled' (found by the live drill 2026-10-28)."""
    from sniper.util.envfiles import load_env_files
    env = os.environ if environ is None else environ
    loaded = []
    for root in (env.get("SNIPER_STATE_ROOT"), str(release_root)):
        if root:
            loaded += load_env_files(root, env=env)
    return loaded
