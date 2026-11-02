"""Phase 7 review M2 — inter-process lock for `run_analysis_pipeline`.

Asserts:

* Two concurrent invocations against the same lock path cannot both
  enter the pipeline body. Default `lock_block=False` raises
  `PipelineLockBusyError` immediately.
* `lock_block=True` queues — the second caller proceeds once the first
  releases.
* The lock is released on exception (try/finally semantics in the
  context manager).

The pipeline body itself is heavy (DuckDB + analyze_wallet); these
tests stub `run_analysis_pipeline`'s downstream callees so the focus
stays on the lock contract. A separate synchronous test exercises
`acquire_pipeline_lock` directly with two file descriptors so we don't
depend on subprocess machinery for the simple case.
"""
from __future__ import annotations

import multiprocessing
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from sniper.analysis.wallet_protocol.ledger.pipeline_lock import (
    PipelineLockBusyError,
    acquire_pipeline_lock,
)


# ---------------------------------------------------------------------------
# Direct lock-primitive tests (no pipeline involvement)
# ---------------------------------------------------------------------------


def test_fail_fast_when_other_holder(tmp_path: Path) -> None:
    """Default block=False raises PipelineLockBusyError if held."""
    lock_path = tmp_path / "pipeline.lock"
    with acquire_pipeline_lock(lock_path=lock_path):
        with pytest.raises(PipelineLockBusyError):
            with acquire_pipeline_lock(lock_path=lock_path):
                pytest.fail("second acquire should have failed")  # pragma: no cover


def test_lock_released_on_normal_exit(tmp_path: Path) -> None:
    lock_path = tmp_path / "pipeline.lock"
    with acquire_pipeline_lock(lock_path=lock_path):
        pass
    # Lock released → second acquire succeeds without blocking.
    with acquire_pipeline_lock(lock_path=lock_path):
        pass


def test_lock_released_on_exception(tmp_path: Path) -> None:
    lock_path = tmp_path / "pipeline.lock"

    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        with acquire_pipeline_lock(lock_path=lock_path):
            raise _Boom("simulated mid-run failure")

    # Lock released on exception → second acquire succeeds.
    with acquire_pipeline_lock(lock_path=lock_path):
        pass


def test_env_var_path_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SNIPER_LEDGER_PIPELINE_LOCK env var is honoured when no explicit
    `lock_path` is passed."""
    custom = tmp_path / "custom-pipeline.lock"
    monkeypatch.setenv("SNIPER_LEDGER_PIPELINE_LOCK", str(custom))
    with acquire_pipeline_lock():
        assert custom.exists(), "lock file should be created at env-var path"


# ---------------------------------------------------------------------------
# Concurrent two-process test — fcntl.flock is per-fd so we need real
# child processes to validate cross-process exclusion (in-process
# duplicate-acquire would not exercise the kernel lock advisory layer
# with the same realism).
# ---------------------------------------------------------------------------


def _hold_lock(lock_path: str, hold_seconds: float, ready_path: str) -> None:
    """Child process: acquire lock, signal ready, hold for N seconds."""
    with acquire_pipeline_lock(lock_path=lock_path):
        Path(ready_path).touch()
        time.sleep(hold_seconds)


def test_two_processes_cannot_both_hold(tmp_path: Path) -> None:
    """Cross-process: while child A holds the lock, child B's fail-fast
    acquire must raise PipelineLockBusyError."""
    lock_path = tmp_path / "pipeline.lock"
    ready_path = tmp_path / "child_ready"

    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(
        target=_hold_lock,
        args=(str(lock_path), 2.0, str(ready_path)),
    )
    proc.start()
    try:
        # Wait until the child has acquired the lock.
        deadline = time.monotonic() + 5.0
        while not ready_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready_path.exists(), "child failed to acquire lock"

        # Parent: fail-fast acquire must raise.
        with pytest.raises(PipelineLockBusyError):
            with acquire_pipeline_lock(lock_path=lock_path):
                pytest.fail("parent acquired while child held lock")  # pragma: no cover
    finally:
        proc.join(timeout=10.0)
        if proc.is_alive():  # pragma: no cover
            proc.terminate()
            proc.join(timeout=2.0)

    # After child exits, parent can acquire.
    with acquire_pipeline_lock(lock_path=lock_path):
        pass


def test_block_mode_queues_until_release(tmp_path: Path) -> None:
    """block=True queues — the second caller proceeds once the first
    releases. Use a child holding the lock briefly; the parent's
    blocking acquire must succeed AFTER the child releases."""
    lock_path = tmp_path / "pipeline.lock"
    ready_path = tmp_path / "child_ready"
    hold_for = 0.4

    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(
        target=_hold_lock,
        args=(str(lock_path), hold_for, str(ready_path)),
    )
    proc.start()
    try:
        deadline = time.monotonic() + 5.0
        while not ready_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready_path.exists(), "child failed to acquire lock"

        # Blocking acquire from parent. Should not raise; should block
        # until the child releases (~hold_for seconds).
        start = time.monotonic()
        with acquire_pipeline_lock(lock_path=lock_path, block=True):
            elapsed = time.monotonic() - start
        assert elapsed >= 0.05, (
            "blocking acquire returned suspiciously fast; expected to wait "
            "for child release"
        )
    finally:
        proc.join(timeout=10.0)
        if proc.is_alive():  # pragma: no cover
            proc.terminate()
            proc.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Integration: run_analysis_pipeline honours the lock
# ---------------------------------------------------------------------------


def test_run_pipeline_releases_lock_on_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run_analysis_pipeline` must release the lock when its body
    raises — otherwise a single failure would wedge subsequent runs."""
    from sniper.analysis.wallet_protocol.ledger import run_pipeline as rp_mod
    from sniper.analysis.wallet_protocol.ledger.run_pipeline import (
        run_analysis_pipeline,
    )
    from sniper.analysis.wallet_protocol.ledger.window import AnalysisWindow

    lock_path = tmp_path / "pipeline.lock"

    window = AnalysisWindow(
        window_id="w1",
        capture_run_ids=("run-a",),
        start_ts=1_700_000_000,
        end_ts=1_700_086_400,
        source_versions=(("price_v", "v1"),),
    )

    class _Boom(RuntimeError):
        pass

    def _explode(**_: object) -> None:
        raise _Boom("simulated materializer failure")

    monkeypatch.setattr(rp_mod, "materialize_wallet_data", _explode)

    with pytest.raises(_Boom):
        run_analysis_pipeline(
            wallet="0x" + "a" * 40,
            window=window,
            ledger_root=tmp_path,
            run_id="run-a",
            lock_path=lock_path,
        )

    # Lock must be released → a second invocation can re-acquire (and
    # itself raise from the still-broken materializer, which proves the
    # lock cleanup happened).
    with pytest.raises(_Boom):
        run_analysis_pipeline(
            wallet="0x" + "a" * 40,
            window=window,
            ledger_root=tmp_path,
            run_id="run-a",
            lock_path=lock_path,
        )
