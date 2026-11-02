import sqlite3

import scripts.tick_feature_job as job


def test_inactive_tape_makes_no_provider_calls(tmp_path, monkeypatch, capsys):
    from sniper.persistence import db_ticks

    tc = db_ticks.connect_ticks(str(tmp_path / "ticks.db"))
    mc = sqlite3.connect(":memory:")
    monkeypatch.setattr(job.t, "connect_ticks", lambda: tc)
    monkeypatch.setattr(job, "_connect_main", lambda path: mc)
    monkeypatch.setattr(job.cfg, "evm_rpc_for_chain", lambda chain: "https://rpc.test")
    monkeypatch.setattr(job, "CHAIN_QUOTE", {"4663": {"token": "0xquote"}})
    def unexpected(*a, **kw):
        raise AssertionError("inactive tape caused provider work")
    monkeypatch.setattr(job, "_eth_call_fn", unexpected)
    monkeypatch.setattr(job, "quote_usd_price", unexpected)
    job.main()
    assert "no_recent_swaps" in capsys.readouterr().out


def test_main_ledger_connection_does_not_pin_wal_between_statements(tmp_path):
    path = tmp_path / "main.sqlite"
    sqlite3.connect(path).close()

    conn = job._connect_main(path)
    try:
        assert conn.isolation_level is None
        assert conn.row_factory is sqlite3.Row
        conn.execute("SELECT 1").fetchone()
        assert conn.in_transaction is False
    finally:
        conn.close()
