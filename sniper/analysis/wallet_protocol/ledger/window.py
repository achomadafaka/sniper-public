"""Slice 5 — `AnalysisWindow` dataclass.

Frozen, hashable identity for a materialized wallet view. Used as input to
`materialize_wallet_data(wallet, window, ledger_root)` and echoed back on
the resulting `MaterializedWalletData` for traceability.

The window is input only. All output meta (manifest hashes, row counts,
captured_at extents) lives on `MaterializedWalletData`, not on the window.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AnalysisWindow:
    """Immutable input. Hashable. Identifies a materialized wallet view.

    Fields:
        window_id: caller-supplied stable slug. Convention is
            `YYYY-MM-DD_<slug>_<vN>`. Required (non-empty).
        capture_run_ids: tuple of capture-run identifiers whose facts
            populate this window. Plural — a single analysis window may
            bridge several incremental capture runs that landed activity
            in `[start_ts, end_ts]`. Order matters for hashing and is
            preserved by the caller. Required (non-empty).
        start_ts: inclusive unix-epoch lower bound on facts.
        end_ts: inclusive unix-epoch upper bound on facts. Must satisfy
            `start_ts <= end_ts`.
        source_versions: tuple of `(key, value)` tuples — never a dict —
            so the dataclass stays hashable. Read-site code may convert
            to a dict via `dict(window.source_versions)` when a mapping
            is more convenient.
    """

    window_id: str
    capture_run_ids: tuple[str, ...]
    start_ts: int
    end_ts: int
    source_versions: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.window_id, str) or not self.window_id:
            raise ValueError("window_id must be a non-empty string")

        if not isinstance(self.capture_run_ids, tuple):
            raise TypeError(
                "capture_run_ids must be a tuple of strings (got "
                f"{type(self.capture_run_ids).__name__})"
            )
        if not self.capture_run_ids:
            raise ValueError("capture_run_ids must be non-empty")
        for run_id in self.capture_run_ids:
            if not isinstance(run_id, str) or not run_id:
                raise ValueError(
                    "capture_run_ids must contain non-empty strings"
                )

        if not isinstance(self.start_ts, int) or isinstance(self.start_ts, bool):
            raise TypeError("start_ts must be an int")
        if not isinstance(self.end_ts, int) or isinstance(self.end_ts, bool):
            raise TypeError("end_ts must be an int")
        if self.start_ts > self.end_ts:
            raise ValueError(
                f"start_ts={self.start_ts} must not exceed end_ts={self.end_ts}"
            )

        if not isinstance(self.source_versions, tuple):
            raise TypeError(
                "source_versions must be a tuple of (key, value) tuples (got "
                f"{type(self.source_versions).__name__})"
            )
        for entry in self.source_versions:
            if (
                not isinstance(entry, tuple)
                or len(entry) != 2
                or not isinstance(entry[0], str)
                or not isinstance(entry[1], str)
            ):
                raise TypeError(
                    f"source_versions entries must be (str, str) tuples; got {entry!r}"
                )


__all__ = ["AnalysisWindow"]
