"""Fork proof for type-2 signing (spec 2026-09-30-memecoin-evm-type2-signing-design.md section 6).

On a local anvil fork of Ethereum mainnet, the PRODUCTION executor's `_sign` signs one
`WETH.deposit` as legacy and one as type 2 at the SAME cap, and both are sent to the fork. Legacy
pays its whole signed price; type 2 pays the block's base fee plus the tip, so it costs less for the
same gas. Runs only when asked: ETH_FORK_UPSTREAM (a public archive gateway, never custody, the
relay or a funded read provider) and anvil. The test process connects to loopback only; anvil alone
reads the upstream. The wallet is a throwaway key funded with anvil_setBalance (anvil's account 0
carries an EIP-7702 delegation on mainnet).

The fork's fee market is anvil's: this proves the mechanism, not live chain-1 savings.
"""
import base64
import json
import os
import socket
import subprocess
import time
import urllib.request

import pytest
from eth_account import Account

from sniper.execution import evm_real_seams as S
from sniper.execution.submission_wiring import evm_tx_identity

pytestmark = pytest.mark.timeout(300)

ANVIL = os.path.expanduser("~/.foundry/bin/anvil")
UPSTREAM = os.getenv("ETH_FORK_UPSTREAM", "")
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
TIP_GWEI = "0.05"
TIP = 5 * 10 ** 7
DENIED = ("infura", "flashbots", "mevblocker", "publicnode", "alchemy", "drpc", "blastapi",
          "quiknode", "quicknode", "nodereal", "helius")
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _post(url, method, params):
    req = urllib.request.Request(url, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                                                       "params": params}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


@pytest.fixture
def fork():
    if not UPSTREAM or not os.path.exists(ANVIL):
        pytest.skip("fork proof runs only with ETH_FORK_UPSTREAM and anvil")
    if any(word in UPSTREAM.lower() for word in DENIED):
        pytest.fail("fork upstream is custody, the relay or a funded read provider: %s" % UPSTREAM)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    url = "http://127.0.0.1:%d" % port
    proc = subprocess.Popen([ANVIL, "--fork-url", UPSTREAM, "--chain-id", "1", "--port", str(port),
                             "--host", "127.0.0.1", "--silent"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(120):
            try:
                if _post(url, "eth_chainId", [])["result"] == "0x1":
                    break
            except OSError:
                time.sleep(0.5)
        else:
            pytest.fail("anvil fork did not come up")
        yield url
    finally:
        proc.terminate()
        proc.wait(10)


@pytest.fixture
def loopback_only(monkeypatch):
    refused, real_connect = [], socket.socket.connect

    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if self.family in (socket.AF_INET, socket.AF_INET6) and str(host) not in LOOPBACK:
            refused.append(str(host))
            raise OSError("type-2 fork proof refused a connection to %s" % (host,))
        return real_connect(self, address)
    monkeypatch.setattr(socket.socket, "connect", connect)
    yield refused
    assert refused == []


def _land(url, raw):
    tx_hash = _post(url, "eth_sendRawTransaction", [raw])["result"]
    for _ in range(120):                      # automine answers before the receipt exists
        receipt = _post(url, "eth_getTransactionReceipt", [tx_hash])["result"]
        if receipt:
            return tx_hash, receipt
        time.sleep(0.25)
    pytest.fail("no receipt for %s" % tx_hash)


def test_a_type2_deposit_lands_and_costs_less_than_legacy_at_the_same_cap(fork, loopback_only, monkeypatch):
    # "" lists nothing on main and on beta/eth-mainnet, whose default is "1" (Lambda 2026-09-30).
    monkeypatch.setenv("MEMECOIN_EVM_TYPE2_CHAINS", "")
    monkeypatch.delenv("MEMECOIN_EVM_APPROVAL_PIPELINE", raising=False)
    monkeypatch.setenv("MEMECOIN_EVM_TIP_GWEI_1", TIP_GWEI)
    acct = Account.create()
    _post(fork, "anvil_setBalance", [acct.address, hex(10 ** 18)])
    ex = S.build_real_evm_executor(acct=acct, chain_id="1", rpc_url=fork,
                                   usdc_fn=lambda: 0.0, token_fn=lambda _mint: 0.0)
    deposit = {"to": WETH, "data": "0xd0e30db0", "value": 10 ** 15, "gas": 150_000}

    _land(fork, ex["_sign"](dict(deposit))[0])        # warm-up: the first deposit writes a cold slot
    raw0, h0, _n0 = ex["_sign"](dict(deposit))                       # chain 1 not listed: legacy
    assert int(raw0[2:4], 16) >= 0xc0
    cap = S.decode_signed_tx_cost(base64.b64encode(raw0.encode()).decode())["gas_price"]
    got0, r0 = _land(fork, raw0)

    monkeypatch.setenv("MEMECOIN_EVM_TYPE2_CHAINS", "1")
    raw2, h2, _n2 = ex["_sign"](dict(deposit), gas_price_override=cap)   # the SAME cap
    assert raw2.startswith("0x02")
    cost2 = S.decode_signed_tx_cost(base64.b64encode(raw2.encode()).decode())
    assert (cost2["gas_price"], cost2["max_priority_fee"]) == (cap, min(TIP, cap))
    assert cost2["worst_case_wei"] == 150_000 * cap + 10 ** 15      # the exit gate's admission bound
    got2, r2 = _land(fork, raw2)

    block2 = _post(fork, "eth_getBlockByNumber", [r2["blockNumber"], False])["result"]
    base2 = int(block2["baseFeePerGas"], 16)
    eff0, eff2 = int(r0["effectiveGasPrice"], 16), int(r2["effectiveGasPrice"], 16)
    used0, used2 = int(r0["gasUsed"], 16), int(r2["gasUsed"], 16)
    print(json.dumps({"block": int(r2["blockNumber"], 16), "base_fee_gwei": base2 / 1e9,
                      "cap_gwei": cap / 1e9, "tip_gwei": TIP / 1e9, "legacy_effective_gwei": eff0 / 1e9,
                      "type2_effective_gwei": eff2 / 1e9, "gas_used": [used0, used2],
                      "legacy_cost_wei": used0 * eff0, "type2_cost_wei": used2 * eff2}))

    assert r0["status"] == r2["status"] == "0x1"
    assert (got0, got2) == (h0, h2) and h2 == evm_tx_identity(raw2)       # write-ahead identity holds
    assert eff0 == cap                                                     # legacy pays all of it
    assert base2 + TIP < cap and eff2 == base2 + TIP                        # type 2 pays base + tip
    assert used2 == used0 and used2 * eff2 < used0 * eff0              # same work, less paid
