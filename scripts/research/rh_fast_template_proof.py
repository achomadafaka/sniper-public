"""Read-only eth_call proof of the 4663 fast lane buy template (spec
2026-10-01-memecoin-4663-fast-lane-design.md §2.3; trap db0e84dc: verify a struct by eth_call on the
target chain before trusting it).

The template is `memecoin_rh_fast_decide.native_buy_tx`: UR [V4_SWAP] paid by msg.value from the
wallet's native float, no unwrap, no approval, no Permit2. Every call is an eth_call at ONE pinned
block on the 4663 research QuickNode (never a funded read host, Alpha 2026-10-01), from a THROWAWAY
address with a balance override. No key, no signature, no broadcast. Calls are spaced 0.5 s (the key
429s at about 10 calls in 1.5 s).

Per pool, rows that must pass and controls that must FAIL, so a pass cannot come from a call that
never reached the swap:
    capped       min_out = 0.9 x the V4Quoter's amountOut         must pass
    min_out_2x   min_out = 2 x amountOut                            must revert
    unpaid       value 0                                            must revert

    python3 scripts/research/rh_fast_template_proof.py
Exit 0 = every row matched, 1 = a mismatch, 2 = the node refused or did not quote (no verdict).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import sniper.execution.evm_v4_quote as q  # noqa: E402
from sniper.services.memecoin_rh_fast_decide import native_buy_tx  # noqa: E402
from sniper.execution.evm_v4_multihop_tx import native_multihop_buy_tx  # noqa: E402

SPACING_S = 0.5
AMOUNT = 10 ** 15                              # 0.001 ETH
TAKER = "0x" + "5e" * 20                       # no code, no key: a balance override funds it
CONF = q.V4["4663"]
HOOK = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"
# Pool keys of funded 4663 fills (memecoin_positions.executed_pool -> memecoin_evm_v4_pool_keys).
POOLS = [
    ("ZUPITER hooked", dict(currency0=q._NATIVE, currency1="0x148ff42c18a5a1e965e4f338b0e110c076da0a96",
                            fee=0, tick_spacing=200, hooks=HOOK)),
    ("ZIP hooked", dict(currency0=q._NATIVE, currency1="0xc00899951d84ee5afb1bf22df1d91d5206457d86",
                        fee=0, tick_spacing=200, hooks=HOOK)),
    ("DOTTIE plain", dict(currency0=q._NATIVE, currency1="0x5b3c2cd87083ea5c4436525dca6213740405b69e",
                          fee=10000, tick_spacing=200, hooks=q._ZERO_ADDRESS)),
]
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
# Routes rev 2 (2026-10-02): busy 4663 pools on the tape that day.
# R2 native -> USDG -> coin, through the hooked native/USDG pool 0x43be6b05.
A_USDG = dict(currency0=q._NATIVE, currency1=USDG, fee=1, tick_spacing=1,
              hooks="0xbfa1918d012eb5ed21df523790646630f41bcc8a")
BRIDGES = [
    ("USDG -> 0x6a08 plain", A_USDG, dict(currency0=USDG, currency1="0x6a08b947170a5d44d3e1358df49bdbca8ba92527",
                                          fee=9000, tick_spacing=90, hooks=q._ZERO_ADDRESS)),
    ("USDG -> 0xd0ca hooked", A_USDG, dict(currency0=USDG, currency1="0xd0ca71118ca21d674f9018a53026a38a0ab54dbf",
                                           fee=10000, tick_spacing=200,
                                           hooks="0xfbf47e5de83e0cc2896d7e80165a59c61d90dddc")),
]
# R3 WETH-quoted pools, wrapped by the router (WETH is currency0 in both: zeroForOne).
WETH_POOLS = [
    ("WETH 0x2926 plain", dict(currency0=WETH, currency1="0x292612116f87b6ba877219bba37acce0b2304108",
                               fee=100, tick_spacing=1, hooks=q._ZERO_ADDRESS)),
    ("WETH 0x5209 hooked", dict(currency0=WETH, currency1="0x52090044b98bacba07693b464e11cbdf69313351",
                                fee=3000, tick_spacing=60, hooks="0x32f5a629b9b829963c4d885d0d1edaaab8d48a80")),
]
_last = [0.0]


def _path_quote(url, head, path, amount):
    """V4Quoter quoteExactInput(native, path, amount) by eth_call at the pinned block, on the given
    research host only (never a read-pool member)."""
    from eth_abi import encode
    from sniper.execution.evm_v4_multihop import _QUOTE_EXACT_INPUT
    params = (q._NATIVE, [(cur, int(k["fee"]), int(k["tick_spacing"]), k["hooks"], b"") for cur, k in path],
              int(amount))
    data = "0x" + _QUOTE_EXACT_INPUT + encode(["(address,(address,uint24,int24,address,bytes)[],uint128)"],
                                              [params]).hex()
    result, err = rpc(url, "eth_call", [{"to": CONF["quoter"], "data": data}, head])
    return (int(result[2:66], 16) if result and result != "0x" else None), err


_HEX_BYTES = __import__("re").compile(r"0x(?:[0-9a-fA-F]{2})*")


def call_verdict(result, err):
    """'pass', 'revert' or None (no verdict). Only a contract revert answers a negative control: a
    rate limit, an invalid request, an unsupported method or a missing/malformed result says nothing
    about the guard (Codex EVM-09: two provider errors were counted as the expected reverts)."""
    if err is None:                      # whole bytes only: 0x, or an even run of hex digits
        return "pass" if isinstance(result, str) and _HEX_BYTES.fullmatch(result) else None
    if not isinstance(err, dict) or "http" in err or "transport" in err:
        return None
    message = str(err.get("message") or "").lower()
    if err.get("code") == 3 or "execution reverted" in message or message.startswith("revert"):
        return "revert"
    return None


def _rows(url, head, name, out, build, mismatches_refused):
    for label, min_out, value, must_pass in (("capped", out * 9 // 10, AMOUNT, True),
                                             ("min_out_2x", out * 2, AMOUNT, False),
                                             ("unpaid", out * 9 // 10, 0, False)):
        tx = build(min_out)
        call = {"from": TAKER, "to": tx["to"], "data": tx["data"], "gas": hex(int(tx["gas"])),
                "value": hex(value)}
        result, err = rpc(url, "eth_call", [call, head, {TAKER: {"balance": hex(10 ** 18)}}])
        verdict = call_verdict(result, err)
        if verdict is None:
            mismatches_refused[1] += 1
            print(json.dumps({"pool": name, "row": label, "refused": err if err is not None else
                              {"malformed_result": repr(result)[:80]}}))
            continue
        passed = verdict == "pass"
        ok = passed == must_pass
        mismatches_refused[0] += not ok
        print(json.dumps({"pool": name, "row": label, "quote_out": out, "min_out": min_out, "value": value,
                          "passed": passed, "expected_pass": must_pass, "match": ok,
                          "revert": None if passed else (err.get("data") or err.get("message"))}))


def rpc(url, method, params):
    """(result, error). An HTTP or transport failure is an error, never a null result."""
    wait = SPACING_S - (time.monotonic() - _last[0])
    if wait > 0:
        time.sleep(wait)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            out = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        out = {"error": {"http": exc.code}}
    except Exception as exc:
        out = {"error": {"transport": type(exc).__name__}}
    finally:
        _last[0] = time.monotonic()
    return out.get("result"), out.get("error")


def main():
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_root
    load_env_files(str(state_root()))          # the release's .env lives under SNIPER_STATE_ROOT
    url = os.environ.get("MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663", "").strip()
    if not url:
        print("MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663 unset: no verdict")
        return 2
    head, err = rpc(url, "eth_blockNumber", [])
    if err or not head:
        print("no head:", err)
        return 2
    print("pinned block", int(head, 16))
    deadline = int(time.time()) + 600
    counts = [0, 0]                            # mismatches, refused (no verdict)
    for name, key in POOLS:
        data = ("0x" + q._SEL_QUOTE_SINGLE + q._w(0x20) + q._encode_pool_key(key) + q._w(1)
                + q._w(AMOUNT) + q._w(0x100) + q._w(0))
        result, err = rpc(url, "eth_call", [{"to": CONF["quoter"], "data": data}, head])
        if err or not result or result == "0x":
            print(json.dumps({"pool": name, "quote": None, "error": err}))
            counts[1] += 1
            continue
        out = int(result[2:66], 16)
        _rows(url, head, name, out, lambda m, key=key: native_buy_tx(key, AMOUNT, m, deadline, taker=TAKER),
              counts)
    for name, a_key, b_key in BRIDGES:
        coin = b_key["currency1"]
        out, err = _path_quote(url, head, [(USDG, a_key), (coin, b_key)], AMOUNT)
        if out is None:
            print(json.dumps({"pool": name, "quote": None, "error": err}))
            counts[1] += 1
            continue
        path = [(USDG, a_key["fee"], a_key["tick_spacing"], a_key["hooks"]),
                (coin, b_key["fee"], b_key["tick_spacing"], b_key["hooks"])]
        _rows(url, head, name, out, lambda m, path=path, coin=coin: native_multihop_buy_tx(
            "4663", coin, path, AMOUNT, m, deadline), counts)
    for name, key in WETH_POOLS:
        data = ("0x" + q._SEL_QUOTE_SINGLE + q._w(0x20) + q._encode_pool_key(key) + q._w(1)
                + q._w(AMOUNT) + q._w(0x100) + q._w(0))
        result, err = rpc(url, "eth_call", [{"to": CONF["quoter"], "data": data}, head])
        if err or not result or result == "0x":
            print(json.dumps({"pool": name, "quote": None, "error": err}))
            counts[1] += 1
            continue
        out = int(result[2:66], 16)
        _rows(url, head, name, out, lambda m, key=key: q.weth_buy_tx(key, True, AMOUNT, m, deadline,
                                                                     taker=TAKER), counts)
    mismatches, refused = counts
    print("mismatches %d, refused %d" % (mismatches, refused))
    return 1 if mismatches else (2 if refused else 0)


if __name__ == "__main__":
    sys.exit(main())
