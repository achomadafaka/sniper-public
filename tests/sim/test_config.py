from sniper.sim.collector.config import CollectorConfig


def test_default_config():
    cfg = CollectorConfig()
    assert cfg.ws_url == "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    assert cfg.raw_dir.endswith("raw")
    assert cfg.venue == "polymarket"
    assert cfg.max_markets == 200
    assert cfg.rotation_seconds == 3600
    assert cfg.reconnect_base_delay == 5.0
    assert cfg.reconnect_max_delay == 300.0
    assert cfg.custom_feature_enabled is True
    assert cfg.snapshot_path.endswith("markets_snapshot.json")
    assert cfg.market_reload_seconds == 60
    assert cfg.app_ping_seconds == 10


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("SIM_RAW_DIR", "/tmp/test_raw")
    monkeypatch.setenv("SIM_MAX_MARKETS", "50")
    monkeypatch.setenv("SIM_MARKETS_SNAPSHOT_PATH", "/tmp/sim-markets.json")
    cfg = CollectorConfig.from_env()
    assert cfg.raw_dir == "/tmp/test_raw"
    assert cfg.max_markets == 50
    assert cfg.snapshot_path == "/tmp/sim-markets.json"
