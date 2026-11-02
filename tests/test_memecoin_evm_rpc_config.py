"""Item 11 — per-chain EVM RPC resolution: a chain-specific override wins over the generic url; unset
returns '' so that chain's EVM leg stays a no-op (never a phantom endpoint)."""
from sniper.settings import memecoin as cfg

evm_rpc_for_chain = cfg.evm_rpc_for_chain


def test_generic_rpc_used_when_no_override(monkeypatch):
    monkeypatch.delenv("MEMECOIN_EVM_RPC_URL_8453", raising=False)
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL", "https://generic")
    assert evm_rpc_for_chain("8453") == "https://generic"


def test_chain_override_wins(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL", "https://generic")
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_8453", "https://base-specific")
    assert evm_rpc_for_chain("8453") == "https://base-specific"
    assert evm_rpc_for_chain("56") == "https://generic"   # BSC falls back to generic


def test_empty_when_unset(monkeypatch):
    monkeypatch.delenv("MEMECOIN_EVM_RPC_URL", raising=False)
    monkeypatch.delenv("MEMECOIN_EVM_RPC_URL_8453", raising=False)
    assert evm_rpc_for_chain("8453") == ""


def test_tickstream_rpc_uses_its_own_endpoint(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_4663", "https://custody")
    monkeypatch.setenv("MEMECOIN_TICKSTREAM_RPC_URL_4663", "https://wide-logs")
    assert hasattr(cfg, "evm_tickstream_rpc_for_chain")
    assert cfg.evm_tickstream_rpc_for_chain("4663") == "https://wide-logs"


def test_robinhood_tickstream_never_falls_back_to_the_custody_endpoint(monkeypatch):
    monkeypatch.delenv("MEMECOIN_TICKSTREAM_RPC_URL_4663", raising=False)
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_4663", "https://custody")
    assert hasattr(cfg, "evm_tickstream_rpc_for_chain")
    assert cfg.evm_tickstream_rpc_for_chain("4663") == ""


def test_robinhood_tickstream_rejects_the_custody_endpoint(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_4663", "https://shared")
    monkeypatch.setenv("MEMECOIN_TICKSTREAM_RPC_URL_4663", "https://shared")

    assert cfg.evm_tickstream_rpc_for_chain("4663") == ""


def test_arc_uses_only_its_explicit_rpc_fallbacks():
    env = {'MEMECOIN_EVM_RPC_URL_5042': 'https://primary',
           'MEMECOIN_EVM_RPC_POOL_URLS_5042': 'https://primary,https://backup,https://backup'}
    assert cfg._evm_rpc_pool(env)['5042'] == ('https://primary', 'https://backup')
    assert '5042' not in cfg._evm_rpc_pool({'MEMECOIN_EVM_RPC_URL_5042': 'https://primary'})
