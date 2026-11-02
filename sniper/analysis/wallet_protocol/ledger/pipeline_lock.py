"""Inter-process lock for the ledger run pipeline.

The ledger pipeline opens DuckDB warehouses for write access via the
materializer + writers chain. DuckDB is single-writer per file; two
concurrent `run_analysis_pipeline` invocations against the same
ledger root can corrupt the warehouse or trigger lock contention
errors mid-run.

This module provides a Linux `fcntl.flock` based file lock that is
held for the duration of the pipeline run. Default behaviour is
**fail-fast** (raise immediately if another writer holds the lock)
to surface concurrency bugs in CI / cron rather than silently
serialising. Callers that want to queue can opt in via `block=True`.

The lock is released on normal exit AND on exception (context-manager
contract via `try/finally`).

Lock path resolution order when `acquire_pipeline_lock` is called
directly:

1. `lock_path` argument to `acquire_pipeline_lock`.
2. `SNIPER_LEDGER_PIPELINE_LOCK` env var (intended for local dev /
   tests where `/var/lock` is unwritable).
3. Module default `DEFAULT_LOCK_PATH` (`/var/lock/sniper-ledger-pipeline.lock`)
   for VPS production.

`run_analysis_pipeline` passes `ledger_root/.sniper-ledger-pipeline.lock`
unless an explicit lock path is provided. That keeps test/dev runs
unprivileged while still locking every writer for the same ledger root.

Linux-only — `fcntl` is unavailable on Windows. The pipeline runs on
the VPS (Linux) and dev macOS / Linux laptops only.
"""
from __future__ import annotations

import errno
import fcntl
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

DEFAULT_LOCK_PATH = "/var/lock/sniper-ledger-pipeline.lock"
LOCK_PATH_ENV_VAR = "SNIPER_LEDGER_PIPELINE_LOCK"


class PipelineLockBusyError(RuntimeError):
    """Raised in fail-fast mode when another writer holds the lock."""


def _resolve_lock_path(lock_path: str | os.PathLike[str] | None) -> Path:
    if lock_path is not None:
        return Path(lock_path)
    env_override = os.environ.get(LOCK_PATH_ENV_VAR)
    if env_override:
        return Path(env_override)
    return Path(DEFAULT_LOCK_PATH)


@contextmanager
def acquire_pipeline_lock(
    *,
    lock_path: str | os.PathLike[str] | None = None,
    block: bool = False,
) -> Iterator[Path]:
    """Acquire the ledger pipeline writer lock.

    Default `block=False` raises `PipelineLockBusyError` immediately if
    another process holds the lock — preferred for CI / cron so that
    overlapping invocations surface as a hard failure rather than a
    silent queue.

    `block=True` blocks until the lock is available — useful for
    operator scripts that explicitly want to queue.

    The lock file is created (mode 0644) if it does not exist and
    survives across runs — `fcntl.flock` releases on fd close, not on
    file unlink, so we keep the file in place.

    Released on normal exit AND on exception via try/finally.
    """
    resolved = _resolve_lock_path(lock_path)
    resolved.parent.mkdir(parents=True, exist_ok=True)

    flags = os.O_RDWR | os.O_CREAT
    fd = os.open(resolved, flags, 0o644)
    try:
        op = fcntl.LOCK_EX
        if not block:
            op |= fcntl.LOCK_NB
        try:
            fcntl.flock(fd, op)
        except OSError as exc:
            if not block and exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise PipelineLockBusyError(
                    f"Another ledger pipeline writer holds the lock at {resolved}. "
                    "Default behaviour is fail-fast — pass block=True to queue."
                ) from exc
            raise
        logger.debug("acquired pipeline lock at %s (block=%s)", resolved, block)
        try:
            yield resolved
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                # Best-effort unlock; closing the fd below also releases.
                logger.debug("flock(LOCK_UN) failed at %s; relying on fd close", resolved)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


__all__ = [
    "DEFAULT_LOCK_PATH",
    "LOCK_PATH_ENV_VAR",
    "PipelineLockBusyError",
    "acquire_pipeline_lock",
]
