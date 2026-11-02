"""Arc (5042) volume-death witness on the bound-pool poller (Alpha 2026-10-08: vol_death only; E2 on Arc
stays unknown and admits, decision (a) C).

Arc's V4 PoolManager carries most of its flow (measured: 10 V4 swaps in 12 blocks, 6 s), and no V2
factory is known, so the canary is the MANAGER itself: any V4 swap on the chain proves the host serves
logs. The V4 read therefore asks the manager for every Swap and keeps our pool ids client-side; the
V2/V3 read is skipped when no V3 pool is bound. Host: keyless arc.drpc.org on its own pacer, a 6 s tick
(12 blocks of 0.5 s), health and canary gap 15 s. The keyless free plan refuses a topic-only
getLogs without an address (measured), so every read names its address.
"""
import json

import pytest

from sniper.services import memecoin_bsc_volume_poll as P
from sniper.services import memecoin_volume_death as vd
from sniper.services.memecoin_evm_v4_events import V4_SWAP

MINT = "0x" + "11" * 20
V4ID = "0x" + "ab" * 32
OTHER_ID = "0x" + "cd" * 32
MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
OPEN = "2026-10-08T18:54:03+00:00"
T0 = 1791485643.0


def _pos():
    return {"id": 9, "network_id": "5042", "mint": MINT, "opened_at": OPEN,
            "funded_contract_json": json.dumps({"classification": {"facts": {}}}),
            "evm_route_json": json.dumps({"protocol": "uniswap_v4", "pool_id": V4ID})}


class Chain:
    def __init__(self, head=24976835):
        self.head, self.v4, self.calls = head, {}, []

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "eth_blockNumber":
            return hex(self.head)
        flt = params[0]
        assert flt.get("address"), "the keyless free plan refuses a getLogs without an address"
        lo, hi = int(flt["fromBlock"], 16), int(flt["toBlock"], 16)
        if flt["address"] == MANAGER:
            want = flt["topics"][1] if len(flt["topics"]) > 1 else None
            return [{"address": MANAGER, "blockNumber": hex(b), "topics": [V4_SWAP, i]}
                    for b in range(lo, hi + 1) for i in self.v4.get(b, ())
                    if want is None or i in want]
        return []


def _poller(chain):
    state = vd.BscPollState(health_window_s=15.0, canary_gap_s=15.0)
    return P.BscVolumePoller(
        state, call=chain.call, positions_fn=lambda: [_pos()],
        discover_fn=lambda mint, quotes=(): frozenset(), executed_pool_fn=lambda pos: None,
        canary=MANAGER, chain="5042", v4_manager=MANAGER, v4_ids_fn=lambda mint: [V4ID],
        block_s=0.5, canary_v4=True), state


def test_the_arc_state_is_its_own_with_a_two_tick_window():
    s = vd.EVM_POLL_STATE["5042"]
    assert s.health_window_s == 15.0 and s.canary_gap_s == 15.0


def test_a_tick_reads_the_manager_wide_and_keeps_our_ids_and_the_canary():
    chain = Chain()
    poller, state = _poller(chain)
    poller.last = chain.head - 12
    chain.v4[chain.head - 2] = [OTHER_ID]           # someone else's pool: the canary
    chain.v4[chain.head] = [V4ID]
    assert poller.tick(now_ts=T0 + 100) == "ok"
    assert [m for m, _p in chain.calls] == ["eth_blockNumber", "eth_getLogs"]   # no V3 pool bound
    assert chain.calls[1][1][0]["topics"] == [V4_SWAP]                        # manager-wide
    assert state._activity_at[V4ID] == pytest.approx(T0 + 100)
    assert OTHER_ID not in state._activity_at
    assert list(state._canary) == pytest.approx([T0 + 99.0, T0 + 100.0])


def test_the_confirm_needs_a_manager_log_and_returns_only_ours():
    chain = Chain()
    chain.v4[chain.head] = [OTHER_ID]
    confirm = P.confirm_logs_fn(chain.call, MANAGER, v4_manager=MANAGER, canary_v4=True)
    assert confirm([V4ID], chain.head - 3, chain.head) == []
    chain.v4[chain.head] = [OTHER_ID, V4ID]
    assert [l["topics"][1] for l in confirm([V4ID], chain.head - 3, chain.head)] == [V4ID]
    chain.v4.clear()
    assert confirm([V4ID], chain.head - 3, chain.head) is None             # nothing at all: blind


def test_the_arc_host_is_keyless_arc_drpc(monkeypatch):
    monkeypatch.delenv("MEMECOIN_VOLDEATH_POLL_URL_5042", raising=False)
    assert P.poll_url("5042") == "https://arc.drpc.org"
