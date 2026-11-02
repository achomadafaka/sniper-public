#!/usr/bin/env python3
"""Independent Office guard. It can kill only office.slice; funded processes are never targets."""
import argparse
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time


AUTO_RESUME_REASONS = frozenset({"funded_heartbeat", "cpu_pressure", "io_pressure", "memory_available"})


PILOT_SHA256 = "cc976837113057f75a118505d5ebdc28d4bea67c4e677f358aaabadabc33f31d"


def finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def load_pilot(path):
    """Exact Gamma-approved bytes, pinned outside the mutable manifest itself."""
    import hashlib
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != PILOT_SHA256:
        raise ValueError("pilot_manifest_unapproved")
    return json.loads(raw)


def caps_applied(path):
    """Pilot base caps only; the mandatory child gate proves idle IO before provider exec."""
    path = Path(path)
    if path.name != "office.slice":
        return False
    try:
        quota, period = (int(x) for x in (path / "cpu.max").read_text().split())
        return (period > 0 and quota == 2 * period
                and (path / "memory.max").read_text().strip() == "4294967296"
                and (path / "pids.max").read_text().strip() == "256")
    except (OSError, ValueError):
        return False


class PilotGuard:
    """Pilot only. Full-floor manifests require their own reviewed implementation."""
    def __init__(self):
        self.last_sample = None
        self.last_heartbeat = None
        self.last_pressure = None
        self.heartbeat_breaches = 0
        self.healthy_since = None
        self.resume_eligible = False

    def _fail(self, reason):
        self.healthy_since = None
        self.resume_eligible = False
        return reason

    def check(self, sample, *, now):
        if (not isinstance(sample, dict) or not finite(now)
                or sample.get("telemetry_error") is True or not finite(sample.get("ts"))):
            return self._fail("telemetry_unknown")
        at = sample["ts"]
        if at > now:
            return self._fail("telemetry_future")
        if now - at > 5:
            return self._fail("telemetry_stale")
        if sample.get("guard_policy_version") != "office-host-guard-pilot-v3":
            return self._fail("guard_policy_unknown")
        for key, reason in (("base_caps_applied", "caps_unapplied"), ("roster_valid", "roster_invalid"),
                            ("resources_verified", "resource_identity_unverified")):
            if sample.get(key) is not True:
                return self._fail(reason)
        keys = ("funded_heartbeat_age_s", "cpu_psi_some_avg10", "io_psi_some_avg10", "mem_available_bytes")
        if any(not finite(sample.get(k)) for k in keys):
            return self._fail("telemetry_unknown")
        if self.last_sample is not None:
            if at < self.last_sample:
                return self._fail("telemetry_regressed")
            if at == self.last_sample:
                return None  # not a new sample: cannot increment debounce or recovery
            if at - self.last_sample > 5:
                self.last_sample = at
                return self._fail("telemetry_missing")
        self.last_sample = at
        age = sample["funded_heartbeat_age_s"]
        if self.last_heartbeat is None or at - self.last_heartbeat >= 2:
            self.last_heartbeat = at
            self.heartbeat_breaches = self.heartbeat_breaches + 1 if age > 5 else 0
        if self.heartbeat_breaches >= 2:
            return self._fail("funded_heartbeat")
        if self.last_pressure is None or at - self.last_pressure >= 5:
            self.last_pressure = at
            for key, reason in (("cpu_psi_some_avg10", "cpu_pressure"), ("io_psi_some_avg10", "io_pressure")):
                if sample[key] > .20:
                    return self._fail(reason)
            if sample["mem_available_bytes"] < 4294967296:
                return self._fail("memory_available")
        healthy = (age <= 5 and sample["cpu_psi_some_avg10"] <= .20
                   and sample["io_psi_some_avg10"] <= .20 and sample["mem_available_bytes"] >= 4294967296)
        if healthy:
            if self.healthy_since is None:
                self.healthy_since = at
            self.resume_eligible = at - self.healthy_since >= 900
        else:
            self.healthy_since = None
            self.resume_eligible = False
        return None


def write_atomic(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".office-guard-", delete=False) as fh:
            tmp = Path(fh.name)
            json.dump(payload, fh, sort_keys=True, allow_nan=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def initialize(path, *, now):
    """Explicit operator/crew startup action; never called by the automatic guard loop."""
    write_atomic(path, {"state": "ready", "reason": None, "at": now, "guard_policy_version":"office-host-guard-pilot-v3"})


def external_guard(path, *, now=None, max_age_s=5.):
    now = time.time() if now is None else now
    try:
        data = json.loads(Path(path).read_text())
        if data.get("state") == "paused" and isinstance(data.get("reason"), str) and data["reason"]:
            return data["reason"]
        if data.get("state") == "ready" and data.get("reason") is None and finite(data.get("at")):
            if not finite(now) or not finite(max_age_s) or not 0 <= now - data["at"] <= max_age_s:
                return "guard_stale"
            return None
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return "guard_unknown"


def pause(path, *, reason, now, terminate):
    if not isinstance(reason, str) or not reason or not finite(now):
        raise ValueError("pause_invalid")
    write_atomic(path, {"state": "paused", "reason": reason, "at": now, "guard_policy_version":"office-host-guard-pilot-v3"})
    terminate("office.slice")


def _slice_path(cgroup):
    path = Path(cgroup)
    if (path.name != "office.slice" or not path.is_absolute() or ".." in path.parts
            or any(p.is_symlink() for p in (path, *path.parents))):
        raise ValueError("foreign_slice")
    return path


def open_kill_control(name, *, cgroup):
    """Only the delegated Office kill file is writable in the watchdog mount namespace."""
    if name != "office.slice":
        raise ValueError("foreign_slice")
    return os.open(_slice_path(cgroup) / "cgroup.kill", os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK)


def slice_populated(cgroup):
    path = _slice_path(cgroup) / "cgroup.events"
    if path.is_symlink():
        raise ValueError("population_unknown")
    with path.open() as stream:
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ValueError("population_unknown")
    rows = [line.split() for line in raw.splitlines()]
    values = [row[1] for row in rows if len(row) == 2 and row[0] == "populated"]
    if len(values) != 1 or values[0] not in ("0", "1") or any(len(row) != 2 for row in rows):
        raise ValueError("population_unknown")
    return values[0] == "1"


def terminate_slice(name, *, cgroup):
    fd = open_kill_control(name, cgroup=cgroup)
    try:
        if os.write(fd, b"1\n") != 2:
            raise OSError("cgroup_kill_short_write")
    finally:
        os.close(fd)


def start_or_resume(path, sample, *, now):
    """Explicit host action; a paused guard must have recorded 900 healthy seconds."""
    guard = PilotGuard()
    if guard.check(sample, now=now) or guard.healthy_since is None:
        raise ValueError("startup_telemetry_unhealthy")
    previous = None
    if Path(path).exists():
        previous = json.loads(Path(path).read_text())
        if previous.get("state") != "paused":
            raise ValueError("guard_already_initialized")
        at, since = previous.get("at"), previous.get("healthy_since")
        if (previous.get("resume_eligible") is not True or not finite(at) or not 0 <= now-at <= 5
                or not finite(since) or now-since < 900):
            raise ValueError("recovery_not_ready")
    write_atomic(path, {"state":"ready", "reason":None, "at":now, "mode":"pilot",
                        "guard_policy_version":"office-host-guard-pilot-v3",
                        "previous_pause_reason":None if previous is None else previous.get("reason")})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--latch", type=Path, required=True)
    parser.add_argument("--cgroup", type=Path)
    parser.add_argument("--initialize", action="store_true", help="explicit first start or eligible manual recovery")
    args = parser.parse_args(argv)
    load_pilot(args.manifest)  # refusal leaves the supervisor's readiness latch stale/unknown
    if args.initialize:
        start_or_resume(args.latch, json.loads(args.sample.read_text()), now=time.time())
        return 0
    if args.cgroup is None:
        parser.error("--cgroup is required for the watchdog loop")
    guard = PilotGuard()
    next_attempt = 0.
    while True:
        now = time.time()
        try:
            sample = json.loads(args.sample.read_text())
        except (OSError, ValueError):
            sample = None
        reason = guard.check(sample, now=now)
        if reason is None:
            try:
                fd = open_kill_control("office.slice", cgroup=args.cgroup)
                os.close(fd)  # opening validates the kill mount without killing an empty slice
            except (OSError, ValueError):
                reason = "kill_control_unavailable"
        missing = not os.path.lexists(args.latch)
        try:
            latch = json.loads(args.latch.read_text())
        except (OSError, ValueError):
            latch = {}
        if not isinstance(latch, dict):
            latch = {}
        first_healthy = missing and reason is None and guard.healthy_since is not None
        latch_reason = None if first_healthy else external_guard(args.latch, now=now)
        if missing and reason is None and not first_healthy:
            latch_reason = "startup_telemetry_unhealthy"
        paused = (latch.get("state") == "paused" and isinstance(latch.get("reason"), str)
                  and bool(latch["reason"]))
        malformed = (not missing and (latch_reason in {"guard_unknown", "guard_stale"}
                     or paused and (latch.get("guard_policy_version") != "office-host-guard-pilot-v3"
                                    or not finite(latch.get("at")) or not 0 <= now-latch["at"] <= 5)))
        termination = latch.get("termination", {})
        if (paused and latch["reason"] in AUTO_RESUME_REASONS and reason is None and guard.resume_eligible
                and not malformed and latch.get("auto_resume_blocked") is not True
                and isinstance(termination, dict) and termination.get("state") != "failed"
                and latch.get("guard_policy_version") == "office-host-guard-pilot-v3"
                and finite(latch.get("at")) and 0 <= now-latch["at"] <= 5):
            previous_reason = latch["reason"]
            write_atomic(args.latch, {"state": "ready", "reason": None, "at": now, "mode": "pilot",
                                     "guard_policy_version": "office-host-guard-pilot-v3",
                                     "previous_pause_reason": previous_reason})
            print(json.dumps({"event": "guard_auto_resumed", "previous_pause_reason": previous_reason}),
                  file=sys.stderr, flush=True)
            next_attempt = 0.
            time.sleep(1.)
            continue
        if paused or reason or latch_reason:
            if not paused:
                latch = {"state": "paused", "reason": reason or latch_reason,
                         "guard_policy_version": "office-host-guard-pilot-v3"}
            if malformed:
                latch["auto_resume_blocked"] = True  # refreshing diagnostics cannot repair malformed authority
            latch.update(at=now, resume_eligible=guard.resume_eligible, healthy_since=guard.healthy_since)
            write_atomic(args.latch, latch)  # readiness must be revoked before the kill attempt
            tick = time.monotonic()
            try:
                populated = slice_populated(args.cgroup)
                latch["population"] = {"state": "populated" if populated else "empty", "at": now}
            except (OSError, ValueError) as exc:
                populated = None
                latch["population"] = {"state": "unknown", "at": now, "error": type(exc).__name__}
            if populated is not False and tick >= next_attempt:
                if populated is None:
                    print(json.dumps(dict(event="population_unknown", **latch["population"])),
                          file=sys.stderr, flush=True)
                try:
                    terminate_slice("office.slice", cgroup=args.cgroup)
                except (OSError, ValueError) as exc:
                    latch["termination"] = {"state": "failed", "at": now,
                                            "error": type(exc).__name__, "errno": getattr(exc, "errno", None)}
                    print(json.dumps(dict(event="termination_failed", **latch["termination"])),
                          file=sys.stderr, flush=True)
                else:
                    latch["termination"] = {"state": "sent", "at": now}
                    print(json.dumps(dict(event="termination_sent", **latch["termination"])),
                          file=sys.stderr, flush=True)
                next_attempt = tick + 5.
            write_atomic(args.latch, latch)
        else:
            next_attempt = 0.
            write_atomic(args.latch, {"state": "ready", "reason": None, "at": now, "mode": "pilot", "guard_policy_version":"office-host-guard-pilot-v3"})
        time.sleep(1.)


def research_environment(config, *, resources, funded_resources):
    """Only explicitly attested research resources; names alone prove no capacity isolation.

    Shared-account reservation enforcement is unsupported here: refuse the resource.
    Caller supplies crew-owned provider/account/pool identities, never agent text.
    """
    allowed = {"MEMECOIN_RESEARCH_SOLANA_RPC_URL", "MEMECOIN_RESEARCH_HELIUS_RPC_URL"}
    if not isinstance(config, dict) or set(config) - allowed:
        raise ValueError("environment_not_allowlisted")
    for name in config:
        matches = [r for r in resources if r.get("env") == name]
        if len(matches) != 1:
            raise ValueError("resource_identity_unknown")
        resource = matches[0]
        identity = tuple(resource.get(k) for k in ("provider", "account", "pool"))
        if not all(isinstance(v, str) and v for v in identity):
            raise ValueError("resource_identity_unknown")
        if any(identity == tuple(r.get(k) for k in ("provider", "account", "pool")) for r in funded_resources):
            raise ValueError("shared_resource")
    return dict(config)


if __name__ == "__main__":
    main()
