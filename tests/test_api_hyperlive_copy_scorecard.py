"""Tests for the /api/hyperlive/copy-scorecard endpoint (UI button backend)."""
import os

from flask import Flask

from sniper.hyperlive import intel_store as IS
from sniper.routes import api_hyperlive as APIHL


def _seed(db):
    c = IS.connect(db)
    IS.ensure_schema(c)
    # two closed, one open
    c1 = IS.open_copy(c, "0xa", "ETH", "long", 2000.0, ts_ms=1000)
    IS.close_copy(c, c1, exit_mark=2040.0, pnl_bps=191.0, ts_ms=int(3.6e6))
    c2 = IS.open_copy(c, "0xa", "SOL", "long", 150.0, ts_ms=2000)
    IS.close_copy(c, c2, exit_mark=147.0, pnl_bps=-209.0, ts_ms=int(3.6e6))
    IS.open_copy(c, "0xa", "BTC", "long", 100.0, ts_ms=3000)
    c.close()


def test_payload_shape(tmp_path, monkeypatch):
    db = os.path.join(str(tmp_path), "hl_intel.sqlite")
    _seed(db)
    monkeypatch.setattr(APIHL, "INTEL_DB", db)
    from sniper.markets import hyperliquid_api as HL
    monkeypatch.setattr(HL, "fetch_marks", lambda: {"BTC": 101.0, "ETH": 2000.0, "SOL": 150.0})
    monkeypatch.setattr(HL, "fetch_candles", lambda *a, **k: [{"t": 0, "c": 50000.0},
                                                             {"t": int(4e6), "c": 50500.0}])
    p = APIHL._copy_scorecard_payload()
    assert p["available"] is True
    assert p["realized"]["n"] == 2
    assert p["total_book"]["open_n"] == 1
    assert "decomp" in p and "consensus" in p["decomp"] and "side" in p["decomp"]
    assert "beta_neutral" in p


def test_route_returns_json(tmp_path, monkeypatch):
    db = os.path.join(str(tmp_path), "hl_intel.sqlite")
    _seed(db)
    monkeypatch.setattr(APIHL, "INTEL_DB", db)
    from sniper.markets import hyperliquid_api as HL
    monkeypatch.setattr(HL, "fetch_marks", lambda: {"BTC": 101.0})
    monkeypatch.setattr(HL, "fetch_candles", lambda *a, **k: [])
    app = Flask(__name__)
    app.register_blueprint(APIHL.create_api_hyperlive_blueprint())
    client = app.test_client()
    r = client.get("/api/hyperlive/copy-scorecard")
    assert r.status_code == 200
    data = r.get_json()
    assert data["available"] is True and data["realized"]["n"] == 2
    # holdout filter -> no holdout rows seeded -> unavailable/empty
    r2 = client.get("/api/hyperlive/copy-scorecard?holdout=1")
    assert r2.status_code == 200


def test_unavailable_when_no_db(tmp_path, monkeypatch):
    monkeypatch.setattr(APIHL, "INTEL_DB", os.path.join(str(tmp_path), "missing.sqlite"))
    p = APIHL._copy_scorecard_payload()
    assert p["available"] is False


def test_hl_real_status_block(tmp_path, monkeypatch):
    """Payload carries the real-leg status block (read-only from real_store). The drift
    verdict itself stays sealed until 20 closes — the block is ops visibility only."""
    from sniper.hyperlive import real_store as RS
    db = os.path.join(str(tmp_path), "hl_intel.sqlite")
    _seed(db)
    c = IS.connect(db)
    RS.get_or_create_deployment(c, now_ms=1000)
    RS.record_intent(c, cloid="0xo1", deployment_id="d", copy_id=1, leg="open",
                     coin="ZEC", side="long", requested_usd=15.0, requested_sz=0.1,
                     state="filled", reason=None, now_ms=1)
    RS.record_fill(c, cloid="0xo1", copy_id=1, leg="open", fill_px=100.0, fill_sz=0.1,
                   filled_ts_ms=1)
    RS.record_intent(c, cloid="0xc1", deployment_id="d", copy_id=2, leg="close",
                     coin="SOL", side="long", requested_usd=None, requested_sz=None,
                     state="filled", reason=None, now_ms=2)
    RS.record_fill(c, cloid="0xc1", copy_id=2, leg="close", fill_px=50.0, fill_sz=0.3,
                   filled_ts_ms=2)
    RS.finalize_close(c, cloid="0xc1", realized_usd=1.0, paper_twin_pnl_bps=100.0,
                      real_net_bps=80.0, drift_bps=-20.0)
    c.close()
    monkeypatch.setattr(APIHL, "INTEL_DB", db)
    from sniper.markets import hyperliquid_api as HL
    monkeypatch.setattr(HL, "fetch_marks", lambda: {})
    monkeypatch.setattr(HL, "fetch_candles", lambda *a, **k: [])
    p = APIHL._copy_scorecard_payload()
    st = p["hl_real_status"]
    assert st["open_n"] == 1
    assert st["intents"] == {"filled": 2}
    assert st["closes_n"] == 1 and st["mean_drift_bps"] == -20.0
    assert st["breaker"] is None and st["deployment"]["epoch_ms"] == 1000
    assert st["read_at_n"] == 20                        # sealed-verdict reminder


def test_holdout_consensus_filters_to_holdout_rows(tmp_path, monkeypatch):
    """The consensus decomp must respect holdout=True — a holdout payload that buckets the
    FULL book silently reports in-sample consensus as out-of-sample."""
    db = os.path.join(str(tmp_path), "hl_intel.sqlite")
    c = IS.connect(db)
    IS.ensure_schema(c)
    c1 = IS.open_copy(c, "0xa", "ETH", "long", 2000.0, ts_ms=1000, is_holdout=0)
    IS.close_copy(c, c1, exit_mark=2040.0, pnl_bps=191.0, ts_ms=int(3.6e6))
    c2 = IS.open_copy(c, "0xa", "SOL", "long", 150.0, ts_ms=2000, is_holdout=1)
    IS.close_copy(c, c2, exit_mark=147.0, pnl_bps=-209.0, ts_ms=int(3.6e6))
    c.close()
    monkeypatch.setattr(APIHL, "INTEL_DB", db)
    from sniper.markets import hyperliquid_api as HL
    monkeypatch.setattr(HL, "fetch_marks", lambda: {})
    monkeypatch.setattr(HL, "fetch_candles", lambda *a, **k: [])
    p = APIHL._copy_scorecard_payload(holdout=True)
    assert p["realized"]["n"] == 1
    bucketed = sum(b["n"] for b in p["decomp"]["consensus"].values())
    assert bucketed == 1        # only the holdout close, not the full book
