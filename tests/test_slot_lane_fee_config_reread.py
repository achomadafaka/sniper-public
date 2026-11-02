"""Slot-lane pump builders re-read their fee recipients (Gamma LOW 2026-10-08).

The PumpSwap builder's GlobalConfig recipients (_buyback, _protocol_recipient) and the curve builder's
Global buyback recipient were read once and never again: a Pump rotation would sign reverting
entries until a restart. The first read stays inline in refresh(); after it, one daemon thread
re-reads every RECIPIENT_REREAD_S, never more than one in flight, never blocking the leg refresh.
A failed, null or default-pubkey (zero) read keeps the pair the builder holds; zero is refused on the
first read too. The GlobalConfig bytes are the real account (pumpswap_v2_buy_20261008.json)."""
import base64
import json
import threading
import time
from pathlib import Path

import pytest
from solders.pubkey import Pubkey

from sniper.execution import pump_curve_buy as pcb
from sniper.execution import pumpswap_buy as ps

GC = json.loads((Path(__file__).parent / "fixtures" / "memecoin" / "pumpswap_v2_buy_20261008.json")
                .read_text())["global_config_b64"]
NEW_PROTOCOL, NEW_BUYBACK = str(Pubkey.new_unique()), str(Pubkey.new_unique())
ZERO = bytes(32)


def _patched(b64, writes):
    raw = bytearray(base64.b64decode(b64))
    for offset, key in writes:
        raw[offset:offset + 32] = key if isinstance(key, bytes) else bytes(Pubkey.from_string(key))
    return base64.b64encode(bytes(raw)).decode()


ROTATED = _patched(GC, [(ps.GLOBAL_CONFIG_PROTOCOL_RECIPIENTS_OFFSET, NEW_PROTOCOL),
                        (ps.GLOBAL_CONFIG_BUYBACK_OFFSET, NEW_BUYBACK)])
ZEROED = _patched(GC, [(ps.GLOBAL_CONFIG_PROTOCOL_RECIPIENTS_OFFSET, ZERO), (ps.GLOBAL_CONFIG_BUYBACK_OFFSET, ZERO)])


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class _Rpc:
    """getAccountInfo answers the current `data`; `gate` (an Event) holds a read until set."""

    def __init__(self, data, owner=ps.AMM_PROGRAM):
        self.data, self.calls, self.gate, self.error = data, 0, None, None
        self.owner = owner

    def __call__(self, method, params):
        assert method == "getAccountInfo"
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(5)
        if self.error is not None:
            raise self.error
        return None if self.data is None else {"value": {"data": [self.data, "base64"],
                                                         "owner": self.owner, "executable": False}}


def _join_rereads():
    for t in threading.enumerate():
        if t.name == "slot-fee-config":
            t.join(5)


@pytest.fixture(autouse=True)
def _no_legs(monkeypatch):
    legs = []
    monkeypatch.setattr(pcb, "refresh_cash_legs", lambda builder, *a, **k: legs.append(builder.clock()))
    yield legs
    _join_rereads()


def _pumpswap(rpc, clock):
    b = ps.SlotLanePumpSwapBuilder(user=str(Pubkey.new_unique()), jup_call=lambda *a, **k: None,
                                   rpc=rpc, clock=clock)
    b.pools.learn_pending = lambda: None
    return b


def test_a_rotation_is_picked_up_after_the_reread_interval_not_before():
    rpc, clock = _Rpc(GC), _Clock()
    b = _pumpswap(rpc, clock)
    b.refresh(5.0)
    first = (b._buyback, b._protocol_recipient)
    assert first == (ps.buyback_fee_recipient_from_global_config(GC), ps.protocol_fee_recipient_from_global_config(GC))
    rpc.data = ROTATED
    clock.t += pcb.RECIPIENT_REREAD_S - 1
    b.refresh(5.0)
    _join_rereads()
    assert (rpc.calls, (b._buyback, b._protocol_recipient)) == (1, first)
    clock.t += 1
    b.refresh(5.0)
    _join_rereads()
    assert (rpc.calls, b._buyback, b._protocol_recipient) == (2, NEW_BUYBACK, NEW_PROTOCOL)


def test_a_hanging_reread_never_blocks_the_leg_refresh_and_never_doubles(_no_legs):
    rpc, clock = _Rpc(GC), _Clock()
    b = _pumpswap(rpc, clock)
    b.refresh(5.0)
    rpc.gate, rpc.data = threading.Event(), ROTATED
    clock.t += pcb.RECIPIENT_REREAD_S
    started = time.monotonic()
    b.refresh(5.0)
    b.refresh(5.0)
    assert time.monotonic() - started < 1.0
    assert rpc.calls == 2 and len(_no_legs) == 3
    assert b._buyback == ps.buyback_fee_recipient_from_global_config(GC)
    rpc.gate.set()
    _join_rereads()
    assert b._buyback == NEW_BUYBACK


@pytest.mark.parametrize("damage", ["raises", "null", "zero"])
def test_a_failed_or_zero_reread_keeps_the_pair(damage, caplog):
    rpc, clock = _Rpc(GC), _Clock()
    b = _pumpswap(rpc, clock)
    b.refresh(5.0)
    first = (b._buyback, b._protocol_recipient)
    if damage == "raises":
        rpc.error = RuntimeError("getAccountInfo: node down")
    else:
        rpc.data = None if damage == "null" else ZEROED
    clock.t += pcb.RECIPIENT_REREAD_S
    b.refresh(5.0)
    _join_rereads()
    assert rpc.calls == 2 and (b._buyback, b._protocol_recipient) == first
    if damage == "raises":
        assert "fee config re-read failed" in caplog.text


def test_a_zero_recipient_on_the_first_read_is_refused():
    rpc, clock = _Rpc(ZEROED), _Clock()
    b = _pumpswap(rpc, clock)
    b.refresh(5.0)
    assert (b._buyback, b._protocol_recipient) == (None, None)
    rpc.data = GC
    b.refresh(5.0)
    assert b._buyback == ps.buyback_fee_recipient_from_global_config(GC)


def test_the_curve_builder_heals_a_buyback_rotation():
    old, new = str(Pubkey.new_unique()), str(Pubkey.new_unique())
    size = pcb.GLOBAL_BUYBACK_OFFSET + 32 * 8

    def global_with(key):
        raw = bytearray(size)
        raw[:8], raw[8] = pcb.GLOBAL_DISCRIMINATOR, 1
        for offset in (pcb.GLOBAL_FEE_RECIPIENT_OFFSET, pcb.GLOBAL_RESERVED_FEE_RECIPIENT_OFFSET):
            raw[offset:offset + 32] = bytes(Pubkey.from_string(old))
        raw[pcb.GLOBAL_BUYBACK_OFFSET:pcb.GLOBAL_BUYBACK_OFFSET + 32] = bytes(Pubkey.from_string(key))
        return base64.b64encode(bytes(raw)).decode()

    rpc, clock = _Rpc(global_with(old), owner=pcb.PUMP_PROGRAM), _Clock()
    b = pcb.SlotLaneCurveBuilder(user=str(Pubkey.new_unique()), jup_call=lambda *a, **k: None,
                                 rpc=rpc, clock=clock)
    b._pump_alt_done = True
    b.refresh(5.0)
    assert b._buyback == old
    rpc.data = global_with(new)
    clock.t += pcb.RECIPIENT_REREAD_S
    b.refresh(5.0)
    _join_rereads()
    assert b._buyback == new
    rpc.data = base64.b64encode(bytes(size)).decode()
    clock.t += pcb.RECIPIENT_REREAD_S
    b.refresh(5.0)
    _join_rereads()
    assert b._buyback == new
