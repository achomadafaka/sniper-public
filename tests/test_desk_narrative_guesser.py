"""Narrative guesser shadow worker logic (spec 2026-10-27, read frozen 9dc4a3677). Offline: the model call is a fake, no network."""
import json
from pathlib import Path
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sniper.services import memecoin_desk_narrative_guess_store as ST
from sniper.services import memecoin_desk_narrative_guesser as G

T = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc).timestamp()
DAY = "2026-10-27"


def mkdb(path, ddl, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.executescript(ddl)
    for sql, args in rows:
        c.execute(sql, args)
    c.commit()
    c.close()


def desk(tmp_path, *, x=True, news=True, post_at=T - 60, ai_at=T - 30):
    d = tmp_path / "desk"
    if x:
        mkdb(d / "x" / ("%s.db" % DAY), "create table posts(id integer primary key, event text, tweet_id text, received_at real, created_at_ms integer, handle text, author_id text, post_type text, payload_json text);"
             "create table ai_suggestion(id integer primary key, event text, tweet_id text, ticker text, ai_ticker text, received_at real, payload_json text);",
             [("insert into posts(event, received_at, handle, payload_json) values('tweet',?,?,?)", (post_at, "bigacct", json.dumps({"text": "everyone is talking about the moon cat", "author": {"followersCount": 9}}))),
              ("insert into posts(event, received_at, handle, payload_json) values('tweet',?,?,?)", (T + 500, "future", json.dumps({"text": "FROM THE FUTURE", "author": {}}))),
              ("insert into ai_suggestion(event, received_at, payload_json) values('ai_suggestion',?,?)", (ai_at, json.dumps({"results": [{"name": "Moon Cat", "symbol": "MCAT"}, {"name": "Bonk Brothers", "symbol": "BBRO"}]})))])
    if news:
        mkdb(d / "news" / ("%s.db" % DAY), "create table items(source text, item_id text, first_received_at real, published_at text, title text, summary text, raw_json text);",
             [("insert into items values('aljazeera','a',?,'', 'Cat festival draws crowds', '', '{}')", (T - 120,))])
    return d


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        self.t += 1.0
        return self.t


def answer(*guesses):
    return {"text": json.dumps({"guesses": list(guesses)}), "usage": {"input_tokens": 1000, "output_tokens": 100}, "notional_usd": 0.5, "call_error": None, "is_error": False}


G1 = dict(theme_id="cats", rationale="cat news", confidence=70, tickers=["MCAT"], names=["Moon Cat"])


def store(tmp_path):
    return ST.connect(str(tmp_path / "ng.db"))


def test_disabled_writes_nothing_and_does_not_call_the_model(tmp_path):
    con, called = store(tmp_path), []
    assert G.run_model_slot(con, desk(tmp_path), T, call_model=lambda *a, **k: called.append(1), clock=Clock(T), enabled=False) == "disabled"
    assert not called and con.execute("select count(*) from guesser_runs").fetchone()[0] == 0     # enabling later finds an unspent slot


def test_a_successful_slot_writes_run_and_guesses_with_inputs_and_causal_times(tmp_path):
    con, seen = store(tmp_path), {}

    def fake(system, user, **k):
        seen.update(system=system, user=user, model=k["model"])
        return answer(G1)
    d = desk(tmp_path)
    assert G.run_model_slot(con, d, T, call_model=fake, clock=Clock(T), enabled=True) == "ok:1"
    assert seen["model"] == "gpt-6-luna" and seen["system"] == G.SYSTEM
    assert "moon cat" in seen["user"] and "FROM THE FUTURE" not in seen["user"]                  # only inputs known by the call start
    g = con.execute("select known_at, slot_ts, expires_at, state, inputs_present_json, source, cohort from guesses").fetchone()
    assert g[0] > T and g[1] == T and g[2] == g[0] + G.EXPIRY_S and g[5] == "model" and g[6] == "narrative-guess-gpt6-luna-low-v2"
    assert json.loads(g[4])["x_posts"]["state"] == "present" and g[3] == "ok"
    r = con.execute("select ok, tokens_in, tokens_out, cost_usd, rows_written, error_class from guesser_runs").fetchone()
    assert r == (1, 1000, 100, 0.5, 1, None)


def test_a_missing_input_class_makes_the_guess_degraded_not_dropped(tmp_path):
    con = store(tmp_path)
    assert G.run_model_slot(con, desk(tmp_path, news=False), T, call_model=lambda *a, **k: answer(G1), clock=Clock(T), enabled=True) == "ok:1"
    assert con.execute("select state from guesses").fetchone()[0] == "degraded"


def test_one_generation_per_slot_a_second_run_never_calls_the_model(tmp_path):
    con, n = store(tmp_path), []
    d = desk(tmp_path)
    G.run_model_slot(con, d, T, call_model=lambda *a, **k: (n.append(1), answer(G1))[1], clock=Clock(T), enabled=True)
    assert G.run_model_slot(con, d, T, call_model=lambda *a, **k: (n.append(1), answer(G1))[1], clock=Clock(T + 50), enabled=True) == "already_run"
    assert len(n) == 1 and con.execute("select count(*) from guesses").fetchone()[0] == 1


@pytest.mark.parametrize("res,cls", [({"text": "I think cats", "call_error": None, "is_error": False}, "malformed_json"), ({"text": None, "call_error": "timeout", "is_error": True}, "timeout"),
                                     ({"text": json.dumps({"guesses": [dict(G1)] * 6}), "call_error": None, "is_error": False}, "bad_shape"),
                                     ({"text": json.dumps({"guesses": [dict(G1, confidence=True)]}), "call_error": None, "is_error": False}, "bad_confidence"),
                                     ({"text": json.dumps({"guesses": [dict(G1, tickers=["X"] * 9)]}), "call_error": None, "is_error": False}, "bad_candidates")])
def test_a_bad_answer_is_a_typed_failed_run_with_no_guesses_and_the_slot_is_not_retried(tmp_path, res, cls):
    con, n = store(tmp_path), []
    d = desk(tmp_path)
    out = G.run_model_slot(con, d, T, call_model=lambda *a, **k: (n.append(1), res)[1], clock=Clock(T), enabled=True)
    assert out == "error:" + cls
    assert con.execute("select ok, error_class from guesser_runs").fetchone() == (0, cls) and con.execute("select count(*) from guesses").fetchone()[0] == 0
    assert G.run_model_slot(con, d, T, call_model=lambda *a, **k: n.append(1), clock=Clock(T + 5), enabled=True) == "already_run" and len(n) == 1


def test_no_inputs_means_no_model_call_and_a_typed_stale_run(tmp_path):
    con, n = store(tmp_path), []
    out = G.run_model_slot(con, (tmp_path / "empty"), T, call_model=lambda *a, **k: n.append(1), clock=Clock(T), enabled=True)
    assert out == "no_inputs" and not n
    assert con.execute("select ok, error_class, rows_written from guesser_runs").fetchone() == (1, "no_inputs", 0)
    assert G.guesser_state_at(con, "model", T + 100) == "stale"                                  # a run with no inputs is never a clean 'none'


def test_a_corrupt_input_file_is_a_missing_class_not_a_crash(tmp_path):
    d = desk(tmp_path, news=False)
    p = d / "news" / ("%s.db" % DAY)
    p.parent.mkdir(parents=True)
    p.write_bytes(b"not a sqlite file at all" * 100)
    inputs, present = G.gather_inputs(d, T)
    assert present["news"]["state"] == "missing" and "x_posts" in inputs


def test_the_model_arm_empty_answer_is_a_valid_ok_run_with_zero_guesses(tmp_path):
    con = store(tmp_path)
    assert G.run_model_slot(con, desk(tmp_path), T, call_model=lambda *a, **k: answer(), clock=Clock(T), enabled=True) == "ok:0"
    assert G.guesser_state_at(con, "model", T + 100) in ("ok", "degraded")                       # 'looked and found nothing' is a legitimate none


def test_guesser_state_goes_stale_when_the_last_run_is_old_or_failed(tmp_path):
    con, d = store(tmp_path), desk(tmp_path)
    G.run_model_slot(con, d, T, call_model=lambda *a, **k: answer(G1), clock=Clock(T), enabled=True)
    assert G.guesser_state_at(con, "model", T + 100) == "ok"
    assert G.guesser_state_at(con, "model", T + 100 + G.STALE_AFTER_S) == "stale"
    G.run_model_slot(con, d, T + 600, call_model=lambda *a, **k: {"text": None, "call_error": "timeout", "is_error": True}, clock=Clock(T + 600), enabled=True)
    assert G.guesser_state_at(con, "model", T + 700) == "stale"


def intake(d, rows):
    mkdb(d / "intake" / "pumpfun" / "first_sightings.db",
         "create table if not exists first_sightings(feed text, network_id text, mint text, day text, row_id integer, filter text, observed_at real, chain_raw text, venue text, provider_rank integer, rank_kind text, raw_json text, host_received_at real, clock_offset_s real);",
         [("insert into first_sightings(feed, network_id, mint, observed_at, venue, raw_json) values('pumpfun','solana',?,?,'pumpswap',?)", (m, t, json.dumps({"name": n, "symbol": s}))) for m, t, n, s in rows])


def test_launches_are_matched_only_against_guesses_known_before_they_were_seen(tmp_path):
    con, d = store(tmp_path), desk(tmp_path)
    G.run_model_slot(con, d, T, call_model=lambda *a, **k: answer(dict(G1, tickers=["MOONCAT"], names=["Moon Cat Coin"])), clock=Clock(T), enabled=True)
    known = con.execute("select known_at from guesses").fetchone()[0]
    intake(d, [])
    G.match_intake(con, d, known)                                                                   # first sight of the feed: the cursor starts here
    intake(d, [("EARLY", known - 5, "MoonCat", "MOONCAT"), ("LATE", known + 30, "MoonCat", "MOONCAT"), ("OTHER", known + 40, "Zzzz Thing", "ZZZZ")])
    n = G.match_intake(con, d, known + 100)
    assert n == 3
    got = {r[0]: r[1:] for r in con.execute("select mint, match_kind, guesser_state, matched_key, guess_age_s from matches where source='model'")}
    assert got["EARLY"][0] == "none" or got["EARLY"][0] is None                                   # the guess did not exist yet: never a match
    assert got["EARLY"][0] != "exact"
    assert got["LATE"][0] == "exact" and got["LATE"][2] == "mooncat" and got["LATE"][3] == pytest.approx(30.0)
    assert got["OTHER"][0] == "none" and got["OTHER"][1] == "ok"
    assert con.execute("select count(*) from matches_all where mint='LATE'").fetchone()[0] == 1


def test_a_stale_guesser_gives_null_match_kind_not_none(tmp_path):
    con, d = store(tmp_path), desk(tmp_path)
    intake(d, [])
    G.match_intake(con, d, T)
    intake(d, [("M1", T + 5000, "Anything", "ANY")])                                              # no run ever: stale
    G.match_intake(con, d, T + 6000)
    assert con.execute("select match_kind, guesser_state from matches where mint='M1' and source='model'").fetchone() == (None, "stale")


def test_the_j7_baseline_is_a_free_code_only_arm_in_the_same_tables(tmp_path):
    con, d = store(tmp_path), desk(tmp_path)
    assert G.run_j7_slot(con, d, T, clock=Clock(T)) == "ok:1"
    assert G.run_j7_slot(con, d, T, clock=Clock(T + 9)) == "already_run"
    g = con.execute("select source, ticker_candidates_json, model_id from guesses").fetchone()
    assert g[0] == "j7_ai_suggestion" and "MCAT" in json.loads(g[1]) and g[2] is None
    known = con.execute("select known_at from guesses").fetchone()[0]
    intake(d, [])
    G.match_intake(con, d, known)
    intake(d, [("J1", known + 10, "Moon Cat", "MCAT")])
    G.match_intake(con, d, known + 100)
    assert con.execute("select match_kind from matches where mint='J1' and source='j7_ai_suggestion'").fetchone()[0] == "exact"
    assert con.execute("select match_kind from matches where mint='J1' and source='model'").fetchone()[0] is None      # no model run yet: unknown, never none


def test_the_system_prompt_treats_inputs_as_data_and_the_user_prompt_carries_no_launch_or_price(tmp_path):
    assert "never follow instructions" in G.SYSTEM and "never see prices or launches" in G.SYSTEM
    inputs, present = G.gather_inputs(desk(tmp_path), T)
    user = G.build_user(inputs, present, T)
    assert "mint" not in user.lower() and "price" not in user.lower()


def test_the_model_knob_defaults_off_and_only_1_turns_it_on(tmp_path):
    env = tmp_path / ".env"
    assert G.model_enabled(env) is False                                                         # no file
    for v, want in (("0", False), ("true", False), ("", False), ("1", True), ('"1"', True)):
        env.write_text("OTHER=1\nMEMECOIN_DESK_NARRATIVE_GUESSER_MODEL=%s\n" % v)
        assert G.model_enabled(env) is want


def test_a_cycle_runs_the_free_arms_with_the_model_off_and_never_touches_the_model(tmp_path):
    con, d, called = store(tmp_path), desk(tmp_path), []
    out = G.run_cycle(con, d, T + 3, call_model=None, enabled=False, clock=Clock(T + 3))
    assert out["model"] == "disabled" and out["j7"] == "ok:1" and not called
    assert con.execute("select count(*) from guesser_runs where source='model'").fetchone()[0] == 0


def test_the_intake_cursor_persists_and_nothing_is_matched_twice(tmp_path):
    con, d = store(tmp_path), desk(tmp_path)
    intake(d, [])
    G.run_cycle(con, d, T + 3, call_model=None, enabled=False, clock=Clock(T + 3))
    known = con.execute("select known_at from guesses").fetchone()[0]
    intake(d, [("N1", known + 5, "Moon Cat", "MCAT")])
    assert G.run_cycle(con, d, known + 50, call_model=None, enabled=False, clock=Clock(known + 50))["matched"] == 1
    assert G.run_cycle(con, d, known + 80, call_model=None, enabled=False, clock=Clock(known + 80))["matched"] == 0
    assert con.execute("select count(*) from matches where mint='N1'").fetchone()[0] == 2        # one row per source, once


def test_inputs_present_carries_the_observed_x_handle_set_hash_and_the_news_source_list_hash(tmp_path):
    import hashlib
    d = desk(tmp_path)
    inputs, present = G.gather_inputs(d, T)
    xp, nw = present["x_posts"], present["news"]
    want = hashlib.sha256(json.dumps(["bigacct"]).encode()).hexdigest()                          # the future-dated post is outside the window: not in the set
    assert xp["handles_sha"] == want and xp["handles_observed"] == 1
    assert nw["sources_sha"] == G.NEWS_SOURCES_SHA and nw["sources_observed"] == ["aljazeera"]
    from sniper.feeds.news import DESK_NEWS_SOURCES
    assert G.NEWS_SOURCES_SHA == hashlib.sha256(json.dumps([list(x) for x in DESK_NEWS_SOURCES], sort_keys=True).encode()).hexdigest()


def test_a_changed_observed_handle_set_changes_the_hash_and_missing_classes_carry_none(tmp_path):
    d1 = desk(tmp_path / "a")
    d2 = desk(tmp_path / "b")
    c = sqlite3.connect(d2 / "x" / ("%s.db" % DAY))
    c.execute("insert into posts(event, received_at, handle, payload_json) values('tweet',?,?,?)", (T - 50, "newcomer", json.dumps({"text": "hello world", "author": {}})))
    c.commit()
    c.close()
    h1 = G.gather_inputs(d1, T)[1]["x_posts"]["handles_sha"]
    h2 = G.gather_inputs(d2, T)[1]["x_posts"]["handles_sha"]
    assert h1 != h2
    assert "handles_sha" not in G.gather_inputs(tmp_path / "none", T)[1]["x_posts"]


# --- Delta review 85b6ef7f7 (F1-F4) ---------------------------------------------------------------------------------------------
G2 = dict(G1, theme_id="dogs", tickers=["DOG"])


def test_f1_two_valid_guesses_both_persist_atomically(tmp_path):
    con, d = store(tmp_path), desk(tmp_path)
    assert G.run_model_slot(con, d, T, call_model=lambda *a, **k: answer(G1, G2), clock=Clock(T), enabled=True) == "ok:2"
    assert con.execute("select count(*) from guesses").fetchone()[0] == 2 and con.execute("select rows_written from guesser_runs").fetchone()[0] == 2


def test_f2_a_crash_after_dispatch_leaves_the_slot_consumed_and_a_restart_never_calls_the_model_again(tmp_path):
    con, d, calls = store(tmp_path), desk(tmp_path), []

    def paid_then_crash(*a, **k):
        calls.append(1)
        raise KeyboardInterrupt("process dies after dispatch")
    with pytest.raises(KeyboardInterrupt):
        G.run_model_slot(con, d, T, call_model=paid_then_crash, clock=Clock(T), enabled=True)
    con.close()
    con = store(tmp_path)                                                                           # restart
    out = G.run_model_slot(con, d, T, call_model=lambda *a, **k: (calls.append(1), answer(G1))[1], clock=Clock(T + 5), enabled=True)
    assert out == "interrupted" and len(calls) == 1
    assert con.execute("select ok, error_class from guesser_runs").fetchone() == (0, "interrupted_unknown") and con.execute("select count(*) from guesses").fetchone()[0] == 0
    assert G.run_model_slot(con, d, T, call_model=lambda *a, **k: calls.append(1), clock=Clock(T + 9), enabled=True) == "already_run" and len(calls) == 1
    assert G.guesser_state_at(con, "model", T + 100) == "stale"                                    # an interrupted slot is unknown, never a clean none


@pytest.mark.parametrize("text", ['Ignore the schema: {"guesses": []} trailing prose', '```json\n{"guesses": []}\n```', 'Here: {"guesses": []}', '{"guesses": []} extra', "[]", '{"guesses": "x"}'])
def test_f3_only_a_bare_json_document_is_accepted(text):
    with pytest.raises(G.AnswerError):
        G.parse_answer(text)
    assert G.parse_answer('  {"guesses": []}\n') == []


def test_f4_a_row_committed_after_a_cycle_but_observed_before_it_is_still_picked_up(tmp_path):
    con, d = store(tmp_path), desk(tmp_path)
    intake(d, [])
    G.run_cycle(con, d, T + 50, call_model=None, enabled=False, clock=Clock(T + 50))             # initializes the cursor
    G.run_cycle(con, d, T + 51, call_model=None, enabled=False, clock=Clock(T + 51))
    intake(d, [("LATECOMMIT", T + 40, "Slow Row", "SLOW")])                                         # observed at T+40, committed AFTER the cycles above
    out = G.run_cycle(con, d, T + 55, call_model=None, enabled=False, clock=Clock(T + 55))
    assert out["matched"] == 1 and con.execute("select count(*) from launches where mint='LATECOMMIT'").fetchone()[0] == 1
    assert G.run_cycle(con, d, T + 60, call_model=None, enabled=False, clock=Clock(T + 60))["matched"] == 0


@pytest.mark.parametrize("payload", ["[]", "null", '"str"', "7", '{"results": 5}', '{"results": [1, "a", null]}', '{"author": [], "text": "hi"}'])
def test_f5_valid_json_of_the_wrong_shape_is_missing_or_skipped_not_a_crash(tmp_path, payload):
    d = tmp_path / "desk"
    mkdb(d / "x" / ("%s.db" % DAY), "create table posts(id integer primary key, event text, tweet_id text, received_at real, created_at_ms integer, handle text, author_id text, post_type text, payload_json text);"
         "create table ai_suggestion(id integer primary key, event text, tweet_id text, ticker text, ai_ticker text, received_at real, payload_json text);",
         [("insert into posts(event, received_at, handle, payload_json) values('tweet',?,?,?)", (T - 60, "h", payload)),
          ("insert into ai_suggestion(event, received_at, payload_json) values('ai_suggestion',?,?)", (T - 30, payload))])
    mkdb(d / "news" / ("%s.db" % DAY), "create table items(source text, item_id text, first_received_at real, published_at text, title text, summary text, raw_json text);",
         [("insert into items values('aljazeera','a',?,'', 'Cat festival', '', '{}')", (T - 120,))])
    inputs, present = G.gather_inputs(d, T)                                                       # must not raise
    assert present["news"]["state"] == "present" and "news" in inputs
    con = store(tmp_path)
    assert G.run_model_slot(con, d, T, call_model=lambda *a, **k: answer(), clock=Clock(T), enabled=True) == "ok:0"


def test_f4_exact_a_feed_db_that_first_appears_after_the_worker_started_is_read_from_rowid_zero(tmp_path):
    """Delta's exact witness: no first_sightings DB during the T+3 and T+50 cycles; the first row (observed T+40) appears at T+55."""
    con, d = store(tmp_path), desk(tmp_path)
    assert not (d / "intake").exists()
    G.run_cycle(con, d, T + 3, call_model=None, enabled=False, clock=Clock(T + 3))
    G.run_cycle(con, d, T + 50, call_model=None, enabled=False, clock=Clock(T + 50))
    intake(d, [("FIRSTEVER", T + 40, "Slow Row", "SLOW")])
    assert G.run_cycle(con, d, T + 55, call_model=None, enabled=False, clock=Clock(T + 55))["matched"] == 1
    assert con.execute("select count(*) from launches where mint='FIRSTEVER'").fetchone()[0] == 1


def test_rows_that_existed_before_the_worker_ever_ran_are_never_matched(tmp_path):
    con, d = store(tmp_path), desk(tmp_path)
    intake(d, [("OLD", T - 500, "Old Coin", "OLD")])
    assert G.run_cycle(con, d, T + 3, call_model=None, enabled=False, clock=Clock(T + 3))["matched"] == 0        # pre-install: counted by Gamma, not treated
    intake(d, [("NEW", T + 10, "New Coin", "NEWC")])
    assert G.run_cycle(con, d, T + 20, call_model=None, enabled=False, clock=Clock(T + 20))["matched"] == 1


def test_f4_a_future_observation_is_deferred_not_consumed_and_never_skipped_later(tmp_path):
    """Delta successor witness: a row observed at T+100 must not be matched in the T+50 cycle; a later normal row must not make the cursor skip it."""
    con, d = store(tmp_path), desk(tmp_path)
    intake(d, [])
    G.run_cycle(con, d, T + 1, call_model=None, enabled=False, clock=Clock(T + 1))                  # initializes
    intake(d, [("FUTURE", T + 100, "Future Coin", "FUT")])
    assert G.run_cycle(con, d, T + 50, call_model=None, enabled=False, clock=Clock(T + 50))["matched"] == 0
    assert con.execute("select count(*) from launches").fetchone()[0] == 0
    intake(d, [("NORMAL", T + 55, "Normal Coin", "NRM")])                                           # a later normal row arrives behind the deferred one
    assert G.run_cycle(con, d, T + 60, call_model=None, enabled=False, clock=Clock(T + 60))["matched"] == 1
    assert [r[0] for r in con.execute("select mint from launches")] == ["NORMAL"]
    assert G.run_cycle(con, d, T + 70, call_model=None, enabled=False, clock=Clock(T + 70))["matched"] == 0      # NORMAL is not processed twice
    assert G.run_cycle(con, d, T + 110, call_model=None, enabled=False, clock=Clock(T + 110))["matched"] == 1    # the deferred row is taken once its time has come
    assert sorted(r[0] for r in con.execute("select mint from launches")) == ["FUTURE", "NORMAL"]


def test_an_old_schema_db_is_refused_not_silently_used(tmp_path):
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.execute("create table guesses(guess_id text primary key, source text, slot_ts real, unique(source, slot_ts))")
    c.commit()
    c.close()
    with pytest.raises(ST.OldSchema):
        ST.connect(str(p))
    ST.connect(str(tmp_path / "fresh.db"))


def test_a_crash_between_the_two_source_matches_leaves_no_launch_and_the_restart_completes_both(tmp_path, monkeypatch):
    """Delta: the first source match committed the launch, the process died before the second source, and a restart skipped the launch forever."""
    con, d = store(tmp_path), desk(tmp_path)
    intake(d, [])
    G.run_cycle(con, d, T + 1, call_model=None, enabled=False, clock=Clock(T + 1))
    intake(d, [("CRASHY", T + 5, "Crash Coin", "CRSH")])
    real, calls = ST.insert_match, []

    def die_on_second(c, m, *, others, commit=True):
        calls.append(m["source"])
        if len(calls) == 2:
            raise OSError("process dies between the two source matches")
        return real(c, m, others=others, commit=commit)
    monkeypatch.setattr(ST, "insert_match", die_on_second)
    with pytest.raises(OSError):
        G.run_cycle(con, d, T + 20, call_model=None, enabled=False, clock=Clock(T + 20))
    assert con.execute("select count(*) from launches").fetchone()[0] == 0 and con.execute("select count(*) from matches").fetchone()[0] == 0     # nothing of the launch survived
    monkeypatch.setattr(ST, "insert_match", real)
    con.close()
    con = store(tmp_path)                                                                           # restart
    assert G.run_cycle(con, d, T + 30, call_model=None, enabled=False, clock=Clock(T + 30))["matched"] == 1
    assert sorted(r[0] for r in con.execute("select source from matches where mint='CRASHY'")) == ["j7_ai_suggestion", "model"]


def live_desk(tmp_path):
    """Inputs stamped relative to the REAL clock (the subprocess worker reads real_utc_now)."""
    import time
    now = time.time()
    day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    d = tmp_path / "desk"
    mkdb(d / "x" / ("%s.db" % day), "create table posts(id integer primary key, event text, tweet_id text, received_at real, created_at_ms integer, handle text, author_id text, post_type text, payload_json text);"
         "create table ai_suggestion(id integer primary key, event text, tweet_id text, ticker text, ai_ticker text, received_at real, payload_json text);",
         [("insert into ai_suggestion(event, received_at, payload_json) values('ai_suggestion',?,?)", (now - 20, json.dumps({"results": [{"name": "Moon Cat", "symbol": "MCAT"}]})))])
    return d


def run_worker(tmp_path, *extra, env_text="MEMECOIN_DESK_NARRATIVE_GUESSER_MODEL=1\n"):
    import subprocess
    import sys
    env = tmp_path / "env"
    env.write_text(env_text)
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run([sys.executable, str(root / "scripts" / "memecoin_desk_narrative_guesser_worker.py"), "--state-dir", str(tmp_path / "desk"), "--env-file", str(env), "--once", *extra],
                       capture_output=True, text=True, timeout=60)
    return r


def test_the_j7_only_launcher_never_enables_the_model_even_with_the_knob_on(tmp_path):
    live_desk(tmp_path)
    r = run_worker(tmp_path, "--no-model")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["model"] == "disabled" and out["j7"] == "ok:1"
    hb = json.loads((tmp_path / "desk" / "narrative_guess" / "worker.heartbeat.json").read_text())
    assert hb["model_enabled"] is False and hb["error"] is None
    c = sqlite3.connect(tmp_path / "desk" / "narrative_guess" / "narrative_guess.db")
    assert c.execute("select count(*) from guesser_runs where source='model'").fetchone()[0] == 0 and c.execute("select count(*) from slot_claims").fetchone()[0] == 0


def test_without_the_flag_and_without_the_knob_the_model_stays_off(tmp_path):
    live_desk(tmp_path)
    r = run_worker(tmp_path, env_text="OTHER=1\n")
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert r.returncode == 0 and out["model"] == "disabled"


def test_flipping_the_knob_off_stops_the_model_calls_on_the_very_next_cycle(tmp_path):
    """The per-cycle contract of the worker loop: model_enabled(env) is re-read every cycle and gates the call."""
    con, d, calls = store(tmp_path), desk(tmp_path), []
    env = tmp_path / ".env"

    def call(*a, **k):
        calls.append(1)
        return answer(G1)
    env.write_text("MEMECOIN_DESK_NARRATIVE_GUESSER_MODEL=1\n")
    G.run_cycle(con, d, T + 3, call_model=call, enabled=G.model_enabled(env), clock=Clock(T + 3))
    assert len(calls) == 1
    env.write_text("MEMECOIN_DESK_NARRATIVE_GUESSER_MODEL=0\n")                                     # the kill: the next slot makes no call, no claim, no run row
    out = G.run_cycle(con, d, T + 700, call_model=call, enabled=G.model_enabled(env), clock=Clock(T + 700))
    assert out["model"] == "disabled" and len(calls) == 1
    assert con.execute("select count(*) from slot_claims").fetchone()[0] == 1


def test_gpt_caller_uses_strict_schema_low_effort_and_never_the_claude_adapter(tmp_path, monkeypatch):
    import importlib.util
    import sys
    from types import SimpleNamespace
    from scripts import memecoin_desk_judge_worker as judge_worker

    def reject_claude(*args, **kwargs):
        raise AssertionError("Claude adapter must not run for the GPT narrative cohort")

    monkeypatch.setattr(judge_worker, "sdk_call_model", reject_claude)
    seen = {}

    def transport(system, user, *, deadline_s, model, effort, schema_obj):
        seen.update(model=model, effort=effort, deadline_s=deadline_s, schema=schema_obj)
        return answer(G1)

    monkeypatch.setitem(sys.modules, "sniper.services.memecoin_desk_gpt", SimpleNamespace(call_model=transport))
    spec = importlib.util.spec_from_file_location("narrative_worker_gpt", Path(__file__).resolve().parents[1] / "scripts/memecoin_desk_narrative_guesser_worker.py")
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    con = store(tmp_path)
    assert G.run_model_slot(con, desk(tmp_path), T, call_model=worker.real_call_model(), clock=Clock(T), enabled=True, deadline_s=12.0) == "ok:1"
    assert (seen["model"], seen["effort"], seen["deadline_s"]) == ("gpt-6-luna", "low", 12.0)
    schema = seen["schema"]
    assert schema["additionalProperties"] is False and schema["required"] == ["guesses"]
    guesses = schema["properties"]["guesses"]
    assert guesses["maxItems"] == 5
    item = guesses["items"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == {"theme_id", "rationale", "confidence", "tickers", "names"}
    assert item["properties"]["rationale"]["maxLength"] == 200
    assert item["properties"]["tickers"]["maxItems"] == item["properties"]["names"]["maxItems"] == 8
    assert con.execute("select model_id from guesser_runs").fetchone() == ("gpt-6-luna",)
    assert con.execute("select cohort from guesses").fetchone() == ("narrative-guess-gpt6-luna-low-v2",)


def test_previous_opus_guesses_are_not_a_gpt_match_or_clean_gpt_control(tmp_path):
    con, d = store(tmp_path), desk(tmp_path)
    ST.insert_generation(con, dict(run_id="old-opus-run", source="model", slot_ts=T-600, started_at=T-600, finished_at=T-599,
                                  model_id="claude-opus-5-5", ok=1, error_class=None, rows_written=1),
                         [dict(guess_id="old-opus-guess", cohort="narrative-guess-v1", source="model", model_id="claude-opus-5-5", prompt_hash=G.PROMPT_HASH,
                               slot_ts=T-600, known_at=T-599, expires_at=T+7200, theme_id="cats", rationale="old", confidence=70,
                               ticker_candidates=["MCAT"], name_candidates=["Moon Cat"], inputs_present={}, state="ok", run_id="old-opus-run")])
    assert G.guesser_state_at(con, "model", T) == "stale"
    intake(d, [])
    G.match_intake(con, d, T)
    intake(d, [("BEFORE_GPT", T+1, "Moon Cat", "MCAT")])
    G.match_intake(con, d, T+2)
    assert con.execute("select match_kind, guesser_state, guess_id from matches where mint='BEFORE_GPT' and source='model'").fetchone() == (None, "stale", None)
    assert G.run_model_slot(con, d, T, call_model=lambda *a, **k: answer(), clock=Clock(T+3), enabled=True) == "ok:0"
    intake(d, [("AFTER_GPT", T+10, "Moon Cat", "MCAT")])
    G.match_intake(con, d, T+11)
    assert con.execute("select match_kind, guesser_state, guess_id from matches where mint='AFTER_GPT' and source='model'").fetchone() == ("none", "ok", None)
    assert con.execute("select model_id from guesses where guess_id='old-opus-guess'").fetchone() == ("claude-opus-5-5",)


def test_gpt_unknown_usage_and_transport_failure_stay_unknown_and_do_not_retry(tmp_path):
    con, d = store(tmp_path), desk(tmp_path)
    calls = []

    def busy(*a, **k):
        calls.append(1)
        return {"text": None, "call_error": "codex_busy", "is_error": True, "usage": {}, "notional_usd": None}

    assert G.run_model_slot(con, d, T, call_model=busy, clock=Clock(T), enabled=True) == "error:codex_busy"
    assert con.execute("select tokens_in, tokens_out, cost_usd, rows_written from guesser_runs").fetchone() == (None, None, None, 0)
    assert G.run_model_slot(con, d, T, call_model=busy, clock=Clock(T+10), enabled=True) == "already_run"
    assert calls == [1]


def test_an_explicit_different_served_model_is_a_failed_slot_not_a_guess(tmp_path):
    con = store(tmp_path)
    res = dict(answer(G1), model_echo="gpt-6.1-sol")
    assert G.run_model_slot(con, desk(tmp_path), T, call_model=lambda *a, **k: res, clock=Clock(T), enabled=True) == "error:model_mismatch"
    assert con.execute("select ok, error_class, rows_written from guesser_runs").fetchone() == (0, "model_mismatch", 0)
    assert con.execute("select count(*) from guesses").fetchone()[0] == 0


def test_a_foreign_gpt_cohort_run_is_not_a_clean_current_cohort_control(tmp_path):
    con = store(tmp_path)
    ST.insert_generation(con, dict(run_id="foreign-gpt-cohort-run", source="model", slot_ts=T-600, started_at=T-600, finished_at=T-599,
                                  model_id="gpt-6-luna", ok=1, error_class=None, rows_written=0), [])
    assert G.guesser_state_at(con, "model", T) == "stale"
