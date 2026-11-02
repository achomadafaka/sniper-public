"""Single source of truth for legacy-pipeline disable guards.

Env vars, either of which fires the guard:
- LEGACY_PIPELINE_DISABLED=1
- LEGACY_STREAM_FROZEN=1

Spec ref:
docs/superpowers/specs/2026-04-30-analyzer-completion-and-archive-design.md
"""

from __future__ import annotations

import os
import sys


_SPEC_POINTER = (
    "See docs/superpowers/specs/"
    "2026-04-30-analyzer-completion-and-archive-design.md for context. "
    "To opt out (not recommended): unset LEGACY_PIPELINE_DISABLED and "
    "LEGACY_STREAM_FROZEN."
)


class LegacyPipelineDisabledError(RuntimeError):
    """Raised when a legacy pipeline path is invoked while disabled."""


def _is_disabled() -> bool:
    return any(
        os.getenv(var) == "1"
        for var in ("LEGACY_PIPELINE_DISABLED", "LEGACY_STREAM_FROZEN")
    )


def assert_legacy_pipeline_enabled(name: str) -> None:
    """Raise when a legacy write path is called while the pipeline is disabled."""
    if _is_disabled():
        raise LegacyPipelineDisabledError(
            f"{name} called while LEGACY_PIPELINE_DISABLED=1. {_SPEC_POINTER}"
        )


def legacy_main_guard(name: str) -> int | None:
    """Return exit code 2 and print stderr when a legacy entrypoint is disabled."""
    if _is_disabled():
        print(
            f"Legacy pipeline entrypoint {name!r} blocked by "
            f"LEGACY_PIPELINE_DISABLED.\n{_SPEC_POINTER}",
            file=sys.stderr,
        )
        return 2
    return None
