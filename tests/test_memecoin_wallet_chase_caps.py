"""Bounded per-wallet chase-cap controls for funded copy entries."""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m


NOW = "2026-08-31T16:45:00+00:00"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    conn.execute("INSERT INTO memecoin_wallets(source_user_id, handle) VALUES('koy','koy')")
    conn.commit()
    return conn


def test_wallet_chase_cap_is_scoped_to_its_chain_and_band():
    conn = _conn()

    m.set_wallet_chase_cap(
        conn, "koy", network_id="4663", coin_band="nano", cap_pct=75.0,
        actor="operator", reason="approved Koy 4663 nano cap", now_iso=NOW,
    )

    assert m.wallet_chase_cap(conn, "koy", network_id="4663", coin_band="nano") == 75.0
    assert m.wallet_chase_cap(conn, "koy", network_id="solana", coin_band="nano") is None
    assert m.wallet_chase_cap(conn, "koy", network_id="4663", coin_band="micro") is None


def test_wallet_chase_cap_rejects_an_unbounded_value():
    conn = _conn()

    with pytest.raises(ValueError):
        m.set_wallet_chase_cap(
            conn, "koy", network_id="4663", coin_band="nano", cap_pct=75.01,
            actor="operator", reason="bad", now_iso=NOW,
        )


def test_wallet_exclusion_clears_the_chase_cap():
    conn = _conn()
    m.set_wallet_chase_cap(
        conn, "koy", network_id="4663", coin_band="nano", cap_pct=75.0,
        actor="operator", reason="approved", now_iso=NOW,
    )

    m.set_wallet_excluded(conn, "koy", True, actor="operator", now_iso=NOW)

    assert m.wallet_chase_cap(conn, "koy", network_id="4663", coin_band="nano") is None
