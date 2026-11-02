"""fwlm-v0 store — schema, §3.1 episode assignment, freeze IO."""
import pytest

from sniper.hyperlive import far_whale_store as S
from sniper.hyperlive.far_whale import EPISODE_GAP_MS, FEE_BPS


@pytest.fixture()
def conn():
    c = S.connect(":memory:")
    S.ensure_schema(c)
    return c


def _cand(liq=2950.0, addr="0xabc", asset="ETH"):
    return {"asset": asset, "whale_address": addr, "liquidation_price": liq,
            "mid_price": 3000.0, "distance_bps": 166.7,
            "direction_to_liquidation": "down", "position_side": "long",
            "liquidation_type": "long_liquidation", "position_notional": 2e6,
            "position_size": 100.0, "spread_bps": 2.0}


def test_schema_has_all_study_tables(conn):
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("hl_far_whale_liq_market_state", "hl_far_whale_liq_rounds",
              "hl_far_whale_liq_position_snapshots", "hl_far_whale_liq_candidates",
              "hl_far_whale_liq_episodes", "hl_far_whale_liq_paper_entries",
              "hl_far_whale_liq_outcomes", "hl_far_whale_liq_controls",
              "hl_far_whale_liq_reports"):
        assert t in names, t


def test_market_state_idempotent_on_asset_ts(conn):
    S.save_market_state(conn, {"ETH": (3000.0, 1e6, 5e8, 2.0)}, ts_ms=1000)
    S.save_market_state(conn, {"ETH": (3000.0, 1e6, 5e8, 2.0)}, ts_ms=1000)
    n = conn.execute("SELECT COUNT(*) FROM hl_far_whale_liq_market_state").fetchone()[0]
    assert n == 1
    assert S.read_market_series(conn, "ETH", 0, 2000) == [(1000, 3000.0)]


def test_assign_episode_same_level_reuses(conn):
    e1, new1 = S.assign_episode(conn, _cand(liq=2950.0), signal_ts_ms=1_000_000)
    e2, new2 = S.assign_episode(conn, _cand(liq=2950.5), signal_ts_ms=1_180_000)  # ~1.7bps drift
    assert new1 is True and new2 is False and e1 == e2


def test_assign_episode_liq_move_over_25bps_starts_new_and_closes_old(conn):
    e1, _ = S.assign_episode(conn, _cand(liq=2950.0), signal_ts_ms=1_000_000)
    moved = 2950.0 * (1 + 30 / 1e4)   # +30 bps
    e2, new2 = S.assign_episode(conn, _cand(liq=moved), signal_ts_ms=1_180_000)
    assert new2 is True and e2 != e1
    old = conn.execute("SELECT status FROM hl_far_whale_liq_episodes WHERE episode_id=?",
                       (e1,)).fetchone()[0]
    assert old == "liq_price_moved"


def test_assign_episode_60min_gap_starts_new(conn):
    e1, _ = S.assign_episode(conn, _cand(), signal_ts_ms=1_000_000)
    e2, new2 = S.assign_episode(conn, _cand(), signal_ts_ms=1_000_000 + 3_600_001)
    assert new2 is True and e2 != e1


def test_assign_episode_gap_exactly_at_threshold_starts_new(conn):
    """§3.1: exits the gate for >= 60 min -> new episode. gap == EPISODE_GAP_MS exactly
    must NOT reuse (strict less-than tie-break)."""
    e1, _ = S.assign_episode(conn, _cand(), signal_ts_ms=1_000_000)
    e2, new2 = S.assign_episode(conn, _cand(), signal_ts_ms=1_000_000 + EPISODE_GAP_MS)
    assert new2 is True and e2 != e1


def test_assign_episode_gap_one_ms_under_threshold_reuses(conn):
    e1, _ = S.assign_episode(conn, _cand(), signal_ts_ms=1_000_000)
    e2, new2 = S.assign_episode(conn, _cand(), signal_ts_ms=1_000_000 + EPISODE_GAP_MS - 1)
    assert new2 is False and e2 == e1


def test_candidate_primary_flag_and_counts(conn):
    eid, _ = S.assign_episode(conn, _cand(), signal_ts_ms=1_000_000)
    S.insert_candidate(conn, _cand(), episode_id=eid, signal_ts_ms=1_000_000,
                       is_primary=1, source_snapshot_hash="h1")
    S.insert_candidate(conn, _cand(), episode_id=eid, signal_ts_ms=1_180_000,
                       is_primary=0, source_snapshot_hash="h2")
    rows = conn.execute("SELECT is_primary, study_version FROM hl_far_whale_liq_candidates"
                        " ORDER BY candidate_id").fetchall()
    assert [r[0] for r in rows] == [1, 0]
    assert all(r[1] == "fwlm-v0" for r in rows)


def test_freeze_roundtrip_and_fail_closed_before_freeze(conn):
    assert S.load_freeze(conn) is None            # no freeze -> capture must not enter
    S.save_report(conn, kind="freeze",
                  payload={"thresholds": {"ETH": 1e6}, "campaign_start_ts_ms": 5_000},
                  ts_ms=5_000)
    fz = S.load_freeze(conn)
    assert fz["thresholds"]["ETH"] == 1e6 and fz["campaign_start_ts_ms"] == 5_000


def test_entry_settle_roundtrip(conn):
    eid, _ = S.assign_episode(conn, _cand(), signal_ts_ms=1_000_000)
    entry_id = S.insert_entry(conn, {
        "candidate_id": 1, "episode_id": eid, "asset": "ETH",
        "direction_to_liquidation": "down", "entry_ts_ms": 1_000_000,
        "entry_price": 3000.0, "paper_notional": 1000.0, "d_bps": 166.7,
        "target_price": 2962.5, "adverse_stop_price": 3025.0,
        "liquidation_price": 2950.0, "spread_bps": 2.0, "is_primary": 1})
    assert S.open_entries(conn)[0]["entry_id"] == entry_id
    S.settle_entry(conn, entry_id, {
        "exit_ts_ms": 1_900_000, "exit_price": 2962.0, "exit_reason": "target_hit",
        "paper_pnl": 1.5, "mfe_bps": 130.0, "mae_bps": -20.0,
        "migrated_toward_liq": 1, "hit_liq_level": 0, "liq_fired": 0,
        "position_persisted": 1, "horizon_ms": 1_800_000})
    assert S.open_entries(conn) == []
    out = conn.execute("SELECT exit_reason, migrated_toward_liq FROM"
                       " hl_far_whale_liq_outcomes WHERE entry_id=?", (entry_id,)).fetchone()
    assert tuple(out) == ("target_hit", 1)


def test_first_candidate_notional_returns_earliest_by_candidate_id(conn):
    eid, _ = S.assign_episode(conn, _cand(), signal_ts_ms=1_000_000)
    assert S.first_candidate_notional(conn, eid) is None    # no candidates yet
    S.insert_candidate(conn, _cand(), episode_id=eid, signal_ts_ms=1_000_000,
                       is_primary=1, source_snapshot_hash="h1")
    later = dict(_cand())
    later["position_notional"] = 5e6
    S.insert_candidate(conn, later, episode_id=eid, signal_ts_ms=1_180_000,
                       is_primary=0, source_snapshot_hash="h2")
    # earliest by candidate_id (2e6 from _cand()), not the later/larger one
    assert S.first_candidate_notional(conn, eid) == 2e6
    assert S.first_candidate_notional(conn, "no-such-episode") is None


def test_episode_status_map_covers_open_and_closed(conn):
    e1, _ = S.assign_episode(conn, _cand(liq=2950.0), signal_ts_ms=1_000_000)
    moved = 2950.0 * (1 + 30 / 1e4)   # +30bps -> closes e1 as liq_price_moved, opens e2
    e2, _ = S.assign_episode(conn, _cand(liq=moved), signal_ts_ms=1_180_000)
    m = S.episode_status_map(conn)
    assert m == {e1: "liq_price_moved", e2: "open"}


def test_entry_and_outcome_carry_fee_assumption(conn):
    """Spec §10/§16: fee must be reported explicitly, not just recoverable via
    study_version -> FEE_BPS. Both the entry and its settled outcome carry fee_assumption
    == the frozen FEE_BPS constant."""
    eid, _ = S.assign_episode(conn, _cand(), signal_ts_ms=1_000_000)
    entry_id = S.insert_entry(conn, {
        "candidate_id": 1, "episode_id": eid, "asset": "ETH",
        "direction_to_liquidation": "down", "entry_ts_ms": 1_000_000,
        "entry_price": 3000.0, "paper_notional": 1000.0, "d_bps": 166.7,
        "target_price": 2962.5, "adverse_stop_price": 3025.0,
        "liquidation_price": 2950.0, "spread_bps": 2.0, "is_primary": 1})
    entry_fee = conn.execute(
        "SELECT fee_assumption FROM hl_far_whale_liq_paper_entries WHERE entry_id=?",
        (entry_id,)).fetchone()[0]
    assert entry_fee == FEE_BPS
    S.settle_entry(conn, entry_id, {
        "exit_ts_ms": 1_900_000, "exit_price": 2962.0, "exit_reason": "target_hit",
        "paper_pnl": 1.5, "mfe_bps": 130.0, "mae_bps": -20.0,
        "migrated_toward_liq": 1, "hit_liq_level": 0, "liq_fired": 0,
        "position_persisted": 1, "horizon_ms": 1_800_000})
    outcome_fee = conn.execute(
        "SELECT fee_assumption FROM hl_far_whale_liq_outcomes WHERE entry_id=?",
        (entry_id,)).fetchone()[0]
    assert outcome_fee == FEE_BPS


def test_sweep_snapshot_rows_reads_ordered_pool(conn):
    S.save_snapshots(conn, 1_000, [{"address": "0xb", "asset": "ETH", "liquidation_price": 2950.0,
                                    "position_notional": 2e6, "szi": 100.0}])
    S.save_snapshots(conn, 500, [{"address": "0xa", "asset": "BTC", "liquidation_price": 60000.0,
                                  "position_notional": 5e6, "szi": 10.0}])
    rows = S.sweep_snapshot_rows(conn, 0, 2_000)
    assert [(r["asset"], r["address"], r["ts_ms"]) for r in rows] == [
        ("BTC", "0xa", 500), ("ETH", "0xb", 1_000)]           # ordered by asset, address, ts
    assert S.sweep_snapshot_rows(conn, 0, 400) == []           # window excludes both
