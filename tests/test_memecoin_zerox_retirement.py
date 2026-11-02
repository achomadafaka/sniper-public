"""Active-runtime contracts for retiring the dead 0x provider."""

from pathlib import Path

from sniper.services import memecoin_evm_leg as leg
from sniper.settings import memecoin as cfg


ACTIVE_RUNTIME_FILES = (
    "sniper/execution/evm_real_seams.py",
    "sniper/execution/evm_dex_quote.py",
    "sniper/execution/evm_token_safety.py",
    "sniper/execution/geckoterminal_quote.py",
    "sniper/persistence/db_memecoin.py",
    "sniper/services/memecoin_evm_leg.py",
    "sniper/services/memecoin_evm_price.py",
    "sniper/services/memecoin_capture_v2.py",
    "sniper/services/memecoin_real_runner.py",
    "sniper/services/memecoin_runner.py",
    "sniper/settings/memecoin.py",
    "sniper/routes/api_memecoin.py",
    "scripts/memecoin_evm_smoke_test.py",
    "static/app.js",
)


def test_active_runtime_has_no_zerox_dependency():
    forbidden = (
        "api.0x.org", "memecoin_zerox_api_key", "zerox_quote",
        "zerox_allowance_holder", "zerox_key_set", "0x api",
        "0x executable", "0x price sweep", "0x fallback", "own 0x",
    )
    root = Path(__file__).resolve().parents[1]
    for name in ACTIVE_RUNTIME_FILES:
        source = (root / name).read_text().lower()
        assert not any(token in source for token in forbidden), name


def test_evm_venue_defaults_and_invalid_values_fail_closed(monkeypatch):
    for chain in ("1", "56", "8453", "4663"):
        monkeypatch.delenv(f"MEMECOIN_EVM_EXEC_VENUE_{chain}", raising=False)
        assert cfg.memecoin_evm_exec_venue(chain) == "disabled"

    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_8453", "zerox")
    assert cfg.memecoin_evm_exec_venue("8453") == "disabled"

    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_4663", "direct")
    assert cfg.memecoin_evm_exec_venue("4663") == "direct"


def test_build_evm_context_refuses_disabled_chain_with_valid_credentials(monkeypatch, tmp_path):
    from sniper.execution.evm_wallet import generate_keypair_file

    keypair = str(tmp_path / "evm.json")
    generate_keypair_file(keypair)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_8453", "disabled")
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_8453", "https://base-rpc")
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", keypair)

    assert leg.build_real_evm_context("8453") is None
