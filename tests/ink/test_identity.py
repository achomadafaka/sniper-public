import sqlite3
import pytest

from sniper.persistence import db_memecoin as db
from sniper.execution.evm_wallet import CHAIN_QUOTE, CHAIN_USDC, NATIVE_IS_QUOTE_CHAINS
from sniper.services.memecoin_chains import canonical_chain_id, funded_chain_keys, resolve_chain
from sniper.settings import memecoin as cfg


def test_ink_canonical_identity():
    assert canonical_chain_id("ink") == "57073"
    assert canonical_chain_id("57073") == "57073"
    assert resolve_chain("Ink") == "ink"


def test_ink_is_default_disabled_but_can_be_explicitly_allowlisted(monkeypatch):
    assert "57073" not in cfg.MEMECOIN_EVM_ALLOWLIST
    assert "ink" not in funded_chain_keys()
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"57073"})
    assert "ink" in funded_chain_keys()


def test_ink_quote_and_stablecoin_have_exact_distinct_identities():
    assert CHAIN_QUOTE["57073"] == {
        "token": "0x4200000000000000000000000000000000000006",
        "decimals": 18, "kind": "eth", "usd_symbol": "ETH",
    }
    assert CHAIN_USDC["57073"].lower() == "0x2d270e6886d130d724215a266106e6832161eaed"
    assert CHAIN_USDC["57073"].lower() != "0xf1815bd50389c46847f0bda824ec8da914045d14"
    assert "57073" not in NATIVE_IS_QUOTE_CHAINS


def test_ink_preconfirmed_admission_is_separately_disabled():
    assert cfg.MEMECOIN_INK_PRECONFIRMED_ADMISSION_ENABLED is False


def test_ink_shared_entry_stops_before_signing_on_refusal(ink_flow):
    result = ink_flow("token_unsafe")
    assert result["result"]["status"] == "unsafe"
    assert result["approve_calls"] == result["sign_calls"] == result["send_calls"] == 0


def test_ink_shared_entry_reaches_injected_transport(ink_flow):
    result = ink_flow("success")
    assert result["result"]["status"] == "submitted_unknown"
    assert result["pending_rows"] == 1
    assert result["sign_calls"] == result["send_calls"] == 1


def test_ink_gas_shortage_stops_before_signing(ink_flow):
    result = ink_flow("gas_shortage")
    assert result["result"]["status"] == "rejected"
    assert result["sign_calls"] == result["send_calls"] == 0


def test_ink_rpc_pool_and_free_endpoints_are_chain_bound():
    from sniper.execution.free_endpoint_registry import free_endpoints

    assert cfg._evm_rpc_pool({})["57073"] == (
        "https://rpc-gel.inkonchain.com", "https://rpc-qnd.inkonchain.com")
    assert {e.expected_identity for e in free_endpoints("57073")} == {"0xdef1"}
    assert all(e.health_method == "eth_chainId" for e in free_endpoints("57073"))


def test_ink_context_refuses_wrong_chain_rpc(monkeypatch, tmp_path):
    from sniper.execution.evm_wallet import generate_keypair_file
    from sniper.services.memecoin_evm_leg import build_real_evm_context

    key = str(tmp_path / "evm.json")
    generate_keypair_file(key)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", key)
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_57073", "direct")
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_57073", "https://wrong.example")

    def wrong_chain(_method, _url, **_kwargs):
        return 200, {"result": "0x2105"}, None

    with pytest.raises(RuntimeError, match="wrong_chain"):
        build_real_evm_context("57073", http=wrong_chain)


def test_enabled_ink_without_context_stays_unroutable(monkeypatch):
    from sniper.services.memecoin_evm_leg import build_real_evm_context

    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_57073", "direct")
    monkeypatch.delenv("MEMECOIN_EVM_RPC_URL_57073", raising=False)
    monkeypatch.delenv("MEMECOIN_EVM_RPC_URL", raising=False)
    assert build_real_evm_context("57073") is None


def test_ink_wallet_authority_is_chain_local_and_pause_clears_it(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"57073", "8453"})
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    db.add_wallet(conn, "ink-source", "ink-source")
    db.set_wallet_chain_armed(conn, "ink-source", "base", True, force=True)
    assert db.real_approved_user_ids(conn, network_id="57073") == set()
    db.set_wallet_chain_armed(conn, "ink-source", "ink", True, force=True)
    assert db.real_approved_user_ids(conn, network_id="57073") == {"ink-source"}
    db.set_wallet_paused(conn, "ink-source", True)
    assert db.real_approved_user_ids(conn, network_id="57073") == set()


def test_ink_quote_price_unknown_remains_unknown():
    from sniper.execution.evm_wallet import quote_usd_price

    assert quote_usd_price("57073", price_fn=lambda: None) is None
    assert quote_usd_price("57073", price_fn=lambda: 2500.0) == 2500.0
