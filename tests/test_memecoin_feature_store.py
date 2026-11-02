"""Tests for the memecoin signal feature-store builder.

The builder materializes `memecoin_signal_features` (one row per copy-signal) joining
signal-time features to forward-return labels already collected. Pure read-derived;
rebuildable. These tests pin band derivation, label thresholds, consensus, and the
shadow-twin outcome join against a small fixture DB.
"""
import sqlite3

import pytest

from scripts.research.memecoin_feature_store import (
    build_feature_store,
    mcap_band,
    size_band,
)


def _fixture_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE memecoin_signal_outcomes(
            fomo_event_id TEXT, source_user_id TEXT, handle TEXT, mint TEXT, ticker TEXT,
            buy_ts TEXT, ref_price REAL, their_size_usd REAL, marks_json TEXT,
            max_gain_pct REAL, peak_horizon_s REAL, last_price REAL, n_marks INT,
            status TEXT, network_id TEXT);
        CREATE TABLE memecoin_events(
            fomo_event_id TEXT, source_user_id TEXT, mint TEXT, their_price REAL,
            their_market_cap REAL, their_size_usd REAL, is_first_buy INT, lag_s REAL,
            chase_gap_pct REAL, network_id TEXT);
        CREATE TABLE memecoin_firehose(
            fomo_event_id TEXT, was_curated INT, was_real_approved INT, is_first_buy INT);
        CREATE TABLE memecoin_positions(
            mint TEXT, source_user_id TEXT, strategy TEXT, execution_mode TEXT,
            is_open INT, realized_pnl REAL, exit_reason TEXT, opened_at TEXT);
        """
    )
    # e1: winner, small-mcap, first buy, curated
    c.execute("INSERT INTO memecoin_signal_outcomes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              ("e1", "u1", "alice", "MINTA", "AAA", "2026-07-12T10:00:00+00:00", 0.001, 2000.0,
               "{}", 61.0, 300.0, 0.0016, 4, "matured", "solana"))
    c.execute("INSERT INTO memecoin_events VALUES(?,?,?,?,?,?,?,?,?,?)",
              ("e1", "u1", "MINTA", 0.001, 500000.0, 2000.0, 1, 3.5, 5.0, "solana"))
    c.execute("INSERT INTO memecoin_firehose VALUES(?,?,?,?)", ("e1", 1, 1, 1))
    # shadow twin for e1 -> real_tp win
    c.execute("INSERT INTO memecoin_positions VALUES(?,?,?,?,?,?,?,?)",
              ("MINTA", "u1", "real_shadow", "paper", 0, 66.0, "real_tp", "2026-07-12T10:00:01+00:00"))

    # e2: loser, big-mcap, not first buy, big size
    c.execute("INSERT INTO memecoin_signal_outcomes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              ("e2", "u2", "bob", "MINTB", "BBB", "2026-07-12T11:00:00+00:00", 5.0, 25000.0,
               "{}", 8.0, 120.0, 4.6, 3, "matured", "solana"))
    c.execute("INSERT INTO memecoin_events VALUES(?,?,?,?,?,?,?,?,?,?)",
              ("e2", "u2", "MINTB", 5.0, 45000000.0, 25000.0, 0, 12.0, 40.0, "solana"))
    # no firehose row for e2 (43% coverage reality) -> curated flags NULL

    # e3: same mint as e1, 30 min later, different buyer -> consensus for a later e4
    c.execute("INSERT INTO memecoin_signal_outcomes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              ("e3", "u3", "carol", "MINTA", "AAA", "2026-07-12T10:30:00+00:00", 0.0012, 1500.0,
               "{}", 25.0, 200.0, 0.0015, 2, "matured", "solana"))
    c.execute("INSERT INTO memecoin_events VALUES(?,?,?,?,?,?,?,?,?,?)",
              ("e3", "u3", "MINTA", 0.0012, 700000.0, 1500.0, 0, 4.0, 20.0, "solana"))

    # e4: still-open label (max_gain_pct NULL) -> hit flags must be NULL, not 0
    c.execute("INSERT INTO memecoin_signal_outcomes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              ("e4", "u4", "dave", "MINTC", "CCC", "2026-07-12T12:00:00+00:00", 0.02, 800.0,
               "{}", None, None, None, 0, "open", "4663"))
    c.execute("INSERT INTO memecoin_events VALUES(?,?,?,?,?,?,?,?,?,?)",
              ("e4", "u4", "MINTC", 0.02, 2000000.0, 800.0, 1, 6.0, 10.0, "4663"))

    # e5: open but already above +20% (max_gain is monotonic) -> hit20=1 is final,
    #     hit50 still unknown (below 50 and not matured)
    c.execute("INSERT INTO memecoin_signal_outcomes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              ("e5", "u5", "erin", "MINTD", "DDD", "2026-07-12T13:00:00+00:00", 0.05, 1200.0,
               "{}", 30.0, 90.0, 0.065, 2, "open", "solana"))
    c.execute("INSERT INTO memecoin_events VALUES(?,?,?,?,?,?,?,?,?,?)",
              ("e5", "u5", "MINTD", 0.05, 900000.0, 1200.0, 1, 5.0, 8.0, "solana"))

    # e6: glitch-level forward gain (quote corruption, cf. the real 491,552% bad_quote row) ->
    #     must be treated as UNLABELED (max_gain + hit flags NULL), never averaged / counted as a hit.
    c.execute("INSERT INTO memecoin_signal_outcomes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              ("e6", "u6", "frank", "MINTE", "EEE", "2026-07-12T14:00:00+00:00", 0.001, 3000.0,
               "{}", 491552.9, 30.0, 5.0, 3, "matured", "solana"))
    c.execute("INSERT INTO memecoin_events VALUES(?,?,?,?,?,?,?,?,?,?)",
              ("e6", "u6", "MINTE", 0.001, 400_000.0, 3000.0, 1, 5.0, 8.0, "solana"))
    c.commit()
    return c


def test_mcap_band_boundaries():
    assert mcap_band(250_000) == "<300k"
    assert mcap_band(500_000) == "300k-1M"
    assert mcap_band(1_000_000) == "1M-5M"
    assert mcap_band(5_000_000) == "5M-30M"
    assert mcap_band(30_000_000) == "30M+"
    assert mcap_band(None) == "unknown"


def test_size_band_boundaries():
    assert size_band(500) == "<1k"
    assert size_band(1000) == "1k-5k"
    assert size_band(5000) == "5k-20k"
    assert size_band(20000) == "20k+"
    assert size_band(None) == "unknown"


def test_build_populates_features_and_labels():
    c = _fixture_db()
    n = build_feature_store(c)
    assert n == 6  # all six signals materialized
    rows = {r["fomo_event_id"]: r for r in
            c.execute("SELECT * FROM memecoin_signal_features").fetchall()}
    e1 = rows["e1"]
    assert e1["mcap_band"] == "300k-1M"
    assert e1["size_band"] == "1k-5k"
    assert e1["is_first_buy"] == 1
    assert e1["hit20"] == 1 and e1["hit50"] == 1   # max_gain 61%
    assert e1["was_curated"] == 1                   # from firehose
    assert e1["firehose_captured"] == 1
    assert e1["shadow_realized_pnl"] == 66.0        # twin join (real_shadow/paper only)
    assert e1["shadow_exit_reason"] == "real_tp"


def test_big_mcap_loser_flags():
    c = _fixture_db()
    build_feature_store(c)
    e2 = c.execute("SELECT * FROM memecoin_signal_features WHERE fomo_event_id='e2'").fetchone()
    assert e2["mcap_band"] == "30M+"
    assert e2["size_band"] == "20k+"
    assert e2["hit20"] == 0 and e2["hit50"] == 0    # max_gain 8%, matured
    assert e2["was_curated"] is None                # no firehose row -> NULL not 0
    assert e2["firehose_captured"] == 0             # explicit "not observed by firehose"


def test_monotonic_hit_flag_final_even_when_open():
    c = _fixture_db()
    build_feature_store(c)
    e5 = c.execute("SELECT * FROM memecoin_signal_features WHERE fomo_event_id='e5'").fetchone()
    # open but max_gain 30% -> hit20 already true (monotonic), hit50 still unknown
    assert e5["hit20"] == 1
    assert e5["hit50"] is None
    assert e5["label_status"] == "open"


def test_open_label_leaves_hit_flags_null():
    c = _fixture_db()
    build_feature_store(c)
    e4 = c.execute("SELECT * FROM memecoin_signal_features WHERE fomo_event_id='e4'").fetchone()
    # unlabeled (still open) -> distinguish unknown from a real 0
    assert e4["hit20"] is None
    assert e4["hit50"] is None
    assert e4["label_status"] == "open"


def test_consensus_60m_counts_prior_distinct_buyers_same_mint():
    c = _fixture_db()
    build_feature_store(c)
    # e1 is the first MINTA buy -> 0 prior buyers
    e1 = c.execute("SELECT consensus_60m FROM memecoin_signal_features WHERE fomo_event_id='e1'").fetchone()
    assert e1["consensus_60m"] == 0
    # e3 is MINTA 30min after e1 -> 1 prior distinct buyer (u1)
    e3 = c.execute("SELECT consensus_60m FROM memecoin_signal_features WHERE fomo_event_id='e3'").fetchone()
    assert e3["consensus_60m"] == 1


def test_rebuild_is_idempotent():
    c = _fixture_db()
    build_feature_store(c)
    build_feature_store(c)  # second run must not duplicate
    n = c.execute("SELECT COUNT(*) FROM memecoin_signal_features").fetchone()[0]
    assert n == 6


def test_feature_scan_runs_both_modes(capsys):
    from scripts.research.memecoin_feature_scan import run_bands, run_rank
    c = _fixture_db()
    build_feature_store(c)
    run_bands(c, min_n=1)
    run_rank(c, limit=5, min_n=1)
    out = capsys.readouterr().out
    assert "mcap_band" in out          # bands mode emitted
    assert "p(hit20)" in out           # rank mode emitted
    assert "base rate" in out or "base hit20" in out


def test_glitch_gain_row_is_nulled_not_averaged():
    c = _fixture_db()
    build_feature_store(c)
    e6 = c.execute("SELECT * FROM memecoin_signal_features WHERE fomo_event_id='e6'").fetchone()
    # corrupt 491,552% gain -> unlabeled: NULL gain + NULL hits (must not read as a hit)
    assert e6["max_gain_pct"] is None
    assert e6["hit20"] is None and e6["hit50"] is None
    # and it must not pollute an aggregate: max over the store excludes it
    mx = c.execute("SELECT MAX(max_gain_pct) FROM memecoin_signal_features").fetchone()[0]
    assert mx < 1000


# --- v2 label fallback: 4663 was invisible to every analysis (2026-09-03) --------------------
# `memecoin_signal_features` sources its labels from `memecoin_signal_outcomes` (v1). On chain
# 4663 v1 holds 64,528 rows with ZERO marks — `n_marks=0`, `marks_json={}`, `max_gain_pct=NULL` —
# and matures 59,522 of them anyway. So `max_gain_pct` is populated on 99% of solana rows and 0%
# of 4663 rows, and every band/hit-rate analysis silently ran on solana only while the gate it
# was analysing fires 87% on 4663.
#
# The marks exist. `memecoin_horizon_marks_v2` holds 359,196 marks over 59,872 4663 outcomes at
# 6 horizons each, ahead of solana's 276,780. Verified before writing this: v2.upstream_event_id
# joins v1.fomo_event_id 1:1 with no duplicates, v2.ref_price is present on 100% of both chains,
# and recomputing v1's solana rows from v2 reproduces them EXACTLY (5.62/5.62, 4.38/4.38,
# -0.68/-0.68). Same metric, not a different scale.
#
# v1 wins wherever it has marks, so no existing number moves. `label_source` records which
# produced each row, because a mixed column with nothing saying which is the defect this fix
# exists to remove.


def _v2_fixture():
    """The v1 fixture plus v2 outcome/horizon tables, and a 4663 row v1 never marked."""
    c = _fixture_db()
    c.executescript(
        """
        CREATE TABLE memecoin_signal_outcomes_v2(
            outcome_id TEXT PRIMARY KEY, upstream_event_id TEXT, network_id TEXT,
            mint TEXT, ref_price REAL, status TEXT);
        CREATE TABLE memecoin_horizon_marks_v2(
            id INTEGER PRIMARY KEY AUTOINCREMENT, outcome_id TEXT, target_s INTEGER,
            price REAL, status TEXT);
        """
    )
    # e4 is the 4663 row v1 left unmarked (max_gain NULL, n_marks 0, status 'open').
    # v2 saw it: ref 0.02, peak 0.026 at 900s = +30%. One horizon still pending -> NOT resolved.
    c.execute("INSERT INTO memecoin_signal_outcomes_v2 VALUES(?,?,?,?,?,?)",
              ("o4", "e4", "4663", "MINTC", 0.02, "partial"))
    for tgt, px in ((60, 0.021), (300, 0.0225), (900, 0.026), (3600, None)):
        c.execute("INSERT INTO memecoin_horizon_marks_v2(outcome_id,target_s,price,status) "
                  "VALUES(?,?,?,?)", ("o4", tgt, px, "observed" if px else "pending"))
    # e1 ALSO has a v2 outcome, with a deliberately different gain (+99% vs v1's +61%). v1 has
    # marks for e1, so v1 must win and this value must never appear. Without a competing row the
    # precedence rule is untestable -- a mutant that always preferred v2 passed every test.
    c.execute("INSERT INTO memecoin_signal_outcomes_v2 VALUES(?,?,?,?,?,?)",
              ("o1", "e1", "solana", "MINTA", 0.001, "complete"))
    for tgt, px in ((60, 0.00199), (300, 0.00199)):
        c.execute("INSERT INTO memecoin_horizon_marks_v2(outcome_id,target_s,price,status) "
                  "VALUES(?,?,?,?)", ("o1", tgt, px, "observed"))
    # e7: v1 unmarked AND every v2 horizon still pending -> no observation anywhere. Must stay
    # unlabeled; a fully-unobserved outcome read as 0% would invent a resolved miss.
    c.execute("INSERT INTO memecoin_signal_outcomes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              ("e7", "u7", "grace", "MINTF", "FFF", "2026-07-12T15:00:00+00:00", 0.004, 900.0,
               "{}", None, None, None, 0, "open", "4663"))
    c.execute("INSERT INTO memecoin_events VALUES(?,?,?,?,?,?,?,?,?,?)",
              ("e7", "u7", "MINTF", 0.004, 300000.0, 900.0, 1, 4.0, 6.0, "4663"))
    c.execute("INSERT INTO memecoin_signal_outcomes_v2 VALUES(?,?,?,?,?,?)",
              ("o7", "e7", "4663", "MINTF", 0.004, "open"))
    for tgt in (60, 300, 900):
        c.execute("INSERT INTO memecoin_horizon_marks_v2(outcome_id,target_s,price,status) "
                  "VALUES(?,?,?,?)", ("o7", tgt, None, "pending"))
    c.commit()
    return c


def _row(conn, ev):
    return dict(conn.execute(
        "SELECT * FROM memecoin_signal_features WHERE fomo_event_id=?", (ev,)).fetchone())


def test_v1_labels_win_wherever_v1_actually_has_marks():
    """No existing number may move. e1 has v1 marks; v2 must not touch it."""
    c = _v2_fixture()
    build_feature_store(c)
    r = _row(c, "e1")
    assert r["max_gain_pct"] == pytest.approx(61.0), "v1 had marks; v2's +99% must not win"
    assert r["label_source"] == "v1"
    assert r["n_marks"] == 4 and r["peak_horizon_s"] == pytest.approx(300.0)


def test_a_4663_row_v1_never_marked_gets_its_label_from_v2():
    """The whole defect: this row had max_gain_pct NULL and was invisible to every analysis."""
    c = _v2_fixture()
    build_feature_store(c)
    r = _row(c, "e4")
    assert r["label_source"] == "v2"
    assert r["max_gain_pct"] == pytest.approx(30.0)   # 0.026/0.02 - 1
    assert r["n_marks"] == 3                          # observed only; the pending one is not a mark
    assert r["peak_horizon_s"] == 900                 # the horizon that achieved the max


def test_a_v2_gain_over_the_threshold_still_hits_while_unresolved():
    """max_gain is a running maximum, so a hit is final even with horizons outstanding."""
    c = _v2_fixture()
    build_feature_store(c)
    r = _row(c, "e4")
    assert r["hit20"] == 1
    assert r["hit50"] is None, "50% is not reached and not resolved -> unknown, never 0"


def test_an_unobserved_horizon_can_never_produce_a_resolved_zero():
    """A missing mark could have hidden the peak. Only a fully observed set may say 'did not hit'."""
    c = _v2_fixture()
    c.execute("UPDATE memecoin_horizon_marks_v2 SET price=0.0201 WHERE target_s=900")  # max now +5%
    c.commit()
    build_feature_store(c)
    r = _row(c, "e4")
    assert r["max_gain_pct"] == pytest.approx(12.5)   # 0.0225/0.02 - 1, the 300s mark
    assert r["hit20"] is None, "one horizon is still pending -> a 0 would be a guess"
    assert r["label_status"] != "matured"


def test_a_fully_observed_v2_set_below_threshold_resolves_to_zero():
    c = _v2_fixture()
    c.execute("UPDATE memecoin_horizon_marks_v2 SET price=0.0201 WHERE target_s=900")
    c.execute("UPDATE memecoin_horizon_marks_v2 SET price=0.0202, status='observed' WHERE target_s=3600")
    c.commit()
    build_feature_store(c)
    r = _row(c, "e4")
    assert r["label_status"] == "matured"
    assert r["hit20"] == 0 and r["hit50"] == 0


def test_the_glitch_cap_applies_to_v2_derived_gains_too():
    """The 491,552% bad_quote row is why this cap exists; a v2 route must not bypass it."""
    c = _v2_fixture()
    c.execute("UPDATE memecoin_horizon_marks_v2 SET price=200.0 WHERE target_s=900")  # ~1e6%
    c.commit()
    build_feature_store(c)
    r = _row(c, "e4")
    assert r["max_gain_pct"] is None
    assert r["hit20"] is None and r["hit50"] is None


def test_the_builder_still_works_when_the_v2_tables_do_not_exist():
    """Older databases and the existing fixture have no v2 tables; absence must not raise."""
    c = _fixture_db()
    assert build_feature_store(c) > 0
    assert _row(c, "e1")["label_source"] == "v1"
    assert _row(c, "e4")["max_gain_pct"] is None


def test_an_outcome_with_no_observation_anywhere_stays_unlabeled():
    """Every v2 horizon pending and v1 empty. There is no evidence, so there is no label -- a 0%
    gain here would manufacture a resolved miss out of an absent measurement."""
    c = _v2_fixture()
    build_feature_store(c)
    r = _row(c, "e7")
    assert r["max_gain_pct"] is None
    assert r["hit20"] is None and r["hit50"] is None
    assert r["label_source"] == "v1", "no v2 evidence -> the row was never handed to v2"


def test_build_migrates_a_pre_existing_table_that_lacks_label_source():
    """`_ensure_table` is CREATE TABLE IF NOT EXISTS, so on the LIVE database — where the table
    already exists with the old 28 columns — a new column is never created and the INSERT dies with
    'no such column: label_source'. The builder must add it in place, idempotently."""
    c = _v2_fixture()
    c.execute("DROP TABLE IF EXISTS memecoin_signal_features")
    c.execute(
        """CREATE TABLE memecoin_signal_features(
             fomo_event_id TEXT PRIMARY KEY, source_user_id TEXT, handle TEXT, mint TEXT,
             ticker TEXT, network_id TEXT, buy_ts TEXT, ref_price REAL, their_market_cap REAL,
             their_size_usd REAL, is_first_buy INTEGER, chase_gap_pct REAL, lag_s REAL,
             was_curated INTEGER, was_real_approved INTEGER, firehose_captured INTEGER,
             consensus_60m INTEGER, mcap_band TEXT, size_band TEXT, max_gain_pct REAL,
             peak_horizon_s REAL, n_marks INTEGER, hit20 INTEGER, hit50 INTEGER,
             shadow_realized_pnl REAL, shadow_exit_reason TEXT, label_status TEXT,
             built_at TEXT)""")
    c.commit()
    assert "label_source" not in {r[1] for r in c.execute(
        "PRAGMA table_info(memecoin_signal_features)")}
    assert build_feature_store(c) > 0
    assert _row(c, "e4")["label_source"] == "v2"
    build_feature_store(c)                    # second pass must not re-add the column
