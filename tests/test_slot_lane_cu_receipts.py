"""The slot lane records the compute budget it signed and what the fill actually consumed (Alpha,
2026-10-02, per-route CU limits). Both ride the attempt trace: the signed limit on the slot_build
substage, the receipt's meta.computeUnitsConsumed on a receipt_cu substage written after the fill
from the receipt the fill already read. No new call, nothing on the send path; a receipt without the
field records None, never 0. This lets the per-class table re-derive from the ledger."""
import time
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_slot_lane as lane
from test_memecoin_solana_slot_executor import _reserve, conn  # noqa: F401  (fixture)


class _Trace:
    def __init__(self):
        self.events = []

    def mark_substage(self, name, **kw):
        self.events.append((name, kw))

    def mark(self, name, **kw):
        self.events.append((name, kw))


def test_the_signed_limit_rides_the_build_substage_and_leaves_the_validity_dict():
    trace = _Trace()
    validity = lane.record_slot_build(trace, {"blockhash": "H", "leg_age_s": 0.4, "cu_limit": 215_000},
                                      duration_ms=1.5)
    assert validity == {"blockhash": "H"}
    assert trace.events == [("slot_build", {"duration_ms": 1.5, "leg_age_s": 0.4, "cu_limit": 215_000})]


def _seam_run(conn, monkeypatch, meta_extra, status="filled"):  # noqa: F811
    from solders.hash import Hash
    from solders.keypair import Keypair
    from solders.message import MessageV0
    from sniper.execution import solana_real_seams as seams, solana_slot_executor as slot
    from sniper.execution.submission_transport import AttemptState
    from test_memecoin_fast_submit_wiring import StubTransport
    kp = Keypair()
    reads = []

    def cached(_read):
        def receipt(sig):
            reads.append(sig)
            return {"transaction": {"signatures": [sig]}, "meta": {"err": None, **meta_extra}}
        return receipt
    monkeypatch.setattr(seams, "successful_receipt_cache", cached)
    monkeypatch.setattr(slot, "execute_prebuilt_entry",
                        lambda _conn, **kw: {"status": status, "sig": kw["sig"]})
    trace = _Trace()
    source_slot = 458_900_000
    seam = seams.build_real_solana_executor(keypair=kp, rpc_url="http://unused",
        http=lambda *a, **kw: pytest.fail("CU attribution made a provider read"),
        submission_transport=StubTransport(AttemptState.ACCEPTED))
    result = seam["slot_execute_fn"](
        conn, _reserve(conn), MessageV0.try_compile(kp.pubkey(), [], [], Hash.default()),
        now_iso="2026-10-02T15:00:00Z", trace=trace,
        entry_min_slot=source_slot, entry_max_slot=source_slot + 1,
        entry_head_fn=lambda: (source_slot, time.monotonic()))
    assert result["status"] == status
    return dict(trace.events).get("receipt_cu"), reads


def test_a_fill_records_the_units_its_receipt_consumed(conn, monkeypatch):  # noqa: F811
    mark, reads = _seam_run(conn, monkeypatch, {"computeUnitsConsumed": 186_857, "fee": 10_005_000})
    # fee_lamports (crowded priority-fee test, 2026-10-15): base + priority as the receipt paid it.
    assert mark == {"consumed": 186_857, "fee_lamports": 10_005_000}
    assert len(set(reads)) == 1                      # the fill's own (cached) receipt, no new read


def test_a_receipt_without_the_field_records_unknown_not_zero(conn, monkeypatch):  # noqa: F811
    mark, _reads = _seam_run(conn, monkeypatch, {})
    assert mark == {"consumed": None, "fee_lamports": None}


def test_no_fill_records_nothing(conn, monkeypatch):  # noqa: F811
    mark, reads = _seam_run(conn, monkeypatch, {"computeUnitsConsumed": 99}, status="unknown")
    assert mark is None and reads == []
