"""Stage 1b inline fetch (Alpha/Gamma 2026-10-28). Offline: fake transport, fake clock."""
import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from sniper.services import memecoin_desk_backfill_history as H
from sniper.services import memecoin_desk_g12_intake as G

_hs = importlib.util.spec_from_file_location("bfh_tests", Path(__file__).resolve().parent / "test_desk_backfill_history.py")
HT = importlib.util.module_from_spec(_hs)
_hs.loader.exec_module(HT)
NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc).timestamp()
NOW_T = NOW


def setup(tmp_path, *, knob="1", costs=True, pages=None, daily_cap=30_000):
    d = tmp_path / "desk"
    (d / "backfill").mkdir(parents=True)
    c = HT.cycle(tmp_path)
    (d / "backfill" / "cycle.json").write_text(json.dumps({k: c[k] for k in c if not k.startswith("_")} | {"attested_at": "2026-10-27T12:00:00Z"}))
    if costs:
        (d / "backfill" / "costs.json").write_text(json.dumps({"calibrated": True, H.METHOD_FULL: 10}))
    fake = HT.Fake(pages if pages is not None else {"M1": [HT.coin_page("M1", 50, 1_793_100_000)], HT.CR: [HT.full_page([HT.create_tx("a", "M1", HT.CR, 50, 1_793_100_000)])]})
    t = [NOW]
    ticks = lambda: t[0]
    env = {G.KNOB: knob} if knob is not None else {}
    return G.Intake(d, environ=env, clock=ticks, transport=fake.transport, daily_cap=daily_cap), fake, d, t


def log_rows(d):
    return sqlite3.connect(d / "backfill" / "history.db").execute("select status, reason, coin, creator_state, credits from g12_inline_log").fetchall()


def test_knob_off_opens_nothing_and_writes_nothing(tmp_path):
    for knob in (None, "0", "true"):
        it, fake, d, _ = setup(tmp_path / str(knob), knob=knob) if (tmp_path / str(knob)).mkdir() is None else None
        assert it.fetch("M1") == {"status": "skipped", "reason": "knob_off", "credits": 0}
        assert fake.calls == [] and not (d / "backfill" / "history.db").exists() and not (d / "backfill" / "credits.db").exists()


def test_uncalibrated_costs_and_a_missing_cycle_are_typed_skips_that_send_nothing(tmp_path):
    it, fake, d, _ = setup(tmp_path, costs=False)
    assert it.fetch("M1")["reason"] == "costs_not_calibrated" and fake.calls == []
    (d / "backfill" / "costs.json").write_text(json.dumps({"calibrated": True, H.METHOD_FULL: 10}))
    (d / "backfill" / "cycle.json").unlink()
    assert it.fetch("M1")["reason"] == "no_cycle" and fake.calls == []


def test_a_fetch_stores_coin_and_creator_scan_and_logs_wall_time_and_credits(tmp_path):
    it, fake, d, _ = setup(tmp_path)
    out = it.fetch("M1")
    assert out["status"] == "fetched" and out["coin"] == "known" and out["creator_state"] == "complete" and out["credits"] == 20 and out["wall_ms"] >= 0
    assert log_rows(d) == [("fetched", None, "known", "complete", 20)]
    h = sqlite3.connect(d / "backfill" / "history.db")
    assert h.execute("select state from coin_creation where mint='M1'").fetchone() == ("known",) and h.execute("select state, covered_slot from creator_scans").fetchone()[0] == "complete"
    assert [c[1] for c in h.execute("pragma table_info(g12_inline_log)")][-2:] == ["wall_ms", "clock_offset_s"]


def test_the_creator_scan_is_capped_at_two_pages_inline(tmp_path):
    pages = {"M1": [HT.coin_page("M1", 50, 1_793_100_000)], HT.CR: [HT.full_page([HT.create_tx("a%d" % i, "X%d" % i, HT.CR, 10 + i, 1000 + i)], "t%d" % i) for i in range(6)]}
    it, fake, d, _ = setup(tmp_path, pages=pages)
    out = it.fetch("M1")
    assert out["creator_state"] == "pagination_incomplete" and out["credits"] == 30 and len(fake.calls) == 3          # one coin call + two creator pages
    sc = sqlite3.connect(d / "backfill" / "history.db").execute("select state, pages from creator_scans").fetchone()
    assert sc == ("capped", 2)                                                                                        # kept as a lower bound for the reader


def test_the_overall_deadline_stops_new_requests_before_they_are_reserved_or_charged(tmp_path):
    it, fake, d, t = setup(tmp_path)
    orig = fake.transport
    def slow(method, params):
        t[0] += 5.0                                                                                                    # the coin call alone burns the 4 s budget
        return orig(method, params)
    it._transport = slow
    out = it.fetch("M1")
    assert out["coin"] == "known" and out["creator_state"] == "deadline" and out["credits"] == 10 and len(fake.calls) == 1
    assert H.prior_launches(sqlite3.connect(d / "backfill" / "history.db"), HT.CR, coin_slot=50, coin_block_time=1_793_100_000) is None   # a deadline is unknown, never lb 0


def test_the_daily_cap_is_a_typed_skip_and_the_counter_follows_the_day(tmp_path):
    it, fake, d, t = setup(tmp_path, daily_cap=30)
    assert it.fetch("M1")["status"] == "fetched"                                                                        # 20 of 30 spent
    out = it.fetch("M2")
    assert out == {**out, "status": "skipped", "reason": "daily_cap"} and it.spent_today() == 20
    t[0] += 86400
    assert it.spent_today() == 0


def test_an_exception_in_the_fetch_is_a_typed_error_row_never_a_raise(tmp_path):
    it, fake, d, t = setup(tmp_path)
    it._open()
    it._transport = lambda m, p: (_ for _ in ()).throw(RuntimeError("boom"))
    out = it.fetch("M1")
    assert out["status"] == "fetched" and out["coin"] == "transport_error"                                              # the Fetcher types it
    it._con.close()
    assert it.fetch("M3")["status"] == "error"


def test_cheap_pass_requires_every_refusing_gate_and_the_collected_facts():
    ok = {"market_data": {"status": "pass"}, "token_safety": {"status": "pass", "authority_mint": "closed", "authority_freeze": "closed"}, "sell_probe": {"status": "pass"}}
    soft = {n: {"status": "pass"} for n in ("holders", "cluster", "dump_group", "chart")}
    assert G.cheap_pass(ok, soft) is True
    assert G.cheap_pass({**ok, "sell_probe": {"status": "kill"}}, soft) is False
    assert G.cheap_pass({**ok, "token_safety": {"status": "pass", "authority_mint": "open"}}, soft) is False
    assert G.cheap_pass(ok, {**soft, "chart": {"status": "unknown"}}) is False
    assert G.cheap_pass(ok, {k: v for k, v in soft.items() if k != "holders"}) is False
    assert G.cheap_pass({}, soft) is False


# ---- the assembler hook
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import memecoin_desk_intake_assembler as IA  # noqa: E402

CHECKS = {"market_data": {"status": "pass"}, "token_safety": {"status": "pass", "authority_mint": "closed", "authority_freeze": "closed"}, "sell_probe": {"status": "pass"}}
SOFT = {n: {"status": "pass"} for n in ("holders", "cluster", "dump_group", "chart")}


def test_the_hook_fetches_only_for_a_cheap_passer_logs_one_row_and_never_touches_the_checks(tmp_path):
    it, fake, d, _ = setup(tmp_path)
    rows = []
    out = IA.history_hook(it, {"mint": "M1", "ticket_id": "T1"}, dict(CHECKS), dict(SOFT), rows.append)
    assert out["status"] == "fetched" and rows[0]["stage"] == "g12_history" and rows[0]["ticket_id"] == "T1" and rows[0]["inline_spent_today"] == 20
    assert IA.history_hook(it, {"mint": "M9", "ticket_id": "T2"}, {**CHECKS, "sell_probe": {"status": "kill"}}, dict(SOFT), rows.append) is None
    assert len(rows) == 1 and [c[1] for c in fake.calls] == ["M1", HT.CR]                    # the refused coin spent nothing


def test_the_hook_is_a_noop_when_the_knob_is_off_or_there_is_no_intake_object(tmp_path):
    it, fake, d, _ = setup(tmp_path, knob="0")
    rows = []
    assert IA.history_hook(it, {"mint": "M1"}, dict(CHECKS), dict(SOFT), rows.append) is None and IA.history_hook(None, {"mint": "M1"}, CHECKS, SOFT, rows.append) is None
    assert rows == [] and fake.calls == []


def test_a_fault_inside_the_hook_is_a_log_row_not_an_exception(tmp_path):
    class Boom:
        def enabled(self):
            return True

        def fetch(self, mint):
            raise RuntimeError("x")
    rows = []
    assert IA.history_hook(Boom(), {"mint": "M1", "ticket_id": "T"}, dict(CHECKS), dict(SOFT), rows.append) is None
    assert rows == [{"stage": "g12_history", "ticket_id": "T", "status": "hook_error", "reason": "RuntimeError"}]


# ---- Delta 0646abf72 witnesses
def test_inline_429_does_not_retry_and_is_a_typed_rate_limit(tmp_path):
    it, fake, d, t = setup(tmp_path)
    fake.script = [(429, b'{"error":"rate limited"}')] * 4
    out = it.fetch("M1")
    assert len(fake.calls) == 1 and out["reason"] == "rate_limited" and out["credits"] == 10


def test_a_failed_diagnostic_log_cannot_reopen_the_daily_budget(tmp_path):
    it, fake, d, t = setup(tmp_path, daily_cap=30)
    assert it._open() is None
    it._con.execute("create trigger reject_log before insert on g12_inline_log begin select raise(ABORT, 'log failure'); end")
    it._con.commit()
    first = it.fetch("M1")
    assert first["status"] == "fetched" and it.spent_today() > 0
    assert it.fetch("M2")["reason"] == "daily_cap"                                                             # the budget row, not the log, is the authority


def test_a_crash_between_reserve_and_settle_keeps_the_reservation_counted(tmp_path):
    it, fake, d, t = setup(tmp_path, daily_cap=60)
    it._settle_day = lambda rid, credits: None                                                                  # the process dies before it settles
    it.fetch("M1")
    assert it.spent_today() == 30 and it.fetch("M2")["status"] == "fetched" and it.spent_today() == 60 and it.fetch("M3")["reason"] == "daily_cap"


def test_two_instances_cannot_both_reserve_the_last_daily_slot(tmp_path):
    a, fake, d, t = setup(tmp_path, daily_cap=30)
    b = G.Intake(d, environ={G.KNOB: "1"}, clock=lambda: t[0], transport=fake.transport, daily_cap=30)
    assert a._open() is None and b._open() is None
    assert a._reserve_day("M1", NOW) is not None and b._reserve_day("M2", NOW) is None


def test_the_inline_path_uses_a_short_sqlite_busy_timeout_and_a_remaining_budget_bound(tmp_path):
    pages = {"M1": [HT.coin_page("M1", 50, 1793100000)], HT.CR: [HT.full_page([HT.create_tx("a", "X", HT.CR, 10, 1000)], "next"), HT.full_page([])]}
    it, fake, d, t = setup(tmp_path, pages=pages)
    assert it._open() is None
    assert it._con.execute("pragma busy_timeout").fetchone()[0] == 500 and it._ledger.con.execute("pragma busy_timeout").fetchone()[0] == 500
    original = it._transport
    def slow(method, params):
        out = original(method, params)
        t[0] += 1.5
        return out
    it._transport = slow
    out = it.fetch("M1")
    assert out["wall_ms"] <= 4000 and out["coin"] == "known" and out["creator_state"] == "deadline"            # the third request would have ended at 4.5 s: not started


# ---- Delta 9f2bed284 P2: fact + provenance are one failure-atomic transaction
def _trigger(con):
    con.execute("create trigger reject_via before insert on g12_via begin select raise(ABORT, 'via write failed'); end")
    con.commit()


def test_a_failed_via_write_leaves_no_coin_fact_and_the_caller_log_cannot_commit_half_of_it(tmp_path):
    it, fake, d, t = setup(tmp_path)
    assert it._open() is None
    _trigger(it._con)
    out = it.fetch("M1")
    assert out["status"] == "error" and out["credits"] > 0                                                      # the provider charge is still recorded (a separate durable ledger)
    con = sqlite3.connect(d / "backfill" / "history.db")
    assert con.execute("select count(*) from coin_creation").fetchone()[0] == 0                                 # no coin fact without its provenance, although _log committed the same connection
    assert con.execute("select count(*) from g12_inline_log").fetchone()[0] == 1 and it.spent_today() > 0       # the error row and the conservative daily reservation survive


def test_a_failed_via_write_leaves_no_scan_and_preserves_prior_committed_facts(tmp_path):
    it, fake, d, t = setup(tmp_path)
    assert it._open() is None
    f = H.Fetcher(it._con, it._ledger, fake.transport, clock=lambda: NOW_T, via="inline")
    it._con.execute("insert into coin_creation values('PRIOR','known',NULL,'sig',5,1000,'w','C','P',1.0,0.0)")      # an earlier committed fact on the same connection
    it._con.commit()
    _trigger(it._con)
    try:
        f._scan(HT.CR, None, 1, 5, [{"mint": "X", "signature": "s", "slot": 7, "block_time": 9}])
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    it._con.commit()                                                                                           # what a caller's later commit would do
    con = sqlite3.connect(d / "backfill" / "history.db")
    assert raised and con.execute("select count(*) from creator_scans").fetchone()[0] == 0 and con.execute("select count(*) from creator_launches").fetchone()[0] == 0
    assert con.execute("select count(*) from creator_scan_log").fetchone()[0] == 0
    assert con.execute("select mint from coin_creation").fetchall() == [("PRIOR",)]                             # the prior committed fact is untouched



def test_cheap_pass_follows_the_cohorts_required_list_unknown_optional_facts_still_fetch():
    ok = {"market_data": {"status": "pass"}, "token_safety": {"status": "pass", "authority_mint": "closed", "authority_freeze": "closed"}, "sell_probe": {"status": "pass"}}
    soft = {n: {"status": "pass"} for n in ("holders", "cluster", "dump_group", "chart")}
    core = ("chart", "holders")
    assert G.cheap_pass(ok, {**soft, "dump_group": {"status": "unknown"}, "cluster": {"status": "unknown"}}, core) is True
    assert G.cheap_pass(ok, {**soft, "chart": {"status": "unknown"}}, core) is False
    assert G.cheap_pass(ok, {**soft, "dump_group": {"status": "kill"}}, core) is False
    assert G.cheap_pass(ok, {**soft, "dump_group": {"status": "unknown"}}) is False                  # default stays the four-list
