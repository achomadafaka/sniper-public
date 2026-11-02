"""Read-only proof of the 4663 V4 buy that unwraps WETH INSIDE the Universal Router call (2026-09-28).

Today a WETH-bankroll V4 buy on 4663 sends WETH.withdraw(amount) as a pre-transaction, waits for
its receipt (p50 1203 ms / p90 1825 ms on 18 fills since 09-21), then sends UR [V4_SWAP] with
value=amount. The candidate shape is ONE transaction, value 0:

    native pool:  UR [PERMIT2_PERMIT(WETH), PERMIT2_TRANSFER_FROM(WETH -> router),
                      UNWRAP_WETH(router), V4_SWAP(settle native from the router)]
    WETH pool:    UR [PERMIT2_PERMIT(WETH), PERMIT2_TRANSFER_FROM(WETH -> router),
                      V4_SWAP(path form, settle WETH from the router)]

Everything here is eth_call with state overrides against robinhood-rpc.publicnode.com, from a
THROWAWAY address whose key is generated in memory and never funded. The overrides give it WETH
and a WETH->Permit2 ERC-20 allowance (the approval the executor would send), and native for gas.
No custody key, no custody RPC, no broadcast. At most one call every 3 s (publicnode answers a
bare HTTP 403 after about 30 research calls in 5 minutes, and the EVM watcher reads 4663 there).

Per pool it runs the candidate shape plus controls that must FAIL, so a pass cannot come from a
call that never reached the swap:
    legacy       today's value-funded shape                         must pass (pool is live)
    in_swap      the candidate                                      must pass
    no_unwrap    candidate without UNWRAP_WETH (native pools)       must revert
    min_out_2x   candidate with min-out doubled                     must revert
    no_approval  candidate without the WETH->Permit2 allowance      must revert

    python3 scripts/research/v4_in_swap_unwrap_proof.py [native_hooked native_plain weth_hooked]
A later pool of the same kind is tried only when the earlier one has no quote.
Exit 0 = every row matched its expectation, 1 = a mismatch, 2 = the node refused (no verdict).
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
from sniper.execution.evm_token_safety import _map_slot, _nested_slot, _SLOT_CANDIDATES

URL = "https://robinhood-rpc.publicnode.com"
UA = "curl/8.5.0"
SPACING_S = 3.0
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
CONF = q.V4["4663"]
UR, PERMIT2 = CONF["universal_router"], CONF["permit2"]
_CMD_PERMIT2_TRANSFER_FROM, _CMD_UNWRAP_WETH = 0x02, 0x0C
AMOUNT = 10 ** 15                              # 0.001 WETH, about a tenth of a live clip

# Pool keys our own 4663 fills quoted (memecoin_entry_attempts traces, evm.quote_initial v4 rung).
POOLS = [
    ("native_hooked", dict(currency0=q._NATIVE, currency1="0xcabb5484150745a3349b956da5d6e97bd7c8dfd9",
                           fee=0, tick_spacing=200, hooks="0xe5e702641ea86f4ae6cc3cdaed2b886f976be044")),
    ("native_plain", dict(currency0=q._NATIVE, currency1="0x715f3014f710d651f292968dfd0e6fd91c9a9cc4",
                          fee=39000, tick_spacing=390, hooks=q._ZERO_ADDRESS)),
    ("native_plain", dict(currency0=q._NATIVE, currency1="0x6249519883b8d7ccf915dfcd6c0442984dae9d24",
                          fee=9000, tick_spacing=90, hooks=q._ZERO_ADDRESS)),
    ("weth_hooked", dict(currency0="0x082235ffc87a25b6c7c4fbf7aa4fc7932eb36ba3", currency1=WETH,
                         fee=0x800000, tick_spacing=200, hooks="0x4e3468951d49f2eea976ed0d6e75ffcb44a9a544")),
]

_last = [0.0]
CALLS = []


def rpc(method, params):
    """One JSON-RPC call, spaced. Returns (result, error); error is the node's JSON-RPC error or an
    HTTP/transport failure, never folded into a null result."""
    wait = SPACING_S - (time.monotonic() - _last[0])
    if wait > 0:
        time.sleep(wait)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json",
                                                          "User-Agent": UA})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            out = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        out = {"error": {"http": exc.code}}
    except Exception as exc:
        out = {"error": {"transport": "%s: %s" % (type(exc).__name__, exc)}}
    finally:
        _last[0] = time.monotonic()
    CALLS.append((method, round((_last[0] - started) * 1000.0)))
    return out.get("result"), out.get("error")


def quote(key, zero_for_one):
    data = ("0x" + q._SEL_QUOTE_SINGLE + q._w(0x20) + q._encode_pool_key(key)
            + q._w(1 if zero_for_one else 0) + q._w(AMOUNT) + q._w(0x100) + q._w(0))
    result, error = rpc("eth_call", [{"to": CONF["quoter"], "data": data}, "latest"])
    if error or not result or result == "0x":
        return None, error
    return int(result[2:66], 16), None


def overrides(taker, *, approved=True):
    weth = {_map_slot(taker, i): q._w(AMOUNT * 10) for i in _SLOT_CANDIDATES}
    weth = {k: "0x" + v for k, v in weth.items()}
    if approved:
        weth.update({_nested_slot(PERMIT2, taker, i): "0x" + q._w(10 ** 30) for i in _SLOT_CANDIDATES})
    return {taker: {"balance": hex(10 ** 18)}, WETH: {"stateDiff": weth}}


def permit_input(acct):
    real_nonce = q._permit2_nonce
    q._permit2_nonce = lambda *a, **k: 0       # a fresh address has Permit2 nonce 0: no read needed
    try:
        return q._permit2_input("4663", acct, WETH, UR, AMOUNT, URL, now_fn=time.time)
    finally:
        q._permit2_nonce = real_nonce


def shapes(kind, key, zf1, amount_out, acct):
    deadline = int(time.time()) + 600
    min_out = amount_out * 90 // 100
    hooked = q._pool_is_hooked(key)
    permit = permit_input(acct)
    pull = q._wa(WETH) + q._w(q._ADDRESS_THIS) + q._w(AMOUNT)       # PERMIT2_TRANSFER_FROM
    unwrap = q._w(q._ADDRESS_THIS) + q._w(AMOUNT)                    # UNWRAP_WETH(recipient, min)

    def swap(m):
        if hooked:
            return q._v4_swap_input_path(key, zf1, AMOUNT, m, payer_is_user=False,
                                         take_to=acct.address, chain="4663")
        return q._v4_swap_input(key, zf1, AMOUNT, m, take_all_to_sender=True)

    def ur(cmds, inputs):
        return q._ur_execute_data(bytes(cmds), inputs, deadline)

    P, T, U, S = q._CMD_PERMIT2_PERMIT, _CMD_PERMIT2_TRANSFER_FROM, _CMD_UNWRAP_WETH, q._CMD_V4_SWAP
    rows = []
    if kind.startswith("native"):
        rows.append(("legacy", ur([S], [swap(min_out)]), AMOUNT, True, True))
        rows.append(("in_swap", ur([P, T, U, S], [permit, pull, unwrap, swap(min_out)]), 0, True, True))
        rows.append(("no_unwrap", ur([P, T, S], [permit, pull, swap(min_out)]), 0, True, False))
    else:
        wrap = q._w(q._ADDRESS_THIS) + q._w(AMOUNT)
        rows.append(("legacy", ur([q._CMD_WRAP_ETH, S], [wrap, swap(min_out)]), AMOUNT, True, True))
        rows.append(("in_swap", ur([P, T, S], [permit, pull, swap(min_out)]), 0, True, True))
    body = [permit, pull] + ([unwrap] if kind.startswith("native") else [])
    cmds = [P, T] + ([U] if kind.startswith("native") else []) + [S]
    rows.append(("min_out_2x", ur(cmds, body + [swap(amount_out * 2)]), 0, True, False))
    rows.append(("no_approval", ur(cmds, body + [swap(min_out)]), 0, False, False))
    return rows


def main():
    acct = Account.create()
    print("throwaway taker %s (never funded, key in memory only)" % acct.address)
    mismatches = refused = 0
    only = set(sys.argv[1:])
    done = set()
    for kind, key in POOLS:
        if (only and kind not in only) or kind in done:
            continue
        zf1 = kind.startswith("native")           # native (currency0) -> coin; WETH is currency1 here
        amount_out, error = quote(key, zf1)
        if amount_out is None:
            print("%-13s quote unavailable: %s" % (kind, error))
            continue
        done.add(kind)                        # a later pool of the same kind is a fallback only
        print("%-13s quoter out %d for %d wei (%s)" % (kind, amount_out, AMOUNT, key["currency1"]))
        for name, data, value, approved, expect_ok in shapes(kind, key, zf1, amount_out, acct):
            tx = {"from": acct.address, "to": UR, "data": data, "value": hex(value), "gas": hex(3_000_000)}
            result, error = rpc("eth_call", [tx, "latest", overrides(acct.address, approved=approved)])
            if error and ("http" in error or "transport" in error):
                refused += 1
                print("  %-12s NODE REFUSED %s" % (name, error))
                continue
            ok = error is None
            verdict = "as expected" if ok == expect_ok else "MISMATCH"
            mismatches += ok != expect_ok
            print("  %-12s %-6s expected %-6s %s %s" % (name, "ok" if ok else "revert",
                                                       "ok" if expect_ok else "revert", verdict,
                                                       "" if ok else json.dumps(error)[:160]))
    print("calls %d, rtt ms %s" % (len(CALLS), [ms for _, ms in CALLS]))
    return 2 if refused else (1 if mismatches else 0)


def selftest():
    """The controls must differ from the candidate in exactly the piece they remove."""
    acct = Account.from_key("0x" + "11" * 32)
    q_permit, q._permit2_input = q._permit2_input, lambda *a, **k: "00" * 32
    try:
        rows = {r[0]: r for r in shapes("native_plain", POOLS[1][1], True, 10 ** 18, acct)}
    finally:
        q._permit2_input = q_permit
    assert rows["legacy"][2] == AMOUNT and rows["in_swap"][2] == 0
    assert rows["in_swap"][1] != rows["no_unwrap"][1] and rows["in_swap"][1] != rows["min_out_2x"][1]
    assert rows["in_swap"][1] == rows["no_approval"][1] and rows["no_approval"][3] is False
    print("selftest ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        selftest()
        sys.exit(0)
    sys.exit(main())
