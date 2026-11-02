"""Live state is DERIVED every time and never stored (spec 1.2, R13). Read-only.

Knob values follow the order the RUNNER uses, and each value names its source (plan review
finding 21). A raw `.env` read is not the runner's value:
  1. the cockpit entry-knob overlay (memecoin_entry_knob_overrides, active rows), which the
     runner applies once per entry decision;
  2. the runner's own process environment (/proc/<pid>/environ: the watchdog's exports), which
     load_env_files never overrides;
  3. `.env` then `.fomo.env`, FIRST occurrence wins, quotes stripped (the runner's own loader,
     sniper/util/envfiles.load_env_files);
  4. the settings module's code default.
The state root is SNIPER_STATE_ROOT, else the MAIN checkout of the repo. A private worktree has no
.env, .runtime or data/, so reading those from it would report every live value UNKNOWN."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

SECRET_NAME = re.compile(r"URL|KEY|TOKEN|SECRET|PRIVATE|PASS|SEED", re.I)


def _run(args, cwd, timeout=10):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def state_root(repo) -> Path:
    env = os.getenv("SNIPER_STATE_ROOT")
    if env:
        return Path(env)
    r = _run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], repo)
    if r.returncode != 0:
        return Path(repo)
    return Path(r.stdout.strip()).resolve().parent


ENV_FILES = (".env", ".fomo.env")


def _env_files(root: Path) -> dict:
    """{name: (value, file)} with the runner's loader and order: earlier file wins."""
    from sniper.util.envfiles import load_env_files
    out = {}
    for f in ENV_FILES:
        vals = {}
        load_env_files(str(root), (f,), env=vals)
        for k, v in vals.items():
            out.setdefault(k, (v, f))
    return out


def _shown(v):
    # A value is hidden when it looks like a credential, whatever its name (Lambda review L4).
    from bot.brain_graph.gate import contains_secret
    t = str(v)
    return "(hidden: secret-shaped value)" if "://" in t or contains_secret(t, ()) else v


def knob_values(root: Path, names, *, overrides=None, runner_env=None, settings=None) -> dict:
    """{name: (value, source)}. Secret-looking names are never read."""
    files = _env_files(Path(root))
    overrides, runner_env = overrides or {}, runner_env or {}
    out = {}
    for n in names:
        if SECRET_NAME.search(n):
            out[n] = ("(secret, name only)", "-")
        elif n in overrides:
            out[n] = (overrides[n], "cockpit overlay")
        elif n in runner_env:
            out[n] = (_shown(runner_env[n]), "runner process env")
        elif n in files:
            out[n] = (_shown(files[n][0]), files[n][1])
        elif settings is not None and hasattr(settings, n):
            out[n] = (getattr(settings, n), "code default")
        else:
            out[n] = ("(unset)", "-")
    return out


def pending_restart(env_paths, started_at):
    """True when any env file changed after the runner started (the process loaded them at start)."""
    if not started_at:
        return None
    started = datetime.fromisoformat(str(started_at))
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    mtime = max(Path(p).stat().st_mtime for p in env_paths if Path(p).exists())
    return datetime.fromtimestamp(mtime, timezone.utc) > started


def deploying(lines) -> bool:
    """A real deploy invocation (`bash .../deploy_memecoin_runtime.sh ...`), matched on its SHAPE, so a
    shell, grep or snapshot that merely names the script does not count."""
    return any(re.match(r"^\S*(bash|sh)\s+\S*deploy_memecoin_runtime\.sh\b", line) for line in lines)


def _runner_env(root: Path) -> dict:
    pid = int((root / "data" / "memecoin_runner.pid").read_text().strip())
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    return dict(kv.split("=", 1) for kv in raw.decode(errors="replace").split("\0") if "=" in kv)


def _overrides(root: Path) -> dict:
    # The runner's own reader: only ADJUSTABLE names apply, one bad row is skipped (Lambda review L2).
    from sniper.persistence.db_core import get_readonly_connection
    from sniper.services.memecoin_entry_knobs import read_overrides
    with closing(get_readonly_connection(str(root / "sniper_tracker.db"))) as c:
        return read_overrides(c)


def derive(repo, *, knobs=()) -> dict:
    root = state_root(repo) if Path(repo).exists() else Path(repo)
    d, errors = {"state_root": str(root)}, []

    def guard(key, fn):
        try:
            d[key] = fn()
        except Exception as exc:
            d[key] = None
            errors.append(f"{key}: {type(exc).__name__}")

    def git(*a):
        r = _run(["git", *a], root)
        if r.returncode != 0:
            raise RuntimeError(r.stderr[:80])
        return r.stdout

    guard("origin_main", lambda: git("rev-parse", "--short", "origin/main").strip())
    guard("head", lambda: git("rev-parse", "--short", "HEAD").strip())
    guard("unpushed", lambda: git("log", "--oneline", "origin/main..HEAD").splitlines())
    guard("tracked_changes", lambda: len([l for l in git("status", "--porcelain", "--untracked-files=no")
                                          .splitlines() if l]))
    guard("runner_release", lambda: os.path.basename(os.readlink(root / ".runtime/memecoin/current"))[:8])
    guard("web_release", lambda: os.path.basename(os.readlink(root / ".runtime/web/current"))[:8])

    def hb():
        h = json.loads((root / "data/memecoin_runner.heartbeat").read_text())
        return round(time.time() - float(h["ts"]), 1), bool(h.get("auth_ok")), h.get("started_at")
    guard("_hb", hb)
    d["heartbeat_age_s"], d["auth_ok"], d["runner_started_at"] = d.pop("_hb") or (None, None, None)

    def procs():
        lines = _run(["ps", "-eo", "args"], root).stdout.splitlines()
        rel = {}
        for m in re.findall(r"memecoin/releases/([0-9a-f]{8})", "\n".join(lines)):
            rel[m] = rel.get(m, 0) + 1
        return rel, deploying(lines)
    guard("_ps", procs)
    d["process_releases"], d["deploy_running"] = d.pop("_ps") or (None, None)

    def book():
        from sniper.persistence.db_core import get_readonly_connection
        with closing(get_readonly_connection(str(root / "sniper_tracker.db"))) as c:
            return c.execute("SELECT COUNT(*) FROM memecoin_positions WHERE is_open=1 "
                             "AND execution_mode='real'").fetchone()[0]
    guard("open_real_positions", book)
    if knobs:
        guard("_ov", lambda: _overrides(root))
        guard("_renv", lambda: _runner_env(root))

        def settings():
            import sniper.settings.memecoin as m
            return m
        guard("_settings", settings)
        guard("knobs", lambda: knob_values(root, list(knobs), overrides=d.get("_ov"),
                                           runner_env=d.get("_renv"), settings=d.get("_settings")))
        for k in ("_ov", "_renv", "_settings"):
            d.pop(k, None)
    else:
        d["knobs"] = {}
    guard("pending_restart", lambda: pending_restart([root / f for f in ENV_FILES], d.get("runner_started_at")))
    d["errors"] = errors
    return d


def render(d: dict) -> str:
    def v(k):
        return "UNKNOWN" if d.get(k) is None else d[k]
    lines = [
        f"git: origin/main {v('origin_main')}, HEAD {v('head')}, unpushed {len(d.get('unpushed') or [])}, "
        f"tracked changes {v('tracked_changes')}",
        f"releases: runner {v('runner_release')}, web {v('web_release')}; processes {v('process_releases')}; "
        f"deploy running {v('deploy_running')}",
        f"runner heartbeat age {v('heartbeat_age_s')} s, auth_ok {v('auth_ok')}; open real positions "
        f"{v('open_real_positions')}; env files newer than runner start (pending restart): {v('pending_restart')}",
    ]
    lines += [f"  unpushed: {u}" for u in (d.get("unpushed") or [])[:5]]
    lines += [f"  knob {k} = {val} [{src}]" for k, (val, src) in (d.get("knobs") or {}).items()]
    if d.get("errors"):
        lines.append("live-state read errors: " + "; ".join(d["errors"]))
    return "\n".join(lines)
