import sqlite3, json
from sniper.persistence.db_memecoin import ensure_memecoin_tables
from sniper.services.memecoin_signal import CopySignal
from sniper.services.memecoin_features import build_snapshot, write_snapshot

def _sig():
    return CopySignal("solana", "MINTA", "u1", "e1", None, "2026-07-12T10:00:00+00:00",
                      "confirmed", None, 2000.0, 0.001)

def test_build_snapshot_promotes_columns_and_hashes():
    snap = build_snapshot(_sig(), mcap=500000.0, liquidity=8000.0, token_age_s=120.0,
                          consensus_n_60m=1, wallet_trust_tier="A", regime_id=2,
                          entry_latency={"detect_ms": 300},
                          decided_at="2026-07-18T12:00:00+00:00")
    assert snap["mcap"] == 500000.0 and snap["upstream_event_id"] == "e1"
    assert snap["decided_at"] == "2026-07-18T12:00:00+00:00"
    assert snap["snapshot_hash"]                       # deterministic content hash present
    assert json.loads(snap["entry_latency_json"])["detect_ms"] == 300


def test_build_snapshot_keeps_extra_features_in_json_only():
    snap = build_snapshot(
        _sig(), mcap=500000.0, liquidity=8000.0, token_age_s=120.0,
        consensus_n_60m=1, wallet_trust_tier="A", regime_id=2, entry_latency={},
        decided_at="2026-07-18T12:00:00+00:00",
        extra_features={"price_change_m5": -4.0, "dex_market_cap": 450000.0},
    )
    features = json.loads(snap["features_json"])
    assert features["price_change_m5"] == -4.0
    assert features["dex_market_cap"] == 450000.0
    assert snap["mcap"] == 500000.0

def test_write_snapshot_is_idempotent_and_records_action():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    ensure_memecoin_tables(c)
    snap = build_snapshot(_sig(), mcap=5e5, liquidity=8e3, token_age_s=120.0, consensus_n_60m=1,
                          wallet_trust_tier="A", regime_id=2, entry_latency={},
                          decided_at="2026-07-18T12:00:00+00:00")
    write_snapshot(c, snap, action="gate_rejected")
    write_snapshot(c, snap, action="opened")          # second write must not duplicate/crash
    rows = c.execute("SELECT action FROM memecoin_entry_snapshots WHERE upstream_event_id='e1'").fetchall()
    assert len(rows) == 1                              # UNIQUE upstream_event_id holds
    assert rows[0]["action"] == "gate_rejected"        # first write wins (immutable snapshot)
