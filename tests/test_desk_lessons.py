"""Lessons: exact-math facts per trade and per counterfactual unit, censoring (never zero), code tags, immutable rows, the digest, and the
review input. Report only: nothing here touches a ledger, a config or a decision."""
import importlib.util
import json
import sqlite3
import statistics
from pathlib import Path

import pytest

from sniper.services import memecoin_desk_lessons as L
from sniper.services import memecoin_desk_lessons_digest as DG
from sniper.services import memecoin_desk_trade_review as TR

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("lessons_worker", ROOT / "scripts/memecoin_desk_lessons.py")
W = importlib.util.module_from_spec(spec)
spec.loader.exec_module(W)

T0 = 1_800_000_000.0
VT = 1e15                      # virtual tokens (atomic); price (SOL per whole token) = vq / 1e18
COST = L.CLIP_USD / L.FX0


def swap(t, price, buy=True, sol=1.0, actor="a", i=0, real_quote=50_000_000_000):
    return {"received_at": t, "slot": int(t) % 10**9, "ordinal": i, "signature": "s%s%d" % (t, i), "is_buy": 1 if buy else 0,
            "quote_atomic": int(sol * 1e9), "token_atomic": int(sol * 1e9 / (price * 1e9) * 1e6), "actor": actor,
            "virtual_quote": int(price * 1e18), "virtual_token": int(VT), "fee_bps": 95, "creator_fee_bps": 30,
            "real_token": 7 * 10**14, "real_quote": real_quote}


def path(points, step=1.0, start=-150.0, **kw):
    return [swap(T0 + start + i * step, p, actor="a%d" % (i % 7), i=i, **kw) for i, p in enumerate(points)]


def unit(**kw):
    return {"kind": "counterfactual", "t_ref": T0 - 1.5, "cost_sol": None, "qty": None, "t_fill": T0, "rails": None, "emitted_at": T0 - 4.0, **kw}


def facts(rows, pool=(), **kw):
    return L.facts_for(unit(**kw.pop("u", {})), L.Tape(rows, pool), launch_at=None, fill_latency=1.5, **kw)


# a coin that dips after our fill then runs: entry is neither the best before nor the best after
RUN = [3.0e-8 + 0.02e-8 * i for i in range(150)] + [4.2e-8 - 0.04e-8 * i for i in range(30)] + [3.0e-8 + 0.03e-8 * i for i in range(300)]


def test_the_exact_buy_and_sell_math_round_trips_with_the_fee():
    t = L.Tape(path([3e-8] * 20))
    i = t.idx(T0 - 100)
    qty = t.buy_tokens(i, COST)
    back = t.v(i, qty)
    assert back < COST and back == pytest.approx(COST * (1 - 0.0125) ** 2, rel=0.03)       # 125 bps each way plus the curve impact of a 0.24 SOL clip


def test_entry_is_compared_with_the_best_buy_before_and_after_the_fill():
    f, tags = facts(path(RUN))
    assert f["censored"] == [] and f["curve_fill_at_entry"] == pytest.approx(50 / 84, abs=1e-3)
    assert f["entry_vs_best_before_pct"] > 0                                              # the window before the fill held cheaper prices
    assert f["entry_vs_best_after_pct"] is not None
    assert f["entry_regret_pct"] == pytest.approx(max(f["entry_vs_best_before_pct"], f["entry_vs_best_after_pct"]), abs=1e-6)
    assert 0 <= f["entry_rank_pct"] <= 100 and f["secs_band_cross_to_fill"] == 4.0


def test_the_alternatives_are_exact_and_rails_fire_on_a_crash():
    crash = [3e-8] * 150 + [3e-8 * (1 - 0.02 * i) for i in range(1, 30)] + [1.2e-8] * 200
    f, _ = facts(path(crash), u={"rails": {"sl_pct": 14, "tp_pct": 30, "trail_pct": 8}})
    a = f["alternatives"]
    assert a["rails"]["reason"] in ("trail", "sl") and -0.35 < a["rails"]["R"] < 0
    assert a["fixed_10s"]["R"] > a["fixed_120s"]["R"] and a["hold300_cat"]["reason"] == "cat"
    assert a["death_any"]["R"] is not None and {"rails", "fixed_10s", "fixed_30s", "fixed_60s", "fixed_120s", "hold300_cat", "death_any",
                                                "sell_before_migration"} == set(a)
    assert f["death_k"] == 4 and f["death_k_provisional"] is True                         # stored in every row


def test_a_graduation_held_through_is_the_pool_state_and_censored_without_a_pool_tape():
    rows = path([3e-8 + 0.01e-8 * i for i in range(300)], real_quote=70_000_000_000)
    rows[-1]["real_quote"] = 85_000_000_000                                               # the curve completes at the last swap
    f, _ = facts(rows)
    assert f["graduated_in_window"] is True and "no_pool_tape" in f["censored"]
    assert f["alternatives"]["fixed_120s"]["censored"] is False                           # exits before completion are fine
    assert f["alternatives"]["hold300_cat"]["R"] is None and f["alternatives"]["hold300_cat"]["censored"] is True
    pool = [{"t": T0 + t, "q": int(4.9e9), "b": int(2e14), "fee": 0.0125} for t in (149, 150, 200, 260, 320)]   # opens about 20% under the last curve print, kept fresh
    g, _ = facts(rows, pool)
    assert g["migration"]["pool_R"] is not None and g["migration"]["step_R"] < 0 and "no_pool_tape" not in g["censored"]
    assert g["alternatives"]["hold300_cat"]["R"] == g["migration"]["pool_R"]


def test_the_code_tags_are_computed_from_the_facts():
    rows = path([3e-8] * 150 + [3.1e-8 - 0.04e-8 * i for i in range(30)] + [3.4e-8 + 0.02e-8 * i for i in range(300)], real_quote=80_000_000_000)
    gross = COST * 0.92 / 0.9875                                                            # 95 + 30 bps of fees: proceeds = 92% of the cost
    rows.append(swap(T0 + 30, 3.1e-8, buy=False, sol=gross, actor="us", i=999))
    info = {"exit_ts": T0 + 30, "exit_sig": rows[-1]["signature"], "realized_pnl_usd": -2.0, "exit_reason": "real_trail", "entry_notional_usd": 25.0}
    f, tags = facts(rows, exit_info=info)
    assert f["exit"]["realized_R"] == pytest.approx(-0.08, abs=1e-3) and f["exit"]["realized_R_basis"] == "sol_tape_all_legs" and f["after_exit_R_hindsight"]["+60s"] is not None and f["after_exit_R_hindsight"]["+300s"] is None   # tape ends before exit+300: unknown, not zero
    assert "late_curve" in tags and "shaken_out" in tags                                    # fill 0.95; we left at a loss, the bag recovered
    assert "entered_farm" not in tags                                                       # null until Gamma's features
    quiet, qtags = facts(path([3e-8] * 400), exit_info={**info, "realized_pnl_usd": 1.0})
    assert "shaken_out" not in qtags and "late_curve" not in qtags


def test_good_exit_is_derived_from_the_running_median_and_needs_five_priors():
    assert L.good_exit_band([0.1, 0.2]) == (None, 2)
    band = L.good_exit_band([0.1, 0.2, 0.3, 0.4, 0.5])
    assert band == (0.3, 5)
    assert "good_exit" in L.unit_tags([], 0.25, band) and "good_exit" not in L.unit_tags([], 0.35, band) and L.unit_tags([], 0.0, (None, 3)) == []


def test_skip_sampling_is_by_hash_not_by_outcome():
    ids = ["desk:U:%024x" % i for i in range(2000)]
    share = sum(L.hash_sampled(i, 20) for i in ids) / len(ids)
    assert 0.17 < share < 0.23 and all(L.hash_sampled(i, 100) for i in ids[:50]) and not any(L.hash_sampled(i, 0) for i in ids[:50])
    assert [L.hash_sampled(i, 20) for i in ids[:100]] == [L.hash_sampled(i, 20) for i in ids[:100]]


def test_a_unit_with_no_tape_or_no_entry_state_is_censored_never_zero():
    f, tags = L.facts_for(unit(), L.Tape(path([3e-8] * 10, start=-5000.0)), launch_at=None, fill_latency=1.5)
    assert "no_swaps_after_entry" in f["censored"] and tags == [] and "alternatives" not in f
    g, _ = L.facts_for(unit(t_fill=T0 - 9000), L.Tape(path([3e-8] * 10)), launch_at=None, fill_latency=1.5)
    assert g["censored"] == ["no_state_at_entry"]


# ------------------------------------------------------------------------------------------------ the worker, on fixture DBs

def _fixture(tmp_path, *, with_skip=True):
    desk = tmp_path / "desk"
    for d in ("swaps", "launches", "pumpswap"):
        (desk / d).mkdir(parents=True)
    day = L.utc_day(T0)
    s = sqlite3.connect(desk / "swaps" / ("%s.db" % day))
    s.execute("CREATE TABLE swaps(slot INTEGER, signature TEXT, ordinal INTEGER, mint TEXT, curve TEXT, is_buy INTEGER, quote_atomic INTEGER, "
              "token_atomic INTEGER, actor TEXT, virtual_quote INTEGER, virtual_token INTEGER, block_time INTEGER, received_at REAL, "
              "real_token INTEGER, fee_bps INTEGER, creator_fee_bps INTEGER, tx_index INTEGER, real_quote INTEGER, PRIMARY KEY(signature, ordinal))")
    for mint, base in (("MA", T0), ("MB", T0 + 2000), ("MC", T0 + 4000)):
        for r in path([3e-8 + 0.001e-8 * i for i in range(1200)], start=(base - T0) - 150.0):
            s.execute("INSERT INTO swaps VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (r["slot"], mint + r["signature"], 0, mint, "c", r["is_buy"], r["quote_atomic"],
                      r["token_atomic"], r["actor"], r["virtual_quote"], r["virtual_token"], 0, r["received_at"], 7 * 10**14, 95, 30, 0, 50_000_000_000))
    s.commit(); s.close()
    ledger = sqlite3.connect(tmp_path / "ledger.db")
    ledger.executescript("CREATE TABLE memecoin_positions(id INTEGER, mint TEXT, opened_at TEXT, closed_at TEXT, realized_pnl REAL, exit_reason TEXT, "
                         "entry_notional REAL, signal_source TEXT, is_open INTEGER, desk_opportunity_id TEXT);"
                         "CREATE TABLE memecoin_fills(position_id INTEGER, kind TEXT, tx_sig TEXT, qty REAL, id INTEGER PRIMARY KEY);")
    iso = lambda t: __import__("datetime").datetime.fromtimestamp(t, __import__("datetime").timezone.utc).isoformat()
    ledger.execute("INSERT INTO memecoin_positions VALUES(1,'MA',?,?,3.0,'real_trail',25.0,'desk',0,'desk:U:a1')", (iso(T0), iso(T0 + 40)))
    ledger.execute("INSERT INTO memecoin_fills(position_id,kind,tx_sig) VALUES(1,'entry',?)", ("MA" + "s%s%d" % (T0 + 150.0 - 150.0, 150),))
    ledger.commit(); ledger.close()
    dk = sqlite3.connect(tmp_path / "desk.db")
    dk.execute("CREATE TABLE desk_tickets(id INTEGER PRIMARY KEY, ticket_id TEXT, mint TEXT, stage TEXT, verdict TEXT, reason TEXT, ts TEXT, "
               "response_at TEXT, output_json TEXT, inputs_json TEXT, cohort_id TEXT, prompt_version TEXT)")
    dk.execute("CREATE TABLE desk_outbox(ticket_id TEXT PRIMARY KEY, state TEXT)")
    def tk(tid, mint, stage, verdict, reason, t, rails=None, emitted=None):
        dk.execute("INSERT INTO desk_tickets(ticket_id, mint, stage, verdict, reason, ts, response_at, output_json, inputs_json, cohort_id, prompt_version) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (tid, mint, stage, verdict, reason, iso(t), iso(t), json.dumps({"result": {"rails": rails}}),
                    json.dumps({"asof": {"candidate": {"emitted_at": iso(emitted or t - 3)}}}), "cohort-x", "judge-v19"))
    rails = {"sl_pct": 14, "tp_pct": 30, "trail_pct": 8}
    tk("desk:U:a1", "MA", "vet", "ENTER", "enter", T0 - 1.5, rails)
    tk("desk:U:a1", "MA", "fill", "ENTER", "intent_filled", T0)
    tk("desk:U:b1", "MB", "vet", "ENTER", "enter", T0 + 2000, rails)
    tk("desk:U:b1", "MB", "fill", "SKIP", "intent_refused", T0 + 2001)
    tk("desk:U:c1", "MC", "vet", "SKIP", "model_skip", T0 + 4000)
    dk.commit(); dk.close()
    return desk, str(tmp_path / "ledger.db"), str(tmp_path / "desk.db")


def _run(tmp_path, desk, ledger, deskdb, now, *, skip_pct="100", extra=()):
    import os
    os.environ["MEMECOIN_DESK_LESSONS_SKIP_SAMPLE_PCT"] = skip_pct
    out = tmp_path / "lessons.db"
    real_time = W.time.time
    W.time.time = lambda: now
    try:
        W.main(["--ledger", ledger, "--desk-dir", str(desk), "--desk-db", deskdb, "--out", str(out), *extra])
    finally:
        W.time.time = real_time
    return out


def test_worker_writes_one_immutable_row_per_unit_kinds_are_labelled_and_a_rerun_changes_nothing(tmp_path):
    desk, ledger, deskdb = _fixture(tmp_path)
    out = _run(tmp_path, desk, ledger, deskdb, T0 + 6000)
    db = sqlite3.connect(out)
    rows = {r[0]: (r[1], json.loads(r[2]), json.loads(r[3])) for r in db.execute("SELECT key, kind, facts_json, tags_json FROM lessons")}
    assert {k: v[0] for k, v in rows.items()} == {"pos:1": "realized", "tkt:desk:U:b1": "counterfactual", "tkt:desk:U:c1": "skip_counterfactual"}
    assert rows["pos:1"][1]["exit"]["realized_pnl_usd"] == 3.0 and rows["pos:1"][1]["alternatives"]["rails"]["R"] is not None
    assert rows["tkt:desk:U:b1"][1]["why"] == "refused" and rows["tkt:desk:U:c1"][1]["why"] == "model_skip"
    assert "exit" not in rows["tkt:desk:U:b1"][1]                                              # a counterfactual has no realized exit
    before = db.execute("SELECT key, computed_at, facts_json FROM lessons ORDER BY key").fetchall()
    _run(tmp_path, desk, ledger, deskdb, T0 + 9000)
    assert db.execute("SELECT key, computed_at, facts_json FROM lessons ORDER BY key").fetchall() == before        # immutable, idempotent


def test_an_after_window_that_has_not_passed_is_not_stored_yet_and_the_skip_share_is_the_hash(tmp_path):
    desk, ledger, deskdb = _fixture(tmp_path)
    out = _run(tmp_path, desk, ledger, deskdb, T0 + 100)                                        # the 15 minute window is still open
    assert not sqlite3.connect(out).execute("SELECT count(*) FROM lessons").fetchone()[0]
    other = tmp_path / "x"
    other.mkdir()
    desk2, ledger2, deskdb2 = _fixture(other)
    out2 = _run(other, desk2, ledger2, deskdb2, T0 + 6000, skip_pct="0")
    kinds = [r[0] for r in sqlite3.connect(out2).execute("SELECT kind FROM lessons")]
    assert "skip_counterfactual" not in kinds and "realized" in kinds and "counterfactual" in kinds


def test_the_kill_knob_and_a_missing_entry_swap(tmp_path, monkeypatch):
    desk, ledger, deskdb = _fixture(tmp_path)
    monkeypatch.setenv("MEMECOIN_DESK_LESSONS_ENABLED", "0")
    assert W.main(["--ledger", ledger, "--desk-dir", str(desk), "--desk-db", deskdb, "--out", str(tmp_path / "off.db")]) == 0
    assert not (tmp_path / "off.db").exists()
    monkeypatch.delenv("MEMECOIN_DESK_LESSONS_ENABLED")
    c = sqlite3.connect(ledger)
    c.execute("UPDATE memecoin_fills SET tx_sig='nope'")
    c.commit(); c.close()
    out = _run(tmp_path, desk, ledger, deskdb, T0 + 6000)
    f = json.loads(sqlite3.connect(out).execute("SELECT facts_json FROM lessons WHERE key='pos:1'").fetchone()[0])
    assert f["censored"] == ["entry_swap_missing"] and "alternatives" not in f                 # a stored censored row, not a hole and not a zero


# ---------------------------------------------------------------------------------------------------------------- the digest

def row(kind, mint, tags, *, pnl=None, R=None, alts=None, fill=None, rank=None, best=0.1, why=None, step=None, regret=None):
    f = {"alternatives": {k: {"R": v} for k, v in (alts or {}).items()}, "curve_fill_at_entry": fill, "entry_rank_pct": rank,
         "best_R_hindsight": best, "entry_regret_pct": 5.0, "entry_vs_best_after_pct": 4.0, "exit_regret_R": regret if regret is not None else 0.1}
    if pnl is not None:
        f["exit"] = {"realized_pnl_usd": pnl, "realized_R": R, "entry_notional_usd": 25.0}
    if why:
        f["why"] = why
    if step is not None:
        f["migration"] = {"step_R": step}
    return {"key": "k%s%s" % (kind, mint), "kind": kind, "mint": mint, "t0": 1.0, "facts": f, "tags": tags}


def test_digest_attaches_usd_ranks_the_mistakes_and_marks_thin_evidence():
    real = [row("realized", "m%d" % i, ["late_curve"], pnl=-2.0, R=-0.08, alts={"rails": -0.05}, fill=0.95) for i in range(6)]
    real += [row("realized", "n%d" % i, ["shaken_out"], pnl=-1.0, R=-0.04, alts={"hold300_cat": 0.05}) for i in range(3)]
    real += [row("realized", "g%d" % i, ["good_exit"], pnl=2.0, R=0.08, alts={"rails": 0.0}, fill=0.7) for i in range(4)]
    cf = [row("counterfactual", "c%d" % i, [], alts={"rails": 0.1, "fixed_30s": 0.02}, fill=0.95, why="refused") for i in range(3)]
    sk = [row("skip_counterfactual", "s%d" % i, [], best=0.4, alts={"fixed_30s": 0.05}, why="model_skip") for i in range(5)]
    d = DG.digest(real + cf + sk)
    assert d["counts"] == {"realized": 13, "counterfactual": 3, "skip_counterfactual": 5}
    assert d["tags"]["late_curve"]["pnl_usd"] == -12.0 and d["tags"]["late_curve"]["n"] == 6 and d["tags"]["good_exit"]["pnl_usd"] == 8.0
    assert d["tags"]["late_curve"]["entry_regret_usd_hindsight"] == pytest.approx(6 * 0.04 * 25.0)             # the saving from waiting, not the run before
    assert [m["tag"] for m in d["mistakes"]] == ["late_curve", "shaken_out"]                  # most negative realized USD first
    late, shaken = d["mistakes"]
    assert "recurring, insufficient evidence" in late["status"] and shaken["status"].startswith("seen, insufficient")      # n < 20 is never 'candidate'
    assert late["evidence"]["n_at_or_above"] == 9 and late["evidence"]["mean_R_below"] == 0.0   # g* (fill .7) rails 0.0
    assert shaken["evidence"]["n"] == 3 and shaken["evidence"]["mean_diff_R"] == pytest.approx(-0.09)
    assert d["selection"]["sampled_skips"]["n"] == 5 and d["selection"]["sampled_skips"]["share_best_ge_20pct"] == 1.0
    assert d["selection"]["skip_reasons"] == {"model_skip": 5} and d["selection"]["entered"]["n"] == 13
    md = "\n".join(DG.render_markdown(d, "2026-10-23"))
    assert "Lessons digest" in md and "late_curve" in md and "nothing here applies" in md.replace("Nothing here applies", "nothing here applies")


def test_a_tag_with_twenty_trades_becomes_a_candidate_and_the_farm_tag_says_it_is_waiting():
    real = [row("realized", "m%d" % i, ["migration_step", "entered_farm"], pnl=-1.0, R=-0.04, alts={"sell_before_migration": 0.02}, step=-0.2) for i in range(20)]
    d = DG.digest(real)
    mig = next(m for m in d["mistakes"] if m["tag"] == "migration_step")
    assert mig["status"] == "candidate" and mig["evidence"]["mean_migration_step_R"] == -0.2
    assert next(m for m in d["mistakes"] if m["tag"] == "entered_farm")["evidence"] == {"note": "no feature version carries the farm features yet", "n_usable": 0}


def test_the_digest_reads_the_newest_feature_version_per_key(tmp_path):
    db = L.connect(tmp_path / "l.db")
    for ver, tags in (("lessons-v1", ["late_curve"]), ("lessons-v2", ["shaken_out"])):
        db.execute("INSERT INTO lessons VALUES('pos:1',?,'realized','M',1,'t',1.0,1.0,?,?)", (ver, json.dumps({"exit": {}}), json.dumps(tags)))
    db.commit()
    rows = DG.load(tmp_path / "l.db")
    assert len(rows) == 1 and rows[0]["tags"] == ["shaken_out"] and DG.load(tmp_path / "missing.db") == []


# ------------------------------------------------------------------------------------------------------- the GPT review input

def test_the_review_gets_the_lessons_as_numbers_and_tokens_and_the_new_tags_are_in_the_enum():
    r = row("realized", "m1", ["late_curve", "shaken_out"], pnl=-2.0, R=-0.08, alts={"rails": -0.05, "death_any": 0.01}, fill=0.95, rank=91.0)
    r["facts"]["after_exit_R_hindsight"] = {"+30s": 0.02, "+300s": 0.2}
    r["facts"]["censored"] = ["no_pool_tape"]
    lf = TR.lessons_facts(r)
    assert lf["code_tags"] == ["late_curve", "shaken_out"] and lf["hindsight"]["alternatives"]["death_any"]["R"] == 0.01 and lf["hindsight"]["after_exit_R"]["+300s"] == 0.2
    assert lf["known_at_entry"]["curve_fill_at_entry"] == 0.95 and lf["censored"] == ["no_pool_tape"] and "alternatives_R" not in lf
    assert "entry_rank_pct" not in lf["known_at_entry"] and lf["hindsight"]["entry_optima"]["entry_rank_pct"] == 91.0     # future optima sit in the hindsight block
    assert lf["hindsight"]["death_k_provisional"] is None                                                                     # the fixture row carries no k: unknown, not claimed
    flat = json.dumps(lf)
    assert "ticker" not in flat and TR.lessons_facts(None) is None
    for tag in L.TAGS:
        assert tag in TR.TAGS
    assert "hindsight" in TR.PROMPT and "NOT knowable at the time" in TR.PROMPT
    facts_ = TR.build_facts({"entry_notional": 25.0}, {}, [], [], lessons=r)
    assert facts_["lessons"] == lf and TR.build_facts({"entry_notional": 25.0}, {}, [], [])["lessons"] is None
    ok = TR.sanitize_review(json.dumps({"what_the_sheet_showed": "a", "what_happened": "b", "what_was_missed": "c", "tag": "late_curve"}))
    assert ok and ok["tag"] == "late_curve"


def test_a_second_write_of_the_same_key_and_version_never_changes_the_row(tmp_path):
    db = L.connect(tmp_path / "l.db")
    assert L.store(db, "pos:1", "realized", "M", 1, "t", 1.0, 1.0, {"v": 1}, ["late_curve"]) is True
    assert L.store(db, "pos:1", "realized", "M", 1, "t", 2.0, 1.0, {"v": 2}, ["shaken_out"]) is False
    r = db.execute("SELECT facts_json, tags_json, computed_at FROM lessons").fetchone()
    assert (json.loads(r[0]), json.loads(r[1]), r[2]) == ({"v": 1}, ["late_curve"], 1.0)
    assert db.execute("SELECT count(*) FROM lessons").fetchone()[0] == 1


def test_the_daily_report_appends_the_digest_and_the_knob_switches_it_off(tmp_path, monkeypatch):
    spec2 = importlib.util.spec_from_file_location("daily_report", ROOT / "scripts/memecoin_desk_daily_report.py")
    D = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(D)
    db = L.connect(tmp_path / "lessons.db")
    L.store(db, "pos:1", "realized", "M", 1, "t", 1.0, 1_800_000_000.0, {"exit": {"realized_pnl_usd": -2.0, "realized_R": -0.08, "entry_notional_usd": 25.0},
                                                                        "alternatives": {}}, ["late_curve"])
    day = "2027-01-15"                                                                       # the UTC day that holds t0 = 1_800_000_000
    kw = dict(ledger=None, desk=None, checker=None, gpt=None, risk=None, gaps=None, deploy_logs=[], out_dir=tmp_path / "out", reviews=False,
              lessons_db=str(tmp_path / "lessons.db"))
    page = D.run(day, **kw)["path"]
    md = Path(page).read_text()
    assert "## Lessons digest" in md and "late_curve" in md and "All-time" in md
    monkeypatch.setenv("MEMECOIN_DESK_LESSONS_DIGEST", "0")
    assert "Lessons digest" not in Path(D.run(day, **{**kw, "out_dir": tmp_path / "out2"})["path"]).read_text()


def test_digest_reads_one_feature_version_states_non_additive_tags_and_needs_distinct_coins(tmp_path):
    db = L.connect(tmp_path / "l.db")
    for ver in ("lessons-v2", "lessons-v10"):
        for i in range(3):
            db.execute("INSERT INTO lessons VALUES(?,?,'realized',?,1,'t',1.0,1.0,?,?)",
                       ("pos:%d" % i, ver, "M%d" % i, json.dumps({"exit": {"realized_pnl_usd": -1.0, "entry_notional_usd": 25.0}}), json.dumps(["late_curve", "shaken_out"])))
    db.commit()
    rows = DG.load(tmp_path / "l.db")
    assert len(rows) == 3 and {r["feature_version"] for r in rows} == {"lessons-v10"}              # v10 is newer than v2, one version only
    assert len(DG.load(tmp_path / "l.db", version="lessons-v2")) == 3
    d = DG.digest(rows)
    assert d["feature_versions"] == ["lessons-v10"] and "not additive" in d["tag_usd_note"] and "-3.00" in d["tag_usd_note"]   # 3 distinct trades, not 6 tag sums
    twenty_one_coin = [row("realized", "same", ["migration_step"], pnl=-1, R=-0.04, alts={"sell_before_migration": 0.02}) for _ in range(20)]
    assert DG.digest(twenty_one_coin)["mistakes"][0]["status"] != "candidate"                      # 20 trades on ONE coin are not 20 independent pairs
    md = "\n".join(DG.render_markdown(DG.digest(rows), "x"))
    assert "frozen rule" in md and "forward cohort" in md and "exploratory" in md


def test_exit_regret_is_the_best_value_after_the_exit_not_the_peak_before_it():
    up = [3e-8 + 0.05e-8 * i for i in range(210)] + [3e-8 + 0.05e-8 * 210 - 0.1e-8 * i for i in range(100)] + [1.5e-8] * 1100   # peak at +60 s, then down
    rows = path(up)
    rows.append(swap(T0 + 150, 1.9e-8, buy=False, sol=COST * 0.9, actor="us", i=999))
    info = {"exit_ts": T0 + 150, "exit_sig": rows[-1]["signature"], "realized_pnl_usd": -1.0, "exit_reason": "real_trail", "entry_notional_usd": 25.0}
    f, _ = facts(rows, exit_info=info)
    assert f["exit_window"] == {"after_s": 900, "known_s": f["exit_window"]["known_s"], "complete": True} and f["exit_window"]["known_s"] > 800
    assert f["exit_regret_R"] == 0.0                                                                  # nothing after the exit beat what we got: no regret for the earlier peak
    assert f["best_R_hindsight"] > 0.2                                                                # the peak before the exit is still reported, separately
