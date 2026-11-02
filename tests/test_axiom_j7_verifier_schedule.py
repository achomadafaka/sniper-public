"""The J7 address verifier on a schedule (P1, Alpha GO 2026-09-29): hourly, on public read hosts only.

Its receipt reads defaulted to av.EVM_RPC, and on 4663 and 8453 those hosts are custody (members of
the funded chain's read pool; mainnet.base.org and rpc.mainnet.chain.robinhood.com are also free
submission endpoints). A scheduled run refuses any read host whose domain another role uses, and
refuses outright when the chain's custody URL is not in the environment, because then nothing proves
the host is not custody. Reads are paced so an hourly run cannot burst the research host it shares.
"""
import json
import sqlite3

import pytest

from scripts.research import verify_axiom_addresses as cli
from sniper.services import axiom_address_verify as av
from sniper.settings import memecoin as cfg
from test_axiom_j7_transfer_verify import FX, _conn

PUBLIC = "https://rpc.public-reads.example"
CUSTODY = "https://custody.example/v2/key"


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "ledger.db"
    src = _conn()
    src.executescript("""CREATE TABLE memecoin_wallets (id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_user_id TEXT, handle TEXT, real_approved INTEGER DEFAULT 0, excluded INTEGER DEFAULT 0,
        paused INTEGER DEFAULT 0, real_chains TEXT);""")
    for uid in FX["armed"]:
        src.execute("INSERT INTO memecoin_wallets(source_user_id,handle,real_approved,real_chains) "
                    "VALUES(?,?,1,'56')", (uid, uid))
    src.commit()
    dst = sqlite3.connect(path)
    dst.executescript("\n".join(src.iterdump()))
    dst.close()
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_56", CUSTODY)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {"56": (CUSTODY, "https://pool-2.example")},
                        raising=False)
    asked = []
    monkeypatch.setattr(av, "_http_call", lambda url, method, params: (
        asked.append((url, method)) or FX["receipts"].get(params[0]), None))
    return {"path": str(path), "asked": asked}


def _statuses(path):
    return {r[0] for r in sqlite3.connect(path).execute("SELECT verify_status FROM axiom_verified_addresses")}


@pytest.mark.parametrize("url", ["https://custody.example", "https://pool-2.example/x",
                                 "https://bsc-dataseed.bnbchain.org", "https://eth.custody.example"])
def test_a_read_host_another_role_uses_is_refused_before_any_read(ledger, url):
    """Custody, another read-pool member, a free-submission endpoint (code, not env), and another
    host of custody's domain: providers budget per account or IP across their hostnames."""
    with pytest.raises(SystemExit):
        cli.main(["--db", ledger["path"], "--confirm", "--j7", "--chain", "56", "--rpc-url", url])
    assert ledger["asked"] == [] and _statuses(ledger["path"]) == set()


def test_the_custody_default_is_refused_when_no_read_host_is_given(ledger, monkeypatch):
    monkeypatch.setitem(av.EVM_RPC, "56", CUSTODY)
    with pytest.raises(SystemExit):
        cli.main(["--db", ledger["path"], "--confirm", "--j7", "--chain", "56"])
    assert ledger["asked"] == []


def test_an_unloaded_env_refuses_the_run(ledger, monkeypatch):
    """Absent is not safe: without the custody URL the pool is library defaults only, and nothing
    shows the host is not custody."""
    monkeypatch.delenv("MEMECOIN_EVM_RPC_URL_56")
    with pytest.raises(SystemExit):
        cli.main(["--db", ledger["path"], "--confirm", "--j7", "--chain", "56", "--rpc-url", PUBLIC])
    assert ledger["asked"] == []


def test_the_read_host_can_come_from_a_named_variable(ledger, monkeypatch, capsys):
    monkeypatch.setenv("MEMECOIN_CAPTURE_EVM_READ_URL_56", PUBLIC)
    cli.main(["--db", ledger["path"], "--confirm", "--j7", "--chain", "56",
              "--rpc-url-env", "MEMECOIN_CAPTURE_EVM_READ_URL_56", "--summary"])
    assert {u for u, _m in ledger["asked"]} == {PUBLIC}
    summary = json.loads(capsys.readouterr().out)
    assert summary == {"mode": "confirm", "chain": "56", "host": "rpc.public-reads.example",
                       "written": 3, "rpc_calls": len(ledger["asked"]),
                       "status": {av.CONFIRMED + "/" + av.J7_PROOF_VERSION: 3}}
    assert _statuses(ledger["path"]) == {av.CONFIRMED}


def test_an_unset_read_host_variable_refuses_the_run(ledger, monkeypatch):
    monkeypatch.delenv("MEMECOIN_CAPTURE_EVM_READ_URL_56", raising=False)
    with pytest.raises(SystemExit):
        cli.main(["--db", ledger["path"], "--confirm", "--j7", "--chain", "56",
                  "--rpc-url-env", "MEMECOIN_CAPTURE_EVM_READ_URL_56"])
    assert ledger["asked"] == []


def test_reads_are_paced():
    clock, slept = [100.0], []

    def sleep(s):
        slept.append(round(s, 3))
        clock[0] += s
    call = cli.bounded_rpc(lambda *a: ({}, None), max_calls=10, max_seconds=60, min_interval_s=0.5,
                           clock=lambda: clock[0], sleep=sleep)
    for _ in range(3):
        call("u", "m", [])
    assert slept == [0.5, 0.5]


def test_the_command_loads_the_state_roots_env_before_its_guard(tmp_path):
    """A fresh interpreter, as cron runs it: the custody URL exists only in the state root's .env.
    Without the load the guard would refuse for the wrong reason (URL unset) and, on a host whose
    .env names more pool members, would pass a custody host it could not see."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    root = tmp_path / "state"
    root.mkdir()
    (root / ".env").write_text("MEMECOIN_EVM_RPC_URL_56=https://custody.example/v2/key\n")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "SNIPER_STATE_ROOT": str(root)}
    script = Path(__file__).resolve().parents[1] / "scripts/research/verify_axiom_addresses.py"
    out = subprocess.run([sys.executable, str(script), "--db", str(tmp_path / "absent.db"), "--verify",
                          "--j7", "--chain", "56", "--rpc-url", "https://custody.example"],
                         env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 2, out.stderr[-500:]
    assert "custody.example is used by another role" in out.stderr, out.stderr[-500:]


def test_the_56_receipt_default_is_not_a_pruning_host():
    """rpc.48.club answers null for receipts older than ~7 days (finding 2026-09-29)."""
    assert "48.club" not in av.EVM_RPC["56"]


def test_the_code_check_reads_on_the_same_approved_host(ledger, monkeypatch):
    """Gamma LOW-1: the cross-chain code read goes to the receipt host, never to EVM_RPC's default."""
    seen = {}

    def spy(conn, **kw):
        seen["code"] = kw["code_fn"]("56", "0x" + "a1" * 20)
        return []
    monkeypatch.setattr(av, "verify_j7_addresses", spy)
    monkeypatch.setattr(av, "_http_call", lambda url, method, params: (
        ledger["asked"].append((url, method)) or "0x", None))
    cli.main(["--db", ledger["path"], "--verify", "--j7", "--chain", "56", "--rpc-url", PUBLIC])
    assert seen["code"] == "0x" and ledger["asked"] == [(PUBLIC, "eth_getCode")]
