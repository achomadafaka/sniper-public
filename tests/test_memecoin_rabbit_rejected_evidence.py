"""Unsupported candidates keep bounded diagnostic evidence on the existing frame drain."""
import asyncio
import json
import threading

from sniper.services.memecoin_rabbit_signal import FrameLog


class Clock:
    def __init__(self):
        self.now = 10.0

    def monotonic(self):
        return self.now


def candidate(signature="sig", data=b"\x00\x01\xff"):
    return dict(signature=signature, owner="owner", slot=123, recv_monotonic=10.0, generation=2,
                account_keys=("owner", "program", "mint"), signers=("owner",),
                writable=(True, False, True), version="v0",
                instructions=(("program", ("mint", "owner", "mint"), data),))


def frame_log(*, log=None):
    lines, clock = [], Clock()
    frames = FrameLog(feed="rabbit:fixture", arrivals_fn=lambda *_: (), clock=clock,
                      log=log or (lambda fmt, *args: lines.append(fmt % args)))
    return frames, lines, clock


def reject(frames, row):
    frames.frame(row["signature"], row["owner"], row["slot"], row["recv_monotonic"])
    frames.note(row["signature"], row["owner"], "unsupported_target")
    return frames.rejected(row)


def flush(frames, clock):
    clock.now += frames.GRACE_S
    frames.write(frames.due())


def test_rejected_evidence_freezes_exact_keys_and_instructions():
    frames, lines, clock = frame_log()
    row = candidate()
    assert reject(frames, row)
    row.update(account_keys=("changed",), signers=(), writable=(), version="v1", instructions=())

    flush(frames, clock)

    assert len(lines) == 1 and "verdict=unsupported_target" in lines[0]
    encoded = lines[0].split(" evidence_json=", 1)[1]
    assert json.loads(encoded) == {
        "basis": "diagnostic_normalized_pre_execution", "version": "v0",
        "keys": ["owner", "program", "mint"], "signer_indices": [0],
        "writable": [True, False, True],
        "instructions": [{"program": 1, "accounts": [2, 0, 2], "data_hex": "0001ff"}],
    }
    assert len(encoded.encode("utf-8")) <= 16 * 1024
    assert frames.stats["evidence"]["written"] == 1
    assert frames.stats["evidence"]["retained"] == 0


def test_duplicate_rejection_keeps_the_first_payload_once():
    frames, lines, clock = frame_log()
    assert reject(frames, candidate(data=b"first"))
    assert not frames.rejected(candidate(data=b"second"))

    flush(frames, clock)

    assert frames.stats["evidence"]["captured"] == 1
    assert frames.stats["evidence"]["duplicate"] == 1
    assert json.loads(lines[0].split(" evidence_json=", 1)[1])["instructions"][0]["data_hex"] == b"first".hex()


def test_payload_capacity_drops_are_explicit_and_due_releases_capacity():
    frames, lines, clock = frame_log()
    for i in range(16):
        assert reject(frames, candidate(str(i)))
    assert not reject(frames, candidate("overflow"))
    assert frames.stats["evidence"]["retained"] == 16
    assert frames.stats["evidence"]["dropped_capacity"] == 1

    flush(frames, clock)

    assert frames.stats["evidence"]["retained"] == 0
    assert "evidence_omitted=capacity" in lines[-1]
    assert reject(frames, candidate("next"))


def test_frame_eviction_releases_its_payload_budget(monkeypatch):
    monkeypatch.setattr(FrameLog, "MAX", 2)
    frames, _, _ = frame_log()
    assert reject(frames, candidate("first"))
    assert reject(frames, candidate("second"))
    assert reject(frames, candidate("third"))

    assert frames.stats["dropped"] == 1
    assert frames.stats["evidence"]["retained"] == 2
    assert list(frames._recs) == [("second", "owner"), ("third", "owner")]


def test_oversized_normalized_candidate_is_not_retained():
    frames, lines, clock = frame_log()
    row = candidate()
    row["instructions"] = tuple(("program", ("mint", "owner", "mint"), b"x" * 1100)
                                for _ in range(16))
    assert not reject(frames, row)

    flush(frames, clock)

    assert frames.stats["evidence"]["dropped_oversize"] == 1
    assert frames.stats["evidence"]["captured"] == 0
    assert "verdict=unsupported_target" in lines[0]
    assert "evidence_omitted=oversize" in lines[0] and "evidence_json=" not in lines[0]


def test_final_encoded_size_cap_omits_payload_without_losing_the_verdict():
    frames, lines, clock = frame_log()
    row = candidate()
    row["instructions"] = tuple(("program", ("mint", "owner", "mint"), b"x" * 1125)
                                for _ in range(8))
    assert reject(frames, row)

    flush(frames, clock)

    assert frames.stats["evidence"]["encoded_oversize"] == 1
    assert "verdict=unsupported_target" in lines[0]
    assert "evidence_omitted=encoded_oversize" in lines[0] and "evidence_json=" not in lines[0]


def test_capture_does_not_serialize_or_log_inline(monkeypatch):
    frames, lines, clock = frame_log()

    def forbidden(*args, **kwargs):
        raise AssertionError("serialization reached the signal loop")

    with monkeypatch.context() as capture:
        capture.setattr(json, "dumps", forbidden)
        assert reject(frames, candidate())
        assert not lines
    flush(frames, clock)
    assert len(lines) == 1


def test_encoding_failure_preserves_the_frame_line(monkeypatch):
    frames, lines, clock = frame_log()
    assert reject(frames, candidate())
    monkeypatch.setattr(json, "dumps", lambda *_args, **_kw: 1 / 0)

    flush(frames, clock)

    assert frames.stats["evidence"]["encode_errors"] == 1
    assert frames.stats["write_errors"] == 0
    assert "verdict=unsupported_target" in lines[0] and "evidence_omitted=encode_error" in lines[0]


def test_missing_or_invalid_candidate_cannot_change_the_refusal():
    frames, lines, clock = frame_log()
    frames.frame("sig", "owner", 123, 10.0)
    frames.note("sig", "owner", "unsupported_target")
    assert not frames.rejected({"signature": "sig", "owner": "owner"})
    assert not frames.rejected(candidate("missing-frame"))

    flush(frames, clock)

    assert frames.stats["evidence"]["invalid"] >= 1
    assert "verdict=unsupported_target" in lines[0]


def test_existing_worker_serializes_and_logs_off_the_event_loop(monkeypatch):
    stop, threads = threading.Event(), []
    real_dumps = json.dumps

    def dumps(*args, **kwargs):
        threads.append(threading.get_ident())
        return real_dumps(*args, **kwargs)

    def log(*args):
        threads.append(threading.get_ident())
        stop.set()

    monkeypatch.setattr(json, "dumps", dumps)
    frames, _, clock = frame_log(log=log)
    frames.TICK_S = .001
    assert reject(frames, candidate())
    clock.now += frames.GRACE_S

    async def run():
        loop_thread = threading.get_ident()
        await asyncio.wait_for(frames.run(stop), timeout=1.0)
        return loop_thread

    loop_thread = asyncio.run(run())
    assert len(threads) == 2 and all(thread != loop_thread for thread in threads)
