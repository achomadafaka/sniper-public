"""Arc wallet proofs are explicit, chain checked, and independent of funded authority."""
import json
import sqlite3

import pytest

from scripts.research import verify_axiom_addresses as cli
from sniper.persistence import db_memecoin as m
from sniper.services import axiom_address_verify as av
from sniper.settings import memecoin as cfg
from test_evm_key_proof_chains import A, W, KEY, CONTRACT, _conn, _conn_at, _proof, _cli_env


def test_arc_key_proof_is_opt_in_and_other_unknown_chains_stay_out():
    assert cfg.evm_key_proof_chains(None) == ()
    assert cfg.evm_key_proof_chains("5042,56,5042,unknown,solana") == ("5042", "56")


def test_cli_proves_arc_using_chain_checked_research_hosts(tmp_path, monkeypatch, capsys):
    _cli_env(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEY_PROOF_CHAINS", ("5042",))
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_5042", "https://arc.custody.example")
    path = tmp_path / "ledger.db"
    c = _conn_at(path)
    _proof(c, W, "4663")
    c.close()
    hosts = {"https://arc.research.example": 5042, "https://rh.research.example": 4663}
    asked = []
    def read(url, method, params):
        asked.append((url, method))
        return (hex(hosts[url]) if method == "eth_chainId" else KEY), None
    monkeypatch.setattr(av, "_http_call", read)
    cli.main(["--db", str(path), "--confirm", "--evm-key", "--chain", "5042", "--summary",
              "--code-url", "5042=https://arc.research.example",
              "--code-url", "4663=https://rh.research.example"])
    report = json.loads(capsys.readouterr().out)
    assert report["written"] == 1
    c = sqlite3.connect(path)
    assert av.watcher_address_owners(c)[("5042", A)][0] == W
    assert [method for _, method in asked[:2]] == ["eth_chainId", "eth_chainId"]
    c.close()


@pytest.mark.parametrize("target_code,watched", [(KEY, True), (None, False), (CONTRACT, False)])
def test_arc_watches_only_proven_accounts_of_armed_wallets(monkeypatch, target_code, watched):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEY_PROOF_CHAINS", ("5042",))
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"5042", "4663"})
    c = _conn()
    m.ensure_memecoin_tables(c)
    m.add_wallet(c, W, "wallet", "")
    assert m.set_wallet_chain_armed(c, W, "5042", True, force=True)["ok"]
    _proof(c, W, "4663")
    from sniper.persistence.db_axiom_identity import proof_snapshot, save_verification_results
    expected = proof_snapshot(c)
    results = av.verify_evm_key_addresses(c, chain="5042",
        code_fn=lambda chain, address: target_code if chain == "5042" else KEY)
    for row in results:
        row["detail"] = av._detail(row)
    save_verification_results(c, results, expected=expected, now="2026-10-03T18:00:00+00:00")
    assert (A in av.evm_watched_owners(c, "5042")) is watched
    assert m.wallet_armed_chains(c, W) == ["5042"]
    if watched:
        monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEY_PROOF_CHAINS", ())
        assert not av.evm_watched_owners(c, "5042")
    c.close()


def test_arc_cli_rejects_wrong_chain_before_code_reads(tmp_path, monkeypatch):
    _cli_env(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEY_PROOF_CHAINS", ("5042",))
    asked = []
    monkeypatch.setattr(av, "_http_call", lambda url, method, params:
                        (asked.append(method) or hex(56), None))
    with pytest.raises(SystemExit):
        cli.main(["--db", str(tmp_path / "absent.db"), "--verify", "--evm-key", "--chain", "5042",
                  "--code-url", "5042=https://arc.research.example"])
    assert asked == ["eth_chainId"]
