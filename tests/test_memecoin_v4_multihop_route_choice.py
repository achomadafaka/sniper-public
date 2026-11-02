"""The multihop search measures a bounded set of complete paths and keeps the cheapest (2026-09-17).

LuluDior (0x46f3c721, chain 4663) refused at -15.4% round trip. Each step returned one pool: the
deepest ETH/LULU pool charged a 5.01% fee, and the most liquid USDG/LULU pool returned no quote.
A read-only check minutes later measured -13.8% through that pool, -6.2% through a 0.50% ETH/LULU
pool, and -4.7% through ETH -> USDG -> LULU -> coin.

Every test here is offline. No live RPC, no provider, no production pool store.
"""
import json
import threading
import time

import pytest

from sniper.execution import evm_v4_multihop as MH
from sniper.execution import evm_v4_multihop_tx as TX

CHAIN = "4663"
NATIVE = "0x" + "00" * 20
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
LULU = "0x4e62068525ab11fe768e29dfd00ef909b9803016"
STOCK2 = "0x" + "33" * 20
COIN = "0x46f3c721d8ce66865bc7d598b3b8ba7fd6badaac"
DYNAMIC = 0x800000
HOOK = "0x4e3468951d49f2eea976ed0d6e75ffcb44a9a544"
AMOUNT = 10 ** 16


def _key(c0, c1, fee, ts=60, hooks=NATIVE):
    return {"currency0": c0, "currency1": c1, "fee": fee, "tick_spacing": ts, "hooks": hooks}


ETH_LULU_DEEP = _key(NATIVE, LULU, 50095, 501)        # 5.01%, the pool the old search took
ETH_LULU_CHEAP = _key(NATIVE, LULU, 5000, 100)        # 0.50%, shallower
USDG_LULU_DEAD = _key(LULU, USDG, 2197, 22)           # most liquid, returns no quote
USDG_LULU_OK = _key(LULU, USDG, 2523, 25)             # next pool, quotes
ETH_USDG = _key(NATIVE, USDG, 100, 1)
LULU_COIN = _key(COIN, LULU, DYNAMIC, 200, HOOK)


@pytest.fixture
def chain(monkeypatch):
    """A stubbed chain. `round_trip` maps a tuple of pool fees (cash -> coin) to the cash ratio."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_MAX_LEGS", 3, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MULTIHOP_SECOND_BRIDGES", {CHAIN: (USDG,)},
                        raising=False)
    st = {"round_trip": {}, "calls": [], "blocks": set(), "delay_s": 0.0, "lock": threading.Lock()}
    monkeypatch.setattr(MH, "_decision_block", lambda *a, **k: "0xabc")
    monkeypatch.setattr(MH, "_pools_with_depth",
                        lambda _c, token, **k: [("p_coin", 60_000.0)] if token == COIN else [])
    monkeypatch.setattr(MH, "_pool_key_for", lambda _c, pid, _r, **k: {"p_coin": LULU_COIN}[pid])
    monkeypatch.setattr(MH, "_direct_bridge_pools",
                        lambda _c, cash, bridge, **k: (
                            [("p_deep", 8_490.0), ("p_cheap", 2_100.0)]
                            if str(bridge).lower() == LULU else None))
    direct_keys = {"p_deep": ETH_LULU_DEEP, "p_cheap": ETH_LULU_CHEAP}
    real_key_for = MH._pool_key_for
    monkeypatch.setattr(MH, "_pool_key_for",
                        lambda _c, pid, _r, **k: direct_keys.get(pid) or real_key_for(_c, pid, _r))

    def stored(_c, a, b):
        pair = {str(a).lower(), str(b).lower()}
        if pair == {NATIVE, USDG}:
            return [("p_eth_usdg", ETH_USDG)]
        if pair == {USDG, LULU}:
            return [("p_dead", USDG_LULU_DEAD), ("p_ok", USDG_LULU_OK)]
        return []
    monkeypatch.setattr(MH, "_stored_pair_pools", stored)
    liquidity = {"p_eth_usdg": 10 ** 20, "p_dead": 89 * 10 ** 15, "p_ok": 67 * 10 ** 15}

    def read_liquidity(_c, pids, _r, **k):         # one batched extsload request per pair
        try:
            k["http"]("POST", "http://rpc", body="{}")
        except MH.RequestNotSent:
            return None
        return {pid: liquidity.get(pid) for pid in pids}
    monkeypatch.setattr(MH, "_onchain_liquidities", read_liquidity)
    MH._POOL_LIQUIDITY.clear()
    MH._BRIDGE_LAST_GOOD.clear()

    def quote_path(_chain, currency_in, path, amount_in, _rpc, block="latest", **_kw):
        # Model dispatch through the search transport, as the real `_quote_path` does via `_rpc`.
        transport = _kw.get("http")
        if transport is not None:
            try:
                transport("POST", "http://rpc", body="{}")
            except MH.RequestNotSent:
                return None
        if st["delay_s"]:
            time.sleep(st["delay_s"])
        fees = tuple(key["fee"] for _c, key in path)
        with st["lock"]:
            st["calls"].append((currency_in, fees, amount_in))
            st["blocks"].add(block)
        if str(currency_in).lower() == NATIVE:
            forward = tuple(fees)
            return 1_000 + len(st["calls"]) if forward in st["round_trip"] else None
        forward = tuple(reversed(fees))
        ratio = st["round_trip"].get(forward)
        return int(AMOUNT * ratio) if ratio else None
    monkeypatch.setattr(MH, "_quote_path", quote_path)
    return st


def _offline_transport(*_a, **_kw):
    return 200, {"jsonrpc": "2.0", "id": 1, "result": "0x0"}, None


def _search():
    return MH.multihop_quote(CHAIN, NATIVE, COIN, AMOUNT, rpc_url="http://rpc",
                             bridge_currencies=(), clip_usd=150.0, min_depth_clips=20.0,
                             http=_offline_transport)


LULU_ROUND_TRIPS = {
    (50095, DYNAMIC): 0.862,               # -13.8%
    (5000, DYNAMIC): 0.938,                # -6.2%
    (100, 2523, DYNAMIC): 0.953,           # -4.7%
}


def test_luludior_takes_the_cheapest_measured_path_not_the_deepest_pool(chain):
    chain["round_trip"] = dict(LULU_ROUND_TRIPS)

    route = _search()

    assert route is not None
    assert [leg["key"]["fee"] for leg in route["legs_path"]] == [100, 2523, DYNAMIC]
    assert round(route["round_trip_pct"], 1) == -4.7


def test_a_cheaper_shallow_bridge_pool_beats_a_deeper_expensive_one(chain):
    two_legs = {k: v for k, v in LULU_ROUND_TRIPS.items() if len(k) == 2}
    chain["round_trip"] = two_legs

    route = _search()

    assert route["bridge_pool_id"] == "p_cheap"
    assert round(route["round_trip_pct"], 1) == -6.2


def test_an_unquotable_most_liquid_pool_does_not_hide_the_next_pool(chain):
    """p_dead has the most liquidity and no quote. The old search measured only p_dead."""
    chain["round_trip"] = {(100, 2523, DYNAMIC): 0.953}

    route = _search()

    assert route is not None
    assert route["legs_path"][1]["key"] == USDG_LULU_OK
    assert any(fees == (100, 2197, DYNAMIC) for _c, fees, _a in chain["calls"]), \
        "the dead pool is still measured; it only stops hiding the pool behind it"


def test_every_path_is_quoted_at_one_block_and_sells_what_it_bought(chain):
    chain["round_trip"] = dict(LULU_ROUND_TRIPS)

    route = _search()

    assert chain["blocks"] == {"0xabc"}
    assert route["quote_block"] == "0xabc"
    bought = {fees: amount for c, fees, amount in chain["calls"] if c == NATIVE}
    # A forward quote's output is unique per call here, so each reverse sells its own path's amount.
    forward_outputs = {}
    for c, fees, amount in chain["calls"]:
        if c != NATIVE:
            forward_outputs[tuple(reversed(fees))] = amount
    for fees, amount in forward_outputs.items():
        assert fees in bought and amount != AMOUNT
    assert len(set(forward_outputs.values())) == len(forward_outputs)


def test_a_later_bridge_currency_is_searched_after_an_earlier_one_routes(chain, monkeypatch):
    """The old search stopped after the first bridge currency that produced any route."""
    coin_pools = [("p_coin", 60_000.0), ("p_coin2", 50_000.0)]
    coin2_key = _key(COIN, STOCK2, 3000)
    keys = {"p_coin": LULU_COIN, "p_coin2": coin2_key, "p_deep": ETH_LULU_DEEP,
            "p_cheap": ETH_LULU_CHEAP, "p_stock2": _key(NATIVE, STOCK2, 500)}
    monkeypatch.setattr(MH, "_pools_with_depth",
                        lambda _c, token, **k: coin_pools if token == COIN else [])
    monkeypatch.setattr(MH, "_pool_key_for", lambda _c, pid, _r, **k: keys[pid])
    monkeypatch.setattr(MH, "_direct_bridge_pools",
                        lambda _c, cash, bridge, **k: (
                            [("p_deep", 8_490.0)] if str(bridge).lower() == LULU
                            else [("p_stock2", 9_000.0)]))
    chain["round_trip"] = {(50095, DYNAMIC): 0.862, (500, 3000): 0.97}

    route = _search()

    assert route["bridge"] == STOCK2
    assert round(route["round_trip_pct"], 1) == -3.0


def test_the_deadline_stops_new_requests_and_ignores_late_answers(chain, monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_SEARCH_DEADLINE_S", 0.1, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_QUOTE_TIME_S", 0.0, raising=False)
    chain["round_trip"] = dict(LULU_ROUND_TRIPS)
    chain["delay_s"] = 0.3

    started = time.monotonic()
    route = _search()
    elapsed = time.monotonic() - started
    time.sleep(0.5)                          # let in-flight quotes finish

    assert route is None
    assert elapsed < 0.3, "the search waits for the deadline, not for the slow requests"
    assert MH.last_multihop_refusal() == "search_stopped:deadline"
    # Only the requests started before the deadline were sent: no reverse leg, no queued path.
    assert all(c == NATIVE for c, _f, _a in chain["calls"])
    assert len(chain["calls"]) <= 4


def test_the_request_budget_bounds_the_total_requests(chain, monkeypatch):
    """One liquidity read leaves 25 requests for quotes. Cached provider results cost no request."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_MAX_PATHS", 12, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_REQUEST_BUDGET", 26, raising=False)
    chain["round_trip"] = dict(LULU_ROUND_TRIPS)

    route = _search()

    assert route is not None
    assert round(route["round_trip_pct"], 1) == -6.2
    assert route["search_discovery_cut"] is True
    assert route["search_requests"] <= 26
    assert not any(2523 in fees for _c, fees, _a in chain["calls"])


def test_the_budget_keeps_the_quote_reserve_and_the_hard_cap():
    budget = MH._SearchBudget(10.0, 5, quote_reserve=3)

    assert budget.take(discovery=True) and budget.take(discovery=True)
    assert not budget.take(discovery=True)            # would enter the reserve
    assert budget.discovery_cut and budget.stop_reason is None
    assert budget.take() and budget.take() and budget.take()
    assert not budget.take()
    assert budget.stop_reason == "request_budget" and budget.used == 5


def test_the_fee_and_hook_controls_still_apply_to_every_option(chain, monkeypatch):
    """A dynamic-fee pool is never a cash leg, however many options each step offers."""
    keys = {"p_dyn": _key(NATIVE, LULU, DYNAMIC), "p_cheap": ETH_LULU_CHEAP,
            "p_coin": LULU_COIN}
    monkeypatch.setattr(MH, "_pool_key_for", lambda _c, pid, _r, **k: keys[pid])
    monkeypatch.setattr(MH, "_direct_bridge_pools",
                        lambda _c, cash, bridge, **k: (
                            [("p_dyn", 99_000_000.0), ("p_cheap", 2_100.0)]
                            if str(bridge).lower() == LULU else None))
    chain["round_trip"] = {(DYNAMIC, DYNAMIC): 0.999, (5000, DYNAMIC): 0.938}

    route = _search()

    assert route["bridge_pool_id"] == "p_cheap"
    assert all(fees[0] != DYNAMIC for c, fees, _a in chain["calls"] if c == NATIVE)


def test_the_stored_selected_path_is_reversed_exactly_on_exit(chain, monkeypatch):
    chain["round_trip"] = dict(LULU_ROUND_TRIPS)
    route = _search()
    built = TX.as_zerox_quote(CHAIN, NATIVE, COIN, route, AMOUNT, 900,
                              out_amount=1_000, taker="0x" + "11" * 20)
    stored = json.loads(json.dumps(built["route"]))       # what the ledger keeps

    seen = []
    monkeypatch.setattr(MH, "_quote_path",
                        lambda _c, currency_in, path, amount, _r, **k: (
                            seen.append((currency_in, path)) or 5_000))
    monkeypatch.setattr(TX, "_permit2_input", lambda *a, **k: "00" * 64)
    exit_quote = MH.dex_v4_multihop_exit_quote(
        CHAIN, COIN, 777, "0x" + "11" * 20, route=stored, rpc_url="http://rpc",
        acct=object(), clip_usd=150.0)

    assert exit_quote is not None
    currency_in, sell_path = seen[0]
    assert currency_in == COIN
    assert [c for c, _k in sell_path] == [LULU, USDG, NATIVE]
    assert [k for _c, k in sell_path] == [LULU_COIN, USDG_LULU_OK, ETH_USDG]
    exit_tx = TX.build_multihop_exit(CHAIN, COIN, NATIVE, stored, 777, 1,
                                     taker="0x" + "11" * 20, acct=object())
    assert exit_tx["path_currencies"] == [LULU, USDG, NATIVE]


def test_a_legacy_two_leg_route_without_a_path_still_exits_through_its_own_pools(monkeypatch):
    legacy = {"bridge": LULU, "bridge_key": ETH_LULU_DEEP, "coin_key": LULU_COIN}
    used = []
    monkeypatch.setattr(MH, "_quote_leg",
                        lambda _c, key, zero_for_one, amount, _r, **k: used.append(key) or 5_000)
    monkeypatch.setattr(TX, "_permit2_input", lambda *a, **k: "00" * 64)

    exit_quote = MH.dex_v4_multihop_exit_quote(
        CHAIN, COIN, 777, "0x" + "11" * 20, route=legacy, rpc_url="http://rpc",
        acct=object(), clip_usd=150.0)

    assert exit_quote is not None
    assert used == [LULU_COIN, ETH_LULU_DEEP]
