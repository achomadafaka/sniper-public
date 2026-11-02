"""Chain-1 volume-death witness (spec 2026-10-08-memecoin-voldeath-chain1-arc-witness-design.md; Alpha
approved: a bound-pool poller every 24 s on the shared drpc_paced research key, network=ethereum).

The 56 poller made per chain. Differences on chain 1:
- V4 pools (73 of 83 roster coins) are bound by pool id and read with a second eth_getLogs on the
  PoolManager, filtered by topic1; their activity lands on the pool id.
- A 24 s tick spans two 12 s blocks, so activity is stamped at its BLOCK's time (now minus the blocks
  behind the head x 12 s), not the tick's: the silence then resolves to about one block, and the
  fire lands within one tick after exit_s instead of up to two ticks late.
- Health and the canary gap are per chain: 60 s on chain 1 (two ticks plus a block), 5 s / 10 s on 56.
- Host: the operator's dRPC research account (the 56 key with network=ethereum), behind drpc_paced;
  never a funded chain-1 host.
"""
import json

import pytest

from sniper.services import memecoin_bsc_volume_poll as P
from sniper.services import memecoin_volume_death as vd
from sniper.services.memecoin_evm_v4_events import V4_POOL_MANAGERS, V4_SWAP

MINT = "0x" + "11" * 20
V2POOL = "0x" + "aa" * 20
V4ID = "0x" + "ab" * 32
CANARY = "0x" + "cc" * 20
MANAGER = V4_POOL_MANAGERS["1"]
OPEN = "2026-10-08T18:54:03+00:00"
T0 = 1791485643.0                     # OPEN as epoch


def _pos(pid=1):
    contract = {"classification": {"facts": {}}}
    return {"id": pid, "network_id": "1", "mint": MINT, "opened_at": OPEN,
            "funded_contract_json": json.dumps(contract),
            "evm_route_json": json.dumps({"protocol": "uniswap_v4", "pool_id": V4ID})}


class Chain:
    def __init__(self, head=26149890):
        self.head, self.v23, self.v4, self.calls = head, {}, {}, []

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "eth_blockNumber":
            return hex(self.head)
        flt = params[0]
        lo, hi = int(flt["fromBlock"], 16), int(flt["toBlock"], 16)
        if flt.get("address") == MANAGER:
            ids = {i.lower() for i in flt["topics"][1]}
            return [{"address": MANAGER, "blockNumber": hex(b), "topics": [V4_SWAP, i]}
                    for b in range(lo, hi + 1) for i in self.v4.get(b, ()) if i in ids]
        want = {a.lower() for a in flt["address"]}
        return [{"address": a, "blockNumber": hex(b), "topics": [P.SWAP_TOPICS[0]]}
                for b in range(lo, hi + 1) for a in self.v23.get(b, ()) if a in want]


def _poller(chain, positions, state=None):
    state = state or vd.BscPollState(health_window_s=60.0, canary_gap_s=60.0)
    return P.BscVolumePoller(
        state, call=chain.call, positions_fn=lambda: positions,
        discover_fn=lambda mint, quotes=(): frozenset({V2POOL}),
        executed_pool_fn=lambda pos: None, canary=CANARY, chain="1",
        v4_manager=MANAGER, v4_ids_fn=lambda mint: [V4ID], block_s=12.0), state


def test_the_chain_1_state_is_its_own_with_a_two_tick_health_window():
    state = vd.EVM_POLL_STATE["1"]
    assert state is not vd.EVM_POLL_STATE["56"]
    assert state.health_window_s == 60.0 and state.canary_gap_s == 60.0
    assert vd.EVM_POLL_STATE["56"].health_window_s == 5.0           # 56 unchanged


def test_a_tick_reads_v2_v3_by_address_and_v4_by_pool_id_on_the_manager():
    chain = Chain()
    poller, state = _poller(chain, [_pos()])
    poller.last = chain.head - 2
    chain.v23[chain.head - 1] = [V2POOL, CANARY]
    chain.v4[chain.head] = [V4ID]
    assert poller.tick(now_ts=T0 + 100) == "ok"
    methods = [m for m, _p in chain.calls]
    assert methods == ["eth_blockNumber", "eth_getLogs", "eth_getLogs"]
    v4_call = chain.calls[2][1][0]
    assert v4_call["address"] == MANAGER and v4_call["topics"] == [V4_SWAP, [V4ID]]
    assert state._activity_at[V4ID] == pytest.approx(T0 + 100)          # head block: now
    assert state._activity_at[V2POOL] == pytest.approx(T0 + 100 - 12)   # one block behind: -12 s


def test_a_position_on_another_chain_is_not_bound():
    chain = Chain()
    other = dict(_pos(), network_id="56")
    poller, _state = _poller(chain, [other])
    assert poller.tick(now_ts=T0 + 100) == "idle" and chain.calls == []


def test_two_ticks_of_quiet_keep_the_rail_healthy_and_fire_at_exit_s():
    chain = Chain()
    poller, state = _poller(chain, [_pos()])
    poller.last = chain.head - 2
    chain.v23[chain.head] = [V2POOL, CANARY]
    chain.v4[chain.head] = [V4ID]
    poller.tick(now_ts=T0 + 100)
    for i in (1, 2):                                   # two quiet ticks, the canary still trading
        chain.head += 2
        chain.v23[chain.head] = [CANARY]
        poller.tick(now_ts=T0 + 100 + 24 * i)
    confirm = lambda pools, first, last: []             # noqa: E731 (the chain says still)
    fire, silence, why = state.decision(_pos(), now_iso="2026-10-08T18:56:31+00:00",
                                        exit_s=30.0, confirm_fn=confirm)
    assert why == "measured_state_still" and fire is True and silence == pytest.approx(48.0)


def test_the_confirm_reads_v4_ids_on_the_manager_and_needs_the_canary():
    chain = Chain()
    chain.v4[chain.head] = [V4ID]
    chain.v23[chain.head] = [CANARY]
    confirm = P.confirm_logs_fn(chain.call, CANARY, v4_manager=MANAGER)
    logs = confirm([V2POOL, V4ID], chain.head - 2, chain.head)
    assert [l["topics"][1] for l in logs] == [V4ID]
    chain.v23[chain.head] = []
    assert confirm([V2POOL, V4ID], chain.head - 2, chain.head) is None   # no canary: blind


def test_the_chain_1_host_is_the_keyless_eth_drpc_never_a_funded_one(monkeypatch):
    """The keyed research account is saturated (its pacer queued ~7 slots, 2026-10-08), so chain 1
    uses the keyless eth.drpc.org with its own pacer; an override names another host."""
    monkeypatch.delenv("MEMECOIN_VOLDEATH_POLL_URL_1", raising=False)
    assert P.poll_url("1") == "https://eth.drpc.org"
    monkeypatch.setenv("MEMECOIN_VOLDEATH_POLL_URL_1", "https://other.example/rpc")
    assert P.poll_url("1") == "https://other.example/rpc"
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_1", "https://mainnet.infura.io/v3/x")
    assert P.archive_host_refusal("1", "https://eth.drpc.org") is None
    assert P.archive_host_refusal("1", "https://mainnet.infura.io/v3/other")


def test_a_429_pauses_the_hosts_own_pacer_and_raises(monkeypatch):
    import io
    import urllib.error
    paused = []

    class Pacer:
        def acquire(self):
            pass

        def pause(self, s):
            paused.append(s)

    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 429, "too many", {}, io.BytesIO(b""))
    monkeypatch.setattr(P.urllib.request, "urlopen", boom)
    call = P.make_call("https://eth.drpc.org", pacer=Pacer())
    with pytest.raises(urllib.error.HTTPError):
        call("eth_blockNumber", [])
    assert paused == [P.PACER_PAUSE_S]


def test_the_runner_starts_the_chain_1_poller_beside_56s():
    src = open("scripts/memecoin_runner_loop.py").read()
    assert "_start_chain1_volume_poll()" in src and "start_chain1_volume_poller(" in src


def test_the_starter_refuses_a_funded_host(monkeypatch):
    monkeypatch.setenv("MEMECOIN_VOLDEATH_POLL_CHAINS", "56,1")
    monkeypatch.setenv("MEMECOIN_VOLDEATH_POLL_URL_1", "https://mainnet.infura.io/v3/a")
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_1", "https://mainnet.infura.io/v3/b")
    said = []
    assert P.start_chain1_volume_poller(positions_fn=lambda: [], entry_tx_fn=lambda p: None,
                                        thread_factory=lambda **k: pytest.fail("started"),
                                        log=said.append) is False
    assert "NOT started" in said[0]


def test_the_chain_1_confirm_reads_to_the_current_head_not_the_last_polled_one(monkeypatch):
    """Alpha: the threshold confirm must read the full window. A 24 s tick leaves up to two blocks
    no tick has read; ending at the last polled head would call a coin that just traded still."""
    calls = []

    def call(method, params):
        calls.append(params[0])
        return [{"address": CANARY, "topics": []}] if params[0].get("address") != MANAGER else []
    monkeypatch.setitem(P._RUNTIME, "1", {"confirm_call": call, "canary": CANARY,
                                          "v4_manager": MANAGER, "confirm_to_latest": True})
    assert P.confirm_fn_for("1")([V2POOL, V4ID], 100, 105) == []
    assert [c["toBlock"] for c in calls] == ["latest", "latest"]
    monkeypatch.setitem(P._RUNTIME, "56", {"confirm_call": call, "canary": CANARY})
    calls.clear()
    P.confirm_fn_for("56")([V2POOL], 100, 105)
    assert [c["toBlock"] for c in calls] == [hex(105)]                 # 56 unchanged


def test_the_confirm_starts_at_the_block_after_the_last_swap_even_in_the_head_block():
    """Last swap in the tick's head block: the confirm must start after it, or the coin reads
    'moving' on its own last swap and loses a whole exit window."""
    chain = Chain()
    poller, state = _poller(chain, [_pos()])
    poller.last = chain.head - 2
    swap_block = chain.head
    chain.v4[swap_block] = [V4ID]
    chain.v23[swap_block] = [V2POOL, CANARY]
    poller.tick(now_ts=T0 + 100)
    for i in (1, 2):
        chain.head += 2
        chain.v23[chain.head] = [CANARY]
        poller.tick(now_ts=T0 + 100 + 24 * i)
    seen = []

    def confirm(pools, first, last):
        seen.append(first)
        return []
    fire, _silence, why = state.decision(_pos(), now_iso="2026-10-08T18:56:31+00:00", exit_s=30.0,
                                         confirm_fn=confirm)
    assert seen == [swap_block + 1] and fire is True, (seen, why)


# --- Gamma on 93eb73537 ---------------------------------------------------------------------------------

def test_a_failed_confirm_is_not_repeated_every_sweep(monkeypatch):
    """MEDIUM: the decision calls the confirm on every sweep once silence >= exit_s, and the Ethereum
    worker sweeps every 2 s on the thread chain-1 entries share. After a failed confirm the chain
    waits CONFIRM_COOLDOWN_S before asking again: one timeout per window, not one per sweep."""
    clock = [1000.0]
    monkeypatch.setattr(P.time, "monotonic", lambda: clock[0])
    calls = []

    def call(method, params):
        calls.append(params[0])
        raise TimeoutError("slow host")
    confirm = P.confirm_logs_fn(call, CANARY, v4_manager=MANAGER, cooldown_s=15.0)
    assert confirm([V2POOL, V4ID], 1, 2) is None
    clock[0] += 2.0
    assert confirm([V2POOL, V4ID], 1, 2) is None
    assert len(calls) == 1
    clock[0] += 14.0
    assert confirm([V2POOL, V4ID], 1, 2) is None
    assert len(calls) == 2


def test_the_runner_docstring_names_the_keyless_host():
    src = open("scripts/memecoin_runner_loop.py").read()
    start = src.index("def _start_chain1_volume_poll():")
    body = src[start:start + 600]
    assert "keyless" in body and "dRPC research key" not in body


def test_every_decision_gets_the_same_stateful_confirm(monkeypatch):
    """The cooldown lives in the confirm the starter built; a fresh closure per decision would
    forget it."""
    marker = object()
    monkeypatch.setitem(P._RUNTIME, "1", {"confirm": marker, "confirm_call": None, "canary": CANARY})
    assert P.confirm_fn_for("1") is marker and P.confirm_fn_for("1") is marker


def test_an_unreadable_canary_at_start_is_retried_off_path_until_the_poller_starts(monkeypatch):
    """Gamma 2026-10-14: one HTTP 408 on the chain-1 canary at the 09:19:19Z start left vol_death blind
    for the runner's whole life. The starter now retries in its own daemon thread (bounded backoff)
    and starts the poller once the canary reads; one retry thread per chain."""
    monkeypatch.setenv("MEMECOIN_VOLDEATH_POLL_CHAINS", "1")
    monkeypatch.setattr(P, "poll_url", lambda chain: "https://keyless.example")
    monkeypatch.setattr(P, "archive_host_refusal", lambda chain, url: None)
    monkeypatch.setattr(P, "make_call", lambda *a, **k: (lambda *x, **y: None))
    answers = iter([None, None, CANARY])          # start fails, first retry fails, second reads
    monkeypatch.setattr(P, "canary_pool", lambda call, chain=None: next(answers))
    monkeypatch.setattr(P, "_retry_sleep", lambda s: None)
    monkeypatch.delitem(P._RUNTIME, "1", raising=False)
    monkeypatch.setattr(P, "_CANARY_RETRY", set())
    threads = []

    class T:
        def __init__(self, **k):
            self.k = k
        def start(self):
            threads.append(self.k)

    said = []
    assert P.start_chain1_volume_poller(positions_fn=lambda: [], entry_tx_fn=lambda p: None,
                                        thread_factory=T, log=said.append) is False
    assert "NOT started" in said[0]
    assert [t["name"] for t in threads] == ["memecoin-volpoll-canary-retry-1"]
    threads[0]["target"]()                         # the retry loop: one more failure, then success
    assert P._RUNTIME["1"]["canary"] == CANARY
    assert [t["name"] for t in threads][1:] == ["memecoin-chain1-volume-poll"]
    assert P._CANARY_RETRY == set()
