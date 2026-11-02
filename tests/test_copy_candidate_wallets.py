"""Paper PnL, add-on buys, missing evidence and hidden roster rows cannot rank a candidate."""
import importlib.util
from pathlib import Path
import sqlite3


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/research/copy_candidate_wallets.py"


def test_copy_candidate_wallets(tmp_path):
    db = tmp_path / "ledger.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE memecoin_wallets(source_user_id TEXT, handle TEXT,
                paused INTEGER, excluded INTEGER, real_approved INTEGER, real_chains TEXT);
            CREATE TABLE memecoin_events(source_user_id TEXT, network_id TEXT, mint TEXT,
                side TEXT, created_at TEXT, is_first_buy INTEGER);
            CREATE TABLE memecoin_positions(id INTEGER PRIMARY KEY, source_user_id TEXT,
                network_id TEXT, mint TEXT, execution_mode TEXT, strategy TEXT, is_open INTEGER,
                opened_at TEXT, opened_recorded_at TEXT, closed_at TEXT, realized_pnl REAL,
                entry_fill_source TEXT, exit_reason TEXT, last_quote_price REAL);
            CREATE TABLE memecoin_fills(position_id INTEGER, kind TEXT,
                fill_source TEXT, qty REAL, notional REAL);
        """)
        conn.executemany("INSERT INTO memecoin_wallets VALUES (?,?,?,?,?,?)", [
            ("a", "SharedHandle", 0, 0, 1, "solana"),
            ("b", "SharedHandle", 0, 0, 0, None),
            ("missing", "MissingPnl", 0, 0, 0, None),
            ("no-oct", "NoOctober", 0, 0, 0, None),
            ("repeat", "OnlyAddon", 0, 0, 0, None),
            ("paused", "Paused", 1, 0, 0, None),
            ("excluded", "Excluded", 0, 1, 0, None),
            ("empty", "Empty", 0, 0, 1, "solana"),
            ("loss", "Loss", 0, 0, 0, None),
        ])
        positions = [
            (1, "a", "solana", "m1", "real", "control", 0, "2026-10-01T00:00:00Z", "2026-10-01T00:00:30Z", "2026-10-01T00:02:30Z", 10, "chain", "real_trail", 1),
            (2, "a", "solana", "m2", "real", "control", 0, "2026-10-02T00:00:00Z", None, "2026-10-02T00:04:00Z", -2, "chain", "real_stop", 1),
            (3, "a", "solana", "paper", "paper", "alpha_b", 0, "2026-10-02T00:00:00Z", None, "2026-10-02T00:04:00Z", 900, "dexscreener", "b_tp", 1),
            (4, "b", "robin", "m3", "real", "control", 0, "2026-10-01T00:00:00Z", None, "2026-10-01T00:01:00Z", 12, "chain", "real_trail", 1),
            (5, "missing", "solana", "m4", "real", "control", 0, "2026-10-01T00:00:00Z", None, "2026-10-01T00:01:00Z", None, "chain", "real_trail", 1),
            (6, "missing", "solana", "m5", "real", "control", 0, "2026-10-01T00:00:00Z", None, "2026-10-01T00:01:00Z", 50, "chain", "real_trail", 1),
            (7, "no-oct", "solana", "m6", "real", "control", 0, "2026-09-30T23:59:59Z", None, "2026-10-01T00:01:00Z", 40, "chain", "real_trail", 1),
            (8, "repeat", "solana", "m7", "real", "control", 0, "2026-10-01T00:00:00Z", None, "2026-10-01T00:01:00Z", 30, "chain", "real_trail", 1),
            (9, "paused", "solana", "m8", "real", "control", 0, "2026-10-01T00:00:00Z", None, "2026-10-01T00:01:00Z", 999, "chain", "real_trail", 1),
            (10, "excluded", "solana", "m9", "real", "control", 0, "2026-10-01T00:00:00Z", None, "2026-10-01T00:01:00Z", 999, "chain", "real_trail", 1),
            (11, "a", "solana", "older", "real", "control", 0, "2026-08-01T00:00:00Z", None, "2026-08-14T23:59:59Z", 999, "chain", "real_trail", 1),
            (12, "a", "solana", "undated", "real", "control", 0, "2026-10-01T00:00:00Z", None, None, None, "chain", "real_trail", 1),
            (13, "loss", "solana", "lost", "real", "control", 0, "2026-10-01T00:00:00Z", None, "2026-10-01T00:01:00Z", -1, "chain", "real_stop", 1),
        ]
        conn.executemany("INSERT INTO memecoin_positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", positions)
        conn.executemany("INSERT INTO memecoin_fills VALUES (?,?,?,?,?)", [
            (3, "entry", "dexscreener", 100, 25),
            (3, "bracket_exit", "dexscreener", 100, 925),
        ])
        conn.executemany("INSERT INTO memecoin_events VALUES (?,?,?,?,?,0)", [
            ("a", "solana", "first", "buy", "2026-10-20T12:00:00Z"),
            ("a", "solana", "first", "buy", "2026-10-26T13:00:00Z"),
            ("a", "solana", "new", "buy", "2026-10-26T13:12:45Z"),
            ("b", "4663", "b-first", "buy", "2026-10-22T12:00:00Z"),
            ("missing", "solana", "missing-first", "buy", "2026-10-20T12:00:00Z"),
            ("no-oct", "solana", "no-oct-first", "buy", "2026-10-20T12:00:00Z"),
            ("repeat", "solana", "addon", "buy", "2026-09-01T12:00:00Z"),
            ("repeat", "solana", "addon", "buy", "2026-10-20T12:00:00Z"),
            ("a", "solana", "sell-only", "sell", "2026-10-26T13:00:00Z"),
            ("a", "evm-unknown", "unknown-chain", "buy", "2026-10-26T13:00:00Z"),
        ])
    assert SCRIPT.is_file(), "copy candidate report has not been implemented"
    spec = importlib.util.spec_from_file_location("copy_candidate_wallets", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    before = db.read_bytes()
    result = mod.candidates(db, "2026-08-15T00:00:00Z")
    rows = {(r["uid"], r["chain"]): r for r in result["rows"]}
    a = rows[("a", "solana")]
    assert (a["n"], a["pnl_known"], a["wins"], a["known_n"]) == (2, 8, 1, 2)
    assert (a["oct_n"], a["oct_pnl_known"], a["hold_median_s"]) == (2, 8, 180)
    assert (a["first_observed_buy_n"], a["pre_gap_n"], a["resumed_n"]) == (2, 1, 1)
    assert a["armed"] and a["screen"] and a["low_n"]
    assert (a["paper_n"], a["paper_pnl"]) == (1, 900)
    assert rows[("b", "4663")]["screen"] and not rows[("b", "4663")]["armed"]
    assert rows[("missing", "solana")]["unknown_n"] == 1
    assert not rows[("missing", "solana")]["screen"]
    assert rows[("no-oct", "solana")]["oct_n"] == 0
    assert not rows[("no-oct", "solana")]["screen"]
    assert rows[("repeat", "solana")]["first_observed_buy_n"] == 0
    assert not rows[("repeat", "solana")]["screen"]
    assert not any(uid in ("paused", "excluded") for uid, _ in rows)
    sol = [r["uid"] for r in result["rows"] if r["chain"] == "solana"]
    assert sol[0] == "a"
    assert sol.index("loss") < sol.index("empty")
    assert result["undated_real_closes"] == 1
    assert result["dropped_evm_unknown_buy_rows"] == 1
    output = mod.render(result)
    assert "Activity window solana: 2026-10-16T06:55:45+00:00 .. 2026-10-26T13:12:45+00:00" in output
    assert all(term in output for term in ("LOW_N", "first_observed_buy", "not watched", "2026-10-23", "2026-10-26", "no funded authority"))
    assert "Undated real closes (all ledger, excluded from dated cohort): 1" in output
    assert "Dropped evm-unknown buy event rows (all retained history): 1" in output
    assert "booked realized_pnl" in output and "backfills" in output
    assert db.read_bytes() == before
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE memecoin_fills")
    unavailable = mod.candidates(db, "2026-08-15T00:00:00Z")
    assert unavailable["paper_status"] == "unavailable"
    assert all(r["paper_n"] is None for r in unavailable["rows"])
