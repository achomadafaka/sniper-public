"""Read-only on-chain proof that a Base (8453) standing-allowance pin is the contract it claims to be
(operator yes 2026-10-15 via Alpha: "yes allow base standing approval"; pins proven before use).

Permit2: code present, and DOMAIN_SEPARATOR() equals the EIP-712 domain Permit2 builds for
chainId 8453 at that address (keccak256("EIP712Domain(string name,uint256 chainId,address
verifyingContract)"), keccak256("Permit2")), which only Permit2's own code returns. Prints the
runtime code hash for the record. Two eth_getCode/eth_call reads on the chain owner's public RPC.

    python3 scripts/research/prove_standing_spender_8453.py [rpc_url] [chain_id]

The chain id defaults to 8453; 4663 (operator yes 2026-10-21 via Alpha, brain 205ef146) runs it
against the 4663 RESEARCH host, never the funded pool.
"""
import json
import sys
import urllib.request

from eth_abi import encode
from eth_utils import keccak, to_checksum_address

RPC = sys.argv[1] if len(sys.argv) > 1 else "https://mainnet.base.org"
CHAIN_ID = int(sys.argv[2]) if len(sys.argv) > 2 else 8453
PERMIT2 = "0x000000000022d473030f116ddee9f6b43ac78ba3"


def rpc(method, params):
    req = urllib.request.Request(RPC, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                                                       "params": params}).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "sniper-proof"})
    with urllib.request.urlopen(req, timeout=20) as r:
        out = json.loads(r.read())
    if "error" in out:
        raise RuntimeError(out["error"])
    return out["result"]


def expected_permit2_domain(chain_id, address):
    type_hash = keccak(text="EIP712Domain(string name,uint256 chainId,address verifyingContract)")
    return "0x" + keccak(encode(["bytes32", "bytes32", "uint256", "address"],
                                [type_hash, keccak(text="Permit2"), chain_id,
                                 to_checksum_address(address)])).hex().removeprefix("0x")


def main():
    assert int(rpc("eth_chainId", []), 16) == CHAIN_ID, "wrong chain"
    code = rpc("eth_getCode", [PERMIT2, "latest"])
    domain = rpc("eth_call", [{"to": PERMIT2, "data": "0x3644e515"}, "latest"])   # DOMAIN_SEPARATOR()
    want = expected_permit2_domain(CHAIN_ID, PERMIT2)
    result = {"spender": PERMIT2, "chain_id": CHAIN_ID, "code_bytes": (len(code) - 2) // 2,
              "code_hash": "0x" + keccak(hexstr=code).hex().removeprefix("0x"),
              "domain_separator": domain, "expected_domain_separator": want,
              "proven": len(code) > 2 and domain.lower() == want.lower()}
    print(json.dumps(result, indent=1))
    return 0 if result["proven"] else 1


if __name__ == "__main__":
    sys.exit(main())
