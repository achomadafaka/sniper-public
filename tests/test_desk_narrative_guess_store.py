"""Narrative guesser store (spec 2026-10-27, Gamma's frozen read 9dc4a3677 section 6): immutable guesses, one generation per (source, slot), no outcome columns."""
import sqlite3

import pytest

from sniper.services import memecoin_desk_narrative_guess_store as S


def con(tmp_path):
    c = S.connect(str(tmp_path / "narrative_guess.db"))
    return c


def guess(**k):
    base = dict(guess_id="g1", cohort="narrative-guess-v1", source="model", model_id="opus-5.5", prompt_hash="ph", slot_ts=1000.0, known_at=1000.0, expires_at=8200.0,
                theme_id="animals", rationale="r", confidence=70, ticker_candidates=["bonk"], name_candidates=["Bonk"], inputs_present={"x": "missing"}, evidence_ids=[1], state="ok", run_id="r1")
    base.update(k)
    return base


def test_one_generation_per_source_and_slot_a_reroll_is_refused(tmp_path):
    c = con(tmp_path)
    run = dict(run_id="r1", source="model", slot_ts=1000.0, started_at=1000.0, finished_at=1004.0, model_id="opus-5.5", ok=1, error_class=None, rows_written=2)
    S.insert_generation(c, run, [guess(guess_id="g1"), guess(guess_id="g2")])                  # one generation may carry several guesses
    with pytest.raises(sqlite3.IntegrityError):
        S.insert_generation(c, dict(run, run_id="r2"), [guess(guess_id="g3")])                  # a second run for the same (source, slot): no re-roll
    assert c.execute("select count(*) from guesses").fetchone()[0] == 2 and c.execute("select count(*) from guesser_runs").fetchone()[0] == 1
    assert S.claim_slot(c, "model", 2000.0, 1.0) is True and S.claim_slot(c, "model", 2000.0, 2.0) is False
    S.insert_guess(c, guess(guess_id="g4", source="j7_ai_suggestion"))                           # another source, same slot: fine


def test_a_generation_is_all_or_nothing(tmp_path):
    c = con(tmp_path)
    run = dict(run_id="r1", source="model", slot_ts=1000.0, started_at=1000.0, finished_at=1004.0, model_id="m", ok=1, rows_written=2)
    with pytest.raises(sqlite3.IntegrityError):
        S.insert_generation(c, run, [guess(guess_id="g1"), guess(guess_id="g1")])               # the second row collides: the run row and the first guess roll back too
    assert c.execute("select count(*) from guesser_runs").fetchone()[0] == 0 and c.execute("select count(*) from guesses").fetchone()[0] == 0


def test_rows_are_immutable(tmp_path):
    c = con(tmp_path)
    S.insert_guess(c, guess())
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("update guesses set confidence=99")
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("delete from guesses")
    S.insert_run(c, dict(run_id="r1", slot_ts=1000.0, started_at=1000.0, finished_at=1004.0, model_id="opus-5.5", ok=1, error_class=None, tokens_in=10, tokens_out=5, cost_usd=0.01, rows_written=1, source="model"))
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("update guesser_runs set ok=0")


def test_no_outcome_columns_anywhere(tmp_path):
    c = con(tmp_path)
    names = {r[1].lower() for t in ("guesses", "guesser_runs", "launches", "matches", "matches_all") for r in c.execute("pragma table_info(%s)" % t)}
    assert names and not [n for n in names if any(w in n for w in ("outcome", "label", "pnl", "price_after", "peak", "return", "mcap"))]


def test_match_row_per_launch_and_source_best_in_matches_all_the_rest(tmp_path):
    c = con(tmp_path)
    m = dict(network_id="solana", mint="M1", source="model", guess_id="g1", match_kind="exact", guesser_state="ok", matched_key="bonk", edit_distance=0, matcher_version_hash="h",
             matched_at=2000.0, guess_age_s=1000.0, hard_filter_verdict="pass", filter_version_hash="fh")
    S.insert_match(c, m, others=[dict(guess_id="g1", match_kind="exact", matched_key="bonk", edit_distance=0), dict(guess_id="g9", match_kind="fuzzy", matched_key="bonkk", edit_distance=1)])
    with pytest.raises(sqlite3.IntegrityError):
        S.insert_match(c, m, others=[])                               # one row per (launch, source)
    assert c.execute("select count(*) from matches_all where mint='M1'").fetchone()[0] == 2
    S.insert_match(c, {**m, "source": "j7_ai_suggestion"}, others=[])  # J7 goes through the same table


def test_none_and_null_are_different_values(tmp_path):
    c = con(tmp_path)
    base = dict(network_id="solana", source="model", guess_id=None, guesser_state="ok", matched_key=None, edit_distance=None, matcher_version_hash="h", matched_at=1.0, guess_age_s=None,
                hard_filter_verdict="not_replayable", filter_version_hash="fh")
    S.insert_match(c, {**base, "mint": "A", "match_kind": "none"}, others=[])
    S.insert_match(c, {**base, "mint": "B", "match_kind": None, "guesser_state": "stale"}, others=[])
    assert [r[0] for r in c.execute("select match_kind from matches order by mint")] == ["none", None]
    with pytest.raises(sqlite3.IntegrityError):
        S.insert_match(c, {**base, "mint": "C", "match_kind": "bogus"}, others=[])
