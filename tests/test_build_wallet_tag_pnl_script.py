"""C3c acceptance: wallet_tag_pnl backfill can report and limit candidates."""

from __future__ import annotations

import sqlite3


def _make_db(path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE user_analysis(wallet TEXT PRIMARY KEY)")
        conn.executemany(
            "INSERT INTO user_analysis(wallet) VALUES (?)",
            [(f"0x{i:040x}",) for i in range(200)],
        )
        conn.execute(
            """
            CREATE TABLE wallet_tag_pnl (
                wallet                    TEXT    NOT NULL,
                tag                       TEXT    NOT NULL,
                n_positions               INTEGER NOT NULL,
                realized_pnl              REAL    NOT NULL,
                win_rate                  REAL    NOT NULL,
                conviction_concentration  REAL    NOT NULL,
                computed_at               INTEGER NOT NULL,
                win_rate_held_only        REAL,
                n_held_to_resolution      INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (wallet, tag)
            )
            """
        )
        conn.execute(
            "INSERT INTO wallet_tag_pnl(wallet, tag, n_positions, realized_pnl, "
            "win_rate, conviction_concentration, computed_at, win_rate_held_only, "
            "n_held_to_resolution) VALUES (?, 'nba', 10, 0, .5, 0, 0, .5, 10)",
            ("0x0000000000000000000000000000000000000000",),
        )
        conn.commit()
    finally:
        conn.close()


def test_report_only_user_analysis_limit_does_not_compute(tmp_path, monkeypatch):
    db_path = tmp_path / "wallets.db"
    _make_db(db_path)

    import scripts.build_wallet_tag_pnl as mod

    called = False

    def _boom(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("report-only must not compute wallet rows")

    monkeypatch.setattr(mod, "run_for_wallet", _boom)

    rc = mod.main([
        "--all",
        "--source",
        "user-analysis",
        "--limit",
        "150",
        "--report-only",
        "--db",
        str(db_path),
    ])

    assert rc == 0
    assert called is False


def test_skip_existing_filters_before_limit(tmp_path, monkeypatch):
    db_path = tmp_path / "wallets.db"
    _make_db(db_path)

    import scripts.build_wallet_tag_pnl as mod

    processed: list[str] = []

    def _record(conn, wallet, min_sample):
        processed.append(wallet)
        return {}

    monkeypatch.setattr(mod, "run_for_wallet", _record)

    rc = mod.main([
        "--all",
        "--source",
        "user-analysis",
        "--limit",
        "2",
        "--skip-existing",
        "--db",
        str(db_path),
    ])

    assert rc == 0
    assert processed == [
        "0x0000000000000000000000000000000000000001",
        "0x0000000000000000000000000000000000000002",
    ]
