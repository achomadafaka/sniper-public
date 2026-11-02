"""Phase 2 Task 7 — exit-state snapshot + candidate-decision persistence (ticks DB)."""
import sqlite3
from datetime import datetime, timezone

from sniper.persistence import db_exit_state as x
from sniper.services.memecoin_exit_market_state import compute_state


def _iso(ep):
    return datetime.fromtimestamp(ep, tz=timezone.utc).isoformat()


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    x.ensure_exit_state_tables(c)
    return c


def _real_state():
    base = 1_700_000_000
    swaps = [{"kind": "v2_swap", "block_no": i,
              "amount0": str((100 + i) if i % 2 else -(90 + i)),
              "amount1": str(-(40 + i) if i % 2 else (50 + i))} for i in range(1, 7)]
    swaps.append({"kind": "v2_sync", "block_no": 6, "amount0": "1000000", "amount1": "2000000"})
    bt = {i: _iso(base + i) for i in range(1, 7)}
    c = {"chain_id": "4663", "mint": "COIN", "pool": "0xp", "pool_kind": "v2",
         "position_id": 7, "quote_side": 0, "entry_price": 1.8, "executable_peak": 2.4,
         "entry_contract_version": 3}
    return compute_state(swaps, bt, c, cfg=None, now_ts=base + 7)


def test_snapshot_upsert_is_idempotent():
    c = _conn()
    s = _real_state()
    assert x.write_market_state(c, s, computed_at="2026-07-23T00:00:07+00:00") == 1
    assert x.write_market_state(c, s, computed_at="2026-07-23T00:00:07+00:00") == 0   # same tick -> no dup
    got = x.latest_state(c, 7)
    assert got["state"] == "trending" and got["position_id"] == 7
    assert got["executable_price"] is None and got["nominal_price"] is not None
    assert got["drawdown"] is None                                   # exec-peak drawdown stays None


def test_decision_row_records_legacy_and_censored():
    c = _conn()
    n = x.write_candidate_decision(c, {
        "position_id": 7, "decided_at": "2026-07-23T00:00:07+00:00", "engine_version": "exit-brain-v1",
        "feature_version": "v1", "candidate_action": "DEFER_TO_LEGACY", "reason_codes": ["source_untrusted"],
        "legacy_action": "hold", "divergent": True, "censored": True,
        "proposed_fraction": 0.5, "executable_confirmed": True, "outcome": "derisked"})
    assert n == 1
    r = c.execute("SELECT legacy_action, divergent, censored, candidate_reason_codes_json, "
                  "proposed_fraction, executable_confirmed, outcome "
                  "FROM memecoin_exit_candidate_decisions WHERE position_id=7").fetchone()
    assert r["legacy_action"] == "hold" and r["divergent"] == 1 and r["censored"] == 1
    assert "source_untrusted" in r["candidate_reason_codes_json"]
    assert r["proposed_fraction"] == 0.5
    assert r["executable_confirmed"] == 1 and r["outcome"] == "derisked"
    # idempotent on (position_id, decided_at, engine_version)
    assert x.write_candidate_decision(c, {
        "position_id": 7, "decided_at": "2026-07-23T00:00:07+00:00", "engine_version": "exit-brain-v1",
        "feature_version": "v1", "candidate_action": "DEFER_TO_LEGACY"}) == 0


def test_latest_state_none_when_empty():
    assert x.latest_state(_conn(), 999) is None


def test_funded_existing_connection_never_creates_or_initializes_missing_db(tmp_path):
    import pytest
    path = tmp_path / "missing-exit-state.db"
    with pytest.raises(sqlite3.OperationalError):
        x.connect_exit_state_existing(path, busy_ms=250)
    assert not path.exists()


def test_snapshot_persists_complete_explainable_json():
    import json
    c = _conn()
    x.write_market_state(c, _real_state(), computed_at="2026-07-23T00:00:07+00:00")
    got = json.loads(x.latest_state(c, 7)["state_json"])
    # the full versioned record — nothing dropped
    assert got["required_families"] and got["entry_contract_version"] == 3
    assert got["buy_n"] == 3 and got["sell_n"] == 3 and got["net_flow_quote"] is not None
    assert got["reserve_quote"] == "1000000"
    assert got["provenance"]["price"]["price_kind"] == "block_vwap_exec_atomic"
    assert got["provenance"]["depth"]["source"] == "v2_sync"


def test_malformed_row_raises_not_swallowed_as_duplicate():
    import sqlite3
    import pytest
    c = _conn()
    s = _real_state()
    object.__setattr__(s, "position_id", None)          # NOT NULL column -> must RAISE, not vanish
    with pytest.raises(sqlite3.IntegrityError):
        x.write_market_state(c, s, computed_at="2026-07-23T00:00:07+00:00")
