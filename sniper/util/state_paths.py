"""Explicit mutable-state paths for the pinned memecoin runtime."""
from __future__ import annotations

import os
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[2]


def state_root(env=None) -> Path:
    env = os.environ if env is None else env
    raw = env.get("SNIPER_STATE_ROOT")
    if not raw:
        return SOURCE_ROOT
    root = Path(raw).expanduser()
    if not root.is_absolute():
        raise ValueError("SNIPER_STATE_ROOT must be absolute")
    return root.resolve()


def require_state_root_outside_release(env=None) -> Path:
    """The state root, refusing one inside a pinned release (Gamma, 2026-10-21). Without
    SNIPER_STATE_ROOT, state_root() is the source tree, which for `cd .runtime/memecoin/current &&
    python3 scripts/...` is the RELEASE: no .env is read, and anything written lands in a directory the
    next deploy replaces (the runtime verifier's "mutable state path is inside release"). Desk entry
    points call this first."""
    root = state_root(env)
    if ".runtime" in root.parts:
        raise SystemExit("refused: state root %s is inside a pinned release (.runtime/); "
                         "set SNIPER_STATE_ROOT=/home/bigpapi/Desktop/Sniper" % root)
    return root


def state_path(*parts, env=None) -> Path:
    root = state_root(env)
    if any(Path(str(part)).is_absolute() for part in parts):
        raise ValueError("state path parts cannot be absolute or escape state root")
    result = root.joinpath(*(str(part) for part in parts)).resolve()
    try:
        result.relative_to(root)
    except ValueError:
        raise ValueError("state path cannot escape state root") from None
    return result


def configure_memecoin_state_env(env=None):
    env = os.environ if env is None else env
    defaults = {
        "DB_PATH": state_path("sniper_tracker.db", env=env),
        "MEMECOIN_RUN_LOCK": state_path("data", "memecoin_runner.lock", env=env),
        "MEMECOIN_HEARTBEAT_PATH": state_path("data", "memecoin_runner.heartbeat", env=env),
        "MEMECOIN_FEED_SNAPSHOT_PATH": state_path("data", "memecoin_feed.json", env=env),
        "SNIPER_CLOCK_OFFSET_FILE": state_path("data", "clock_offset.json", env=env),
        "FOMO_TOKEN_STORE": state_path(".fomo_tokens.json", env=env),
        "MEMECOIN_KILL_SWITCH_FILE": state_path("data", "memecoin_kill_switch", env=env),
        "MEMECOIN_SOLANA_KEYPAIR": state_path("data", "solana_keypair.json", env=env),
        "MEMECOIN_EVM_KEYPAIR": state_path("data", "evm_keypair.json", env=env),
        "MEMECOIN_PIDFILE": state_path("data", "memecoin_runner.pid", env=env),
        "MEMECOIN_RUNNER_LOG": state_path("logs", "memecoin_runner.log", env=env),
        "MEMECOIN_WATCHDOG_LOG": state_path("logs", "memecoin_watchdog.log", env=env),
    }
    for key, value in defaults.items():
        env.setdefault(key, str(value))
    return env
