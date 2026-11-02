"""Chain 1: eth_getLogs never goes to ethereum-rpc.publicnode.com (2026-10-07, Alpha).

publicnode Ethereum joined the chain-1 pool at 18:06Z. It serves eth_getLogs only to about
head-10k blocks (head-10000 answered, head-20000..90000 all HTTP 403), while Infura and MEV Blocker
serve deep ranges with identical counts. The 403 is an error, never a false absence, but every deep
key-walk page penalized and quarantined publicnode, which knocked it out of balance and quote reads
in the middle of a ladder burst. Log reads on chain 1 now go to the other members only; every other
method keeps the whole pool, and no other chain changes.
"""
import json

import pytest

from sniper.execution import evm_rpc_pool as pool
from sniper.execution import evm_wallet as W

INFURA = "https://mainnet.infura.example/v3/KEY"
MEV = "https://rpc.mevblocker.io"
PUBLICNODE = "https://ethereum-rpc.publicnode.com"
LOGS = [{"address": "0x000000000004444c5dc75cb358380d2e3de08a90", "fromBlock": "0x1",
         "toBlock": "0x2710", "topics": []}]


@pytest.fixture(autouse=True)
def _pool(monkeypatch):
    pool.reset_for_tests()
    import sniper.settings.memecoin as real
    monkeypatch.setattr(real, "MEMECOIN_EVM_RPC_POOL_URLS",
                        {"1": (INFURA, MEV, PUBLICNODE), "4663": ("https://a.example", PUBLICNODE)},
                        raising=False)
    for url in (MEV, PUBLICNODE):
        pool.mark_identity(url, True)
    yield
    pool.reset_for_tests()


def _http(calls, failing=()):
    def http(method, url, headers=None, body=None, timeout=None):
        request = json.loads(body)
        if request["method"] == "eth_chainId":
            return 200, {"jsonrpc": "2.0", "id": 1, "result": "0x1"}, {}
        calls.append((url, request["method"]))
        if url in failing:
            return 429, {"error": {"code": -32005, "message": "Too Many Requests"}}, {}
        return 200, {"jsonrpc": "2.0", "id": 1, "result": []}, {}
    return http


def test_chain_1_log_reads_skip_publicnode_even_when_the_others_are_out():
    """THE regression: Infura 429ing and MEV Blocker quarantined sent the page to publicnode."""
    calls = []
    pool.penalize(MEV, "test", quarantine_s=60)
    try:
        W._rpc(INFURA, "eth_getLogs", LOGS, http=_http(calls, failing=(INFURA,)))
    except Exception:
        pass                              # every eligible member out: an honest failure is fine
    assert PUBLICNODE not in {url for url, _m in calls}
    assert pool.available(PUBLICNODE), "a log page can no longer quarantine it"


def test_chain_1_other_reads_still_use_publicnode():
    calls = []
    pool.penalize(MEV, "test", quarantine_s=60)
    W._rpc(INFURA, "eth_getBalance", ["0x" + "12" * 20, "latest"],
           http=_http(calls, failing=(INFURA,)))
    assert PUBLICNODE in {url for url, _m in calls}


def test_chain_1_log_reads_still_rotate_to_mev_blocker():
    calls = []
    assert W._rpc(INFURA, "eth_getLogs", LOGS, http=_http(calls, failing=(INFURA,)))["result"] == []
    assert {url for url, _m in calls} == {INFURA, MEV}


def test_another_chain_keeps_publicnode_for_logs():
    calls = []
    W._rpc("https://a.example", "eth_getLogs", LOGS, http=_http(calls, failing=("https://a.example",)))
    assert PUBLICNODE in {url for url, _m in calls}
