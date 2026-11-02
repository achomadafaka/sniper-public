"""Bounded, read-only Ink RPC capability probe. Never submit a transaction."""

import argparse
import hashlib
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone


CHAIN_ID = 57073
READ_METHODS = (
    "eth_chainId", "eth_getBlockByNumber", "eth_getCode", "eth_call",
    "eth_getTransactionReceipt", "eth_getLogs", "eth_getStorageAt",
    "debug_traceTransaction",
)
EIP1967_IMPLEMENTATION_SLOT = (
    "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc")
CONTRACTS = {
    "weth": "0x4200000000000000000000000000000000000006",
    "usdc": "0x2D270e6886d130D724215A266106e6832161EAEd",
    "usdc_e": "0xF1815bd50389c46847f0Bda824eC8da914045D14",
    "v3_factory": "0x640887A9ba3A9C53Ed27D0F7e8246A4F933f3424",
    "v3_router": "0x177778F19E89dD1012BdBe603F144088A95C4B53",
    "v3_quoter": "0x96b572D2d880cf2Fa2563651BD23ADE6f5516652",
    "v4_pool_manager": "0x360E68faCcca8cA495c1B759Fd9EEe466db9FB32",
    "v4_quoter": "0x3972C00f7ed4885e145823eb7C655375d275A1C5",
}


def validate_chain_reply(reply: dict, expected: int = CHAIN_ID) -> dict:
    if not isinstance(reply, dict) or reply.get("error") is not None:
        return {"capability": "rpc_error" if isinstance(reply, dict) and reply.get("error") is not None else "malformed", "usable": False}
    value = reply.get("result")
    if not isinstance(value, str):
        return {"capability": "malformed", "usable": False}
    try:
        chain = int(value, 16) if value.startswith("0x") else int(value)
    except ValueError:
        return {"capability": "malformed", "usable": False}
    return {"capability": "available" if chain == expected else "wrong_chain", "usable": chain == expected}


def _read(rpc, method, params):
    if method not in READ_METHODS:
        raise ValueError("write method prohibited")
    started = time.monotonic_ns()
    try:
        reply = rpc(method, params)
        capability = "available" if isinstance(reply, dict) and reply.get("error") is None and reply.get("result") is not None else "rpc_error"
        if not isinstance(reply, dict) or (reply.get("error") is None and reply.get("result") is None):
            capability = "malformed"
        result = reply.get("result") if isinstance(reply, dict) else None
    except Exception as exc:
        capability = (f"http_{exc.code}" if isinstance(exc, urllib.error.HTTPError)
                      else type(exc).__name__)
        result = None
    return {"capability": capability, "rtt_ms": round((time.monotonic_ns() - started) / 1_000_000, 3), "result": result}


def _code_sha256(value):
    if not isinstance(value, str) or not value.startswith("0x") or len(value) <= 2:
        return None
    try:
        return hashlib.sha256(bytes.fromhex(value[2:])).hexdigest()
    except ValueError:
        return None


def probe(rpc, *, url: str, transaction_hash: str | None = None, from_block: int | None = None,
          to_block: int | None = None) -> dict:
    """Probe one endpoint. Census only a caller-specified, bounded complete interval."""
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    chain_read = _read(rpc, "eth_chainId", [])
    provider = validate_chain_reply({"result": chain_read["result"]}) if chain_read["capability"] == "available" else {
        "capability": chain_read["capability"], "usable": False}
    report = {"schema_version": 1, "observed_at": now, "url": url, "chain_id": CHAIN_ID,
              "provider": provider, "methods": {"eth_chainId": chain_read}, "contracts": {},
              "census": None}
    if not provider["usable"]:
        return report
    for tag in ("latest", "pending", "safe", "finalized"):
        report["methods"][f"eth_getBlockByNumber:{tag}"] = _read(rpc, "eth_getBlockByNumber", [tag, False])
    latest = report["methods"]["eth_getBlockByNumber:latest"]["result"]
    pinned = None
    if isinstance(latest, dict):
        number, block_hash = latest.get("number"), latest.get("hash")
        try:
            if (isinstance(number, str) and number.startswith("0x")
                    and isinstance(block_hash, str) and len(block_hash) == 66
                    and int(number, 16) >= 0 and int(block_hash, 16) > 0):
                pinned = number
        except ValueError:
            pass
    report["state_block"] = {"number": pinned, "hash": latest.get("hash") if pinned else None}
    state_tag = pinned or "latest"
    for name, address in CONTRACTS.items():
        code = _read(rpc, "eth_getCode", [address, state_tag])
        code_hash = _code_sha256(code["result"])
        slot = _read(rpc, "eth_getStorageAt", [address, EIP1967_IMPLEMENTATION_SLOT, state_tag])
        word = slot["result"]
        implementation = None
        if isinstance(word, str) and word.startswith("0x") and len(word) == 66:
            try:
                if int(word, 16):
                    implementation = "0x" + word[-40:]
            except ValueError:
                pass
        impl_code = (_read(rpc, "eth_getCode", [implementation, state_tag])
                     if implementation else None)
        report["contracts"][name] = {
            "address": address, "code_bytes": (len(code["result"]) - 2) // 2
            if code_hash is not None else None,
            "code_sha256": code_hash, "capability": code["capability"],
            "eip1967_slot": word, "eip1967_slot_capability": slot["capability"],
            "proxy_implementation": implementation,
            "implementation_code_sha256": _code_sha256(impl_code["result"]) if impl_code else None,
        }
    report["methods"]["eth_call:pending"] = _read(rpc, "eth_call", [{"to": CONTRACTS["weth"], "data": "0x313ce567"}, "pending"])
    # A deliberately invalid override request tests method support, not a successful safety simulation.
    report["methods"]["eth_call:state_override"] = _read(
        rpc, "eth_call", [{"to": CONTRACTS["weth"], "data": "0x313ce567"}, state_tag,
                          {CONTRACTS["weth"]: {"balance": "0x0"}}])
    if transaction_hash:
        report["methods"]["eth_getTransactionReceipt"] = _read(rpc, "eth_getTransactionReceipt", [transaction_hash])
        report["methods"]["debug_traceTransaction"] = _read(rpc, "debug_traceTransaction", [transaction_hash, {"tracer": "callTracer"}])
    if (from_block is None) != (to_block is None):
        raise ValueError("both census block bounds are required")
    if from_block is not None:
        if from_block < 0 or to_block < from_block or to_block - from_block > 99:
            raise ValueError("census must be a forward interval of at most 100 blocks")
        logs = _read(rpc, "eth_getLogs", [{"fromBlock": hex(from_block), "toBlock": hex(to_block),
                                           "address": CONTRACTS["v3_factory"]}])
        report["census"] = {"from_block": from_block, "to_block": to_block,
                            "complete_blocks_requested": to_block - from_block + 1,
                            "v3_factory_logs": len(logs["result"]) if isinstance(logs["result"], list) else None,
                            "capability": logs["capability"],
                            "other_venues": "unmeasured", "depth": "unmeasured", "gaps": None}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpc-url", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--transaction-hash")
    parser.add_argument("--from-block", type=int)
    parser.add_argument("--to-block", type=int)
    args = parser.parse_args()

    def rpc(method, params):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        request = urllib.request.Request(
            args.rpc_url, body, {"content-type": "application/json",
                                 "user-agent": "Sniper-Ink-Capability-Probe/1"})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)

    report = probe(rpc, url=args.rpc_url, transaction_hash=args.transaction_hash,
                   from_block=args.from_block, to_block=args.to_block)
    with open(args.output, "w", encoding="utf-8") as output:
        json.dump(report, output, indent=2)
        output.write("\n")


if __name__ == "__main__":
    main()
