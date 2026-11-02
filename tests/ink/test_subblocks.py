import json
from pathlib import Path

import pytest

from sniper.services.memecoin_ink_subblocks import InkSubblockState


@pytest.fixture
def recorded_frames():
    return json.loads(Path("docs/superpowers/research/2026-09-29-ink-evidence/subblock-sample.json").read_text())


def test_mid_build_frame_cannot_emit_candidate(recorded_frames):
    result = InkSubblockState().accept(
        recorded_frames[2], received_mono_ns=1, received_utc="2026-09-29T08:37:00Z")
    assert result["status"] == "waiting_base"
    assert result["observations"] == []


def test_recorded_base_and_continuation_bind_typed_receipt_and_signer(recorded_frames):
    state = InkSubblockState()
    state.accept(recorded_frames[0], received_mono_ns=1, received_utc="2026-09-29T08:37:00Z")
    state.accept(recorded_frames[1], received_mono_ns=2, received_utc="2026-09-29T08:37:00Z")
    result = state.accept(recorded_frames[2], received_mono_ns=3,
                          received_utc="2026-09-29T08:37:00Z")
    assert result["status"] == "accepted"
    assert len(result["observations"]) == 3
    assert result["observations"][0]["tx_hash"] == "0x182885ce449092e5333f7a257a0da846e8e133a367d1d408327dbdc067882769"
    assert result["observations"][0]["owner"].lower() == "0x73fd2e18730a0bf6bd28a20b084223e1bea8e0bf"
    assert result["observations"][0]["canonical_block_hash"] is None
    assert result["observations"][0]["canonical_log_index"] is None
    assert result["observations"][0]["source_timestamp"] == int(recorded_frames[0]["base"]["timestamp"], 16)


def test_duplicate_transport_frame_does_not_reemit(recorded_frames):
    state = InkSubblockState()
    first = state.accept(recorded_frames[0], received_mono_ns=1, received_utc="t")
    assert first["observations"] == []  # recorded base contains only a system deposit
    again = state.accept(recorded_frames[0], received_mono_ns=2, received_utc="t")
    assert again["status"] == "accepted"
    assert again["observations"] == []


def test_oversized_frame_is_malformed():
    result = InkSubblockState().accept("x" * 4_000_001, received_mono_ns=1, received_utc="t")
    assert result["status"] == "malformed"
    assert result["observations"] == []


def test_invalid_typed_transaction_is_malformed_without_crashing(recorded_frames):
    from eth_utils import keccak

    state = InkSubblockState()
    state.accept(recorded_frames[0], received_mono_ns=1, received_utc="t")
    state.accept(recorded_frames[1], received_mono_ns=2, received_utc="t")
    bad = json.loads(json.dumps(recorded_frames[2]))
    bad["diff"]["transactions"] = ["0x02"]
    bad["metadata"]["receipts"] = {
        "0x" + keccak(hexstr="0x02").hex(): {"type": "0x2", "status": "0x1", "logs": []}}
    result = state.accept(bad, received_mono_ns=3, received_utc="t")
    assert result["status"] == "malformed"
    assert result["observations"] == []


def test_gap_and_replacement_invalidate_old_payload(recorded_frames):
    state = InkSubblockState()
    state.accept(recorded_frames[0], received_mono_ns=1, received_utc="t")
    gap = state.accept(recorded_frames[2], received_mono_ns=2, received_utc="t")
    assert gap["status"] == "sequence_gap"
    assert gap["observations"] == []
    replacement = dict(recorded_frames[0], payload_id="0xreplacement")
    replaced = state.accept(replacement, received_mono_ns=3, received_utc="t")
    assert replaced["status"] == "payload_replaced"
    assert replaced["invalidated_payload_ids"] == [recorded_frames[0]["payload_id"]]


def test_missing_receipt_and_failed_status_never_emit_candidate(recorded_frames):
    frame = json.loads(json.dumps(recorded_frames[2]))
    frame["metadata"]["receipts"] = {}
    state = InkSubblockState()
    state.accept(recorded_frames[0], received_mono_ns=1, received_utc="t")
    state.accept(recorded_frames[1], received_mono_ns=2, received_utc="t")
    assert state.accept(frame, received_mono_ns=3, received_utc="t")["observations"] == []
