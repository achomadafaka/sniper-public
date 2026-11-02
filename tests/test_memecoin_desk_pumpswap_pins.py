"""Offline behavioral pins for curve priority, disabled capture and causal pool mappings."""
import asyncio
import importlib.util
from pathlib import Path

import pytest

from sniper.feeds.yellowstone import geyser_pb2 as pb
from sniper.services import memecoin_desk_pumpswap as PS

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "pumpswap_behavior_pins", ROOT / "scripts/memecoin_desk_universe_worker.py")
W = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(W)


def worker(base, monkeypatch, *, capture=True, census=False):
    monkeypatch.setenv("MEMECOIN_DESK_PUMPSWAP_CAPTURE", "1" if capture else "0")
    monkeypatch.setenv("MEMECOIN_DESK_PUMPSWAP_CENSUS", "1" if census else "0")
    monkeypatch.setenv("MEMECOIN_DESK_PUMPSWAP_EMISSIONS", "0")
    return W.Worker(W.knobs({}), base=base, sol_price_fn=lambda: None, alert_fn=lambda *_: None)


def event(kind="buy"):
    row = {"kind": kind, "pool": "pool", "actor": "actor", "coin_creator": "creator",
           "event_time": 1, "payload": {}, "payload_b64": "raw", "base_atomic": "1",
           "quote_atomic": "1", "pre_base_reserve_atomic": "1000", "pre_quote_reserve_atomic": "1000",
           "post_base_reserve_atomic": None, "post_quote_reserve_atomic": None,
           "lp_fee_bps": 20, "protocol_fee_bps": 5, "creator_fee_bps": 0,
           "cashback_fee_bps": None, "buyback_fee_bps": None}
    if kind == "create_pool":
        row.update(base_mint="mint", quote_mint=PS.WSOL_MINT, pool_creator="authority", canonical=True)
    return row


def frame(*, slot=10, index=2, signature=2, names=("pumpswap",)):
    message = pb.SubscribeUpdate()
    message.filters.extend(names)
    message.transaction.slot = slot
    info = message.transaction.transaction
    info.index = index
    info.signature = bytes([signature]) * 64
    info.meta.log_messages.extend([f"Program {PS.AMM_PROGRAM} invoke [1]",
                                   f"Program {PS.AMM_PROGRAM} success"])
    return message.SerializeToString()


def test_actual_async_processor_takes_curve_first_when_both_rings_are_loaded(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    w.push(b"curve-first", received_at=100)
    w.push(b"pumpswap-waits", received_at=101, pumpswap=True)
    seen = []

    def process(raw, *, received_at):
        seen.append((raw, received_at))
        w.stop = True

    monkeypatch.setattr(w, "process", process)
    monkeypatch.setattr(w, "flush_ps_gaps", lambda *_: None)
    monkeypatch.setattr(w, "commit_files", lambda: None)
    monkeypatch.setattr(w, "heartbeat", lambda *_: None)
    monkeypatch.setattr(w, "check_disk", lambda: None)
    asyncio.run(W.processor(w))
    assert seen == [(b"curve-first", 100)]
    assert not w.ring and w.ring_bytes == 0
    assert list(w.ps_ring) == [(b"pumpswap-waits", 101)]
    assert w.ps_ring_bytes == len(b"pumpswap-waits")
    assert not (tmp_path / "pumpswap" / "tape.db").exists()


def test_capture_off_census_on_dual_filter_frame_never_decodes_or_creates_lifetime_tape(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch, capture=False, census=True)
    calls = []

    def decode(logs):
        calls.append(logs)
        return [(1, event("create_pool")), (2, event())]

    monkeypatch.setattr(PS, "events", decode)
    raw = frame(names=("pump", "pumpswap"))
    assert w.census_frame(raw) is False
    w.process(raw, received_at=101)
    assert calls == []
    assert not (tmp_path / "pumpswap" / "tape.db").exists()


@pytest.mark.parametrize("create_slot,create_received", [(20, 100), (10, 102)])
def test_smaller_create_tx_index_cannot_override_later_source_slot_or_receipt(
        tmp_path, monkeypatch, create_slot, create_received):
    w = worker(tmp_path, monkeypatch)
    now = [200.0]
    monkeypatch.setattr(W.time, "time", lambda: now[0])
    monkeypatch.setattr(PS, "events", lambda _: [(1, event("create_pool"))])
    w.process(frame(slot=create_slot, index=1, signature=1), received_at=create_received)
    now[0] = 201.0
    monkeypatch.setattr(PS, "events", lambda _: [(2, event())])
    w.process(frame(slot=10, index=2, signature=2), received_at=101)
    w.commit_files()
    row = w.files.conn("swaps_ps", now[0]).execute("SELECT mint,map_status FROM swaps_ps").fetchone()
    assert row == (None, "unmapped")
    assert w.stats["ps_unmapped"] == 1
