"""G1/G2 worker (stage 1). Offline: fake transport; the key pin, tiering, calibration gate and cap behaviour."""
import importlib.util
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sniper.services import memecoin_desk_backfill_history as H

_hs = importlib.util.spec_from_file_location("bfh_tests", Path(__file__).resolve().parent / "test_desk_backfill_history.py")
HT = importlib.util.module_from_spec(_hs)
_hs.loader.exec_module(HT)
Fake, coin_page, create_tx, full_page, cycle = HT.Fake, HT.coin_page, HT.create_tx, HT.full_page, HT.cycle
spec = importlib.util.spec_from_file_location("bfw", Path(__file__).resolve().parents[1] / "scripts/memecoin_desk_backfill_worker.py")
W = importlib.util.module_from_spec(spec)
spec.loader.exec_module(W)
NOW = datetime(2026, 10, 28, 3, tzinfo=timezone.utc).timestamp()
DAY = "2026-10-27"


def test_the_key_pin_refuses_any_other_key_and_a_missing_one(tmp_path):
    ok = {"MEMECOIN_RESEARCH_HELIUS_RPC_URL": "https://mainnet.helius-rpc.com/?api-key=a56bd061-1111"}
    assert W.helius_url(tmp_path / "none", ok).endswith("a56bd061-1111")
    with pytest.raises(W.Refused, match="key_pin_mismatch"):
        W.helius_url(tmp_path / "none", {"MEMECOIN_RESEARCH_HELIUS_RPC_URL": "https://x/?api-key=dea05c94-2222"})
    with pytest.raises(W.Refused, match="no_key"):
        W.helius_url(tmp_path / "none", {})
    env = tmp_path / ".env"
    env.write_text("OTHER=1\nMEMECOIN_RESEARCH_HELIUS_RPC_URL='https://x/?api-key=a56bd061-3333'\n")
    assert W.helius_url(env, {}).endswith("a56bd061-3333")


def seed_desk(tmp_path):
    d = tmp_path / "desk"
    (d / "candidate_emissions").mkdir(parents=True)
    (d / "candidate_emissions" / (DAY + ".jsonl")).write_text(json.dumps({"network_id": "solana", "mint": "MC"}) + "\n" + json.dumps({"network_id": "base", "mint": "MX"}) + "\n")
    for feed, rows in (("fomo", [("MF", "solana", DAY), ("MC", "solana", DAY), ("ME", "1", DAY)]), ("pumpfun", [("MP", "solana", DAY), ("MF", "solana", DAY), ("MO", "solana", "2026-10-26")])):
        (d / "intake" / feed).mkdir(parents=True)
        con = sqlite3.connect(d / "intake" / feed / "first_sightings.db")
        con.execute("create table first_sightings(feed text, network_id text, mint text, day text, observed_at real, primary key(feed, network_id, mint))")
        for i, (m, n, day) in enumerate(rows):
            con.execute("insert into first_sightings values(?,?,?,?,?)", (feed, n, m, day, float(i)))
        con.commit()
        con.close()
    (d / "backfill").mkdir()
    return d


def test_targets_are_tiered_solana_only_for_the_day_and_deduplicated(tmp_path):
    d = seed_desk(tmp_path)
    assert W.load_targets(d, DAY) == [("MC", 1), ("MF", 2), ("MP", 4)]


def prepare(tmp_path, *, costs=True, cap=5_000_000):
    d = seed_desk(tmp_path)
    c = cycle(tmp_path)
    (d / "backfill" / "cycle.json").write_text(json.dumps({k: c[k] for k in c if not k.startswith("_")} | {"attested_at": "2026-10-27T12:00:00Z", "month_cap": cap}))
    if costs:
        (d / "backfill" / "costs.json").write_text(json.dumps({"calibrated": True, H.METHOD_FULL: 10}))
    return d


def fake3():
    return Fake({m: [coin_page(m, 10 + i, 1_793_100_000 + i)] for i, m in enumerate(("MC", "MF", "MP"))} | {HT.CR: [full_page([create_tx("a", "MC", HT.CR, 10, 1_793_100_000)])]})


def test_nightly_refuses_until_costs_are_calibrated(tmp_path):
    d = prepare(tmp_path, costs=False)
    fake = fake3()
    with pytest.raises(W.Refused, match="costs_not_calibrated"):
        W.nightly(d, DAY, 1000, fake.transport, clock=lambda: NOW)
    assert fake.calls == []


def test_nightly_fetches_by_tier_resolves_creators_and_a_rerun_spends_nothing(tmp_path):
    d = prepare(tmp_path)
    fake = fake3()
    out = W.nightly(d, DAY, 1000, fake.transport, clock=lambda: NOW)
    assert [c[1] for c in fake.calls] == ["MC", HT.CR, "MF", "MP"] and out["coins"] == 3                  # tier 1 coin, ITS creator, then the later tiers
    con = sqlite3.connect(d / "backfill" / "history.db")
    assert con.execute("select count(*) from coin_creation where state='known'").fetchone()[0] == 3
    assert con.execute("select creator, state from creator_scans").fetchall() == [(HT.CR, "complete")]
    n_calls, total = len(fake.calls), out["ledger_total"]
    again = W.nightly(d, DAY, 1000, fake.transport, clock=lambda: NOW)
    assert len(fake.calls) == n_calls and again["ledger_total"] == total and again["coins"] == 0


def test_the_run_cap_leaves_the_rest_pending_and_never_spends_past_it(tmp_path):
    d = prepare(tmp_path)
    fake = fake3()
    out = W.nightly(d, DAY, 20, fake.transport, clock=lambda: NOW)
    assert out["run_spent"] <= 20 and out["stopped"] == "credits_exhausted" and len(fake.calls) == 2
    con = sqlite3.connect(d / "backfill" / "history.db")
    assert con.execute("select key, state from g12_targets where kind='coin' order by priority, rowid").fetchall() == [("MC", "done"), ("MF", "pending"), ("MP", "pending")]
    assert con.execute("select state from creator_scans").fetchall() == [("complete",)]                       # the cap went to tier 1 coin + its creator, not to later tiers' coins


def test_attest_costs_refuses_a_dashboard_delta_above_the_documented_bound(tmp_path):
    d = prepare(tmp_path, costs=False)
    (d / "backfill" / "calibration.json").write_text(json.dumps({"ledger_total": 200}))
    with pytest.raises(W.Refused):
        W.attest_costs(d, 201)
    assert W.load_costs(d / "backfill") is None
    W.attest_costs(d, 170)
    assert W.load_costs(d / "backfill") == {H.METHOD_FULL: 10}


def test_shared_key_attestation_needs_an_aggregate_floor_and_a_zero_delta_is_refused(tmp_path):
    d = prepare(tmp_path, costs=False)
    (d / "backfill" / "calibration.json").write_text(json.dumps({"ledger_total": 610}))
    with pytest.raises(W.Refused, match="shared_key_needs_expected_min"):
        W.attest_costs(d, 800, shared_key=True)
    with pytest.raises(W.Refused, match="below_expected_min"):
        W.attest_costs(d, 0, shared_key=True, expected_min=430)                                          # a zero delta is never attested (the floor is aggregate: it cannot prove our own cost)
    with pytest.raises(W.Refused, match="below_expected_min"):
        W.attest_costs(d, 429, shared_key=True, expected_min=430)
    assert W.load_costs(d / "backfill") is None
    out = W.attest_costs(d, 800, shared_key=True, expected_min=430)                                       # above the ledger bound is allowed ONLY on the shared-key path
    rec = json.loads((d / "backfill" / "costs.json").read_text())
    assert out["shared_key"] is True and rec["shared_key"] is True and rec["dashboard_delta"] == 800 and rec["expected_min"] == 430 and "shared" in rec["note"]
    assert W.load_costs(d / "backfill") == {H.METHOD_FULL: 10}


def test_the_strict_path_still_refuses_above_the_bound_and_bool_or_negative_deltas(tmp_path):
    d = prepare(tmp_path, costs=False)
    (d / "backfill" / "calibration.json").write_text(json.dumps({"ledger_total": 610}))
    for bad in (611, -1, True):
        with pytest.raises(W.Refused):
            W.attest_costs(d, bad)
    assert W.load_costs(d / "backfill") is None


def test_a_tight_cap_buys_creators_of_the_best_tier_before_any_later_tier_coin(tmp_path):
    d = prepare(tmp_path)
    fake = fake3()
    W.nightly(d, DAY, 20, fake.transport, clock=lambda: NOW)
    assert [c[1] for c in fake.calls] == ["MC", HT.CR]                                                       # 33k tier-4 coins can no longer starve every creator scan


def test_a_tier_whose_coins_are_done_still_gets_its_unfinished_creator_scanned(tmp_path, monkeypatch):      # Delta 9e2b0116e P1
    d = prepare(tmp_path)
    monkeypatch.setattr(W, "load_targets", lambda desk, day: [("MC", 1)])
    fake = Fake({"MC": [coin_page("MC", 10, 1_793_100_000)], HT.CR: [full_page([create_tx("a", "MC", HT.CR, 10, 1_793_100_000)])]})
    W.nightly(d, DAY, 10, fake.transport, clock=lambda: NOW)
    assert [c[1] for c in fake.calls] == ["MC"]                                                             # cap 10: the coin is known, the creator is still owed
    W.nightly(d, DAY, 20, fake.transport, clock=lambda: NOW)
    assert [c[1] for c in fake.calls] == ["MC", HT.CR]
    assert sqlite3.connect(d / "backfill" / "history.db").execute("select state from creator_scans").fetchall() == [("complete",)]


@pytest.mark.parametrize("bound", [None, -1, "610", True, 6.5, [610]])
def test_a_malformed_calibration_bound_never_enables_spend_on_either_path(tmp_path, bound):                  # Delta P1
    d = prepare(tmp_path, costs=False)
    (d / "backfill" / "calibration.json").write_text(json.dumps({"ledger_total": bound}))
    with pytest.raises(W.Refused, match="calibration_ledger_total_invalid"):
        W.attest_costs(d, 800, shared_key=True, expected_min=430)
    with pytest.raises(W.Refused, match="calibration_ledger_total_invalid"):
        W.attest_costs(d, 100)
    assert W.load_costs(d / "backfill") is None and not (d / "backfill" / "costs.json").exists()


def test_an_unreadable_or_non_object_calibration_record_fails_closed_before_any_write(tmp_path):
    d = prepare(tmp_path, costs=False)
    for content in (None, "{not json", "[]", "null"):
        cal = d / "backfill" / "calibration.json"
        cal.unlink(missing_ok=True)
        if content is not None:
            cal.write_text(content)
        with pytest.raises(W.Refused):
            W.attest_costs(d, 800, shared_key=True, expected_min=430)
        assert not (d / "backfill" / "costs.json").exists()


def test_the_floor_cannot_exceed_what_our_own_ledger_attempted_and_the_record_is_labelled_floor_only(tmp_path):
    d = prepare(tmp_path, costs=False)
    (d / "backfill" / "calibration.json").write_text(json.dumps({"ledger_total": 610}))
    with pytest.raises(W.Refused, match="above_ledger_bound"):
        W.attest_costs(d, 900, shared_key=True, expected_min=700)
    W.attest_costs(d, 800, shared_key=True, expected_min=430)
    rec = json.loads((d / "backfill" / "costs.json").read_text())
    assert rec["model"] == "operator_attested_10_per_attempt_shared_key_floor_only" and "ONLY" in rec["note"] and "no upper bound" in rec["note"]
    assert "never under" not in json.dumps(rec) and "undercount" not in json.dumps(rec)


def test_a_nightly_run_records_its_window_so_a_nightly_claim_can_be_checked(tmp_path):                       # Gamma: via cross-check needs run windows
    d = prepare(tmp_path)
    fake = fake3()
    ticks = iter([NOW + i for i in range(1000)])
    W.nightly(d, DAY, 1000, fake.transport, clock=lambda: next(ticks))
    con = sqlite3.connect(d / "backfill" / "history.db")
    mode, day, start, end, cap, spent, outcome = con.execute("select mode, day, started_at, finished_at, run_cap, run_spent, outcome from g12_runs").fetchone()
    assert (mode, day, cap, outcome) == ("nightly", DAY, 1000, "done") and end > start and spent == 40
    fetched = [r[0] for r in con.execute("select fetched_at from coin_creation")]
    assert fetched and all(start <= f <= end for f in fetched)                                                 # every fetch of the run sits inside its recorded window


def test_a_run_that_stops_records_why(tmp_path):
    d = prepare(tmp_path)
    W.nightly(d, DAY, 20, fake3().transport, clock=lambda: NOW)
    assert sqlite3.connect(d / "backfill" / "history.db").execute("select outcome, run_spent from g12_runs").fetchone() == ("credits_exhausted", 20)
