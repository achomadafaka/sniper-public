"""Chain-aware coin identity and SQLite race contracts for memecoin entries."""
from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_evm_risk as evm_risk
from sniper.services import memecoin_real_runner as real_runner


def _connect(path):
    conn = sqlite3.connect(path, timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _file_db(tmp_path):
    path = tmp_path / "chain_identity.sqlite"
    conn = _connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    m.ensure_memecoin_tables(conn)
    conn.close()
    return path


def _reserve(conn, *, network_id, mint="0xabc", source="global_feed"):
    return m.reserve_pending(
        conn,
        mint=mint,
        source_user_id=source,
        strategy="alpha_b",
        notional=10.0,
        reserved_at="2026-07-15T00:00:00Z",
        network_id=network_id,
        signal_source=source,
    )


def _open(conn, *, network_id, mint="0xabc", mode="real"):
    return m.open_position(
        conn,
        source_user_id="u1",
        source_handle="h",
        mint=mint,
        ticker="T",
        entry_price=1.0,
        entry_notional=10.0,
        entry_fill_source="chain",
        entry_lag_s=0.1,
        their_size_usd=0.0,
        opened_at="2026-07-15T00:00:00Z",
        strategy="alpha_b",
        execution_mode=mode,
        network_id=network_id,
    )


@pytest.mark.parametrize(
    ("network_id", "mint", "expected"),
    [
        (None, "So1Base58", "solana"),
        ("", "So1Base58", "solana"),
        (" SOLANA ", "So1Base58", "solana"),
        ("1399811149", "So1Base58", "solana"),
        (None, "0xAbC", "evm-unknown"),
        ("", " 0XAbC ", "evm-unknown"),
        ("8453", "0xAbC", "8453"),
        (" 4663 ", "0xAbC", "4663"),
    ],
)
def test_normalized_coin_network_matches_legacy_identity_contract(network_id, mint, expected):
    assert m.normalized_coin_network(network_id, mint) == expected
    assert m.coin_identity(network_id, mint) == (expected, str(mint or "").strip())
    conn = sqlite3.connect(":memory:")
    sql_value = conn.execute(
        f"SELECT {m.COIN_NETWORK_SQL} FROM (SELECT ? AS network_id, ? AS mint)",
        (network_id, mint),
    ).fetchone()[0]
    assert sql_value == expected


def test_same_address_on_base_and_robinhood_reserves_independently(tmp_path):
    path = _file_db(tmp_path)
    a = _connect(path)
    b = _connect(path)
    try:
        base_id = _reserve(a, network_id="8453")
        robinhood_id = _reserve(b, network_id="4663", source="follow_activity")
        assert base_id is not None and robinhood_id is not None
        assert base_id != robinhood_id
        assert m.pending_open_coin_ids(a) == {("8453", "0xabc"), ("4663", "0xabc")}
    finally:
        a.close()
        b.close()


def test_same_chain_direct_reservation_race_has_one_winner(tmp_path):
    path = _file_db(tmp_path)

    def worker(source):
        conn = _connect(path)
        try:
            return _reserve(conn, network_id="4663", source=source)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(worker, ("global_feed", "follow_activity")))
    assert sum(pending_id is not None for pending_id in ids) == 1
    conn = _connect(path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM memecoin_pending_entries WHERE status='reserved'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_global_and_follow_funded_race_has_one_winner(tmp_path, monkeypatch):
    monkeypatch.setattr(evm_risk.cfg, "MEMECOIN_REAL_ENABLED", True)
    path = _file_db(tmp_path)

    def worker(source):
        conn = _connect(path)
        try:
            return evm_risk.reserve_entry_evm(
                conn,
                mint="0xabc",
                source_user_id="u1",
                strategy="alpha_b",
                network_id="4663",
                day="2026-07-15",
                usdc_balance=300.0,
                native_balance=1.0,
                now_iso="2026-07-15T00:00:00Z",
                signal_source=source,
            )[1]
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(worker, ("global_feed", "follow_activity")))
    assert sum(pending_id is not None for pending_id in ids) == 1


def test_legacy_unknown_network_buckets_are_deterministic(tmp_path):
    path = _file_db(tmp_path)
    conn = _connect(path)
    try:
        assert _reserve(conn, network_id=None, mint="So1Legacy") is not None
        assert _reserve(conn, network_id="solana", mint="So1Legacy") is None
        assert _reserve(conn, network_id="1399811149", mint="So1Legacy") is None

        assert _reserve(conn, network_id=None, mint="0xlegacy") is not None
        assert _reserve(conn, network_id="evm-unknown", mint="0xlegacy") is None
        assert _reserve(conn, network_id="8453", mint="0xlegacy") is not None
        assert _reserve(conn, network_id="4663", mint="0xlegacy") is not None
    finally:
        conn.close()


def test_open_uniqueness_is_chain_mode_arm_and_user_scoped():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    base_id = _open(conn, network_id="8453")
    robinhood_id = _open(conn, network_id="4663")
    paper_id = _open(conn, network_id="8453", mode="paper")
    assert len({base_id, robinhood_id, paper_id}) == 3
    with pytest.raises(sqlite3.IntegrityError):
        _open(conn, network_id="8453")


def test_get_open_position_requires_exact_chain_for_evm_identity():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    base_id = _open(conn, network_id="8453")
    robinhood_id = _open(conn, network_id="4663")
    assert m.get_open_position(
        conn, "u1", "0xabc", "alpha_b", mode="real", network_id="8453"
    )["id"] == base_id
    assert m.get_open_position(
        conn, "u1", "0xabc", "alpha_b", mode="real", network_id="4663"
    )["id"] == robinhood_id
    assert m.get_open_position(conn, "u1", "0xabc", "alpha_b", mode="real") is None


def test_real_mirror_closes_only_the_event_chain_and_unknown_closes_none(monkeypatch):
    # Chain selection is independent of the receipt-only full-liquidation policy.
    monkeypatch.setattr(real_runner.cfg, "MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION", 0.5)
    monkeypatch.setattr(real_runner.cfg, "MEMECOIN_REAL_MIRROR_SIZE_CHAINS", frozenset())
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    base_id = _open(conn, network_id="8453")
    _open(conn, network_id="4663")
    calls = []
    contexts = {
        "8453": {"exit_fn": lambda c, pos, reason, now: calls.append(pos["id"]) or {"status": "closed"}},
        "4663": {"exit_fn": lambda c, pos, reason, now: calls.append(pos["id"]) or {"status": "closed"}},
    }
    event = type("Event", (), {
        "source_user_id": "u1", "mint": "0xabc", "network_id": "8453"
    })()
    real_runner.real_mirror_on_sell(conn, event, contexts, now_fn=lambda: "t")
    assert calls == [base_id]
    event.network_id = ""
    assert real_runner.real_mirror_on_sell(conn, event, contexts, now_fn=lambda: "t") is None
    assert calls == [base_id]


def test_risk_dedup_blocks_same_chain_but_not_other_concrete_chain(monkeypatch):
    monkeypatch.setattr(evm_risk.cfg, "MEMECOIN_REAL_ENABLED", True)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    _open(conn, network_id="8453")
    robinhood = evm_risk.build_evm_risk_context(
        conn,
        usdc_balance=300.0,
        native_balance=1.0,
        day="2026-07-15",
        network_id="4663",
    )
    base = evm_risk.build_evm_risk_context(
        conn,
        usdc_balance=300.0,
        native_balance=1.0,
        day="2026-07-15",
        network_id="8453",
    )
    assert evm_risk.evm_entry_allowed(
        robinhood, mint="0xabc", trade_size=robinhood.trade_size_usd
    ).allowed
    assert not evm_risk.evm_entry_allowed(
        base, mint="0xabc", trade_size=base.trade_size_usd
    ).allowed


@pytest.mark.parametrize("network_id", [None, "", "evm-unknown"])
def test_evm_reservation_refuses_unresolved_network(monkeypatch, network_id):
    monkeypatch.setattr(evm_risk.cfg, "MEMECOIN_REAL_ENABLED", True)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    decision, pending_id = evm_risk.reserve_entry_evm(
        conn,
        mint="0xabc",
        source_user_id="u1",
        strategy="alpha_b",
        network_id=network_id,
        day="2026-07-15",
        usdc_balance=300.0,
        native_balance=1.0,
        now_iso="2026-07-15T00:00:00Z",
    )
    assert not decision.allowed and pending_id is None
    assert "unresolved" in decision.reason
    assert m.pending_totals(conn) == (0, 0.0)


def test_index_migration_creates_new_guards_before_dropping_old_guards(tmp_path):
    path = _file_db(tmp_path)
    conn = _connect(path)
    try:
        conn.execute("DROP INDEX IF EXISTS idx_mc_pending_coin_uniq_v2")
        conn.execute("DROP INDEX IF EXISTS idx_mc_pos_open_uniq_v3")
        conn.execute("DROP INDEX IF EXISTS idx_mc_pending_mint_uniq")
        conn.execute("DROP INDEX IF EXISTS idx_mc_pos_open_uniq_v2")
        conn.execute(
            "CREATE UNIQUE INDEX idx_mc_pending_mint_uniq "
            "ON memecoin_pending_entries(mint) WHERE status='reserved'"
        )
        conn.execute(
            "CREATE UNIQUE INDEX idx_mc_pos_open_uniq_v2 ON memecoin_positions("
            "source_user_id, mint, strategy, COALESCE(execution_mode,'paper')) WHERE is_open=1"
        )
        conn.commit()
        sql = []
        conn.set_trace_callback(sql.append)
        m.ensure_memecoin_tables(conn)
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )}
        assert "idx_mc_pending_coin_uniq_v2" in names
        assert "idx_mc_pos_open_uniq_v3" in names
        assert "idx_mc_pending_mint_uniq" not in names
        assert "idx_mc_pos_open_uniq_v2" not in names
        create_new = [i for i, statement in enumerate(sql) if (
            "idx_mc_pending_coin_uniq_v2" in statement or "idx_mc_pos_open_uniq_v3" in statement
        ) and "CREATE" in statement.upper()]
        drop_old = min(i for i, statement in enumerate(sql) if (
            "idx_mc_pending_mint_uniq" in statement or "idx_mc_pos_open_uniq_v2" in statement
        ) and "DROP" in statement.upper())
        assert len(create_new) == 2
        assert max(create_new) < drop_old
        m.ensure_memecoin_tables(conn)
    finally:
        conn.close()


def test_owned_index_migration_failure_rolls_back_partial_schema(tmp_path, monkeypatch):
    path = _file_db(tmp_path)
    conn = _connect(path)
    conn.execute("DROP INDEX idx_mc_pending_coin_uniq_v2")
    conn.execute("DROP INDEX idx_mc_pos_open_uniq_v3")
    conn.commit()
    original = m._create_and_verify_index

    def fail_second_index(target, name, sql):
        if name == "idx_mc_pending_coin_uniq_v2":
            raise RuntimeError("injected pending-index failure")
        return original(target, name, sql)

    monkeypatch.setattr(m, "_create_and_verify_index", fail_second_index)
    with pytest.raises(RuntimeError, match="injected pending-index failure"):
        m.ensure_memecoin_tables(conn)

    assert not conn.in_transaction
    names = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    )}
    assert "idx_mc_pos_open_uniq_v3" not in names
    assert "idx_mc_pending_coin_uniq_v2" not in names
    conn.close()


def test_schema_ensure_fails_before_mutating_caller_owned_transaction(tmp_path):
    path = _file_db(tmp_path)
    conn = _connect(path)
    conn.execute("DROP INDEX idx_mc_pending_coin_uniq_v2")
    conn.execute("DROP INDEX idx_mc_pos_open_uniq_v3")
    conn.commit()
    conn.execute("BEGIN")
    conn.execute(
        "INSERT INTO memecoin_wallets(source_user_id, handle) VALUES ('caller-owned', 'h')"
    )
    schema_before = conn.execute(
        "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()

    with pytest.raises(
        RuntimeError,
        match="ensure_memecoin_tables must be invoked outside an active transaction",
    ):
        m.ensure_memecoin_tables(conn)

    assert conn.in_transaction
    schema_after = conn.execute(
        "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    assert [tuple(row) for row in schema_after] == [tuple(row) for row in schema_before]
    names = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    )}
    assert "idx_mc_pos_open_uniq_v3" not in names
    assert "idx_mc_pending_coin_uniq_v2" not in names
    assert conn.execute(
        "SELECT COUNT(*) FROM memecoin_wallets WHERE source_user_id='caller-owned'"
    ).fetchone()[0] == 1
    conn.rollback()
    assert conn.execute(
        "SELECT COUNT(*) FROM memecoin_wallets WHERE source_user_id='caller-owned'"
    ).fetchone()[0] == 0
    conn.close()


def test_schema_ensure_reserves_writer_before_reading_migrations(tmp_path):
    path = _file_db(tmp_path)
    conn = _connect(path)
    statements = []
    conn.set_trace_callback(statements.append)

    m.ensure_memecoin_tables(conn)

    begin = next(sql.strip().upper() for sql in statements if sql.strip().upper().startswith("BEGIN"))
    assert begin == "BEGIN IMMEDIATE"
    conn.close()


def test_index_migration_reports_colliding_normalized_groups(tmp_path):
    path = _file_db(tmp_path)
    conn = _connect(path)
    conn.execute("DROP INDEX idx_mc_pos_open_uniq_v3")
    conn.commit()
    _open(conn, network_id="8453")
    _open(conn, network_id="8453")

    with pytest.raises(
        sqlite3.IntegrityError,
        match=r"colliding_groups=.*8453.*0xabc",
    ):
        m.ensure_memecoin_tables(conn)

    assert not conn.in_transaction
    conn.close()


def test_startup_cleanup_does_not_close_cross_chain_positions():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    base_id = _open(conn, network_id="8453")
    robinhood_id = _open(conn, network_id="4663")
    m.ensure_memecoin_tables(conn)
    rows = conn.execute(
        "SELECT id, is_open FROM memecoin_positions WHERE id IN (?,?) ORDER BY id",
        (base_id, robinhood_id),
    ).fetchall()
    assert [(row["id"], row["is_open"]) for row in rows] == [
        (base_id, 1), (robinhood_id, 1)
    ]
