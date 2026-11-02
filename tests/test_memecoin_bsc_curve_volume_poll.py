"""Portal trade ABI: flap-sh/FlapVaultExample src/flap/IPortal.sol TokenBought/TokenSold.

Both carry seven non-indexed words; token is the second word, never topics[1].
"""
import json
from pathlib import Path

import pytest
from eth_abi import encode
from eth_utils import keccak

from sniper.execution.evm_flap_curve import FLAP_PORTAL
from sniper.execution.evm_pancake_infinity import MANAGER, SWAP_TOPIC
from sniper.services import memecoin_bsc_volume_poll as P
from sniper.services import memecoin_volume_death as vd
from sniper.services import memecoin_executed_pool as X

MINT, OTHER, CANARY = ("0x" + byte * 20 for byte in ("11", "22", "33"))
PID = "0x" + "aa" * 32
FLAP = "flap:" + MINT
BUY, SELL = ("0x" + keccak(text=f"{name}(uint256,address,address,uint256,uint256,uint256,uint256)").hex()
             for name in ("TokenBought", "TokenSold"))


def pos():
    return {"id": 1, "network_id": "56", "mint": MINT,
            "funded_contract_json": "{}", "evm_route_json": json.dumps(
                {"protocol": "flap_curve", "curve": FLAP_PORTAL["56"]})}


def trade(mint=MINT, topic=BUY):
    return {"address": FLAP_PORTAL["56"], "topics": [topic], "blockNumber": "0x64",
            "data": "0x" + encode(["uint256", "address", "address", "uint256", "uint256", "uint256", "uint256"],
                                   [100, mint, OTHER, 1, 1, 0, 1]).hex()}


def call_for(extra):
    def call(method, params):
        if method == "eth_blockNumber":
            return "0x64"
        if method == "eth_getLogs":
            return [{"address": CANARY, "topics": [P.V2_SWAP]}, *extra]
        raise AssertionError(method)
    return call


def poller(extra=(), found=frozenset()):
    state = vd.BscPollState()
    obj = P.BscVolumePoller(state, call=call_for(extra), positions_fn=lambda: [pos()],
                            discover_fn=lambda mint, quotes: found,
                            executed_pool_fn=lambda p: None, canary=CANARY)
    return obj, state


@pytest.mark.parametrize("topic", [BUY, SELL])
def test_flap_curve_binds_token_and_counts_only_its_portal_trades(topic):
    p, state = poller([trade(OTHER), trade(topic=topic)])
    assert p.tick(now_ts=100) == "ok"
    assert state._bindings == {FLAP: MINT}
    assert state._activity_at == {FLAP: 100}


def test_unknown_curve_and_failed_discovery_do_not_create_coverage():
    p, state = poller(found=None)
    assert p.tick(now_ts=100) == "idle"
    assert state.coverage_snapshot(100)["pools_covered"] == 0
    p.positions_fn = lambda: [dict(pos(), evm_route_json=json.dumps({"curve": OTHER}))]
    assert p.bindings(100) == {}


def test_malformed_portal_trade_blinds_entire_range():
    p, state = poller([dict(trade(), data="0x")])
    assert p.tick(now_ts=100) == "blind"
    assert state.coverage_snapshot(100)["pools_covered"] == 0


def test_confirm_filters_other_tokens_and_checks_portal_trades_without_extra_rpc():
    calls = []
    def call(method, params):
        calls.append(params[0])
        return call_for([trade(OTHER)])(method, params)
    confirm = P.confirm_logs_fn(call, CANARY)
    assert confirm([FLAP], 99, 100) == []
    assert len(calls) == 1
    assert FLAP_PORTAL["56"] in calls[0]["address"] and MINT not in calls[0]["address"]
    confirm = P.confirm_logs_fn(call_for([trade()]), CANARY)
    assert confirm([FLAP], 99, 100)[0]["address"] == FLAP


@pytest.mark.parametrize("extra", [[dict(trade(), data="0x")], None])
def test_flap_confirm_malformed_or_failed_response_remains_unknown(extra):
    call = (lambda *a: None) if extra is None else call_for(extra)
    confirm = P.confirm_logs_fn(call, CANARY)
    assert confirm([FLAP], 99, 100) is None


def test_infinity_receipt_requires_its_own_manager_and_topic():
    log = {"address": MANAGER, "topics": [SWAP_TOPIC, PID, "0x" + "00" * 32]}
    assert X.executed_pool_from_receipt("56", {"logs": [log]}) == (PID, "receipt_infinity_swap", 1)
    assert X.executed_pool_from_receipt("1", {"logs": [log]}) == (None, None, 0)
    assert X.executed_pool_from_receipt("56", {"logs": [dict(log, address=OTHER)]}) == (None, None, 0)


def test_infinity_pool_ids_are_polled_and_confirmed_on_its_own_manager():
    seen = []
    def call(method, params):
        if method == "eth_blockNumber":
            return "0x64"
        seen.append(params[0])
        assert MANAGER in params[0]["address"]
        return [{"address": CANARY, "topics": [P.V2_SWAP]},
                {"address": MANAGER, "topics": [SWAP_TOPIC, PID, "0x" + "00" * 32],
                 "data": "0x" + encode(["int128", "int128", "uint160", "uint128", "int24", "uint24", "uint16"],
                                         [1, -1, 1, 1, 1, 1, 1]).hex()}]
    state = vd.BscPollState()
    p = P.BscVolumePoller(state, call=call, positions_fn=lambda: [dict(pos(), evm_route_json="{}")],
                          discover_fn=lambda m, q: frozenset(), executed_pool_fn=lambda p: PID,
                          canary=CANARY, v4_manager=MANAGER, v4_swap_topic=SWAP_TOPIC,
                          v4_ids_fn=lambda mint: [PID])
    assert p.tick(now_ts=100) == "ok"
    assert state._activity_at[PID] == 100
    confirm = P.confirm_logs_fn(call, CANARY, v4_manager=MANAGER, v4_swap_topic=SWAP_TOPIC)
    assert confirm([PID], 99, 100)[0]["address"] == PID
    assert SWAP_TOPIC in seen[-1]["topics"][0]
    assert len(seen) == 2  # One poll getLogs and one confirmation getLogs.


def test_arc_executed_v4_pool_survives_receipt_and_length_filter():
    log = {"address": P.ARC["v4_manager"], "topics": [P.V4_SWAP, PID]}
    fn = P.executed_pool_fn_for(lambda m, p: {"logs": [log]}, lambda p: "tx", chain="5042")
    assert fn({}) == PID


def test_real_arc_entry_receipt_resolves_the_executed_v4_pool():
    receipt = json.loads((Path(__file__).parent / "fixtures/memecoin/arc_executed_v4_receipt.json").read_text())
    fn = P.executed_pool_fn_for(lambda m, p: receipt, lambda p: receipt["transactionHash"], chain="5042")
    assert fn({}) == "0x6fc6b7375376ebb0d4755566df6e529073d250f7adfbd9d28a1d29b809203537"


def test_flap_bindings_keep_known_graduated_pairs_and_add_new_discovered_pools():
    p, state = poller()
    position = dict(pos(), funded_contract_json=json.dumps(
        {"classification": {"facts": {"liquidity_pair_address": OTHER}}}))
    p.positions_fn = lambda: [position]
    assert p.bindings(100) == {FLAP: MINT, OTHER: MINT}
    p.discover_fn = lambda m, q: {CANARY}
    assert p.bindings(121) == {FLAP: MINT, OTHER: MINT, CANARY: MINT}
    assert state.coverage_snapshot(121)["pools_covered"] == 0


def test_infinity_cache_failure_keeps_even_the_executed_pool_uncovered():
    p, state = poller()
    p.v4_manager = MANAGER
    p.executed_pool_fn = lambda pos: PID
    def unavailable(mint):
        raise RuntimeError("cache unavailable")
    p.v4_ids_fn = unavailable
    assert p.tick(now_ts=100) == "idle"
    assert state.coverage_snapshot(100)["pools_covered"] == 0


def test_real_portal_and_infinity_logs_decode_and_remain_coin_specific():
    actual = json.loads((Path(__file__).parent / "fixtures/memecoin/bsc_curve_volume_logs.json").read_text())["logs"]
    mint = "0x3215741a61a2b9453215e80f874a7be5ae4d7777"
    key, pid = "flap:" + mint, actual["infinity"]["topics"][1]
    assert [l["address"] for l in P._bound_logs(list(actual.values()), {key, pid}, infinity=True)] == [key, pid]
    assert P._bound_logs(list(actual.values()), {FLAP, PID}, infinity=True) == []
    assert X.executed_pool_from_receipt("56", {"logs": [actual["infinity"]]}) == (pid, X.SOURCE_INFINITY, 1)


def test_any_malformed_infinity_response_freezes_confirmation():
    malformed = {"address": MANAGER, "topics": [SWAP_TOPIC, PID], "data": "0x"}
    confirm = P.confirm_logs_fn(call_for([malformed]), CANARY, v4_manager=MANAGER, v4_swap_topic=SWAP_TOPIC)
    assert confirm([PID], 99, 100) is None


def test_successful_portal_read_without_canary_never_proves_silence():
    confirm = P.confirm_logs_fn(lambda m, p: [trade(OTHER)], CANARY)
    assert confirm([FLAP], 99, 100) is None


def test_startup_wires_cached_infinity_keys_and_one_call_confirmation(monkeypatch):
    from sniper.execution import evm_pancake_infinity as inf
    monkeypatch.setenv("MEMECOIN_VOLDEATH_POLL_CHAINS", "56")
    monkeypatch.setattr(P, "archive_url", lambda chain: "https://volume.example")
    monkeypatch.setattr(P, "canary_pool", lambda call: CANARY)
    monkeypatch.setattr(P, "make_call", lambda *a, **k: call_for([]))
    monkeypatch.setattr(inf, "stored_keys", lambda mint, **kw: [(PID, {})])
    monkeypatch.setattr(P, "_RUNTIME", {})
    class Thread:
        def __init__(self, **kw):
            pass
        def start(self):
            pass
    assert P.start_bsc_volume_poller(positions_fn=lambda: [pos()], entry_tx_fn=lambda p: None,
                                    thread_factory=Thread)
    p = P._RUNTIME["56"]["poller"]
    assert p.v4_manager == MANAGER and p.v4_swap_topic == SWAP_TOPIC
    assert p.v4_ids_fn(MINT) == [PID]
    assert P.confirm_fn_for("56")([FLAP, PID], 99, 100) == []


@pytest.mark.parametrize("response", [None, {}, {"logs": None}, {"logs": "broken"}])
def test_unreadable_entry_receipt_never_grants_partial_pool_coverage(response):
    p, state = poller(found={OTHER})
    p.executed_pool_fn = P.executed_pool_fn_for(lambda m, a: response, lambda p: "tx")
    assert p.tick(now_ts=100) == "idle"
    assert state.coverage_snapshot(100)["pools_covered"] == 0


def test_receipt_transport_error_retries_without_binding_factory_subset():
    responses = [TimeoutError(), {"logs": []}]
    def call(m, a):
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result
    p, state = poller(found={OTHER})
    p.executed_pool_fn = P.executed_pool_fn_for(call, lambda p: "tx")
    assert p.tick(now_ts=100) == "idle"
    assert p.tick(now_ts=102) == "ok"
    assert state._bindings == {OTHER: MINT, FLAP: MINT}


@pytest.mark.parametrize("bad", ["0x" + "zz" * 32, "0xabc", ""])
def test_arc_malformed_executed_pool_never_binds_partial_discovery(bad):
    log = {"address": P.ARC["v4_manager"], "topics": [P.V4_SWAP, bad]}
    p, state = poller(found={OTHER})
    p.chain = "5042"
    p.positions_fn = lambda: [dict(pos(), network_id="5042", evm_route_json="{}")]
    p.v4_manager = P.ARC["v4_manager"]
    p.executed_pool_fn = P.executed_pool_fn_for(lambda m, a: {"logs": [log]}, lambda p: "tx", chain="5042")
    assert p.tick(now_ts=100) == "idle"
    assert state.coverage_snapshot(100)["pools_covered"] == 0


@pytest.mark.parametrize("broken", [True, False])
def test_v4_complete_cache_reader_never_hides_db_error_or_bad_pool_id(monkeypatch, broken):
    from sniper.execution.evm_v4_quote import _stored_pools_for_mint
    from sniper.persistence import db_core
    class Conn:
        def execute(self, *args):
            if broken:
                raise RuntimeError("cache locked")
            return self
        def fetchall(self):
            return [("0x" + "zz" * 32,)]
        def close(self):
            pass
    monkeypatch.setattr(db_core, "get_readonly_connection", lambda: Conn())
    with pytest.raises(RuntimeError if broken else ValueError):
        _stored_pools_for_mint("5042", MINT, require_complete=True)
