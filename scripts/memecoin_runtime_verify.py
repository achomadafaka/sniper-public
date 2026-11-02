#!/usr/bin/env python3
"""Fail-closed verification for a pinned memecoin release."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
SIDECAR_TARGET = "x86_64-unknown-linux-gnu"
PINNED_MEMECOIN_CURRENT = ".runtime/memecoin/current"
CLUSTER_SOCKET_NAME = "memecoin_cluster_gate.sock"
CLUSTER_DB_NAME = "memecoin_cluster_gate.db"
STATE_ROOT_ENV = "SNIPER_STATE_ROOT"


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_sidecar(sidecar, code_root, release_sha):
    """Validate the deploy manifest's TPU sidecar object against the on-disk binary and the release
    tree. Returns a list of error strings ([] when valid). The verifier recomputes the binary and
    Cargo.lock hashes itself and checks the artifact path + mode — it never trusts the manifest's
    self-reported values. A missing or invalid sidecar object is an error (blocks deployment)."""
    if not isinstance(sidecar, dict):
        return ["sidecar manifest object missing or invalid"]
    errors = []
    path = str(sidecar.get("path") or "")
    sha256 = str(sidecar.get("sha256") or "")
    source_sha = str(sidecar.get("source_sha") or "")
    lock_sha = str(sidecar.get("cargo_lock_sha256") or "")
    target = str(sidecar.get("target") or "")
    image = str(sidecar.get("build_image_digest") or "")
    if not HEX64.match(sha256):
        errors.append("sidecar sha256 is not 64 lowercase hex")
    if not FULL_SHA.match(source_sha):
        errors.append("sidecar source_sha is not a full commit SHA")
    elif release_sha and source_sha != release_sha:
        errors.append("sidecar source_sha does not match release SHA")
    if not HEX64.match(lock_sha):
        errors.append("sidecar cargo_lock_sha256 is not 64 lowercase hex")
    if target != SIDECAR_TARGET:
        errors.append("sidecar target is not " + SIDECAR_TARGET)
    if not image.startswith("sha256:"):
        errors.append("sidecar build_image_digest is not a sha256 digest")
    p = Path(path)
    if p.is_symlink():
        errors.append("sidecar binary is a symlink")
    elif not p.is_file():
        errors.append("sidecar binary missing")
    else:
        parts = p.absolute().parts
        if (len(parts) < 3 or tuple(parts[-3:]) !=
                ("artifacts", source_sha, "memecoin-tpu-sidecar")):
            errors.append("sidecar binary is outside artifacts/<source_sha>")
        mode = p.stat().st_mode & 0o777
        if mode & 0o222:
            errors.append("sidecar binary is writable")
        if not mode & 0o111:
            errors.append("sidecar binary is not executable")
        if HEX64.match(sha256) and _sha256_file(p) != sha256:
            errors.append("sidecar binary hash mismatch")
        lock = Path(code_root) / "rust" / "solana-tpu-sidecar" / "Cargo.lock"
        if not lock.is_file():
            errors.append("release Cargo.lock missing")
        elif HEX64.match(lock_sha) and _sha256_file(lock) != lock_sha:
            errors.append("sidecar Cargo.lock hash mismatch")
    return errors


@dataclass(frozen=True)
class Verification:
    ok: bool
    code_root: str
    state_root: str
    sha: str | None
    errors: tuple[str, ...]


def load_manifest(path):
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def verify_lifi_manifest(manifest, code_root):
    expected = str(manifest.get("lifi_manifest_sha256") or "")
    if not HEX64.fullmatch(expected):
        return ["deploy manifest LI.FI hash is not 64 lowercase hex"]
    path = Path(code_root) / "sniper" / "execution" / "lifi_4663_manifest.json"
    try:
        value = json.loads(path.read_text())
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (OSError, TypeError, json.JSONDecodeError, ValueError):
        return ["release LI.FI manifest is absent or invalid"]
    if not isinstance(value, dict) or value.get("chain_id") != "4663":
        return ["release LI.FI manifest has the wrong chain"]
    if hashlib.sha256(canonical.encode()).hexdigest() != expected:
        return ["LI.FI manifest hash mismatch"]
    return []


def _git(code_root, *args):
    result = subprocess.run(
        ["git", *args], cwd=code_root, text=True, capture_output=True, check=False,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _default_config_validator(code_root, state_root, manifest_path=None, provider_env=None,
                              allow_shyft_unavailable=False):
    env = os.environ.copy()
    env["SNIPER_STATE_ROOT"] = str(state_root)
    env["DB_PATH"] = str(Path(state_root) / "sniper_tracker.db")
    env["PYTHONPATH"] = str(code_root)
    if provider_env is not None:
        from scripts.research.shyft_migration_preflight import PROVIDER_KEYS, read_env
        staged = read_env(provider_env)
        for key in PROVIDER_KEYS:
            env.pop(key, None)
            if key in staged:
                env[key] = staged[key]
    if manifest_path is not None:
        env["SNIPER_MEMECOIN_DEPLOY_MANIFEST"] = str(Path(manifest_path).expanduser().resolve())
        # Candidate validation runs before the old pinned runner stops. Never let its temporary
        # sidecar unlink or bind the live runner's socket.
        env["SNIPER_TPU_SOCKET_PATH_OVERRIDE"] = str(
            Path(state_root) / "data" / f".memecoin-tpu-verify-{os.getpid()}.sock"
        )
    script = """
from sniper.util.envfiles import load_env_files
from sniper.util.state_paths import configure_memecoin_state_env
load_env_files(%r)
configure_memecoin_state_env()
from sniper.settings import memecoin as cfg
cfg.validate_real_exit_policy()
from sniper.persistence.db_core import get_connection
conn = get_connection()
custody = None
if cfg.memecoin_lifi_mode() == 'authenticated_funded':
    from sniper.execution.evm_wallet import load_keypair
    custody = load_keypair(cfg.MEMECOIN_EVM_KEYPAIR).address
cfg.validate_lifi_config(conn=conn, custody_wallet=custody)
from sniper.persistence import db_memecoin as m
if cfg.MEMECOIN_REAL_ENABLED or m.ink_recovery_required(conn):
    from sniper.services.memecoin_real_runner import build_all_contexts
    if not build_all_contexts(conn=conn, allow_shyft_unavailable=%r):
        raise SystemExit('real leg armed but no venue context is constructible')
conn.close()
""" % (str(state_root), allow_shyft_unavailable)
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=code_root, env=env,
        text=True, capture_output=True, check=False,
    )
    if result.returncode:
        raise ValueError((result.stderr or result.stdout or "real config validation failed").strip())


def provider_stage_errors(state_root, staged_env, preflight_path, *, inherited_env=None):
    """Recalculate the private report's input bindings, scope and allocation; safe error codes only."""
    from scripts.research.shyft_migration_preflight import (
        PROVIDER_KEYS, check_preflight, file_digest, read_env, validate_provider_stage,
    )
    state = Path(state_root)
    inherited_env = os.environ if inherited_env is None else inherited_env
    try:
        stage = Path(staged_env)
        if stage.is_symlink() or not stage.is_file() or stat.S_IMODE(stage.stat().st_mode) & 0o077:
            return ["provider_stage_not_private"]
        report = json.loads(Path(preflight_path).read_text())
        errors = list(validate_provider_stage(state / ".env", state / ".fomo.env", stage, report))
        record_path = Path(report["allocation_record"])
        if file_digest(record_path) != report.get("allocation_record_sha256"):
            errors.append("allocation_record_changed")
        env = read_env(stage)
        for key, value in read_env(state / ".fomo.env").items():
            env.setdefault(key, value)
        if any(key in inherited_env and inherited_env[key] != env.get(key, "") for key in PROVIDER_KEYS):
            errors.append("provider_shell_override")
        calculated = check_preflight(env, json.loads(record_path.read_text()))
        errors.extend(calculated["reasons"])
        for key in ("version", "provider", "provider_roles", "allocation", "topology", "capabilities"):
            if calculated.get(key) != report.get(key):
                errors.append("provider_preflight_mismatch")
        return list(dict.fromkeys(errors))
    except (OSError, ValueError, TypeError, KeyError):
        return ["provider_stage_invalid"]


def _heartbeat_time(value):
    if type(value) in (int, float):
        return value
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.utcoffset() is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def shyft_watcher_errors(expected_script, beat, ready, rows, *, now=None, monotonic=None,
                         allow_overlap=False, primary_only=False, candidate_pid=None):
    """Actual process/path plus current roster ACK and fresh confirmed heads, without live calls."""
    now = time.time() if now is None else now
    monotonic = time.monotonic() if monotonic is None else monotonic
    errors = []
    current = [pid for pid, path in rows if path == expected_script]
    parts = ready.split()
    selected = candidate_pid if candidate_pid is not None else (current[0] if len(current) == 1 else None)
    if selected not in current or (len(current) != 1 and not (allow_overlap and candidate_pid is not None)):
        errors.append("shyft_watcher_wrong_release")
    if not allow_overlap and len(rows) != 1:
        errors.append("shyft_watcher_not_sole")
    if (selected is None or not parts or parts[0] != str(selected) or "partial" in parts[1:]
            or (not primary_only and "primary" in parts[1:])):
        errors.append("shyft_watcher_not_ready")
    if not isinstance(beat, dict):
        return errors + ["shyft_watcher_heartbeat_missing"]
    if beat.get('pid') != selected:
        errors.append('shyft_watcher_wrong_generation')
    ts = _heartbeat_time(beat.get("ts"))
    if ts is None or not 0 <= now - ts <= 10:
        errors.append("shyft_watcher_heartbeat_stale")
    desired = beat.get("subs_desired")
    if (beat.get("provider") != "shyft" or beat.get("connected") is not True
            or type(desired) is not int or desired < 1 or beat.get("subs") != desired
            or beat.get("subs_in_flight") != 0):
        errors.append("shyft_watcher_ack_incomplete")
    status = beat.get("shyft_confirmed")
    feeds = status.get("feeds") if isinstance(status, dict) else None
    if not isinstance(feeds, dict):
        return list(dict.fromkeys(errors + ["shyft_watcher_ack_incomplete"]))
    try:
        hosts = {url if url in ("grpc.ams.shyft.to", "grpc.fra.shyft.to") else urlsplit(url).hostname
                 for url in feeds}
        full = {"grpc.ams.shyft.to", "grpc.fra.shyft.to"}
        phase = (status.get('handover') or {}).get('phase')
        valid = hosts == full or (primary_only and len(hosts) == 1 and hosts <= full and phase == 'primary')
        if not valid:
            errors.append("shyft_watcher_graph_invalid")
    except (ValueError, TypeError):
        errors.append("shyft_watcher_graph_invalid")
    requested_sets = []
    for feed in feeds.values():
        if not isinstance(feed, dict):
            errors.append("shyft_watcher_ack_incomplete")
            continue
        requested, ack = feed.get("requested_owners"), feed.get("acknowledged_owners")
        if (feed.get("connected") is not True or not isinstance(requested, (list, tuple))
                or any(not isinstance(owner, str) or not owner for owner in requested)
                or len(set(requested)) != len(requested)
                or len(requested) != desired or ack != requested):
            errors.append("shyft_watcher_ack_incomplete")
        else:
            requested_sets.append(tuple(requested))
        head_time, head = feed.get("head_monotonic"), feed.get("head_slot")
        if (type(head_time) not in (int, float) or not 0 <= monotonic - head_time <= 2
                or type(head) is not int or head <= 0):
            errors.append("shyft_watcher_head_stale")
    if len(set(requested_sets)) != 1:
        errors.append("shyft_watcher_ack_incomplete")
    return list(dict.fromkeys(errors))


def _owned_script_rows(state_root, script_suffix):
    # Scope on explicit state-root environment and script argument, never a substring/pkill.
    from scripts.memecoin_runtime_processes import _python_entry_script
    rows = []
    state = Path(state_root).resolve()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            args = [s.decode() for s in (entry / "cmdline").read_bytes().split(b"\0") if s]
            if not args or not Path(args[0]).name.startswith("python"):
                continue
            script = _python_entry_script(args)
            if not script:
                continue
            path = Path(script)
            path = path if path.is_absolute() else (entry / "cwd").resolve() / path
            path = path.resolve()
            if not str(path).endswith("/" + script_suffix):
                continue
            process_env = dict(s.decode().split("=", 1) for s in
                               (entry / "environ").read_bytes().split(b"\0") if b"=" in s)
            if Path(process_env.get(STATE_ROOT_ENV, "/nonexistent")).resolve() != state:
                continue
            rows.append((int(entry.name), str(path)))
        except (OSError, UnicodeError, ValueError):
            continue
    return rows


def _watcher_rows(state_root):
    return _owned_script_rows(state_root, "scripts/research/onchain_wallet_watch.py")


def _process_start_time(pid):
    try:
        return (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def watcher_handover_record(code_root, state_root, candidate_pid):
    expected = str((Path(code_root) / 'scripts/research/onchain_wallet_watch.py').resolve())
    rows = _watcher_rows(state_root)
    birth = _process_start_time(candidate_pid)
    if (candidate_pid, expected) not in rows or birth is None:
        raise ValueError('shyft_watcher_wrong_generation')
    old = [[pid, path, _process_start_time(pid)] for pid, path in rows if pid != candidate_pid]
    if any(row[2] is None for row in old):
        raise ValueError('shyft_watcher_identity_unknown')
    return {'version': 1, 'candidate': [candidate_pid, expected, birth], 'old': old}


def owned_script_record(state_root, suffix, *, expected_root=None):
    allowed = {'scripts/memecoin_rh_feed_shadow_loop.py', 'scripts/research/onchain_wallet_watch.py'}
    if suffix not in allowed:
        raise ValueError('process_record_script_invalid')
    rows = _owned_script_rows(state_root, suffix)
    if expected_root is not None and any(path != str(Path(expected_root).resolve() / suffix) for pid, path in rows):
        raise ValueError('process_record_release_drift')
    old = [[pid, path, _process_start_time(pid)] for pid, path in rows]
    if any(birth is None for pid, path, birth in old):
        raise ValueError('process_record_identity_unknown')
    return {'suffix': suffix, 'old': old}


def retire_old_watchers(state_root, record, *, script_suffix='scripts/research/onchain_wallet_watch.py', wait_s=0):
    """The existing watchdog's SIGTERM, bound to captured identities and pidfds; never SIGKILL."""
    import signal
    state = Path(state_root).resolve()
    rows_fn = _watcher_rows if script_suffix == 'scripts/research/onchain_wallet_watch.py' else (
        lambda state: _owned_script_rows(state, script_suffix))
    rows = set(rows_fn(state))
    old = record.get('old') if isinstance(record, dict) else None
    if not isinstance(old, list):
        return False
    ok = True
    for identity in old:
        if not isinstance(identity, list) or len(identity) != 3:
            return False
        pid, path, birth = identity
        if type(pid) is not int or not isinstance(path, str) or not isinstance(birth, str):
            return False
        owned = (path.startswith(str(state / '.runtime/memecoin/releases') + '/')
                 or path == str(state / script_suffix))
        if not owned:
            ok = False
            continue
        if (pid, path) not in rows:
            if _process_start_time(pid) == birth:
                ok = False  # alive but membership is unreadable; never infer it is gone
            continue
        fd = None
        try:
            fd = os.pidfd_open(pid)
            if _process_start_time(pid) != birth or (pid, path) not in set(rows_fn(state)):
                ok = False
                continue
            signal.pidfd_send_signal(fd, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except (OSError, AttributeError):
            ok = False
        finally:
            if fd is not None:
                os.close(fd)
    if ok and wait_s:
        deadline = time.monotonic() + wait_s
        while any((pid, path) in set(rows_fn(state)) and _process_start_time(pid) == birth
                  for pid, path, birth in old):
            if time.monotonic() >= deadline:
                return False
            time.sleep(.05)
    return ok


def abort_watcher_handover(code_root, state_root, record):
    try:
        pid, path, birth = record['candidate']
        if type(pid) is not int or pid <= 0 or not isinstance(path, str) or not isinstance(birth, str) or not birth:
            return False
        expected = str(Path(code_root).resolve() / 'scripts/research/onchain_wallet_watch.py')
        state = Path(state_root).resolve()
        suffix = 'scripts/research/onchain_wallet_watch.py'
        releases = str(state / '.runtime/memecoin/releases') + '/'
        rows = set(_watcher_rows(state_root))
        if not any(type(old_pid) is int and old_pid > 0 and isinstance(old_path, str)
                   and isinstance(old_birth, str) and old_birth
                   and (old_path == str(state / suffix)
                        or (old_path.startswith(releases) and old_path.endswith('/' + suffix)))
                   and (old_pid, old_path) in rows and _process_start_time(old_pid) == old_birth
                   for old_pid, old_path, old_birth in record['old']):
            return False
        if (pid, path) not in rows:
            # A missing identity can mean unreadable /proc. Only an absent PID directory proves
            # the candidate gone; rollback may inspect a record from a newer owned release.
            try:
                (Path('/proc') / str(pid)).stat()
            except FileNotFoundError:
                return str(Path(path).resolve()).startswith(releases) and path.endswith('/' + suffix)
            return False
        if path != expected or _process_start_time(pid) != birth:
            return False
        return retire_old_watchers(state_root, {'old': [[pid, path, birth]]}, wait_s=5)
    except (OSError, TypeError, ValueError, KeyError):
        return False


def provider_rollback_errors(runner_script, watcher_script, heartbeat, beat, ready,
                             runner_rows, watcher_rows, started, *, now=None):
    """Readonly proof before releasing a provider rollback's funded admission hold."""
    from sniper.services.memecoin_runner import runner_launch_health
    now = time.time() if now is None else now
    errors = []
    if len(runner_rows) != 1 or runner_rows[0][1] != runner_script:
        errors.append("rollback_runner_wrong_release")
    try:
        if (not isinstance(heartbeat, dict) or not 0 <= now - heartbeat["ts"] <= 10
                or runner_launch_health(heartbeat, started, now)["reason"] != "ok"):
            errors.append("rollback_runner_not_ready")
    except (ValueError, TypeError, KeyError):
        errors.append("rollback_runner_not_ready")
    if isinstance(beat, dict) and beat.get("provider") == "shyft":
        errors.extend(shyft_watcher_errors(watcher_script, beat, ready, watcher_rows, now=now))
    else:
        if len(watcher_rows) != 1 or watcher_rows[0][1] != watcher_script:
            errors.append("rollback_watcher_wrong_release")
        parts = ready.split()
        if (not watcher_rows or not parts or parts[0] != str(watcher_rows[0][0])
                or "partial" in parts[1:]):
            errors.append("rollback_watcher_not_ready")
        ts = _heartbeat_time(beat.get("ts")) if isinstance(beat, dict) else None
        if ts is None or not 0 <= now - ts <= 10:
            errors.append("rollback_watcher_heartbeat_stale")
        desired = beat.get("subs_desired") if isinstance(beat, dict) else None
        if (type(desired) is not int or desired < 0 or beat.get("connected") is not True
                or beat.get("subs") != desired or beat.get("subs_in_flight") != 0):
            errors.append("rollback_watcher_ack_incomplete")
    return list(dict.fromkeys(errors))


def verify_provider_rollback_processes(code_root, state_root, started):
    code, state = Path(code_root).resolve(), Path(state_root).resolve()
    try:
        heartbeat = json.loads((state / "data/memecoin_runner.heartbeat").read_text())
        beat = json.loads((state / "data/onchain_watch.heartbeat").read_text())
        ready = (state / "data/onchain_watch.ready").read_text().strip()
    except (OSError, ValueError):
        return ["rollback_heartbeat_missing"]
    return provider_rollback_errors(
        str(code / "scripts/memecoin_runner_loop.py"),
        str(code / "scripts/research/onchain_wallet_watch.py"), heartbeat, beat, ready,
        _owned_script_rows(state, "scripts/memecoin_runner_loop.py"), _watcher_rows(state), started)


def verify_shyft_watcher(code_root, state_root, *, allow_overlap=False, primary_only=False,
                         candidate_pid=None, candidate_birth=None):
    state = Path(state_root)
    try:
        beat = json.loads((state / "data/onchain_watch.heartbeat").read_text())
        ready = (state / "data/onchain_watch.ready").read_text().strip()
    except (OSError, ValueError):
        return ["shyft_watcher_heartbeat_missing"]
    expected = str((Path(code_root) / "scripts/research/onchain_wallet_watch.py").resolve())
    if candidate_birth is not None and _process_start_time(candidate_pid) != candidate_birth:
        return ['shyft_watcher_wrong_generation']
    return shyft_watcher_errors(expected, beat, ready, _watcher_rows(state), allow_overlap=allow_overlap,
                                primary_only=primary_only, candidate_pid=candidate_pid)


def shyft_runtime_graph_errors(heartbeat, rh_heartbeat, env, *, now=None, monotonic=None, rh_now=None):
    """Startup-only proof of actual funded provider roles; does not change admission policy."""
    import math
    from sniper.services.memecoin_solana_provider import provider_urls
    from sniper.services.memecoin_real_runner import _provider_role_identity
    now = time.time() if now is None else now
    monotonic = time.monotonic() if monotonic is None else monotonic
    rh_now = now if rh_now is None else rh_now
    errors = []
    def obj(value):
        return value if isinstance(value, dict) else {}
    def number(value):
        return type(value) in (int, float) and math.isfinite(value)
    def positive(value):
        return type(value) is int and value > 0
    hb = obj(heartbeat)
    lane = obj(hb.get('slot_lane'))
    # NOTHING ARMED (copy stop, 2026-10-23): an empty roster is the correct subscription only when the
    # lane's own armed and owner counts AND the address registry's independent armed count are all an
    # exact int 0. A missing or failed count is unknown, never zero, and keeps the non-empty rule.
    nothing_armed = all(type(v) is int and v == 0 for v in
                        (lane.get('armed'), lane.get('owners'), obj(hb.get('address_coverage')).get('armed')))
    def owners(row):
        wanted, ack = row.get('requested_owners'), row.get('acknowledged_owners')
        if (nothing_armed and isinstance(wanted, (list, tuple)) and isinstance(ack, (list, tuple))
                and not wanted and not ack):
            return ()
        if (not isinstance(wanted, (list, tuple)) or not wanted
                or any(type(v) is not str or not v for v in wanted)
                or len(set(wanted)) != len(wanted) or not isinstance(ack, (list, tuple))
                or tuple(wanted) != tuple(ack)):
            return None
        return tuple(wanted)
    def fingerprint(row):
        value = row.get('requested_filter_fingerprint')
        return type(value) is str and HEX64.fullmatch(value) and value == row.get('acknowledged_filter_fingerprint')
    ts = _heartbeat_time(hb.get('ts'))
    age = now - ts if number(ts) else None
    if age is None or not 0 <= age <= 10 or hb.get('auth_ok') is not True:
        errors.append('shyft_runtime_heartbeat_unready')
    age = age if age is not None else 1000
    roles = obj(obj(hb.get('solana_rpc_pool')).get('provider_roles'))
    try:
        expected = {role: [_provider_role_identity(url) for url in provider_urls(role, env)]
                    for role in ('funded', 'read', 'tpu')}
        actual = {role: ([{k: obj(row).get(k) for k in ('hostname', 'url_fingerprint')}
                         for row in roles.get(role, [])] if role == 'funded' else
                        [{k: obj(roles.get(role)).get(k) for k in ('hostname', 'url_fingerprint')}])
                  for role in expected}
        if roles.get('provider') != 'shyft' or actual != expected or obj(roles.get('tpu')).get('constructed') is not True:
            errors.append('shyft_runtime_http_graph_invalid')
    except (ValueError, TypeError, AttributeError):
        errors.append('shyft_runtime_http_graph_invalid')
    tpu = obj(hb.get('tpu'))
    if (any(tpu.get(k) is not True for k in ('configured', 'running', 'ready'))
            or not number(tpu.get('last_health_age_s')) or not 0 <= tpu['last_health_age_s'] + age <= 10):
        errors.append('shyft_runtime_tpu_unready')
    feeds = obj(lane.get('signal_feeds'))
    expected_names = {'shyft:grpc.ams.shyft.to', 'shyft:grpc.fra.shyft.to'}
    if set(feeds) != expected_names:
        errors.append('shyft_runtime_processed_graph_invalid')
    rosters = []
    for name in expected_names:
        feed = obj(feeds.get(name))
        roster = owners(feed)
        watch, connection = feed.get('watch_generation'), feed.get('connection_generation')
        if (feed.get('connected') is not True or roster is None or not positive(connection)
                or not positive(watch) or type(feed.get('acknowledged_watch_generation')) is not int
                or feed.get('acknowledged_watch_generation') != watch
                or type(feed.get('requested_watch_generation')) is not int
                or feed.get('requested_watch_generation') != watch or not fingerprint(feed)):
            errors.append('shyft_runtime_processed_ack_incomplete')
        else:
            rosters.append(roster)
        if (not positive(feed.get('head_slot')) or not number(feed.get('head_age_s'))
                or not 0 <= feed['head_age_s'] + age <= 2):
            errors.append('shyft_runtime_processed_head_stale')
        labels = []
        for kind in ('migration', 'damm'):
            label = feed.get(kind + '_filter_generation')
            prefix = 'migrations' if kind == 'migration' else 'damm'
            match = re.fullmatch(prefix + r':c([1-9][0-9]*):r[1-9][0-9]*', label) if type(label) is str else None
            if (feed.get(kind + '_filter_requested') is not True
                    or feed.get(kind + '_filter_acknowledged') is not True or match is None):
                errors.append('shyft_runtime_processed_filter_unready')
            elif match is not None:
                labels.append(match.group(1))
                if int(match.group(1)) != connection:
                    errors.append('shyft_runtime_processed_filter_unready')
        if len(set(labels)) != 1:
            errors.append('shyft_runtime_processed_filter_unready')
    if len(set(rosters)) != 1:
        errors.append('shyft_runtime_processed_ack_incomplete')
    def accounts(row, *, host, commitment, marks=False):
        count = row.get('requested_account_count')
        return (row.get('endpoint' if marks else 'hostname') == host
                and row.get('commitment') == commitment and row.get('connected') is True
                and row.get('subscription_acknowledged') is True
                and positive(row.get('connection_generation')) and positive(row.get('watch_generation'))
                and type(count) is int and count >= 0
                and row.get('coverage') in ('idle', 'pending', 'partial', 'observed')
                and (row.get('coverage') == 'idle') == (count == 0)
                and (marks or (row.get('provider') == 'shyft' and fingerprint(row))))
    account = obj(lane.get('account_stream'))
    observed = account.get('acknowledged_account_count')
    if (not accounts(account, host='grpc.ams.shyft.to', commitment='confirmed', marks=True)
            or type(observed) is not int or not 0 <= observed <= account.get('requested_account_count', -1)):
        errors.append('shyft_runtime_marks_ack_incomplete')
    source = obj(obj(obj(hb.get('fastlane')).get('source_sell_mirror')).get('account_socket'))
    if not accounts(source, host='grpc.ams.shyft.to', commitment='processed'):
        errors.append('shyft_runtime_source_accounts_ack_incomplete')
    def transaction_ack(row, *, observer=False):
        count = row.get('requested_count')
        generation = row.get('generation')
        return (row.get('provider') == 'shyft' and row.get('hostname') == 'grpc.fra.shyft.to'
                and row.get('commitment') == 'processed' and row.get('connected') is True
                and positive(generation) and type(row.get('acknowledged_generation')) is int
                and row.get('acknowledged_generation') == generation
                and type(count) is int and count >= 0 and type(row.get('acknowledged_count')) is int
                and row.get('acknowledged_count') == count
                and (not observer or count == 1) and fingerprint(row)
                and positive(row.get('head_slot')) and number(row.get('head_monotonic'))
                and 0 <= monotonic - row['head_monotonic'] <= 2)
    if not transaction_ack(obj(lane.get('execution_observer')), observer=True):
        errors.append('shyft_runtime_observer_unready')
    if env.get('MEMECOIN_SHYFT_PREPARE_ENABLED') == '1':
        rabbit = obj(obj(lane.get('preparation')).get('feed'))
        if (rabbit.get('endpoint_hostname') != urlsplit(env.get('MEMECOIN_SHYFT_PREPARE_GRPC_URL', '')).hostname
                or rabbit.get('connected') is not True or not positive(rabbit.get('connection_generation'))
                or owners(rabbit) not in rosters or owners(rabbit) is None):
            errors.append('shyft_runtime_prepare_unready')
    rh = obj(rh_heartbeat)
    rh_ts = _heartbeat_time(rh.get('at'))
    stream = obj(rh.get('stream'))
    watch = stream.get('watch_generation')
    if (rh_ts is None or not 0 <= rh_now - rh_ts <= 90 or not transaction_ack(stream)
            or not positive(watch) or stream.get('requested_watch_generation') != watch
            or stream.get('acknowledged_watch_generation') != watch):
        errors.append('shyft_runtime_rh_deposit_unready')
    return list(dict.fromkeys(errors))


def verify_shyft_runtime_graph(code_root, state_root):
    """Read files and same-state process identities only. No RPC, signing or SQLite handle."""
    from scripts.research.shyft_migration_preflight import read_env
    code, state = Path(code_root).resolve(), Path(state_root).resolve()
    errors = []
    def read_beat(name):
        try:
            value = json.loads((state / 'data' / name).read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}
    heartbeat = read_beat('memecoin_runner.heartbeat')
    rh = read_beat('memecoin_rh_feed_shadow.heartbeat')
    for suffix, label in [('scripts/memecoin_runner_loop.py', 'runner'),
                          ('scripts/memecoin_rh_feed_shadow_loop.py', 'rh')]:
        rows = _owned_script_rows(state, suffix)
        expected = str(code / suffix)
        if len(rows) != 1 or rows[0][1] != expected or (label == 'rh' and rh.get('pid') != rows[0][0]):
            errors.append('shyft_runtime_' + label + '_process_invalid')
    stamp = _heartbeat_time(rh.get('ts'))
    if stamp is None or not 0 <= time.time() - stamp <= 15:
        errors.append('shyft_runtime_rh_heartbeat_stale')
    try:
        env = read_env(state / '.env')
        for key, value in (read_env(state / '.fomo.env') if (state / '.fomo.env').is_file() else {}).items():
            env.setdefault(key, value)
    except (OSError, ValueError):
        return errors + ['shyft_runtime_configuration_missing']
    from sniper.services.memecoin_solana_provider import provider_urls
    from sniper.services.memecoin_real_runner import _provider_role_identity
    watcher = read_beat('onchain_watch.heartbeat')
    try:
        status = watcher.get('shyft_confirmed') or {}
        if status.get('http_role') != _provider_role_identity(provider_urls('signal', env)[0]):
            errors.append('shyft_runtime_watcher_http_graph_invalid')
    except (ValueError, TypeError, AttributeError):
        errors.append('shyft_runtime_watcher_http_graph_invalid')
    deposit = rh.get('deposit')
    stream = deposit.get('stream') if isinstance(deposit, dict) else None
    errors.extend(shyft_runtime_graph_errors(heartbeat, {'at': rh.get('ts'), 'stream': stream}, env))
    errors.extend(verify_shyft_watcher(code, state))
    return list(dict.fromkeys(errors))


def verify_rh_process(code_root, state_root):
    state = Path(state_root).resolve()
    try:
        beat = json.loads((state / 'data/memecoin_rh_feed_shadow.heartbeat').read_text())
    except (OSError, ValueError):
        return ['shyft_runtime_rh_heartbeat_stale']
    rows = _owned_script_rows(state, 'scripts/memecoin_rh_feed_shadow_loop.py')
    expected = str(Path(code_root).resolve() / 'scripts/memecoin_rh_feed_shadow_loop.py')
    if (not isinstance(beat, dict) or len(rows) != 1 or rows[0][1] != expected
            or beat.get('pid') != rows[0][0]):
        return ['shyft_runtime_rh_process_invalid']
    stamp = _heartbeat_time(beat.get('ts'))
    return [] if stamp is not None and 0 <= time.time() - stamp <= 15 else ['shyft_runtime_rh_heartbeat_stale']


def source_mirror_capability_errors(code_root, state_root):
    """An active profile or durable partial requires the candidate's actual reader/recovery code."""
    from sniper.util.envfiles import load_env_files
    code, state = Path(code_root).resolve(), Path(state_root).resolve()
    env = os.environ.copy()
    profiles = []
    try:
        load_env_files(str(state), env=env)
        required = bool(env.get('MEMECOIN_REAL_SOURCE_MIRROR_TP100_WALLETS', '').strip())
        ledger = Path(env.get('DB_PATH') or state / 'sniper_tracker.db').expanduser()
        ledger = ledger.resolve() if ledger.is_absolute() else (state / ledger).resolve()
        with closing(sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True,
                                     timeout=1.0)) as conn:
            conn.row_factory = sqlite3.Row
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'memecoin_positions' in tables:
                columns = {r[1] for r in conn.execute('PRAGMA table_info(memecoin_positions)')}
                if {'execution_mode', 'is_open', 'funded_contract_json'} <= columns:
                    fields = ('source_user_id', 'network_id', 'mint', 'funded_contract_version',
                              'funded_contract_hash', 'funded_contract_json')
                    projection = ','.join(name if name in columns else f'NULL AS {name}' for name in fields)
                    for row in conn.execute(f"SELECT {projection} FROM memecoin_positions "
                                            "WHERE execution_mode='real' AND is_open=1 AND "
                                            "funded_contract_json LIKE '%source-mirror-tp100-v1%'"):
                        profiles.append(dict(row))
            if 'memecoin_manual_exit_intents' in tables:
                required = required or bool(conn.execute(
                    "SELECT 1 FROM memecoin_manual_exit_intents WHERE action='source_partial' "
                    "AND COALESCE(state,'') NOT IN ('done','refused','expired') LIMIT 1").fetchone())
    except (OSError, ValueError, sqlite3.Error):
        return ['source_mirror_capability_state_unreadable']
    if not required and not profiles:
        return []
    # Safe-path Python uses only the candidate PYTHONPATH; retain installed user-site dependencies.
    probe = r'''
import importlib, inspect, json, sys
from pathlib import Path
code = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(code))
modules = {}
for name in ('sniper.services.memecoin_funded_policy', 'sniper.services.memecoin_exit_intent',
             'sniper.services.memecoin_manual_exit_intents', 'sniper.services.memecoin_real_runner',
             'sniper.persistence.db_memecoin', 'sniper.execution.solana_exit_executor',
             'sniper.execution.evm_exit_executor'):
    module = importlib.import_module(name)
    assert Path(module.__file__).resolve().is_relative_to(code), 'candidate import escaped code root'
    modules[name.rsplit('.', 1)[-1]] = module
fp, ei = modules['memecoin_funded_policy'], modules['memecoin_exit_intent']
assert fp.SOURCE_MIRROR_TP100_POLICY_ID == 'source-mirror-tp100-v1'
policy = fp.source_mirror_tp100_policy(coin_band='small')
assert fp._validated_policy_data(policy.to_dict()) == policy
from sniper.services.memecoin_market_state import EntryClassification
classification = EntryClassification(1, 'solana', 'probe', 'onchain',
    '2026-10-20T00:00:00+00:00', 'small', 'young', 'balanced', 'climbing', 'unknown',
    'complete', 1.0, (), {})
contract = fp.build_funded_contract(classification=classification, base_clip_usd=1,
    approved_clip_usd=1, policy=policy, origin_event_id='probe', source_mirror_tp100={
        'policy_id': fp.SOURCE_MIRROR_TP100_POLICY_ID, 'source_user_id': 'probe', 'network_id': 'solana'})
encoded, digest = fp.canonical_funded_contract(contract)
position = {'source_user_id': 'probe', 'network_id': 'solana', 'mint': 'probe',
    'funded_contract_version': 1, 'funded_contract_hash': digest, 'funded_contract_json': encoded}
assert fp.is_source_mirror_tp100_position(position)
for position in json.load(sys.stdin):
    assert fp.is_source_mirror_tp100_position(position), 'candidate cannot read live profile'
assert ei.PARTIAL == 'partial'
position = {'id': 1, 'is_open': 1, 'derisk_claim_id': 'probe', 'controller_revision': 0}
intent = ei.build_exit_intent(position, claim_id='probe', action=ei.PARTIAL, fraction=.25,
    source_request_id='probe', balance_snapshot_atomic='4', sell_decimals=0)
restored = ei.ExitIntent.from_json(intent.to_json())
assert restored is not None and restored.action == ei.PARTIAL and restored.source_request_id == 'probe'
assert ei.verify_exit_intent(restored, position, balance_atomic=4) is None
assert ei.verify_exit_intent(restored, position, balance_atomic=5) == 'balance_changed'
for name in ('enqueue_source_partial', 'drain_source_partials', 'start_source_partial_worker'):
    assert callable(getattr(modules['memecoin_manual_exit_intents'], name, None))
assert callable(getattr(modules['memecoin_real_runner'], 'recover_source_partials', None))
for name in ('claim_source_partial', 'release_source_partial', 'mark_source_partial_submitted',
             'reset_source_partial_submission', 'book_source_partial_fill'):
    assert callable(getattr(modules['db_memecoin'], name, None))
for module, name in (('solana_exit_executor', 'execute_partial_exit'),
                     ('evm_exit_executor', 'execute_partial_exit_evm')):
    assert 'source_request' in inspect.signature(getattr(modules[module], name)).parameters
print('source-mirror-capable')
'''
    env.update(SNIPER_STATE_ROOT=str(state), DB_PATH=str(ledger), PYTHONPATH=str(code))
    try:
        result = subprocess.run([sys.executable, '-P', '-B', '-c', probe, str(code)],
                                cwd=code, env=env, input=json.dumps(profiles), text=True,
                                capture_output=True, check=False, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return ['source_mirror_candidate_probe_failed']
    return ([] if result.returncode == 0 and result.stdout.strip() == 'source-mirror-capable'
            else ['source_mirror_candidate_incapable'])


def verify_release(code_root, state_root, manifest, config_validator=None, manifest_path=None,
                   provider_env=None, provider_preflight=None, allow_shyft_unavailable=False):
    code = Path(code_root).expanduser().resolve()
    state = Path(state_root).expanduser().resolve()
    errors = []
    if provider_env is not None or provider_preflight is not None:
        if provider_env is None or provider_preflight is None:
            errors.append("provider stage requires environment and preflight")
        else:
            errors.extend(provider_stage_errors(state, provider_env, provider_preflight))
    sha = None
    if not isinstance(manifest, dict):
        return Verification(False, str(code), str(state), None, ("deploy manifest missing or invalid",))
    manifest_sha = str(manifest.get("sha") or "")
    manifest_release = Path(str(manifest.get("release_path") or "")).expanduser().resolve()
    manifest_state = Path(str(manifest.get("state_root") or "")).expanduser().resolve()
    if not FULL_SHA.fullmatch(manifest_sha):
        errors.append("manifest SHA is not a full commit SHA")
    if manifest_release != code:
        errors.append("manifest release path mismatch")
    if manifest_state != state:
        errors.append("manifest state root mismatch")
    if not code.is_dir():
        errors.append("release path missing or dangling")
    if not state.is_dir():
        errors.append("state root missing")
    try:
        state.relative_to(code)
        errors.append("mutable state path is inside release")
    except ValueError:
        pass
    required = (state / ".fomo.env", state / "sniper_tracker.db", state / "data", state / "logs")
    for path in required:
        if not path.exists():
            errors.append(f"required state path missing: {path}")
        else:
            try:
                path.resolve().relative_to(code)
                errors.append("mutable state path is inside release")
            except ValueError:
                pass
    worker = code / "scripts" / "memecoin_cluster_gate_worker.py"
    if not worker.is_file():
        errors.append("cluster worker is missing from the pinned release")
    for cluster_path in (
        state / "data" / CLUSTER_SOCKET_NAME,
        state / "data" / CLUSTER_DB_NAME,
    ):
        try:
            cluster_path.resolve().relative_to(code)
            errors.append("cluster mutable path is inside release")
        except ValueError:
            pass
    if code.is_dir():
        rc, head, _stderr = _git(code, "rev-parse", "HEAD")
        if rc:
            errors.append("release is not a Git worktree")
        else:
            sha = head
            if head != manifest_sha:
                errors.append("release HEAD does not match manifest SHA")
        rc, dirty, _stderr = _git(code, "status", "--porcelain", "--untracked-files=no")
        if rc or dirty:
            errors.append("release has tracked changes")
    # A present TPU sidecar object is validated against the on-disk binary + release Cargo.lock. Its
    # absence is the deploy script's concern (it builds + records one before stopping the old runner).
    if isinstance(manifest.get("sidecar"), dict):
        errors.extend(verify_sidecar(manifest.get("sidecar"), code, manifest_sha))
    errors.extend(verify_lifi_manifest(manifest, code))
    if not errors:
        errors.extend(source_mirror_capability_errors(code, state))
    if not errors:
        try:
            if config_validator is None:
                _default_config_validator(code, state, manifest_path, provider_env, allow_shyft_unavailable)
            else:
                config_validator(code, state)
        except Exception as exc:
            errors.append(f"real configuration invalid: {exc}")
    return Verification(not errors, str(code), str(state), sha, tuple(dict.fromkeys(errors)))


def baseline_terms(path):
    """The --shyft-runtime-only terms the OLD runner already failed before the deploy's trip, read
    from `path` (a JSON list of strings, written by the deploy's snapshot). Anything unreadable or
    malformed is the empty set: no term is excused, the gate is strict as before."""
    try:
        terms = json.loads(Path(path).read_text())
    except (OSError, ValueError, TypeError):
        return frozenset()
    if not isinstance(terms, list) or not all(isinstance(t, str) for t in terms):
        return frozenset()
    return frozenset(terms)


# Provider-availability terms whose cause is outside our release: the vendor's own stream. Only these
# may be excused AFTER THE SWITCH when the old runner already failed them (Gamma B1 on 00459ca48): a
# process, graph, generation, configuration or TPU term is ours, and a snapshot that caught it failing
# (an RH feed mid-restart) must not let a release whose own process never starts land. Add a term only
# with evidence that its cause is the vendor. The rollback (scope "all") excuses the whole baseline:
# the restored old release against its own pre-deploy terms is the status quo.
VENDOR_TERMS = frozenset({"shyft_runtime_prepare_unready"})


def excusable_terms(baseline, scope):
    return frozenset(baseline) if scope == "all" else frozenset(baseline) & VENDOR_TERMS


def split_preexisting(errors, baseline):
    """(fresh, preexisting): a term the old runner already failed before the trip is preexisting
    (the deploy pages it once and does not refuse, 2026-10-16: 3a2b415a8 was refused and its
    rollback held entries on a RabbitStream outage the old runner had too); any other term is
    fresh and refuses as before."""
    return ([e for e in errors if e not in baseline], [e for e in errors if e in baseline])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--code-root", required=True)
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--manifest")
    parser.add_argument("--provider-env")
    parser.add_argument("--provider-preflight")
    parser.add_argument("--check-provider-stage", action="store_true")
    parser.add_argument("--watcher-only", action="store_true")
    parser.add_argument("--allow-shyft-unavailable-custody", action="store_true")
    parser.add_argument("--rollback-only", action="store_true")
    parser.add_argument("--startup-since", type=float, default=0)
    parser.add_argument("--allow-watcher-overlap", action="store_true")
    parser.add_argument("--watcher-primary-only", action="store_true")
    parser.add_argument("--watcher-candidate-pid", type=int)
    parser.add_argument("--watcher-candidate-birth")
    parser.add_argument("--watcher-record-pid", type=int)
    parser.add_argument("--retire-watcher-record")
    parser.add_argument("--shyft-runtime-only", action="store_true")
    parser.add_argument("--source-mirror-only", action="store_true")
    parser.add_argument("--baseline-errors")
    parser.add_argument("--baseline-scope", choices=("vendor", "all"), default="vendor")
    parser.add_argument("--process-record-script")
    parser.add_argument("--record-expected-root")
    parser.add_argument("--stop-process-record")
    parser.add_argument("--rh-process-only", action='store_true')
    parser.add_argument("--abort-watcher-record")
    args = parser.parse_args()
    if args.abort_watcher_record:
        try:
            ok = abort_watcher_handover(args.code_root, args.state_root,
                json.loads(Path(args.abort_watcher_record).read_text()))
        except (OSError, ValueError):
            ok = False
        raise SystemExit(0 if ok else 1)
    if args.process_record_script:
        try:
            record = owned_script_record(args.state_root, args.process_record_script,
                                          expected_root=args.record_expected_root)
        except ValueError:
            raise SystemExit(1)
        print(json.dumps(record))
        raise SystemExit(0)
    if args.stop_process_record:
        try:
            record = json.loads(Path(args.stop_process_record).read_text())
            suffix = record['suffix']
            if suffix not in {'scripts/memecoin_rh_feed_shadow_loop.py', 'scripts/research/onchain_wallet_watch.py'}:
                raise ValueError()
            ok = retire_old_watchers(args.state_root, record, script_suffix=suffix, wait_s=5)
        except (OSError, ValueError, TypeError, KeyError):
            ok = False
        raise SystemExit(0 if ok else 1)
    if args.watcher_record_pid is not None:
        try:
            record = watcher_handover_record(args.code_root, args.state_root, args.watcher_record_pid)
        except ValueError:
            raise SystemExit(1)
        print(json.dumps(record))
        raise SystemExit(0)
    if args.retire_watcher_record:
        try:
            record = json.loads(Path(args.retire_watcher_record).read_text())
            pid, path, birth = record['candidate']
            errors = verify_shyft_watcher(args.code_root, args.state_root, allow_overlap=True,
                primary_only=True, candidate_pid=pid, candidate_birth=birth)
            expected = str((Path(args.code_root) / 'scripts/research/onchain_wallet_watch.py').resolve())
            ok = path == expected and not errors and retire_old_watchers(args.state_root, record)
        except (OSError, ValueError, TypeError, KeyError):
            ok = False
        raise SystemExit(0 if ok else 1)
    if (args.check_provider_stage or args.watcher_only or args.rollback_only or args.shyft_runtime_only
            or args.rh_process_only or args.source_mirror_only):
        if args.check_provider_stage:
            if not args.provider_env or not args.provider_preflight:
                parser.error("provider stage requires environment and preflight")
            errors = provider_stage_errors(args.state_root, args.provider_env, args.provider_preflight)
        elif args.source_mirror_only:
            errors = source_mirror_capability_errors(args.code_root, args.state_root)
        elif args.rollback_only:
            errors = verify_provider_rollback_processes(args.code_root, args.state_root, args.startup_since)
        elif args.shyft_runtime_only:
            errors = verify_shyft_runtime_graph(args.code_root, args.state_root)
            if args.baseline_errors:
                errors, preexisting = split_preexisting(
                    errors, excusable_terms(baseline_terms(args.baseline_errors), args.baseline_scope))
                print(json.dumps({"ok": not errors, "errors": errors, "preexisting": preexisting},
                                 sort_keys=True))
                raise SystemExit(1 if errors else 0)
        elif args.rh_process_only:
            errors = verify_rh_process(args.code_root, args.state_root)
        else:
            errors = verify_shyft_watcher(args.code_root, args.state_root,
                allow_overlap=args.allow_watcher_overlap, primary_only=args.watcher_primary_only,
                candidate_pid=args.watcher_candidate_pid, candidate_birth=args.watcher_candidate_birth)
        print(json.dumps({"ok": not errors, "errors": errors}, sort_keys=True))
        raise SystemExit(1 if errors else 0)
    manifest_path = args.manifest or str(Path(args.state_root) / "data" / "memecoin_deploy.json")
    result = verify_release(
        args.code_root, args.state_root, load_manifest(manifest_path),
        manifest_path=manifest_path,
        provider_env=args.provider_env, provider_preflight=args.provider_preflight,
        allow_shyft_unavailable=args.allow_shyft_unavailable_custody,
    )
    print(json.dumps(asdict(result), sort_keys=True))
    raise SystemExit(0 if result.ok else 1)


if __name__ == "__main__":
    main()
