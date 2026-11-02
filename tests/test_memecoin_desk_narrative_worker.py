"""Desk narrative worker (code only): tags, theme aggregates, emergent tokens with creators."""
import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("desk_narrative", ROOT / "scripts" / "memecoin_desk_narrative.py")
N = importlib.util.module_from_spec(spec)
spec.loader.exec_module(N)

H = datetime(2026, 10, 21, 12, tzinfo=timezone.utc).timestamp()          # an hour start
DAY = "2026-10-21"


def desk(tmp_path, launches=(), swaps=(), emissions=()):
    d = tmp_path / "desk"
    for sub in ("launches", "swaps", "candidate_emissions"):
        (d / sub).mkdir(parents=True)
    lc = sqlite3.connect(d / "launches" / f"{DAY}.db")
    lc.execute("CREATE TABLE launches(mint TEXT PRIMARY KEY, curve TEXT, creator TEXT, slot INTEGER, signature TEXT, "
               "block_time INTEGER, received_at REAL, symbol TEXT, name TEXT)")
    lc.executemany("INSERT INTO launches (mint, creator, received_at, symbol, name) VALUES (?,?,?,?,?)", launches)
    lc.commit()
    sw = sqlite3.connect(d / "swaps" / f"{DAY}.db")
    sw.execute("CREATE TABLE swaps(slot INTEGER, signature TEXT, ordinal INTEGER, mint TEXT, is_buy INTEGER, "
               "quote_atomic INTEGER, actor TEXT, virtual_quote INTEGER, virtual_token INTEGER, real_token INTEGER, "
               "received_at REAL)")
    sw.executemany("INSERT INTO swaps VALUES (?,?,0,?,1,?,?,1,1,5,?)", swaps)
    sw.commit()
    (d / "candidate_emissions" / f"{DAY}.jsonl").write_text(
        "".join(json.dumps({"mint": m, "snapshot": {"emission_at": t}}) + "\n" for m, t in emissions))
    (d / "universe.heartbeat").write_text(json.dumps({"ts": H + 3600, "sol_usd": 100.0, "sol_age_s": 5}))
    return d


def test_name_tokens_drop_stopwords_short_and_digits_and_count_once():
    assert N.name_tokens("KIMCHI", "kimchi coin the kimchi 2 ai") == ["kimchi"]
    assert N.name_tokens("ＤＯＧ", "Good Dog") == ["dog", "good"]


def test_tags_are_ingested_once_from_a_cursor(tmp_path):
    d = desk(tmp_path, launches=[("m1", "c1", H + 10, "CAT", "Cat"), ("m2", "c2", H + 20, "X", "Kimchi")])
    store = N.Store(tmp_path / "out")
    (tmp_path / "out").mkdir()
    assert N.ingest_tags(store, d, H + 30) == 2 and N.ingest_tags(store, d, H + 40) == 0
    rows = store.db(H + 40).execute("SELECT mint, theme, matched FROM tags ORDER BY mint").fetchall()
    assert rows == [("m1", "animal_cat", "cat"), ("m2", "food", "kimchi")]
    store.close()


def test_theme_aggregates_windows_band_entries_buys_and_slope(tmp_path):
    now = H + 3600
    launches = [("c%d" % i, "x", now - 100, "CAT", "cat") for i in range(3)] + [("o1", "x", now - 2000, "CAT", "cat")]
    d = desk(tmp_path, launches=launches, emissions=[("c0", now - 50)])
    store = N.Store(tmp_path / "out")
    (tmp_path / "out").mkdir()
    N.ingest_tags(store, d, now)
    tape = [(1, "s", 0, "c1", 1, 2 * 10**9, "buyer", 1, 1, 5, now - 30), (1, "t", 0, "c2", 1, 10**9, "buyer", 1, 1, 5, now - 30)]
    rows = {(r[0], r[1]): r for r in N.theme_aggregates(tmp_path / "out", d, tape, now, 100.0)}
    assert rows[("animal_cat", 300)][2:6] == (3, 1, 300.0, 1)               # launches, band, buy USD, buyers
    assert rows[("animal_cat", 3600)][2] == 4
    assert rows[("animal_cat", 900)][6] == (3 / 15) / (4 / 60)             # slope
    assert rows[("food", 900)][6] is None                                  # no launches in 60 min: no slope
    assert N.theme_aggregates(tmp_path / "out", d, tape, now, None)[0][4] is None   # no SOL/USD: USD unknown
    store.close()


def test_token_hour_counts_launches_and_distinct_creators_against_the_previous_hour(tmp_path):
    launches = [("f%d" % i, "farm", H + i, "ONBOARD", "onboard") for i in range(5)]
    launches += [("k%d" % i, "c%d" % i, H + 100 + i, "KIM", "kimchi") for i in range(3)]
    launches += [("p1", "z", H - 100, "KIM", "kimchi")]
    d = desk(tmp_path, launches=launches)
    words, tokens = N.token_hour(tmp_path / "out", d, H)
    assert tokens["onboard"] == (5, 1) and tokens["kimchi"] == (3, 3) and words["kimchi"] == (3, 3)
    store = N.Store(tmp_path / "out")
    (tmp_path / "out").mkdir()
    N.write_token_hour(store, tmp_path / "out", d, H, H + 3600)
    row = store.db(H + 3600).execute("SELECT launches, creators, prev_hour_launches FROM token_hourly "
                                     "WHERE kind='name_token' AND token='kimchi'").fetchone()
    assert row == (3, 3, 1)
    store.close()


def test_no_launch_files_at_cold_start_keeps_worker_alive_and_recovers(tmp_path):
    d=tmp_path/'desk';d.mkdir();out=tmp_path/'out';out.mkdir();store=N.Store(out)
    try:
        assert N.ingest_tags(store,d,H+30)==0
        assert store.db(H+30).execute('SELECT COUNT(*) FROM tags').fetchone()[0]==0
        desk(tmp_path,launches=[('late','creator',H+20,'CAT','Cat')])
        assert N.ingest_tags(store,d,H+40)==1
        assert N.ingest_tags(store,d,H+50)==0
    finally:store.close()


def test_missing_inputs_rotate_output_at_utc_midnight_with_retired_coverage(tmp_path):
    d=tmp_path/'desk';d.mkdir();out=tmp_path/'out';out.mkdir();store=N.Store(out)
    tomorrow=H+86400
    (d/'universe.heartbeat').write_text(json.dumps({'ts':tomorrow,'curve_capture':False,'sol_usd':100,'sol_age_s':1}))
    try:
        store.db(H)
        assert N.ingest_tags(store,d,tomorrow)==0
        assert store.day==N.utc_day(tomorrow)
        result=N.run_pass(store,d,N.T.Tape(d),tomorrow,{})
        assert result=={'tagged':0,'coverage':'curve_retired'}
        assert store.db(tomorrow).execute('SELECT DISTINCT coverage FROM theme_agg').fetchall()==[('curve_retired',)]
        assert not (d/'launches').exists() and not (d/'swaps').exists()
    finally:store.close()
