"""The report must exclude paper/unarmed rows and preserve unknown outcomes."""
from pathlib import Path
import sqlite3
import subprocess
import sys


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/research/copy_revival_report.py"


def test_copy_revival_report(tmp_path):
    db = tmp_path / "ledger.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE memecoin_wallets(source_user_id TEXT, handle TEXT,
                real_approved INTEGER, real_chains TEXT, paused INTEGER, excluded INTEGER);
            CREATE TABLE memecoin_events(source_user_id TEXT, network_id TEXT,
                side TEXT, created_at TEXT, real_decision TEXT, real_decision_reason TEXT);
            CREATE TABLE memecoin_positions(source_user_id TEXT, network_id TEXT,
                execution_mode TEXT, opened_at TEXT, is_open INTEGER, closed_at TEXT,
                realized_pnl REAL);
        """)
        conn.executemany("INSERT INTO memecoin_wallets VALUES (?,?,?,?,?,?)", [
            ("armed", "Alice", 1, "solana,robin", 0, 0),
            ("quiet", "Quiet", 1, "solana", 0, 0),
            ("watch", "Unarmed", 0, "solana", 0, 0),
            ("paused", "Paused", 1, "solana", 1, 0),
        ])
        conn.executemany("INSERT INTO memecoin_events VALUES (?,?,?,?,?,?)", [
            ("armed", "solana", "buy", "2026-10-26T13:00:00Z", "rejected", "exposure"),
            ("armed", "solana", "buy", "2026-10-26T09:01:00-04:00", "rejected", "exposure"),
            ("armed", "solana", "buy", "2026-10-26T13:02:00Z", "filled", "filled $25"),
            ("armed", "solana", "buy", "2026-10-26T13:02:00Z", None, None),
            ("armed", "solana", "buy", "2026-10-26T13:02:00Z", "deferred", "waiting"),
            ("armed", "4663", "buy", "2026-10-26T13:03:00Z", "unsafe", None),
            ("armed", "8453", "buy", "2026-10-26T13:03:00Z", "rejected", "wrong-chain"),
            ("armed", "solana", "buy", "2026-10-26T12:59:59Z", "rejected", "too-old"),
            ("armed", "solana", "sell", "2026-10-26T13:03:00Z", "rejected", "sell"),
            ("watch", "solana", "buy", "2026-10-26T13:03:00Z", "rejected", "unarmed"),
            ("paused", "solana", "buy", "2026-10-26T13:03:00Z", "rejected", "paused"),
        ])
        conn.executemany("INSERT INTO memecoin_positions VALUES (?,?,?,?,?,?,?)", [
            ("armed", "solana", "real", "2026-10-26T09:01:00-04:00", 0, "2026-10-26T13:04:00Z", 4),
            ("armed", "solana", "real", "2026-10-26T13:01:00Z", 0, "2026-10-26T13:04:00Z", 0),
            ("armed", "solana", "real", "2026-10-26T13:01:00Z", 1, None, None),
            ("armed", "4663", "real", "2026-10-26T13:01:00Z", 0, "2026-10-26T13:04:00Z", None),
            ("armed", "4663", "real", "2026-10-26T13:01:00Z", 0, None, None),
            ("armed", "solana", "paper", "2026-10-26T13:01:00Z", 0, "2026-10-26T13:04:00Z", 900),
            ("watch", "solana", "real", "2026-10-26T13:01:00Z", 0, "2026-10-26T13:04:00Z", 500),
            ("armed", "8453", "real", "2026-10-26T13:01:00Z", 0, "2026-10-26T13:04:00Z", 600),
            ("armed", "solana", "real", "2026-10-26T12:59:59Z", 0, "2026-10-26T13:04:00Z", 700),
        ])
    before = db.read_bytes()
    assert SCRIPT.is_file(), "copy revival report has not been implemented"
    run = subprocess.run([sys.executable, str(SCRIPT), "--db", str(db)],
                         capture_output=True, text=True, timeout=15)
    assert run.returncode == 0, run.stderr
    lines = [line.split() for line in run.stdout.splitlines()]
    assert ["armed", "Alice", "solana", "5", "3", "2", "1", "0", "4.00", "0"] in lines
    assert ["armed", "Alice", "4663", "1", "2", "1", "0", "1", "0.00", "1"] in lines
    assert ["quiet", "Quiet", "solana", "0", "0", "0", "0", "0", "0.00", "0"] in lines
    assert ["TOTAL", "-", "-", "6", "5", "3", "1", "1", "4.00", "1"] in lines
    assert ["armed", "solana", "rejected", "exposure", "2"] in lines
    assert ["armed", "solana", "unknown", "unknown", "1"] in lines
    assert ["rejected", "exposure", "2"] in lines
    assert ["unsafe", "unknown", "1"] in lines
    assert ["deferred", "waiting", "1"] not in lines
    assert all(word not in run.stdout for word in ("Unarmed", "Paused", "wrong-chain", "too-old"))
    assert "not chain time" in run.stdout
    assert db.read_bytes() == before
    missing = tmp_path / "missing.db"
    refused = subprocess.run([sys.executable, str(SCRIPT), "--db", str(missing)],
                             capture_output=True, text=True, timeout=15)
    assert refused.returncode == 2 and not missing.exists()
    naive = subprocess.run([sys.executable, str(SCRIPT), "--db", str(db),
                            "--since", "2026-10-26T13:00:00"],
                           capture_output=True, text=True, timeout=15)
    assert naive.returncode == 2 and "explicit UTC offset" in naive.stderr
