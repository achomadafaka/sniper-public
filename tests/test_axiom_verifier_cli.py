"""The verifier command uses bounded reads and short proof writes."""
import json
import sqlite3

import pytest

from scripts.research import verify_axiom_addresses as cli
from sniper.persistence.db_axiom_identity import proof_snapshot, save_verification_results
from sniper.services import axiom_address_verify as av
from test_axiom_address_verify import _fixture, _buys, _reader, NOW


def test_rpc_budget_refuses_an_extra_request():
    calls = []
    call = cli.bounded_rpc(lambda *args: (calls.append(args) or {}, None),
                           max_calls=2, max_seconds=30)
    assert call("url", "method", [])[1] is None
    assert call("url", "method", [])[1] is None
    assert call("url", "method", [])[1] == "rpc_budget_exhausted"
    assert len(calls) == 2


def test_proof_write_preserves_a_concurrent_refutation():
    conn = _fixture(_buys(3))
    expected = proof_snapshot(conn)
    results = av.verify_candidates(conn, rpc_read_fn=_reader([av.BOUGHT]), now_iso=lambda: NOW,
                                    write_results=False)
    results[0]["detail"] = av._detail(results[0])
    conn.execute("INSERT INTO axiom_verified_addresses "
                 "(source_user_id,handle,network_id,address,verify_status,verified_at) "
                 "VALUES ('u1','alice','solana','ADDR1','REFUTED',?)", (NOW,))
    conn.commit()
    assert save_verification_results(conn, results, expected=expected, now=NOW) == 0
    assert conn.execute("SELECT verify_status FROM axiom_verified_addresses").fetchone()[0] == "REFUTED"


def test_atomic_proof_batch_refuses_all_chains_on_a_concurrent_change():
    conn = _fixture(_buys(3))
    expected = proof_snapshot(conn)
    results = av.verify_candidates(conn, rpc_read_fn=_reader([av.BOUGHT]), now_iso=lambda: NOW,
                                    write_results=False)
    results[0]['detail'] = av._detail(results[0])
    results.append({**results[0], 'chain':'56'})
    conn.execute("INSERT INTO axiom_verified_addresses "
                 "(source_user_id,handle,network_id,address,verify_status,verified_at) "
                 "VALUES ('u1','alice','56','ADDR1','REFUTED',?)",(NOW,))
    conn.commit()
    with pytest.raises(ValueError, match='proof batch changed'):
        save_verification_results(conn, results, expected=expected, now=NOW, require_all=True)
    assert [(r[0],r[1]) for r in conn.execute(
        'SELECT network_id,verify_status FROM axiom_verified_addresses')] == [('56','REFUTED')]


def test_cli_default_lists_candidates_without_rpc_or_writes(tmp_path, monkeypatch, capsys):
    path = tmp_path / "ledger.db"
    source = _fixture(_buys(3))
    target = sqlite3.connect(path)
    target.executescript("\n".join(source.iterdump()))
    target.close()
    monkeypatch.setattr(av, "_http_call", lambda *args: pytest.fail("unexpected RPC"))
    cli.main(["--db", str(path)])
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "candidates" and len(report["candidates"]) == 1
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM axiom_verified_addresses").fetchone()[0] == 0


def test_proof_write_keeps_the_strongest_candidate_per_chain():
    conn = _fixture(_buys(3))
    expected = proof_snapshot(conn)
    results = av.verify_candidates(conn, rpc_read_fn=_reader([av.BOUGHT]), now_iso=lambda: NOW,
                                    write_results=False)
    results[0]["detail"] = av._detail(results[0])
    weak = {**results[0], "address": "OTHER", "verify_status": "unverified"}
    assert save_verification_results(conn, results + [weak], expected=expected, now=NOW) == 1
    assert conn.execute("SELECT address FROM axiom_verified_addresses").fetchone()[0] == "ADDR1"


def test_shared_proof_is_revoked_even_when_another_candidate_wins():
    from test_axiom_address_verify import _conn, _lane_row
    conn = _conn(directory_rows=[('alice','Alice','ADDR1','solana',NOW),
                                 ('alice','Alice','ADDR2','solana',NOW)],
                 wallet_rows=[('u1','alice',1,'solana')], events=_buys(3))
    _lane_row(conn, 'u2', 'bob', 'solana', 'ADDR1', av.CONFIRMED)
    expected = proof_snapshot(conn)
    results = av.verify_candidates(conn, rpc_read_fn=_reader([av.BOUGHT]), now_iso=lambda: NOW,
                                    write_results=False)
    for result in results:
        result['detail'] = av._detail(result)
    save_verification_results(conn, results, expected=expected, now=NOW)
    proofs = {r[0]: (r[1],r[2]) for r in conn.execute(
        'SELECT source_user_id,address,verify_status FROM axiom_verified_addresses')}
    assert proofs == {'u1': ('ADDR2', av.CONFIRMED), 'u2': ('ADDR1', av.SHARED)}
