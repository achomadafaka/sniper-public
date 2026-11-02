"""Per-wallet queryable store (db_store): persistence, idempotent re-run, current-run flipping,
cross-wallet isolation, the per-position activity drill-down, path validation, open-position live
marks, and schema migration of pre-existing DBs."""
import os
import sqlite3

import pytest

from sniper.analysis.winning_wallet_thesis import db_store
from sniper.analysis.winning_wallet_thesis.methods.types import MethodFinding
from sniper.analysis.winning_wallet_thesis.types import (
    CapitalRiskProfile, EdgeCluster, EdgeSlice, LedgerRow, PositionBook,
    PositionEpisode, ThesisProfile, WalletEventLedger,
)

W1 = "0x" + "a" * 40
W2 = "0x" + "b" * 40


@pytest.fixture(autouse=True)
def _isolated_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(db_store, "DB_DIR", str(tmp_path / "wallet_analysis"))


def _build(wallet, run_id="run1", *, cond="cond1", pnl=1000.0):
    rows = [
        LedgerRow("buy", wallet, cond, 100, seq=0, outcome_index=0, price=0.4, size=10, usdc=4.0, episode_index=0),
        LedgerRow("sell", wallet, cond, 200, seq=1, outcome_index=0, price=0.6, size=10, usdc=6.0, episode_index=0),
        LedgerRow("split", wallet, cond, 150, seq=2, usdc=1.0),                                   # condition-level event
        LedgerRow("buy", wallet, cond, 300, seq=3, outcome_index=1, price=0.5, size=5, usdc=2.5, episode_index=0),  # other outcome
    ]
    ledger = WalletEventLedger(wallet, rows=rows)
    ep = PositionEpisode(wallet, cond, "ev1", 0, 0, buy_shares=10, buy_cost=4.0,
                         sell_shares=10, sell_proceeds=6.0, resolved=True, won=1)
    book = PositionBook(wallet, episodes=[ep])
    sl = EdgeSlice("0.40-0.60", "price_band", n_positions=1, n_events=1, wins=1,
                   entry_sum=0.4, pnl=2.0, cost=4.0)
    slice_tables = {"price_band": {"0.40-0.60": sl}}
    cluster = EdgeCluster("cl1", "cheap longshots", {}, "price_band", 1, 1, 2.0, 20.0,
                          member_position_keys=[(cond, 0, 0)])
    mf = MethodFinding("specialist_domain", "descriptive", 1.0, 0.5, 1, n_events=1)
    cr = CapitalRiskProfile(wallet, total_buy_cost=4.0, total_realized_pnl=pnl,
                            total_realized_pnl_with_inventory=pnl, inventory_reconciliation=0.0)
    profile = ThesisProfile(wallet, run_id, "headline", edge_clusters=[cluster], loss_centers=[],
                            reward_share_pct=0.0, capital_risk=cr, method_findings={"cl1": [mf]})
    return profile, book, slice_tables, [cluster], ledger


def _counts(wallet):
    conn = db_store.connect(wallet)
    try:
        return {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                for t in ("wa_runs", "wa_activity", "wa_positions", "wa_slices", "wa_clusters", "wa_findings")}
    finally:
        conn.close()


def test_persist_run_writes_expected_rows():
    profile, book, st, clusters, ledger = _build(W1)
    db_store.persist_run(profile=profile, book=book, slice_tables=st, clusters=clusters, ledger=ledger)
    c = _counts(W1)
    assert c["wa_runs"] == 1
    assert c["wa_activity"] == 4          # all ledger rows
    assert c["wa_positions"] == 1
    assert c["wa_slices"] == 1
    assert c["wa_clusters"] == 1
    assert c["wa_findings"] == 1


def test_idempotent_rerun_same_run_id_no_duplicates():
    args = _build(W1, "run1")
    kw = dict(profile=args[0], book=args[1], slice_tables=args[2], clusters=args[3], ledger=args[4])
    db_store.persist_run(**kw)
    db_store.persist_run(**kw)                                 # same run_id again
    c = _counts(W1)
    assert c["wa_runs"] == 1 and c["wa_activity"] == 4 and c["wa_positions"] == 1


def test_new_run_flips_is_current():
    a = _build(W1, "run1")
    db_store.persist_run(profile=a[0], book=a[1], slice_tables=a[2], clusters=a[3], ledger=a[4])
    b = _build(W1, "run2", pnl=2000.0)
    db_store.persist_run(profile=b[0], book=b[1], slice_tables=b[2], clusters=b[3], ledger=b[4])
    conn = db_store.connect(W1)
    try:
        cur = conn.execute("SELECT run_id FROM wa_runs WHERE is_current=1").fetchall()
        old = conn.execute("SELECT is_current FROM wa_runs WHERE run_id='run1'").fetchone()
    finally:
        conn.close()
    assert len(cur) == 1 and cur[0]["run_id"] == "run2"
    assert old["is_current"] == 0


def test_analysis_state_uses_current_run_cursor():
    a = _build(W1, "run1")
    db_store.persist_run(profile=a[0], book=a[1], slice_tables=a[2], clusters=a[3], ledger=a[4])
    b = _build(W1, "run2", cond="cond2")
    db_store.persist_run(profile=b[0], book=b[1], slice_tables=b[2], clusters=b[3], ledger=b[4])

    st = db_store.analysis_state(W1)

    assert st["wallet"] == W1
    assert st["has_store"] is True
    assert st["run_id"] == "run2"
    assert st["created_ts"] is not None
    assert st["max_activity_ts"] == 300
    assert st["n_activity"] == 4
    assert st["n_positions"] == 1


def test_cross_wallet_isolation():
    a = _build(W1, "rA", cond="cond1")
    db_store.persist_run(profile=a[0], book=a[1], slice_tables=a[2], clusters=a[3], ledger=a[4])
    b = _build(W2, "rB", cond="cond2")
    db_store.persist_run(profile=b[0], book=b[1], slice_tables=b[2], clusters=b[3], ledger=b[4])
    # separate files, and neither sees the other's data
    assert db_store.db_path(W1) != db_store.db_path(W2)
    p1 = db_store.positions_for(W1)
    p2 = db_store.positions_for(W2)
    assert {r["condition_id"] for r in p1} == {"cond1"}
    assert {r["condition_id"] for r in p2} == {"cond2"}


def test_activity_for_position_returns_episode_trades_plus_condition_events():
    a = _build(W1)
    db_store.persist_run(profile=a[0], book=a[1], slice_tables=a[2], clusters=a[3], ledger=a[4])
    act = db_store.activity_for_position(W1, "cond1", 0, 0)
    kinds = sorted(r["kind"] for r in act)
    # buy+sell for (cond1, outcome 0, episode 0) + the condition-level split; the outcome-1 buy excluded
    assert kinds == ["buy", "sell", "split"]
    assert all(not (r["kind"] in ("buy", "sell") and r["outcome_index"] == 1) for r in act)


def test_db_path_rejects_non_wallet_input():
    for junk in ("../../etc/passwd", "0xZZZ", "", "0x" + "a" * 39, "robert'; DROP TABLE wa_runs;--"):
        with pytest.raises(ValueError):
            db_store.db_path(junk)
    assert db_store.has_store("../../etc/passwd") is False


def _build_open(wallet, cond="condO"):
    """A still-OPEN directional episode (unresolved, holding shares) — the case live marks attach to."""
    ledger = WalletEventLedger(wallet, rows=[
        LedgerRow("buy", wallet, cond, 100, seq=0, outcome_index=0, price=0.3, size=100, usdc=30.0, episode_index=0)])
    ep = PositionEpisode(wallet, cond, "evO", 0, 0, outcome="Yes", buy_shares=100, buy_cost=30.0, resolved=False)
    book = PositionBook(wallet, episodes=[ep])
    cluster = EdgeCluster("clO", "open", {}, "price_band", 1, 1, 0.0, 0.0, member_position_keys=[(cond, 0, 0)])
    cr = CapitalRiskProfile(wallet, total_buy_cost=30.0, total_realized_pnl=0.0,
                            total_realized_pnl_with_inventory=0.0, inventory_reconciliation=0.0)
    profile = ThesisProfile(wallet, "runO", "headline", edge_clusters=[cluster], loss_centers=[],
                            reward_share_pct=0.0, capital_risk=cr, method_findings={})
    return profile, book, {}, [cluster], ledger


def test_persist_attaches_marks_to_open_positions():
    a = _build_open(W1)
    marks = {("condO", 0): {"current_price": 0.55, "current_value": 55.0, "unrealized_pnl": 25.0}}
    db_store.persist_run(profile=a[0], book=a[1], slice_tables=a[2], clusters=a[3], ledger=a[4], current_marks=marks)
    conn = db_store.connect(W1)
    row = conn.execute("SELECT outcome, current_price, current_value, unrealized_pnl FROM wa_positions").fetchone()
    conn.close()
    assert row["outcome"] == "Yes"
    assert row["current_price"] == 0.55 and row["current_value"] == 55.0 and row["unrealized_pnl"] == 25.0


def test_marks_not_attached_to_resolved_positions():
    a = _build(W1)                                      # _build's episode is resolved
    marks = {("cond1", 0): {"current_price": 0.9, "current_value": 9.0, "unrealized_pnl": 1.0}}
    db_store.persist_run(profile=a[0], book=a[1], slice_tables=a[2], clusters=a[3], ledger=a[4], current_marks=marks)
    conn = db_store.connect(W1)
    row = conn.execute("SELECT current_price, unrealized_pnl FROM wa_positions WHERE condition_id='cond1'").fetchone()
    conn.close()
    assert row["current_price"] is None and row["unrealized_pnl"] is None   # resolved → no live mark


def test_migrate_adds_new_columns_to_old_schema_db():
    # Simulate a DB written before the outcome/marks columns existed, then connect() and assert the
    # migration ALTERs them in without dropping existing data.
    os.makedirs(db_store.DB_DIR, exist_ok=True)
    path = db_store.db_path(W1)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE wa_positions (run_id TEXT, condition_id TEXT, outcome_index INTEGER, realized_pnl REAL)")
    conn.execute("INSERT INTO wa_positions(run_id, condition_id, outcome_index, realized_pnl) VALUES('r','c',0,5.0)")
    conn.commit(); conn.close()

    migrated = db_store.connect(W1)                     # runs _migrate
    cols = {r[1] for r in migrated.execute("PRAGMA table_info(wa_positions)")}
    row = migrated.execute("SELECT realized_pnl, outcome, current_price FROM wa_positions").fetchone()
    migrated.close()
    assert {"outcome", "current_price", "current_value", "unrealized_pnl"} <= cols
    # the negRisk convert_* columns must also migrate into pre-existing DBs (review-2026-06-17)
    assert {"convert_in_shares", "convert_in_cost", "convert_out_shares",
            "convert_out_cost", "convert_cash"} <= cols
    assert row["realized_pnl"] == 5.0                   # existing data preserved
    assert row["outcome"] is None and row["current_price"] is None   # new columns default NULL


def test_convert_fields_round_trip_and_held_shares():
    """negRisk CONVERSION flow must persist through wa_positions and be readable, and a leg held
    only via conversion must count as still-held (held_shares includes convert). (review-2026-06-17)"""
    wallet, cond = W1, "condC"
    ledger = WalletEventLedger(wallet, rows=[
        LedgerRow("buy", wallet, cond, 100, seq=0, outcome_index=0, price=0.5, size=4, usdc=2.0, episode_index=0)])
    # converted IN 20 YES shares, none sold/merged -> still holding via conversion, unresolved
    ep = PositionEpisode(wallet, cond, "evC", 0, 0, outcome="Yes", buy_shares=0.0, buy_cost=0.0,
                         convert_in_shares=20.0, convert_in_cost=8.0, resolved=False)
    assert ep.held_shares == pytest.approx(20.0)        # property counts converted-in shares
    book = PositionBook(wallet, episodes=[ep])
    cr = CapitalRiskProfile(wallet, total_buy_cost=0.0, total_realized_pnl=0.0,
                            total_realized_pnl_with_inventory=0.0, inventory_reconciliation=0.0)
    profile = ThesisProfile(wallet, "runC", "headline", edge_clusters=[], loss_centers=[],
                            reward_share_pct=0.0, capital_risk=cr, method_findings={})
    db_store.persist_run(profile=profile, book=book, slice_tables={}, clusters=[], ledger=ledger)
    row = db_store.positions_for(wallet)[0]
    assert row["convert_in_shares"] == 20.0 and row["convert_in_cost"] == 8.0
    assert row["convert_out_shares"] == 0.0 and row["convert_cash"] == 0.0
