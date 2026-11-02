"""Desk trending board (trend-v1) from the U tape: causal windows, ranking, KOTH, NULL USD, coverage."""
import importlib.util
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("desk_trending", ROOT / "scripts" / "memecoin_desk_trending.py")
T = importlib.util.module_from_spec(spec)
spec.loader.exec_module(T)

NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc).timestamp()


def row(mint, age, *, buy=True, sol=1.0, actor="a", vq=30e9, vt=1e15, rt=5, sig=None, slot=1):
    t = NOW - age
    return (slot, sig or "%s-%s-%s" % (mint, age, actor), 0, mint, int(buy), int(sol * 1e9), actor, int(vq), int(vt), rt, t)


def test_ranking_by_15_min_buys_with_causal_windows_and_null_usd():
    rows = [row("A", 100, sol=2), row("A", 100, sol=1, actor="b"), row("B", 600, sol=5), row("B", 1200, sol=50),
            row("C", -5, sol=99), row("D", 200, buy=False, sol=7)]
    ranks, koth, coins = T.board(rows, set(), NOW, 100.0)
    assert [r["mint"] for r in ranks] == ["B", "A", "D"]                # C is after the clock; D sold only
    a = ranks[1]
    assert (a["buy_usd_5m"], a["buy_usd_15m"], a["makers_15m"]) == (300.0, 300.0, 2)
    assert ranks[0]["buy_usd_60m"] == 5500.0 and ranks[0]["buy_usd_15m"] == 500.0
    no_sol = T.board(rows, set(), NOW, None)[0]
    assert no_sol[0]["buy_usd_15m"] is None and no_sol[0]["mcap_usd"] is None      # unknown, never 0
    assert [r["mint"] for r in no_sol] == ["B", "A", "D"]                         # ranking from atomic SOL


def test_koth_is_the_highest_live_curve_coin_traded_in_5_min():
    rows = [row("HI", 100, vq=80e9), row("GRAD", 50, vq=200e9), row("OLD", 400, vq=150e9), row("LO", 10, vq=40e9)]
    koth = T.board(rows, {"GRAD"}, NOW, 100.0)[1]
    assert koth["mint"] == "HI" and round(koth["mcap_usd"]) == 8000    # (80 SOL / 1e9 tokens) x 1e9 supply x $100


def test_coverage_names_a_stale_heartbeat_or_a_gap_row(tmp_path):
    (tmp_path / "universe.heartbeat").write_text(json.dumps({"ts": NOW - 2, "sol_usd": 150.0, "sol_age_s": 30}))
    assert T.coverage(tmp_path, NOW) == ("ok", 150.0)
    (tmp_path / "gaps").mkdir()
    g = sqlite3.connect(tmp_path / "gaps" / "2026-10-21.db")
    g.execute("CREATE TABLE gaps(at REAL, cause TEXT, last_slot INTEGER)")
    g.execute("INSERT INTO gaps VALUES (?, 'worker_start', NULL)", (NOW - 600,))
    g.commit()
    assert T.coverage(tmp_path, NOW)[0] == "gap"
    (tmp_path / "universe.heartbeat").write_text(json.dumps({"ts": NOW - 30, "sol_usd": 150.0, "sol_age_s": 500}))
    assert T.coverage(tmp_path, NOW) == ("gap", None)


def test_tape_reads_incrementally_without_duplicates(tmp_path):
    (tmp_path / "swaps").mkdir()
    db = sqlite3.connect(tmp_path / "swaps" / "2026-10-21.db")
    db.execute("CREATE TABLE swaps(slot INTEGER, signature TEXT, ordinal INTEGER, mint TEXT, is_buy INTEGER, "
               "quote_atomic INTEGER, actor TEXT, virtual_quote INTEGER, virtual_token INTEGER, real_token INTEGER, "
               "received_at REAL)")
    ins = lambda slot, sig, age: db.execute("INSERT INTO swaps VALUES (?,?,0,'A',1,1,'a',1,1,5,?)", (slot, sig, NOW - age))
    ins(1, "old", 7200)
    ins(100, "s1", 50)
    db.commit()
    tape = T.Tape(tmp_path)
    tape.refresh(NOW)
    assert [r[1] for r in tape.rows] == ["s1"]                         # the 2 h old row is outside the hour
    ins(101, "s2", 10)
    ins(99, "late", 20)                                                # a late row inside LATE_SLOTS
    db.commit()
    tape.refresh(NOW)
    assert sorted(r[1] for r in tape.rows) == ["late", "s1", "s2"]
    tape.refresh(NOW + 3600)
    assert list(tape.rows) == []                                       # aged out


def test_curve_kind_labels_standard_and_nonstandard_curves():
    # the 2026-10-21 KOTH: ~1,858 virtual SOL against 925.5 M virtual tokens
    assert T.curve_kind(30 * 10**9, 1_073_000_000 * 10**6) == "standard"
    assert T.curve_kind(60 * 10**9, 536_500_000 * 10**6) == "standard"          # later on the same curve
    assert T.curve_kind(1_857_879_221_525, 925_551_713_740_941) == "nonstandard"
    assert T.curve_kind(0, 5) is None
    ranks, koth, _ = T.board([row("M", 10, vq=1_857_879_221_525, vt=925_551_713_740_941),
                              row("S", 20, vq=60e9, vt=536_500_000e6)], set(), NOW, 100.0)
    assert koth["mint"] == "M" and koth["curve_kind"] == "nonstandard"
    assert {r["mint"]: r["curve_kind"] for r in ranks} == {"M": "nonstandard", "S": "standard"}


def test_coverage_names_a_retired_curve_tape(tmp_path):
    (tmp_path / "universe.heartbeat").write_text(json.dumps({"ts": NOW - 2, "sol_usd": 150.0, "sol_age_s": 30,
                                                             "curve_capture": False}))
    assert T.coverage(tmp_path, NOW) == ("curve_retired", 150.0)
