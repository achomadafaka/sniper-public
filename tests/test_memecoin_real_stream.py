"""SSE cockpit stream: pushes only on change, on its own read-only connection.

One idle dashboard tab held ~100 requests/min against the dev server — a 1s blind ticker poll plus
a 4s five-endpoint cockpit poll. The stream replaces the trigger, not the pipeline: the client's
existing fetch/render path still builds every payload, so the poll path and the push path cannot
drift. A dead stream must degrade to exactly the old polling.
"""
import json
import sqlite3

import pytest

import sniper.routes.api_memecoin as A
from sniper.persistence import db_memecoin as m


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "stream.db"))
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.commit()
    return c


def _open_real(c, mint="MintS"):
    from sniper.settings import memecoin as cfg
    return m.open_position(
        c, source_user_id="u1", source_handle="h", mint=mint, ticker="S",
        entry_price=0.001, entry_notional=50.0, entry_fill_source="chain", entry_lag_s=-1.0,
        their_size_usd=0.0, opened_at="2026-08-14T13:00:00+00:00",
        strategy=cfg.MEMECOIN_REAL_POLICY, execution_mode="real", network_id="solana")


def test_ticker_payload_builds_on_any_connection(conn):
    pid = _open_real(conn)
    t = A._real_ticker_payload(conn, "2026-08-14T13:01:00+00:00")
    assert [p["id"] for p in t["positions"]] == [pid]


def test_change_signature_moves_on_the_cockpit_drivers(conn, monkeypatch):
    from sniper.services import memecoin_arming
    monkeypatch.setattr(memecoin_arming, "is_kill_tripped", lambda path=None: False)
    base = A._cockpit_change_signature(conn)
    assert A._cockpit_change_signature(conn) == base, "no writes -> no change"

    pid = _open_real(conn)
    opened = A._cockpit_change_signature(conn)
    assert opened != base, "an open position must change the signature"

    conn.execute("UPDATE memecoin_positions SET is_open=0, closed_at=datetime('now'), "
                 "closed_recorded_at=datetime('now'), realized_pnl=5.0 WHERE id=?", (pid,))
    conn.commit()
    assert A._cockpit_change_signature(conn) != opened, "a close must change the signature"


def test_change_signature_ignores_the_mark_tape(conn, monkeypatch):
    """Marks commit every second. If they moved the signature, 'changed' would mean 'always' and
    the client would be back on a blind poll with extra steps."""
    from sniper.services import memecoin_arming
    monkeypatch.setattr(memecoin_arming, "is_kill_tripped", lambda path=None: False)
    _open_real(conn)
    base = A._cockpit_change_signature(conn)
    conn.execute(
        "INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, source, quality) "
        "VALUES('solana','MintS',datetime('now'),0.002,'fastlane_mid','live')")
    conn.commit()
    assert A._cockpit_change_signature(conn) == base


def test_kill_switch_moves_the_signature(conn, monkeypatch):
    from sniper.services import memecoin_arming
    tripped = {"v": False}
    monkeypatch.setattr(memecoin_arming, "is_kill_tripped", lambda path=None: tripped["v"])
    base = A._cockpit_change_signature(conn)
    tripped["v"] = True
    assert A._cockpit_change_signature(conn) != base
