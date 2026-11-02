"""Off-path fee reconciler: research hosts only, write-once per tx, unknown != 0, stop on the first error,
and a fills-without-fee counter that shows any unrecorded path. Fake RPC answers only; no network."""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_fee_reconciler as F

T = "2026-10-21T12:00:00+00:00"


def ledger(tmp_path, fills):
    conn = sqlite3.connect(tmp_path / "ledger.db")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    for i, (network, kind, sig) in enumerate(fills):
        pid = m.open_position(conn, source_user_id="u", source_handle="h", mint="M%d" % i, ticker=None,
                              entry_price=0.001, entry_notional=25, entry_fill_source="chain", entry_lag_s=0,
                              their_size_usd=0, opened_at=T, execution_mode="real", network_id=network)
        m.insert_fill(conn, position_id=pid, kind=kind, price=0.001, qty=1, notional=25, fill_source="chain",
                      fraction=1.0, created_at=T, tx_sig=sig)
    conn.commit()
    return conn


SENDER_TIP = "4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE"
NOZOMI_TIP = "noz3jAjPiHuBPqiSPkkugaJDkJscPuRhYnSpbi8UvC4"


def sol_tx(fee, keys=("payer",), pre=(10**9,), post=(10**9,), loaded=None, slot=7):
    meta = {"fee": fee, "preBalances": list(pre), "postBalances": list(post)}
    if loaded:
        meta["loadedAddresses"] = loaded
    return {"slot": slot, "meta": meta, "transaction": {"message": {"accountKeys": list(keys)}}}


class Rpc:
    def __init__(self, answers, fail_on=None):
        self.answers, self.calls, self.fail_on = answers, [], fail_on

    def __call__(self, method, params):
        self.calls.append((method, params[0] if params else None))
        if self.fail_on and len(self.calls) == self.fail_on:
            raise F.Stop("http_429")
        return self.answers.get((method, str(params[0])))


def test_hosts_are_research_only_and_refuse_funded_overlaps():
    env = {"MEMECOIN_RESEARCH_DRPC_RPC_URL_8453": "https://lb.drpc.org/base/KEY",
           "MEMECOIN_EVM_RPC_URL_8453": "https://mainnet.base.org",
           "MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663_2": "https://q2.example/k"}
    assert F.host_for("8453", env).startswith("https://lb.drpc.org")
    assert F.host_for("56", env) == F.MEOWRPC_56
    assert F.host_for("4663", env) == "https://q2.example/k"
    with pytest.raises(ValueError, match="mainnet.base.org"):
        F.host_for("8453", {**env, "MEMECOIN_RESEARCH_DRPC_RPC_URL_8453": "https://mainnet.base.org"})
    with pytest.raises(ValueError, match="funded host"):
        F.host_for("4663", {**env, "MEMECOIN_EVM_RPC_URL_4663": "https://q2.example/other"})
    with pytest.raises(ValueError, match="unset"):
        F.host_for("solana", {})
    funded = "https://mainnet.helius-rpc.com/?api-key=SAMEKEY"
    with pytest.raises(ValueError, match="funded key"):
        F.host_for("solana", {"MEMECOIN_RESEARCH_HELIUS_RPC_URLS": funded, "MEMECOIN_SOLANA_RPC_URL": funded})
    with pytest.raises(ValueError, match="not reconciled"):
        F.host_for("1", env)                                     # chain 1 / Ink keep the runner's reconcile


def test_solana_fees_are_recorded_once_pending_stays_and_counter_drops(tmp_path):
    conn = ledger(tmp_path, [("solana", "entry", "SIG1"), ("solana", "bracket_exit", "SIG2")])
    assert m.fills_without_fee(conn) == {"solana": {"entry": 1, "bracket_exit": 1}}
    rpc = Rpc({("getTransaction", "SIG1"): sol_tx(105000)})   # SIG2 not found yet
    out = F.run_pass(conn, "solana", rpc, now_iso=T)
    assert out == {"recorded": 1, "unknown": 0, "pending": 1, "stopped": None}
    assert [tuple(r) for r in conn.execute("SELECT fee_lamports, slot, status FROM memecoin_sol_tx_fees")] == \
        [(105000, 7, "complete")]
    assert m.fills_without_fee(conn) == {"solana": {"bracket_exit": 1}}
    m.record_sol_tx_fee(conn, tx_sig="SIG1", kind="entry", fee={"fee_lamports": 1, "status": "complete"})
    assert conn.execute("SELECT fee_lamports FROM memecoin_sol_tx_fees").fetchone()[0] == 105000   # write-once


def test_sender_and_nozomi_tips_are_stored_apart_from_meta_fee(tmp_path):
    # Gamma 2026-10-21: tips are SOL transfers outside meta.fee (113 of 617 exit-grid entries); the
    # Nozomi tip account arrives through an ALT (loadedAddresses), the decoy key gains but is not a tip
    conn = ledger(tmp_path, [("solana", "entry", "SIG1"), ("solana", "entry", "SIG2")])
    tx1 = sol_tx(5000, keys=("payer", SENDER_TIP, "pool"), pre=(10**9, 0, 50), post=(10**9 - 1_005_000, 1_000_000, 9_000))
    tx2 = sol_tx(5000, keys=("payer",), pre=(10**9, 7, 0), post=(10**9 - 5000, 9, 5_000_000),
                 loaded={"writable": ["pool"], "readonly": [NOZOMI_TIP]})
    out = F.run_pass(conn, "solana", Rpc({("getTransaction", "SIG1"): tx1, ("getTransaction", "SIG2"): tx2}), now_iso=T)
    assert out["recorded"] == 2
    assert [tuple(r) for r in conn.execute(
        "SELECT tx_sig, fee_lamports, tip_lamports, status FROM memecoin_sol_tx_fees ORDER BY tx_sig")] == [
        ("SIG1", 5000, 1_000_000, "complete"), ("SIG2", 5000, 5_000_000, "complete")]


def test_unreadable_balances_leave_the_tip_unknown_not_zero(tmp_path):
    conn = ledger(tmp_path, [("solana", "entry", "SIG1")])
    F.run_pass(conn, "solana", Rpc({("getTransaction", "SIG1"): {"slot": 7, "meta": {"fee": 5000}}}), now_iso=T)
    assert tuple(conn.execute("SELECT fee_lamports, tip_lamports, status FROM memecoin_sol_tx_fees").fetchone()) == \
        (5000, None, "unknown:tip")
    assert m.fills_without_fee(conn) == {"solana": {"entry": 1}}


def test_a_fill_with_its_own_fee_column_is_not_missing(tmp_path):
    conn = ledger(tmp_path, [("solana", "entry", "SIG1"), ("56", "entry", None)])
    conn.execute("UPDATE memecoin_fills SET network_fee_lamports=5000 WHERE tx_sig='SIG1'")
    assert m.fills_without_fee(conn) == {"56": {"entry": 1}}              # no tx_sig: unknown, still counted


def test_a_missing_fee_is_unknown_never_zero(tmp_path):
    conn = ledger(tmp_path, [("solana", "entry", "SIG1")])
    F.run_pass(conn, "solana", Rpc({("getTransaction", "SIG1"): {"slot": 7, "meta": {}}}), now_iso=T)
    assert tuple(conn.execute("SELECT fee_lamports, status FROM memecoin_sol_tx_fees").fetchone()) == (None, "unknown:fee")
    assert m.fills_without_fee(conn) == {"solana": {"entry": 1}}


def test_the_first_429_stops_the_chain_pass(tmp_path):
    conn = ledger(tmp_path, [("solana", "entry", "S%d" % i) for i in range(5)])
    rpc = Rpc({}, fail_on=2)
    out = F.run_pass(conn, "solana", rpc, now_iso=T)
    assert out["stopped"] == "http_429" and len(rpc.calls) == 2


def test_evm_fees_per_chain_family(tmp_path):
    conn = ledger(tmp_path, [("56", "entry", "0xAA"), ("8453", "entry", "0xBB")])
    receipt = {"blockNumber": "0x10", "gasUsed": "0x5208", "effectiveGasPrice": "0x3b9aca00"}
    out = F.run_pass(conn, "56", Rpc({("eth_getTransactionReceipt", "0xaa"): receipt}), now_iso=T)
    assert out["recorded"] == 1
    assert tuple(conn.execute("SELECT total_wei, status FROM memecoin_evm_tx_fees WHERE tx_hash='0xaa'").fetchone()) == \
        (str(21000 * 10**9), "complete")
    oracle_call = {"to": "0x420000000000000000000000000000000000000F", "data": "0x275aedd2" + format(21000, "064x")}
    op = Rpc({("eth_getTransactionReceipt", "0xbb"): {**receipt, "l1Fee": "0x64"},
              ("eth_call", str(oracle_call)): "0x" + "0" * 63 + "5"})
    F.run_pass(conn, "8453", op, now_iso=T)
    assert ("eth_call", oracle_call) in op.calls                         # the operator fee at the tx's block
    assert tuple(conn.execute("SELECT total_wei, operator_wei, status FROM memecoin_evm_tx_fees WHERE tx_hash='0xbb'"
                              ).fetchone()) == (str(21000 * 10**9 + 100 + 5), "5", "complete")


def test_paced_caller_caps_calls_and_paces(tmp_path):
    slept = []
    p = F.Paced("u", 1.0, cap=2, post=lambda url, body: {"result": None}, clock=lambda: 0.0, sleep=slept.append)
    p("m", [])
    p("m", [])
    with pytest.raises(F.Stop, match="call_cap"):
        p("m", [])
    assert slept == [1.0]
    with pytest.raises(F.Stop, match="rpc_429"):
        F.Paced("u", 0, post=lambda url, body: {"error": {"code": 429}})("m", [])


def test_a_drpc_research_read_takes_the_shared_account_pacer_and_a_limit_pauses_it(monkeypatch):
    # Alpha 2026-10-21: every reader of the keyed dRPC research account goes through its one pacer
    import sniper.services.memecoin_research_pacer as rp
    took, paused = [], []
    monkeypatch.setattr(rp.SharedPacer, "acquire", lambda self: took.append(self.path))
    monkeypatch.setattr(rp.SharedPacer, "pause", lambda self, s: paused.append(s))
    F.Paced("https://lb.drpc.org/ogrpc?network=base&dkey=KEY", 0, post=lambda url, body: {"result": "0x1"})("m", [])
    assert len(took) == 1 and "drpc-keyed" in str(took[0])
    with pytest.raises(F.Stop, match="rpc_error"):
        F.Paced("https://lb.drpc.org/ogrpc?network=base&dkey=KEY", 0,
                post=lambda url, body: {"error": {"code": 15, "message": "free plan rate limit"}})("m", [])
    assert paused == [rp.DRPC_LIMIT_PAUSE_S]
    F.Paced(F.MEOWRPC_56, 0, post=lambda url, body: {"result": None})("m", [])
    assert len(took) == 2                                                  # another host: not this account


def test_cli_dry_run_makes_no_call_and_live_needs_the_knob(tmp_path, monkeypatch, capsys):
    import importlib.util
    import json as _json
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("fee_cli", Path(__file__).resolve().parents[1] / "scripts" /
                                                  "memecoin_fee_reconciler.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    conn = ledger(tmp_path, [("solana", "entry", "SIG1"), ("56", "entry", "0xaa")])
    conn.close()
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.delenv("MEMECOIN_FEE_RECONCILER_ENABLED", raising=False)
    boom = lambda *a: (_ for _ in ()).throw(AssertionError("network call in a dry run"))
    assert cli.main(["--db", str(tmp_path / "ledger.db")], post=boom) == 0
    out = _json.loads(capsys.readouterr().out)
    assert out["mode"] == "dry_run" and out["backlog"]["solana"] == 1 and out["backlog"]["56"] == 1
    assert out["fills_without_fee"] == {"56": {"entry": 1}, "solana": {"entry": 1}}
    assert cli.main(["--db", str(tmp_path / "ledger.db"), "--live"], post=boom) == 2
