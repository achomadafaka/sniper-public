"""One Flap ladder owns its state; exact simulations and live checks remain decisive."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from sniper.execution import evm_flap_curve as flap, evm_read_rpc
from sniper.execution.evm_real_seams import venue_quote_with_v2_fallback
from sniper.settings import memecoin as cfg
from test_memecoin_evm_flap_curve import _state

MINT = "0x8d411a22bf0194b2f5ef665f8a2c7a9430ee7777"
TAKER = "0x898be9c27f8b391c47a3d9f2da17781aa8aa3a1d"
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
RPC = "https://bsc.example"


class FlapReads:
    def __init__(self, *, moved_fee=False, reverse_fails=False):
        self.calls = []
        self.state = _state()
        self.moved_fee, self.reverse_fails = moved_fee, reverse_fails

    def read(self, to, data, rpc_url, **kwargs):
        assert rpc_url == RPC
        selector = data[2:10]
        if selector == flap._SEL_GET_TOKEN_V6:
            self.calls.append(("state", None))
            s = self.state
            values = [s["status"], s["reserve"], s["circulating_supply"], s["price"],
                      s["token_version"], s["r"], s["h"], s["k"], s["dex_supply_thresh"],
                      int(s["quote_token"], 16), s["native_to_quote_swap_enabled"], 0,
                      s["creator_fee_bps"], 0, 0]
            return "0x" + "".join(format(value, "064x") for value in values)
        assert selector == flap._SEL_SWAP_EXACT_INPUT
        amount = int(data[202:266], 16)
        is_buy = int(data[74:138], 16) == 0
        self.calls.append(("buy" if is_buy else "sell", amount))
        if is_buy:
            out = (1601572398443673817714856 if self.state["creator_fee_bps"] == 100
                   and amount == 10**16 else flap.curve_buy_out(self.state, amount))
        elif self.reverse_fails or kwargs.get("overrides") is None:
            return None
        else:
            s = _state(creator_fee_bps=300) if self.moved_fee else self.state
            out = flap.curve_sell_out(s, amount)
        return None if out is None else "0x" + format(out, "064x")


def _ladder(monkeypatch, reads, records=None):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_FLAP_CURVE_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ENTRY_MEASURED_LADDER", True)
    monkeypatch.setattr(evm_read_rpc, "eth_call_raw", reads.read)
    monkeypatch.setattr(flap, "discover_sell_overrides", lambda *a, **k: {"fixture": True})
    monkeypatch.setattr(flap, "_sell_override_slot_cache", {
        (MINT, TAKER, flap.FLAP_PORTAL["56"]): ("0x" + "11" * 32, "0x" + "22" * 32)})
    return venue_quote_with_v2_fallback(lambda *a, **k: None, chain="56",
        quote_token=WBNB, taker=TAKER, rpc_url=RPC,
        on_rung=records.append if records is not None else None)


def test_measured_flap_ladder_reads_one_state_and_simulates_the_exact_whole_bag(monkeypatch):
    reads = FlapReads()
    q = _ladder(monkeypatch, reads)(WBNB, MINT, 10**16, only_rung="flap_curve")
    assert q["buyAmount"] == "1601572398443673817714856"
    assert q["route"]["round_trip_measured_by"] == "flap_curve"
    assert reads.calls == [("state", None), ("buy", 10**16),
                           ("sell", 1601572398443673817714856)]


def test_later_flap_ladder_reads_current_fee_instead_of_reusing_the_prior_signal(monkeypatch):
    reads = FlapReads()
    ladder = _ladder(monkeypatch, reads)
    first = ladder(WBNB, MINT, 10**16, only_rung="flap_curve")
    reads.state = _state(creator_fee_bps=300)
    second = ladder(WBNB, MINT, 10**16, only_rung="flap_curve")
    assert first["buyAmount"] != second["buyAmount"]
    assert [kind for kind, _ in reads.calls].count("state") == 2


def test_missing_flap_state_remains_an_ordinary_route_miss(monkeypatch):
    records = []
    ladder = _ladder(monkeypatch, FlapReads(), records)
    monkeypatch.setattr(flap, "curve_state", lambda *a, **k: None)
    assert ladder(WBNB, MINT, 10**16, only_rung="flap_curve") is None
    probe = next(item for item in records if item["rung"] == "flap_curve")
    assert probe["blind_reason"] is None


@pytest.mark.parametrize("failure", ["fee_movement", "simulation_failure"])
def test_bound_flap_metadata_cannot_admit_failed_or_changed_live_reverse(monkeypatch, failure):
    reads = FlapReads(moved_fee=failure == "fee_movement",
                      reverse_fails=failure == "simulation_failure")
    assert _ladder(monkeypatch, reads)(WBNB, MINT, 10**16, only_rung="flap_curve") is None
    assert [kind for kind, _ in reads.calls].count("state") == 1
    assert ("sell", 1601572398443673817714856) in reads.calls


def test_concurrent_flap_ladders_have_independent_fee_snapshots(monkeypatch):
    reads = FlapReads()
    ladder = _ladder(monkeypatch, reads)
    barrier = threading.Barrier(2)
    sources = [FlapReads(), FlapReads()]
    sources[1].state = _state(creator_fee_bps=300)
    available = list(sources)
    owners = {}

    def read(*args, **kwargs):
        if args[1][2:10] == flap._SEL_GET_TOKEN_V6:
            if threading.get_ident() not in owners:
                owners[threading.get_ident()] = available.pop()
            barrier.wait(timeout=2)
        return owners[threading.get_ident()].read(*args, **kwargs)

    monkeypatch.setattr(evm_read_rpc, "eth_call_raw", read)

    with ThreadPoolExecutor(max_workers=2) as workers:
        a, b = list(workers.map(lambda _: ladder(WBNB, MINT, 10**16, only_rung="flap_curve"),
                               [None, None]))
    assert a["buyAmount"] != b["buyAmount"]
    assert [[kind for kind, _ in source.calls] for source in sources] == [["state", "buy", "sell"]] * 2
