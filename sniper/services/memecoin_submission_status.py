"""Sanitized submission health + attempt status for the operator cockpit.

Read-only projection: per-provider health (active / excluded / quarantined + last-health age) merged
with recent-attempt stats from ``memecoin_submission_attempts`` (accepted / ambiguous / rate-limit
counts, rolling p50/p95 latency, last attempt state + latency), plus the TPU sidecar's leader /
restart status. It makes NO network call (no RPC, no http) — the cockpit poll stays cheap — and it
NEVER adds execution authority: an audit read failure only produces empty stats.

In the runner process the live in-memory health/sidecar state is used directly. The runner also
snapshots that state to ``<state_root>/data/memecoin_submission_health.json`` (mode 0600, atomic) so a
separate web process can render it. Nothing exposed here is a URL, payload, key, or raw error string —
provider labels, states, counts, and latencies only.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile

logger = logging.getLogger(__name__)

_HEALTH_FILENAME = "memecoin_submission_health.json"
_HEALTH_FIELDS = ("state", "consecutive_failures", "ok", "fail", "wrong_chain", "last_health_age_s")


def _percentile(sorted_vals, pct):
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return round(float(sorted_vals[0]), 3)
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return round(sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac, 3)


def _in_memory_health_sidecars():
    """Live health snapshots + sidecar status from the current process (populated only where the
    submission backends were built — i.e. the runner). Empty elsewhere (e.g. the web process)."""
    try:
        from sniper.execution.submission_backends import (
            submission_health_snapshots, submission_sidecars,
        )
        health = submission_health_snapshots()
        sidecars = []
        for sc in submission_sidecars():
            try:
                sidecars.append(sc.sanitized_status())
            except Exception:
                pass
        return health, sidecars
    except Exception:
        return {}, []


def _read_snapshot_file(state_root):
    try:
        with open(os.path.join(str(state_root), "data", _HEALTH_FILENAME)) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}, []
    if not isinstance(data, dict):
        return {}, []
    health = data.get("health") if isinstance(data.get("health"), dict) else {}
    sidecars = data.get("sidecars") if isinstance(data.get("sidecars"), list) else []
    return health, sidecars


def _attempt_stats(conn, limit):
    from sniper.persistence import db_memecoin as m
    try:
        rows = m.recent_submission_attempts(conn, limit=limit)   # newest first, sanitized columns
    except Exception:
        return {}
    by = {}
    for r in rows:
        pid = r.get("provider_id")
        if not pid:
            continue
        d = by.setdefault(pid, {"accepted": 0, "ambiguous": 0, "explicit_rejected": 0, "not_sent": 0,
                                "rate_limit": 0, "count": 0, "last_state": None,
                                "last_latency_ms": None, "_lat": []})
        d["count"] += 1
        state = r.get("state")
        if state in ("accepted", "ambiguous", "explicit_rejected", "not_sent"):
            d[state] += 1
        reason = r.get("reason_code")
        if reason and "429" in str(reason):
            d["rate_limit"] += 1
        lat = r.get("latency_ms")
        if lat is not None:
            d["_lat"].append(float(lat))
        if d["last_state"] is None:                  # first row per provider == newest (DESC order)
            d["last_state"] = state
            d["last_latency_ms"] = None if lat is None else round(float(lat), 3)
    out = {}
    for pid, d in by.items():
        lat = sorted(d.pop("_lat"))
        d["p50_ms"] = _percentile(lat, 50)
        d["p95_ms"] = _percentile(lat, 95)
        out[pid] = d
    return out


def submission_status(conn, state_root=None, *, attempt_limit=2000):
    """The sanitized ``submission`` object for the cockpit. No network I/O."""
    health, sidecars = _in_memory_health_sidecars()
    if state_root:
        file_health, file_sidecars = _read_snapshot_file(state_root)
        # A separate web process can have partial in-memory registries after importing funded
        # context code but never own the TPU child. Fill missing providers and sidecar state from
        # the runner snapshot; current-process entries remain authoritative when both exist.
        health = {**file_health, **health}
        if not sidecars:
            sidecars = file_sidecars
    stats = _attempt_stats(conn, attempt_limit) if conn is not None else {}
    providers = {}
    for pid in set(health) | set(stats):
        row = {}
        row.update({k: v for k, v in health.get(pid, {}).items() if k in _HEALTH_FIELDS})
        row.update(stats.get(pid, {}))
        providers[pid] = row
    return {"providers": providers, "sidecars": list(sidecars)}


def _atomic_write_0600(path, text):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".mc_submit_health.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)                        # atomic same-directory rename
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_submission_health(state_root):
    """Atomically snapshot the in-process health + sidecar state to the 0600 observability file. This
    is the only artifact a separate reader process needs; attempt stats are always re-read live from
    the shared DB. Best-effort — a failure warns and never affects execution."""
    try:
        health, sidecars = _in_memory_health_sidecars()
        path = os.path.join(str(state_root), "data", _HEALTH_FILENAME)
        _atomic_write_0600(path, json.dumps({"health": health, "sidecars": sidecars}, sort_keys=True))
        return True
    except Exception:
        logger.warning("memecoin submission health snapshot write failed", exc_info=True)
        return False
