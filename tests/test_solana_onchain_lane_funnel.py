"""The research funnel must keep chain, time, and transaction identity scoped."""

import sqlite3
import sys

from scripts.research import solana_onchain_lane_funnel as funnel


def test_funnel_uses_solana_fixed_window_and_exact_origin(tmp_path, monkeypatch, capsys):
    db = tmp_path / "funnel.db"
    c = sqlite3.connect(db)
    c.executescript("""
        CREATE TABLE onchain_wallet_detections (
            signature TEXT, source_user_id TEXT, handle TEXT, address TEXT,
            network_id TEXT, mint TEXT, detected_at TEXT, detect_latency_s REAL,
            feed_event_id TEXT);
        CREATE TABLE memecoin_entry_attempts (
            signal_source TEXT, network_id TEXT, mint TEXT, source_user_id TEXT,
            origin_event_id TEXT, decision_started_at TEXT, terminal_status TEXT,
            terminal_reason TEXT);
        CREATE TABLE memecoin_positions (
            execution_mode TEXT, signal_source TEXT, network_id TEXT,
            opened_at TEXT, realized_pnl REAL, is_open INTEGER);
        CREATE TABLE memecoin_wallets (
            source_user_id TEXT, real_approved INTEGER, paused INTEGER,
            excluded INTEGER, real_chains TEXT);
        CREATE TABLE memecoin_wallet_approvals (
            source_user_id TEXT, chain TEXT, created_at TEXT, action TEXT);
        CREATE TABLE memecoin_events (
            fomo_event_id TEXT, source_user_id TEXT, mint TEXT, network_id TEXT, side TEXT,
            received_at_normalized TEXT, received_at TEXT);
    """)
    sig = "1" * 80
    c.executemany("INSERT INTO onchain_wallet_detections VALUES (?,?,?,?,?,?,?,?,?)", [
        (sig, "u", "h", "a", "solana", "m", "2026-09-24T10:00:00+00:00", 1.0, None),
        ("other", "u", "h", "a", "4663", "m", "2026-09-24T10:00:00+00:00", 1.0, None),
        ("late", "u", "h", "a", "solana", "m", "2026-09-24T11:00:00+00:00", 1.0, None),
    ])
    c.executemany("INSERT INTO memecoin_entry_attempts VALUES (?,?,?,?,?,?,?,?)", [
        ("follow_activity", "solana", "m", "u", "swap:raybot:" + "2" * 80,
         "2026-09-24T09:59:59+00:00", "refused", "no quote"),
        ("onchain_watch", "solana", "m", "u", "onchain:" + sig,
         "2026-09-24T10:00:01+00:00", "filled", None),
        ("onchain_watch", "4663", "m", "u", "onchain:other",
         "2026-09-24T10:00:01+00:00", "filled", None),
        ("onchain_watch", "solana", "m", "u", "onchain:late",
         "2026-09-24T11:00:01+00:00", "filled", None),
    ])
    c.executemany("INSERT INTO memecoin_positions VALUES (?,?,?,?,?,?)", [
        ("real", "onchain_watch", "solana", "2026-09-24T10:00:02+00:00", 1.0, 0),
        ("real", "onchain_watch", "4663", "2026-09-24T10:00:02+00:00", 2.0, 0),
        ("real", "onchain_watch", "solana", "2026-09-24T11:00:02+00:00", 3.0, 0),
    ])
    c.execute("INSERT INTO memecoin_wallets VALUES ('u',1,0,0,'solana')")
    c.executemany("INSERT INTO memecoin_events VALUES (?,?,?,?,?,?,?)", [
        ("swap:raybot:" + sig + ":buy", "u", "m", "solana", "buy",
         "2026-09-24T10:02:00Z", None),
        ("swap:raybot:other:buy", "u", "m", "4663", "buy",
         "2026-09-24T10:01:00+00:00", None),
        ("onchain:late", "u", "m", "solana", "buy",
         "2026-09-24T11:02:00+00:00", None),
    ])
    c.commit()
    c.close()
    monkeypatch.setattr(funnel, "DB", f"file:{db}?mode=ro")
    monkeypatch.setattr(sys, "argv", ["funnel", "--since", "2026-09-24T09:00:00Z",
                                   "--until", "2026-09-24T10:30:00Z"])
    assert funnel.main() == 0
    out = capsys.readouterr().out
    assert "COUNT detections 1 " in out
    assert "COUNT attempts   1 " in out
    assert "COUNT fills      1  realized 1.0" in out
    assert "{'onchain_watch': 1}" in out
    assert "feed-matched when recorded" in out
    assert "later exact feed arrival by cutoff 1" in out
