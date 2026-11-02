"""Narrative guesser store (docs/superpowers/specs/2026-10-27-desk-narrative-guesser-design.md; columns per Gamma's frozen read 9dc4a3677, section 6).

One sqlite file. guesses and guesser_runs are append-only (triggers refuse UPDATE/DELETE); a re-roll is impossible because the one-generation key lives on guesser_runs UNIQUE(source, slot_ts) and the durable slot_claims row (a generation may carry several guess rows).
No outcome, price-after-t0 or label column exists anywhere. match_kind NULL = unknown (stale guesser), 'none' = looked and found nothing: never the same value."""
import json
import sqlite3

DDL = """
create table if not exists guesses(
  guess_id text primary key, cohort text not null, source text not null check(source in ('model','j7_ai_suggestion')), model_id text, prompt_hash text,
  slot_ts real not null, known_at real not null, expires_at real not null, theme_id text, rationale text, confidence integer check(confidence between 0 and 100),
  ticker_candidates_json text not null, name_candidates_json text not null, inputs_present_json text not null, evidence_ids_json text not null,
  state text not null check(state in ('ok','degraded')), run_id text);
create index if not exists guesses_known on guesses(known_at);
create index if not exists guesses_slot on guesses(source, slot_ts);
create table if not exists slot_claims(source text not null, slot_ts real not null, claimed_at real not null, primary key(source, slot_ts));
create table if not exists guesser_runs(
  run_id text primary key, source text not null, slot_ts real not null, started_at real not null, finished_at real, model_id text, ok integer not null check(ok in (0,1)),
  error_class text, tokens_in integer, tokens_out integer, cost_usd real, rows_written integer not null default 0, unique(source, slot_ts));
create table if not exists launches(
  network_id text not null, mint text not null, name text, symbol text, first_seen_known_at real not null, first_swap_slot integer, venue text not null,
  primary key(network_id, mint));
create table if not exists matches(
  network_id text not null, mint text not null, source text not null check(source in ('model','j7_ai_suggestion')), guess_id text,
  match_kind text check(match_kind is null or match_kind in ('exact','fuzzy','theme_only','none')),
  guesser_state text not null check(guesser_state in ('ok','degraded','stale')), matched_key text, edit_distance integer, matcher_version_hash text not null,
  matched_at real not null, guess_age_s real, hard_filter_verdict text not null, filter_version_hash text not null,
  primary key(network_id, mint, source));
create table if not exists matches_all(
  network_id text not null, mint text not null, source text not null, guess_id text not null, match_kind text not null, matched_key text, edit_distance integer,
  primary key(network_id, mint, source, guess_id));
create trigger if not exists guesses_no_update before update on guesses begin select raise(abort, 'guesses are immutable'); end;
create trigger if not exists guesses_no_delete before delete on guesses begin select raise(abort, 'guesses are immutable'); end;
create trigger if not exists runs_no_update before update on guesser_runs begin select raise(abort, 'guesser_runs are immutable'); end;
create trigger if not exists runs_no_delete before delete on guesser_runs begin select raise(abort, 'guesser_runs are immutable'); end;
"""


class OldSchema(Exception):
    """A narrative_guess.db made before 7190feb55 (guesses UNIQUE(source, slot_ts)): it would refuse a multi-guess generation. Use a fresh file or migrate explicitly."""


def connect(path):
    con = sqlite3.connect(path, timeout=30)
    con.execute("pragma journal_mode=wal")
    for _seq, name, unique, *_rest in con.execute("pragma index_list(guesses)").fetchall():
        if unique and [r[2] for r in con.execute("pragma index_info(%s)" % name)] == ["source", "slot_ts"]:
            raise OldSchema(path)
    con.executescript(DDL)
    return con


def _guess_args(g):
    return (g["guess_id"], g["cohort"], g["source"], g.get("model_id"), g.get("prompt_hash"), g["slot_ts"], g["known_at"], g["expires_at"], g.get("theme_id"), g.get("rationale"),
            g.get("confidence"), json.dumps(g.get("ticker_candidates") or []), json.dumps(g.get("name_candidates") or []), json.dumps(g.get("inputs_present") or {}, sort_keys=True),
            json.dumps(g.get("evidence_ids") or []), g["state"], g.get("run_id"))


def insert_guess(con, g):
    con.execute("insert into guesses values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", _guess_args(g))
    con.commit()


def claim_slot(con, source, slot_ts, now):
    """Durable pre-dispatch claim: True only for the caller that created it. A claim without a run row is an interrupted attempt and stays consumed (never re-rolled)."""
    cur = con.execute("insert or ignore into slot_claims values(?,?,?)", (source, slot_ts, now))
    con.commit()
    return cur.rowcount == 1


def insert_generation(con, run, guesses):
    """One generation = one run row plus ALL its guess rows in ONE transaction: either the whole set is stored or none of it."""
    con.execute("begin immediate")
    try:
        con.execute("insert into guesser_runs values(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run["run_id"], run["source"], run["slot_ts"], run["started_at"], run.get("finished_at"), run.get("model_id"), run["ok"], run.get("error_class"), run.get("tokens_in"),
                     run.get("tokens_out"), run.get("cost_usd"), run.get("rows_written", 0)))
        for g in guesses:
            con.execute("insert into guesses values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", _guess_args(g))
    except BaseException:
        con.execute("rollback")
        raise
    con.execute("commit")


def insert_run(con, r):
    con.execute("insert into guesser_runs values(?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["run_id"], r["source"], r["slot_ts"], r["started_at"], r.get("finished_at"), r.get("model_id"), r["ok"], r.get("error_class"), r.get("tokens_in"), r.get("tokens_out"),
                 r.get("cost_usd"), r.get("rows_written", 0)))
    con.commit()


def insert_match(con, m, *, others, commit=True):
    cols = ("network_id", "mint", "source", "guess_id", "match_kind", "guesser_state", "matched_key", "edit_distance", "matcher_version_hash", "matched_at", "guess_age_s",
            "hard_filter_verdict", "filter_version_hash")
    try:
        con.execute("insert into matches values(%s)" % ",".join("?" * len(cols)), [m.get(c) for c in cols])
        for o in others:
            con.execute("insert into matches_all values(?,?,?,?,?,?,?)", (m["network_id"], m["mint"], m["source"], o["guess_id"], o["match_kind"], o.get("matched_key"), o.get("edit_distance")))
    except sqlite3.Error:
        if commit:
            con.rollback()
        raise
    if commit:
        con.commit()
