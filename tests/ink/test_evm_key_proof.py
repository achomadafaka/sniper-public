"""evm-key-v1: an Ink (57073) address proof from a direct proof on another EVM chain (Alpha GO,
2026-09-29).

J7 reports no Ink trades, so no Ink address can be proven from J7 receipts and the Ink reader's
watched set was structurally empty. An EVM address a key controls is the same account on every EVM
chain, so W is proved on Ink for A when:
  - W holds a current DIRECT proof of A on another EVM chain (never a cross-chain one);
  - eth_getCode(A) on Ink is empty or an exact EIP-7702 designator (anything else, or an
    unreadable answer, is no proof: a contract at A on Ink can have another controller);
  - no other wallet holds A anywhere.
Proof only. Arming stays the operator's; the reader may watch every evm-key owner observe-only.
"""
import json
import sqlite3

from sniper.persistence.db_axiom import ensure_axiom_tables
from sniper.persistence.db_axiom_identity import proof_snapshot, save_verification_results
from sniper.services import axiom_address_verify as av

NOW = "2026-09-29T21:00:00+00:00"
W, OTHER = "uid-w", "uid-o"
A = "0x" + "a1" * 20
B = "0x" + "b2" * 20
DESIGNATOR = "0xef0100" + "de" * 20
TX1, TX2 = "0x" + "1" * 64, "0x" + "2" * 64


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_axiom_tables(c)
    c.executescript("""
        CREATE TABLE memecoin_wallet_addresses (source_user_id TEXT PRIMARY KEY, handle TEXT,
            network_id TEXT NOT NULL, address TEXT NOT NULL, resolved_at TEXT NOT NULL,
            method TEXT, coins_held INTEGER, coins_checked INTEGER, verify_status TEXT,
            verify_detail TEXT);
    """)
    return c


def _proof(c, uid, chain, address=A, version=av.J7_PROOF_VERSION, status=av.CONFIRMED, **extra):
    detail = {"proof_version": version, "contradictions": 0,
              "event_evidence": [{"tx": TX1, "mint": "0xm", "verdict": av.BOUGHT},
                                 {"tx": TX2, "mint": "0xm", "verdict": av.BOUGHT}]}
    detail.update(extra)
    c.execute("INSERT INTO axiom_verified_addresses(source_user_id,handle,x_handle,network_id,address,"
              "verify_status,matches,contradictions,no_data,checked,verified_at,detail) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              (uid, uid, uid, chain, address, status, 2, 0, 0, 2, NOW, json.dumps(detail)))
    c.commit()


def _verify(c, code=DESIGNATOR):
    asked = []

    def code_fn(chain, address):
        asked.append((chain, address))
        return code(address) if callable(code) else code
    out = {r["source_user_id"]: r for r in av.verify_evm_key_addresses(c, chain="57073", code_fn=code_fn)}
    return out, asked


def _save(c, results):
    expected = proof_snapshot(c)
    for r in results.values():
        r["detail"] = av._detail(r)
    return save_verification_results(c, list(results.values()), expected=expected, now=NOW)


def _ink_owner(c):
    return av.watcher_address_owners(c).get(("57073", A))


def test_a_direct_proof_elsewhere_and_a_key_account_on_ink_prove_the_ink_address():
    c = _conn()
    _proof(c, W, "56")
    for code, kind in ((DESIGNATOR, "7702"), ("0x", "empty")):
        r, asked = _verify(c, code=code)
        assert r[W]["verify_status"] == av.CONFIRMED and r[W]["proof_version"] == av.EVM_KEY_PROOF_VERSION
        assert r[W]["chain_code"] == kind and r[W]["address"] == A
        assert asked == [("57073", A), ("56", A)]          # Ink, then the basis chain
        assert r[W]["crosschain_basis"] == [{"chain": "56", "proof_version": av.J7_PROOF_VERSION}]
    assert _save(c, r) == 1
    assert _ink_owner(c) == (W, W)


def test_a_contract_account_at_the_address_on_ink_refuses():
    """Alpha's red test: a contract at A on Ink can have another controller."""
    c = _conn()
    _proof(c, W, "56")
    for code in ("0x6080604052", "0xef0100" + "de" * 19, "0xef0200" + "de" * 20):
        r, _asked = _verify(c, code=code)
        assert r[W]["verify_status"] != av.CONFIRMED and r[W]["chain_code"] == "contract", code
    _save(c, r)
    assert _ink_owner(c) is None


def test_an_unreadable_code_is_no_proof_and_never_demotes_a_current_one():
    c = _conn()
    _proof(c, W, "56")
    r, _asked = _verify(c, code=None)
    assert r[W]["verify_status"] == av.UNVERIFIED and r[W]["chain_code"] == "unknown"
    _save(c, _verify(c)[0])
    assert _ink_owner(c) == (W, W)
    r, _asked = _verify(c, code=None)
    assert r[W]["skip_reason"] == "keeps_current_proof" and _save(c, r) == 0
    assert _ink_owner(c) == (W, W)


def test_a_cross_chain_proof_is_never_a_basis():
    c = _conn()
    _proof(c, W, "56", version=av.J7_CROSSCHAIN_PROOF_VERSION)
    r, asked = _verify(c)
    assert W not in r and asked == []


def test_an_address_another_wallet_holds_is_no_basis():
    c = _conn()
    _proof(c, W, "56")
    _proof(c, OTHER, "8453")
    r, asked = _verify(c)
    assert all(x["verify_status"] != av.CONFIRMED for x in r.values()) and asked == []


def test_a_wallet_with_two_direct_addresses_is_skipped():
    c = _conn()
    _proof(c, W, "56")
    _proof(c, W, "8453", address=B)
    r, asked = _verify(c)
    assert r[W]["skip_reason"] == "evm_key_multiple_addresses" and asked == []


def test_a_retired_basis_retires_the_ink_proof_when_read():
    c = _conn()
    _proof(c, W, "56")
    _save(c, _verify(c)[0])
    assert _ink_owner(c) == (W, W)
    c.execute("UPDATE axiom_verified_addresses SET verify_status=? WHERE network_id='56'", (av.REFUTED,))
    c.commit()
    assert _ink_owner(c) is None


def test_an_ink_proof_is_never_a_basis_and_counts_only_on_ink():
    c = _conn()
    _proof(c, W, "57073", version=av.EVM_KEY_PROOF_VERSION)
    _proof(c, W, "4663", version=av.EVM_KEY_PROOF_VERSION)
    assert not {k for k in av.watcher_address_owners(c) if k[1] == A}


def test_an_evm_key_row_outside_ink_is_never_current_even_with_a_basis():
    c = _conn()
    _proof(c, W, "56")
    _proof(c, W, "4663", version=av.EVM_KEY_PROOF_VERSION)      # a stray row on a J7 chain
    assert ("4663", A) not in av.watcher_address_owners(c)
    assert ("56", A) in av.watcher_address_owners(c)


# --- the hourly verifier's Ink line: --evm-key --chain 57073 ------------------------------------

def _ledger(tmp_path):
    path = tmp_path / "ledger.db"
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    ensure_axiom_tables(c)
    c.executescript("""
        CREATE TABLE memecoin_wallet_addresses (source_user_id TEXT PRIMARY KEY, handle TEXT,
            network_id TEXT NOT NULL, address TEXT NOT NULL, resolved_at TEXT NOT NULL,
            method TEXT, coins_held INTEGER, coins_checked INTEGER, verify_status TEXT,
            verify_detail TEXT);
    """)
    _proof(c, W, "56")
    c.close()
    return str(path)


def test_the_verifier_writes_an_ink_proof_from_a_non_custody_read_host(tmp_path, monkeypatch, capsys):
    from scripts.research import verify_axiom_addresses as cli
    path = _ledger(tmp_path)
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_4663", "https://custody-4663.example")   # .env loaded
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_56", "https://custody-56.example")
    monkeypatch.setenv("MEMECOIN_CAPTURE_EVM_READ_URL_56", "https://bsc.reads.example")
    monkeypatch.delenv("MEMECOIN_EVM_RPC_URL_57073", raising=False)                   # Ink: none yet
    asked = []
    monkeypatch.setattr(av, "_http_call", lambda url, method, params: (
        (hex(56), None) if method == "eth_chainId" else (asked.append((url, method)) or DESIGNATOR, None)))
    # The basis read goes to the research host named for 56, never the CAPTURE host (rule 0724bc08).
    cli.main(["--db", path, "--confirm", "--evm-key", "--chain", "57073",
              "--rpc-url", "https://ink.reads.example", "--summary",
              "--code-url", "56=https://bsc.research.example"])
    summary = json.loads(capsys.readouterr().out)
    assert summary["written"] == 1 and summary["status"] == {"axiom_confirmed/evm-key-v1": 1}
    assert asked == [("https://ink.reads.example", "eth_getCode"),
                     ("https://bsc.research.example", "eth_getCode")]
    c = sqlite3.connect(path)
    assert av.watcher_address_owners(c)[("57073", A)][0] == W


def test_the_verifier_refuses_an_ink_host_another_role_uses(tmp_path, monkeypatch):
    """rpc-gel.inkonchain.com is a 57073 free-submission endpoint (taken)."""
    import pytest
    from scripts.research import verify_axiom_addresses as cli
    path = _ledger(tmp_path)
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_4663", "https://custody-4663.example")
    monkeypatch.setattr(av, "_http_call", lambda *a: pytest.fail("no read before the guard"))
    with pytest.raises(SystemExit):
        cli.main(["--db", path, "--confirm", "--evm-key", "--chain", "57073",
                  "--rpc-url", "https://rpc-gel.inkonchain.com"])
    with pytest.raises(SystemExit):                            # no read host at all
        cli.main(["--db", path, "--confirm", "--evm-key", "--chain", "57073"])


def test_the_ink_line_refuses_when_no_custody_url_proves_env_loaded(tmp_path, monkeypatch):
    import pytest
    from scripts.research import verify_axiom_addresses as cli
    path = _ledger(tmp_path)
    for chain in ("4663", "56", "8453", "57073"):
        monkeypatch.delenv("MEMECOIN_EVM_RPC_URL_%s" % chain, raising=False)
    monkeypatch.setattr(av, "_http_call", lambda *a: pytest.fail("no read before the guard"))
    with pytest.raises(SystemExit):
        cli.main(["--db", path, "--confirm", "--evm-key", "--chain", "57073",
                  "--rpc-url", "https://ink.reads.example"])


# --- Gamma LOW-1: the BASIS address must be a key account on its own chain too ------------------

def _verify_by_chain(c, codes):
    asked = []

    def code_fn(chain, address):
        asked.append((chain, address))
        value = codes.get(chain)
        return value(address) if callable(value) else value
    return {r["source_user_id"]: r for r in av.verify_evm_key_addresses(
        c, chain="57073", code_fn=code_fn)}, asked


def test_a_contract_wallet_on_the_basis_chain_proves_nothing_on_ink():
    """An empty Ink address only says "not deployed there yet"; if A is a contract wallet on the
    basis chain, whoever deploys it on Ink need not be W."""
    c = _conn()
    _proof(c, W, "56")
    r, asked = _verify_by_chain(c, {"57073": "0x", "56": "0x6080604052"})
    assert r[W]["verify_status"] != av.CONFIRMED and r[W]["basis_code"] == {"56": "contract"}
    assert ("56", A) in asked


def test_an_unreadable_basis_code_is_no_proof():
    c = _conn()
    _proof(c, W, "56")
    r, _asked = _verify_by_chain(c, {"57073": "0x", "56": None})
    assert r[W]["verify_status"] == av.UNVERIFIED and r[W]["basis_code"] == {"56": "failed"}


def test_key_accounts_on_every_basis_chain_and_on_ink_confirm_and_are_recorded():
    c = _conn()
    _proof(c, W, "56")
    _proof(c, W, "8453")
    r, _asked = _verify_by_chain(c, {"57073": DESIGNATOR, "56": "0x", "8453": DESIGNATOR})
    assert r[W]["verify_status"] == av.CONFIRMED
    assert r[W]["basis_code"] == {"56": "empty", "8453": "7702"}
    assert json.loads(av._detail(r[W]))["basis_code"] == {"56": "empty", "8453": "7702"}



def test_a_basis_chain_without_an_approved_read_host_is_never_read_on_a_default(tmp_path, monkeypatch, capsys):
    """No --code-url for 56: the basis code is unread (no proof); neither EVM_RPC's 56 default nor a
    CAPTURE host is ever asked."""
    from scripts.research import verify_axiom_addresses as cli
    path = _ledger(tmp_path)
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_4663", "https://custody-4663.example")
    monkeypatch.delenv("MEMECOIN_CAPTURE_EVM_READ_URL_56", raising=False)
    asked = []
    monkeypatch.setattr(av, "_http_call", lambda url, method, params: (
        asked.append((url, method)) or DESIGNATOR, None))
    cli.main(["--db", path, "--verify", "--evm-key", "--chain", "57073",
              "--rpc-url", "https://ink.reads.example", "--summary"])
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == {"unverified/evm-key-v1": 1}
    assert asked == [("https://ink.reads.example", "eth_getCode")]
