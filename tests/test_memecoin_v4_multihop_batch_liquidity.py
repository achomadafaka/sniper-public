"""Pair liquidity in ONE eth_call per pair: PoolManager.extsload(bytes32[]) (2026-09-28, Alpha GO).

The first organic cold store-first search (4663, coin 0x8920d999, 00:27Z) needed 96 pair-pool
readings -- ETH/USDG 40, USDG/0xc0d6 49, ETH/0xc0d6 7 -- against a cap of 32 single `extsload`
reads per decision. The first bridge spent the cap, the pair holding the coin's deep pool was never
read, and the route quoted -46.4% where the provider path quoted -2.7%. The cap exists to bound
REQUESTS on the endpoint (SHIT 0x685dfd38, 09-17), so the reads are now batched through the
PoolManager's own extsload(bytes32[]): at most 256 slots per call, and each call takes one from the
same 32-per-decision allowance.

The fixture is a real uncapped search recorded at `latest` on robinhood-rpc.publicnode.com
(tests/fixtures/memecoin/v4_multihop_batch_liquidity_8920_4663.json). The fake chain below answers
BOTH the single and the batch selector from the recorded readings, so the capped per-pool search
fails here on its own merits (the starved -46% route), not on a missing stub.
"""
import json
import threading
import time
from pathlib import Path

import pytest
from eth_utils import keccak

from sniper.execution import evm_read_rpc as RPC
from sniper.execution import evm_v4_multihop as MH
from sniper.execution import evm_v4_quote as VQ
from sniper.execution import evm_wallet as W
from sniper.settings import memecoin as cfg

FX = json.loads((Path(__file__).parent / "fixtures/memecoin/v4_multihop_batch_liquidity_8920_4663.json")
                .read_text())
MINT = FX["mint"]
DEEP_POOL = "0x907c9500d95d98a5a18e697a7ef97e92274c5ea3a0b1c98c366cef4468ad991f"
AMOUNT = 10 ** 16
SINGLE = "0x1e2eaeaf"                       # extsload(bytes32)
BATCH = "0xdbd035ff"                        # extsload(bytes32[])


def _slot(pool_id):
    """The pool's liquidity slot, computed here from the V4 layout (pools mapping at slot 6,
    liquidity at offset 3), NOT from the module, so an encoding error cannot agree with itself."""
    state = int.from_bytes(keccak(bytes.fromhex(pool_id[2:]) + (6).to_bytes(32, "big")), "big")
    return "%064x" % (state + 3)


class Chain:
    """A PoolManager that answers extsload from recorded readings. An unknown slot reads 0."""

    def __init__(self, liquidity, drop_words=0, delay_s=0.0):
        self.by_slot = {_slot(p): int(v or 0) for p, v in liquidity.items()}
        self.drop_words = drop_words
        self.delay_s = delay_s
        self.batches = []
        self.singles = 0
        self.lock = threading.Lock()

    def __call__(self, url, method, params, http=None, **_kw):
        assert method == "eth_call"
        data = params[0]["data"]
        if self.delay_s:
            time.sleep(self.delay_s)
        if data.startswith(SINGLE):
            with self.lock:
                self.singles += 1
            return {"result": "0x%064x" % self.by_slot.get(data[10:], 0)}
        assert data.startswith(BATCH), data[:10]
        words = [data[10 + 64 * i:74 + 64 * i] for i in range((len(data) - 10) // 64)]
        assert int(words[0], 16) == 32 and int(words[1], 16) == len(words) - 2
        slots = words[2:]
        with self.lock:
            self.batches.append(len(slots))
        out = ["%064x" % 32, "%064x" % len(slots)] + ["%064x" % self.by_slot.get(s, 0) for s in slots]
        return {"result": "0x" + "".join(out[:len(out) - self.drop_words])}


def _qkey(currency_in, path, amount_in):
    return json.dumps([str(currency_in).lower(), [[str(c).lower(), k] for c, k in path],
                       str(int(amount_in))], sort_keys=True)


@pytest.fixture
def recorded(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_MIN_DEPTH_CLIPS", 20.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_MAX_LEGS", 3, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_LIQUIDITY_READS", 32, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_STORE_FIRST", True, raising=False)
    monkeypatch.setattr(VQ, "_stored_pools_for_mint",
                        lambda chain, mint: list(FX["stored_pools_for_mint"].get(str(mint).lower(), [])))
    monkeypatch.setattr(MH, "_stored_pair_pools", lambda chain, a, b: [
        (p, k) for p, k in FX["stored_pair_pools"].get(
            "%s|%s" % tuple(sorted((str(a).lower(), str(b).lower()))), [])])
    monkeypatch.setattr(MH, "_pool_key_for",
                        lambda chain, pid, rpc_url, **kw: FX["pool_key"].get(str(pid).lower()))
    monkeypatch.setattr(MH, "_quote_path", lambda chain, currency_in, path, amount_in, rpc_url,
                        block="latest", **kw: FX["quote"].get(_qkey(currency_in, path, amount_in)))
    monkeypatch.setattr(MH, "_decision_block", lambda chain, rpc_url, **kw: "0x1")
    monkeypatch.setattr(VQ, "_gt_pools_ranked", lambda *a, **k: ([], True))
    monkeypatch.setattr(VQ, "_ds_pools_ranked", lambda *a, **k: ([], True))
    monkeypatch.setattr(MH, "_POOL_LIQUIDITY", {})            # COLD: no reading from any decision
    monkeypatch.setattr(MH, "_BRIDGE_LAST_GOOD", {})
    chain = Chain(FX["liquidity"])
    monkeypatch.setattr(RPC, "_rpc", chain)
    return chain


def _pair(a, b):
    return [(p, k) for p, k in FX["stored_pair_pools"]["%s|%s" % tuple(sorted((a, b)))]]


USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
BRIDGE_C = "0xc0d6457c16cc70d6790dd43521c899c87ce02f35"


def test_the_cold_search_reads_every_pair_and_finds_the_deep_pool(recorded):
    route = MH.multihop_quote("4663", FX["cash"], MINT, AMOUNT, rpc_url="https://rpc",
                              bridge_currencies=(), clip_usd=25.0)
    assert route is not None, MH.last_multihop_refusal()
    assert route["coin_pool_id"].lower() == DEEP_POOL
    assert route["round_trip_pct"] > -3.3, "the recorded uncapped best is -2.77%; the starved route -46%"
    assert route["search_discovery_cut"] is False
    assert recorded.singles == 0 and recorded.batches and max(recorded.batches) <= 256
    assert len(recorded.batches) <= 3, "one call per pair: ETH/USDG, ETH/0xc0d6, USDG/0xc0d6"


def test_a_chunk_result_short_by_one_word_gives_no_readings(recorded):
    recorded.drop_words = 1
    pools = _pair(FX["cash"], USDG)
    assert MH._onchain_pair_options("4663", FX["cash"], USDG, "https://rpc") == []
    assert recorded.batches, "the batch was sent"
    assert not any(("4663", p) in MH._POOL_LIQUIDITY for p, _k in pools), (
        "a malformed answer is not remembered; the next decision reads again")


def test_a_length_word_that_disagrees_with_the_request_gives_no_readings(recorded, monkeypatch):
    real = recorded.__call__

    def lying(url, method, params, http=None, **kw):
        out = real(url, method, params, http=http, **kw)["result"]
        # Same byte length as a true answer; only the length word disagrees with the request.
        return {"result": out[:2 + 64] + "%064x" % (int(out[2 + 64:130], 16) - 1) + out[130:]}
    monkeypatch.setattr(RPC, "_rpc", lying)
    assert MH._onchain_pair_options("4663", FX["cash"], USDG, "https://rpc") == []


def test_the_batch_values_equal_the_single_reads(recorded):
    pools = [p for p, _k in _pair(FX["cash"], USDG) + _pair(USDG, BRIDGE_C) if p in FX["liquidity"]]
    assert len(pools) == 89                   # the recorded ETH/USDG 40 + USDG/0xc0d6 49
    got = MH._onchain_liquidities("4663", pools, "https://rpc")
    assert got == {p: int(FX["liquidity"][p] or 0) for p in pools}
    assert got == {p: MH._onchain_liquidity("4663", p, "https://rpc") for p in pools}


def test_each_chunk_of_at_most_256_slots_takes_one_from_the_allowance(recorded, monkeypatch):
    many = {"0x%064x" % (i + 1): 10 ** 18 + i for i in range(300)}
    key = dict(_pair(FX["cash"], USDG)[0][1])
    monkeypatch.setattr(MH, "_stored_pair_pools", lambda chain, a, b: [(p, key) for p in many])
    recorded.by_slot = {_slot(p): v for p, v in many.items()}
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_LIQUIDITY_READS", 1, raising=False)
    budget = MH._SearchBudget(deadline_s=30.0, max_requests=1000)
    first = MH._onchain_pair_options("4663", FX["cash"], USDG, "https://rpc", budget=budget)
    assert recorded.batches == [256] and len(first) == 256
    assert budget.liquidity_reads_used == 1 and budget.discovery_cut is True
    second = MH._onchain_pair_options("4663", FX["cash"], USDG, "https://rpc",
                                      budget=MH._SearchBudget(deadline_s=30.0, max_requests=1000))
    assert recorded.batches == [256, 44] and len(second) == 300, "the next decision reads the rest"


def test_a_slow_batch_is_cut_at_the_discovery_deadline(recorded):
    """A slow batch cuts like a slow single read: the wait ends at the discovery deadline, the
    search is marked cut, and the late answer is never used."""
    recorded.delay_s = 1.5
    budget = MH._SearchBudget(deadline_s=0.4, max_requests=1000)
    t0 = time.monotonic()
    out = MH._onchain_pair_options("4663", FX["cash"], USDG, "https://rpc", budget=budget)
    assert time.monotonic() - t0 < 1.0, "the rung never waits out a slow batch"
    assert out == [] and budget.discovery_cut is True


def test_the_rung_record_carries_the_batch_wall_time_and_slot_count(recorded, monkeypatch):
    calls = []
    monkeypatch.setattr(W._HTTP_TAP, "calls", calls, raising=False)
    MH._onchain_pair_options("4663", FX["cash"], USDG, "https://rpc")
    taps = [c for c in calls if c["label"] == "v4_multihop:liquidity_batch"]
    assert len(taps) == 1 and recorded.batches
    assert taps[0]["slots"] == sum(recorded.batches) and taps[0]["calls"] == len(recorded.batches)
    assert taps[0]["rtt_ms"] >= 0 and taps[0]["outcome"] == "ok"
