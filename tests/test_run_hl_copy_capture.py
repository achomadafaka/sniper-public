"""Integration tests: run_hl_copy._cycle populates the fidelity capture columns (Task D)."""
import importlib
import json

from sniper.hyperlive import intel_store as IS


def _mod():
    return importlib.import_module("scripts.run_hl_copy")


def _seed_watch_fallback(conn, wallet="0xw"):
    IS.upsert_screened(conn, wallet, {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.upsert_tracked(conn, wallet, {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.set_copy_target(conn, wallet, active=True, copy_tier="watch_fallback", ts_ms=1)


def test_cycle_captures_entry_fidelity(tmp_path, monkeypatch):
    mod = _mod()
    conn = IS.connect(str(tmp_path / "hl.sqlite"))
    IS.ensure_schema(conn)
    _seed_watch_fallback(conn)
    monkeypatch.setattr(mod, "_marks", lambda: {"ETH": 2000.5})
    holdings = []
    monkeypatch.setattr(mod.HL, "fetch_clearinghouse_state", lambda w: {"assetPositions": holdings})
    monkeypatch.setattr(mod.HL, "fetch_l2_book",
                        lambda coin: {"coin": coin, "mid": 2000.0, "spread_bps": 10.0,
                                      "bids": [{"px": 1999.0, "sz": 10.0}],
                                      "asks": [{"px": 2001.0, "sz": 10.0}]})
    monkeypatch.setattr(mod.HL, "fetch_funding_history", lambda *a, **k: [])

    mod._cycle(conn, now_ms=900_000)            # baseline: target flat
    holdings.append({"position": {"coin": "ETH", "szi": "8", "entryPx": "1999"}})
    opened, _ = mod._cycle(conn, now_ms=1_000_000)   # ETH now a genuine new entry
    assert opened == 1
    r = IS.get_open_copies(conn, target="0xw")[0]
    assert r["target_entry_px"] == 1999.0 and r["target_szi"] == 8.0
    assert r["target_notional"] == round(8 * 1999.0, 2)
    assert r["entry_spread_bps"] == 10.0
    assert r["lag_bps"] is not None and r["lag_bps"] > 0        # we entered above target's fill
    assert json.loads(r["entry_depth_json"])["asks"][0] == [2001.0, 10.0]
    assert r["is_holdout"] == 0
    assert r["source_latency_status"] == "not_instrumented"


def test_cycle_matches_arm_entry_to_source_fills_and_stamps_latency(tmp_path, monkeypatch):
    mod = _mod()
    conn = IS.connect(str(tmp_path / "hl.sqlite"))
    IS.ensure_schema(conn)
    _seed_watch_fallback(conn)
    arm = {"arm_id": "frozen-v1", "targets_json": json.dumps(["0xw"])}
    monkeypatch.setattr(mod, "_marks", lambda: {"kPEPE": 0.01})
    holdings = []
    monkeypatch.setattr(mod.HL, "fetch_clearinghouse_state", lambda w: {"assetPositions": holdings})
    monkeypatch.setattr(mod.HL, "fetch_l2_book", lambda coin: None)
    monkeypatch.setattr(mod.HL, "fetch_funding_history", lambda *a, **k: None)
    capture_clock = iter([1_000_005, 1_000_010, 1_000_020, 1_000_030])
    monkeypatch.setattr(mod, "_now_ms", lambda: next(capture_clock))

    mod._cycle(conn, now_ms=900_000, arm=arm)
    IS.record_copy_source_events(conn, [{
        "wallet": "0xw", "tid": "fill-1", "coin": "kPEPE", "side": "B",
        "direction": "Open Long", "px": 0.0099, "sz": 8.0, "start_position": 0.0,
        "oid": "7", "tx_hash": "0xabc", "source_event_ts_ms": 950_000,
        "received_ts_ms": 960_000, "connection_epoch": 1,
    }])
    holdings.append({"position": {"coin": "kPEPE", "szi": "8", "entryPx": "0.0099"}})

    opened, _ = mod._cycle(conn, now_ms=1_000_000, arm=arm)

    assert opened == 1
    row = dict(conn.execute("SELECT * FROM hl_copy_book").fetchone())
    assert row["source_latency_status"] == "matched"
    assert row["source_event_ts_ms"] == 950_000
    assert row["source_received_ts_ms"] == 960_000
    assert row["decision_ts_ms"] == 1_000_005
    assert row["book_request_ts_ms"] == 1_000_010
    assert row["book_received_ts_ms"] == 1_000_020
    assert row["mirror_open_ts_ms"] == 1_000_030
    assert json.loads(row["source_event_tid"]) == ["fill-1"]
    event = dict(conn.execute("SELECT * FROM hl_copy_source_events").fetchone())
    assert event["match_status"] == "matched"
    assert event["copy_id"] == row["id"]


def test_cycle_censors_exact_source_match_when_queue_gap_overlaps_baseline(tmp_path, monkeypatch):
    mod = _mod()
    conn = IS.connect(str(tmp_path / "hl.sqlite"))
    IS.ensure_schema(conn)
    _seed_watch_fallback(conn)
    arm = {"arm_id": "frozen-v1", "targets_json": json.dumps(["0xw"])}
    holdings = []
    monkeypatch.setattr(mod, "_marks", lambda: {"ETH": 100.0})
    monkeypatch.setattr(mod.HL, "fetch_clearinghouse_state", lambda _w: {"assetPositions": holdings})
    monkeypatch.setattr(mod.HL, "fetch_l2_book", lambda _coin: None)
    monkeypatch.setattr(mod.HL, "fetch_funding_history", lambda *_a, **_k: None)
    monkeypatch.setattr(mod, "_now_ms", lambda: 1_000_000)
    mod._cycle(conn, now_ms=900_000, arm=arm)
    IS.record_copy_source_events(conn, [{
        "wallet": "0xw", "tid": "fill-1", "coin": "ETH", "direction": "Open Long",
        "sz": 2, "source_event_ts_ms": 950_000, "received_ts_ms": 960_000,
        "connection_epoch": 1,
    }])
    IS.record_copy_source_gaps(conn, [{
        "start_received_ts_ms": 940_000, "end_received_ts_ms": 970_000,
        "connection_epoch": 1, "dropped_count": 1, "reason": "queue_overflow",
    }], persisted_ts_ms=980_000)
    holdings.append({"position": {"coin": "ETH", "szi": "2", "entryPx": "100"}})
    mod._cycle(conn, now_ms=1_000_000, arm=arm)
    row = dict(conn.execute("SELECT * FROM hl_copy_book").fetchone())
    assert row["source_latency_status"] == "matched_gap_censored"


def test_cycle_stamps_holdout_after_freeze(tmp_path, monkeypatch):
    mod = _mod()
    conn = IS.connect(str(tmp_path / "hl.sqlite"))
    IS.ensure_schema(conn)
    _seed_watch_fallback(conn)
    IS.set_holdout_freeze(conn, 500_000)
    monkeypatch.setattr(mod, "_marks", lambda: {"ETH": 2000.0})
    holdings = []
    monkeypatch.setattr(mod.HL, "fetch_clearinghouse_state", lambda w: {"assetPositions": holdings})
    monkeypatch.setattr(mod.HL, "fetch_l2_book", lambda coin: None)
    monkeypatch.setattr(mod.HL, "fetch_funding_history", lambda *a, **k: None)

    mod._cycle(conn, now_ms=600_000)                            # baseline (>= freeze, opens nothing)
    holdings.append({"position": {"coin": "ETH", "szi": "8", "entryPx": "1999"}})
    mod._cycle(conn, now_ms=1_000_000)                          # new entry, 1_000_000 >= freeze
    r = IS.get_open_copies(conn, target="0xw")[0]
    assert r["is_holdout"] == 1


def test_cycle_entry_filter_rejects_chase(tmp_path, monkeypatch):
    from sniper.hyperlive.copy_filters import EntryPolicy
    mod = _mod()
    conn = IS.connect(str(tmp_path / "hl.sqlite"))
    IS.ensure_schema(conn)
    _seed_watch_fallback(conn)
    # we detect at 3000 but target filled at 1999 -> huge lag -> reject with max_lag_bps=150
    monkeypatch.setattr(mod, "_marks", lambda: {"ETH": 3000.0})
    holdings = []
    monkeypatch.setattr(mod.HL, "fetch_clearinghouse_state", lambda w: {"assetPositions": holdings})
    monkeypatch.setattr(mod.HL, "fetch_l2_book", lambda coin: None)
    monkeypatch.setattr(mod.HL, "fetch_funding_history", lambda *a, **k: None)

    mod._cycle(conn, now_ms=900_000, policy=EntryPolicy(max_lag_bps=150.0))   # baseline
    holdings.append({"position": {"coin": "ETH", "szi": "8", "entryPx": "1999"}})   # new entry (chase)
    opened, _ = mod._cycle(conn, now_ms=1_000_000, policy=EntryPolicy(max_lag_bps=150.0))
    assert opened == 0                                         # chase filtered out (not baseline)
    assert IS.get_open_copies(conn, target="0xw") == []


def test_cycle_default_policy_opens_normally(tmp_path, monkeypatch):
    mod = _mod()
    conn = IS.connect(str(tmp_path / "hl.sqlite"))
    IS.ensure_schema(conn)
    _seed_watch_fallback(conn)
    monkeypatch.setattr(mod, "_marks", lambda: {"ETH": 3000.0})
    holdings = []
    monkeypatch.setattr(mod.HL, "fetch_clearinghouse_state", lambda w: {"assetPositions": holdings})
    monkeypatch.setattr(mod.HL, "fetch_l2_book", lambda coin: None)
    monkeypatch.setattr(mod.HL, "fetch_funding_history", lambda *a, **k: None)
    mod._cycle(conn, now_ms=900_000)                         # baseline: target flat
    holdings.append({"position": {"coin": "ETH", "szi": "8", "entryPx": "1999"}})
    opened, _ = mod._cycle(conn, now_ms=1_000_000)           # default policy = no filter, ETH new
    assert opened == 1


def test_cycle_close_captures_funding_and_exit(tmp_path, monkeypatch):
    mod = _mod()
    conn = IS.connect(str(tmp_path / "hl.sqlite"))
    IS.ensure_schema(conn)
    IS.upsert_screened(conn, "0xw", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.set_copy_target(conn, "0xw", active=True, ts_ms=1)
    IS.open_copy(conn, "0xw", "ETH", "long", 2000.0, ts_ms=100)
    monkeypatch.setattr(mod, "_marks", lambda: {"ETH": 2040.0})
    monkeypatch.setattr(mod.HL, "fetch_clearinghouse_state", lambda w: {"assetPositions": []})
    monkeypatch.setattr(mod.HL, "fetch_l2_book",
                        lambda coin: {"coin": coin, "mid": 2040.0, "spread_bps": 8.0,
                                      "bids": [{"px": 2039.0, "sz": 10.0}],
                                      "asks": [{"px": 2041.0, "sz": 10.0}]})
    monkeypatch.setattr(mod.HL, "fetch_funding_history",
                        lambda coin, s, e: [{"time": 3_600_000, "rate": 0.0001}])

    _, closed = mod._cycle(conn, now_ms=3_600_001)
    assert closed == 1
    row = dict(conn.execute("SELECT * FROM hl_copy_book WHERE target='0xw'").fetchone())
    assert row["status"] == "closed"
    assert row["funding_bps"] == round(0.0001 * 1e4, 3)        # long pays positive funding
    assert row["funding_pnl_bps"] == -round(0.0001 * 1e4, 3)
    assert row["funding_provenance_status"] == "complete"
    assert row["exit_spread_bps"] == 8.0
    assert json.loads(row["exit_depth_json"])["mid"] == 2040.0
    assert row["mfe_bps"] is not None and row["mfe_bps"] > 0    # excursion tracked before close


def test_cycle_first_sight_opens_nothing_no_cold_start(tmp_path, monkeypatch):
    mod = _mod()
    conn = IS.connect(str(tmp_path / "hl.sqlite"))
    IS.ensure_schema(conn)
    _seed_watch_fallback(conn)
    monkeypatch.setattr(mod, "_marks", lambda: {"ETH": 2000.0})
    monkeypatch.setattr(mod.HL, "fetch_clearinghouse_state",
                        lambda w: {"assetPositions": [{"position": {"coin": "ETH", "szi": "8",
                                                                    "entryPx": "1999"}}]})
    monkeypatch.setattr(mod.HL, "fetch_l2_book", lambda coin: None)
    monkeypatch.setattr(mod.HL, "fetch_funding_history", lambda *a, **k: None)
    opened, _ = mod._cycle(conn, now_ms=1_000_000)
    assert opened == 0                                    # first sight = baseline, NOT cold-start
    assert IS.get_open_copies(conn, target="0xw") == []
    assert IS.seen_copy_trader(conn, "0xw") is True       # baseline recorded


def test_cycle_opens_only_genuinely_new_entry(tmp_path, monkeypatch):
    mod = _mod()
    conn = IS.connect(str(tmp_path / "hl.sqlite"))
    IS.ensure_schema(conn)
    _seed_watch_fallback(conn)
    holdings = [{"position": {"coin": "ETH", "szi": "8", "entryPx": "1999"}}]   # cycle-1 book
    monkeypatch.setattr(mod, "_marks", lambda: {"ETH": 2000.0, "SOL": 150.0})
    monkeypatch.setattr(mod.HL, "fetch_clearinghouse_state", lambda w: {"assetPositions": holdings})
    monkeypatch.setattr(mod.HL, "fetch_l2_book", lambda coin: None)
    monkeypatch.setattr(mod.HL, "fetch_funding_history", lambda *a, **k: None)
    # cycle 1: baseline ETH, opens nothing
    mod._cycle(conn, now_ms=1_000_000)
    # cycle 2: target adds SOL (new) while still holding ETH (pre-existing)
    holdings.append({"position": {"coin": "SOL", "szi": "10", "entryPx": "150"}})
    opened, _ = mod._cycle(conn, now_ms=2_000_000)
    assert opened == 1                                    # only SOL, not the pre-existing ETH
    coins = sorted(o["coin"] for o in IS.get_open_copies(conn, target="0xw"))
    assert coins == ["SOL"]
