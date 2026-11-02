"""Regression: replay_wallet_run must reconstruct --analyst-fast from
args_json so persisted runs idempotently round-trip.

Caught by reviewer on the G5 slice (2026-05-17). Without this, a persisted
run carrying analyst_fast=true in args_json would replay WITHOUT the flag,
and the replay's own args_json would record analyst_fast=false — breaking
idempotency of persisted metadata even though the analysis results agree
(because no_history/no_exec are wired separately).
"""
from __future__ import annotations

from scripts.replay_wallet_run import _argv_from_args_json
from scripts.run_wallet_protocol import _build_parser, _replay_args_json


WALLET = "0xdeadbeef"


def test_analyst_fast_round_trips_through_replay_argv():
    """args_json with analyst_fast=true → reconstructed argv contains --analyst-fast."""
    args = _build_parser().parse_args([WALLET, "--analyst-fast"])
    args_json = _replay_args_json(args)

    argv = _argv_from_args_json(args_json, WALLET)

    assert "--analyst-fast" in argv, (
        f"--analyst-fast missing from reconstructed argv: {argv}"
    )


def test_analyst_fast_false_does_not_emit_flag_in_replay_argv():
    """args_json with analyst_fast=false → reconstructed argv omits the flag."""
    args = _build_parser().parse_args([WALLET])
    args_json = _replay_args_json(args)

    argv = _argv_from_args_json(args_json, WALLET)

    assert "--analyst-fast" not in argv, (
        f"--analyst-fast should not appear when flag is unset: {argv}"
    )
