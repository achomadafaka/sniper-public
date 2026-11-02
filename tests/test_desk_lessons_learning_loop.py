"""A single offline fixture pins learning bounds, costs, cohorts, censoring and daily output."""
import copy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path

import pytest

from sniper.services import memecoin_desk_lessons as L
from sniper.services import memecoin_desk_lessons_digest as DG


ROOT = Path(__file__).resolve().parents[1]


def test_desk_learning_loop(tmp_path):
    assert hasattr(L, "learning_facts"), "watch-window learning facts are not implemented"
    t0, offset = 1_800_000_000.0, 3600.0
    rows = []
    for k in range(-20, 1002):
        q = 80 if -4 <= k < 0 else 110 if k == 0 else 1 if k in (-20, 1) else 100
        rows.append(dict(received_at=t0+k, slot=10000+k, ordinal=0, tx_index=0,
                         signature=f"s{k}", is_buy=1, quote_atomic=1_000_000_000,
                         token_atomic=1_000_000, actor="wallet", virtual_quote=q*10**9,
                         virtual_token=10**15, real_token=7*10**14, real_quote=50*10**9,
                         fee_bps=100, creator_fee_bps=0, cashback_fee_bps=0,
                         quote_mint="11111111111111111111111111111111"))
    unit = dict(kind="realized", t_ref=t0, t_fill=t0, cost_sol=1.0, qty=8_920_606_584_274,
                watch_started_at=t0-10, watch_expires_at=t0+20, trigger_at=t0-4,
                judge_mcap_usd=9000, fill_mcap_usd=11000, entry_venue="curve",
                rails={"sl_pct":25, "tp_pct":0, "trail_pct":10}, plan_horizon_s=60,
                closed_at_corrected=t0+30-offset)
    tape = L.Tape(rows, coverage_until=t0+1001)
    f = L.learning_facts(unit, tape, watch_coverage_until=t0+1001, clock_offset_s=offset)
    assert f["basis"] == "tape_model_net_after_known_venue_costs"
    assert f["all_in_net"] == "unknown" and f["costs"]["residual_model_cost_pp"] == 2.5
    assert f["costs"]["entry_lag_s"] == 1.5
    best = f["watch_entry"]["upper_bound_hindsight"]
    assert best["price_sol_per_token"] == pytest.approx(8.180000015514056e-8, rel=1e-8)
    assert t0-10-offset <= best["entry_at_utc"] <= t0-offset
    assert f["times"]["fill_utc"] == t0-offset
    assert f["returns"]["R_touch"] > f["returns"]["R_response"]
    assert f["returns"]["R_fill"] is not None
    assert {"live_rails", "trail_10pct", "trail_15pct", "trail_20pct", "hold_plan", "no_exit_mark900"} <= set(f["alternatives"])
    assert f["alternatives"]["no_exit_mark900"]["mark_only"] is True
    assert f["stages"]["judge"] == "4-10k" and f["stages"]["fill"] == "10-20k"
    assert [L.mcap_stage(x) for x in (None, 0, 3999, 4000, 10000, 20000, 30000)] == [
        "unknown", "below_4k", "below_4k", "4-10k", "10-20k", "20-30k", "30k-completion (curve)"]
    assert L.mcap_stage(50000, migrated=True) == "post_migration_entry_censored"
    invalid = copy.deepcopy(rows)
    invalid[20]["cashback_fee_bps"] = None
    bad = L.learning_facts(unit, L.Tape(invalid, coverage_until=t0+1001),
                           watch_coverage_until=t0+1001, clock_offset_s=offset)
    assert bad["returns"]["R_fill"] is None and "cashback_fee_unknown" in bad["censored"]
    assert bad["all_in_net"] == "unknown"
    gap = L.learning_facts(unit, tape, watch_coverage_until=t0-1, clock_offset_s=offset)
    assert gap["watch_entry"]["upper_bound_hindsight"] is None
    assert "watch_coverage_incomplete" in gap["censored"]
    pool = L.learning_facts({**unit, "entry_venue":"pumpswap"}, tape,
                            watch_coverage_until=t0+1001, clock_offset_s=offset)
    assert pool["stages"]["fill"] == "post_migration_entry_censored"
    assert pool["returns"]["R_fill"] is None

    spec = importlib.util.spec_from_file_location("lessons_learning_worker", ROOT / "scripts/memecoin_desk_lessons.py")
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
    vet = dict(response_at=iso(t0-10), inputs_json=json.dumps({
        "response_at":iso(t0-10), "sheet":{"mcap_usd":9000},
        "trigger":{"expires_at":iso(t0+20)}, "result":{"plan":{"horizon_s":60}}}),
        output_json=json.dumps({"result":{"plan":{"horizon_s":60}}}))
    meta = worker._learning_inputs(vet, {"inputs_json":json.dumps({"fire":{"received_at":t0-4}})})
    assert (meta["watch_started_at"], meta["trigger_at"], meta["judge_mcap_usd"]) == (t0-10, t0-4, 9000)
    early, _ = worker.compute(dict(kind="realized", key="pos:99", mint="M", t_ref=t0,
                                   cohort="c", prompt_version="p", entry_sig=None,
                                   exit_info={"exit_ts":t0+30}), tmp_path, 1.5, t0+2000, worker.Clock(offset))
    assert early["identity"]["cohort_id"] == "c" and early["identity"]["prompt_version"] == "p"
    assert early["learning_loop"]["closed_at_corrected"] == t0+30-offset

    db = L.connect(tmp_path / "lessons.db")
    for i in range(39):
        cohort = "a" if i < 20 else "b"
        loop = copy.deepcopy(f if i else bad)
        facts = {"identity":{"cohort_id":cohort,"prompt_version":"p"}, "learning_loop":loop,
                 "exit":{"realized_pnl_usd":1.0,"entry_notional_usd":25.0,"realized_R":0.04},
                 "alternatives":{}, "closed_at_corrected":t0+30-offset}
        L.store(db, f"pos:{i}", "realized", f"coin{i//2}", i, f"ticket{i}", t0+i, t0+39-i, facts, [])
    db.close()
    loaded = DG.load(tmp_path / "lessons.db")
    summary = DG.learning_loop(loaded, batch_n=20)
    assert len(summary["rows"]) == 39 and len(summary["batches"]) == 1
    assert summary["batches"][0]["n"] == 20 and summary["batches"][0]["coin_n"] == 10
    assert summary["pending"]["cohort=b prompt=p"] == 19
    assert summary["comparison_count"] == 24
    assert summary["emitted_comparison_count"] == 12
    rendered = "\n".join(DG.render_learning_loop(loaded, batch_n=20))
    assert "descriptive; no rule change from a digest alone" in rendered
    assert "hypothesis, needs prereg + holdout" in rendered and "coin-clustered" in rendered
    assert "achievable" not in rendered.lower() and "all_in_net" in rendered and "unknown" in rendered
    assert rendered.count("| pos:0 |") == 1
    assert "usable booked-rails baseline=19/20" in rendered
    assert "booked SL/TP retained" in rendered
    position_line = next(line for line in rendered.splitlines() if line.startswith("| pos:1 |"))
    for trail in (10, 15, 20):
        assert f"rails + trail {trail}%=" in position_line
        assert f"| rails + trail {trail}% |" in rendered
    assert {c["alternative"] for c in summary["batches"][0]["comparisons"]} == {
        "trail_10pct", "trail_15pct", "trail_20pct"}
    zero_baseline = copy.deepcopy(loaded[:20])
    zero_baseline[0]["facts"]["learning_loop"]["alternatives"]["live_rails"].update(R=0.0, censored=True)
    zero_baseline[1]["facts"]["learning_loop"]["alternatives"]["live_rails"].update(R=0.0, censored=False)
    assert "usable booked-rails baseline=19/20" in "\n".join(DG.render_learning_loop(zero_baseline, batch_n=20))
    db = L.connect(tmp_path / "lessons.db")
    facts["identity"]["cohort_id"] = "a"
    L.store(db, "pos:late", "realized", "latecoin", 99, "late", t0+100, t0-100, facts, [])
    db.close()
    late = DG.learning_loop(DG.load(tmp_path / "lessons.db"), batch_n=20)
    assert late["batches"][0]["keys"] == [f"pos:{i}" for i in range(20)]
    assert late["batches"][0]["coin_n"] == 10 and late["pending"]["cohort=a prompt=p"] == 1

    mixed_db = tmp_path / "mixed-lessons.db"
    db = L.connect(mixed_db)
    legacy = {"identity":{"cohort_id":"a","prompt_version":"p"}, "alternatives":{}, "exit":{}}
    for i in range(100, 120):
        db.execute("INSERT INTO lessons VALUES (?,?,?,?,?,?,?,?,?,?)", (
            f"pos:{i}", "lessons-v2", "realized", f"old{i}", i, f"oldticket{i}", t0+i, t0,
            json.dumps(legacy), "[]"))
    db.commit()
    L.store(db, "pos:200", "realized", "newcoin", 200, "new", t0+200, t0, facts, [])
    assert len(DG.load(mixed_db)) == 1
    L.store(db, "pos:100", "realized", "old100", 100, "upgraded", t0+201, t0, facts, [])
    db.close()
    mixed = DG.load(mixed_db, latest_per_key=True)
    assert len(mixed) == 21 and len(DG.load(mixed_db)) == 2
    assert sum(r["key"] == "pos:100" for r in mixed) == 1
    assert DG.learning_loop(mixed, batch_n=20)["batches"] == []

    spec = importlib.util.spec_from_file_location("learning_daily", ROOT / "scripts/memecoin_desk_daily_report.py")
    daily = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(daily)
    day = datetime.fromtimestamp(t0-offset, timezone.utc).date().isoformat()
    result = daily.run(day, ledger=None, desk=None, checker=None, gpt=None, risk=None, gaps=None,
                       deploy_logs=[], out_dir=tmp_path / "report", reviews=False,
                       lessons_db=str(tmp_path / "lessons.db"))
    assert "Desk learning loop" in Path(result["path"]).read_text()
    broken = tmp_path / "broken-lessons.db"
    broken.write_text("not a SQLite database")
    result = daily.run(day, ledger=None, desk=None, checker=None, gpt=None, risk=None, gaps=None,
                       deploy_logs=[], out_dir=tmp_path / "report-broken", reviews=False,
                       lessons_db=str(broken))
    markdown = Path(result["path"]).read_text()
    assert "Desk learning loop" in markdown and "unavailable" in markdown
    assert "No stored realized positions" not in markdown
