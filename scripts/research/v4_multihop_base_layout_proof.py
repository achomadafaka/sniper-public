#!/usr/bin/env python3
"""Read-only proof that Base's Universal Router takes the FOUR-field multihop ExactInputParams
(Gamma LOW 1 on 1192ad309, Alpha 2026-10-01).

One real Base multihop route is found by the production search (evm_v4_multihop.multihop_quote,
store_only: our PoolKey store, no third-party provider). Its EXIT (coin -> bridge -> ETH) is built by
the production builder (build_multihop_exit) and run by eth_call on Base state against the research
host https://base.gateway.tenderly.co (rule 0724bc08's 8453 research host), from a THROWAWAY address
whose key lives in memory and is never funded. State overrides give it the coin and an ERC-20 allowance
to Permit2 (slots found by the production read-back discovery) and native for gas; the Permit2
allowance comes from the builder's own signed PERMIT2_PERMIT.

Rows (each must match its expectation, so a pass cannot come from a call that never reached the swap):
    four_field      the production bytes on 8453                         must pass
    five_field      the same exit forced to the five-field layout         must revert
    min_out_huge    four-field with an impossible minimum                 must revert
    no_coin         four-field without the coin balance override          must revert

Exit 0 = every row matched, 1 = a mismatch, 2 = no route or the node refused (no verdict).
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from eth_account import Account

import sniper.execution.evm_v4_quote as q
import sniper.execution.evm_v4_multihop as mh
import sniper.execution.evm_v4_multihop_tx as T
from sniper.execution.evm_token_safety import _map_slot, _nested_slot, _SLOT_CANDIDATES

URL = "https://base.gateway.tenderly.co"
CHAIN = "8453"
CONF = q.V4[CHAIN]
UR, PERMIT2 = CONF["universal_router"], CONF["permit2"]
SPACING_S = 0.5
CALLS = []
_last = [0.0]


def rpc(method, params):
    wait = SPACING_S - (time.monotonic() - _last[0])
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(URL, json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                                                  "params": params}).encode(),
                                 {"Content-Type": "application/json", "User-Agent": "sniper-research/1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            out = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        out = {"error": {"http": exc.code}}
    except Exception as exc:
        out = {"error": {"transport": "%s: %s" % (type(exc).__name__, exc)}}
    _last[0] = time.monotonic()
    CALLS.append(method)
    return out.get("result"), out.get("error")


_FOUND = {}


def overrides(taker, coin, amount, *, with_coin=True):
    """The coin balance and its ERC-20 allowance to Permit2 for `taker`: the production read-back
    slot discovery (evm_flap_curve.discover_sell_overrides) when it finds the layout, else a spray
    over the common slots."""
    if with_coin:
        if coin not in _FOUND:
            from sniper.execution.evm_flap_curve import discover_sell_overrides
            _FOUND[coin] = discover_sell_overrides(coin, PERMIT2, amount, taker=taker, rpc_url=URL)
        if _FOUND[coin]:
            return {**_FOUND[coin], taker: {"balance": hex(10 ** 18)}}
    diff = {}
    if with_coin:
        for i in _SLOT_CANDIDATES:
            diff[_map_slot(taker, i)] = "0x" + q._w(amount)
            diff[_nested_slot(PERMIT2, taker, i)] = "0x" + q._w((1 << 160) - 1)
    out = {taker: {"balance": hex(10 ** 18)}}
    if diff:
        out[coin] = {"stateDiff": diff}
    return out


def build(route, coin, amount, min_out, acct, *, four):
    real = q._EXACT_INPUT_FOUR_FIELD_CHAINS
    if not four:
        q._EXACT_INPUT_FOUR_FIELD_CHAINS = frozenset(real - {CHAIN})
    try:
        return T.build_multihop_exit(CHAIN, coin, q._NATIVE, {**route, "rpc_url": URL}, amount, min_out,
                                     taker=acct.address, acct=acct, now_fn=time.time)
    finally:
        q._EXACT_INPUT_FOUR_FIELD_CHAINS = real


def find_route(candidates):
    for coin in candidates:
        route = mh.multihop_quote(CHAIN, q._NATIVE, coin, 2 * 10 ** 15, rpc_url=URL, clip_usd=5.0,
                                  store_only=True)
        if route:
            return coin, route
    return None, None


def main(candidates):
    result, error = rpc("eth_chainId", [])
    if error or int(result, 16) != 8453:
        print("node refused or wrong chain:", result, error)
        return 2
    coin, route = find_route(candidates)
    if not route:
        print("no multihop route among %d candidates" % len(candidates))
        return 2
    amount = int(route["amount_out"])
    print("route coin %s bridge %s legs %d amount_out %d round_trip %.2f%%" % (
        coin, route.get("bridge"), int(route.get("legs") or len(route.get("legs_path") or [])), amount,
        float(route.get("round_trip_pct") or 0)))
    acct = Account.create()
    rows = [("four_field", build(route, coin, amount, 1, acct, four=True), True, True),
            ("five_field", build(route, coin, amount, 1, acct, four=False), True, False),
            ("min_out_huge", build(route, coin, amount, 1 << 120, acct, four=True), True, False),
            ("no_coin", build(route, coin, amount, 1, acct, four=True), False, False)]
    mismatches = 0
    for name, built, with_coin, expect_ok in rows:
        if built is None:
            print(name, "BUILD FAILED"); mismatches += 1; continue
        tx = {"from": acct.address, "to": built["to"], "data": built["data"], "value": built["value"],
              "gas": hex(3_000_000)}
        res, err = rpc("eth_call", [tx, "latest", overrides(acct.address, coin, amount, with_coin=with_coin)])
        if isinstance(err, dict) and ("http" in err or "transport" in err):
            print(name, "NODE REFUSED", err); return 2
        ok = err is None
        verdict = "match" if ok == expect_ok else "MISMATCH"
        mismatches += ok != expect_ok
        print("%-13s %s (expected %s): %s" % (name, "ok" if ok else "revert", "ok" if expect_ok else "revert",
                                             verdict), "" if ok else json.dumps(err)[:160])
    print("calls:", {m: CALLS.count(m) for m in set(CALLS)})
    return 0 if mismatches == 0 else 1


if __name__ == "__main__":
    sys.exit(main([a.lower() for a in sys.argv[1:]]))
