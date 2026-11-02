import os

from sniper.hyperlive import persistence as P


def _conn(tmp_path):
    c = P.connect(os.path.join(str(tmp_path), "engine.sqlite"))
    P.ensure_schema(c)
    return c


def test_obs_roundtrip_ascending(tmp_path):
    c = _conn(tmp_path)
    P.save_cascade_obs(c, {"BTC": (100.0, 5e8, 12, 250000.0)}, ts_ms=1000)
    P.save_cascade_obs(c, {"BTC": (101.0, 4.9e8, 30, 900000.0),
                           "ETH": (50.0, 2e8, 5, 40000.0)}, ts_ms=2000)
    btc = P.read_cascade_obs(c, asset="BTC")
    assert [r["ts_ms"] for r in btc] == [1000, 2000]
    assert btc[1]["mid"] == 101.0 and btc[1]["n_trades"] == 30
    assert len(P.read_cascade_obs(c)) == 3
    assert len(P.read_cascade_obs(c, since_ms=1500)) == 2


def test_event_roundtrip_and_version_filter(tmp_path):
    c = _conn(tmp_path)
    ev = {"event_id": "BTC:2000:v1", "asset": "BTC", "ts_ms": 2000, "direction": 1,
          "ret_w": 80.0, "sigma_w": 12.0, "oi_delta_w": -2.0, "trade_count_w": 40,
          "traded_notional_w": 9e5, "magnitude_bucket": "60-120", "detector_version": "v1"}
    P.save_cascade_event(c, ev)
    P.save_cascade_event(c, ev)  # same event_id -> idempotent, still one row
    rows = P.read_cascade_events(c)
    assert len(rows) == 1 and rows[0]["direction"] == 1 and rows[0]["magnitude_bucket"] == "60-120"
    assert P.read_cascade_events(c, detector_version="v1") == rows
    assert P.read_cascade_events(c, detector_version="v2") == []


def test_audit_latest(tmp_path):
    c = _conn(tmp_path)
    assert P.read_latest_cascade_audit(c) is None
    P.save_cascade_audit(c, {"status": "accruing", "n_events": 3}, ts_ms=1000)
    P.save_cascade_audit(c, {"status": "no-edge", "n_events": 60}, ts_ms=2000)
    latest = P.read_latest_cascade_audit(c)
    assert latest["ts_ms"] == 2000 and latest["verdict"]["status"] == "no-edge"


def test_migration_idempotent(tmp_path):
    c = _conn(tmp_path)
    P.ensure_schema(c)  # second call must not raise
    P.save_cascade_obs(c, {"BTC": (100.0, 5e8, 1, 1.0)}, ts_ms=1)
    assert len(P.read_cascade_obs(c)) == 1


def test_obs_idempotent_on_asset_ts(tmp_path):
    # A re-flushed bucket (restart) must not double-count the same (asset, ts_ms).
    c = _conn(tmp_path)
    P.save_cascade_obs(c, {"BTC": (100.0, 5e8, 12, 250000.0)}, ts_ms=1000)
    P.save_cascade_obs(c, {"BTC": (100.0, 5e8, 12, 250000.0)}, ts_ms=1000)   # duplicate flush
    P.save_cascade_obs(c, {"BTC": (101.0, 4.9e8, 30, 900000.0)}, ts_ms=2000)
    btc = P.read_cascade_obs(c, asset="BTC")
    assert [r["ts_ms"] for r in btc] == [1000, 2000]      # the duplicate at 1000 ignored
    assert btc[0]["n_trades"] == 12                        # first write wins (OR IGNORE)
