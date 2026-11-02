"""Historical seed rows cannot replace current proof."""
import sqlite3
import sys

from scripts.research import seed_axiom_verified_from_phase1 as seed
from sniper.persistence.db_axiom import ensure_axiom_tables


def test_seed_preserves_a_newer_refutation(monkeypatch, tmp_path):
    path = tmp_path / "ledger.db"
    conn = sqlite3.connect(path)
    ensure_axiom_tables(conn)
    conn.execute("CREATE TABLE memecoin_wallets(source_user_id TEXT, handle TEXT)")
    conn.execute("INSERT INTO memecoin_wallets VALUES ('u1','CryoPalmar')")
    conn.execute("INSERT INTO axiom_verified_addresses "
                 "(source_user_id,handle,network_id,address,verify_status,verified_at,detail) "
                 "VALUES ('u1','CryoPalmar','4663','0xnew','REFUTED','2030-01-01','new proof')")
    conn.commit()
    monkeypatch.setattr(sys, "argv", ["seed", "--confirm", "--db", str(path)])
    seed.main()
    row = conn.execute("SELECT address,verify_status,detail FROM axiom_verified_addresses").fetchone()
    assert row == ("0xnew", "REFUTED", "new proof")


def test_seed_rows_are_unverified_candidates():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE memecoin_wallets(source_user_id TEXT, handle TEXT)")
    conn.execute("INSERT INTO memecoin_wallets VALUES ('u1','CryoPalmar')")
    row = seed.build(conn)[0]
    assert row[5] == "unverified"
    assert row[6:10] == (0, 0, 0, 0)
