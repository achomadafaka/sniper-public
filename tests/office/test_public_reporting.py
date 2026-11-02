"""scripts/office_public_reporting.py: agent_daily + equity_snapshots rollups from recorded facts only. Offline, temp databases only."""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from sniper.office import ledger as L
from sniper.office import runtime_observations as RO

spec = importlib.util.spec_from_file_location("opr", Path(__file__).resolve().parents[2] / "scripts" / "office_public_reporting.py")
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)

DAY = 86400.0
NOW = 1_800_000_000.0 - (1_800_000_000.0 % 86400.0) + 12 * 3600.0       # 12:00 UTC of some day
TODAY = P.day_of(NOW)
YESTERDAY = P.day_of(NOW - DAY)


def store_indexes(office, ledger):
    """What the STORE OWNER adds (the script creates none): the exact list the script publishes as REQUIRED_INDEXES."""
    o, lg = sqlite3.connect(office), sqlite3.connect(ledger)
    for name in ("turn_runs_created_at",):
        o.execute(P.REQUIRED_INDEXES[name])
    for name in ("decision_receipts_created_at", "decision_receipts_order", "orders_closed_at", "orders_open"):
        lg.execute(P.REQUIRED_INDEXES[name])
    o.commit()
    lg.commit()
    o.close()
    lg.close()


def drop_store_indexes(office, ledger):
    """An Office database from before the store owner added them (open_ledger now creates the ledger ones at init)."""
    o, lg = sqlite3.connect(office), sqlite3.connect(ledger)
    o.execute("DROP INDEX IF EXISTS turn_runs_created_at")
    for name in ("decision_receipts_created_at", "decision_receipts_order", "orders_closed_at", "orders_open"):
        lg.execute("DROP INDEX IF EXISTS " + name)
    o.commit()
    lg.commit()
    o.close()
    lg.close()


def world(tmp_path, calls=True, fx=True, indexes=True, **kw):
    office, ledger = tmp_path / "office.db", tmp_path / "ledger.db"
    o = sqlite3.connect(office)
    o.execute("CREATE TABLE turn_runs(id TEXT NOT NULL PRIMARY KEY, payload_json TEXT NOT NULL)")
    if calls:
        RO.ensure_schema(o)
    o.commit()
    o.close()
    lg = L.open_ledger(ledger)
    L.open_epoch(lg, epoch_id="E1", clip_usd=30.0, rule_version="r1", actor="t", note="t", now=NOW - 7 * DAY)
    L.open_book(lg, book_id="A", epoch_id="E1", kind="evaluated", actor="t", note="t", now=NOW - 7 * DAY)
    L.fund(lg, book_id="A", usd=1000.0, kind="initial", actor="t", note="t", now=NOW - 7 * DAY)
    lg.close()
    if indexes:
        store_indexes(office, ledger)
    else:
        drop_store_indexes(office, ledger)
    desk = tmp_path / "desk"
    desk.mkdir()
    if fx:
        (desk / "universe.heartbeat").write_text(json.dumps({"ts": NOW - 5.0, "sol_usd": 100.0, "sol_age_s": 3.0}))
    return office, ledger, desk


def turn(office, tid, actor, gen, state, at):
    c = sqlite3.connect(office)
    c.execute("INSERT INTO turn_runs VALUES(?,?)", (tid, json.dumps({"actor_id": actor, "generation": gen, "state": state, "created_at": at})))
    c.commit()
    c.close()


def call(office, cid, actor, gen, at, outcome="ok", cost=None):
    c = sqlite3.connect(office)
    c.execute("INSERT INTO model_calls(call_id, actor_id, generation, turn_id, requested_provider, requested_model, started_at, ended_at, outcome, usage_state, cost_basis, cost_usd) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (cid, actor, gen, "t", "p", "m", at - 1, at, outcome, "reported", "list", cost))
    c.commit()
    c.close()


def handoff(office, eid, at, frm, to):
    c = sqlite3.connect(office)
    c.execute("INSERT INTO handoff_events(event_id, at, from_actor_id, to_actor_id, from_record_kind, from_record_id, to_record_kind, to_record_id, kind, public_summary) "
              "VALUES(?,?,?,?,?,?,?,?,?,?)", (eid, at, frm, to, "memo", "m1", "decision", "d1", "memo_to_decision", "s"))
    c.commit()
    c.close()


def order(ledger, oid, state, closed_at=None, spend=30.0, exit_usd=None, costs=0.2, tokens="1000", pool="P"):
    c = sqlite3.connect(ledger)
    c.execute("INSERT INTO orders(order_id, book_id, epoch_id, book_kind, network_id, mint, pool, decision_bundle_hash, policy_json, policy_hash, fill_model_version, state, requested_at, "
              "filled_at, closed_at, spend_usd, exit_usd, costs_usd, tokens_atomic) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (oid, "A", "E1", "evaluated", "solana", "m" + oid, pool, "h", "{}", "ph", "office-fill-v1", state, NOW - 3 * DAY, NOW - 3 * DAY + 60, closed_at, spend, exit_usd, costs, tokens))
    c.commit()
    c.close()


def receipt(ledger, key, actor, order_id, at):
    c = sqlite3.connect(ledger)
    c.execute("INSERT INTO decision_receipts(action_key,decision_id,book_id,bundle_hash,outcome,reason,order_id,risk_version,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (key, "d" + key, "A", "b", "filled", None, order_id, "rv", actor, at))
    c.commit()
    c.close()


def rows(office, day=None):
    c = sqlite3.connect(office)
    c.row_factory = sqlite3.Row
    q = "SELECT * FROM agent_daily" + (" WHERE utc_day=?" if day else "") + " ORDER BY utc_day, actor_id, generation"
    return [dict(r) for r in c.execute(q, (day,) if day else ())]


def consumed(office):
    c = sqlite3.connect(office)
    return dict(c.execute("SELECT table_name, consumed_seq FROM runtime_observation_state").fetchall())


# ---------------------------------------------------------------- equity_snapshots
def test_a_known_book_is_snapshotted_with_the_ledgers_own_equity_and_written_once(tmp_path):
    office, ledger, desk = world(tmp_path)
    order(ledger, "o1", "open")
    c = sqlite3.connect(ledger)
    c.execute("INSERT INTO marks(order_id, at, lamports, source) VALUES('o1', ?, '2000000000', 'exact')", (NOW - 10,))
    c.commit()
    c.close()
    out = P.run_once(office, ledger, desk, now=NOW)
    assert out["equity_rows"] == 1
    ro = L.open_readonly(ledger)
    eq = L.equity(ro, now=NOW, mark_max_age_s=60.0, sol_usd=100.0, sol_at=NOW - 8.0, book_id="A")
    got = [dict(r) for r in ro.execute("SELECT * FROM equity_snapshots")][0]
    assert got["at"] == NOW and got["bucket_key"] == NOW - NOW % P.EQUITY_STEP_S and got["state"] == "known" and got["priced_n"] == 1 and got["unpriced_n"] == 0
    assert got["cash_usd"] == pytest.approx(eq.cash) and got["known_liq_usd"] == pytest.approx(eq.known_liq_lower) and got["equity_usd"] == pytest.approx(eq.cash + eq.known_liq_lower)
    assert got["fx_usd"] == 100.0 and got["fx_at"] == pytest.approx(NOW - 8.0) and json.loads(got["source_heads"])["marks_max_id"] == 1
    assert got["fx_at"] <= got["at"]                                                          # every input is at or before the as-of time: never backdated into the bucket
    assert P.run_once(office, ledger, desk, now=NOW + 60)["equity_rows"] == 0                  # same 5-minute bucket (NOW is on a bucket boundary): written once
    assert P.run_once(office, ledger, desk, now=NOW + 301)["equity_rows"] == 1                 # the next step is a new point


def test_an_unknown_valuation_is_a_row_with_null_equity_never_a_skipped_point_or_a_guess(tmp_path):
    office, ledger, desk = world(tmp_path, fx=False)
    order(ledger, "o1", "open")
    P.run_once(office, ledger, desk, now=NOW)
    got = [dict(r) for r in L.open_readonly(ledger).execute("SELECT * FROM equity_snapshots")]
    assert len(got) == 1 and got[0]["state"] == "unknown" and got[0]["equity_usd"] is None and got[0]["known_liq_usd"] is None
    assert got[0]["unpriced_n"] == 1 and got[0]["priced_n"] == 0 and got[0]["fx_usd"] is None and got[0]["cash_usd"] == pytest.approx(1000.0)


def test_a_stale_fx_is_unknown_and_a_book_without_open_inventory_is_known_cash(tmp_path):
    office, ledger, desk = world(tmp_path)
    (desk / "universe.heartbeat").write_text(json.dumps({"ts": NOW - 500.0, "sol_usd": 100.0, "sol_age_s": 3.0}))
    P.run_once(office, ledger, desk, now=NOW)
    got = [dict(r) for r in L.open_readonly(ledger).execute("SELECT * FROM equity_snapshots")][0]
    assert got["state"] == "known" and got["equity_usd"] == pytest.approx(1000.0) and got["fx_usd"] is None        # nothing open: cash needs no rate, and no rate is invented


@pytest.mark.parametrize("beat", ["{", json.dumps({"ts": NOW, "sol_usd": 0, "sol_age_s": 1}), json.dumps({"ts": NOW, "sol_usd": 100, "sol_age_s": 999}),
                                  json.dumps({"ts": NOW + 100, "sol_usd": 100, "sol_age_s": 1}), json.dumps({"sol_usd": 100})])
def test_the_fx_reader_refuses_malformed_future_or_stale_heartbeats(tmp_path, beat):
    (tmp_path / "universe.heartbeat").write_text(beat)
    assert P.read_fx(tmp_path, NOW) == (None, None)
    assert P.read_fx(tmp_path / "nowhere", NOW) == (None, None)


# ---------------------------------------------------------------- agent_daily
def test_the_daily_row_counts_turns_calls_cost_handoffs_and_the_ledger_facts_without_inventing_any(tmp_path):
    office, ledger, desk = world(tmp_path)
    t = NOW - 3600
    turn(office, "t1", "scout1", 2, "completed", t)
    turn(office, "t2", "scout1", 2, "partial", t + 1)
    turn(office, "t3", "scout1", 2, "failed", t + 2)
    turn(office, "t4", "judge1", 1, "completed", t + 3)
    call(office, "c1", "scout1", 2, t, cost=0.5)
    call(office, "c2", "scout1", 2, t + 1, cost=None)                           # unknown cost stays unknown
    call(office, "c3", "scout1", 2, t + 2, outcome="timeout", cost=0.25)
    handoff(office, "h1", t, "scout1", "judge1")
    order(ledger, "o1", "closed", closed_at=t, spend=30.0, exit_usd=36.0, costs=1.0)       # +5
    order(ledger, "o2", "closed", closed_at=t, spend=30.0, exit_usd=20.0, costs=1.0)       # -11
    order(ledger, "o3", "closed", closed_at=t, spend=None, exit_usd=None)                  # pnl unknown
    order(ledger, "o4", "open")
    for i, (a, o) in enumerate([("judge1", "o1"), ("judge1", "o2"), ("judge1", "o3"), ("judge1", "o4")]):
        receipt(ledger, "k%d" % i, a, o, t + i)
    P.run_once(office, ledger, desk, now=NOW)
    r = {(x["actor_id"], x["generation"]): x for x in rows(office, TODAY) if x["epoch_id"] is None}
    e1 = {x["actor_id"]: x for x in rows(office, TODAY) if x["epoch_id"] == "E1"}
    s = r[("scout1", 2)]
    assert (s["turns_started"], s["turns_completed"], s["turns_partial"]) == (3, 1, 1)
    assert s["calls"] == 3 and s["calls_not_ok"] == 1 and s["cost_unknown_n"] == 1 and s["measured_cost_usd"] == pytest.approx(0.75)
    assert r[("judge1", 1)]["turns_started"] == 1 and r[("judge1", 1)]["calls"] == 0                 # known zero calls that day
    assert r[("scout1", -1)]["handoffs_out"] == 1 and r[("judge1", -1)]["handoffs_in"] == 1
    assert r[("judge1", -1)]["decisions"] is None and r[("judge1", -1)]["open_n"] is None          # the epoch-unknown row holds no ledger fact: those carry E1
    j = e1["judge1"]
    assert j["calls"] is None and j["handoffs_in"] is None                                         # calls and handoffs carry no epoch: never attributed to E1
    assert j["decisions"] == 4 and j["linked_closed_n"] == 3 and j["wins_n"] == 1 and j["pnl_unknown_n"] == 1 and j["open_n"] == 1
    assert j["realized_paper_pnl_usd"] == pytest.approx(5.0 - 11.0) and j["overlap_n"] == 0
    assert j["covered_through"] == NOW and "release_sha" in json.loads(j["source_heads"])
    assert json.loads(r[("scout1", -1)]["source_heads"])["handoff_events_seq"] == 1


def test_without_the_producer_tables_calls_and_cost_are_null_not_zero(tmp_path):
    office, ledger, desk = world(tmp_path, calls=False)
    turn(office, "t1", "scout1", 2, "completed", NOW - 100)
    P.run_once(office, ledger, desk, now=NOW)
    s = rows(office, TODAY)[0]
    assert s["turns_started"] == 1 and s["calls"] is None and s["calls_not_ok"] is None and s["measured_cost_usd"] is None and s["handoffs_out"] is None


def test_a_cost_unknown_for_every_call_leaves_the_measured_total_null(tmp_path):
    office, ledger, desk = world(tmp_path)
    call(office, "c1", "scout1", 2, NOW - 10, cost=None)
    P.run_once(office, ledger, desk, now=NOW)
    s = rows(office, TODAY)[0]
    assert s["calls"] == 1 and s["cost_unknown_n"] == 1 and s["measured_cost_usd"] is None


def test_an_order_claimed_by_two_actors_is_credited_once_and_the_overlap_is_counted(tmp_path):
    office, ledger, desk = world(tmp_path)
    t = NOW - 3600
    order(ledger, "o1", "closed", closed_at=t, spend=30.0, exit_usd=36.0, costs=1.0)
    receipt(ledger, "k1", "judge1", "o1", t)
    receipt(ledger, "k2", "judge2", "o1", t + 1)
    P.run_once(office, ledger, desk, now=NOW)
    r = {x["actor_id"]: x for x in rows(office, TODAY) if x["generation"] == -1}
    assert r["judge1"]["linked_closed_n"] == 1 and r["judge1"]["realized_paper_pnl_usd"] == pytest.approx(5.0)
    assert r["judge2"]["linked_closed_n"] == 0 and r["judge2"]["overlap_n"] == 1 and r["judge2"]["realized_paper_pnl_usd"] == 0.0


def test_a_closed_day_keeps_the_open_count_it_had_when_it_was_today(tmp_path):
    office, ledger, desk = world(tmp_path)
    order(ledger, "o1", "open")
    receipt(ledger, "k1", "judge1", "o1", NOW - 100)
    P.run_once(office, ledger, desk, now=NOW)
    assert rows(office, TODAY)[0]["open_n"] == 1
    call(office, "late", "judge1", 1, NOW - 10)
    P.run_once(office, ledger, desk, now=NOW + DAY + 100)                      # tomorrow: today's row is recomputed as a closed day, open_n unknown NOW, previous value kept
    assert [r for r in rows(office, TODAY) if r["actor_id"] == "judge1" and r["generation"] == -1][0]["open_n"] == 1


# ---------------------------------------------------------------- watermark (Codex's contract)
def test_the_watermark_stops_below_todays_first_row_and_a_late_row_for_a_closed_day_is_recomputed(tmp_path):
    office, ledger, desk = world(tmp_path)
    call(office, "y1", "a", 1, NOW - DAY + 100)          # seq 1, yesterday
    call(office, "y2", "a", 1, NOW - DAY + 200)          # seq 2, yesterday
    call(office, "t1", "a", 1, NOW - 100)                # seq 3, today
    P.run_once(office, ledger, desk, now=NOW)
    assert consumed(office)["model_calls"] == 2          # today's row (seq 3) is never released
    call(office, "late", "a", 1, NOW - DAY + 300)        # seq 4: a late row for the closed day
    P.run_once(office, ledger, desk, now=NOW + 60)
    assert consumed(office)["model_calls"] == 2 and rows(office, YESTERDAY)[0]["calls"] == 3         # recomputed, still held below today's first row
    P.run_once(office, ledger, desk, now=NOW + DAY)      # the next UTC day: today's rows are now closed
    assert consumed(office)["model_calls"] == 4
    assert rows(office, TODAY)[0]["calls"] == 1


def test_the_watermark_never_passes_a_row_this_run_did_not_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "NEW_ROWS_LIMIT", 2)
    office, ledger, desk = world(tmp_path)
    for i in range(5):
        call(office, "y%d" % i, "a", 1, NOW - 2 * DAY + 100 + i)
    P.run_once(office, ledger, desk, now=NOW)
    assert consumed(office)["model_calls"] == 2                                 # scanned seq 1..2 only
    P.run_once(office, ledger, desk, now=NOW + 60)
    assert consumed(office)["model_calls"] == 4
    P.run_once(office, ledger, desk, now=NOW + 120)
    assert consumed(office)["model_calls"] == 5
    assert rows(office, P.day_of(NOW - 2 * DAY + 100))[0]["calls"] == 5


def test_a_row_with_no_end_time_blocks_consumption_at_its_seq(tmp_path):
    office, ledger, desk = world(tmp_path)
    call(office, "y1", "a", 1, NOW - DAY + 100)
    c = sqlite3.connect(office)
    c.execute("INSERT INTO model_calls(call_id, actor_id, generation, turn_id, requested_provider, requested_model, outcome, usage_state, cost_basis) VALUES('open','a',1,'t','p','m','running','unknown','unknown')")
    c.commit()
    c.close()
    call(office, "y3", "a", 1, NOW - DAY + 300)
    P.run_once(office, ledger, desk, now=NOW)
    assert consumed(office)["model_calls"] == 1                                 # held below the in-flight row (seq 2)
    assert rows(office) and all(r["missing_n"] == 1 for r in rows(office))                  # the unplaceable row is counted, not dropped


# ---------------------------------------------------------------- retention and bounded reads
def test_summary_rows_older_than_365_days_are_pruned_and_younger_ones_stay(tmp_path):
    office, ledger, desk = world(tmp_path)
    P.run_once(office, ledger, desk, now=NOW)
    c = sqlite3.connect(office)
    for day in (P.day_of(NOW - 366 * DAY), P.day_of(NOW - 364 * DAY)):
        c.execute("INSERT INTO agent_daily(utc_day, actor_id, generation, source_heads, covered_through, missing_n, written_at) VALUES(?,?,?,?,?,?,?)", (day, "a", 1, "{}", 0, 0, 0))
    c.commit()
    c.close()
    lg = sqlite3.connect(ledger)
    for at in (NOW - 366 * DAY, NOW - 364 * DAY):
        lg.execute("INSERT INTO equity_snapshots VALUES('A',?,?,1,1,1,0,0,NULL,NULL,'{}','known','v',0)", (at, at))
    lg.commit()
    lg.close()
    P.run_once(office, ledger, desk, now=NOW + 60)
    assert [r["utc_day"] for r in rows(office) if r["actor_id"] == "a"] == [P.day_of(NOW - 364 * DAY)]
    assert sqlite3.connect(ledger).execute("SELECT COUNT(*) FROM equity_snapshots WHERE at < ?", (NOW - 365 * DAY,)).fetchone()[0] == 0


def test_every_read_is_index_backed(tmp_path):
    office, ledger, desk = world(tmp_path)
    P.run_once(office, ledger, desk, now=NOW)
    o, lg = sqlite3.connect(office), sqlite3.connect(ledger)
    def plan(conn, sql, args):
        return " | ".join(str(r) for r in conn.execute("EXPLAIN QUERY PLAN " + sql, args))
    for sql, args in ((P.Q_TURNS, (0, 1)), (P.Q_CALLS, (0, 1)), (P.Q_HANDOFFS, (0, 1))):
        text = plan(o, sql, args)
        assert "USING" in text and "INDEX" in text.upper() and "SCAN turn_runs" not in text and "SCAN model_calls" not in text and "SCAN handoff_events" not in text, text
    assert "turn_runs_created_at" in plan(o, P.Q_TURNS, (0, 1)) and "model_calls_ended_at_call_id" in plan(o, P.Q_CALLS, (0, 1))
    for sql, args in ((P.Q_RECEIPTS, (0, 1)), (P.Q_CLOSED, (0, 1)), (P.Q_OPEN, (1, 1))):
        steps = [str(r[3]) for r in lg.execute("EXPLAIN QUERY PLAN " + sql, args)]
        # every access step uses an index; the only SCAN allowed is a walk of the PARTIAL open-orders index (bounded by the number of open orders)
        assert all("USING" in x and "INDEX" in x for x in steps if x.startswith(("SCAN", "SEARCH"))), steps
        assert all("orders_open" in x for x in steps if x.startswith("SCAN")), steps


def test_schema_creation_is_idempotent_additive_and_touches_no_other_table(tmp_path):
    office, ledger, desk = world(tmp_path)
    before = {n for (n,) in sqlite3.connect(office).execute("SELECT name FROM sqlite_master WHERE type='table'")}
    P.run_once(office, ledger, desk, now=NOW)
    P.run_once(office, ledger, desk, now=NOW + 1)
    after = {n for (n,) in sqlite3.connect(office).execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert after - before == {"agent_daily", "agent_daily_late", "funnel_daily", "funnel_refused_token"} and before <= after


def test_the_cli_never_creates_a_database_and_only_one_instance_runs(tmp_path, capsys):
    assert P.main(["--office-db", str(tmp_path / "no.db"), "--ledger-db", str(tmp_path / "no2.db"), "--desk-dir", str(tmp_path)]) == 2
    office, ledger, desk = world(tmp_path)
    desk_db(tmp_path, [])                                    # the desk store the default resolves to: the parent of --desk-dir
    import fcntl
    lock = open(office.parent / "reporting.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert P.main(["--office-db", str(office), "--ledger-db", str(ledger), "--desk-dir", str(desk)]) == 75
    fcntl.flock(lock, fcntl.LOCK_UN)
    lock.close()
    assert P.main(["--office-db", str(office), "--ledger-db", str(ledger), "--desk-dir", str(desk)]) == 0
    assert "agent_daily" in capsys.readouterr().out


def test_without_the_store_indexes_the_sources_are_unknown_and_never_scanned(tmp_path, monkeypatch):
    office, ledger, desk = world(tmp_path, indexes=False)
    turn(office, "t1", "scout1", 2, "completed", NOW - 100)
    call(office, "c1", "scout1", 2, NOW - 90, cost=0.5)
    order(ledger, "o1", "closed", closed_at=NOW - 100, spend=30.0, exit_usd=36.0, costs=1.0)
    receipt(ledger, "k1", "judge1", "o1", NOW - 100)
    seen = []
    real = sqlite3.Connection.execute
    monkeypatch.setattr(P, "Q_TURNS", P.Q_TURNS)
    P.run_once(office, ledger, desk, now=NOW)
    r = rows(office, TODAY)
    heads = json.loads(r[0]["source_heads"])
    assert set(heads["index_missing"]) >= {"turn_runs_created_at", "decision_receipts_created_at", "orders_closed_at", "orders_open"}
    assert all(x["turns_started"] is None and x["decisions"] is None and x["linked_closed_n"] is None for x in r)         # unknown, not 0
    assert [x for x in r if x["actor_id"] == "scout1"][0]["calls"] == 1                                                  # the producer index (Codex) exists, so calls are known


def test_the_script_creates_no_index_on_any_table_it_does_not_own(tmp_path):
    office, ledger, desk = world(tmp_path, indexes=False)
    before_o = {n for (n,) in sqlite3.connect(office).execute("SELECT name FROM sqlite_master WHERE type='index'")}
    before_l = {n for (n,) in sqlite3.connect(ledger).execute("SELECT name FROM sqlite_master WHERE type='index'")}
    P.run_once(office, ledger, desk, now=NOW)
    after_o = {n for (n,) in sqlite3.connect(office).execute("SELECT name FROM sqlite_master WHERE type='index'")}
    after_l = {n for (n,) in sqlite3.connect(ledger).execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert after_o - before_o <= {"agent_daily_actor_day", "agent_daily_key"} and after_l - before_l <= {"equity_snapshots_at_book", "equity_snapshots_bucket"}
    assert set(P.REQUIRED_INDEXES) >= {"turn_runs_created_at", "decision_receipts_created_at", "decision_receipts_order", "orders_closed_at", "orders_open"}


def test_the_equity_write_lock_is_never_held_across_the_equity_computation(tmp_path, monkeypatch):
    office, ledger, desk = world(tmp_path)
    from sniper.office import ledger as Lmod
    real = Lmod.equity
    probes = []

    def spy(conn, **kw):
        other = sqlite3.connect(ledger, timeout=0, isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")                       # would raise 'database is locked' if the rollup held the writer lock
            other.execute("ROLLBACK")
            probes.append("free")
        finally:
            other.close()
        return real(conn, **kw)
    monkeypatch.setattr(Lmod, "equity", spy)
    P.run_once(office, ledger, desk, now=NOW)
    assert probes == ["free"]


def test_the_watermark_queries_are_index_backed_and_never_walk_the_rowid(tmp_path):
    office, ledger, desk = world(tmp_path)
    o = sqlite3.connect(office)
    for table, col in P.RAW.items():
        spec = dict(table=table, col=col, index=P.PRODUCER_INDEX[table])
        for sql, args in ((P.Q_FIRST_TODAY % spec, (0,)), (P.Q_FIRST_NULL % spec, ())):
            text = " | ".join(str(r[3]) for r in o.execute("EXPLAIN QUERY PLAN " + sql, args))
            assert "USING" in text and spec["index"] in text, text


def test_the_store_owners_own_init_provides_every_index_the_script_reads_through(tmp_path):
    from sniper.office import store as S
    o = S.open_store(tmp_path / "o.db")
    lg = L.open_ledger(tmp_path / "l.db")
    have = {n for (n,) in o.execute("SELECT name FROM sqlite_master WHERE type='index'")} | {n for (n,) in lg.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert set(P.REQUIRED_INDEXES) <= have, set(P.REQUIRED_INDEXES) - have


# ---------------------------------------------------------------- Zeta's four reporting cuts (Alpha's rulings)
def test_a_rollup_of_today_excludes_events_dated_after_the_cut_and_covers_exactly_the_cut(tmp_path):
    office, ledger, desk = world(tmp_path)
    call(office, "now1", "a", 1, NOW - 10, cost=1.0)
    call(office, "future", "a", 1, NOW + 100, cost=5.0)                            # dated after the cut
    turn(office, "tf", "a", 1, "completed", NOW + 100)
    handoff(office, "hf", NOW + 100, "a", "b")
    order(ledger, "of", "closed", closed_at=NOW + 100, spend=30.0, exit_usd=40.0, costs=1.0)
    receipt(ledger, "kf", "j", "of", NOW + 100)
    P.run_once(office, ledger, desk, now=NOW)
    a = [r for r in rows(office, TODAY) if r["actor_id"] == "a" and r["generation"] == 1][0]
    assert a["calls"] == 1 and a["measured_cost_usd"] == pytest.approx(1.0) and a["turns_started"] == 0 and a["covered_through"] == NOW
    assert not [r for r in rows(office, TODAY) if r["actor_id"] in ("b", "j")]
    P.run_once(office, ledger, desk, now=NOW + 200)                                  # the cut has passed them: now they count
    a = [r for r in rows(office, TODAY) if r["actor_id"] == "a" and r["generation"] == 1][0]
    assert a["calls"] == 2 and a["turns_started"] == 1 and a["covered_through"] == NOW + 200
    assert [r for r in rows(office, TODAY) if r["actor_id"] == "j"][0]["linked_closed_n"] == 1


def test_equity_is_stored_at_the_actual_asof_with_a_bucket_key_and_one_point_per_bucket(tmp_path):
    office, ledger, desk = world(tmp_path)
    t = NOW - NOW % 300 + 290.0                                                        # late in a bucket
    (desk / "universe.heartbeat").write_text(json.dumps({"ts": t - 5.0, "sol_usd": 100.0, "sol_age_s": 3.0}))
    assert P.run_once(office, ledger, desk, now=t)["equity_rows"] == 1
    assert P.run_once(office, ledger, desk, now=t + 5)["equity_rows"] == 0             # same bucket: the unique (book_id, bucket_key) holds
    (desk / "universe.heartbeat").write_text(json.dumps({"ts": t + 9.0, "sol_usd": 100.0, "sol_age_s": 3.0}))
    assert P.run_once(office, ledger, desk, now=t + 10)["equity_rows"] == 1            # next bucket
    got = [dict(r) for r in L.open_readonly(ledger).execute("SELECT at, bucket_key, fx_at FROM equity_snapshots ORDER BY at")]
    assert [g["at"] for g in got] == [t, t + 10] and got[0]["bucket_key"] == t - 290.0 and got[1]["bucket_key"] == t - 290.0 + 300.0 and all(g["fx_at"] <= g["at"] for g in got)


def test_a_capital_flow_dated_after_the_as_of_is_not_counted_and_is_once_the_as_of_passes_it(tmp_path):
    office, ledger, desk = world(tmp_path)
    c = sqlite3.connect(ledger)
    c.execute("INSERT INTO capital_flows(book_id, at, kind, usd, actor, note) VALUES('A', ?, 'deposit', 200, 't', 't')", (NOW + 60,))
    c.commit()
    c.close()
    P.run_once(office, ledger, desk, now=NOW)
    got = [dict(r) for r in L.open_readonly(ledger).execute("SELECT * FROM equity_snapshots")][0]
    assert got["state"] == "known" and got["cash_usd"] == pytest.approx(1000.0) and got["equity_usd"] == pytest.approx(1000.0)      # ledger.equity is causal at its as-of
    P.run_once(office, ledger, desk, now=NOW + 400)                                    # once the flow's time has passed it is counted
    last = [dict(r) for r in L.open_readonly(ledger).execute("SELECT * FROM equity_snapshots ORDER BY at")][-1]
    assert last["state"] == "known" and last["cash_usd"] == pytest.approx(1200.0)


def prune_calls(office, table, retained_from):
    c = sqlite3.connect(office)
    c.execute("UPDATE runtime_observation_state SET retained_from=? WHERE table_name=?", (retained_from, table))
    c.commit()
    c.close()


def test_a_closed_day_whose_raw_coverage_was_pruned_is_frozen_keeps_every_column_and_counts_a_late_row(tmp_path):
    office, ledger, desk = world(tmp_path)
    old = NOW - 40 * DAY
    day = P.day_of(old)
    call(office, "o1", "a", 1, old + 100, cost=0.5)
    call(office, "o2", "a", 1, old + 200, cost=0.5)
    handoff(office, "h1", old + 100, "a", "b")
    turn(office, "t1", "a", 1, "completed", old + 50)
    P.run_once(office, ledger, desk, now=NOW)
    before = [r for r in rows(office, day) if r["actor_id"] == "a" and r["generation"] == 1][0]
    assert before["calls"] == 2 and before["measured_cost_usd"] == pytest.approx(1.0) and before["turns_started"] == 1
    # the producer prunes the day's raw rows (outside the open window, after consumption) and records where retention now starts
    c = sqlite3.connect(office)
    c.execute("PRAGMA foreign_keys=OFF")
    c.execute("DROP TRIGGER IF EXISTS model_calls_no_update")
    c.execute("DELETE FROM model_calls WHERE call_id='o1'")
    c.execute("UPDATE runtime_observation_state SET retained_from=? WHERE table_name IN ('model_calls','handoff_events')", (old + 150,))
    c.commit()
    c.close()
    call(office, "late", "a", 1, old + 300, cost=9.0)                                 # a late row for the pruned closed day
    P.run_once(office, ledger, desk, now=NOW + 60)
    after = [r for r in rows(office, day) if r["actor_id"] == "a" and r["generation"] == 1][0]
    # a recount from the retained subset would say 2 calls / 9.5 USD: the totals instead keep the 2 known at close and add the late row EXACTLY (3 calls, 10.0 USD)
    assert after["calls"] == 3 and after["calls_not_ok"] == before["calls_not_ok"] and after["measured_cost_usd"] == pytest.approx(10.0) and after["cost_unknown_n"] == before["cost_unknown_n"]
    for column in ("turns_started", "turns_completed", "turns_partial"):
        assert after[column] == before[column], column                               # every other previously known column survives
    assert after["late_after_close"] == 1 and "model_calls" in json.loads(after["source_heads"])["frozen"]
    assert [r for r in rows(office, day) if r["actor_id"] == "b" and r["generation"] == -1][0]["handoffs_in"] == 1       # the handoff table is frozen too (its retained_from moved): untouched, still known
    P.run_once(office, ledger, desk, now=NOW + 120)
    assert [r for r in rows(office, day) if r["actor_id"] == "a" and r["generation"] == 1][0]["late_after_close"] == 1      # counted once: the watermark passed it


def test_the_first_run_on_a_partially_pruned_day_freezes_instead_of_writing_a_short_count(tmp_path):
    office, ledger, desk = world(tmp_path)
    old = NOW - 40 * DAY
    call(office, "left", "a", 1, old + 500, cost=2.0)                                  # the only retained row of that day
    prune_calls(office, "model_calls", old + 400)                                      # earlier rows of the day were pruned: coverage is partial
    P.run_once(office, ledger, desk, now=NOW)
    r = [x for x in rows(office, P.day_of(old)) if x["actor_id"] == "a"][0]
    assert r["calls"] is None and r["measured_cost_usd"] is None and r["late_after_close"] == 1          # unknown, not 1 call; the retained row is only counted as late


def test_a_fully_retained_closed_day_is_still_recomputed_on_a_late_row(tmp_path):
    office, ledger, desk = world(tmp_path)
    old = NOW - 3 * DAY
    call(office, "o1", "a", 1, old + 100)
    P.run_once(office, ledger, desk, now=NOW)
    call(office, "late", "a", 1, old + 200)
    prune_calls(office, "model_calls", old - 5 * DAY)                                  # retention starts well before the day
    P.run_once(office, ledger, desk, now=NOW + 60)
    r = [x for x in rows(office, P.day_of(old)) if x["actor_id"] == "a"][0]
    assert r["calls"] == 2 and not r["late_after_close"]


def test_a_late_row_for_a_frozen_day_is_counted_once_even_while_the_global_watermark_is_held(tmp_path):
    office, ledger, desk = world(tmp_path)
    old = NOW - 40 * DAY
    day = P.day_of(old)
    call(office, "o1", "a", 1, old + 100)
    call(office, "o2", "a", 1, old + 200)
    P.run_once(office, ledger, desk, now=NOW)
    c = sqlite3.connect(office)
    c.execute("DELETE FROM model_calls")                                              # the producer pruned the old rows
    c.execute("UPDATE runtime_observation_state SET retained_from=? WHERE table_name='model_calls'", (old + 300,))
    c.commit()
    c.close()
    call(office, "today", "a", 1, NOW - 5)                                            # a today row holds the watermark below the late row that follows
    call(office, "late", "a", 1, old + 400)
    for i in range(3):                                                                # reruns with no new raw row change nothing
        P.run_once(office, ledger, desk, now=NOW + 60 + i)
    r = [x for x in rows(office, day) if x["actor_id"] == "a" and x["generation"] == 1][0]
    assert r["calls"] == 3 and r["late_after_close"] == 1


def test_a_closed_day_rolled_up_before_the_freeze_does_not_count_its_own_rows_as_late(tmp_path):
    office, ledger, desk = world(tmp_path)
    old = NOW - 40 * DAY
    day = P.day_of(old)
    call(office, "today", "a", 1, NOW - 5)                                            # holds the watermark below the old rows
    call(office, "o1", "a", 1, old + 100)
    P.run_once(office, ledger, desk, now=NOW)                                         # day rolled up fully retained: o1 counted
    prune_calls(office, "model_calls", old + 500)                                     # then the day becomes partially pruned (frozen)
    P.run_once(office, ledger, desk, now=NOW + 60)
    r = [x for x in rows(office, day) if x["actor_id"] == "a" and x["generation"] == 1][0]
    assert r["calls"] == 1 and not r["late_after_close"]


def test_open_inventory_is_as_of_the_cut_a_future_receipt_or_fill_is_not_open_yet(tmp_path):
    office, ledger, desk = world(tmp_path)
    order(ledger, "fut", "open")
    receipt(ledger, "kfut", "a", "fut", NOW + 100)                                    # receipt dated after the cut
    order(ledger, "now", "open")
    receipt(ledger, "know", "a", "now", NOW - 10)
    P.run_once(office, ledger, desk, now=NOW)
    assert [r for r in rows(office, TODAY) if r["actor_id"] == "a"][0]["open_n"] == 1
    P.run_once(office, ledger, desk, now=NOW + 200)
    assert [r for r in rows(office, TODAY) if r["actor_id"] == "a"][0]["open_n"] == 2


def test_ledger_facts_are_split_by_epoch_and_an_unknown_epoch_is_never_merged_into_a_known_one(tmp_path):
    office, ledger, desk = world(tmp_path)
    t = NOW - 3600
    order(ledger, "oa", "closed", closed_at=t, spend=30.0, exit_usd=40.0, costs=1.0)
    order(ledger, "ob", "closed", closed_at=t, spend=30.0, exit_usd=20.0, costs=1.0)
    c = sqlite3.connect(ledger)
    c.execute("UPDATE orders SET epoch_id='E2' WHERE order_id='ob'")
    c.commit()
    c.close()
    receipt(ledger, "ka", "j", "oa", t)
    receipt(ledger, "kb", "j", "ob", t)
    receipt(ledger, "kc", "j", "missing-order", t)                                     # no order: epoch unknown
    call(office, "c1", "j", -1, t)
    P.run_once(office, ledger, desk, now=NOW)
    by = {x["epoch_id"]: x for x in rows(office, TODAY) if x["actor_id"] == "j"}
    assert set(by) == {"E1", "E2", None}
    assert by["E1"]["decisions"] == 1 and by["E1"]["linked_closed_n"] == 1 and by["E1"]["realized_paper_pnl_usd"] == pytest.approx(9.0)
    assert by["E2"]["decisions"] == 1 and by["E2"]["realized_paper_pnl_usd"] == pytest.approx(-11.0)
    assert by[None]["decisions"] == 1 and by[None]["linked_closed_n"] is None and by[None]["calls"] == 1      # unknown epoch: its own row
    here = Path(__file__).resolve().parents[2].name
    assert json.loads(by["E1"]["source_heads"])["release_sha"] == (here if len(here) == 40 and set(here) <= set("0123456789abcdef") else None)   # the checkout's own name: a pinned release dir (the deploy gate) names its SHA, a plain checkout has none
    P.run_once(office, ledger, desk, now=NOW + 60)
    assert len([x for x in rows(office, TODAY) if x["actor_id"] == "j"]) == 3          # idempotent: still three rows


def test_inventory_filled_before_the_cut_and_closed_after_it_is_open_at_the_cut(tmp_path):
    office, ledger, desk = world(tmp_path)
    order(ledger, "fc", "closed")
    receipt(ledger, "kfc", "a", "fc", NOW - 100)
    c = sqlite3.connect(ledger)
    c.execute("UPDATE orders SET filled_at=?, closed_at=? WHERE order_id='fc'", (NOW - 60, NOW + 60))
    c.commit()
    c.close()
    order(ledger, "closed_before", "closed", closed_at=NOW - 30)
    receipt(ledger, "kcb", "a", "closed_before", NOW - 100)
    P.run_once(office, ledger, desk, now=NOW)
    assert sum((r["open_n"] or 0) for r in rows(office, TODAY)) == 1                   # fc open at the cut; closed_before is not
    plan = " ".join(str(x[-1]) for x in L.open_readonly(ledger).execute("EXPLAIN QUERY PLAN " + P.Q_OPEN_CLOSED_LATER, (NOW, NOW, NOW)))
    assert "orders_closed_at" in plan and "SCAN o" not in plan.replace("SCAN o USING", "")


def test_a_recounted_day_is_a_fresh_snapshot_a_closed_order_leaves_open_inventory_and_writes_zero(tmp_path):
    office, ledger, desk = world(tmp_path)
    order(ledger, "o", "open")
    receipt(ledger, "k", "actor", "o", NOW - 100)
    P.run_once(office, ledger, desk, now=NOW)
    assert sum((r["open_n"] or 0) for r in rows(office, TODAY)) == 1
    c = sqlite3.connect(ledger)
    c.execute("UPDATE orders SET state='closed', closed_at=? WHERE order_id='o'", (NOW + 1,))
    c.commit()
    c.close()
    P.run_once(office, ledger, desk, now=NOW + 2)
    got = [r for r in rows(office, TODAY) if r["epoch_id"] == "E1"]
    assert got and all(r["open_n"] == 0 for r in got)                                  # the stored row had no open order left: written 0, not the stale 1


def test_a_frozen_source_keeps_its_columns_while_every_other_column_is_a_fresh_snapshot(tmp_path):
    office, ledger, desk = world(tmp_path)
    old = NOW - 40 * DAY
    day = P.day_of(old)
    call(office, "o1", "a", 1, old + 100)
    turn(office, "t1", "a", 1, "completed", old + 50)
    P.run_once(office, ledger, desk, now=NOW)
    prune_calls(office, "model_calls", old + 500)
    c = sqlite3.connect(office)
    c.execute("INSERT INTO turn_runs VALUES('t2', ?)", (json.dumps({"actor_id": "a", "generation": 1, "state": "partial", "created_at": old + 60}),))
    c.commit()
    c.close()
    call(office, "late", "a", 1, old + 600)                                             # a late call makes the day recount
    P.run_once(office, ledger, desk, now=NOW + 60)
    r = [x for x in rows(office, day) if x["actor_id"] == "a"][0]
    assert r["calls"] == 2                                                              # frozen source: kept and the late row added exactly
    assert r["turns_started"] == 2 and r["turns_partial"] == 1                          # recounted source: fresh


def test_open_n_of_a_day_older_than_yesterday_is_kept_not_blanked_by_a_recount(tmp_path):
    office, ledger, desk = world(tmp_path)
    old = NOW - 10 * DAY
    P.run_once(office, ledger, desk, now=NOW)                                           # creates the schema
    c = sqlite3.connect(office)
    c.execute("INSERT INTO agent_daily(utc_day, actor_id, generation, epoch_id, open_n, source_heads, covered_through, missing_n, written_at) VALUES(?,?,?,?,?,?,?,?,?)",
              (P.day_of(old), "z", -1, "E1", 3, "{}", old, 0, old))
    c.commit()
    c.close()
    call(office, "late", "z", 1, old + 100)                                             # a late row recounts that old day
    P.run_once(office, ledger, desk, now=NOW + 60)
    assert [r for r in rows(office, P.day_of(old)) if r["actor_id"] == "z" and r["epoch_id"] == "E1"][0]["open_n"] == 3


def test_a_stored_row_with_no_fact_left_this_recount_is_rewritten_with_known_zeros(tmp_path):
    office, ledger, desk = world(tmp_path)
    order(ledger, "o", "open")
    receipt(ledger, "k", "actor", "o", NOW - 2 * DAY)                                   # the row exists today only through its open inventory
    P.run_once(office, ledger, desk, now=NOW)
    assert [r for r in rows(office, TODAY) if r["actor_id"] == "actor"][0]["open_n"] == 1
    c = sqlite3.connect(ledger)
    c.execute("UPDATE orders SET state='closed', closed_at=? WHERE order_id='o'", (NOW - DAY * 1.5,))      # closed in an earlier window: today has no fact left for it
    c.commit()
    c.close()
    P.run_once(office, ledger, desk, now=NOW + 2)
    r = [r for r in rows(office, TODAY) if r["actor_id"] == "actor"][0]
    assert r["open_n"] == 0 and r["decisions"] == 0                                    # no fact left: the stored row is recounted, not left at the stale 1


def test_snapshot_priced_and_unpriced_come_from_the_same_causal_inventory(tmp_path):
    office, ledger, desk = world(tmp_path)
    order(ledger, "later-close", "closed")                                             # filled before the cut, closed after it: still inventory at the cut, no mark = unpriced
    order(ledger, "gone", "closed", closed_at=NOW - 30)                                # closed before the cut: not inventory
    order(ledger, "not-yet", "open")
    order(ledger, "not-yet2", "open")
    c = sqlite3.connect(ledger)
    c.execute("UPDATE orders SET filled_at=?, closed_at=? WHERE order_id='later-close'", (NOW - 60, NOW + 60))
    c.execute("UPDATE orders SET filled_at=? WHERE order_id IN ('not-yet', 'not-yet2')", (NOW + 10,))     # filled after the cut: does not exist yet
    c.commit()
    c.close()
    P.run_once(office, ledger, desk, now=NOW)
    priced, unpriced = sqlite3.connect(ledger).execute("SELECT priced_n, unpriced_n FROM equity_snapshots").fetchone()
    assert (priced, unpriced) == (0, 1)


def test_main_names_every_missing_index_on_stderr_and_stays_quiet_when_none_is_missing(tmp_path, capsys):
    office, ledger, desk = world(tmp_path)
    desk_db(tmp_path, [])                                    # the default desk store (parent of --desk-dir) must exist
    args = ["--office-db", str(office), "--ledger-db", str(ledger), "--desk-dir", str(desk)]
    assert P.main(args) == 0
    assert capsys.readouterr().err == ""
    c = sqlite3.connect(ledger)
    c.execute("DROP INDEX decision_receipts_created_at")
    c.commit()
    c.close()
    assert P.main(args) == 0
    assert "decision_receipts_created_at" in capsys.readouterr().err


# ---------------------------------------------------------------- near-pass funnel (Gamma prereg near-pass-v1 section 3)
def funnel(office, day=None):
    c = sqlite3.connect(office)
    got = {(r[0], r[1], r[2]): r[3] for r in c.execute("SELECT utc_day, feed, step, n FROM funnel_daily WHERE utc_day=?", (day or TODAY,))}
    c.close()
    return got


def desk_db(tmp_path, tickets):
    path = tmp_path / "memecoin_desk.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE desk_tickets(id INTEGER PRIMARY KEY, ticket_id TEXT, stage TEXT, verdict TEXT, reason TEXT, inputs_json TEXT, ts TEXT)")
    c.execute("CREATE INDEX idx_desk_tickets_stage_ts ON desk_tickets(stage, ts)")
    for i, (stage, verdict, reason, evidence, at) in enumerate(tickets):
        c.execute("INSERT INTO desk_tickets(ticket_id, stage, verdict, reason, inputs_json, ts) VALUES(?,?,?,?,?,?)",
                  ("t%d" % i, stage, verdict, reason, json.dumps({"evidence": evidence}), P._iso(at)))
    c.commit()
    c.close()
    return path


NEAR_EVIDENCE = {"checks": {"market_data": {"status": "pass", "reason": "ok"}, "token_safety": {"status": "pass", "reason": "ok"}, "sell_probe": {"status": "pass", "reason": "ok"},
                            "dump_group": {"status": "unknown", "reason": "dump_group_unknown"},
                            "cluster": {"status": "pass", "reason": "ok"}, "holders": {"status": "pass", "reason": "ok"}, "chart": {"status": "pass", "reason": "ok"}}}      # all seven checks (near-pass-allow-v1)


def bundle_chain(office, order_hash, feed):
    """decision_bundles(row_hash=order_hash) -> receipts(row_hash=r) whose request has `feed` (None = a PASS sheet with no feed key)."""
    c = sqlite3.connect(office)
    c.execute("CREATE TABLE IF NOT EXISTS receipts(seq INTEGER PRIMARY KEY, payload_json TEXT NOT NULL, prev_hash TEXT, row_hash TEXT NOT NULL UNIQUE)")
    c.execute("CREATE TABLE IF NOT EXISTS decision_bundles(seq INTEGER PRIMARY KEY, payload_json TEXT NOT NULL, prev_hash TEXT, row_hash TEXT NOT NULL UNIQUE)")
    request = {} if feed is None else {"feed": feed}
    c.execute("INSERT INTO receipts(payload_json, row_hash) VALUES(?,?)", (json.dumps({"kind": "sheet", "request": request}), "r" + order_hash))
    c.execute("INSERT INTO decision_bundles(payload_json, row_hash) VALUES(?,?)", (json.dumps({"receipt_row_hash": "r" + order_hash}), order_hash))
    c.commit()
    c.close()


def test_funnel_counts_desk_tickets_and_the_eligible_near_pass_subset_per_feed(tmp_path):
    office, ledger, desk = world(tmp_path)
    at = NOW - 100
    path = desk_db(tmp_path, [("hard_filter", "PASS", "ok", {}, at), ("hard_filter", "PASS", "ok", {}, at),
                              ("hard_filter", "SKIP", "unknown:dump_group:dump_group_unknown", NEAR_EVIDENCE, at),
                              ("hard_filter", "SKIP", "unknown:chart:chart_unreadable", {"checks": {"chart": {"status": "unknown", "reason": "chart_unreadable"}}}, at),   # not eligible
                              ("hard_filter", "SKIP", "kill:x", {}, at),
                              ("chart", "PASS", "ok", {}, at),                                                                                                          # other stage
                              ("hard_filter", "PASS", "ok", {}, NOW + 500)])                                                                                           # after the cut
    P.run_once(office, ledger, desk, now=NOW, desk_db=path)
    f = funnel(office)
    assert f[(TODAY, "all", "a_tickets")] == 5 and f[(TODAY, "pass", "b_eligible")] == 2 and f[(TODAY, "near_pass", "b_eligible")] == 1
    assert f[(TODAY, "pass", "e_verdicts")] is None and f[(TODAY, "near_pass", "e_verdicts")] is None       # no Judge verdict writer yet: unknown, not 0


def test_funnel_entries_and_closes_are_traced_to_their_sheet_feed_and_an_untraceable_chain_is_unknown_not_pass(tmp_path):
    office, ledger, desk = world(tmp_path)
    t = NOW - 600
    for oid, feed in (("p1", None), ("n1", "near_pass"), ("n2", "near_pass")):
        order(ledger, oid, "closed", closed_at=t + 60)
        bundle_chain(office, "h" + oid, feed)
    order(ledger, "orphan", "closed", closed_at=t + 60)                               # bundle hash points at nothing
    order(ledger, "odd", "closed", closed_at=t + 60)                                  # a sheet whose feed value is neither absent nor near_pass
    bundle_chain(office, "hodd", "something_else")
    c = sqlite3.connect(ledger)
    for oid in ("p1", "n1", "n2", "orphan", "odd"):
        c.execute("UPDATE orders SET decision_bundle_hash=?, filled_at=? WHERE order_id=?", ("h" + oid if oid != "orphan" else "missing", t, oid))
    c.commit()
    c.close()
    P.run_once(office, ledger, desk, now=NOW)
    f = funnel(office)
    assert (f[(TODAY, "pass", "f_entries")], f[(TODAY, "near_pass", "f_entries")], f[(TODAY, "unknown", "f_entries")]) == (1, 2, 2)
    assert (f[(TODAY, "pass", "g_closes")], f[(TODAY, "near_pass", "g_closes")], f[(TODAY, "unknown", "g_closes")]) == (1, 2, 2)


def test_funnel_without_the_desk_store_or_a_source_index_is_unknown_and_a_big_day_is_capped_not_partial(tmp_path, monkeypatch):
    office, ledger, desk = world(tmp_path)
    P.run_once(office, ledger, desk, now=NOW)                                         # no desk db at all
    f = funnel(office)
    assert f[(TODAY, "all", "a_tickets")] is None and f[(TODAY, "near_pass", "b_eligible")] is None and f[(TODAY, "pass", "f_entries")] is None   # orders exist? none, but sources lack the bundle tables: unknown
    monkeypatch.setattr(P, "FUNNEL_CAP", 2)
    path = desk_db(tmp_path, [("hard_filter", "PASS", "ok", {}, NOW - 10 - i) for i in range(3)])
    P.run_once(office, ledger, desk, now=NOW + 1, desk_db=path)
    g = funnel(office)
    assert g[(TODAY, "all", "a_tickets")] == 3 and g[(TODAY, "pass", "b_eligible")] is None                 # over the cap: the total is known, the dependent counts are not guessed


def test_funnel_delivered_and_sheet_counts_use_the_payload_time_and_do_not_guess_past_the_tail(tmp_path, monkeypatch):
    office, ledger, desk = world(tmp_path)
    at = NOW - 300
    bundle_chain(office, "x", None)                                                   # creates receipts / decision_bundles tables
    c = sqlite3.connect(office)
    P.receipt_steps(c, NOW - 60, NOW)
    assert P.receipt_steps(c, NOW - 60, NOW)[0] is None                               # no timestamp indexes yet: unknown, not a scan
    from sniper.office import store as S
    S.open_store(office).close()                                                      # the store owner's init creates the indexes
    def put(payload, n):
        c.execute("INSERT INTO receipts(payload_json, row_hash) VALUES(?,?)", (json.dumps(payload), "rr%d" % n))
    cut = {"basis": "corrected_utc", "receipt_cut": at}
    put({"kind": "sheet", "clock_sample": {"corrected_utc": at - 2 * DAY}, "request": {}}, 7)                                    # another day
    put({"tool": "desk.near_pass_list", "cut": cut, "result": {"items": [{"ticket_id": "t1"}, {"ticket_id": "t2"}]}}, 1)
    put({"tool": "desk.near_pass_list", "cut": cut, "result": {"items": [{"ticket_id": "t2"}, {"ticket_id": "t3"}]}}, 2)          # t2 listed twice: one ticket
    put({"tool": "desk.near_pass_list", "cut": dict(cut, basis="host"), "result": {"items": [{"ticket_id": "t9"}]}}, 3)           # wrong clock basis: ignored
    put({"tool": "desk.near_pass_list", "cut": dict(cut, receipt_cut=NOW + 500), "result": {"items": [{"ticket_id": "t8"}]}}, 4)  # after the cut
    put({"kind": "sheet", "clock_sample": {"corrected_utc": at}, "request": {"feed": "near_pass"}}, 5)
    put({"kind": "sheet", "clock_sample": {"corrected_utc": at}, "request": {}}, 6)
    c.commit()
    c.close()
    P.run_once(office, ledger, desk, now=NOW)
    f = funnel(office)
    assert f[(TODAY, "near_pass", "c_delivered")] == 3 and f[(TODAY, "pass", "c_delivered")] is None
    assert f[(TODAY, "near_pass", "d_sheet")] == 1 and f[(TODAY, "pass", "d_sheet")] == 1
    monkeypatch.setattr(P, "FUNNEL_CAP", 1)                                           # more rows than the cap in the day: unknown, not a partial count
    P.run_once(office, ledger, desk, now=NOW + 1)
    g = funnel(office)
    assert g[(TODAY, "near_pass", "c_delivered")] is None and g[(TODAY, "pass", "d_sheet")] is None


def test_funnel_unknown_sheet_feed_is_never_pass_and_a_late_old_receipt_cannot_fake_coverage(tmp_path):
    from sniper.office import store as S
    c = S.open_store(tmp_path / "o.db")
    def sheet(at, request):
        n = c.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
        c.execute("INSERT INTO receipts(payload_json, prev_hash, row_hash) VALUES(?,?,?)", (json.dumps({"kind": "sheet", "clock_sample": {"corrected_utc": at}, "request": request}), "" if n == 0 else "%064d" % (n - 1), "%064d" % n))
    sheet(NOW - 10, {"feed": "something_else"})
    sheet(NOW - 9, {"feed": "pass"})
    sheet(NOW - 8, {})
    sheet(NOW - 7, {"feed": "near_pass"})
    sheet(NOW - 600, {})                                                              # appended last, dated before the window: not counted
    c.commit()
    got, _ = P.receipt_steps(c, NOW - 60, NOW)
    assert (got["d_pass"], got["d_near_pass"], got["d_unknown"]) == (2, 1, 1)


def test_funnel_refused_by_token_counts_only_tickets_one_denied_reason_away_from_near_pass(tmp_path):
    base = NEAR_EVIDENCE["checks"]
    def ticket(**extra):
        checks = dict(base, **extra)
        return ("hard_filter", "SKIP", "unknown:x", {"checks": checks}, NOW - 100)
    unk = lambda reason: {"status": "unknown", "reason": reason}
    tickets = [
        ticket(flow=unk("flow_60s_thin")), ticket(flow=unk("flow_60s_thin")),                       # one outside token, twice
        ticket(flow=unk("flow_60s_unreadable")),                                                    # denied pattern
        ticket(flow=unk("flow_60s_thin"), chart=unk("odd_chart")),                                 # two offenders: not one token
        ticket(flow={"status": "kill", "reason": "flow_dead"}),                                    # kill: refused for a different reason
        ticket(chart=unk("kill:chart_dead")),                                                      # kill reason under an unknown status: still a kill
        ticket(),                                                                                  # eligible: not a refusal
        ("hard_filter", "SKIP", "unknown:x", {"checks": dict(base, market_data={"status": "unknown", "reason": "md"}, flow=unk("flow_60s_thin"))}, NOW - 100),   # required check failed
    ]
    office, ledger, desk = world(tmp_path)
    path = desk_db(tmp_path, tickets)
    P.run_once(office, ledger, desk, now=NOW, desk_db=path)
    c = sqlite3.connect(office)
    got = {t: (n, sh) for t, n, sh in c.execute("SELECT token, n, share FROM funnel_refused_token WHERE utc_day=?", (TODAY,))}
    assert got == {"flow_60s_thin": (2, 2 / 3), "flow_60s_unreadable": (1, 1 / 3)}
    P.FUNNEL_CAP, cap = 1, P.FUNNEL_CAP
    try:
        P.run_once(office, ledger, desk, now=NOW + 1, desk_db=path)
    finally:
        P.FUNNEL_CAP = cap
    assert list(c.execute("SELECT token, n, share FROM funnel_refused_token WHERE utc_day=?", (TODAY,))) == [("*", None, None)]     # capped: unknown, not a partial table


def test_the_cli_derives_the_desk_db_from_desk_dir_and_never_goes_blind_silently(tmp_path, capsys):
    """Alpha: the cron line once omitted --desk-db and the default resolved inside the pinned worktree, so the funnel ran with desk=None and a_tickets/b_eligible were NULL for hours."""
    office, ledger, desk = world(tmp_path)
    base = ["--office-db", str(office), "--ledger-db", str(ledger), "--desk-dir", str(desk)]
    assert P.main(base) == 2                                                     # no desk store at the parent of --desk-dir: loud, not an 'unavailable' funnel
    assert json.loads(capsys.readouterr().out) == {"error": "missing_db", "which": "memecoin_desk.db"}
    assert P.main(base + ["--desk-db", str(tmp_path / "elsewhere.db")]) == 2     # an explicit path that does not exist fails the same way
    assert json.loads(capsys.readouterr().out)["which"] == "elsewhere.db"
    assert not sqlite3.connect(office).execute("SELECT name FROM sqlite_master WHERE name='funnel_daily'").fetchall()   # nothing was written
    desk_db(tmp_path, [("hard_filter", "PASS", "ok", {}, NOW - 100)])
    assert P.main(base) == 0                                                     # the parent of --desk-dir holds it: no flag needed
    capsys.readouterr()
    a_tickets = [n for (n,) in sqlite3.connect(office).execute("SELECT n FROM funnel_daily WHERE step='a_tickets' AND feed='all'")]
    assert a_tickets and a_tickets[-1] is not None                               # known, not NULL
