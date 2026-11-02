"""Unsupported owner frames retain diagnostics without gaining admission authority."""
import pytest

from sniper.services import memecoin_shyft_prepare as prep
from test_memecoin_shyft_prepare import OWNER, packet


def test_rejected_candidate_hook_keeps_exact_instructions_and_old_miss_contract():
    update = packet()
    update.transaction.transaction.transaction.message.instructions[0].data = b"unsupported-shape"
    rejected, misses = [], []
    result = prep.preparation_candidate(
        update, {OWNER: ("uid", "h")}, recv_monotonic=1.0, generation=1,
        on_owner_miss=lambda sig, owner, reason: misses.append((sig, owner, reason)),
        on_rejected_candidate=rejected.append)
    assert result is None and len(rejected) == 1
    assert rejected[0]["instructions"][0][2] == b"unsupported-shape"
    assert rejected[0]["owner"] == OWNER
    assert misses == [(rejected[0]["signature"], OWNER, "unsupported_target")]


@pytest.mark.parametrize("kind", ["supported", "unowned"])
def test_only_unsupported_proven_owner_frames_reach_diagnostic_hook(kind):
    rejected = []
    update = packet()
    owners = {OWNER: ("uid", "h")} if kind == "supported" else {}
    result = prep.preparation_candidate(
        update, owners, recv_monotonic=1.0, generation=1,
        on_rejected_candidate=rejected.append)
    assert (result is not None) == (kind == "supported")
    assert rejected == []


def test_diagnostic_failure_cannot_change_original_refusal():
    update = packet()
    update.transaction.transaction.transaction.message.instructions[0].data = b"unsupported-shape"
    stats = {}

    def fail(_candidate):
        raise RuntimeError("diagnostic failed")

    assert prep.preparation_candidate(
        update, {OWNER: ("uid", "h")}, recv_monotonic=1.0, generation=1, stats=stats,
        on_rejected_candidate=fail) is None
    assert stats == {"observer_errors": 1, "unsupported_target": 1}
