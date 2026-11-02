"""wallet_signal_profiles + live_positions persistence for the Live Wallet Signals board."""
from __future__ import annotations

import sqlite3

from sniper.persistence import db_wallet_signals as DB


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    DB.ensure_wallet_signals_tables(c)
    return c


def test_open_positions_excludes_persisted_zero_price_rows():
    # CODEX P2: a stale resolved row may already sit in live_positions with cur_price=0, is_open=1
    # (persisted before the redeemable fix, or a wallet not re-polled). open_positions must defensively
    # exclude it so the Signals board never surfaces a worthless resolved bet as a live signal.
    c = _conn()
    W = "0x" + "a" * 40
    DB.upsert_live_position(c, {"wallet": W, "condition_id": "0xlive", "outcome_index": 0,
                                "cur_price": 0.4, "size": 100, "current_value": 40})
    DB.upsert_live_position(c, {"wallet": W, "condition_id": "0xdead", "outcome_index": 0,
                                "cur_price": 0.0, "size": 100, "current_value": 0})
    cids = {p["condition_id"] for p in DB.open_positions(c)}
    assert cids == {"0xlive"}


def test_profile_round_trip():
    c = _conn()
    g = {"wallet": "0x" + "A" * 40, "archetype": "directional_discretionary", "intent": "thesis_specialist",
         "archetype_confidence": "high", "copy_policy": "validated_copy", "directional_profile": True,
         "in_sample_edge_read": "profitable_provable", "edge_read_strength": "high",
         "edge_domains": ["big NBA bets", "soccer"], "primary_tag": "NBA", "q75_usdc": 30000.0,
         "description": "x", "analyzed_ts": 123}
    DB.upsert_profile(c, g, now_ts=999)
    got = DB.all_profiles(c)
    w = ("0x" + "a" * 40)
    assert w in got
    assert got[w]["directional_profile"] is True and got[w]["edge_domains"] == ["big NBA bets", "soccer"]
    assert got[w]["archetype"] == "directional_discretionary" and got[w]["profiled_ts"] == 999


def test_profile_upsert_overwrites():
    c = _conn()
    w = "0x" + "b" * 40
    DB.upsert_profile(c, {"wallet": w, "archetype": "a1", "directional_profile": False})
    DB.upsert_profile(c, {"wallet": w, "archetype": "a2", "directional_profile": True})
    assert DB.all_profiles(c)[w]["archetype"] == "a2" and DB.all_profiles(c)[w]["directional_profile"] is True


def test_live_position_upsert_and_close():
    c = _conn()
    w = "0x" + "c" * 40
    DB.upsert_live_position(c, {"wallet": w, "condition_id": "0x1", "outcome_index": 0,
                                "outcome": "YES", "title": "T", "league": "Basketball",
                                "size": 100, "avg_price": 0.4, "cur_price": 0.5,
                                "current_value": 50, "cash_pnl": 10})
    DB.upsert_live_position(c, {"wallet": w, "condition_id": "0x2", "outcome_index": 1,
                                "cur_price": 0.6, "current_value": 99})
    assert len(DB.open_positions(c)) == 2
    # 0x2 gone from the current open set -> marked closed
    DB.mark_closed(c, w, open_keys={("0x1", 0)})
    open_now = DB.open_positions(c)
    assert len(open_now) == 1 and open_now[0]["condition_id"] == "0x1"


def test_legacy_wallet_grades_migrates_into_profiles():
    # A deployment that predates the rename has a `wallet_grades` table; ensure() copies it across
    # (non-destructively, only while the new table is empty) with the column-rename mapping.
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        """CREATE TABLE wallet_grades (
             wallet TEXT PRIMARY KEY, archetype TEXT, intent TEXT, archetype_confidence TEXT,
             copy_policy TEXT, copyable INTEGER, edge_verdict TEXT, edge_confidence TEXT,
             edge_domains TEXT, primary_tag TEXT, q75_usdc REAL, description TEXT,
             graded_ts INTEGER, analyzed_ts INTEGER);""")
    w = "0x" + "a" * 40
    c.execute("INSERT INTO wallet_grades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (w, "directional_discretionary", "thesis_specialist", "high", "validated_copy", 1,
               "profitable_provable", "high", '["big NBA bets"]', "NBA", 30000.0, "sharp", 5, 9))
    c.commit()
    DB.ensure_wallet_signals_tables(c)
    got = DB.all_profiles(c)
    assert w in got
    assert got[w]["directional_profile"] is True            # copyable -> directional_profile
    assert got[w]["copy_policy"] == "validated_copy"
    assert got[w]["in_sample_edge_read"] == "profitable_provable"  # edge_verdict -> in_sample_edge_read
    assert got[w]["edge_read_strength"] == "high"           # edge_confidence -> edge_read_strength
    assert got[w]["edge_domains"] == ["big NBA bets"] and got[w]["primary_tag"] == "NBA"
    assert got[w]["profiled_ts"] == 5


def test_legacy_migration_skipped_when_new_table_populated():
    # If the new table already has rows (the grader ran post-rename), legacy rows are NOT re-imported.
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    DB.ensure_wallet_signals_tables(c)
    DB.upsert_profile(c, {"wallet": "0x" + "b" * 40, "archetype": "x", "directional_profile": True})
    c.executescript("CREATE TABLE wallet_grades (wallet TEXT PRIMARY KEY, archetype TEXT);")
    c.execute("INSERT INTO wallet_grades VALUES (?, ?)", ("0x" + "a" * 40, "legacy"))
    c.commit()
    DB.ensure_wallet_signals_tables(c)
    assert set(DB.all_profiles(c).keys()) == {"0x" + "b" * 40}  # legacy row not pulled in


def test_prune_profiles_removes_unanalyzed():
    c = _conn()
    for w in ("0xkeep", "0xdrop"):
        DB.upsert_profile(c, {"wallet": w, "archetype": "x", "intent": None, "archetype_confidence": "low",
                              "copy_policy": "study_only", "directional_profile": False,
                              "in_sample_edge_read": "mixed", "edge_read_strength": "low",
                              "edge_domains": [], "primary_tag": None, "q75_usdc": 0.0,
                              "description": "d", "analyzed_ts": 0}, now_ts=1)
    n = DB.prune_profiles(c, keep_wallets={"0xkeep"})
    assert n == 1 and set(DB.all_profiles(c).keys()) == {"0xkeep"}
